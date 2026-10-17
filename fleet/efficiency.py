"""Per-agent work-efficiency metrics for the fleet UI (HIVE-172).

Reads the per-agent productivity gauges that the team-throughput-exporter
publishes to Prometheus (throughput, cycle time, WIP, blocked, overdue,
staleness) plus the agent-health cost gauge, and returns a {email: {...}} map.
One Prometheus query per refresh, cached briefly so list calls don't hammer it.
Fails open (empty map) so the fleet list never breaks if Prometheus is down.
"""
import json
import time
import urllib.parse
import urllib.request
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

_CACHE = {"at": 0.0, "data": {}}
_TTL = 20.0  # seconds

# Prometheus metric -> efficiency key. agent_* come from team-throughput-exporter;
# shizuha_agent_cost_dollars_total comes from agent-health-exporter (label `agent`
# is the short name there, so we also index by short name).
_METRICS = {
    "agent_tasks_completed_24h": "completed_24h",
    "agent_avg_lead_time_seconds": "cycle_seconds",
    "agent_wip_depth": "wip",
    "agent_backlog_depth": "backlog",
    "agent_blocked_depth": "blocked",
    "agent_overdue_tasks": "overdue",
    "agent_oldest_open_item_age_seconds": "oldest_age_seconds",
}


def _prom_url() -> str:
    return getattr(settings, "PROMETHEUS_URL", "").rstrip("/")


def _query(expr: str):
    base = _prom_url()
    if not base:
        return []
    url = f"{base}/api/v1/query?query=" + urllib.parse.quote(expr)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=4) as resp:
        return json.load(resp).get("data", {}).get("result", [])


def fetch_agent_efficiency() -> dict:
    """Return {agent_email: {completed_24h, cycle_seconds, wip, backlog, blocked,
    overdue, oldest_age_seconds, cost_dollars_total}}. Cached for _TTL seconds."""
    now = time.time()
    if now - _CACHE["at"] < _TTL and _CACHE["data"]:
        return _CACHE["data"]
    out: dict = {}
    try:
        names = "|".join(_METRICS)
        for series in _query(f'{{__name__=~"{names}"}}'):
            m = series.get("metric", {})
            email = (m.get("agent") or "").strip().lower()
            if not email or "@" not in email:
                continue
            key = _METRICS.get(m.get("__name__"))
            if not key:
                continue
            try:
                out.setdefault(email, {})[key] = float(series["value"][1])
            except (KeyError, IndexError, ValueError):
                pass
        # cost gauge keyed by short agent name -> map onto <name>@shizuha.com
        for series in _query("shizuha_agent_cost_dollars_total"):
            short = (series.get("metric", {}).get("agent") or "").strip().lower()
            if not short:
                continue
            email = short if "@" in short else f"{short}@shizuha.com"
            try:
                out.setdefault(email, {})["cost_dollars_total"] = float(series["value"][1])
            except (KeyError, IndexError, ValueError):
                pass
    except Exception as exc:
        logger.warning("fetch_agent_efficiency failed: %s", exc)
        return _CACHE["data"] or {}
    _CACHE.update(at=now, data=out)
    return out


def attach_efficiency(agents: list) -> list:
    """Attach an `efficiency` dict to each agent (matched by email). Mutates+returns."""
    eff = fetch_agent_efficiency()
    if not eff:
        return agents
    for a in agents:
        email = (a.get("email") or f"{a.get('agent_username','')}@shizuha.com").strip().lower()
        data = dict(eff.get(email, {}))
        if data.get("completed_24h") is not None:
            done = data["completed_24h"] or 1
            if data.get("cost_dollars_total") is not None:
                data["cost_per_completed_task"] = round(data["cost_dollars_total"] / max(done, 1), 4)
        a["efficiency"] = data
    return agents
