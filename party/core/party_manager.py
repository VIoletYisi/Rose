#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Party Manager
Orchestrator for party mode skin sharing via WebSocket relay.
"""

import asyncio
import copy
import hashlib
import secrets
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from lcu import LCU
from state import SharedState
from utils.core.logging import get_logger

from ..network.ws_relay import PartyRelay, compute_room_key
from ..protocol.token_codec import PartyToken, create_token
from ..protocol.message_types import SkinSelection
from ..discovery.lobby_matcher import LobbyMatcher
from ..discovery.skin_collector import SkinCollector, PartySkinData
from .party_state import PartyState

log = get_logger()

LOBBY_CHECK_INTERVAL = 2.0
SKIN_BROADCAST_INTERVAL = 1.0
MEMBER_CONFIRM_TIMEOUT = 10.0
RECONNECT_DELAYS = (1.0, 2.0, 4.0, 8.0, 15.0)
TOKEN_REFRESH_CHECK_INTERVAL = 300.0
TOKEN_REFRESH_THRESHOLD = 600
_UNSET = object()


class PartyManager:
    """Main orchestrator for party mode."""

    def __init__(self, lcu: LCU, state: SharedState, injection_manager=None):
        self.lcu = lcu
        self.state = state
        self.injection_manager = injection_manager

        self.party_state = PartyState()

        # Networking
        self._my_key: Optional[bytes] = None
        self._my_token: Optional[PartyToken] = None
        self._relay: Optional[PartyRelay] = None
        self._active_room_key: Optional[str] = None
        self._active_invite_token: Optional[str] = None
        self._host_summoner_id: Optional[int] = None
        self._room_role = "none"
        self._relay_lock = asyncio.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._last_broadcast_payload = _UNSET
        self._team_champions: Dict[int, int] = {}

        # Discovery
        self._lobby_matcher: Optional[LobbyMatcher] = None
        self._skin_collector: Optional[SkinCollector] = None

        # Background tasks
        self._running = False
        self._lobby_check_task: Optional[asyncio.Task] = None
        self._skin_broadcast_task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._token_refresh_task: Optional[asyncio.Task] = None

        # Callbacks for UI updates
        self._on_state_change: Optional[Callable[[PartyState], None]] = None
        self._on_peer_update: Optional[Callable[[int, dict], None]] = None

    @property
    def enabled(self) -> bool:
        return self.party_state.enabled

    @property
    def my_token_str(self) -> Optional[str]:
        return self.party_state.my_token

    def set_callbacks(
        self,
        on_state_change: Optional[Callable[[PartyState], None]] = None,
        on_peer_update: Optional[Callable[[int, dict], None]] = None,
    ):
        self._on_state_change = on_state_change
        self._on_peer_update = on_peer_update

    async def enable(self) -> str:
        """Enable party mode: generate token and connect to relay room."""
        if self.party_state.enabled:
            return self.party_state.my_token or ""

        log.info("[PARTY] Enabling party mode...")
        relay: Optional[PartyRelay] = None

        try:
            self._loop = asyncio.get_running_loop()
            self._lobby_matcher = LobbyMatcher(self.lcu, self.state)
            self._skin_collector = SkinCollector(self.state)

            my_summoner_id = self._lobby_matcher.get_my_summoner_id()
            my_summoner_name = self._lobby_matcher.get_my_summoner_name()

            if not my_summoner_id:
                raise RuntimeError("Failed to get summoner ID - is League client running?")

            self.party_state.my_summoner_id = my_summoner_id
            self.party_state.my_summoner_name = my_summoner_name

            # Generate key and token
            self._my_key = secrets.token_bytes(32)
            self._my_token = create_token(
                summoner_id=my_summoner_id,
                encryption_key=self._my_key,
            )

            token_str = self._my_token.encode()

            room_key = compute_room_key(my_summoner_id, self._my_key)
            self.party_state.set_connection_status("connecting")
            self._notify_state_change()

            relay = self._create_relay(room_key)
            if not await relay.connect():
                raise RuntimeError("Failed to connect to relay")
            if not await relay.join(my_summoner_id, my_summoner_name):
                raise RuntimeError("Failed to announce this account to relay")
            if not await relay.wait_for_member(
                my_summoner_id,
                timeout=MEMBER_CONFIRM_TIMEOUT,
            ):
                raise RuntimeError("Relay did not confirm this account")

            self._relay = relay
            self._active_room_key = room_key
            self._active_invite_token = token_str
            self._host_summoner_id = my_summoner_id
            self._room_role = "host"

            self.party_state.my_token = token_str
            self.party_state.enabled = True
            self.party_state.room_role = self._room_role
            self.party_state.host_summoner_id = self._host_summoner_id
            self.party_state.set_connection_status("online")
            self._sync_relay_members(relay, relay.get_members_snapshot())
            log.info(f"[PARTY] Connected to relay room {room_key[:8]}...")

            # Start background tasks
            self._running = True
            self._lobby_check_task = asyncio.create_task(self._lobby_check_loop())
            self._skin_broadcast_task = asyncio.create_task(self._skin_broadcast_loop())
            self._token_refresh_task = asyncio.create_task(
                self._token_refresh_loop()
            )
            await self.broadcast_skin_update(force=True)

            log.info("[PARTY] Party mode enabled")
            self._notify_state_change()
            return token_str

        except Exception as e:
            log.error(f"[PARTY] Failed to enable party mode: {e}")
            self._running = False
            if relay:
                await relay.disconnect()
            self._relay = None
            self._active_room_key = None
            self._active_invite_token = None
            self._host_summoner_id = None
            self._room_role = "none"
            self._my_key = None
            self._my_token = None
            self.party_state.clear_all()
            self.party_state.set_connection_status("error", error=str(e))
            self._notify_state_change()
            raise RuntimeError(f"Failed to enable party mode: {e}")

    async def disable(self):
        """Disable party mode."""
        log.info("[PARTY] Disabling party mode...")
        self._running = False

        current_task = asyncio.current_task()
        tasks = [
            self._lobby_check_task,
            self._skin_broadcast_task,
            self._reconnect_task,
            self._token_refresh_task,
        ]
        for task in tasks:
            if task and task is not current_task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass

        self._lobby_check_task = None
        self._skin_broadcast_task = None
        self._reconnect_task = None
        self._token_refresh_task = None

        async with self._relay_lock:
            if self._relay:
                await self._relay.disconnect()
                self._relay = None

        self.party_state.clear_all()
        self._my_key = None
        self._my_token = None
        self._active_room_key = None
        self._active_invite_token = None
        self._host_summoner_id = None
        self._room_role = "none"
        self._loop = None
        self._last_broadcast_payload = _UNSET
        self._team_champions.clear()

        log.info("[PARTY] Party mode disabled")
        self._notify_state_change()

    async def add_peer(self, token_str: str) -> Tuple[bool, Optional[str]]:
        """Join another player's party room by pasting their token."""
        if not self.party_state.enabled:
            return False, "Party mode not enabled"

        token_str = "".join(token_str.split())

        try:
            token = PartyToken.decode(token_str)
            log.info(f"[PARTY] Joining party of summoner {token.summoner_id}")

            if token.summoner_id == self.party_state.my_summoner_id:
                return False, "You cannot add yourself"

            target_room_key = compute_room_key(token.summoner_id, token.encryption_key)

            async with self._relay_lock:
                current_relay = self._relay

                if (
                    current_relay
                    and current_relay.room_key == target_room_key
                    and current_relay.connected
                ):
                    if await current_relay.wait_for_member(
                        token.summoner_id,
                        timeout=MEMBER_CONFIRM_TIMEOUT,
                    ):
                        log.info("[PARTY] Already in requested party room")
                        return True, None
                    return False, "Party host is not online"

                if current_relay and current_relay.get_members_snapshot():
                    peer_ids = {
                        self._member_summoner_id(member)
                        for member in current_relay.get_members_snapshot()
                    }
                    peer_ids.discard(self.party_state.my_summoner_id or 0)
                    if peer_ids:
                        return (
                            False,
                            "Already in a party. Leave it before joining another.",
                        )

                # Connect the candidate before dropping the old room. A bad or
                # offline token therefore cannot destroy a working session.
                candidate = self._create_relay(target_room_key)
                if not await candidate.connect():
                    return False, "Failed to connect to relay"
                if not await candidate.join(
                    self.party_state.my_summoner_id,
                    self.party_state.my_summoner_name,
                ):
                    await candidate.disconnect()
                    return False, "Failed to join relay room"
                if not await candidate.wait_for_member(
                    token.summoner_id,
                    timeout=MEMBER_CONFIRM_TIMEOUT,
                ):
                    await candidate.disconnect()
                    return False, "Party host is not online"

                old_relay = self._relay
                self._relay = candidate
                self._active_room_key = target_room_key
                self._active_invite_token = token_str
                self._host_summoner_id = token.summoner_id
                self._room_role = "guest"

                # Every member displays and shares the active room token, not
                # the token for the empty room they created during enable().
                self.party_state.my_token = token_str
                self.party_state.room_role = self._room_role
                self.party_state.host_summoner_id = self._host_summoner_id
                self.party_state.set_connection_status("online")
                self._sync_relay_members(
                    candidate,
                    candidate.get_members_snapshot(),
                )
                self._notify_state_change()

                if old_relay:
                    await old_relay.disconnect()

                await self.broadcast_skin_update(force=True)
                log.info(
                    f"[PARTY] Joined party room {target_room_key[:8]}..."
                )
                return True, None

        except ValueError as e:
            error_str = str(e)
            if "expired" in error_str.lower():
                return False, "Token has expired. Ask your friend for a new one."
            return False, f"Invalid token: {error_str}"
        except Exception as e:
            log.error(f"[PARTY] Failed to join party: {e}")
            return False, f"Unexpected error: {e}"

    async def remove_peer(self, summoner_id: int) -> bool:
        """Shared author relay has no kick operation."""
        log.info(
            f"[PARTY] Ignoring remove request for {summoner_id}: "
            "relay does not support kicking members"
        )
        return False

    async def broadcast_skin_update(self, force: bool = False) -> bool:
        """Broadcast the effective selection, including an explicit clear."""
        if not self.enabled or not self._relay or not self._relay.connected:
            return False
        if not self._skin_collector:
            return False

        selection = self._skin_collector.get_my_selection(
            self.party_state.my_summoner_id,
            self.party_state.my_summoner_name,
        )

        if not selection:
            skin_data = None
        else:
            skin_data = {
                "champion_id": selection.champion_id,
                "skin_id": selection.skin_id,
                "chroma_id": selection.chroma_id,
            }

            # For custom mods, share a content hash instead of the file path.
            if selection.custom_mod_path:
                mod_hash = self._hash_custom_mod(selection.custom_mod_path)
                if mod_hash:
                    skin_data["custom_mod_hash"] = mod_hash
                    skin_data["is_custom"] = True

        return await self._send_skin_payload(skin_data, force=force)

    async def _send_skin_payload(
        self,
        skin_data: Optional[dict],
        *,
        force: bool = False,
    ) -> bool:
        relay = self._relay
        if not self.enabled or not relay or not relay.connected:
            return False
        if not force and skin_data == self._last_broadcast_payload:
            return False
        if not await relay.send_skin(skin_data):
            return False
        self._last_broadcast_payload = copy.deepcopy(skin_data)
        return True

    def get_party_skins(self) -> List[PartySkinData]:
        """Get all skin selections for injection."""
        if not self.enabled or not self._lobby_matcher or not self._skin_collector:
            return []

        current_team = self._lobby_matcher.get_team_champion_mapping()
        if current_team:
            self._team_champions = current_team

        # Collect skins from relay members
        return self._skin_collector.collect_relay_skins(
            members=(
                self._relay.get_members_snapshot()
                if self._relay
                else []
            ),
            my_summoner_id=self.party_state.my_summoner_id,
            team_champions=dict(self._team_champions),
        )

    def on_champ_select_reset(self, generation: int) -> None:
        """Clear skin data from the previous game and notify the relay.

        ChampSelect reset can be called from either the HTTP poller thread or
        the WebSocket thread, while PartyManager's relay runs on its asyncio
        loop. Keep membership intact and schedule only the skin clear.
        """
        self.party_state.clear_all_skins()
        if self._skin_collector:
            self._skin_collector.clear_all()

        self._team_champions = {}
        if self._lobby_matcher:
            fresh_team = self._lobby_matcher.get_team_champion_mapping()
            if fresh_team:
                self._team_champions = fresh_team

        self._last_broadcast_payload = _UNSET
        self._notify_state_change()
        log.info(
            f"[PARTY] Cleared per-game skin state for ChampSelect "
            f"generation {generation}"
        )

        loop = self._loop
        if not self.enabled or not loop or not loop.is_running():
            return

        async def send_clear():
            await self._send_skin_payload(None, force=True)

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop is loop:
            loop.create_task(send_clear())
        else:
            asyncio.run_coroutine_threadsafe(send_clear(), loop)

    def get_state_dict(self) -> dict:
        return self.party_state.to_dict()

    # ─── Relay callbacks ─────────────────────────────────────────────────

    def _create_relay(self, room_key: str) -> PartyRelay:
        relay = PartyRelay(room_key)
        relay.set_on_members_changed(
            lambda members, source=relay: self._on_relay_members_changed(
                source,
                members,
            )
        )
        relay.set_on_disconnected(
            lambda reason, source=relay: self._on_relay_disconnected(
                source,
                reason,
            )
        )
        return relay

    def _on_relay_members_changed(
        self,
        relay: PartyRelay,
        members: list,
    ):
        """Called by the relay when the member list changes."""
        if relay is not self._relay:
            return
        self._sync_relay_members(relay, members)

    def _sync_relay_members(
        self,
        relay: PartyRelay,
        members: list,
    ):
        if relay is not self._relay:
            return

        my_id = self.party_state.my_summoner_id

        # Update party state with relay members (exclude ourselves)
        current_peer_ids = set()
        for member in members:
            if not isinstance(member, dict):
                continue
            sid = self._member_summoner_id(member)
            if sid == my_id or not sid:
                continue

            current_peer_ids.add(sid)
            name = str(member.get("summoner_name") or "Unknown")[:64]
            skin = member.get("skin")

            self.party_state.update_peer(
                sid,
                summoner_name=name,
                connected=True,
                connection_state="connected",
            )

            # Update skin selection
            if isinstance(skin, dict) and self._skin_collector:
                try:
                    sel = SkinSelection(
                        summoner_id=sid,
                        summoner_name=name,
                        champion_id=int(skin.get("champion_id", 0)),
                        skin_id=int(skin.get("skin_id", 0)),
                        chroma_id=skin.get("chroma_id"),
                    )
                    if sel.champion_id <= 0 or sel.skin_id <= 0:
                        raise ValueError("invalid relay skin payload")
                    self.party_state.update_peer_skin(sid, sel)
                    self._skin_collector.update_from_peer(sel)
                except Exception as e:
                    log.debug(f"[PARTY] Failed to update peer skin: {e}")
                    self.party_state.clear_peer_skin(sid)
                    self._skin_collector.clear_peer(sid)
            else:
                self.party_state.clear_peer_skin(sid)
                if self._skin_collector:
                    self._skin_collector.clear_peer(sid)

        # Remove peers that are no longer in the room
        stale = [
            sid
            for sid in self.party_state.get_peer_ids()
            if sid not in current_peer_ids
        ]
        for sid in stale:
            self.party_state.remove_peer(sid)
            if self._skin_collector:
                self._skin_collector.clear_peer(sid)
            log.info(f"[PARTY] Removed peer {sid}")

        self._notify_state_change()

    def _on_relay_disconnected(
        self,
        relay: PartyRelay,
        reason: Optional[str],
    ):
        if relay is not self._relay or not self._running or not self.enabled:
            return

        self.party_state.clear_peers()
        self.party_state.set_connection_status(
            "reconnecting",
            error=reason or "Relay disconnected",
        )
        self._notify_state_change()

        if not self._reconnect_task or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(
                self._reconnect_loop()
            )

    # ─── Background tasks ────────────────────────────────────────────────

    async def _reconnect_loop(self):
        attempt = 0
        try:
            while self._running and self.enabled:
                delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
                attempt += 1
                await asyncio.sleep(delay)

                room_key = self._active_room_key
                my_id = self.party_state.my_summoner_id
                my_name = self.party_state.my_summoner_name
                if not room_key or not my_id:
                    return

                candidate = self._create_relay(room_key)
                if not await candidate.connect():
                    continue
                if not await candidate.join(my_id, my_name):
                    await candidate.disconnect()
                    continue
                if not await candidate.wait_for_member(
                    my_id,
                    timeout=MEMBER_CONFIRM_TIMEOUT,
                ):
                    await candidate.disconnect()
                    continue

                if (
                    self._room_role == "guest"
                    and self._host_summoner_id
                    and not await candidate.wait_for_member(
                        self._host_summoner_id,
                        timeout=MEMBER_CONFIRM_TIMEOUT,
                    )
                ):
                    await candidate.disconnect()
                    continue

                async with self._relay_lock:
                    if (
                        not self._running
                        or room_key != self._active_room_key
                    ):
                        await candidate.disconnect()
                        return

                    old_relay = self._relay
                    self._relay = candidate
                    self.party_state.set_connection_status("online")
                    self._sync_relay_members(
                        candidate,
                        candidate.get_members_snapshot(),
                    )
                    if old_relay and old_relay is not candidate:
                        await old_relay.disconnect()

                await self.broadcast_skin_update(force=True)
                log.info("[PARTY] Relay connection restored")
                self._notify_state_change()
                return
        except asyncio.CancelledError:
            return
        finally:
            if asyncio.current_task() is self._reconnect_task:
                self._reconnect_task = None

    async def _lobby_check_loop(self):
        """Check lobby membership and update peer status."""
        while self._running:
            try:
                await asyncio.sleep(LOBBY_CHECK_INTERVAL)
                if not self._running or not self._lobby_matcher:
                    continue

                lobby_ids = self._lobby_matcher.get_all_summoner_ids()
                team_champions = (
                    self._lobby_matcher.get_team_champion_mapping()
                )
                if team_champions:
                    self._team_champions = team_champions
                for sid in self.party_state.get_peer_ids():
                    in_lobby = sid in lobby_ids
                    self.party_state.update_peer_lobby_status(sid, in_lobby)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.info(f"[PARTY] Lobby check error: {e}")

    async def _skin_broadcast_loop(self):
        """Broadcast skin updates when selection changes."""
        while self._running:
            try:
                await asyncio.sleep(SKIN_BROADCAST_INTERVAL)
                if not self._running:
                    continue
                await self.broadcast_skin_update()

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.info(f"[PARTY] Skin broadcast error: {e}")

    async def _token_refresh_loop(self):
        """Refresh the host invite without changing its room key."""
        try:
            while self._running:
                await asyncio.sleep(TOKEN_REFRESH_CHECK_INTERVAL)
                if (
                    self._room_role != "host"
                    or not self._my_token
                    or not self._my_key
                    or not self.party_state.my_summoner_id
                ):
                    continue
                if self._my_token.time_until_expiry() > TOKEN_REFRESH_THRESHOLD:
                    continue

                self._my_token = create_token(
                    summoner_id=self.party_state.my_summoner_id,
                    encryption_key=self._my_key,
                )
                token_str = self._my_token.encode()
                self._active_invite_token = token_str
                self.party_state.my_token = token_str
                self._notify_state_change()
                log.info("[PARTY] Refreshed party invite token")
        except asyncio.CancelledError:
            return

    @staticmethod
    def _member_summoner_id(member: dict) -> int:
        try:
            return int(member.get("summoner_id", 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _hash_custom_mod(mod_path: str) -> Optional[str]:
        """Compute a stable hash for an imported mod archive or directory."""
        from utils.core.paths import get_user_data_dir

        try:
            mods_root = (get_user_data_dir() / "mods").resolve()
            full_path = (mods_root / mod_path).resolve()
            full_path.relative_to(mods_root)
            return PartyManager._hash_mod_path(full_path)
        except Exception as e:
            log.debug(f"[PARTY] Failed to hash custom mod: {e}")
            return None

    @staticmethod
    def find_local_mod_by_hash(content_hash: str, champion_id: int) -> Optional[str]:
        """Search modern and legacy skin-mod storage for a content match.

        Returns:
            Relative path to the matching mod (from mods root), or None.
        """
        from utils.core.paths import get_user_data_dir

        try:
            content_hash = str(content_hash).strip().lower()
            if len(content_hash) != 16 or any(
                char not in "0123456789abcdef" for char in content_hash
            ):
                return None

            champion_id = int(champion_id)
            mods_root = get_user_data_dir() / "mods"
            skins_dir = mods_root / "skins"
            if not skins_dir.exists():
                return None

            # Modern storage uses skins/{champion_id * 1000}/{mod folder}.
            # Older builds may have used skins/{skin_id}/{archive}.
            for storage_dir in sorted(skins_dir.iterdir()):
                if not storage_dir.is_dir():
                    continue
                try:
                    storage_id = int(storage_dir.name)
                except ValueError:
                    continue
                if (
                    storage_id != champion_id
                    and storage_id // 1000 != champion_id
                ):
                    continue

                for candidate in sorted(storage_dir.iterdir()):
                    if candidate.name in {
                        "rose_mod_targets.json",
                        "rose_wad_targets.json",
                    }:
                        continue
                    if not (
                        candidate.is_dir()
                        or (
                            candidate.is_file()
                            and candidate.suffix.lower() in (".zip", ".fantome")
                        )
                    ):
                        continue
                    try:
                        if PartyManager._hash_mod_path(candidate) == content_hash:
                            return candidate.relative_to(mods_root).as_posix()
                    except Exception:
                        continue
        except Exception as e:
            log.debug(f"[PARTY] Error searching local mods: {e}")

        return None

    @staticmethod
    def _hash_mod_path(mod_path: Path) -> Optional[str]:
        """Hash a mod without depending on its outer directory name."""
        path = Path(mod_path)
        if path.is_file():
            digest = hashlib.sha256()
            digest.update(b"file\0")
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            return digest.hexdigest()[:16]

        if not path.is_dir():
            return None

        digest = hashlib.sha256()
        digest.update(b"directory\0")
        files = sorted(
            (
                file_path
                for file_path in path.rglob("*")
                if file_path.is_file() and not file_path.is_symlink()
            ),
            key=lambda item: item.relative_to(path).as_posix().casefold(),
        )
        if not files:
            return None
        for file_path in files:
            relative = file_path.relative_to(path).as_posix()
            digest.update(relative.casefold().encode("utf-8"))
            digest.update(b"\0")
            with file_path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            digest.update(b"\0")
        return digest.hexdigest()[:16]

    def _notify_state_change(self):
        if self._on_state_change:
            self._on_state_change(self.party_state)
