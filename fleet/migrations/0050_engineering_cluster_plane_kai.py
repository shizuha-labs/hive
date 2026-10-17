"""PLAT-5591: restore Kai cluster-plane kubeconfig via dedicated capability + seat override.

Root cause (PLAT-5584): Kai is a historical cluster-plane seat (PLAT-3412 list:
ichi, san, kai, ni) but his signed effective capabilities are `engineering`
without `kubeconfig`, so the daemon renderer (`isPrivilegedK8sAgent`) selects the
ordinary variant (serviceAccountName=default, no kubeconfig init/mount/env). The
Deployment annotation `inline-failover-v6-privileged-kubeconfig-v1` is a
non-authorizing lie (ADR-0016/HIVE-272: annotations do not authorize).

This migration adds an explicit authorizing seat override (HIVE-272 §4.2) via a
DEDICATED cluster-plane capability that confers ONLY `kubeconfig` (no
host_exec/credential_broker — plane separation per PLAT-5584 contract: do NOT
grant host SSH as a substitute). The renderer's `isPrivilegedK8sAgent` gate
accepts `credentialScopes.has('kubeconfig')`, so this triggers the privileged
variant (kubeconfig-materialize init + KUBECONFIG env + agent-kai-ops SA).

RBAC scoping (bounded can-i, NOT cluster-admin) is enforced at the
agent-kai-ops ServiceAccount / RoleBinding layer, analogous to Ni's bounded
matrix — not in this migration.

Contract authority: PLAT-5584 architecture triage (kumo) A.2/A.3; ADR-0016
`f1b4c974`; HIVE-272 `83b6280c`; PLAT-3412 / runbook `86fbc086` v6.
"""
import hashlib
import json

from django.db import migrations


def _definition_hash(payload):
    encoded = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(encoded).hexdigest()


def _capability_payload():
    return {
        "slug": "engineering-cluster-plane",
        "display_name": "Engineering Cluster-Plane",
        "description": (
            "Dedicated cluster-plane capability for engineering seats that hold "
            "the historical k3s cluster-plane contract (PLAT-3412 list: kai). "
            "Confers ONLY the kubeconfig sensitive flag + credential scope — NO "
            "host_exec/credential_broker (plane separation, PLAT-5584). RBAC is "
            "scoped at the agent-<user>-ops SA/RoleBinding layer (bounded can-i, "
            "not cluster-admin)."
        ),
        "lifecycle_status": "draft",
        "activation_status": "active",
        "risk_tier": "sensitive",
        "skill_refs": [],
        "eager_skill_refs": [],
        "inline_profile_refs": [],
        "mcp_server_refs": [],
        "runtime_flags": {},
        "sensitive_access_flags": ["kubeconfig"],
        "default_credential_grant_scopes": ["kubeconfig"],
        "conflicts_with": [],
        "requires": [],
        "non_removable": False,
    }


def forwards(apps, schema_editor):
    CapabilityDefinition = apps.get_model('fleet', 'CapabilityDefinition')
    AgentCapabilityOverride = apps.get_model('fleet', 'AgentCapabilityOverride')
    payload = _capability_payload()
    cap, _ = CapabilityDefinition.objects.update_or_create(
        slug=payload["slug"],
        defaults={
            **payload,
            "definition_hash": _definition_hash(payload),
            "security_review_required": True,
            "security_reviewed_by": "san@shizuha.com",
            "security_reviewed_at": "2026-08-20T00:00:00Z",
            "approved_by": "san@shizuha.com",
            "approved_at": "2026-08-20T00:00:00Z",
            "created_by": "san@shizuha.com",
            "updated_by": "san@shizuha.com",
        },
    )
    # Explicit authorizing seat override for Kai (HIVE-272 §4.2 — never
    # annotation-only, never team-slug inference).
    AgentCapabilityOverride.objects.update_or_create(
        agent_identifier="kai@shizuha.com",
        capability=cap,
        defaults={
            "action": "add",
            "reason": (
                "PLAT-5591/PLAT-5584: restore Kai cluster-plane kubeconfig. Kai is a "
                "historical cluster-plane seat (PLAT-3412 list: ichi, san, kai, ni) but "
                "engineering team profile confers no kubeconfig. Dedicated "
                "engineering-cluster-plane capability (kubeconfig only, no host_exec) "
                "added via explicit authorizing seat override per HIVE-272 §4.2. "
                "RBAC scoped at agent-kai-ops SA (bounded can-i, not cluster-admin)."
            ),
            "expires_at": None,
            "approved_by": "san@shizuha.com",
            "approved_at": "2026-08-20T00:00:00Z",
            "created_by": "san@shizuha.com",
            "updated_by": "san@shizuha.com",
        },
    )


def backwards(apps, schema_editor):
    CapabilityDefinition = apps.get_model('fleet', 'CapabilityDefinition')
    AgentCapabilityOverride = apps.get_model('fleet', 'AgentCapabilityOverride')
    AgentCapabilityOverride.objects.filter(
        agent_identifier="kai@shizuha.com",
        capability__slug="engineering-cluster-plane",
    ).delete()
    CapabilityDefinition.objects.filter(slug="engineering-cluster-plane").delete()


class Migration(migrations.Migration):
    dependencies = [
        # PLAT-5591: depend on the current fleet leaf (main advanced past 0049 via
        # 0050_hiveorgproviderreference → 0051 → 0052 → 0032_plat4159_runtime_projection_v2).
        # Depending on 0049 created a conflicting leaf node and broke the CI
        # migration graph check (CommandError: multiple leaf nodes).
        ('fleet', '0032_plat4159_runtime_projection_v2'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]

# PLAT-5591: re-trigger CI after main merge (b1ac7b7a). No functional change.
