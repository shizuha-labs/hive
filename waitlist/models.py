"""
HIVE-100 — Free Claw no-spend demand-capture store.

Two models, both an **auditable internal store** (no external analytics vendor,
no third-party form service — all rows live in the hive Postgres DB):

- ``WaitlistEntry``  — one row per interested person, deduped by email. The
  durable intent signal: who, which channel they prefer, their top use case.
- ``WaitlistEvent``  — the analytics funnel (visit / start / submit /
  channel_select). Append-only; never updated. This is what answers "how many
  visits converted to submits, and which channel do people pick".

Data minimization (HIVE-100 guardrail "no collection beyond necessary"): we
store email + preferred channel + use-case text only. No IP, no User-Agent. The
anonymous ``session_key`` is Django's random session id, used solely to stitch
visit→start→submit for one browser into a funnel — it is not PII.
"""
import uuid

from django.db import models


class Channel(models.TextChoices):
    WEB = 'web', 'Web'
    APP = 'app', 'App'
    WHATSAPP = 'whatsapp', 'WhatsApp'


class WaitlistEntry(models.Model):
    """A captured Free Claw waitlist / intent signal (deduped by email)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True, db_index=True)
    # Optional Shizuha-ID linkage when the visitor is a logged-in owner. Blank for
    # the common anonymous pre-launch visit (email is the identity in that case).
    owner_id = models.IntegerField(null=True, blank=True, db_index=True)
    owner_username = models.CharField(max_length=150, blank=True)
    preferred_channel = models.CharField(
        max_length=20, choices=Channel.choices, default=Channel.WEB,
    )
    top_use_case = models.CharField(max_length=500, blank=True)
    # Where the signal came from (landing slug / campaign), for later attribution.
    source = models.CharField(max_length=100, blank=True, default='hive-free-claw-landing')
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'waitlist entry'
        verbose_name_plural = 'waitlist entries'

    def __str__(self):
        return f'{self.email} ({self.preferred_channel})'


class WaitlistEvent(models.Model):
    """Append-only analytics funnel event for the Free Claw landing surface."""

    class Type(models.TextChoices):
        VISIT = 'visit', 'Visit'
        START = 'start', 'Start (form interaction)'
        SUBMIT = 'submit', 'Submit'
        CHANNEL_SELECT = 'channel_select', 'Channel selected'

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event_type = models.CharField(max_length=20, choices=Type.choices, db_index=True)
    # Anonymous browser-session correlator (Django session key) — stitches a
    # single visitor's visit→start→submit funnel. Not PII.
    session_key = models.CharField(max_length=64, blank=True, db_index=True)
    channel = models.CharField(max_length=20, blank=True)
    entry = models.ForeignKey(
        WaitlistEntry, null=True, blank=True, on_delete=models.SET_NULL,
        related_name='events',
    )
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['event_type', 'created_at']),
        ]

    def __str__(self):
        return f'{self.event_type}@{self.created_at:%Y-%m-%dT%H:%M:%SZ}'
