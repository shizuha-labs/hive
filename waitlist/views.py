"""
HIVE-100 — Free Claw no-spend waitlist + intent survey landing page.

Server-rendered (same inline-HTML / ``_hive_shell`` convention as
``claws.views``) so it ships with zero new frontend build/vendor. All three
acceptance surfaces live here:

1. ``GET  /hive/free-claw/``            — public value prop + intent survey; emits a ``visit`` event.
2. ``POST /hive/free-claw/``            — stores the WaitlistEntry; emits a ``submit`` event.
3. ``POST /hive/api/v1/waitlist/event`` — tiny beacon for ``start`` / ``channel_select`` events.

Launch gate (HIVE-100 guardrail "requires Security/QA launch gates before any
public push"): the whole surface is behind ``HIVE_FREE_CLAW_WAITLIST_ENABLED``
(default ``False``). Until ops flips it post-sign-off, every route 404s.
"""
import logging

from django.conf import settings
from django.http import HttpResponse, HttpResponseNotFound
from django.middleware.csrf import get_token
from django.utils.html import escape
from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from .models import Channel, WaitlistEntry, WaitlistEvent

logger = logging.getLogger(__name__)

# HIVE-56 flagship Free Claw bet — the landing copy links back here.
HIVE_56_LINK = '/hive/assistants'

_USE_CASES = [
    'Personal research & writing assistant',
    'Customer support / inbox triage',
    'Coding & automation helper',
    'WhatsApp business assistant',
    'Something else',
]


def _enabled() -> bool:
    return bool(getattr(settings, 'HIVE_FREE_CLAW_WAITLIST_ENABLED', False))


def _gated_404():
    # Pre-launch: the surface exists in code but is not publicly reachable until
    # Security/QA sign off and ops sets HIVE_FREE_CLAW_WAITLIST_ENABLED=true.
    return HttpResponseNotFound('Not found.')


def _session_key(request) -> str:
    """Stable anonymous correlator for the funnel (creates the session if needed)."""
    if not request.session.session_key:
        request.session.create()
    return request.session.session_key or ''


