"""Minimal fleet management UI — HIVE-B4 operator surface."""
import json
from django.http import HttpResponse
from django.middleware.csrf import get_token
from django.utils.html import escape
from django.views import View
from claws.authentication import FederatedJWTAuthentication
from .models import FleetAgent
from .parallelism import native_agent_parallelism
from . import provisioner

_AUTH = FederatedJWTAuthentication()

STATUS_PILL = {
    'running': 'running',
    'hibernated': 'hibernated',
    'stopped': 'stopped',
    'provisioning': 'provisioning',
    'pending': 'pending',
    'deleting': 'deleting',
    'failed': 'failed',
    'deleted': 'deleted',
    'blocked': 'blocked',
}


def _fleet_shell(title, body, request=None):
    user_label = ''
    if request and getattr(request, 'user', None) and getattr(request.user, 'is_authenticated', False):
        user_label = f'<span class="user">{escape(getattr(request.user, "username", ""))}</span>'
    return f'''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{color-scheme:dark;--bg:#09111f;--panel:#111d2f;--muted:#93a4ba;--text:#eef5ff;--line:#26344a;--accent:#8bd3ff;--ok:#68d391;--warn:#f6c453;--danger:#ff8b8b;}}
    *{{box-sizing:border-box;}}
    body{{margin:0;min-height:100vh;font:16px/1.45 system-ui,sans-serif;background:radial-gradient(circle at top left,#173154,var(--bg) 42rem);color:var(--text);}}
    header,main{{width:min(1120px,calc(100% - 32px));margin:0 auto;}}
    header{{display:flex;align-items:center;justify-content:space-between;padding:24px 0;}}
    a{{color:var(--accent);}} .brand{{color:var(--text);font-weight:800;letter-spacing:.02em;text-decoration:none;}}
    .nav{{display:flex;gap:16px;align-items:center;color:var(--muted);}}
    .panel{{background:rgba(17,29,47,.88);border:1px solid var(--line);border-radius:18px;padding:24px;box-shadow:0 20px 60px rgba(0,0,0,.24);}}
    table{{width:100%;border-collapse:collapse;}} th,td{{text-align:left;padding:10px 12px;border-bottom:1px solid var(--line);}} th{{color:var(--muted);font-weight:600;font-size:13px;}}
    .pill{{display:inline-flex;align-items:center;border-radius:999px;border:1px solid var(--line);padding:2px 10px;font-size:12px;text-transform:uppercase;letter-spacing:.04em;}}
    .pill.running{{border-color:rgba(104,211,145,.5);color:var(--ok);}}
    .pill.hibernated{{border-color:rgba(139,211,255,.55);color:var(--accent);}}
    .pill.failed,.pill.blocked{{border-color:rgba(255,139,139,.5);color:var(--danger);}}
    .pill.deleting,.pill.provisioning,.pill.pending{{border-color:rgba(246,196,83,.5);color:var(--warn);}}
    .pill.stopped{{border-color:var(--line);color:var(--muted);}}
    .actions{{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px;}}
    .btn{{border:0;border-radius:10px;padding:8px 14px;background:var(--accent);color:#06101d;font-weight:700;cursor:pointer;font-size:14px;text-decoration:none;}}
    .btn.secondary{{background:#20324d;color:var(--text);border:1px solid var(--line);}}
    .btn.danger{{background:var(--danger);color:#06101d;}}
    input,select{{width:100%;border-radius:10px;border:1px solid var(--line);background:#09111f;color:var(--text);padding:9px 12px;}}
    label{{display:grid;gap:5px;margin:10px 0;color:var(--muted);font-size:14px;}}
    .error{{border-radius:12px;padding:12px 14px;margin:12px 0;color:#330808;background:var(--danger);}}
    .muted{{color:var(--muted);}}
    .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;margin-top:16px;}}
    .card{{background:#0d1728;border:1px solid var(--line);border-radius:14px;padding:18px;}}
    details summary{{cursor:pointer;color:var(--accent);user-select:none;}}
    pre{{background:#0a0f1a;border-radius:10px;padding:12px;overflow:auto;font-size:13px;color:#c9d7ff;}}
  </style>
</head>
<body>
<header>
  <a href="/hive/" class="brand">Shizuha Hive</a>
  <div class="nav">
    <a href="/hive/assistants/">Assistants</a>
    <a href="/hive/fleet/">Fleet</a>
    {user_label}
  </div>
</header>
<main>{body}</main>
</body>
</html>'''


