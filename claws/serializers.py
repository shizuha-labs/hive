from urllib.parse import quote

from django.conf import settings
from rest_framework import serializers
from .models import Claw, ClawEvent, ClawExportJob, WhatsAppLink, OrgTemplate, TenantProvisioningRun
from hive_platform.models import TokenEntry, TokenPool
from hive_project.avatars import avatar_url_for


class ClawSerializer(serializers.ModelSerializer):
    dashboard_path = serializers.SerializerMethodField()
    ingress_url = serializers.SerializerMethodField()
    lifecycle_annotations = serializers.SerializerMethodField()
    connect_profile = serializers.SerializerMethodField()

    class Meta:
        model = Claw
        fields = [
            'id', 'owner_id', 'owner_username', 'owner_email',
            'name', 'tier_id', 'agent_type', 'model', 'platform_url',
            'namespace', 'status', 'status_message', 'runtime_image',
            'runtime_compat_mode', 'pvc_size',
            'dashboard_path', 'ingress_url', 'connect_profile',
            'recycle_after', 'reclaim_now', 'legal_hold',
            'lifecycle_annotations',
            'created_at', 'updated_at', 'last_active_at', 'provisioned_at',
        ]
        read_only_fields = [
            'id', 'owner_id', 'owner_username', 'owner_email',
            'agent_type', 'model',
            'namespace', 'status', 'status_message',
            'runtime_image', 'runtime_compat_mode',
            'recycle_after', 'reclaim_now', 'legal_hold',
            'created_at', 'updated_at', 'last_active_at', 'provisioned_at',
        ]

    def get_dashboard_path(self, obj):
        # HLD §5: list rows carry the deep-link path; the signed URL is minted
        # on demand at GET /v1/claws/<id>/dashboard-url (HIVE-6, follow-up PR).
        if obj.agent_type != Claw.AgentType.CLAW:
            return None
        return f'/v1/claws/{obj.id}/dashboard-url'

    def get_ingress_url(self, obj):
        # HLD §5.2: the claw's reachable ingress path (OpenClaw dashboard
        # surface). Path-based on the platform host; the signed token is
        # appended on demand at the dashboard-url endpoint.
        if obj.agent_type != Claw.AgentType.CLAW:
            return None
        template = getattr(
            settings, 'HIVE_OPENCLAW_DASHBOARD_URL_TEMPLATE',
            '/hive/openclaw/{owner_id}/{claw_id}/',
        )
        return template.format(
            claw_id=obj.id,
            owner_id=obj.owner_id,
            namespace=obj.namespace,
        )

    def get_lifecycle_annotations(self, obj):
        from .lifecycle import annotation_state
        return annotation_state(obj)

    def get_connect_profile(self, obj):
        if obj.agent_type not in (
            Claw.AgentType.CLAW,
            Claw.AgentType.HERMES,
            Claw.AgentType.SCLI,
        ):
            return None
        return connect_agent_payload(obj)


def _runtime_kind(obj):
    agent_type = getattr(obj, 'agent_type', obj)
    if agent_type == Claw.AgentType.HERMES and getattr(obj, 'runtime_compat_mode', False):
        return 'openclaw-compat'
    if agent_type == Claw.AgentType.HERMES:
        return 'hermes'
    if agent_type == Claw.AgentType.CLAW:
        return 'openclaw'
    return str(agent_type or '')


def _connect_stable_id(obj):
    return f'hive:{obj.agent_type}:{obj.id}'


def _connect_username(obj):
    return f'hive-{obj.agent_type}-{obj.id.hex}'


