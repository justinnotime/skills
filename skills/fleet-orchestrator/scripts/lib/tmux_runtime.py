#!/usr/bin/env python3
"""Resolve the machine-local tmux server used by coordination tools.

The harness may move every agent to a named tmux server (``tmux -L NAME``).
Callers must not silently fall back to the default socket: an unreachable
configured server means observations are UNKNOWN, not that every pane vanished.

Precedence:
  1. NW_TMUX_SERVER environment override (tests and one-shot commands)
  2. state/fleet-orchestrator/tmux-server under the configured runtime root
  3. default tmux server when neither is configured

The config file is one trimmed tmux socket name, never command-line arguments.
"""

from __future__ import annotations

import os
import re
import hashlib
import json
import subprocess
from pathlib import Path

import runtime_paths as nw_paths
import runtime_config as cfg

SERVER_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class TmuxRuntimeConfigError(ValueError):
    pass


def config_path() -> Path:
    return cfg.path("tmux.server_file", nw_paths.orchestrator_state_dir() / "tmux-server")


def validate_server(value: str, source: str) -> str:
    value = value.strip()
    if not value:
        raise TmuxRuntimeConfigError(f"empty tmux server in {source}")
    if not SERVER_RE.fullmatch(value):
        raise TmuxRuntimeConfigError(
            f"invalid tmux server {value!r} in {source}; use one socket name"
        )
    return value


def configured_server() -> tuple[str | None, str]:
    """Return ``(name, source)``; name None selects tmux's default server."""
    env_value = os.environ.get("NW_TMUX_SERVER")
    if env_value is not None:
        return validate_server(env_value, "NW_TMUX_SERVER"), "env"
    path = config_path()
    try:
        value = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, "default"
    return validate_server(value, str(path)), str(path)


def base_cmd() -> list[str]:
    server, _source = configured_server()
    return ["tmux", "-L", server] if server else ["tmux"]


def pane_scope() -> list[str]:
    """A selected session is the terminal boundary, including on shared servers."""
    session = os.environ.get("NW_FLEET_PRIMARY_SESSION", "")
    if not session:
        return ["-a"]
    if not SERVER_RE.fullmatch(session):
        raise TmuxRuntimeConfigError("invalid fleet primary session")
    return ["-s", "-t", "=" + session]


def window_scope() -> list[str]:
    scope = pane_scope()
    return scope[1:] if scope[0] == "-s" else scope


def pane_snapshot() -> dict[str, dict[str, str | bool]]:
    """Observe exact pane identities and their current locations once.

    Pane IDs are only unique for a tmux server's lifetime. The socket, process
    ID and server start time bind a registration to that generation; a later
    server cannot inherit an old registration by reusing a pane number.
    """
    fields = (
        "#{socket_path}", "#{pid}", "#{start_time}", "#{pane_id}",
        "#{session_name}:#{window_index}.#{pane_index}", "#{pane_dead}",
    )
    try:
        result = subprocess.run(
            [*base_cmd(), "-u", "list-panes", *pane_scope(), "-F", "\t".join(fields)],
            text=True, capture_output=True, check=False, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired, TmuxRuntimeConfigError) as exc:
        raise RuntimeError(f"tmux observation unavailable: {exc}") from exc
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "tmux observation unavailable")
    panes: dict[str, dict[str, str | bool]] = {}
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != len(fields):
            raise RuntimeError("tmux observation returned an incomplete pane identity")
        socket_path, pid, started, pane, location, dead = parts
        if not socket_path or not pid.isdigit() or not started.isdigit() or not re.fullmatch(r"%\d+", pane):
            raise RuntimeError("tmux observation returned an incomplete server identity")
        identity = json.dumps([socket_path, pid, started], separators=(",", ":"))
        server_id = "tmux:" + hashlib.sha256(identity.encode()).hexdigest()
        observation = {"server_id": server_id, "location": location, "dead": dead == "1"}
        # Grouped viewers expose the same physical pane under another session.
        # Use the primary's location when a default-server scan sees both.
        previous = panes.get(pane)
        if previous is None or (str(previous["location"]).startswith("tview-") and not location.startswith("tview-")):
            panes[pane] = observation
    return panes


def identity() -> str:
    try:
        server, source = configured_server()
    except TmuxRuntimeConfigError as exc:
        return f"invalid-config ({exc})"
    return f"named:{server}" if server else f"default ({source})"