def _auth_user(request):
    """Return (user, error_response) from the JWT cookie/header."""
    try:
        result = _AUTH.authenticate(request)
    except Exception:
        return None, None
    if result is None:
        return None, None
    user, _ = result
    return user, None


class FleetListView(View):
    """GET /hive/fleet/ — list all fleet agents with status + quick actions."""

    def get(self, request):
        user, _ = _auth_user(request)
        if not user or not user.is_staff:
            return HttpResponse(
                _fleet_shell('Fleet — Login Required', '<div class="panel"><p>Staff login required. <a href="/hive/login/">Log in</a></p></div>'),
                content_type='text/html',
            )
        agents = FleetAgent.objects.exclude(status=FleetAgent.Status.DELETED).order_by('agent_username')
        rows = ''
        for a in agents:
            pill_cls = STATUS_PILL.get(a.status, 'stopped')
            enable_btn = ''
            disable_btn = ''
            csrf = escape(get_token(request))
            if a.status in {
                FleetAgent.Status.HIBERNATED,
                FleetAgent.Status.STOPPED,
            }:
                enable_btn = f'<form method="post" action="/hive/fleet/agents/{a.id}/enable/" style="display:inline"><input type="hidden" name="csrfmiddlewaretoken" value="{csrf}"><button class="btn" type="submit">Enable</button></form>'
            elif a.status == FleetAgent.Status.RUNNING:
                disable_btn = f'<form method="post" action="/hive/fleet/agents/{a.id}/disable/" style="display:inline"><input type="hidden" name="csrfmiddlewaretoken" value="{csrf}"><button class="btn secondary" type="submit">Disable</button></form>'
            rows += f'''<tr>
  <td><a href="/hive/fleet/agents/{a.id}/">{escape(a.agent_username)}</a></td>
  <td>{escape(a.display_name or a.agent_username)}</td>
  <td>{escape(a.role or '—')}</td>
  <td><span class="pill {pill_cls}">{escape(a.status)}</span></td>
  <td class="muted" style="font-size:13px">{escape(a.image or '(default)')}</td>
  <td>{enable_btn}{disable_btn}<a href="/hive/fleet/agents/{a.id}/" class="btn secondary" style="margin-left:4px">Detail</a></td>
</tr>'''
        body = f'''<div class="panel">
<h2 style="margin-top:0">Fleet Agents</h2>
<p class="muted">Operator-managed dev-agent fleet running on k3s. <a href="/hive/fleet/agents/new/">+ Add agent</a></p>
<table>
  <thead><tr><th>Username</th><th>Name</th><th>Role</th><th>Status</th><th>Image</th><th>Actions</th></tr></thead>
  <tbody>{rows if rows else '<tr><td colspan="6" class="muted">No agents provisioned yet.</td></tr>'}</tbody>
</table>
</div>'''
        return HttpResponse(_fleet_shell('Fleet — Shizuha Hive', body, request), content_type='text/html')


