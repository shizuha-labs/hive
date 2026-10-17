"""HIVE-450 hosted-claw WAN-egress regression smoke.

Runs from the Hive control-plane container and execs a tiny Node.js probe inside
an internal hosted-claw test pod. The probe asserts that public HTTPS remains
reachable while private/cluster/metadata/tailnet TCP destinations cannot be
opened from the same claw namespace.
"""
from __future__ import annotations

import argparse
import json
import os
import textwrap
from dataclasses import dataclass
from typing import Iterable, Sequence

DEFAULT_NAMESPACE_LABEL_SELECTOR = (
    "app.kubernetes.io/managed-by=shizuha-hive,shizuha.io/wan-egress-smoke=true"
)
DEFAULT_POD_LABEL_SELECTOR = "app=claw"
DEFAULT_PUBLIC_URL = "https://github.com"
DEFAULT_PUBLIC_STATUSES = (200, 301, 302)
DEFAULT_BLOCKED_TARGETS = (
    "cluster-service=10.43.0.1:443",
    "cluster-pod=10.42.0.1:80",
    "rfc1918=192.168.1.1:80",
    "metadata=169.254.169.254:80",
    "tailnet=100.64.0.1:443",
)
# PLAT-8489: the s1 bridge services (ori MCP :9500, google-drive MCP :9600,
# fleet-control socat :8016) are the one deliberate tailnet carve-out in
# ``fleet-agent-allow-egress``. The 2026-09-10 fleet-wide MCP wedge clipped
# exactly these ports while every web-egress assertion stayed green — so the
# smoke must assert them as MUST-CONNECT, not only assert what must stay dark.
DEFAULT_ALLOWED_TARGETS = (
    "s1-ori-mcp=100.64.0.3:9500",
    "s1-drive-mcp=100.64.0.3:9600",
    "s1-fleet-control=100.64.0.3:8016",
)


@dataclass(frozen=True)
class ProbeTarget:
    namespace: str
    pod: str


@dataclass(frozen=True)
class WanEgressProbeConfig:
    namespace: str | None = None
    pod: str | None = None
    namespace_label_selector: str = DEFAULT_NAMESPACE_LABEL_SELECTOR
    pod_label_selector: str = DEFAULT_POD_LABEL_SELECTOR
    public_url: str = DEFAULT_PUBLIC_URL
    public_statuses: tuple[int, ...] = DEFAULT_PUBLIC_STATUSES
    blocked_targets: tuple[str, ...] = DEFAULT_BLOCKED_TARGETS
    allowed_targets: tuple[str, ...] = DEFAULT_ALLOWED_TARGETS
    timeout_ms: int = 5000

    @classmethod
    def from_env(cls) -> "WanEgressProbeConfig":
        return cls(
            namespace=_env("HIVE_WAN_EGRESS_PROBE_NAMESPACE") or None,
            pod=_env("HIVE_WAN_EGRESS_PROBE_POD") or None,
            namespace_label_selector=_env("HIVE_WAN_EGRESS_PROBE_NAMESPACE_SELECTOR", DEFAULT_NAMESPACE_LABEL_SELECTOR),
            pod_label_selector=_env("HIVE_WAN_EGRESS_PROBE_POD_SELECTOR", DEFAULT_POD_LABEL_SELECTOR),
            public_url=_env("HIVE_WAN_EGRESS_PUBLIC_URL", DEFAULT_PUBLIC_URL),
            public_statuses=_parse_statuses(_env("HIVE_WAN_EGRESS_PUBLIC_STATUSES"), DEFAULT_PUBLIC_STATUSES),
            blocked_targets=tuple(_parse_targets(_env("HIVE_WAN_EGRESS_BLOCKED_TARGETS"))) or DEFAULT_BLOCKED_TARGETS,
            allowed_targets=tuple(_parse_targets(_env("HIVE_WAN_EGRESS_ALLOWED_TARGETS"))) or DEFAULT_ALLOWED_TARGETS,
            timeout_ms=int(_env("HIVE_WAN_EGRESS_PROBE_TIMEOUT_MS", "5000")),
        )


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _parse_statuses(raw: str, default: tuple[int, ...]) -> tuple[int, ...]:
    if not raw:
        return default
    return tuple(int(part.strip()) for part in raw.split(",") if part.strip())


def _parse_targets(raw: str) -> Iterable[str]:
    if not raw:
        return ()
    return (part.strip() for part in raw.split(",") if part.strip())


def get_k8s_core_api():
    """Return CoreV1Api using in-cluster config first, local kubeconfig second."""
    from kubernetes import client, config

    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.CoreV1Api()


