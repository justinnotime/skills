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

    def configure_handoffs(self):
        directory = self.root / "configured-handoffs"
        directory.mkdir()
        (directory / "2020-01-01-shared-worker.md").write_text("Default fleet history.\n")
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

    def test_native_fleets_isolate_handoff_reads_writes_and_same_named_agents(self):
        configured, published = self.configure_handoffs()
        notes = {}
        for name in ("alpha", "beta"):
            self.native_session(name)
            identity = self.join(name, "shared-worker")
            self.run_command([ORC, "--fleet", name, "open", "--to", "operator",
                              "--subject", "Synthetic handoff test", "--body",
                              "Initialize this isolated test ledger.", "--no-check"])
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

    def test_default_fleet_retains_configured_handoff_publication(self):
        configured, published = self.configure_handoffs()
        self.native_session("0")
        env = self.session_environment("0")
        result = self.bus("default", "join", "default-worker", "default-worker", "test", "pull",
                          socket.gethostname().split('.')[0], "tmux=0:0.0 win=test", env=env)
        identity = json.loads(result.stdout)["agent_id"]
        self.run_command([ORC, "--fleet", "default", "checkout", identity,
                          "--summary", "Default archive publication."])
        names = published.read_text().splitlines()
        self.assertEqual(len(names), 1)
        self.assertIn("Default archive publication.", (configured / names[0]).read_text())
        self.assertIn("2020-01-01-shared-worker.md",
                      self.run_command([ORC, "--fleet", "default", "topology"]).stdout)

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

    def test_numeric_session_keeps_its_terminal_from_another_current_session(self):
        self.native_session("0")
        joined = self.bus("default", "join", "worker", "worker", "test", "pull",
                          socket.gethostname().split('.')[0], "tmux=0:0.0 win=test",
                          env=self.session_environment("0"))
        identity = json.loads(joined.stdout)["agent_id"]
        self.native_session("beta")
        self.tmux("new-session", "-d", "-t", "0", "-s", "tview-test")
        for env in (self.env, self.session_environment("beta")):
            with self.subTest(inside_other_session="TMUX" in env):
                members = [json.loads(line) for line in
                           self.bus("default", "members", env=env).stdout.splitlines()]
                member = next(row for row in members if row["agent_id"] == identity)
                self.assertEqual(member["terminal_presence"], "present")
                self.assertEqual(member["tmux"], "tmux=0:0.0")

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

    def session_environment(self, name):
        fields = self.tmux("display-message", "-p", "-t", f"={name}:0.0",
                           "#{socket_path}|#{pid}|#{session_id}|#{pane_id}").stdout.strip().split("|")
        return {**self.env, "TMUX": f"{fields[0]},{fields[1]},{fields[2][1:]}", "TMUX_PANE": fields[3]}

    def test_stale_exports_lose_to_actual_session_but_explicit_nested_scope_survives(self):
        self.native_session("alpha")
        self.native_session("beta")
        env = {**self.session_environment("beta"), "NW_FLEET": "alpha",
               "NW_FLEET_PROFILE_APPLIED": "alpha", "NW_FLEET_PRIMARY_SESSION": "alpha"}
        actual = json.loads(self.run_command([BUS, "environment"], env=env).stdout)
        self.assertEqual(actual["database"], self.view("beta")["agent_bus_db"])
        profile = ROOT / "scripts/lib/fleet-profile.py"
        nested = self.run_command([sys.executable, profile, "exec", "default", "--",
                                   "bash", "-c", 'exec "$1" environment', "test", BUS], env=env)
        self.assertEqual(json.loads(nested.stdout)["database"], str(self.root / "default/bus/inbox.sqlite3"))
        dead = {**env, "NW_FLEET_COMMAND_SCOPE": "99999999|0|alpha"}
        actual = json.loads(self.run_command([BUS, "environment"], env=dead).stdout)
        self.assertEqual(actual["database"], self.view("beta")["agent_bus_db"])

    def test_rename_preserves_running_processes_and_history_after_reopen(self):
        self.run_command([ORC, "fleet", "start", "alpha"])
        self.native_session("beta")
        worker = self.join("alpha", "worker")
        self.run_command([ORC, "--fleet", "alpha", "open", "--to", "operator",
                          "--subject", "rename history", "--body", "Synthetic test.", "--no-check"])
        before = self.view("alpha")
        pane = self.tmux("list-panes", "-s", "-t", "=alpha", "-F", "#{pane_id}|#{pane_pid}").stdout
        self.run_command([ORC, "fleet", "rename", "alpha", "gamma"])
        self.assertEqual(self.tmux("list-panes", "-s", "-t", "=gamma", "-F", "#{pane_id}|#{pane_pid}").stdout, pane)
        self.assertEqual(self.view("gamma")["agent_bus_db"], before["agent_bus_db"])
        self.assertEqual(self.view("alpha")["primary_session"], "gamma")
        self.assertIn(worker, self.bus("gamma", "members").stdout)
        self.run_command([ORC, "fleet", "start", "alpha"])
        self.assertNotEqual(self.tmux("has-session", "-t", "=alpha", check=False).returncode, 0)
        self.run_command([ORC, "fleet", "stop", "gamma"])
        self.run_command([ORC, "fleet", "start", "gamma"])
        self.assertIn("rename history", self.run_command([ORC, "--fleet", "gamma", "board"]).stdout)
        self.assertFalse((self.root / "profiles").exists())

    def test_native_rename_keeps_runtime_and_surviving_group_remains_discoverable(self):
        self.native_session("alpha")
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

    def test_default_group_survives_native_primary_close(self):
        self.native_session("0")
        self.tmux("new-session", "-d", "-t", "0", "-s", "tview-default")
        self.tmux("kill-session", "-t", "=0")
        self.assertEqual(self.view("default")["primary_session"], "tview-default")
        listing = json.loads(self.run_command([ROOT / "scripts/tview", "--list", "--json"]).stdout)
        default = next(row for row in listing if row["name"] == "default")
        self.assertEqual(default["status"], "online")

    def test_rename_refuses_different_history_and_runtime_alias_escape(self):
        self.run_command([ORC, "fleet", "start", "alpha"])
        (self.root / "fleets/taken").mkdir(parents=True)
        result = self.run_command([ORC, "fleet", "rename", "alpha", "taken"], check=False)
        self.assertNotEqual(result.returncode, 0)
        self.tmux("has-session", "-t", "=alpha")
        (self.root / "fleets/escape").symlink_to(self.root)
        result = self.run_command([ORC, "fleet", "show", "escape"], check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("inside the runtime directory", result.stderr)

    def test_native_first_task_binds_history_but_readers_and_dry_run_do_not(self):
        self.native_session("alpha")
        self.run_command([ORC, "--fleet", "alpha", "board"])
        self.bus("alpha", "members")
        self.assertEqual(self.tmux("show-options", "-qv", "-t", "alpha", "@orc-runtime").stdout.strip(), "")
        self.run_command([ORC, "--fleet", "alpha", "open", "--to", "operator",
                          "--subject", "native history", "--body", "Synthetic test.", "--no-check"])
        self.assertEqual(self.tmux("show-options", "-qv", "-t", "alpha", "@orc-runtime").stdout.strip(), "alpha")
        self.tmux("rename-session", "-t", "=alpha", "renamed")
        self.assertIn("native history", self.run_command([ORC, "--fleet", "renamed", "board"]).stdout)
        # A reader must not repair even deliberately removed optional metadata.
        self.tmux("rename-session", "-t", "=renamed", "alpha")
        self.tmux("set-option", "-u", "-t", "alpha", "@orc-runtime")
        self.run_command([ORC, "fleet", "tick", "--dry-run"])
        self.assertEqual(self.tmux("show-options", "-qv", "-t", "alpha", "@orc-runtime").stdout.strip(), "")

    def test_default_stop_retires_registrations_from_configured_bus_database(self):
        self.native_session("0")
        self.native_session("alpha")
        pane = self.tmux("display-message", "-p", "-t", "=0:0.0", "#{pane_id}").stdout.strip()
        self.bus("default", "join", "worker", "worker", "test", "pull",
                 socket.gethostname().split('.')[0], "tmux=0:0.0 win=test",
                 env={**self.env, "TMUX_PANE": pane})
        self.run_command([ORC, "fleet", "stop", "default"])
        with sqlite3.connect(self.root / "default/bus/inbox.sqlite3") as db:
            self.assertEqual(db.execute("SELECT status FROM identities").fetchone()[0], "retired")
        self.tmux("has-session", "-t", "=alpha")


if __name__ == "__main__":
    unittest.main()
