#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Skin Collector
Collects and manages skin selections from party members
"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from state import SharedState
from utils.core.logging import get_logger

from ..protocol.message_types import SkinSelection
from ..network.peer_connection import PeerConnection

log = get_logger()


@dataclass
class PartySkinData:
    """Aggregated skin data from party members"""
    summoner_id: int
    summoner_name: str
    champion_id: int
    skin_id: int
    chroma_id: Optional[int] = None
    custom_mod_path: Optional[str] = None
    is_local: bool = False  # True if this is our own selection


class SkinCollector:
    """Collects skin selections from party members for injection"""

    def __init__(self, state: SharedState):
        """Initialize skin collector

        Args:
            state: Shared application state
        """
        self.state = state

        # Cached skin selections by summoner ID
        self._selections: Dict[int, SkinSelection] = {}

    def update_from_peer(self, selection: SkinSelection):
        """Update skin selection from peer

        Args:
            selection: Peer's skin selection
        """
        self._selections[selection.summoner_id] = selection
        log.debug(
            f"[SKIN_COLLECT] Updated selection from {selection.summoner_name}: "
            f"champion {selection.champion_id} -> skin {selection.skin_id}"
        )

    def clear_peer(self, summoner_id: int):
        """Clear skin selection for a peer

        Args:
            summoner_id: Peer's summoner ID to clear
        """
        if summoner_id in self._selections:
            del self._selections[summoner_id]
            log.debug(f"[SKIN_COLLECT] Cleared selection for summoner {summoner_id}")

    def clear_all(self):
        """Clear all peer skin selections"""
        self._selections.clear()
        log.debug("[SKIN_COLLECT] Cleared all peer selections")

    def get_my_selection(
        self, summoner_id: int, summoner_name: str
    ) -> Optional[SkinSelection]:
        """Return the skin that Rose will actually inject for this game.

        Args:
            summoner_id: Our summoner ID
            summoner_name: Our summoner name

        Returns:
            Our skin selection or None
        """
        champion_id = self._positive_int(
            getattr(self.state, "locked_champ_id", None)
            or getattr(self.state, "hovered_champ_id", None)
        )
        if champion_id is None:
            return None

        # Custom mods take precedence over the normal, random and historic
        # selection paths. Only advertise a mod selected for this champion.
        custom_mod_path = None
        selected_custom_mod = getattr(self.state, "selected_custom_mod", None)
        if isinstance(selected_custom_mod, dict):
            custom_champion = self._positive_int(
                selected_custom_mod.get("champion_id")
            )
            if custom_champion == champion_id:
                custom_mod_path = selected_custom_mod.get("relative_path")
                skin_id = self._positive_int(
                    selected_custom_mod.get("skin_id")
                    or selected_custom_mod.get("storage_skin_id")
                )
                if custom_mod_path and skin_id is not None:
                    return SkinSelection(
                        summoner_id=summoner_id,
                        summoner_name=summoner_name,
                        champion_id=champion_id,
                        skin_id=skin_id,
                        chroma_id=self._positive_int(
                            getattr(self.state, "selected_chroma_id", None)
                        ),
                        custom_mod_path=str(custom_mod_path),
                    )

        # Historic and Random modes override the skin detected from the
        # client's carousel. Their IDs were previously never sent to Party.
        skin_id = None
        if getattr(self.state, "historic_mode_active", False):
            historic_value = getattr(self.state, "historic_skin_id", None)
            skin_id = self._positive_int(historic_value)
            if skin_id is None and isinstance(historic_value, str):
                custom_mod_path = self._historic_custom_mod_path(historic_value)
                skin_id = self._skin_id_from_mod_path(custom_mod_path)
        elif getattr(self.state, "random_mode_active", False):
            skin_id = self._positive_int(
                getattr(self.state, "random_skin_id", None)
            )

        if skin_id is None:
            skin_id = self._positive_int(
                getattr(self.state, "last_hovered_skin_id", None)
                or getattr(self.state, "selected_skin_id", None)
            )

        if skin_id is None:
            return None

        chroma_id = self._positive_int(
            getattr(self.state, "selected_chroma_id", None)
        )

        return SkinSelection(
            summoner_id=summoner_id,
            summoner_name=summoner_name,
            champion_id=champion_id,
            skin_id=skin_id,
            chroma_id=chroma_id,
            custom_mod_path=custom_mod_path,
        )

    @staticmethod
    def _positive_int(value) -> Optional[int]:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None

    @staticmethod
    def _historic_custom_mod_path(value: str) -> Optional[str]:
        """Decode the path marker used by Rose's historic custom-mod mode."""
        try:
            from utils.core.historic import get_custom_mod_path, is_custom_mod_path

            if is_custom_mod_path(value):
                return get_custom_mod_path(value)
        except Exception:
            return None
        return None

    @classmethod
    def _skin_id_from_mod_path(cls, value: Optional[str]) -> Optional[int]:
        if not value:
            return None
        parts = str(value).replace("\\", "/").split("/")
        try:
            skins_index = parts.index("skins")
        except ValueError:
            return None
        if skins_index + 1 >= len(parts):
            return None
        return cls._positive_int(parts[skins_index + 1])

    def collect_all_skins(
        self,
        peers: List[PeerConnection],
        my_summoner_id: int,
        my_summoner_name: str,
        team_champions: Dict[int, int],
    ) -> List[PartySkinData]:
        """Collect all skin selections for injection

        Args:
            peers: List of connected peers in lobby
            my_summoner_id: Our summoner ID
            my_summoner_name: Our summoner name
            team_champions: Mapping of summoner_id -> champion_id

        Returns:
            List of PartySkinData for all party members
        """
        skins = []

        # Add our own selection first
        my_selection = self.get_my_selection(my_summoner_id, my_summoner_name)
        if my_selection:
            skins.append(
                PartySkinData(
                    summoner_id=my_summoner_id,
                    summoner_name=my_summoner_name,
                    champion_id=my_selection.champion_id,
                    skin_id=my_selection.skin_id,
                    chroma_id=my_selection.chroma_id,
                    custom_mod_path=my_selection.custom_mod_path,
                    is_local=True,
                )
            )

        # Add peer selections (require connected; in_lobby may be cleared at injection time when phase changes)
        for peer in peers:
            if not peer.is_connected:
                continue

            selection = peer.skin_selection
            if not selection:
                # Use cached selection
                selection = self._selections.get(peer.summoner_id)

            if selection:
                # Verify champion matches team champion
                expected_champion = team_champions.get(selection.summoner_id)
                if expected_champion and expected_champion != selection.champion_id:
                    log.warning(
                        f"[SKIN_COLLECT] Champion mismatch for {selection.summoner_name}: "
                        f"expected {expected_champion}, got {selection.champion_id}"
                    )
                    continue

                skins.append(
                    PartySkinData(
                        summoner_id=selection.summoner_id,
                        summoner_name=selection.summoner_name,
                        champion_id=selection.champion_id,
                        skin_id=selection.skin_id,
                        chroma_id=selection.chroma_id,
                        custom_mod_path=selection.custom_mod_path,
                        is_local=False,
                    )
                )

        log.info(
            f"[SKIN_COLLECT] Collected {len(skins)} skin selections "
            f"({sum(1 for s in skins if s.is_local)} local, "
            f"{sum(1 for s in skins if not s.is_local)} from peers)"
        )

        return skins

    def collect_relay_skins(
        self,
        members: list,
        my_summoner_id: int,
        team_champions: Dict[int, int],
    ) -> List[PartySkinData]:
        """Collect skins from relay room members for injection.

        Args:
            members: List of member dicts from the relay (each has summoner_id, skin, etc.)
            my_summoner_id: Our summoner ID (to exclude ourselves)
            team_champions: Mapping of summoner_id -> champion_id

        Returns:
            List of PartySkinData for party members
        """
        skins = []
        seen_summoner_ids = set()
        my_id = self._positive_int(my_summoner_id)

        for member in members:
            if not isinstance(member, dict):
                continue
            sid = self._positive_int(member.get("summoner_id"))
            if sid is None or sid == my_id or sid in seen_summoner_ids:
                continue
            seen_summoner_ids.add(sid)

            skin = member.get("skin")
            if not isinstance(skin, dict):
                continue

            champion_id = self._positive_int(skin.get("champion_id"))
            skin_id = self._positive_int(skin.get("skin_id"))
            if champion_id is None or skin_id is None:
                continue

            # Relay membership alone is not enough: a room can contain people
            # who are not in this match. Require an exact current-team match.
            expected = team_champions.get(sid)
            if expected is None:
                log.debug(
                    f"[SKIN_COLLECT] Ignoring relay member {sid}: not on current team"
                )
                continue
            if int(expected) != champion_id:
                log.warning(
                    f"[SKIN_COLLECT] Champion mismatch for {sid}: "
                    f"expected {expected}, got {champion_id}"
                )
                continue
            if not self._skin_matches_champion(skin_id, champion_id):
                log.warning(
                    f"[SKIN_COLLECT] Skin {skin_id} does not belong to "
                    f"champion {champion_id}; ignoring relay member {sid}"
                )
                continue

            # For custom mods, try to find a local match by content hash
            custom_mod_path = None
            if skin.get("is_custom") and skin.get("custom_mod_hash"):
                from ..core.party_manager import PartyManager
                local_path = PartyManager.find_local_mod_by_hash(
                    skin["custom_mod_hash"], champion_id
                )
                if local_path:
                    custom_mod_path = local_path
                    log.info(f"[SKIN_COLLECT] Matched custom mod for peer {sid}: {local_path}")
                else:
                    log.info(f"[SKIN_COLLECT] No local match for peer {sid}'s custom mod, skipping")
                    continue

            skins.append(PartySkinData(
                summoner_id=sid,
                summoner_name=member.get("summoner_name", "Unknown"),
                champion_id=champion_id,
                skin_id=skin_id,
                chroma_id=self._positive_int(skin.get("chroma_id")),
                custom_mod_path=custom_mod_path,
                is_local=False,
            ))

        log.info(f"[SKIN_COLLECT] Collected {len(skins)} relay skin selections")
        return skins

    @staticmethod
    def _skin_matches_champion(skin_id: int, champion_id: int) -> bool:
        """Validate regular skins and Rose's special form/chroma identifiers."""
        if skin_id // 1000 == champion_id:
            return True
        try:
            from utils.core.utilities import get_base_skin_id_for_chroma

            base_skin_id = get_base_skin_id_for_chroma(skin_id, None)
            return bool(
                base_skin_id
                and int(base_skin_id) // 1000 == champion_id
            )
        except Exception:
            return False

    def get_peer_selections(self) -> Dict[int, SkinSelection]:
        """Get all cached peer selections

        Returns:
            Dict mapping summoner_id to SkinSelection
        """
        return dict(self._selections)
