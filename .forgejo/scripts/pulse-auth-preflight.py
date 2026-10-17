#!/usr/bin/env python3
"""Prove Hive's Pulse rollback credential without leaking or orphaning probes."""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Mapping


TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504}
PROBE_SOURCE = "hive_deploy_preflight"
PROBE_TITLE = "[PREFLIGHT][hive-rollback-create-permission]"
# Source-less probes predate stable preflight identity. Keep this as a bounded
# one-time migration list: never run a global Pulse text search in the release
# gate, and never delete a listed item unless its disposable identity matches.
LEGACY_PROBE_REFS = ("HIVE-1372",)


class PreflightFailure(RuntimeError):
    pass


class TransportFailure(RuntimeError):
    pass


class HttpResponse:
    def __init__(self, status: int, body: bytes, headers: Mapping[str, str]):
        self.status = status
        self.body = body
        self.headers = headers


def http_transport(method, url, headers, body, timeout):
    request = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return HttpResponse(
                response.status,
                response.read(),
                dict(response.headers.items()),
            )
    except urllib.error.HTTPError as exc:
        return HttpResponse(exc.code, exc.read(), dict(exc.headers.items()))
    except (OSError, TimeoutError, socket.timeout, urllib.error.URLError) as exc:
        raise TransportFailure(str(exc)) from exc


def request_with_retry(
    stage,
    method,
    url,
    *,
    headers=None,
    payload=None,
    expected=(200,),
    attempts=3,
    transport=http_transport,
    sleeper=time.sleep,
):
    request_headers = dict(headers or {})
    body = None
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
        body = json.dumps(payload, separators=(",", ":")).encode()
    last_detail = "no attempt"
    for attempt in range(1, attempts + 1):
        try:
            response = transport(method, url, request_headers, body, 15)
        except TransportFailure as exc:
            last_detail = f"transport error: {exc}"
        else:
            if response.status in expected:
                return response
            last_detail = (
                f"HTTP {response.status}: "
                f"{response.body[:300].decode(errors='replace')}"
            )
            if response.status not in TRANSIENT_HTTP:
                raise PreflightFailure(f"{stage} failed: {last_detail}")
        if attempt < attempts:
            print(
                f"{stage} transient failure ({last_detail}); "
                f"retrying {attempt + 1}/{attempts}",
                file=sys.stderr,
            )
            sleeper(attempt)
    raise PreflightFailure(f"{stage} failed after {attempts} attempts: {last_detail}")


def parse_json(stage, response):
    try:
        return json.loads(response.body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise PreflightFailure(f"{stage} returned invalid JSON") from exc


def collection_rows(payload):
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("results"), list):
        return payload["results"]
    raise PreflightFailure("Pulse collection response has no result rows")


def auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


def access_token_from_login(payload):
    """Read the canonical ID login envelope, retaining legacy compatibility."""
    if not isinstance(payload, dict):
        return None
    tokens = payload.get("tokens")
    token = tokens.get("access") if isinstance(tokens, dict) else None
    if not token:
        token = payload.get("access")
    return token if isinstance(token, str) and token else None


def find_hive_project_id(base_url, token, *, transport, sleeper):
    response = request_with_retry(
        "Pulse HIVE project visibility",
        "GET",
        f"{base_url}/api/projects/?key=HIVE",
        headers=auth_headers(token),
        transport=transport,
        sleeper=sleeper,
    )
    rows = collection_rows(parse_json("Pulse HIVE project visibility", response))
    matches = [
        row
        for row in rows
        if isinstance(row, dict) and row.get("key") == "HIVE"
    ]
    if len(matches) == 1 and isinstance(matches[0].get("id"), int):
        return matches[0]["id"]
    raise PreflightFailure(
        "Pulse HIVE project visibility was incomplete: "
        f"rows={len(rows)} hive_matches={len(matches)} "
        f"numeric_ids={sum(isinstance(row.get('id'), int) for row in matches)}"
    )


def mint_hive_access(
    id_login,
    pulse_base,
    username,
    password,
    *,
    transport,
    sleeper,
):
    """Mint until ID includes the org claims needed for HIVE visibility."""
    last_detail = "no attempt"
    for attempt in range(1, 6):
        login = request_with_retry(
            "ID login for Pulse preflight",
            "POST",
            id_login,
            payload={"username": username, "password": password},
            transport=transport,
            sleeper=sleeper,
        )
        login_payload = parse_json("ID login for Pulse preflight", login)
        token = access_token_from_login(login_payload)
        if not token:
            last_detail = "ID login response omitted its access token"
        else:
            try:
                project_id = find_hive_project_id(
                    pulse_base,
                    token,
                    transport=transport,
                    sleeper=sleeper,
                )
            except PreflightFailure as exc:
                last_detail = str(exc)
            else:
                return token, project_id
        if attempt < 5:
            # ID deliberately fails closed by minting an empty
            # organization_memberships claim when its bounded Admin lookup
            # times out. A Pulse retry with that same JWT can never recover;
            # discard it and remint so ID re-runs the authoritative lookup.
            print(
                "Fresh ID token lacked usable HIVE visibility "
                f"({last_detail}); reminting {attempt + 1}/5",
                file=sys.stderr,
            )
            sleeper(min(attempt, 3))
    raise PreflightFailure(
        "Fresh ID tokens lacked usable HIVE visibility after 5 mints: "
        f"{last_detail}"
    )


