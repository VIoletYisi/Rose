import unittest
from types import SimpleNamespace
from unittest.mock import patch

from party.discovery.skin_collector import SkinCollector


class SkinCollectorTests(unittest.TestCase):
    def _state(self, **overrides):
        values = {
            "locked_champ_id": 99,
            "hovered_champ_id": None,
            "last_hovered_skin_id": 99001,
            "selected_skin_id": None,
            "selected_chroma_id": None,
            "selected_custom_mod": None,
            "historic_mode_active": False,
            "historic_skin_id": None,
            "random_mode_active": False,
            "random_skin_id": None,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_historic_and_random_modes_override_hovered_skin(self):
        collector = SkinCollector(self._state(
            historic_mode_active=True,
            historic_skin_id=99007,
            random_mode_active=True,
            random_skin_id=99008,
        ))

        selection = collector.get_my_selection(100, "Local")

        self.assertEqual(99007, selection.skin_id)

        collector.state.historic_mode_active = False
        selection = collector.get_my_selection(100, "Local")
        self.assertEqual(99008, selection.skin_id)

    def test_custom_mod_for_another_champion_is_not_advertised(self):
        collector = SkinCollector(self._state(
            selected_custom_mod={
                "champion_id": 103,
                "skin_id": 103001,
                "relative_path": "skins/103001/example.fantome",
            },
        ))

        selection = collector.get_my_selection(100, "Local")

        self.assertEqual(99001, selection.skin_id)
        self.assertIsNone(selection.custom_mod_path)

    def test_relay_skins_require_exact_current_team_match(self):
        collector = SkinCollector(self._state())
        members = [
            {
                "summoner_id": 200,
                "summoner_name": "Teammate",
                "skin": {"champion_id": 99, "skin_id": 99001},
            },
            {
                "summoner_id": 300,
                "summoner_name": "Other room member",
                "skin": {"champion_id": 103, "skin_id": 103001},
            },
        ]

        skins = collector.collect_relay_skins(
            members,
            my_summoner_id=100,
            team_champions={100: 1, 200: 99},
        )

        self.assertEqual([200], [skin.summoner_id for skin in skins])

    def test_relay_skins_reject_mismatch_and_duplicates(self):
        collector = SkinCollector(self._state())
        members = [
            {
                "summoner_id": "200",
                "skin": {"champion_id": 103, "skin_id": 103001},
            },
            {
                "summoner_id": 200,
                "skin": {"champion_id": 99, "skin_id": 99001},
            },
            {
                "summoner_id": 201,
                "skin": {"champion_id": 99, "skin_id": 103001},
            },
        ]

        skins = collector.collect_relay_skins(
            members,
            my_summoner_id=100,
            team_champions={200: 99, 201: 99},
        )

        # The first entry for 200 is authoritative and mismatched; a duplicate
        # cannot replace it. 201's skin ID belongs to another champion.
        self.assertEqual([], skins)

    def test_custom_relay_skin_requires_a_local_hash_match(self):
        collector = SkinCollector(self._state())
        members = [{
            "summoner_id": 200,
            "skin": {
                "champion_id": 99,
                "skin_id": 99001,
                "is_custom": True,
                "custom_mod_hash": "abc123",
            },
        }]

        with patch(
            "party.core.party_manager.PartyManager.find_local_mod_by_hash",
            return_value=None,
        ):
            skins = collector.collect_relay_skins(
                members,
                my_summoner_id=100,
                team_champions={200: 99},
            )

        self.assertEqual([], skins)


if __name__ == "__main__":
    unittest.main()
