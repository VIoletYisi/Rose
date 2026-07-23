#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Game Detector
Handles detection of League of Legends game and client directories.
Supports both Riot and WeGame directory layouts.
"""

from pathlib import Path
from typing import Optional, Tuple

try:
    import psutil

    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    psutil = None

from utils.core.logging import get_logger, log_success
from ..config.config_manager import ConfigManager

log = get_logger()


class GameDetector:
    """Detect League of Legends game and client directories."""

    GAME_EXE = "League of Legends.exe"
    CLIENT_EXE = "LeagueClient.exe"

    def __init__(self, config_manager: ConfigManager):
        self.config_manager = config_manager

    @classmethod
    def _is_valid_game_dir(cls, directory: Optional[Path]) -> bool:
        """Return True when directory directly contains League of Legends.exe."""
        if directory is None:
            return False

        try:
            return directory.is_dir() and (directory / cls.GAME_EXE).is_file()
        except OSError:
            return False

    @classmethod
    def _is_valid_client_dir(cls, directory: Optional[Path]) -> bool:
        """Return True when directory directly contains LeagueClient.exe."""
        if directory is None:
            return False

        try:
            return directory.is_dir() and (directory / cls.CLIENT_EXE).is_file()
        except OSError:
            return False

    @classmethod
    def _infer_game_dir_from_client_dir(
        cls,
        client_dir: Path,
    ) -> Optional[Path]:
        """Infer the game directory from the LeagueClient directory.

        Supported examples:

        Riot layout:
            League of Legends/
            ├── LeagueClient.exe
            └── Game/
                └── League of Legends.exe

        WeGame layout:
            LOL/
            ├── LeagueClient/
            │   └── LeagueClient.exe
            └── Game/
                └── League of Legends.exe
        """
        candidates = [
            # Riot layout:
            # D:/Riot Games/League of Legends/Game
            client_dir / "Game",

            # WeGame layout:
            # D:/WeGameApps/LOL/Game
            client_dir.parent / "Game",

            # Alternative Riot installer layout:
            client_dir.parent / "League of Legends" / "Game",
        ]

        seen = set()

        for candidate in candidates:
            candidate_key = str(candidate).casefold()

            if candidate_key in seen:
                continue

            seen.add(candidate_key)

            log.debug(f"Checking for League at: {candidate / cls.GAME_EXE}")

            if cls._is_valid_game_dir(candidate):
                return candidate

        return None

    @classmethod
    def _infer_client_dir_from_game_dir(
        cls,
        game_dir: Path,
    ) -> Optional[Path]:
        """Infer the LeagueClient directory from the game directory.

        In WeGame installations the two directories are commonly siblings:

            D:/WeGameApps/LOL/Game
            D:/WeGameApps/LOL/LeagueClient
        """
        candidates = []

        if game_dir.name.casefold() == "game":
            install_root = game_dir.parent

            candidates.extend(
                [
                    # WeGame layout:
                    install_root / "LeagueClient",

                    # Riot layout:
                    install_root,
                ]
            )

        candidates.extend(
            [
                game_dir.parent / "LeagueClient",
                game_dir.parent,
            ]
        )

        seen = set()

        for candidate in candidates:
            candidate_key = str(candidate).casefold()

            if candidate_key in seen:
                continue

            seen.add(candidate_key)

            log.debug(
                f"Checking for LeagueClient at: "
                f"{candidate / cls.CLIENT_EXE}"
            )

            if cls._is_valid_client_dir(candidate):
                return candidate

        return None

    def detect_paths(self) -> Tuple[Optional[Path], Optional[Path]]:
        """Detect game and client directories.

        Returns:
            Tuple containing:
            - game directory containing League of Legends.exe
            - client directory containing LeagueClient.exe
        """
        config_league_path = self.config_manager.load_league_path()
        config_client_path = self.config_manager.load_client_path()

        configured_game_dir = (
            Path(config_league_path).expanduser()
            if config_league_path
            else None
        )
        configured_client_dir = (
            Path(config_client_path).expanduser()
            if config_client_path
            else None
        )

        # Case 1: both configured paths are already valid.
        if (
            self._is_valid_game_dir(configured_game_dir)
            and self._is_valid_client_dir(configured_client_dir)
        ):
            log_success(
                log,
                (
                    "Using paths from config: "
                    f"league={configured_game_dir}, "
                    f"client={configured_client_dir}"
                ),
                "",
            )
            return configured_game_dir, configured_client_dir

        # Case 2: game path is valid, but client path is absent or invalid.
        if self._is_valid_game_dir(configured_game_dir):
            inferred_client_dir = self._infer_client_dir_from_game_dir(
                configured_game_dir
            )

            if inferred_client_dir is not None:
                log_success(
                    log,
                    (
                        "Using configured League game path and inferred "
                        "client path: "
                        f"league={configured_game_dir}, "
                        f"client={inferred_client_dir}"
                    ),
                    "",
                )

                self.config_manager.save_paths(
                    str(configured_game_dir),
                    str(inferred_client_dir),
                )

                return configured_game_dir, inferred_client_dir

            log.warning(
                "Configured League game path is valid, but LeagueClient.exe "
                f"could not be found near: {configured_game_dir}"
            )

        # Case 3: client path is valid, but game path is absent or invalid.
        if self._is_valid_client_dir(configured_client_dir):
            inferred_game_dir = self._infer_game_dir_from_client_dir(
                configured_client_dir
            )

            if inferred_game_dir is not None:
                log_success(
                    log,
                    (
                        "Using configured League client path and inferred "
                        "game path: "
                        f"league={inferred_game_dir}, "
                        f"client={configured_client_dir}"
                    ),
                    "",
                )

                self.config_manager.save_paths(
                    str(inferred_game_dir),
                    str(configured_client_dir),
                )

                return inferred_game_dir, configured_client_dir

            log.warning(
                "Configured League client path is valid, but "
                "League of Legends.exe could not be found near: "
                f"{configured_client_dir}"
            )

        # Case 4: detect paths from the running LeagueClient.exe process.
        log.debug(
            "Config paths missing or invalid; "
            "detecting via LeagueClient.exe"
        )

        detected_game_dir, detected_client_dir = (
            self._detect_via_leagueclient()
        )

        if detected_game_dir and detected_client_dir:
            self.config_manager.save_paths(
                str(detected_game_dir),
                str(detected_client_dir),
            )

            return detected_game_dir, detected_client_dir

        log.warning(
            "Could not detect League of Legends paths. "
            "Please ensure League Client is running or manually set "
            "leaguePath and clientPath in config.ini"
        )

        return None, None

    def detect_game_dir(self) -> Optional[Path]:
        """Return the detected League game directory."""
        game_dir, _ = self.detect_paths()
        return game_dir

    def _detect_via_leagueclient(
        self,
    ) -> Tuple[Optional[Path], Optional[Path]]:
        """Detect paths using the running LeagueClient.exe process."""
        if not PSUTIL_AVAILABLE:
            log.debug(
                "psutil not available; "
                "skipping LeagueClient.exe process detection"
            )
            return None, None

        try:
            log.debug("Looking for LeagueClient.exe process...")

            found_client_process = False

            for proc in psutil.process_iter(["pid", "name", "exe"]):
                try:
                    process_name = proc.info.get("name") or ""

                    if process_name.casefold() != self.CLIENT_EXE.casefold():
                        continue

                    found_client_process = True
                    exe_path = proc.info.get("exe")

                    if not exe_path:
                        log.debug(
                            "LeagueClient.exe process found, "
                            "but executable path is unavailable"
                        )
                        continue

                    log.debug(
                        f"Found LeagueClient.exe at: {exe_path}"
                    )

                    client_dir = Path(exe_path).parent

                    if not self._is_valid_client_dir(client_dir):
                        log.debug(
                            "LeagueClient.exe does not exist in detected "
                            f"directory: {client_dir}"
                        )
                        continue

                    game_dir = self._infer_game_dir_from_client_dir(
                        client_dir
                    )

                    if game_dir is not None:
                        log_success(
                            log,
                            (
                                "Found League paths via LeagueClient.exe: "
                                f"game={game_dir}, "
                                f"client={client_dir}"
                            ),
                            "",
                        )

                        return game_dir, client_dir

                    log.debug(
                        "League of Legends.exe was not found in any "
                        f"supported location near: {client_dir}"
                    )

                except (
                    psutil.NoSuchProcess,
                    psutil.AccessDenied,
                    psutil.ZombieProcess,
                ):
                    continue

                except (OSError, ValueError) as exc:
                    log.debug(
                        f"Error inspecting LeagueClient process: {exc}"
                    )
                    continue

            if found_client_process:
                log.debug(
                    "LeagueClient.exe process was found, but the game "
                    "directory could not be inferred"
                )
            else:
                log.debug("No LeagueClient.exe process found")

            return None, None

        except Exception as exc:
            log.warning(
                f"Error detecting paths via LeagueClient.exe: {exc}"
            )
            return None, None
