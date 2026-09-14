import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/install-opencode-config"
loader = importlib.machinery.SourceFileLoader("opencode_config", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)


class OpenCodeConfigTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "config/opencode"
        self.data = self.root / "share/opencode"
        self.sentinel = "example-private-value-for-fixture-only"
        self.document = {"model": "custom/model", "provider": {
            "custom": {"options": {"apiKey": self.sentinel}, "models": {"model": {}}}
        }}

    def call(self, *args, document=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--input", "-", *args],
            input=json.dumps(self.document if document is None else document),
            capture_output=True, text=True, timeout=10,
        )

    def test_explicit_profile_preserves_other_auth_and_is_idempotent(self):
        self.data.mkdir(parents=True)
        original = {"other": {"type": "oauth", "refresh": "example-refresh"}}
        (self.data / "auth.json").write_text(json.dumps(original))
        for _ in range(2):
            result = self.call("--profile-root", str(self.root))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(self.sentinel, result.stdout + result.stderr)
            self.assertNotIn(self.sentinel, (self.config / "opencode.json").read_text())
            auth = json.loads((self.data / "auth.json").read_text())
            self.assertEqual(auth["other"], original["other"])
            self.assertEqual(auth["custom"]["key"], self.sentinel)
            self.assertEqual((self.data / "auth.json").stat().st_mode & 0o777, 0o600)

    def test_default_xdg_and_two_profiles_do_not_share_credentials(self):
        native = self.root / "native"
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(native / "config"), "XDG_DATA_HOME": str(native / "data")}):
            self.assertEqual(self.call().returncode, 0)
        for name in ("alpha", "beta"):
            document = json.loads(json.dumps(self.document))
            document["provider"]["custom"]["options"]["apiKey"] += "-" + name
            self.assertEqual(self.call("--profile-root", str(self.root / name), document=document).returncode, 0)
        keys = [json.loads((native / "data/opencode/auth.json").read_text())["custom"]["key"]]
        keys += [json.loads((self.root / name / "share/opencode/auth.json").read_text())["custom"]["key"] for name in ("alpha", "beta")]
        self.assertEqual(len(set(keys)), 3)

    def test_native_defaults_resolve_without_changing_home(self):
        output = io.StringIO()
        with patch.object(Path, "home", return_value=self.root), patch.dict(os.environ, {}, clear=True):
            with patch.object(sys, "argv", [str(SCRIPT), "--input", "-"]), patch.object(sys, "stdin", io.StringIO(json.dumps(self.document))), contextlib.redirect_stdout(output):
                self.assertEqual(module.main(), 0)
        self.assertTrue((self.root / ".config/opencode/opencode.json").exists())
        self.assertTrue((self.root / ".local/share/opencode/auth.json").exists())

    def test_check_has_no_filesystem_effects(self):
        self.assertEqual(self.call("--profile-root", str(self.root), "--check").returncode, 0)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_references_remain_references_and_do_not_create_auth(self):
        for key in ("{env:EXAMPLE_API_KEY}", "{file:/private/credential}"):
            self.document["provider"]["custom"]["options"]["apiKey"] = key
            result = self.call("--profile-root", str(self.root))
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads((self.config / "opencode.json").read_text()), self.document)
            self.assertFalse((self.data / "auth.json").exists())

    def test_invalid_and_repeated_credentials_never_install(self):
        for key in ("", None, {"unexpected": self.sentinel}, "{env:BROKEN"):
            self.document["provider"]["custom"]["options"]["apiKey"] = key
            result = self.call("--profile-root", str(self.root))
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn(self.sentinel, result.stdout + result.stderr)
            self.assertEqual(list(self.root.iterdir()), [])
        self.document["provider"]["custom"]["options"]["apiKey"] = self.sentinel
        self.document["extra-header"] = "Bearer " + self.sentinel
        self.assertNotEqual(self.call("--profile-root", str(self.root)).returncode, 0)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_ambiguous_duplicate_json_is_rejected(self):
        with self.assertRaises(module.Invalid):
            module.install('{"provider":{}, "provider":{}}', self.config, self.data)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_shared_config_and_data_are_rejected(self):
        self.assertNotEqual(self.call("--config-dir", str(self.root), "--data-dir", str(self.root / "child")).returncode, 0)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_symlink_and_broken_auth_are_preserved(self):
        self.data.mkdir(parents=True)
        other = self.root / "other.json"
        other.write_text("{}")
        auth = self.data / "auth.json"
        auth.symlink_to(other)
        self.assertNotEqual(self.call("--profile-root", str(self.root)).returncode, 0)
        self.assertTrue(auth.is_symlink())
        self.assertEqual(other.read_text(), "{}")
        auth.unlink()
        auth.write_text("invalid " + self.sentinel)
        result = self.call("--profile-root", str(self.root))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn(self.sentinel, result.stdout + result.stderr)
        self.assertFalse(self.config.exists())

    def test_failed_config_replace_restores_previous_auth(self):
        self.config.mkdir(parents=True)
        self.data.mkdir(parents=True)
        previous = b'{"old":{"type":"api","key":"example-old"}}\n'
        (self.data / "auth.json").write_bytes(previous)
        (self.config / "opencode.json").write_text('{"previous":true}')
        replace = module.os.replace

        def failing_replace(source, target):
            if target == self.config / "opencode.json":
                raise OSError("synthetic config replacement failure")
            return replace(source, target)

        with patch.object(module.os, "replace", side_effect=failing_replace):
            with self.assertRaises(OSError):
                module.install(json.dumps(self.document), self.config, self.data)
        self.assertEqual((self.data / "auth.json").read_bytes(), previous)
        self.assertEqual((self.config / "opencode.json").read_text(), '{"previous":true}')
        self.assertEqual(list(self.config.glob(".opencode-config-*")), [])
        self.assertEqual(list(self.data.glob(".opencode-config-*")), [])


if __name__ == "__main__":
    unittest.main()
