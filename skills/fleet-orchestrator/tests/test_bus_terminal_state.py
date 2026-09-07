"""Terminal routing is derived from registrations and a private real tmux server."""

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("tmux"), "tmux is required")
class BusTerminalStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bus-pane-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.server = "test-" + uuid.uuid4().hex[:10]
        self.database = self.root / "bus" / "bus.sqlite3"
        configuration = self.root / "config.json"
        configuration.write_text('{"schema":"fleet-runtime/v1"}')
        self.env = {
            "PATH": os.environ.get("PATH", os.defpath), "HOME": str(self.root),
            "FLEET_ORCHESTRATOR_CONFIG": str(configuration),
            "AGENT_BUS_TRANSPORT": "local", "AGENT_BUS_CFG": str(self.root / "bus"),
            "AGENT_BUS_DB": str(self.database), "NW_TMUX_SERVER": self.server,
            "NW_FLEET_PRIMARY_SESSION": "alpha", "TMUX_TMPDIR": str(self.root),
            "PYTHONDONTWRITEBYTECODE": "1",
        }
        self.addCleanup(lambda: self.tmux("kill-server", check=False))
        self.tmux("new-session", "-d", "-s", "alpha", "sleep 300")
        self.pane = self.tmux("display-message", "-p", "-t", "=alpha:0.0", "#{pane_id}").stdout.strip()

    def run_command(self, command, *, extra=None, check=True):
        result = subprocess.run(command, env={**self.env, **(extra or {})}, text=True,
                                capture_output=True, timeout=15)
        if check:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def tmux(self, *args, check=True):
        return self.run_command(["tmux", "-u", "-L", self.server, *args], check=check)

    def bus(self, *args, extra=None):
        return self.run_command([sys.executable, str(ROOT / "scripts/agent-bus-v3.py"), *args], extra=extra)

    def join(self, handle="worker", pane=None, location="alpha:0.0", harness="test"):
        return json.loads(self.bus(
            "join", handle, handle, harness, "pull", "test-host", f"tmux={location} win={harness}",
            extra={"TMUX_PANE": pane or self.pane},
        ).stdout)["agent_id"]

    def members(self):
        return [json.loads(line) for line in self.bus("members").stdout.splitlines()]

    def test_renaming_and_renumbering_change_location_without_registration_writes(self):
        agent = self.join()
        first = self.members()[0]
        self.assertEqual(first["terminal_presence"], "present")
        self.assertEqual(first["pane_id"], self.pane)
        self.assertTrue(first["tmux_server_id"].startswith("tmux:"))
        self.tmux("rename-session", "-t", "=alpha", "renamed")
        self.env["NW_FLEET_PRIMARY_SESSION"] = "renamed"
        self.tmux("move-window", "-s", "=renamed:0", "-t", "=renamed:8")
        before = self.database.read_bytes()
        member = self.members()[0]
        self.assertEqual(member["agent_id"], agent)
        self.assertEqual(member["tmux"], "tmux=renamed:8.0")
        self.assertEqual(member["registration_tmux"], "tmux=alpha:0.0 win=test")
        self.assertEqual(self.database.read_bytes(), before)

    def test_closed_pane_loses_terminal_route_but_keeps_registration(self):
        agent = self.join()
        self.tmux("new-window", "-d", "-t", "=alpha", "sleep 300")
        self.tmux("kill-pane", "-t", self.pane)
        member = self.members()[0]
        self.assertEqual(member["agent_id"], agent)
        self.assertEqual(member["status"], "active")
        self.assertEqual(member["terminal_presence"], "absent")
        self.assertEqual(member["tmux"], "")

    def test_restarted_server_cannot_inherit_a_recycled_pane_id(self):
        self.join()
        before = self.members()[0]
        self.tmux("kill-server")
        self.tmux("new-session", "-d", "-s", "alpha", "sleep 300")
        recycled = self.tmux("display-message", "-p", "-t", "=alpha:0.0", "#{pane_id}").stdout.strip()
        self.assertEqual(recycled, self.pane)
        member = self.members()[0]
        self.assertEqual(member["tmux_server_id"], before["tmux_server_id"])
        self.assertEqual(member["terminal_presence"], "absent")
        self.assertEqual(member["tmux"], "")

    def test_unavailable_tmux_is_unknown_and_does_not_hide_registration(self):
        self.join()
        self.tmux("kill-server")
        member = self.members()[0]
        self.assertEqual(member["terminal_presence"], "unknown")
        self.assertEqual(member["terminal_detail"], "tmux observation unavailable")
        self.assertEqual(member["tmux"], "")

    def test_legacy_registration_requires_explicit_binding_without_read_migration(self):
        self.join()
        with sqlite3.connect(self.database) as conn:
            conn.execute("ALTER TABLE identities DROP COLUMN tmux_server_id")
        before = self.database.read_bytes()
        member = self.members()[0]
        self.assertEqual(member["terminal_presence"], "unknown")
        self.assertEqual(member["tmux"], "")
        self.assertIn("rejoin", member["terminal_detail"])
        self.assertEqual(self.database.read_bytes(), before)

    def test_notify_claim_uses_current_exact_terminal_after_rename(self):
        receiver = self.join(harness="codex")
        sender = json.loads(self.bus(
            "join", "sender", "sender", "test", "pull", "test-host", "no-tmux"
        ).stdout)["agent_id"]
        self.bus("send", sender, receiver, "synthetic", "test notification")
        self.tmux("rename-session", "-t", "=alpha", "renamed")
        self.env["NW_FLEET_PRIMARY_SESSION"] = "renamed"
        old = json.loads(self.bus("notify-claim", "test-host", "alpha:0.0").stdout)
        self.assertFalse(old["notify"])
        current = json.loads(self.bus("notify-claim", "test-host", "renamed:0.0").stdout)
        self.assertTrue(current["notify"])
        self.assertEqual(current["agent_id"], receiver)


if __name__ == "__main__":
    unittest.main()
