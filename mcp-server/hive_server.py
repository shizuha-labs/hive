#!/usr/bin/env python3
"""
Shizuha Hive MCP Server (HIVE-MCP-1 skeleton).

This MCP server provides tools for AI agents to interact with Shizuha Hive,
the tenant/Claw provisioning and owner-lifecycle service. This is the
foundational skeleton (FastMCP setup, OAuth wiring, CLI arg parsing,
ShizuhaMCPBase integration) that the concrete Hive tools are built on top of.

Configuration:
- SHIZUHA_HIVE_URL or SHIZUHA_BASE_URL: Base URL for the Hive API.
    With SHIZUHA_BASE_URL the URL resolves to {SHIZUHA_BASE_URL}/hive.
- SHIZUHA_HIVE_JWT_TOKEN: JWT token for authentication. Optional — when unset,
    the per-request Authorization header is used (the standard fleet pattern).

Transport (mirrors the pulse MCP server):
    python hive_server.py --http-port 18107   # streamable HTTP (daemon)
    python hive_server.py                      # stdio (local / inspector)
"""

import argparse
import json
import logging
import os
import sys
from typing import Optional

from mcp.server.fastmcp import FastMCP

# Initialize FastMCP server.
# stateless_http=True (mirrors pulse PLAT-518): Hive MCP may run >1 replica and
# streamable-HTTP sessions are replica-local — stateless mode makes every request
# self-contained so ANY replica serves ANY request. Our tools are request/response,
# so we keep no long-lived server-side session state.
mcp = FastMCP("shizuha-hive", stateless_http=True)

# Wire OAuth: discovery metadata + (when SHIZUHA_MCP_OAUTH_ENFORCE=true)
# transport-level bearer enforcement. Shared with every other Shizuha service
# via base.setup_oauth_routes.
from base import setup_oauth_routes as _setup_oauth_routes  # noqa: E402
from base import ShizuhaMCPBase  # noqa: E402

_setup_oauth_routes(mcp, "hive")
from mcp.server.sse import TransportSecuritySettings  # noqa: E402
mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

