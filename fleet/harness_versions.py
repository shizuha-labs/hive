"""Fleet harness version discovery and runtime-build dispatch.

Hive may inspect reviewed runtime-image labels and dispatch immutable build
candidates. It deliberately has no API for promoting a candidate or mutating
runtime desired state; adoption belongs to reviewed ``DesiredRuntimeRelease``
source history.
"""
from __future__ import annotations

import logging
import os
import time

import requests
from django.core.cache import cache
from fleet import workflow_dispatch_guard
from fleet.cache_resilience import optional_get, optional_set


logger = logging.getLogger(__name__)

HARNESS_BY_METHOD = {
    'codex_app_server': ('codex', '@openai/codex', 'Codex'),
    'claude_code_server': ('claude_code', '@anthropic-ai/claude-code', 'Claude Code'),
    'antigravity_server': ('antigravity', None, 'Antigravity CLI'),
    'gemini_cli_server': ('antigravity', None, 'Antigravity CLI'),
    'openclaw_bridge': ('openclaw', 'openclaw', 'OpenClaw'),
    'shizuha': ('scli', None, 'Shizuha CLI'),
    'grok_build': ('scli', None, 'Shizuha CLI'),
}
HARNESS_KEYS = {
    'codex': ('@openai/codex', 'Codex'),
    'claude_code': ('@anthropic-ai/claude-code', 'Claude Code'),
    'antigravity': (None, 'Antigravity CLI'),
    'openclaw': ('openclaw', 'OpenClaw'),
    'scli': (None, 'Shizuha CLI'),
}

_ANTIGRAVITY_MANIFEST_URL = (
    'https://antigravity-cli-auto-updater-974169037036.us-central1.run.app'
    '/manifests/linux_amd64.json'
)
_LABEL_PREFIX = 'org.shizuha.harness.'
_IMAGE_LABEL_TTL = 300
_NPM_LATEST_TTL = 1800
_NPM_LATEST_FAILURE_TTL = 30
_NPM_LATEST_ATTEMPTS = 3
_NPM_LATEST_RETRY_DELAY = 0.25
_HTTP_TIMEOUT = 4
_HARNESS_BUILD_INPUT = {
    'claude_code': 'claude_code_version',
    'codex': 'codex_version',
    'antigravity': 'antigravity_version',
    'openclaw': 'openclaw_version',
}


