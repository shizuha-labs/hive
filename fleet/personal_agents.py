"""Owner-scoped personal Shizuha provisioning (HIVE-620/HIVE-644/HIVE-2131).

Operator 2026-07-12 ("un-fence now, harden later; give me my agents to talk
to"): this creates the FleetAgent row + its dedicated Shizuha ID identity AND
provisions a real runtime — a pod in the owner's OWN per-owner ISOLATED
namespace (``isolated=True``) — so the owner can chat with and manage their
agent today. Full owner-scoped sandbox hardening (gVisor/Kata + strict
per-tenant admission, HLD §15 / PLAT-4235) is deferred follow-up, not a
precondition. The provisioner's isolation gate still fail-closes a personal
agent that would land in the SHARED fleet namespace.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
import logging
import uuid

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .models import (
    BASE_PLATFORM_EAGER_SKILLS,
    BASE_PLATFORM_SKILLS,
    FleetAgent,
    FleetAgentEvent,
)
from . import provisioner
from .provisioner import _generate_agent_password, _provision_platform_identity_details

_re_logger = logging.getLogger(__name__)


class PersonalShizuhaProvisionError(RuntimeError):
    """The logical row exists fail-closed, but its ID identity is incomplete."""


@dataclass(frozen=True)
class PersonalShizuhaEnsureResult:
    agent: FleetAgent
    created: bool
    identity_created: bool


def personal_shizuha_username(owner_id: int) -> str:
    try:
        owner = int(owner_id)
    except (TypeError, ValueError) as exc:
        raise ValueError('owner_id must be a positive integer subject') from exc
    if owner <= 0:
        raise ValueError('owner_id must be a positive integer subject')
    username = f'shizuha-{owner}'
    if len(username) > 47:
        raise ValueError('owner subject exceeds the FleetAgent DNS name budget')
    return username


# Operator 2026-08-05 banned garbage auto-spawns (testuser, scli-qa, liveqa,
# origin-ci, demo, fixture domains). 2026-08-19: every *real* human still gets
# one default, but staff / anyone who already owns a fleet agent (Hritik) is
# left alone.
_SKIP_EMAIL_SUFFIXES = (
    '@example.com', '@example.invalid', '@example.qa',
    '@agents.shizuha.io', '@services.shizuha.io',
    '@qa.invalid', '@qa.shizuha.local', '@qa-internal.shizuha.com',
    '@shizuha-test.local', '@shizuha-validation.test',
    '@test.shizuha.io', '@test.shizuha.com', '@test.shizuha.internal',
    '@shizuha.test', '@shizuha.local',
)
_SKIP_USERNAMES = frozenset({
    'testuser', 'testchat', 'scli-qa', 'liveqa', 'enaqa', 'origin-ci',
    'demo', 'system', 'metrics-exporter', 'buildscheck',
})
_SKIP_USERNAME_PREFIXES = (
    'qa', 'test', 'e2e_', 'cli_test', 'direct_test', 'claude_mcp',
    'ntsqa', 'id40', 'id65', 'id69', 'id70', 'drv', 'gpdemo', 'gp-demo',
    'goldenpath', 'mail-e2e', 'dojo', 'finqa', 'v4-', 'pls700', 'bks161',
    'kot90', 'kot93', 'hive676', 'qamkt', 'qamint', 'origin-ci', 'liveqa',
)
_SKIP_USERNAME_MARKERS = (
    '_qa', '-qa', 'qa_', 'qa-', 'test_agent', '_test_', '-test-',
)


def owner_already_has_fleet_agent(owner_id) -> bool:
    """True when this human already owns any non-deleted FleetAgent."""
    try:
        owner = int(owner_id)
    except (TypeError, ValueError):
        return False
    if owner <= 0:
        return False
    qs = (
        FleetAgent.objects
        .exclude(status=FleetAgent.Status.DELETED)
        .filter(owner_id=owner)
    )
    try:
        qs = qs.exclude(agent_username=personal_shizuha_username(owner))
    except ValueError:
        pass
    return qs.exists()


def looks_like_service_or_qa_account(username: str, email: str) -> bool:
    """Fixture / QA / service identities must never receive a default agent."""
    user = str(username or '').strip().lower()
    mail = str(email or '').strip().lower()
    if not user:
        return True
    if user in _SKIP_USERNAMES:
        return True
    if any(user.startswith(prefix) for prefix in _SKIP_USERNAME_PREFIXES):
        return True
    if any(marker in user for marker in _SKIP_USERNAME_MARKERS):
        return True
    if mail.endswith('qa@shizuha.com'):
        return True
    if 'scli-qa' in mail or '+qa@' in mail or '.qa@' in mail:
        return True
    if any(mail.endswith(suffix) for suffix in _SKIP_EMAIL_SUFFIXES):
        return True
    return False


def owner_record(
    *,
    owner_id,
    username: str,
    email: str = '',
    is_staff: bool = False,
    is_superuser: bool = False,
    is_agent: bool = False,
    account_type: str = 'human',
):
    """Build the user-like object eligibility + ensure accept from ID rows."""
    return SimpleNamespace(
        id=int(owner_id),
        username=str(username or ''),
        email=str(email or ''),
        is_staff=bool(is_staff),
        is_superuser=bool(is_superuser),
        is_agent=bool(is_agent),
        is_authenticated=True,
        profile=SimpleNamespace(account_type=str(account_type or 'human')),
    )


def owner_record_from_id_row(row: dict):
    data = row or {}
    profile = data.get('profile') or {}
    account_type = (
        data.get('account_type')
        or profile.get('account_type')
        or 'human'
    )
    return owner_record(
        owner_id=data.get('id'),
        username=data.get('username') or '',
        email=data.get('email') or '',
        is_staff=bool(data.get('is_staff')),
        is_superuser=bool(data.get('is_superuser')),
        is_agent=bool(data.get('is_agent')) or account_type == 'agent',
        account_type=account_type,
    )


def eligible_for_default_personal_agent(user) -> bool:
    """Humans get one isolated hibernated default. Staff, existing owners,
    agents, and test/service accounts do not (operator 2026-08-05 + 2026-08-19)."""
    if user is None or not getattr(user, 'is_authenticated', False):
        return False
    if getattr(user, 'is_staff', False) or getattr(user, 'is_superuser', False):
        return False
    if getattr(user, 'is_agent', False):
        return False
    username = str(getattr(user, 'username', '') or '').strip()
    email = str(getattr(user, 'email', '') or '').strip().lower()
    if looks_like_service_or_qa_account(username, email):
        return False
    if FleetAgent.objects.filter(agent_username=username).exists():
        return False
    profile = getattr(user, 'profile', None)
    account_type = getattr(profile, 'account_type', 'human') if profile is not None else 'human'
    if account_type == 'agent':
        return False
    owner_id = getattr(user, 'id', None)
    if owner_id and owner_already_has_fleet_agent(owner_id):
        return False
    return True


def get_personal_agent(owner_id: int):
    """Return the owner's non-deleted personal Shizuha, if one exists.

    PLAT-4235 ("users own fleets"): an owner may own a whole fleet, so the
    personal agent is identified by its canonical deterministic username
    (`shizuha-<owner_id>`), NOT by owner_id uniqueness (that constraint was
    dropped in migration 0030).
    """
    try:
        username = personal_shizuha_username(owner_id)
    except ValueError:
        return None
    return (
        FleetAgent.objects
        .exclude(status=FleetAgent.Status.DELETED)
        .filter(agent_username=username)
        .first()
    )


def _owner_daemon(owner_id):
    """The CONNECTED fleet daemon owned by this user, or None.

    Unified plane (operator 2026-07-13): a user's agents — the personal Shizuha
    included — run under that user's fleet daemon, exactly like every other
    agent. No daemon (or a daemon without a control endpoint) falls back to the
    legacy isolated-namespace provisioner until per-user daemons roll out.
    """
    from .models import DaemonRegistry
    try:
        return (
            DaemonRegistry.objects
            .filter(owner_subject=int(owner_id), status=DaemonRegistry.Status.CONNECTED)
            .order_by('id')
            .first()
        )
    except (TypeError, ValueError):
        return None


def _actuate_personal_runtime(agent: FleetAgent, daemon=None) -> None:
    """Make the personal Shizuha LIVE — via the owner's fleet daemon when it has
    one (unified HIVE-247 top-down provision, same plane as every fleet agent),
    else the legacy k3s provisioner (isolated per-owner namespace).

    Idempotent: an already-daemon-materialized row is only (re)started, never
    re-provisioned (a second provision would duplicate the daemon roster entry).
    """
    from . import runtime_fleet

    daemon = daemon if daemon is not None else _owner_daemon(agent.owner_id)
    if not agent.isolated:
        # Daemon-plane row (unified path). NEVER hand it to the k3s provisioner —
        # its isolation gate rightly BLOCKS a shared-ns personal row that is not
        # yet daemon-bound (canary-observed 2026-07-13). On failure, record and
        # fail SOFT: the row stays PENDING/FAILED and a later ensure retries.
        if agent.agent_id and agent.managing_daemon_id:
            ok, detail = runtime_fleet.set_runtime_agent_enabled(agent.agent_id, True)
        elif daemon is not None:
            ok, detail = runtime_fleet.provision_runtime_agent_via_daemon(agent, daemon)
        else:
            ok, detail = False, 'owner daemon not connected'
        if not ok:
            if isinstance(detail, dict) and detail.get('lease_waiting') is True:
                # Materialization has already retained the cold binding, or
                # this was an existing bound seat. Preserve its state and
                # binding instead of relabeling a recoverable wait as FAILED.
                _re_logger.info('personal runtime %s awaiting model lease (%s)',
                                agent.agent_username, detail.get('reason'))
                return
            _re_logger.warning(
                'daemon-path personal actuation failed for %s (%s); will retry on a later ensure',
                agent.agent_username, detail)
            FleetAgent.objects.filter(pk=agent.pk).update(
                status=FleetAgent.Status.FAILED,
                status_message=f'daemon provision pending retry: {str(detail)[:200]}',
            )
        return
    # Legacy isolated-namespace path (owners without a fleet daemon).
    if (
        getattr(agent, 'namespace', '')
        and agent.status in {
            FleetAgent.Status.HIBERNATED,
            FleetAgent.Status.STOPPED,
        }
    ):
        provisioner.start_agent(agent)
    else:
        provisioner.provision_agent(agent)


_FACTORY_PERSONAL_MODELS = frozenset({
    'gpt-5.5',
    'openai/gpt-5.5',
    'gpt-5.3-codex-spark',
    'gpt-5.6-sol',
    'openai/gpt-5.6-sol',
})


def _personal_talk_spec() -> tuple[str, str, bool]:
    """(model, execution_method, is_codex) for a new or aligned personal seat."""
    model = getattr(settings, 'PERSONAL_AGENT_MODEL', 'cortex/grok-voice-think-fast-2.0')
    lower = str(model or '').strip().lower()
    is_voice = 'grok-voice' in lower
    is_codex = (not is_voice) and (
        'codex' in lower or lower.startswith('gpt-') or '/gpt-' in lower
    )
    method = 'codex_app_server' if is_codex else 'shizuha'
    return model, method, is_codex


def _personal_model_fields(model: str, method: str, is_codex: bool) -> dict:
    return {
        'model': model,
        'model_overrides': {'codex_app_server': model} if is_codex else {},
        'model_fallbacks': (
            [{'method': method, 'model': model, 'reasoningEffort': 'high'}]
            + ([{'method': 'codex_app_server', 'model': 'gpt-5.5', 'reasoningEffort': 'high'}]
               if is_codex and model != 'gpt-5.5' else [])
        ),
    }


def _should_align_personal_talk_runtime(agent: FleetAgent) -> bool:
    """Retarget unused / factory Codex personal seats to the current talk model."""
    if agent.status == FleetAgent.Status.DELETED:
        return False
    model, method, _is_codex = _personal_talk_spec()
    current = str(agent.model or '').strip()
    mcp = agent.mcp_config if isinstance(agent.mcp_config, dict) else {}
    current_method = str(mcp.get('execution_method') or '').strip()
    if current == model and current_method == method:
        return False
    unused = (
        str(getattr(agent, 'hibernation_reason', '') or '') == 'default_until_first_use'
        and not getattr(agent, 'last_active_at', None)
    )
    factory = current in _FACTORY_PERSONAL_MODELS or current.endswith('/gpt-5.5')
    return unused or factory


def _align_personal_talk_runtime(agent: FleetAgent) -> bool:
    """Write Hive SoT talk-model for a factory/unused personal seat. Never starts a pod."""
    if not _should_align_personal_talk_runtime(agent):
        return False
    model, method, is_codex = _personal_talk_spec()
    mcp = dict(agent.mcp_config) if isinstance(agent.mcp_config, dict) else {}
    mcp['execution_method'] = method
    updates = {
        **_personal_model_fields(model, method, is_codex),
        'mcp_config': mcp,
    }
    FleetAgent.objects.filter(pk=agent.pk).exclude(
        status=FleetAgent.Status.DELETED,
    ).update(**updates)
    agent.refresh_from_db()
    try:
        from .agent_index_cache import invalidate as invalidate_agent_index
        invalidate_agent_index(f'personal talk model align {agent.agent_username}')
    except Exception:
        _re_logger.exception(
            'personal talk model index invalidate failed for %s', agent.agent_username)
    return True


def _get_or_create_owner_agent(
    owner_id: int, owner_username: str, daemon_managed: bool = False,
) -> tuple[FleetAgent, bool]:
    username = personal_shizuha_username(owner_id)
    model, method, _is_codex = _personal_talk_spec()
    # Operator 2026-07-12 / 2026-08-25: a personal Shizuha is a real fleet agent
    # in its OWN per-owner isolated namespace. It is a talk assistant (Grok Voice
    # S2S by default) — runs no untrusted code / inner docker — so
    # sandbox_required=False. Parked hibernated until first Connect DM.
    _servers = ['connect', 'id', 'pulse', 'wiki', 'mail', 'notes', 'drive']
    defaults = {
        'agent_username': username,
        # HIVE-1502: the runtime entrypoint requires AGENT_ID. The HTTP create
        # serializer mints this; get_or_create here used to skip it, so first
        # connect-wake provision blocked forever (shizuha-115 / shizuha-741).
        'agent_id': str(uuid.uuid4()),
        'display_name': 'Shizuha',
        'role': 'Personal Shizuha',
        **_personal_model_fields(model, method, _is_codex),
        'reasoning_effort': 'high',
        'resource_profile': FleetAgent.ResourceProfile.SMALL,
        'owner_username': str(owner_username or '')[:150],
        # Unified plane: when the owner has a fleet daemon, the personal Shizuha
        # is an ordinary daemon-managed agent in that daemon's fleet namespace
        # (isolated=False; the shared-ns gate admits it because daemon owner ==
        # agent owner). Without a daemon it gets the legacy isolated namespace.
        'isolated': not daemon_managed,
        'sandbox_required': False,
        'status': FleetAgent.Status.PENDING,
        'sidecar_set': ['broker'],
        'credential_grant_scopes': [],
        'shared_workspace': False,
        'expose_http': False,
        'mcp_config': {
            'mcp_servers': [{'name': s, 'slug': s} for s in _servers],
            # method matches the model family (was hardcoded 'shizuha' while the
            # override targeted codex_app_server — mismatched primary lane).
            'execution_method': method, 'runtime_environment': 'k8s',
            'enabled_mcp_server_ids': _servers,
        },
        'skill_config': {
            'skills': sorted({'connect-messaging', 'personal-assistant', *BASE_PLATFORM_SKILLS}),
            'eager_skills': sorted(BASE_PLATFORM_EAGER_SKILLS),
        },
        'context_prompt': (
            'You are Shizuha, the user\'s personal AI assistant on the Shizuha platform. '
            'Chat naturally and be genuinely helpful with anything on the platform — mail, '
            'notes, drive, tasks, org setup, and general questions. Respond directly and '
            'concisely; you are a responsive personal assistant, not a task-queue worker.'),
        'env': {
            'SHIZUHA_IDLE_HEARTBEAT_MS': '1800000',
            'SHIZUHA_HEARTBEAT_DEBOUNCE_MS': '1800000',
            'SHIZUHA_FIRST_HEARTBEAT_MS': '8000',
            'CLAUDE_CODE_DISABLE_1M_CONTEXT': '1',
        },
    }
    # PLAT-4235 ("users own fleets"): key on the canonical username (unique), NOT
    # owner_id — an owner may own many agents now, so owner_id is no longer a
    # unique lookup. agent_username uniqueness provides the same race-safety.
    try:
        with transaction.atomic():
            agent, created = FleetAgent.objects.get_or_create(
                agent_username=username,
                defaults={**defaults, 'owner_id': owner_id},
            )
    except IntegrityError:
        # Concurrent first-use requests race on the agent_username uniqueness; the
        # loser converges on the row committed by the winner.
        agent = FleetAgent.objects.get(agent_username=username)
        created = False

    if (agent.owner_id or None) != int(owner_id):
        raise PersonalShizuhaProvisionError('canonical personal agent username is bound to a different owner')
    return agent, created


def _floor_platform_skills(agent: FleetAgent) -> None:
    """Union the Pulse/Hive heartbeat floor onto personal skill_config.

    Personal seats skip team-capability reconcile. Without this they keep the
    old connect-messaging/personal-assistant seed and never share the fleet
    heartbeat protocol.
    """
    sc = dict(agent.skill_config or {})
    skills = list(sc.get('skills') or [])
    eager = list(sc.get('eager_skills') or [])
    want_skills = sorted(set(skills) | set(BASE_PLATFORM_SKILLS))
    want_eager = sorted(set(eager) | set(BASE_PLATFORM_EAGER_SKILLS))
    if want_skills == sorted(skills) and want_eager == sorted(eager):
        return
    sc['skills'] = want_skills
    sc['eager_skills'] = want_eager
    FleetAgent.objects.filter(pk=agent.pk).update(skill_config=sc)
    agent.skill_config = sc


def _park_default_hibernated(agent: FleetAgent) -> None:
    """Leave the seat hibernated with no running pod until first wake/DM.

    DB-only park used to leave an already-provisioned Deployment at replicas=1
    (Navin/Ganesh ``shizuha-{id}`` burned Hane while the row said
    ``default_until_first_use``). Scale to zero when a namespace exists.
    """
    from .timeutils import naive_utc_now
    if str(getattr(agent, 'namespace', '') or '').strip():
        try:
            if provisioner.hibernate_agent(agent, reason='default_until_first_use'):
                agent.refresh_from_db()
                if agent.hibernation_reason != 'default_until_first_use':
                    FleetAgent.objects.filter(pk=agent.pk).update(
                        hibernation_reason='default_until_first_use',
                    )
                    agent.refresh_from_db()
                return
        except Exception:
            _re_logger.exception(
                'personal default park failed to scale %s; falling back to DB park',
                agent.agent_username,
            )
    now = naive_utc_now()
    FleetAgent.objects.filter(pk=agent.pk).exclude(
        status=FleetAgent.Status.DELETED,
    ).update(
        status=FleetAgent.Status.HIBERNATED,
        hibernation_reason='default_until_first_use',
        hibernated_at=now,
        status_message='',
    )
    agent.refresh_from_db()


def ensure_personal_shizuha_agent(*, owner_id: int, owner_username: str = '') -> PersonalShizuhaEnsureResult:
    """Idempotently ensure one isolated, hibernated personal Shizuha + ID identity.

    Operator 2026-08-19: every human gets a default agent, parked until they
    use it. Do not start a pod here — connect-wake / explicit enable does that.
    """
    owner = int(owner_id)
    # Invariant (operator 2026-07-13): an AGENT must never own a personal fleet.
    # A personal Shizuha is itself a fleet agent, so an agent owning one would
    # recurse (its Shizuha spawns its own Shizuha, ad infinitum). Choke-point
    # guard: refuse if the owner account IS a fleet agent (roster is the local SoT;
    # the view also pre-checks via request.user.is_agent). See HIVE-644.
    if owner_username and FleetAgent.objects.filter(agent_username=owner_username).exists():
        raise PersonalShizuhaProvisionError(
            f'owner {owner_username!r} is a fleet agent; agents do not own personal fleets')
    # Operator 2026-08-19: users who already have agents (Hritik / CEO Office)
    # must not get an extra default. Idempotent ensure of an existing personal
    # row still proceeds.
    if get_personal_agent(owner) is None and owner_already_has_fleet_agent(owner):
        raise PersonalShizuhaProvisionError(
            'owner already has fleet agents; default personal Shizuha is not created')
    daemon = _owner_daemon(owner)
    agent, created = _get_or_create_owner_agent(owner, owner_username, daemon_managed=daemon is not None)
    _floor_platform_skills(agent)
    if not (agent.agent_id or '').strip():
        # Self-heal rows created before agent_id was minted on this path.
        FleetAgent.objects.filter(pk=agent.pk).filter(agent_id='').update(
            agent_id=str(uuid.uuid4()),
        )
        agent.refresh_from_db()
    if (
        agent.identity_provisioned_at is not None
        and agent.identity_user_id is not None
        and agent.identity_profile_id is not None
    ):
        # Already materialized. Do not start a pod from ensure. Deletion is
        # operator intent and must never be reversed by this automatic path.
        if agent.status == FleetAgent.Status.DELETED:
            return PersonalShizuhaEnsureResult(
                agent=agent, created=created, identity_created=False)
        if agent.status in {
            FleetAgent.Status.PENDING,
            FleetAgent.Status.FAILED,
            FleetAgent.Status.BLOCKED,
        }:
            _park_default_hibernated(agent)
        _align_personal_talk_runtime(agent)
        return PersonalShizuhaEnsureResult(agent=agent, created=created, identity_created=False)

    binding = _provision_platform_identity_details(
        agent,
        _generate_agent_password(),
        getattr(settings, 'SHIZUHA_PLATFORM_URL', ''),
    )
    if binding is None:
        FleetAgent.objects.filter(pk=agent.pk).update(
            status=FleetAgent.Status.FAILED,
            status_message='Dedicated Shizuha ID identity/profile binding failed; safe to retry.',
        )
        raise PersonalShizuhaProvisionError('dedicated Shizuha ID identity/profile binding failed')

    confirmed_at = timezone.now()
    updated = FleetAgent.objects.filter(pk=agent.pk).exclude(
        identity_provisioned_at__isnull=False,
        owner_profile_id=binding.owner_profile_id,
        identity_user_id=binding.user_id,
        identity_profile_id=binding.profile_id,
    ).update(
        owner_profile_id=binding.owner_profile_id,
        identity_user_id=binding.user_id,
        identity_profile_id=binding.profile_id,
        identity_provisioned_at=confirmed_at,
        # Reset a prior FAILED row to PENDING on successful (re)binding so the
        # provision step below actuates it (don't strand it in FAILED).
        status=FleetAgent.Status.PENDING,
        status_message='',
    )
    agent.refresh_from_db()
    if updated:
        FleetAgentEvent.objects.create(
            agent=agent,
            event_type='identity_provisioned',
            message='Dedicated personal Shizuha ID user/profile binding confirmed.',
        )
    # Operator 2026-08-19: park hibernated. A running pod per signup is cost
    # and was the 2026-08-05 garbage-spawn. First Connect DM / Hive enable wakes.
    _park_default_hibernated(agent)
    _align_personal_talk_runtime(agent)
    try:
        wire_personal_agent_connectivity(owner, agent)
    except Exception:
        _re_logger.exception('personal Shizuha connectivity wire failed for owner %s', owner)
    try:
        from .agent_index_cache import invalidate as invalidate_agent_index
        invalidate_agent_index(f'personal default ensure owner={owner}')
    except Exception:
        _re_logger.exception('personal Shizuha index invalidate failed for owner %s', owner)
    return PersonalShizuhaEnsureResult(agent=agent, created=created, identity_created=bool(updated))


# ── Connectivity (operator 2026-07-11): make the owner able to find, be CONNECTED
# to, and DM their personal agent so it shows in the sidebar by default (no connect
# request) and is DM-wakeable while offline. Additive to the on-demand identity/
# runtime path above; every step is best-effort/fail-soft and self-heals on a later
# ensure. No tenancy exemption — the agent joins the owner's personal org so
# owner↔agent DMs pass the CON-129 shared-org gate.
import logging as _logging  # noqa: E402
import requests as _rq  # noqa: E402

_conn_logger = _logging.getLogger(__name__)


def _svc_headers():
    h = {'Content-Type': 'application/json', 'X-Internal-Service': 'shizuha-hive'}
    tok = getattr(settings, 'ADMIN_INTERNAL_SERVICE_TOKEN', '')
    if tok:
        h['X-Internal-Service-Token'] = tok
    return h


def _id_base():
    return (getattr(settings, 'SHIZUHA_ID_INTERNAL_URL', '')
            or 'http://shizuha-id.shizuha.svc.cluster.local:8001').rstrip('/')


def fetch_id_owner_record(owner_id: int):
    """Resolve one ID human so the backfill command can target a single owner."""
    try:
        response = _rq.get(
            f'{_id_base()}/api/internal/users/{int(owner_id)}/',
            headers=_svc_headers(), timeout=8,
        )
        if response.status_code == 200:
            return owner_record_from_id_row(response.json() or {})
    except (TypeError, ValueError, _rq.RequestException) as exc:
        _conn_logger.warning('personal agent: id owner lookup failed for %s: %s', owner_id, exc)
    try:
        return owner_record(owner_id=int(owner_id), username='')
    except (TypeError, ValueError):
        return None


def iter_id_human_directory(*, page_size: int = 100):
    """Page ID's in-cluster human directory. Raises on transport/HTTP errors."""
    limit = max(1, min(int(page_size or 100), 200))
    offset = 0
    while True:
        response = _rq.get(
            f'{_id_base()}/api/internal/humans/',
            params={'limit': limit, 'offset': offset},
            headers=_svc_headers(), timeout=15,
        )
        response.raise_for_status()
        payload = response.json() or {}
        rows = payload.get('results') or []
        for row in rows:
            yield owner_record_from_id_row(row)
        if len(rows) < limit:
            return
        offset += limit


