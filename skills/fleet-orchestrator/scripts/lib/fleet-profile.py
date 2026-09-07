#!/usr/bin/env python3
"""Use tmux sessions as local fleets, with separate durable task/message stores.

Ordinary local sessions need no fleet configuration file. Explicit legacy and
Matrix profiles remain supported; the default fleet retains its existing data
and optional display alias. Grouped terminal views are not additional fleets.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path
from typing import Mapping

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_config as cfg

SCHEMA = 1
LOCAL_SCHEMA = 2
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
TMUX_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
SESSION_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
ROOM_RE = re.compile(r"^![^\s:]+:[^\s:]+$")
MATRIX_EXPECTED_FIELDS = {
    "schema",
    "name",
    "tmux_server",
    "primary_session",
    "matrix_homeserver",
    "matrix_room",
    "matrix_registry_room",
}
LOCAL_EXPECTED_FIELDS = {
    "schema",
    "name",
    "tmux_server",
    "primary_session",
    "agent_bus_transport",
    "local_host",
}
# Kept as a compatibility name for callers that imported the original schema.
EXPECTED_FIELDS = MATRIX_EXPECTED_FIELDS

PROFILE_ENV_KEYS = (
    "NW_FLEET",
    "NW_FLEET_PROFILE_APPLIED",
    "NW_FLEET_PROFILE_PATH",
    "NW_FLEET_PRIMARY_SESSION",
    "NW_TMUX_SERVER",
    "NOTES_RUNTIME_DIR",
    "DISPATCH_LEDGER_DB",
    "AGENT_BUS_TRANSPORT",
    "AGENT_BUS_CFG",
    "AGENT_BUS_DB",
    "MATRIX_BUS_CFG",
    "MATRIX_BUS_HS",
    "MATRIX_BUS_ROOM",
    "MATRIX_BUS_REGISTRY_ROOM",
)

class FleetProfileError(ValueError):
    """A profile is missing, partial, unsafe, or not isolated."""


def _home(env: Mapping[str, str]) -> Path:
    return Path(env.get("HOME", str(Path.home()))).expanduser()


def profile_dir(env: Mapping[str, str] = os.environ) -> Path:
    override = env.get("NW_FLEET_PROFILE_DIR", "").strip()
    return (Path(override).expanduser() if override
            else cfg.path("fleets.profile_directory",
                          Path(env.get("XDG_CONFIG_HOME", str(_home(env) / ".config")))
                          / "fleet-orchestrator/fleets", env=env))


def runtime_root(env: Mapping[str, str] = os.environ) -> Path:
    override = env.get("NW_FLEET_RUNTIME_ROOT", "").strip()
    return (Path(override).expanduser() if override
            else cfg.path("fleets.runtime_directory",
                          Path(env.get("XDG_STATE_HOME", str(_home(env) / ".local/state")))
                          / "fleet-orchestrator/fleets", env=env))


def matrix_config_root(env: Mapping[str, str] = os.environ) -> Path:
    override = env.get("NW_FLEET_MATRIX_CFG_ROOT", "").strip()
    return (Path(override).expanduser() if override
            else cfg.path("fleets.matrix_config_directory",
                          Path(env.get("XDG_CONFIG_HOME", str(_home(env) / ".config")))
                          / "fleet-orchestrator/matrix-fleets", env=env))


def local_hostname() -> str:
    """Return the same short host identity used by Agent Bus onboarding."""
    return socket.gethostname().split(".", 1)[0]


def validate_name(name: str) -> str:
    if name == "default":
        return name
    if not NAME_RE.fullmatch(name):
        raise FleetProfileError(
            "fleet name must match [a-z0-9][a-z0-9-]{0,31}"
        )
    return name


def _default_environment(base: Mapping[str, str]) -> dict[str, str]:
    """Remove a named selection before reading the default configuration."""
    result = dict(base)
    if (result.get("NW_FLEET_PROFILE_APPLIED")
            or result.get("NW_FLEET", "default") not in {"", "default"}):
        for key in PROFILE_ENV_KEYS:
            result.pop(key, None)
    else:
        for key in ("NW_FLEET", "NW_FLEET_PROFILE_PATH",
                    "NW_FLEET_PRIMARY_SESSION"):
            result.pop(key, None)
        if (result.get("AGENT_BUS_TRANSPORT", "").strip().lower() == "local"
                or result.get("AGENT_BUS_CFG")):
            for key in ("AGENT_BUS_TRANSPORT", "AGENT_BUS_CFG", "AGENT_BUS_DB"):
                result.pop(key, None)
    return result


def default_name(env: Mapping[str, str] = os.environ) -> str:
    values = _default_environment(env)
    name = cfg.get("fleets.default_name", "default", env=values)
    if not isinstance(name, str):
        raise FleetProfileError("fleets.default_name must be a fleet name")
    validate_name(name)
    collision = profile_dir(values) / f"{name}.json"
    if collision.exists() or collision.is_symlink():
        raise FleetProfileError(
            f"default fleet name {name!r} conflicts with a named profile"
        )
    return name


def _canonical_name(name: str, env: Mapping[str, str]) -> str:
    validate_name(name)
    return "default" if name in {"default", default_name(env)} else name


def profile_path(name: str, env: Mapping[str, str] = os.environ) -> Path:
    if _canonical_name(name, env) == "default":
        raise FleetProfileError("the default fleet deliberately has no profile")
    return profile_dir(env) / f"{name}.json"


def _read_json(path: Path) -> dict[str, object]:
    try:
        if path.is_symlink():
            raise FleetProfileError(f"profile must not be a symlink: {path}")
        if not stat.S_ISREG(path.stat().st_mode):
            raise FleetProfileError(f"profile must be a regular file: {path}")
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FleetProfileError(f"fleet profile does not exist: {path}") from exc
    except OSError as exc:
        raise FleetProfileError(f"cannot read fleet profile {path}: {exc}") from exc
    try:
        def unique_object(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise FleetProfileError(f"duplicate field {key!r} in {path}")
                result[key] = item
            return result

        value = json.loads(raw, object_pairs_hook=unique_object)
    except json.JSONDecodeError as exc:
        raise FleetProfileError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise FleetProfileError(f"fleet profile must be one JSON object: {path}")
    return value


def _nonempty_string(profile: Mapping[str, object], field: str, path: Path) -> str:
    value = profile.get(field)
    if not isinstance(value, str) or not value.strip():
        raise FleetProfileError(f"{field} must be a non-empty string in {path}")
    if value != value.strip():
        raise FleetProfileError(f"{field} must not have surrounding whitespace in {path}")
    return value


def _validate_profile(
    name: str,
    value: dict[str, object],
    path: Path,
    *,
    require_local_host: bool = True,
) -> dict[str, object]:
    schema = value.get("schema")
    if type(schema) is not int or schema not in {SCHEMA, LOCAL_SCHEMA}:
        raise FleetProfileError(f"unsupported schema in {path}: {schema!r}")
    expected_fields = (
        MATRIX_EXPECTED_FIELDS if schema == SCHEMA else LOCAL_EXPECTED_FIELDS
    )
    fields = set(value)
    if fields != expected_fields:
        missing = sorted(expected_fields - fields)
        unknown = sorted(fields - expected_fields)
        detail = []
        if missing:
            detail.append("missing=" + ",".join(missing))
        if unknown:
            detail.append("unknown=" + ",".join(unknown))
        raise FleetProfileError(f"profile fields are not exact in {path}: {' '.join(detail)}")
    if value["name"] != name:
        raise FleetProfileError(
            f"profile name {value['name']!r} does not match filename {name!r}"
        )

    tmux_server = _nonempty_string(value, "tmux_server", path)
    primary_session = _nonempty_string(value, "primary_session", path)
    if not TMUX_RE.fullmatch(tmux_server):
        raise FleetProfileError(f"invalid tmux_server in {path}: {tmux_server!r}")
    if not SESSION_RE.fullmatch(primary_session):
        raise FleetProfileError(
            f"invalid primary_session in {path}: {primary_session!r}"
        )
    if schema == LOCAL_SCHEMA:
        transport = _nonempty_string(value, "agent_bus_transport", path)
        local_host = _nonempty_string(value, "local_host", path)
        if transport != "local":
            raise FleetProfileError(
                f"agent_bus_transport must be 'local' in {path}"
            )
        current_host = local_hostname()
        if require_local_host and local_host != current_host:
            raise FleetProfileError(
                f"local fleet {name!r} belongs to host {local_host!r}, "
                f"not {current_host!r}"
            )
        return value

    homeserver = _nonempty_string(value, "matrix_homeserver", path)
    room = _nonempty_string(value, "matrix_room", path)
    registry_room = _nonempty_string(value, "matrix_registry_room", path)
    parsed_homeserver = urllib.parse.urlsplit(homeserver)
    if (
        parsed_homeserver.scheme != "https"
        or not parsed_homeserver.hostname
        or parsed_homeserver.username is not None
        or parsed_homeserver.password is not None
        or parsed_homeserver.path not in {"", "/"}
        or parsed_homeserver.query
        or parsed_homeserver.fragment
        or any(c.isspace() for c in homeserver)
    ):
        raise FleetProfileError(
            f"matrix_homeserver must be one https origin in {path}"
        )
    for field, room_id in (("matrix_room", room),
                           ("matrix_registry_room", registry_room)):
        if not ROOM_RE.fullmatch(room_id):
            raise FleetProfileError(f"invalid {field} in {path}: {room_id!r}")
    if room == registry_room:
        raise FleetProfileError(f"message and registry rooms must differ in {path}")
    return value


def _default_tmux_server(env: Mapping[str, str]) -> str | None:
    env = _default_environment(env)
    override = env.get("NW_DEFAULT_TMUX_SERVER", "").strip()
    if override:
        if not TMUX_RE.fullmatch(override):
            raise FleetProfileError("invalid NW_DEFAULT_TMUX_SERVER")
        return override
    inherited = env.get("NW_TMUX_SERVER", "").strip()
    if inherited and not env.get("NW_FLEET_PROFILE_APPLIED"):
        if not TMUX_RE.fullmatch(inherited):
            raise FleetProfileError("invalid inherited NW_TMUX_SERVER")
        return inherited
    state = cfg.path("paths.orchestrator_state",
                     cfg.path("runtime_dir",
                              Path(env.get("XDG_STATE_HOME", str(_home(env) / ".local/state")))
                              / "fleet-orchestrator", env=env)
                     / "state/fleet-orchestrator", env=env)
    path = cfg.path("tmux.server_file", state / "tmux-server", env=env)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise FleetProfileError(f"cannot read default tmux selector {path}: {exc}") from exc
    if not TMUX_RE.fullmatch(value):
        raise FleetProfileError(f"invalid default tmux selector in {path}")
    return value


def _default_matrix_rooms(env: Mapping[str, str]) -> set[str]:
    """Rooms the default fleet can currently reach.

    The default transport configuration remains protected even while a named
    profile replaces the current process's Matrix environment.
    """
    rooms = {value for value in (cfg.get("matrix.room", "", env=env),
                                  cfg.get("matrix.registry_room", "", env=env))
             if value}
    if not env.get("NW_FLEET_PROFILE_APPLIED"):
        for key in ("MATRIX_BUS_ROOM", "MATRIX_BUS_REGISTRY_ROOM"):
            value = env.get(key, "").strip()
            if value:
                rooms.add(value)
    return rooms


def _validate_unique(selected_name: str, selected: Mapping[str, object],
                     env: Mapping[str, str]) -> None:
    selected_server = str(selected["tmux_server"])
    default_server = _default_tmux_server(env)
    if default_server and selected_server == default_server:
        raise FleetProfileError(
            f"fleet {selected_name!r} reuses the default tmux server {default_server!r}"
        )

    selected_rooms = (
        {
            str(selected["matrix_room"]),
            str(selected["matrix_registry_room"]),
        }
        if selected["schema"] == SCHEMA
        else set()
    )
    if selected_rooms and selected_rooms & _default_matrix_rooms(env):
        raise FleetProfileError(
            f"fleet {selected_name!r} reuses a default Matrix room"
        )
    root = profile_dir(env)
    try:
        candidates = sorted(root.glob("*.json"))
    except OSError as exc:
        raise FleetProfileError(f"cannot inspect fleet profile directory {root}: {exc}") from exc
    for other_path in candidates:
        if other_path.name == f"{selected_name}.json":
            continue
        other_name = other_path.stem
        validate_name(other_name)
        # Other profiles still participate in uniqueness checks even when they
        # are host-bound elsewhere. Host ownership applies only when selecting
        # that profile, not while inspecting the directory around it.
        other = _validate_profile(
            other_name,
            _read_json(other_path),
            other_path,
            require_local_host=False,
        )
        if selected_server == other["tmux_server"]:
            raise FleetProfileError(
                f"fleets {selected_name!r} and {other_name!r} reuse tmux server"
            )
        other_rooms = (
            {str(other["matrix_room"]), str(other["matrix_registry_room"])}
            if other["schema"] == SCHEMA
            else set()
        )
        if selected_rooms and selected_rooms & other_rooms:
            raise FleetProfileError(
                f"fleets {selected_name!r} and {other_name!r} reuse a Matrix room"
            )


def _ensure_profile_dir(env: Mapping[str, str]) -> Path:
    root = profile_dir(env)
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink() or not stat.S_ISDIR(root.stat().st_mode):
            raise FleetProfileError(
                f"fleet profile directory must be a real directory: {root}"
            )
        root.chmod(0o700)
    except OSError as exc:
        raise FleetProfileError(
            f"cannot prepare fleet profile directory {root}: {exc}"
        ) from exc
    return root


def _publish_new_profile(path: Path, value: Mapping[str, object]) -> None:
    """Publish complete bytes without replacing a concurrent creator."""
    payload = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    fd, raw_temp = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp"
    )
    temp_path = Path(raw_temp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # A hard link is an atomic no-replace publication on this filesystem.
        os.link(temp_path, path, follow_symlinks=False)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def create_local_profile(
    name: str,
    *,
    tmux_server: str | None = None,
    primary_session: str | None = None,
    env: Mapping[str, str] = os.environ,
) -> tuple[Path, bool]:
    """Create one host-bound local profile, or validate the existing profile."""
    if _canonical_name(name, env) == "default":
        raise FleetProfileError("the default fleet deliberately has no profile")
    root = _ensure_profile_dir(env)
    path = profile_path(name, env)

    lock_flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        lock_flags |= os.O_NOFOLLOW
    try:
        lock_fd = os.open(root / ".create.lock", lock_flags, 0o600)
    except OSError as exc:
        raise FleetProfileError(f"cannot lock fleet profile directory {root}: {exc}") from exc
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if path.exists() or path.is_symlink():
            existing = _validate_profile(name, _read_json(path), path)
            _validate_unique(name, existing, env)
            if tmux_server is not None and existing["tmux_server"] != tmux_server:
                raise FleetProfileError(
                    f"fleet profile already uses tmux server "
                    f"{existing['tmux_server']!r}: {path}"
                )
            if (primary_session is not None
                    and existing["primary_session"] != primary_session):
                raise FleetProfileError(
                    f"fleet profile already uses primary session "
                    f"{existing['primary_session']!r}: {path}"
                )
            return path, False
        server = f"nw-{name}" if tmux_server is None else tmux_server
        session = name if primary_session is None else primary_session
        desired: dict[str, object] = {
            "schema": LOCAL_SCHEMA,
            "name": name,
            "tmux_server": server,
            "primary_session": session,
            "agent_bus_transport": "local",
            "local_host": local_hostname(),
        }
        _validate_profile(name, desired, path)
        _validate_unique(name, desired, env)
        try:
            _publish_new_profile(path, desired)
        except FileExistsError:
            # A writer that does not use this lock may still race us. Validate
            # its complete publication instead of overwriting it.
            existing = _validate_profile(name, _read_json(path), path)
            _validate_unique(name, existing, env)
            if tmux_server is not None and existing["tmux_server"] != tmux_server:
                raise FleetProfileError(
                    f"fleet profile was concurrently created with tmux server "
                    f"{existing['tmux_server']!r}: {path}"
                )
            if (primary_session is not None
                    and existing["primary_session"] != primary_session):
                raise FleetProfileError(
                    f"fleet profile was concurrently created with primary session "
                    f"{existing['primary_session']!r}: {path}"
                )
            return path, False
        return path, True
    finally:
        os.close(lock_fd)


def resolve(name: str, env: Mapping[str, str] = os.environ) -> dict[str, str]:
    """Return the complete environment selection for one named fleet."""
    name = _canonical_name(name, env)
    if name == "default":
        return {}
    path = profile_path(name, env)
    if path.exists() or path.is_symlink():
        profile = _validate_profile(name, _read_json(path), path)
        _validate_unique(name, profile, env)
        profile_source = str(path)
    else:
        sessions = local_sessions(env)
        if name not in sessions and not (runtime_root(env) / name).is_dir():
            raise FleetProfileError(f"fleet {name!r} does not exist: no tmux session or saved work")
        profile = {
            "schema": LOCAL_SCHEMA,
            "tmux_server": _default_tmux_server(env) or "default",
            "primary_session": name,
            "local_host": local_hostname(),
        }
        profile_source = ""

    runtime = runtime_root(env) / name
    common = {
        "NW_FLEET": name,
        "NW_FLEET_PROFILE_APPLIED": name,
        "NW_FLEET_PROFILE_PATH": profile_source,
        "NW_FLEET_PRIMARY_SESSION": str(profile["primary_session"]),
        "NW_TMUX_SERVER": str(profile["tmux_server"]),
        "NOTES_RUNTIME_DIR": str(runtime),
    }
    if profile["schema"] == LOCAL_SCHEMA:
        agent_bus_cfg = runtime / "state" / "agent-bus"
        common.update({
            "DISPATCH_LEDGER_DB": str(
                runtime / "state" / "fleet-orchestrator" / "dispatch-ledger.sqlite3"
            ),
            "AGENT_BUS_TRANSPORT": "local",
            "AGENT_BUS_CFG": str(agent_bus_cfg),
            "AGENT_BUS_DB": str(agent_bus_cfg / "agent-bus-v3.sqlite3"),
        })
        return common

    matrix_cfg = matrix_config_root(env) / name
    common.update({
        "MATRIX_BUS_CFG": str(matrix_cfg),
        "DISPATCH_LEDGER_DB": str(matrix_cfg / "dispatch-ledger.sqlite3"),
        "AGENT_BUS_TRANSPORT": "matrix",
        "AGENT_BUS_DB": str(matrix_cfg / "agent-bus-v3.sqlite3"),
        "MATRIX_BUS_HS": str(profile["matrix_homeserver"]),
        "MATRIX_BUS_ROOM": str(profile["matrix_room"]),
        "MATRIX_BUS_REGISTRY_ROOM": str(profile["matrix_registry_room"]),
    })
    return common


def command_env(name: str, base: Mapping[str, str] = os.environ) -> dict[str, str]:
    name = _canonical_name(name, base)
    if name == "default":
        # When a named profile produced this environment, remove its complete
        # selection.  In an ordinary legacy environment there is no marker,
        # so explicit manual overrides retain exactly their old meaning.
        result = _default_environment(base)
        # Retain TMUX for terminal-client context, but terminal observers
        # must not follow that socket after selecting default runtime data.
        result["NW_TMUX_SERVER"] = _default_tmux_server(base) or "default"
        return result
    result = dict(base)
    resolved = resolve(name, base)
    for key in PROFILE_ENV_KEYS:
        result.pop(key, None)
    result.update(resolved)
    return result


def _tmux_context(name: str, env: Mapping[str, str]) -> tuple[list[str], dict[str, str]]:
    values = resolve(name, env)
    process_env = dict(os.environ)
    process_env.update(command_env(name, env))
    process_env.pop("TMUX", None)
    tmux_bin = process_env.get("TMUX_BIN", "tmux").strip()
    if not tmux_bin:
        raise FleetProfileError("TMUX_BIN must not be empty")
    return [tmux_bin, "-L", values["NW_TMUX_SERVER"]], process_env


def _run_tmux(base: list[str], args: list[str], process_env: Mapping[str, str]):
    try:
        return subprocess.run(
            [*base, *args],
            env=dict(process_env),
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise FleetProfileError(f"could not run tmux: {exc}") from exc


def apply_tmux_environment(name: str, *, dry_run: bool,
                           env: Mapping[str, str] = os.environ) -> None:
    values = resolve(name, env)
    server = values["NW_TMUX_SERVER"]
    session_scope = not values["NW_FLEET_PROFILE_PATH"]
    scope = (["-t", "=" + values["NW_FLEET_PRIMARY_SESSION"]]
             if session_scope else ["-g"])
    clear_scope = [*scope, "-u"] if session_scope else ["-gu"]
    base, process_env = _tmux_context(name, env)
    probe = _run_tmux(base, ["list-sessions"], process_env)
    if probe.returncode:
        raise FleetProfileError(
            probe.stderr.strip() or f"tmux server {server!r} is unreachable"
        )
    if dry_run:
        clears = sum(1 for key in PROFILE_ENV_KEYS if key not in values)
        print(
            f"would set {len(values)} variables and clear {clears} variables "
            f"on tmux server {server}"
        )
        return

    # Remove the completion marker first and restore it last. If tmux dies
    # during the update, descendants see NW_FLEET without a matching marker;
    # wrappers re-resolve it and the direct adapter refuses it.
    clear = _run_tmux(
        base, ["set-environment", *clear_scope, "NW_FLEET_PROFILE_APPLIED"], process_env
    )
    if clear.returncode:
        raise FleetProfileError(
            clear.stderr.strip() or "could not clear tmux fleet completion marker"
        )
    for key in PROFILE_ENV_KEYS:
        if key in values or key == "NW_FLEET_PROFILE_APPLIED":
            continue
        result = _run_tmux(base, ["set-environment", *clear_scope, key], process_env)
        if result.returncode:
            raise FleetProfileError(
                result.stderr.strip() or f"could not clear tmux environment {key}"
            )
    ordered = ["NW_FLEET"] + [
        key for key in values
        if key not in {"NW_FLEET", "NW_FLEET_PROFILE_APPLIED"}
    ] + ["NW_FLEET_PROFILE_APPLIED"]
    for key in ordered:
        value = values[key]
        result = _run_tmux(
            base, ["set-environment", *scope, key, value], process_env
        )
        if result.returncode:
            raise FleetProfileError(
                result.stderr.strip() or f"could not set tmux environment {key}"
            )
    target = values["NW_FLEET_PRIMARY_SESSION"] if session_scope else server
    print(f"set fleet environment on tmux {'session' if session_scope else 'server'} {target}")


def ensure_primary_session(
    name: str,
    *,
    rollback_profile: Path | None = None,
    env: Mapping[str, str] = os.environ,
) -> tuple[str, str, bool]:
    """Make a named fleet immediately attachable without exposing tmux setup."""
    root = _ensure_profile_dir(env)
    if rollback_profile is not None and rollback_profile != profile_path(name, env):
        raise FleetProfileError("rollback profile does not match the selected fleet")
    lock_flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        lock_flags |= os.O_NOFOLLOW
    try:
        lock_fd = os.open(root / ".create.lock", lock_flags, 0o600)
    except OSError as exc:
        raise FleetProfileError(f"cannot lock fleet profile directory {root}: {exc}") from exc
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            values = resolve(name, env)
            server = values["NW_TMUX_SERVER"]
            session = values["NW_FLEET_PRIMARY_SESSION"]
            base, process_env = _tmux_context(name, env)
            server_probe = _run_tmux(base, ["list-sessions"], process_env)
            created = False
            if server_probe.returncode == 0:
                owner = _run_tmux(
                    base,
                    ["show-environment", "-g", "NW_FLEET_PROFILE_APPLIED"],
                    process_env,
                )
                marker_key, separator, marker = owner.stdout.strip().partition("=")
                if (
                    owner.returncode != 0
                    or marker_key != "NW_FLEET_PROFILE_APPLIED"
                    or not separator
                    or not marker
                ):
                    raise FleetProfileError(
                        f"tmux server {server!r} already exists without a valid "
                        "fleet owner; refusing to adopt it"
                    )
                if marker != name:
                    raise FleetProfileError(
                        f"tmux server {server!r} belongs to fleet {marker!r}"
                    )
                apply_tmux_environment(name, dry_run=False, env=env)
                session_probe = _run_tmux(
                    base, ["has-session", "-t", f"={session}"], process_env
                )
                if session_probe.returncode:
                    started = _run_tmux(
                        base,
                        ["new-session", "-d", "-s", session, "-n", "main"],
                        process_env,
                    )
                    if started.returncode:
                        raise FleetProfileError(
                            started.stderr.strip()
                            or f"could not create primary tmux session {session!r}"
                        )
                    created = True
            else:
                started = _run_tmux(
                    base,
                    ["new-session", "-d", "-s", session, "-n", "main"],
                    process_env,
                )
                if started.returncode:
                    raise FleetProfileError(
                        started.stderr.strip()
                        or f"could not start tmux server {server!r}"
                    )
                created = True
                apply_tmux_environment(name, dry_run=False, env=env)

            verified = _run_tmux(
                base, ["has-session", "-t", f"={session}"], process_env
            )
            if verified.returncode:
                raise FleetProfileError(
                    verified.stderr.strip()
                    or f"primary tmux session {session!r} is not reachable"
                )
            return server, session, created
        except BaseException as exc:
            if rollback_profile is not None:
                try:
                    rollback_profile.unlink()
                except FileNotFoundError:
                    pass
                except OSError as cleanup_exc:
                    raise FleetProfileError(
                        f"{exc}; could not remove incomplete fleet profile "
                        f"{rollback_profile}: {cleanup_exc}"
                    ) from exc
            raise
    finally:
        os.close(lock_fd)


def _terminal_target(name: str, env: Mapping[str, str]) -> dict[str, str]:
    """Resolve a fleet's terminal without changing its runtime environment."""
    env = _default_environment(env)
    canonical = _canonical_name(name, env)
    if canonical == "default":
        values = _default_environment(env)
        primary = cfg.get("tmux.primary_session", "0", env=values)
        if not isinstance(primary, str) or not SESSION_RE.fullmatch(primary):
            raise FleetProfileError("invalid tmux.primary_session")
        return {
            "name": default_name(values),
            "tmux_server": _default_tmux_server(values) or "default",
            "primary_session": primary,
        }
    values = resolve(canonical, env)
    return {
        "name": canonical,
        "tmux_server": values["NW_TMUX_SERVER"],
        "primary_session": values["NW_FLEET_PRIMARY_SESSION"],
    }