def dispatch_agent_runtime_build(version_overrides: dict) -> dict:
    """Dispatch one immutable multi-harness build candidate through Origin CI."""
    token = os.environ.get('HIVE_FORGEJO_TOKEN', '').strip()
    api = os.environ.get(
        'HIVE_ORIGIN_API_URL',
        'http://forgejo-http.origin.svc.cluster.local',
    ).rstrip('/')
    repo = os.environ.get('HIVE_AGENT_RUNTIME_REPO', 'shizuha-labs/shizuha-beta')
    ref = os.environ.get('HIVE_AGENT_RUNTIME_REF', 'master')
    registry = os.environ.get('HIVE_RUNTIME_REGISTRY', 'localhost:30500')
    if not token:
        return {'dispatched': False, 'error': 'HIVE_FORGEJO_TOKEN not configured'}

    tag = 'harness-' + time.strftime('%Y%m%d%H%M', time.gmtime())
    resolved = dict(version_overrides or {})
    unresolved = []
    for harness, (package, _label) in HARNESS_KEYS.items():
        if harness == 'scli' or resolved.get(harness):
            continue
        if harness == 'antigravity':
            latest = antigravity_latest()
        elif package:
            latest = npm_latest(package)
        else:
            continue
        if latest:
            resolved[harness] = latest
        else:
            unresolved.append(harness)
    if unresolved:
        return {
            'dispatched': False,
            'error': f'could not resolve safe versions for: {", ".join(sorted(unresolved))}',
        }

    inputs = {'tag': tag}
    for harness, version in resolved.items():
        field = _HARNESS_BUILD_INPUT.get(harness)
        if field and version:
            inputs[field] = str(version)

    # PLAT-9683 leg 4 (dispatch-side pre-validation): strict-load the target
    # workflow AT THE DISPATCH REF before firing. A workflow that fatalizes
    # the runner's intake decode (duplicate-key class, PLAT-9660) yields a
    # silent 204-but-no-run + pre-log daemon death; fail the dispatch loudly
    # instead, with the decoder-style parse error.
    guard = workflow_dispatch_guard.validate_workflow_dispatch_yaml(
        repo,
        ref,
        'build-agent-runtime.yml',
        api=api,
        token=token,
    )
    if not guard.get('ok'):
        logger.warning(
            'SCLI-331: agent-runtime build dispatch BLOCKED by workflow YAML guard: %s',
            guard.get('error'),
        )
        return {'dispatched': False, 'error': guard.get('error')}

    url = f'{api}/api/v1/repos/{repo}/actions/workflows/build-agent-runtime.yml/dispatches'
    try:
        response = requests.post(
            url,
            json={'ref': ref, 'inputs': inputs},
            headers={
                'Authorization': f'token {token}',
                'Content-Type': 'application/json',
            },
            timeout=15,
        )
        if response.status_code in (201, 204):
            image = f'{registry}/shizuha-agent-runtime:{tag}'
            logger.warning(
                'SCLI-331: dispatched agent-runtime build %s inputs=%s',
                tag,
                inputs,
            )
            return {'dispatched': True, 'tag': tag, 'image': image}
        return {
            'dispatched': False,
            'error': f'dispatch HTTP {response.status_code}: {response.text[:200]}',
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning('SCLI-331: agent-runtime build dispatch failed: %s', exc)
        return {'dispatched': False, 'error': str(exc)[:200]}


def _registry_base(host: str) -> str:
    return f'http://{host}'


def image_harness_labels(image_ref: str) -> dict:
    """Read ``org.shizuha.harness.*`` labels from an OCI image."""
    if not image_ref or '/' not in image_ref:
        return {}
    cache_key = f'harness:img-labels:{image_ref}'
    cached = optional_get(cache_key)
    if cached is not None:
        return cached
    labels = {}
    try:
        host, rest = image_ref.split('/', 1)
        repo, _, tag = rest.partition(':')
        tag = tag or 'latest'
        read_host = host
        if host.startswith('localhost:') or host.endswith(':30500'):
            read_host = os.environ.get(
                'HIVE_RUNTIME_REGISTRY_INTERNAL',
                'registry.registry.svc.cluster.local:5000',
            )
        base = _registry_base(read_host)
        accept = ','.join([
            'application/vnd.oci.image.index.v1+json',
            'application/vnd.docker.distribution.manifest.list.v2+json',
            'application/vnd.oci.image.manifest.v1+json',
            'application/vnd.docker.distribution.manifest.v2+json',
        ])
        session = requests.Session()
        manifest_response = session.get(
            f'{base}/v2/{repo}/manifests/{tag}',
            headers={'Accept': accept},
            timeout=_HTTP_TIMEOUT,
        )
        manifest_response.raise_for_status()
        manifest = manifest_response.json()
        if manifest.get('manifests'):
            child = manifest['manifests'][0]['digest']
            child_response = session.get(
                f'{base}/v2/{repo}/manifests/{child}',
                headers={'Accept': accept},
                timeout=_HTTP_TIMEOUT,
            )
            child_response.raise_for_status()
            manifest = child_response.json()
        config_digest = (manifest.get('config') or {}).get('digest')
        if config_digest:
            config_response = session.get(
                f'{base}/v2/{repo}/blobs/{config_digest}',
                timeout=_HTTP_TIMEOUT,
            )
            config_response.raise_for_status()
            config_labels = (
                (config_response.json().get('config') or {}).get('Labels') or {}
            )
            for key, value in config_labels.items():
                if key.startswith(_LABEL_PREFIX) and value:
                    labels[key[len(_LABEL_PREFIX):]] = str(value)
    except Exception as exc:  # noqa: BLE001
        logger.info('harness label read failed for %s: %s', image_ref, exc)
    optional_set(cache_key, labels, _IMAGE_LABEL_TTL)
    return labels


def runtime_image_tag_exists(tag: str) -> bool:
    """Return whether a dispatched candidate tag reached the registry."""
    if not tag:
        return False
    host = os.environ.get(
        'HIVE_RUNTIME_REGISTRY_INTERNAL',
        'registry.registry.svc.cluster.local:5000',
    )
    try:
        response = requests.get(
            f'{_registry_base(host)}/v2/shizuha-agent-runtime/tags/list',
            timeout=_HTTP_TIMEOUT,
        )
        response.raise_for_status()
        return tag in (response.json().get('tags') or [])
    except Exception as exc:  # noqa: BLE001
        logger.info('registry tag check failed for %s: %s', tag, exc)
        return False


def npm_latest(package: str) -> str | None:
    cache_key = f'harness:npm-latest:{package}'
    cached = optional_get(cache_key)
    if cached is not None:
        return cached or None
    version = None
    for attempt in range(1, _NPM_LATEST_ATTEMPTS + 1):
        try:
            response = requests.get(
                f'https://registry.npmjs.org/{package}/latest',
                timeout=_HTTP_TIMEOUT,
            )
            response.raise_for_status()
            version = (response.json() or {}).get('version')
            if version:
                break
            raise ValueError('latest metadata omitted version')
        except Exception as exc:  # noqa: BLE001
            logger.info(
                'npm latest lookup failed for %s (attempt %s/%s): %s',
                package,
                attempt,
                _NPM_LATEST_ATTEMPTS,
                exc,
            )
            if attempt < _NPM_LATEST_ATTEMPTS:
                time.sleep(_NPM_LATEST_RETRY_DELAY * attempt)
    ttl = _NPM_LATEST_TTL if version else _NPM_LATEST_FAILURE_TTL
    optional_set(cache_key, version or '', ttl)
    return version


def antigravity_latest() -> str | None:
    """Return Google's latest native Antigravity CLI release."""
    cache_key = 'harness:antigravity-latest'
    cached = optional_get(cache_key)
    if cached is not None:
        return cached or None
    version = None
    try:
        response = requests.get(_ANTIGRAVITY_MANIFEST_URL, timeout=_HTTP_TIMEOUT)
        if response.ok:
            version = (response.json() or {}).get('version')
    except Exception as exc:  # noqa: BLE001
        logger.info('antigravity latest lookup failed: %s', exc)
    optional_set(cache_key, version or '', _NPM_LATEST_TTL)
    return version


def _semver_tuple(version: str | None):
    if not version:
        return ()
    parts = []
    for token in str(version).replace('-', '.').split('.'):
        if token.isdigit():
            parts.append(int(token))
        else:
            break
    return tuple(parts)


def upgrade_available(current: str | None, latest: str | None) -> bool:
    if not current or not latest:
        return False
    return _semver_tuple(latest) > _semver_tuple(current)


# HIVE-615: how aggressive automatic promotion is allowed to be.
#
# Auto-rolling the whole fleet on EVERY upstream release is how the 0.136->0.144
# codex bump poisoned sessions fleet-wide. The operator asked for a dial: auto
# for small bumps, notify-only for the ones that historically break things.
#
# Ordered least->most aggressive. 'off' still DETECTS and reports; it only
# withholds dispatch, so the harness panel keeps telling the truth about what is
# available.
AUTOUPGRADE_POLICIES = ('off', 'patch', 'minor')
DEFAULT_AUTOUPGRADE_POLICY = 'minor'
_BUMP_RANK = {'patch': 1, 'minor': 2, 'major': 3}


def bump_level(current: str | None, latest: str | None) -> str | None:
    """Classify an upgrade as patch/minor/major.

    Returns None when this is not an upgrade at all (equal, older, or
    unparseable), so a caller can never classify a downgrade as a bump —
    the failure mode that let a fleet roll backward.
    """
    if not upgrade_available(current, latest):
        return None
    cur = _semver_tuple(current)
    new = _semver_tuple(latest)
    # Compare position by position; the first differing component decides.
    # Zero-pad so 1.2 -> 1.2.1 reads as a patch rather than falling through.
    width = max(len(cur), len(new), 3)
    cur = cur + (0,) * (width - len(cur))
    new = new + (0,) * (width - len(new))
    if new[0] != cur[0]:
        return 'major'
    if new[1] != cur[1]:
        return 'minor'
    return 'patch'


def autoupgrade_policy() -> str:
    """Read HARNESS_AUTOUPGRADE, falling back to the conservative default.

    An unrecognised value falls back rather than raising: this gates a cron that
    must keep running, and a typo should not silently become 'promote anything'.
    """
    raw = (os.environ.get('HARNESS_AUTOUPGRADE') or '').strip().lower()
    return raw if raw in AUTOUPGRADE_POLICIES else DEFAULT_AUTOUPGRADE_POLICY


def autoupgrade_allowed(current: str | None, latest: str | None,
                        policy: str | None = None) -> bool:
    """True when policy permits AUTOMATIC promotion of this bump.

    Detection is unaffected — a withheld upgrade is still reported so the
    operator can promote it deliberately.
    """
    level = bump_level(current, latest)
    if level is None:
        return False
    active = policy if policy in AUTOUPGRADE_POLICIES else autoupgrade_policy()
    if active == 'off':
        return False
    return _BUMP_RANK[level] <= _BUMP_RANK[active]


# ── HIVE-615: durable promotion run + renewable fencing lease ────────────────
#
# Architecture decision (CoS, HLD v7): the whole upgrade lifecycle stays inside
# ONE orchestration command. Canary observation may be factored into a
# read-only probe, but it reports under the SAME run ID and the SAME renewable
# fencing lease, and only the owning command advances or rolls back.
#
# The pre-existing promotion lock is a 300s `cache.add` with a token-checked
# release. That is sufficient for a fast promote, but NOT for a canary that
# waits on a real agent turn: the lock can silently expire mid-run while the
# code keeps mutating fleet state. These primitives close that gap.
#
# Fail-closed is the rule everywhere: if we cannot PROVE we still hold the
# lease, we do not mutate, and an unverified rollback is recorded as a failure
# rather than as `rolled_back`.

HARNESS_UPGRADE_RUN_KEY = 'fleet:harness-upgrade-run'
# Single-writer promotion lock: shared with the fast-promote path so the
# renewable lease and a plain promote can never run concurrently.
_AUTOMATED_PROMOTION_LOCK_KEY = 'fleet:automated-promotion-lock'
LEASE_TTL_SECONDS = 300
LEASE_RENEW_AFTER_SECONDS = 120
# A run may legitimately outlive many lease TTLs (canary waits on a real turn),
# so the run record is kept well beyond the lease to survive a restart.
RUN_TTL_SECONDS = 24 * 3600

# Phases are ordered: everything from CANARY_SWITCHED onward has MUTATED fleet
# state and therefore has something to roll back.
PHASE_DETECTED = 'detected'
PHASE_DISPATCHED = 'dispatched'
PHASE_CANARY_SWITCHED = 'canary_switched'
PHASE_CANARY_VERIFIED = 'canary_verified'
PHASE_FLEET_SWITCHED = 'fleet_switched'
PHASE_VERIFIED = 'verified'
PHASE_ROLLING_BACK = 'rolling_back'
PHASE_ROLLED_BACK = 'rolled_back'
PHASE_ROLLBACK_FAILED = 'rollback_failed'
PHASE_FAILED = 'failed'

MUTATED_PHASES = frozenset({
    PHASE_CANARY_SWITCHED, PHASE_CANARY_VERIFIED,
    PHASE_FLEET_SWITCHED, PHASE_VERIFIED,
    PHASE_ROLLING_BACK,
})
TERMINAL_PHASES = frozenset({
    PHASE_VERIFIED, PHASE_ROLLED_BACK, PHASE_ROLLBACK_FAILED, PHASE_FAILED,
})


class LeaseLost(RuntimeError):
    """Raised when the fencing lease can no longer be proven held.

    Never caught-and-continued: losing the lease means another writer may own
    the fleet image, so continuing to mutate is exactly the race this design
    exists to prevent.
    """


class PromotionLease:
    """Renewable, fenced, single-writer lease over fleet image promotion.

    Reuses the existing promotion-lock key so this cannot run concurrently with
    the fast-promote path — one writer, one key.
    """

    def __init__(self, run_id: str, key: str = _AUTOMATED_PROMOTION_LOCK_KEY,
                 ttl: int = LEASE_TTL_SECONDS):
        self.run_id = run_id
        self.key = key
        self.ttl = ttl
        self.token = f'{run_id}:{time.time_ns()}'
        self._held = False

    def acquire(self) -> bool:
        self._held = bool(cache.add(self.key, self.token, self.ttl))
        return self._held

    def adopt(self, token: str) -> bool:
        """Re-attach to a lease we already own after a process restart."""
        if token and cache.get(self.key) == token:
            self.token = token
            self._held = True
            return True
        return False

    def held(self) -> bool:
        return self._held and cache.get(self.key) == self.token

    def assert_held(self) -> None:
        """Fence. Call immediately before EVERY mutation."""
        if not self.held():
            self._held = False
            raise LeaseLost(
                f'promotion lease {self.key} no longer held by run {self.run_id} '
                '— refusing to mutate fleet state'
            )

    def renew(self) -> bool:
        """Extend the TTL, but only if we still hold it. Never steals."""
        if cache.get(self.key) != self.token:
            self._held = False
            return False
        cache.set(self.key, self.token, self.ttl)
        return True

    def release(self) -> None:
        if cache.get(self.key) == self.token:
            cache.delete(self.key)
        self._held = False


def new_run(candidate: str, *, run_id: str, lease_token: str,
            last_good: str | None, upgrades: list | None = None) -> dict:
    return {
        'run_id': run_id,
        'phase': PHASE_DETECTED,
        'candidate': candidate,
        'last_good': last_good,
        'pre_switch_turns': {},
        'canary': None,
        'deadlines': {},
        'lease_token': lease_token,
        'upgrades': upgrades or [],
        'started_at': time.time(),
        'updated_at': time.time(),
    }


def load_run() -> dict | None:
    run = cache.get(HARNESS_UPGRADE_RUN_KEY)
    return run if isinstance(run, dict) and run.get('run_id') else None


def save_run(run: dict, lease: 'PromotionLease | None' = None) -> dict:
    """Persist run state. Fenced: a lease holder must still hold it to write."""
    if lease is not None:
        lease.assert_held()
    run['updated_at'] = time.time()
    cache.set(HARNESS_UPGRADE_RUN_KEY, run, RUN_TTL_SECONDS)
    return run


def clear_run(lease: 'PromotionLease | None' = None) -> None:
    if lease is not None:
        lease.assert_held()
    cache.delete(HARNESS_UPGRADE_RUN_KEY)


def run_needs_rollback(run: dict | None) -> bool:
    """True when a resumed run already mutated fleet state and did not finish.

    This is the restart contract: a run interrupted after the first switch must
    either resume to completion or roll back — it must never be silently
    abandoned mid-promotion.
    """
    if not run:
        return False
    return run.get('phase') in MUTATED_PHASES


def rollback_target(run: dict | None) -> str | None:
    """The only image a rollback may write, or None if unproven.

    Returning None is load-bearing: with no PROVEN last-good we must fail
    closed rather than POST an empty or arbitrary image and record it as
    rolled back. Hive's FleetRuntimeImageView rejects an empty image with 400,
    and the earlier Home implementation reported that failure as success.
    """
    if not run:
        return None
    last_good = (run.get('last_good') or '').strip()
    return last_good or None

# --- Canary evidence (HIVE-615) -------------------------------------------
#
# revi's P1 #4 bar: a canary is only VERIFIED by a post-switch **non-empty turn
# at a newer sequence**. Three ways a naive check passes without that, all of
# which this function must refuse:
#
#   1. the sequence advanced but every new event is empty -- a crash-loop or a
#      bare lifecycle record bumps `total` without the agent doing any work;
#   2. the pre-switch sequence is unknown, so "newer" is unprovable. Treating an
#      absent baseline as 0 makes ANY pre-existing activity look post-switch --
#      the canary then passes on evidence recorded before the image changed;
#   3. the activity read failed. An empty transcript from a transport error is
#      byte-identical to a genuinely silent agent, so it must not read as
#      either verified OR as proof of failure.
#
# Returns a verdict dict rather than a bool so the caller records WHY, and so
# 'unproven' is distinguishable from 'proven bad'. Only VERIFIED advances a
# promotion; UNPROVEN and REGRESSED both fail closed, differing in whether a
# rollback is warranted.

CANARY_VERIFIED = 'verified'
CANARY_UNPROVEN = 'unproven'
CANARY_PENDING = 'pending'


def _event_has_content(event: object) -> bool:
    """True when an activity event represents real agent output."""
    if not isinstance(event, dict):
        return False
    for key in ('text', 'content', 'message', 'output', 'result'):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, (list, dict)) and value:
            return True
    return False