class FleetAgentFormView(View):
    """GET /hive/fleet/agents/new/ — new agent form; POST — create+provision."""

    def get(self, request):
        user, _ = _auth_user(request)
        if not user or not user.is_staff:
            return HttpResponse(status=403)
        csrf = escape(get_token(request))
        body = f'''<div class="panel">
<h2 style="margin-top:0">Add Fleet Agent</h2>
<form method="post" action="/hive/fleet/agents/new/">
<input type="hidden" name="csrfmiddlewaretoken" value="{csrf}">
<label>Username (DNS-1123, ≤47 chars, drives k8s names)
  <input name="agent_username" required pattern="[a-z0-9][a-z0-9-]*[a-z0-9]" maxlength="47"></label>
<label>Display Name <input name="display_name"></label>
<label>Role <input name="role"></label>
<label>Agent ID (platform AGENT_ID env) <input name="agent_id"></label>
<label>Image (blank = cluster default) <input name="image" placeholder="localhost:30500/shizuha-runtime:latest"></label>
<label>Resource Profile
  <select name="resource_profile">
    <option value="small">Small</option>
    <option value="standard" selected>Standard</option>
    <option value="heavy">Heavy</option>
  </select>
</label>
<label>Agent Password (written into identity Secret, never stored here)
  <input type="password" name="agent_password" autocomplete="new-password"></label>
<label><input type="checkbox" name="isolated"> Isolated namespace (DevOps SSH agents)</label>
<label><input type="checkbox" name="expose_http"> Expose HTTP (dashboard agent)</label>
<div class="actions"><button class="btn" type="submit">Add &amp; Provision</button>
<a href="/hive/fleet/" class="btn secondary">Cancel</a></div>
</form></div>'''
        return HttpResponse(_fleet_shell('New Fleet Agent', body, request), content_type='text/html')

    def post(self, request):
        user, _ = _auth_user(request)
        if not user or not user.is_staff:
            return HttpResponse(status=403)
        from .serializers import FleetAgentCreateSerializer
        from .models import FleetAgent
        data = {
            'agent_username': request.POST.get('agent_username', '').strip(),
            'display_name': request.POST.get('display_name', '').strip(),
            'role': request.POST.get('role', '').strip(),
            'agent_id': request.POST.get('agent_id', '').strip(),
            'image': request.POST.get('image', '').strip(),
            'resource_profile': request.POST.get('resource_profile', 'standard'),
            'isolated': bool(request.POST.get('isolated')),
            'expose_http': bool(request.POST.get('expose_http')),
        }
        serializer = FleetAgentCreateSerializer(data=data)
        if not serializer.is_valid():
            errors = '<br>'.join(f'{k}: {v[0]}' for k, v in serializer.errors.items())
            body = f'<div class="panel"><div class="error">{escape(errors)}</div><a href="/hive/fleet/agents/new/" class="btn secondary">Back</a></div>'
            return HttpResponse(_fleet_shell('Error', body, request), content_type='text/html', status=400)
        try:
            agent = serializer.save()
        except Exception as e:
            body = f'<div class="panel"><div class="error">{escape(str(e))}</div><a href="/hive/fleet/agents/new/" class="btn secondary">Back</a></div>'
            return HttpResponse(_fleet_shell('Error', body, request), content_type='text/html', status=400)
        provisioner.provision_agent(agent, agent_password=request.POST.get('agent_password', ''))
        from django.http import HttpResponseRedirect
        return HttpResponseRedirect(f'/hive/fleet/agents/{agent.id}/')


