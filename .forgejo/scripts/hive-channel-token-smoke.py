"""Bounded R0 functional smoke for Hive's channel-token contract.

Run only inside the isolated staging Deployment via ``manage.py shell``.  The
fixture is deliberately unique to PLAT-4086 and is removed even on assertion
failure, so the smoke never requires or mutates production identity data.
"""

from types import SimpleNamespace

from rest_framework.test import APIRequestFactory, force_authenticate

from claws.models import Claw
from claws.views import ClawChannelTokenView


OWNER_ID = 4_086_001
fixture, _ = Claw.objects.update_or_create(
    owner_id=OWNER_ID,
    tier_id="staging-smoke",
    agent_type=Claw.AgentType.CLAW,
    model="channel-token-smoke",
    defaults={
        "owner_username": "plat4086-smoke",
        "owner_email": "plat4086-smoke@invalid.example",
        "name": "PLAT-4086 channel token smoke",
        "status": Claw.Status.RUNNING,
        "platform_url": "https://staging.invalid.example",
    },
)

try:
    request = APIRequestFactory().post("/hive/api/v1/claws/mine/channel-token")
    user = SimpleNamespace(
        id=OWNER_ID,
        pk=OWNER_ID,
        email="plat4086-smoke@invalid.example",
        is_authenticated=True,
    )
    force_authenticate(request, user=user)
    response = ClawChannelTokenView.as_view()(request)
    assert response.status_code == 200, response.data
    assert response.data.get("token"), response.data
    assert response.data.get("expires_at"), response.data
    print("R0 channel-token smoke PASS")
finally:
    fixture.delete()