def _terminal_tmux(args: list[str], env: Mapping[str, str]):
    binary = env.get("TMUX_BIN", "tmux").strip()
    if not binary:
        raise FleetProfileError("TMUX_BIN must not be empty")
    try:
        return subprocess.run(
            # C keeps errors classifiable; -u keeps tmux from replacing the
            # tab delimiters with underscores when no client is attached.
            [binary, "-u", *args], env={**env, "LC_ALL": "C"}, text=True, capture_output=True,
            check=False, timeout=3,
        )
    except subprocess.TimeoutExpired as exc:
        raise FleetProfileError("tmux inspection timed out") from exc
    except OSError as exc:
        raise FleetProfileError("tmux could not be executed") from exc


def local_sessions(env: Mapping[str, str] = os.environ) -> dict[str, str]:
    """Discover primary sessions; tmux owns their lifecycle, not a registry."""
    values = _default_environment(env)
    values.pop("TMUX", None)
    values.pop("TMUX_PANE", None)
    result = _terminal_tmux([
        "-L", _default_tmux_server(env) or "default", "list-sessions", "-F",
        "#{session_name}\t#{session_group}",
    ], values)
    if result.returncode:
        if any(message in result.stderr.lower() for message in (
            "no server running", "no such file or directory", "connection refused",
        )):
            return {}
        raise FleetProfileError(result.stderr.strip() or "cannot list tmux sessions")
    sessions = {}
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 2:
            continue
        name, group = fields
        if NAME_RE.fullmatch(name) and not name.startswith("tview-"):
            sessions[name] = group
    # A grouped view is another client view of the same windows.
    return {name: group for name, group in sessions.items()
            if not group or group == name or group not in sessions}


