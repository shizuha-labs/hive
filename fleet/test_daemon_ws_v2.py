"""PLAT-4159 protocol-v2 negotiation, epoch and commit-before-ACK tests."""

import asyncio
from datetime import timedelta
import hashlib
import uuid

from channels.testing import WebsocketCommunicator
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from .consumers import DaemonLinkConsumer
from .models import DaemonCredential, DaemonRegistry
from .runtime_projection_v2 import canonical_digest


TEST_CHANNEL_LAYER = {
    "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
}
TEST_TOKEN = "runtime-v2-secret"
TEST_DAEMON = "runtime-v2-daemon"


@override_settings(
    CHANNEL_LAYERS=TEST_CHANNEL_LAYER,
    HIVE_RUNTIME_PROJECTION_V2_DARK=True,
    HIVE_RUNTIME_PROJECTION_V2_ENFORCE=True,
)
class DaemonProtocolV2Tests(TransactionTestCase):
    def setUp(self):
        DaemonCredential.objects.create(
            daemon_id=TEST_DAEMON,
            token_hash=hashlib.sha256(TEST_TOKEN.encode()).hexdigest(),
        )
        self.epoch = uuid.uuid4()

    async def connect_v2(self):
        comm = WebsocketCommunicator(DaemonLinkConsumer.as_asgi(), "/v1/fleet/daemon-link")
        connected, _ = await comm.connect()
        self.assertTrue(connected)
        await comm.send_json_to({
            "type": "register",
            "daemon_id": TEST_DAEMON,
            "fleet_id": "runtime-v2-fleet",
            "runtime": "k3s",
            "token": TEST_TOKEN,
            "protocol_versions": [1, 2],
            "runtime_epoch": str(self.epoch),
            "protocol_capabilities": {"streaming_list": True},
        })
        ack = await comm.receive_json_from()
        self.assertEqual(ack["type"], "register_ack")
        self.assertEqual(ack["selected_protocol_version"], 2)
        return comm

    def barrier(self, seq, **payload_changes):
        entries = []
        fences = []
        for index, collection_key in enumerate(("deployments", "pods"), start=1):
            fences.append({
                "schema_version": 1,
                "collection_key": collection_key,
                "mode": "list_watch",
                "list_resource_version": f"rv/{collection_key}/not-a-number",
                "barrier_resource_version": f"rv/{collection_key}/opaque",
                "watch_generation": index,
                "barrier_end_frame_seq": seq,
            })
        data = {
            "snapshot_id": str(uuid.uuid4()),
            "R_collection_fences": fences,
            "L_snapshot_seq": 0,
            "L_barrier_seq": 0,
            "barrier_end_frame_seq": seq,
            "rl_count": 0,
            "rl_digest": canonical_digest(entries),
            "rl_entries": entries,
        }
        data.update(payload_changes)
        return {
            "type": "runtime_barrier_complete_v2",
            "frame_type": "runtime_barrier_complete_v2",
            "protocol_version": 2,
            "daemon_id": TEST_DAEMON,
            "runtime_epoch": str(self.epoch),
            "frame_seq": seq,
            "sent_at": timezone.now().isoformat(),
            "payload_digest": canonical_digest(data),
            "owner_generation": 0,
            "executor_generation": 0,
            "candidate_deadline": (timezone.now() + timedelta(minutes=5)).isoformat(),
            "data": data,
        }

    async def registry(self):
        return await asyncio.get_running_loop().run_in_executor(
            None, lambda: DaemonRegistry.objects.get(daemon_id=TEST_DAEMON),
        )

    async def test_barrier_commits_before_ack_and_duplicate_is_idempotent(self):
        comm = await self.connect_v2()
        frame = self.barrier(1)
        await comm.send_json_to(frame)
        ack = await comm.receive_json_from()
        self.assertEqual(ack["type"], "ack")
        self.assertEqual(ack["frame_seq"], 1)
        registry = await self.registry()
        self.assertEqual(registry.last_committed_frame_seq, 1)

        await comm.send_json_to(frame)
        duplicate_ack = await comm.receive_json_from()
        self.assertEqual(duplicate_ack["frame_seq"], 1)
        registry = await self.registry()
        self.assertEqual(registry.last_committed_frame_seq, 1)
        await comm.disconnect()

    async def test_gap_nacks_exact_expected_sequence_without_advancing(self):
        comm = await self.connect_v2()
        await comm.send_json_to(self.barrier(2))
        nack = await comm.receive_json_from()
        self.assertEqual(nack["type"], "nack")
        self.assertEqual(nack["expected_frame_seq"], 1)
        registry = await self.registry()
        self.assertEqual(registry.last_committed_frame_seq, 0)
        await comm.disconnect()

    async def test_legacy_final_digest_is_deterministically_rejected_and_cursor_advances(self):
        comm = await self.connect_v2()
        await comm.send_json_to(self.barrier(1, final_digest="forbidden"))
        ack = await comm.receive_json_from()
        self.assertEqual(ack["type"], "ack")
        self.assertEqual(ack["rejected"], "LEGACY_FINAL_DIGEST_REJECTED")
        registry = await self.registry()
        self.assertEqual(registry.last_committed_frame_seq, 1)
        await comm.disconnect()

    async def test_corrupt_payload_digest_closes_without_advancing_cursor(self):
        comm = await self.connect_v2()
        frame = self.barrier(1)
        frame["payload_digest"] = "0" * 64
        await comm.send_json_to(frame)
        error = await comm.receive_json_from()
        self.assertEqual(error["type"], "close_reason")
        self.assertEqual(error["code"], "payload_digest_mismatch")
        registry = await self.registry()
        self.assertEqual(registry.last_committed_frame_seq, 0)

    async def test_semantic_payload_rejection_advances_exact_cursor(self):
        comm = await self.connect_v2()
        frame = self.barrier(1, rl_count="not-an-int")
        await comm.send_json_to(frame)
        ack = await comm.receive_json_from()
        self.assertEqual(ack["type"], "ack")
        self.assertEqual(ack["rejected"], "RL_COUNT_INVALID")
        registry = await self.registry()
        self.assertEqual(registry.last_committed_frame_seq, 1)
        await comm.disconnect()


