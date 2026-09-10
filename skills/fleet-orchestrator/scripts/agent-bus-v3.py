#!/usr/bin/env python3
"""Durable Agent Bus v3 adapter over Matrix or one machine-local SQLite file.

SQLite always owns identities, inbox/outbox state, dedup, delivery leases, and
receive cursors.  Matrix is an optional explicitly configured cross-host transport.  The local
transport deletes that remote hop: a send atomically writes the recipient's
existing SQLite inbox and the sender's delivery state in the same database.
Presentation remains at-least-once; side effects must still deduplicate on
msg_id.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import re
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import runtime_config as cfg  # noqa: E402
import tmux_runtime  # noqa: E402

HS = os.environ.get("MATRIX_BUS_HS", str(cfg.get("matrix.homeserver", "")))
ROOM = os.environ.get("MATRIX_BUS_ROOM", str(cfg.get("matrix.room", "")))
REGISTRY_ROOM = os.environ.get("MATRIX_BUS_REGISTRY_ROOM", str(cfg.get("matrix.registry_room", "")))
CFG = Path(
    os.environ.get("AGENT_BUS_CFG") or os.environ.get("MATRIX_BUS_CFG")
    or cfg.path("bus.config_directory", Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "fleet-orchestrator" / "bus")
)
DB_PATH = Path(os.environ.get("AGENT_BUS_DB") or (
    CFG / "agent-bus-v3.sqlite3"
    if os.environ.get("AGENT_BUS_CFG") or os.environ.get("MATRIX_BUS_CFG")
    else cfg.path("bus.database", CFG / "agent-bus-v3.sqlite3")
))
EVENT_NAMESPACE = str(cfg.get("bus.event_namespace", "org.agent_bus"))
SOURCE_PATH = Path(__file__).resolve()
FLEET_PROFILE_PATH = SOURCE_PATH.parent / "lib" / "fleet-profile.py"
LOADED_SOURCE_FD = globals().get("_AGENT_BUS_SOURCE_FD")
SOURCE_MEMFD_NAME = "agent-bus-v3-source"
# Linux UAPI constants are stable even when a Python build omits their names.
F_ADD_SEALS = getattr(fcntl, "F_ADD_SEALS", 1033)
F_GET_SEALS = getattr(fcntl, "F_GET_SEALS", 1034)
SOURCE_SEALS = 0x0001 | 0x0002 | 0x0004 | 0x0008

SNAPSHOT_LOADER = (
    "import os,sys\n"
    "fd=int(sys.argv[1]);source=sys.argv[2];sys.argv=sys.argv[2:]\n"
    "size=os.fstat(fd).st_size;data=os.pread(fd,size,0)\n"
    "if len(data)!=size: raise RuntimeError('short Agent Bus source snapshot')\n"
    "exec(compile(data,source,'exec'),"
    "{'__name__':'__main__','__file__':source,'__package__':None,"
    "'__cached__':None,'_AGENT_BUS_SOURCE_FD':fd})"
)
MESSAGE_TYPE = f"{EVENT_NAMESPACE}.message.v3"
ACK_TYPE = f"{EVENT_NAMESPACE}.ack.v3"
AGENT_TYPE = f"{EVENT_NAMESPACE}.agent.v3"
LEASE_SECONDS = int(os.environ.get("AGENT_BUS_PRESENTATION_LEASE", "300"))
PRESENT_ATTEMPT_CAP = int(os.environ.get("AGENT_BUS_PRESENT_CAP", "5"))
MEMBER_LEASE_SECONDS = int(os.environ.get("AGENT_BUS_MEMBER_LEASE", "604800"))
PULL_MEMBER_LEASE_SECONDS = int(os.environ.get("AGENT_BUS_PULL_MEMBER_LEASE", "604800"))
SQLITE_LOCK_RETRY_DELAYS = (1.0, 2.0)
HEARTBEAT_OVERDUE_MARGIN_SECONDS = int(os.environ.get("AGENT_BUS_HEARTBEAT_OVERDUE_MARGIN", "3600"))
HEARTBEAT_FAIL_FLAG_THRESHOLD = int(os.environ.get("AGENT_BUS_HEARTBEAT_FAIL_THRESHOLD", "5"))
LOCAL_WATCH_POLL_SECONDS = 0.25


def transport_name() -> str:
    """Return the selected transport; local needs no network account."""
    value = os.environ.get("AGENT_BUS_TRANSPORT", str(cfg.get("bus.transport", "local"))).strip().lower()
    if value not in {"matrix", "local"}:
        raise RuntimeError(
            "AGENT_BUS_TRANSPORT must be 'matrix' or 'local', "
            f"got {value!r}"
        )
    return value


def is_local_transport() -> bool:
    return transport_name() == "local"


def fleet_profile_module():
    """Load the package's single fleet identity implementation."""
    spec = importlib.util.spec_from_file_location(
        "agent_bus_fleet_profile", FLEET_PROFILE_PATH
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load fleet profile resolver: {FLEET_PROFILE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def expected_fleet_environment(name: str) -> dict[str, str]:
    """Resolve a named fleet through the one profile implementation."""
    try:
        module = fleet_profile_module()
        return module.resolve(name, os.environ)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"named fleet {name!r} profile is invalid: {exc}") from exc


def source_bytes(path: Path = SOURCE_PATH) -> bytes:
    """Read one complete published program, never a half-written checkout.

    The canonical script lives in a Git checkout. Read its committed blob so
    a concurrent pull exposes either the old complete program or the new
    complete program, never a prefix visible while Git writes the worktree.
    Test and installed copies without repository metadata use their file bytes.
    """
    resolved = path.resolve()
    try:
        root = subprocess.run(
            ["git", "-C", str(resolved.parent), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if root.returncode:
            return resolved.read_bytes()
        repo = Path(root.stdout.strip())
        relative = resolved.relative_to(repo).as_posix()
        result = subprocess.run(
            ["git", "-C", str(repo), "show", "--no-textconv", f"HEAD:{relative}"],
            capture_output=True, timeout=5,
        )
        if result.returncode:
            # A not-yet-published development file has no committed blob.
            tracked = subprocess.run(
                ["git", "-C", str(repo), "ls-tree", "HEAD", "--", relative],
                capture_output=True, timeout=5,
            )
            if not tracked.returncode and not tracked.stdout:
                return resolved.read_bytes()
            raise RuntimeError("cannot read committed Agent Bus source")
        return result.stdout
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"cannot read Agent Bus source: {exc}") from exc


def source_identity(path: Path = SOURCE_PATH) -> str:
    """Content identity for the complete resident Agent Bus program.

    This reports the adapter source retained by the resident process.
    Configuration values are caller-owned and are not part of this source hash.
    File timestamps are excluded.
    """
    return "sha256:" + hashlib.sha256(source_bytes(path)).hexdigest()


def source_fd_identity(fd: int) -> str:
    """Hash the immutable source snapshot this process actually executes."""
    seals = fcntl.fcntl(fd, F_GET_SEALS)
    if seals & SOURCE_SEALS != SOURCE_SEALS:
        raise RuntimeError("loaded Agent Bus source snapshot is not fully sealed")
    size = os.fstat(fd).st_size
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        chunk = os.pread(fd, min(1024 * 1024, size - offset), offset)
        if not chunk:
            raise RuntimeError("loaded Agent Bus source snapshot ended early")
        digest.update(chunk)
        offset += len(chunk)
    return "sha256:" + digest.hexdigest()


def auto_reexec_enabled() -> bool:
    value = os.environ.get("AGENT_BUS_AUTO_REEXEC", "1").strip().lower()
    return value not in {"0", "false", "no", "off"} \
        and not (CFG / "auto-reexec.disabled").exists()


def source_snapshot(path: Path = SOURCE_PATH) -> tuple[str, int]:
    """Return the identity and inheritable fd of the exact bytes to execute."""
    if not sys.platform.startswith("linux") or not hasattr(os, "memfd_create"):
        raise RuntimeError("resident source snapshots require Linux memfd support")
    data = source_bytes(path)
    compile(data, str(path), "exec")
    fd = os.memfd_create(
        SOURCE_MEMFD_NAME,
        flags=os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING,
    )
    try:
        view = memoryview(data)
        written = 0
        while written < len(view):
            count = os.write(fd, view[written:])
            if count == 0:
                raise OSError("short write while preparing source snapshot")
            written += count
        os.lseek(fd, 0, os.SEEK_SET)
        fcntl.fcntl(
            fd,
            F_ADD_SEALS,
            SOURCE_SEALS,
        )
        os.set_inheritable(fd, True)
    except Exception:
        os.close(fd)
        raise
    return "sha256:" + hashlib.sha256(data).hexdigest(), fd


def resident_exec_argv(snapshot_fd: int) -> list[str]:
    """Execute sealed bytes while preserving only the public command argv."""
    return [
        sys.executable, "-I", "-S", "-c", SNAPSHOT_LOADER, str(snapshot_fd),
        str(SOURCE_PATH), *sys.argv[1:],
    ]


def exec_current_source(identity: str, *, reason: str = "source changed") -> None:
    """Atomically replace this process image, retaining PID, env, and argv."""
    try:
        snapshot_identity, snapshot_fd = source_snapshot()
    except (OSError, SyntaxError, RuntimeError) as exc:
        print(
            f"FATAL agent-bus-v3: cannot prepare source snapshot: {exc}",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(70) from exc
    if snapshot_identity != identity:
        reason += f"; source advanced from {identity}"
        identity = snapshot_identity
    argv = resident_exec_argv(snapshot_fd)
    print(
        f"NOTE agent-bus-v3: {reason}; re-execing as {identity}",
        file=sys.stderr,
        flush=True,
    )
    try:
        if isinstance(LOADED_SOURCE_FD, int):
            # The next image owns exactly one proof fd.  Mark the old snapshot
            # close-on-exec so repeated source changes cannot accumulate stale
            # memfds that an external verifier would have to guess between.
            os.set_inheritable(LOADED_SOURCE_FD, False)
        os.execv(sys.executable, argv)
    except OSError as exc:
        os.close(snapshot_fd)
        print(
            f"FATAL agent-bus-v3: re-exec failed for {identity}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(70) from exc
    os.close(snapshot_fd)
    print(
        f"FATAL agent-bus-v3: re-exec returned unexpectedly for {identity}",
        file=sys.stderr,
        flush=True,
    )
    raise SystemExit(70)


def maybe_reexec(loaded_identity: str) -> None:
    """At a caller-declared idle boundary, adopt changed source or fail loud."""
    if not auto_reexec_enabled():
        return
    try:
        current = source_identity()
    except OSError as exc:
        print(
            f"FATAL agent-bus-v3: cannot identify current source: {exc}",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(70) from exc
    # Source identification can run Git subprocesses. Honor a pause created
    # while that work was in progress before replacing the process image.
    if current != loaded_identity and auto_reexec_enabled():
        exec_current_source(current)


def is_sqlite_lock_contention(exc: BaseException) -> bool:
    """Match only retryable SQLite lock result codes, including extensions."""
    code = getattr(exc, "sqlite_errorcode", None)
    return (
        isinstance(exc, sqlite3.OperationalError)
        and isinstance(code, int)
        and (code & 0xFF) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    )


def retry_sqlite_lock(label: str, operation) -> Any:
    """Retry one atomic SQLite operation within a small, fixed budget."""
    attempts = len(SQLITE_LOCK_RETRY_DELAYS) + 1
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except sqlite3.OperationalError as exc:
            if not is_sqlite_lock_contention(exc):
                raise
            if attempt == attempts:
                print(
                    f"agent-bus-v3: {label} sqlite lock retry exhausted "
                    f"after {attempts} attempts: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                raise
            delay = SQLITE_LOCK_RETRY_DELAYS[attempt - 1]
            print(
                f"agent-bus-v3: {label} sqlite lock retry "
                f"{attempt}/{attempts - 1} in {delay:g}s: {exc}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)


def now_ms() -> int:
    return int(time.time() * 1000)


def iso(ms: int | None = None) -> str:
    value = dt.datetime.fromtimestamp((ms or now_ms()) / 1000, dt.timezone.utc)
    return value.isoformat().replace("+00:00", "Z")


def fail(message: str) -> None:
    raise SystemExit(f"agent-bus-v3: {message}")


def db() -> sqlite3.Connection:
    CFG.mkdir(parents=True, exist_ok=True, mode=0o700)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        _initialize_db(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def local_member_rows() -> list[sqlite3.Row]:
    """Read registration facts without creating or migrating persistent state.

    An unstarted bus has no members. An existing unreadable or incompatible
    database is unavailable, not empty. A single read supplies both identity
    facts and heartbeat diagnostics for the public member snapshot.
    """
    try:
        DB_PATH.stat()
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise RuntimeError(f"Agent Bus members unavailable: {exc}") from exc
    try:
        with closing(sqlite3.connect(
            DB_PATH.resolve().as_uri() + "?mode=ro", uri=True, timeout=10
        )) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            cursor = conn.execute("SELECT * FROM identities ORDER BY agent_id")
            required = {
                "agent_id", "slot", "handle", "aliases_json", "generation", "status",
                "harness", "mode", "host", "tmux", "updated_ms", "lease_until_ms",
                "pane_id", "heartbeat_fails", "heartbeat_last_error",
            }
            if not required.issubset({column[0] for column in cursor.description}):
                raise RuntimeError("Agent Bus members unavailable: incomplete identity schema")
            return cursor.fetchall()
    except (OSError, sqlite3.Error) as exc:
        raise RuntimeError(f"Agent Bus members unavailable: {exc}") from exc


def _initialize_db(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS identities (
          agent_id TEXT PRIMARY KEY, slot TEXT UNIQUE NOT NULL, handle TEXT UNIQUE NOT NULL,
          generation INTEGER NOT NULL, status TEXT NOT NULL, harness TEXT NOT NULL,
          mode TEXT NOT NULL, host TEXT NOT NULL, tmux TEXT NOT NULL,
          aliases_json TEXT NOT NULL DEFAULT '[]', created_ms INTEGER NOT NULL,
          updated_ms INTEGER NOT NULL, lease_until_ms INTEGER
        );
        CREATE TABLE IF NOT EXISTS cursors (
          agent_id TEXT PRIMARY KEY REFERENCES identities(agent_id), token TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS inbox (
          agent_id TEXT NOT NULL REFERENCES identities(agent_id), msg_id TEXT NOT NULL,
          matrix_event_id TEXT, sender_agent_id TEXT NOT NULL, sender_handle TEXT NOT NULL,
          subject TEXT NOT NULL, body TEXT NOT NULL, priority TEXT NOT NULL,
          created_ms INTEGER NOT NULL, expires_ms INTEGER, state TEXT NOT NULL,
          lease_until_ms INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(agent_id,msg_id), UNIQUE(agent_id,matrix_event_id)
        );
        CREATE TABLE IF NOT EXISTS outbox (
          msg_id TEXT PRIMARY KEY, sender_agent_id TEXT NOT NULL, subject TEXT NOT NULL,
          body TEXT NOT NULL, created_ms INTEGER NOT NULL, expires_ms INTEGER,
          matrix_event_id TEXT, transport_state TEXT NOT NULL, last_error TEXT,
          payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS outbox_recipients (
          msg_id TEXT NOT NULL REFERENCES outbox(msg_id), recipient_agent_id TEXT NOT NULL,
          handle_at_send TEXT NOT NULL, delivered_ms INTEGER, processed_ms INTEGER,
          processed_status TEXT, PRIMARY KEY(msg_id,recipient_agent_id)
        );
        CREATE TABLE IF NOT EXISTS ack_outbox (
          msg_id TEXT NOT NULL, from_agent_id TEXT NOT NULL, to_agent_id TEXT NOT NULL,
          stage TEXT NOT NULL, status TEXT NOT NULL, detail TEXT, created_ms INTEGER NOT NULL,
          matrix_event_id TEXT, PRIMARY KEY(msg_id,from_agent_id,stage)
        );
        CREATE TABLE IF NOT EXISTS inbox_signal (
          agent_id TEXT PRIMARY KEY REFERENCES identities(agent_id),
          generation INTEGER NOT NULL DEFAULT 0,
          notified_generation INTEGER NOT NULL DEFAULT 0,
          notified_ms INTEGER
        );
        """
    )
    outbox_columns = {row[1] for row in conn.execute("PRAGMA table_info(outbox)")}
    if "payload_json" not in outbox_columns:
        conn.execute("ALTER TABLE outbox ADD COLUMN payload_json TEXT NOT NULL DEFAULT '{}'")
    identity_columns = {row[1] for row in conn.execute("PRAGMA table_info(identities)")}
    if "heartbeat_fails" not in identity_columns:
        conn.execute("ALTER TABLE identities ADD COLUMN heartbeat_fails INTEGER NOT NULL DEFAULT 0")
    if "heartbeat_last_error" not in identity_columns:
        conn.execute("ALTER TABLE identities ADD COLUMN heartbeat_last_error TEXT")
    if "pane_id" not in identity_columns:
        conn.execute("ALTER TABLE identities ADD COLUMN pane_id TEXT")
    if "tmux_server_id" not in identity_columns:
        conn.execute("ALTER TABLE identities ADD COLUMN tmux_server_id TEXT")
    if "retired_kind" not in identity_columns:
        conn.execute("ALTER TABLE identities ADD COLUMN retired_kind TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS one_active_seat_per_pane"
        " ON identities(host, pane_id)"
        " WHERE status='active' AND pane_id IS NOT NULL")
    # Sender-only cron identities never had a model that could read an
    # inbox.  Remove pre-upgrade unread rows and their not-yet-published
    # delivered acknowledgements; otherwise upgrading the receiver still
    # preserves permanent unread mail and may publish a false delivery fact.
    conn.execute(
        "DELETE FROM inbox WHERE state!='done' AND agent_id IN"
        " (SELECT agent_id FROM identities WHERE lower(harness)='cron')"
    )
    conn.execute(
        "DELETE FROM ack_outbox WHERE stage='delivered'"
        " AND matrix_event_id IS NULL AND from_agent_id IN"
        " (SELECT agent_id FROM identities WHERE lower(harness)='cron')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO inbox_signal(agent_id,generation) "
        "SELECT i.agent_id,CASE WHEN EXISTS(SELECT 1 FROM inbox x WHERE x.agent_id=i.agent_id AND x.state!='done' AND (x.expires_ms IS NULL OR x.expires_ms>?)) THEN 1 ELSE 0 END FROM identities i",
        (now_ms(),),
    )
    conn.commit()


def auth_header_path() -> Path:
    # A selected transport directory owns its credentials as well as its DB.
    # Named profiles set MATRIX_BUS_CFG before this module is imported.
    if os.environ.get("AGENT_BUS_CFG") or os.environ.get("MATRIX_BUS_CFG"):
        return CFG / "auth.hdr"
    return cfg.path("matrix.token_file", CFG / "auth.hdr")


def auth_token() -> str:
    header = auth_header_path()
    if not header or not header.exists():
        fail("Matrix credential file is missing; configure matrix.token_file")
    text = header.read_text().strip()
    if text.lower().startswith("authorization: bearer "):
        text = text.split(None, 2)[2]
    if not text or any(char.isspace() for char in text):
        fail("Matrix credential file must contain one token or bearer header")
    return text


def matrix(method: str, path: str, payload: dict[str, Any] | None = None, timeout: int = 45) -> dict[str, Any]:
    if not HS or not ROOM or not REGISTRY_ROOM:
        raise RuntimeError("Matrix requires an explicit homeserver, message room and registry room")
    url = HS + path
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Authorization": f"Bearer {auth_token()}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        raise RuntimeError(f"Matrix HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError(f"Matrix request uncertain: {exc}") from exc
    if isinstance(result, dict) and result.get("errcode"):
        raise RuntimeError(f"Matrix {result['errcode']}: {result.get('error', '')}")
    return result


def validate_fleet_scope() -> None:
    """A named process must never fall through to another fleet's transport."""
    selected_transport = transport_name()
    name = os.environ.get("NW_FLEET", "").strip()
    if not name or name == "default":
        return
    expected = expected_fleet_environment(name)
    mismatched = []
    for key, value in expected.items():
        if key == "NW_FLEET_PRIMARY_SESSION":
            # The tmux name is derived from the selected durable fleet identity
            # and may change while a watcher remains alive. Validate every
            # durable setting before adopting its current terminal name below.
            continue
        actual = os.environ.get(key)
        # Matrix was the only transport before this field existed, so an
        # already-running schema-1 fleet may legitimately inherit no explicit
        # selector. Absence keeps its historical Matrix meaning.
        if key == "AGENT_BUS_TRANSPORT" and value == "matrix" and not actual:
            actual = "matrix"
        if actual != value:
            mismatched.append(key)
    transport = expected.get("AGENT_BUS_TRANSPORT")
    if transport == "local":
        mismatched.extend(
            key for key in (
                "MATRIX_BUS_CFG", "MATRIX_BUS_HS", "MATRIX_BUS_ROOM",
                "MATRIX_BUS_REGISTRY_ROOM",
            )
            if os.environ.get(key)
        )
    elif transport == "matrix" and os.environ.get("AGENT_BUS_CFG"):
        mismatched.append("AGENT_BUS_CFG")
    if mismatched:
        fields = ", ".join(sorted(set(mismatched)))
        raise RuntimeError(
            f"named fleet {name!r} is not fully resolved ({fields}); use "
            f"matrix-bus.sh --fleet {name} <verb>"
        )
    if "NW_FLEET_PRIMARY_SESSION" in expected:
        os.environ["NW_FLEET_PRIMARY_SESSION"] = expected["NW_FLEET_PRIMARY_SESSION"]


def call_with_deadline(fn, deadline_s: float) -> Any:
    """Run fn() but never block the caller past deadline_s wall-clock seconds.

    urllib's socket timeout is per-operation, so a half-open connection can hang a
    sync long-poll far past its intended timeout. The watch loop runs its 120s
    registry heartbeat inline before the sync, so a hung sync starves the heartbeat
    and the member lease silently expires while the watcher is still up. A hard
    wall-clock deadline bounds that: the abandoned daemon worker unwinds on its own
    socket timeout while the loop proceeds to the next heartbeat.
    """
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised to the caller below
            box["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(deadline_s)
    if worker.is_alive():
        raise RuntimeError(f"call exceeded {deadline_s:.0f}s wall-clock deadline")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def encoded(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def sync(token: str | None, timeout_ms: int) -> dict[str, Any]:
    query = {"timeout": str(timeout_ms)}
    if token:
        query["since"] = token
    return matrix("GET", "/_matrix/client/v3/sync?" + urllib.parse.urlencode(query), timeout=max(45, timeout_ms // 1000 + 10))


def put_event(event_type: str, txn: str, content: dict[str, Any]) -> str:
    result = matrix(
        "PUT",
        f"/_matrix/client/v3/rooms/{encoded(ROOM)}/send/{encoded(event_type)}/{encoded(txn)}",
        content,
    )
    event_id = result.get("event_id")
    if not event_id:
        raise RuntimeError(f"Matrix accepted no event_id: {result}")
    return str(event_id)


def put_state(agent_id: str, content: dict[str, Any]) -> str:
    if is_local_transport():
        # The identities table is the local registry.  State publication would
        # only duplicate the same fact into another local structure.
        return f"local:state:{agent_id}:{content.get('generation', 0)}"
    result = matrix(
        "PUT",
        f"/_matrix/client/v3/rooms/{encoded(REGISTRY_ROOM)}/state/{encoded(AGENT_TYPE)}/{encoded(agent_id)}",
        content,
    )
    event_id = result.get("event_id")
    if not event_id:
        raise RuntimeError(f"Matrix accepted no state event_id: {result}")
    return str(event_id)


@contextmanager
def registry_lock(agent_id: str):
    """Serialize one identity's state writes without holding a SQLite lock."""
    lock_path = CFG / f"registry-{agent_id}.lock"
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def identity(conn: sqlite3.Connection, value: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM identities WHERE agent_id=? OR handle=? OR slot=?", (value, value, value)
    ).fetchone()
    if not row:
        fail(f"unknown local identity: {value}; run join first")
    return row


def state_content(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "schema": "agent-bus/agent/v3", "agent_id": row["agent_id"],
        "handle": row["handle"], "aliases": json.loads(row["aliases_json"]),
        "generation": row["generation"], "status": row["status"],
        "harness": row["harness"], "mode": row["mode"], "host": row["host"],
        "tmux": row["tmux"], "capabilities": ["v3", "delivery-ack", "processed-ack"],
        "pane_id": row["pane_id"],
        "tmux_server_id": row["tmux_server_id"] if "tmux_server_id" in row.keys() else None,
        "updated_at": iso(row["updated_ms"]),
        "lease_until": iso(row["lease_until_ms"]) if row["lease_until_ms"] else None,
    }


def validated_harness(harness: str, mode: str, tmux: str) -> str:
    """Return canonical harness metadata or reject an unwakeable named seat."""
    value = harness.strip()
    canonical = value.lower()
    if not value or canonical == "unknown":
        fail("harness must identify the real runtime; 'unknown' is forbidden")
    if canonical == "codex":
        if mode != "pull":
            fail("harness 'codex' requires mode 'pull'")
        if not tmux.startswith("tmux=") or " win=" not in tmux or tmux.endswith(" win="):
            fail("harness 'codex' requires a concrete tmux pane for pull-notify")
        return canonical
    if canonical in {"claude", "opencode"}:
        if mode != "watch":
            fail(f"harness '{canonical}' requires mode 'watch'")
        if not tmux.startswith("tmux=") or " win=" not in tmux or tmux.endswith(" win="):
            fail(f"harness '{canonical}' requires a concrete tmux pane for its watcher")
        return canonical
    return value


def _join_pane_guard(conn: sqlite3.Connection, slot: str, host: str,
                     pane_id: str | None, tmux: str) -> None:
    """One tmux pane = one ACTIVE seat, enforced at the registry itself so
    every join path (boot script, harness plugins, hand-run CLI) hits it
    The registry check applies to every join path, including harness plugins.
    Match on the stable pane %id when both sides have one; fall back to
    exact location-string equality for pre-migration rows."""
    if not tmux.startswith("tmux="):
        return
    for row in conn.execute(
            "SELECT * FROM identities WHERE status='active' AND host=?"
            " AND slot!=?", (host, slot)):
        same_pane = bool(pane_id and row["pane_id"]
                         and row["pane_id"] == pane_id)
        legacy_same = (not row["pane_id"]) and row["tmux"] == tmux
        if same_pane or legacy_same:
            raise RuntimeError(
                f"this pane already has an ACTIVE seat: {row['handle']}"
                f" (slot={row['slot']}, agent_id={row['agent_id']})."
                f" One pane = one active seat."
                f" Resuming that session? re-run boot with"
                f" AGENT_BUS_SLOT={row['slot']}."
                f" Replacing a dead predecessor? run the sanctioned"
                f" succession first: scripts/orc pane-succession"
                f" (it fail-closes on obligations and retires only a"
                f" provably absent seat), then re-run this join.")


def cmd_join(args: argparse.Namespace) -> None:
    args.harness = validated_harness(args.harness, args.mode, args.tmux)
    fleet_name = os.environ.get("NW_FLEET", "").strip()
    if is_local_transport() and fleet_name and fleet_name != "default":
        profile = fleet_profile_module()
        selected = profile.resolve(fleet_name, os.environ)
        if selected and not selected["NW_FLEET_PROFILE_PATH"]:
            # The first real write records the history key on tmux. Pure member,
            # board and dry-run queries must never create this identity binding.
            profile.bind_local_session(fleet_name, os.environ)
    conn = db()
    pane_id = os.environ.get("TMUX_PANE", "").strip() or None
    server_id = None
    if pane_id and args.tmux.startswith("tmux="):
        try:
            pane = tmux_runtime.pane_snapshot().get(pane_id)
            if pane and not pane["dead"]:
                server_id = pane["server_id"]
        except RuntimeError:
            # Registration and durable messages remain valid without terminal
            # visibility. Such a registration cannot be used for tmux routing.
            pass
    current = conn.execute("SELECT * FROM identities WHERE slot=?", (args.slot,)).fetchone()
    agent_id = current["agent_id"] if current else str(uuid.uuid4())
    with registry_lock(agent_id):
        current = conn.execute("SELECT * FROM identities WHERE slot=?", (args.slot,)).fetchone()
        if current and current["agent_id"] != agent_id:
            raise RuntimeError("slot was concurrently registered; retry join")
        if (current and current["status"] == "retired"
                and current["retired_kind"] == "checkout"
                and os.environ.get("AGENT_BUS_REVIVE_CHECKEDOUT") != "1"):
            raise RuntimeError(
                f"slot {args.slot} was retired BY CHECKOUT and stays"
                f" retired; boot a fresh task-slug for new work."
                f" Operator override: AGENT_BUS_REVIVE_CHECKEDOUT=1")
        _join_pane_guard(conn, args.slot, args.host, pane_id, args.tmux)
        cursor_token = None
        if not conn.execute("SELECT 1 FROM cursors WHERE agent_id=?", (agent_id,)).fetchone():
            if is_local_transport():
                # The local watcher compares this durable value with
                # inbox_signal.generation. Starting at zero makes mail written
                # before the watcher starts visible on its first pass.
                cursor_token = "local:0"
            else:
                cursor_token = sync(None, 0).get("next_batch")
                if not cursor_token:
                    fail("Matrix sync returned no next_batch")
        stamp = now_ms()
        lease_seconds = MEMBER_LEASE_SECONDS if args.mode == "watch" else PULL_MEMBER_LEASE_SECONDS
        try:
            if current:
                aliases = json.loads(current["aliases_json"])
                generation = current["generation"]
                if current["handle"] != args.handle:
                    aliases = ([current["handle"]] + aliases)[:5]
                    generation += 1
                conn.execute(
                    "UPDATE identities SET handle=?,generation=?,status='active',harness=?,mode=?,host=?,tmux=?,pane_id=?,tmux_server_id=?,retired_kind=NULL,aliases_json=?,updated_ms=?,lease_until_ms=? WHERE agent_id=?",
                    (args.handle, generation, args.harness, args.mode, args.host, args.tmux,
                     pane_id, server_id, json.dumps(aliases), stamp, stamp + lease_seconds * 1000, current["agent_id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO identities(agent_id,slot,handle,generation,status,harness,mode,"
                    "host,tmux,pane_id,tmux_server_id,aliases_json,created_ms,updated_ms,lease_until_ms) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (agent_id, args.slot, args.handle, 1, "active", args.harness, args.mode,
                     args.host, args.tmux, pane_id, server_id, "[]", stamp, stamp, stamp + lease_seconds * 1000),
                )
        except sqlite3.IntegrityError as exc:
            # round 2 (tmux3 barrier repro): two concurrent joins both
            # passed the guard's SELECT under their per-agent locks. The
            # DATABASE is the final arbiter - the race loser gets the same
            # refusal the guard gives, and zero rows land.
            conn.rollback()
            if "identities.host, identities.pane_id" in str(exc):
                raise RuntimeError(
                    "this pane already has an ACTIVE seat (a concurrent"
                    " join won the race). One pane = one active seat -"
                    " resume that seat's slot or run scripts/orc"
                    " pane-succession, then re-run this join.") from exc
            raise
        if cursor_token:
            conn.execute("INSERT INTO cursors VALUES(?,?)", (agent_id, cursor_token))
        conn.execute("INSERT OR IGNORE INTO inbox_signal(agent_id) VALUES(?)", (agent_id,))
        conn.commit()
        row = identity(conn, agent_id)
        event_id = put_state(agent_id, state_content(row))
    print(json.dumps({
        "schema": "agent-bus/join-result/v3", "agent_id": row["agent_id"],
        "handle": row["handle"], "slot": row["slot"], "generation": row["generation"],
        "status": row["status"], "harness": row["harness"], "mode": row["mode"],
        "host": row["host"], "tmux": row["tmux"], "event_id": event_id,
    }))


def cmd_retire(args: argparse.Namespace) -> None:
    conn = db(); original = identity(conn, args.identity)
    kind = getattr(args, "kind", None) or "manual"
    with registry_lock(original["agent_id"]):
        row = identity(conn, original["agent_id"]); stamp = now_ms()
        conn.execute("UPDATE identities SET status='retired',retired_kind=?,updated_ms=?,lease_until_ms=NULL WHERE agent_id=?", (kind, stamp, row["agent_id"]))
        swept = conn.execute(
            "UPDATE inbox SET expires_ms=? WHERE agent_id=? AND state!='done'"
            " AND (expires_ms IS NULL OR expires_ms>?)",
            (stamp, row["agent_id"], stamp)).rowcount
        conn.commit(); row = identity(conn, row["agent_id"])
        try:
            put_state(row["agent_id"], state_content(row))
        except RuntimeError as exc:
            print(f"WARN: Matrix state publication failed ({exc});"
                  " local retire and sweep are durable", file=sys.stderr)
    print(f"retired [{kind}] {row['handle']} ({row['agent_id']})"
          + (f"; tombstoned {swept} pending inbox message(s)" if swept else ""))


def room_members() -> list[dict[str, Any]]:
    if is_local_transport():
        return [state_content(row) for row in local_member_rows()]
    result = matrix("GET", f"/_matrix/client/v3/rooms/{encoded(REGISTRY_ROOM)}/state")
    if not isinstance(result, list):
        raise RuntimeError(f"Matrix registry state returned a non-list response: {result}")
    members = []
    for event in result:
        if not isinstance(event, dict):
            continue
        content = event.get("content", {})
        if not isinstance(content, dict):
            continue
        agent_id = content.get("agent_id")
        if (
            event.get("type") == AGENT_TYPE
            and content.get("schema") == "agent-bus/agent/v3"
            and isinstance(agent_id, str)
            and event.get("state_key") == agent_id
        ):
            members.append(content)
    return members


def active_members(members: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Return every active, unexpired registry fact."""
    stamp = dt.datetime.now(dt.timezone.utc)
    active = []
    for member in room_members() if members is None else members:
        try:
            lease = dt.datetime.fromisoformat((member.get("lease_until") or "").replace("Z", "+00:00"))
        except ValueError:
            continue
        if member.get("status") == "active" and lease >= stamp:
            active.append(member)
    return active


def addressable_members(members: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Return model seats that can receive exact or broadcast messages."""
    # Cron identities have no model and never pull presented messages.  They
    # can send and ingest ACKs, but are absent from collaborator listings and
    # must never turn a broadcast into permanent unread mail.
    return [
        member for member in active_members(members)
        if str(member.get("harness", "")).lower() != "cron"
    ]


def resolve_target(target: str, sender_id: str) -> list[dict[str, Any]]:
    if not target.strip():
        fail("target must not be empty")
    members = addressable_members()
    if is_local_transport():
        with closing(db()) as conn:
            sender_host = str(identity(conn, sender_id)["host"])
        members = [member for member in members
                   if str(member.get("host", "")) == sender_host]
    if target in {"all", "@all"}:
        return [m for m in members if m.get("agent_id") != sender_id]
    found = [m for m in members if target in {m.get("agent_id"), m.get("handle")} or target in m.get("aliases", [])]
    if not found:
        # ORC's human-facing short names are unique handle/alias segments.
        # Resolve them here, against the current Agent Bus registry, instead
        # of letting ORC rewrite them through a possibly stale pane cache.
        # Ambiguity still fails closed below.
        segment = re.compile(rf"(^|[/-]){re.escape(target)}([/-]|$)")
        found = [
            member for member in members
            if any(segment.search(name) for name in
                   [str(member.get("handle", "")),
                    *[str(alias) for alias in member.get("aliases", [])]])
        ]
    if len(found) != 1:
        fail(f"target {target!r} resolved to {len(found)} active agents")
    return found


def member_view(member: dict[str, Any], local: dict[str, sqlite3.Row], now: dt.datetime) -> dict[str, Any]:
    """Registration facts plus derived heartbeat health. None of these fields
    is liveness: liveness = a real watch process with the canonical content
    identity, plus a round trip (README → Failure semantics)."""
    view = dict(member)
    view["addressable"] = str(member.get("harness", "")).lower() != "cron"
    view["liveness"] = "unverified — registration facts only; prove with a round trip"
    age_s = None
    try:
        updated = dt.datetime.fromisoformat(str(member.get("updated_at") or "").replace("Z", "+00:00"))
        age_s = max(0, int((now - updated).total_seconds()))
    except ValueError:
        pass
    view["updated_age_s"] = age_s
    if member.get("mode") == "watch" and age_s is not None:
        # Watch seats write as soon as the lease-half throttle opens; pull
        # seats heartbeat only when they pull, so age says nothing about them.
        view["heartbeat_overdue"] = age_s > MEMBER_LEASE_SECONDS // 2 + HEARTBEAT_OVERDUE_MARGIN_SECONDS
    else:
        view["heartbeat_overdue"] = None
    row = local.get(str(member.get("agent_id")))
    # A resume slot belongs to this selected database, never remote metadata.
    view.pop("slot", None)
    if row is not None:
        view["slot"] = row["slot"]
    if row is not None and (row["heartbeat_fails"] or 0) >= HEARTBEAT_FAIL_FLAG_THRESHOLD:
        view["heartbeat_failing"] = row["heartbeat_fails"]
        view["heartbeat_last_error"] = row["heartbeat_last_error"]
    return view


def local_terminal_view(
    view: dict[str, Any], panes: dict[str, dict[str, str | bool]] | None,
) -> dict[str, Any]:
    """Derive routable terminal location without rewriting registration facts.

    A live pane does not establish agent responsiveness. Missing generations
    are deliberately not inferred from a stale location or a recycled pane ID.
    Headless identities retain normal durable-message behavior.
    """
    registered = str(view.get("tmux", ""))
    if not registered.startswith("tmux="):
        return view
    view["registration_tmux"] = registered
    view["tmux"] = ""
    view["terminal_presence"] = "unknown"
    pane_id, server_id = view.get("pane_id"), view.get("tmux_server_id")
    if not pane_id or not server_id:
        view["terminal_detail"] = "registration has no verified tmux server generation; rejoin to bind this terminal"
    elif panes is None:
        view["terminal_detail"] = "tmux observation unavailable"
    else:
        pane = panes.get(str(pane_id))
        if pane is None or pane["server_id"] != server_id or pane["dead"]:
            view["terminal_presence"] = "absent"
            view["terminal_detail"] = "registered pane is absent from the selected session and server generation"
        else:
            view["terminal_presence"] = "present"
            view["tmux"] = "tmux=" + str(pane["location"])
    return view


def observe_member_terminals(members: list[dict[str, Any]]) -> dict[str, dict[str, str | bool]] | None:
    if not any(
        member.get("pane_id") and member.get("tmux_server_id")
        and str(member.get("tmux", "")).startswith("tmux=") for member in members
    ):
        return {}
    try:
        return tmux_runtime.pane_snapshot()
    except RuntimeError:
        return None


def cmd_members(_args: argparse.Namespace) -> None:
    rows = local_member_rows()
    local = {str(row["agent_id"]): row for row in rows}
    members = (
        addressable_members([state_content(row) for row in rows])
        if is_local_transport() else addressable_members()
    )
    panes = observe_member_terminals(members) if is_local_transport() else None
    now = dt.datetime.now(dt.timezone.utc)
    # This command lists collaborators that can receive work. Sender-only
    # service identities remain local for sends and acknowledgements but are
    # not terminal members of the fleet.
    for member in sorted(members, key=lambda m: m.get("handle", "")):
        view = member_view(member, local, now)
        if is_local_transport():
            view = local_terminal_view(view, panes)
        print(json.dumps(view, separators=(",", ":")))


def cmd_heartbeat(args: argparse.Namespace) -> None:
    conn = db(); original = identity(conn, args.identity)
    with registry_lock(original["agent_id"]):
        row = identity(conn, original["agent_id"]); stamp = now_ms()
        lease_seconds = MEMBER_LEASE_SECONDS if row["mode"] == "watch" else PULL_MEMBER_LEASE_SECONDS
        if row["status"] != "active":
            fail(f"identity {row['agent_id']} is retired; rejoin it before heartbeat")
        if row["lease_until_ms"] and row["lease_until_ms"] - stamp > lease_seconds * 1000 // 2:
            return
        lease_until = stamp + lease_seconds * 1000
        content = state_content(row)
        content["updated_at"] = iso(stamp)
        content["lease_until"] = iso(lease_until)
        try:
            put_state(row["agent_id"], content)
        except Exception as exc:
            # A due write that failed. Count it in the local registry so the
            # failure is visible in `members`/`unread`, not only on stderr.
            fails = (row["heartbeat_fails"] or 0) + 1
            conn.execute(
                "UPDATE identities SET heartbeat_fails=?,heartbeat_last_error=? WHERE agent_id=?",
                (fails, str(exc)[:500], row["agent_id"]),
            )
            conn.commit()
            if fails == HEARTBEAT_FAIL_FLAG_THRESHOLD:
                print(
                    f"agent-bus-v3: registry heartbeat DEGRADED for {row['handle']} after "
                    f"{fails} consecutive failed writes — registry writes are not landing "
                    "(transport/account/room problem; NOT proof this seat is broken). "
                    "No automatic action is taken; the flag clears on the next successful write.",
                    file=sys.stderr, flush=True,
                )
            raise
        conn.execute(
            "UPDATE identities SET updated_ms=?,lease_until_ms=?,heartbeat_fails=0,heartbeat_last_error=NULL WHERE agent_id=?",
            (stamp, lease_until, row["agent_id"]),
        )
        conn.commit()


def cmd_registry_migrate(args: argparse.Namespace) -> None:
    bind_local = getattr(args, "bind_local_terminals", False)
    if bind_local and not is_local_transport():
        fail("--bind-local-terminals requires local transport")
    conn = db(); stamp = now_ms()
    rows = conn.execute(
        "SELECT * FROM identities WHERE status='active' AND lease_until_ms>=? ORDER BY agent_id",
        (stamp,),
    ).fetchall()
    if is_local_transport():
        bound = []
        unmatched = []
        if bind_local:
            # Explicit transport migration, preserving identities and work.
            # Never infer a replacement pane from a window number alone.
            panes = tmux_runtime.pane_snapshot()
            with conn:
                for row in rows:
                    if row["host"] != local_host() or row["tmux_server_id"]:
                        continue
                    if not str(row["tmux"]).startswith("tmux="):
                        continue
                    pane = panes.get(row["pane_id"])
                    expected = str(row["tmux"]).split(" ", 1)[0]
                    if (not pane or pane["dead"]
                            or expected != "tmux=" + str(pane["location"])):
                        unmatched.append(row["agent_id"])
                        continue
                    changed = conn.execute(
                        "UPDATE identities SET tmux_server_id=? WHERE agent_id=? "
                        "AND status='active' AND lease_until_ms>=? "
                        "AND tmux_server_id IS NULL AND pane_id=? AND tmux=? AND host=?",
                        (pane["server_id"], row["agent_id"], stamp,
                         row["pane_id"], row["tmux"], row["host"]),
                    ).rowcount
                    if changed:
                        bound.append(row["agent_id"])
        print(json.dumps({
            "schema": "agent-bus/registry-migrate/v3",
            "transport": "local",
            "registry_room": None,
            "registered": len(rows),
            "published": 0,
            "legacy_published": 0,
            "bound_local_terminals": bound,
            "unmatched_local_terminals": unmatched,
        }, separators=(",", ":")))
        return
    published = 0
    legacy_published = 0
    for row in rows:
        with registry_lock(row["agent_id"]):
            current = identity(conn, row["agent_id"])
            if current["status"] != "active" or not current["lease_until_ms"] or current["lease_until_ms"] < stamp:
                continue
            content = state_content(current)
            put_state(current["agent_id"], content)
            published += 1
            if args.legacy_timeline:
                put_event(
                    AGENT_TYPE,
                    f"ab3-registry-migrate-{current['agent_id']}-{current['generation']}-{current['updated_ms']}",
                    content,
                )
                legacy_published += 1
    print(json.dumps({
        "schema": "agent-bus/registry-migrate/v3",
        "registry_room": REGISTRY_ROOM,
        "published": published,
        "legacy_published": legacy_published,
    }, separators=(",", ":")))


SUBJECT_MAX_BYTES = 160


def clip_subject(subject: str, limit: int = SUBJECT_MAX_BYTES) -> str:
    """Clip UTF-8 subjects to the transport byte limit without splitting a character."""
    raw = subject.encode("utf-8")
    if len(raw) <= limit:
        return subject
    return raw[: limit - 3].decode("utf-8", "ignore") + "..."


def local_event_id(msg_id: str) -> str:
    return f"local:{msg_id}"


def _local_insert_recipient(
    conn: sqlite3.Connection,
    content: dict[str, Any],
    recipient_id: str,
    handle_at_send: str,
    event_id: str,
    delivered_ms: int,
    sender_host: str,
) -> None:
    """Deliver one local recipient using only the existing v3 tables."""
    recipient = conn.execute(
        "SELECT harness,status,lease_until_ms,host FROM identities"
        " WHERE agent_id=?", (recipient_id,)
    ).fetchone()
    if recipient is None:
        raise RuntimeError(f"local recipient disappeared: {recipient_id}")
    if (recipient["status"] != "active"
            or not recipient["lease_until_ms"]
            or int(recipient["lease_until_ms"]) < now_ms()):
        raise RuntimeError(
            f"local recipient is no longer active: {recipient_id}; retry send"
        )
    if str(recipient["host"]) != sender_host:
        raise RuntimeError(
            f"local recipient moved to another host: {recipient_id}; retry send"
        )
    if str(recipient["harness"]).lower() == "cron":
        raise RuntimeError(f"local recipient is sender-only: {recipient_id}")
    conn.execute(
        "INSERT OR IGNORE INTO outbox_recipients"
        "(msg_id,recipient_agent_id,handle_at_send,delivered_ms)"
        " VALUES(?,?,?,?)",
        (content["msg_id"], recipient_id, handle_at_send, delivered_ms),
    )
    conn.execute(
        "UPDATE outbox_recipients SET delivered_ms=COALESCE(delivered_ms,?)"
        " WHERE msg_id=? AND recipient_agent_id=?",
        (delivered_ms, content["msg_id"], recipient_id),
    )
    inserted = conn.execute(
        "INSERT OR IGNORE INTO inbox VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",
        (
            recipient_id,
            content["msg_id"],
            event_id,
            content.get("from", {}).get("agent_id", ""),
            content.get("from", {}).get("handle", "unknown"),
            content.get("subject", "(no subject)"),
            content.get("body", ""),
            content.get("priority", "normal"),
            parse_time(content.get("created_at")) or delivered_ms,
            parse_time(content.get("expires_at")),
            "available",
            None,
        ),
    ).rowcount
    if inserted:
        conn.execute(
            "UPDATE inbox_signal SET generation=generation+1 WHERE agent_id=?",
            (recipient_id,),
        )


def _local_create_message(
    conn: sqlite3.Connection,
    sender: sqlite3.Row,
    recipients: list[dict[str, Any]],
    content: dict[str, Any],
) -> str:
    """Commit acceptance, every inbox delivery, and delivery status together."""
    msg_id = str(content["msg_id"])
    event_id = local_event_id(msg_id)
    created_ms = parse_time(content.get("created_at")) or now_ms()
    expires_ms = parse_time(content.get("expires_at"))
    try:
        conn.execute("BEGIN IMMEDIATE")
        current_sender = conn.execute(
            "SELECT status,lease_until_ms,host,handle,generation FROM identities"
            " WHERE agent_id=?",
            (sender["agent_id"],),
        ).fetchone()
        if (current_sender is None
                or current_sender["status"] != "active"
                or not current_sender["lease_until_ms"]
                or int(current_sender["lease_until_ms"]) < now_ms()):
            raise RuntimeError(
                f"local sender is no longer active: {sender['agent_id']}; retry send"
            )
        if (current_sender["host"] != sender["host"]
                or current_sender["handle"] != sender["handle"]
                or current_sender["generation"] != sender["generation"]):
            raise RuntimeError(
                f"local sender identity changed: {sender['agent_id']}; retry send"
            )
        conn.execute(
            "INSERT INTO outbox VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                msg_id,
                sender["agent_id"],
                content["subject"],
                content["body"],
                created_ms,
                expires_ms,
                event_id,
                "accepted",
                None,
                json.dumps(content, separators=(",", ":")),
            ),
        )
        for recipient in recipients:
            _local_insert_recipient(
                conn,
                content,
                str(recipient["agent_id"]),
                str(recipient["handle"]),
                event_id,
                created_ms,
                str(sender["host"]),
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return event_id


def cmd_send(args: argparse.Namespace) -> None:
    if len(args.body.encode()) > 16384:
        fail("message body exceeds 16 KiB")
    args.subject = clip_subject(args.subject)
    # A sender must remain discoverable long enough for target resolution and
    # delivery status. Renew the EXISTING identity at the one send entry point;
    # cmd_heartbeat is throttled, rejects retired/unknown identities, and never
    # creates a replacement identity. This keeps lifecycle out of every caller
    # without keeping idle services alive forever.
    try:
        cmd_heartbeat(argparse.Namespace(identity=args.sender))
    except RuntimeError as exc:
        # Matrix registry and message traffic use separate rooms. A registry
        # write failure must stay visible, but must not suppress a message that
        # its own room can still accept. Unknown/retired identities fail via
        # SystemExit and deliberately do not reach this continuation.
        print(
            f"agent-bus-v3: sender registry refresh pending: {exc};"
            " continuing message send",
            file=sys.stderr,
        )
    conn = db(); sender = identity(conn, args.sender); recipients = resolve_target(args.target, sender["agent_id"])
    if not recipients:
        fail("no active recipients")
    msg_id = str(uuid.uuid4()); stamp = now_ms(); expires = stamp + args.ttl * 1000
    content = {
        "schema": "agent-bus/message/v3", "msg_id": msg_id,
        "from": {"agent_id": sender["agent_id"], "handle": sender["handle"], "generation": sender["generation"]},
        "to": [{"agent_id": r["agent_id"], "handle_at_send": r["handle"]} for r in recipients],
        "subject": args.subject, "body": args.body, "priority": args.priority,
        "created_at": iso(stamp), "expires_at": iso(expires),
        "ack_requested": ["delivered", "processed"],
    }
    if is_local_transport():
        event_id = _local_create_message(conn, sender, recipients, content)
        print(json.dumps({"schema": "agent-bus/send-result/v3", "msg_id": msg_id,
                          "transport_state": "accepted", "matrix_event_id": event_id,
                          "recipients": len(recipients),
                          "recipient_agent_ids": [r["agent_id"] for r in recipients]}))
        return
    conn.execute("INSERT INTO outbox VALUES(?,?,?,?,?,?,?,?,?,?)", (msg_id, sender["agent_id"], args.subject, args.body, stamp, expires, None, "queued", None, json.dumps(content, separators=(",", ":"))))
    for recipient in recipients:
        conn.execute("INSERT INTO outbox_recipients(msg_id,recipient_agent_id,handle_at_send) VALUES(?,?,?)", (msg_id, recipient["agent_id"], recipient["handle"]))
    conn.commit()
    if not send_outbox_row(conn, msg_id):
        row = conn.execute("SELECT last_error FROM outbox WHERE msg_id=?", (msg_id,)).fetchone()
        fail(f"send not accepted; msg_id={msg_id}; retained for retry: {row['last_error']}")
    event_id = conn.execute("SELECT matrix_event_id FROM outbox WHERE msg_id=?", (msg_id,)).fetchone()[0]
    print(json.dumps({"schema": "agent-bus/send-result/v3", "msg_id": msg_id,
                      "transport_state": "accepted", "matrix_event_id": event_id,
                      "recipients": len(recipients),
                      "recipient_agent_ids": [r["agent_id"] for r in recipients]}))


def send_outbox_row(conn: sqlite3.Connection, msg_id: str) -> bool:
    row = conn.execute("SELECT * FROM outbox WHERE msg_id=?", (msg_id,)).fetchone()
    if not row or row["transport_state"] == "accepted":
        return bool(row)
    if is_local_transport():
        # A local send is one transaction, so it can never leave a retryable
        # partial row. Refuse a foreign/legacy queued row without networking.
        conn.execute(
            "UPDATE outbox SET transport_state='pending_retry',last_error=?"
            " WHERE msg_id=?",
            ("queued row predates atomic local transport", msg_id),
        )
        conn.commit()
        return False
    try:
        event_id = put_event(MESSAGE_TYPE, f"ab3-msg-{msg_id}", json.loads(row["payload_json"]))
    except RuntimeError as exc:
        conn.execute("UPDATE outbox SET transport_state='pending_retry',last_error=? WHERE msg_id=?", (str(exc), msg_id)); conn.commit()
        return False
    conn.execute("UPDATE outbox SET transport_state='accepted',matrix_event_id=?,last_error=NULL WHERE msg_id=?", (event_id, msg_id)); conn.commit()
    return True


def cmd_retry(args: argparse.Namespace) -> None:
    conn = db(); sender = identity(conn, args.sender)
    rows = conn.execute("SELECT msg_id FROM outbox WHERE sender_agent_id=? AND transport_state IN ('queued','pending_retry') ORDER BY created_ms", (sender["agent_id"],)).fetchall()
    accepted = sum(send_outbox_row(conn, row["msg_id"]) for row in rows)
    print(json.dumps({"schema": "agent-bus/retry-result/v3", "attempted": len(rows), "accepted": accepted}))


def parse_time(value: str | None) -> int | None:
    if not value:
        return None
    return int(dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def queue_ack(conn: sqlite3.Connection, msg_id: str, from_id: str, to_id: str, stage: str, status: str, detail: str | None = None) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO ack_outbox(msg_id,from_agent_id,to_agent_id,stage,status,detail,created_ms) VALUES(?,?,?,?,?,?,?)",
        (msg_id, from_id, to_id, stage, status, detail, now_ms()),
    )


def gap_events(from_token: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    token = from_token
    while True:
        result = matrix("GET", f"/_matrix/client/v3/rooms/{encoded(ROOM)}/messages?" + urllib.parse.urlencode({"from": token, "dir": "f", "limit": "100"}))
        chunk = result.get("chunk", [])
        events.extend(chunk)
        end = result.get("end")
        if not chunk or not end or end == token:
            break
        token = end
    return events


def flush_acks(conn: sqlite3.Connection) -> None:
    for ack in conn.execute("SELECT * FROM ack_outbox WHERE matrix_event_id IS NULL ORDER BY created_ms").fetchall():
        sender = identity(conn, ack["from_agent_id"])
        content = {
            "schema": "agent-bus/ack/v3", "msg_id": ack["msg_id"],
            "from": {"agent_id": sender["agent_id"], "handle": sender["handle"]},
            "to_agent_id": ack["to_agent_id"], "stage": ack["stage"],
            "status": ack["status"], "detail": ack["detail"], "at": iso(ack["created_ms"]),
        }
        try:
            event_id = put_event(ACK_TYPE, f"ab3-ack-{ack['msg_id']}-{ack['from_agent_id']}-{ack['stage']}", content)
        except RuntimeError as exc:
            print(f"agent-bus-v3: ACK retry pending: {exc}", file=sys.stderr)
            continue
        with conn:
            conn.execute(
                "UPDATE ack_outbox SET matrix_event_id=? "
                "WHERE msg_id=? AND from_agent_id=? AND stage=?",
                (event_id, ack["msg_id"], ack["from_agent_id"], ack["stage"]),
            )


def ingest(agent_value: str, timeout_ms: int, retry_label: str = "ingest") -> int:
    if is_local_transport():
        # Local send already committed directly into this identity's inbox.
        # Claim its durable signal cursor just as Matrix ingest advances the
        # sync cursor. A later watcher must not announce a generation that a
        # pull, replay, unread check, or notify check already observed.
        retry_sqlite_lock(
            f"{retry_label} local signal",
            lambda: local_watch_poll(agent_value),
        )
        return 0
    with closing(retry_sqlite_lock(f"{retry_label} database open", db)) as conn:
        agent = identity(conn, agent_value)
        lock_path = CFG / f"ingest-{agent['agent_id']}.lock"
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            token = conn.execute("SELECT token FROM cursors WHERE agent_id=?", (agent["agent_id"],)).fetchone()[0]
            # Hard wall-clock cap on the long-poll: a stalled sync must not hang the
            # watch loop and starve its inline heartbeat. Cap sits above the intended
            # long-poll + urllib timeout, so only a true stall is cut short.
            response = call_with_deadline(lambda: sync(token, timeout_ms), timeout_ms / 1000 + 25)
            timeline = response.get("rooms", {}).get("join", {}).get(ROOM, {}).get("timeline", {})
            events = gap_events(token) if timeline.get("limited") else timeline.get("events", [])
            next_batch = response.get("next_batch")

            def commit_batch() -> int:
                inserted = 0
                with conn:
                    for event in events:
                        try:
                            inserted += ingest_event(conn, agent, event)
                        except (KeyError, TypeError, ValueError, sqlite3.IntegrityError) as exc:
                            print(
                                f"agent-bus-v3: quarantined malformed event "
                                f"{event.get('event_id', '?')}: {exc}",
                                file=sys.stderr,
                            )
                        # OperationalError deliberately escapes: quarantining a
                        # lock failure could advance the cursor past an event that
                        # never committed. The outer retry replays this whole batch.
                    if next_batch:
                        conn.execute(
                            "UPDATE cursors SET token=? WHERE agent_id=?",
                            (next_batch, agent["agent_id"]),
                        )
                return inserted

            inserted = retry_sqlite_lock(f"{retry_label} ingest transaction", commit_batch)
            try:
                retry_sqlite_lock(f"{retry_label} ACK flush", lambda: flush_acks(conn))
            except sqlite3.OperationalError as exc:
                if not is_sqlite_lock_contention(exc):
                    raise
                # The deterministic Matrix transaction can be replayed later.
                # Preserve the committed count so the watcher still wakes.
                print(
                    f"agent-bus-v3: {retry_label} ACK persistence deferred: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
            return inserted


def ingest_event(conn: sqlite3.Connection, agent: sqlite3.Row, event: dict[str, Any]) -> int:
    content = event.get("content", {})
    if event.get("type") == MESSAGE_TYPE and content.get("schema") == "agent-bus/message/v3":
        # Sender-only cron identities have no model and never pull an inbox.
        # New sends already exclude them, but old queued outbox rows and old
        # senders can still name their id.  Refuse that historical receive
        # path here so it cannot create permanent unread mail or a false
        # delivered acknowledgement.  ACK events remain receivable below.
        if str(agent["harness"]).lower() == "cron":
            return 0
        if not isinstance(content.get("to"), list) or not isinstance(content.get("msg_id"), str):
            raise ValueError("invalid message fields")
        recipient_ids = {item.get("agent_id") for item in content["to"] if isinstance(item, dict)}
        if agent["agent_id"] not in recipient_ids:
            return 0
        before = conn.total_changes
        conn.execute(
            "INSERT OR IGNORE INTO inbox VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)",
            (agent["agent_id"], content["msg_id"], event.get("event_id"),
             content.get("from", {}).get("agent_id", ""), content.get("from", {}).get("handle", "unknown"),
             content.get("subject", "(no subject)"), content.get("body", ""), content.get("priority", "normal"),
             parse_time(content.get("created_at")) or event.get("origin_server_ts") or now_ms(),
             parse_time(content.get("expires_at")), "available", None),
        )
        if conn.total_changes > before:
            conn.execute("UPDATE inbox_signal SET generation=generation+1 WHERE agent_id=?", (agent["agent_id"],))
            queue_ack(conn, content["msg_id"], agent["agent_id"], content.get("from", {}).get("agent_id", ""), "delivered", "ok")
            return 1
    elif event.get("type") == ACK_TYPE and content.get("schema") == "agent-bus/ack/v3" and content.get("to_agent_id") == agent["agent_id"]:
        sender_id = content.get("from", {}).get("agent_id")
        if content.get("stage") == "delivered":
            conn.execute("UPDATE outbox_recipients SET delivered_ms=? WHERE msg_id=? AND recipient_agent_id=?", (parse_time(content.get("at")) or now_ms(), content.get("msg_id"), sender_id))
        elif content.get("stage") == "processed":
            conn.execute("UPDATE outbox_recipients SET processed_ms=?,processed_status=? WHERE msg_id=? AND recipient_agent_id=?", (parse_time(content.get("at")) or now_ms(), content.get("status"), content.get("msg_id"), sender_id))
    return 0


def available(conn: sqlite3.Connection, agent_id: str, limit: int, max_bytes: int) -> tuple[list[sqlite3.Row], dict[str, Any]]:
    stamp = now_ms()
    conn.execute("UPDATE inbox SET state='available',lease_until_ms=NULL WHERE agent_id=? AND state='presented' AND lease_until_ms<?", (agent_id, stamp))
    conn.execute("UPDATE inbox SET state='parked',lease_until_ms=NULL WHERE agent_id=? AND state='available' AND attempts>=?", (agent_id, PRESENT_ATTEMPT_CAP))
    rows = conn.execute(
        "SELECT * FROM inbox WHERE agent_id=? AND state='available' AND (expires_ms IS NULL OR expires_ms>?) ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 ELSE 2 END,created_ms",
        (agent_id, stamp),
    ).fetchall()
    chosen, used = [], 0
    for row in rows:
        size = len(json.dumps(message_envelope(row), separators=(",", ":")).encode())
        if len(chosen) >= limit or used + size > max_bytes:
            break
        chosen.append(row); used += size
    ids = [row["msg_id"] for row in chosen]
    if ids:
        marks = ",".join("?" for _ in ids)
        conn.execute(f"UPDATE inbox SET state='presented',lease_until_ms=?,attempts=attempts+1 WHERE agent_id=? AND msg_id IN ({marks})", (stamp + LEASE_SECONDS * 1000, agent_id, *ids))
    remaining = rows[len(chosen):]
    parked = conn.execute("SELECT COUNT(*) FROM inbox WHERE agent_id=? AND state='parked'", (agent_id,)).fetchone()[0]
    digest = {"remaining": len(remaining), "urgent": sum(r["priority"] == "urgent" for r in remaining),
              "oldest_ms": remaining[0]["created_ms"] if remaining else None, "parked": parked}
    conn.commit()
    return chosen, digest


def message_envelope(row: sqlite3.Row) -> dict[str, Any]:
    return {"schema": "agent-bus/message/v3", "msg_id": row["msg_id"],
            "sender_agent_id": row["sender_agent_id"], "sender_handle": row["sender_handle"],
            "subject": row["subject"], "body": row["body"], "priority": row["priority"],
            "created_ms": row["created_ms"], "attempt": row["attempts"] + 1}


def cmd_replay(args: argparse.Namespace) -> None:
    """Re-show messages that are already in the inbox, WITHOUT consuming anything.

    Exists because ``pull`` is destructive in one specific way that fooled two
    seats into idling for hours: it flips the rows it shows to ``presented``
    with a lease, so a second ``pull`` inside the lease window returns nothing.
    A seat whose terminal truncated the first pull's output (``| head``), or
    whose harness swallowed it, then reads "no messages" and concludes the
    dispatch never arrived — while the sender sees ``delivered=1``. That gap is
    the whole defect: ``delivered`` asserts the transport handed it over, not
    that a model read it.

    The content was never lost — the row stays in ``inbox`` — there was simply
    no verb to look at it. What this does NOT do is the part that matters:
    it never presents. No state moves to ``presented``, no lease is taken, no
    ``attempts`` counter moves, nothing is acked. It DOES ingest first, exactly
    like ``unread`` does, because a seat that has never pulled would otherwise
    be told its inbox is empty while the sender sees ``delivered=1`` — which is
    the very confusion this verb exists to end. Ingest adds; only presentation
    consumes. Running it twice gives the same answer, and running it can never
    make a later ``pull`` show less.

    One consequence discovered by using it: **``replay`` shows, it does not hand
    over.** ``ack`` requires the message to have been presented first, so a
    message you read only through ``replay`` cannot be acked yet — the ACK would
    be asserting you were handed something you were not. Recover the content with
    ``replay``, then ``pull`` it (it is still queued) and ack that. This is the
    correct split rather than a wart: presentation is what ``ack`` is an answer
    to. It is documented here because the first person to hit it will otherwise
    read "must be presented before processed ACK" as a bug in the verb.

    Ordering is ``created_ms, rowid``: a burst of messages sent inside one
    millisecond shares a ``created_ms``, so ordering by that column alone is
    not a total order and the output shuffles between runs. The first draft of
    this verb did exactly that and its own test caught it — six messages in one
    burst is precisely the shape of the incident that prompted this.
    """
    ingest(args.identity, 0)
    conn = db()
    agent = identity(conn, args.identity)
    stamp = now_ms()
    rows = conn.execute(
        "SELECT * FROM inbox WHERE agent_id=? AND state IN ('available','presented') "
        "AND (expires_ms IS NULL OR expires_ms>?) ORDER BY created_ms, rowid",
        (agent["agent_id"], stamp),
    ).fetchall()
    shown, used = [], 0
    for row in reversed(rows):  # newest first while budgeting, so a small --max keeps the NEWEST
        size = len(json.dumps(message_envelope(row), separators=(",", ":")).encode())
        if len(shown) >= args.max or used + size > args.max_bytes:
            break
        shown.append(row); used += size
    for row in reversed(shown):  # ... but print oldest-first for readability
        envelope = message_envelope(row)
        envelope["replayed"] = True
        envelope["inbox_state"] = row["state"]
        print(json.dumps(envelope, separators=(",", ":")))
    omitted = len(rows) - len(shown)
    processed = conn.execute(
        "SELECT COUNT(*) c FROM inbox WHERE agent_id=? AND state='processed'", (agent["agent_id"],)
    ).fetchone()["c"]
    print(json.dumps({"schema": "agent-bus/replay-summary/v3", "live": len(rows),
                      "shown": len(shown), "omitted_by_budget": omitted,
                      "already_processed": processed,
                      "note": "read-only: nothing was consumed, leased, or acked"},
                     separators=(",", ":")))


def cmd_pull(args: argparse.Namespace) -> None:
    try:
        cmd_heartbeat(argparse.Namespace(identity=args.identity))
    except RuntimeError as exc:
        print(f"agent-bus-v3: registry heartbeat retry pending: {exc}", file=sys.stderr)
    ingest(args.identity, 0)
    conn = db(); agent = identity(conn, args.identity); rows, digest = available(conn, agent["agent_id"], args.max, args.max_bytes)
    for row in rows:
        print(json.dumps(message_envelope(row), separators=(",", ":")))
    if digest["remaining"] or digest["parked"]:
        print(json.dumps({"schema": "agent-bus/digest/v3", **digest}, separators=(",", ":")))


def unread_status(conn: sqlite3.Connection, agent_id: str, commit: bool = True) -> dict[str, Any]:
    stamp = now_ms()
    conn.execute("UPDATE inbox SET state='available',lease_until_ms=NULL WHERE agent_id=? AND state='presented' AND lease_until_ms<?", (agent_id, stamp))
    row = conn.execute(
        "SELECT COUNT(*) count, SUM(priority='urgent') urgent FROM inbox WHERE agent_id=? AND state='available' AND (expires_ms IS NULL OR expires_ms>?)",
        (agent_id, stamp),
    ).fetchone()
    signal = conn.execute("SELECT generation,notified_generation FROM inbox_signal WHERE agent_id=?", (agent_id,)).fetchone()
    ident = conn.execute("SELECT heartbeat_fails,heartbeat_last_error FROM identities WHERE agent_id=?", (agent_id,)).fetchone()
    if commit:
        conn.commit()
    parked = conn.execute(
        "SELECT COUNT(*) FROM inbox WHERE agent_id=? AND state='parked'",
        (agent_id,),
    ).fetchone()[0]
    out = {"count": row["count"], "urgent": row["urgent"] or 0, "parked": parked,
           "generation": signal["generation"], "notified_generation": signal["notified_generation"]}
    if ident is not None and (ident["heartbeat_fails"] or 0) >= HEARTBEAT_FAIL_FLAG_THRESHOLD:
        # Surface degraded registry heartbeats to the seat itself — the one
        # party that can act on it (transport/account/room problem, not
        # necessarily a broken seat; never self-terminate over it).
        out["registry_heartbeat"] = {"failing": True,
                                     "consecutive_failures": ident["heartbeat_fails"],
                                     "last_error": ident["heartbeat_last_error"]}
    return out


def pending_unread(conn: sqlite3.Connection, agent_id: str) -> dict[str, Any]:
    stamp = now_ms()
    row = conn.execute(
        "SELECT COUNT(*) count, SUM(priority='urgent') urgent FROM inbox WHERE agent_id=? AND state='available' AND (expires_ms IS NULL OR expires_ms>?)",
        (agent_id, stamp),
    ).fetchone()
    signal = conn.execute("SELECT generation,notified_generation FROM inbox_signal WHERE agent_id=?", (agent_id,)).fetchone()
    return {"count": row["count"], "urgent": row["urgent"] or 0,
            "generation": signal["generation"], "notified_generation": signal["notified_generation"]}


def cmd_unread(args: argparse.Namespace) -> None:
    if not args.local_only:
        ingest(args.identity, 0)
    conn = db(); agent = identity(conn, args.identity)
    print(json.dumps({"schema": "agent-bus/unread/v3", "agent_id": agent["agent_id"],
                      **unread_status(conn, agent["agent_id"])}, separators=(",", ":")))


def local_host() -> str:
    return socket.gethostname().split(".", 1)[0]


def parse_dispatch_interval(raw: str, source: str) -> float:
    try:
        interval = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{source} must be a number, got {raw!r}") from exc
    if not math.isfinite(interval) or interval < 0:
        raise RuntimeError(
            f"{source} must be a finite non-negative number, got {raw!r}"
        )
    return interval


def configured_dispatch_interval() -> float:
    return parse_dispatch_interval(
        os.environ.get("AGENT_BUS_DISPATCH_INTERVAL", "10"),
        "AGENT_BUS_DISPATCH_INTERVAL",
    )


def command_dispatch_interval(raw: str) -> float:
    try:
        return parse_dispatch_interval(raw, "--interval")
    except RuntimeError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def cmd_dispatch(args: argparse.Namespace) -> None:
    host = args.host or local_host()
    interval = (
        args.interval if args.interval is not None
        else configured_dispatch_interval()
    )
    loaded_identity = getattr(args, "loaded_source_identity", None)
    if loaded_identity is None:
        loaded_identity = source_identity()
    while True:
        def discover_agents() -> list[sqlite3.Row]:
            with closing(db()) as conn:
                stamp = now_ms()
                return conn.execute(
                    "SELECT agent_id FROM identities WHERE status='active' "
                    "AND mode='pull' AND host=? AND lease_until_ms>=?",
                    (host, stamp),
                ).fetchall()

        try:
            agents = retry_sqlite_lock("dispatch discovery", discover_agents)
        except sqlite3.OperationalError as exc:
            if is_sqlite_lock_contention(exc):
                print("agent-bus-v3: dispatch sqlite lock budget exhausted", file=sys.stderr, flush=True)
            raise
        ingested = 0; errors = 0
        for agent in agents:
            try:
                ingested += ingest(agent["agent_id"], 0, retry_label="dispatch")
            except RuntimeError as exc:
                errors += 1
                print(f"agent-bus-v3: dispatch retry for {agent['agent_id']}: {exc}", file=sys.stderr)
            except sqlite3.OperationalError as exc:
                if is_sqlite_lock_contention(exc):
                    print(
                        f"agent-bus-v3: dispatch sqlite lock budget exhausted "
                        f"for {agent['agent_id']}",
                        file=sys.stderr,
                        flush=True,
                    )
                raise
        if args.once:
            print(json.dumps({"schema": "agent-bus/dispatch/v3", "agents": len(agents),
                              "ingested": ingested, "errors": errors}, separators=(",", ":")))
            return
        # The complete discovery + per-seat ingest pass is one delivery
        # transaction boundary.  No Matrix request or SQLite transaction is
        # live here, so replacing the process cannot split a message.
        maybe_reexec(loaded_identity)
        time.sleep(interval)


def cmd_notify_claim(args: argparse.Namespace) -> None:
    conn = db(); stamp = now_ms()
    # Exact pane and harness matching prevents a stale slot from waking a reused pane.
    rows = conn.execute(
        "SELECT i.* FROM identities i WHERE i.status='active' AND i.mode='pull' AND i.harness='codex' AND i.host=? AND i.lease_until_ms>=?",
        (args.host, stamp),
    ).fetchall()
    members = [state_content(row) for row in rows]
    panes = observe_member_terminals(members)
    def matches_terminal(member: dict[str, Any]) -> bool:
        if not is_local_transport() and not member.get("tmux_server_id"):
            # Preserve the existing network-fleet hook until its owner normally
            # rejoins and records a server generation. No status read rewrites
            # these legacy registrations or silently disables their hook.
            return str(member.get("tmux", "")).split(" ", 1)[0] == f"tmux={args.pane}"
        return local_terminal_view(member, panes)["tmux"] == f"tmux={args.pane}"

    rows = [row for row, member in zip(rows, members) if matches_terminal(member)]
    if len(rows) != 1:
        print(json.dumps({"notify": False, "reason": "identity-not-unique"}, separators=(",", ":")))
        return
    agent = rows[0]
    ingest(agent["agent_id"], 0)
    conn = db()
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Do not expire presentation leases here: a message already shown to the
        # model must not generate a new automatic user turn. Lease recovery is a
        # pull concern and will be noticed on a later genuinely new generation.
        status = pending_unread(conn, agent["agent_id"])
        if not status["count"] or status["generation"] <= status["notified_generation"]:
            conn.commit()
            print(json.dumps({"notify": False, "reason": "no-new-unread"}, separators=(",", ":")))
            return
        changed = conn.execute(
            "UPDATE inbox_signal SET notified_generation=?,notified_ms=? WHERE agent_id=? AND notified_generation<?",
            (status["generation"], stamp, agent["agent_id"], status["generation"]),
        ).rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    print(json.dumps({"notify": bool(changed), "agent_id": agent["agent_id"],
                      "generation": status["generation"]}, separators=(",", ":")))


def cmd_notify_reset(args: argparse.Namespace) -> None:
    conn = db()
    conn.execute(
        "UPDATE inbox_signal SET notified_generation=CASE WHEN notified_generation=? THEN ?-1 ELSE notified_generation END WHERE agent_id=?",
        (args.generation, args.generation, args.agent_id),
    )
    conn.commit()


def cmd_ack(args: argparse.Namespace) -> None:
    conn = db(); agent = identity(conn, args.identity)
    local = is_local_transport()
    if local:
        conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM inbox WHERE agent_id=? AND msg_id=?",
            (agent["agent_id"], args.msg_id),
        ).fetchone()
        if not row:
            fail(f"message {args.msg_id} is not in this inbox")
        if row["state"] == "done":
            if local:
                existing = conn.execute(
                    "SELECT processed_status FROM outbox_recipients"
                    " WHERE msg_id=? AND recipient_agent_id=?",
                    (args.msg_id, agent["agent_id"]),
                ).fetchone()
                status = existing["processed_status"] if existing else "unknown"
            else:
                existing = conn.execute(
                    "SELECT status FROM ack_outbox WHERE msg_id=?"
                    " AND from_agent_id=? AND stage='processed'",
                    (args.msg_id, agent["agent_id"]),
                ).fetchone()
                status = existing["status"] if existing else "unknown"
            fail(f"message already processed as {status}")
        if row["state"] != "presented":
            fail("message must be presented before processed ACK")
        conn.execute(
            "UPDATE inbox SET state='done',lease_until_ms=NULL"
            " WHERE agent_id=? AND msg_id=?",
            (agent["agent_id"], args.msg_id),
        )
        if local:
            changed = conn.execute(
                "UPDATE outbox_recipients SET processed_ms=?,processed_status=?"
                " WHERE msg_id=? AND recipient_agent_id=?",
                (now_ms(), args.status, args.msg_id, agent["agent_id"]),
            ).rowcount
            if changed != 1:
                raise RuntimeError("local message has no sender delivery row")
        else:
            queue_ack(
                conn, args.msg_id, agent["agent_id"], row["sender_agent_id"],
                "processed", args.status, args.detail,
            )
        conn.commit()
    except BaseException:
        if local:
            conn.rollback()
        raise
    if not local:
        flush_acks(conn)
    action = "recorded" if local else "queued"
    print(f"processed ACK {action}: {args.msg_id} {args.status}")


def cmd_expire(args: argparse.Namespace) -> None:
    """Tombstone undelivered/unprocessed inbox rows: stale by
    declaration, so redelivery stops - WITHOUT forging a processed state
    (expired is expired; done is a recipient's word alone). Two selectors:
    --msg <id> tombstones one message everywhere it is still pending;
    --agent <identity> tombstones a seat's whole pending inbox (the retired
    -seat case: nobody will ever pull them). Local DB only, no network."""
    conn = db(); stamp = now_ms()
    if not args.msg and not args.agent:
        fail("expire needs --msg <id> or --agent <identity>")
    where, params = [], []
    if args.msg:
        where.append("msg_id=?"); params.append(args.msg)
    if args.agent:
        target = identity(conn, args.agent)
        where.append("agent_id=?"); params.append(target["agent_id"])
    cur = conn.execute(
        f"UPDATE inbox SET expires_ms=? WHERE {' AND '.join(where)}"
        f" AND state!='done' AND (expires_ms IS NULL OR expires_ms>?)",
        (stamp, *params, stamp))
    conn.commit()
    print(json.dumps({"schema": "agent-bus/expire-result/v3",
                      "tombstoned": cur.rowcount,
                      "reason": args.reason or ""},
                     separators=(",", ":")))


def cmd_revive(args: argparse.Namespace) -> None:
    """Re-arm a PARKED message (one that exhausted its presentation cap
    without a processed ACK). Explicit and per-message by design: parking
    exists to stop unbounded wake burn, so only a deliberate hand - the seat
    itself or whoever triages its board - restarts that clock. Resets the
    attempts counter; the message returns on the next pull."""
    conn = db(); agent = identity(conn, args.identity)
    row = conn.execute("SELECT state FROM inbox WHERE agent_id=? AND msg_id=?", (agent["agent_id"], args.msg_id)).fetchone()
    if not row:
        fail(f"message {args.msg_id} is not in this inbox")
    if row["state"] != "parked":
        fail(f"message is {row['state']}, not parked - only parked messages revive")
    conn.execute("UPDATE inbox SET state='available',attempts=0,lease_until_ms=NULL WHERE agent_id=? AND msg_id=?", (agent["agent_id"], args.msg_id))
    conn.commit()
    print(f"revived: {args.msg_id} returns on the next pull")


def _local_generation(conn: sqlite3.Connection, agent_id: str) -> tuple[int, int]:
    signal = conn.execute(
        "SELECT generation FROM inbox_signal WHERE agent_id=?", (agent_id,)
    ).fetchone()
    cursor = conn.execute(
        "SELECT token FROM cursors WHERE agent_id=?", (agent_id,)
    ).fetchone()
    generation = int(signal["generation"]) if signal else 0
    token = str(cursor["token"]) if cursor else "local:0"
    try:
        seen = int(token.removeprefix("local:")) if token.startswith("local:") else 0
    except ValueError:
        seen = 0
    return generation, seen


def local_watch_poll(
    agent_value: str,
    connection: sqlite3.Connection | None = None,
) -> int:
    """Claim unseen local inbox generations for exactly one watcher output.

    The cursor update and signal read share one immediate transaction. Two
    watcher processes therefore cannot announce the same generation, and a
    watcher starting after a send still observes every durable generation.
    """
    conn = connection or db()
    owned_connection = connection is None
    try:
        agent = identity(conn, agent_value)
        generation, seen = _local_generation(conn, str(agent["agent_id"]))
        if generation <= seen:
            return 0
        try:
            # The common idle path above is read-only. Take the write lock only
            # after observing a new generation, then re-read under that lock so
            # concurrent watchers cannot announce it twice.
            conn.execute("BEGIN IMMEDIATE")
            generation, seen = _local_generation(conn, str(agent["agent_id"]))
            if generation <= seen:
                conn.commit()
                return 0
            live = conn.execute(
                "SELECT COUNT(*) FROM inbox WHERE agent_id=? AND state!='done'"
                " AND (expires_ms IS NULL OR expires_ms>?)",
                (agent["agent_id"], now_ms()),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO cursors(agent_id,token) VALUES(?,?)"
                " ON CONFLICT(agent_id) DO UPDATE SET token=excluded.token",
                (agent["agent_id"], f"local:{generation}"),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return generation - seen if live else 0
    finally:
        if owned_connection:
            conn.close()


def cmd_local_watch(args: argparse.Namespace) -> None:
    with closing(db()) as conn:
        agent_id = str(identity(conn, args.identity)["agent_id"])
    loaded_identity = getattr(args, "loaded_source_identity", None)
    if loaded_identity is None:
        loaded_identity = source_identity()
    last_heartbeat = 0.0
    last_reexec = 0.0
    watch_conn: sqlite3.Connection | None = db()
    try:
        while True:
            monotonic = time.monotonic()
            if monotonic - last_heartbeat > 120:
                retry_sqlite_lock(
                    "watch heartbeat",
                    lambda: cmd_heartbeat(argparse.Namespace(identity=agent_id)),
                )
                last_heartbeat = time.monotonic()
            count = retry_sqlite_lock(
                "watch local signal",
                lambda: local_watch_poll(agent_id, watch_conn),
            )
            if count:
                print(json.dumps({"schema": "agent-bus/inbox-changed/v3",
                                  "agent_id": agent_id, "count": count}), flush=True)
            monotonic = time.monotonic()
            if monotonic - last_reexec >= 5:
                # Resident replacement happens with no SQLite connection open.
                watch_conn.close()
                watch_conn = None
                maybe_reexec(loaded_identity)
                watch_conn = db()
                last_reexec = monotonic
            # A bounded sleep is the only empty local-watch path. SQLite remains
            # the durable signal; no extra daemon, socket, or event table exists.
            time.sleep(LOCAL_WATCH_POLL_SECONDS)
    finally:
        if watch_conn is not None:
            watch_conn.close()


def cmd_watch(args: argparse.Namespace) -> None:
    if is_local_transport():
        cmd_local_watch(args)
        return

    def load_agent_id() -> str:
        with closing(db()) as conn:
            return str(identity(conn, args.identity)["agent_id"])

    agent_id = retry_sqlite_lock("watch startup", load_agent_id)
    loaded_identity = getattr(args, "loaded_source_identity", None)
    if loaded_identity is None:
        loaded_identity = source_identity()
    last_heartbeat = 0.0
    while True:
        if time.monotonic() - last_heartbeat > 120:
            try:
                retry_sqlite_lock(
                    "watch heartbeat",
                    lambda: cmd_heartbeat(argparse.Namespace(identity=agent_id)),
                )
                last_heartbeat = time.monotonic()
            except RuntimeError as exc:
                print(f"agent-bus-v3: registry heartbeat retry pending: {exc}", file=sys.stderr)
            except sqlite3.OperationalError as exc:
                if is_sqlite_lock_contention(exc):
                    print("agent-bus-v3: watch sqlite lock budget exhausted", file=sys.stderr, flush=True)
                raise
        try:
            count = ingest(agent_id, 30000, retry_label="watch")
            if count:
                print(json.dumps({"schema": "agent-bus/inbox-changed/v3", "agent_id": agent_id, "count": count}), flush=True)
        except RuntimeError as exc:
            print(f"agent-bus-v3: watch retry: {exc}", file=sys.stderr); time.sleep(5)
        except sqlite3.OperationalError as exc:
            if is_sqlite_lock_contention(exc):
                print("agent-bus-v3: watch sqlite lock budget exhausted", file=sys.stderr, flush=True)
            raise
        # ingest() has released both the per-seat file lock and its SQLite
        # connection.  This is the only watcher replacement point.
        maybe_reexec(loaded_identity)


def cmd_source_identity(_args: argparse.Namespace) -> None:
    print(source_identity())


def cmd_delivery(args: argparse.Namespace) -> None:
    conn = db(); sender = identity(conn, args.sender)
    row = conn.execute("SELECT * FROM outbox WHERE msg_id=? AND sender_agent_id=?", (args.msg_id, sender["agent_id"])).fetchone()
    if not row:
        fail("unknown message for this sender")
    recipients = [dict(r) for r in conn.execute("SELECT * FROM outbox_recipients WHERE msg_id=? ORDER BY handle_at_send", (args.msg_id,))]
    print(json.dumps({"schema": "agent-bus/delivery-status/v3", "msg_id": args.msg_id,
                      "transport_state": row["transport_state"], "recipients": recipients}, separators=(",", ":")))


def cmd_environment(_args: argparse.Namespace) -> None:
    """Print only non-secret path/transport settings for bundled wrappers."""
    import tmux_runtime

    print(json.dumps({
        "tmux_server": tmux_runtime.configured_server()[0],
        "config_directory": str(CFG), "database": str(DB_PATH),
        "transport": transport_name(),
        "token_file": str(auth_header_path()),
    }))


def cmd_brief(_args: argparse.Namespace) -> None:
    command = cfg.command("commands.brief")
    if command:
        result = subprocess.run(command, check=False, timeout=30)
        if result.returncode:
            raise RuntimeError("configured briefing command failed")


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command", required=True)
    j = sub.add_parser("join"); j.add_argument("handle"); j.add_argument("slot"); j.add_argument("harness"); j.add_argument("mode", choices=["watch", "pull"]); j.add_argument("host"); j.add_argument("tmux"); j.set_defaults(func=cmd_join)
    r = sub.add_parser("retire"); r.add_argument("identity"); r.add_argument("--kind", choices=["manual", "checkout", "reaper", "succession"], default="manual"); r.set_defaults(func=cmd_retire)
    e = sub.add_parser("expire", help="tombstone pending inbox rows: stale by"
                                      " declaration, never forged-processed")
    e.add_argument("--msg", default=""); e.add_argument("--agent", default="")
    e.add_argument("--reason", default=""); e.set_defaults(func=cmd_expire)
    m = sub.add_parser("members"); m.set_defaults(func=cmd_members)
    h = sub.add_parser("heartbeat"); h.add_argument("identity"); h.set_defaults(func=cmd_heartbeat)
    g = sub.add_parser("registry-migrate"); g.add_argument("--legacy-timeline", action="store_true"); g.add_argument("--bind-local-terminals", action="store_true", help="explicitly bind matching same-host legacy panes after a local transport migration"); g.set_defaults(func=cmd_registry_migrate)
    s = sub.add_parser("send"); s.add_argument("sender"); s.add_argument("target"); s.add_argument("subject"); s.add_argument("body"); s.add_argument("--priority", choices=["normal", "high", "urgent"], default="normal"); s.add_argument("--ttl", type=int, default=86400); s.set_defaults(func=cmd_send)
    y = sub.add_parser("retry"); y.add_argument("sender"); y.set_defaults(func=cmd_retry)
    q = sub.add_parser("pull"); q.add_argument("identity"); q.add_argument("--max", type=int, default=10); q.add_argument("--max-bytes", type=int, default=32768); q.set_defaults(func=cmd_pull)
    v = sub.add_parser("replay"); v.add_argument("identity"); v.add_argument("--max", type=int, default=10); v.add_argument("--max-bytes", type=int, default=32768); v.set_defaults(func=cmd_replay)
    u = sub.add_parser("unread"); u.add_argument("identity"); u.add_argument("--local-only", action="store_true"); u.set_defaults(func=cmd_unread)
    x = sub.add_parser("dispatch"); x.add_argument("--once", action="store_true"); x.add_argument("--interval", type=command_dispatch_interval); x.add_argument("--host"); x.set_defaults(func=cmd_dispatch)
    n = sub.add_parser("notify-claim"); n.add_argument("host"); n.add_argument("pane"); n.set_defaults(func=cmd_notify_claim)
    z = sub.add_parser("notify-reset"); z.add_argument("agent_id"); z.add_argument("generation", type=int); z.set_defaults(func=cmd_notify_reset)
    a = sub.add_parser("ack"); a.add_argument("identity"); a.add_argument("msg_id"); a.add_argument("status", choices=["ok", "rejected", "failed"]); a.add_argument("detail", nargs="?"); a.set_defaults(func=cmd_ack)
    rv = sub.add_parser("revive"); rv.add_argument("identity"); rv.add_argument("msg_id"); rv.set_defaults(func=cmd_revive)
    w = sub.add_parser("watch"); w.add_argument("identity"); w.set_defaults(func=cmd_watch)
    d = sub.add_parser("delivery"); d.add_argument("sender"); d.add_argument("msg_id"); d.set_defaults(func=cmd_delivery)
    env = sub.add_parser("environment"); env.set_defaults(func=cmd_environment)
    brief = sub.add_parser("brief"); brief.set_defaults(func=cmd_brief)
    si = sub.add_parser("source-identity"); si.set_defaults(func=cmd_source_identity)
    return p


def main() -> None:
    args = parser().parse_args()
    try:
        if args.command not in {"environment", "brief"}:
            validate_fleet_scope()
        resident = args.command == "watch" or (
            args.command == "dispatch" and not args.once
        )
        if resident:
            if not isinstance(LOADED_SOURCE_FD, int):
                if auto_reexec_enabled():
                    # A disk-launched resident first replaces itself with an
                    # immutable source snapshot. No command-line value can
                    # claim what bytes were loaded.
                    exec_current_source(
                        source_identity(), reason="loading sealed source snapshot"
                    )
                # While replacement is disabled the process runs normally but
                # carries no freshness proof.  Keep an empty identity so the
                # first safe boundary after the switch is removed seals and
                # adopts the current source.
                args.loaded_source_identity = ""
            else:
                try:
                    args.loaded_source_identity = source_fd_identity(
                        LOADED_SOURCE_FD
                    )
                except (OSError, RuntimeError) as exc:
                    print(
                        f"FATAL agent-bus-v3: cannot verify loaded source: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    raise SystemExit(70) from exc
        args.func(args)
    except RuntimeError as exc:
        fail(str(exc))


if __name__ == "__main__":
    main()