# Default streamable-HTTP port for the Hive MCP server (HIVE-MCP-1).
DEFAULT_HTTP_PORT = 18107


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for CLI argument compatibility.

    Mirrors the pulse MCP server: callers that launch the server as a subprocess
    without env vars can pass the token/URL directly.
    """
    parser = argparse.ArgumentParser(description="Shizuha Hive MCP Server")
    parser.add_argument(
        "--jwt-token",
        help="JWT token for authentication (overrides SHIZUHA_HIVE_JWT_TOKEN env var)",
    )
    parser.add_argument(
        "--api-url",
        help="Hive API URL (overrides SHIZUHA_HIVE_URL env var)",
    )
    args, _ = parser.parse_known_args()
    return args


# Parse CLI args (for callers that don't pass env vars to MCP subprocesses).
_args = parse_args()

# Configuration priority: CLI args > environment > defaults.
HIVE_API_URL = (
    _args.api_url
    or os.environ.get("SHIZUHA_HIVE_URL")
    or os.environ.get("HIVE_API_URL")
)
HIVE_JWT_TOKEN = (
    _args.jwt_token
    or os.environ.get("SHIZUHA_HIVE_JWT_TOKEN")
    or os.environ.get("HIVE_JWT_TOKEN")
    or os.environ.get("JWT_TOKEN")
    or ""
)

# Log to stderr (stdout is reserved for the MCP protocol over stdio).
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("shizuha-hive")


# ShizuhaMCPBase provides the shared, per-request-header-aware API client
# (auth header resolution, token refresh, org scoping, api_request). It is
# constructed lazily so importing this module (e.g. for tests) never requires a
# configured API URL — the URL is only needed when a tool actually calls out.
_base: Optional[ShizuhaMCPBase] = None


def get_base() -> ShizuhaMCPBase:
    """Return the shared ShizuhaMCPBase, constructing it on first use."""
    global _base
    if _base is None:
        _base = ShizuhaMCPBase(
            service_name="hive",
            default_port=DEFAULT_HTTP_PORT,
            api_prefix="/api",
            api_url=HIVE_API_URL,
            jwt_token=HIVE_JWT_TOKEN or None,
            request_context_getter=lambda: mcp.get_context(),
        )
    return _base


# ---------------------------------------------------------------------------
# Skeleton tools — prove the FastMCP + OAuth + ShizuhaMCPBase wiring end to end.
# The concrete Hive tools (Claw lifecycle, export/offboarding, Cortex keys, …)
# are added on this seam in the follow-up HIVE-MCP subtasks.
# ---------------------------------------------------------------------------
def _err_text(result: dict) -> str:
    """Full failure text: the HTTP error AND the backend's explanation.

    ``ShizuhaMCPBase.api_request`` already returns ``{'error', 'detail',
    'status_code'}``, but every formatter here used to print only ``error`` —
    i.e. ``"400 Client Error: Bad Request for url: ..."`` — and throw the
    ``detail`` away. That made real, actionable backend validation invisible:
    a model change rejected with ``CREDENTIAL_CAPABILITY_UNAVAILABLE:
    codex.oauth`` was indistinguishable from a malformed request, and could
    only be diagnosed by bypassing MCP entirely (2026-07-27). Pulse's server
    surfaces ``detail``; Hive's must too.
    """
    error = str(result.get("error") or "request failed")
    detail = result.get("detail")
    if not detail:
        return error
    if isinstance(detail, (dict, list)):
        try:
            detail = json.dumps(detail, ensure_ascii=False)
        except (TypeError, ValueError):
            detail = str(detail)
    detail = str(detail).strip()
    if not detail or detail in error:
        return error
    return f"{error} — {detail}"


@mcp.tool(name="hive_health")
def hive_health() -> str:
    """Health check — confirms the Hive MCP server can reach the Hive API."""
    result = get_base().api_request("GET", "health/", authenticated=False)
    if isinstance(result, dict) and result.get("error"):
        return f"Hive API health check failed: {_err_text(result)}"
    return f"Hive API reachable: {result}"


@mcp.tool(name="hive_get_my_identity")
def hive_get_my_identity() -> str:
    """Return the authenticated caller's identity as seen by the Hive API.

    Uses the per-request Authorization bearer (or the configured static token),
    so it doubles as an auth-wiring smoke test.
    """
    result = get_base().api_request("GET", "v1/me/")
    if isinstance(result, dict) and result.get("error"):
        return f"Could not resolve identity: {_err_text(result)}"
    return f"Hive identity: {result}"


# ---------------------------------------------------------------------------
# ADR-0080 (HIVE-2008): Claw is retired as a customer-facing product; FleetAgent
# is the only first-class agent. These tools are RENAMED with a `hive_legacy_`
# prefix to mark the retired Claw surface (per ADR-0080 consequences: "Move or
# rename MCP hive_list_claws etc. once UI is Fleet-only"). Existing Claw rows
# stay readable/stoppable (read/stop/delete only); NO new Claw rows may be
# created — `hive_legacy_create_claw` refuses to mint a row.
# ---------------------------------------------------------------------------
def _claw_result(action: str, result: dict, claw_id: Optional[str] = None) -> str:
    """Format a Claw CRUD API result while preserving backend error text."""
    target = f" claw {claw_id}" if claw_id else " claws"
    if isinstance(result, dict) and result.get("error"):
        return f"Failed to {action}{target}: {_err_text(result)}"
    return f"Hive {action}{target}: {result}"


@mcp.tool(name="hive_legacy_list_claws")
def hive_legacy_list_claws() -> str:
    """List the authenticated caller's legacy Claw rows. GET v1/claws/.

    ADR-0080: Claw is retired; existing rows remain readable. No new Claw rows
    are created from this surface.
    """
    result = get_base().api_request("GET", "v1/claws/")
    return _claw_result("list", result)


@mcp.tool(name="hive_legacy_create_claw")
def hive_legacy_create_claw(
    name: str,
    tier_id: str = "free",
    agent_type: str = "scli",
    model: str = "cortex/grok-4.7",
) -> str:
    """REFUSED — Claw is retired (ADR-0080).

    No new Claw rows may be created; a personal assistant is an isolated
    FleetAgent (`shizuha-{id}`), not a `claws.Claw` row. This tool never
    provisions a Claw and never calls POST v1/claws/.
    """
    return (
        "Claw is retired (ADR-0080): no new Claw rows are created. "
        "Personal agents are FleetAgents (see hive_create_fleet_agent / "
        "the default shizuha-{id} talk seat). Existing Claw rows remain "
        "readable/stoppable via hive_legacy_get_claw / hive_legacy_stop_claw."
    )


@mcp.tool(name="hive_legacy_get_claw")
def hive_legacy_get_claw(claw_id: str) -> str:
    """Return a legacy Claw row's detail. GET v1/claws/{claw_id}/."""
    result = get_base().api_request("GET", f"v1/claws/{claw_id}/")
    return _claw_result("get", result, claw_id)