def create_session(name: str, env: Mapping[str, str] = os.environ) -> None:
    """The compatibility create command creates only a native tmux session."""
    validate_name(name)
    if _canonical_name(name, env) == "default":
        raise FleetProfileError("use the existing default session")
    server = _default_tmux_server(env) or "default"
    values = _default_environment(env)
    values.pop("TMUX", None)
    values.pop("TMUX_PANE", None)
    if name not in local_sessions(env):
        result = _terminal_tmux([
            "-L", server, "new-session", "-d", "-s", name, "-n", "main",
        ], values)
        if result.returncode and name not in local_sessions(env):
            raise FleetProfileError(result.stderr.strip() or "cannot create tmux session")
    print(f"ready tmux session {name!r}; fleet mapping is automatic")
    print(f"attach with: tview --fleet {name}")


def session_action(action: str, name: str | None,
                   env: Mapping[str, str] = os.environ) -> None:
    """Manage native windows; no second lifecycle registry is maintained."""
    target = terminal_target(name, env)
    status, actual = _terminal_observation(target, env)
    if status != "online":
        raise FleetProfileError(f"fleet {target['name']!r} has no running session")
    values = dict(env)
    values.pop("TMUX", None)
    base = ["-L", target["tmux_server"]]
    primary = target["primary_session"]
    if action == "window":
        result = _terminal_tmux([
            *base, "new-window", "-d", "-P", "-F", "#{session_name}:#{window_index}",
            "-t", "=" + primary,
        ], values)
        if result.returncode:
            raise FleetProfileError(result.stderr.strip() or "cannot add window")
        print(result.stdout.strip())
        return

    result = _terminal_tmux([
        *base, "list-panes", "-a", "-F",
        "#{session_name}\t#{session_group}\t#{window_id}\t#{pane_id}",
    ], values)
    if result.returncode:
        raise FleetProfileError(result.stderr.strip() or "cannot inspect windows")
    rows = [line.split("\t") for line in result.stdout.splitlines()]
    rows = [row for row in rows if len(row) == 4]
    windows = {row[2] for row in rows if row[0] == primary}
    panes = {row[3] for row in rows if row[0] == primary}
    if not windows:
        raise FleetProfileError("no windows found; refusing an unverified stop")
    if any(row[2] in windows and row[0] != primary
           and (not actual["group"] or row[1] != actual["group"]) for row in rows):
        raise FleetProfileError("a window is linked outside this session group")
    selected = command_env(target["name"], env)

    database = selected.get("AGENT_BUS_DB")
    if database and Path(database).is_file():
        with sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True) as db:
            registrations = db.execute(
                "SELECT agent_id,pane_id FROM identities WHERE status='active' AND host=?",
                (local_hostname(),),
            ).fetchall()
        for agent_id, pane in registrations:
            if pane not in panes:
                continue
            retired = subprocess.run([
                "bash", str(Path(__file__).resolve().parents[1] / "matrix-bus.sh"),
                "--fleet", target["name"], "retire", agent_id,
            ], env=dict(env), text=True, capture_output=True, check=False)
            if retired.returncode:
                raise FleetProfileError(retired.stderr.strip() or "registration retirement failed")
    # Close the caller's own window last: a stop invoked inside this session
    # must finish retiring registrations and closing its peers before exiting.
    caller_pane = env.get("TMUX_PANE")
    last = next((row[2] for row in rows
                 if row[0] == primary and row[3] == caller_pane), sorted(windows)[-1])
    result = _terminal_tmux([*base, "kill-window", "-a", "-t", last], values)
    if result.returncode:
        raise FleetProfileError(result.stderr.strip() or "cannot close peer windows")
    result = _terminal_tmux([*base, "kill-window", "-t", last], values)
    if result.returncode:
        raise FleetProfileError(result.stderr.strip() or "cannot close final window")
    print(f"stopped session {primary!r}; saved tasks and message history retained")


