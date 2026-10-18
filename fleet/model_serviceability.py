"""Which Cortex models can actually serve right now (operator 2026-08-06).

    grok usage is gone which is fine .. and the right approach here is that
    the agents should stay as is .. but hive can hibernate them to save
    resources if needed .. whenever grok capacity is back the agents should
    resume back to working

Cortex's ``/v1/models`` is the truth for this: it hides a model while every
credential on its provider is inactive or cooling (a confirmed 0%-credit
quota bench is a cooldown), and re-lists it the moment a bench lapses —
verified live 2026-08-06: grok-4.5 vanished while all nine SuperGrok accounts
were credit-dry and reappeared at the first quota reset, after which a routed
completion succeeded.

FAIL OPEN. An unreachable/empty catalog must mean "assume everything is
serviceable": hibernating the whole fleet because Cortex blipped for one
sweep would be a far worse failure than one extra retry per agent.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 60.0
_cache: dict[str, object] = {'at': 0.0, 'ids': None}


def _bare(model: str) -> str:
    """Canonical bare model id: strip the ``cortex/`` routing prefix, casefold."""
    return str(model or '').strip().removeprefix('cortex/').lower()


def is_cortex_model(model: str) -> bool:
    return str(model or '').strip().lower().startswith('cortex/')


def is_realtime_omni_model(model: str) -> bool:
    """Grok Voice / omni IDs mint via Cortex audio realtime, not /v1/models.

    The chat catalog is the serviceability signal for credit-dry grok-4.x
    seats. Applying it to grok-voice-* hibernates Voice experiment seats
    the moment they are pinned (Hina 2026-08-17) because those IDs are
    not advertised on /v1/models.
    """
    return _bare(model).startswith('grok-voice')


def explicitly_nontext_model(item: dict) -> bool:
    """Only explicit typed output metadata can disqualify an agent chat model.

    Missing/legacy/malformed capabilities are unknown, not evidence that chat
    is unsupported. Availability and tool support are separate contracts.
    """
    metadata = item.get('x_cortex')
    outputs = metadata.get('output_modalities') if isinstance(metadata, dict) else None
    return (
        isinstance(outputs, list) and bool(outputs)
        and all(isinstance(value, str) and value.strip() for value in outputs)
        and 'text' not in {value.strip().casefold() for value in outputs}
    )


def fetch_cortex_model_catalog() -> list[dict] | None:
    """Read the existing viewer-scoped catalog; preserve unknown on failure."""
    import requests
    from django.conf import settings

    base = (getattr(settings, 'CORTEX_BASE_URL', '') or '').rstrip('/')
    if base.endswith('/v1'):
        base = base[:-3].rstrip('/')
    key = (
        getattr(settings, 'HIVE_CORTEX_LEASE_API_KEY', '')
        or getattr(settings, 'CORTEX_API_KEY', '')
    )
    if not base:
        return None
    try:
        response = requests.get(
            f'{base}/v1/models',
            headers={'Authorization': f'Bearer {key}'} if key else {},
            timeout=10,
        )
        if response.status_code != 200:
            logger.warning('model-serviceability: /v1/models -> %s', response.status_code)
            return None
        data = response.json().get('data') or []
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else None
    except Exception as exc:  # noqa: BLE001
        logger.warning('model-serviceability: /v1/models fetch failed: %s', exc)
        return None


def _fetch_served_model_ids() -> set[str] | None:
    """Model ids Cortex currently advertises to the fleet, or None on failure."""
    data = fetch_cortex_model_catalog()
    ids = {_bare(item.get('id')) for item in data or [] if item.get('id')}
    # An empty catalog is indistinguishable from a broken one — fail open.
    return ids or None


def explicitly_nontext_cortex_model(model: str) -> bool:
    if not is_cortex_model(model):
        return False
    wanted = _bare(str(model).casefold())
    for item in fetch_cortex_model_catalog() or []:
        aliases = item.get('aliases')
        names = [item.get('id')] + (aliases if isinstance(aliases, list) else [])
        if any(isinstance(name, str) and _bare(name.casefold()) == wanted for name in names):
            if explicitly_nontext_model(item):
                return True
    return False


def served_cortex_model_ids(*, now: float | None = None) -> set[str] | None:
    now = now if now is not None else time.monotonic()
    if _cache['ids'] is not None and now - float(_cache['at']) < _CACHE_TTL_SECONDS:
        return _cache['ids']  # type: ignore[return-value]
    ids = _fetch_served_model_ids()
    if ids is not None:
        _cache['at'] = now
        _cache['ids'] = ids
    return ids


def unserviceable_cortex_models(agent_models) -> frozenset[str]:
    """Subset of the fleet's cortex models that Cortex does not serve right now.

    Returns BARE lowercase ids. Non-cortex models (claude/gpt/gemini) are never
    classified here — their capacity lives with their own providers.
    """
    wanted = {
        _bare(m) for m in agent_models or []
        if is_cortex_model(m) and not is_realtime_omni_model(m)
    }
    if not wanted:
        return frozenset()
    served = served_cortex_model_ids()
    if served is None:
        return frozenset()  # fail open
    return frozenset(wanted - served)
