"""Tests for the machine-local tmux server selector and shared consumers."""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))


def load(script: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / script)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# Imports capture some runtime defaults; never read the caller's fleet settings.
with tempfile.TemporaryDirectory() as import_root:
    import_config = Path(import_root) / "fleet-config.json"
    import_config.write_text('{"schema":"fleet-runtime/v1"}', encoding="utf-8")
    with mock.patch.dict(os.environ, {
        "HOME": import_root,
        "FLEET_ORCHESTRATOR_CONFIG": str(import_config),
        "NOTES_RUNTIME_DIR": str(Path(import_root) / "runtime"),
    }):
        import tmux_runtime
        import pane_sense
        import workplane

        TMUX_SEND = load("agent-tmux-send.py", "agent_tmux_send_for_runtime_tests")
        ORCHESTRATOR = load(
            "fleet-orchestrator.py", "fleet_orchestrator_for_runtime_tests"
        )


class TmuxRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "runtime"
        configuration = Path(self.tmp.name) / "fleet-config.json"
        configuration.write_text('{"schema":"fleet-runtime/v1"}', encoding="utf-8")
        self.env = mock.patch.dict(os.environ, {
            "HOME": self.tmp.name,
            "FLEET_ORCHESTRATOR_CONFIG": str(configuration),
            "NOTES_RUNTIME_DIR": str(self.root),
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("NW_TMUX_SERVER", None)
        os.environ.pop("NW_FLEET_PRIMARY_SESSION", None)
        os.environ.pop("DISPATCH_LEDGER_ACTOR", None)
        self.addCleanup(self.tmp.cleanup)

    def write_config(self, value: str):
        path = tmux_runtime.config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        return path

    def test_selector_path_is_confined_to_this_test(self):
        self.assertTrue(tmux_runtime.config_path().is_relative_to(self.root))

    def test_default_without_selector(self):
        self.assertEqual(tmux_runtime.configured_server(), (None, "default"))
        self.assertEqual(tmux_runtime.base_cmd(), ["tmux"])

    def test_machine_selector_is_shared_by_sense_and_send(self):
        self.write_config("tmux37\n")
        self.assertEqual(tmux_runtime.base_cmd(), ["tmux", "-L", "tmux37"])
        self.assertEqual(TMUX_SEND.tmux_base_cmd(), ["tmux", "-L", "tmux37"])
        with mock.patch("subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "ok", "")
            self.assertEqual(pane_sense.tmux_out(["display-message"]), "ok")
        self.assertEqual(run.call_args.args[0][:3], ["tmux", "-L", "tmux37"])

    def test_environment_override_wins_for_staging(self):
        self.write_config("production")
        with mock.patch.dict(os.environ, {"NW_TMUX_SERVER": "staging"}):
            self.assertEqual(tmux_runtime.configured_server(), ("staging", "env"))
            self.assertEqual(tmux_runtime.base_cmd(), ["tmux", "-L", "staging"])

    def test_pane_snapshot_uses_selected_session_despite_grouped_viewers(self):
        def observe(command, **kwargs):
            scope = command[command.index("list-panes") + 1:command.index("-F")]
            self.assertEqual(scope, ["-s", "-t", "=alpha"])
            # tmux can report a grouped viewer as session_name even when the
            # target is the primary session. Honor the requested format here.
            values = {
                "socket_path": "/tmp/test-tmux/socket", "pid": "123",
                "start_time": "456", "pane_id": "%7", "session_name": "tview-test",
                "window_index": "2", "pane_index": "0", "pane_dead": "0",
            }
            output = command[-1]
            for name, value in values.items():
                output = output.replace("#{" + name + "}", value)
            return subprocess.CompletedProcess(command, 0, output + "\n", "")

        with mock.patch.dict(os.environ, {"NW_FLEET_PRIMARY_SESSION": "alpha"}):
            with mock.patch.object(tmux_runtime.subprocess, "run", side_effect=observe):
                pane = tmux_runtime.pane_snapshot()["%7"]
        self.assertEqual(pane["location"], "alpha:2.0")
        self.assertFalse(pane["dead"])
        self.assertTrue(pane["server_id"].startswith("tmux:"))

    def test_unscoped_pane_snapshot_preserves_reported_session(self):
        output = "/tmp/test-tmux/socket\t123\t456\t%7\tbeta:2.0\t0\n"
        with mock.patch.object(tmux_runtime.subprocess, "run", return_value=
                               subprocess.CompletedProcess([], 0, output, "")) as run:
            pane = tmux_runtime.pane_snapshot()["%7"]
        command = run.call_args.args[0]
        self.assertEqual(command[command.index("list-panes") + 1:command.index("-F")], ["-a"])
        self.assertIn("#{session_name}", command[-1])
        self.assertEqual(pane["location"], "beta:2.0")

    def test_invalid_selector_fails_closed(self):
        path = self.write_config("tmux37 -- bad")
        with self.assertRaisesRegex(tmux_runtime.TmuxRuntimeConfigError,
                                    "invalid tmux server"):
            tmux_runtime.configured_server()
        self.assertIn(str(path), tmux_runtime.identity())

    def test_unreachable_server_is_unknown_not_empty_fleet(self):
        self.write_config("missing")
        failed = subprocess.CompletedProcess([], 1, "", "no server")
        with mock.patch("subprocess.run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "no server"):
                pane_sense.agent_panes()
            with self.assertRaisesRegex(RuntimeError, "no server"):
                pane_sense.window_titles()

    def test_identity_and_succession_checks_use_selected_server(self):
        with mock.patch.dict(os.environ, {
            "NW_TMUX_SERVER": "fleet-alpha", "TMUX_PANE": "%1"
        }, clear=False):
            identity_result = subprocess.CompletedProcess(
                [], 0, "codex@0:14.0\n", ""
            )
            with mock.patch.object(
                workplane.subprocess, "run", return_value=identity_result
            ) as run:
                self.assertEqual(workplane.whoami(), "codex@0:14.0")
            self.assertEqual(run.call_args.args[0][:3],
                             ["tmux", "-L", "fleet-alpha"])

            pane_result = subprocess.CompletedProcess([], 0, "codex\n", "")
            with mock.patch.object(
                ORCHESTRATOR.subprocess, "run", return_value=pane_result
            ) as run:
                self.assertEqual(ORCHESTRATOR._pane_current_command("%1"), "codex")
            self.assertEqual(run.call_args.args[0][:3],
                             ["tmux", "-L", "fleet-alpha"])


if __name__ == "__main__":
    unittest.main()