def _terminal_observation(target: Mapping[str, str], env: Mapping[str, str]):
    values = dict(env)
    values.pop("TMUX", None)
    values.pop("TMUX_PANE", None)
    result = _terminal_tmux(
        ["-L", target["tmux_server"], "list-sessions", "-F",
         "#{session_name}\t#{session_group}\t#{socket_path}"], values,
    )
    if result.returncode:
        diagnostic = result.stderr.lower()
        missing_server = any(phrase in diagnostic for phrase in (
            "no server running", "no such file or directory", "connection refused",
        ))
        return ("offline" if missing_server else "unavailable"), None
    valid_rows = 0
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) != 3 or not fields[0] or not fields[2]:
            continue
        valid_rows += 1
        if fields[0] != target["primary_session"]:
            continue
        return "online", {
            "session": fields[0], "group": fields[1], "socket": fields[2],
        }
    return ("missing-primary" if valid_rows else "unavailable"), None


def _configured_names(env: Mapping[str, str]) -> list[str]:
    # Include the default even when it is invalid, so an inventory does not
    # silently hide an unavailable configuration.
    names = ["default"]
    try:
        names.extend(path.stem for path in sorted(profile_dir(_default_environment(env)).glob("*.json"))
                     if path.stem != "default")
    except OSError as exc:
        raise FleetProfileError("fleet profiles could not be listed") from exc
    primary = cfg.get("tmux.primary_session", "0", env=_default_environment(env))
    try:
        discovered = local_sessions(env)
    except FleetProfileError:
        # The default-server row still reports the failed observation. Keep
        # independently configured fleets inspectable when that server fails.
        discovered = {}
    names.extend(name for name in discovered
                 if name != primary and name not in names
                 and name != default_name(env))
    return names


