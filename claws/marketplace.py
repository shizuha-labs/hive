from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import MarketplaceTemplate, MarketplaceTemplateAuditEvent

TRANSITIONS = {
    MarketplaceTemplate.Status.DRAFT: {MarketplaceTemplate.Status.REVIEWED},
    MarketplaceTemplate.Status.REVIEWED: {MarketplaceTemplate.Status.PUBLISHED},
    MarketplaceTemplate.Status.PUBLISHED: {MarketplaceTemplate.Status.DEPRECATED},
}


@transaction.atomic
def transition_marketplace_template(template_id, *, target, actor):
    template = MarketplaceTemplate.objects.select_for_update().get(id=template_id)
    previous = template.status
    if target not in TRANSITIONS.get(previous, set()):
        raise ValidationError({'status': f'cannot transition {previous} to {target}'})
    if target == MarketplaceTemplate.Status.PUBLISHED:
        if any('sensitive_access' in str(flag) for flag in (template.risk_flags or [])):
            raise ValidationError({'risk_flags': 'templates with sensitive_access_flags cannot publish'})
        template.published_at = timezone.now()
    elif target == MarketplaceTemplate.Status.REVIEWED:
        template.reviewed_at = timezone.now()
    else:
        template.deprecated_at = timezone.now()
    template.status = target
    template.full_clean()
    template.save()
    MarketplaceTemplateAuditEvent.objects.create(
        template=template, actor=actor, event_type=target,
        from_status=previous, to_status=target,
    )
    return template
