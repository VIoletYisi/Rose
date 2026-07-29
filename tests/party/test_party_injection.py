import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from injection.core.injector import SkinInjector
from injection.core.manager import InjectionManager
from party.discovery.skin_collector import PartySkinData
from party.integration.injection_hook import PartyInjectionHook


class FakePartyState:
    def get_connected_peers(self):
        return [object()]

    def get_lobby_peers(self):
        return [object()]


class FakePartyManager:
    enabled = True
    party_state = FakePartyState()

    def __init__(self, skins):
        self.skins = skins

    def get_party_skins(self):
        return list(self.skins)


class FakeInjector:
    def __init__(self, root: Path):
        self.mods_dir = root / "injection-mods"
        self.mods_dir.mkdir()
        self.zip_path = root / "resolved.zip"
        self.zip_path.write_bytes(b"zip")
        self.resolve_calls = []

    def _resolve_zip(self, name, **kwargs):
        self.resolve_calls.append((name, kwargs))
        return self.zip_path

    def _extract_zip_to_mod(self, path):
        return SimpleNamespace(name=path.stem)


class PartyInjectionHookTests(unittest.TestCase):
    def _hook(self, skins):
        return PartyInjectionHook(
            FakePartyManager(skins),
            SimpleNamespace(),
        )

    def test_chroma_uses_chroma_identifier_for_resolution(self):
        skin = PartySkinData(
            summoner_id=200,
            summoner_name="Friend",
            champion_id=99,
            skin_id=99007,
            chroma_id=99991,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            injector = FakeInjector(Path(temp_dir))

            result = self._hook([skin]).prepare_party_mods(injector)

        self.assertEqual(["resolved"], result)
        self.assertEqual("chroma_99991", injector.resolve_calls[0][0])
        self.assertEqual(99991, injector.resolve_calls[0][1]["chroma_id"])
        self.assertEqual(99, injector.resolve_calls[0][1]["champion_id"])

    def test_default_teammate_skin_does_not_look_for_an_archive(self):
        skin = PartySkinData(
            summoner_id=200,
            summoner_name="Friend",
            champion_id=99,
            skin_id=99000,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            injector = FakeInjector(Path(temp_dir))

            result = self._hook([skin]).prepare_party_mods(injector)

        self.assertEqual([], result)
        self.assertEqual([], injector.resolve_calls)

    def test_duplicate_party_selections_are_staged_once(self):
        first = PartySkinData(200, "One", 99, 99001)
        second = PartySkinData(201, "Two", 99, 99001)
        with tempfile.TemporaryDirectory() as temp_dir:
            injector = FakeInjector(Path(temp_dir))

            result = self._hook([first, second]).prepare_party_mods(injector)

        self.assertEqual(["resolved"], result)
        self.assertEqual(1, len(injector.resolve_calls))

    def test_extracted_custom_mod_directory_is_staged(self):
        skin = PartySkinData(
            summoner_id=200,
            summoner_name="Friend",
            champion_id=99,
            skin_id=99001,
            custom_mod_path="skins/99000/custom-mod",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mod_path = root / "mods" / skin.custom_mod_path
            mod_path.mkdir(parents=True)
            (mod_path / "content.wad.client").write_bytes(b"same")
            injector = FakeInjector(root)

            def fake_stage(_source, destination, cache_dir):
                del cache_dir
                destination.mkdir(parents=True)

            with (
                patch(
                    "party.integration.injection_hook.get_user_data_dir",
                    return_value=root,
                ),
                patch(
                    "party.integration.injection_hook.get_injection_dir",
                    return_value=root / "injection",
                ),
                patch(
                    "party.integration.injection_hook.link_or_extract",
                    side_effect=fake_stage,
                ),
            ):
                result = self._hook([skin]).prepare_party_mods(injector)

        self.assertEqual(["party-200-custom-mod"], result)


class ModsOnlyInjectorTests(unittest.TestCase):
    def test_mods_only_cleans_deduplicates_and_runs_overlay(self):
        injector = SkinInjector.__new__(SkinInjector)
        injector._clean_mods_dir = Mock()
        injector._clean_overlay_dir = Mock()
        injector._mk_run_overlay = Mock(return_value=0)

        result = injector.inject_mods_only(
            extra_mods_callback=lambda _injector: [
                "friend-skin",
                "friend-skin",
                "other-skin",
            ],
        )

        self.assertTrue(result)
        injector._clean_mods_dir.assert_called_once_with()
        injector._clean_overlay_dir.assert_called_once_with()
        self.assertEqual(
            ["friend-skin", "other-skin"],
            injector._mk_run_overlay.call_args.args[0],
        )

    def test_base_skin_routes_to_party_only_injection(self):
        manager = InjectionManager.__new__(InjectionManager)
        manager.inject_party_mods_immediately = Mock(return_value=True)

        result = manager.inject_skin_immediately(
            "skin_99000",
            champion_id=99,
        )

        self.assertTrue(result)
        manager.inject_party_mods_immediately.assert_called_once_with(
            stop_callback=None
        )


if __name__ == "__main__":
    unittest.main()