def _admin_base():
    return (getattr(settings, 'HIVE_ADMIN_API_URL', '')
            or 'http://shizuha-admin.shizuha.svc.cluster.local:8003/api').rstrip('/')


def resolve_agent_user_id(agent):
    """The agent's Connect / shizuha-id user_id. Prefer the exact binding sara
    persists (``identity_user_id``); fall back to the id by-username lookup."""
    uid = getattr(agent, 'identity_user_id', None)
    if uid:
        return int(uid)
    try:
        r = _rq.get(f'{_id_base()}/api/internal/users/by-username/',
                    params={'username': agent.agent_username}, headers=_svc_headers(), timeout=8)
        if r.status_code == 200:
            return (r.json() or {}).get('id')
    except _rq.RequestException as exc:
        _conn_logger.warning('personal agent: id by-username failed for %s: %s', agent.agent_username, exc)
    return None


def _owner_personal_org_id(owner_id):
    try:
        r = _rq.get(f'{_admin_base()}/internal/users/{int(owner_id)}/organizations/',
                    headers=_svc_headers(), timeout=8)
        if r.status_code == 200:
            for org in (r.json() or {}).get('organizations', []):
                if org.get('is_personal') or str(org.get('slug', '')).startswith('personal-'):
                    return org.get('id')
    except _rq.RequestException as exc:
        _conn_logger.warning('personal agent: owner org lookup failed for %s: %s', owner_id, exc)
    return None


