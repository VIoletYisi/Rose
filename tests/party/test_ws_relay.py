import asyncio
import unittest
from types import SimpleNamespace

from party.network.ws_relay import PartyRelay


class PartyRelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_for_member_requires_real_member_update(self):
        relay = PartyRelay("room-key")
        relay._ws = SimpleNamespace(closed=False)
        relay._connected = True

        waiter = asyncio.create_task(relay.wait_for_member(42, timeout=0.5))
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())

        relay._replace_members([
            {"summoner_id": 42, "summoner_name": "Host"},
        ])

        self.assertTrue(await waiter)

    async def test_wait_for_missing_member_times_out(self):
        relay = PartyRelay("room-key")
        relay._ws = SimpleNamespace(closed=False)
        relay._connected = True

        self.assertFalse(await relay.wait_for_member(99, timeout=0.01))

    async def test_unexpected_disconnect_clears_members_and_notifies_once(self):
        relay = PartyRelay("room-key")
        relay._ws = SimpleNamespace(closed=False)
        relay._connected = True
        relay._replace_members([
            {"summoner_id": 42, "summoner_name": "Host"},
        ])

        reasons = []
        relay.set_on_disconnected(reasons.append)
        relay._mark_disconnected("network lost")
        relay._mark_disconnected("duplicate")

        self.assertEqual([], relay.get_members_snapshot())
        self.assertEqual(["network lost"], reasons)


if __name__ == "__main__":
    unittest.main()