@mcp.tool(name="hive_legacy_update_claw")
def hive_legacy_update_claw(claw_id: str, name: str) -> str:
    """Rename a legacy Claw row. PATCH v1/claws/{claw_id}/ with {name}."""
    result = get_base().api_request(
        "PATCH",
        f"v1/claws/{claw_id}/",
        data={"name": name},
    )
    return _claw_result("update", result, claw_id)


@mcp.tool(name="hive_legacy_delete_claw")
def hive_legacy_delete_claw(claw_id: str, confirm: str) -> str:
    """Schedule a legacy Claw row for deletion. DELETE v1/claws/{claw_id}/?confirm=<name>."""
    result = get_base().api_request(
        "DELETE",
        f"v1/claws/{claw_id}/",
        params={"confirm": confirm},
    )
    return _claw_result("delete", result, claw_id)


# ---------------------------------------------------------------------------
# HIVE-437 (HIVE-MCP-3): Claw lifecycle tools — thin wrappers over the existing
# owner-authenticated Hive API routes (claws/urls.py). Owner scoping/authz is
# enforced server-side from the per-request bearer; these tools carry only the
# claw_id and surface the API's JSON result or its error verbatim.
# ---------------------------------------------------------------------------
def _claw_action(method: str, path: str, verb: str, claw_id: str) -> str:
    """Shared helper: call a claw-scoped Hive API route and format the result."""
    result = get_base().api_request(method, path)
    if isinstance(result, dict) and result.get("error"):
        return f"Failed to {verb} claw {claw_id}: {_err_text(result)}"
    return f"Claw {claw_id} {verb}: {result}"


@mcp.tool(name="hive_legacy_start_claw")
def hive_legacy_start_claw(claw_id: str) -> str:
    """Start a legacy Claw row (scale its Deployment 0 -> 1). POST v1/claws/{claw_id}/start/."""
    return _claw_action("POST", f"v1/claws/{claw_id}/start/", "start", claw_id)


@mcp.tool(name="hive_legacy_stop_claw")
def hive_legacy_stop_claw(claw_id: str) -> str:
    """Stop a legacy Claw row (scale its Deployment 1 -> 0). POST v1/claws/{claw_id}/stop/."""
    return _claw_action("POST", f"v1/claws/{claw_id}/stop/", "stop", claw_id)


@mcp.tool(name="hive_legacy_undo_delete_claw")
def hive_legacy_undo_delete_claw(claw_id: str) -> str:
    """Undo a legacy Claw deletion within the 60s soft-delete window.
    POST v1/claws/{claw_id}/undo-delete/."""
    return _claw_action("POST", f"v1/claws/{claw_id}/undo-delete/", "undo-delete", claw_id)