def select_probe_target(core_v1, cfg: WanEgressProbeConfig) -> ProbeTarget:
    """Select the explicitly configured or internal-smoke-labelled claw pod.

    We deliberately do NOT fall back to an arbitrary ``claw-*`` namespace: the
    smoke must never interact with external/customer claws. Operators can either
    set ``HIVE_WAN_EGRESS_PROBE_NAMESPACE`` to a known internal test claw, or add
    ``shizuha.io/wan-egress-smoke=true`` to that test namespace.
    """
    namespace = cfg.namespace
    if not namespace:
        namespaces = core_v1.list_namespace(label_selector=cfg.namespace_label_selector).items
        if not namespaces:
            raise RuntimeError(
                "no hosted-claw WAN-egress smoke namespace found; set "
                "HIVE_WAN_EGRESS_PROBE_NAMESPACE or label one internal test claw namespace "
                f"with {cfg.namespace_label_selector!r}"
            )
        if len(namespaces) > 1:
            names = ", ".join(sorted(ns.metadata.name for ns in namespaces))
            raise RuntimeError(f"multiple WAN-egress smoke namespaces match; set HIVE_WAN_EGRESS_PROBE_NAMESPACE: {names}")
        namespace = namespaces[0].metadata.name

    pod = cfg.pod
    if not pod:
        pods = core_v1.list_namespaced_pod(namespace, label_selector=cfg.pod_label_selector).items
        running = [p for p in pods if p.status.phase == "Running"]
        if not running:
            raise RuntimeError(
                f"no Running hosted-claw pod in {namespace!r} matching selector {cfg.pod_label_selector!r}"
            )
        running.sort(key=lambda p: p.metadata.name)
        pod = running[0].metadata.name
    return ProbeTarget(namespace=namespace, pod=pod)


def build_node_probe_script(cfg: WanEgressProbeConfig) -> str:
    """Build the Node.js script executed inside the hosted-claw pod."""
    public_statuses = list(cfg.public_statuses)
    blocked_targets = list(cfg.blocked_targets)
    allowed_targets = list(cfg.allowed_targets)
    return textwrap.dedent(
        f"""
        const https = require('https');
        const http = require('http');
        const net = require('net');
        const publicUrl = {json.dumps(cfg.public_url)};
        const publicStatuses = new Set({json.dumps(public_statuses)});
        const blockedTargets = {json.dumps(blocked_targets)};
        const allowedTargets = {json.dumps(allowed_targets)};
        const timeoutMs = {int(cfg.timeout_ms)};

        function checkPublic() {{
          return new Promise((resolve) => {{
            const lib = publicUrl.startsWith('http://') ? http : https;
            const req = lib.get(publicUrl, {{timeout: timeoutMs}}, (res) => {{
              res.resume();
              resolve({{name: 'public-https', ok: publicStatuses.has(res.statusCode), status: res.statusCode}});
            }});
            req.on('timeout', () => req.destroy(new Error('timeout')));
            req.on('error', (err) => resolve({{name: 'public-https', ok: false, error: String(err.message || err)}}));
          }});
        }}

        function parseTarget(raw) {{
          const eq = raw.indexOf('=');
          const name = eq >= 0 ? raw.slice(0, eq) : raw;
          const endpoint = eq >= 0 ? raw.slice(eq + 1) : raw;
          const colon = endpoint.lastIndexOf(':');
          return {{name, host: endpoint.slice(0, colon), port: Number(endpoint.slice(colon + 1))}};
        }}

        function checkBlocked(raw) {{
          const target = parseTarget(raw);
          return new Promise((resolve) => {{
            const sock = net.createConnection({{host: target.host, port: target.port}});
            let settled = false;
            function finish(ok, extra) {{
              if (settled) return;
              settled = true;
              sock.destroy();
              resolve(Object.assign({{name: target.name, host: target.host, port: target.port, ok}}, extra || {{}}));
            }}
            sock.setTimeout(timeoutMs, () => finish(true, {{blocked: 'timeout'}}));
            sock.on('connect', () => finish(false, {{error: 'unexpected TCP connect'}}));
            sock.on('error', (err) => finish(true, {{blocked: String(err.code || err.message || err)}}));
          }});
        }}

        function checkAllowed(raw) {{
          // PLAT-8489: s1 bridge ports are the deliberate carve-out; a clip
          // here is the outage signature, so a failed connect fails the smoke.
          const target = parseTarget(raw);
          return new Promise((resolve) => {{
            const sock = net.createConnection({{host: target.host, port: target.port}});
            let settled = false;
            function finish(ok, extra) {{
              if (settled) return;
              settled = true;
              sock.destroy();
              resolve(Object.assign({{name: target.name, host: target.host, port: target.port, ok}}, extra || {{}}));
            }}
            sock.setTimeout(timeoutMs, () => finish(false, {{error: 'timeout (egress clip?)'}}));
            sock.on('connect', () => finish(true, {{allowed: 'tcp connect'}}));
            sock.on('error', (err) => finish(false, {{error: String(err.code || err.message || err)}}));
          }});
        }}

        (async () => {{
          const results = [await checkPublic()];
          for (const raw of blockedTargets) results.push(await checkBlocked(raw));
          for (const raw of allowedTargets) results.push(await checkAllowed(raw));
          const failures = results.filter((r) => !r.ok);
          console.log(JSON.stringify({{ok: failures.length === 0, results}}, null, 2));
          process.exit(failures.length ? 1 : 0);
        }})().catch((err) => {{
          console.error(err && err.stack ? err.stack : String(err));
          process.exit(1);
        }});
        """
    ).strip()


