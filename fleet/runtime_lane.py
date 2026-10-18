"""Pinned, release-qualified runtime-lane admission for Hive fleet agents."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from django.conf import settings


RUNTIME_LANE_FIELDS = frozenset({
    "model", "execution_method", "model_fallbacks", "model_overrides",
})
_MANIFEST_PATH = Path(__file__).with_name("runtime_lane_capabilities.json")


class RuntimeLaneError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _load_manifest() -> dict[str, Any]:
    try:
        manifest = json.loads(_MANIFEST_PATH.read_text())
    except (OSError, ValueError) as exc:
        raise RuntimeLaneError(
            "RUNTIME_CAPABILITY_MANIFEST_INVALID",
            f"Pinned runtime capability manifest cannot be loaded: {exc}",
        ) from exc
    required = {"registry_version", "snapshot_source", "runtime_release_id", "methods", "families"}
    if required.difference(manifest) or not manifest["runtime_release_id"]:
        raise RuntimeLaneError(
            "RUNTIME_CAPABILITY_MANIFEST_INVALID",
            "Pinned runtime capability manifest is incomplete.",
        )
    return manifest


RUNTIME_CAPABILITY_MANIFEST = _load_manifest()
RUNTIME_LANE_REGISTRY_VERSION = RUNTIME_CAPABILITY_MANIFEST["registry_version"]
RUNTIME_RELEASE_ID = RUNTIME_CAPABILITY_MANIFEST["runtime_release_id"]
MANAGED_GROK_FALLBACK_MODEL = "cortex/DeepSeek-V4-Flash"

# HIVE-1937: an explicit provider prefix in a model string routes the runtime to
# that provider (e.g. `xai:grok-4.5` → the `xai` provider). The runtime fails at
# startup with "Provider xai is not configured/available" when the execution
# method cannot serve that provider (observed: `xai:grok-4.5` on Aya's shizuha
# gateway crash-looped the pod). This is the effective runtime provider registry
# per execution method — the providers the runtime image actually configures for
# that lane. A model with an explicit provider prefix outside the method's set is
# rejected before it can reach a pod env that exits at startup.
_METHOD_SERVABLE_PROVIDERS: dict[str, frozenset[str]] = {
    # shizuha gateway: openai/google/cortex/ollama are the configured providers.
    'shizuha': frozenset({'openai', 'google', 'cortex', 'ollama', 'vllm', 'llamacpp'}),
    # grok_build bridge: xAI TokenPool + cortex-routed offers.
    'grok_build': frozenset({'xai', 'cortex', 'ollama', 'vllm', 'llamacpp'}),
    'codex_app_server': frozenset({'openai', 'cortex', 'ollama', 'vllm', 'llamacpp'}),
    'openclaw_app_server': frozenset({'openai', 'cortex', 'ollama', 'vllm', 'llamacpp'}),
    'claude_code_server': frozenset({'anthropic', 'cortex', 'ollama', 'vllm', 'llamacpp'}),
    'antigravity_server': frozenset({'anthropic', 'google', 'cortex', 'ollama', 'vllm', 'llamacpp'}),
}
_KNOWN_PROVIDER_PREFIXES = frozenset({
    'xai', 'anthropic', 'openai', 'google', 'deepseek',
    'cortex', 'ollama', 'vllm', 'llamacpp', 'mistral', 'groq',
})


def _explicit_provider_prefix(model: str) -> str | None:
    """Return the explicit provider prefix of a model string, or None.

    `xai:grok-4.5` → `xai`; `cortex/grok-4.5` → `cortex`; a bare model name
    (`grok-4.5`, `gpt-5.6-sol`) has no explicit provider and resolves
    implicitly, so it is never gated here.
    """
    m = str(model or "").strip()
    for sep in (":", "/"):
        idx = m.find(sep)
        if idx > 0:
            prefix = m[:idx].strip().lower()
            if prefix in _KNOWN_PROVIDER_PREFIXES:
                return prefix
    return None


def validate_model_provider_servable(model: str, method: str) -> None:
    """HIVE-1937: reject a model whose explicit provider the method cannot serve.

    Raises RuntimeLaneError (PROVIDER_NOT_SERVABLE) when the model carries an
    explicit provider prefix outside the execution method's effective provider
    registry. Bare models (no explicit prefix) pass. This is the durable gate
    that keeps `xai:grok-4.5` off a shizuha-gateway pod env that would exit at
    startup with "Provider xai is not configured/available".
    """
    provider = _explicit_provider_prefix(model)
    if provider is None:
        return
    servable = _METHOD_SERVABLE_PROVIDERS.get(method)
    if servable is None or provider in servable:
        return
    raise RuntimeLaneError(
        "PROVIDER_NOT_SERVABLE",
        f"Model {model!r} routes to provider {provider!r}, which execution "
        f"method {method!r} cannot serve. Use the cortex-routed form "
        f"(e.g. cortex/grok-4.5) instead.",
    )

# Retired serving ids Hive must not persist. Target is the current fleet pin
# (GLM-5.3-Flash): Qwen3.8-27B-Q2 left with MiniMax-on-i9, Vision-Metal was
# deprecated 2026-09 (Nova kept POSTing the parked ds4 slot after Hive SoT
# already said GLM because remap never included Metal).
CANONICAL_FLEET_CORTEX_MODEL = "cortex/GLM-5.3-Flash"
RETIRED_FLEET_MODELS = {
    "cortex/qwen3.8-27b-q2": CANONICAL_FLEET_CORTEX_MODEL,
    "qwen3.8-27b-q2": CANONICAL_FLEET_CORTEX_MODEL,
    "vllm/qwen3.8-27b-q2": CANONICAL_FLEET_CORTEX_MODEL,
    "cortex/deepseek-v4-flash-vision-metal": CANONICAL_FLEET_CORTEX_MODEL,
    "deepseek-v4-flash-vision-metal": CANONICAL_FLEET_CORTEX_MODEL,
    "vllm/deepseek-v4-flash-vision-metal": CANONICAL_FLEET_CORTEX_MODEL,
}
_RETIRED_MODEL_SUFFIXES = (
    "qwen3.8-27b-q2",
    "deepseek-v4-flash-vision-metal",
)
# Cortex serving ids (no cortex/ prefix) still holding leftover KV books after
# Hive remapped every pin off them. Lease reconcile drains these even when the
# pool has zero members — an empty-pool tick used to return before leftover yield.
RETIRED_CORTEX_SERVING_IDS = (
    "Qwen3.8-27B-Q2",
    "DeepSeek-V4-Flash-Vision-Metal",
)
# grok-4.6 is a deprecated alias of grok-4.7. Rewrite the version token only.
# Never send it through the GLM suffix table, and never touch voice/imagine ids.
_DEPRECATED_GROK_46 = re.compile(r"grok-4\.6(?:-latest)?", re.IGNORECASE)


def _canonicalize_deprecated_grok(raw: str) -> str:
    lowered = raw.lower()
    if "grok-voice" in lowered or "grok-imagine" in lowered:
        return raw
    if _DEPRECATED_GROK_46.search(raw) is None:
        return raw
    return _DEPRECATED_GROK_46.sub("grok-4.7", raw)


def remap_retired_fleet_model(model: str) -> str:
    raw = str(model or "").strip()
    if not raw:
        return raw
    canonical_grok = _canonicalize_deprecated_grok(raw)
    if canonical_grok != raw:
        return canonical_grok
    lowered = raw.lower()
    mapped = RETIRED_FLEET_MODELS.get(lowered)
    if mapped:
        return mapped
    for suffix in _RETIRED_MODEL_SUFFIXES:
        if (
            lowered == suffix
            or lowered.endswith("/" + suffix)
            or lowered.endswith(":" + suffix)
        ):
            return CANONICAL_FLEET_CORTEX_MODEL
    return raw


def is_retired_fleet_model(model: str) -> bool:
    raw = str(model or "").strip()
    return bool(raw) and remap_retired_fleet_model(raw) != raw


@dataclass(frozen=True)
class RuntimeFamily:
    lane_id: str
    provider: str
    exact_models: frozenset[str]
    prefixes: tuple[str, ...]
    allowed_methods: tuple[str, ...]
    canonical_method: str

    def matches(self, model: str) -> bool:
        lowered = model.lower()
        return lowered in self.exact_models or any(lowered.startswith(prefix) for prefix in self.prefixes)


def _families() -> tuple[RuntimeFamily, ...]:
    return tuple(RuntimeFamily(
        lane_id=str(item["lane_id"]),
        provider=str(item["provider"]),
        exact_models=frozenset(str(model).lower() for model in item.get("models") or []),
        prefixes=tuple(str(prefix).lower() for prefix in item.get("model_prefixes") or []),
        allowed_methods=tuple(str(method) for method in item.get("allowed_methods") or []),
        canonical_method=str(item.get("canonical_method") or ""),
    ) for item in RUNTIME_CAPABILITY_MANIFEST["families"])


def _family_for(model: str) -> RuntimeFamily:
    normalized = str(model or "").strip()
    if not normalized:
        raise RuntimeLaneError("MODEL_REQUIRED", "A runtime lane requires a model.")
    # Exact entries win over the general cortex/* namespace.
    exact = [family for family in _families() if normalized.lower() in family.exact_models]
    matches = exact or [family for family in _families() if family.matches(normalized)]
    if len(matches) != 1:
        code = "UNREGISTERED_MODEL" if not matches else "AMBIGUOUS_RUNTIME_LANE"
        raise RuntimeLaneError(
            code,
            f"Model {normalized!r} does not resolve unambiguously in runtime-lane registry "
            f"{RUNTIME_LANE_REGISTRY_VERSION}.",
        )
    return matches[0]


def runtime_lane_from_agent(agent: Any) -> dict[str, Any]:
    if isinstance(agent, dict):
        method = agent.get("execution_method") or "shizuha"
        return {
            "model": str(agent.get("model") or agent.get("effective_model") or "").strip(),
            "execution_method": str(method).strip(),
            "model_fallbacks": list(agent.get("model_fallbacks") or []),
            "model_overrides": dict(agent.get("model_overrides") or {}),
        }
    mcp_config = getattr(agent, "mcp_config", None) or {}
    return {
        "model": str(getattr(agent, "model", "") or "").strip(),
        "execution_method": str(mcp_config.get("execution_method") or "shizuha").strip(),
        "model_fallbacks": list(getattr(agent, "model_fallbacks", None) or []),
        "model_overrides": dict(getattr(agent, "model_overrides", None) or {}),
    }


def _method_capabilities(method: str) -> list[str]:
    method_spec = RUNTIME_CAPABILITY_MANIFEST["methods"].get(method)
    if not isinstance(method_spec, dict):
        raise RuntimeLaneError(
            "UNREGISTERED_EXECUTION_METHOD",
            f"Execution method {method!r} is absent from pinned capability snapshot.",
        )
    return [str(capability) for capability in method_spec.get("credential_capabilities") or []]


def managed_model_fallbacks(model: str, method: str) -> list[dict[str, str]]:
    """No agent has a fallback chain. An agent is pinned to one backend.

    Operator directive 2026-08-04: *"perhaps there should be zero concept of a
    'fallback' .. the agent needs to be pinned to a backend and that's it .. to
    make the life simpler"*.

    This replaces the 2026-08-01 exception that gave Grok Build a
    `grok primary -> cortex/DeepSeek-V4-Flash` tail. That chain cost more than
    it bought:

    * The Agents page showed `cortex/grok-4.5 pinned` while the agent actually
      ran DeepSeek, so the operator could not tell what a agent was using.
    * Drift detection compared the running model against the primary and
      reported every sanctioned failover as `runtime_config_drift` / degraded.
    * Cortex attributed two models to one key, making per-agent error taxonomy
      and quota accounting ambiguous (nagi: 2523 requests over two models in
      24h).
    * A failing primary was hidden instead of surfaced — nagi silently absorbed
      191 failed grok attempts in a day rather than making the quota problem
      visible.

    Returning an empty chain is an already-supported runtime state: the pod's
    inline failover loop collapses to a single step, and `MODEL_FALLBACKS=[]`
    means no failover by design.

    Kept as a function (rather than deleted) because `FleetAgent.save` calls it
    at the model chokepoint to normalise every write — admin, migrations,
    reconcilers, daemon sync and REST all funnel through it, so this is the one
    place that guarantees no hidden chain can be persisted.
    """
    return []


def resolve_runtime_lane(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Resolve a partial request into one complete, pinned RuntimeLane."""
    patch = dict(patch or {})
    if patch.get("model_fallbacks"):
        raise RuntimeLaneError(
            "MODEL_FALLBACKS_DISABLED",
            "Per-agent model fallbacks are disabled; submit one primary model lane.",
        )
    if patch.get("model_overrides"):
        raise RuntimeLaneError(
            "MODEL_OVERRIDES_DISABLED",
            "Hidden per-method model overrides are disabled; submit one primary model lane.",
        )

    model = remap_retired_fleet_model(
        str(patch.get("model", current.get("model", "")) or "").strip()
    )
    method_was_requested = "execution_method" in patch
    method = str(patch.get("execution_method", current.get("execution_method", "")) or "").strip()
    family = _family_for(model)
    # HIVE-1937: an explicit provider prefix must be servable by the method —
    # `xai:grok-4.5` on a method without xai capability reaches a pod env that
    # exits at startup. Check before the method-compatibility derivation so the
    # error names the provider, not a method mismatch.
    validate_model_provider_servable(model, method)
    derivations: list[dict[str, str]] = []
    if method not in family.allowed_methods:
        if "model" in patch and not method_was_requested and family.canonical_method:
            derivations.append({
                "field": "execution_method", "from": method, "to": family.canonical_method,
                "reason": "canonical method from pinned capability snapshot",
            })
            method = family.canonical_method
        else:
            raise RuntimeLaneError(
                "INCOMPATIBLE_RUNTIME_LANE",
                f"Execution method {method!r} cannot run model {model!r} in registry "
                f"{RUNTIME_LANE_REGISTRY_VERSION}.",
            )

    capabilities = _method_capabilities(method)
    model_fallbacks = managed_model_fallbacks(model, method)
    return {
        "lane_id": family.lane_id,
        "registry_version": RUNTIME_LANE_REGISTRY_VERSION,
        "runtime_release_id": RUNTIME_RELEASE_ID,
        "execution_method": method,
        "primary": {"provider": family.provider, "model_id": model},
        "model": model,
        "model_fallbacks": model_fallbacks,
        "model_overrides": {},
        "provider": family.provider,
        "credential_requirements": capabilities,
        "required_capabilities": capabilities,
        "derivations": derivations,
        "snapshot_source": RUNTIME_CAPABILITY_MANIFEST["snapshot_source"],
    }