@mcp.tool(name="hive_legacy_get_claw_events")
def hive_legacy_get_claw_events(claw_id: str) -> str:
    """Return a legacy Claw row's lifecycle event log (audit trail).
    GET v1/claws/{claw_id}/events/."""
    result = get_base().api_request("GET", f"v1/claws/{claw_id}/events/")
    if isinstance(result, dict) and result.get("error"):
        return f"Could not fetch events for claw {claw_id}: {_err_text(result)}"
    return f"Claw {claw_id} events: {result}"


@mcp.tool(name="hive_legacy_get_claw_status")
def hive_legacy_get_claw_status(claw_id: str) -> str:
    """Return a legacy Claw row's live Kubernetes status (pod/deployment state).
    GET v1/claws/{claw_id}/status/."""
    result = get_base().api_request("GET", f"v1/claws/{claw_id}/status/")
    if isinstance(result, dict) and result.get("error"):
        return f"Could not fetch status for claw {claw_id}: {_err_text(result)}"
    return f"Claw {claw_id} status: {result}"


@mcp.tool(name="hive_legacy_export_claw")
def hive_legacy_export_claw(claw_id: str) -> str:
    """Queue an export job for a legacy Claw row's data (owner-scoped).
    POST v1/owner/claws/{claw_id}/export/."""
    return _claw_action("POST", f"v1/owner/claws/{claw_id}/export/", "export requested", claw_id)


@mcp.tool(name="hive_legacy_hard_delete_claw")
def hive_legacy_hard_delete_claw(claw_id: str) -> str:
    """Queue a legacy Claw row for hard delete / reclaim (owner-scoped,
    irreversible after the reclaim window). POST v1/owner/claws/{claw_id}/hard-delete/."""
    return _claw_action("POST", f"v1/owner/claws/{claw_id}/hard-delete/", "hard-delete queued", claw_id)


@mcp.tool(name="hive_legacy_cancel_delete_claw")
def hive_legacy_cancel_delete_claw(claw_id: str) -> str:
    """Cancel a pending reclaim/hard-delete intent for a legacy Claw row
    (owner-scoped). POST v1/owner/claws/{claw_id}/cancel-delete/."""
    return _claw_action("POST", f"v1/owner/claws/{claw_id}/cancel-delete/", "delete cancelled", claw_id)


# ---------------------------------------------------------------------------
# HIVE-441 (HIVE-MCP-4): Fleet management tools — thin wrappers over the existing
# staff-authenticated /v1/fleet/ routes (fleet/urls.py). Authz (staff/org scope)
# is enforced server-side from the per-request bearer; these tools surface the
# API's JSON result or its error verbatim.
# ---------------------------------------------------------------------------
@mcp.tool(name="hive_list_fleet_agents")
def hive_list_fleet_agents() -> str:
    """List all fleet agents with their status. GET v1/fleet/agents/."""
    result = get_base().api_request("GET", "v1/fleet/agents/")
    if isinstance(result, dict) and result.get("error"):
        return f"Could not list fleet agents: {_err_text(result)}"
    return f"Fleet agents: {result}"


@mcp.tool(name="hive_get_agent_roster")
def hive_get_agent_roster(agent_id: str) -> str:
    """Get one fleet agent's roster detail (model, skills, team, status).
    GET v1/fleet/agents/{agent_id}/."""
    result = get_base().api_request("GET", f"v1/fleet/agents/{agent_id}/")
    if isinstance(result, dict) and result.get("error"):
        return f"Could not fetch agent {agent_id}: {_err_text(result)}"
    return f"Agent {agent_id} roster: {result}"


@mcp.tool(name="hive_list_teams")
def hive_list_teams() -> str:
    """List fleet teams/pods and their rosters. GET v1/fleet/teams/."""
    result = get_base().api_request("GET", "v1/fleet/teams/")
    if isinstance(result, dict) and result.get("error"):
        return f"Could not list teams: {_err_text(result)}"
    return f"Fleet teams: {result}"


