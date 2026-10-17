"""Listing-mint token-scope parity validator (HIVE-2695, PLAT-9430 child 1).

The PLAT-9426 defect class: a seat's capability listing advertises a
credential verb-tag (``github:merge``, ``github:review``, …) that the seat's
backing Origin credential cannot exercise (read-scoped ORIGIN_TOKEN on a
whitelisted-merger listing). Hive owns both sides — the capability catalog
that advertises verb-tags via ``CapabilityDefinition.default_credential_grant_scopes``
(surfaced as the grant payload's ``credential_grant_scopes``) and the
credential broker that mints the seat's Origin token scopes — so the parity
check is Hive-side, at mint/serve time.

Design (sara, PLAT-9430 comment 826977): verb-by-verb comparison of the
listing's advertised verb-tags against the backing credential's actual
scopes. Mismatch → mint with an explicit ``scope_gap`` flag that the seat's
system-prompt rendering surfaces as "⚠ advertised-but-unexercisable". We
chose the flag path over failing the grant loud: failing the mint would
brick drifted seats' provisioning (repair-not-brick doctrine,
``ensure_agent_forgejo_token`` patches, never rotates) — the retro drift is
enumerated separately by the PLAT-9492 audit command.

Verb-tag → Origin scope contract:

- ``github:merge``  requires ``write:repository`` (merge = write on Origin).
- ``github:review`` requires ``read:repository``  (review state via API).
- ``kubeconfig``    is a non-Origin broker provider — not comparable at this
  layer; recorded in the detail with its class (the bearer-wall nuance: the
  flag list goes to the entitled lane), but it does NOT set ``scope_gap``.
- Any OTHER ``github:*`` tag is UNMAPPED — fail closed: an unmapped tag sets
  ``scope_gap`` so a new verb-tag type cannot silently skip the check. Add
  it to ``ORIGIN_VERB_TAG_REQUIREMENTS`` to clear the flag.

Forgejo scope model: ``write:<resource>`` includes ``read:<resource>`` for
the same resource, so a read requirement is satisfied by either the read or
the write scope.
"""
from __future__ import annotations

from dataclasses import dataclass

# verb-tag (grant payload credential_grant_scopes entry) → frozenset of
# Origin token scopes that must be present for the verb to be exercisable.
ORIGIN_VERB_TAG_REQUIREMENTS = {
    'github:merge': frozenset({'write:repository'}),
    'github:review': frozenset({'read:repository'}),
}

# Forgejo write-implies-read on the same resource: a read requirement is
# satisfied by the write scope too.
_WRITE_IMPLIES_READ = {
    'read:repository': 'write:repository',
    'read:issue': 'write:issue',
    'read:user': 'write:user',
    'read:organization': 'write:organization',
}

# Verb-tags that are not Origin/Forgejo concepts: recorded, never scope_gap.
NON_ORIGIN_TAG_CLASSES = {
    'kubeconfig': 'non_origin_broker_provider',
}

GAP_CLASS_EXERCISED = 'advertised-but-unexercisable'
GAP_CLASS_UNMAPPED = 'unmapped'
GAP_CLASS_NON_ORIGIN = 'non_origin_broker_provider'


@dataclass(frozen=True)
class ScopeGap:
    tag: str
    gap_class: str
    required_origin_scopes: tuple
    missing_scopes: tuple

    def as_dict(self):
        return {
            'tag': self.tag,
            'gap_class': self.gap_class,
            'required_origin_scopes': sorted(self.required_origin_scopes),
            'missing_scopes': sorted(self.missing_scopes),
        }


@dataclass(frozen=True)
class ParityResult:
    scope_gap: bool
    gaps: tuple
    checked_tags: tuple

    def as_dict(self):
        return {
            'scope_gap': self.scope_gap,
            'scope_gap_detail': [g.as_dict() for g in self.gaps],
            'checked_tags': sorted(self.checked_tags),
        }


def scope_satisfied(token_scopes, required_scope) -> bool:
    """Is ``required_scope`` exercisable with ``token_scopes``?

    Exact match, or the Forgejo write-implies-read pairing for the same
    resource (``write:repository`` satisfies ``read:repository``).
    """
    scopes = {str(s).strip() for s in (token_scopes or []) if str(s).strip()}
    required = str(required_scope or '').strip()
    if not required:
        return True
    if required in scopes:
        return True
    writer = _WRITE_IMPLIES_READ.get(required)
    return bool(writer) and writer in scopes


def _resolve_origin_token_scopes(agent, origin_token_scopes):
    if origin_token_scopes is not None:
        return [str(s).strip() for s in (origin_token_scopes or []) if str(s).strip()]
    if agent is None:
        # No agent row and no explicit scopes: fail closed — every mapped
        # Origin verb-tag becomes a gap (a listing with no backing credential
        # is the origin-blind fixture class).
        return []
    from fleet.provisioner import _forgejo_token_scopes

    return list(_forgejo_token_scopes(agent))


def check_scope_parity(agent, grant, origin_token_scopes=None) -> ParityResult:
    """Compare a listing's advertised credential verb-tags against the seat's
    actual Origin token scopes.

    Args:
        agent: FleetAgent row (or None when scopes are passed explicitly).
        grant: the capability listing payload — must carry
            ``credential_grant_scopes`` (the advertised verb-tags).
        origin_token_scopes: explicit backing scopes override (tests, or a
            caller that already resolved them). When omitted, Hive's mint-time
            SoT ``fleet.provisioner._forgejo_token_scopes(agent)`` is used —
            existing tokens are never replaced, so the mint-time derivation IS
            the live token's scope set.

    Returns ParityResult: ``scope_gap`` is True when any advertised verb-tag
    is exercisable-only on paper (advertised-but-unexercisable) or cannot be
    verified (unmapped). ``non_origin_broker_provider`` tags (kubeconfig) are
    recorded in the detail without setting the flag.
    """
    advertised = sorted({
        str(t).strip()
        for t in ((grant or {}).get('credential_grant_scopes') or [])
        if str(t or '').strip()
    })
    token_scopes = _resolve_origin_token_scopes(agent, origin_token_scopes)

    gaps = []
    for tag in advertised:
        non_origin_class = NON_ORIGIN_TAG_CLASSES.get(tag)
        if non_origin_class:
            gaps.append(ScopeGap(tag, non_origin_class, (), ()))
            continue
        required = ORIGIN_VERB_TAG_REQUIREMENTS.get(tag)
        if required is None:
            gaps.append(ScopeGap(tag, GAP_CLASS_UNMAPPED, (), ()))
            continue
        missing = tuple(
            r for r in sorted(required)
            if not scope_satisfied(token_scopes, r)
        )
        if missing:
            gaps.append(ScopeGap(
                tag, GAP_CLASS_EXERCISED, tuple(sorted(required)), missing,
            ))

    exercised = tuple(
        g for g in gaps
        if g.gap_class in (GAP_CLASS_EXERCISED, GAP_CLASS_UNMAPPED)
    )
    return ParityResult(
        scope_gap=bool(exercised),
        gaps=tuple(gaps),
        checked_tags=tuple(advertised),
    )
