"""Type contract for the durable FleetAgent configuration outbox."""

import logging

from django.core.exceptions import ValidationError


logger = logging.getLogger(__name__)


def validate_pending_config_mapping(value) -> None:
    """Reject JSON scalars/arrays at model and serializer validation boundaries."""
    if not isinstance(value, dict):
        raise ValidationError("pending_config must be a JSON object")


def pending_config_or_empty(value, *, agent_identifier, boundary: str) -> dict:
    """Return a defensive copy, failing loud without taking down a shared reader.

    ``JSONField`` accepts every JSON type, while the durable config outbox is a
    key/value mapping.  Legacy rows may therefore contain a scalar.  Readers
    must stay available during migration/version skew, but the bad value must
    remain visible in logs instead of being silently coerced.
    """
    if isinstance(value, dict):
        return dict(value)
    logger.error(
        "FleetAgent pending_config invariant violation: agent=%s boundary=%s "
        "value_type=%s; treating as an empty object",
        agent_identifier,
        boundary,
        type(value).__name__,
    )
    return {}
