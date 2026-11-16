"""Hive runtime-hours metering producer (PLAT-9124, R2 slice 2 of PLAT-7476).

Emits ``io.shizuha.usage.measured.v1`` CloudEvents for agent runtime-hours to
the R1A neutral ingest (``POST /internal/v1/usage/events[:batch]``), following
the accepted R1B producer-adapter pattern (PLAT-7006: scs#72 outbox adapter +
hive#243 accounts:resolve).

Invariants (HLD b131fbff / ADR-0032):
- Emission is transactional with the telemetry checkpoint: the checkpoint
  advance and the outbox row commit together or not at all — never
  fire-and-forget, never double-counted across restarts.
- Delivery is at-least-once with the SAME (source, event_id) and a
  byte-identical envelope, so the core's replay invariant classifies
  redelivery as replay success — never a new fact, never a conflict.
- No direct cross-service tables: the producer consumes the neutral HTTP API
  only (invariant 10), even though the core lives in this repo.
- The payer is server-derived (accounts:resolve pattern) — never
  body-supplied, never client-chosen.
"""
