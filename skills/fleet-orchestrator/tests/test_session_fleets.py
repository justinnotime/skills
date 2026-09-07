"""Exercise session lifecycle and bus separation on a private real tmux server."""

import json
import os
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ORC = ROOT / "scripts/orc"
BUS = ROOT / "scripts/matrix-bus.sh"


@unittest.skipUnless(shutil.which("tmux"), "tmux is required")
class SessionFleetTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="session-fleet-test-")
        self.root = Path(self.temp.name)
        self.server = "session-test-" + uuid.uuid4().hex[:12]
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({
            "schema": "fleet-runtime/v1",
            "runtime_dir": str(self.root / "default"),
            "paths": {"ledger": str(self.root / "default/tasks.sqlite3")},
            "bus": {
                "transport": "local",
                "config_directory": str(self.root / "default/bus"),
                "database": str(self.root / "default/bus/inbox.sqlite3"),
            },
            "fleets": {
                "profile_directory": str(self.root / "profiles"),
                "runtime_directory": str(self.root / "fleets"),
            },
        }))
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("NW_", "AGENT_BUS_", "MATRIX_BUS_", "DISPATCH_LEDGER_"))
                    and k not in {"TMUX", "TMUX_PANE", "NOTES_RUNTIME_DIR"}}
        self.env.update({
            "HOME": str(self.root),
            "FLEET_ORCHESTRATOR_CONFIG": str(self.config),
            "NW_DEFAULT_TMUX_SERVER": self.server,
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(lambda: self.tmux("kill-server", check=False))

    def run_command(self, command, *, env=None, check=True):
        result = subprocess.run([str(x) for x in command], env=env or self.env,
                                text=True, capture_output=True, timeout=20)
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def tmux(self, *args, **kwargs):
        return self.run_command(["tmux", "-u", "-L", self.server, *args], **kwargs)

    def native_session(self, name):
        self.tmux("new-session", "-d", "-s", name, "sleep 300")

    def view(self, name):
        return json.loads(self.run_command([ORC, "fleet", "show", name]).stdout)

    def bus(self, name, *args, **kwargs):
        return self.run_command([BUS, "--fleet", name, *args], **kwargs)

    def join(self, name, handle):
        pane = self.tmux("display-message", "-p", "-t", f"={name}:0.0", "#{pane_id}").stdout.strip()
        result = self.bus(name, "join", handle, handle, "test", "pull",
                          socket.gethostname().split('.')[0], f"tmux={name}:0.0 win=test",
                          env={**self.env, "TMUX_PANE": pane})
        return json.loads(result.stdout)["agent_id"]

    def test_native_sessions_are_fleets_without_profiles_and_keep_buses_separate(self):
        self.native_session("alpha")
        self.native_session("beta")
        alpha, beta = self.view("alpha"), self.view("beta")
        self.assertEqual(alpha["profile_path"], "")
        self.assertEqual(alpha["tmux_server"], self.server)
        self.assertNotEqual(alpha["agent_bus_db"], beta["agent_bus_db"])
        self.assertFalse((self.root / "profiles").exists())
        sender = self.join("alpha", "sender")
        self.join("beta", "receiver")
        result = self.bus("alpha", "send", sender, "receiver", "test", "test", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("0 active agents", result.stderr)

    def test_full_lifecycle_closes_grouped_views_and_preserves_work(self):
        self.run_command([ORC, "fleet", "start", "alpha"])
        self.native_session("beta")
        self.run_command([ORC, "fleet", "window", "alpha"])
        self.tmux("new-session", "-d", "-t", "alpha", "-s", "tview-test")
        windows = self.tmux("list-windows", "-t", "=alpha", "-F", "#{window_id}").stdout.splitlines()
        self.assertEqual(len(windows), 2)
        self.join("alpha", "worker")
        self.run_command([ORC, "--fleet", "alpha", "open", "--to", "operator",
                          "--subject", "saved work", "--body", "Synthetic test task.", "--no-check"])
        view = self.view("alpha")
        self.run_command([ORC, "fleet", "stop", "alpha"])
        self.assertNotEqual(self.tmux("has-session", "-t", "=alpha", check=False).returncode, 0)
        self.assertNotEqual(self.tmux("has-session", "-t", "=tview-test", check=False).returncode, 0)
        self.tmux("has-session", "-t", "=beta")
        with sqlite3.connect(view["agent_bus_db"]) as db:
            self.assertEqual(db.execute("SELECT status FROM identities").fetchone()[0], "retired")
        self.assertIn("saved work", self.run_command([ORC, "--fleet", "alpha", "board"]).stdout)
        self.run_command([ORC, "fleet", "start", "alpha"])
        self.assertIn("saved work", self.run_command([ORC, "--fleet", "alpha", "board"]).stdout)
        self.assertFalse((self.root / "profiles").exists())

    def test_existing_process_uses_its_actual_session_without_export_or_restart(self):
        self.native_session("alpha")
        self.native_session("beta")
        self.tmux("new-session", "-d", "-t", "alpha", "-s", "tview-test")
        fields = self.tmux("display-message", "-p", "-t", "=alpha:0.0",
                           "#{socket_path}|#{pid}|#{session_id}|#{pane_id}").stdout.strip().split("|")
        env = {**self.env, "TMUX": f"{fields[0]},{fields[1]},{fields[2][1:]}", "TMUX_PANE": fields[3]}
        result = self.run_command([BUS, "environment"], env=env)
        self.assertEqual(json.loads(result.stdout)["database"], self.view("alpha")["agent_bus_db"])
        explicit = self.run_command([BUS, "--fleet", "default", "environment"], env=env)
        self.assertEqual(json.loads(explicit.stdout)["database"], str(self.root / "default/bus/inbox.sqlite3"))
        self.assertIn("board empty", self.run_command([ORC, "board"], env=env).stdout)
        listing = self.run_command([ROOT / "scripts/tview", "--list"], env=env).stdout
        self.assertIn("alpha", listing)
        self.assertIn("beta", listing)
        self.assertNotIn("tview-test", listing)

    def test_sender_rejects_a_pane_outside_the_selected_session(self):
        self.native_session("alpha")
        self.native_session("beta")
        pane = self.tmux("display-message", "-p", "-t", "=beta:0.0", "#{pane_id}").stdout.strip()
        program = "import runpy,sys; runpy.run_path(sys.argv[1])['pane_info'](sys.argv[2])"
        command = [sys.executable, ROOT / "scripts/lib/fleet-profile.py", "exec", "alpha", "--",
                   sys.executable, "-c", program, ROOT / "scripts/agent-tmux-send.py", pane]
        result = self.run_command(command, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside the selected fleet session", result.stderr)
        self.tmux("has-session", "-t", "=beta")

    def test_stop_from_inside_its_own_window_finishes_the_whole_session(self):
        self.native_session("alpha")
        self.native_session("beta")
        self.join("alpha", "worker")
        database = self.view("alpha")["agent_bus_db"]
        self.tmux("new-session", "-d", "-t", "alpha", "-s", "tview-test")
        self.tmux("new-window", "-d", "-t", "=alpha",
                  shlex.join([str(ORC), "fleet", "stop", "alpha"]))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.tmux("has-session", "-t", "=alpha", check=False).returncode:
                break
            time.sleep(0.05)
        self.assertNotEqual(self.tmux("has-session", "-t", "=alpha", check=False).returncode, 0)
        self.assertNotEqual(self.tmux("has-session", "-t", "=tview-test", check=False).returncode, 0)
        with sqlite3.connect(database) as db:
            self.assertEqual(db.execute("SELECT status FROM identities").fetchone()[0], "retired")
        self.tmux("has-session", "-t", "=beta")


if __name__ == "__main__":
    unittest.main()