def find_probe_refs(base_url, token, *, transport, sleeper):
    # HIVE-1523: reconcile by SOURCE, not by per-sha source_id. A probe orphaned
    # by an interrupted preflight run for a DIFFERENT release SHA carries a
    # different source_id and was never found by a later run's per-sha lookup,
    # so it survived forever. Every row with source=hive_deploy_preflight is a
    # disposable probe (the preflight is serialized per deploy), so finding and
    # deleting all of them is safe and self-healing.
    query = urllib.parse.urlencode(
        {
            "source": PROBE_SOURCE,
            "permission_hint": "false",
            "page_size": "100",
        }
    )
    response = request_with_retry(
        "Pulse probe recovery lookup",
        "GET",
        f"{base_url}/api/items/?{query}",
        headers=auth_headers(token),
        transport=transport,
        sleeper=sleeper,
    )
    refs = {
        row.get("item_key")
        for row in collection_rows(parse_json("Pulse probe recovery lookup", response))
        if isinstance(row, dict) and row.get("item_key")
    }
    return sorted(refs)


def find_legacy_probe_refs(base_url, token, *, transport, sleeper):
    """Find known source-less probes without an unbounded text search."""
    refs = []
    for ref in LEGACY_PROBE_REFS:
        response = request_with_retry(
            f"Pulse legacy probe recovery lookup {ref}",
            "GET",
            f"{base_url}/api/items/{urllib.parse.quote(ref, safe='')}/",
            headers=auth_headers(token),
            expected=(200, 404),
            attempts=1,
            transport=transport,
            sleeper=sleeper,
        )
        if response.status == 404:
            continue
        row = parse_json(f"Pulse legacy probe recovery lookup {ref}", response)
        if (
            isinstance(row, dict)
            and row.get("item_key") == ref
            and row.get("title") == PROBE_TITLE
            and not row.get("source")
            and not row.get("source_id")
        ):
            refs.append(ref)
        else:
            print(
                f"Pulse legacy probe {ref} no longer matches its disposable "
                "source-less identity; preserving it",
                file=sys.stderr,
            )
    return refs


def cleanup_probe_refs(base_url, token, refs, *, transport, sleeper):
    for ref in sorted(set(refs)):
        request_with_retry(
            f"Pulse probe cleanup {ref}",
            "DELETE",
            f"{base_url}/api/items/{urllib.parse.quote(ref, safe='')}/",
            headers=auth_headers(token),
            expected=(204, 404),
            transport=transport,
            sleeper=sleeper,
        )


def create_or_recover_probe(
    base_url,
    token,
    payload,
    *,
    transport,
    sleeper,
):
    source_id = payload["source_id"]
    # A prior process can die after Pulse commits but before cleanup. Reconcile
    # that stable identity before making a fresh probe.
    cleanup_probe_refs(
        base_url,
        token,
        find_probe_refs(
            base_url, token, transport=transport, sleeper=sleeper
        ),
        transport=transport,
        sleeper=sleeper,
    )

    response = None
    uncertain_detail = ""
    for attempt in range(1, 3):
        try:
            response = transport(
                "POST",
                f"{base_url}/api/items/",
                {
                    **auth_headers(token),
                    "Content-Type": "application/json",
                },
                json.dumps(payload, separators=(",", ":")).encode(),
                15,
            )
        except TransportFailure as exc:
            uncertain_detail = f"transport error: {exc}"
        else:
            if response.status == 201:
                break
            uncertain_detail = (
                f"HTTP {response.status}: "
                f"{response.body[:300].decode(errors='replace')}"
            )
            if response.status not in TRANSIENT_HTTP:
                raise PreflightFailure(
                    f"Pulse permission-probe create failed: {uncertain_detail}"
                )

        # A timeout/5xx can happen after the transaction committed. Recover by
        # stable source identity before deciding whether a second POST is safe.
        recovered = find_probe_refs(
            base_url, token, transport=transport, sleeper=sleeper
        )
        if recovered:
            return recovered
        if attempt < 2:
            print(
                "Pulse permission-probe create had an uncertain result "
                f"({uncertain_detail}); no committed probe found, retrying 2/2",
                file=sys.stderr,
            )
            sleeper(1)
    else:
        raise PreflightFailure(
            "Pulse permission-probe create failed after recovery: "
            f"{uncertain_detail}"
        )

    created = parse_json("Pulse permission-probe create", response)
    returned_ref = created.get("item_key") if isinstance(created, dict) else None
    refs = find_probe_refs(
        base_url, token, transport=transport, sleeper=sleeper
    )
    if returned_ref:
        refs = sorted(set(refs) | {returned_ref})
    if not refs:
        raise PreflightFailure("Pulse permission-probe create returned no item_key")
    return refs


