"""HiveCapabilityEvidenceV2 producer (HIVE-1153 / PLS-277 v18 §5.2).

Pulse supplies an exact seat and policy request.  Those values are assertions:
the grant is derived only from Hive's fresh roster/materialization projection,
then the complete result is bound into a five-second Ed25519 compact JWS.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from django.conf import settings
from django.utils import timezone

from fleet.effective_capabilities import compute_effective_capability_grant_for_memberships
from fleet.models import CapabilityEvidenceProducerGeneration, FleetAgent

REQUEST_SCHEMA = 'pulse.capability-evidence-request.v1'
EVIDENCE_SCHEMA = 'hive.capability-evidence.v2'
EVIDENCE_ISSUER = 'shizuha-hive'
EVIDENCE_AUDIENCE = 'shizuha-pulse'
EVIDENCE_TYP = 'hive-capability-evidence+jwt'
MAX_LIFETIME_SECONDS = 5
MATERIALIZER_VERSION = 'hive-capability-evidence-v2'

_REQUEST_FIELDS = frozenset({
    'schema_version', 'request_id', 'email', 'organization_id', 'team_slug',
    'requirement_key', 'required_capabilities', 'item_id',
    'assignment_generation',
})
_CAPABILITY_RE = re.compile(r'^[a-z0-9][a-z0-9:+.-]{0,79}$')
_TEAM_RE = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$')
_EMAIL_RE = re.compile(r'^[a-z0-9.!#$%&\'*+/=?^_`{|}~-]+@[a-z0-9.-]+$')


class CapabilityEvidenceError(Exception):
    def __init__(self, code: str, *, http_status: int = 400, detail: str = ''):
        self.code = code
        self.http_status = http_status
        self.detail = detail
        super().__init__(f'{code}: {detail}' if detail else code)

    def as_dict(self):
        payload = {'error': self.code}
        if self.detail:
            payload['detail'] = self.detail
        return payload


@dataclass(frozen=True)
class SigningIdentity:
    private_key: Ed25519PrivateKey
    kid: str
    public_jwk: dict
    thumbprint: str


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode('ascii')


def canonical_json_bytes(value) -> bytes:
    _reject_floats(value)
    return json.dumps(
        value, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
    ).encode('utf-8')


def _sha256(raw: bytes) -> str:
    return f'sha256:{hashlib.sha256(raw).hexdigest()}'


def _reject_floats(value):
    if isinstance(value, float):
        raise CapabilityEvidenceError('HIVE_EVIDENCE_FLOAT_NOT_ALLOWED')
    if isinstance(value, dict):
        for item in value.values():
            _reject_floats(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_floats(item)


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise CapabilityEvidenceError('HIVE_EVIDENCE_DUPLICATE_JSON_KEY', detail=str(key))
        obj[key] = value
    return obj


def parse_request(raw: bytes) -> dict:
    """Parse and validate the exact canonical Pulse request bytes."""
    if not raw or len(raw) > 16 * 1024:
        raise CapabilityEvidenceError('HIVE_EVIDENCE_REQUEST_SIZE_INVALID')
    try:
        text = raw.decode('utf-8', errors='strict')
        request = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_float=lambda value: (_ for _ in ()).throw(
                CapabilityEvidenceError('HIVE_EVIDENCE_FLOAT_NOT_ALLOWED', detail=value)
            ),
            parse_constant=lambda value: (_ for _ in ()).throw(
                CapabilityEvidenceError('HIVE_EVIDENCE_NUMBER_NOT_FINITE', detail=value)
            ),
        )
    except CapabilityEvidenceError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapabilityEvidenceError('HIVE_EVIDENCE_REQUEST_MALFORMED', detail=str(exc)) from exc
    if not isinstance(request, dict):
        raise CapabilityEvidenceError('HIVE_EVIDENCE_REQUEST_NOT_OBJECT')
    if set(request) != _REQUEST_FIELDS:
        raise CapabilityEvidenceError('HIVE_EVIDENCE_REQUEST_FIELDS_MISMATCH')
    if canonical_json_bytes(request) != raw:
        raise CapabilityEvidenceError('HIVE_EVIDENCE_REQUEST_NOT_CANONICAL')

    if request['schema_version'] != REQUEST_SCHEMA:
        raise CapabilityEvidenceError('HIVE_EVIDENCE_REQUEST_SCHEMA_UNSUPPORTED')
    try:
        parsed_id = uuid.UUID(str(request['request_id']))
    except (ValueError, TypeError, AttributeError) as exc:
        raise CapabilityEvidenceError('HIVE_EVIDENCE_REQUEST_ID_INVALID') from exc
    if str(parsed_id) != request['request_id']:
        raise CapabilityEvidenceError('HIVE_EVIDENCE_REQUEST_ID_NOT_CANONICAL')

    email = request['email']
    if not isinstance(email, str) or email != email.strip().lower() or not _EMAIL_RE.fullmatch(email):
        raise CapabilityEvidenceError('HIVE_EVIDENCE_SUBJECT_INVALID')
    team = request['team_slug']
    if not isinstance(team, str) or not _TEAM_RE.fullmatch(team):
        raise CapabilityEvidenceError('HIVE_EVIDENCE_TEAM_INVALID')
    requirement_key = request['requirement_key']
    if not isinstance(requirement_key, str) or not requirement_key or len(requirement_key) > 512:
        raise CapabilityEvidenceError('HIVE_EVIDENCE_REQUIREMENT_KEY_INVALID')

    for field, minimum in (('organization_id', 1), ('item_id', 1), ('assignment_generation', 0)):
        value = request[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise CapabilityEvidenceError(f'HIVE_EVIDENCE_{field.upper()}_INVALID')

    capabilities = request['required_capabilities']
    if not isinstance(capabilities, list) or len(capabilities) > 32:
        raise CapabilityEvidenceError('HIVE_EVIDENCE_CAPABILITIES_INVALID')
    if any(not isinstance(cap, str) or not _CAPABILITY_RE.fullmatch(cap) for cap in capabilities):
        raise CapabilityEvidenceError('HIVE_EVIDENCE_CAPABILITY_INVALID')
    if capabilities != sorted(set(capabilities)):
        raise CapabilityEvidenceError('HIVE_EVIDENCE_CAPABILITIES_NOT_SORTED_UNIQUE')
    return request


def request_sha256(request: dict) -> str:
    return _sha256(canonical_json_bytes(request))


def load_signing_identity(private_pem: str | bytes | None = None, kid: str | None = None) -> SigningIdentity:
    private_pem = private_pem if private_pem is not None else getattr(
        settings, 'HIVE_CAPABILITY_EVIDENCE_PRIVATE_KEY_PEM', '',
    )
    kid = str(kid if kid is not None else getattr(
        settings, 'HIVE_CAPABILITY_EVIDENCE_KID', '',
    )).strip()
    if not private_pem or not kid:
        raise CapabilityEvidenceError('HIVE_EVIDENCE_SIGNER_NOT_CONFIGURED', http_status=503)
    raw_pem = private_pem.encode('utf-8') if isinstance(private_pem, str) else private_pem
    try:
        key = serialization.load_pem_private_key(raw_pem, password=None)
    except (TypeError, ValueError) as exc:
        raise CapabilityEvidenceError('HIVE_EVIDENCE_SIGNER_INVALID', http_status=503) from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise CapabilityEvidenceError('HIVE_EVIDENCE_SIGNER_NOT_ED25519', http_status=503)
    public_raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    public_jwk = {
        'alg': 'EdDSA', 'crv': 'Ed25519', 'kid': kid, 'kty': 'OKP',
        'use': 'sig', 'x': _b64url(public_raw),
    }
    thumbprint_body = {
        'crv': public_jwk['crv'], 'kty': public_jwk['kty'], 'x': public_jwk['x'],
    }
    return SigningIdentity(
        private_key=key,
        kid=kid,
        public_jwk=public_jwk,
        thumbprint=_sha256(canonical_json_bytes(thumbprint_body)),
    )


def schema_digest() -> str:
    contract = {
        'request_schema': REQUEST_SCHEMA,
        'request_fields': sorted(_REQUEST_FIELDS),
        'evidence_schema': EVIDENCE_SCHEMA,
        'header': {'alg': 'EdDSA', 'typ': EVIDENCE_TYP, 'kid': 'non-empty'},
        'issuer': EVIDENCE_ISSUER,
        'audience': EVIDENCE_AUDIENCE,
        'max_lifetime_seconds': MAX_LIFETIME_SECONDS,
        'results': ['ABSENT', 'PRESENT'],
    }
    return _sha256(canonical_json_bytes(contract))


def materializer_digest() -> str:
    # The materializer is a call graph, not this adapter alone. Bind every
    # source file that can change roster projection or effective-grant output so
    # a deploy cannot keep claiming the previous generation after an imported
    # authorization rule changed.
    source_root = Path(__file__).resolve().parent
    files = (
        source_root / 'capability_evidence.py',
        source_root / 'effective_capabilities.py',
        source_root / 'models.py',
        source_root / 'views.py',
    )
    manifest = {
        path.name: _sha256(path.read_bytes())
        for path in files
    }
    return _sha256(canonical_json_bytes(manifest))


def config_digest() -> str:
    config = {
        'service_account': str(getattr(settings, 'HIVE_CAPABILITY_EVIDENCE_SERVICE_ACCOUNT', 'hive-api')),
        'roster_max_age_seconds': int(getattr(settings, 'HIVE_CAPABILITY_EVIDENCE_ROSTER_MAX_AGE_SECONDS', 90)),
        'issuer': EVIDENCE_ISSUER,
        'audience': EVIDENCE_AUDIENCE,
        'typ': EVIDENCE_TYP,
        'lifetime_seconds': MAX_LIFETIME_SECONDS,
    }
    return _sha256(canonical_json_bytes(config))


def _org_id(row) -> int | None:
    nested = row.get('organization') if isinstance(row.get('organization'), dict) else {}
    value = row.get('organization_id') or row.get('org_id') or nested.get('id')
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fresh_memberships(email: str):
    """Return Hive's fresh projected roster rows; never authorize from 24h LKG."""
    from fleet import views as fleet_views

    rows, built_at = fleet_views._roster_process_entry_read(email)
    shared_rows, shared_built_at = fleet_views._roster_membership_cache_read(email)
    if shared_rows is not None and (rows is None or shared_built_at > built_at):
        rows, built_at = shared_rows, shared_built_at
        fleet_views._roster_process_membership_write(email, rows, built_at)
    # A fresh replica has no process snapshot, and per-subject Redis entries are
    # populated only by subject-specific mutations/daemon reads. The shared
    # full-roster snapshot is the same authoritative Pulse projection and is
    # refreshed by normal fleet reads. Consult it as a replica-safe source too,
    # otherwise every rolling deploy intermittently returns 503 until each new
    # process happens to warm every requested subject independently.
    shared_index, shared_index_built_at = fleet_views._roster_cache_read()
    if (
        shared_index is not None
        and (rows is None or shared_index_built_at > built_at)
    ):
        rows = list(shared_index.get(str(email or '').strip().lower(), []))
        built_at = shared_index_built_at
        fleet_views._roster_process_membership_write(email, rows, built_at)
    max_age = int(getattr(settings, 'HIVE_CAPABILITY_EVIDENCE_ROSTER_MAX_AGE_SECONDS', 90))
    age = time.time() - float(built_at or 0)
    # PLAT-8592: a fresh replica with zero staff fleet reads has all three
    # sources cold — the shared full-roster snapshot decays when no
    # agents-index request lands in the window, and every ingestion then fails
    # closed (73-89 × 503/min observed) until a staff read warms it. Before
    # failing, project the subject from Pulse's NETWORK-SECURED service roster
    # (no user token, ≤30s cache — the same authoritative projection the
    # codebase already serves to non-staff owners, HIVE-617, only leaner:
    # role=member, org name=slug). Absence of the subject there is still a
    # valid authoritative answer and keeps the 503.
    if rows is None or built_at <= 0 or age > max_age:
        service_rows = fleet_views._service_pulse_roster_index().get(
            str(email or '').strip().lower(),
        )
        if service_rows:
            rows = list(service_rows)
            built_at = time.time()
            age = 0
            fleet_views._roster_process_membership_write(email, rows, built_at)
            fleet_views._roster_membership_cache_write(email, rows)
    if rows is None or built_at <= 0 or age > max_age:
        raise CapabilityEvidenceError('HIVE_MATERIALIZATION_ROSTER_STALE', http_status=503)
    return list(rows), max(0, int(age)), float(built_at)