def _connect_base():
    return (getattr(settings, 'SHIZUHA_CONNECT_INTERNAL_URL', '')
            or getattr(settings, 'SHIZUHA_PLATFORM_URL', '')
            or 'http://shizuha-nginx.shizuha.svc.cluster.local').rstrip('/')


def connect_agent_connection_url(base: str | None = None) -> str:
    root = (base or _connect_base()).rstrip('/')
    if 'shizuha-connect' in root:
        return f'{root}/api/internal/agent-connection/'
    return f'{root}/connect/api/internal/agent-connection/'


def wire_personal_agent_connectivity(owner_id, agent):
    """Resolve the agent's Connect user_id, join it to the owner's personal org
    (shared-org DM gate, no exemption), and ensure an accepted Connection + starter
    DM conversation so the agent shows in the owner's sidebar by default and is
    DM-wakeable while offline. Returns the agent's Connect user_id (or None)."""
    uid = resolve_agent_user_id(agent)
    if uid is None:
        return None
    org_id = _owner_personal_org_id(owner_id)
    if org_id is not None:
        try:
            # Admin rejects an unsigned membership write (HIVE-671). The roster
            # path already signs this envelope; the personal-agent path has to
            # as well or the owner and the agent never share an org and Connect
            # refuses the DM.
            from .org_membership_sync import _signed_membership_request
            url = f'{_admin_base()}/internal/organizations/{int(org_id)}/members/'
            body, headers = _signed_membership_request(url, {
                'user_id': int(uid),
                'user_email': f'{agent.agent_username}@agents.shizuha.io',
                'display_name': agent.display_name or 'Shizuha',
                'role': 'member',
                'operation_id': f'personal-agent-org:{int(org_id)}:{int(uid)}',
            })
            _rq.post(url, data=body, headers=headers, timeout=8)
        except (ValueError, _rq.RequestException) as exc:
            _conn_logger.warning('personal agent: add-to-org failed for %s: %s', agent.agent_username, exc)
    try:
        _rq.post(connect_agent_connection_url(),
                 json={'owner_user_id': int(owner_id), 'agent_user_id': int(uid),
                       'agent_name': agent.display_name or 'Shizuha',
                       'agent_email': f'{agent.agent_username}@agents.shizuha.io'},
                 headers={'Content-Type': 'application/json', 'X-Internal-Service': 'shizuha-agent'},
                 timeout=8)
    except _rq.RequestException as exc:
        _conn_logger.warning('personal agent: auto-connect failed for %s: %s', agent.agent_username, exc)
    return uid
