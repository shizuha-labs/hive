from django.contrib import admin
from .models import Claw, ClawEvent
from hive_platform.models import BillingPlan, PricingRate, TenantSubscription


class ClawEventInline(admin.TabularInline):
    model = ClawEvent
    readonly_fields = ['event_type', 'message', 'created_at']
    extra = 0
    ordering = ['-created_at']


@admin.register(Claw)
class ClawAdmin(admin.ModelAdmin):
    list_display = ['name', 'owner_username', 'owner_id', 'status', 'tier_id', 'namespace', 'created_at']
    list_filter = ['status', 'tier_id']
    search_fields = ['name', 'owner_username', 'owner_email', 'namespace']
    readonly_fields = ['id', 'created_at', 'updated_at', 'provisioned_at', 'last_active_at']
    inlines = [ClawEventInline]


class PricingRateInline(admin.TabularInline):
    model = PricingRate
    extra = 0
    fields = ['metric', 'unit', 'model', 'currency', 'unit_price', 'included_quantity', 'overage_policy', 'active']


@admin.register(BillingPlan)
class BillingPlanAdmin(admin.ModelAdmin):
    list_display = ['plan_id', 'name', 'currency', 'monthly_base_price', 'active', 'updated_at']
    list_filter = ['active', 'currency']
    search_fields = ['plan_id', 'name']
    readonly_fields = ['created_at', 'updated_at']
    inlines = [PricingRateInline]


@admin.register(TenantSubscription)
class TenantSubscriptionAdmin(admin.ModelAdmin):
    list_display = ['tenant_org_slug', 'tenant_org_id', 'plan', 'status', 'manual_collection', 'updated_at']
    list_filter = ['status', 'manual_collection', 'plan']
    search_fields = ['tenant_org_id', 'tenant_org_slug', 'customer_name', 'billing_email']
    readonly_fields = ['created_at', 'updated_at']