def connect_agent_payload(obj):
    stable_id = _connect_stable_id(obj)
    model = obj.model or getattr(settings, 'HIVE_CORTEX_DEFAULT_MODEL', 'DeepSeek-V4-Flash')
    return {
        'id': stable_id,
        'stable_id': stable_id,
        'source': 'hive',
        'kind': 'personal_agent',
        'hive_claw_id': str(obj.id),
        'agent_type': obj.agent_type,
        'runtime_kind': _runtime_kind(obj),
        'backing_image': getattr(obj, 'runtime_image', '') or '',
        'runtime_compat_mode': bool(getattr(obj, 'runtime_compat_mode', False)),
        'display_name': obj.name or obj.get_agent_type_display(),
        'connect_username': _connect_username(obj),
        'owner_id': obj.owner_id,
        'owner_username': obj.owner_username,
        'owner_email': obj.owner_email,
        # HIVE-1242: portrait URL for the owning identity, or None (initials).
        'avatar_url': avatar_url_for(obj.owner_username),
        'model': model,
        'effective_model': model,
        'model_provider': 'Cortex',
        'runtime_backend': 'cortex',
        'status': obj.status,
        'liveness': {
            'status': obj.status,
            'status_message': obj.status_message,
            'namespace': obj.namespace,
            'pod_name': getattr(obj, 'pod_name', ''),
            'last_active_at': getattr(obj, 'last_active_at', None),
            'provisioned_at': getattr(obj, 'provisioned_at', None),
            'updated_at': getattr(obj, 'updated_at', None),
        },
        'open_in_connect_path': f"/connect/?agent={quote(stable_id, safe='')}",
    }


class ClawConnectAgentSerializer(serializers.ModelSerializer):
    stable_id = serializers.SerializerMethodField()
    source = serializers.SerializerMethodField()
    kind = serializers.SerializerMethodField()
    hive_claw_id = serializers.SerializerMethodField()
    runtime_kind = serializers.SerializerMethodField()
    backing_image = serializers.SerializerMethodField()
    runtime_compat_mode = serializers.SerializerMethodField()
    display_name = serializers.SerializerMethodField()
    connect_username = serializers.SerializerMethodField()
    effective_model = serializers.SerializerMethodField()
    model_provider = serializers.SerializerMethodField()
    runtime_backend = serializers.SerializerMethodField()
    liveness = serializers.SerializerMethodField()
    open_in_connect_path = serializers.SerializerMethodField()
    avatar_url = serializers.SerializerMethodField()

    class Meta:
        model = Claw
        fields = [
            'stable_id', 'source', 'kind', 'hive_claw_id', 'agent_type',
            'runtime_kind', 'backing_image', 'runtime_compat_mode',
            'display_name', 'connect_username', 'avatar_url',
            'owner_id', 'owner_username', 'owner_email',
            'model', 'effective_model', 'model_provider', 'runtime_backend',
            'status', 'liveness', 'open_in_connect_path',
        ]

    def _payload(self, obj):
        return connect_agent_payload(obj)

    def get_stable_id(self, obj):
        return self._payload(obj)['stable_id']

    def get_source(self, obj):
        return 'hive'

    def get_kind(self, obj):
        return 'personal_agent'

    def get_hive_claw_id(self, obj):
        return str(obj.id)

    def get_runtime_kind(self, obj):
        return _runtime_kind(obj)

    def get_backing_image(self, obj):
        return self._payload(obj)['backing_image']

    def get_runtime_compat_mode(self, obj):
        return self._payload(obj)['runtime_compat_mode']

    def get_display_name(self, obj):
        return obj.name or obj.get_agent_type_display()

    def get_connect_username(self, obj):
        return _connect_username(obj)

    def get_avatar_url(self, obj):
        return self._payload(obj)['avatar_url']

    def get_effective_model(self, obj):
        return self._payload(obj)['effective_model']

    def get_model_provider(self, obj):
        return 'Cortex'

    def get_runtime_backend(self, obj):
        return 'cortex'

    def get_liveness(self, obj):
        return self._payload(obj)['liveness']

    def get_open_in_connect_path(self, obj):
        return self._payload(obj)['open_in_connect_path']


