"""Management command: audit_capability_scope_parity

One-time, read-only listings-vs-credential-scopes parity sweep
(PLAT-9430 child 3 / PLAT-9492; prevention mechanism for the PLAT-9426
retro: capability listings ≠ token scopes).

Enumerates every seat's effective capability listing (Pulse TeamRoster
memberships ∪ live FleetAgent rows → ``compute_effective_capability_grant``),
resolves the backing Origin credential's actual scopes via the mint-time
source of truth (``fleet.provisioner._forgejo_token_scopes``), and runs the
HIVE-2695 parity check (``fleet.scope_parity.check_scope_parity``) per seat —
the exact function + input shape the listing-mint path uses, so the audit can
never drift from mint semantics.

Output is the divergence flag list, one row per (seat, listing, verb-tag):

    (seat, listing(s), advertised verbs, actual scopes, gap class, missing scopes)

Gap classes (``fleet.scope_parity``):
    advertised-but-unexercisable  — mapped Origin verb-tag, backing token lacks
                                    the required scope (the shizuha fixture:
                                    merge-scope listing on a read-scoped token).
    unmapped                      — ``github:*`` tag with no scope contract
                                    (fails closed; add to
                                    ORIGIN_VERB_TAG_REQUIREMENTS to clear).
    non_origin_broker_provider    — bearer-wall tags (kubeconfig): scopes are
                                    not seat-readable — recorded for the
                                    entitled lane (read-and-flag, per shion's
                                    acceptance nuance), never a scope_gap.
    origin-blind                  — seat has advertised credential verbs but
                                    no resolvable backing credential at all
                                    (the kai fixture).

The three PLAT-9426 retro fixtures (shizuha merge-scope, kai origin-blind,
haru ci-logs bearer) are covered by the generic enumeration — they must appear
in the flag list (or their post-remediation absence), which is the acceptance
criterion. Re-runnable at any time; after HIVE-2695 stamped mints are live,
a clean re-run on fresh mints proves zero new divergence.

Read-only: no DB writes, no credential mints, no roster mutations.

Usage:
    python manage.py audit_capability_scope_parity
    python manage.py audit_capability_scope_parity --json
    python manage.py audit_capability_scope_parity --seat jun --json
    python manage.py audit_capability_scope_parity --fail-on-gap   # CI form

Run leg: admin-ops (per the PLAT-9426 ownership) — engineering ships, the
entitled lane executes and files the divergence flags.
"""
from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from fleet.effective_capabilities import compute_effective_capability_grant
from fleet.models import CapabilityDefinition, FleetAgent
from fleet.scope_parity import (
    GAP_CLASS_NON_ORIGIN,
    check_scope_parity,
)

# Re-exported class name used for the no-backing-credential fixture class.
GAP_CLASS_ORIGIN_BLIND = 'origin-blind'

BACKING_FLEET_AGENT = 'fleet_agent'
BACKING_NONE = 'none'
# Classes that count as an exercised divergence (vs informational bearer walls).
EXERCISED_CLASSES = frozenset({
    GAP_CLASS_ORIGIN_BLIND, 'advertised-but-unexercisable', 'unmapped',
})


