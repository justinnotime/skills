#!/usr/bin/env python3

import argparse
import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("agent_bus_v3", ROOT / "scripts" / "agent-bus-v3.py")
bus = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(bus)


class AgentBusV3Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        bus.CFG = Path(self.tmp.name)
        bus.DB_PATH = bus.CFG / "bus.sqlite3"
        (bus.CFG / "auth.hdr").write_text("Authorization: Bearer test\n")
        self.home = Path(self.tmp.name) / "home"
        self.xdg_config = Path(self.tmp.name) / "xdg-config"
        self.git_template = Path(self.tmp.name) / "git-template"
        for directory in (self.home, self.xdg_config, self.git_template):
            directory.mkdir()
        # Subprocesses get an explicit environment, not the developer's pane,
        # Python startup hooks, Git templates/hooks, or user/system Git config.
        # source_snapshot() invokes Git, so isolation belongs in the fixture
        # rather than in whichever shell happens to run the test.
        self.env_patch = mock.patch.dict(os.environ, {
            "PATH": os.environ.get("PATH", os.defpath),
            "AGENT_BUS_TRANSPORT": "matrix",
            "HOME": str(self.home),
            "XDG_CONFIG_HOME": str(self.xdg_config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TEMPLATE_DIR": str(self.git_template),
            "NOTES_RUNTIME_DIR": self.tmp.name,
        }, clear=True)
        self.env_patch.start()
        self.states = {}
        self.timeline = []
        self.token = 0

        def fake_sync(_token, _timeout):
            self.token += 1
            events, self.timeline = self.timeline, []
            return {"next_batch": f"t{self.token}", "rooms": {"join": {bus.ROOM: {"timeline": {"events": events}}}}}

        def fake_state(agent_id, content):
            self.states[agent_id] = content
            return f"$state-{agent_id}"

        def fake_event(event_type, txn, content):
            self.timeline.append({"event_id": f"${txn}", "type": event_type, "origin_server_ts": bus.now_ms(), "content": content})
            return f"${txn}"

        self.sync_patch = mock.patch.object(bus, "sync", side_effect=fake_sync)
        self.state_patch = mock.patch.object(bus, "put_state", side_effect=fake_state)
        self.event_patch = mock.patch.object(bus, "put_event", side_effect=fake_event)
        self.members_patch = mock.patch.object(bus, "room_members", side_effect=lambda: list(self.states.values()))
        self.panes_patch = mock.patch.object(bus.tmux_runtime, "pane_snapshot", side_effect=lambda: {
            f"%{number}": {"server_id": "synthetic-server", "location": f"0:{number}.0", "dead": False}
            for number in self.__dict__.get("_slot_panes", {}).values()
        })
        for patcher in (self.sync_patch, self.state_patch, self.event_patch, self.members_patch, self.panes_patch):
            patcher.start()

    def tearDown(self):
        mock.patch.stopall()
        self.tmp.cleanup()

    def join(self, handle, slot, mode="watch"):
        panes = self.__dict__.setdefault("_slot_panes", {})
        if slot not in panes:
            panes[slot] = len(panes) + 1
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.dict(os.environ, {"TMUX_PANE": f"%{panes[slot]}"}):
            bus.cmd_join(argparse.Namespace(handle=handle, slot=slot, harness="test", mode=mode, host="host", tmux=f"tmux=0:{panes[slot]}.0"))
        return json.loads(output.getvalue())

    def test_subprocess_environment_is_private(self):
        self.assertEqual(Path.home(), self.home)
        self.assertEqual(os.environ["XDG_CONFIG_HOME"], str(self.xdg_config))
        self.assertEqual(os.environ["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(os.environ["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(os.environ["GIT_TEMPLATE_DIR"], str(self.git_template))
        for name in ("TMUX_PANE", "PYTHONPATH", "PYTHONINSPECT",
                     "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"):
            self.assertNotIn(name, os.environ)

    def test_pause_created_during_source_hash_prevents_reexec(self):
        def changed_source():
            (bus.CFG / "auto-reexec.disabled").touch()
            return "sha256:new"

        with mock.patch.object(bus, "source_identity", side_effect=changed_source), \
                mock.patch.object(bus, "exec_current_source") as reexec:
            bus.maybe_reexec("sha256:old")
        reexec.assert_not_called()

    def test_source_identity_uses_bytes_not_mtime(self):
        source = Path(self.tmp.name) / "resident.py"
        source.write_text("one\n")
        first = bus.source_identity(source)
        os.utime(source, (source.stat().st_atime + 100, source.stat().st_mtime + 100))
        self.assertEqual(bus.source_identity(source), first)
        source.write_text("two\n")
        self.assertNotEqual(bus.source_identity(source), first)

    def test_resident_exec_argv_keeps_only_public_command(self):
        with mock.patch.object(
            bus.sys,
            "argv", [str(bus.SOURCE_PATH), "watch", "agent-1"],
        ):
            argv = bus.resident_exec_argv(9)
        self.assertEqual(argv[-2:], ["watch", "agent-1"])
        self.assertEqual(argv[0], bus.sys.executable)
        self.assertEqual(argv[1:5], ["-I", "-S", "-c", bus.SNAPSHOT_LOADER])
        self.assertEqual(argv[5], "9")
        self.assertEqual(Path(argv[6]), bus.SOURCE_PATH)

    def test_snapshot_loader_ignores_shared_fd_offset(self):
        source = b"import os\nos.write(1,b'loaded-from-zero')\n"
        fd = os.memfd_create(
            "agent-bus-v3-source", os.MFD_ALLOW_SEALING
        )
        try:
            os.write(fd, source)
            os.lseek(fd, 0, os.SEEK_END)
            bus.fcntl.fcntl(fd, bus.F_ADD_SEALS, bus.SOURCE_SEALS)
            result = subprocess.run(
                [
                    bus.sys.executable, "-I", "-S", "-c",
                    bus.SNAPSHOT_LOADER, str(fd), "resident.py",
                ],
                pass_fds=[fd],
                capture_output=True,
                timeout=5,
            )
        finally:
            os.close(fd)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"loaded-from-zero")

    def test_former_loaded_identity_argument_is_not_public(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit):
                bus.parser().parse_args([
                    "watch", "agent-1", "--loaded-source-identity",
                    "sha256:" + "a" * 64,
                ])
        self.assertIn("unrecognized arguments", stderr.getvalue())

    def test_source_snapshot_keeps_verified_bytes_after_file_changes(self):
        source = Path(self.tmp.name) / "resident.py"
        source.write_text("value = 'one'\n")
        identity, fd = bus.source_snapshot(source)
        try:
            source.write_text("value = 'two'\n")
            self.assertEqual(identity, bus.source_fd_identity(fd))
            self.assertNotEqual(identity, bus.source_identity(source))
        finally:
            os.close(fd)

    def test_published_source_comes_from_complete_git_blob(self):
        repo = Path(self.tmp.name) / "published"
        source = repo / "scripts" / "agent-bus-v3.py"
        source.parent.mkdir(parents=True)
        complete = b"value = 'complete'\n"
        source.write_bytes(complete)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "add", "scripts/agent-bus-v3.py"],
            check=True,
        )
        subprocess.run(
            [
                "git", "-C", str(repo),
                "-c", "user.name=Agent Bus Test",
                "-c", "user.email=agent-bus-test@invalid",
                "commit", "-qm", "published source",
            ],
            check=True,
        )

        # Model the short interval in which a checkout path can expose a
        # truncated prefix. Publication is the committed blob, not that
        # transient worktree view.
        source.write_bytes(b"value =")
        expected = "sha256:" + bus.hashlib.sha256(complete).hexdigest()
        self.assertEqual(bus.source_identity(source), expected)
        identity, fd = bus.source_snapshot(source)
        try:
            self.assertEqual(identity, expected)
            self.assertEqual(os.pread(fd, len(complete), 0), complete)
        finally:
            os.close(fd)

    def test_unsealed_source_fd_is_rejected(self):
        fd = os.memfd_create("agent-bus-unsealed-test", flags=0)
        try:
            os.write(fd, b"print('not trusted')\n")
            with self.assertRaisesRegex(RuntimeError, "not fully sealed"):
                bus.source_fd_identity(fd)
        finally:
            os.close(fd)

    def test_idle_boundary_reexecs_only_on_content_change(self):
        with (
            mock.patch.object(bus, "auto_reexec_enabled", return_value=True),
            mock.patch.object(bus, "source_identity", return_value="sha256:new"),
            mock.patch.object(bus, "exec_current_source") as reexec,
        ):
            bus.maybe_reexec("sha256:old")
            reexec.assert_called_once_with("sha256:new")
            reexec.reset_mock()
            bus.maybe_reexec("sha256:new")
            reexec.assert_not_called()

    def test_runtime_file_kill_switch_disables_reexec(self):
        switch = bus.CFG / "auto-reexec.disabled"
        switch.touch()
        with (
            mock.patch.dict(os.environ, {"AGENT_BUS_AUTO_REEXEC": "1"}),
            mock.patch.object(bus, "source_identity", return_value="sha256:new"),
            mock.patch.object(bus, "exec_current_source") as reexec,
        ):
            bus.maybe_reexec("sha256:old")
        reexec.assert_not_called()

    def test_startup_kill_switch_skips_snapshot_until_a_boundary(self):
        captured = {}
        args = argparse.Namespace(
            command="dispatch",
            once=False,
            func=lambda parsed: captured.setdefault(
                "identity", parsed.loaded_source_identity
            ),
        )
        fake_parser = mock.Mock()
        fake_parser.parse_args.return_value = args
        with (
            mock.patch.object(bus, "parser", return_value=fake_parser),
            mock.patch.object(bus, "LOADED_SOURCE_FD", None),
            mock.patch.object(bus, "auto_reexec_enabled", return_value=False),
            mock.patch.object(bus, "exec_current_source") as reexec,
        ):
            bus.main()
        reexec.assert_not_called()
        self.assertEqual(captured["identity"], "")

        with (
            mock.patch.object(bus, "auto_reexec_enabled", return_value=True),
            mock.patch.object(bus, "source_identity", return_value="sha256:new"),
            mock.patch.object(bus, "exec_current_source") as reexec,
        ):
            bus.maybe_reexec("")
        reexec.assert_called_once_with("sha256:new")

    def test_bad_dispatch_interval_affects_only_dispatch(self):
        with mock.patch.dict(
            os.environ, {"AGENT_BUS_DISPATCH_INTERVAL": "not-a-number"}
        ):
            parsed = bus.parser().parse_args(["source-identity"])
            self.assertEqual(parsed.command, "source-identity")
            with self.assertRaisesRegex(
                RuntimeError, "AGENT_BUS_DISPATCH_INTERVAL.*not-a-number"
            ):
                bus.configured_dispatch_interval()
        for invalid in ("nan", "inf", "-1"):
            with mock.patch.dict(
                os.environ, {"AGENT_BUS_DISPATCH_INTERVAL": invalid}
            ):
                with self.assertRaisesRegex(RuntimeError, "finite non-negative"):
                    bus.configured_dispatch_interval()
        for invalid in ("nan", "inf", "-1", "not-a-number"):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                with self.assertRaises(SystemExit):
                    bus.parser().parse_args(["dispatch", "--interval", invalid])
            self.assertIn("--interval", stderr.getvalue())

    def test_reexec_failure_exits_nonzero_and_is_loud(self):
        stderr = io.StringIO()
        snapshot_fd = os.memfd_create("agent-bus-test-snapshot", flags=0)
        with (
            mock.patch.object(bus.sys, "argv", [str(bus.SOURCE_PATH), "watch", "agent-1"]),
            mock.patch.object(
                bus, "source_snapshot", return_value=("sha256:new", snapshot_fd)
            ),
            mock.patch.object(bus.os, "execv", side_effect=OSError("exec denied")),
            contextlib.redirect_stderr(stderr),
        ):
            with self.assertRaises(SystemExit) as caught:
                bus.exec_current_source("sha256:new")
        self.assertEqual(caught.exception.code, 70)
        self.assertIn("FATAL agent-bus-v3: re-exec failed", stderr.getvalue())

    @contextlib.contextmanager
    def lock_ingest_transaction(self, message, next_batch):
        real_db = bus.db
        state = {"holder": None, "lock_acquired": False, "sync_calls": 0}

        def fast_test_db():
            conn = real_db()
            conn.execute("PRAGMA busy_timeout=25")
            return conn

        def locked_sync(_token, _timeout):
            state["sync_calls"] += 1
            if not state["lock_acquired"]:
                holder = sqlite3.connect(bus.DB_PATH, timeout=0, check_same_thread=False)
                holder.execute("PRAGMA busy_timeout=0")
                holder.execute("BEGIN IMMEDIATE")
                state["holder"] = holder
                state["lock_acquired"] = True
            return {
                "next_batch": next_batch,
                "rooms": {"join": {bus.ROOM: {"timeline": {"events": [message]}}}},
            }

        def release_lock(_delay):
            self.assertIsNotNone(state["holder"])
            state["holder"].rollback()
            state["holder"].close()
            state["holder"] = None

        try:
            with (
                mock.patch.object(bus, "db", side_effect=fast_test_db),
                mock.patch.object(bus, "sync", side_effect=locked_sync),
                mock.patch.object(bus, "SQLITE_LOCK_RETRY_DELAYS", (0.0,)),
                mock.patch.object(bus, "source_identity", return_value="sha256:test-source"),
                mock.patch.object(bus.time, "sleep", side_effect=release_lock) as sleep_mock,
            ):
                yield state, sleep_mock
        finally:
            if state["holder"] is not None:
                state["holder"].rollback()
                state["holder"].close()

    def members(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            bus.cmd_members(argparse.Namespace())
        return [json.loads(line) for line in output.getvalue().splitlines() if line]

    def make_heartbeat_due(self, agent_id):
        conn = bus.db()
        conn.execute("UPDATE identities SET lease_until_ms=? WHERE agent_id=?",
                     (bus.now_ms() - 1000, agent_id))
        conn.commit()
        conn.close()

    def test_unacked_message_parks_at_cap_and_revives(self):
        sender = self.join("host-a/s-tmux1", "s1")
        receiver = self.join("host-a/r-tmux2", "r1")
        with contextlib.redirect_stdout(io.StringIO()):
            bus.cmd_send(argparse.Namespace(
                sender=sender["agent_id"], target=receiver["agent_id"],
                subject="poison", body="never acked", priority="normal",
                ttl=86400))
        # Deterministic clock: with LEASE_SECONDS=0 the presentation lease
        # expires at exactly the presenting pull's millisecond, and the
        # un-present query is a strict `lease_until_ms < now`. Two pulls
        # landing in the SAME wall-clock millisecond therefore tie, the row
        # stays 'presented', and the pull yields nothing — a load-dependent
        # coin toss (observed twice under full-battery load, never in
        # isolation). Every now_ms() call advancing 1ms makes the next
        # pull's stamp strictly later than any lease set by the previous
        # one, with no wall-clock dependence at all.
        real_now_ms = bus.now_ms
        base = real_now_ms()
        ticks = iter(range(1, 1_000_000))

        def monotonic_now_ms():
            return base + next(ticks)

        with mock.patch.object(bus, "LEASE_SECONDS", 0), \
                mock.patch.object(bus, "now_ms", monotonic_now_ms):
            for n in range(bus.PRESENT_ATTEMPT_CAP):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    bus.cmd_pull(argparse.Namespace(
                        identity=receiver["agent_id"], max=10,
                        max_bytes=65536))
                self.assertIn("poison", out.getvalue(), f"pull {n}")
            # cap reached: the next pull parks it instead of presenting
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                bus.cmd_pull(argparse.Namespace(
                    identity=receiver["agent_id"], max=10, max_bytes=65536))
        self.assertNotIn("poison", out.getvalue())
        digest = json.loads(out.getvalue().strip().splitlines()[-1])
        self.assertEqual(digest["parked"], 1)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bus.cmd_unread(argparse.Namespace(identity=receiver["agent_id"],
                                              local_only=True))
        self.assertEqual(json.loads(out.getvalue())["parked"], 1)
        # revive re-arms exactly this message; it presents and acks normally
        with contextlib.redirect_stdout(io.StringIO()):
            bus.cmd_revive(argparse.Namespace(
                identity=receiver["agent_id"],
                msg_id=self.inbox_msg_id(receiver["agent_id"])))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            bus.cmd_pull(argparse.Namespace(
                identity=receiver["agent_id"], max=10, max_bytes=65536))
        self.assertIn("poison", out.getvalue())
        msg_id = self.inbox_msg_id(receiver["agent_id"])
        with contextlib.redirect_stdout(io.StringIO()):
            bus.cmd_ack(argparse.Namespace(
                identity=receiver["agent_id"], msg_id=msg_id, status="ok",
                detail=None))
        conn = bus.db()
        state = conn.execute("SELECT state FROM inbox WHERE msg_id=?",
                             (msg_id,)).fetchone()["state"]
        self.assertEqual(state, "done")

    def inbox_msg_id(self, agent_id):
        conn = bus.db()
        return conn.execute("SELECT msg_id FROM inbox WHERE agent_id=?",
                            (agent_id,)).fetchone()["msg_id"]

    def test_heartbeat_failures_flag_member_and_recover(self):
        joined = self.join("host-b/hb-tmux1", "host-b/hb")
        agent_id = joined["agent_id"]
        stderr = io.StringIO()
        with mock.patch.object(bus, "put_state", side_effect=RuntimeError("transport down")), \
                contextlib.redirect_stderr(stderr):
            for _ in range(bus.HEARTBEAT_FAIL_FLAG_THRESHOLD):
                self.make_heartbeat_due(agent_id)
                with self.assertRaises(RuntimeError):
                    bus.cmd_heartbeat(argparse.Namespace(identity=agent_id))
        row = next(m for m in self.members() if m["agent_id"] == agent_id)
        self.assertEqual(row["heartbeat_failing"], bus.HEARTBEAT_FAIL_FLAG_THRESHOLD)
        self.assertIn("transport down", row["heartbeat_last_error"])
        self.assertIn("DEGRADED", stderr.getvalue())
        self.assertIn("NOT proof", stderr.getvalue())
        conn = bus.db()
        status = bus.unread_status(conn, agent_id)
        conn.close()
        self.assertTrue(status["registry_heartbeat"]["failing"])
        self.assertEqual(status["registry_heartbeat"]["consecutive_failures"],
                         bus.HEARTBEAT_FAIL_FLAG_THRESHOLD)
        # one successful due write clears the flag everywhere
        self.make_heartbeat_due(agent_id)
        bus.cmd_heartbeat(argparse.Namespace(identity=agent_id))
        row = next(m for m in self.members() if m["agent_id"] == agent_id)
        self.assertNotIn("heartbeat_failing", row)
        conn = bus.db()
        status = bus.unread_status(conn, agent_id)
        conn.close()
        self.assertNotIn("registry_heartbeat", status)

    def test_healthy_throttled_watch_seat_is_not_flagged(self):
        joined = self.join("host-b/fresh-tmux1", "host-b/fresh")
        agent_id = joined["agent_id"]
        # A healthy watch seat's updated_at legitimately ages to just under
        # lease/2 (the write throttle) — it must NOT read as overdue.
        self.states[agent_id]["updated_at"] = bus.iso(
            bus.now_ms() - (bus.MEMBER_LEASE_SECONDS // 2 - 60) * 1000)
        row = next(m for m in self.members() if m["agent_id"] == agent_id)
        self.assertFalse(row["heartbeat_overdue"])
        self.assertNotIn("heartbeat_failing", row)
        self.assertTrue(row["liveness"].startswith("unverified"))

    def test_watch_seat_past_throttle_plus_margin_is_overdue(self):
        joined = self.join("host-b/stale-tmux1", "host-b/stale")
        agent_id = joined["agent_id"]
        self.states[agent_id]["updated_at"] = bus.iso(
            bus.now_ms() - (bus.MEMBER_LEASE_SECONDS // 2
                            + bus.HEARTBEAT_OVERDUE_MARGIN_SECONDS + 3600) * 1000)
        row = next(m for m in self.members() if m["agent_id"] == agent_id)
        self.assertTrue(row["heartbeat_overdue"])

    def test_pull_seat_age_is_never_judged(self):
        joined = self.join("host-b/pull-tmux1", "host-b/pull", mode="pull")
        agent_id = joined["agent_id"]
        self.states[agent_id]["updated_at"] = bus.iso(
            bus.now_ms() - 30 * 24 * 3600 * 1000)
        row = next(m for m in self.members() if m["agent_id"] == agent_id)
        self.assertIsNone(row["heartbeat_overdue"])

    def test_join_rename_preserves_agent_id_without_model_message(self):
        first = self.join("host-b/task-tmux1", "host-b/task")
        second = self.join("host-b/task-tmux9", "host-b/task")
        self.assertEqual(first["agent_id"], second["agent_id"])
        state = self.states[first["agent_id"]]
        self.assertEqual(state["generation"], 2)
        self.assertEqual(state["handle"], "host-b/task-tmux9")
        self.assertIn("host-b/task-tmux1", state["aliases"])
        self.assertEqual(self.timeline, [])

    def test_unique_short_segment_resolves_in_live_registry(self):
        receiver = self.join("host-b/worker-7-tmux7", "host-b/worker-7")
        found = bus.resolve_target("worker-7", "sender")
        self.assertEqual([m["agent_id"] for m in found],
                         [receiver["agent_id"]])

    def test_exact_alias_wins_before_another_members_segment(self):
        exact = self.join("host-b/exact-tmux7", "host-b/exact")
        self.states[exact["agent_id"]]["aliases"].append("worker-7")
        self.join("host-a/worker-7-tmux8", "host-a/segment")
        found = bus.resolve_target("worker-7", "sender")
        self.assertEqual([m["agent_id"] for m in found], [exact["agent_id"]])

    def test_ambiguous_short_segment_is_refused(self):
        self.join("host-b/worker-7-tmux7", "host-b/worker-7-a")
        self.join("host-a/worker-7-tmux8", "host-a/worker-7-b")
        with self.assertRaises(SystemExit):
            bus.resolve_target("worker-7", "sender")

    def test_empty_target_never_resolves_to_the_only_member(self):
        self.join("host-b/only-tmux7", "host-b/only")
        with self.assertRaises(SystemExit):
            bus.resolve_target("", "sender")

    def test_retire_overwrites_same_registry_state_key(self):
        joined = self.join("host-b/task-tmux1", "host-b/task")
        with contextlib.redirect_stdout(io.StringIO()):
            bus.cmd_retire(argparse.Namespace(identity=joined["agent_id"]))
        state = self.states[joined["agent_id"]]
        self.assertEqual(state["agent_id"], joined["agent_id"])
        self.assertEqual(state["status"], "retired")
        self.assertIsNone(state["lease_until"])
        self.assertEqual(self.timeline, [])

    def test_join_result_reports_committed_identity_metadata(self):
        result = self.join("host-b/task-tmux1", "host-b/task", mode="pull")
        self.assertEqual(result["schema"], "agent-bus/join-result/v3")
        self.assertEqual(result["handle"], "host-b/task-tmux1")
        self.assertEqual(result["slot"], "host-b/task")
        self.assertEqual(result["harness"], "test")
        self.assertEqual(result["mode"], "pull")
        self.assertEqual(result["host"], "host")
        self.assertEqual(result["tmux"], "tmux=0:1.0")
        self.assertEqual(result["status"], "active")

    def test_join_rejects_unwakeable_named_harness_metadata(self):
        cases = [
            ("unknown", "pull", "tmux=0:1.0 win=codex"),
            ("codex", "watch", "tmux=0:1.0 win=codex"),
            ("codex", "pull", "no-tmux"),
            ("claude", "pull", "tmux=0:1.0 win=claude"),
            ("claude", "watch", "no-tmux"),
            ("opencode", "pull", "tmux=0:1.0 win=opencode"),
            ("opencode", "watch", "opencode"),
        ]
        for harness, mode, tmux in cases:
            with self.subTest(harness=harness, mode=mode, tmux=tmux):
                args = argparse.Namespace(
                    handle=f"host-b/{harness}-tmux1", slot=f"host-b/{harness}",
                    harness=harness, mode=mode, host="host", tmux=tmux,
                )
                with self.assertRaises(SystemExit):
                    bus.cmd_join(args)

    def test_opencode_watch_accepts_concrete_tmux_metadata(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            bus.cmd_join(argparse.Namespace(
                handle="host-a/opencode-tmux4",
                slot="opencode:/workspace",
                harness="opencode",
                mode="watch",
                host="dev-host",
                tmux="tmux=4:4.0 win=opencode",
            ))
        result = json.loads(output.getvalue())
        self.assertEqual(result["host"], "dev-host")
        self.assertEqual(result["tmux"], "tmux=4:4.0 win=opencode")

    def test_exact_send_delivery_processed_ack(self):
        sender = self.join("host-b/sender-tmux1", "host-b/sender")
        receiver = self.join("host-b/receiver-tmux2", "host-b/receiver")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            bus.cmd_send(argparse.Namespace(sender=sender["agent_id"], target="host-b/receiver-tmux2", subject="Review", body="Check commit abc", priority="high", ttl=3600))
        sent = json.loads(output.getvalue())
        self.assertEqual(sent["transport_state"], "accepted")
        self.assertEqual(sent["recipients"], 1)
        self.assertEqual(sent["recipient_agent_ids"], [receiver["agent_id"]])

        # Sender's receive cursor skips its own message; receiver persists it and emits delivered ACK.
        message = self.timeline.pop(0)
        self.timeline = [message]
        self.assertEqual(bus.ingest(receiver["agent_id"], 0), 1)
        ack_event = self.timeline.pop(0)
        self.assertEqual(ack_event["content"]["stage"], "delivered")

        self.timeline = [ack_event]
        bus.ingest(sender["agent_id"], 0)
        conn = bus.db()
        row = conn.execute("SELECT * FROM outbox_recipients WHERE msg_id=?", (sent["msg_id"],)).fetchone()
        self.assertIsNotNone(row["delivered_ms"])
        self.assertIsNone(row["processed_ms"])

        bus.available(conn, receiver["agent_id"], limit=1, max_bytes=10000)
        with contextlib.redirect_stdout(io.StringIO()):
            bus.cmd_ack(argparse.Namespace(identity=receiver["agent_id"], msg_id=sent["msg_id"], status="ok", detail="reviewed"))
        processed_event = self.timeline.pop(0)
        self.timeline = [processed_event]
        bus.ingest(sender["agent_id"], 0)
        row = conn.execute("SELECT * FROM outbox_recipients WHERE msg_id=?", (sent["msg_id"],)).fetchone()
        self.assertEqual(row["processed_status"], "ok")

    def test_cron_sender_is_not_addressable_but_still_receives_acks(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            bus.cmd_join(argparse.Namespace(
                handle="host-a/fleet-orchestrator-cron",
                slot="host-a/fleet-orchestrator-cron",
                harness="cron", mode="pull", host="host",
                tmux="headless=cron service=fleet-orchestrator",
            ))
        service = json.loads(output.getvalue())
        receiver = self.join("host-b/receiver-tmux2", "host-b/receiver")

        with self.assertRaises(SystemExit):
            bus.resolve_target(service["handle"], receiver["agent_id"])
        with self.assertRaises(SystemExit):
            bus.resolve_target("fleet-orchestrator-cron", receiver["agent_id"])
        broadcast = bus.resolve_target("all", receiver["agent_id"])
        self.assertNotIn(
            service["agent_id"], {member["agent_id"] for member in broadcast}
        )
        members = self.members()
        self.assertNotIn(
            service["agent_id"], {member["agent_id"] for member in members}
        )

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            bus.cmd_send(argparse.Namespace(
                sender=service["agent_id"], target=receiver["agent_id"],
                subject="headless sender", body="body", priority="normal",
                ttl=3600,
            ))
        sent = json.loads(output.getvalue())
        message = self.timeline.pop(0)
        self.timeline = [message]
        self.assertEqual(bus.ingest(receiver["agent_id"], 0), 1)
        ack_event = self.timeline.pop(0)
        self.timeline = [ack_event]

        dispatch = io.StringIO()
        with contextlib.redirect_stdout(dispatch):
            bus.cmd_dispatch(argparse.Namespace(
                once=True, interval=0, host="host"
            ))
        self.assertEqual(json.loads(dispatch.getvalue())["agents"], 1)
        with contextlib.closing(bus.db()) as conn:
            row = conn.execute(
                "SELECT delivered_ms FROM outbox_recipients WHERE msg_id=?",
                (sent["msg_id"],),
            ).fetchone()
            self.assertIsNotNone(row["delivered_ms"])

    def test_cron_sender_rejects_legacy_direct_message_at_ingest(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            bus.cmd_join(argparse.Namespace(
                handle="host-a/fleet-orchestrator-cron",
                slot="host-a/fleet-orchestrator-cron",
                harness="cron", mode="pull", host="host",
                tmux="headless=cron service=fleet-orchestrator",
            ))
        service = json.loads(output.getvalue())
        conn = bus.db()
        agent = bus.identity(conn, service["agent_id"])
        event = {
            "event_id": "$legacy-cron-target",
            "type": bus.MESSAGE_TYPE,
            "origin_server_ts": bus.now_ms(),
            "content": {
                "schema": "agent-bus/message/v3",
                "msg_id": "legacy-cron-target",
                "to": [{"agent_id": service["agent_id"]}],
                "from": {"agent_id": "old-sender", "handle": "old/sender"},
                "subject": "old queued message",
                "body": "must not become permanent unread mail",
                "priority": "normal",
                "created_at": bus.iso(bus.now_ms()),
            },
        }
        self.assertEqual(bus.ingest_event(conn, agent, event), 0)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM inbox WHERE agent_id=?",
            (service["agent_id"],),
        ).fetchone()[0], 0)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM ack_outbox WHERE from_agent_id=?"
            " AND stage='delivered'",
            (service["agent_id"],),
        ).fetchone()[0], 0)
        conn.close()

    def test_upgrade_deletes_legacy_cron_mail_without_rewinding_signal(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            bus.cmd_join(argparse.Namespace(
                handle="host-a/fleet-orchestrator-cron",
                slot="host-a/fleet-orchestrator-cron",
                harness="cron", mode="pull", host="host",
                tmux="headless=cron service=fleet-orchestrator",
            ))
        service = json.loads(output.getvalue())
        conn = bus.db()
        with conn:
            conn.execute(
                "INSERT INTO inbox (agent_id,msg_id,matrix_event_id,"
                " sender_agent_id,sender_handle,subject,body,priority,"
                " created_ms,expires_ms,state,lease_until_ms,attempts)"
                " VALUES (?,?,?,'old-sender','old/sender','old','body',"
                " 'normal',?,NULL,'available',NULL,0)",
                (service["agent_id"], "legacy-unread", "$legacy", bus.now_ms()),
            )
            conn.execute(
                "INSERT INTO ack_outbox"
                " (msg_id,from_agent_id,to_agent_id,stage,status,detail,created_ms)"
                " VALUES (?,?,'old-sender','delivered','ok',NULL,?)",
                ("legacy-unread", service["agent_id"], bus.now_ms()),
            )
            conn.execute(
                "UPDATE inbox_signal SET generation=7,notified_generation=2"
                " WHERE agent_id=?", (service["agent_id"],),
            )
        conn.close()

        reopened = bus.db()
        self.assertEqual(reopened.execute(
            "SELECT COUNT(*) FROM inbox WHERE agent_id=? AND state!='done'",
            (service["agent_id"],),
        ).fetchone()[0], 0)
        self.assertEqual(reopened.execute(
            "SELECT COUNT(*) FROM ack_outbox WHERE from_agent_id=?"
            " AND stage='delivered' AND matrix_event_id IS NULL",
            (service["agent_id"],),
        ).fetchone()[0], 0)
        signal = reopened.execute(
            "SELECT generation,notified_generation FROM inbox_signal"
            " WHERE agent_id=?", (service["agent_id"],),
        ).fetchone()
        self.assertEqual(tuple(signal), (7, 2))
        reopened.close()

    def test_pull_bounds_context_without_dropping_remainder(self):
        sender = self.join("host-b/sender-tmux1", "host-b/sender")
        receiver = self.join("host-b/receiver-tmux2", "host-b/receiver", mode="pull")
        for index in range(4):
            with contextlib.redirect_stdout(io.StringIO()):
                bus.cmd_send(argparse.Namespace(sender=sender["agent_id"], target=receiver["agent_id"], subject=f"s{index}", body="x" * 10, priority="normal", ttl=3600))
        bus.ingest(receiver["agent_id"], 0)
        conn = bus.db()
        rows, digest = bus.available(conn, receiver["agent_id"], limit=2, max_bytes=10000)
        self.assertEqual(len(rows), 2)
        self.assertEqual(digest["remaining"], 2)
        states = dict(conn.execute("SELECT state,count(*) FROM inbox GROUP BY state").fetchall())
        self.assertEqual(states, {"available": 2, "presented": 2})

    def test_unread_does_not_present_message_and_increments_generation(self):
        sender = self.join("host-b/sender-tmux1", "host-b/sender")
        receiver = self.join("host-b/receiver-tmux2", "host-b/receiver", mode="pull")
        with contextlib.redirect_stdout(io.StringIO()):
            bus.cmd_send(argparse.Namespace(sender=sender["agent_id"], target=receiver["agent_id"], subject="wake", body="untrusted `body`", priority="urgent", ttl=3600))
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            bus.cmd_unread(argparse.Namespace(identity=receiver["agent_id"], local_only=False))
        status = json.loads(output.getvalue())
        self.assertEqual(status["count"], 1)
        self.assertEqual(status["urgent"], 1)
        self.assertEqual(status["generation"], 1)
        conn = bus.db()
        self.assertEqual(conn.execute("SELECT state FROM inbox").fetchone()[0], "available")

    def test_notify_claim_is_exact_atomic_and_body_free(self):
        sender = self.join("host-b/sender-tmux1", "host-b/sender")
        receiver = self.join("host-b/receiver-tmux2", "host-b/receiver", mode="pull")
        conn = bus.db()
        conn.execute("UPDATE identities SET harness='codex',host='host',tmux='tmux=0:2.0 win=codex' WHERE agent_id=?", (receiver["agent_id"],))
        conn.commit()
        with contextlib.redirect_stdout(io.StringIO()):
            bus.cmd_send(argparse.Namespace(sender=sender["agent_id"], target=receiver["agent_id"], subject="evil", body="$(touch /tmp/pwned)", priority="normal", ttl=3600))
        bus.ingest(receiver["agent_id"], 0)
        first = io.StringIO(); second = io.StringIO()
        with contextlib.redirect_stdout(first):
            with mock.patch.object(bus, "ingest", return_value=0):
                bus.cmd_notify_claim(argparse.Namespace(host="host", pane="0:2.0"))
        with contextlib.redirect_stdout(second):
            with mock.patch.object(bus, "ingest", return_value=0):
                bus.cmd_notify_claim(argparse.Namespace(host="host", pane="0:2.0"))
        self.assertTrue(json.loads(first.getvalue())["notify"])
        self.assertFalse(json.loads(second.getvalue())["notify"])
        self.assertNotIn("evil", first.getvalue())
        self.assertNotIn("touch", first.getvalue())

    def test_notify_does_not_wake_for_already_presented_message(self):
        sender = self.join("host-b/sender-tmux1", "host-b/sender")
        receiver = self.join("host-b/receiver-tmux2", "host-b/receiver", mode="pull")
        conn = bus.db()
        conn.execute("UPDATE identities SET harness='codex',host='host',tmux='tmux=0:2.0 win=codex' WHERE agent_id=?", (receiver["agent_id"],))
        conn.commit()
        with contextlib.redirect_stdout(io.StringIO()):
            bus.cmd_send(argparse.Namespace(sender=sender["agent_id"], target=receiver["agent_id"], subject="seen", body="body", priority="normal", ttl=3600))
        bus.ingest(receiver["agent_id"], 0)
        bus.available(conn, receiver["agent_id"], 1, 10000)
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.object(bus, "ingest", return_value=0):
            bus.cmd_notify_claim(argparse.Namespace(host="host", pane="0:2.0"))
        self.assertFalse(json.loads(output.getvalue())["notify"])

    def test_legacy_matrix_notify_keeps_existing_exact_location_binding(self):
        sender = self.join("host-b/sender-tmux1", "host-b/sender")
        receiver = self.join("host-b/receiver-tmux2", "host-b/receiver", mode="pull")
        conn = bus.db()
        conn.execute(
            "UPDATE identities SET harness='codex',tmux_server_id=NULL WHERE agent_id=?",
            (receiver["agent_id"],),
        )
        conn.commit()
        with contextlib.redirect_stdout(io.StringIO()):
            bus.cmd_send(argparse.Namespace(
                sender=sender["agent_id"], target=receiver["agent_id"], subject="legacy",
                body="synthetic compatibility message", priority="normal", ttl=3600,
            ))
        bus.ingest(receiver["agent_id"], 0)
        wrong = io.StringIO()
        right = io.StringIO()
        with mock.patch.object(bus.tmux_runtime, "pane_snapshot", side_effect=AssertionError("legacy must remain unchanged")):
            with contextlib.redirect_stdout(wrong):
                bus.cmd_notify_claim(argparse.Namespace(host="host", pane="0:7.0"))
            with contextlib.redirect_stdout(right), mock.patch.object(bus, "ingest", return_value=0):
                bus.cmd_notify_claim(argparse.Namespace(host="host", pane="0:2.0"))
        self.assertFalse(json.loads(wrong.getvalue())["notify"])
        self.assertTrue(json.loads(right.getvalue())["notify"])

    def test_limited_timeline_preserves_cursor(self):
        agent = self.join("host-b/a-tmux1", "host-b/a")
        conn = bus.db()
        sender = self.join("host-b/s-tmux2", "host-b/s")
        content = {"schema": "agent-bus/message/v3", "msg_id": "gap-msg", "from": {"agent_id": sender["agent_id"], "handle": "host-b/s-tmux2"}, "to": [{"agent_id": agent["agent_id"]}], "subject": "gap", "body": "recovered", "priority": "normal", "created_at": bus.iso(), "expires_at": bus.iso(bus.now_ms() + 100000)}
        with mock.patch.object(bus, "sync", return_value={"next_batch": "later", "rooms": {"join": {bus.ROOM: {"timeline": {"limited": True, "events": []}}}}}):
            with mock.patch.object(bus, "gap_events", return_value=[{"event_id": "$gap", "type": bus.MESSAGE_TYPE, "content": content}]):
                self.assertEqual(bus.ingest(agent["agent_id"], 0), 1)
        after = conn.execute("SELECT token FROM cursors WHERE agent_id=?", (agent["agent_id"],)).fetchone()[0]
        self.assertEqual("later", after)

    def test_failed_send_retries_same_message(self):
        sender = self.join("host-b/sender-tmux1", "host-b/sender")
        receiver = self.join("host-b/receiver-tmux2", "host-b/receiver")
        original = bus.put_event.side_effect
        bus.put_event.side_effect = RuntimeError("timeout")
        with self.assertRaises(SystemExit):
            bus.cmd_send(argparse.Namespace(sender=sender["agent_id"], target=receiver["agent_id"], subject="retry", body="body", priority="normal", ttl=3600))
        conn = bus.db()
        row = conn.execute("SELECT * FROM outbox").fetchone()
        self.assertEqual(row["transport_state"], "pending_retry")
        msg_id = row["msg_id"]
        bus.put_event.side_effect = original
        with contextlib.redirect_stdout(io.StringIO()):
            bus.cmd_retry(argparse.Namespace(sender=sender["agent_id"]))
        row = conn.execute("SELECT * FROM outbox WHERE msg_id=?", (msg_id,)).fetchone()
        self.assertEqual(row["transport_state"], "accepted")

    def test_pull_lease_is_long_lived_and_refreshes(self):
        agent = self.join("host-b/pull-tmux1", "host-b/pull", mode="pull")
        conn = bus.db()
        row = bus.identity(conn, agent["agent_id"])
        self.assertGreater(row["lease_until_ms"] - bus.now_ms(), 86400 * 1000)

    def test_heartbeat_renews_state_only_after_half_lease(self):
        agent = self.join("host-b/pull-tmux1", "host-b/pull", mode="pull")
        bus.put_state.reset_mock()
        bus.cmd_heartbeat(argparse.Namespace(identity=agent["agent_id"]))
        bus.put_state.assert_not_called()

        conn = bus.db()
        conn.execute(
            "UPDATE identities SET lease_until_ms=? WHERE agent_id=?",
            (bus.now_ms() - 1, agent["agent_id"]),
        )
        conn.commit()
        bus.cmd_heartbeat(argparse.Namespace(identity=agent["agent_id"]))
        bus.put_state.assert_called_once()
        row = bus.identity(conn, agent["agent_id"])
        self.assertGreater(row["lease_until_ms"] - bus.now_ms(), 86400 * 1000)

    def test_send_renews_existing_expired_sender_for_dispatch(self):
        sender = self.join("host-b/sender-tmux1", "host-b/sender", mode="pull")
        receiver = self.join("host-b/receiver-tmux2", "host-b/receiver")
        conn = bus.db()
        original_count = conn.execute("SELECT COUNT(*) FROM identities").fetchone()[0]
        conn.execute(
            "UPDATE identities SET lease_until_ms=? WHERE agent_id=?",
            (bus.now_ms() - 1, sender["agent_id"]),
        )
        conn.commit()
        conn.close()

        bus.put_state.reset_mock()
        with contextlib.redirect_stdout(io.StringIO()):
            bus.cmd_send(argparse.Namespace(
                sender=sender["agent_id"], target=receiver["agent_id"],
                subject="renew sender", body="body", priority="normal",
                ttl=3600,
            ))

        with contextlib.closing(bus.db()) as conn:
            row = bus.identity(conn, sender["agent_id"])
            self.assertGreater(row["lease_until_ms"], bus.now_ms())
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM identities").fetchone()[0],
                original_count,
            )
        bus.put_state.assert_called_once()

        output = io.StringIO()
        with mock.patch.object(bus, "ingest", return_value=0) as ingest, \
                contextlib.redirect_stdout(output):
            bus.cmd_dispatch(argparse.Namespace(once=True, interval=0, host="host"))
        self.assertEqual(json.loads(output.getvalue())["agents"], 1)
        ingest.assert_called_once_with(
            sender["agent_id"], 0, retry_label="dispatch"
        )

    def test_send_never_revives_or_recreates_sender(self):
        receiver = self.join("host-b/receiver-tmux2", "host-b/receiver")
        retired = self.join("host-b/retired-tmux1", "host-b/retired", mode="pull")
        with contextlib.redirect_stdout(io.StringIO()):
            bus.cmd_retire(argparse.Namespace(
                identity=retired["agent_id"], kind="manual"
            ))
        with contextlib.closing(bus.db()) as conn:
            original_count = conn.execute("SELECT COUNT(*) FROM identities").fetchone()[0]

        for sender in (retired["agent_id"], "missing-sender"):
            with self.subTest(sender=sender), self.assertRaises(SystemExit):
                bus.cmd_send(argparse.Namespace(
                    sender=sender, target=receiver["agent_id"],
                    subject="must fail", body="body", priority="normal",
                    ttl=3600,
                ))
        with contextlib.closing(bus.db()) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM identities").fetchone()[0],
                original_count,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0
            )

    def test_registry_refresh_failure_does_not_block_message_room(self):
        sender = self.join("host-b/sender-tmux1", "host-b/sender", mode="pull")
        receiver = self.join("host-b/receiver-tmux2", "host-b/receiver")
        self.make_heartbeat_due(sender["agent_id"])
        with contextlib.closing(bus.db()) as conn:
            original_count = conn.execute("SELECT COUNT(*) FROM identities").fetchone()[0]

        stderr = io.StringIO()
        with mock.patch.object(
                bus, "put_state", side_effect=RuntimeError("registry unavailable")
        ), contextlib.redirect_stderr(stderr), \
                contextlib.redirect_stdout(io.StringIO()):
            bus.cmd_send(argparse.Namespace(
                sender=sender["agent_id"], target=receiver["agent_id"],
                subject="message room still works", body="body",
                priority="normal", ttl=3600,
            ))

        with contextlib.closing(bus.db()) as conn:
            row = bus.identity(conn, sender["agent_id"])
            self.assertLess(row["lease_until_ms"], bus.now_ms())
            self.assertEqual(row["heartbeat_fails"], 1)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM identities").fetchone()[0],
                original_count,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1
            )
        self.assertIn("sender registry refresh pending", stderr.getvalue())

    def test_deadline_aborts_a_stalled_call(self):
        # urllib's socket timeout is per-operation, so a half-open connection can
        # hang a sync long-poll far past its intended timeout. The watch loop runs
        # its 120s heartbeat inline before the sync, so a hung sync starves the
        # heartbeat and the member lease expires while the watcher is still up.
        # call_with_deadline must bound the caller's wall-clock even if the callee
        # never returns.
        import threading as _th, time as _t
        release = _th.Event()
        start = _t.monotonic()
        try:
            with self.assertRaises(RuntimeError):
                bus.call_with_deadline(lambda: release.wait(30), 0.3)
            self.assertLess(_t.monotonic() - start, 2.0)
        finally:
            release.set()

    def test_deadline_returns_fast_result(self):
        self.assertEqual(bus.call_with_deadline(lambda: 42, 5), 42)

    def test_deadline_propagates_callee_error(self):
        def boom():
            raise ValueError("callee failed")
        with self.assertRaises(ValueError):
            bus.call_with_deadline(boom, 5)

    def test_failed_heartbeat_rolls_back_lease_and_retries(self):
        agent = self.join("host-b/pull-tmux1", "host-b/pull", mode="pull")
        conn = bus.db()
        old_updated = bus.now_ms() - 10_000
        old_lease = bus.now_ms() - 1
        conn.execute(
            "UPDATE identities SET updated_ms=?,lease_until_ms=? WHERE agent_id=?",
            (old_updated, old_lease, agent["agent_id"]),
        )
        conn.commit()

        bus.put_state.side_effect = RuntimeError("Matrix request uncertain: timeout")
        with self.assertRaises(RuntimeError):
            bus.cmd_heartbeat(argparse.Namespace(identity=agent["agent_id"]))
        row = bus.identity(conn, agent["agent_id"])
        self.assertEqual((row["updated_ms"], row["lease_until_ms"]), (old_updated, old_lease))

        def recovered_state(current_id, content):
            self.states[current_id] = content
            return f"$state-{current_id}"

        bus.put_state.side_effect = recovered_state
        bus.cmd_heartbeat(argparse.Namespace(identity=agent["agent_id"]))
        row = bus.identity(conn, agent["agent_id"])
        self.assertGreater(row["lease_until_ms"], old_lease)

    def test_heartbeat_network_does_not_hold_sqlite_write_lock(self):
        agent = self.join("host-b/pull-tmux1", "host-b/pull", mode="pull")
        conn = bus.db()
        conn.execute(
            "UPDATE identities SET lease_until_ms=? WHERE agent_id=?",
            (bus.now_ms() - 1, agent["agent_id"]),
        )
        conn.commit()

        def concurrent_state(current_id, content):
            other = bus.db()
            other.execute(
                "UPDATE identities SET host='concurrent-writer' WHERE agent_id=?",
                (current_id,),
            )
            other.commit()
            other.close()
            self.states[current_id] = content
            return f"$state-{current_id}"

        bus.put_state.side_effect = concurrent_state
        bus.cmd_heartbeat(argparse.Namespace(identity=agent["agent_id"]))
        row = bus.identity(conn, agent["agent_id"])
        self.assertEqual(row["host"], "concurrent-writer")

    def test_registry_migration_is_explicit_filtered_and_idempotent(self):
        active = self.join("host-b/active-tmux1", "host-b/active")
        retired = self.join("host-b/retired-tmux2", "host-b/retired")
        expired = self.join("host-b/expired-tmux3", "host-b/expired")
        with contextlib.redirect_stdout(io.StringIO()):
            bus.cmd_retire(argparse.Namespace(identity=retired["agent_id"]))
        conn = bus.db()
        conn.execute(
            "UPDATE identities SET lease_until_ms=? WHERE agent_id=?",
            (bus.now_ms() - 1, expired["agent_id"]),
        )
        conn.commit()

        self.states.clear()
        self.timeline.clear()
        bus.put_state.reset_mock()
        first = io.StringIO()
        with contextlib.redirect_stdout(first):
            bus.cmd_registry_migrate(argparse.Namespace(legacy_timeline=False))
        self.assertEqual(set(self.states), {active["agent_id"]})
        self.assertEqual(self.timeline, [])
        self.assertEqual(json.loads(first.getvalue())["published"], 1)

        with contextlib.redirect_stdout(io.StringIO()):
            bus.cmd_registry_migrate(argparse.Namespace(legacy_timeline=False))
        self.assertEqual(set(self.states), {active["agent_id"]})
        self.assertEqual(self.timeline, [])

        with contextlib.redirect_stdout(io.StringIO()):
            bus.cmd_registry_migrate(argparse.Namespace(legacy_timeline=True))
        self.assertEqual(len(self.timeline), 1)
        self.assertEqual(self.timeline[0]["type"], bus.AGENT_TYPE)

    def test_join_state_failure_can_retry_same_slot_and_agent_id(self):
        bus.put_state.side_effect = RuntimeError("Matrix HTTP 403: M_FORBIDDEN")
        with self.assertRaises(RuntimeError):
            self.join("host-b/task-tmux1", "host-b/task")
        conn = bus.db()
        agent_id = conn.execute(
            "SELECT agent_id FROM identities WHERE slot='host-b/task'"
        ).fetchone()[0]

        def recovered_state(current_id, content):
            self.states[current_id] = content
            return f"$state-{current_id}"

        bus.put_state.side_effect = recovered_state
        result = self.join("host-b/task-tmux1", "host-b/task")
        self.assertEqual(result["agent_id"], agent_id)
        self.assertIn(agent_id, self.states)

    def test_processed_ack_is_terminal(self):
        sender = self.join("host-b/sender-tmux1", "host-b/sender")
        receiver = self.join("host-b/receiver-tmux2", "host-b/receiver")
        with contextlib.redirect_stdout(io.StringIO()) as output:
            bus.cmd_send(argparse.Namespace(sender=sender["agent_id"], target=receiver["agent_id"], subject="ack", body="body", priority="normal", ttl=3600))
        msg_id = json.loads(output.getvalue())["msg_id"]
        bus.ingest(receiver["agent_id"], 0)
        conn = bus.db(); bus.available(conn, receiver["agent_id"], 1, 10000)
        with contextlib.redirect_stdout(io.StringIO()):
            bus.cmd_ack(argparse.Namespace(identity=receiver["agent_id"], msg_id=msg_id, status="ok", detail=None))
        with self.assertRaises(SystemExit):
            bus.cmd_ack(argparse.Namespace(identity=receiver["agent_id"], msg_id=msg_id, status="failed", detail=None))

    def test_ingest_closes_its_sqlite_connection(self):
        agent = self.join("host-b/pull-tmux1", "host-b/pull", mode="pull")
        real_db = bus.db
        connections = []

        def tracked_db():
            connection = real_db()
            connections.append(connection)
            return connection

        with mock.patch.object(bus, "db", side_effect=tracked_db):
            bus.ingest(agent["agent_id"], 0)
        with self.assertRaises(bus.sqlite3.ProgrammingError):
            connections[-1].execute("SELECT 1")

    def test_watch_retries_real_write_lock_and_ingests_exactly_once(self):
        sender = self.join("host-b/sender-tmux1", "host-b/sender")
        receiver = self.join("host-b/receiver-tmux2", "host-b/receiver")
        with contextlib.redirect_stdout(io.StringIO()):
            bus.cmd_send(argparse.Namespace(
                sender=sender["agent_id"], target=receiver["agent_id"],
                subject="locked", body="deliver once", priority="normal", ttl=3600,
            ))
        message = self.timeline.pop(0)

        real_ingest = bus.ingest
        completed = False

        class WatchComplete(Exception):
            pass

        def ingest_until_complete(*args, **kwargs):
            nonlocal completed
            if completed:
                raise WatchComplete
            count = real_ingest(*args, **kwargs)
            completed = count == 1
            return count

        stdout = io.StringIO()
        stderr = io.StringIO()
        with self.lock_ingest_transaction(message, "after-lock") as (state, sleep_mock):
            with (
                mock.patch.object(bus, "ingest", side_effect=ingest_until_complete),
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                with self.assertRaises(WatchComplete):
                    bus.cmd_watch(argparse.Namespace(identity=receiver["agent_id"]))

        self.assertEqual(state["sync_calls"], 1)
        self.assertEqual(sleep_mock.call_count, 1)
        self.assertIn("watch ingest transaction sqlite lock retry 1/1", stderr.getvalue())
        changed = [line for line in stdout.getvalue().splitlines() if "inbox-changed" in line]
        self.assertEqual(len(changed), 1)
        self.assertEqual(json.loads(changed[0])["count"], 1)

        with contextlib.closing(bus.db()) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM inbox WHERE agent_id=? AND msg_id=?",
                (receiver["agent_id"], message["content"]["msg_id"]),
            ).fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT generation FROM inbox_signal WHERE agent_id=?",
                (receiver["agent_id"],),
            ).fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM ack_outbox WHERE from_agent_id=? AND stage='delivered'",
                (receiver["agent_id"],),
            ).fetchone()[0], 1)
            self.assertEqual(conn.execute(
                "SELECT token FROM cursors WHERE agent_id=?",
                (receiver["agent_id"],),
            ).fetchone()[0], "after-lock")

    def test_dispatch_retries_real_ingest_lock(self):
        sender = self.join("host-b/sender-tmux1", "host-b/sender")
        receiver = self.join("host-b/pull-tmux2", "host-b/pull", mode="pull")
        with contextlib.redirect_stdout(io.StringIO()):
            bus.cmd_send(argparse.Namespace(
                sender=sender["agent_id"], target=receiver["agent_id"],
                subject="dispatch lock", body="deliver once", priority="normal", ttl=3600,
            ))
        message = self.timeline.pop(0)

        stdout = io.StringIO()
        stderr = io.StringIO()
        with self.lock_ingest_transaction(message, "dispatch-after-lock"):
            with (
                contextlib.redirect_stdout(stdout),
                contextlib.redirect_stderr(stderr),
            ):
                bus.cmd_dispatch(argparse.Namespace(once=True, interval=0, host="host"))

        result = json.loads(stdout.getvalue())
        self.assertEqual((result["agents"], result["ingested"], result["errors"]), (1, 1, 0))
        self.assertIn("dispatch ingest transaction sqlite lock retry 1/1", stderr.getvalue())
        with contextlib.closing(bus.db()) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM inbox WHERE agent_id=? AND msg_id=?",
                (receiver["agent_id"], message["content"]["msg_id"]),
            ).fetchone()[0], 1)

    def test_ack_persist_lock_exhaustion_preserves_committed_ingest_count(self):
        sender = self.join("host-b/sender-tmux1", "host-b/sender")
        receiver = self.join("host-b/receiver-tmux2", "host-b/receiver")
        with contextlib.redirect_stdout(io.StringIO()):
            bus.cmd_send(argparse.Namespace(
                sender=sender["agent_id"], target=receiver["agent_id"],
                subject="ack lock", body="still wake", priority="normal", ttl=3600,
            ))
        message = self.timeline.pop(0)
        self.timeline = [message]
        real_retry = bus.retry_sqlite_lock

        def fail_after_ack_retry_budget(label, operation):
            if label.endswith("ACK flush"):
                exc = sqlite3.OperationalError("database is locked")
                exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise exc
            return real_retry(label, operation)

        stderr = io.StringIO()
        with (
            mock.patch.object(bus, "retry_sqlite_lock", side_effect=fail_after_ack_retry_budget),
            contextlib.redirect_stderr(stderr),
        ):
            count = bus.ingest(receiver["agent_id"], 0)

        self.assertEqual(count, 1)
        self.assertIn("ACK persistence deferred", stderr.getvalue())
        with contextlib.closing(bus.db()) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM inbox WHERE agent_id=? AND msg_id=?",
                (receiver["agent_id"], message["content"]["msg_id"]),
            ).fetchone()[0], 1)
            ack = conn.execute(
                "SELECT matrix_event_id FROM ack_outbox "
                "WHERE from_agent_id=? AND stage='delivered'",
                (receiver["agent_id"],),
            ).fetchone()
            self.assertIsNotNone(ack)
            self.assertIsNone(ack["matrix_event_id"])

    def test_sqlite_lock_retry_budget_is_finite(self):
        calls = 0

        def always_locked():
            nonlocal calls
            calls += 1
            exc = sqlite3.OperationalError("database is locked")
            exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise exc

        stderr = io.StringIO()
        with (
            mock.patch.object(bus, "SQLITE_LOCK_RETRY_DELAYS", (0.0, 0.0)),
            mock.patch.object(bus.time, "sleep") as sleep_mock,
            contextlib.redirect_stderr(stderr),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                bus.retry_sqlite_lock("watch test", always_locked)
        self.assertEqual(calls, 3)
        self.assertEqual(sleep_mock.call_count, 2)
        self.assertIn("retry exhausted after 3 attempts", stderr.getvalue())

    def test_failed_database_initialization_closes_before_retry(self):
        conn = mock.Mock()
        exc = sqlite3.OperationalError("database is locked")
        exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
        with (
            mock.patch.object(bus.sqlite3, "connect", return_value=conn),
            mock.patch.object(bus, "_initialize_db", side_effect=exc),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                bus.db()
        conn.close.assert_called_once_with()

    def test_watch_and_dispatch_propagate_exhausted_lock_budget(self):
        watch = self.join("host-b/watch-tmux1", "host-b/watch")
        self.join("host-b/pull-tmux2", "host-b/pull", mode="pull")

        for surface, command in (
            ("watch", lambda: bus.cmd_watch(argparse.Namespace(identity=watch["agent_id"]))),
            ("dispatch", lambda: bus.cmd_dispatch(
                argparse.Namespace(once=True, interval=0, host="host")
            )),
        ):
            with self.subTest(surface=surface):
                exc = sqlite3.OperationalError("database is locked")
                exc.sqlite_errorcode = sqlite3.SQLITE_BUSY
                stderr = io.StringIO()
                with (
                    mock.patch.object(bus, "ingest", side_effect=exc),
                    contextlib.redirect_stderr(stderr),
                ):
                    with self.assertRaises(sqlite3.OperationalError):
                        command()
                self.assertIn(f"{surface} sqlite lock budget exhausted", stderr.getvalue())

    def test_sqlite_lock_retry_does_not_swallow_other_operational_errors(self):
        exc = sqlite3.OperationalError("no such table: broken")
        exc.sqlite_errorcode = sqlite3.SQLITE_ERROR
        with mock.patch.object(bus.time, "sleep") as sleep_mock:
            with self.assertRaises(sqlite3.OperationalError) as caught:
                bus.retry_sqlite_lock("watch test", lambda: (_ for _ in ()).throw(exc))
        self.assertIs(caught.exception, exc)
        sleep_mock.assert_not_called()

    def test_active_members_skips_retired_null_lease(self):
        rows = [
            {"handle": "host-b/live-tmux1", "status": "active", "lease_until": bus.iso(bus.now_ms() + 60_000)},
            {"handle": "host-b/retired-tmux2", "status": "retired", "lease_until": None},
            {"handle": "host-a/fleet-orchestrator-cron", "harness": "cron", "status": "active", "lease_until": bus.iso(bus.now_ms() + 60_000)},
        ]
        with mock.patch.object(bus, "room_members", return_value=rows):
            active = bus.active_members()
        self.assertEqual(
            [m["handle"] for m in active],
            ["host-b/live-tmux1", "host-a/fleet-orchestrator-cron"],
        )


class AgentBusLocalTransportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        bus.CFG = Path(self.tmp.name) / "cfg"
        bus.DB_PATH = bus.CFG / "bus.sqlite3"
        self.env_patch = mock.patch.dict(
            os.environ, {"AGENT_BUS_TRANSPORT": "local"}, clear=True
        )
        self.env_patch.start()
        self.urlopen_patch = mock.patch.object(
            bus.urllib.request,
            "urlopen",
            side_effect=AssertionError("local transport attempted HTTP"),
        )
        self.urlopen = self.urlopen_patch.start()
        self.pane = 0
        self.panes_patch = mock.patch.object(bus.tmux_runtime, "pane_snapshot", side_effect=lambda: {
            f"%{number}": {"server_id": "synthetic-server", "location": f"0:{number}.0", "dead": False}
            for number in range(1, self.pane + 1)
        })
        self.panes_patch.start()

    def tearDown(self):
        self.panes_patch.stop()
        self.urlopen_patch.stop()
        self.env_patch.stop()
        self.tmp.cleanup()

    def join(self, handle, *, mode="pull", harness="test"):
        self.pane += 1
        tmux = f"tmux=0:{self.pane}.0 win={harness}"
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.dict(os.environ, {"TMUX_PANE": f"%{self.pane}"}):
            bus.cmd_join(argparse.Namespace(
                handle=handle,
                slot=f"slot/{handle}",
                harness=harness,
                mode=mode,
                host="local-host",
                tmux=tmux,
            ))
        return json.loads(output.getvalue())

    def send(self, sender, target, subject):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            bus.cmd_send(argparse.Namespace(
                sender=sender,
                target=target,
                subject=subject,
                body=f"body for {subject}",
                priority="normal",
                ttl=3600,
            ))
        return json.loads(output.getvalue())

    def test_members_read_one_snapshot_without_initializing_the_database(self):
        joined = self.join("host/reader")
        output = io.StringIO()
        with (
            mock.patch.object(bus, "db", side_effect=AssertionError("unexpected writer")),
            mock.patch.object(bus, "local_member_rows", wraps=bus.local_member_rows) as read,
            mock.patch.object(bus.tmux_runtime, "pane_snapshot", wraps=bus.tmux_runtime.pane_snapshot) as observe,
            contextlib.redirect_stdout(output),
        ):
            bus.cmd_members(argparse.Namespace())
        self.assertEqual(read.call_count, 1)
        self.assertEqual(observe.call_count, 1)
        self.assertEqual(json.loads(output.getvalue())["agent_id"], joined["agent_id"])

    def test_derived_session_refresh_requires_matching_durable_fleet_scope(self):
        expected = {
            "NW_FLEET": "alpha", "NW_FLEET_PRIMARY_SESSION": "renamed",
            "NW_FLEET_PROFILE_APPLIED": "alpha", "AGENT_BUS_TRANSPORT": "local",
            "AGENT_BUS_DB": str(bus.DB_PATH),
        }
        original = {**expected, "NW_FLEET_PRIMARY_SESSION": "alpha"}
        with mock.patch.object(bus, "expected_fleet_environment", return_value=expected):
            with mock.patch.dict(os.environ, original, clear=True):
                bus.validate_fleet_scope()
                self.assertEqual(os.environ["NW_FLEET_PRIMARY_SESSION"], "renamed")
            with mock.patch.dict(os.environ, {**original, "AGENT_BUS_DB": "/other/bus.sqlite3"}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "not fully resolved"):
                    bus.validate_fleet_scope()
                self.assertEqual(os.environ["NW_FLEET_PRIMARY_SESSION"], "alpha")

    def test_first_named_local_join_binds_history_before_creating_the_bus(self):
        profile = mock.Mock()
        profile.resolve.return_value = {"NW_FLEET_PROFILE_PATH": ""}
        profile.bind_local_session.side_effect = lambda *_args: self.assertFalse(bus.DB_PATH.exists())
        with mock.patch.dict(os.environ, {"NW_FLEET": "example"}), \
                mock.patch.object(bus, "fleet_profile_module", return_value=profile):
            self.join("host/worker")
        profile.bind_local_session.assert_called_once()
        self.assertTrue(bus.DB_PATH.is_file())

    def test_failed_history_binding_does_not_create_registration_state(self):
        profile = mock.Mock()
        profile.resolve.return_value = {"NW_FLEET_PROFILE_PATH": ""}
        profile.bind_local_session.side_effect = RuntimeError("conflicting session identity")
        with mock.patch.dict(os.environ, {"NW_FLEET": "example"}), \
                mock.patch.object(bus, "fleet_profile_module", return_value=profile):
            with self.assertRaisesRegex(RuntimeError, "conflicting session identity"):
                self.join("host/worker")
        self.assertFalse(bus.CFG.exists())

    def test_missing_local_registry_is_empty_without_creating_directories(self):
        self.assertEqual(bus.room_members(), [])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            bus.cmd_members(argparse.Namespace())
        self.assertEqual(output.getvalue(), "")
        self.assertFalse(bus.CFG.exists())

    def test_incomplete_registry_is_unavailable_without_schema_migration(self):
        bus.DB_PATH.parent.mkdir()
        with sqlite3.connect(bus.DB_PATH) as conn:
            conn.execute("CREATE TABLE identities(agent_id TEXT)")
        before = bus.DB_PATH.read_bytes()
        with self.assertRaisesRegex(RuntimeError, "Agent Bus members unavailable"):
            bus.room_members()
        self.assertEqual(bus.DB_PATH.read_bytes(), before)

    def test_local_end_to_end_never_uses_http(self):
        sender = self.join("host/sender-tmux1")
        receiver = self.join(
            "host/receiver-tmux2", mode="pull", harness="codex"
        )
        self.assertTrue(sender["event_id"].startswith("local:state:"))

        with contextlib.closing(bus.db()) as conn:
            conn.execute(
                "UPDATE identities SET lease_until_ms=0 WHERE agent_id=?",
                (sender["agent_id"],),
            )
            conn.commit()
        bus.cmd_heartbeat(argparse.Namespace(identity=sender["agent_id"]))

        members_out = io.StringIO()
        with contextlib.redirect_stdout(members_out):
            bus.cmd_members(argparse.Namespace())
        member_ids = {
            json.loads(line)["agent_id"]
            for line in members_out.getvalue().splitlines()
        }
        self.assertEqual(member_ids, {sender["agent_id"], receiver["agent_id"]})

        registry = io.StringIO()
        with contextlib.redirect_stdout(registry):
            bus.cmd_registry_migrate(argparse.Namespace(legacy_timeline=True))
        registry_result = json.loads(registry.getvalue())
        self.assertEqual(registry_result["transport"], "local")
        self.assertIsNone(registry_result["registry_room"])
        self.assertEqual(registry_result["registered"], 2)
        self.assertEqual(registry_result["published"], 0)

        sent = self.send(sender["agent_id"], receiver["agent_id"], "local")
        self.assertEqual(sent["transport_state"], "accepted")
        self.assertTrue(sent["matrix_event_id"].startswith("local:"))
        with contextlib.closing(bus.db()) as conn:
            outbox = conn.execute(
                "SELECT transport_state FROM outbox WHERE msg_id=?",
                (sent["msg_id"],),
            ).fetchone()
            recipient = conn.execute(
                "SELECT delivered_ms,processed_ms FROM outbox_recipients"
                " WHERE msg_id=? AND recipient_agent_id=?",
                (sent["msg_id"], receiver["agent_id"]),
            ).fetchone()
            inbox = conn.execute(
                "SELECT state FROM inbox WHERE agent_id=? AND msg_id=?",
                (receiver["agent_id"], sent["msg_id"]),
            ).fetchone()
            self.assertEqual(outbox["transport_state"], "accepted")
            self.assertIsNotNone(recipient["delivered_ms"])
            self.assertIsNone(recipient["processed_ms"])
            self.assertEqual(inbox["state"], "available")
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM ack_outbox"
            ).fetchone()[0], 0)

        unread = io.StringIO()
        with contextlib.redirect_stdout(unread):
            bus.cmd_unread(argparse.Namespace(
                identity=receiver["agent_id"], local_only=False
            ))
        self.assertEqual(json.loads(unread.getvalue())["count"], 1)

        replay = io.StringIO()
        with contextlib.redirect_stdout(replay):
            bus.cmd_replay(argparse.Namespace(
                identity=receiver["agent_id"], max=10, max_bytes=32768
            ))
        self.assertIn('"subject":"local"', replay.getvalue())

        notify = io.StringIO()
        with contextlib.redirect_stdout(notify):
            bus.cmd_notify_claim(argparse.Namespace(
                host="local-host", pane="0:2.0"
            ))
        self.assertTrue(json.loads(notify.getvalue())["notify"])

        pulled = io.StringIO()
        with contextlib.redirect_stdout(pulled):
            bus.cmd_pull(argparse.Namespace(
                identity=receiver["agent_id"], max=10, max_bytes=32768
            ))
        self.assertIn(sent["msg_id"], pulled.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            bus.cmd_ack(argparse.Namespace(
                identity=receiver["agent_id"], msg_id=sent["msg_id"],
                status="ok", detail="handled locally"
            ))
        with contextlib.closing(bus.db()) as conn:
            recipient = conn.execute(
                "SELECT processed_ms,processed_status FROM outbox_recipients"
                " WHERE msg_id=? AND recipient_agent_id=?",
                (sent["msg_id"], receiver["agent_id"]),
            ).fetchone()
            self.assertIsNotNone(recipient["processed_ms"])
            self.assertEqual(recipient["processed_status"], "ok")
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM ack_outbox"
            ).fetchone()[0], 0)

        with contextlib.redirect_stdout(io.StringIO()):
            bus.cmd_retire(argparse.Namespace(
                identity=receiver["agent_id"], kind="manual"
            ))
        self.urlopen.assert_not_called()

    def test_local_watcher_uses_durable_generation_cursor(self):
        sender = self.join("host/sender-tmux1")
        receiver = self.join(
            "host/watcher-tmux2", mode="watch", harness="claude"
        )

        self.send(sender["agent_id"], receiver["agent_id"], "before-watch")
        self.assertEqual(bus.local_watch_poll(receiver["agent_id"]), 1)
        self.assertEqual(bus.local_watch_poll(receiver["agent_id"]), 0)

        self.send(sender["agent_id"], receiver["agent_id"], "while-running")
        self.assertEqual(bus.local_watch_poll(receiver["agent_id"]), 1)
        self.assertEqual(bus.local_watch_poll(receiver["agent_id"]), 0)

        self.send(sender["agent_id"], receiver["agent_id"], "before-pull")
        with contextlib.redirect_stdout(io.StringIO()):
            bus.cmd_pull(argparse.Namespace(
                identity=receiver["agent_id"], max=10, max_bytes=32768
            ))
        self.assertEqual(bus.local_watch_poll(receiver["agent_id"]), 0)
        with contextlib.closing(bus.db()) as conn:
            generation = conn.execute(
                "SELECT generation FROM inbox_signal WHERE agent_id=?",
                (receiver["agent_id"],),
            ).fetchone()[0]
            cursor = conn.execute(
                "SELECT token FROM cursors WHERE agent_id=?",
                (receiver["agent_id"],),
            ).fetchone()[0]
        self.assertEqual(cursor, f"local:{generation}")
        self.urlopen.assert_not_called()

    def test_local_watcher_idle_poll_does_not_take_a_write_transaction(self):
        receiver = self.join("host/watcher-tmux2", mode="watch", harness="claude")
        statements = []
        with contextlib.closing(bus.db()) as conn:
            conn.set_trace_callback(statements.append)
            self.assertEqual(
                bus.local_watch_poll(receiver["agent_id"], conn), 0
            )
        self.assertFalse(
            any(statement.startswith("BEGIN IMMEDIATE") for statement in statements),
            statements,
        )

    def test_concurrent_local_watcher_processes_claim_one_generation_once(self):
        sender = self.join("host/sender-tmux1")
        receiver = self.join(
            "host/watcher-tmux2", mode="watch", harness="claude"
        )
        self.send(sender["agent_id"], receiver["agent_id"], "one-generation")
        go = Path(self.tmp.name) / "go"
        worker = (
            "import importlib.util,os,time; from pathlib import Path; "
            "spec=importlib.util.spec_from_file_location('worker_bus',os.environ['SCRIPT']); "
            "bus=importlib.util.module_from_spec(spec); spec.loader.exec_module(bus); "
            "conn=bus.db(); Path(os.environ['READY']).touch(); "
            "\nwhile not Path(os.environ['GO']).exists(): time.sleep(0.001)\n"
            "print(bus.local_watch_poll(os.environ['AID'],conn)); conn.close()"
        )
        processes = []
        ready_paths = []
        for index in range(8):
            ready = Path(self.tmp.name) / f"ready-{index}"
            ready_paths.append(ready)
            env = {
                "AGENT_BUS_TRANSPORT": "local",
                "AGENT_BUS_CFG": str(bus.CFG),
                "AGENT_BUS_DB": str(bus.DB_PATH),
                "SCRIPT": str(ROOT / "scripts" / "agent-bus-v3.py"),
                "READY": str(ready),
                "GO": str(go),
                "AID": receiver["agent_id"],
                "PYTHONWARNINGS": "ignore::ResourceWarning",
            }
            processes.append(subprocess.Popen(
                [sys.executable, "-c", worker], env=env, text=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            ))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not all(
            path.exists() for path in ready_paths
        ):
            time.sleep(0.01)
        self.assertTrue(all(path.exists() for path in ready_paths))
        go.touch()
        results = [process.communicate(timeout=10) + (process.returncode,)
                   for process in processes]
        self.assertTrue(all(code == 0 for _, _, code in results), results)
        self.assertEqual(sum(int(stdout.strip()) for stdout, _, _ in results), 1)

    def test_cron_cleanup_keeps_generation_monotonic_across_role_change(self):
        receiver = self.join("host/receiver-tmux2", harness="cron")
        with contextlib.closing(bus.db()) as conn:
            conn.execute(
                "UPDATE inbox_signal SET generation=5 WHERE agent_id=?",
                (receiver["agent_id"],),
            )
            conn.execute(
                "UPDATE cursors SET token='local:5' WHERE agent_id=?",
                (receiver["agent_id"],),
            )
            conn.commit()

        receiver = self.join(
            "host/receiver-tmux2", mode="pull", harness="codex"
        )
        sender = self.join("host/sender-tmux1")
        self.send(sender["agent_id"], receiver["agent_id"], "after-role-change")

        self.assertEqual(bus.local_watch_poll(receiver["agent_id"]), 1)
        self.assertEqual(bus.local_watch_poll(receiver["agent_id"]), 0)

    def test_local_send_rolls_back_all_delivery_state_together(self):
        sender = self.join("host/sender-tmux1")
        receiver = self.join("host/receiver-tmux2")
        with contextlib.closing(bus.db()) as conn:
            conn.execute(
                "CREATE TRIGGER reject_signal BEFORE UPDATE OF generation"
                " ON inbox_signal BEGIN SELECT RAISE(ABORT,'reject signal'); END"
            )
            conn.commit()

        with self.assertRaises(sqlite3.IntegrityError):
            self.send(sender["agent_id"], receiver["agent_id"], "rollback")
        with contextlib.closing(bus.db()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM outbox_recipients").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0], 0)
        self.urlopen.assert_not_called()

    def test_local_send_does_not_mark_a_concurrently_retired_recipient_delivered(self):
        sender = self.join("host/sender-tmux1")
        receiver = self.join("host/receiver-tmux2")
        original_resolve = bus.resolve_target

        def retire_after_resolution(target, sender_id):
            recipients = original_resolve(target, sender_id)
            with contextlib.closing(bus.db()) as conn:
                conn.execute(
                    "UPDATE identities SET status='retired',lease_until_ms=NULL"
                    " WHERE agent_id=?",
                    (receiver["agent_id"],),
                )
                conn.commit()
            return recipients

        with mock.patch.object(
            bus, "resolve_target", side_effect=retire_after_resolution
        ), self.assertRaisesRegex(RuntimeError, "no longer active"):
            self.send(sender["agent_id"], receiver["agent_id"], "retired")

        with contextlib.closing(bus.db()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM outbox_recipients").fetchone()[0], 0
            )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0], 0)
        self.urlopen.assert_not_called()

    def test_local_send_does_not_accept_a_concurrently_retired_sender(self):
        sender = self.join("host/sender-tmux1")
        receiver = self.join("host/receiver-tmux2")
        original_resolve = bus.resolve_target

        def retire_sender_after_resolution(target, sender_id):
            recipients = original_resolve(target, sender_id)
            with contextlib.closing(bus.db()) as conn:
                conn.execute(
                    "UPDATE identities SET status='retired',lease_until_ms=NULL"
                    " WHERE agent_id=?",
                    (sender["agent_id"],),
                )
                conn.commit()
            return recipients

        with mock.patch.object(
            bus, "resolve_target", side_effect=retire_sender_after_resolution
        ), self.assertRaisesRegex(RuntimeError, "sender is no longer active"):
            self.send(sender["agent_id"], receiver["agent_id"], "retired-sender")

        with contextlib.closing(bus.db()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM inbox").fetchone()[0], 0)
        self.urlopen.assert_not_called()


class AgentBusRegistryStateTest(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"AGENT_BUS_TRANSPORT": "matrix"})
        self.env.start()
        self.addCleanup(self.env.stop)
        for name, value in [("ROOM", "!messages:example.test"),
                            ("REGISTRY_ROOM", "!registry:example.test")]:
            patcher = mock.patch.object(bus, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def member(self, agent_id="quiet-agent"):
        return {
            "schema": "agent-bus/agent/v3",
            "agent_id": agent_id,
            "handle": "host-b/quiet-tmux1",
            "aliases": [],
            "generation": 1,
            "status": "active",
            "harness": "codex",
            "mode": "pull",
            "host": "host",
            "tmux": "tmux=0:1.0 win=codex",
            "updated_at": bus.iso(),
            "lease_until": bus.iso(bus.now_ms() + 60_000),
        }

    def test_registry_survives_2001_intervening_timeline_events(self):
        timeline = [
            {"event_id": f"$message-{index}", "type": bus.MESSAGE_TYPE, "content": {}}
            for index in range(2001)
        ]
        quiet = self.member()
        paths = []

        def fake_matrix(method, path, payload=None, timeout=45):
            paths.append(path)
            if path == f"/_matrix/client/v3/rooms/{bus.encoded(bus.REGISTRY_ROOM)}/state":
                return [{"type": bus.AGENT_TYPE, "state_key": quiet["agent_id"], "content": quiet}]
            if "/messages" in path:
                return {"chunk": timeline}
            raise AssertionError(f"unexpected Matrix request: {method} {path}")

        with mock.patch.object(bus, "matrix", side_effect=fake_matrix):
            found = bus.resolve_target(quiet["agent_id"], "sender")
        self.assertEqual([member["agent_id"] for member in found], [quiet["agent_id"]])
        self.assertEqual(len(timeline), 2001)
        self.assertFalse(any("/messages" in path for path in paths))

    def test_registry_filters_mismatched_state_key(self):
        valid = self.member("valid")
        mismatched = self.member("content-id")
        events = [
            {"type": bus.AGENT_TYPE, "state_key": "valid", "content": valid},
            {"type": bus.AGENT_TYPE, "state_key": "different", "content": mismatched},
            {"type": "m.room.topic", "state_key": "", "content": {}},
            {"type": bus.AGENT_TYPE, "state_key": "bad", "content": None},
            "not-an-event",
        ]
        with mock.patch.object(bus, "matrix", return_value=events):
            self.assertEqual(bus.room_members(), [valid])

    def test_transport_and_registry_rooms_are_separate(self):
        calls = []

        def fake_matrix(method, path, payload=None, timeout=45):
            calls.append((method, path))
            return {"event_id": "$accepted"}

        content = self.member()
        with mock.patch.object(bus, "matrix", side_effect=fake_matrix):
            bus.put_event(bus.MESSAGE_TYPE, "txn", {"schema": "agent-bus/message/v3"})
            bus.put_state(content["agent_id"], content)
        self.assertIn(bus.encoded(bus.ROOM), calls[0][1])
        self.assertNotIn(bus.encoded(bus.REGISTRY_ROOM), calls[0][1])
        self.assertIn(bus.encoded(bus.REGISTRY_ROOM), calls[1][1])
        self.assertNotIn(f"/{bus.encoded(bus.ROOM)}/", calls[1][1])

    def test_put_state_requires_event_id(self):
        with mock.patch.object(bus, "matrix", return_value={}):
            with self.assertRaises(RuntimeError):
                bus.put_state("agent", self.member("agent"))


if __name__ == "__main__":
    unittest.main()


class ReplayVerbTest(AgentBusV3Test):
    """`replay` must recover what a truncated `pull` appeared to lose.

    The defect it exists for: `pull` leases the rows it shows, so a second
    `pull` inside the lease window returns nothing. A seat whose first pull was
    truncated (`| head`) then reads "no messages" while the sender sees
    delivered=1. Replay must preserve the leased rows while making them visible.
    """

    def send_to(self, sender, target_handle, subject, body="b"):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            bus.cmd_send(argparse.Namespace(sender=sender, target=target_handle, subject=subject,
                                            body=body, priority="normal", ttl=86400))
        return json.loads(output.getvalue())

    def run_verb(self, func, **kwargs):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            func(argparse.Namespace(**kwargs))
        return [json.loads(line) for line in output.getvalue().splitlines() if line.strip()]

    def setup_two_seats_with_messages(self, count=3):
        sender = self.join("host/sender-tmux1", "sender")["agent_id"]
        receiver = self.join("host/receiver-tmux2", "receiver")["agent_id"]
        for i in range(count):
            self.send_to(sender, "host/receiver-tmux2", f"subject-{i}")
        return sender, receiver

    def test_pull_then_pull_loses_sight_of_the_messages(self):
        """Pin the defect itself, so the fix is not tested against a fiction."""
        _, receiver = self.setup_two_seats_with_messages()
        first = [m for m in self.run_verb(bus.cmd_pull, identity=receiver, max=10, max_bytes=32768)
                 if m.get("schema", "").startswith("agent-bus/message")]
        self.assertEqual(len(first), 3)
        second = [m for m in self.run_verb(bus.cmd_pull, identity=receiver, max=10, max_bytes=32768)
                  if m.get("schema", "").startswith("agent-bus/message")]
        self.assertEqual(second, [], "pull is expected to be destructive within the lease window")

    def test_replay_recovers_after_a_truncated_pull(self):
        """The dispatch's own acceptance: pull once, output truncated, still recoverable."""
        _, receiver = self.setup_two_seats_with_messages()
        pulled = self.run_verb(bus.cmd_pull, identity=receiver, max=10, max_bytes=32768)
        seen_by_seat = pulled[:1]          # the seat's terminal truncated to one line
        self.assertEqual(len(seen_by_seat), 1)
        replayed = [m for m in self.run_verb(bus.cmd_replay, identity=receiver, max=10, max_bytes=32768)
                    if m.get("schema", "").startswith("agent-bus/message")]
        self.assertEqual([m["subject"] for m in replayed],
                         ["subject-0", "subject-1", "subject-2"])
        self.assertTrue(all(m["replayed"] for m in replayed))
        self.assertTrue(all(m["inbox_state"] == "presented" for m in replayed))

    def test_replay_consumes_nothing(self):
        """Read-only: replay twice, then pull must still behave exactly as before."""
        _, receiver = self.setup_two_seats_with_messages()
        first = [m["subject"] for m in self.run_verb(bus.cmd_replay, identity=receiver, max=10, max_bytes=32768)
                 if m.get("schema", "").startswith("agent-bus/message")]
        second = [m["subject"] for m in self.run_verb(bus.cmd_replay, identity=receiver, max=10, max_bytes=32768)
                  if m.get("schema", "").startswith("agent-bus/message")]
        self.assertEqual(first, second, "replay must be idempotent")
        pulled = [m for m in self.run_verb(bus.cmd_pull, identity=receiver, max=10, max_bytes=32768)
                  if m.get("schema", "").startswith("agent-bus/message")]
        self.assertEqual(len(pulled), 3, "replay must not have consumed or leased anything")

    def test_replay_budget_keeps_the_newest(self):
        """A small --max must keep the NEWEST messages, not the oldest."""
        _, receiver = self.setup_two_seats_with_messages(count=4)
        shown = [m["subject"] for m in self.run_verb(bus.cmd_replay, identity=receiver, max=2, max_bytes=32768)
                 if m.get("schema", "").startswith("agent-bus/message")]
        self.assertEqual(shown, ["subject-2", "subject-3"])
        summary = [m for m in self.run_verb(bus.cmd_replay, identity=receiver, max=2, max_bytes=32768)
                   if m.get("schema") == "agent-bus/replay-summary/v3"][0]
        self.assertEqual((summary["live"], summary["shown"], summary["omitted_by_budget"]), (4, 2, 2))

    def test_replay_reports_an_empty_inbox_as_empty_not_as_silence(self):
        """An empty result must still print a summary — silence would read as failure."""
        self.join("host/lonely-tmux3", "lonely")
        lonely = self.join("host/lonely-tmux3", "lonely")["agent_id"]
        out = self.run_verb(bus.cmd_replay, identity=lonely, max=10, max_bytes=32768)
        summary = [m for m in out if m.get("schema") == "agent-bus/replay-summary/v3"]
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0]["live"], 0)
