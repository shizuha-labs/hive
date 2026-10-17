from django.contrib import admin

from .models import WaitlistEntry, WaitlistEvent


@admin.register(WaitlistEntry)
class WaitlistEntryAdmin(admin.ModelAdmin):
    list_display = ('email', 'preferred_channel', 'top_use_case', 'owner_username', 'created_at')
    list_filter = ('preferred_channel', 'created_at')
    search_fields = ('email', 'owner_username', 'top_use_case')
    readonly_fields = ('id', 'created_at', 'updated_at')
    ordering = ('-created_at',)


@admin.register(WaitlistEvent)
class WaitlistEventAdmin(admin.ModelAdmin):
    list_display = ('event_type', 'channel', 'session_key', 'entry', 'created_at')
    list_filter = ('event_type', 'channel', 'created_at')
    search_fields = ('session_key',)
    readonly_fields = ('id', 'event_type', 'session_key', 'channel', 'entry', 'metadata', 'created_at')
    ordering = ('-created_at',)