class Command(BaseCommand):
    help = (
        'Read-only listings-vs-credential-scopes parity sweep (PLAT-9492): '
        'enumerate effective capability listings, resolve backing Origin '
        'scopes, emit the divergence flag list.'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--json', action='store_true',
            help='Emit machine-readable JSON (the flag list for the entitled lane).',
        )
        parser.add_argument(
            '--seat', action='append', dest='seats', default=[],
            help='Limit the sweep to these seat usernames/emails (repeatable).',
        )
        parser.add_argument(
            '--fail-on-gap', action='store_true',
            help='Exit non-zero when any exercised divergence is found (CI form).',
        )

    # ── roster + seats ───────────────────────────────────────────────────

    def _roster_teams_by_email(self) -> dict[str, set[str]]:
        """Pulse TeamRoster email → team slugs (the membership source of truth).

        Fail loud on outage: a silently empty roster would under-derive every
        seat's scopes and flood the flag list with false divergences.
        """
        from fleet.on_demand import memberships_by_email
        from fleet.pulse_auth import pulse_fleet_rosters

        try:
            rosters = pulse_fleet_rosters()
        except Exception as exc:  # noqa: BLE001 — the audit must not pass on a blind roster
            raise CommandError(
                f'Pulse TeamRoster read failed ({exc}) — refusing to emit a '
                'flag list derived from an empty roster (fail loud).'
            ) from exc
        return memberships_by_email(rosters) if rosters else {}

    def _seat_rows(self, roster_emails):
        """Union of audit candidates: live FleetAgent rows + roster seats."""
        live = list(
            FleetAgent.objects.exclude(status=FleetAgent.Status.DELETED)
            .order_by('agent_username')
        )
        rows_by_username = {
            (a.agent_username or '').strip().lower(): a for a in live
        }
        seats = dict(rows_by_username)
        for email in sorted(roster_emails):
            username = email.split('@', 1)[0]
            if email.endswith('@shizuha.com') and username and username not in seats:
                seats[username] = None  # roster seat with no native FleetAgent row
        return seats

    # ── listing + parity ─────────────────────────────────────────────────

    @staticmethod
    def _tag_to_listing_map() -> dict[str, list[str]]:
        """verb-tag → capability slugs advertising it (ACTIVE catalog rows)."""
        tag_map: dict[str, list[str]] = {}
        rows = CapabilityDefinition.objects.filter(
            activation_status=CapabilityDefinition.ActivationStatus.ACTIVE,
        ).values_list('slug', 'default_credential_grant_scopes')
        for slug, tags in rows:
            for tag in tags or []:
                tag_map.setdefault(str(tag).strip(), []).append(slug)
        return {tag: sorted(slugs) for tag, slugs in tag_map.items()}

    @staticmethod
    def _actual_scopes(agent):
        """Mint-time SoT for the live token's scope set (provisioner)."""
        if agent is None:
            return []
        from fleet.provisioner import _forgejo_token_scopes

        return list(_forgejo_token_scopes(agent))

    def handle(self, *args, **options):
        as_json = bool(options['json'])
        seat_filter = {
            str(s).strip().lower().split('@', 1)[0]
            for s in (options['seats'] or []) if str(s or '').strip()
        }
        fail_on_gap = bool(options['fail_on_gap'])

        roster_teams = self._roster_teams_by_email()
        seats = self._seat_rows(roster_teams.keys())
        if seat_filter:
            unknown = seat_filter - set(seats)
            if unknown:
                raise CommandError(f'unknown seat(s): {sorted(unknown)}')
            seats = {k: v for k, v in seats.items() if k in seat_filter}

        from fleet.on_demand import team_slug_from_key

        tag_map = self._tag_to_listing_map()
        flags = []
        seats_audited = 0
        seats_with_verbs = 0

        for username, agent in sorted(seats.items()):
            if seat_filter and username not in seat_filter:
                continue
            seats_audited += 1
            email = f'{username}@shizuha.com'
            team_slugs = sorted(
                team_slug_from_key(t) for t in roster_teams.get(email, set())
            )
            identifiers = {username, email}
            if agent is not None:
                identifiers.update({
                    str(agent.agent_id or ''),
                    str(agent.agent_username or ''),
                })
            grant = compute_effective_capability_grant(
                team_slugs, identifiers,
            )
            advertised = [
                str(t).strip() for t in (grant.get('credential_grant_scopes') or [])
                if str(t or '').strip()
            ]
            if not advertised:
                continue  # no credential verbs advertised → no listing to audit
            seats_with_verbs += 1

            actual_scopes = self._actual_scopes(agent)
            if agent is None:
                # Origin-blind: advertised credential verbs with no resolvable
                # backing credential. Pass explicit empty scopes so every
                # mapped tag reports its missing scope (fail closed).
                actual_scopes = []
            parity = check_scope_parity(
                agent=agent, grant=grant,
                origin_token_scopes=actual_scopes,
            )

            for gap in parity.gaps:
                is_blind = agent is None and gap.gap_class != GAP_CLASS_NON_ORIGIN
                gap_class = (
                    GAP_CLASS_ORIGIN_BLIND if is_blind else gap.gap_class
                )
                flags.append({
                    'seat': username,
                    'listing': tag_map.get(gap.tag, []),
                    'advertised_verb': gap.tag,
                    'actual_scopes': sorted(actual_scopes),
                    'gap_class': gap_class,
                    'required_origin_scopes': sorted(gap.required_origin_scopes),
                    'missing_scopes': sorted(gap.missing_scopes),
                    'backing_credential': (
                        BACKING_NONE if agent is None else BACKING_FLEET_AGENT
                    ),
                })

        exercised_flags = [
            f for f in flags if f['gap_class'] in EXERCISED_CLASSES
        ]

        if as_json:
            self.stdout.write(json.dumps({
                'audit': 'capability_scope_parity',
                'refs': ['PLAT-9492', 'PLAT-9430 child 3', 'PLAT-9426'],
                'read_only': True,
                'roster_source': 'pulse:/admin/team-roster/',
                'parity_check': 'fleet.scope_parity.check_scope_parity (HIVE-2695 mint path)',
                'seats_audited': seats_audited,
                'seats_with_credential_verbs': seats_with_verbs,
                'divergence_count': len(exercised_flags),
                'flags': flags,
            }, indent=2, sort_keys=True))
        else:
            self.stdout.write(self.style.MIGRATE_HEADING(
                f'capability scope parity audit — {seats_audited} seats, '
                f'{seats_with_verbs} with credential verbs, '
                f'{len(exercised_flags)} divergence flag(s)'
            ))
            if not flags:
                self.stdout.write(self.style.SUCCESS(
                    'no credential-verb listings diverged from backing scopes'
                ))
            for flag in flags:
                marker = (
                    self.style.ERROR('GAP ')
                    if flag['gap_class'] in EXERCISED_CLASSES
                    else self.style.WARNING('info')
                )
                listings = ','.join(flag['listing']) or '<unattributed>'
                self.stdout.write(
                    f"{marker} seat={flag['seat']} listing={listings} "
                    f"verb={flag['advertised_verb']} class={flag['gap_class']} "
                    f"actual_scopes={flag['actual_scopes']} "
                    f"missing={flag['missing_scopes']} "
                    f"backing={flag['backing_credential']}"
                )
            self.stdout.write(
                'note: non_origin_broker_provider rows are bearer walls — '
                'scopes are not seat-readable; route to the entitled lane.'
            )

        if fail_on_gap and exercised_flags:
            raise CommandError(
                f'{len(exercised_flags)} scope divergence flag(s) — failing '
                'per --fail-on-gap'
            )