def lane_config(lane: dict[str, Any]) -> dict[str, Any]:
    """Fields consumed by the existing daemon config renderer."""
    return {
        "model": lane["model"],
        "execution_method": lane["execution_method"],
        "model_fallbacks": list(lane.get("model_fallbacks") or []),
        "model_overrides": dict(lane.get("model_overrides") or {}),
    }


def lane_payload(lane: dict[str, Any]) -> dict[str, Any]:
    """Complete persisted/evented RuntimeLane; never a partial daemon patch."""
    return {
        "lane_id": lane["lane_id"],
        "registry_version": lane["registry_version"],
        "runtime_release_id": lane["runtime_release_id"],
        "execution_method": lane["execution_method"],
        "primary": dict(lane["primary"]),
        "model": lane["model"],
        "model_fallbacks": list(lane.get("model_fallbacks") or []),
        "model_overrides": dict(lane.get("model_overrides") or {}),
        "provider": lane["provider"],
        "credential_requirements": list(lane.get("credential_requirements") or []),
    }


def lane_digest(lane: dict[str, Any]) -> str:
    encoded = json.dumps(lane_payload(lane), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _credential_scopes(agent: Any) -> list[tuple[str, str]]:
    from hive_platform.models import TokenPool

    owner_id = agent.get("owner_id") if isinstance(agent, dict) else getattr(agent, "owner_id", None)
    if owner_id is not None:
        return [(TokenPool.OwnerKind.USER, str(owner_id))]
    scopes = [(TokenPool.OwnerKind.ORG, TokenPool.ORG_OWNER_ID)]
    fleet_owner = str(getattr(settings, "HIVE_FLEET_OWNER_USER_ID", "") or "").strip()
    if fleet_owner:
        scopes.insert(0, (TokenPool.OwnerKind.USER, fleet_owner))
    return scopes


def validate_chat_model_output(model: str) -> None:
    """Reject only known non-text Cortex output at desired-config admission."""
    from .model_serviceability import explicitly_nontext_cortex_model

    if explicitly_nontext_cortex_model(model):
        raise RuntimeLaneError(
            "MODEL_NOT_CHAT_CAPABLE",
            "The selected Cortex model explicitly produces non-text output and cannot run an agent chat lane.",
        )


def preflight_runtime_lane(agent: Any, lane: dict[str, Any]) -> dict[str, Any]:
    """Call the broker's scoped, capability-only probe; no secret leaves it."""
    validate_chat_model_output(lane.get('model', ''))
    required = list(lane.get("credential_requirements") or [])
    if not required:
        return {"state": "not_required", "required": []}

    from hive_platform.token_coordinator import probe_token_capabilities

    result = probe_token_capabilities(_credential_scopes(agent), required)
    if result["missing"]:
        raise RuntimeLaneError(
            "CREDENTIAL_CAPABILITY_UNAVAILABLE",
            "Required broker capability unavailable for this agent lane: "
            + ", ".join(result["missing"]),
        )
    return {
        "state": "passed",
        "required": required,
        "available": result["available"],
        "source": "hive_token_coordinator_capability_probe",
    }


def runtime_registry_method_models(dynamic_cortex: list[dict[str, Any]] | None = None) -> dict[str, list[dict[str, str]]]:
    """UI catalog derived from the same pinned manifest used by admission."""
    dynamic_cortex = dynamic_cortex or []
    result: dict[str, list[dict[str, str]]] = {}
    for method, spec in RUNTIME_CAPABILITY_MANIFEST["methods"].items():
        values = list(spec.get("models") or [])
        if spec.get("model_prefixes"):
            values.extend(item["id"] for item in dynamic_cortex if item.get("id"))
        result[method] = [{"id": value, "label": value} for value in dict.fromkeys(values)]
    return result