def canary_turn_evidence(agent_id: str, pre_sequence: object,
                         *, activity: dict | None = None,
                         fetch=None) -> dict:
    """Read-only probe: did this agent produce a non-empty turn post-switch?

    Pure with respect to fleet state -- it never mutates and never touches the
    lease, so it can run as the ruling's "read-only async probe" while the
    owning command remains the sole writer.
    """
    verdict = {
        'agent_id': agent_id,
        'status': CANARY_PENDING,
        'pre_sequence': pre_sequence,
        'post_sequence': None,
        'new_events': 0,
        'content_events': 0,
        'reason': '',
    }

    if not isinstance(pre_sequence, int) or pre_sequence < 0:
        # Case 2. Without a numeric baseline captured BEFORE the switch there is
        # no such thing as "newer", and defaulting to 0 would silently accept
        # activity that predates the promotion.
        verdict['status'] = CANARY_UNPROVEN
        verdict['reason'] = 'no pre-switch sequence recorded; "newer" is unprovable'
        return verdict

    if activity is None:
        if fetch is None:
            from .runtime_fleet import get_runtime_agent_activity as fetch  # noqa: PLC0415
        try:
            activity = fetch(agent_id)
        except Exception as exc:  # noqa: BLE001 -- a probe must never raise into the orchestrator
            verdict['status'] = CANARY_UNPROVEN
            verdict['reason'] = 'activity read failed: %s' % exc
            return verdict

    if not isinstance(activity, dict) or not activity:
        # Case 3. {} is what get_runtime_agent_activity returns on ANY failure,
        # which is indistinguishable from a genuinely idle agent.
        verdict['status'] = CANARY_UNPROVEN
        verdict['reason'] = 'no activity payload; cannot distinguish transport failure from silence'
        return verdict

    total = activity.get('total')
    if not isinstance(total, int):
        verdict['status'] = CANARY_UNPROVEN
        verdict['reason'] = 'activity payload carries no numeric total'
        return verdict
    verdict['post_sequence'] = total

    if total <= pre_sequence:
        verdict['status'] = CANARY_PENDING
        verdict['reason'] = 'sequence has not advanced past the pre-switch baseline'
        return verdict

    events = activity.get('events')
    events = events if isinstance(events, list) else []
    new_events = events[-(total - pre_sequence):] if events else []
    verdict['new_events'] = len(new_events)
    content = [e for e in new_events if _event_has_content(e)]
    verdict['content_events'] = len(content)

    if not content:
        # Case 1. The sequence moved but nothing was produced.
        verdict['status'] = CANARY_PENDING
        verdict['reason'] = 'sequence advanced but no new event carries content'
        return verdict

    verdict['status'] = CANARY_VERIFIED
    verdict['reason'] = 'non-empty turn at sequence %d > %d' % (total, pre_sequence)
    return verdict


