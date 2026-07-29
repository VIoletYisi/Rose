import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from party.core.party_manager import PartyManager
from party.network.ws_relay import compute_room_key
from party.protocol.token_codec import create_token


class FakeLCU:
    def __init__(self):
        self.current_summoner = {
            "summonerId": 100,
            "displayName": "Local",
        }
        self.session = {"myTeam": []}

    def get(self, _path):
        return {}


class FakeRelay:
    room_members = {}
    connect_results = {}
    created = []

    def __init__(self, room_key):
        self.room_key = room_key
        self.connected = False
        self.members = [
            dict(member)
            for member in self.room_members.get(room_key, [])
        ]
        self.on_members = None
        self.on_disconnected = None
        self.sent_skins = []
        self.created.append(self)

    @classmethod
    def reset(cls):
        cls.room_members = {}
        cls.connect_results = {}
        cls.created = []

    def set_on_members_changed(self, callback):
        self.on_members = callback

    def set_on_disconnected(self, callback):
        self.on_disconnected = callback

    async def connect(self):
        self.connected = self.connect_results.get(self.room_key, True)
        return self.connected

    async def join(self, summoner_id, summoner_name):
        if not self.connected:
            return False
        if not any(
            int(member.get("summoner_id", 0)) == int(summoner_id)
            for member in self.members
        ):
            self.members.append({
                "summoner_id": summoner_id,
                "summoner_name": summoner_name,
            })
        if self.on_members:
            self.on_members(self.get_members_snapshot())
        return True

    async def wait_for_member(self, summoner_id, timeout=10):
        del timeout
        return any(
            int(member.get("summoner_id", 0)) == int(summoner_id)
            for member in self.members
        )

    async def disconnect(self):
        self.connected = False
        self.members = []

    async def send_skin(self, skin):
        if not self.connected:
            return False
        self.sent_skins.append(skin)
        return True

    def get_members_snapshot(self):
        return [dict(member) for member in self.members]


class PartyManagerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeRelay.reset()
        self.lcu = FakeLCU()
        self.shared_state = SimpleNamespace(
            locked_champ_id=None,
            hovered_champ_id=None,
            last_hovered_skin_id=None,
            selected_chroma_id=None,
            selected_custom_mod=None,
            phase="Lobby",
        )

    async def test_enable_fails_when_relay_is_unavailable(self):
        FakeRelay.connect_results = {}

        def always_fail(room_key):
            relay = FakeRelay(room_key)
            relay.connect_results[room_key] = False
            return relay

        manager = PartyManager(self.lcu, self.shared_state)
        with patch("party.core.party_manager.PartyRelay", side_effect=always_fail):
            with self.assertRaisesRegex(RuntimeError, "Failed to connect"):
                await manager.enable()

        self.assertFalse(manager.enabled)
        self.assertEqual("error", manager.party_state.relay_status)

    async def test_offline_host_does_not_replace_current_room(self):
        manager = PartyManager(self.lcu, self.shared_state)
        with patch("party.core.party_manager.PartyRelay", FakeRelay):
            await manager.enable()
            original_relay = manager._relay

            host_token = create_token(200)
            success, error = await manager.add_peer(host_token.encode())

            self.assertFalse(success)
            self.assertEqual("Party host is not online", error)
            self.assertIs(original_relay, manager._relay)
            await manager.disable()

    async def test_joiner_switches_to_confirmed_host_room(self):
        manager = PartyManager(self.lcu, self.shared_state)
        host_token = create_token(200)
        target_room = compute_room_key(
            host_token.summoner_id,
            host_token.encryption_key,
        )
        FakeRelay.room_members[target_room] = [{
            "summoner_id": 200,
            "summoner_name": "Host",
        }]

        with patch("party.core.party_manager.PartyRelay", FakeRelay):
            await manager.enable()
            success, error = await manager.add_peer(host_token.encode())

            self.assertTrue(success)
            self.assertIsNone(error)
            self.assertEqual(target_room, manager._relay.room_key)
            self.assertEqual("guest", manager.party_state.room_role)
            self.assertEqual(host_token.encode(), manager.party_state.my_token)
            self.assertEqual([200], manager.party_state.get_peer_ids())
            await manager.disable()

    async def test_unexpected_disconnect_reconnects_to_same_room(self):
        manager = PartyManager(self.lcu, self.shared_state)

        with (
            patch("party.core.party_manager.PartyRelay", FakeRelay),
            patch(
                "party.core.party_manager.RECONNECT_DELAYS",
                (0.01,),
            ),
        ):
            await manager.enable()
            original_room = manager._active_room_key
            original_relay = manager._relay
            original_relay.connected = False
            manager._on_relay_disconnected(original_relay, "lost")

            await asyncio.sleep(0.05)

            self.assertEqual("online", manager.party_state.relay_status)
            self.assertEqual(original_room, manager._relay.room_key)
            self.assertIsNot(original_relay, manager._relay)
            await manager.disable()


if __name__ == "__main__":
    unittest.main()
