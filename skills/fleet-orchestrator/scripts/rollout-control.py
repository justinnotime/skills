#!/usr/bin/env python3
"""Narrow deterministic rollout control for ORC/Agent Bus/harness artifacts.

The default command is read-only observation. Mutating commands operate on one
explicit artifact x harness x seat tuple, never restart a process/service, never
send a message, and never grant trust. Disk publication, process activation,
and behavior verification are deliberately separate facts.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(HERE / "lib"))
import runtime_paths as nw_paths
import runtime_config as cfg

DEFAULT_MANIFEST = cfg.path("rollout.manifest", cfg.config_path().parent / "artifacts.json")
REPO = cfg.path("rollout.source_root", REPO)
CANONICAL_REPO = Path(os.environ.get("ROLLOUT_CANONICAL_REPO") or cfg.path("rollout.canonical_root", REPO))
MAX_SOURCE_BYTES = 1024 * 1024
STATES = (
    "ABSENT", "STAGED", "INSTALLED", "ACTIVATION_REQUIRED",
    "ACTIVE_UNVERIFIED", "VERIFIED", "BLOCKED_TRUST", "DRIFTED", "FAILED",
    # UNKNOWN is honest cannot-read, DISTINCT from FAILED (probe ran and
    # refuted): a broken sensor must not silence the rows it cannot see,
    # and must not impersonate a refuting probe on the rows it can't read.
    "UNKNOWN",
)
FAILURE_STATES = {"BLOCKED_TRUST", "DRIFTED", "FAILED"}
SUCCESS_FOR_TARGET = {
    "INSTALLED": {"INSTALLED", "ACTIVATION_REQUIRED", "ACTIVE_UNVERIFIED", "VERIFIED"},
    "ACTIVE_UNVERIFIED": {"ACTIVE_UNVERIFIED", "VERIFIED"},
    "VERIFIED": {"VERIFIED"},
}
ARTIFACT_KEYS = {
    "id", "class", "source", "source_globs", "harness", "harnesses", "seat",
    "activation", "target_state", "install", "target", "target_roots",
    "copy_source", "observer", "config", "format", "events", "command",
    "trust_required", "package", "required_row", "cron_exact_line",
    "observation_log", "max_observation_age_s", "context_loading",
    "source_roots", "cron_exact_line_command", "command_argv", "command_environment",
}
REQUIRED_ARTIFACT_KEYS = {"id", "class", "harness", "seat", "activation", "target_state", "install"}
ARTIFACT_CLASSES = {
    "reexec", "published-file", "skill-links", "opencode-plugin", "hooks",
    "dsh-plugin", "dsh-composition",
}
INSTALL_MODES = {
    "merge-only", "status-only", "symlink-tree", "copy-file",
    "claude-json-hooks", "codex-stage-hooks", "dsh-profile-plugin",
}
ACTIVATIONS = {
    "reexec", "invocation", "process-restart",
    "profile-restart", "immediate",
}
HARNESS_PROCESS = {
    "claude": "claude", "codex": "codex", "opencode": "opencode", "dsh": "dsh",
}
REEXEC_RECEIPT_RE = re.compile(
    rb"^OK tick done:.*\bprocess_started_ns=(\d+), completed_ns=(\d+)$",
    re.MULTILINE,
)


class ManifestError(ValueError):
    pass


class OperationError(RuntimeError):
    pass


def utc(epoch: float | int | None = None) -> str:
    value = time.time() if epoch is None else float(epoch)
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "unknown"


def expand_vars(value: str, env: dict[str, str]) -> str:
    mapping = {
        "NOTES_REPO_ROOT": env.get("NOTES_REPO_ROOT", str(CANONICAL_REPO)),
        "HOME": env.get("ROLLOUT_HOME", env.get("HOME", str(Path.home()))),
        "XDG_CONFIG_HOME": env.get("XDG_CONFIG_HOME", str(Path(env.get("HOME", str(Path.home()))) / ".config")),
        "DSH_HOME": env.get("DSH_HOME", str(Path(env.get("HOME", str(Path.home()))) / ".dsh")),
        "NOTES_RUNTIME_DIR": env.get("NOTES_RUNTIME_DIR", str(cfg.path("runtime_dir", Path(env.get("HOME", str(Path.home()))) / ".local/state/fleet-orchestrator", env=env))),
    }
    out = value
    for key, val in mapping.items():
        out = out.replace("${" + key + "}", val)
    return out


def expand_path(value: str, env: dict[str, str]) -> Path:
    path = Path(os.path.expanduser(expand_vars(value, env)))
    if not path.is_absolute():
        raise ManifestError(f"expanded destination must be absolute: {value}")
    return path


def reexec_receipt(path: Path) -> tuple[int | None, int | None]:
    """Return the most recent bounded successful-run receipt from a log."""
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - 64 * 1024))
            tail = handle.read(64 * 1024)
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        raise OperationError(f"cannot read reexec observation log {path}: {exc}") from exc
    matches = list(REEXEC_RECEIPT_RE.finditer(tail))
    if not matches:
        return None, None
    match = matches[-1]
    return int(match.group(1)), int(match.group(2))


def safe_relative(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ManifestError(f"source path must be repository-relative without '..': {value}")
    return path


def read_manifest(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc}") from exc
    data = cfg.resolve(data, config_directory=path.resolve().parent)
    validate_manifest(data)
    for artifact in data["artifacts"]:
        if "command_argv" in artifact:
            environment = artifact.get("command_environment", {})
            prefix = (["/usr/bin/env", *[key + "=" + value for key, value in environment.items()]]
                      if environment else [])
            artifact["command"] = shlex.join([*prefix, *artifact["command_argv"]])
    return data


def validate_manifest(data: Any) -> None:
    if not isinstance(data, dict):
        raise ManifestError("manifest root must be an object")
    expected = {"$schema", "schema", "states", "artifacts"}
    if set(data) != expected:
        raise ManifestError(f"manifest keys must be exactly {sorted(expected)}")
    if data["schema"] != "orc-rollout/v1":
        raise ManifestError("manifest schema must be orc-rollout/v1")
    if data["states"] != list(STATES):
        raise ManifestError("manifest states must match the v1 ordered state list exactly")
    if not isinstance(data["artifacts"], list) or not data["artifacts"]:
        raise ManifestError("artifacts must be a non-empty array")
    seen: set[str] = set()
    for idx, artifact in enumerate(data["artifacts"]):
        where = f"artifacts[{idx}]"
        if not isinstance(artifact, dict):
            raise ManifestError(f"{where} must be an object")
        unknown = set(artifact) - ARTIFACT_KEYS
        missing = REQUIRED_ARTIFACT_KEYS - set(artifact)
        if unknown:
            raise ManifestError(f"{where} has unknown keys: {sorted(unknown)}")
        if missing:
            raise ManifestError(f"{where} missing keys: {sorted(missing)}")
        aid = artifact["id"]
        if not isinstance(aid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", aid):
            raise ManifestError(f"{where}.id must be lowercase kebab-case")
        if aid in seen:
            raise ManifestError(f"duplicate artifact id: {aid}")
        seen.add(aid)
        if artifact["class"] not in ARTIFACT_CLASSES:
            raise ManifestError(f"{where}.class unknown: {artifact['class']}")
        if artifact["activation"] not in ACTIVATIONS:
            raise ManifestError(f"{where}.activation unknown: {artifact['activation']}")
        if artifact["install"] not in INSTALL_MODES:
            raise ManifestError(f"{where}.install unknown: {artifact['install']}")
        if artifact["target_state"] not in {"INSTALLED", "ACTIVE_UNVERIFIED", "VERIFIED"}:
            raise ManifestError(f"{where}.target_state is not an allowed success state")
        if not artifact.get("source") and not artifact.get("source_globs"):
            raise ManifestError(f"{where} needs source or source_globs")
        for source in artifact.get("source", []):
            safe_relative(source)
        for pattern in artifact.get("source_globs", []):
            safe_relative(pattern)
        if "source_roots" in artifact:
            roots = artifact["source_roots"]
            if not isinstance(roots, dict) or set(roots) != {"working", "canonical"}:
                raise ManifestError(f"{where}.source_roots needs working and canonical paths")
            for location in roots.values():
                if not isinstance(location, str) or not Path(cfg.expand(location)).is_absolute():
                    raise ManifestError(f"{where}.source_roots must be absolute paths")
        for key in ("cron_exact_line_command", "command_argv"):
            if key in artifact and (not isinstance(artifact[key], list) or not artifact[key]
                                    or any(not isinstance(arg, str) or not arg or "\0" in arg
                                           for arg in artifact[key])):
                raise ManifestError(f"{where}.{key} must be an argument array")
        if "command_argv" in artifact and "command" in artifact:
            raise ManifestError(f"{where} must select command or command_argv")
        environment = artifact.get("command_environment", {})
        if not isinstance(environment, dict) or any(
                not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                or not isinstance(value, str) or "\0" in value
                for key, value in environment.items()):
            raise ManifestError(f"{where}.command_environment is invalid")
        if "cron_exact_line" in artifact and "cron_exact_line_command" in artifact:
            raise ManifestError(f"{where} must select one cron expectation")
        if artifact.get("events") and not all(isinstance(x, str) and x for x in artifact["events"]):
            raise ManifestError(f"{where}.events must be non-empty strings")
        # Destination/config roots are fixed fields interpreted by reviewed
        # adapters; arbitrary commands from the manifest are never executed.
        for key in ("target", "config", "observation_log"):
            if key in artifact and not isinstance(artifact[key], str):
                raise ManifestError(f"{where}.{key} must be a string")


def skill_sources(root: Path) -> dict[str, Path]:
    """Read configured Skill locations or an explicit read-only source command."""
    try:
        query = cfg.command("rollout.skill_sources_command")
        if query:
            result = subprocess.run(query, capture_output=True, text=True, timeout=30, check=True)
            values = json.loads(result.stdout)
        else:
            values = cfg.get("rollout.skill_sources", {})
        if not isinstance(values, dict) or not values:
            raise ValueError("empty skill selection")
        sources = {}
        for name, location in values.items():
            if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
                raise ValueError("invalid Skill name")
            if isinstance(location, str):
                location = cfg.expand(location)
            if not isinstance(location, str) or not Path(location).is_absolute():
                raise ValueError("Skill source must be absolute")
            bundle = Path(location).resolve()
            if not (bundle / "SKILL.md").is_file() or (bundle / "SKILL.md").is_symlink():
                raise ValueError("Skill instructions missing or not regular")
            sources[name] = bundle
        return sources
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise OperationError("cannot read configured Skill sources") from exc


def artifact_root(artifact: dict[str, Any], root: Path, *, canonical: bool = False) -> Path:
    roots = artifact.get("source_roots", {})
    selected = roots.get("canonical" if canonical else "working")
    return Path(cfg.expand(selected)) if selected else root


def source_entries(artifact: dict[str, Any], root: Path, *, canonical: bool = False) -> list[tuple[str, Path]]:
    root = artifact_root(artifact, root, canonical=canonical)
    entries: dict[str, Path] = {}
    for value in artifact.get("source", []):
        rel = safe_relative(value)
        path = root / rel
        if path.is_symlink():
            raise OperationError(f"source may not be a symlink: {path}")
        if path.is_file():
            entries[rel.as_posix()] = path
        elif path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_symlink():
                    raise OperationError(f"source tree contains symlink: {child}")
                if child.is_file():
                    entries[child.relative_to(root).as_posix()] = child
        else:
            raise OperationError(f"source missing or not regular: {path}")
    for pattern in artifact.get("source_globs", []):
        for path in sorted(root.glob(pattern)):
            if path.is_symlink() or not path.is_file():
                raise OperationError(f"glob source must be a regular non-symlink file: {path}")
            entries[path.relative_to(root).as_posix()] = path
    if artifact["class"] == "skill-links":
        for name, bundle in skill_sources(root).items():
            entries[f"public-skills/{name}/SKILL.md"] = bundle / "SKILL.md"
    if not entries:
        raise OperationError(f"artifact {artifact['id']} resolved no source files")
    total = sum(path.stat().st_size for path in entries.values())
    if total > MAX_SOURCE_BYTES:
        raise OperationError(f"artifact {artifact['id']} source is {total} bytes; cap is {MAX_SOURCE_BYTES}")
    return sorted(entries.items())


def spec_identity(artifact: dict[str, Any]) -> bytes:
    # Every behavior-affecting manifest field belongs in the identity. Only
    # human explanation and routing expansion fields are excluded.
    excluded = {"id", "source", "source_globs", "context_loading", "harness", "harnesses", "seat", "target_state"}
    keep = {key: value for key, value in artifact.items() if key not in excluded}
    return json.dumps(keep, sort_keys=True, separators=(",", ":")).encode()


def digest_entries(entries: Iterable[tuple[str, Path]], artifact: dict[str, Any]) -> str:
    h = hashlib.sha256()
    h.update(spec_identity(artifact))
    for logical, path in entries:
        h.update(logical.encode())
        h.update(b"\0")
        data = path.read_bytes()
        h.update(str(len(data)).encode())
        h.update(b"\0")
        h.update(data)
        h.update(b"\0")
    return "sha256:" + h.hexdigest()


def desired_version(artifact: dict[str, Any], repo: Path, *, canonical: bool = False) -> str:
    return digest_entries(source_entries(artifact, repo, canonical=canonical), artifact)


@dataclass
class Seat:
    agent_id: str
    handle: str
    aliases: list[str]
    harness: str
    mode: str
    host: str
    tmux: str
    window: str | None
    pane_id: str | None = None
    session_name: str | None = None
    pane_index: str | None = None
    profile: str | None = None


@dataclass
class ProcessInfo:
    pid: int
    started: float
    command: str
    pane: str | None
    session: str | None
    window: str | None
    profile: str | None = None

    def view(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "started_at": utc(self.started),
            "command": self.command[:240],
        }


class ControlPlane:
    def __init__(self, manifest: dict[str, Any], *, repo: Path = REPO,
                 canonical_repo: Path = CANONICAL_REPO,
                 env: dict[str, str] | None = None):
        self.manifest = {**manifest, "artifacts": [dict(item) for item in manifest["artifacts"]]}
        self.repo = repo.resolve()
        self.canonical_repo = canonical_repo.resolve()
        self.env = dict(os.environ if env is None else env)
        self.env.setdefault("NOTES_REPO_ROOT", str(self.canonical_repo))
        for item in self.manifest["artifacts"]:
            if "command" in item:
                item["command"] = shlex.join(
                    expand_vars(arg, self.env) for arg in shlex.split(item["command"])
                )
        self.home = Path(self.env.get("ROLLOUT_HOME", self.env.get("HOME", str(Path.home()))))
        self.cfg = Path(self.env.get(
            "AGENT_BUS_CFG",
            self.env.get("MATRIX_BUS_CFG", cfg.path("bus.config_directory", self.home / ".config/fleet-orchestrator/bus", env=self.env)),
        ))
        self.bus_db = Path(self.env.get("AGENT_BUS_DB", cfg.path("bus.database", self.cfg / "agent-bus-v3.sqlite3", env=self.env)))
        self.runtime = Path(self.env.get("NOTES_RUNTIME_DIR", cfg.path("runtime_dir", self.home / ".local/state/fleet-orchestrator", env=self.env)))
        self.ledger_db = Path(
            self.env.get("DISPATCH_LEDGER_DB", cfg.path("paths.ledger", self.runtime / "state/fleet-orchestrator/dispatch-ledger.sqlite3", env=self.env))
        )
        self.stage_root = Path(self.env.get("ROLLOUT_STAGE_DIR", self.runtime / "state/rollout-control/staged"))
        self._members_cache: list[Seat] | None = None
        self._panes_cache: dict[str, dict[str, Any]] | None = None
        self._proc_cache: dict[int, tuple[int, list[str], float]] | None = None
        self._all_records_cache: list[dict[str, Any]] | None = None

    def artifact(self, artifact_id: str) -> dict[str, Any]:
        for artifact in self.manifest["artifacts"]:
            if artifact["id"] == artifact_id:
                return artifact
        raise OperationError(f"unknown artifact: {artifact_id}")

    def _members(self) -> list[Seat]:
        if self._members_cache is not None:
            return self._members_cache
        db = self.bus_db
        if not db.exists():
            self._members_cache = []
            return []
        now_ms = int(time.time() * 1000)
        host = socket.gethostname()
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM identities WHERE status='active' AND host=?"
                " AND lease_until_ms IS NOT NULL AND lease_until_ms>?",
                (host, now_ms),
            ).fetchall()
            conn.close()
        except sqlite3.Error as exc:
            raise OperationError(f"cannot read Agent Bus registry {db}: {exc}") from exc
        seats = []
        for row in rows:
            match = re.search(r"tmux=([^:\s]+):(\d+)\.(\d+)", row["tmux"] or "")
            try:
                aliases = json.loads(row["aliases_json"] or "[]")
            except (json.JSONDecodeError, TypeError):
                aliases = []
            seats.append(Seat(
                agent_id=row["agent_id"], handle=row["handle"], aliases=aliases,
                harness=row["harness"], mode=row["mode"], host=row["host"],
                tmux=row["tmux"], window=match.group(2) if match else None,
                session_name=match.group(1) if match else None,
                pane_index=match.group(3) if match else None,
            ))
        self._members_cache = seats
        return seats

    def _proc_snapshot(self) -> dict[int, tuple[int, list[str], float]]:
        if self._proc_cache is not None:
            return self._proc_cache
        result: dict[int, tuple[int, list[str], float]] = {}
        try:
            boot = time.time() - float(Path("/proc/uptime").read_text().split()[0])
            ticks = os.sysconf("SC_CLK_TCK")
        except (OSError, ValueError):
            boot, ticks = 0.0, 100
        for item in Path("/proc").iterdir():
            if not item.name.isdigit():
                continue
            pid = int(item.name)
            try:
                raw = (item / "cmdline").read_bytes()
                argv = [x for x in raw.decode(errors="replace").split("\0") if x]
                stat = (item / "stat").read_text()
                tail = stat.rsplit(")", 1)[1].split()
                ppid = int(tail[1])
                started = boot + int(tail[19]) / ticks
            except (OSError, ValueError, IndexError):
                continue
            result[pid] = (ppid, argv, started)
        self._proc_cache = result
        return result

    def _tmux_base(self) -> list[str]:
        name = self.env.get("NW_TMUX_SERVER")
        return ["tmux", "-L", name] if name else ["tmux"]

    def _panes(self) -> dict[str, dict[str, Any]]:
        if self._panes_cache is not None:
            return self._panes_cache
        if "ROLLOUT_TMUX_PANES_JSON" in self.env:
            data = json.loads(self.env["ROLLOUT_TMUX_PANES_JSON"])
            self._panes_cache = {
                f"{row.get('session', '0')}:{row['window']}.{row.get('pane_index', '0')}": row
                for row in data
            }
            return self._panes_cache
        fmt = "#{pane_id}\t#{session_name}\t#{window_index}\t#{pane_index}\t#{window_name}\t#{pane_current_command}\t#{pane_pid}"
        session = self.env.get("NW_FLEET_PRIMARY_SESSION")
        scope = ["-s", "-t", "=" + session] if session else ["-a"]
        try:
            out = subprocess.run([*self._tmux_base(), "list-panes", *scope, "-F", fmt],
                                 capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            self._panes_cache = {}
            return {}
        panes: dict[str, dict[str, Any]] = {}
        seen: set[str] = set()
        if out.returncode == 0:
            for line in out.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) != 7 or parts[0] in seen:
                    continue
                seen.add(parts[0])
                panes[f"{parts[1]}:{parts[2]}.{parts[3]}"] = {
                    "pane": parts[0], "session": parts[1], "window": parts[2],
                    "pane_index": parts[3], "name": parts[4], "command": parts[5],
                    "pid": int(parts[6]),
                }
        self._panes_cache = panes
        return panes

    def _descendants(self, root: int) -> list[int]:
        procs = self._proc_snapshot()
        children: dict[int, list[int]] = {}
        for pid, (ppid, _argv, _started) in procs.items():
            children.setdefault(ppid, []).append(pid)
        found, stack = [], [root]
        while stack:
            pid = stack.pop()
            if pid in found:
                continue
            found.append(pid)
            stack.extend(children.get(pid, []))
        return found

    def _process_for(self, seat: Seat) -> ProcessInfo | None:
        if not seat.window:
            return None
        panes = self._panes()
        if seat.pane_id:
            pane = next((p for p in panes.values() if p.get("pane") == seat.pane_id), None)
        else:
            key = f"{seat.session_name or '0'}:{seat.window}.{seat.pane_index or '0'}"
            pane = panes.get(key)
        if not pane:
            return None
        seat.pane_id = pane.get("pane")
        procs = self._proc_snapshot()
        candidates = self._descendants(int(pane["pid"]))
        want = HARNESS_PROCESS.get(seat.harness, seat.harness)
        chosen: tuple[int, list[str], float] | None = None
        for pid in candidates:
            info = procs.get(pid)
            if not info or not info[1]:
                continue
            argv = info[1]
            names = [Path(x).name for x in argv[:2]]
            joined = " ".join(argv)
            match = (want in names or (seat.harness == "dsh" and " dsh --profile " in f" {joined} ")
                     or (seat.harness == "dsh" and any(Path(x).name == "dsh" for x in argv)))
            if match:
                chosen = (pid, argv, info[2])
                break
        if chosen is None:
            return None
        pid, argv, started = chosen
        profile = None
        if "--profile" in argv:
            try:
                profile = argv[argv.index("--profile") + 1]
            except IndexError:
                pass
        return ProcessInfo(
            pid=pid, started=started, command=" ".join(argv), pane=pane["pane"],
            session=pane["session"], window=seat.window, profile=profile,
        )

    def _find_process(self, script: Path, tail: str) -> ProcessInfo | None:
        expected = str(script.resolve())
        for pid, (_ppid, argv, started) in self._proc_snapshot().items():
            if expected in argv and tail in argv:
                return ProcessInfo(pid, started, " ".join(argv), None, None, None)
        return None

    def _select_seats(self, artifact: dict[str, Any]) -> list[Seat]:
        sentinel = artifact["seat"]
        harness = artifact["harness"]
        if sentinel == "machine":
            return [Seat("machine", "machine", [], harness, "none", socket.gethostname(), "", None)]
        if sentinel == "fleet-orchestrator-cron":
            return [Seat(sentinel, sentinel, [], harness, "cron", socket.gethostname(), "", None)]
        if sentinel == "dispatcher":
            return [Seat(sentinel, sentinel, [], harness, "pull", socket.gethostname(), "", None)]
        members = self._members()
        if sentinel == "registered-watch-seats":
            return [s for s in members if s.mode == "watch"]
        if sentinel == "registered-seats":
            return [s for s in members if s.harness == harness]
        return [Seat(sentinel, sentinel, [], harness, "none", socket.gethostname(), "", None)]

    def targets(self, artifact: dict[str, Any]) -> list[tuple[str, Seat]]:
        if artifact.get("harnesses"):
            return [(h, Seat("machine", "machine", [], h, "none", socket.gethostname(), "", None))
                    for h in artifact["harnesses"]]
        seats = self._select_seats(artifact)
        return [(seat.harness if artifact["harness"] == "from-seat" else artifact["harness"], seat)
                for seat in seats]

    def select(self, artifact_id: str, harness: str, seat_value: str) -> tuple[dict[str, Any], str, Seat]:
        artifact = self.artifact(artifact_id)
        candidates = self.targets(artifact)
        hits = []
        for h, seat in candidates:
            window_alias = f"tmux{seat.window}" if seat.window else ""
            names = {seat.agent_id, seat.handle, *seat.aliases, window_alias}
            if seat.agent_id == "machine":
                names.add("machine")
            if h == harness and seat_value in names:
                hits.append((artifact, h, seat))
        if len(hits) != 1:
            raise OperationError(f"target {artifact_id} x {harness} x {seat_value} resolved to {len(hits)} tuples")
        return hits[0]

    def _stage_dir(self, artifact: dict[str, Any], harness: str, seat: Seat, version: str) -> Path:
        return self.stage_root / slug(artifact["id"]) / slug(harness) / slug(seat.agent_id) / version.removeprefix("sha256:")

    def _staged(self, artifact: dict[str, Any], harness: str, seat: Seat, version: str) -> bool:
        path = self._stage_dir(artifact, harness, seat, version)
        try:
            meta = json.loads((path / "metadata.json").read_text())
            if meta.get("desired_version") != version or meta.get("artifact") != artifact["id"]:
                return False
            staged_entries = []
            for logical, _source in source_entries(artifact, self.repo):
                target = path / "files" / logical
                if target.is_symlink() or not target.is_file():
                    return False
                staged_entries.append((logical, target))
            return digest_entries(staged_entries, artifact) == version
        except (OSError, json.JSONDecodeError, OperationError):
            return False

    def _canonical_entries(self, artifact: dict[str, Any]) -> list[tuple[str, Path]]:
        return source_entries(artifact, self.canonical_repo, canonical=True)

    def _installed_source_version(self, artifact: dict[str, Any]) -> str | None:
        try:
            return digest_entries(self._canonical_entries(artifact), artifact)
        except OperationError:
            return None

    def _skill_status(self, artifact: dict[str, Any], harness: str) -> tuple[str | None, bool, str]:
        root = expand_path(artifact["target_roots"][harness], self.env)
        desired_skills = skill_sources(self.canonical_repo)
        if not desired_skills:
            return None, False, "no desired skills"
        for name, expected in sorted(desired_skills.items()):
            target = root / name
            if not target.exists() and not target.is_symlink():
                return None, False, f"missing link {target}"
            if not target.is_symlink() or target.resolve() != expected:
                return None, False, f"drifted skill entry {target}"
        # Publication is the link topology; desired content identity remains
        # the version-controlled manifest digest. The model loads body bytes
        # only on a later invocation and that is intentionally unobserved.
        return desired_version(artifact, self.repo), True, f"{len(desired_skills)} skill links published"

    def _copy_status(self, artifact: dict[str, Any]) -> tuple[str | None, bool, float | None, str]:
        target = expand_path(artifact["target"], self.env)
        if not target.exists():
            return None, False, None, f"destination absent: {target}"
        if target.is_symlink() or not target.is_file():
            return None, False, None, f"destination is not a regular file: {target}"
        logical = artifact.get("copy_source") or artifact["source"][0]
        entries: list[tuple[str, Path]] = []
        for source in artifact["source"]:
            rel = safe_relative(source)
            path = target if source == logical else artifact_root(artifact, self.canonical_repo, canonical=True) / rel
            if not path.is_file():
                return None, False, None, f"installed dependency absent: {path}"
            entries.append((rel.as_posix(), path))
        return digest_entries(entries, artifact), True, target.stat().st_mtime, f"published at {target}"

    def _claude_hook_status(self, artifact: dict[str, Any]) -> tuple[str | None, bool, bool, float | None, str]:
        path = expand_path(artifact["config"], self.env)
        if not path.exists():
            return None, False, True, None, f"config absent: {path}"
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise OperationError(f"cannot parse Claude hooks: {exc}") from exc
        found = []
        conflicts = []
        for event in artifact["events"]:
            exact = 0
            for entry in data.get("hooks", {}).get(event, []):
                if not isinstance(entry, dict):
                    continue
                for hook in entry.get("hooks", []):
                    command = hook.get("command", "") if isinstance(hook, dict) else ""
                    if command == artifact["command"] and hook.get("type") == "command":
                        exact += 1
                    elif "orc-turn-report.py" in command:
                        conflicts.append(f"{event}:{command}")
            found.append(exact)
        if conflicts or any(n > 1 for n in found):
            return None, False, True, path.stat().st_mtime, "conflicting or duplicate Claude hook"
        if not all(n == 1 for n in found):
            return None, False, True, path.stat().st_mtime, "required Claude hook missing"
        version = self._installed_source_version(artifact)
        return version, True, True, path.stat().st_mtime, "exact Claude hooks installed"

    def _codex_hook_status(self, artifact: dict[str, Any]) -> tuple[str | None, bool, bool, float | None, str]:
        path = expand_path(artifact["config"], self.env)
        if not path.exists():
            return None, False, False, None, f"config absent: {path}"
        try:
            data = tomllib.loads(path.read_text())
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise OperationError(f"cannot parse Codex hooks: {exc}") from exc
        hooks = data.get("hooks", {})
        state = hooks.get("state", {}) if isinstance(hooks, dict) else {}
        trusted = True
        exact_all = True
        for event in artifact["events"]:
            entries = hooks.get(event, []) if isinstance(hooks, dict) else []
            matches: list[tuple[int, int]] = []
            conflicts = []
            for i, entry in enumerate(entries if isinstance(entries, list) else []):
                for j, hook in enumerate(entry.get("hooks", []) if isinstance(entry, dict) else []):
                    command = hook.get("command", "") if isinstance(hook, dict) else ""
                    if command == artifact["command"] and hook.get("type") == "command":
                        matches.append((i, j))
                    elif Path(shlex.split(artifact["command"])[1]).name in command:
                        conflicts.append(command)
            if conflicts or len(matches) != 1:
                exact_all = False
                trusted = False
                continue
            i, j = matches[0]
            key = f"{path}:{event.lower().replace('userpromptsubmit', 'user_prompt_submit')}:{i}:{j}"
            trust_row = state.get(key, {}) if isinstance(state, dict) else {}
            if not isinstance(trust_row, dict) or not trust_row.get("trusted_hash") or trust_row.get("enabled") is False:
                trusted = False
        if not exact_all:
            return None, False, trusted, path.stat().st_mtime, "required Codex hook missing or conflicting"
        version = self._installed_source_version(artifact)
        return version, True, trusted, path.stat().st_mtime, "exact Codex hook entries installed"

    def _presence(self, seat: Seat, harness: str, after: float = 0) -> tuple[int | None, str]:
        db = self.ledger_db
        if not db.exists():
            return None, "ledger absent"
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM seat_presence WHERE seat=? AND harness=?",
                               (seat.agent_id, harness)).fetchone()
            conn.close()
        except sqlite3.Error:
            return None, "seat_presence unavailable"
        if not row:
            return None, "no seat_presence observation"
        if row["at_ms"] < after:
            return row["at_ms"], "seat_presence predates installed/active artifact"
        return row["at_ms"], f"seat_presence {row['kind']} starts={row['starts']} ends={row['ends']}"

    def _dsh_profile(self, seat: Seat, process: ProcessInfo | None) -> str | None:
        return process.profile if process and process.profile else seat.profile

    def _dsh_plugin_status(self, artifact: dict[str, Any], profile: str | None) -> tuple[str | None, bool, float | None, str]:
        if not profile:
            return None, False, None, "cannot resolve DSH profile from running seat"
        root = expand_path("${DSH_HOME}", self.env) / "profiles" / profile
        target = root / "node_modules" / artifact["package"]
        if not target.exists():
            return None, False, None, f"package absent from user profile {profile}"
        if target.is_symlink() or not target.is_dir():
            return None, False, None, f"package target is not a regular directory: {target}"
        entries = []
        prefix = safe_relative(artifact["source"][0])
        for child in sorted(target.rglob("*")):
            if child.is_symlink():
                return None, False, None, f"package contains symlink: {child}"
            if child.is_file():
                logical = (prefix / child.relative_to(target)).as_posix()
                entries.append((logical, child))
        return digest_entries(entries, artifact), True, max((p.stat().st_mtime for _, p in entries), default=target.stat().st_mtime), f"package installed in profile {profile}"

    def _dsh_composition_status(self, artifact: dict[str, Any], profile: str | None) -> tuple[str | None, bool, float | None, str]:
        if not profile:
            return None, False, None, "cannot resolve DSH profile from running seat"
        root = expand_path("${DSH_HOME}", self.env) / "profiles" / profile
        package = root / "package.json"
        row = artifact["required_row"]
        if not package.exists():
            return None, False, None, f"user profile {profile} package.json absent"
        try:
            data = json.loads(package.read_text())
        except (OSError, json.JSONDecodeError):
            return None, False, package.stat().st_mtime, f"user profile {profile} package.json unreadable"
        bundles = data.get("dsh", {}).get("profile", {}).get("bundles", [])
        if row not in bundles:
            return None, False, package.stat().st_mtime, f"bundle {row} absent from user profile {profile} bundles"
        # This proves profile composition selection only. The exact package
        # bytes and running activation remain separate artifacts/evidence.
        return desired_version(artifact, self.repo), True, package.stat().st_mtime, f"bundle {row} selected by user profile {profile}"

    def probe(self, artifact: dict[str, Any], harness: str, seat: Seat) -> dict[str, Any]:
        stamp = utc()
        process = self._process_for(seat) if seat.window else None
        if seat.harness == "dsh" and process:
            seat.profile = process.profile
        result: dict[str, Any] = {
            "schema": "orc-rollout/status-v1",
            "artifact": artifact["id"], "class": artifact["class"],
            "harness": harness, "seat": seat.agent_id,
            "handle": seat.handle, "state": "FAILED",
            "target_state": artifact["target_state"],
            "desired_version": None, "installed_version": None,
            "activated_version": None, "observed_version": None,
            "trust": {"required": bool(artifact.get("trust_required")), "status": "not-applicable", "evidence": ""},
            "process": process.view() if process else None,
            "session": process.session if process else (seat.tmux or None),
            "pane": process.pane if process else None,
            "profile": process.profile if process else None,
            "content_published": False, "context_loaded": None,
            "evidence_timestamp": stamp, "detail": "",
        }
        try:
            desired = desired_version(artifact, self.repo)
            result["desired_version"] = desired
            staged = self._staged(artifact, harness, seat, desired)
            installed = False
            installed_mtime: float | None = None
            detail = ""
            trust_granted = True
            cls = artifact["class"]
            if cls == "hooks" and seat.agent_id == "machine":
                if artifact["format"] == "claude-json":
                    installed_version, installed, trust_granted, installed_mtime, detail = self._claude_hook_status(artifact)
                else:
                    installed_version, installed, trust_granted, installed_mtime, detail = self._codex_hook_status(artifact)
                result["installed_version"] = installed_version
                result["trust"] = {
                    "required": bool(artifact.get("trust_required")),
                    "status": "granted" if trust_granted else "blocked",
                    "evidence": "harness-owned trust row" if artifact.get("trust_required") else "not applicable",
                }
                if installed and artifact.get("trust_required") and not trust_granted:
                    result.update(state="BLOCKED_TRUST", content_published=True,
                                  detail=detail + "; trust not granted")
                elif installed and installed_version == desired:
                    result.update(state="INSTALLED", content_published=True,
                                  detail=detail + "; process activation is a separate artifact")
                elif installed:
                    result.update(state="DRIFTED", content_published=True,
                                  detail=f"installed version differs from desired; {detail}")
                else:
                    result.update(state="STAGED" if staged else "ABSENT", detail=detail)
                return result
            if cls in {"published-file", "opencode-plugin"} \
                    and seat.agent_id == "machine":
                installed_version, installed, installed_mtime, detail = self._copy_status(artifact)
                result["installed_version"] = installed_version
                result["context_loaded"] = None
                if installed and installed_version != desired:
                    result.update(state="DRIFTED", content_published=True,
                                  detail=f"installed version differs from desired; {detail}")
                elif installed:
                    result.update(state="INSTALLED", content_published=True,
                                  detail=detail + ("; running service source is checked separately"
                                                   if cls == "published-file"
                                                   else "; process activation is a separate artifact"))
                else:
                    result.update(state="STAGED" if staged else "ABSENT", detail=detail)
                return result
            if cls == "reexec":
                try:
                    installed_entries = self._canonical_entries(artifact)
                    installed_version = digest_entries(installed_entries, artifact)
                    installed_mtime_ns = max(
                        path.stat().st_mtime_ns
                        for _logical, path in installed_entries)
                except OperationError:
                    installed_version = None
                    installed_mtime_ns = None
                installed = installed_version is not None
                result["installed_version"] = installed_version
                crontab = self.env.get("ROLLOUT_CRONTAB_TEXT")
                if crontab is None:
                    run = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=5)
                    crontab = run.stdout if run.returncode == 0 else ""
                log = expand_path(artifact["observation_log"], self.env)
                # Exact-line assertion, never substring: a live line with the
                # same invocation but a changed prefix/redirect (e.g.
                # NW_TMUX_SERVER=... injected ahead of the command) is exactly
                # the drift this row exists to catch, and a substring passes it.
                if "cron_exact_line_command" in artifact:
                    query = [expand_vars(arg, self.env) for arg in artifact["cron_exact_line_command"]]
                    try:
                        command = subprocess.run(query, env=self.env, capture_output=True, text=True,
                                                 timeout=30, check=True)
                    except (OSError, subprocess.SubprocessError):
                        raise OperationError("cannot read configured cron expectation") from None
                    expected = command.stdout.rstrip("\n")
                    if not expected or any(char in expected for char in "\r\n\0"):
                        raise OperationError("configured cron expectation is not one line")
                else:
                    expected = expand_vars(artifact["cron_exact_line"], self.env)
                cron_lines = [ln.strip() for ln in crontab.splitlines()
                              if ln.strip() and not ln.strip().startswith("#")]
                cron_ok = expected in cron_lines
                near_miss = ""
                if not cron_ok:
                    script_token = next((tok for tok in expected.split() if "/" in tok), "")
                    near_miss = next((ln for ln in cron_lines
                                      if script_token and script_token in ln), "")
                if cron_ok:
                    cron_detail = f"cron=exact; line={expected}"
                elif near_miss:
                    cron_detail = f"cron=drifted; line={near_miss}; expected={expected}"
                else:
                    cron_detail = "cron=missing"
                process_started_ns, completed_ns = reexec_receipt(log)
                age_ns = (time.time_ns() - completed_ns
                          if completed_ns is not None else None)
                valid_interval = (process_started_ns is not None
                                  and completed_ns is not None
                                  and completed_ns >= process_started_ns)
                fresh = (valid_interval and age_ns is not None and 0 <= age_ns
                         <= artifact["max_observation_age_s"] * 1_000_000_000)
                observed_current = (fresh and process_started_ns is not None
                                    and installed_mtime_ns is not None
                                    and process_started_ns > installed_mtime_ns)
                if observed_current:
                    observation = "fresh-after-source"
                elif process_started_ns is None or completed_ns is None:
                    observation = "receipt-absent"
                elif not valid_interval:
                    observation = "invalid-interval"
                elif age_ns is not None and age_ns < 0:
                    observation = "future"
                elif not fresh:
                    observation = "stale"
                else:
                    observation = "not-after-source"
                detail = f"{cron_detail}; observation={observation}"
                if installed and installed_version == desired and cron_ok and observed_current:
                    result.update(state="VERIFIED", activated_version=installed_version, observed_version=installed_version,
                                  content_published=True, context_loaded=True, detail=detail)
                    return result
                if installed and installed_version == desired and cron_ok:
                    result.update(state="ACTIVATION_REQUIRED",
                                  content_published=True, context_loaded=False,
                                  detail=detail)
                    return result
                if near_miss:
                    result.update(state="DRIFTED", content_published=installed, detail=detail)
                    return result
                if installed and installed_version == desired:
                    result.update(state="ACTIVATION_REQUIRED",
                                  content_published=True, context_loaded=False,
                                  detail=detail)
                    return result
            elif cls == "skill-links":
                installed_version, installed, detail = self._skill_status(artifact, harness)
                result["installed_version"] = installed_version
                result["context_loaded"] = None
            elif cls == "opencode-plugin":
                installed_version, installed, installed_mtime, detail = self._copy_status(artifact)
                result["installed_version"] = installed_version
                result["context_loaded"] = False
            elif cls == "hooks":
                if artifact["format"] == "claude-json":
                    installed_version, installed, trust_granted, installed_mtime, detail = self._claude_hook_status(artifact)
                else:
                    installed_version, installed, trust_granted, installed_mtime, detail = self._codex_hook_status(artifact)
                result["installed_version"] = installed_version
                result["trust"] = {
                    "required": bool(artifact.get("trust_required")),
                    "status": "granted" if trust_granted else "blocked",
                    "evidence": "harness-owned trust row" if artifact.get("trust_required") else "not applicable",
                }
                if artifact.get("trust_required") and installed and not trust_granted:
                    result.update(state="BLOCKED_TRUST", content_published=True, detail=detail + "; trust not granted")
                    return result
            elif cls == "dsh-plugin":
                profile = self._dsh_profile(seat, process)
                result["profile"] = profile
                installed_version, installed, installed_mtime, detail = self._dsh_plugin_status(artifact, profile)
                result["installed_version"] = installed_version
            elif cls == "dsh-composition":
                profile = self._dsh_profile(seat, process)
                result["profile"] = profile
                installed_version, installed, installed_mtime, detail = self._dsh_composition_status(artifact, profile)
                result["installed_version"] = installed_version
            else:
                raise OperationError(f"no probe adapter for {cls}")

            if installed and result["installed_version"] != desired:
                result.update(state="DRIFTED", content_published=True,
                              detail=f"installed version differs from desired; {detail}")
                return result
            if not installed:
                result.update(state="STAGED" if staged else "ABSENT", detail=detail)
                return result
            result["content_published"] = True
            activation = artifact["activation"]
            if activation == "invocation":
                result.update(state="INSTALLED", detail=detail + "; model-context loading is invocation-time and unobserved")
                return result
            if cls == "dsh-plugin":
                result.update(state="INSTALLED", detail=detail + "; composition is a separate artifact")
                return result
            if process is None:
                result.update(state="ACTIVATION_REQUIRED", detail=detail + "; no matching running process")
                return result
            activated = installed_mtime is None or process.started >= installed_mtime
            observed_at = None
            observed_detail = ""
            if artifact.get("observer") == "seat-presence":
                observed_at, observed_detail = self._presence(seat, harness, max(installed_mtime or 0, process.started))
                if observed_at and observed_at >= max(installed_mtime or 0, process.started):
                    result.update(state="ACTIVE_UNVERIFIED", activated_version=desired,
                                  context_loaded=True,
                                  detail=detail + "; " + observed_detail
                                  + "; observation is not artifact-versioned")
                    return result
            elif artifact.get("observer") == "agent-bus-watcher":
                watcher = self._find_process(self.canonical_repo / "scripts/agent-bus-v3.py", seat.agent_id)
                if watcher and activated:
                    result.update(state="ACTIVE_UNVERIFIED", activated_version=desired,
                                  context_loaded=True,
                                  detail=detail + "; plugin process and supervised watcher observed; no processed-ACK round trip")
                    return result
            if not activated:
                result.update(state="ACTIVATION_REQUIRED", context_loaded=False,
                              detail=detail + "; current process predates installed bytes")
            else:
                result.update(state="ACTIVE_UNVERIFIED", activated_version=desired,
                              context_loaded=True,
                              detail=detail + ("; " + observed_detail if observed_detail else "; no behavior proof"))
            return result
        except OperationError as exc:
            # Honest cannot-read, distinct from FAILED: FAILED means the probe
            # ran and refuted; UNKNOWN means the sensor itself could not read.
            result.update(state="UNKNOWN", detail=f"cannot read honestly: {str(exc)[:280]}")
            return result
        except Exception as exc:
            result.update(state="FAILED", detail=str(exc)[:300])
            return result

    def _unresolved_record(self, artifact: dict[str, Any], harness: str, reason: str) -> dict[str, Any]:
        """One honest UNKNOWN row when target seats cannot be enumerated.

        Only artifacts whose seat resolution needs the bus registry reach
        this (machine/cron/dispatcher sentinels never read it), so a broken
        registry marks exactly the member-needing rows and nothing else."""
        return {
            "schema": "orc-rollout/status-v1",
            "artifact": artifact["id"], "class": artifact["class"],
            "harness": harness, "seat": "unresolved", "handle": "unresolved",
            "state": "UNKNOWN", "target_state": artifact["target_state"],
            "desired_version": None, "installed_version": None,
            "activated_version": None, "observed_version": None,
            "trust": {"required": bool(artifact.get("trust_required")), "status": "not-applicable", "evidence": ""},
            "process": None, "session": None, "pane": None, "profile": None,
            "content_published": False, "context_loaded": None,
            "evidence_timestamp": utc(),
            "detail": f"cannot enumerate target seats: {reason[:260]}",
        }

    def records(self, artifact_filter: str | None = None, harness_filter: str | None = None,
                seat_filter: str | None = None) -> list[dict[str, Any]]:
        records = []
        for artifact in self.manifest["artifacts"]:
            if artifact_filter and artifact["id"] != artifact_filter:
                continue
            try:
                pairs = self.targets(artifact)
            except OperationError as exc:
                # One broken sensor must not silence the whole truth layer:
                # every other artifact still reports, and this one reports
                # UNKNOWN instead of vanishing.
                if harness_filter and artifact.get("harness") not in (harness_filter, "from-seat"):
                    continue
                if seat_filter:
                    continue
                records.append(self._unresolved_record(artifact, artifact.get("harness", ""), str(exc)))
                continue
            for harness, seat in pairs:
                names = {seat.agent_id, seat.handle, *seat.aliases, f"tmux{seat.window}" if seat.window else ""}
                if harness_filter and harness != harness_filter:
                    continue
                if seat_filter and seat_filter not in names:
                    continue
                records.append(self.probe(artifact, harness, seat))
        return records

    def summary(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        counts = {state: 0 for state in STATES}
        for record in records:
            counts[record["state"]] += 1
        return {
            "schema": "orc-rollout/summary-v1",
            "records": len(records),
            "counts": {key: value for key, value in counts.items() if value},
            "needs_activation": counts["ACTIVATION_REQUIRED"],
            "blocked_trust": counts["BLOCKED_TRUST"],
            "drifted": counts["DRIFTED"],
            "failed": counts["FAILED"],
            "unknown": counts["UNKNOWN"],
            "evidence_timestamp": utc(),
        }

    def stage(self, artifact: dict[str, Any], harness: str, seat: Seat) -> dict[str, Any]:
        version = desired_version(artifact, self.repo)
        dest = self._stage_dir(artifact, harness, seat, version)
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(tempfile.mkdtemp(prefix=".stage-", dir=dest.parent))
            try:
                files = tmp / "files"
                for logical, source in source_entries(artifact, self.repo):
                    target = files / logical
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                meta = {
                    "schema": "orc-rollout/stage-v1", "artifact": artifact["id"],
                    "harness": harness, "seat": seat.agent_id,
                    "desired_version": version,
                }
                (tmp / "metadata.json").write_text(json.dumps(meta, sort_keys=True) + "\n")
                os.rename(tmp, dest)
            except Exception:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
        return self.probe(artifact, harness, seat)

    def _require_canonical_merged(self, artifact: dict[str, Any]) -> None:
        desired = desired_version(artifact, self.repo)
        canonical = desired_version(artifact, self.canonical_repo, canonical=True)
        if desired != canonical:
            raise OperationError("refusing permanent install from an unmerged worktree; merge reviewed bytes into the canonical checkout first")

    @staticmethod
    def _atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(name, mode)
            os.replace(name, path)
        except Exception:
            try:
                os.unlink(name)
            except OSError:
                pass
            raise

    def install(self, artifact: dict[str, Any], harness: str, seat: Seat) -> dict[str, Any]:
        desired = desired_version(artifact, self.repo)
        if not self._staged(artifact, harness, seat, desired):
            raise OperationError("install requires an exact staged artifact first")
        self._require_canonical_merged(artifact)
        current = self.probe(artifact, harness, seat)
        if current["state"] in {"DRIFTED", "FAILED"}:
            raise OperationError(f"refusing to overwrite {current['state'].lower()} target: {current['detail']}")
        mode = artifact["install"]
        if mode in {"merge-only", "status-only"}:
            raise OperationError(f"{artifact['id']} is status-only; activation/restart/trust remains operator-owned")
        if mode == "copy-file":
            source = artifact_root(artifact, self.canonical_repo, canonical=True) / safe_relative(artifact.get("copy_source") or artifact["source"][0])
            target = expand_path(artifact["target"], self.env)
            source_bytes = source.read_bytes()
            if target.exists():
                if target.read_bytes() != source_bytes:
                    raise OperationError(f"destination drifted; refusing overwrite: {target}")
                return self.probe(artifact, harness, seat)  # exact install: true no-op
            self._atomic_write(target, source_bytes, source.stat().st_mode & 0o777)
        elif mode == "symlink-tree":
            root = expand_path(artifact["target_roots"][harness], self.env)
            root.mkdir(parents=True, exist_ok=True)
            for name, bundle in sorted(skill_sources(self.canonical_repo).items()):
                target = root / name
                if target.exists() and not target.is_symlink():
                    raise OperationError(f"refusing to overwrite real skill entry: {target}")
                if target.is_symlink() and target.resolve() != bundle.resolve():
                    raise OperationError(f"refusing to replace drifted skill link: {target}")
                if not target.exists() and not target.is_symlink():
                    target.symlink_to(bundle)
        elif mode == "claude-json-hooks":
            path = expand_path(artifact["config"], self.env)
            data = json.loads(path.read_text()) if path.exists() else {}
            hooks = data.setdefault("hooks", {})
            for event in artifact["events"]:
                entries = hooks.setdefault(event, [])
                commands = [h.get("command", "") for e in entries if isinstance(e, dict)
                            for h in e.get("hooks", []) if isinstance(h, dict)]
                if artifact["command"] not in commands:
                    if any("orc-turn-report.py" in cmd for cmd in commands):
                        raise OperationError(f"conflicting Claude hook for {event}")
                    entries.append({"matcher": "", "hooks": [{
                        "type": "command", "command": artifact["command"], "timeout": 5,
                    }]})
            existing_mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
            self._atomic_write(path, (json.dumps(data, indent=2) + "\n").encode(), existing_mode)
        elif mode == "codex-stage-hooks":
            path = expand_path(artifact["config"], self.env)
            text = path.read_text() if path.exists() else ""
            parsed = tomllib.loads(text) if text else {}
            hooks = parsed.get("hooks", {})
            missing = []
            for event in artifact["events"]:
                found = any(h.get("command") == artifact["command"]
                            for e in hooks.get(event, []) if isinstance(e, dict)
                            for h in e.get("hooks", []) if isinstance(h, dict))
                if not found:
                    missing.append(event)
            if missing:
                append = []
                for event in missing:
                    append += ["", f"[[hooks.{event}]]", f"[[hooks.{event}.hooks]]",
                               'type = "command"', f'command = {json.dumps(artifact["command"])}',
                               "timeout = 5"]
                existing_mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
                self._atomic_write(path, (text.rstrip() + "\n" + "\n".join(append) + "\n").encode(), existing_mode)
        elif mode == "dsh-profile-plugin":
            raise OperationError(
                "DSH bundle installation is deliberately status-only: use the reviewed DSH profile bundle command,"
                " then edit only the user-owned profile composition and restart separately"
            )
        else:
            raise OperationError(f"unsupported install mode: {mode}")
        return self.probe(artifact, harness, seat)


def print_records(records: list[dict[str, Any]], as_json: bool) -> None:
    if as_json:
        for record in records:
            print(json.dumps(record, sort_keys=True, separators=(",", ":")))
        return
    if not records:
        print("OK no matching rollout targets")
        return
    for record in records:
        process = f" pid={record['process']['pid']}" if record.get("process") else ""
        profile = f" profile={record['profile']}" if record.get("profile") else ""
        print(f"{record['state']:<20} {record['artifact']} x {record['harness']} x {record['seat']}"
              f"{process}{profile} — {record['detail']}")


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    sub = ap.add_subparsers(dest="verb", required=True)
    sub.add_parser("validate")
    status = sub.add_parser("status")
    status.add_argument("--artifact")
    status.add_argument("--harness")
    status.add_argument("--seat")
    status.add_argument("--summary", action="store_true")
    status.add_argument("--json", action="store_true")
    for name in ("stage", "install", "verify"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--artifact", required=True)
        cmd.add_argument("--harness", required=True)
        cmd.add_argument("--seat", required=True)
        cmd.add_argument("--json", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        manifest = read_manifest(args.manifest)
        if args.verb == "validate":
            print(f"OK {args.manifest}: {len(manifest['artifacts'])} artifact definitions, schema orc-rollout/v1")
            return 0
        control = ControlPlane(manifest)
        if args.verb == "status":
            records = control.records(args.artifact, args.harness, args.seat)
            if args.summary:
                summary = control.summary(records)
                print(json.dumps(summary, sort_keys=True, separators=(",", ":"))
                      if args.json else
                      "rollout " + " ".join(f"{k.lower()}={v}" for k, v in summary["counts"].items()))
            else:
                print_records(records, args.json)
            # UNKNOWN is not a failure (nothing was refuted) but it must not
            # exit clean either: a status run that could not read part of the
            # truth layer has to be visible in automation, with the state
            # string carrying the failed-vs-unreadable distinction.
            return 3 if any(r["state"] in FAILURE_STATES or r["state"] == "UNKNOWN"
                            for r in records) else 0
        artifact, harness, seat = control.select(args.artifact, args.harness, args.seat)
        if args.verb == "stage":
            record = control.stage(artifact, harness, seat)
        elif args.verb == "install":
            record = control.install(artifact, harness, seat)
        else:
            record = control.probe(artifact, harness, seat)
        print_records([record], args.json)
        if args.verb == "verify":
            return 0 if record["state"] in SUCCESS_FOR_TARGET[artifact["target_state"]] else 3
        return 3 if record["state"] in FAILURE_STATES else 0
    except (ManifestError, OperationError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