@mcp.tool(name="hive_get_team_roster")
def hive_get_team_roster(team_name: str) -> str:
    """Get a team's member roster. GET v1/fleet/teams/{team_name}/members/."""
    result = get_base().api_request("GET", f"v1/fleet/teams/{team_name}/members/")
    if isinstance(result, dict) and result.get("error"):
        return f"Could not fetch team {team_name}: {_err_text(result)}"
    return f"Team {team_name} roster: {result}"


@mcp.tool(name="hive_get_capability_grants")
def hive_get_capability_grants(agent_id: str) -> str:
    """Query the effective capability grants for a fleet agent.
    GET v1/fleet/agents/{agent_id}/effective-capabilities/."""
    result = get_base().api_request("GET", f"v1/fleet/agents/{agent_id}/effective-capabilities/")
    if isinstance(result, dict) and result.get("error"):
        return f"Could not fetch capabilities for agent {agent_id}: {_err_text(result)}"
    return f"Agent {agent_id} capability grants: {result}"


@mcp.tool(name="hive_set_agent_team")
def hive_set_agent_team(
    agent_id: str, team_id: str, action: str = "add",
    role: str = "member", organization_id: int | None = None,
) -> str:
    """Assign/remove a fleet agent's team membership (staff-only).
    POST v1/fleet/agents/{agent_id}/team-placement/. `action` is add|remove|update_role;
    `role` is the membership role for add/update_role."""
    body = {"team_id": team_id, "action": action, "membership_role": role}
    result = get_base().api_request(
        "POST", f"v1/fleet/agents/{agent_id}/team-placement/",
        data=body, organization_id=organization_id,
    )
    if isinstance(result, dict) and result.get("error"):
        return f"Could not {action} agent {agent_id} on team {team_id}: {_err_text(result)}"
    return f"Agent {agent_id} team-placement ({action} → {team_id}): {result}"


@mcp.tool(name="hive_list_agent_templates")
def hive_list_agent_templates(query: str = "", max_results: int = 20) -> str:
    """Search/list fleet agent templates.
    GET v1/fleet/agent-templates/. Staff-only; templates are agent blueprints
    used by Hive fleet creation/configuration flows."""
    params = _compact_body(query=query, max_results=max_results)
    result = get_base().api_request("GET", "v1/fleet/agent-templates/", params=params)
    if isinstance(result, dict) and result.get("error"):
        return f"Could not list agent templates: {_err_text(result)}"
    return f"Hive agent templates: {result}"


@mcp.tool(name="hive_get_agent_template")
def hive_get_agent_template(template: str) -> str:
    """Load a full fleet agent template by slug or name.
    GET v1/fleet/agent-templates/{template}/. Staff-only."""
    result = get_base().api_request("GET", f"v1/fleet/agent-templates/{template}/")
    if isinstance(result, dict) and result.get("error"):
        return f"Could not fetch agent template {template}: {_err_text(result)}"
    return f"Hive agent template {template}: {result}"


def _compact_body(**kwargs) -> dict:
    """Return a request body with only explicitly provided values."""
    return {
        key: value
        for key, value in kwargs.items()
        if value is not None and value != ""
    }


def _fleet_agent_result(action: str, result: dict, agent_id: Optional[str] = None) -> str:
    target = f" agent {agent_id}" if agent_id else " agent"
    if isinstance(result, dict) and result.get("error"):
        return f"Failed to {action}{target}: {_err_text(result)}"
    return f"Hive {action}{target}: {result}"


@mcp.tool(name="hive_create_fleet_agent")
def hive_create_fleet_agent(
    agent_username: str,
    display_name: str = "",
    role: str = "",
    model: str = "",
    image: str = "",
    resource_profile: str = "",
    platform_url: str = "",
) -> str:
    """Create/provision a Hive-managed fleet agent.
    POST v1/fleet/agents/. Staff-only; backend validates the exact fields."""
    result = get_base().api_request(
        "POST",
        "v1/fleet/agents/",
        data=_compact_body(
            agent_username=agent_username,
            display_name=display_name,
            role=role,
            model=model,
            image=image,
            resource_profile=resource_profile,
            platform_url=platform_url,
        ),
    )
    return _fleet_agent_result("create", result)


