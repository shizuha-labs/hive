#!/usr/bin/env python3
"""Storage-enforced ownership for Hive R1/R2 release mutations.

The stable Lease is the only authority.  Run-scoped requests and Jobs are
evidence/liveness artifacts and deliberately cannot grant write authority.
This module contains no free-form resource or command input: callers select a
ring and the allow-list below determines the complete protected surface.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterable
from typing import Any


LEASE_NAMESPACE = "origin"
LEASE_DURATION_SECONDS = 90
RENEW_INTERVAL_SECONDS = 15
HOLDER_KEYS = (
    "repo", "workflow", "run_id", "run_number", "job", "ring",
    "release_sha", "restored_sha",
)
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SAFE_ATOM_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,160}$")


class FenceError(RuntimeError):
    """A fail-closed ownership, API, or schema error."""


class Conflict(FenceError):
    """The Kubernetes API rejected a resourceVersion/test precondition.

    ownership_lost=True marks the holder/token variant: the observed lease
    belongs to a SUCCESSOR, so no observation latency is justified — the
    executor cancels immediately (our lease is gone; the killpg cannot
    outlive a lease we no longer own). Phase conflicts on a still-ours
    lease keep ownership_lost=False and keep the bounded committed-state
    observation (INV-F).
    """

    def __init__(self, message: str, *, ownership_lost: bool = False):
        super().__init__(message)
        self.ownership_lost = ownership_lost


@dataclasses.dataclass(frozen=True)
class Holder:
    repo: str
    workflow: str
    run_id: str
    run_number: str
    job: str
    ring: str
    release_sha: str
    restored_sha: str

    def __post_init__(self) -> None:
        if self.ring not in {"canary", "fleet"}:
            raise FenceError("ring must be canary or fleet")
        for key in HOLDER_KEYS[:-2]:
            value = getattr(self, key)
            if not SAFE_ATOM_RE.fullmatch(value):
                raise FenceError(f"invalid holder field: {key}")
        for key in ("release_sha", "restored_sha"):
            if not SHA_RE.fullmatch(getattr(self, key)):
                raise FenceError(f"invalid holder SHA: {key}")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Holder":
        if set(value) != set(HOLDER_KEYS):
            raise FenceError("holder tuple has unknown or missing fields")
        return cls(**{key: str(value[key]) for key in HOLDER_KEYS})

    @classmethod
    def from_env(cls) -> "Holder":
        return cls.from_dict({key: os.environ[f"RING_{key.upper()}"] for key in HOLDER_KEYS})

    def serialized(self) -> str:
        return json.dumps(dataclasses.asdict(self), sort_keys=True, separators=(",", ":"))

    @property
    def generation(self) -> str:
        # Full tuple remains stored in holderIdentity; this bounded digest-like
        # label value is attributable without exposing credentials.
        import hashlib
        return hashlib.sha256(self.serialized().encode()).hexdigest()

    @property
    def lease_name(self) -> str:
        return f"hive-ring-owner-{self.ring}"


def parse_lease_microtime(value: str) -> dt.datetime | None:
    """Parse a Kubernetes MicroTime. None if missing or unreadable (fail-closed)."""
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


@dataclasses.dataclass(frozen=True)
class FenceState:
    holder: str
    token: int
    phase: str
    resource_version: str
    duration: int
    renew_time: dt.datetime | None = None

    def kube_expired(self, *, now: dt.datetime | None = None) -> bool:
        """True when the Kubernetes Lease holder is past renewTime+duration.

        Recover watches an *unchanged record* with LeaseObserver (wall clock
        of the writer is not authority while the owner is alive). Acquire of a
        *new* generation may treat a kube-expired non-terminal Lease as
        abandoned: the owner is gone and cannot renew. Missing/unreadable
        renewTime is not expired (fail-closed — do not steal a live fence).
        """
        if self.renew_time is None:
            return False
        current = now or dt.datetime.now(dt.timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=dt.timezone.utc)
        return current >= self.renew_time + dt.timedelta(seconds=max(int(self.duration), 0))

    @classmethod
    def from_lease(cls, lease: dict[str, Any]) -> "FenceState":
        try:
            metadata = lease["metadata"]
            annotations = metadata["annotations"]
            spec = lease["spec"]
            return cls(
                holder=spec["holderIdentity"],
                token=int(annotations["shizuha.io/fence-token"]),
                phase=annotations["shizuha.io/phase"],
                resource_version=str(metadata["resourceVersion"]),
                duration=int(spec["leaseDurationSeconds"]),
                renew_time=parse_lease_microtime(str(spec.get("renewTime") or "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FenceError("malformed ring Lease") from exc


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _now() -> dt.datetime:
    """Wall-clock hook for the renew window guard.

    Deterministic tests (HIVE-2516 P1) monkeypatch this instead of
    dt.datetime to advance simulated time across real 45s timeouts.
    """
    return dt.datetime.now(dt.timezone.utc)


class Kubectl:
    """Small JSON-only kubectl adapter; stderr is bounded and never logged raw."""

    # Read-only verbs are safe to retry: a timeout on a probe is transient
    # API latency, not evidence of a fence violation. Mutating verbs are
    # never retried here — CAS conflicts surface as Conflict via the caller.
    READ_ONLY_VERBS = frozenset({
        "get", "auth", "api-resources", "api-versions", "version",
        "cluster-info", "explain", "top", "diff",
    })
    # Flags whose NEXT arg is a value (e.g. -n origin): must be skipped so a
    # namespace/output value is never mistaken for the command verb.
    VALUE_FLAGS = frozenset({
        "-n", "--namespace", "-o", "--output", "-l", "--selector",
        "--field-selector", "--context", "--kubeconfig", "-s", "--server",
        "--cluster", "--user", "--token", "--as", "--for", "-f", "--filename",
        "--patch", "--from", "-k", "--kustomize", "--cache-dir",
    })
    TIMEOUT_RETRIES = 3
    TIMEOUT_BACKOFF_SECONDS = 10.0

    def __init__(self, binary: str | None = None):
        self.binary = binary or os.environ.get("RING_KUBECTL_REAL", "/usr/local/bin/kubectl")

    @classmethod
    def _is_read_only(cls, args: list[str]) -> bool:
        skip_next = False
        for item in args:
            if skip_next:
                skip_next = False
                continue
            if item.startswith("-"):
                if "=" not in item and item in cls.VALUE_FLAGS:
                    skip_next = True
                continue
            return item in cls.READ_ONLY_VERBS
        return False

    def run(self, args: list[str], *, stdin: str | None = None, check: bool = True,
            deadline: dt.datetime | None = None,
            read_attempts: int | None = None) -> subprocess.CompletedProcess[str]:
        # HIVE-2516 P1 (second review): callers inside the renewal window pass
        # an absolute deadline — the per-attempt subprocess timeout is clamped
        # to the remaining time and the read-retry loop declines attempts that
        # cannot start inside it. This is what bounds the whole
        # renewal/cancellation operation (entry reads, first mutation,
        # ambiguity re-reads) instead of only the retry starts.
        process_timeout = 45
        for item in args:
            match = re.fullmatch(r"--timeout=([0-9]+)s", item)
            if match:
                # The outer watchdog must outlive kubectl's own bounded wait.
                # Hive rollouts intentionally bake each replica for 180s, so a
                # fixed 45s subprocess budget makes recovery fail while the
                # healthy rollback is still converging.
                process_timeout = max(
                    process_timeout,
                    min(int(match.group(1)) + 15, 1860),
                )
        # Transient kube-apiserver latency must not fail a production ring:
        # retry read-only probes with backoff (3 attempts, ~2 min total
        # window) before surfacing the timeout. Mutating commands and
        # definitive API errors still fail on first occurrence.
        attempts = read_attempts if read_attempts is not None else (
            self.TIMEOUT_RETRIES if self._is_read_only(args) else 1)
        backoff = self.TIMEOUT_BACKOFF_SECONDS
        for attempt in range(1, attempts + 1):
            timeout = process_timeout
            if deadline is not None:
                remaining = (deadline - _now()).total_seconds()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(cmd=" ".join(args[:2]), timeout=process_timeout)
                timeout = min(timeout, remaining)
            try:
                result = subprocess.run(
                    [self.binary, *args], input=stdin, text=True, capture_output=True,
                    timeout=timeout,
                    env={**os.environ, "KUBECTL_EXTERNAL_DIFF": "/bin/false"},
                )
                break
            except subprocess.TimeoutExpired:
                if attempt == attempts:
                    raise
                if deadline is not None and deadline <= _now():
                    raise
                time.sleep(backoff)
                backoff *= 2.5
        if check and result.returncode:
            # HIVE-2819 (D1): preserve the full bounded stderr and the exact
            # command shape. D1's 422 reclassification still treats the generic
            # k3s text as a CAS conflict so commit() retries it.
            detail = result.stderr[-4000:].replace("\n", " ").strip()
            command_shape = " ".join(args)[:300]
            generic_422 = "the server rejected our request due to an error in our request" in detail
            cas_like = "Conflict" in detail or "test failed" in detail or "object has been modified" in detail or generic_422
            if cas_like:
                # PLAT-11234 (HIVE-2819 residual): kubectl summarizes generic
                # 422s — the full Status body (.details/.causes) never reaches
                # plain stderr, so #455's bounded capture still records the
                # truncated generic text (run 3451). CAS-class failures have
                # not mutated anything (create is atomic; 409 means no change),
                # so ONE verbose re-run of the same command is safe and kubectl
                # -v=6 prints the full response body. Keep whichever capture is
                # richer; the re-run's own failure must not mask the original.
                try:
                    verbose = subprocess.run(
                        [self.binary, "-v=6", *args], input=stdin, text=True,
                        capture_output=True, timeout=process_timeout,
                        env={**os.environ, "KUBECTL_EXTERNAL_DIFF": "/bin/false"},
                    )
                    vdetail = verbose.stderr[-4000:].replace("\n", " ").strip()
                    if len(vdetail) > len(detail):
                        detail = vdetail
                except subprocess.TimeoutExpired:
                    pass
                raise Conflict(f"Kubernetes CAS conflict: {detail}")
            raise FenceError(
                f"Kubernetes API failure rc={result.returncode}: {detail} "
                f"[command: kubectl {command_shape}]"
            )
        return result

    def get_lease(self, name: str, *, deadline: dt.datetime | None = None,
                  read_attempts: int | None = None) -> dict[str, Any]:
        result = self.run(["-n", LEASE_NAMESPACE, "get", "lease", name, "-o", "json"], check=False,
                          deadline=deadline, read_attempts=read_attempts)
        if result.returncode:
            # Absence is meaningful only when kubectl's typed status says so.
            if "NotFound" in result.stderr:
                raise KeyError(name)
            raise FenceError("ring Lease read failed; absence is ambiguous")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise FenceError("ring Lease returned malformed JSON") from exc

    def create_lease(self, holder: Holder) -> FenceState:
        body = {
            "apiVersion": "coordination.k8s.io/v1", "kind": "Lease",
            "metadata": {
                "name": holder.lease_name, "namespace": LEASE_NAMESPACE,
                "annotations": {
                    "shizuha.io/fence-token": "1", "shizuha.io/phase": "acquiring",
                    "shizuha.io/ring-generation": holder.generation,
                },
            },
            "spec": {
                "holderIdentity": holder.serialized(),
                "leaseDurationSeconds": LEASE_DURATION_SECONDS,
                "acquireTime": utc_now(), "renewTime": utc_now(), "leaseTransitions": 1,
            },
        }
        result = self.run(["create", "-f", "-", "-o", "json"], stdin=json.dumps(body))
        return FenceState.from_lease(json.loads(result.stdout))

    def json_patch(self, namespace: str, resource: str, name: str, operations: list[dict[str, Any]],
                   deadline: dt.datetime | None = None) -> dict[str, Any]:
        # ConfigMap evidence patches can approach the object-size limit. Stream
        # the patch through stdin so it never crosses Linux's argv ARG_MAX.
        patch = json.dumps(operations, separators=(",", ":"))
        result = self.run([
            "-n", namespace, "patch", resource, name, "--type=json", "-o", "json",
            "--patch-file=/dev/stdin",
        ], stdin=patch, deadline=deadline)
        return json.loads(result.stdout)


class Fence:
    ACTIVE_PHASES = {"acquiring", "active", "rollback-started", "evidence-pending", "recovering"}
    TERMINAL_PHASES = {"committed", "completed"}

    def __init__(self, holder: Holder, api: Kubectl | Any):
        self.holder = holder
        self.api = api
        self.token: int | None = None

    def read(self, *, deadline: dt.datetime | None = None,
             read_attempts: int | None = None) -> FenceState:
        lease = (self.api.get_lease(self.holder.lease_name, deadline=deadline, read_attempts=read_attempts)
                 if deadline is not None or read_attempts is not None
                 else self.api.get_lease(self.holder.lease_name))
        return FenceState.from_lease(lease)

    def window_deadline(self) -> dt.datetime | None:
        """Last-known renewal-window deadline (renewTime + duration - margin).

        The executor uses this to bound its post-Conflict committed-state
        observation so the killpg it issues can never outlive the lease.
        """
        return getattr(self, "_window_deadline", None)

    def _observe_window(self, state: "FenceState") -> None:
        """HIVE-2516 third review: refresh the cached window deadline from
        any freshly-read lease state — acquisition, the renew entry read,
        and every successful renewal. A stale cache (last acquisition's
        window) makes the next tick decline its GET against a deadline that
        no longer governs, killing a healthy child up to a full window
        early; a never-set cache (first-entry Conflict) leaves the
        executor's observation unbounded. Both are closed by observing the
        window at every successful read."""
        try:
            self._window_deadline = self._renew_deadline(state)
        except Conflict:
            pass  # unreadable renewTime: keep the previous cache (fail-closed)

    def acquire(self, *, expected_prior_token: int | None = None) -> FenceState:
        try:
            lease = self.api.get_lease(self.holder.lease_name)
        except KeyError:
            if expected_prior_token not in (None, 0):
                raise Conflict("ring Lease disappeared after request submission")
            state = self.api.create_lease(self.holder)
            self.token = state.token
            self._observe_window(state)
            return state
        previous = FenceState.from_lease(lease)
        if expected_prior_token is not None and previous.token != expected_prior_token:
            raise Conflict("ring generation advanced after request submission")
        if previous.phase not in self.TERMINAL_PHASES:
            # A dead recover Job can leave phase=recovering forever (TTL GC
            # deletes the Job; the stable Lease is never deleted). Execute
            # used to Conflict here even after kube expiry, so fleet could
            # not promote (5cc6cf6 canary / a85cceb fleet, 2026-09-02).
            if not previous.kube_expired():
                raise Conflict(f"ring already owned in phase={previous.phase}")
        next_token = previous.token + 1
        patch = self._lease_tests(previous) + [
            {"op": "replace", "path": "/spec/holderIdentity", "value": self.holder.serialized()},
            {"op": "replace", "path": "/spec/leaseDurationSeconds", "value": LEASE_DURATION_SECONDS},
            {"op": "replace", "path": "/spec/acquireTime", "value": utc_now()},
            {"op": "replace", "path": "/spec/renewTime", "value": utc_now()},
            {"op": "replace", "path": "/metadata/annotations/shizuha.io~1fence-token", "value": str(next_token)},
            {"op": "replace", "path": "/metadata/annotations/shizuha.io~1phase", "value": "acquiring"},
            {"op": "replace", "path": "/metadata/annotations/shizuha.io~1ring-generation", "value": self.holder.generation},
        ]
        updated = self.api.json_patch(LEASE_NAMESPACE, "lease", self.holder.lease_name, patch)
        state = FenceState.from_lease(updated)
        self.token = state.token
        self._observe_window(state)
        return state

    def validate(self, allowed_phases: Iterable[str] = ACTIVE_PHASES, *,
                 deadline: dt.datetime | None = None,
                 read_attempts: int | None = None) -> FenceState:
        if self.token is None:
            raise FenceError("fence token not initialized")
        state = self.read(deadline=deadline, read_attempts=read_attempts)
        # Observe the window ONLY from a still-ours read (fourth review): a
        # successor's renewTime must never extend OUR kill deadline — after
        # ownership loss the executor cancels on OUR clock (immediately),
        # while the same-owner committed case keeps its observed window for
        # the bounded INV-F observation.
        if state.holder != self.holder.serialized() or state.token != self.token:
            raise Conflict("ring holder/token changed", ownership_lost=True)
        self._observe_window(state)
        if state.phase not in set(allowed_phases):
            raise Conflict(f"ring phase {state.phase} is not writable")
        return state

    # HIVE-2516 (runs 2665/2667, 2026-09-08): one transient kube-apiserver
    # latency spike during a renew window used to be fatal — the renew PATCH
    # is a mutating verb (single attempt in the Kubectl adapter), so a single
    # TimeoutExpired killed the renew loop, the executor process group, and
    # the fail-closed path rolled back a release that may already have
    # converged. Retry the renew within the lease window instead: after a
    # successful renew there are ~75s of slack (duration 90s, tick 15s), so
    # bounded retries fit comfortably. A Conflict on a RETRY is ambiguous —
    # the previous attempt may have landed with its response lost — so
    # re-read and let ownership be the verdict: still ours means the CAS loss
    # was our own resourceVersion advance (fresh state is authoritative); a
    # successor takeover still raises Conflict. The window guard keeps the
    # fail-closed contract: once a fresh renewTime could no longer land
    # before the current lease lapses, stop retrying and surface the timeout.
    RENEW_RETRY_ATTEMPTS = 3
    RENEW_RETRY_BACKOFF_SECONDS = 5.0
    RENEW_SAFETY_MARGIN_SECONDS = 20
    # HIVE-2516 P1 review (rei): the window guard must bound IN-FLIGHT work,
    # not merely attempt starts. A mutating PATCH occupies the subprocess up
    # to Kube.run's 45s timeout, so an attempt "permitted to start" just
    # before the old deadline still runs ~45s past lease expiry — failure
    # surfaces at t=120 for expiry t=90 (rei's deterministic repro) and the
    # executor cancels its child into a successor's acquire window. Every
    # retry attempt AND the ambiguous-CAS re-read must be able to FINISH
    # before the deadline; when nothing fits, fail immediately without
    # sleeping so cancellation lands inside our own lease.
    RENEW_ATTEMPT_BUDGET_SECONDS = 45

    def renew(self) -> FenceState:
        # HIVE-2516 P1 (second review): the WHOLE renewal operation is
        # deadline-bounded — the entry read (single attempt, clamped to the
        # last-known window), the first mutation (clamped to this window's
        # guard deadline), and the ambiguity re-read. The executor's
        # cancellation can therefore never outlive the lease.
        cached = self.window_deadline()
        state = (self.validate(deadline=cached, read_attempts=1)
                 if cached is not None else self.validate(read_attempts=1))
        self._window_deadline = self._renew_deadline(state)
        last_exc: BaseException | None = None
        for attempt in range(1, type(self).RENEW_RETRY_ATTEMPTS + 1):
            if attempt > 1 and self._renew_window_exhausted(state):
                break
            try:
                patch = self._lease_tests(state) + [
                    {"op": "replace", "path": "/spec/renewTime", "value": utc_now()},
                ]
                updated = self.api.json_patch(
                    LEASE_NAMESPACE, "lease", self.holder.lease_name, patch,
                    deadline=self._renew_deadline(state),
                )
                fresh = FenceState.from_lease(updated)
                self._observe_window(fresh)
                return fresh
            except subprocess.TimeoutExpired as exc:
                last_exc = exc
                # No attempt (and not even the backoff sleep) may run past
                # the deadline: surface the timeout now — the executor's
                # child cancellation stays inside our own lease.
                if self._renew_window_exhausted(state):
                    raise
                room = (self._renew_deadline(state) - _now()).total_seconds()
                time.sleep(min(
                    type(self).RENEW_RETRY_BACKOFF_SECONDS * attempt,
                    max(room - type(self).RENEW_ATTEMPT_BUDGET_SECONDS, 0.0),
                ))
            except Conflict:
                if attempt == 1:
                    raise
                # Ambiguous CAS on a retry: re-read; ownership is the verdict.
                # The re-read is itself a 45s-capable api call — it is bounded
                # to the window deadline (single attempt), and when even that
                # cannot finish inside the window, fail closed without it.
                if self._renew_window_exhausted(state):
                    raise
                return self.validate(deadline=self._renew_deadline(state), read_attempts=1)
        assert last_exc is not None
        raise last_exc

    def _renew_deadline(self, state: FenceState) -> dt.datetime:
        if state.renew_time is None:
            raise Conflict("renew window unreadable")
        return state.renew_time + dt.timedelta(
            seconds=LEASE_DURATION_SECONDS - type(self).RENEW_SAFETY_MARGIN_SECONDS,
        )

    def _renew_window_exhausted(self, state: FenceState) -> bool:
        if state.renew_time is None:
            # Fail-closed: an unreadable renewTime means we cannot bound the
            # window — do not push luck past an unmeasurable deadline.
            return True
        # An attempt must be able to FINISH (in-flight budget included)
        # before the deadline — starting inside the window is not enough.
        return _now() + dt.timedelta(
            seconds=type(self).RENEW_ATTEMPT_BUDGET_SECONDS,
        ) > self._renew_deadline(state)

    def phase(self, next_phase: str, allowed_from: Iterable[str]) -> FenceState:
        state = self.validate(allowed_from)
        patch = self._lease_tests(state) + [
            {"op": "replace", "path": "/metadata/annotations/shizuha.io~1phase", "value": next_phase},
        ]
        updated = self.api.json_patch(LEASE_NAMESPACE, "lease", self.holder.lease_name, patch)
        return FenceState.from_lease(updated)

    def takeover(self, predecessor: FenceState, recovery: Holder) -> "Fence":
        if predecessor.phase in self.TERMINAL_PHASES:
            raise Conflict("completed owner cannot be recovered")
        next_token = predecessor.token + 1
        patch = self._lease_tests(predecessor) + [
            {"op": "replace", "path": "/spec/holderIdentity", "value": recovery.serialized()},
            {"op": "replace", "path": "/spec/acquireTime", "value": utc_now()},
            {"op": "replace", "path": "/spec/renewTime", "value": utc_now()},
            {"op": "replace", "path": "/metadata/annotations/shizuha.io~1fence-token", "value": str(next_token)},
            {"op": "replace", "path": "/metadata/annotations/shizuha.io~1phase", "value": "recovering"},
            {"op": "replace", "path": "/metadata/annotations/shizuha.io~1ring-generation", "value": recovery.generation},
        ]
        updated = self.api.json_patch(LEASE_NAMESPACE, "lease", recovery.lease_name, patch)
        result = Fence(recovery, self.api)
        result.token = FenceState.from_lease(updated).token
        return result

    @staticmethod
    def _lease_tests(state: FenceState) -> list[dict[str, Any]]:
        return [
            {"op": "test", "path": "/metadata/resourceVersion", "value": state.resource_version},
            {"op": "test", "path": "/spec/holderIdentity", "value": state.holder},
            {"op": "test", "path": "/metadata/annotations/shizuha.io~1fence-token", "value": str(state.token)},
            {"op": "test", "path": "/metadata/annotations/shizuha.io~1phase", "value": state.phase},
        ]

    def target_tests(self, target: dict[str, Any]) -> list[dict[str, Any]]:
        state = self.validate()
        metadata = target.get("metadata", {})
        annotations = metadata.get("annotations", {})
        if annotations.get("shizuha.io/ring-generation") != self.holder.generation:
            raise Conflict("target belongs to another generation")
        if annotations.get("shizuha.io/fence-token") != str(state.token):
            raise Conflict("target carries a stale fence token")
        return [
            {"op": "test", "path": "/metadata/resourceVersion", "value": str(metadata["resourceVersion"])},
            {"op": "test", "path": "/metadata/annotations/shizuha.io~1ring-generation", "value": self.holder.generation},
            {"op": "test", "path": "/metadata/annotations/shizuha.io~1fence-token", "value": str(state.token)},
        ]


class LeaseObserver:
    """Expire only after a locally monotonic unchanged-record window."""

    def __init__(self, read: Callable[[], FenceState], monotonic: Callable[[], float] = time.monotonic):
        self.read = read
        self.monotonic = monotonic
        self.serialized: tuple[str, int, str, str] | None = None
        self.observed_at: float | None = None

    def observe(self) -> tuple[FenceState, bool]:
        # Any read/auth/transport/malformed/404 failure propagates: it never
        # becomes permission for early takeover.
        state = self.read()
        current = (state.holder, state.token, state.phase, state.resource_version)
        now = self.monotonic()
        if current != self.serialized:
            self.serialized = current
            self.observed_at = now
            return state, False
        assert self.observed_at is not None
        return state, now - self.observed_at >= state.duration

    def relist_after_gap(self) -> None:
        # 410/history gaps discard the observation window.
        self.serialized = None
        self.observed_at = None


def ring_order(rings: Iterable[str]) -> list[str]:
    order = {"canary": 0, "fleet": 1}
    result = list(rings)
    if any(item not in order for item in result):
        raise FenceError("unknown ring")
    if result != sorted(result, key=order.__getitem__):
        raise FenceError("cross-ring acquisition must be canary then fleet")
    return result