def resolve_pulse_access(env, *, transport, sleeper):
    """Mint per run when ID credentials exist; retain token-only installations.

    A copied access token expires even when the bootstrap credentials are valid.
    Configured login failure must fail closed, never fall back to that snapshot.
    """
    pulse_base = env.get(
        "PULSE_BASE_URL",
        "http://shizuha-pulse.shizuha.svc.cluster.local:8002",
    ).rstrip("/")
    id_login = env.get(
        "ID_LOGIN_URL",
        "http://shizuha-id.shizuha.svc.cluster.local:8001/api/auth/login/",
    )
    token = str(env.get("PULSE_TOKEN") or "").strip()
    username = str(env.get("PULSE_USERNAME") or "")
    password = str(env.get("PULSE_PASSWORD") or "")
    if username and password:
        minted, project_id = mint_hive_access(
            id_login,
            pulse_base,
            username,
            password,
            transport=transport,
            sleeper=sleeper,
        )
        return pulse_base, minted, project_id
    if token:
        project_id = find_hive_project_id(
            pulse_base, token, transport=transport, sleeper=sleeper,
        )
        return pulse_base, token, project_id
    raise PreflightFailure(
        "Pulse preflight credentials are unavailable "
        "(need PULSE_TOKEN or PULSE_USERNAME+PULSE_PASSWORD)"
    )


def run_preflight(env=None, *, transport=http_transport, sleeper=time.sleep):
    env = os.environ if env is None else env
    release_sha = env.get("RELEASE_SHA", "")
    if len(release_sha) != 40 or any(ch not in "0123456789abcdef" for ch in release_sha):
        raise PreflightFailure("Pulse preflight RELEASE_SHA is not a full lowercase SHA")

    pulse_base, token, project_id = resolve_pulse_access(
        env, transport=transport, sleeper=sleeper,
    )

    # Do not retain secrets in the process environment for Pulse calls.
    if env is os.environ:
        os.environ.pop("PULSE_PASSWORD", None)
        os.environ.pop("PULSE_TOKEN", None)

    options = request_with_retry(
        "Pulse item permission discovery",
        "OPTIONS",
        f"{pulse_base}/api/items/",
        headers=auth_headers(token),
        transport=transport,
        sleeper=sleeper,
    )
    allow = next(
        (value for key, value in options.headers.items() if key.lower() == "allow"),
        "",
    )
    if "POST" not in {part.strip().upper() for part in allow.split(",")}:
        raise PreflightFailure("Pulse item permission discovery omitted POST")

    source_id = f"hive-rollback-create-permission-{release_sha}"
    probe_payload = {
        "project": project_id,
        "title": PROBE_TITLE,
        "description": "permission probe; deleted before rollout mutation",
        "mode": "task",
        "priority": "low",
        "workflow_name": "autonomous-bug",
        "assignment_group": "engineering",
        "source": PROBE_SOURCE,
        "source_id": source_id,
    }
    # The old preflight did not stamp source/source_id, so an interrupted
    # create/delete cycle could leave an actionable disposable task forever.
    # Reconcile only rows whose title exactly matches the private probe marker.
    # Legacy probes are historical hygiene only: this release never creates
    # source-less rows, and the versioned source/source_id reconciliation below
    # remains the hard safety gate. A historical lookup must not block an
    # otherwise healthy deployment.
    try:
        legacy_refs = find_legacy_probe_refs(
            pulse_base,
            token,
            transport=transport,
            sleeper=sleeper,
        )
    except PreflightFailure as exc:
        print(
            f"Pulse legacy probe cleanup deferred ({exc}); "
            "continuing with versioned probe safety",
            file=sys.stderr,
        )
    else:
        cleanup_probe_refs(
            pulse_base,
            token,
            legacy_refs,
            transport=transport,
            sleeper=sleeper,
        )
    refs = create_or_recover_probe(
        pulse_base,
        token,
        probe_payload,
        transport=transport,
        sleeper=sleeper,
    )
    cleanup_probe_refs(
        pulse_base,
        token,
        refs,
        transport=transport,
        sleeper=sleeper,
    )
    print("Pulse token auth+HIVE visibility+item-create permission: valid")


def main():
    try:
        run_preflight()
    except PreflightFailure as exc:
        print(f"Pulse auth preflight failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
