import unittest

from party.core.party_state import PartyState
from party.protocol.message_types import SkinSelection


class PartyStateTests(unittest.TestCase):
    def test_skin_cache_is_keyed_by_summoner(self):
        state = PartyState()
        state.add_peer(1, "One", True, "connected")
        state.add_peer(2, "Two", True, "connected")

        state.update_peer_skin(
            1,
            SkinSelection(1, "One", 99, 99001),
        )
        state.update_peer_skin(
            2,
            SkinSelection(2, "Two", 99, 99002),
        )

        self.assertEqual({1, 2}, set(state.party_skins))

    def test_clear_all_skins_keeps_members(self):
        state = PartyState()
        state.add_peer(1, "One", True, "connected")
        state.update_peer_skin(
            1,
            SkinSelection(1, "One", 99, 99001),
        )

        state.clear_all_skins()

        self.assertEqual([1], state.get_peer_ids())
        self.assertIsNone(state.peers[1].skin_selection)
        self.assertEqual({}, state.party_skins)


if __name__ == "__main__":
    unittest.main()