def terminal_inventory(env: Mapping[str, str] = os.environ) -> list[dict[str, str]]:
    """Return configuration and read-only terminal status, without private paths."""
    rows = []
    for name in _configured_names(env):
        row = {
            "name": name, "tmux_server": "", "primary_session": "",
            "status": "invalid", "command": "", "detail": "invalid configuration",
        }
        try:
            target = _terminal_target(name, env)
            row.update(target)
            row["command"] = f"tview --fleet {target['name']}"
            try:
                status, _ = _terminal_observation(target, env)
            except FleetProfileError as exc:
                row.update(status="unavailable", detail=str(exc))
            else:
                row.update(status=status, detail={
                    "online": "", "offline": "tmux server unavailable",
                    "missing-primary": "configured primary session is missing",
                    "unavailable": "tmux inspection did not return usable session data",
                }[status])
        except (FleetProfileError, ValueError, OSError):
            # Config and transport diagnostics may contain caller-owned
            # paths or room IDs; a fleet list does not need those details.
            pass
        rows.append(row)
    return rows


def terminal_target(name: str | None = None,
                    env: Mapping[str, str] = os.environ) -> dict[str, str]:
    """Prefer an explicit fleet, then the actual current terminal association."""
    if name is not None:
        return _terminal_target(name, env)
    if not env.get("TMUX"):
        return _terminal_target(env.get("NW_FLEET") or "default", env)

    args = ["display-message", "-p"]
    if env.get("TMUX_PANE"):
        args.extend(["-t", env["TMUX_PANE"]])
    args.append("#{socket_path}\t#{session_name}\t#{session_group}")
    try:
        result = _terminal_tmux(args, env)
    except FleetProfileError as exc:
        raise FleetProfileError(
            f"{exc}; run tview --list and select --fleet NAME"
        ) from exc
    current = result.stdout.rstrip("\n").split("\t")
    if result.returncode or len(current) != 3 or not current[0] or not current[1]:
        raise FleetProfileError(
            "cannot inspect the current tmux session; "
            "run tview --list and select --fleet NAME"
        )
    socket_path, session, group = current
    matches = []
    for candidate in _configured_names(env):
        try:
            target = _terminal_target(candidate, env)
            status, actual = _terminal_observation(target, env)
        except (FleetProfileError, ValueError, OSError):
            continue
        if status != "online" or actual["socket"] != socket_path:
            continue
        if actual["session"] == session or (group and actual["group"] == group):
            matches.append(target)
    if len(matches) != 1:
        reason = "not associated with a configured fleet" if not matches else "ambiguous"
        raise FleetProfileError(
            f"current tmux session is {reason}; "
            "run tview --list and select --fleet NAME"
        )
    return matches[0]


