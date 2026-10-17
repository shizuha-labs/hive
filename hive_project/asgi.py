"""
ASGI entry-point for Shizuha Hive (HIVE-235).

HTTP requests are handled by the standard Django WSGI-wrapped ASGI app.
WebSocket connections to /v1/fleet/daemon-link are routed to the
DaemonLinkConsumer (fleet/consumers.py).
"""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'hive_project.settings')

# Django app must be initialised before importing consumers.
django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from fleet.routing import websocket_urlpatterns  # noqa: E402
from hive_project.listener_boundary import ConnectDeliveryListenerBoundary  # noqa: E402

protocol_application = ProtocolTypeRouter({
    "http": django_asgi_app,
    # WS connections are unauthenticated at the HTTP layer; the consumer
    # enforces its own token-in-register-frame auth.
    "websocket": URLRouter(websocket_urlpatterns),
})

# HIVE-725: make the accepted local socket port part of the delivery trust
# boundary.  See listener_boundary.py and k8s/hive-connect-delivery-boundary.yaml.
application = ConnectDeliveryListenerBoundary(protocol_application)