class ClawCreateSerializer(serializers.Serializer):
    """
    Input for provisioning a new claw — HLD §5 ``POST /v1/claws {name, tier_id}``.

    The owner (owner_id / username / email) is derived from the authenticated
    caller, never from the request body.
    """
    name = serializers.CharField(max_length=100)
    tier_id = serializers.CharField(max_length=50, required=False, allow_blank=True, default='')
    # Runtime type: 'claw' (OpenClaw, default) or 'scli' (shizuha-runtime).
    agent_type = serializers.ChoiceField(
        choices=Claw.AgentType.choices, required=False, default=Claw.AgentType.CLAW,
    )
    # Optional per-owner Cortex model id; blank falls back to the tier default.
    model = serializers.CharField(max_length=128, required=False, allow_blank=True, default='')


class ClawRenameSerializer(serializers.Serializer):
    """Input for ``PATCH /v1/claws/<id> {name}`` (display-name only, HLD M4)."""
    name = serializers.CharField(max_length=100)


class ClawEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClawEvent
        fields = ['event_type', 'message', 'created_at']


class ClawStatusSerializer(serializers.Serializer):
    """Live claw status from k8s."""
    status = serializers.CharField()
    namespace = serializers.CharField()
    pod_status = serializers.CharField()
    pod_ip = serializers.CharField(allow_null=True)
    pod_name = serializers.CharField(required=False)
    containers = serializers.IntegerField()


class LegalHoldSerializer(serializers.Serializer):
    """Input for admin legal-hold mutation endpoints (HIVE-4 section 5.4)."""
    reason = serializers.CharField(required=False, allow_blank=True)
    case_id = serializers.CharField(required=False, allow_blank=True, max_length=128)


class LifecycleStateSerializer(serializers.Serializer):
    """Documented shape returned by lifecycle/backstop endpoints."""
    claw_id = serializers.CharField()
    namespace = serializers.CharField()
    legal_hold = serializers.BooleanField()
    legal_hold_reason = serializers.CharField(allow_blank=True)
    legal_hold_case_id = serializers.CharField(allow_blank=True)
    legal_hold_set_by = serializers.CharField(allow_blank=True)
    legal_hold_set_at = serializers.DateTimeField(allow_null=True)
    recycle_after = serializers.DateTimeField(allow_null=True)
    reclaim_now = serializers.BooleanField()
    annotations = serializers.DictField(child=serializers.CharField())


class ClawExportRequestSerializer(serializers.Serializer):
    """Owner/admin export request body (HIVE-4 §5)."""
    include_session = serializers.BooleanField(required=False, default=False)
    include_credits = serializers.BooleanField(required=False, default=True)
    step_up_token = serializers.CharField(required=False, allow_blank=False, write_only=True)


class ClawExportJobSerializer(serializers.ModelSerializer):
    job_id = serializers.UUIDField(source='id', read_only=True)

    class Meta:
        model = ClawExportJob
        fields = [
            'job_id', 'claw_id', 'owner_id', 'requested_by', 'requested_by_actor',
            'include_session', 'include_credits', 'status', 'snapshot_name',
            'share_url', 'expires_at', 'byte_count', 'sha256', 'created_at',
            'updated_at', 'completed_at', 'cancelled_at',
        ]
        read_only_fields = fields



class MarketplaceTemplateSerializer(serializers.ModelSerializer):
    base_template_ref = serializers.UUIDField(source='base_template_id', read_only=True)

    class Meta:
        from .models import MarketplaceTemplate
        model = MarketplaceTemplate
        fields = ['id', 'slug', 'version', 'artifact_type', 'base_template_ref', 'category',
                  'status', 'visibility', 'required_inputs', 'expected_first_artifact',
                  'risk_flags', 'compatibility']
        read_only_fields = fields