def _print_terminal_inventory(rows: list[dict[str, str]]) -> None:
    columns = [("FLEET", "name"), ("SESSION", "primary_session"),
               ("STATUS", "status"), ("ENTER", "command")]
    # Escape control characters even when an invalid filename supplied a row.
    values = [[json.dumps(row[key], ensure_ascii=False)[1:-1] for _, key in columns]
              for row in rows]
    widths = [max([len(title), *(len(row[i]) for row in values)])
              for i, (title, _) in enumerate(columns)]
    print("  ".join(title.ljust(width) for (title, _), width in zip(columns, widths)))
    for row, original in zip(values, rows):
        print("  ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip())
        if original["detail"]:
            print(f"  {original['detail']}")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)

    terminal_p = sub.add_parser("terminal", help="resolve one terminal fleet")
    terminal_p.add_argument("name", nargs="?")
    list_p = sub.add_parser("list", help="list configured fleets and terminal status")
    list_p.add_argument("--json", action="store_true")

    resolve_p = sub.add_parser("resolve", help="validate and print a profile")
    resolve_p.add_argument("name")
    resolve_p.add_argument("--field", choices=sorted({
        "name", "tmux_server", "primary_session", "profile_path",
        "runtime_dir", "agent_bus_transport", "agent_bus_config_dir",
        "dispatch_ledger_db", "agent_bus_db", "local_host",
        "matrix_config_dir", "matrix_homeserver", "matrix_room",
        "matrix_registry_room",
    }))

    create_p = sub.add_parser(
        "create", help="create a host-bound local fleet and its primary tmux session"
    )
    create_p.add_argument("name")
    create_p.add_argument("--tmux-server", help=argparse.SUPPRESS)
    create_p.add_argument("--primary-session", help=argparse.SUPPRESS)

    for action in ("window", "stop"):
        action_p = sub.add_parser(action, help=(
            "add a window to the session" if action == "window" else
            "terminate the session's windows and agents; retain saved work"))
        action_p.add_argument("name", nargs="?")

    exec_p = sub.add_parser("exec", help="run one command in a named fleet")
    exec_p.add_argument("name")
    exec_p.add_argument("command", nargs=argparse.REMAINDER)

    sub.add_parser("current", help="identify this process's tmux-session fleet")

    tmux_p = sub.add_parser(
        "apply-tmux", help="make new processes on the fleet tmux server inherit its profile"
    )
    tmux_p.add_argument("name")
    tmux_p.add_argument("--dry-run", action="store_true")
    return p


