#!/usr/bin/env python3
"""Verify the Hive -> Pulse read-only service identity with a fresh OAuth grant.

CI only reads the existing Secret and calls ID's OAuth endpoint. An administrator
can run --bootstrap once to create/register the identity; ID receives only its
SHA-256 verifier through stdin. No secret or token is logged in either mode.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import subprocess
from urllib import error, parse, request


CLIENT_ID = "hive-pulse-fleet-read"
SCOPE = "pulse:fleet-snapshot.read"
SECRET_NAME = "hive-pulse-fleet-read"
TOKEN_URL = "http://shizuha-id.shizuha.svc.cluster.local:8001/api/oauth/token"
RESULT_PREFIX = "HIVE_PULSE_CLIENT:"


class ProvisioningError(RuntimeError):
    pass


def run_kubectl(args, *, stdin=None, operation="kubectl"):
    result = subprocess.run(
        ["kubectl", *args], input=stdin, text=True, capture_output=True,
        timeout=90, check=False,
    )
    if result.returncode:
        # A failed Secret create may echo the submitted document. Never forward
        # stderr/stdout or CalledProcessError's command representation to CI.
        raise ProvisioningError(f"{operation} failed (exit {result.returncode})")
    return result.stdout


def registration_program(verifier=None):
    """Run with ID's existing models; deployment of a new ID command is not needed."""
    return f'''import hmac
import json
from django.db import transaction
from identity.models import OAuthClient

client_id = {CLIENT_ID!r}
scope = {SCOPE!r}
verifier = {verifier!r}
with transaction.atomic():
    client = OAuthClient.objects.select_for_update().filter(client_id=client_id).first()
    if verifier is None:
        result = "existing" if client else "absent"
    elif client is None:
        OAuthClient.objects.create(
            client_id=client_id, client_name="Hive Pulse Fleet Reader",
            client_secret_hash=verifier, client_type=OAuthClient.CLIENT_TYPE_CONFIDENTIAL,
            is_system=True, grant_types=["client_credentials"],
            response_types=[], redirect_uris=[], scope=scope,
        )
        result = "created"
    else:
        if client.access_revoked_before is not None:
            raise RuntimeError("Hive Pulse client is revoked; refusing to clear revocation")
        if not hmac.compare_digest(client.client_secret_hash or "", verifier):
            raise RuntimeError("Hive Pulse client verifier mismatch; refusing implicit rotation")
        if (client.client_type != OAuthClient.CLIENT_TYPE_CONFIDENTIAL
                or not client.is_system or client.scope != scope
                or client.grant_types != ["client_credentials"]
                or client.response_types or client.redirect_uris):
            raise RuntimeError("Hive Pulse client contract mismatch; refusing to change its grants")
        result = "verified"
print({RESULT_PREFIX!r} + json.dumps({{"state": result}}))
'''


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def exchange_token(values):
    body = parse.urlencode({
        "grant_type": "client_credentials", "client_id": values["client-id"],
        "client_secret": values["client-secret"], "scope": SCOPE,
    }).encode()
    req = request.Request(values["token-url"], data=body, headers={
        "Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json",
    })
    try:
        with request.build_opener(NoRedirect).open(req, timeout=20) as response:
            if response.status != 200:
                raise ProvisioningError("ID OAuth client verification returned a non-success status")
            return json.load(response)
    except error.HTTPError as exc:
        raise ProvisioningError(f"ID OAuth client verification rejected (HTTP {exc.code})") from None
    except (error.URLError, OSError, ValueError):
        raise ProvisioningError("ID OAuth client verification failed or returned invalid JSON") from None