def _subject_identity(email: str):
    username = email.split('@', 1)[0]
    row = (
        FleetAgent.objects.exclude(status=FleetAgent.Status.DELETED)
        .filter(agent_username=username).first()
    )
    if row is not None:
        canonical_email = f'{row.agent_username}@shizuha.com'
        if canonical_email != email:
            raise CapabilityEvidenceError('HIVE_EVIDENCE_SUBJECT_MISMATCH', http_status=409)
        return {
            'kind': 'fleetagent', 'pk': str(row.pk), 'agent_id': row.agent_id,
            'username': row.agent_username, 'email': canonical_email,
            'status': row.status, 'credential_grant_scopes': sorted(row.credential_grant_scopes or []),
            'identifiers': [str(row.pk), row.agent_id, row.agent_username, canonical_email],
        }

    from fleet.runtime_fleet import list_runtime_agents
    matches = [agent for agent in list_runtime_agents(include_disabled=True) if agent.get('email') == email]
    if len(matches) != 1:
        raise CapabilityEvidenceError('HIVE_EVIDENCE_SUBJECT_NOT_MATERIALIZED', http_status=409)
    agent = matches[0]
    return {
        'kind': 'runtime', 'pk': str(agent.get('id') or ''),
        'agent_id': str(agent.get('agent_id') or ''),
        'username': str(agent.get('agent_username') or ''), 'email': email,
        'status': str(agent.get('status') or ''),
        'credential_grant_scopes': sorted(agent.get('credential_grant_scopes') or []),
        'identifiers': [str(agent.get('id') or ''), str(agent.get('agent_id') or ''),
                        str(agent.get('agent_username') or ''), email],
    }