# --- Rollback execution (HIVE-615) ----------------------------------------
#
# The ruling: only the owning command rolls back, and an unverified rollback
# FAILS CLOSED. Both halves are load-bearing here, and each has a specific
# failure this guards against:
#
#   * unfenced write -- the pre-existing 300s lock had no renewal, so a canary
#     that waits on a real agent turn could outlive it while the code kept
#     mutating fleet state. Every write below is preceded by assert_held().
#   * unverified target -- the abandoned Home implementation POSTed an empty
#     image and recorded the resulting 400 as a successful rollback. Hive's
#     FleetRuntimeImageView rejects an empty image with 400, so "rolled back"
#     would have been written over a fleet that never moved.
#
# The phase is advanced to ROLLING_BACK *before* the write and to ROLLED_BACK
# only *after* it returns, so an interruption between them leaves the run in a
# phase that run_needs_rollback() still reports as mutated. Recording success
# first would make a crash mid-write look completed.

def execute_rollback(run: dict | None, lease: 'PromotionLease | None' = None,
                     *, apply=None) -> dict:
    """Roll the fleet back to the run's PROVEN last-good image.

    Returns a result dict; never raises for an unproven target, because the
    caller must be able to record WHY a rollback was refused. A lost lease DOES
    propagate (LeaseLost), since continuing to mutate without the fence is the
    one thing that must never be swallowed.
    """
    result = {
        'action': 'rollback',
        'status': PHASE_ROLLBACK_FAILED,
        'target': None,
        'reason': '',
    }
    if not run:
        result['reason'] = 'no run record; nothing to roll back'
        return result

    target = rollback_target(run)
    if not target:
        # Fail closed. Refusing is the correct outcome: with no proven
        # last-good there is no image we are entitled to write, and writing
        # something arbitrary is how a rollback becomes a second outage.
        result['reason'] = (
            'no proven last-good image; refusing to write an unverified target'
        )
        run['phase'] = PHASE_ROLLBACK_FAILED
        run['rollback'] = dict(result)
        save_run(run, lease)
        record_harness_upgrade_failure(run, result, lease)
        return result

    result['target'] = target

    # Fence, then declare intent, then write. If we die after this save the
    # phase is ROLLING_BACK, which run_needs_rollback() still counts as
    # mutated, so a resumed run retries rather than assuming completion.
    if lease is not None:
        lease.assert_held()
    run['phase'] = PHASE_ROLLING_BACK
    save_run(run, lease)

    if apply is None:
        apply = set_desired_runtime_image
    try:
        if lease is not None:
            lease.assert_held()
        applied = apply(target)
    except LeaseLost:
        raise
    except Exception as exc:  # noqa: BLE001
        result['reason'] = 'rollback write failed: %s' % exc
        run['phase'] = PHASE_ROLLBACK_FAILED
        run['rollback'] = dict(result)
        save_run(run, lease)
        record_harness_upgrade_failure(run, result, lease)
        return result

    result['status'] = PHASE_ROLLED_BACK
    result['reason'] = 'fleet target restored to proven last-good'
    result['applied'] = applied
    run['phase'] = PHASE_ROLLED_BACK
    run['rollback'] = dict(result)
    save_run(run, lease)
    return result


