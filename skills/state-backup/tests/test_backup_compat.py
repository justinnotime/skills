"""Compatibility tests for the standalone ``backup.sh`` entry point.

All inputs live under a temporary HOME.  External copy commands are replaced
with small recorders so these tests never inspect or modify machine state.
"""

from __future__ import annotations

import os
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
BACKUP_SCRIPT = REPOSITORY_ROOT / "scripts" / "backup"


BACKUP_ENVIRONMENT_VARIABLES = {
    "BACKUP_LOG",
    "BACKUP_ROOT",
    "CLAUDE_BACKUP_DIR",
    "CLAUDE_HOME",
    "CLAUDE_PROFILES",
    "CODEX_BACKUP_DIR",
    "CODEX_HOME",
    "CODEX_PROFILES",
    "CURSOR_BACKUP_DIR",
    "CURSOR_HOME",
    "CURSOR_USER_DIR",
    "DSH_BACKUP_PREFIX",
    "DSH_BACKUP_DIR",
    "DSH_HOME",
    "DSH_INCLUDE_DEFAULT",
    "DSH_PROFILES",
    "MACHINE_ID",
    "OPENCLAW_BACKUP_DIR",
    "OPENCLAW_HOME",
    "OPENCODE_BACKUP_DIR",
    "OPENCODE_CONFIG_SRC",
    "OPENCODE_DATA_DIR",
    "OPENCODE_PROFILES",
    "OPENCODE_STATE_DIR",
    "SYNCTHING_ROOT",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
}


class BackupCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        self.test_root = Path(temporary_directory.name)
        self.home = self.test_root / "home"
        self.home.mkdir()

        self.command_directory = self.test_root / "commands"
        self.command_directory.mkdir()
        self.rsync_log = self.test_root / "rsync-calls.tsv"
        self.python_log = self.test_root / "python-calls.txt"
        self._install_command_stubs()

        self.environment = os.environ.copy()
        for variable in BACKUP_ENVIRONMENT_VARIABLES:
            self.environment.pop(variable, None)
        self.environment.update(
            {
                "HOME": str(self.home),
                "LC_ALL": "C",
                "MACHINE_ID": "fixture-node",
                "PATH": f"{self.command_directory}{os.pathsep}{os.environ['PATH']}",
                "PYTHON_CALL_LOG": str(self.python_log),
                "PYTHONNOUSERSITE": "1",
                "RSYNC_LOG": str(self.rsync_log),
            }
        )
        self.environment.pop("PYTHONPATH", None)

    def _install_command_stubs(self) -> None:
        rsync = self.command_directory / "rsync"
        rsync.write_text(
            """#!/bin/sh
{
  printf 'CALL'
  for argument in "$@"; do
    printf '\\t%s' "$argument"
  done
  printf '\\n'
} >> "$RSYNC_LOG"
exit 0
""",
            encoding="utf-8",
        )
        rsync.chmod(0o755)

        failing_python = """#!/bin/sh
printf '%s\\n' "$0 $*" >> "$PYTHON_CALL_LOG"
exit 97
"""
        for command_name in ("python", "python3"):
            command = self.command_directory / command_name
            command.write_text(failing_python, encoding="utf-8")
            command.chmod(0o755)

    def _write_file(self, path: Path, contents: str = "synthetic fixture\n") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    def _write_config(self, assignments: dict[str, str]) -> None:
        config = self.home / ".config" / "backup" / "config"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            "".join(
                f"{name}={shlex.quote(value)}\n" for name, value in assignments.items()
            ),
            encoding="utf-8",
        )

    def _run_backup(
        self, *, through_home_symlink: bool = False, expected_status: int = 0
    ) -> subprocess.CompletedProcess[str]:
        command = BACKUP_SCRIPT
        if through_home_symlink:
            command = self.home / "bin" / "backup"
            command.parent.mkdir(parents=True, exist_ok=True)
            command.symlink_to(BACKUP_SCRIPT)

        result = subprocess.run(
            [str(command)],
            cwd=self.home,
            env=self.environment,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            expected_status,
            f"backup command failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}",
        )
        return result

    def _database_fixture(self) -> tuple[Path, Path]:
        source = self.home / ".local/share/opencode/opencode.db"
        destination = self.home / "syncthing/backup/fixture-node/opencode/db/opencode.db"
        self._write_file(source, "synthetic source\n")
        self._write_file(destination, "previous good snapshot\n")
        self._write_file(Path(f"{source}-wal"), "never raw copy WAL\n")
        self._write_file(self.home / ".dsh/sessions/later.jsonl", "{}\n")
        self._write_file(self.home / ".config/opencode/opencode.json", "{}\n")
        return source, destination

    def _sqlite_stub(self, body: str) -> None:
        command = self.command_directory / "sqlite3"
        command.write_text(
            '#!/bin/bash\ntarget=${2#\'.backup "\'}\ntarget=${target%\'"\'}\n' + body,
            encoding="utf-8",
        )
        command.chmod(0o755)

    def _assert_database_failure(self, destination: Path, result: subprocess.CompletedProcess) -> None:
        self.assertEqual(destination.read_text(), "previous good snapshot\n")
        self.assertEqual(list(destination.parent.iterdir()), [destination])
        self.assertIn("opencode (default) backup incomplete", result.stdout)
        self.assertNotIn("opencode (default) backup completed", result.stdout)
        self.assertNotIn("Backup complete!", result.stdout)
        self.assertIn("Backup incomplete: 1 configured target(s) failed", result.stdout)
        calls = self._recorded_rsync_calls()
        self.assertIn(str(self.home / ".dsh") + "/", calls)
        self.assertIn(str(self.home / ".config/opencode") + "/", calls)
        self.assertNotIn("opencode.db", calls)
        log = self.home / ".local/log/backup.log"
        self.assertIn("Backup incomplete:", log.read_text())

    def test_missing_sqlite_preserves_snapshot_and_continues_other_harnesses(self) -> None:
        _, destination = self._database_fixture()
        for name in ("mkdir", "dirname", "date", "tee", "find", "wc", "du", "cut", "basename"):
            (self.command_directory / name).symlink_to(shutil.which(name))
        self.environment["PATH"] = str(self.command_directory)
        result = self._run_backup(expected_status=1)
        self.assertIn("sqlite3 is required", result.stdout)
        self._assert_database_failure(destination, result)

    def test_failed_sqlite_removes_partial_snapshot_and_continues(self) -> None:
        _, destination = self._database_fixture()
        self._sqlite_stub(
            'for suffix in "" -journal -wal -shm; do printf partial > "$target$suffix"; done\n'
            'exit 1\n'
        )
        result = self._run_backup(expected_status=1)
        self._assert_database_failure(destination, result)

    def test_failed_first_snapshot_publishes_nothing(self) -> None:
        _, destination = self._database_fixture()
        destination.unlink()
        self._sqlite_stub('printf partial > "$target"\nexit 1\n')
        self._run_backup(expected_status=1)
        self.assertEqual(list(destination.parent.iterdir()), [])

    def test_directory_at_snapshot_path_is_not_a_successful_replacement(self) -> None:
        _, destination = self._database_fixture()
        destination.unlink()
        destination.mkdir()
        self._sqlite_stub('printf snapshot > "$target"\n')
        self._run_backup(expected_status=1)
        self.assertEqual(list(destination.iterdir()), [])
        self.assertEqual(list(destination.parent.iterdir()), [destination])

    def test_interrupted_sqlite_cleans_its_temporary_snapshot(self) -> None:
        _, destination = self._database_fixture()
        self._sqlite_stub('printf partial > "$target"\nkill -TERM "$PPID"\nexit 1\n')
        result = self._run_backup(expected_status=1)
        self._assert_database_failure(destination, result)

    def test_failed_rename_preserves_previous_snapshot(self) -> None:
        _, destination = self._database_fixture()
        self._sqlite_stub('printf complete > "$target"\n')
        command = self.command_directory / "mv"
        command.write_text("#!/bin/sh\nexit 1\n")
        command.chmod(0o755)
        result = self._run_backup(expected_status=1)
        self._assert_database_failure(destination, result)

    def test_failed_profile_does_not_block_later_database_or_profile(self) -> None:
        source, destination = self._database_fixture()
        self._write_file(source.parent / "second.db")
        additional = self.home / "additional"
        self._write_file(additional / "share/opencode/other.db")
        self._write_config({"OPENCODE_PROFILES": f"extra:{additional}"})
        self._sqlite_stub(
            'printf snapshot > "$target"\n'
            '[[ "$1" != */opencode.db ]]\n'
        )
        result = self._run_backup(expected_status=1)
        self.assertEqual(destination.read_text(), "previous good snapshot\n")
        self.assertEqual((destination.parent / "second.db").read_text(), "snapshot")
        self.assertEqual(
            (destination.parents[2] / "opencode-extra/db/other.db").read_text(), "snapshot"
        )
        self.assertIn("opencode (extra) backup completed", result.stdout)
        self.assertIn("Backup incomplete: 1 configured target(s) failed", result.stdout)

    @unittest.skipUnless(shutil.which("sqlite3"), "sqlite3 CLI required for real WAL snapshot test")
    def test_real_wal_snapshot_is_atomic_and_handles_quoted_destination(self) -> None:
        source, _ = self._database_fixture()
        source.unlink()
        Path(f"{source}-wal").unlink()
        destination = self.test_root / 'backup with spaces and \'quotes"\\' / "db/opencode.db"
        self._write_file(destination, "previous good snapshot\n")
        self._write_config({"OPENCODE_BACKUP_DIR": str(destination.parent.parent)})
        with closing(sqlite3.connect(source)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            connection.execute("CREATE TABLE sessions (value TEXT)")
            connection.execute("INSERT INTO sessions VALUES ('committed in WAL')")
            connection.commit()
            with destination.open() as previous_reader:
                self._run_backup()
                self.assertEqual(previous_reader.read(), "previous good snapshot\n")
            with closing(sqlite3.connect(destination)) as snapshot:
                self.assertEqual(snapshot.execute("PRAGMA integrity_check").fetchone(), ("ok",))
                self.assertEqual(snapshot.execute("SELECT value FROM sessions").fetchall(), [("committed in WAL",)])
        self.assertEqual(list(destination.parent.iterdir()), [destination])

    def test_concurrent_failure_cannot_remove_another_runs_good_snapshot(self) -> None:
        _, destination = self._database_fixture()
        ready = self.test_root / "ready"
        release = self.test_root / "release"
        self._sqlite_stub(
            'printf complete > "$target"\n'
            'if [[ -n "${HOLD_SNAPSHOT:-}" ]]; then\n'
            '  touch "$HOLD_SNAPSHOT/ready"\n'
            '  while [[ ! -f "$HOLD_SNAPSHOT/release" ]]; do sleep 0.02; done\n'
            '  exit 1\n'
            'fi\n'
        )
        environment = dict(self.environment, HOLD_SNAPSHOT=str(self.test_root))
        process = subprocess.Popen(
            [str(BACKUP_SCRIPT)], cwd=self.home, env=environment,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "first snapshot never started")
            self.assertEqual(destination.read_text(), "previous good snapshot\n")
            self.assertEqual(len(list(destination.parent.glob(".opencode-backup.*"))), 1)
            self._run_backup()
            self.assertEqual(destination.read_text(), "complete")
            self.assertEqual(len(list(destination.parent.glob(".opencode-backup.*"))), 1)
        finally:
            release.touch()
            stdout, stderr = process.communicate(timeout=20)
        self.assertEqual(process.returncode, 1, stdout + stderr)
        self.assertEqual(destination.read_text(), "complete")
        self.assertEqual(list(destination.parent.iterdir()), [destination])

    def _recorded_rsync_calls(self) -> str:
        return self.rsync_log.read_text(encoding="utf-8")

    def test_native_defaults_and_home_symlink_need_no_python_runtime(self) -> None:
        self._write_file(
            self.home / ".openclaw" / "agents" / "main" / "sessions" / "session.jsonl",
            "{}\n",
        )
        self._write_file(
            self.home / ".claude" / "projects" / "project" / "session.jsonl",
            "{}\n",
        )
        self._write_file(
            self.home / ".codex" / "sessions" / "2026" / "session.jsonl",
            "{}\n",
        )
        self._write_file(
            self.home / ".dsh" / "sessions" / "session.jsonl",
            "{}\n",
        )
        self._write_file(
            self.home / ".config" / "opencode" / "opencode.json",
            "{}\n",
        )
        self._write_file(
            self.home
            / ".cursor"
            / "projects"
            / "project"
            / "agent-transcripts"
            / "session"
            / "transcript.jsonl",
            "{}\n",
        )

        self._run_backup(through_home_symlink=True)

        output = self.home / "syncthing" / "backup" / "fixture-node"
        expected_directories = (
            output / "openclaw" / "sessions",
            output / "claude" / "projects",
            output / "codex" / "sessions",
            output / "dsh",
            output / "opencode" / "config",
            output / "cursor" / "projects",
        )
        for directory in expected_directories:
            with self.subTest(directory=directory):
                self.assertTrue(directory.is_dir())

        calls = self._recorded_rsync_calls()
        for native_source in (
            self.home / ".openclaw" / "agents" / "main" / "sessions",
            self.home / ".claude" / "projects",
            self.home / ".codex" / "sessions",
            self.home / ".dsh",
            self.home / ".config" / "opencode",
            self.home / ".cursor" / "projects",
        ):
            with self.subTest(native_source=native_source):
                self.assertIn(f"{native_source}/", calls)

        self.assertFalse(
            self.python_log.exists(),
            "backup.sh unexpectedly invoked Python",
        )

    def test_existing_profile_formats_keep_their_destination_names(self) -> None:
        profile_roots: dict[str, dict[str, Path]] = {
            "claude": {
                "alpha": self.home / ".claude-alpha",
                "beta": self.home / ".claude-beta",
            },
            "codex": {
                "alpha": self.home / ".codex-alpha",
                "beta": self.home / ".codex-beta",
            },
            "opencode": {
                "alpha": self.home / ".opencode-alpha",
                "beta": self.home / ".opencode-beta",
            },
            "dsh": {
                "alpha": self.home / ".dsh-alpha",
                "beta": self.home / "synthetic roots" / ".dsh-beta",
            },
        }

        for root in profile_roots["claude"].values():
            self._write_file(root / "projects" / "project" / "session.jsonl", "{}\n")
        for root in profile_roots["codex"].values():
            self._write_file(root / "sessions" / "2026" / "session.jsonl", "{}\n")
        for root in profile_roots["opencode"].values():
            self._write_file(root / "config" / "opencode" / "opencode.json", "{}\n")
        for root in profile_roots["dsh"].values():
            self._write_file(root / "sessions" / "session.jsonl", "{}\n")

        output = self.test_root / "backup-output"
        self._write_config(
            {
                "MACHINE_ID": "fixture-node",
                "BACKUP_ROOT": str(output),
                "CLAUDE_PROFILES": " ".join(
                    f"{label}:{root}" for label, root in profile_roots["claude"].items()
                ),
                "CODEX_PROFILES": " ".join(
                    f"{label}:{root}" for label, root in profile_roots["codex"].items()
                ),
                "OPENCODE_PROFILES": " ".join(
                    f"{label}:{root}"
                    for label, root in profile_roots["opencode"].items()
                ),
                "DSH_PROFILES": "\n".join(
                    f"{label}:{root}" for label, root in profile_roots["dsh"].items()
                ),
            }
        )

        self._run_backup()

        expected_directories = (
            output / "claude-alpha" / "projects",
            output / "claude-beta" / "projects",
            output / "codex-alpha" / "sessions",
            output / "codex-beta" / "sessions",
            output / "opencode-alpha" / "config",
            output / "opencode-beta" / "config",
            output / "dsh-alpha",
            output / "dsh-beta",
        )
        for directory in expected_directories:
            with self.subTest(directory=directory):
                self.assertTrue(directory.is_dir())

        calls = self._recorded_rsync_calls()
        for tool_roots in profile_roots.values():
            for source in tool_roots.values():
                with self.subTest(source=source):
                    self.assertIn(str(source), calls)


if __name__ == "__main__":
    unittest.main()