@mcp.tool(name="hive_update_fleet_agent")
def hive_update_fleet_agent(
    agent_id: str,
    display_name: str = "",
    role: str = "",
    model: str = "",
    execution_method: str = "",
    resource_profile: str = "",
    resource_overrides: Optional[dict] = None,
    platform_url: str = "",
    context_prompt: str = "",
    reasoning_effort: str = "",
    hostplane_ssh: Optional[bool] = None,
) -> str:
    """Update a fleet agent's non-identity configuration.
    PATCH v1/fleet/agents/{agent_id}/. Staff-only.

    execution_method switches the agent's runtime harness (e.g. shizuha,
    codex_app_server, claude_code_server, grok_build, antigravity_server).
    When changing harness, set the corresponding model too (the runtime-lane
    admission rejects model/method mismatches).

    resource_overrides is an authoritative per-agent {requests, limits} spec
    that bypasses the provisioner's profile/bridge defaults (e.g.
    {"limits": {"memory": "8Gi"}} to raise an agent's memory limit).

    hostplane_ssh grants the devops-ssh host-plane seat (fleet SSH key mount
    plus tailnet :22 egress). Never dump the private key."""
    result = get_base().api_request(
        "PATCH",
        f"v1/fleet/agents/{agent_id}/",
        data=_compact_body(
            display_name=display_name,
            role=role,
            model=model,
            execution_method=execution_method,
            resource_profile=resource_profile,
            resource_overrides=resource_overrides,
            platform_url=platform_url,
            context_prompt=context_prompt,
            reasoning_effort=reasoning_effort,
            hostplane_ssh=hostplane_ssh,
        ),
    )
    return _fleet_agent_result("update", result, agent_id)


@mcp.tool(name="hive_delete_fleet_agent")
def hive_delete_fleet_agent(agent_id: str) -> str:
    """Deprovision/delete a Hive-managed fleet agent.
    DELETE v1/fleet/agents/{agent_id}/. Staff-only."""
    result = get_base().api_request("DELETE", f"v1/fleet/agents/{agent_id}/")
    return _fleet_agent_result("delete", result if isinstance(result, dict) else {}, agent_id)


@mcp.tool(name="hive_enable_fleet_agent")
def hive_enable_fleet_agent(agent_id: str) -> str:
    """Enable/start a fleet agent.
    POST v1/fleet/agents/{agent_id}/enable/. Staff-only."""
    result = get_base().api_request("POST", f"v1/fleet/agents/{agent_id}/enable/")
    return _fleet_agent_result("enable", result, agent_id)


@mcp.tool(name="hive_disable_fleet_agent")
def hive_disable_fleet_agent(agent_id: str) -> str:
    """Disable/stop a fleet agent.
    POST v1/fleet/agents/{agent_id}/disable/. Staff-only."""
    result = get_base().api_request("POST", f"v1/fleet/agents/{agent_id}/disable/")
    return _fleet_agent_result("disable", result, agent_id)


@mcp.tool(name="hive_restart_fleet_agent")
def hive_restart_fleet_agent(agent_id: str) -> str:
    """Restart a runtime fleet agent when supported by Hive.
    POST v1/fleet/agents/{agent_id}/restart/. Staff-only."""
    result = get_base().api_request("POST", f"v1/fleet/agents/{agent_id}/restart/")
    return _fleet_agent_result("restart", result, agent_id)


@mcp.tool(name="hive_reset_agent_runtime_session")
def hive_reset_agent_runtime_session(agent_id: str, reason: str = "") -> str:
    """Clear an agent's durable runtime session when supported by Hive.

    This is a lifecycle-control operation for recovery from wedged/detached
    sessions. The backend enforces staff/control authorization and currently
    supports rt-fleet/docker agents through the daemon control proxy.
    POST v1/fleet/agents/{agent_id}/reset-session/. Staff-only.
    """
    result = get_base().api_request(
        "POST",
        f"v1/fleet/agents/{agent_id}/reset-session/",
        data=_compact_body(reason=reason),
    )
    return _fleet_agent_result("reset runtime session for", result, agent_id)


