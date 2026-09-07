#!/usr/bin/env python3
"""Attribute a new Git commit to a configured local fleet participant.

The helper reads tmux and the selected registry; it never changes fleet state,
registers a participant, or sends a message. Attribution failures do not block Git.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import runtime_config as cfg
import tmux_runtime


def note(reason: str) -> None:
    print(f"NOTE seat trailer skipped: {reason}", file=sys.stderr)


def settings() -> dict | None:
    value = cfg.get("seat_trailer")
    if value is None:
        return None
    required = {"members_command", "agent_windows", "host", "trailer_key"}
    # The former ledger field is accepted for old caller configurations, but ignored.
    if not isinstance(value, dict) or set(value) - {"ledger"} != required:
        raise ValueError("invalid attribution configuration")
    for key in ("agent_windows", "members_command"):
        if not isinstance(value[key], list) or any(
            not isinstance(item, str) or not item or any(c in item for c in "\r\n\0")
            for item in value[key]
        ):
            raise ValueError("invalid attribution configuration")
    if not isinstance(value["trailer_key"], str) or not re.fullmatch(
        r"[A-Za-z][A-Za-z0-9-]*", value["trailer_key"]
    ):
        raise ValueError("invalid trailer key")
    if not isinstance(value["host"], str) or not value["host"].strip():
        raise ValueError("invalid attribution host")
    return value


def pane_locations() -> list[tuple[str, str]]:
    """All grouped-session aliases for the caller's inherited tmux pane."""
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return []
    out = subprocess.run(
        [
            *tmux_runtime.base_cmd(),
            "list-panes",
            *tmux_runtime.pane_scope(),
            "-F",
            "#{pane_id} #{session_name}:#{window_index}.#{pane_index} #{window_name}",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if out.returncode:
        raise RuntimeError("tmux observation failed")
    found = []
    for line in out.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) >= 2 and parts[0] == pane:
            found.append((parts[1], parts[2] if len(parts) > 2 else ""))
    return found


def members(config: dict) -> list[dict]:
    command = [cfg.expand(argument) for argument in config["members_command"]]
    if not command:
        return []
    out = subprocess.run(
        command, capture_output=True, text=True, timeout=5, check=False
    )
    if out.returncode:
        raise RuntimeError("member lookup failed")
    rows = []
    for line in out.stdout.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or not isinstance(row.get("agent_id"), str):
            raise ValueError("invalid member response")
        rows.append(row)
    return rows


def resolve_trailer(config: dict) -> str | None:
    locations = pane_locations()
    if not locations:
        return None
    host = config["host"]
    if host == "{short_hostname}":
        host = socket.gethostname().split(".", 1)[0]
    prefixes = tuple(f"tmux={location} " for location, _window in locations)
    hits = {
        row["agent_id"]: row
        for row in members(config)
        if isinstance(row.get("agent_id"), str)
        and row["agent_id"]
        and row.get("status") == "active"
        and row.get("host") == host
        and row.get("terminal_presence") not in {"unknown", "absent"}
        and (row["pane_id"] == os.environ.get("TMUX_PANE")
             if row.get("pane_id")
             else str(row.get("tmux", "")).startswith(prefixes))
    }
    location, window = locations[0]
    key = config["trailer_key"]
    if len(hits) == 1:
        row = next(iter(hits.values()))
        result = f"{key}: {row.get('handle', '?')} ({row['agent_id']})"
    elif len(hits) > 1:
        handles = ",".join(sorted(str(row.get("handle", "?")) for row in hits.values()))
        result = f"{key}: ambiguous pane {location} ({handles})"
    elif window in config["agent_windows"]:
        result = f"{key}: unregistered {window} pane {location}"
    else:
        return None
    if any(character in result for character in "\r\n\0"):
        raise ValueError("participant metadata is not single-line text")
    return result


def replaying_someone_elses_commit() -> bool:
    """Skip sequencer replay, while allowing plain commits, amendments and merges."""
    action = os.environ.get("GIT_REFLOG_ACTION", "")
    if action.startswith(("rebase", "cherry-pick", "revert")) or "--rebase" in action:
        return True
    try:
        out = subprocess.run(
            [
                "git",
                "rev-parse",
                "--git-path",
                "CHERRY_PICK_HEAD",
                "--git-path",
                "REVERT_HEAD",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0 and any(
        Path(path.strip()).exists() for path in out.stdout.splitlines() if path.strip()
    )


def append_trailer(path: Path, trailer: str) -> None:
    """Use Git's trailer placement and preserve the original if installation fails."""
    raw = path.read_bytes()
    rendered = raw.rstrip(b"\n") + b"\n\n" + trailer.encode("utf-8") + b"\n"
    try:
        done = subprocess.run(
            [
                "git",
                "interpret-trailers",
                "--if-exists",
                "addIfDifferent",
                "--trailer",
                trailer,
            ],
            input=raw,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if done.returncode == 0:
            rendered = done.stdout
    except (OSError, subprocess.TimeoutExpired):
        pass
    descriptor, temporary = tempfile.mkstemp(prefix=".seat-trailer-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(rendered)
        os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["--config"] and len(arguments) >= 2:
        os.environ["FLEET_ORCHESTRATOR_CONFIG"] = arguments[1]
        del arguments[:2]
    if not arguments or arguments[0].startswith("--"):
        note("expected [--config PATH] COMMIT_MESSAGE_FILE")
        return 0
    try:
        config = settings()
        if config is None or replaying_someone_elses_commit():
            return 0
        path = Path(arguments[0])
        if path.is_symlink() or not path.is_file():
            raise ValueError("message file is not regular")
        current = path.read_bytes().decode("utf-8", "surrogateescape")
        if re.search(
            r"^" + re.escape(config["trailer_key"]) + r":\s", current, re.MULTILINE
        ):
            return 0
        trailer = resolve_trailer(config)
        if trailer:
            append_trailer(path, trailer)
    except (
        OSError,
        RuntimeError,
        subprocess.TimeoutExpired,
        ValueError,
        TypeError,
    ) as exc:
        note(type(exc).__name__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
