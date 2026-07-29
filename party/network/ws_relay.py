#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WebSocket Relay Client for Party Mode
Connects to a shared room where party members broadcast skin selections.
"""

import asyncio
import copy
import hashlib
import json
import os
import threading
from typing import Callable, List, Optional

import websockets
from websockets.exceptions import ConnectionClosed

from utils.core.logging import get_logger

log = get_logger()

try:
    from .relay_config import RELAY_URL as _CONFIGURED_URL
except ImportError:
    _CONFIGURED_URL = ""

RELAY_URL = os.environ.get("ROSE_RELAY_URL", _CONFIGURED_URL)
PING_INTERVAL = 25.0
DEFAULT_MEMBER_WAIT_TIMEOUT = 10.0


def compute_room_key(host_summoner_id: int, host_key: bytes) -> str:
    """Derive a room key from the host's token."""
    raw = str(host_summoner_id).encode() + host_key
    return hashlib.sha256(raw).hexdigest()[:32]


class PartyRelay:
    """WebSocket connection to a shared party room.

    Members join, announce themselves, and broadcast skin selections.
    The Worker broadcasts the full member list on every change.
    """

    def __init__(self, room_key: str):
        self.room_key = room_key
        self._ws = None
        self._connected = False
        self._intentional_disconnect = False
        self._disconnect_notified = False
        self._recv_task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._members_changed = asyncio.Event()
        self._members_lock = threading.RLock()

        # Current state: list of members with their skin picks
        self.members: List[dict] = []

        # Callbacks
        self._on_members_changed: Optional[Callable[[List[dict]], None]] = None
        self._on_disconnected: Optional[Callable[[Optional[str]], None]] = None

    @property
    def connected(self) -> bool:
        if not self._connected or self._ws is None:
            return False
        return not bool(getattr(self._ws, "closed", False))

    def set_on_members_changed(self, callback: Callable[[List[dict]], None]):
        """Called whenever the member list changes (join/leave/skin update)."""
        self._on_members_changed = callback

    def set_on_disconnected(self, callback: Callable[[Optional[str]], None]):
        """Called after an unexpected relay disconnect."""
        self._on_disconnected = callback

    def get_members_snapshot(self) -> List[dict]:
        """Return a thread-safe copy of the current relay members."""
        with self._members_lock:
            return copy.deepcopy(self.members)

    def has_member(self, summoner_id: int) -> bool:
        """Return whether a summoner is currently announced in this room."""
        try:
            target_id = int(summoner_id)
        except (TypeError, ValueError):
            return False

        return any(
            self._coerce_summoner_id(member.get("summoner_id")) == target_id
            for member in self.get_members_snapshot()
        )

    async def wait_for_member(
        self,
        summoner_id: int,
        timeout: float = DEFAULT_MEMBER_WAIT_TIMEOUT,
    ) -> bool:
        """Wait until a specific summoner is present in the relay member list."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.1, float(timeout))

        while self.connected:
            if self.has_member(summoner_id):
                return True

            remaining = deadline - loop.time()
            if remaining <= 0:
                return False

            # Clear, re-check, then wait. The second check avoids losing an
            # update that arrives between the first check and clear().
            self._members_changed.clear()
            if self.has_member(summoner_id):
                return True

            try:
                await asyncio.wait_for(
                    self._members_changed.wait(),
                    timeout=remaining,
                )
            except asyncio.TimeoutError:
                return False

        return False

    async def connect(self, timeout: float = 15.0) -> bool:
        """Connect to the relay room."""
        if not RELAY_URL:
            log.warning("[RELAY] No relay URL configured")
            return False

        url = f"{RELAY_URL}/room?key={self.room_key}"
        log.info(f"[RELAY] Connecting to room {self.room_key[:8]}...")

        self._intentional_disconnect = False
        self._disconnect_notified = False

        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(
                    url,
                    max_size=65536,
                    ping_interval=20,
                    ping_timeout=20,
                ),
                timeout=timeout,
            )
            self._connected = True
            self._recv_task = asyncio.create_task(self._receive_loop())
            self._ping_task = asyncio.create_task(self._keepalive_loop())
            log.info("[RELAY] Connected")
            return True
        except Exception as e:
            log.warning(f"[RELAY] Connection failed: {e}")
            self._connected = False
            if self._ws:
                try:
                    await self._ws.close()
                except Exception:
                    pass
            self._ws = None
            return False

    async def join(self, summoner_id: int, summoner_name: str) -> bool:
        """Announce ourselves to the room."""
        return await self._send_json({
            "type": "join",
            "summoner_id": summoner_id,
            "summoner_name": summoner_name,
        })

    async def send_skin(self, skin: Optional[dict]) -> bool:
        """Broadcast our skin selection to the room."""
        return await self._send_json({
            "type": "skin",
            "skin": skin,
        })

    async def disconnect(self):
        """Leave the room."""
        self._intentional_disconnect = True
        self._connected = False

        current_task = asyncio.current_task()
        for task in [self._ping_task, self._recv_task]:
            if task and task is not current_task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass

        self._ping_task = None
        self._recv_task = None

        if self._ws:
            try:
                await self._ws.send(json.dumps({"type": "leave"}))
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        self._replace_members([])
        log.info("[RELAY] Disconnected")

    async def _send_json(self, data: dict) -> bool:
        if not self.connected:
            return False

        try:
            await self._ws.send(json.dumps(data))
            return True
        except ConnectionClosed as exc:
            self._mark_disconnected(str(exc))
        except Exception as exc:
            self._mark_disconnected(str(exc))
        return False

    async def _receive_loop(self):
        try:
            async for message in self._ws:
                if isinstance(message, str):
                    if message == "pong":
                        continue
                    try:
                        msg = json.loads(message)
                    except json.JSONDecodeError:
                        continue

                    if msg.get("type") == "members":
                        members = msg.get("members", [])
                        if not isinstance(members, list):
                            members = []
                        self._replace_members(members)
                        log.info(
                            f"[RELAY] Members updated: "
                            f"{len(self.get_members_snapshot())} in room"
                        )
        except ConnectionClosed as exc:
            log.info("[RELAY] Connection closed")
            self._mark_disconnected(str(exc))
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.warning(f"[RELAY] Receive error: {e}")
            self._mark_disconnected(str(e))
        finally:
            if not self._intentional_disconnect and self._connected:
                self._mark_disconnected("Relay receive loop ended")

    async def _keepalive_loop(self):
        try:
            while self._connected:
                await asyncio.sleep(PING_INTERVAL)
                if self._ws and self._connected:
                    try:
                        await self._ws.send("ping")
                    except Exception as exc:
                        self._mark_disconnected(str(exc))
                        break
        except asyncio.CancelledError:
            return

    def _replace_members(self, members: List[dict]):
        normalized = [member for member in members if isinstance(member, dict)]
        with self._members_lock:
            changed = normalized != self.members
            self.members = copy.deepcopy(normalized)

        self._members_changed.set()
        if changed and self._on_members_changed:
            try:
                self._on_members_changed(self.get_members_snapshot())
            except Exception as exc:
                log.debug(f"[RELAY] Callback error: {exc}")

    def _mark_disconnected(self, reason: Optional[str] = None):
        was_connected = self._connected
        self._connected = False
        self._members_changed.set()
        self._replace_members([])

        if (
            was_connected
            and not self._intentional_disconnect
            and not self._disconnect_notified
        ):
            self._disconnect_notified = True
            if self._on_disconnected:
                try:
                    self._on_disconnected(reason)
                except Exception as exc:
                    log.debug(f"[RELAY] Disconnect callback error: {exc}")

    @staticmethod
    def _coerce_summoner_id(value) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0