def _credential_result(action: str, result: dict, credential_id: Optional[str] = None) -> str:
    target = f" credential {credential_id}" if credential_id else " credentials"
    if isinstance(result, dict) and result.get("error"):
        return f"Failed to {action}{target}: {_err_text(result)}"
    return f"Hive {action}{target}: {result}"


@mcp.tool(name="hive_list_credentials")
def hive_list_credentials(
    scope: str = "",
    agent_id: str = "",
    team_slug: str = "",
    provider: str = "",
    purpose: str = "",
    is_active: str = "",
) -> str:
    """List redacted Hive credential grants. GET v1/fleet/credentials/."""
    params = _compact_body(
        scope=scope,
        agent=agent_id,
        team_slug=team_slug,
        provider=provider,
        purpose=purpose,
        is_active=is_active,
    )
    result = get_base().api_request("GET", "v1/fleet/credentials/", params=params)
    return _credential_result("list", result)


@mcp.tool(name="hive_create_credential")
def hive_create_credential(
    scope: str,
    secret_ref: str,
    agent_id: str = "",
    team_slug: str = "",
    provider: str = "",
    purpose: str = "",
    fingerprint: str = "",
) -> str:
    """Create a redacted Hive credential grant.
    POST v1/fleet/credentials/. secret_ref must be a broker/vault/k8s handle,
    never plaintext secret material."""
    result = get_base().api_request(
        "POST",
        "v1/fleet/credentials/",
        data=_compact_body(
            scope=scope,
            secret_ref=secret_ref,
            agent=agent_id,
            team_slug=team_slug,
            provider=provider,
            purpose=purpose,
            fingerprint=fingerprint,
        ),
    )
    return _credential_result("create", result)


@mcp.tool(name="hive_upsert_self_credential")
def hive_upsert_self_credential(
    service: str,
    secret_ref: str,
    agent_id: str = "",
    fingerprint: str = "",
) -> str:
    """Upsert the caller agent's own credential handle.

    POST v1/fleet/credentials/self-upsert/. The backend resolves self from the
    bearer token for agent calls; staff/operator calls may pass agent_id for
    repair. secret_ref must be a broker/vault/k8s handle, never plaintext.
    """
    result = get_base().api_request(
        "POST",
        "v1/fleet/credentials/self-upsert/",
        data=_compact_body(
            service=service,
            secret_ref=secret_ref,
            agent_id=agent_id,
            fingerprint=fingerprint,
        ),
    )
    return _credential_result("self-upsert", result)


@mcp.tool(name="hive_request_credential")
def hive_request_credential(scope: str, reason: str, expiry: str = "", agent_id: str = "") -> str:
    """Request a credential grant for the caller agent.

    POST v1/fleet/credential-requests/. Agent calls resolve self from bearer
    identity; staff/operator calls may pass agent_id for repair/simulation.
    """
    result = get_base().api_request(
        "POST",
        "v1/fleet/credential-requests/",
        data=_compact_body(scope=scope, reason=reason, expiry=expiry, agent_id=agent_id),
    )
    return _credential_result("request", result)


@mcp.tool(name="hive_list_credential_requests")
def hive_list_credential_requests(
    status: str = "",
    scope: str = "",
    agent_id: str = "",
) -> str:
    """List redacted Hive credential requests.

    Staff callers can list/filter all requests. Agent callers see only their own
    requests.
    """
    result = get_base().api_request(
        "GET",
        "v1/fleet/credential-requests/",
        params=_compact_body(status=status, scope=scope, agent=agent_id),
    )
    return _credential_result("list credential requests", result)