def _normalize_capability(value: str) -> str:
    return str(value or '').strip().lower().replace('_', '-')


def _materialize(request: dict):
    email = request['email']
    memberships, roster_age, roster_built_at = _fresh_memberships(email)
    exact = [
        row for row in memberships
        if _org_id(row) == request['organization_id']
        and _normalize_capability(row.get('team_slug') or row.get('team_name')) == request['team_slug']
        and str(row.get('email') or email).strip().lower() == email
    ]
    if len(exact) != 1:
        raise CapabilityEvidenceError('HIVE_EVIDENCE_EXACT_SEAT_MISMATCH', http_status=409)
    org_slug = str(
        exact[0].get('organization_slug')
        or (exact[0].get('organization') or {}).get('slug')
        or ''
    ).strip().lower()
    if not org_slug:
        raise CapabilityEvidenceError('HIVE_EVIDENCE_ORG_MATERIALIZATION_INCOMPLETE', http_status=503)

    subject = _subject_identity(email)
    # Resolve from the asserted seat alone. Passing an explicit organization scope
    # here would deliberately apply the tenant allow-list even for the internal
    # ``shizuha`` organization, while passing the subject's whole roster could let
    # a second seat contribute capabilities. The exact roster row is the sole
    # authorization input for this evidence statement.
    grant = compute_effective_capability_grant_for_memberships(
        [exact[0]], subject['identifiers'],
    )
    available = {
        _normalize_capability(cap.get('slug')) for cap in grant.get('capabilities') or []
    }
    available.update(_normalize_capability(cap) for cap in grant.get('sensitive_access_flags') or [])
    available.update(_normalize_capability(cap) for cap in grant.get('credential_grant_scopes') or [])
    available.update(_normalize_capability(cap) for cap in subject['credential_grant_scopes'])
    available.discard('')
    results = {
        cap: ('PRESENT' if cap in available else 'ABSENT')
        for cap in request['required_capabilities']
    }
    generation_input = {
        'subject': {key: subject[key] for key in ('kind', 'pk', 'agent_id', 'username', 'email', 'status')},
        'seat': {
            'organization_id': request['organization_id'], 'organization_slug': org_slug,
            'team_slug': request['team_slug'],
        },
        'roster': sorted(
            [{
                'email': str(row.get('email') or email).strip().lower(),
                'organization_id': _org_id(row),
                'organization_slug': str(row.get('organization_slug') or '').strip().lower(),
                'team_slug': _normalize_capability(row.get('team_slug') or row.get('team_name')),
            }
            for row in memberships
            ], key=lambda row: (
            row['organization_id'] or -1, row['organization_slug'], row['team_slug'], row['email'],
        )),
        'roster_built_at_millis': int(roster_built_at * 1000),
        'grant': {
            'capabilities': sorted(available),
            'definition_hashes': grant.get('definition_hashes') or {},
            'catalog_version': int(grant.get('catalog_version') or 0),
            'agent_overrides': grant.get('agent_overrides') or [],
        },
    }
    return results, f'mat-{_sha256(canonical_json_bytes(generation_input))}', roster_age