class FleetAgentDetailUIView(View):
    """GET /hive/fleet/agents/{id}/ — agent detail with configure+action forms."""

    def get(self, request, agent_id):
        user, _ = _auth_user(request)
        if not user or not user.is_staff:
            return HttpResponse(status=403)
        try:
            agent = FleetAgent.objects.get(id=agent_id)
        except FleetAgent.DoesNotExist:
            return HttpResponse(status=404)

        pill_cls = STATUS_PILL.get(agent.status, 'stopped')
        events = agent.events.all()[:10]
        event_rows = ''.join(
            f'<tr><td style="font-size:12px;color:var(--muted)">{e.created_at.strftime("%Y-%m-%d %H:%M") if e.created_at else ""}</td>'
            f'<td style="font-size:13px">{escape(e.event_type)}</td>'
            f'<td style="font-size:13px">{escape(e.message[:120])}</td></tr>'
            for e in events
        )

        csrf = escape(get_token(request))
        enable_btn = disable_btn = ''
        if agent.status in {
            FleetAgent.Status.HIBERNATED,
            FleetAgent.Status.STOPPED,
        }:
            enable_btn = f'<form method="post" action="/hive/fleet/agents/{agent.id}/enable/" style="display:inline"><input type="hidden" name="csrfmiddlewaretoken" value="{csrf}"><button class="btn" type="submit">Enable</button></form>'
        elif agent.status == FleetAgent.Status.RUNNING:
            disable_btn = f'<form method="post" action="/hive/fleet/agents/{agent.id}/disable/" style="display:inline"><input type="hidden" name="csrfmiddlewaretoken" value="{csrf}"><button class="btn secondary" type="submit">Disable</button></form>'
        parallelism = native_agent_parallelism(agent)
        parallel_label = f'{parallelism.effective_max_tasks} task{"s" if parallelism.effective_max_tasks != 1 else ""}'

        body = f'''<div class="panel">
<p><a href="/hive/fleet/">← Fleet</a></p>
<h2 style="margin-top:0">{escape(agent.agent_username)} <span class="pill {pill_cls}">{escape(agent.status)}</span></h2>
<p class="muted">{escape(agent.display_name or '')} &middot; {escape(agent.role or '')}</p>
<div class="actions">{enable_btn}{disable_btn}
  <a href="/hive/fleet/agents/{agent.id}/configure/" class="btn secondary">Configure</a>
  <a href="/hive/fleet/agents/{agent.id}/switch-backend/" class="btn secondary">Switch Backend</a>
  <a href="/hive/api/v1/fleet/agents/{agent.id}/status/" class="btn secondary">Live Status (JSON)</a>
</div>
<div class="grid">
  <div class="card"><h4 style="margin:0 0 8px">Identity</h4>
    <p class="muted">Username</p><p>{escape(agent.agent_username)}</p>
    <p class="muted">Agent ID</p><p>{escape(agent.agent_id or '—')}</p>
    <p class="muted">Namespace</p><p>{escape(agent.namespace or '—')}</p>
    <p class="muted">Isolated</p><p>{'Yes' if agent.isolated else 'No'}</p>
  </div>
  <div class="card"><h4 style="margin:0 0 8px">Runtime</h4>
    <p class="muted">Image</p><p style="font-size:13px">{escape(agent.image or '(cluster default)')}</p>
    <p class="muted">Profile</p><p>{escape(agent.resource_profile)}</p>
    <p class="muted">Arch</p><p>{escape(agent.arch)}</p>
    <p class="muted">Parallel readiness</p><p>{escape(parallelism.status)} · {escape(parallel_label)}</p>
    <p class="muted">Parallel evidence</p><p style="font-size:13px">{escape(parallelism.reason)}</p>
  </div>
  <div class="card"><h4 style="margin:0 0 8px">Sidecars &amp; Wiring</h4>
    <p class="muted">Sidecars</p><p>{escape(", ".join(agent.sidecar_set) or "none")}</p>
    <p class="muted">Platform URL</p><p style="font-size:13px">{escape(agent.platform_url)}</p>
    <p class="muted">Sandbox Required</p><p>{'Yes' if agent.sandbox_required else 'No'}</p>
  </div>
</div>
<h3>Recent Events</h3>
<table><thead><tr><th>Time</th><th>Type</th><th>Message</th></tr></thead>
<tbody>{event_rows if event_rows else '<tr><td colspan="3" class="muted">No events.</td></tr>'}</tbody></table>
<div style="margin-top:16px"><details><summary>Raw config (JSON)</summary>
<pre>{escape(json.dumps({"id": str(agent.id), "agent_username": agent.agent_username, "agent_id": agent.agent_id, "display_name": agent.display_name, "role": agent.role, "image": agent.image, "resource_profile": agent.resource_profile, "arch": agent.arch, "namespace": agent.namespace, "status": agent.status, "platform_url": agent.platform_url, "sidecar_set": agent.sidecar_set, "isolated": agent.isolated, "sandbox_required": agent.sandbox_required, "pvc_size": agent.pvc_size, "effective_parallelism": parallelism.as_dict()}, indent=2))}</pre>
</details></div>
</div>'''
        return HttpResponse(_fleet_shell(f'{agent.agent_username} — Fleet', body, request), content_type='text/html')