# --- Persistent failure fact (HIVE-615, architecture ruling v8 §5.2) -------
#
# ROLLBACK_FAILED stays terminal and non-retrying, but it must be
# CRITICAL-AND-LOUD: a dedicated alert pages from an UNRESOLVED PERSISTENT
# failure fact, and clearing requires proven fleet convergence plus an audited
# resolution -- explicitly NOT Job success, expiry, or acknowledgement.
#
# That rules out storing the fact in the run record, which carries
# RUN_TTL_SECONDS (24h). A run record that expires takes the alert with it,
# which is clear-by-expiry wearing a different hat: the fleet stays on the bad
# image and the page simply stops. The fact is therefore written with NO
# expiry and outlives the run it came from.
#
# ROLLING_BACK past its deadline, or lost mid-write, is recorded the same way:
# an interrupted rollback is not a resolved one.

HARNESS_UPGRADE_FAILURE_KEY = 'fleet:harness-upgrade-failure'


def record_harness_upgrade_failure(run: dict | None, result: dict,
                                   lease: 'PromotionLease | None' = None) -> dict:
    """Persist an unresolved failure fact with NO expiry.

    Deliberately not fenced on the lease: a lease LOST mid-rollback is itself a
    reportable failure, and requiring the fence to record it would make the
    worst case the one we cannot report.
    """
    run = run or {}
    fact = {
        'run_id': run.get('run_id'),
        'phase': run.get('phase'),
        'candidate': run.get('candidate'),
        'last_good': run.get('last_good'),
        'target': result.get('target'),
        'reason': result.get('reason') or '',
        'failed_at': time.time(),
        'resolution': None,
    }
    cache.set(HARNESS_UPGRADE_FAILURE_KEY, fact, None)  # None TTL == never expires
    return fact


