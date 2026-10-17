"""WebSocket URL routing for fleet daemon-link (HIVE-235)."""
from django.urls import re_path
from . import consumers

websocket_urlpatterns = [
    re_path(r'^v1/fleet/daemon-link$', consumers.DaemonLinkConsumer.as_asgi()),
]
