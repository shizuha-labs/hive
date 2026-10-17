"""Hive source adapter for the company-state fleet section (HIVE-1569)."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone as datetime_timezone
import os
from typing import Any

SCHEMA = "hive.company_state.fleet.v1"
HEALTH_FAMILIES = (
    "provider_healthy",
    "auth_failed",
    "bridge_auth_unavailable",
    "mcp_errors",
    "wedge_errors",
    "empty_turn_wedge",
    "pulse_wedge",
    "capacity_unavailable",
)
COUNT_FAMILIES = {"mcp_errors", "wedge_errors"}


class CompanyStateSourceUnavailable(RuntimeError):
    """A required Hive source failed, so zero would be dishonest."""

    def __init__(self, source: str):
        super().__init__(source)
        self.source = source


def _observed_datetime(snapshot: dict[str, Any]) -> datetime | None:
    raw = snapshot.get("received_at")
    if raw is None:
        raw = snapshot.get("ts")
    if isinstance(raw, datetime):
        value = raw
    else:
        try:
            epoch = float(raw)
        except (TypeError, ValueError):
            return None
        if epoch > 100_000_000_000:
            epoch /= 1000
        try:
            value = datetime.fromtimestamp(epoch, tz=datetime_timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime_timezone.utc)
    return value


def _unknown_family() -> dict[str, Any]:
    return {"known": False, "value": None, "observed_at": None, "source": None}


def _known_family(value: bool | int, observed_at: datetime, source: str) -> dict[str, Any]:
    return {
        "known": True,
        "value": value,
        "observed_at": observed_at.isoformat(),
        "source": source,
    }


def _explicit_value(health: dict[str, Any], family: str):
    if family in health:
        raw = health[family]
    elif family == "provider_healthy" and "provider_unavailable" in health:
        raw = health["provider_unavailable"]
        return (not raw) if isinstance(raw, bool) else None
    elif family == "capacity_unavailable" and "token_pool_unavailable" in health:
        raw = health["token_pool_unavailable"]
    else:
        return None

    if family in COUNT_FAMILIES:
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0:
            return None
        return int(raw)
    return raw if isinstance(raw, bool) else None


def _connect_health(snapshot: dict[str, Any], now: datetime) -> dict[str, dict[str, Any]]:
    observed_at = _observed_datetime(snapshot)
    max_age = int(os.environ.get("COMPANY_STATE_TELEMETRY_FRESH_SECONDS", "300"))
    if observed_at is None or (now - observed_at).total_seconds() > max_age:
        return {}
    health = snapshot.get("health")
    if not isinstance(health, dict):
        health = {}
    # Allow a future bridge to publish the same explicit family keys at the
    # snapshot root without changing this adapter's contract.
    health = {**{key: snapshot[key] for key in HEALTH_FAMILIES if key in snapshot}, **health}
    result = {}
    for family in HEALTH_FAMILIES:
        value = _explicit_value(health, family)
        if value is not None:
            result[family] = _known_family(
                value, observed_at, f"connect.telemetry.health.{family}",
            )
    return result


def _host_provider_health(snapshot, now: datetime) -> dict[str, dict[str, Any]]:
    if snapshot is None:
        return {}
    observed_at = snapshot.received_at
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=datetime_timezone.utc)
    max_age = int(os.environ.get("COMPANY_STATE_HOST_HEALTH_FRESH_SECONDS", "300"))
    if (now - observed_at).total_seconds() > max_age:
        return {}
    health = snapshot.provider_health if isinstance(snapshot.provider_health, dict) else {}
    available = health.get("available")
    quota_ok = health.get("quota_ok")
    in_backoff = health.get("in_backoff")
    explicitly_unhealthy = available is False or quota_ok is False or in_backoff is True
    explicitly_healthy = available is True and quota_ok is True and in_backoff is False
    if not (explicitly_unhealthy or explicitly_healthy):
        return {}
    return {
        "provider_healthy": _known_family(
            explicitly_healthy,
            observed_at,
            "hive.agent_health_snapshot.provider_health",
        )
    }


def _latest_host_snapshots(usernames: set[str]) -> dict[str, Any]:
    """Return the newest explicit host-exporter sample per roster username."""
    from .models import AgentHealthSnapshot

    try:
        rows = (
            AgentHealthSnapshot.objects
            .filter(agent__agent_username__in=usernames)
            .select_related("agent")
            .order_by("agent__agent_username", "-received_at")
        )
        latest = {}
        for row in rows.iterator():
            latest.setdefault(row.agent.agent_username.lower(), row)
        return latest
    except Exception:
        # Host health is optional. Per-family unknown is the honest degraded
        # result; a source failure must never be converted to a zero/false.
        return {}


def _tenant_summary() -> dict[str, Any]:
    from hive_platform.models import TenantSubscription

    try:
        rows = list(
            TenantSubscription.objects.exclude(tenant_org_slug="")
            .values_list("tenant_org_slug", "status")
        )
    except Exception as exc:
        raise CompanyStateSourceUnavailable("tenant_subscriptions") from exc

    all_slugs = {slug for slug, _ in rows}
    by_status_sets: dict[str, set[str]] = {
        status: set() for status, _ in TenantSubscription.Status.choices
    }
    for slug, status in rows:
        by_status_sets.setdefault(status, set()).add(slug)
    by_status = {status: len(slugs) for status, slugs in by_status_sets.items()}
    active = by_status.get(TenantSubscription.Status.ACTIVE, 0)
    trial = by_status.get(TenantSubscription.Status.TRIAL, 0)
    return {
        "total": len(all_slugs),
        "active": active,
        "trial": trial,
        "active_or_trial": len(
            by_status_sets.get(TenantSubscription.Status.ACTIVE, set())
            | by_status_sets.get(TenantSubscription.Status.TRIAL, set())
        ),
        "by_status": by_status,
    }


def build_fleet_aggregate() -> dict[str, Any]:
    """Build one no-silent-zero platform aggregate from Hive-owned sources."""
    from .external_agents import agent_telemetry
    from .lifecycle_fleet import list_lifecycle_agents

    now = datetime.now(tz=datetime_timezone.utc)
    try:
        roster = list_lifecycle_agents(include_disabled=True)
    except Exception as exc:
        raise CompanyStateSourceUnavailable("fleet_roster") from exc
    try:
        telemetry = agent_telemetry()
    except Exception:
        # Health is tri-state: a telemetry read failure makes health unknown;
        # it must not turn a healthy or unhealthy family into a silent value.
        telemetry = {}

    usernames = {
        str(agent.get("agent_username") or agent.get("username") or "").strip().lower()
        for agent in roster
    }
    usernames.discard("")
    host_snapshots = _latest_host_snapshots(usernames)

    entries = []
    for agent in sorted(
        roster,
        key=lambda item: str(item.get("agent_username") or item.get("username") or "").lower(),
    ):
        username = str(agent.get("agent_username") or agent.get("username") or "").strip().lower()
        if not username:
            continue
        families = {family: _unknown_family() for family in HEALTH_FAMILIES}
        connect_values = _connect_health(telemetry.get(username) or {}, now)
        is_native = str(agent.get("lifecycle_plane") or "").lower() == "native"
        candidates = dict(connect_values)
        if not is_native:
            for family, value in _host_provider_health(host_snapshots.get(username), now).items():
                existing = candidates.get(family)
                if existing is None or datetime.fromisoformat(
                    value["observed_at"]
                ) > datetime.fromisoformat(existing["observed_at"]):
                    candidates[family] = value
        families.update(candidates)
        email = str(agent.get("email") or "").strip().lower()
        if not email:
            email = f"{username}@shizuha.com"
        entries.append({
            "username": username,
            "email": email,
            "lifecycle_status": str(agent.get("status") or "").strip().lower(),
            "enabled": bool(agent.get("enabled")),
            **families,
        })

    status_counts = Counter(str(agent.get("status") or "").lower() for agent in roster)
    enabled = sum(agent.get("enabled") is True for agent in roster)
    running = status_counts.get("running", 0)
    stopped = sum(
        agent.get("enabled") is False
        or str(agent.get("status") or "").lower() in {"stopped", "disabled", "offline"}
        for agent in roster
    )
    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA,
        "observed_at": now.isoformat(),
        "scope": "platform_aggregate",
        "tenants": _tenant_summary(),
        "fleet": {"enabled": enabled, "running": running, "stopped": stopped},
        "agents": entries,
    }