def harness_upgrade_failure() -> dict | None:
    """The unresolved failure fact, or None. The alert/metric reads this."""
    fact = cache.get(HARNESS_UPGRADE_FAILURE_KEY)
    if not isinstance(fact, dict) or not fact:
        return None
    return fact if not fact.get('resolution') else None


def clear_harness_upgrade_failure(*, resolved_by: str, note: str,
                                  converged_image: str | None,
                                  expected_image: str | None) -> dict:
    """Clear ONLY on proven convergence plus an audited resolution.

    Every refusal below is a way the ruling could be satisfied on paper while
    the fleet is still broken:
      * no resolver or no note -> a bare acknowledgement, which the ruling
        explicitly rejects as a clearing condition;
      * convergence unknown or mismatched -> "the Job finished" is not "the
        fleet converged", and Job success is likewise rejected.
    """
    fact = cache.get(HARNESS_UPGRADE_FAILURE_KEY)
    if not isinstance(fact, dict) or not fact:
        return {'cleared': False, 'reason': 'no failure fact recorded'}

    resolved_by = (resolved_by or '').strip()
    note = (note or '').strip()
    if not resolved_by or not note:
        return {'cleared': False,
                'reason': 'audited resolution requires both a resolver and a note; '
                          'acknowledgement alone does not clear'}

    converged = (converged_image or '').strip()
    expected = (expected_image or '').strip()
    if not converged or not expected:
        return {'cleared': False,
                'reason': 'fleet convergence unproven; refusing to clear on an '
                          'unverified image'}
    if converged != expected:
        return {'cleared': False,
                'reason': 'fleet converged to %r, expected %r' % (converged, expected)}

    fact['resolution'] = {
        'resolved_by': resolved_by,
        'note': note,
        'converged_image': converged,
        'resolved_at': time.time(),
    }
    cache.set(HARNESS_UPGRADE_FAILURE_KEY, fact, None)
    return {'cleared': True, 'fact': fact}


