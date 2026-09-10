"""Mechanical hook staging only; no harness, trust, or service invocation."""
import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/stage-codex-turn-hooks.sh"


class StageCodexTurnHooks(unittest.TestCase):
    def test_stages_and_preserves_existing_entries_without_accepting_trust(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "directory with spaces/config.toml"
            config.parent.mkdir()
            config.write_text('model = "example"\n')
            selected = root / "private profile.json"
            environment = {"PATH": os.environ["PATH"], "HOME": temporary,
                           "TURN_HOOKS_CODEX_CONFIG": str(config)}
            run = subprocess.run(["bash", str(SCRIPT), "--config", str(selected)],
                                 env=environment, capture_output=True, text=True, check=False)
            self.assertEqual(run.returncode, 0, run.stderr)
            text = config.read_text()
            self.assertIn("hooks.UserPromptSubmit", text)
            self.assertIn("hooks.Stop", text)
            self.assertIn("FLEET_ORCHESTRATOR_CONFIG=", text)
            self.assertNotIn("trusted_hash", text)
            hooks = tomllib.loads(text)["hooks"]
            self.assertEqual(set(hooks), {"UserPromptSubmit", "Stop"})
            for event in hooks:
                self.assertEqual(hooks[event][0]["hooks"][0]["type"], "command")
                self.assertIn(" -B ", hooks[event][0]["hooks"][0]["command"])
            self.assertEqual(config.with_name("config.toml.bak.turn-hooks").read_text(), 'model = "example"\n')
            repeated = subprocess.run(["bash", str(SCRIPT)], env=environment,
                                      capture_output=True, text=True, check=False)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(config.read_text(), text)

    def test_preserves_uninspected_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            config.write_text('model = "example"\n')
            backup = config.with_name("config.toml.bak.turn-hooks")
            backup.write_text("original")
            result = subprocess.run(["bash", str(SCRIPT)], capture_output=True,
                                    env={"PATH": os.environ["PATH"], "HOME": temporary,
                                         "TURN_HOOKS_CODEX_CONFIG": str(config)}, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(backup.read_text(), "original")
            self.assertEqual(config.read_text(), 'model = "example"\n')

    def test_partial_staging_adds_only_missing_event_and_preserves_trust(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            codex = root / "selected root"
            codex.mkdir()
            config = codex / "config.toml"
            original = ('[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\n'
                        'command = "python3 /old/package/orc-turn-report.py --harness codex"\n'
                        '[hooks.state."synthetic:stop:0:0"]\n'
                        'enabled = false\ntrusted_hash = "synthetic-hash"\n')
            config.write_text(original)
            result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True,
                                    env={"PATH": os.environ["PATH"], "HOME": temporary,
                                         "CODEX_HOME": str(codex)}, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(config.read_text().startswith(original))
            hooks = tomllib.loads(config.read_text())["hooks"]
            self.assertEqual(len(hooks["Stop"]), 1)
            self.assertEqual(len(hooks["UserPromptSubmit"]), 1)
            self.assertFalse(hooks["state"]["synthetic:stop:0:0"]["enabled"])
            self.assertFalse((root / ".codex").exists())

    def test_filename_in_comment_is_not_an_installed_hook(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            config.write_text('# previously used orc-turn-report.py\n')
            result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True,
                                    env={"PATH": os.environ["PATH"], "HOME": temporary,
                                         "TURN_HOOKS_CODEX_CONFIG": str(config)}, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(set(tomllib.loads(config.read_text())["hooks"]), {"UserPromptSubmit", "Stop"})

    def test_duplicate_entries_require_inspection_before_any_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.toml"
            original = ('[[hooks.Stop]]\n[[hooks.Stop.hooks]]\ntype = "command"\n'
                        'command = "orc-turn-report --harness codex"\n') * 2
            config.write_text(original)
            result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True,
                                    env={"PATH": os.environ["PATH"], "HOME": temporary,
                                         "TURN_HOOKS_CODEX_CONFIG": str(config)}, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(config.read_text(), original)
            self.assertFalse(config.with_name("config.toml.bak.turn-hooks").exists())
