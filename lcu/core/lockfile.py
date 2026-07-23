#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lockfile Detection and Parsing

Supports:
1. Standard Riot LeagueClient lockfile
2. LeagueClientUx command-line detection
3. Tencent / WeGame League Client
"""

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import psutil

from utils.core.logging import get_logger

log = get_logger()

SWIFTPLAY_MODES = {"SWIFTPLAY", "BRAWL"}
SWIFTPLAY_QUEUE_ID = 480


@dataclass
class Lockfile:
    """Parsed League Client lockfile data."""

    name: str
    pid: int
    port: int
    password: str
    protocol: str


def _parse_lockfile_content(content: str) -> Optional[Lockfile]:
    """
    Parse standard League Client lockfile content.

    Expected format:
        LeagueClient:PID:PORT:PASSWORD:https
    """

    try:
        # maxsplit=4 avoids accidentally splitting the password field
        parts = content.strip().split(":", 4)

        if len(parts) != 5:
            return None

        name, pid, port, password, protocol = parts

        pid_value = int(pid)
        port_value = int(port)
        protocol_value = protocol.strip().lower()

        if not name.strip():
            return None

        if not password.strip():
            return None

        if not 1 <= port_value <= 65535:
            return None

        if protocol_value not in {"http", "https"}:
            return None

        return Lockfile(
            name=name.strip(),
            pid=pid_value,
            port=port_value,
            password=password.strip(),
            protocol=protocol_value,
        )

    except (TypeError, ValueError):
        return None


def parse_lockfile(lockfile_path: str) -> Optional[Lockfile]:
    """Parse a League Client lockfile.

    Args:
        lockfile_path: Path to lockfile.

    Returns:
        Parsed Lockfile, or None if parsing failed.
    """

    if not lockfile_path:
        return None

    path = Path(lockfile_path)

    if not path.is_file():
        return None

    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        result = _parse_lockfile_content(content)

        if result is None:
            log.debug(f"Invalid lockfile format: {path}")

        return result

    except (OSError, PermissionError) as exc:
        log.debug(f"Failed to read lockfile {path}: {exc}")
        return None


def _is_league_client_lockfile(path: Path) -> bool:
    """
    Check whether a path contains a League Client LCU lockfile.

    This deliberately rejects Riot Client lockfiles such as:
        Riot Client:PID:PORT:PASSWORD:https
    """

    parsed = parse_lockfile(str(path))

    if parsed is None:
        return False

    normalized_name = "".join(
        character
        for character in parsed.name.lower()
        if character.isalnum()
    )

    return normalized_name.startswith("leagueclient")


def _extract_command_line_argument(
    command_line: List[str],
    argument_name: str,
) -> Optional[str]:
    """
    Extract either of these command-line formats:

        --app-port=12345
        --app-port 12345
    """

    flag = f"--{argument_name}"

    for index, raw_argument in enumerate(command_line):
        argument = str(raw_argument).strip().strip('"').strip("'")

        if argument.startswith(f"{flag}="):
            value = argument.split("=", 1)[1]
            return value.strip().strip('"').strip("'")

        if argument == flag and index + 1 < len(command_line):
            value = str(command_line[index + 1])
            return value.strip().strip('"').strip("'")

    return None


def _has_command_line_flag(
    command_line: List[str],
    argument_name: str,
) -> bool:
    """Check whether a command-line flag is present."""

    flag = f"--{argument_name}"

    for raw_argument in command_line:
        argument = str(raw_argument).strip().strip('"').strip("'")

        if argument == flag or argument.startswith(f"{flag}="):
            return True

    return False


def _find_lcu_from_process() -> Optional[Lockfile]:
    """
    Find League Client LCU credentials from LeagueClientUx.exe.

    Tencent / WeGame clients may not expose a usable LeagueClient
    lockfile. However, LeagueClientUx is started with arguments such as:

        --app-port=12345
        --remoting-auth-token=xxxxx
        --app-pid=1234
    """

    candidates = []

    try:
        processes = psutil.process_iter(
            attrs=["pid", "name", "exe"],
        )

        for process in processes:
            try:
                process_name = process.info.get("name") or ""
                normalized_name = Path(process_name).stem.lower()

                # Do not match LeagueClientUxRender or helper processes.
                if normalized_name not in {
                    "leagueclientux",
                    "leagueclient",
                }:
                    continue

                command_line = process.cmdline()

                if not command_line:
                    continue

                app_port = _extract_command_line_argument(
                    command_line,
                    "app-port",
                )
                auth_token = _extract_command_line_argument(
                    command_line,
                    "remoting-auth-token",
                )

                # These are the League Client LCU values.
                # Do not substitute riotclient-app-port or
                # riotclient-auth-token here.
                if not app_port or not auth_token:
                    continue

                try:
                    port_value = int(app_port)
                except ValueError:
                    continue

                if not 1 <= port_value <= 65535:
                    continue

                app_pid = _extract_command_line_argument(
                    command_line,
                    "app-pid",
                )

                try:
                    pid_value = int(app_pid) if app_pid else process.pid
                except ValueError:
                    pid_value = process.pid

                protocol = (
                    "http"
                    if _has_command_line_flag(command_line, "use-http")
                    else "https"
                )

                lockfile = Lockfile(
                    name="LeagueClient",
                    pid=pid_value,
                    port=port_value,
                    password=auth_token,
                    protocol=protocol,
                )

                # Prefer LeagueClientUx because its command line normally
                # contains the direct LCU credentials.
                priority = (
                    0 if normalized_name == "leagueclientux" else 1
                )

                candidates.append((priority, lockfile, process_name))

            except (
                psutil.AccessDenied,
                psutil.NoSuchProcess,
                psutil.ZombieProcess,
                OSError,
                ValueError,
            ) as exc:
                log.debug(
                    f"Failed to inspect process "
                    f"{process.info.get('name')}: {exc}"
                )
                continue

    except (psutil.Error, OSError) as exc:
        log.debug(f"LCU process scan failed: {exc}")
        return None

    if not candidates:
        return None

    candidates.sort(key=lambda candidate: candidate[0])

    _, selected, process_name = candidates[0]

    log.info(
        f"Detected League Client LCU credentials from "
        f"{process_name} on port {selected.port}"
    )

    return selected


def _write_compatibility_lockfile(lockfile: Lockfile) -> Optional[str]:
    """
    Write process-derived credentials as a standard temporary lockfile.

    This preserves compatibility with existing Rose code that expects:

        path = find_lockfile()
        lockfile = parse_lockfile(path)
    """

    content = (
        f"{lockfile.name}:"
        f"{lockfile.pid}:"
        f"{lockfile.port}:"
        f"{lockfile.password}:"
        f"{lockfile.protocol}"
    )

    candidate_directories = []

    local_app_data = os.environ.get("LOCALAPPDATA")

    if local_app_data:
        candidate_directories.append(
            Path(local_app_data) / "Rose"
        )

    candidate_directories.append(
        Path(tempfile.gettempdir()) / "Rose"
    )

    for directory in candidate_directories:
        try:
            directory.mkdir(parents=True, exist_ok=True)

            target = directory / "wegame-lcu.lockfile"
            temporary = directory / "wegame-lcu.lockfile.tmp"

            temporary.write_text(
                content,
                encoding="utf-8",
            )

            os.replace(temporary, target)

            try:
                os.chmod(target, 0o600)
            except OSError:
                pass

            log.info(
                f"Created Rose compatibility lockfile: {target}"
            )

            return str(target)

        except (OSError, PermissionError) as exc:
            log.debug(
                f"Failed to create compatibility lockfile in "
                f"{directory}: {exc}"
            )

    return None


def find_lockfile(explicit: Optional[str] = None) -> Optional[str]:
    """Find a usable League Client LCU lockfile.

    Detection order:

    1. Explicitly supplied path
    2. LCU_LOCKFILE environment variable
    3. Standard installation locations
    4. Lockfile near a running LeagueClient executable
    5. LeagueClientUx command-line arguments, including WeGame

    Args:
        explicit: Optional explicit path to lockfile.

    Returns:
        Path to a usable LeagueClient lockfile, or None.
    """

    # 1. Explicit path
    if explicit:
        explicit_path = Path(explicit)

        if (
            explicit_path.is_file()
            and _is_league_client_lockfile(explicit_path)
        ):
            return str(explicit_path)

        log.debug(
            f"Explicit lockfile is not a valid "
            f"LeagueClient lockfile: {explicit_path}"
        )

    # 2. Environment variable
    env = os.environ.get("LCU_LOCKFILE")

    if env:
        env_path = Path(env)

        if (
            env_path.is_file()
            and _is_league_client_lockfile(env_path)
        ):
            return str(env_path)

        # This is important for the WeGame case:
        # Riot Client Data\Config\lockfile must not be accepted.
        log.debug(
            f"LCU_LOCKFILE does not point to a valid "
            f"LeagueClient lockfile: {env_path}"
        )

    # 3. Standard installation paths
    if os.name == "nt":
        common_paths = [
            Path("C:/Riot Games/League of Legends/lockfile"),
            Path(
                "C:/Program Files/"
                "Riot Games/League of Legends/lockfile"
            ),
            Path(
                "C:/Program Files (x86)/"
                "Riot Games/League of Legends/lockfile"
            ),
        ]
    else:
        common_paths = [
            Path(
                "/Applications/"
                "League of Legends.app/Contents/LoL/lockfile"
            ),
            Path.home()
            / ".local/share/League of Legends/lockfile",
        ]

    for path in common_paths:
        if path.is_file() and _is_league_client_lockfile(path):
            return str(path)

    # 4. Search around LeagueClient process executables
    try:
        for process in psutil.process_iter(
            attrs=["name", "exe"],
        ):
            try:
                process_name = process.info.get("name") or ""
                normalized_name = Path(process_name).stem.lower()

                if normalized_name not in {
                    "leagueclient",
                    "leagueclientux",
                }:
                    continue

                executable = process.info.get("exe")

                if not executable:
                    continue

                executable_path = Path(executable)

                directories = [
                    executable_path.parent,
                    executable_path.parent.parent,
                ]

                for directory in directories:
                    candidate = directory / "lockfile"

                    if (
                        candidate.is_file()
                        and _is_league_client_lockfile(candidate)
                    ):
                        return str(candidate)

            except (
                psutil.AccessDenied,
                psutil.NoSuchProcess,
                psutil.ZombieProcess,
                OSError,
            ):
                continue

    except (psutil.Error, OSError) as exc:
        log.debug(
            f"Failed to find lockfile near LeagueClient process: {exc}"
        )

    # 5. WeGame / Tencent fallback
    process_lockfile = _find_lcu_from_process()

    if process_lockfile is not None:
        return _write_compatibility_lockfile(process_lockfile)

    log.warning(
        "Unable to find League Client LCU credentials. "
        "Make sure LeagueClientUx.exe is running and Rose has "
        "permission to inspect its command line."
    )

    return None