@mcp.tool(name="hive_deny_credential_request")
def hive_deny_credential_request(request_id: str, reason: str) -> str:
    """Deny a pending Hive credential request. Staff/control-plane only."""
    result = get_base().api_request(
        "POST",
        f"v1/fleet/credential-requests/{request_id}/deny/",
        data={"reason": reason},
    )
    return _credential_result("deny credential request", result, request_id)


@mcp.tool(name="hive_expire_credential_requests")
def hive_expire_credential_requests(scope: str = "") -> str:
    """Expire pending credential requests whose requested expiry is in the past."""
    result = get_base().api_request(
        "POST",
        "v1/fleet/credential-requests/expire/",
        data=_compact_body(scope=scope),
    )
    return _credential_result("expire credential requests", result)


@mcp.tool(name="hive_get_credential")
def hive_get_credential(credential_id: str) -> str:
    """Get one redacted Hive credential grant.
    GET v1/fleet/credentials/{credential_id}/."""
    result = get_base().api_request("GET", f"v1/fleet/credentials/{credential_id}/")
    return _credential_result("get", result, credential_id)


@mcp.tool(name="hive_update_credential")
def hive_update_credential(
    credential_id: str,
    scope: str = "",
    secret_ref: str = "",
    agent_id: str = "",
    team_slug: str = "",
    provider: str = "",
    purpose: str = "",
    fingerprint: str = "",
    is_active: bool | None = None,
) -> str:
    """Update a redacted Hive credential grant.
    PATCH v1/fleet/credentials/{credential_id}/."""
    result = get_base().api_request(
        "PATCH",
        f"v1/fleet/credentials/{credential_id}/",
        data=_compact_body(
            scope=scope,
            secret_ref=secret_ref,
            agent=agent_id,
            team_slug=team_slug,
            provider=provider,
            purpose=purpose,
            fingerprint=fingerprint,
            is_active=is_active,
        ),
    )
    return _credential_result("update", result, credential_id)


@mcp.tool(name="hive_revoke_credential")
def hive_revoke_credential(credential_id: str) -> str:
    """Revoke/deactivate a Hive credential grant.
    DELETE v1/fleet/credentials/{credential_id}/."""
    result = get_base().api_request("DELETE", f"v1/fleet/credentials/{credential_id}/")
    return _credential_result("revoke", result if isinstance(result, dict) else {}, credential_id)


@mcp.tool(name="hive_get_credential_audit")
def hive_get_credential_audit(credential_id: str) -> str:
    """Return redacted audit events for a Hive credential grant.
    GET v1/fleet/credentials/{credential_id}/audit/."""
    result = get_base().api_request("GET", f"v1/fleet/credentials/{credential_id}/audit/")
    return _credential_result("audit", result, credential_id)


def main() -> None:
    """Run the MCP server (transport selected by CLI flag, mirrors pulse)."""
    if not HIVE_JWT_TOKEN:
        logger.info(
            "SHIZUHA_HIVE_JWT_TOKEN not set; expecting Authorization header per request"
        )
    logger.info("Starting Shizuha Hive MCP server")
    logger.info(f"API URL: {HIVE_API_URL}")

    if "--http-port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--http-port") + 1])
        host = "0.0.0.0"
        logger.info(f"Streamable HTTP transport on {host}:{port}")
        mcp.settings.host = host
        mcp.settings.port = port
        # Internal service behind cluster networking — accept any Host header.
        mcp.settings.transport_security = None
        # HIVE-558: stateless_http is a FastMCP CONSTRUCTOR kwarg (set at line
        # ~34), NOT a run() kwarg — passing it here TypeErrors on the installed
        # mcp SDK and crashlooped every shizuha-hive-mcp pod at boot.
        mcp.run(transport="streamable-http")
    elif "--sse-port" in sys.argv:
        port = int(sys.argv[sys.argv.index("--sse-port") + 1])
        logger.info(f"SSE transport on 0.0.0.0:{port} (deprecated, use --http-port)")
        mcp.settings.host = "0.0.0.0"
        mcp.settings.port = port
        mcp.settings.transport_security = None
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