class FleetAgentConfigureUIView(View):
    """GET/POST /hive/fleet/agents/{id}/configure/ — edit non-identity fields."""

    def get(self, request, agent_id):
        user, _ = _auth_user(request)
        if not user or not user.is_staff:
            return HttpResponse(status=403)
        try:
            agent = FleetAgent.objects.get(id=agent_id)
        except FleetAgent.DoesNotExist:
            return HttpResponse(status=404)
        csrf = escape(get_token(request))
        body = f'''<div class="panel">
<p><a href="/hive/fleet/agents/{agent.id}/">← {escape(agent.agent_username)}</a></p>
<h2 style="margin-top:0">Configure {escape(agent.agent_username)}</h2>
<form method="post">
<input type="hidden" name="csrfmiddlewaretoken" value="{csrf}">
<label>Display Name <input name="display_name" value="{escape(agent.display_name or '')}"></label>
<label>Role <input name="role" value="{escape(agent.role or '')}"></label>
<label>Resource Profile
  <select name="resource_profile">
    {''.join(f'<option value="{v}" {"selected" if agent.resource_profile == v else ""}>{l}</option>' for v, l in FleetAgent.ResourceProfile.choices)}
  </select>
</label>
<label>Platform URL <input name="platform_url" value="{escape(agent.platform_url)}"></label>
<div class="actions"><button class="btn" type="submit">Save</button>
<a href="/hive/fleet/agents/{agent.id}/" class="btn secondary">Cancel</a></div>
</form></div>'''
        return HttpResponse(_fleet_shell(f'Configure {agent.agent_username}', body, request), content_type='text/html')

    def post(self, request, agent_id):
        user, _ = _auth_user(request)
        if not user or not user.is_staff:
            return HttpResponse(status=403)
        try:
            agent = FleetAgent.objects.get(id=agent_id)
        except FleetAgent.DoesNotExist:
            return HttpResponse(status=404)
        fields = []
        if request.POST.get('display_name') is not None:
            agent.display_name = request.POST['display_name'].strip()
            fields.append('display_name')
        if request.POST.get('role') is not None:
            agent.role = request.POST['role'].strip()
            fields.append('role')
        if request.POST.get('resource_profile'):
            agent.resource_profile = request.POST['resource_profile']
            fields.append('resource_profile')
        if 'platform_url' in request.POST:
            agent.platform_url = request.POST['platform_url'].strip()
            fields.append('platform_url')
        if fields:
            fields.append('updated_at')
            agent.save(update_fields=fields)
        from django.http import HttpResponseRedirect
        return HttpResponseRedirect(f'/hive/fleet/agents/{agent.id}/')