def current_generation(signing: SigningIdentity | None = None):
    rows = list(CapabilityEvidenceProducerGeneration.objects.filter(active=True)[:2])
    if len(rows) != 1:
        raise CapabilityEvidenceError('HIVE_PRODUCER_GENERATION_NOT_CURRENT', http_status=503)
    generation = rows[0]
    signing = signing or load_signing_identity()
    if (
        generation.signing_kid != signing.kid
        or generation.signing_key_thumbprint != signing.thumbprint
        or generation.schema_digest != schema_digest()
        or generation.materializer_version != MATERIALIZER_VERSION
        or generation.materializer_digest != materializer_digest()
        or generation.config_digest != config_digest()
        or generation.service_account != str(getattr(
            settings, 'HIVE_CAPABILITY_EVIDENCE_SERVICE_ACCOUNT', 'hive-api',
        ))
    ):
        raise CapabilityEvidenceError('HIVE_PRODUCER_GENERATION_NOT_CURRENT', http_status=503)
    return generation, signing


def _compact_jws(claims: dict, signing: SigningIdentity) -> str:
    protected = {'alg': 'EdDSA', 'kid': signing.kid, 'typ': EVIDENCE_TYP}
    encoded_header = _b64url(canonical_json_bytes(protected))
    encoded_payload = _b64url(canonical_json_bytes(claims))
    signing_input = f'{encoded_header}.{encoded_payload}'.encode('ascii')
    signature = signing.private_key.sign(signing_input)
    return f'{encoded_header}.{encoded_payload}.{_b64url(signature)}'


