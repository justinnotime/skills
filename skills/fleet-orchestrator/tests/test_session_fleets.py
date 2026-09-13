"""Exercise the explicit fleet lifecycle and bus separation on a private real tmux server."""

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
            "runtime_dir": str(self.root / "machine"),
            "bus": {"transport": "local"},
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
        """A plain tmux session: a terminal, never a fleet."""
        self.tmux("new-session", "-d", "-s", name, "sleep 300")

    def start(self, name):
        return self.run_command([ORC, "fleet", name, "start"])

    def view(self, name):
        return json.loads(self.run_command([ORC, "fleet", "show", name]).stdout)

    def bus(self, name, *args, **kwargs):
        return self.run_command([BUS, "--fleet", name, *args], **kwargs)

    def inventory(self):
        return [row["name"] for row in json.loads(self.run_command([ORC, "--json"]).stdout)]

    def join(self, name, handle, window=0):
        pane = self.tmux("display-message", "-p", "-t", f"={name}:{window}.0", "#{pane_id}").stdout.strip()
        result = self.bus(name, "join", handle, handle, "test", "pull",
                          socket.gethostname().split('.')[0], f"tmux={name}:{window}.0 win=test",
                          env={**self.env, "TMUX_PANE": pane})
        return json.loads(result.stdout)["agent_id"]

    def open_task(self, name, subject):
        self.run_command([ORC, "--fleet", name, "open", "--to", "operator",
                          "--subject", subject, "--body", "Synthetic test task.", "--no-check"])

    def session_environment(self, name):
        fields = self.tmux("display-message", "-p", "-t", f"={name}:0.0",
                           "#{socket_path}|#{pid}|#{session_id}|#{pane_id}").stdout.strip().split("|")
        return {**self.env, "TMUX": f"{fields[0]},{fields[1]},{fields[2][1:]}", "TMUX_PANE": fields[3]}

    def configure_handoffs(self):
        directory = self.root / "configured-handoffs"
        directory.mkdir()
        (directory / "2020-01-01-shared-worker.md").write_text("Configured fleet history.\n")
        marker = self.root / "publisher-invocations"
        publisher = self.root / "publish-handoff.py"
        publisher.write_text(
            "import os, pathlib, shutil\n"
            "directory = pathlib.Path(os.environ['ORC_HANDOFF_DIRECTORY'])\n"
            "directory.mkdir(parents=True, exist_ok=True)\n"
            "shutil.copyfile(os.environ['ORC_HANDOFF_SRC'], "
            "directory / os.environ['ORC_HANDOFF_DST'])\n"
            f"with pathlib.Path({str(marker)!r}).open('a') as output:\n"
            "    output.write(os.environ['ORC_HANDOFF_DST'] + '\\n')\n")
        config = json.loads(self.config.read_text())
        config["handoff"] = {"directory": str(directory),
                             "publish_command": [sys.executable, str(publisher)]}
        self.config.write_text(json.dumps(config))
        return directory, marker

    def test_started_fleets_isolate_handoff_reads_writes_and_same_named_agents(self):
        configured, published = self.configure_handoffs()
        notes = {}
        for name in ("alpha", "beta"):
            self.start(name)
            identity = self.join(name, "shared-worker")
            self.open_task(name, "Synthetic handoff test")
            view = self.view(name)
            topology = self.run_command([ORC, "--fleet", name, "topology"]).stdout
            onboard = self.run_command([ORC, "--fleet", name, "onboard", identity]).stdout
            self.assertNotIn("2020-01-01-shared-worker.md", topology + onboard)
            self.run_command([ORC, "--fleet", name, "checkout", identity,
                              "--summary", f"Work from {name} only."])
            directory = Path(view["dispatch_ledger_db"]).parent / "handoffs"
            files = list(directory.glob("*.md"))
            self.assertEqual(len(files), 1)
            notes[name] = files[0]
            onboard = self.run_command([ORC, "--fleet", name, "onboard", "shared-worker"]).stdout
            self.assertIn(str(files[0]), onboard)
            self.assertNotIn(str(configured), onboard)
            with sqlite3.connect(view["agent_bus_db"]) as db:
                self.assertEqual(db.execute("SELECT status FROM identities WHERE agent_id=?",
                                            (identity,)).fetchone()[0], "retired")
        self.assertEqual(notes["alpha"].name, notes["beta"].name)
        self.assertNotEqual(notes["alpha"].parent, notes["beta"].parent)
        self.assertIn("Work from alpha only.", notes["alpha"].read_text())
        self.assertIn("Work from beta only.", notes["beta"].read_text())
        self.assertEqual(list(configured.iterdir()), [configured / "2020-01-01-shared-worker.md"])
        self.assertFalse(published.exists())

    def test_explicit_legacy_fleet_retains_configured_handoff_publication(self):
        configured, published = self.configure_handoffs()
        legacy_server = self.server + "-legacy"
        self.addCleanup(lambda: self.run_command(
            ["tmux", "-u", "-L", legacy_server, "kill-server"], check=False))
        self.run_command([ORC, "fleet", "create", "legacy", "--tmux-server", legacy_server,
                          "--primary-session", "work"])
        pane = self.run_command(["tmux", "-u", "-L", legacy_server, "display-message", "-p",
                                 "-t", "=work:0.0", "#{pane_id}"]).stdout.strip()
        result = self.bus("legacy", "join", "legacy-worker", "legacy-worker", "test", "pull",
                          socket.gethostname().split('.')[0], "tmux=work:0.0 win=test",
                          env={**self.env, "TMUX_PANE": pane})
        identity = json.loads(result.stdout)["agent_id"]
        self.run_command([ORC, "--fleet", "legacy", "checkout", identity,
                          "--summary", "Explicit legacy archive publication."])
        names = published.read_text().splitlines()
        self.assertEqual(len(names), 1)
        self.assertIn("Explicit legacy archive publication.", (configured / names[0]).read_text())

    def test_plain_tmux_sessions_are_not_fleets_until_started(self):
        self.native_session("alpha")
        self.native_session("beta")
        result = self.run_command([ORC, "fleet", "show", "alpha"], check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not exist", result.stderr)
        self.assertIn("orc fleet alpha start", result.stderr)
        self.assertEqual(self.inventory(), [])
        listing = json.loads(self.run_command([ROOT / "scripts/tview", "--list", "--json"]).stdout)
        self.assertEqual(listing, [])
        # A plain session cannot be scheduled, entered or registered against.
        tick = self.run_command([ORC, "admin", "tick", "--dry-run"]).stdout
        self.assertNotIn("alpha", tick)
        entered = self.run_command([ROOT / "scripts/tview", "-t", "alpha"], check=False)
        self.assertNotEqual(entered.returncode, 0)
        joined = self.bus("alpha", "members", check=False)
        self.assertNotEqual(joined.returncode, 0)

        started = self.start("alpha").stdout
        self.assertIn("started fleet 'alpha'", started)
        self.assertEqual(self.inventory(), ["alpha"])
        self.assertEqual(self.tmux("show-options", "-qv", "-t", "alpha", "@orc-runtime").stdout.strip(), "alpha")
        self.assertTrue((self.root / "fleets/alpha").is_dir())
        self.assertFalse((self.root / "profiles").exists())
        alpha = self.view("alpha")
        self.assertEqual(alpha["profile_path"], "")
        self.assertEqual(alpha["tmux_server"], self.server)
        self.start("beta")
        self.assertNotEqual(alpha["agent_bus_db"], self.view("beta")["agent_bus_db"])
        sender = self.join("alpha", "sender")
        self.join("beta", "receiver")
        result = self.bus("alpha", "send", sender, "receiver", "test", "test", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("0 active agents", result.stderr)

    def test_full_lifecycle_closes_grouped_views_and_preserves_work(self):
        self.start("alpha")
        self.native_session("beta")
        self.run_command([ORC, "fleet", "window", "alpha"])
        self.tmux("new-session", "-d", "-t", "alpha", "-s", "tview-test")
        windows = self.tmux("list-windows", "-t", "=alpha", "-F", "#{window_id}").stdout.splitlines()
        self.assertEqual(len(windows), 2)
        self.join("alpha", "worker")
        self.open_task("alpha", "saved work")
        view = self.view("alpha")
        self.run_command([ORC, "fleet", "stop", "alpha"])
        self.assertNotEqual(self.tmux("has-session", "-t", "=alpha", check=False).returncode, 0)
        self.assertNotEqual(self.tmux("has-session", "-t", "=tview-test", check=False).returncode, 0)
        self.tmux("has-session", "-t", "=beta")
        with sqlite3.connect(view["agent_bus_db"]) as db:
            self.assertEqual(db.execute("SELECT status FROM identities").fetchone()[0], "retired")
        rows = json.loads(self.run_command([ORC, "--json"]).stdout)
        self.assertEqual([(row["name"], row["status"]) for row in rows], [("alpha", "stopped")])
        self.assertIn("orc fleet alpha retire", rows[0]["detail"])
        self.assertIn("saved work", self.run_command([ORC, "--fleet", "alpha", "board"]).stdout)
        stopped = self.run_command([ORC, "fleet", "stop", "alpha"], check=False)
        self.assertNotEqual(stopped.returncode, 0)
        self.assertIn("no running session", stopped.stderr)
        self.assertIn("started fleet 'alpha'", self.start("alpha").stdout)
        self.assertIn("saved work", self.run_command([ORC, "--fleet", "alpha", "board"]).stdout)
        self.assertFalse((self.root / "profiles").exists())

    def test_retire_archives_saved_work_and_retires_remaining_seats(self):
        self.start("alpha")
        self.start("beta")
        worker = self.join("alpha", "worker")
        self.open_task("alpha", "unfinished alpha work")
        view = self.view("alpha")
        self.run_command([ORC, "fleet", "stop", "alpha"])
        headless = self.bus("alpha", "join", "helper", "helper", "cron", "pull",
                            socket.gethostname().split('.')[0], "headless=test")
        helper = json.loads(headless.stdout)["agent_id"]
        retired = self.run_command([ORC, "fleet", "alpha", "retire"]).stdout
        self.assertIn("retired fleet 'alpha'", retired)
        self.assertIn("1 task(s) were still open", retired)
        self.assertIn("1 remaining seat registration(s)", retired)
        archives = list((self.root / "fleets-archive").iterdir())
        self.assertEqual(len(archives), 1)
        self.assertTrue(archives[0].name.startswith("alpha-"))
        self.assertFalse((self.root / "fleets/alpha").exists())
        self.assertIn(str(archives[0]), retired)
        archived_bus = archives[0] / Path(view["agent_bus_db"]).relative_to(self.root / "fleets/alpha")
        with sqlite3.connect(archived_bus) as db:
            kinds = dict(db.execute("SELECT agent_id, retired_kind FROM identities").fetchall())
            active = db.execute("SELECT count(*) FROM identities WHERE status='active'").fetchone()[0]
        self.assertEqual(kinds[helper], "fleet-retired")
        self.assertEqual(active, 0)
        self.assertIn(worker, kinds)
        self.assertEqual(self.inventory(), ["beta"])
        listing = json.loads(self.run_command([ROOT / "scripts/tview", "--list", "--json"]).stdout)
        self.assertEqual([row["name"] for row in listing], ["beta"])
        gone = self.run_command([ORC, "fleet", "show", "alpha"], check=False)
        self.assertNotEqual(gone.returncode, 0)
        self.assertIn("does not exist", gone.stderr)
        again = self.run_command([ORC, "fleet", "alpha", "retire"], check=False)
        self.assertNotEqual(again.returncode, 0)
        # The name is free again and starts empty; the archive is not resurrected.
        self.start("alpha")
        self.assertIn("No open tasks.", self.run_command([ORC, "--fleet", "alpha", "board"]).stdout)
        self.assertEqual(len(list((self.root / "fleets-archive").iterdir())), 1)
        self.tmux("has-session", "-t", "=beta")

    def test_retire_of_a_running_fleet_stops_it_first(self):
        self.start("alpha")
        self.join("alpha", "worker")
        self.tmux("new-session", "-d", "-t", "alpha", "-s", "tview-test")
        self.native_session("beta")
        self.run_command([ORC, "fleet", "alpha", "retire"])
        self.assertNotEqual(self.tmux("has-session", "-t", "=alpha", check=False).returncode, 0)
        self.assertNotEqual(self.tmux("has-session", "-t", "=tview-test", check=False).returncode, 0)
        self.tmux("has-session", "-t", "=beta")
        self.assertFalse((self.root / "fleets/alpha").exists())
        self.assertEqual(self.inventory(), [])

    def test_retire_from_inside_its_own_session_is_refused(self):
        self.start("alpha")
        result = self.run_command([ORC, "fleet", "alpha", "retire"],
                                  env=self.session_environment("alpha"), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("from outside", result.stderr)
        self.tmux("has-session", "-t", "=alpha")
        self.assertTrue((self.root / "fleets/alpha").is_dir())

    def test_start_after_tmux_server_death_retires_dead_seats_and_lists_them(self):
        self.start("alpha")
        self.run_command([ORC, "fleet", "window", "alpha"])
        worker = self.join("alpha", "worker", window=1)
        self.open_task("alpha", "survives the server")
        view = self.view("alpha")
        self.tmux("kill-server")
        rows = json.loads(self.run_command([ORC, "--json"]).stdout)
        self.assertEqual([(row["name"], row["status"]) for row in rows], [("alpha", "stopped")])
        with sqlite3.connect(view["agent_bus_db"]) as db:
            self.assertEqual(db.execute("SELECT status FROM identities WHERE agent_id=?",
                                        (worker,)).fetchone()[0], "active")
        started = self.start("alpha").stdout
        self.assertIn("started fleet 'alpha'", started)
        self.assertIn("retired 1 seat(s) whose terminals no longer exist", started)
        self.assertRegex(started, r"window 1\s+test\s+worker")
        with sqlite3.connect(view["agent_bus_db"]) as db:
            self.assertEqual(db.execute("SELECT status, retired_kind FROM identities WHERE agent_id=?",
                                        (worker,)).fetchone(), ("retired", "restart"))
        self.assertIn("survives the server", self.run_command([ORC, "--fleet", "alpha", "board"]).stdout)
        # The same slot registers again in the rebuilt session without any manual retirement.
        successor = self.join("alpha", "worker")
        self.assertNotEqual(successor, "")
        self.assertIn("running fleet 'alpha'", self.start("alpha").stdout)
        with sqlite3.connect(view["agent_bus_db"]) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM identities WHERE status='active'").fetchone()[0], 1)

    def test_hand_made_session_of_a_saved_fleet_is_that_fleet(self):
        self.start("alpha")
        self.open_task("alpha", "saved alpha work")
        self.run_command([ORC, "fleet", "stop", "alpha"])
        self.native_session("alpha")
        self.assertEqual(self.tmux("show-options", "-qv", "-t", "alpha", "@orc-runtime").stdout.strip(), "")
        rows = json.loads(self.run_command([ORC, "--json"]).stdout)
        self.assertEqual([(row["name"], row["status"]) for row in rows], [("alpha", "online")])
        self.assertIn("saved alpha work", self.run_command([ORC, "board"], env=self.session_environment("alpha")).stdout)
        self.assertIn("running fleet 'alpha'", self.start("alpha").stdout)
        self.assertEqual(self.tmux("show-options", "-qv", "-t", "alpha", "@orc-runtime").stdout.strip(), "alpha")

    def test_existing_process_uses_its_actual_session_without_export_or_restart(self):
        self.start("alpha")
        self.start("beta")
        self.native_session("gamma")
        self.tmux("new-session", "-d", "-t", "alpha", "-s", "tview-test")
        env = self.session_environment("alpha")
        result = self.run_command([BUS, "environment"], env=env)
        self.assertEqual(json.loads(result.stdout)["database"], self.view("alpha")["agent_bus_db"])
        explicit = self.run_command([BUS, "--fleet", "beta", "environment"], env=env)
        self.assertEqual(json.loads(explicit.stdout)["database"], self.view("beta")["agent_bus_db"])
        self.assertIn("No open tasks.", self.run_command([ORC, "board"], env=env).stdout)
        listing = self.run_command([ROOT / "scripts/tview", "--list"], env=env).stdout
        self.assertIn("alpha", listing)
        self.assertIn("beta", listing)
        self.assertNotIn("tview-test", listing)
        self.assertNotIn("gamma", listing)
        plain = self.run_command([ORC, "board"], env=self.session_environment("gamma"), check=False)
        self.assertNotEqual(plain.returncode, 0)
        self.assertIn("is not a fleet", plain.stderr)
        self.assertIn("orc fleet NAME start", plain.stderr)
        outside = self.run_command([ORC, "board"], check=False)
        self.assertNotEqual(outside.returncode, 0)
        self.assertIn("no fleet selected", outside.stderr)

    def test_sender_rejects_a_pane_outside_the_selected_session(self):
        self.start("alpha")
        self.native_session("beta")
        pane = self.tmux("display-message", "-p", "-t", "=beta:0.0", "#{pane_id}").stdout.strip()
        program = "import runpy,sys; runpy.run_path(sys.argv[1])['pane_info'](sys.argv[2])"
        command = [sys.executable, ROOT / "scripts/lib/fleet-profile.py", "exec", "alpha", "--",
                   sys.executable, "-c", program, ROOT / "scripts/agent-tmux-send.py", pane]
        result = self.run_command(command, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("outside the selected fleet session", result.stderr)
        self.tmux("has-session", "-t", "=beta")

    def test_numeric_fleet_name_keeps_its_terminal_from_another_current_session(self):
        self.start("0")
        identity = self.join("0", "worker")
        self.native_session("beta")
        self.tmux("new-session", "-d", "-t", "0", "-s", "tview-test")
        for env in (self.env, self.session_environment("beta")):
            with self.subTest(inside_other_session="TMUX" in env):
                members = [json.loads(line) for line in
                           self.bus("0", "members", env=env).stdout.splitlines()]
                member = next(row for row in members if row["agent_id"] == identity)
                self.assertEqual(member["terminal_presence"], "present")
                self.assertEqual(member["tmux"], "tmux=0:0.0")

    def test_stop_from_inside_its_own_window_finishes_the_whole_session(self):
        self.start("alpha")
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

    def test_stale_exports_lose_to_actual_session_but_explicit_nested_scope_survives(self):
        self.start("alpha")
        self.start("beta")
        env = {**self.session_environment("beta"), "NW_FLEET": "alpha",
               "NW_FLEET_PROFILE_APPLIED": "alpha", "NW_FLEET_PRIMARY_SESSION": "alpha"}
        actual = json.loads(self.run_command([BUS, "environment"], env=env).stdout)
        self.assertEqual(actual["database"], self.view("beta")["agent_bus_db"])
        profile = ROOT / "scripts/lib/fleet-profile.py"
        nested = self.run_command([sys.executable, profile, "exec", "alpha", "--",
                                   "bash", "-c", 'exec "$1" environment', "test", BUS], env=env)
        self.assertEqual(json.loads(nested.stdout)["database"], self.view("alpha")["agent_bus_db"])
        dead = {**env, "NW_FLEET_COMMAND_SCOPE": "99999999|0|alpha"}
        actual = json.loads(self.run_command([BUS, "environment"], env=dead).stdout)
        self.assertEqual(actual["database"], self.view("beta")["agent_bus_db"])

    def test_direct_and_installed_turn_reporter_follow_registration_fleet(self):
        self.start("alpha")
        self.start("beta")
        identity = self.join("beta", "reporter-worker")
        config = json.loads(self.config.read_text())
        # This path resolves differently after selecting the actual fleet.
        config["turn_report"] = {"seats_file": "${NOTES_RUNTIME_DIR}/enrolled.json"}
        self.config.write_text(json.dumps(config))
        view = self.view("beta")
        (Path(view["runtime_dir"]) / "enrolled.json").write_text(json.dumps([identity]))
        env = {**self.session_environment("beta"), "NW_FLEET": "alpha",
               "NW_FLEET_PROFILE_APPLIED": "alpha", "NOTES_RUNTIME_DIR": str(self.root / "wrong"),
               "NW_FLEET_COMMAND_SCOPE": "99999999|0|alpha"}
        launcher = self.root / "commands with spaces/orc-turn-report"
        self.run_command([sys.executable, "-B", ROOT / "scripts/install",
                          "--command", "orc-turn-report", "--target", launcher])
        for command in ([sys.executable, "-B", ROOT / "scripts/orc-turn-report.py"], [launcher]):
            for event in ("UserPromptSubmit", "Stop"):
                result = subprocess.run([str(x) for x in [*command, "--harness", "codex"]],
                                        env=env, input=json.dumps({"hook_event_name": event}),
                                        text=True, capture_output=True, timeout=10, check=False)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
        with sqlite3.connect(view["dispatch_ledger_db"]) as conn:
            self.assertEqual(conn.execute("SELECT seat, starts, ends FROM seat_presence").fetchall(),
                             [(identity, 2, 2)])
        self.assertFalse((self.root / "wrong").exists())
        self.assertFalse(Path(self.view("alpha")["dispatch_ledger_db"]).exists())
        # An explicit descendant selection is respected even from beta's pane.
        selected = self.run_command([
            sys.executable, "-B", ROOT / "scripts/lib/fleet-profile.py", "exec", "alpha", "--",
            sys.executable, "-B", ROOT / "scripts/orc-turn-report.py", "--kind", "start",
        ], env=env)
        self.assertEqual(selected.stdout, "")
        with sqlite3.connect(view["dispatch_ledger_db"]) as conn:
            self.assertEqual(conn.execute("SELECT starts FROM seat_presence").fetchone(), (2,))
        self.assertFalse(Path(self.view("alpha")["dispatch_ledger_db"]).exists())

    def test_rename_preserves_running_processes_and_history_after_reopen(self):
        self.start("alpha")
        self.native_session("beta")
        worker = self.join("alpha", "worker")
        self.open_task("alpha", "rename history")
        before = self.view("alpha")
        pane = self.tmux("list-panes", "-s", "-t", "=alpha", "-F", "#{pane_id}|#{pane_pid}").stdout
        self.run_command([ORC, "fleet", "rename", "alpha", "gamma"])
        self.assertEqual(self.tmux("list-panes", "-s", "-t", "=gamma", "-F", "#{pane_id}|#{pane_pid}").stdout, pane)
        self.assertEqual(self.view("gamma")["agent_bus_db"], before["agent_bus_db"])
        self.assertEqual(self.view("alpha")["primary_session"], "gamma")
        self.assertIn(worker, self.bus("gamma", "members").stdout)
        self.start("alpha")
        self.assertNotEqual(self.tmux("has-session", "-t", "=alpha", check=False).returncode, 0)
        self.run_command([ORC, "fleet", "stop", "gamma"])
        self.start("gamma")
        self.assertIn("rename history", self.run_command([ORC, "--fleet", "gamma", "board"]).stdout)
        self.assertFalse((self.root / "profiles").exists())

    def test_native_rename_keeps_runtime_and_surviving_group_remains_discoverable(self):
        self.start("alpha")
        self.native_session("beta")
        self.tmux("new-session", "-d", "-t", "alpha", "-s", "tview-existing")
        self.join("alpha", "worker")
        before = self.view("alpha")
        self.tmux("rename-session", "-t", "=alpha", "gamma")
        self.assertEqual(self.view("gamma")["agent_bus_db"], before["agent_bus_db"])
        self.tmux("kill-session", "-t", "=gamma")
        self.assertEqual(self.view("alpha")["primary_session"], "tview-existing")
        self.assertEqual(self.view("alpha")["agent_bus_db"], before["agent_bus_db"])
        self.run_command([ORC, "fleet", "stop", "alpha"])
        self.assertNotEqual(self.tmux("has-session", "-t", "=tview-existing", check=False).returncode, 0)
        self.tmux("has-session", "-t", "=beta")

    def test_group_survives_native_primary_close(self):
        self.start("alpha")
        self.tmux("new-session", "-d", "-t", "alpha", "-s", "tview-alpha")
        self.tmux("kill-session", "-t", "=alpha")
        self.assertEqual(self.view("alpha")["primary_session"], "tview-alpha")
        listing = json.loads(self.run_command([ROOT / "scripts/tview", "--list", "--json"]).stdout)
        alpha = next(row for row in listing if row["name"] == "alpha")
        self.assertEqual(alpha["status"], "online")

    def test_rename_refuses_different_history_and_runtime_alias_escape(self):
        self.start("alpha")
        (self.root / "fleets/taken").mkdir(parents=True)
        result = self.run_command([ORC, "fleet", "rename", "alpha", "taken"], check=False)
        self.assertNotEqual(result.returncode, 0)
        self.tmux("has-session", "-t", "=alpha")
        (self.root / "fleets/escape").symlink_to(self.root)
        result = self.run_command([ORC, "fleet", "show", "escape"], check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("inside the runtime directory", result.stderr)

    def test_start_binds_history_and_readers_never_bind_a_plain_session(self):
        self.native_session("alpha")
        plain = self.run_command([ORC, "--fleet", "alpha", "board"], check=False)
        self.assertNotEqual(plain.returncode, 0)
        self.run_command([ORC, "admin", "tick", "--dry-run"])
        self.assertEqual(self.tmux("show-options", "-qv", "-t", "alpha", "@orc-runtime").stdout.strip(), "")
        self.assertFalse((self.root / "fleets/alpha").exists())
        self.start("alpha")
        self.assertEqual(self.tmux("show-options", "-qv", "-t", "alpha", "@orc-runtime").stdout.strip(), "alpha")
        self.open_task("alpha", "started history")
        self.tmux("rename-session", "-t", "=alpha", "renamed")
        self.assertIn("started history", self.run_command([ORC, "--fleet", "renamed", "board"]).stdout)
        # A reader must not repair even deliberately removed optional metadata.
        self.tmux("rename-session", "-t", "=renamed", "alpha")
        self.tmux("set-option", "-u", "-t", "alpha", "@orc-runtime")
        self.run_command([ORC, "fleet", "tick", "--dry-run"])
        self.run_command([ORC, "--fleet", "alpha", "board"])
        self.assertEqual(self.tmux("show-options", "-qv", "-t", "alpha", "@orc-runtime").stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