def ensure_client(namespace="shizuha-hive", id_namespace="shizuha", *,
                  bootstrap=False, run=run_kubectl, exchange=exchange_token):
    def read_secret():
        raw = run(
            ["-n", namespace, "get", "secret", SECRET_NAME, "--ignore-not-found", "-o", "json"],
            operation="read Hive Pulse Secret",
        )
        return json.loads(raw) if raw.strip() else None

    def register(verifier=None):
        output = run(
            ["-n", id_namespace, "exec", "-i", "deployment/shizuha-id", "-c", "shizuha-id",
             "--", "/app/.venv/bin/python", "manage.py", "shell", "--command",
             "exec(__import__('sys').stdin.read())"],
            stdin=registration_program(verifier), operation="verify/register ID Hive Pulse client",
        )
        for line in output.splitlines():
            if line.startswith(RESULT_PREFIX):
                return json.loads(line[len(RESULT_PREFIX):])["state"]
        raise ProvisioningError("ID Hive Pulse client verification returned no result")

    resource = read_secret()
    if resource is None:
        if not bootstrap:
            raise ProvisioningError("Hive Pulse Secret missing; administrator must run --bootstrap")
        # A hash cannot recover an existing secret. Do not invent a replacement
        # when ID already holds a verifier (including an emergency-revoked row).
        if register() != "absent":
            raise ProvisioningError("Hive Pulse Secret missing but ID client exists; refusing implicit rotation")
        values = {"client-id": CLIENT_ID, "client-secret": secrets.token_urlsafe(48), "token-url": TOKEN_URL}
        desired = {
            "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
            "metadata": {"name": SECRET_NAME, "namespace": namespace},
            "data": {key: base64.b64encode(value.encode()).decode() for key, value in values.items()},
        }
        try:
            run(["create", "-f", "-"], stdin=json.dumps(desired), operation="create Hive Pulse Secret")
        except ProvisioningError:
            # Concurrent canary/fleet jobs may both observe absence. Re-read the
            # winner, then verify its exact secret; never patch or replace it.
            resource = read_secret()
            if resource is None:
                raise
        else:
            resource = read_secret()

    try:
        data = resource["data"]
        values = {key: base64.b64decode(data[key], validate=True).decode() for key in
                  ("client-id", "client-secret", "token-url")}
    except (KeyError, TypeError, ValueError, UnicodeError) as exc:
        raise ProvisioningError("Hive Pulse Secret has missing or invalid fields") from exc
    if values["client-id"] != CLIENT_ID or values["token-url"] != TOKEN_URL:
        raise ProvisioningError("Hive Pulse Secret client/token endpoint mismatch")
    secret = values["client-secret"]
    if len(secret) < 32 or any(char.isspace() for char in secret):
        raise ProvisioningError("Hive Pulse client secret must contain at least 32 non-whitespace characters")
    if bootstrap:
        verifier = hashlib.sha256(secret.encode()).hexdigest()
        if register(verifier) not in {"created", "verified"}:
            raise ProvisioningError("ID Hive Pulse client registration was not confirmed")
    else:
        result = exchange(values)
        if not isinstance(result, dict):
            raise ProvisioningError("ID OAuth client verification returned an invalid envelope")
        access = result.get("access_token")
        ttl = result.get("expires_in")
        if (not isinstance(access, str) or not access or any(char.isspace() for char in access)
                or str(result.get("token_type", "")).lower() != "bearer"
                or result.get("scope") != SCOPE
                or type(ttl) is not int or not 0 < ttl <= 900
                or result.get("refresh_token")):
            raise ProvisioningError("ID OAuth client verification returned an invalid scope/token/TTL contract")
    return CLIENT_ID


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("namespace", nargs="?", default="shizuha-hive")
    parser.add_argument("id_namespace", nargs="?", default="shizuha")
    parser.add_argument("--bootstrap", action="store_true",
                        help="administrator-only create/register; requires ID pods/exec permission")
    args = parser.parse_args()
    try:
        ensure_client(args.namespace, args.id_namespace, bootstrap=args.bootstrap)
    except (ProvisioningError, subprocess.TimeoutExpired, ValueError) as exc:
        # Our own errors are safe; JSON/parsing/timeout exceptions may retain
        # subprocess input and are deliberately reduced to their class name.
        detail = str(exc) if isinstance(exc, ProvisioningError) else type(exc).__name__
        print(f"::error::Hive Pulse service-client verification: {detail}")
        return 1
    print(f"{CLIENT_ID} verified: confidential client_credentials, scope={SCOPE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