def produce_evidence(raw_request: bytes, *, now: int | None = None, jti: str | None = None):
    request = parse_request(raw_request)
    generation, signing = current_generation()
    results, materialization_generation, _roster_age = _materialize(request)
    issued_at = int(time.time()) if now is None else int(now)
    claims = {
        'schema_version': EVIDENCE_SCHEMA,
        'iss': EVIDENCE_ISSUER,
        'aud': EVIDENCE_AUDIENCE,
        'sub': request['email'],
        'organization_id': request['organization_id'],
        'team_slug': request['team_slug'],
        'request_id': request['request_id'],
        'request_sha256': request_sha256(request),
        'requirement_key': request['requirement_key'],
        'required_capabilities': request['required_capabilities'],
        'results': results,
        'materialization_generation': materialization_generation,
        'producer_generation_id': generation.generation_id,
        'producer_generation_digest': generation.generation_digest,
        'iat': issued_at,
        'nbf': issued_at,
        'exp': issued_at + MAX_LIFETIME_SECONDS,
        'jti': str(jti or uuid.uuid4()),
    }
    return _compact_jws(claims, signing), claims


def decode_and_verify_for_test(compact_jws: str, public_key: Ed25519PublicKey):
    """Minimal deterministic verifier used by producer/golden-vector tests."""
    header_b64, payload_b64, signature_b64 = compact_jws.split('.')
    pad = lambda value: value + '=' * (-len(value) % 4)
    signature = base64.urlsafe_b64decode(pad(signature_b64))
    public_key.verify(signature, f'{header_b64}.{payload_b64}'.encode('ascii'))
    header = json.loads(base64.urlsafe_b64decode(pad(header_b64)))
    payload = json.loads(base64.urlsafe_b64decode(pad(payload_b64)))
    return header, payload