@override_settings(
    CHANNEL_LAYERS=TEST_CHANNEL_LAYER,
    HIVE_RUNTIME_PROJECTION_V2_DARK=True,
    HIVE_RUNTIME_PROJECTION_V2_ENFORCE=False,
)
class DaemonProtocolV2DarkModeTests(TransactionTestCase):
    def setUp(self):
        DaemonCredential.objects.create(
            daemon_id=TEST_DAEMON,
            token_hash=hashlib.sha256(TEST_TOKEN.encode()).hexdigest(),
        )

    async def test_v2_capable_daemon_negotiates_dark_protocol_without_enforcement(self):
        comm = WebsocketCommunicator(DaemonLinkConsumer.as_asgi(), "/v1/fleet/daemon-link")
        connected, _ = await comm.connect()
        self.assertTrue(connected)
        await comm.send_json_to({
            "type": "register",
            "daemon_id": TEST_DAEMON,
            "fleet_id": "runtime-v2-fleet",
            "runtime": "k3s",
            "token": TEST_TOKEN,
            "protocol_versions": [1, 2],
            "runtime_epoch": str(uuid.uuid4()),
        })
        ack = await comm.receive_json_from()
        self.assertEqual(ack["supported_protocol_versions"], [1, 2])
        self.assertEqual(ack["selected_protocol_version"], 2)
        registry = await asyncio.get_running_loop().run_in_executor(
            None, lambda: DaemonRegistry.objects.get(daemon_id=TEST_DAEMON),
        )
        self.assertEqual(registry.selected_protocol_version, 2)
        self.assertIsNotNone(registry.active_runtime_epoch)
        await comm.disconnect()
