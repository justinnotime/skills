import importlib.util
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ORC = ROOT / "scripts" / "fleet-orchestrator.py"
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import workplane as wp


class OrcTmuxObservationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.runtime = base / "runtime"
        self.env = dict(os.environ)
        config = base / "config.json"
        config.write_text("{}")
        self.env.update({
            "FLEET_ORCHESTRATOR_CONFIG": str(config),
            "DISPATCH_LEDGER_DB": str(base / "ledger.sqlite3"),
            "NOTES_RUNTIME_DIR": str(self.runtime),
            "MATRIX_BUS_CFG": str(base / "bus"),
            "NW_BUS_CLI": str(base / "fake-bus.sh"),
            "NW_TMUX_SERVER": "unreachable-test-server",
            "DISPATCH_LEDGER_ACTOR": "test",
            "NW_GH_CLI": str(base / "gh"),
        })
        member = json.dumps({
            "agent_id": "fake-tmux2", "handle": "test/tmux2",
            "aliases": ["tmux2"],
            "host": socket.gethostname().split(".", 1)[0],
            "tmux": "tmux=0:2.0 win=codex", "status": "active",
            "addressable": True,
        })
        Path(self.env["NW_BUS_CLI"]).write_text(
            f"#!/bin/sh\nif [ \"$1\" = members ]; then\n"
            f"cat <<'EOF'\n{member}\nEOF\nexit 0\nfi\nexit 1\n"
        )
        Path(self.env["NW_BUS_CLI"]).chmod(0o755)
        Path(self.env["NW_GH_CLI"]).write_text("#!/bin/sh\necho '[]'\n")
        Path(self.env["NW_GH_CLI"]).chmod(0o755)
        subprocess.run([sys.executable, str(ORC), "open", "--to", "tmux2",
                        "--subject", "observe me", "--check", "true"],
                       check=True, capture_output=True, env=self.env)
        with mock.patch.dict(os.environ, self.env, clear=True):
            conn = wp.connect_writable()
            self.assertTrue(wp.members_available(conn))
            row = conn.execute("SELECT * FROM dispatch").fetchone()
            context = wp.continuation_context(conn, row)
            conn.execute(
                "INSERT INTO drive(task_id,seat,generation,st,cycles,grace_used,"
                "idle_waits,absent_ticks,updated_ms) VALUES(?,?,?,?,?,?,?,?,?)",
                (row["id"], context["seat"], context["generation"],
                 "pulled", 0, 0, 3, 4, 1),
            )
            conn.commit()
            conn.close()
        self.addCleanup(self.tmp.cleanup)

    def test_unreachable_server_does_not_advance_absence_or_escalate(self):
        out = subprocess.run([sys.executable, str(ORC), "tick"], text=True,
                             capture_output=True, env=self.env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("tmux observation unavailable", out.stdout)
        conn = sqlite3.connect(self.env["DISPATCH_LEDGER_DB"])
        row = conn.execute(
            "SELECT d.state, x.st, x.absent_ticks FROM dispatch d JOIN drive x"
            " ON x.task_id=d.id"
        ).fetchone()
        self.assertEqual(row, ("open", "pulled", 4))

    def test_unreachable_env_selector_makes_doctor_fail_readably(self):
        out = subprocess.run([sys.executable, str(ORC), "doctor"], text=True,
                             capture_output=True, env=self.env)
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("FAIL  tmux observation unavailable", out.stdout)
        self.assertNotIn("Traceback", out.stderr)

    def test_invalid_machine_selector_makes_doctor_fail_readably(self):
        env = dict(self.env)
        env.pop("NW_TMUX_SERVER")
        selector = self.runtime / "state" / "fleet-orchestrator" / "tmux-server"
        selector.parent.mkdir(parents=True, exist_ok=True)
        selector.write_text("bad server -- args\n")
        out = subprocess.run([sys.executable, str(ORC), "doctor"], text=True,
                             capture_output=True, env=env)
        self.assertEqual(out.returncode, 1, out.stdout + out.stderr)
        self.assertIn("FAIL  tmux observation unavailable", out.stdout)
        self.assertNotIn("Traceback", out.stderr)


if __name__ == "__main__":
    unittest.main()