class OrgTemplateSummarySerializer(serializers.ModelSerializer):
    """Customer-safe preview card for the HIVE-368 template catalog."""

    team_count = serializers.SerializerMethodField()
    total_agent_count = serializers.SerializerMethodField()
    starter_task_count = serializers.SerializerMethodField()
    project_count = serializers.SerializerMethodField()
    workflow_count = serializers.SerializerMethodField()
    teams_preview = serializers.SerializerMethodField()
    starter_tasks_preview = serializers.SerializerMethodField()
    what_you_get = serializers.SerializerMethodField()

    class Meta:
        model = OrgTemplate
        fields = [
            'slug', 'version', 'name', 'description', 'category', 'icon',
            'recommended_tier', 'team_count', 'total_agent_count',
            'starter_task_count', 'project_count', 'workflow_count',
            'teams_preview', 'starter_tasks_preview', 'what_you_get',
        ]
        read_only_fields = fields

    def get_team_count(self, obj):
        return len(obj.teams or [])

    def get_total_agent_count(self, obj):
        return sum(int((team or {}).get('agent_count') or 0) for team in (obj.teams or []))

    def get_starter_task_count(self, obj):
        return len(obj.starter_tasks or [])

    def get_project_count(self, obj):
        return len(obj.projects or [])

    def get_workflow_count(self, obj):
        return len(obj.workflows or [])

    def get_teams_preview(self, obj):
        return [
            {
                'slug': team.get('slug'),
                'name': team.get('name') or str(team.get('slug') or '').replace('-', ' ').title(),
                'agent_count': int(team.get('agent_count') or 0),
                'capabilities': team.get('capabilities') or team.get('capability') or [],
                'tier': team.get('tier') or 'core',
                'roster_shape': team.get('roster_shape') or 'member',
            }
            for team in (obj.teams or [])
            if isinstance(team, dict)
        ]

    def get_starter_tasks_preview(self, obj):
        return [
            {
                'title': task.get('title'),
                'assignment_group': task.get('assignment_group'),
                'workflow': task.get('workflow'),
            }
            for task in (obj.starter_tasks or [])[:3]
            if isinstance(task, dict)
        ]

    def get_what_you_get(self, obj):
        teams = self.get_team_count(obj)
        agents = self.get_total_agent_count(obj)
        tasks = self.get_starter_task_count(obj)
        projects = self.get_project_count(obj)
        return [
            f'{teams} team{"s" if teams != 1 else ""} ready to work',
            f'{agents} starter agent{"s" if agents != 1 else ""}',
            f'{tasks} seeded first task{"s" if tasks != 1 else ""}',
            f'{projects} launch workspace{"s" if projects != 1 else ""}',
        ]


class OrgTemplateDetailSerializer(OrgTemplateSummarySerializer):
    template_plan = serializers.SerializerMethodField()

    class Meta(OrgTemplateSummarySerializer.Meta):
        fields = OrgTemplateSummarySerializer.Meta.fields + ['template_plan']
        read_only_fields = fields

    def get_template_plan(self, obj):
        from .org_templates import TemplatePlanContext, resolve_org_template_plan

        org_name = self.context.get('org_name') or 'Your Organization'
        org_slug = self.context.get('org_slug') or 'your-organization'
        plan_slug = self.context.get('plan_slug') or obj.recommended_tier or 'pilot'
        return resolve_org_template_plan(
            obj,
            TemplatePlanContext(
                org_name=org_name,
                org_slug=org_slug,
                plan_slug=plan_slug,
                tenant_kind='customer',
            ),
        )


