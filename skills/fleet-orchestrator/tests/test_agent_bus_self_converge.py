#!/usr/bin/env python3
"""Hermetic process-level test for Agent Bus resident source convergence.

The test runs real watcher and dispatcher processes against a private SQLite
database and an in-process fake Matrix server.  It atomically replaces a
staged copy of agent-bus-v3.py, then proves both process images adopt the new
content at an idle boundary without changing PID or duplicating inbox rows.
No production path, account, database, service, or tmux server is touched.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
BUS_SOURCE = ROOT / "scripts" / "agent-bus-v3.py"
FRESHNESS_SOURCE = ROOT / "scripts" / "agent-bus-watcher-freshness.py"
# Linux UAPI constants are stable even when a Python build omits their names.
F_ADD_SEALS = getattr(fcntl, "F_ADD_SEALS", 1033)
F_GET_SEALS = getattr(fcntl, "F_GET_SEALS", 1034)
SOURCE_SEALS = 0x0001 | 0x0002 | 0x0004 | 0x0008



def content_identity(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def wait_for(description: str, condition: Callable[[], bool], timeout: float = 12) -> None:
    deadline = time.monotonic() + timeout
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        try:
            if condition():
                return
        except (OSError, sqlite3.Error, json.JSONDecodeError) as exc:
            last_error = exc
        time.sleep(0.05)
    suffix = f"; last error: {last_error}" if last_error else ""
    raise AssertionError(f"timed out waiting for {description}{suffix}")


def process_argv(pid: int) -> list[str]:
    raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    return [part for part in raw.decode(errors="replace").split("\0") if part]


def loaded_identity(pid: int) -> str | None:
    candidates = []
    for entry in Path(f"/proc/{pid}/fd").iterdir():
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if "memfd:agent-bus-v3-source" in target:
            candidates.append(entry)
    if len(candidates) != 1:
        return None
    fd = os.open(candidates[0], os.O_RDONLY | os.O_CLOEXEC)
    try:
        if fcntl.fcntl(fd, F_GET_SEALS) & SOURCE_SEALS != SOURCE_SEALS:
            return None
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                return "sha256:" + digest.hexdigest()
            digest.update(chunk)
    finally:
        os.close(fd)


class MatrixFixture:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.events: list[dict] = []
        self.states: dict[str, dict] = {}
        self.transactions: dict[str, str] = {}
        self.block_delivered_acks = False
        self.blocked_delivered_acks = 0
        self.delivered_ack_release = threading.Event()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format: str, *_args) -> None:
                return

            def reply(self, payload: object, status: int = 200) -> None:
                data = json.dumps(payload, separators=(",", ":")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path.endswith("/sync"):
                    query = urllib.parse.parse_qs(parsed.query)
                    with fixture.lock:
                        end = len(fixture.events)
                        if "since" not in query:
                            events = []
                        else:
                            start = int(query["since"][0])
                            events = list(fixture.events[start:end])
                    self.reply({
                        "next_batch": str(end),
                        "rooms": {
                            "join": {
                                "!stage:local": {
                                    "timeline": {"events": events, "limited": False}
                                }
                            }
                        },
                    })
                    return
                if parsed.path.endswith("/state"):
                    with fixture.lock:
                        states = [
                            {
                                "type": "org.agent_bus.agent.v3",
                                "state_key": agent_id,
                                "content": dict(content),
                            }
                            for agent_id, content in fixture.states.items()
                        ]
                    self.reply(states)
                    return
                if parsed.path.endswith("/messages"):
                    self.reply({"chunk": [], "end": "0"})
                    return
                self.reply({"errcode": "M_NOT_FOUND", "error": self.path}, 404)

            def do_PUT(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                path = urllib.parse.urlparse(self.path).path
                parts = path.split("/")
                if "state" in parts:
                    index = parts.index("state")
                    agent_id = urllib.parse.unquote(parts[index + 2])
                    with fixture.lock:
                        fixture.states[agent_id] = payload
                    self.reply({"event_id": f"$state-{agent_id}"})
                    return
                if "send" in parts:
                    index = parts.index("send")
                    event_type = urllib.parse.unquote(parts[index + 1])
                    transaction = urllib.parse.unquote(parts[index + 2])
                    should_block = False
                    with fixture.lock:
                        if (
                            fixture.block_delivered_acks
                            and event_type == "org.agent_bus.ack.v3"
                            and payload.get("stage") == "delivered"
                        ):
                            fixture.blocked_delivered_acks += 1
                            should_block = True
                    if should_block:
                        fixture.delivered_ack_release.wait(timeout=10)
                    with fixture.lock:
                        event_id = fixture.transactions.get(transaction)
                        if event_id is None:
                            event_id = f"$event-{len(fixture.events) + 1}"
                            fixture.transactions[transaction] = event_id
                            fixture.events.append({
                                "event_id": event_id,
                                "type": event_type,
                                "origin_server_ts": int(time.time() * 1000),
                                "content": payload,
                            })
                    self.reply({"event_id": event_id})
                    return
                self.reply({"errcode": "M_NOT_FOUND", "error": self.path}, 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.delivered_ack_release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)

    def pause_delivered_acks(self) -> None:
        with self.lock:
            self.block_delivered_acks = True
            self.blocked_delivered_acks = 0
        self.delivered_ack_release.clear()

    def blocked_ack_count(self) -> int:
        with self.lock:
            return self.blocked_delivered_acks

    def resume_delivered_acks(self) -> None:
        with self.lock:
            self.block_delivered_acks = False
        self.delivered_ack_release.set()


class AgentBusSelfConvergeE2E(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="agent-bus-self-converge-")
        self.stage = Path(self.temp.name)
        self.repo = self.stage / "repo"
        self.scripts = self.repo / "scripts"
        self.scripts.mkdir(parents=True)
        self.bus = self.scripts / "agent-bus-v3.py"
        self.freshness = self.scripts / "agent-bus-watcher-freshness.py"
        shutil.copy2(BUS_SOURCE, self.bus)
        shutil.copy2(FRESHNESS_SOURCE, self.freshness)
        shutil.copytree(ROOT / "scripts/lib", self.scripts / "lib",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.cfg = self.stage / "matrix"
        self.cfg.mkdir()
        (self.cfg / "auth.hdr").write_text("Authorization: Bearer staging\n")
        self.db = self.cfg / "agent-bus-v3.sqlite3"
        self.runtime = self.stage / "runtime"
        self.home = self.stage / "home"
        self.xdg_config = self.stage / "xdg-config"
        self.git_template = self.stage / "git-template"
        for directory in (self.home, self.xdg_config, self.git_template):
            directory.mkdir()
        self.matrix = MatrixFixture()
        self.matrix.start()
        self.host = socket.gethostname()
        # Whitelist the complete child environment.  A test launched from a
        # real agent must not inherit its pane, HOME/XDG files, Python startup
        # hooks, or Git templates/hooks/config.  Join calls below supply
        # private pane ids and every runtime path stays under this stage root.
        self.env = {
            "PATH": os.environ.get("PATH", os.defpath),
            "AGENT_BUS_TRANSPORT": "matrix",
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.xdg_config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TEMPLATE_DIR": str(self.git_template),
            "MATRIX_BUS_HS": self.matrix.url,
            "MATRIX_BUS_ROOM": "!stage:local",
            "MATRIX_BUS_REGISTRY_ROOM": "!registry:local",
            "MATRIX_BUS_CFG": str(self.cfg),
            "AGENT_BUS_DB": str(self.db),
            "NOTES_RUNTIME_DIR": str(self.runtime),
            "NOTES_REPO": str(self.repo),
            "AGENT_BUS_AUTO_REEXEC": "1",
            "AGENT_BUS_DISPATCH_INTERVAL": "0.1",
        }
        self.processes: list[subprocess.Popen[str]] = []

    def tearDown(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
        for process in self.processes:
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate(timeout=3)
        self.matrix.close()
        self.temp.cleanup()

    def run_bus(
        self, *args: str, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(self.bus), *args],
            env=env or self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"bus command failed: {args}\nstdout={result.stdout}\nstderr={result.stderr}",
        )
        return result

    def join(self, handle: str, slot: str, harness: str, mode: str, pane: int) -> dict:
        join_env = dict(self.env)
        join_env["TMUX_PANE"] = f"%{100 + pane}"
        result = self.run_bus(
            "join", handle, slot, harness, mode, self.host,
            f"tmux=stage:{pane}.0 win={harness}",
            env=join_env,
        )
        return json.loads(result.stdout)

    def start_resident(self, *args: str) -> subprocess.Popen[str]:
        process = subprocess.Popen(
            [sys.executable, str(self.bus), *args],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.processes.append(process)
        return process

    def inbox_count(self, agent_id: str, msg_id: str) -> int:
        with sqlite3.connect(self.db, timeout=3) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM inbox WHERE agent_id=? AND msg_id=?",
                (agent_id, msg_id),
            ).fetchone()
        return int(row[0])

    def assert_freshness(self, expected_returncode: int) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [sys.executable, str(self.freshness)],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(
            result.returncode,
            expected_returncode,
            f"unexpected freshness result\nstdout={result.stdout}\nstderr={result.stderr}",
        )
        return result

    def atomic_source_change(self, marker: str) -> str:
        replacement = self.bus.with_name(f".{self.bus.name}.new")
        replacement.write_bytes(self.bus.read_bytes() + f"\n# {marker}\n".encode())
        replacement.chmod(self.bus.stat().st_mode)
        os.replace(replacement, self.bus)
        return content_identity(self.bus)

    def send(self, sender: str, target: str, subject: str) -> str:
        result = self.run_bus("send", sender, target, subject, f"body: {subject}")
        return str(json.loads(result.stdout)["msg_id"])

    def test_watch_and_dispatch_converge_without_loss_or_identity_change(self) -> None:
        for name in ("TMUX_PANE", "PYTHONPATH", "PYTHONINSPECT",
                     "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"):
            self.assertNotIn(name, self.env)
        sender = self.join("stage/sender-tmux1", "stage/sender", "codex", "pull", 1)
        watcher = self.join("stage/watcher-tmux2", "stage/watcher", "claude", "watch", 2)
        pull = self.join("stage/pull-tmux3", "stage/pull", "codex", "pull", 3)
        with sqlite3.connect(self.db) as conn:
            pane_rows = set(conn.execute(
                "SELECT pane_id, agent_id FROM identities"
                " WHERE pane_id IN ('%101','%102','%103')"
            ).fetchall())
        self.assertEqual(pane_rows, {
            ("%101", sender["agent_id"]),
            ("%102", watcher["agent_id"]),
            ("%103", pull["agent_id"]),
        })
        self.assertFalse(
            self.runtime.exists(),
            "Agent Bus joins must keep pane identity only in the database",
        )

        watcher_process = self.start_resident("watch", watcher["agent_id"])
        dispatcher_process = self.start_resident("dispatch")
        original_pids = (watcher_process.pid, dispatcher_process.pid)
        initial_identity = content_identity(self.bus)
        wait_for(
            "initial watcher and dispatcher identities",
            lambda: loaded_identity(watcher_process.pid) == initial_identity
            and loaded_identity(dispatcher_process.pid) == initial_identity,
        )
        first_fresh = self.assert_freshness(0)
        self.assertIn("RESULT: PASS", first_fresh.stdout)

        # Hold each resident inside flush_acks(), after its inbox transaction
        # committed but before ingest() releases its lock and connection.  A
        # source change here must not replace either process until both calls
        # return to the declared idle boundary.
        self.matrix.pause_delivered_acks()
        before_watch = self.send(sender["agent_id"], watcher["agent_id"], "before-watch")
        before_pull = self.send(sender["agent_id"], pull["agent_id"], "before-pull")
        wait_for(
            "watcher and dispatcher to block inside delivered ACK flush",
            lambda: self.matrix.blocked_ack_count() >= 2,
        )
        self.assertEqual(self.inbox_count(watcher["agent_id"], before_watch), 1)
        changed_identity = self.atomic_source_change("staging source generation two")

        time.sleep(0.2)
        self.assertEqual(loaded_identity(watcher_process.pid), initial_identity)
        self.assertEqual(loaded_identity(dispatcher_process.pid), initial_identity)
        self.matrix.resume_delivered_acks()

        wait_for(
            "both residents to adopt changed source",
            lambda: loaded_identity(watcher_process.pid) == changed_identity
            and loaded_identity(dispatcher_process.pid) == changed_identity,
        )
        self.assertEqual(
            (watcher_process.pid, dispatcher_process.pid), original_pids,
            "os.execv must preserve both resident PIDs",
        )

        after_watch = self.send(sender["agent_id"], watcher["agent_id"], "after-watch")
        after_pull = self.send(sender["agent_id"], pull["agent_id"], "after-pull")
        expected = [
            (watcher["agent_id"], before_watch),
            (pull["agent_id"], before_pull),
            (watcher["agent_id"], after_watch),
            (pull["agent_id"], after_pull),
        ]
        wait_for(
            "all four messages to commit exactly once",
            lambda: all(self.inbox_count(agent, msg) == 1 for agent, msg in expected),
        )
        for agent, msg in expected:
            self.assertEqual(self.inbox_count(agent, msg), 1)

        with sqlite3.connect(self.db) as conn:
            identities = conn.execute(
                "SELECT agent_id,generation FROM identities ORDER BY agent_id"
            ).fetchall()
            delivered = {
                (agent, msg): conn.execute(
                    "SELECT COUNT(*) FROM ack_outbox WHERE from_agent_id=? "
                    "AND msg_id=? AND stage='delivered'",
                    (agent, msg),
                ).fetchone()[0]
                for agent, msg in expected
            }
        self.assertEqual(
            {row[0] for row in identities},
            {sender["agent_id"], watcher["agent_id"], pull["agent_id"]},
        )
        self.assertTrue(all(row[1] == 1 for row in identities))
        self.assertTrue(all(count == 1 for count in delivered.values()))
        second_fresh = self.assert_freshness(0)
        self.assertIn(changed_identity, second_fresh.stdout)
        self.assertIn("RESULT: PASS", second_fresh.stdout)

        switch = self.cfg / "auto-reexec.disabled"
        switch.touch()
        paused_identity = self.atomic_source_change("staging source generation three")
        time.sleep(0.5)
        self.assertEqual(loaded_identity(watcher_process.pid), changed_identity)
        self.assertEqual(loaded_identity(dispatcher_process.pid), changed_identity)
        paused = self.assert_freshness(1)
        self.assertIn(paused_identity, paused.stdout)
        self.assertIn("RESULT: FAIL", paused.stdout)

        switch.unlink()
        wait_for(
            "residents to converge after removing the kill switch",
            lambda: loaded_identity(watcher_process.pid) == paused_identity
            and loaded_identity(dispatcher_process.pid) == paused_identity,
        )
        resumed = self.assert_freshness(0)
        self.assertIn("RESULT: PASS", resumed.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