def public_view(name: str, env: Mapping[str, str] = os.environ) -> dict[str, str]:
    name = _canonical_name(name, env)
    if name == "default":
        return _terminal_target(name, env)
    values = resolve(name, env)
    view = {
        "name": name,
        "tmux_server": values["NW_TMUX_SERVER"],
        "primary_session": values["NW_FLEET_PRIMARY_SESSION"],
        "profile_path": values["NW_FLEET_PROFILE_PATH"],
        "runtime_dir": values["NOTES_RUNTIME_DIR"],
        "agent_bus_transport": values["AGENT_BUS_TRANSPORT"],
        "agent_bus_config_dir": values.get(
            "AGENT_BUS_CFG", values.get("MATRIX_BUS_CFG", "")
        ),
        "dispatch_ledger_db": values["DISPATCH_LEDGER_DB"],
        "agent_bus_db": values["AGENT_BUS_DB"],
    }
    if values["AGENT_BUS_TRANSPORT"] == "local":
        view["local_host"] = local_hostname()
    else:
        view.update({
            "matrix_config_dir": values["MATRIX_BUS_CFG"],
            "matrix_homeserver": values["MATRIX_BUS_HS"],
            "matrix_room": values["MATRIX_BUS_ROOM"],
            "matrix_registry_room": values["MATRIX_BUS_REGISTRY_ROOM"],
        })
    return view


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.action == "terminal":
            target = terminal_target(args.name)
            print("\t".join(target[key] for key in
                            ("name", "tmux_server", "primary_session")))
            return 0
        if args.action == "list":
            rows = terminal_inventory()
            if args.json:
                print(json.dumps(rows, sort_keys=True, indent=2))
            else:
                _print_terminal_inventory(rows)
            return 0
        if args.action == "create":
            if (not args.tmux_server and not args.primary_session
                    and not profile_path(args.name).exists()):
                create_session(args.name)
                return 0
            path, created = create_local_profile(
                args.name,
                tmux_server=args.tmux_server,
                primary_session=args.primary_session,
            )
            server, session, session_created = ensure_primary_session(
                args.name,
                rollback_profile=path if created else None,
            )
            status = "created" if created else "validated existing"
            session_status = "created" if session_created else "using"
            print(f"{status} fleet profile: {path}")
            print(
                f"ready fleet {args.name!r}: {session_status} primary tmux session "
                f"{session!r} on server {server!r}"
            )
            print(f"attach with: tview --fleet {args.name}")
            return 0
        if args.action == "current":
            target = terminal_target()
            print(_canonical_name(target["name"], os.environ))
            return 0
        if args.action in {"window", "stop"}:
            session_action(args.action, args.name)
            return 0
        if args.action == "resolve":
            view = public_view(args.name)
            if args.field:
                if args.field not in view:
                    raise FleetProfileError(
                        f"field {args.field!r} does not exist for the default fleet"
                    )
                print(view[args.field])
            else:
                print(json.dumps(view, sort_keys=True, indent=2))
            return 0
        if args.action == "exec":
            command = list(args.command)
            if command and command[0] == "--":
                command.pop(0)
            if not command:
                raise FleetProfileError("exec needs a command after --")
            os.execvpe(command[0], command, command_env(args.name))
        if args.action == "apply-tmux":
            if _canonical_name(args.name, os.environ) == "default":
                raise FleetProfileError("apply-tmux requires a named fleet")
            apply_tmux_environment(args.name, dry_run=args.dry_run)
            return 0
    except (FleetProfileError, ValueError) as exc:
        print(f"fleet-profile: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
