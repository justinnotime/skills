"""One scheduler covers every live fleet; the checkout patrol belongs to the machine."""

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


class TickFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fleet-tick-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        self.write_config()
        self.env = {
            "HOME": str(self.root), "PATH": os.environ["PATH"],
            "FLEET_ORCHESTRATOR_CONFIG": str(self.config),
            "PYTHONDONTWRITEBYTECODE": "1",
            "NW_DEFAULT_TMUX_SERVER": "fleet-tick-unused-" + uuid.uuid4().hex[:12],
        }
        if "TMUX_TMPDIR" in os.environ:
            self.env["TMUX_TMPDIR"] = os.environ["TMUX_TMPDIR"]

    def write_config(self, **extra):
        self.config.write_text(json.dumps({
            "schema": "fleet-runtime/v1",
            "runtime_dir": str(self.root / "machine"),
            "bus": {"transport": "local"},
            "fleets": {
                "profile_directory": str(self.root / "profiles"),
                "runtime_directory": str(self.root / "fleets"),
            },
            **extra,
        }))

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

    def saved(self, name):
        database = self.root / name / "tasks.sqlite3"
        database.parent.mkdir(parents=True, exist_ok=True)
        database.touch()
        return database


class FleetTickTest(TickFixture):
    def test_discovers_once_skips_empty_and_deduplicates_stores(self):
        database = self.saved("alpha")
        selected = {name: self.selection(name) for name in ("alpha", "empty")}
        selected["alias"] = self.selection("alias", database=database)
        selected["legacy"] = {**self.selection("legacy"), "NW_FLEET_PROFILE_PATH": "profile.json"}
        with mock.patch.object(tick.profile, "local_sessions", return_value={
            "alpha": "", "alias": "", "empty": "", "legacy": "",
        }) as discover, mock.patch.object(tick, "select_environment", side_effect=lambda name, _: selected[name]), \
                mock.patch.object(tick.profile, "bind_local_session") as bind, \
                mock.patch.object(tick, "run_tick", return_value=0) as run, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(tick.run(self.env), 0)
        discover.assert_called_once()
        bind.assert_not_called()
        # alias and alpha name one store; exactly one of them is scheduled.
        self.assertEqual(len(run.call_args_list), 1)
        self.assertIn(run.call_args.args[0]["NW_FLEET"], {"alpha", "alias"})
        self.assertIn("task store already scheduled", output.getvalue())
        self.assertIn("SKIP fleet empty: no saved tasks", output.getvalue())
        self.assertIn("explicit profile is not automatically scheduled", output.getvalue())

    def test_failure_does_not_prevent_other_fleets_and_discards_terminal_context(self):
        selected = {name: self.selection(name) for name in ("alpha", "beta")}
        for name in selected:
            self.saved(name)
        stale = {**self.env, "NW_FLEET": "old", "NW_FLEET_PROFILE_APPLIED": "old",
                 "TMUX": "stale", "TMUX_PANE": "%999", "DISPATCH_LEDGER_DB": "/wrong.sqlite3"}
        with mock.patch.object(tick.profile, "local_sessions", return_value={"alpha": "", "beta": ""}) as discover, \
                mock.patch.object(tick, "select_environment", side_effect=lambda name, _: selected[name]), \
                mock.patch.object(tick, "run_tick", side_effect=[RuntimeError("test failure"), 0]) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tick.run(stale), 1)
        self.assertEqual(run.call_count, 2)
        base = discover.call_args.args[0]
        for key in ("TMUX", "TMUX_PANE", "NW_FLEET", "NW_FLEET_PROFILE_APPLIED", "DISPATCH_LEDGER_DB"):
            self.assertNotIn(key, base)

    def test_failed_discovery_schedules_nothing_and_reports_the_failure(self):
        with mock.patch.object(tick.profile, "local_sessions", side_effect=ValueError("unavailable")), \
                mock.patch.object(tick, "run_tick", return_value=0) as run, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(tick.run(self.env), 1)
        run.assert_not_called()
        self.assertIn("FAIL local fleet discovery: unavailable", output.getvalue())

    def test_without_a_fleet_runtime_the_standalone_store_is_scheduled(self):
        self.config.write_text(json.dumps({
            "schema": "fleet-runtime/v1",
            "runtime_dir": str(self.root / "machine"),
            "bus": {"transport": "local"},
        }))
        with mock.patch.object(tick.profile, "local_sessions",
                               side_effect=AssertionError("standalone mode must not scan tmux")), \
                mock.patch.object(tick, "run_tick", return_value=0) as run, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(tick.run(self.env), 0)
        run.assert_called_once()
        self.assertNotIn("NW_FLEET", run.call_args.args[0])
        self.assertIn("RUN standalone store", output.getvalue())

    def test_no_live_fleet_is_a_quiet_success(self):
        with mock.patch.object(tick.profile, "local_sessions", return_value={}), \
                mock.patch.object(tick, "run_tick", return_value=0) as run, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(tick.run(self.env), 0)
        run.assert_not_called()
        self.assertNotIn("FAIL", output.getvalue())

    def test_a_blocked_fleet_does_not_delay_another(self):
        selected = {name: self.selection(name) for name in ("alpha", "beta")}
        for name in selected:
            self.saved(name)
        fast_done = threading.Event()

        def run_fleet(env, _dry_run):
            if env["NW_FLEET"] == "alpha":
                self.assertTrue(fast_done.wait(2), "a slow fleet blocked an independent fleet")
            else:
                fast_done.set()
            return 0

        with mock.patch.object(tick.profile, "local_sessions", return_value={"alpha": "", "beta": ""}), \
                mock.patch.object(tick, "select_environment", side_effect=lambda name, _: selected[name]), \
                mock.patch.object(tick, "run_tick", side_effect=run_fleet), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tick.run(self.env), 0)

    def test_local_sender_is_idempotent_and_never_messages_an_agent(self):
        (self.root / "fleets/alpha").mkdir(parents=True)  # saved work of a stopped fleet
        env = tick.profile.command_env("alpha", self.env)
        tick.ensure_local_sender(env)
        tick.ensure_local_sender(env)
        with sqlite3.connect(env["AGENT_BUS_DB"]) as conn:
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

    def test_scheduling_never_assigns_a_tmux_history_option(self):
        selected = self.selection("alpha")
        self.saved("alpha")
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run), \
                    mock.patch.object(tick.profile, "local_sessions", return_value={"alpha": ""}), \
                    mock.patch.object(tick.profile, "resolve", return_value=selected), \
                    mock.patch.object(tick.profile, "bind_local_session") as bind, \
                    mock.patch.object(tick, "run_tick", return_value=0) as run, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(tick.run(self.env, dry_run=dry_run), 0)
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
        run.assert_not_called()
        self.assertIn("open in multiple sessions", output.getvalue())


class CheckoutPatrolTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="checkout-patrol-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.json"
        self.env = {
            "HOME": str(self.root), "PATH": os.environ["PATH"],
            "FLEET_ORCHESTRATOR_CONFIG": str(self.config),
            "PYTHONDONTWRITEBYTECODE": "1",
            "NW_DEFAULT_TMUX_SERVER": "patrol-unused-" + uuid.uuid4().hex[:12],
        }

    def make_repo(self, name, *, bare=False):
        path = self.root / name
        args = ["git", "init", "-q"] + (["--bare"] if bare else []) + [str(path)]
        subprocess.run(args, check=True, capture_output=True)
        return path

    def test_dirty_checkout_found_with_mtimes_and_exempts(self):
        repo = self.make_repo("co")
        (repo / "junk.log").write_text("x")
        (repo / "spool").mkdir()
        (repo / "spool" / "runtime-file").write_text("x")
        findings = tick.checkout_findings(
            {"path": str(repo), "kind": "checkout", "exempt": ["spool/"]}, self.env)
        self.assertEqual(len(findings), 1)
        self.assertIn("junk.log", findings[0])
        self.assertIn("T", findings[0])

    def test_in_repo_worktree_dirs_are_flagged_not_excused(self):
        repo = self.make_repo("co")
        (repo / ".claude" / "worktrees" / "wt").mkdir(parents=True)
        (repo / ".claude" / "worktrees" / "wt" / "f").write_text("x")
        findings = tick.checkout_findings({"path": str(repo), "kind": "checkout"}, self.env)
        self.assertEqual(len(findings), 1)
        self.assertIn(".claude/worktrees/wt/f", findings[0])

    def test_clean_is_silent_and_absent_is_reported(self):
        repo = self.make_repo("co")
        self.assertEqual(tick.checkout_findings({"path": str(repo), "kind": "checkout"}, self.env), [])
        self.assertEqual(tick.checkout_findings(
            {"path": str(self.root / "nope"), "kind": "checkout"}, self.env), ["MISSING CHECKOUT"])

    def test_failed_inspection_is_not_reported_as_clean(self):
        repo = self.make_repo("co")
        for kind in ("checkout", "bare-hub"):
            with self.subTest(kind=kind), mock.patch.object(tick.subprocess, "run") as run:
                run.return_value = subprocess.CompletedProcess(["git"], 1, "", "")
                findings = tick.checkout_findings({"path": str(repo), "kind": kind}, self.env)
                self.assertEqual(len(findings), 1)
                self.assertIn("CHECK FAILED", findings[0])
        with mock.patch.object(tick.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 30)):
            self.assertEqual(tick.checkout_findings({"path": str(repo), "kind": "checkout"}, self.env),
                             ["CHECK FAILED: git inspection unavailable"])

    def test_bare_hub_regression_flagged(self):
        hub = self.make_repo("hub.git", bare=True)
        self.assertEqual(tick.checkout_findings({"path": str(hub), "kind": "bare-hub"}, self.env), [])
        nonbare = self.make_repo("co")
        self.assertEqual(tick.checkout_findings({"path": str(nonbare), "kind": "bare-hub"}, self.env),
                         ["NON-BARE"])

    def test_patrol_writes_machine_state_and_never_creates_fleet_work(self):
        dirty = self.make_repo("dirty")
        (dirty / "leaked.tmp").write_text("x")
        clean = self.make_repo("clean")
        self.config.write_text(json.dumps({
            "schema": "fleet-runtime/v1",
            "runtime_dir": str(self.root / "machine"),
            "fleets": {"runtime_directory": str(self.root / "fleets")},
            "watched_repositories": [
                {"path": str(dirty), "kind": "checkout", "exempt": []},
                {"path": str(clean), "kind": "checkout", "exempt": []},
            ],
        }))
        state = self.root / "machine/state/fleet-orchestrator/checkout-patrol.json"
        with mock.patch.object(tick.profile, "local_sessions", return_value={}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(tick.run(self.env, dry_run=True), 0)
        self.assertIn("WARN checkout patrol", output.getvalue())
        self.assertIn("leaked.tmp", output.getvalue())
        self.assertIn("OK checkout patrol: 1 clean, 1 not clean", output.getvalue())
        self.assertFalse(state.exists(), "a dry run must not write the status file")
        with mock.patch.object(tick.profile, "local_sessions", return_value={}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(tick.run(self.env), 0)
        report = json.loads(state.read_text())
        self.assertEqual(report["schema"], "checkout-patrol/v1")
        self.assertEqual(len(report["repositories"][str(dirty)]["findings"]), 1)
        self.assertEqual(report["repositories"][str(clean)]["findings"], [])
        self.assertFalse((self.root / "fleets").exists(), "the patrol must not create fleet state")
        self.assertFalse(list((self.root / "machine").rglob("*.sqlite3")),
                         "the patrol must not open or create a task store")

    def test_malformed_patrol_entry_fails_loudly(self):
        self.config.write_text(json.dumps({
            "schema": "fleet-runtime/v1",
            "runtime_dir": str(self.root / "machine"),
            "fleets": {"runtime_directory": str(self.root / "fleets")},
            "watched_repositories": [{"kind": "checkout"}],
        }))
        with mock.patch.object(tick.profile, "local_sessions", return_value={}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(tick.run(self.env), 1)
        self.assertIn("FAIL checkout patrol", output.getvalue())


@unittest.skipUnless(shutil.which("tmux"), "tmux is required")
class RealFanoutTest(TickFixture):
    def test_real_fanout_keeps_fleet_work_independent_and_scans_no_projects(self):
        server = "fleet-tick-" + uuid.uuid4().hex[:12]
        self.env["NW_DEFAULT_TMUX_SERVER"] = server

        def command(*args, check=True):
            result = subprocess.run([str(arg) for arg in args], env=self.env, text=True,
                                    capture_output=True, timeout=30)
            if check:
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result

        self.addCleanup(lambda: command("tmux", "-L", server, "kill-server", check=False))
        for name in ("alpha", "beta", "empty"):
            command(ROOT / "scripts/orc", "fleet", name, "start")
        command("tmux", "-L", server, "new-session", "-d", "-s", "plain", "sleep 300")
        for name in ("alpha", "beta"):
            command(ROOT / "scripts/orc", "--fleet", name, "open", "--to", "operator",
                    "--subject", "synthetic work", "--body", "test", "--no-check")
        beta_lock = self.root / "fleets/beta/cache/locks/fleet-orchestrator.lock"
        beta_lock.parent.mkdir(parents=True, exist_ok=True)
        self.write_config(github={"owner": "example"},
                          authority={"merge_keys": {"unrelated-project": "operator"}})
        fake_gh = self.root / "gh"
        gh_log = self.root / "gh-called"
        fake_gh.write_text(f"#!/bin/sh\ntouch '{gh_log}'\nprintf '[]\\n'\n")
        fake_gh.chmod(0o755)
        self.env["NW_GH_CLI"] = str(fake_gh)
        with beta_lock.open("w") as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = command(sys.executable, ROOT / "scripts/fleet-tick.py")
        self.assertIn("RUN fleet alpha", result.stdout)
        self.assertIn("RUN fleet beta", result.stdout)
        self.assertIn("[beta] SKIP another fleet-orchestrator tick holds the lock", result.stdout)
        self.assertIn("SKIP fleet empty: no saved tasks", result.stdout)
        self.assertNotIn("plain", result.stdout)
        self.assertTrue((self.root / "fleets/alpha/state/fleet-orchestrator/tick-last.json").is_file())
        self.assertFalse((self.root / "fleets/beta/state/fleet-orchestrator/tick-last.json").exists())
        self.assertFalse((self.root / "fleets/empty/state/fleet-orchestrator/dispatch-ledger.sqlite3").exists())
        self.assertFalse((self.root / "fleets/plain").exists())
        self.assertFalse(gh_log.exists(), "a fleet scanned projects it never registered")
        self.assertFalse((self.root / "profiles").exists())


if __name__ == "__main__":
    unittest.main()