def harness_upgrade_failure_to_prometheus() -> str:
    """Prometheus text for the unresolved-failure fact (HIVE-615 v9 §5.3).

    The ALERTING series are deliberately UNLABELED and always present:

        hive_harness_upgrade_failure       1 while unresolved, else 0
        hive_harness_upgrade_failed_at     unix ts, else 0

    Identity moves to a companion info series. That split is not cosmetic --
    with run_id/phase on the alerting gauge, a resolved failure does not drive
    its series to 0, it simply STOPS BEING EMITTED, and Prometheus keeps
    serving the last value until staleness (~5m). The healthy sample would be a
    different series (different label set) entirely, so the page would outlive
    the audited clear by minutes. A stable unlabeled series transitions 1 -> 0
    in place and clears immediately.

        hive_harness_upgrade_failure_info{run_id=..,phase=..} 1   (only when failing)

    Emitting the alerting series ALWAYS -- 0 when healthy -- is also load
    bearing: an absent-when-healthy metric makes "nothing is wrong" and "the
    exporter is broken" identical. Deploy owns the bounded absent() alert that
    covers the second case.

    `reason` is never a label: free text would explode cardinality. It stays on
    the durable fact for the responder to read.
    """
    fact = harness_upgrade_failure()
    lines = [
        '# HELP hive_harness_upgrade_failure Unresolved harness-upgrade failure (1=paging).',
        '# TYPE hive_harness_upgrade_failure gauge',
        '# HELP hive_harness_upgrade_failed_at Unix timestamp of the unresolved failure, 0 when none.',
        '# TYPE hive_harness_upgrade_failed_at gauge',
        '# HELP hive_harness_upgrade_failure_info Identity of the unresolved failure, absent when none.',
        '# TYPE hive_harness_upgrade_failure_info gauge',
    ]
    if not fact:
        lines.append('hive_harness_upgrade_failure 0')
        lines.append('hive_harness_upgrade_failed_at 0')
        return '\n'.join(lines) + '\n'

    def _lbl(value):
        # Positive selection, not a denylist. An allowlist states what a label
        # may contain; everything else is dropped by construction.
        return ''.join(
            c for c in str(value or '')
            if c.isalnum() or c in '._:/- '
        )[:64]

    lines.append('hive_harness_upgrade_failure 1')
    lines.append('hive_harness_upgrade_failed_at %d' % int(fact.get('failed_at') or 0))
    lines.append('hive_harness_upgrade_failure_info{run_id="%s",phase="%s"} 1'
                 % (_lbl(fact.get('run_id')), _lbl(fact.get('phase'))))
    return '\n'.join(lines) + '\n'
