"""Synthetic registry and real Git tests for configured commit attribution."""

import json
import importlib.util
import sqlite3
from unittest.mock import patch
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "seat-trailer.py"

MEMBERS = [
    {
        "agent_id": "aaaa-1111",
        "handle": "node-a/worker-a",
        "host": "test-host",
        "tmux": "tmux=0:14.0 win=codex",
        "status": "active",
        "mode": "pull",
    },
    {
        "agent_id": "bbbb-2222",
        "handle": "node-a/retired",
        "host": "test-host",
        "tmux": "tmux=0:15.0 win=claude",
        "status": "retired",
        "mode": "watch",
    },
    {
        "agent_id": "cccc-3333",
        "handle": "node-b/worker-b",
        "host": "other-host",
        "tmux": "tmux=0:14.0 win=codex",
        "status": "active",
        "mode": "pull",
    },
]


class SeatTrailerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.members = base / "members.jsonl"
        self.members.write_text("\n".join(json.dumps(m) for m in MEMBERS) + "\n")
        self.msg = base / "COMMIT_EDITMSG"
        binary = base / "bin"
        binary.mkdir()
        tmux = binary / "tmux"
        tmux.write_text(
            "#!/usr/bin/env python3\nimport os\npane=os.environ.get('SEAT_TRAILER_PANE_LOC','')\nprint('%42 '+pane if pane else '')\n"
        )
        tmux.chmod(0o755)
        self.configuration = base / "config.json"
        self.configuration.write_text(
            json.dumps(
                {
                    "schema": "fleet-runtime/v1",
                    "tmux": {"server_file": str(base / "tmux-server")},
                    "seat_trailer": {
                        "ledger": str(base / "absent.sqlite3"),
                        "members_command": [
                            sys.executable,
                            "-c",
                            "import os,pathlib;print(pathlib.Path(os.environ['SEAT_TRAILER_MEMBERS']).read_text())",
                        ],
                        "agent_windows": ["claude", "codex"],
                        "host": "test-host",
                        "trailer_key": "Seat",
                    },
                }
            )
        )
        self.hook = base / "prepare-commit-msg"
        self.hook.write_text(
            "#!/bin/sh\nexec "
            + shlex.quote(sys.executable)
            + " "
            + shlex.quote(str(HELPER))
            + ' "$@"\n'
        )
        self.hook.chmod(0o755)

    def environment(self):
        base = Path(self.tmp.name)
        return {
            "HOME": str(base),
            "PATH": str(base / "bin") + os.pathsep + os.environ["PATH"],
            "FLEET_ORCHESTRATOR_CONFIG": str(self.configuration),
            "TMUX_PANE": "%42",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "PYTHONDONTWRITEBYTECODE": "1",
        }

    def run_hook(self, message: str, pane: str | None, *, via_wrapper=False):
        self.msg.write_text(message)
        env = self.environment()

        env["SEAT_TRAILER_MEMBERS"] = str(self.members)
        env["SEAT_TRAILER_HOST"] = "test-host"
        if pane is None:
            env["SEAT_TRAILER_PANE_LOC"] = ""
        else:
            env["SEAT_TRAILER_PANE_LOC"] = pane
        cmd = (
            ["bash", str(self.hook), str(self.msg)]
            if via_wrapper
            else [sys.executable, str(HELPER), str(self.msg)]
        )
        done = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return self.msg.read_text()

    def test_registered_seat_pane_names_handle_and_id(self):
        out = self.run_hook("fix: thing\n\nbody\n", "0:14.0 codex")
        self.assertIn("Seat: node-a/worker-a (aaaa-1111)", out)
        self.assertTrue(out.startswith("fix: thing\n"), "subject untouched")

    def test_retired_registration_does_not_name_the_seat(self):
        # a retired row is not an active seat; the pane is still an agent pane
        out = self.run_hook("fix: thing\n", "0:15.0 claude")
        self.assertNotIn("retired", out)
        self.assertIn("Seat: unregistered claude pane 0:15.0", out)

    def test_unregistered_agent_pane_is_recorded(self):
        out = self.run_hook("docs: x\n", "0:2.0 codex")
        self.assertIn("Seat: unregistered codex pane 0:2.0", out)

    def test_shell_pane_and_no_tmux_add_nothing(self):
        self.assertEqual(self.run_hook("chore: x\n", "0:0.0 zsh"), "chore: x\n")
        self.assertEqual(self.run_hook("chore: x\n", None), "chore: x\n")

    def test_other_host_registration_is_not_this_seat(self):
        # same window number on another host must not be mistaken for ours
        out = self.run_hook("x\n", "0:14.0 codex")
        self.assertNotIn("node-b/worker-b", out)

    def test_idempotent_when_trailer_present(self):
        msg = "fix: thing\n\nSeat: node-a/worker-a (aaaa-1111)\n"
        self.assertEqual(self.run_hook(msg, "0:14.0 codex"), msg)

    def test_trailer_sits_above_template_comments(self):
        msg = "fix: thing\n\n# Please enter the commit message\n# On branch x\n"
        out = self.run_hook(msg, "0:14.0 codex")
        self.assertLess(
            out.index("Seat: node-a/worker-a"),
            out.index("# Please enter"),
            "git interpret-trailers keeps the trailer above comments",
        )

    def _git_repo_with_hook(self):
        """A scratch repository whose prepare-commit-msg hook is the real
        wrapper + helper, so replays are driven by real git, not by a seam."""
        base = Path(self.tmp.name)
        repo = base / "repo"
        hooks = base / "hooks"
        hooks.mkdir()
        (hooks / "prepare-commit-msg").write_text(self.hook.read_text())
        (hooks / "prepare-commit-msg").chmod(0o755)
        repo.mkdir()
        self._git(repo, "init", "-q", "-b", "main")
        self._git(repo, "config", "core.hooksPath", str(hooks))
        self._git(repo, "config", "user.name", "t")
        self._git(repo, "config", "user.email", "t@example.invalid")
        return repo

    def _git(self, repo, *args, pane=None):
        env = dict(
            self.environment(),
            SEAT_TRAILER_MEMBERS=str(self.members),
            SEAT_TRAILER_HOST="test-host",
            SEAT_TRAILER_PANE_LOC="" if pane is None else pane,
        )
        env.pop("GIT_REFLOG_ACTION", None)

        done = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        self.assertEqual(
            done.returncode, 0, f"git {args}\n{done.stdout}\n{done.stderr}"
        )
        return done.stdout

    def _log_body(self, repo, ref):
        return self._git(repo, "log", "-1", "--format=%B", ref)

    def test_rebase_and_cherry_pick_replays_are_not_stamped(self):
        repo = self._git_repo_with_hook()
        (repo / "a").write_text("1\n")
        self._git(repo, "add", "a")
        self._git(repo, "commit", "-q", "-m", "base")
        self._git(repo, "checkout", "-q", "-b", "side")
        (repo / "b").write_text("2\n")
        self._git(repo, "add", "b")
        self._git(repo, "commit", "-q", "-m", "theirs: no seat")  # shell pane
        self.assertNotIn("Seat:", self._log_body(repo, "side"))
        self._git(repo, "checkout", "-q", "main")
        (repo / "c").write_text("3\n")
        self._git(repo, "add", "c")
        self._git(repo, "commit", "-q", "-m", "main moved")
        self._git(repo, "checkout", "-q", "side")
        self._git(repo, "rebase", "-q", "main", pane="0:14.0 codex")  # seat replays
        self.assertNotIn(
            "Seat:",
            self._log_body(repo, "side"),
            "a rebase replay must not gain the rebasing seat's trailer",
        )
        self._git(repo, "checkout", "-q", "main")
        self._git(repo, "cherry-pick", "side", pane="0:14.0 codex")
        self.assertNotIn(
            "Seat:",
            self._log_body(repo, "main"),
            "a cherry-pick replay must not gain the picking seat's trailer",
        )
        # A clean revert creates a new commit without replay markers on this Git.
        self._git(repo, "revert", "--no-edit", "HEAD", pane="0:14.0 codex")
        self.assertIn("Seat: node-a/worker-a (aaaa-1111)", self._log_body(repo, "main"))
        # A new commit by the participant is still attributed.
        (repo / "d").write_text("4\n")
        self._git(repo, "add", "d")
        self._git(repo, "commit", "-q", "-m", "mine", pane="0:14.0 codex")
        self.assertIn("Seat: node-a/worker-a (aaaa-1111)", self._log_body(repo, "main"))
        # and an amend keeps exactly one trailer
        self._git(repo, "commit", "-q", "--amend", "--no-edit", pane="0:14.0 codex")
        self.assertEqual(self._log_body(repo, "main").count("Seat:"), 1)

    def test_non_utf8_message_is_handled_without_a_traceback(self):
        self.msg.write_bytes("fix: caf\xe9\n".encode("latin-1"))
        env = dict(
            self.environment(),
            SEAT_TRAILER_MEMBERS=str(self.members),
            SEAT_TRAILER_PANE_LOC="0:14.0 codex",
            SEAT_TRAILER_HOST="test-host",
        )
        done = subprocess.run(
            [sys.executable, str(HELPER), str(self.msg)],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn("Traceback", done.stderr)
        raw = self.msg.read_bytes()
        self.assertTrue(
            raw.startswith("fix: caf\xe9\n".encode("latin-1")), "bytes preserved"
        )
        self.assertIn(b"Seat: node-a/worker-a (aaaa-1111)", raw)

    def test_wrapper_never_blocks_the_commit(self):
        # a broken members file makes resolution fail: the wrapper still exits 0
        self.members.write_text("{not json\n")
        out = self.run_hook("fix: thing\n", "0:14.0 codex", via_wrapper=True)
        self.assertEqual(out, "fix: thing\n")
        env = dict(
            self.environment(),
            SEAT_TRAILER_MEMBERS="/nonexistent/members",
            SEAT_TRAILER_PANE_LOC="0:14.0 codex",
            SEAT_TRAILER_HOST="test-host",
        )
        self.msg.write_text("fix: thing\n")
        done = subprocess.run(
            ["bash", str(self.hook), str(self.msg)],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        self.assertEqual(
            done.returncode, 0, "attribution failure must not fail the commit"
        )
        self.assertIn("seat trailer skipped", done.stderr)
        self.assertEqual(self.msg.read_text(), "fix: thing\n")

    def test_grouped_sessions_keep_all_aliases(self):
        result = self.run_hook("fix: grouped\n", "viewer:14.0 codex\n%42 0:14.0 codex")
        self.assertIn("Seat: node-a/worker-a (aaaa-1111)", result)

    def test_ambiguous_identity_is_reported_without_choosing(self):
        rows = MEMBERS + [
            {**MEMBERS[0], "agent_id": "dddd-4444", "handle": "node-a/worker-d"}
        ]
        self.members.write_text("\n".join(json.dumps(row) for row in rows))
        result = self.run_hook("fix: ambiguous\n", "0:14.0 codex")
        self.assertIn(
            "Seat: ambiguous pane 0:14.0 (node-a/worker-a,node-a/worker-d)", result
        )

    def test_legacy_ledger_never_substitutes_for_unavailable_members(self):
        ledger = Path(self.tmp.name) / "ledger?synthetic#test.sqlite3"
        with sqlite3.connect(ledger) as connection:
            connection.execute(
                "CREATE TABLE seat(agent_id,handle,host,tmux,addressable)"
            )
            row = MEMBERS[0]
            connection.execute(
                "INSERT INTO seat VALUES(?,?,?,?,1)",
                tuple(row[key] for key in ("agent_id", "handle", "host", "tmux")),
            )
        config = json.loads(self.configuration.read_text())
        config["seat_trailer"]["ledger"] = str(ledger)
        config["seat_trailer"]["members_command"] = ["/unavailable/command"]
        self.configuration.write_text(json.dumps(config))
        before = ledger.read_bytes()
        result = self.run_hook("fix: cache\n", "0:14.0 codex")
        self.assertEqual(result, "fix: cache\n")
        self.assertEqual(ledger.read_bytes(), before)
        self.assertFalse(Path(str(ledger) + "-journal").exists())

    def test_configuration_without_ledger_uses_current_bus_members(self):
        config = json.loads(self.configuration.read_text())
        del config["seat_trailer"]["ledger"]
        self.configuration.write_text(json.dumps(config))
        result = self.run_hook("fix: direct source\n", "0:14.0 codex")
        self.assertIn("Seat: node-a/worker-a (aaaa-1111)", result)

    def test_exact_pane_uses_current_location_after_window_rename(self):
        row = {**MEMBERS[0], "pane_id": "%42", "terminal_presence": "present",
               "tmux": "tmux=renamed:2.0 win=codex"}
        self.members.write_text(json.dumps(row))
        result = self.run_hook("fix: moved\n", "renamed:2.0 codex")
        self.assertIn("Seat: node-a/worker-a (aaaa-1111)", result)
        row["pane_id"] = "%43"
        self.members.write_text(json.dumps(row))
        result = self.run_hook("fix: neighbour\n", "renamed:2.0 codex")
        self.assertNotIn("aaaa-1111", result)

    def test_missing_ledger_is_never_created(self):
        self.run_hook("fix: missing cache\n", "0:14.0 codex")
        self.assertFalse((Path(self.tmp.name) / "absent.sqlite3").exists())

    def test_metadata_cannot_inject_additional_trailers(self):
        self.members.write_text(
            json.dumps({**MEMBERS[0], "handle": "not-a-label\nOther: injected"})
        )
        self.assertEqual(
            self.run_hook("fix: injection\n", "0:14.0 codex"), "fix: injection\n"
        )

    def test_unconfigured_helper_does_not_touch_message(self):
        self.configuration.write_text('{"schema":"fleet-runtime/v1"}')
        self.assertEqual(
            self.run_hook("fix: disabled\n", "0:14.0 codex"), "fix: disabled\n"
        )

    def test_failed_atomic_install_preserves_message_and_removes_temporary(self):
        spec = importlib.util.spec_from_file_location("seat_trailer_synthetic", HELPER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.msg.write_bytes(b"fix: original\n")
        with patch.object(
            module.os, "replace", side_effect=OSError("synthetic failure")
        ):
            with self.assertRaises(OSError):
                module.append_trailer(self.msg, "Seat: synthetic")
        self.assertEqual(self.msg.read_bytes(), b"fix: original\n")
        self.assertEqual(list(Path(self.tmp.name).glob(".seat-trailer-*")), [])

    def test_interpret_trailers_failure_uses_plain_append_preserving_bytes(self):
        spec = importlib.util.spec_from_file_location("seat_trailer_fallback", HELPER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.msg.write_bytes(b"fix: original\n")
        with patch.object(
            module.subprocess, "run", side_effect=OSError("synthetic unavailable")
        ):
            module.append_trailer(self.msg, "Seat: synthetic")
        self.assertEqual(self.msg.read_bytes(), b"fix: original\n\nSeat: synthetic\n")

    def test_reflog_replay_actions_preserve_message(self):
        for action in ("rebase", "cherry-pick", "revert", "pull --rebase"):
            self.msg.write_text("fix: replay\n")
            env = dict(
                self.environment(),
                GIT_REFLOG_ACTION=action,
                SEAT_TRAILER_MEMBERS=str(self.members),
                SEAT_TRAILER_PANE_LOC="0:14.0 codex",
            )
            result = subprocess.run(
                [sys.executable, str(HELPER), str(self.msg)],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0)
            self.assertEqual(self.msg.read_text(), "fix: replay\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