class FleetAgentSwitchBackendUIView(View):
    """GET/POST /hive/fleet/agents/{id}/switch-backend/ — change image."""

    def get(self, request, agent_id):
        user, _ = _auth_user(request)
        if not user or not user.is_staff:
            return HttpResponse(status=403)
        try:
            agent = FleetAgent.objects.get(id=agent_id)
        except FleetAgent.DoesNotExist:
            return HttpResponse(status=404)
        csrf = escape(get_token(request))
        body = f'''<div class="panel">
<p><a href="/hive/fleet/agents/{agent.id}/">← {escape(agent.agent_username)}</a></p>
<h2 style="margin-top:0">Switch Backend — {escape(agent.agent_username)}</h2>
<p class="muted">Current image: <code>{escape(agent.image or "(cluster default)")}</code></p>
<form method="post">
<input type="hidden" name="csrfmiddlewaretoken" value="{csrf}">
<label>New Image <input name="image" required value="{escape(agent.image or '')}" placeholder="localhost:30500/shizuha-runtime:glm-5.1"></label>
<label>Pull Policy
  <select name="image_pull_policy">
    {''.join(f'<option value="{p}" {"selected" if agent.image_pull_policy == p else ""}>{p}</option>' for p in ["Always", "IfNotPresent", "Never"])}
  </select>
</label>
<p class="muted" style="font-size:14px">If the agent is running, the live Deployment will be patched immediately (rolling restart).</p>
<div class="actions"><button class="btn" type="submit">Switch Backend</button>
<a href="/hive/fleet/agents/{agent.id}/" class="btn secondary">Cancel</a></div>
</form></div>'''
        return HttpResponse(_fleet_shell(f'Switch Backend — {agent.agent_username}', body, request), content_type='text/html')

    def post(self, request, agent_id):
        user, _ = _auth_user(request)
        if not user or not user.is_staff:
            return HttpResponse(status=403)
        try:
            agent = FleetAgent.objects.get(id=agent_id)
        except FleetAgent.DoesNotExist:
            return HttpResponse(status=404)
        from django.http import HttpResponseRedirect
        new_image = request.POST.get('image', '').strip()
        pull_policy = request.POST.get('image_pull_policy', 'IfNotPresent')
        if not new_image:
            return HttpResponseRedirect(f'/hive/fleet/agents/{agent_id}/')
        from .runtime_release import is_managed_runtime_image_ref
        if is_managed_runtime_image_ref(new_image):
            return HttpResponse(
                'Managed runtime images are adopted only through a reviewed '
                'DesiredRuntimeRelease.',
                status=409,
                content_type='text/plain',
            )
        old_image = agent.image
        agent.image = new_image
        agent.image_pull_policy = pull_policy
        agent.save(update_fields=['image', 'image_pull_policy', 'updated_at'])
        from .models import FleetAgentEvent
        FleetAgentEvent.objects.create(
            agent=agent, event_type='backend_switched',
            message=f'image: {old_image!r} -> {new_image!r} (pull: {pull_policy})',
        )
        if agent.status == FleetAgent.Status.RUNNING:
            try:
                core_v1, apps_v1, _ = provisioner.get_k8s_client()
                ns = provisioner._live_ns(agent)
                deploy = apps_v1.read_namespaced_deployment(agent.deployment_name, ns)
                # The runtime container is named 'agent' (not the username); also
                # accept a legacy username-named container and, as a fallback, any
                # container carrying a shizuha-agent-runtime image. Matching only
                # by username silently no-op'd the image change (SCLI-331).
                matched = []
                for c in deploy.spec.template.spec.containers:
                    if c.name in ('agent', agent.agent_username) or 'shizuha-agent-runtime' in (c.image or ''):
                        c.image = new_image
                        c.image_pull_policy = pull_policy
                        matched.append(c.name)
                apps_v1.patch_namespaced_deployment(agent.deployment_name, ns, deploy)
                FleetAgentEvent.objects.create(
                    agent=agent, event_type='backend_applied',
                    message=f'Deployment patched with {new_image!r} (containers: {matched or "none-matched"})',
                )
            except Exception as exc:
                FleetAgentEvent.objects.create(
                    agent=agent, event_type='backend_patch_failed', message=str(exc),
                )
        return HttpResponseRedirect(f'/hive/fleet/agents/{agent_id}/')


class FleetAgentEnableUIView(View):
    """POST /hive/fleet/agents/{id}/enable/ — start from UI form."""

    def post(self, request, agent_id):
        user, _ = _auth_user(request)
        if not user or not user.is_staff:
            return HttpResponse(status=403)
        try:
            agent = FleetAgent.objects.get(id=agent_id)
        except FleetAgent.DoesNotExist:
            return HttpResponse(status=404)
        if not provisioner.enable_or_provision_agent(agent):
            message = provisioner.start_failure_message(agent)
            body = f'''<div class="panel">
<p><a href="/hive/fleet/agents/{agent.id}/">← {escape(agent.agent_username)}</a></p>
<div class="error">{escape(message)}</div>
</div>'''
            return HttpResponse(
                _fleet_shell(f'Enable failed — {agent.agent_username}', body, request),
                status=500,
                content_type='text/html',
            )
        from django.http import HttpResponseRedirect
        return HttpResponseRedirect(f'/hive/fleet/agents/{agent_id}/')


class FleetAgentDisableUIView(View):
    """POST /hive/fleet/agents/{id}/disable/ — stop from UI form."""

    def post(self, request, agent_id):
        user, _ = _auth_user(request)
        if not user or not user.is_staff:
            return HttpResponse(status=403)
        try:
            agent = FleetAgent.objects.get(id=agent_id)
        except FleetAgent.DoesNotExist:
            return HttpResponse(status=404)
        from fleet.lifecycle_fleet import hibernate_lifecycle_agent
        hibernate_lifecycle_agent(
            str(agent.id),
            reason='Hive UI disable (hibernate-only; operator stop is separate)',
        )
        from django.http import HttpResponseRedirect
        return HttpResponseRedirect(f'/hive/fleet/agents/{agent_id}/')
