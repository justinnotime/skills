"""Exercise the shipped CLI in a copied package with only synthetic state."""

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class StandaloneBusTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.package = self.base / "standalone package"
        shutil.copytree(ROOT / "scripts", self.package / "scripts",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.home = self.base / "home"
        self.home.mkdir()
        self.config = self.base / "private config.json"
        self.config.write_text(json.dumps({
            "schema": "fleet-runtime/v1",
            "bus": {"transport": "local", "config_directory": str(self.base / "bus"),
                    "database": str(self.base / "state" / "bus.sqlite3")},
        }))
        self.environment = {
            "HOME": str(self.home), "XDG_CONFIG_HOME": str(self.home / "config"),
            "PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        }

    def run_bus(self, *arguments, success=True):
        result = subprocess.run(
            ["bash", str(self.package / "scripts/agent-bus"),
             "--config", str(self.config), *arguments],
            env=self.environment, text=True, capture_output=True, timeout=15,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_local_delivery_and_ack_work_without_a_private_repository(self):
        sender = json.loads(self.run_bus(
            "join", "host/sender", "sender", "test", "pull", "host", "no-tmux"
        ).stdout)["agent_id"]
        receiver = json.loads(self.run_bus(
            "join", "host/receiver", "receiver", "test", "pull", "host", "no-tmux"
        ).stdout)["agent_id"]
        sent = json.loads(self.run_bus("send", sender, receiver, "sample", "synthetic body").stdout)
        inbox = [json.loads(line) for line in self.run_bus("pull", receiver).stdout.splitlines()]
        self.assertEqual(inbox[0]["body"], "synthetic body")
        self.run_bus("ack", receiver, sent["msg_id"], "ok")
        status = json.loads(self.run_bus("delivery", sender, sent["msg_id"]).stdout)
        self.assertEqual(status["transport_state"], "accepted")
        self.assertEqual(status["recipients"][0]["processed_status"], "ok")
        self.assertTrue((self.base / "state" / "bus.sqlite3").is_file())

    def test_matrix_requires_explicit_endpoints(self):
        self.config.write_text(json.dumps({"bus": {"transport": "matrix"}}))
        result = self.run_bus("members", success=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Matrix requires an explicit homeserver", result.stderr)

    def test_members_before_first_join_is_empty_without_creating_state(self):
        result = self.run_bus("members")
        self.assertEqual(result.stdout, "")
        self.assertFalse((self.base / "bus").exists())
        self.assertFalse((self.base / "state").exists())

    def test_members_does_not_migrate_or_repair_an_existing_database(self):
        joined = json.loads(self.run_bus(
            "join", "host/reader", "reader", "test", "pull", "host", "no-tmux"
        ).stdout)
        database = self.base / "state" / "bus.sqlite3"
        with sqlite3.connect(database) as conn:
            conn.execute("DROP TABLE inbox_signal")
        before = database.read_bytes()
        members = [json.loads(line) for line in self.run_bus("members").stdout.splitlines()]
        self.assertEqual([member["agent_id"] for member in members], [joined["agent_id"]])
        self.assertEqual(database.read_bytes(), before)
        with sqlite3.connect(database) as conn:
            self.assertIsNone(conn.execute(
                "SELECT name FROM sqlite_master WHERE name='inbox_signal'"
            ).fetchone())

    def test_unreadable_members_are_an_error_not_an_empty_or_repaired_bus(self):
        database = self.base / "state" / "bus.sqlite3"
        database.parent.mkdir()
        for content in (b"not a sqlite database", b""):
            with self.subTest(content=content):
                database.write_bytes(content)
                result = self.run_bus("members", success=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("Agent Bus members unavailable", result.stderr)
                self.assertEqual(database.read_bytes(), content)
                self.assertFalse((self.base / "bus").exists())


if __name__ == "__main__":
    unittest.main()
