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


def identity() -> str:
    try:
        server, source = configured_server()
    except TmuxRuntimeConfigError as exc:
        return f"invalid-config ({exc})"
    return f"named:{server}" if server else f"default ({source})"
