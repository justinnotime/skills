"""One scheduler discovers current local work without per-fleet jobs."""

import contextlib
import fcntl
import importlib.util
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("fleet_tick_test", ROOT / "scripts/fleet-tick.py")
tick = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(tick)


class FleetTickTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fleet-tick-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
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
        self.env = {
            "HOME": str(self.root), "PATH": os.environ["PATH"],
            "FLEET_ORCHESTRATOR_CONFIG": str(self.config),
            "PYTHONDONTWRITEBYTECODE": "1",
            "NW_DEFAULT_TMUX_SERVER": "fleet-tick-unused-" + uuid.uuid4().hex[:12],
        }
        if "TMUX_TMPDIR" in os.environ:
            self.env["TMUX_TMPDIR"] = os.environ["TMUX_TMPDIR"]

    def selection(self, name, *, database=None):
        root = self.root / name
        return {
            **self.env,
            "NW_FLEET": name, "NW_FLEET_PROFILE_APPLIED": name,
            "NW_FLEET_PRIMARY_SESSION": name,
            "NW_FLEET_PROFILE_PATH": "",
            "NOTES_RUNTIME_DIR": str(root),
            "DISPATCH_LEDGER_DB": str(database or root / "tasks.sqlite3"),
            "AGENT_BUS_TRANSPORT": "local",
            "AGENT_BUS_CFG": str(root / "bus"),
            "AGENT_BUS_DB": str(root / "bus/inbox.sqlite3"),
        }

    def test_discovers_once_skips_empty_and_deduplicates_stores(self):
        database = self.root / "alpha/tasks.sqlite3"
        database.parent.mkdir()
        database.touch()
        selected = {name: self.selection(name) for name in ("default", "alpha", "empty")}
        selected["alias"] = self.selection("alias", database=database)
        selected["legacy"] = {**self.selection("legacy"), "NW_FLEET_PROFILE_PATH": "profile.json"}
        with mock.patch.object(tick.profile, "local_sessions", return_value={
            "0": "", "alpha": "", "alias": "", "empty": "", "legacy": "",
        }) as discover, mock.patch.object(tick, "select_environment", side_effect=lambda name, _: selected[name]), \
                mock.patch.object(tick.profile, "bind_local_session"), \
                mock.patch.object(tick, "run_tick", return_value=0) as run, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(tick.run(self.env), 0)
        discover.assert_called_once()
        self.assertCountEqual([call.args[0]["NW_FLEET"] for call in run.call_args_list], ["default", "alpha"])
        self.assertIn("task store already scheduled", output.getvalue())
        self.assertIn("no saved tasks", output.getvalue())
        self.assertIn("explicit profile is not automatically scheduled", output.getvalue())

    def test_failure_does_not_prevent_other_fleets_and_discards_terminal_context(self):
        selected = {name: self.selection(name) for name in ("default", "alpha")}
        path = Path(selected["alpha"]["DISPATCH_LEDGER_DB"])
        path.parent.mkdir()
        path.touch()
        stale = {**self.env, "NW_FLEET": "old", "NW_FLEET_PROFILE_APPLIED": "old",
                 "TMUX": "stale", "TMUX_PANE": "%999", "DISPATCH_LEDGER_DB": "/wrong.sqlite3"}
        with mock.patch.object(tick.profile, "local_sessions", return_value={"alpha": ""}) as discover, \
                mock.patch.object(tick, "select_environment", side_effect=lambda name, _: selected[name]), \
                mock.patch.object(tick.profile, "bind_local_session"), \
                mock.patch.object(tick, "run_tick", side_effect=[RuntimeError("test failure"), 0]) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tick.run(stale), 1)
        self.assertEqual(run.call_count, 2)
        base = discover.call_args.args[0]
        self.assertNotIn("TMUX", base)
        self.assertNotIn("TMUX_PANE", base)
        self.assertNotIn("NW_FLEET", base)
        self.assertNotIn("DISPATCH_LEDGER_DB", base)

    def test_failed_discovery_still_runs_default(self):
        with mock.patch.object(tick.profile, "local_sessions", side_effect=ValueError("unavailable")), \
                mock.patch.object(tick, "run_tick", return_value=0) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tick.run(self.env), 1)
        run.assert_called_once()

    def test_blocked_default_does_not_delay_local_fleet(self):
        selected = {name: self.selection(name) for name in ("default", "alpha")}
        path = Path(selected["alpha"]["DISPATCH_LEDGER_DB"])
        path.parent.mkdir()
        path.touch()
        local_done = threading.Event()

        def run_fleet(env, _dry_run):
            if env["NW_FLEET"] == "default":
                self.assertTrue(local_done.wait(2), "default blocked an independent local fleet")
            else:
                local_done.set()
            return 0

        with mock.patch.object(tick.profile, "local_sessions", return_value={"alpha": ""}), \
                mock.patch.object(tick, "select_environment", side_effect=lambda name, _: selected[name]), \
                mock.patch.object(tick.profile, "bind_local_session"), \
                mock.patch.object(tick, "run_tick", side_effect=run_fleet), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tick.run(self.env), 0)

    def test_local_sender_is_idempotent_and_never_messages_an_agent(self):
        env = self.env
        tick.ensure_local_sender(env)
        tick.ensure_local_sender(env)
        with sqlite3.connect(self.root / "default/bus/inbox.sqlite3") as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM identities").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT harness,mode,pane_id FROM identities").fetchone(),
                             ("cron", "pull", None))
            self.assertEqual(conn.execute("SELECT count(*) FROM outbox").fetchone()[0], 0)
            conn.execute("UPDATE identities SET harness='claude'")
            conn.commit()
        with self.assertRaisesRegex(ValueError, "conflicts"):
            tick.ensure_local_sender(env)

    def test_dry_run_never_provisions_sender(self):
        with mock.patch.object(tick, "ensure_local_sender") as sender, \
                mock.patch.object(tick.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as child:
            self.assertEqual(tick.run_tick(self.selection("alpha"), True), 0)
        sender.assert_not_called()
        self.assertEqual(child.call_args.args[0][-1], "--dry-run")

    def test_default_scope_uses_the_actual_surviving_default_session(self):
        with mock.patch.object(tick.profile, "_terminal_target", return_value={
            "name": "default", "tmux_server": "default", "primary_session": "tview-original",
        }):
            selected = tick.select_environment("default", self.env)
        self.assertEqual(selected["NW_FLEET_PRIMARY_SESSION"], "tview-original")
        with mock.patch.dict(os.environ, selected, clear=True):
            import tmux_runtime
            self.assertEqual(tmux_runtime.pane_scope(), ["-s", "-t", "=tview-original:"])

    def test_dry_preparation_does_not_assign_a_tmux_history_option(self):
        selected = self.selection("alpha")
        database = Path(selected["DISPATCH_LEDGER_DB"])
        database.parent.mkdir()
        database.touch()
        with mock.patch.object(tick.profile, "local_sessions", return_value={"alpha": ""}), \
                mock.patch.object(tick.profile, "resolve", return_value=selected), \
                mock.patch.object(tick.profile, "bind_local_session") as bind, \
                mock.patch.object(tick, "run_tick", return_value=0) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tick.run(self.env, dry_run=True), 0)
        bind.assert_not_called()
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0]["NW_FLEET_PRIMARY_SESSION"], "alpha")

    def test_multiple_live_sessions_cannot_schedule_one_saved_runtime(self):
        sessions = {
            name: {"session": name, "group": "", "id": session_id, "runtime_key": "alpha"}
            for name, session_id in (("alpha", "$1"), ("beta", "$2"))
        }
        with mock.patch.object(tick.profile, "local_session_details", return_value=sessions), \
                mock.patch.object(tick, "run_tick", return_value=0) as run, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(tick.run(self.env), 1)
        run.assert_called_once()  # Only the compatible default may run.
        self.assertIn("open in multiple sessions", output.getvalue())

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required")
    def test_real_fanout_keeps_local_work_independent_of_default_lock_and_project_scans(self):
        server = "fleet-tick-" + uuid.uuid4().hex[:12]
        self.env["NW_DEFAULT_TMUX_SERVER"] = server

        def command(*args, check=True):
            result = subprocess.run([str(arg) for arg in args], env=self.env, text=True,
                                    capture_output=True, timeout=30)
            if check:
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result

        self.addCleanup(lambda: command("tmux", "-L", server, "kill-server", check=False))
        command("tmux", "-L", server, "new-session", "-d", "-s", "alpha", "sleep 300")
        command("tmux", "-L", server, "new-session", "-d", "-s", "empty", "sleep 300")
        for selection in (["--fleet", "default"], ["--fleet", "alpha"]):
            command(ROOT / "scripts/orc", *selection, "open", "--to", "operator",
                    "--subject", "synthetic work", "--body", "test", "--no-check")
        default_lock = self.root / "default/locks/fleet-orchestrator.lock"
        default_lock.parent.mkdir(parents=True)
        config = json.loads(self.config.read_text())
        config["github"] = {"owner": "example"}
        config["authority"] = {"merge_keys": {"unrelated-project": "operator"}}
        self.config.write_text(json.dumps(config))
        fake_gh = self.root / "gh"
        gh_log = self.root / "gh-called"
        fake_gh.write_text(f"#!/bin/sh\ntouch '{gh_log}'\nprintf '[]\\n'\n")
        fake_gh.chmod(0o755)
        self.env["NW_GH_CLI"] = str(fake_gh)
        with default_lock.open("w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = command(sys.executable, ROOT / "scripts/fleet-tick.py")
        self.assertIn("another fleet-orchestrator tick holds the lock", result.stdout)
        self.assertIn("RUN fleet alpha", result.stdout)
        self.assertIn("SKIP fleet empty: no saved tasks", result.stdout)
        self.assertTrue((self.root / "fleets/alpha/state/fleet-orchestrator/tick-last.json").is_file())
        self.assertFalse((self.root / "fleets/empty/state/fleet-orchestrator/dispatch-ledger.sqlite3").exists())
        self.assertFalse(gh_log.exists(), "a local fleet scanned the default fleet's projects")
        self.assertFalse((self.root / "profiles").exists())


if __name__ == "__main__":
    unittest.main()
