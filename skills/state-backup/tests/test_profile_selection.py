"""Real copies under synthetic homes; no native account or source is read."""

import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/backup"


class ProfileSelectionTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.env = {"HOME": str(self.home), "PATH": os.defpath, "LC_ALL": "C"}
        # A broken unrelated repository must never be consulted by --config.
        legacy = self.home / ".config/backup/config"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("source /missing/unrelated-repository/profiles.conf\n")

    def config(self, text):
        path = self.root / "selected.conf"
        path.write_text("MACHINE_ID=fixture\nBACKUP_INCLUDE_DEFAULT=false\n" + text)
        return path

    def run_backup(self, config, *args, **env):
        return subprocess.run(
            ["bash", str(SCRIPT), "--config", str(config), *args],
            env=self.env | env,
            text=True,
            capture_output=True,
        )

    def write(self, path, text="selected state"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_two_nodes_two_cursor_profiles_and_repository_independence(self):
        for node in ("node-a", "node-b"):
            profiles = []
            allowlists = []
            for label in ("alpha", "beta"):
                root = self.root / node / label
                self.write(
                    root / f"projects/{label}-project/agent-transcripts/session.jsonl"
                )
                self.write(
                    root / "projects/other-project/agent-transcripts/session.jsonl",
                    "UNSELECTED",
                )
                self.write(root / "auth.json", "SYNTHETIC_AUTH_SENTINEL")
                profiles.append(f"{label}:{root}")
                allowlists.append(
                    f"CURSOR_PROJECT_ALLOWLIST[{label}]={label}-project\n"
                )
            self.write(
                self.home / ".codex/sessions/default.jsonl", "UNSELECTED_DEFAULT"
            )
            target = self.root / "backup" / node
            config = self.config(
                f"BACKUP_ROOT={shlex.quote(str(target))}\nCURSOR_PROFILES={shlex.quote(chr(10).join(profiles))}\n"
                + "".join(allowlists)
            )
            result = self.run_backup(config)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            files = sorted(p for p in target.rglob("*") if p.is_file())
            self.assertEqual(len(files), 2)
            for label in ("alpha", "beta"):
                self.assertEqual(
                    (
                        target
                        / f"cursor-{label}/projects/{label}-project/agent-transcripts/session.jsonl"
                    ).read_text(),
                    "selected state",
                )

    def test_explicit_roots_survive_disabling_all_native_defaults(self):
        selections = []
        for label in ("alpha", "beta"):
            for harness, relative in (
                ("claude", "projects/session.jsonl"),
                ("codex", "sessions/session.jsonl"),
                ("opencode", "config/opencode/opencode.json"),
                ("dsh", "sessions/session.jsonl"),
            ):
                root = self.root / f"{harness}-{label}"
                self.write(root / relative, "{}")
        for harness in ("claude", "codex", "opencode", "dsh"):
            separator = "\n" if harness == "dsh" else " "
            entries = separator.join(
                f"{label}:{self.root}/{harness}-{label}" for label in ("alpha", "beta")
            )
            selections.append(f"{harness.upper()}_PROFILES={shlex.quote(entries)}\n")
        result = self.run_backup(self.config("".join(selections)))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        destination = self.home / "syncthing/backup/fixture"
        for harness in ("claude", "codex", "opencode", "dsh"):
            self.assertFalse((destination / harness).exists())
            for label in ("alpha", "beta"):
                self.assertTrue(any((destination / f"{harness}-{label}").rglob("*")))

    def test_shared_native_cursor_allows_only_literal_selected_project(self):
        root = self.home / ".cursor/projects"
        self.write(root / "selected/agent-transcripts/a.jsonl")
        self.write(root / "unselected/agent-transcripts/future.jsonl", "UNSELECTED")
        config = self.config(
            "CURSOR_INCLUDE_DEFAULT=true\nCURSOR_PROJECT_ALLOWLIST[default]=selected\n"
        )
        result = self.run_backup(config)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        destination = self.home / "syncthing/backup/fixture/cursor/projects"
        self.assertTrue((destination / "selected/agent-transcripts/a.jsonl").is_file())
        self.assertFalse((destination / "unselected").exists())

    def test_missing_selected_config_and_broken_link_never_use_defaults(self):
        self.write(self.home / ".codex/sessions/default.jsonl")
        for config in (self.root / "missing", self.root / "broken-link"):
            if config.name == "broken-link":
                config.symlink_to(self.root / "missing")
            self.assertNotEqual(self.run_backup(config).returncode, 0)
        self.assertFalse((self.home / "syncthing").exists())

    def test_check_is_read_only_and_environment_selection_works(self):
        config = self.config(
            '[[ -z "${ALREADY_LOADED:-}" ]] || exit 91\nALREADY_LOADED=true\nCURSOR_PROFILES=alpha:/unused/cursor\n'
        )
        result = subprocess.run(
            ["bash", str(SCRIPT), "--check"],
            env=self.env | {"BACKUP_CONFIG": str(config)},
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CURSOR_PROFILES=alpha:/unused/cursor", result.stdout)
        self.assertFalse((self.home / ".local").exists())
        self.assertFalse((self.home / "syncthing").exists())

    def test_missing_allowed_project_fails_without_copying_unselected(self):
        self.write(self.home / ".cursor/projects/other/file.jsonl")
        config = self.config(
            "CURSOR_INCLUDE_DEFAULT=true\nCURSOR_PROJECT_ALLOWLIST[default]=missing\n"
        )
        result = self.run_backup(config)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(
            (self.home / "syncthing/backup/fixture/cursor/projects/other").exists()
        )

    def test_invalid_selection_fails_before_any_copy(self):
        for text in (
            "BACKUP_INCLUDE_DEFAULT=auto\n",
            'CURSOR_PROJECT_ALLOWLIST[default]=""\n',
            'CURSOR_PROJECT_ALLOWLIST[default]="*"\n',
            'CURSOR_PROJECT_ALLOWLIST[default]="../other"\n',
            'CURSOR_PROFILES="alpha:/a\nalpha:/b"\n',
            "CURSOR_PROJECT_ALLOWLIST[unknown]=project\n",
        ):
            with self.subTest(text=text):
                self.assertNotEqual(self.run_backup(self.config(text)).returncode, 0)
                self.assertFalse((self.home / "syncthing").exists())


if __name__ == "__main__":
    unittest.main()