class TenantProvisioningRunCreateSerializer(serializers.Serializer):
    org_name = serializers.CharField(max_length=160)
    org_slug = serializers.RegexField(
        regex=r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$',
        max_length=80,
        required=False,
        allow_blank=True,
        error_messages={'invalid': 'Use lowercase letters, numbers, and hyphens.'},
    )
    template_slug = serializers.RegexField(regex=r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$', max_length=80)
    plan_slug = serializers.RegexField(
        regex=r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$',
        max_length=80,
        required=False,
        allow_blank=True,
        error_messages={'invalid': 'Use lowercase letters, numbers, and hyphens.'},
    )
    dry_run = serializers.BooleanField(required=False, default=True)
    idempotency_key = serializers.CharField(max_length=128, required=False, allow_blank=True)
    advance = serializers.BooleanField(required=False, default=True)

    def validate(self, attrs):
        from django.utils.text import slugify

        org_name = (attrs.get('org_name') or '').strip()
        attrs['org_name'] = org_name
        org_slug = (attrs.get('org_slug') or '').strip().lower()
        if not org_slug:
            org_slug = slugify(org_name)[:80].strip('-') or 'my-organization'
        attrs['org_slug'] = org_slug
        attrs['template_slug'] = (attrs.get('template_slug') or '').strip().lower()
        attrs['plan_slug'] = (attrs.get('plan_slug') or '').strip().lower()
        return attrs


class TenantProvisioningRunSerializer(serializers.ModelSerializer):
    steps = serializers.SerializerMethodField()
    progress = serializers.SerializerMethodField()
    dashboard_path = serializers.SerializerMethodField()
    first_task = serializers.SerializerMethodField()
    onboarding = serializers.SerializerMethodField()
    is_preview = serializers.BooleanField(source='dry_run', read_only=True)

    class Meta:
        model = TenantProvisioningRun
        fields = [
            'id', 'org_name', 'org_slug', 'template_slug', 'plan_slug', 'dry_run',
            'is_preview', 'status', 'current_step', 'status_message', 'steps',
            'progress', 'resource_refs', 'audit_events', 'dashboard_path',
            'first_task', 'onboarding', 'created_at', 'updated_at', 'completed_at',
        ]
        read_only_fields = fields

    def get_steps(self, obj):
        from .tenant_provisioning import PROVISIONING_STEPS

        state = obj.step_state or {}
        return [
            {
                'slug': step.slug,
                'title': (state.get(step.slug) or {}).get('title') or step.title,
                'status': (state.get(step.slug) or {}).get('status') or TenantProvisioningRun.StepStatus.PENDING,
                'message': (state.get(step.slug) or {}).get('message') or '',
                'side_effectful': bool((state.get(step.slug) or {}).get('side_effectful', step.side_effectful)),
                'updated_at': (state.get(step.slug) or {}).get('updated_at'),
            }
            for step in PROVISIONING_STEPS
        ]

    def get_progress(self, obj):
        steps = self.get_steps(obj)
        total = len(steps) or 1
        done = sum(1 for step in steps if step['status'] == TenantProvisioningRun.StepStatus.DONE)
        needs_review = any(step['status'] == TenantProvisioningRun.StepStatus.MANUAL_REVIEW for step in steps)
        failed = any(step['status'] == TenantProvisioningRun.StepStatus.FAILED for step in steps)
        return {
            'done': done,
            'total': total,
            'percent': round(done * 100 / total),
            'needs_review': needs_review,
            'failed': failed,
        }

    def get_dashboard_path(self, obj):
        if obj.status == TenantProvisioningRun.Status.READY:
            return f'/overview?org={quote(obj.org_slug, safe="")}'
        return None

    def get_first_task(self, obj):
        plan = (obj.resource_refs or {}).get('template_plan') or {}
        tasks = plan.get('starter_tasks') or []
        if tasks:
            return tasks[0]
        return None

    def get_onboarding(self, obj):
        """HIVE-372 first-run contract for the post-provision dashboard.

        This is deliberately derived from the resolved template plan so the UI can
        guide a fresh org without hardcoding Tech Company-only copy.  The links
        are stable same-domain surface entry points; each surface can render its
        own empty state from the query context while v1 still lives in Hive.
        """
        plan = (obj.resource_refs or {}).get('template_plan') or {}
        template = plan.get('template') or {}
        starter_tasks = plan.get('starter_tasks') or []
        teams = plan.get('teams') or []
        planned_agent_count = sum(int(team.get('agent_count') or 0) for team in teams if isinstance(team, dict))
        org_query = quote(obj.org_slug, safe='')
        first_task = starter_tasks[0] if starter_tasks else None
        first_task_title = quote((first_task or {}).get('title') or '', safe='')
        return {
            'version': 'hive-372-v1',
            'template': {
                'slug': template.get('slug') or obj.template_slug,
                'name': template.get('name') or obj.template_slug.replace('-', ' ').title(),
            },
            'summary': {
                'team_count': len(teams),
                'agent_count': planned_agent_count,
                'starter_task_count': len(starter_tasks),
                'org_slug': obj.org_slug,
            },
            'first_task_flow': {
                'title': (first_task or {}).get('title') or 'Create your first custom task',
                'assignment_group': (first_task or {}).get('assignment_group') or 'product',
                'workflow': (first_task or {}).get('workflow') or 'autonomous-dev',
                'href': f'/pulse/tasks?org={org_query}&onboarding=first-task&title={first_task_title}',
                'steps': [
                    'Review the seeded starter task from your template.',
                    'Assign it to the recommended team so an agent can pick it up.',
                    'Watch Pulse move the work through its workflow automatically.',
                ],
            },
            'surfaces': [
                {
                    'id': 'dashboard',
                    'label': 'Dashboard',
                    'href': f'/hive/overview?org={org_query}',
                    'headline': f'Your {template.get("name") or obj.template_slug.replace("-", " ").title()} workspace is ready',
                    'body': 'Start here: see the setup checklist and jump to the first task flow.',
                    'cta': 'Open dashboard',
                },
                {
                    'id': 'hive',
                    'label': 'Agents',
                    'href': f'/hive/agents?org={org_query}&onboarding=agents',
                    'headline': 'Your agent fleet inherits team capabilities',
                    'body': 'Each template team provisions agents with the skills and MCP access allowed for that capability set.',
                    'cta': 'View agents',
                },
                {
                    'id': 'pulse',
                    'label': 'Work',
                    'href': f'/pulse/tasks?org={org_query}&onboarding=first-task',
                    'headline': 'Pulse already has starter work',
                    'body': 'Create, assign, and follow the first real task so the org becomes useful immediately.',
                    'cta': 'Give first task',
                },
                {
                    'id': 'admin',
                    'label': 'Admin',
                    'href': f'/admin/?org={org_query}&onboarding=org-structure',
                    'headline': 'Customize teams, members, and policy later',
                    'body': 'Start simple: the template creates structure first, then Admin reveals deeper controls as you grow.',
                    'cta': 'Review org',
                },
            ],
        }


class TokenEntrySerializer(serializers.ModelSerializer):
    fingerprint_prefix = serializers.SerializerMethodField()
    codex_account_email = serializers.SerializerMethodField()
    codex_account_status = serializers.SerializerMethodField()
    codex_access_expires_at = serializers.SerializerMethodField()
    codex_last_refresh_at = serializers.SerializerMethodField()
    codex_reauth_url = serializers.SerializerMethodField()

    class Meta:
        model = TokenEntry
        fields = [
            'id', 'label', 'account_email', 'agent_leasing_enabled', 'active',
            'cooldown_until', 'last_rate_limit_at',
            'last_used_at', 'last_status', 'usage_count', 'tier', 'priority',
            'fingerprint_prefix', 'token_hint', 'codex_account_email', 'codex_account_status',
            'codex_access_expires_at', 'codex_last_refresh_at', 'codex_reauth_url',
            'quota_snapshot', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'cooldown_until', 'last_rate_limit_at', 'last_used_at',
            'last_status', 'usage_count', 'fingerprint_prefix', 'token_hint',
            'codex_account_email', 'codex_account_status', 'codex_access_expires_at',
            'codex_last_refresh_at', 'codex_reauth_url', 'quota_snapshot',
            'created_at', 'updated_at',
        ]

    def get_fingerprint_prefix(self, obj):
        return (obj.token_fingerprint or '')[:12]

    def _codex_info(self, obj):
        if obj.pool.provider != TokenPool.Provider.OPENAI:
            return {}
        cache_attr = '_codex_observability_cache'
        if hasattr(obj, cache_attr):
            return getattr(obj, cache_attr)
        try:
            from .codex_refresh_metrics import entry_observability
            info = entry_observability(obj)
        except Exception:
            info = {}
        setattr(obj, cache_attr, info)
        return info

    def get_codex_account_email(self, obj):
        return self._codex_info(obj).get('email') or ''

    def get_codex_account_status(self, obj):
        return self._codex_info(obj).get('status') or ''

    def get_codex_access_expires_at(self, obj):
        return self._codex_info(obj).get('access_expires_at')

    def get_codex_last_refresh_at(self, obj):
        return self._codex_info(obj).get('last_refresh_at')

    def get_codex_reauth_url(self, obj):
        return self._codex_info(obj).get('reauth_path') or ''


class TokenPoolSerializer(serializers.ModelSerializer):
    entries = TokenEntrySerializer(many=True, read_only=True)
    entry_count = serializers.IntegerField(read_only=True)
    active_entry_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = TokenPool
        fields = [
            'id', 'owner_kind', 'owner_id', 'provider', 'display_name',
            'org_fallback_allowed', 'entry_count', 'active_entry_count',
            'entries', 'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'owner_kind', 'owner_id', 'entry_count', 'active_entry_count',
            'entries', 'created_at', 'updated_at',
        ]


class TokenPoolCreateSerializer(serializers.Serializer):
    provider = serializers.ChoiceField(choices=TokenPool.Provider.choices)
    display_name = serializers.CharField(max_length=160, required=False, allow_blank=True)
    scope = serializers.ChoiceField(choices=['user', 'org'], required=False, default='user')
    org_fallback_allowed = serializers.BooleanField(required=False, default=False)


class TokenPoolUpdateSerializer(serializers.Serializer):
    display_name = serializers.CharField(max_length=160, required=False, allow_blank=True)
    org_fallback_allowed = serializers.BooleanField(required=False)


class TokenEntryCreateSerializer(serializers.Serializer):
    label = serializers.CharField(max_length=120)
    token = serializers.CharField(write_only=True, trim_whitespace=False, allow_blank=False)
    account_email = serializers.CharField(max_length=254, required=False, allow_blank=True, default='')
    tier = serializers.CharField(max_length=50, required=False, allow_blank=True, default='')
    priority = serializers.IntegerField(required=False, default=0)


class TokenEntryUpdateSerializer(serializers.Serializer):
    label = serializers.CharField(max_length=120, required=False)
    account_email = serializers.CharField(max_length=254, required=False, allow_blank=True)
    agent_leasing_enabled = serializers.BooleanField(required=False)
    active = serializers.BooleanField(required=False)
    tier = serializers.CharField(max_length=50, required=False, allow_blank=True)
    priority = serializers.IntegerField(required=False)


def _mask_whatsapp_jid(value):
    raw = str(value or '')
    local = raw.split('@', 1)[0]
    digits = ''.join(ch for ch in local if ch.isdigit())
    if len(digits) <= 4:
        return '••••' if digits else ''
    if len(digits) <= 8:
        return f'••••{digits[-4:]}'
    return f'+{digits[:2]}••••••{digits[-4:]}'


class WhatsAppLinkSerializer(serializers.ModelSerializer):
    masked_number = serializers.SerializerMethodField()
    display_number = serializers.SerializerMethodField()
    status = serializers.SerializerMethodField()
    verified_at = serializers.DateTimeField(source='linked_at', allow_null=True, read_only=True)
    last_seen = serializers.SerializerMethodField()

    class Meta:
        model = WhatsAppLink
        fields = [
            'code', 'masked_number', 'display_number', 'status',
            'verified_at', 'last_seen', 'created_at',
        ]
        read_only_fields = fields

    def get_masked_number(self, obj):
        return _mask_whatsapp_jid(obj.wa_phone_jid)

    def get_display_number(self, obj):
        return self.get_masked_number(obj)

    def get_status(self, obj):
        mapping = {
            WhatsAppLink.Status.LINKED: 'active',
            WhatsAppLink.Status.PAUSED: 'paused',
            WhatsAppLink.Status.RATE_LIMITED: 'rate_limited',
            WhatsAppLink.Status.EXPIRED: 'unverified_expired',
            WhatsAppLink.Status.PENDING: 'unverified_pending',
        }
        return mapping.get(obj.status, obj.status)

    def get_last_seen(self, obj):
        return None