def _record_event(event_type, *, session_key='', channel='', entry=None, metadata=None):
    """Best-effort funnel write — analytics must never break the user path."""
    try:
        WaitlistEvent.objects.create(
            event_type=event_type,
            session_key=session_key or '',
            channel=channel or '',
            entry=entry,
            metadata=metadata or {},
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception('[waitlist] failed to record %s event', event_type)


def _shell(title, body):
    """Minimal standalone page shell (public — no owner nav / no auth context)."""
    return HttpResponse(f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{ color-scheme: dark; --bg:#09111f; --panel:#111d2f; --muted:#93a4ba; --text:#eef5ff; --line:#26344a; --accent:#8bd3ff; --ok:#68d391; }}
    * {{ box-sizing: border-box; }}
    body {{ margin:0; min-height:100vh; font:16px/1.5 system-ui,-apple-system,Segoe UI,sans-serif; background:radial-gradient(circle at top left,#173154,var(--bg) 42rem); color:var(--text); }}
    header, main {{ width:min(760px, calc(100% - 32px)); margin:0 auto; }}
    header {{ display:flex; align-items:center; justify-content:space-between; padding:24px 0; }}
    a {{ color:var(--accent); }}
    .brand {{ color:var(--text); font-weight:800; letter-spacing:.02em; text-decoration:none; }}
    .panel {{ background:rgba(17,29,47,.88); border:1px solid var(--line); border-radius:18px; padding:28px; box-shadow:0 20px 60px rgba(0,0,0,.24); }}
    .hero h1 {{ font-size:2rem; margin:.2em 0; }}
    .lede {{ font-size:1.15rem; color:var(--text); }}
    .muted {{ color:var(--muted); }}
    .pill {{ display:inline-flex; border-radius:999px; border:1px solid rgba(104,211,145,.5); color:var(--ok); padding:2px 10px; font-size:12px; text-transform:uppercase; letter-spacing:.04em; }}
    button {{ border:0; border-radius:12px; padding:12px 18px; background:var(--accent); color:#06101d; font-weight:700; cursor:pointer; font-size:1rem; }}
    input, select {{ width:100%; border-radius:12px; border:1px solid var(--line); background:#09111f; color:var(--text); padding:11px 12px; font-size:1rem; }}
    label {{ display:grid; gap:6px; margin:16px 0; color:var(--muted); }}
    .banner {{ border-radius:14px; padding:12px 14px; margin:14px 0; }}
    .banner.danger {{ color:#330808; background:#ff8b8b; }}
    .banner.ok {{ color:#06210f; background:var(--ok); }}
    ul.benefits {{ padding-left:1.1em; color:var(--muted); }}
  </style>
</head>
<body>
  <header>
    <a class="brand" href="/hive/free-claw">Hive · Free Claw</a>
    <span class="pill">Free preview</span>
  </header>
  <main>{body}</main>
</body>
</html>''')



class LaunchGatedAPIView(APIView):
    """Avoid pre-launch dependencies before the gate returns its 404.

    DRF runs throttles during ``initial()`` before handler methods. The public
    waitlist JSON/beacon views use Redis-backed throttling when launched, but
    while HIVE_FREE_CLAW_WAITLIST_ENABLED is false the surface must fail closed
    without touching Redis or any other runtime dependency.
    """

    def get_throttles(self):
        if not _enabled():
            return []
        return super().get_throttles()

    def options(self, request, *args, **kwargs):
        if not _enabled():
            return _gated_404()
        return super().options(request, *args, **kwargs)


class FreeClawLandingView(APIView):
    """Public landing + intent survey. No auth — email is the identity."""
    authentication_classes = []
    permission_classes = []

    def _form_body(self, request, error='', email='', use_case='', channel=Channel.WEB):
        error_note = f'<p class="banner danger">{escape(error)}</p>' if error else ''
        channel_opts = ''.join(
            '<option value="{v}"{sel}>{label}</option>'.format(
                v=escape(v), label=escape(label),
                sel=' selected' if v == channel else '',
            )
            for v, label in Channel.choices
        )
        use_case_opts = ''.join(
            '<option value="{v}"{sel}>{v}</option>'.format(
                v=escape(uc), sel=' selected' if uc == use_case else '',
            )
            for uc in _USE_CASES
        )
        csrf = escape(get_token(request))
        return f'''
<section class="panel hero">
  <h1>Your own managed AI assistant — free to start</h1>
  <p class="lede">Free Claw gives you a personal, always-on AI assistant that Shizuha hosts and manages for you. No setup, no servers, no card required.</p>
  <ul class="benefits">
    <li>Fully managed — we run and update it; you just use it.</li>
    <li>Reach it on the web, in the app, or over WhatsApp.</li>
    <li>Part of the Hive Free Claw program (<a href="{HIVE_56_LINK}">see Hive assistants</a>).</li>
  </ul>
</section>
<br>
<section class="panel">
  <h2>Join the waitlist</h2>
  <p class="muted">Tell us how you'd use it — we'll invite you as spots open.</p>
  {error_note}
  <form action="/hive/free-claw" method="post" id="waitlist-form">
    <input type="hidden" name="csrfmiddlewaretoken" value="{csrf}">
    <label>Email <input type="email" name="email" maxlength="254" placeholder="you@example.com" value="{escape(email)}" required></label>
    <label>Where would you use it most?
      <select name="preferred_channel" id="channel-select">{channel_opts}</select>
    </label>
    <label>What's your top use case?
      <select name="top_use_case">{use_case_opts}</select>
    </label>
    <button type="submit">Join the waitlist</button>
  </form>
</section>
<script>
// No-spend, no-vendor analytics: tiny same-origin beacons for the funnel.
(function() {{
  var form = document.getElementById('waitlist-form');
  var csrf = '{csrf}';
  var started = false;
  function beacon(type, channel) {{
    try {{
      fetch('/hive/api/v1/waitlist/event', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json', 'X-CSRFToken': csrf }},
        body: JSON.stringify({{ event_type: type, channel: channel || '' }}),
        keepalive: true,
      }});
    }} catch (e) {{}}
  }}
  if (form) {{
    form.addEventListener('focusin', function() {{
      if (!started) {{ started = true; beacon('start'); }}
    }}, {{ once: false }});
  }}
  var sel = document.getElementById('channel-select');
  if (sel) {{
    sel.addEventListener('change', function() {{ beacon('channel_select', sel.value); }});
  }}
}})();
</script>'''

    def get(self, request):
        if not _enabled():
            return _gated_404()
        sk = _session_key(request)
        _record_event(WaitlistEvent.Type.VISIT, session_key=sk)
        return _shell('Free Claw — join the waitlist', self._form_body(request))

    def post(self, request):
        if not _enabled():
            return _gated_404()
        sk = _session_key(request)
        data = request.data
        email = (data.get('email') or '').strip().lower()
        channel = (data.get('preferred_channel') or Channel.WEB).strip()
        use_case = (data.get('top_use_case') or '').strip()[:500]

        if not email or '@' not in email or '.' not in email.split('@')[-1]:
            return _shell(
                'Free Claw — join the waitlist',
                self._form_body(request, error='Please enter a valid email address.',
                                email=email, use_case=use_case, channel=channel),
            )
        if channel not in Channel.values:
            channel = Channel.WEB

        owner_id = None
        owner_username = ''
        user = getattr(request, 'user', None)
        if user is not None and getattr(user, 'is_authenticated', False):
            owner_id = getattr(user, 'id', None)
            owner_username = getattr(user, 'username', '') or ''

        entry, _created = WaitlistEntry.objects.update_or_create(
            email=email,
            defaults={
                'preferred_channel': channel,
                'top_use_case': use_case,
                'owner_id': owner_id,
                'owner_username': owner_username,
            },
        )
        _record_event(
            WaitlistEvent.Type.SUBMIT, session_key=sk, channel=channel, entry=entry,
            metadata={'created': _created},
        )
        body = '''
<section class="panel">
  <h1>You're on the list 🎉</h1>
  <p class="lede banner ok">Thanks — we'll email you when your Free Claw is ready.</p>
  <p class="muted">Want to look around in the meantime? <a href="''' + HIVE_56_LINK + '''">Explore Hive assistants</a>.</p>
</section>'''
        return _shell('Free Claw — you\'re on the list', body)




class WaitlistSignupView(LaunchGatedAPIView):
    """JSON adapter for the public React HIVE landing waitlist form.

    Reuses the HIVE-100 WaitlistEntry/WaitlistEvent store rather than creating a
    second launch list. The HIVE_FREE_CLAW_WAITLIST_ENABLED launch gate remains
    authoritative, so this endpoint stays 404 until Security/QA sign-off and ops
    enable the surface.
    """
    authentication_classes = []
    permission_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'waitlist'

    def post(self, request):
        if not _enabled():
            return _gated_404()
        data = request.data if isinstance(request.data, dict) else {}
        email = (data.get('email') or '').strip().lower()
        if not email or '@' not in email or '.' not in email.split('@')[-1]:
            return Response({'error': 'valid email required'}, status=status.HTTP_400_BAD_REQUEST)

        channel = (data.get('preferred_channel') or Channel.WEB).strip()
        if channel not in Channel.values:
            channel = Channel.WEB
        use_case = (data.get('top_use_case') or '').strip()[:500]

        owner_id = None
        owner_username = ''
        user = getattr(request, 'user', None)
        if user is not None and getattr(user, 'is_authenticated', False):
            owner_id = getattr(user, 'id', None)
            owner_username = getattr(user, 'username', '') or ''

        entry, created = WaitlistEntry.objects.update_or_create(
            email=email,
            defaults={
                'preferred_channel': channel,
                'top_use_case': use_case,
                'owner_id': owner_id,
                'owner_username': owner_username,
                'source': 'hive_public_landing',
            },
        )
        _record_event(
            WaitlistEvent.Type.SUBMIT,
            session_key=_session_key(request),
            channel=channel,
            entry=entry,
            metadata={'created': created, 'source': 'hive_public_landing'},
        )
        return Response(
            {'ok': True, 'status': 'joined' if created else 'already_joined'},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

class WaitlistEventView(LaunchGatedAPIView):
    """Same-origin analytics beacon for start / channel_select funnel events."""
    authentication_classes = []
    permission_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'waitlist'

    _ACCEPTED = {WaitlistEvent.Type.START, WaitlistEvent.Type.CHANNEL_SELECT}

    def post(self, request):
        if not _enabled():
            return _gated_404()
        event_type = (request.data.get('event_type') or '').strip()
        if event_type not in self._ACCEPTED:
            return Response({'error': 'unsupported event_type'},
                            status=status.HTTP_400_BAD_REQUEST)
        channel = (request.data.get('channel') or '').strip()
        if channel and channel not in Channel.values:
            channel = ''
        _record_event(event_type, session_key=_session_key(request), channel=channel)
        return Response(status=status.HTTP_204_NO_CONTENT)
