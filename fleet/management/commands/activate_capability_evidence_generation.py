"""Origin-CI activation of one immutable capability-evidence producer tuple."""
import json

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from fleet.capability_evidence import (
    MATERIALIZER_VERSION,
    config_digest,
    load_signing_identity,
    materializer_digest,
    schema_digest,
)
from fleet.models import CapabilityEvidenceProducerGeneration


class Command(BaseCommand):
    help = 'Create/activate the CI-bound HiveCapabilityEvidenceV2 producer generation.'

    def add_arguments(self, parser):
        parser.add_argument('--generation-id', required=True)
        parser.add_argument('--source-sha', required=True)
        parser.add_argument('--source-tree', required=True)
        parser.add_argument('--image-index-digest', required=True)

    def handle(self, *args, **options):
        signing = load_signing_identity()
        service_account = 'hive-api'
        from django.conf import settings
        service_account = str(settings.HIVE_CAPABILITY_EVIDENCE_SERVICE_ACCOUNT)
        bound = {
            'source_sha': options['source_sha'],
            'source_tree': options['source_tree'],
            'image_index_digest': options['image_index_digest'],
            'schema_digest': schema_digest(),
            'materializer_version': MATERIALIZER_VERSION,
            'materializer_digest': materializer_digest(),
            'config_digest': config_digest(),
            'service_account': service_account,
            'signing_kid': signing.kid,
            'signing_key_thumbprint': signing.thumbprint,
        }
        generation_id = options['generation_id']

        with transaction.atomic():
            existing = (
                CapabilityEvidenceProducerGeneration.objects.select_for_update()
                .filter(pk=generation_id).first()
            )
            if existing:
                mismatches = [
                    key for key, value in bound.items()
                    if getattr(existing, key) != value
                ]
                if mismatches:
                    raise CommandError(
                        'generation id already exists with different immutable fields: '
                        + ', '.join(mismatches)
                    )
                if not existing.active:
                    raise CommandError('generation id exists but is retired and cannot be reactivated')
                generation = existing
            else:
                current = (
                    CapabilityEvidenceProducerGeneration.objects.select_for_update()
                    .filter(active=True).first()
                )
                if current:
                    current.active = False
                    current.retired_at = timezone.now()
                    current.save(update_fields=['active', 'retired_at'])
                generation = CapabilityEvidenceProducerGeneration.objects.create(
                    generation_id=generation_id,
                    activated_at=timezone.now(),
                    active=True,
                    **bound,
                )

        payload = {
            'producer_generation_id': generation.generation_id,
            'producer_generation_digest': generation.generation_digest,
            'kid': generation.signing_kid,
            'public_jwks': {'keys': [signing.public_jwk]},
        }
        self.stdout.write(json.dumps(payload, sort_keys=True, separators=(',', ':')))
