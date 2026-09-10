from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install-agent-bus-pull-notify.py"
SPEC = importlib.util.spec_from_file_location("install_agent_bus_pull_notify", SCRIPT)
installer = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(installer)


class InstallAgentBusPullNotifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.codex = root / "codex" / "config.toml"
        self.unit_dir = root / "systemd" / "user"
        self.default_unit = self.unit_dir / "agent-bus-dispatcher.service"
        self.fleet_unit = self.unit_dir / "agent-bus-dispatcher@.service"
        self.calls: list[list[str]] = []
        self.transports: dict[str, str] = {}
        self.template = root / "dispatcher.service.in"
        self.template.write_text(
            "[Service]\n@CONFIG_ENV@\nExecStart=/bin/bash @BUS_CLI@ --fleet %i dispatch\n"
        )

        def fake_run(args, **kwargs):
            self.calls.append(list(args))
            stdout = None
            if "fleet-profile.py" in " ".join(str(arg) for arg in args):
                name = args[args.index("resolve") + 1]
                stdout = self.transports.get(name, "matrix") + "\n"
            return subprocess.CompletedProcess(args, 0, stdout=stdout)

        patches = (
            mock.patch.dict(os.environ, {"AGENT_BUS_TRANSPORT": "matrix", "HOME": str(root)}, clear=True),
            mock.patch.object(installer.cfg, "path", return_value=self.template),
            mock.patch.object(installer, "CODEX", self.codex),
            mock.patch.object(installer, "UNIT_DIR", self.unit_dir),
            mock.patch.object(installer, "UNIT", self.default_unit),
            mock.patch.object(installer, "FLEET_UNIT", self.fleet_unit),
            mock.patch.object(installer.subprocess, "run", side_effect=fake_run),
        )
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_default_install_keeps_existing_service_and_commands(self) -> None:
        self.codex.parent.mkdir(parents=True)
        self.codex.write_text('notify = ["old"]\nmodel = "test"\n')

        installer.main([])

        expected = installer.render_unit(self.template)
        self.assertEqual(self.default_unit.read_text(), expected)
        self.assertFalse(self.fleet_unit.exists())

        self.assertEqual(
            self.calls,
            [
                ["systemctl", "--user", "daemon-reload"],
                [
                    "systemctl", "--user", "enable", "--now",
                    "agent-bus-dispatcher.service",
                ],
            ],
        )
        config = self.codex.read_text()
        self.assertIn('notify = ["old"]', config)
        self.assertIn('model = "test"', config)
        self.assertEqual(config.count(installer.HOOK_MARKER), 1)

    def test_named_install_selects_its_own_configured_template(self) -> None:
        installer.main(["--fleet", "alpha"])
        installer.cfg.path.assert_called_with("bus.named_dispatcher_template")

    def test_named_install_uses_template_and_one_stop_hook(self) -> None:
        installer.main(["--fleet", "alpha-1"])
        installer.main(["--fleet", "beta"])

        text = self.fleet_unit.read_text()
        self.assertIn(
            f'ExecStart=/bin/bash "{ROOT}/scripts/'
            'matrix-bus.sh" --fleet %i dispatch',
            text,
        )
        self.assertNotIn("agent-bus-v3.py", text)
        self.assertFalse(self.default_unit.exists())
        self.assertEqual(self.codex.read_text().count(installer.HOOK_MARKER), 1)
        self.assertEqual(
            [call[-1] for call in self.calls if "enable" in call],
            [
                "agent-bus-dispatcher@alpha-1.service",
                "agent-bus-dispatcher@beta.service",
            ],
        )
        resolve_calls = [call for call in self.calls if "fleet-profile.py" in " ".join(call)]
        self.assertEqual(
            [call[call.index("resolve") + 1] for call in resolve_calls],
            ["alpha-1", "beta"],
        )
        self.assertTrue(all(call[-2:] == ["--field", "agent_bus_transport"]
                            for call in resolve_calls))

    def test_local_named_fleet_installs_only_the_stop_hook(self) -> None:
        self.transports["alpha"] = "local"

        installer.main(["--fleet", "alpha"])

        self.assertTrue(self.codex.exists())
        self.assertEqual(self.codex.read_text().count(installer.HOOK_MARKER), 1)
        self.assertFalse(self.default_unit.exists())
        self.assertFalse(self.fleet_unit.exists())
        self.assertFalse(self.unit_dir.exists())
        self.assertEqual(len(self.calls), 1)
        self.assertIn("fleet-profile.py", " ".join(self.calls[0]))

    def test_existing_managed_hook_is_replaced_without_losing_user_settings(self) -> None:
        self.codex.parent.mkdir(parents=True)
        self.codex.write_text(
            '[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\n'
            'command = "python3 /old/runtime/hook.py"\n'
            + installer.HOOK_MARKER + '\nnotify = ["custom"]\nmodel = "test"\n'
        )
        installer.main([])
        output = self.codex.read_text()
        self.assertNotIn("/old/runtime/hook.py", output)
        self.assertIn('notify = ["custom"]', output)
        self.assertIn('model = "test"', output)
        self.assertEqual(output.count(installer.HOOK_MARKER), 1)
        self.assertIn(str(ROOT / "scripts/agent-bus-codex-stop-hook.py"), output)

    def test_identical_unmarked_hook_survives_marker_drift_and_retains_trust(self) -> None:
        original = ('model = "test"\n' + "\n".join(installer.hook_lines()[:-1]) + '\n'
                    '[[hooks.UserPromptSubmit]]\n[[hooks.UserPromptSubmit.hooks]]\n'
                    'type = "command"\ncommand = "custom-start"\n'
                    + installer.HOOK_MARKER + '\n'
                    '[hooks.state."synthetic:stop:0:0"]\n'
                    'enabled = false\ntrusted_hash = "synthetic-hash"\n')
        self.codex.parent.mkdir(parents=True)
        self.codex.write_text(original)
        installer.main([])
        installer.main([])
        self.assertEqual(self.codex.read_text(), original)
        parsed = tomllib.loads(original)
        self.assertEqual(len(parsed["hooks"]["Stop"]), 1)
        self.assertFalse(parsed["hooks"]["state"]["synthetic:stop:0:0"]["enabled"])

    def test_marker_in_a_later_table_does_not_delete_an_unrelated_stop_hook(self) -> None:
        original = ('[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\n'
                    'command = "custom-stop"\n'
                    '[tui]\n' + installer.HOOK_MARKER + '\nanimations = false\n')
        result = tomllib.loads(installer.replace_managed_hook(original))
        self.assertEqual(result["hooks"]["Stop"][0]["hooks"][0]["command"], "custom-stop")
        self.assertFalse(result["tui"]["animations"])
        self.assertEqual(len(result["hooks"]["Stop"]), 2)

    def test_matching_text_outside_stop_is_not_an_installed_stop_hook(self) -> None:
        command = tomllib.loads("\n".join(installer.hook_lines()))["hooks"]["Stop"][0]["hooks"][0]["command"]
        original = ('[[hooks.UserPromptSubmit]]\n[[hooks.UserPromptSubmit.hooks]]\n'
                    'type = "command"\ncommand = ' + json.dumps(command) + '\n')
        result = tomllib.loads(installer.replace_managed_hook(original))
        self.assertEqual(len(result["hooks"]["Stop"]), 1)
        self.assertEqual(len(result["hooks"]["UserPromptSubmit"]), 1)

    def test_missing_named_profile_fails_before_writing(self) -> None:
        installer.subprocess.run.side_effect = subprocess.CalledProcessError(
            2, ["fleet-profile.py", "resolve", "alpha"]
        )
        with self.assertRaises(subprocess.CalledProcessError):
            installer.main(["--fleet", "alpha"])
        self.assertFalse(self.codex.exists())
        self.assertFalse(self.unit_dir.exists())

    def test_invalid_fleet_names_fail_before_writing_or_systemctl(self) -> None:
        invalid = (
            "",
            "default",
            "Alpha",
            "-alpha",
            "alpha_beta",
            "alpha/beta",
            "a" * 33,
        )
        for name in invalid:
            with self.subTest(name=name), redirect_stderr(StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    installer.main(["--fleet", name])
                self.assertEqual(raised.exception.code, 2)
        self.assertFalse(self.codex.exists())
        self.assertFalse(self.unit_dir.exists())
        self.assertEqual(self.calls, [])


def test_installer_respects_codex_home_and_explicit_file_override(tmp_path):
    for override in (False, True):
        home = tmp_path / str(override)
        codex = home / "custom codex"
        target = home / "selected config.toml" if override else codex / "config.toml"
        env = {"HOME": str(home), "CODEX_HOME": str(codex), "PYTHONDONTWRITEBYTECODE": "1"}
        if override:
            env["TURN_HOOKS_CODEX_CONFIG"] = str(target)
        result = subprocess.run([sys.executable, "-B", str(SCRIPT)], env=env,
                                capture_output=True, text=True, timeout=10, check=False)
        assert result.returncode == 0, result.stderr
        assert len(tomllib.loads(target.read_text())["hooks"]["Stop"]) == 1
        assert not (home / ".codex").exists()
        assert not (home / ".config/systemd").exists()
        assert "trusted_hash" not in target.read_text()
        if override:
            assert not codex.exists()


if __name__ == "__main__":
    unittest.main()