def run_probe(core_v1, cfg: WanEgressProbeConfig) -> tuple[bool, dict]:
    target = select_probe_target(core_v1, cfg)
    script = build_node_probe_script(cfg)
    try:
        raw = _exec_pod(
            core_v1,
            target.namespace,
            target.pod,
            command=["node", "-e", script],
        )
    except Exception as exc:
        status = getattr(exc, "status", "")
        reason = getattr(exc, "reason", str(exc))
        detail = f"{status} {reason}".strip()
        raise RuntimeError(f"exec into {target.namespace}/{target.pod} failed: {detail}") from exc

    payload = _extract_json(raw)
    payload["target"] = {"namespace": target.namespace, "pod": target.pod}
    return bool(payload.get("ok")), payload


def _exec_pod(core_v1, namespace: str, pod: str, *, command: Sequence[str]) -> str:
    from kubernetes import stream

    return stream.stream(
        core_v1.connect_get_namespaced_pod_exec,
        pod,
        namespace,
        command=command,
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
    )


def _extract_json(raw: str) -> dict:
    """Parse the probe payload out of the exec stream output.

    HIVE-537: the Kubernetes Python ``stream.stream(...)`` helper can hand back
    the payload as a PYTHON-LITERAL dict string (single quotes, ``True``/
    ``False``/``None``) instead of raw JSON, so ``json.loads`` alone rejected a
    green probe every 15 minutes. Try strict JSON first, then fall back to
    ``ast.literal_eval`` — safe (literals only, no execution) and exactly
    matches the repr-shaped output observed live.
    """
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < start:
        raise RuntimeError(f"probe did not return JSON: {raw[:500]}")
    candidate = raw[start:end + 1]
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        import ast
        try:
            payload = ast.literal_eval(candidate)
        except (ValueError, SyntaxError) as exc:
            raise RuntimeError(
                f"probe output is neither JSON nor a Python-literal dict: {raw[:500]}"
            ) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"probe returned a non-object payload: {raw[:500]}")
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the HIVE-450 hosted-claw WAN-egress live smoke")
    parser.add_argument("--namespace", help="internal hosted-claw namespace to probe")
    parser.add_argument("--pod", help="hosted-claw pod to exec; defaults to first Running app=claw pod")
    parser.add_argument("--namespace-selector", default=None, help="selector for the internal smoke namespace")
    parser.add_argument("--pod-selector", default=None, help="selector for the claw pod inside the namespace")
    parser.add_argument("--public-url", default=None, help="public HTTPS URL that must be reachable")
    parser.add_argument("--blocked-target", action="append", default=None, help="name=ip:port target that must NOT connect")
    parser.add_argument("--allowed-target", action="append", default=None, help="name=ip:port target that MUST connect (PLAT-8489 s1 bridge ports)")
    args = parser.parse_args(argv)

    cfg = WanEgressProbeConfig.from_env()
    cfg = WanEgressProbeConfig(
        namespace=args.namespace or cfg.namespace,
        pod=args.pod or cfg.pod,
        namespace_label_selector=args.namespace_selector or cfg.namespace_label_selector,
        pod_label_selector=args.pod_selector or cfg.pod_label_selector,
        public_url=args.public_url or cfg.public_url,
        public_statuses=cfg.public_statuses,
        blocked_targets=tuple(args.blocked_target) if args.blocked_target else cfg.blocked_targets,
        allowed_targets=tuple(args.allowed_target) if args.allowed_target else cfg.allowed_targets,
        timeout_ms=cfg.timeout_ms,
    )
    ok, payload = run_probe(get_k8s_core_api(), cfg)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
