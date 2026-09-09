import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))

import workplane as wp
from send_outcome import SendOutcome

ORC = str(ROOT / "scripts" / "fleet-orchestrator.py")
LEDGER = str(ROOT / "scripts" / "dispatch-ledger.py")

_TEST_POLICY = ROOT / "tests" / "fixtures" / "orc-policy.json"
_TEST_CONTEXT = []


def setUpModule():
    policy = json.loads(_TEST_POLICY.read_text())
    _TEST_CONTEXT.extend([
        mock.patch.dict(os.environ, {"FLEET_ORCHESTRATOR_CONFIG": str(_TEST_POLICY)}),
        mock.patch.object(wp, "MERGE_KEYS", policy["authority"]["merge_keys"]),
        mock.patch.object(wp, "SERVICE_HANDLE", policy["authority"]["service_handle"]),
        mock.patch.object(wp, "read_members", return_value=[]),
    ])
    for context in _TEST_CONTEXT:
        context.start()


def tearDownModule():
    for context in reversed(_TEST_CONTEXT):
        context.stop()
    _TEST_CONTEXT.clear()


class DriveMachineTests(unittest.TestCase):


    def test_full_walk_to_escalation(self):
        entry = {}
        action, entry = wp.step_drive(entry, busy=True)
        self.assertEqual((action, entry["st"]), (None, wp.S_WORKING))
        action, entry = wp.step_drive(entry, busy=False)
        self.assertIsNone(action)
        self.assertEqual(entry["st"], wp.S_WORKING)
        for _ in range(wp.IDLE_WAIT_LIMIT - 2):
            action, entry = wp.step_drive(entry, busy=False)
            self.assertIsNone(action)
        action, entry = wp.step_drive(entry, busy=False)
        self.assertEqual((action, entry["st"]), ("escalate", wp.S_ESCALATED))
        action, entry = wp.step_drive(entry, busy=True)
        self.assertEqual((action, entry["st"]), (None, wp.S_WORKING),
                         "real work must clear an old escalation")

    def test_fresh_node_idle_pane_pulls_first(self):
        action, entry = wp.step_drive({}, busy=False)
        self.assertEqual((action, entry["st"]), ("pull", wp.S_PULLED))

    def test_seat_voice_resets_the_silent_count(self):


        action, entry = wp.step_drive({}, busy=False)
        for _ in range(wp.IDLE_WAIT_LIMIT - 1):
            action, entry = wp.step_drive(entry, busy=False)
        self.assertIsNone(action, "one tick short of escalation")
        action, entry = wp.step_drive(entry, busy=False, spoke=True)
        self.assertIsNone(action, "speech never escalates")
        self.assertEqual(int(entry.get("idle_waits", 0)), 0,
                         "speech RESETS the count, not merely defers")
        for i in range(wp.IDLE_WAIT_LIMIT - 1):
            action, entry = wp.step_drive(entry, busy=False)
            self.assertIsNone(action, f"tick {i}: full window owed again")
        action, entry = wp.step_drive(entry, busy=False)
        self.assertEqual(action, "escalate",
                         "a full speech-free window still escalates")

    def test_spoke_rearms_an_escalated_pairing(self):
        entry = {"st": wp.S_ESCALATED, "cycles": 1}
        action, entry = wp.step_drive(entry, busy=False, spoke=True)
        self.assertEqual((action, entry["st"]), (None, wp.S_PULLED))
        self.assertEqual(entry.get("idle_waits", 0), 0)

    def test_idle_escalated_pairing_starts_a_new_reminder_cycle(self):
        entry = {"st": wp.S_ESCALATED, "cycles": 1, "idle_waits": 6}
        action, entry = wp.step_drive(entry, busy=False)
        self.assertEqual((action, entry["st"]), ("pull", wp.S_ESCALATED))
        self.assertEqual(entry["cycles"], 1)
        self.assertEqual(entry["idle_waits"], 6,
                         "the unresolved wake attempt owns the retry clock")

    def test_legacy_authorized_state_is_demoted_without_typing(self):
        action, entry = wp.step_drive({"st": wp.S_AUTHORIZED, "cycles": 2},
                                      busy=False)
        self.assertIsNone(action)
        self.assertEqual(entry["st"], wp.S_PULLED)
        self.assertEqual(entry["cycles"], 2)

    def test_stretched_idle_limit_defers_escalation(self):

        entry = {"st": wp.S_PULLED, "idle_waits": 6}
        action, entry2 = wp.step_drive(entry, busy=False,
                                       idle_wait_limit=wp.IDLE_WAIT_LIMIT_ACTIVE)
        self.assertIsNone(action, "7th idle tick must not escalate at limit 24")
        self.assertEqual(entry2["idle_waits"], 7)
        entry = {"st": wp.S_PULLED, "idle_waits": 23}
        action, _ = wp.step_drive(entry, busy=False,
                                  idle_wait_limit=wp.IDLE_WAIT_LIMIT_ACTIVE)
        self.assertEqual(action, "escalate", "the stretched limit still ends")

    def test_busy_resets_the_idle_wait_counter(self):
        _, entry = wp.step_drive({}, busy=False)
        _, entry = wp.step_drive(entry, busy=False)
        _, entry = wp.step_drive(entry, busy=True)
        action, entry = wp.step_drive(entry, busy=False)
        self.assertIsNone(action)
        self.assertEqual(entry.get("idle_waits"), 1)

    def test_busy_pane_is_never_nudged(self):
        entry = {}
        for _ in range(6):
            action, entry = wp.step_drive(entry, busy=True)
            self.assertIsNone(action)


class RelationTests(unittest.TestCase):
    def test_all_relations_sound(self):
        self.assertEqual(wp.verify_relations(), [])

    def test_mechanical_permission_rule_actually_fires(self):


        original = wp.WORKFLOWS["pr"]["grants_permission"]
        wp.WORKFLOWS["pr"]["grants_permission"] = frozenset({"awaiting-review"})
        try:
            problems = wp.verify_relations()
        finally:
            wp.WORKFLOWS["pr"]["grants_permission"] = original
        self.assertTrue(any("mechanical" in p and "permission" in p
                            for p in problems), problems)

    def test_parent_has_no_close_from_running(self):

        self.assertNotIn(("running", "close"), wp.WORKFLOWS["parent"]["transitions"])
        self.assertEqual(
            wp.WORKFLOWS["parent"]["transitions"][("ready-to-close", "close")],
            "closed")

    def test_dispatch_relation_is_byte_identical(self):


        self.assertEqual(len(wp.DISPATCH_LEGACY_TRANSITIONS), 9)
        for key, target in wp.DISPATCH_LEGACY_TRANSITIONS.items():
            self.assertEqual(wp.TRANSITIONS[key], target, key)
        self.assertEqual(wp.TRANSITIONS[("_new", "open")], "open")
        self.assertEqual(wp.TRANSITIONS[("acked", "ack")], "acked")

        into_waiting = [(s, e) for (s, e), t in wp.TRANSITIONS.items()
                        if t == wp.WAITING_STATE and s != wp.WAITING_STATE]
        self.assertEqual(into_waiting, [("_new", wp.EVENT_OPEN_WAITING)])

    def test_breaker_event_may_only_be_a_self_loop(self):


        trans = wp.WORKFLOWS["dispatch"]["transitions"]
        key = ("open", wp.EVENT_BREAKER_FIRED)
        self.assertEqual(trans[key], "open")
        trans[key] = "acked"
        try:
            problems = wp.verify_relations()
        finally:
            trans[key] = "open"
        self.assertTrue(any("self loop" in p for p in problems), problems)
        self.assertEqual(wp.verify_relations(), [])


class CanonicalizeTests(unittest.TestCase):
    def test_progress_hash_ignores_timestamps_and_ages(self):
        a = "PR #12 head abc123 updated 2026-08-09T10:00:00Z (3m ago)"
        b = "PR #12 head abc123 updated 2026-08-09T11:37:22Z (2h ago)"
        c = "PR #12 head def456 updated 2026-08-09T10:00:00Z (3m ago)"
        self.assertEqual(wp.content_hash(a), wp.content_hash(b))
        self.assertNotEqual(wp.content_hash(a), wp.content_hash(c))


class StorePathTests(unittest.TestCase):
    def test_explicit_db_path_set_after_import_is_honored(self):

        with tempfile.TemporaryDirectory() as tmp:
            private = Path(tmp) / "late-private-ledger.sqlite3"
            with mock.patch.dict(
                    os.environ, {"DISPATCH_LEDGER_DB": str(private)}):
                conn = wp.connect_writable()
                conn.close()
            self.assertTrue(private.exists())

    def test_readonly_never_creates_or_changes_a_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.sqlite3"
            with mock.patch.dict(
                    os.environ, {"DISPATCH_LEDGER_DB": str(path)}):
                with self.assertRaises(sqlite3.OperationalError):
                    wp.connect_readonly()
                self.assertFalse(path.exists())

                seed = sqlite3.connect(path)
                seed.execute("CREATE TABLE sentinel(value TEXT)")
                seed.execute("INSERT INTO sentinel VALUES ('unchanged')")
                seed.commit()
                before = seed.execute(
                    "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
                ).fetchall()
                seed.close()

                conn = wp.connect_readonly()
                self.assertEqual(conn.execute("PRAGMA query_only").fetchone()[0], 1)
                self.assertEqual(conn.execute(
                    "SELECT value FROM sentinel").fetchone()[0], "unchanged")
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("CREATE TABLE forbidden(value TEXT)")
                conn.close()

                check = sqlite3.connect(path)
                after = check.execute(
                    "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
                ).fetchall()
                self.assertEqual(after, before)
                self.assertEqual(check.execute(
                    "SELECT value FROM sentinel").fetchone()[0], "unchanged")
                check.close()

    def test_linked_worktree_cannot_implicitly_write_production(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "review-worktree"
            scripts = root / "scripts"
            scripts.mkdir(parents=True)
            (root / ".git").write_text("gitdir: /tmp/not-the-main-checkout\n")
            would_be_production = Path(tmp) / "must-not-exist.sqlite3"
            env = dict(os.environ)
            env.pop("DISPATCH_LEDGER_DB", None)
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(wp, "SCRIPT_DIR", scripts), \
                    mock.patch.object(
                        wp, "CANONICAL_REPO_ROOT", Path(tmp) / "canonical"), \
                    mock.patch.object(wp, "DB_PATH", would_be_production):
                with self.assertRaisesRegex(
                        ValueError, "non-canonical checkout"):
                    wp.connect_writable()
            self.assertFalse(would_be_production.exists())

    def test_linked_worktree_cannot_explicitly_name_production_alias(self):

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            canonical = base / "canonical"
            worktree = base / "review-worktree"
            scripts = worktree / "scripts"
            scripts.mkdir(parents=True)
            production = base / "live" / "dispatch-ledger.sqlite3"
            production.parent.mkdir()
            production.write_bytes(b"production-sentinel")
            symbolic = base / "symbolic-ledger.sqlite3"
            symbolic.symlink_to(production)
            hard = base / "hard-link-ledger.sqlite3"
            os.link(production, hard)
            spellings = (production, symbolic, hard,
                         Path(os.path.relpath(production, Path.cwd())))
            for spelling in spellings:
                with self.subTest(spelling=spelling), \
                        mock.patch.dict(
                            os.environ,
                            {"DISPATCH_LEDGER_DB": str(spelling)}), \
                        mock.patch.object(wp, "SCRIPT_DIR", scripts), \
                        mock.patch.object(
                            wp, "CANONICAL_REPO_ROOT", canonical), \
                        mock.patch.object(wp, "PRODUCTION_DB_PATH", production):
                    with self.assertRaisesRegex(
                            ValueError, "production file or an alias"):
                        wp.connect_writable()
            self.assertEqual(production.read_bytes(), b"production-sentinel")

    def test_linked_worktree_cannot_write_a_live_named_fleet(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            canonical = base / "canonical"
            scripts = base / "review-worktree" / "scripts"
            scripts.mkdir(parents=True)
            named_root = base / "live-fleets"
            named_db = named_root / "alpha" / "dispatch-ledger.sqlite3"
            with mock.patch.dict(
                    os.environ, {"DISPATCH_LEDGER_DB": str(named_db)}), \
                    mock.patch.object(wp, "SCRIPT_DIR", scripts), \
                    mock.patch.object(wp, "CANONICAL_REPO_ROOT", canonical), \
                    mock.patch.object(wp, "PRODUCTION_NAMED_DB_ROOTS", (named_root,)):
                with self.assertRaisesRegex(
                        ValueError, "named production file"):
                    wp.connect_writable()
            self.assertFalse(named_db.exists())

    def test_linked_worktree_cannot_write_a_live_local_fleet(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            canonical = base / "canonical"
            scripts = base / "review-worktree" / "scripts"
            scripts.mkdir(parents=True)
            fleet_root = base / "live-fleets"
            local_db = (fleet_root / "alpha" / "state" /
                        "fleet-orchestrator" / "dispatch-ledger.sqlite3")
            with mock.patch.dict(
                    os.environ, {"DISPATCH_LEDGER_DB": str(local_db)}), \
                    mock.patch.object(wp, "SCRIPT_DIR", scripts), \
                    mock.patch.object(wp, "CANONICAL_REPO_ROOT", canonical), \
                    mock.patch.object(wp, "PRODUCTION_NAMED_DB_ROOTS", (fleet_root,)):
                with self.assertRaisesRegex(
                        ValueError, "named production file"):
                    wp.connect_writable()
            self.assertFalse(local_db.exists())

    def test_bare_hub_main_checkout_can_implicitly_write(self):

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "canonical"
            scripts = root / "scripts"
            scripts.mkdir(parents=True)
            (root / ".git").write_text("gitdir: /tmp/bare-hub/worktrees/main\n")
            private = Path(tmp) / "simulated-production.sqlite3"
            env = dict(os.environ)
            env.pop("DISPATCH_LEDGER_DB", None)
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(wp, "SCRIPT_DIR", scripts), \
                    mock.patch.object(wp, "CANONICAL_REPO_ROOT", root), \
                    mock.patch.object(wp, "DB_PATH", private):
                conn = wp.connect_writable()
                conn.close()
            self.assertTrue(private.exists())

    def test_ambiguous_connect_entry_point_is_deleted(self):
        self.assertFalse(hasattr(wp, "connect"))


class StoreTestCase(unittest.TestCase):


    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        bus_db = base / "void" / "agent-bus-v3.sqlite3"
        self.env = dict(os.environ)
        self.env.update({
            "DISPATCH_LEDGER_DB": str(base / "ledger.sqlite3"),
            "NOTES_RUNTIME_DIR": str(base / "rt"),
            "MATRIX_BUS_CFG": str(base / "void"),
            "MATRIX_BUS_HS": "http://127.0.0.1:1",
            "AGENT_BUS_DB": str(bus_db),
            "NW_BUS_CLI": str(ROOT / "scripts" / "matrix-bus.sh"),
            "ORC_SEAT_ID": "",
            "TMUX_PANE": "",
            "DISPATCH_LEDGER_ACTOR": "test@workplane",
            "NW_TMUX_SERVER": "nw-test-none",


            "NW_ORC_HANDSHAKE": "0",
        })


        gh_stub = base / "gh-inert.sh"
        gh_stub.write_text("#!/usr/bin/env bash\n"
                           "if [ \"$2\" = list ]; then echo '[]'; else echo ok; fi\n")
        gh_stub.chmod(0o755)
        self.env["NW_GH_CLI"] = str(gh_stub)
        self._old_db = wp.DB_PATH
        wp.DB_PATH = base / "ledger.sqlite3"


        self._old_cfg = wp.CFG
        wp.CFG = base / "void"
        wp.CFG.mkdir(parents=True, exist_ok=True)
        self._bus_env = mock.patch.dict(os.environ, {
            "DISPATCH_LEDGER_DB": str(base / "ledger.sqlite3"),
            "MATRIX_BUS_CFG": str(base / "void"),
            "MATRIX_BUS_HS": "http://127.0.0.1:1",
            "AGENT_BUS_DB": str(bus_db),
            "NW_BUS_CLI": str(ROOT / "scripts" / "matrix-bus.sh"),
            "ORC_SEAT_ID": "",
            "TMUX_PANE": "",
        })
        self._bus_env.start()
        self._members = mock.patch.object(wp, "read_members", side_effect=self.read_member_fixture)
        self._members.start()

    def read_member_fixture(self):
        source = Path(self.tmp.name) / "members.jsonl"
        return ([json.loads(line) for line in source.read_text().splitlines()]
                if source.exists() else [])

    def tearDown(self):
        self._members.stop()
        self._bus_env.stop()
        wp.DB_PATH = self._old_db
        wp.CFG = self._old_cfg
        self.tmp.cleanup()

    def _seed_bus_identity(self, agent_id, *, pane="%1", host=None,
                           status="active", lease_ms=None, harness="codex"):

        path = Path(self.env["AGENT_BUS_DB"])
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS identities ("
                "agent_id TEXT PRIMARY KEY, status TEXT NOT NULL,"
                " host TEXT NOT NULL, pane_id TEXT, harness TEXT NOT NULL,"
                " lease_until_ms INTEGER)")
            conn.execute(
                "INSERT OR REPLACE INTO identities"
                " (agent_id,status,host,pane_id,harness,lease_until_ms)"
                " VALUES (?,?,?,?,?,?)",
                (agent_id, status,
                 host or socket.gethostname().split(".", 1)[0], pane, harness,
                 (lease_ms if lease_ms is not None else
                  int(time.time() * 1000) + 3_600_000)))
        conn.close()
        return path

    def run_cli(self, *argv, expect=0):
        out = subprocess.run([sys.executable, *argv], text=True,
                             capture_output=True, env=self.env)
        self.assertEqual(out.returncode, expect,
                         f"{argv}\nstdout: {out.stdout}\nstderr: {out.stderr}")
        return out.stdout

    def member_source(self, conn):
        """Expose the fixture through Agent Bus, rather than an ORC disk cache."""
        members = []
        for row in conn.execute("SELECT * FROM seat"):
            member = dict(row)
            member["aliases"] = [alias for alias in member["aliases"].split(",") if alias]
            member["addressable"] = bool(member["addressable"])
            members.append(member)
        source = Path(self.tmp.name) / "members.jsonl"
        source.write_text("".join(json.dumps(member) + "\n" for member in members))
        bus = Path(self.tmp.name) / "member-source.sh"
        bus.write_text(
            '#!/bin/sh\nif [ "$1" = members ]; then\n'
            f'cat "{source}"\nelse\n'
            f'exec bash "{ROOT / "scripts" / "matrix-bus.sh"}" "$@"\nfi\n')
        self.env["NW_BUS_CLI"] = str(bus)
        os.environ["NW_BUS_CLI"] = str(bus)

    def task_ids(self):
        out = self.run_cli(LEDGER, "list", "--json", "--all")
        return [json.loads(ln) for ln in out.splitlines()]

    def set_current_drive(self, conn, task_id, state=wp.S_ESCALATED, **fields):

        row = wp.fetch(conn, task_id)
        context = wp.continuation_context(conn, row)
        self.assertIsNotNone(context)
        values = {
            "cycles": 0, "grace_used": 0, "idle_waits": 0,
            "absent_ticks": 0, **fields,
        }
        conn.execute(
            "INSERT OR REPLACE INTO drive"
            " (task_id,seat,generation,st,cycles,grace_used,idle_waits,"
            " absent_ticks,updated_ms) VALUES (?,?,?,?,?,?,?,?,?)",
            (task_id, context["seat"], context["generation"], state,
             values["cycles"], values["grace_used"], values["idle_waits"],
             values["absent_ticks"], wp.now()),
        )
        return context

    def record_current_voice(self, conn, task_id, kind="note", note="working",
                             actor=""):

        row = wp.fetch(conn, task_id)
        context = wp.continuation_context(conn, row)
        self.assertIsNotNone(context)
        speaker = actor or context.get("agent_id") or context["requested"]
        return wp.record(
            conn, task_id, kind, note, actor=speaker,
            continuation_generation=context["generation"],
        )

    def record_current_message(self, conn, task_id, purpose, dedup_key,
                               target, subject, body="", **observed):

        task = conn.execute(
            "SELECT * FROM dispatch WHERE id=?", (task_id,),
        ).fetchone()
        if task is not None:
            observed.setdefault(
                "expected_responsibility_version",
                task["responsibility_version"],
            )
            if purpose in wp.ATTENTION_ROUTE_PURPOSES:
                observed.setdefault(
                    "expected_latest_id",
                    wp.latest_message_id(conn, task_id, purpose),
                )
        return wp.record_msg(
            conn, task_id, purpose, dedup_key, target, subject, body,
            **observed,
        )

    def route_current(self, conn, task_id, purpose, dedup_key, target,
                      subject, body="", parent_task_id="", **observed):

        task = wp.fetch(conn, task_id)
        observed.setdefault(
            "expected_responsibility_version",
            task["responsibility_version"],
        )
        return wp.route(
            conn, task_id, purpose, dedup_key, target, subject, body,
            parent_task_id, **observed,
        )

    def accept_current_responsibility(self, conn, task_id, *, actual="",
                                      pane="%1"):

        row = wp.fetch(conn, task_id)
        target = wp.owed_party(row)
        actual = actual or target
        purpose = {
            "awaiting-review": "review-request",
            "receipt-due": "receipt-request",
        }.get(row["state"], "dispatch" if wp.row_workflow(row) == "dispatch"
              else "author-request")
        purposes = wp.responsibility_purposes(row)
        existing = conn.execute(
            "SELECT id FROM task_msg WHERE task_id=? AND target=?"
            " AND recipient_version=? AND purpose IN (%s)"
            " ORDER BY id DESC LIMIT 1"
            % ",".join("?" for _ in purposes),
            (task_id, target, row["responsibility_version"], *purposes),
        ).fetchone()
        msg_row_id = (existing["id"] if existing is not None
                      else self.record_current_message(
            conn, task_id, purpose,
            f"test-current:{task_id}:v{row['responsibility_version']}:"
            f"{time.time_ns()}", target, "test responsibility",
            "test responsibility body",
        ))
        self.assertIsNotNone(msg_row_id)
        conn.execute(
            "UPDATE task_msg SET send_state='accepted',msg_id=?,"
            " recipient_agent_id=? WHERE id=?",
            (f"test-msg-{msg_row_id}", actual, msg_row_id),
        )
        context = wp.continuation_context(conn, wp.fetch(conn, task_id))
        self.assertIsNotNone(context)
        self.assertEqual(context.get("agent_id"), actual)
        return context

    def insert_legacy_message(self, conn, task_id, purpose, target, *,
                              dedup_key="legacy", subject="legacy",
                              body="legacy", recipient_version=None):

        row = wp.fetch(conn, task_id)
        version = (int(row["responsibility_version"])
                   if recipient_version is None else recipient_version)
        cur = conn.execute(
            "INSERT INTO task_msg(task_id,dedup_key,purpose,target,subject,"
            " at_ms,body,recipient_version) VALUES (?,?,?,?,?,?,?,?)",
            (task_id, dedup_key, purpose, target, subject, wp.now(), body,
             version),
        )
        return cur.lastrowid


class ReadOnlyCommandTests(StoreTestCase):


    def _dump(self):
        conn = sqlite3.connect(self.env["DISPATCH_LEDGER_DB"])
        try:
            return tuple(conn.iterdump())
        finally:
            conn.close()

    def test_show_includes_stored_review_evidence_without_changing_it(self):
        receipt = "Artifact: reviewed commit abc123\nValidation: independent test log\nGaps: none"
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="author", subject="reviewed work",
                                 workflow="pr", owner_seat="author", reviewer_seat="reviewer")
            for state in ("awaiting-review", "receipt-due", "merge-pending"):
                conn.execute("UPDATE dispatch SET state=? WHERE id=?", (state, did))
            conn.execute("UPDATE dispatch SET receipt_body=? WHERE id=?",
                         (receipt, did))
        conn.close()
        before = self._dump()
        output = self.run_cli(ORC, "show", did)
        self.assertIn(receipt, output)
        self.assertEqual(self._dump(), before)

    def test_observer_commands_leave_schema_and_rows_unchanged(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="observer-fixture",
                                 subject="read-only fixture", check_cmd="true")
            conn.execute("DROP TRIGGER dispatch_responsibility_version")
        conn.close()
        before = self._dump()

        commands = (
            (LEDGER, "list", "--all"),
            (LEDGER, "show", did),
            (LEDGER, "overdue"),
            (LEDGER, "brief"),
            (LEDGER, "doctor"),
            (LEDGER, "verify"),
            (ORC, "board"),
            (ORC, "tree"),
            (ORC, "statusline", "--no-color"),
            (ORC, "kanban", "--no-color"),
            (ORC, "role", "list"),
            (ORC, "team", "list"),
            (ORC, "onboard", "observer-fixture"),
            (ORC, "topology"),
            (ORC, "verify"),
            (ORC, "snapshot"),
            (ORC, "tick", "--dry-run"),
        )
        for command in commands:
            self.run_cli(*command)
        self.run_cli(ORC, "doctor", expect=1)

        self.assertEqual(self._dump(), before)
        check = sqlite3.connect(self.env["DISPATCH_LEDGER_DB"])
        self.assertIsNone(check.execute(
            "SELECT 1 FROM sqlite_master WHERE type='trigger'"
            " AND name='dispatch_responsibility_version'").fetchone())
        check.close()

    def test_scheduler_does_not_claim_prs_from_shared_github_account(self):
        wp.connect_writable().close()
        # The configured account and merge roles span multiple projects. A PR
        # on that account is not evidence that this fleet owns the work.
        stub = Path(self.env["NW_GH_CLI"])
        calls = Path(self.tmp.name) / "github-calls"
        stub.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            f"with open({str(calls)!r}, 'a') as log:\n"
            "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
            "if sys.argv[1:3] == ['pr', 'list']:\n"
            "    print(json.dumps([{'number': 47, 'isDraft': False,\n"
            "        'title': 'Work owned by a different fleet',\n"
            "        'headRefName': 'feature/other-project',\n"
            "        'headRefOid': 'abc123'}]))\n"
            "else:\n"
            "    print('[]')\n")
        # Prove the fixture offers a real candidate before exercising the full
        # scheduler. An empty or broken GitHub stub would hide the regression.
        offered = subprocess.run(
            [str(stub), "pr", "list"], text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(offered.stdout)[0]["number"], 47)
        calls.unlink()
        self.run_cli(ORC, "tick")
        self.assertEqual(self.task_ids(), [])
        self.assertFalse(calls.exists(), "an empty fleet must not scan GitHub")

    def test_board_doctor_and_dry_tick_do_not_reconcile_claims(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="old-seat",
                                 subject="stale claim fixture", check_cmd="true")
            wp.claim_open(conn, wp.fetch(conn, did), "finished old duty")
            conn.execute("UPDATE dispatch SET recipient='new-seat' WHERE id=?",
                         (did,))
        conn.close()

        def status():
            check = sqlite3.connect(self.env["DISPATCH_LEDGER_DB"])
            try:
                return check.execute(
                    "SELECT status FROM completion_claim WHERE task_id=?",
                    (did,),
                ).fetchone()[0]
            finally:
                check.close()

        self.assertEqual(status(), "standing")
        self.run_cli(ORC, "board")
        self.assertEqual(status(), "standing")
        self.run_cli(ORC, "doctor", expect=1)
        self.assertEqual(status(), "standing")
        self.run_cli(ORC, "tick", "--dry-run")
        self.assertEqual(status(), "standing")


class CallerSeatIdentityTests(StoreTestCase):


    def test_one_active_local_pane_identity_resolves(self):
        self._seed_bus_identity("seat-one", pane="%77")
        with mock.patch.dict(os.environ, {
                "ORC_SEAT_ID": "", "TMUX_PANE": "%77"}):
            self.assertEqual(wp.caller_seat_id(), "seat-one")
            self.assertEqual(wp.caller_seat_id("77"), "seat-one")

    def test_explicit_override_wins_even_when_database_is_ambiguous(self):
        self._seed_bus_identity("seat-one", pane="%77")
        self._seed_bus_identity("seat-two", pane="%77")
        with mock.patch.dict(os.environ, {
                "ORC_SEAT_ID": "explicit-seat", "TMUX_PANE": "%77"}):
            self.assertEqual(wp.caller_seat_id(), "explicit-seat")

    def test_multiple_active_rows_are_refused_not_guessed(self):
        self._seed_bus_identity("seat-one", pane="%77")
        self._seed_bus_identity("seat-two", pane="%77")
        with mock.patch.dict(os.environ, {
                "ORC_SEAT_ID": "", "TMUX_PANE": "%77"}):
            self.assertEqual(wp.caller_seat_id(), "")

    def test_missing_database_fails_closed_without_creating_it(self):
        path = Path(self.env["AGENT_BUS_DB"])
        self.assertFalse(path.exists())
        with mock.patch.dict(os.environ, {
                "ORC_SEAT_ID": "", "TMUX_PANE": "%77"}):
            self.assertEqual(wp.caller_seat_id(), "")
        self.assertFalse(path.exists(), "read-only lookup must create nothing")

    def test_expired_identity_is_not_active(self):
        self._seed_bus_identity("expired", pane="%77", lease_ms=1)
        with mock.patch.dict(os.environ, {
                "ORC_SEAT_ID": "", "TMUX_PANE": "%77"}):
            self.assertEqual(wp.caller_seat_id(), "")
        self.assertFalse(wp.agent_bus_identity_active("expired"))

    def test_open_records_the_requester_without_enabling_terminal_mail(self):
        self.env["ORC_SEAT_ID"] = "requester-1"
        self.run_cli(ORC, "open", "--to", "worker-1", "--subject",
                     "remember who asked", "--check", "true")
        conn = wp.connect_writable()
        row = conn.execute(
            "SELECT requester_seat,await_notify FROM dispatch"
            " WHERE subject='remember who asked'",
        ).fetchone()
        self.assertEqual(tuple(row), ("requester-1", 0))


class BlockedNoteTests(StoreTestCase):


    def test_empty_blocked_refused_stated_blocked_recorded(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="tmux9", subject="x",
                                 check_cmd="true")
        conn.close()
        self.run_cli(ORC, "blocked", did, expect=1)
        conn = wp.connect_writable()
        row = wp.fetch(conn, did)
        conn.close()
        self.assertFalse(row["ask_flag"],
                         "a refused blocked must not raise the marker")
        self.env["ORC_SEAT_ID"] = "tmux9"
        self.run_cli(ORC, "blocked", did,
                     "--note", "need the operator to choose the rollout window")
        conn = wp.connect_writable()
        row = wp.fetch(conn, did)
        note = conn.execute(
            "SELECT note FROM event WHERE dispatch_id=? AND note LIKE"
            " 'blocked-on-authorization%'", (did,)).fetchone()[0]
        conn.close()
        self.assertTrue(row["ask_flag"], "stated blocked raises the marker")
        self.assertIn("choose the rollout window", note,
                      "the question travels in the ledger note")

    def test_foreign_seat_cannot_park_the_current_workers_task(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="worker-a", subject="x",
                                 check_cmd="true")
        conn.close()
        self.env["ORC_SEAT_ID"] = "worker-b"
        out = subprocess.run(
            [sys.executable, ORC, "blocked", did, "--note",
             "pretend this waits on a person"],
            text=True, capture_output=True, env=self.env,
        )
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("does not owe", out.stdout + out.stderr)
        conn = wp.connect_writable()
        self.assertEqual(wp.fetch(conn, did)["ask_flag"], 0)
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM event WHERE dispatch_id=? AND note LIKE ?",
            (did, f"{wp.ASK_NOTE_PREFIX}%"),
        ).fetchone())

    def test_foreign_ack_cannot_hide_the_current_workers_silence(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="worker-a", subject="x",
                                 check_cmd="true")
        conn.close()
        self.env["ORC_SEAT_ID"] = "worker-b"
        out = subprocess.run(
            [sys.executable, LEDGER, "ack", did], text=True,
            capture_output=True, env=self.env,
        )
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("does not owe", out.stdout + out.stderr)
        conn = wp.connect_writable()
        self.assertEqual(wp.fetch(conn, did)["state"], "open")
        self.assertFalse(wp.seat_spoke_recently(conn, did))

        conn.close()
        self.env["ORC_SEAT_ID"] = "worker-a"
        self.run_cli(LEDGER, "ack", did)
        conn = wp.connect_writable()
        self.assertEqual(wp.fetch(conn, did)["state"], "acked")
        self.assertTrue(wp.seat_spoke_recently(conn, did))


class DurableWakeAttemptTests(StoreTestCase):


    def _conn(self):
        return wp.connect_writable()

    def test_same_generation_dedups_until_resolved(self):
        conn = self._conn()
        with conn:
            self.assertTrue(wp.wake_attempt_open(conn, "t1", "s1", "pull", "open:s1"))
            self.assertFalse(wp.wake_attempt_open(conn, "t1", "s1", "pull", "open:s1"),
                             "unresolved same-key attempt must dedup")
            wp.wake_attempt_resolve(conn, "t1", "s1", "reacted-voice")
            self.assertTrue(wp.wake_attempt_open(conn, "t1", "s1", "pull", "open:s1"),
                            "a resolved attempt re-opens")
        conn.close()

    def test_generation_change_rearms_and_supersedes(self):
        conn = self._conn()
        with conn:
            self.assertTrue(wp.wake_attempt_open(conn, "t2", "s1", "pull", "open:s1"))
            self.assertTrue(wp.wake_attempt_open(conn, "t2", "s1", "pull", "fixing:s1"),
                            "a moved state/seat is a fresh responsibility")
            old = conn.execute(
                "SELECT outcome, resolved_ms FROM wake_attempt WHERE task_id='t2'"
                " AND generation='open:s1'").fetchone()
        self.assertEqual(old["outcome"], "superseded")
        self.assertGreater(old["resolved_ms"], 0)
        conn.close()

    def test_send_failure_backs_off_boundedly(self):
        conn = self._conn()
        with conn:
            self.assertTrue(wp.wake_attempt_open(conn, "t3", "s1", "pull", "g"))
            wp.wake_attempt_fail(conn, "t3", "s1", "pull", "g")
            wp.wake_attempt_fail(conn, "t3", "s1", "pull", "g")
            row = conn.execute("SELECT fails FROM wake_attempt WHERE task_id='t3'").fetchone()
        self.assertEqual(row["fails"], 2)
        self.assertEqual(wp._wake_ttl_s(0), wp.WAKE_ATTEMPT_TTL_S)
        self.assertEqual(wp._wake_ttl_s(2), wp.WAKE_ATTEMPT_TTL_S * 4)
        self.assertEqual(wp._wake_ttl_s(99), wp.WAKE_ATTEMPT_MAX_BACKOFF_S,
                         "backoff is BOUNDED")
        conn.close()

    def test_ttl_expiry_rearms(self):
        conn = self._conn()
        with conn:
            self.assertTrue(wp.wake_attempt_open(conn, "t4", "s1", "pull", "g"))
            conn.execute("UPDATE wake_attempt SET at_ms=at_ms-? WHERE task_id='t4'",
                         (wp.WAKE_ATTEMPT_TTL_S + 5,))
            self.assertTrue(wp.wake_attempt_open(conn, "t4", "s1", "pull", "g"),
                            "past ttl the attempt re-arms")
        conn.close()

    def test_unresolved_attempt_survives_new_connection_and_process(self):
        conn = self._conn()
        with conn:
            self.assertTrue(wp.wake_attempt_open(conn, "t5", "s1", "pull", "g"))
        conn.close()
        conn2 = self._conn()
        with conn2:
            self.assertFalse(wp.wake_attempt_open(conn2, "t5", "s1", "pull", "g"),
                             "the unresolved attempt must survive reconnect")
        conn2.close()

        code = (
            "import sys; sys.path.insert(0, %r); import workplane as wp;"
            "c = wp.connect_writable();"
            "print('DEDUP' if not wp.wake_attempt_open(c, 't5', 's1', 'pull', 'g')"
            " else 'OPENED')"
        ) % str(ROOT / "scripts" / "lib")
        out = subprocess.run([sys.executable, "-c", code], text=True,
                             capture_output=True, env=self.env)
        self.assertIn("DEDUP", out.stdout,
                      f"new process must see the row: {out.stdout} {out.stderr}")

    def test_concurrent_claim_is_atomic_one_winner_no_crash(self):


        c1 = wp.connect_writable()
        c2 = wp.connect_writable()
        with c1:
            first = wp.wake_attempt_open(c1, "tr", "s1", "pull", "g")
        with c2:
            second = wp.wake_attempt_open(c2, "tr", "s1", "pull", "g")
        self.assertTrue(first)
        self.assertFalse(second, "the loser reads dedup, it does not crash")
        c1.close(); c2.close()

    def test_expired_rearm_keeps_backoff_growing(self):
        conn = wp.connect_writable()
        with conn:
            self.assertTrue(wp.wake_attempt_open(conn, "tb", "s1", "pull", "g",
                                                 now_s=1_800_000_000))
            wp.wake_attempt_fail(conn, "tb", "s1", "pull", "g")

            self.assertFalse(wp.wake_attempt_open(
                conn, "tb", "s1", "pull", "g",
                now_s=1_800_000_000 + wp.WAKE_ATTEMPT_TTL_S + 5))

            self.assertTrue(wp.wake_attempt_open(
                conn, "tb", "s1", "pull", "g",
                now_s=1_800_000_000 + 2 * wp.WAKE_ATTEMPT_TTL_S + 5))
            row = conn.execute("SELECT fails FROM wake_attempt WHERE"
                               " task_id='tb'").fetchone()
        self.assertEqual(row["fails"], 1, "expiry re-arm must not reset fails")
        conn.close()

    def test_ladder_skips_send_while_attempt_stands(self):


        conn = self._conn()
        with conn:
            did = wp.insert_task(conn, recipient="tmux9", subject="w",
                                 check_cmd="true")
            context = self.set_current_drive(
                conn, did, state=wp.S_DISPATCHED)
            self.assertTrue(wp.wake_attempt_open(conn, did, "tmux9", "pull",
                                                 context["generation"]))

        with conn:
            self.assertFalse(wp.wake_attempt_open(
                conn, did, "tmux9", "pull", context["generation"]))
        conn.close()


class SeatActivityTests(StoreTestCase):


    def test_seat_active_recently_kinds_and_window(self):
        conn = wp.connect_writable()
        t1 = wp.insert_task(conn, recipient="tmux9", subject="a",
                            check_cmd="true")
        t2 = wp.insert_task(conn, recipient="tmux9", subject="b",
                            check_cmd="true")
        ids = [t1, t2]
        self.assertFalse(wp.seat_active_recently(conn, ids),
                         "open events are not the seat's voice")
        self.assertFalse(wp.seat_active_recently(conn, []),
                         "no tasks -> silent")
        with conn:
            wp.record(conn, t2, "chase", "engine chasing")
        self.assertFalse(wp.seat_active_recently(conn, ids),
                         "a chase is the chaser's voice, not the seat's")
        with conn:
            self.record_current_voice(conn, t2, "ack", "took it")
        self.assertTrue(wp.seat_active_recently(conn, ids),
                        "an ack on ANY owned task counts")
        self.assertFalse(wp.seat_active_recently(conn, ids, window_s=0),
                         "outside the window -> silent")


class TriggerTests(StoreTestCase):
    def test_trigger_refuses_illegal_pr_move_from_raw_sql(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="tmux1", subject="pr fixture",
                                 workflow="pr", owner_seat="tmux1",
                                 reviewer_seat="tmux2")
        with self.assertRaises(sqlite3.IntegrityError):
            with conn:
                conn.execute("UPDATE dispatch SET state='merge-pending' WHERE id=?",
                             (did,))

    def test_trigger_still_refuses_illegal_dispatch_move(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="tmux1", subject="d fixture",
                                 check_cmd="true")
            conn.execute("UPDATE dispatch SET state='acked' WHERE id=?", (did,))
        with self.assertRaises(sqlite3.IntegrityError):
            with conn:
                conn.execute("UPDATE dispatch SET state='open' WHERE id=?", (did,))

    def test_insert_task_refuses_non_parent_parent(self):
        conn = wp.connect_writable()
        with conn:
            plain = wp.insert_task(conn, recipient="tmux1", subject="plain",
                                   check_cmd="true")
        with self.assertRaises(SystemExit):
            wp.insert_task(conn, recipient="tmux2", subject="child",
                           check_cmd="true", parent_id=plain)


class OwedPartyTests(StoreTestCase):


    def test_task_open_refuses_blank_current_responsibility(self):
        conn = wp.connect_writable()
        with self.assertRaisesRegex(SystemExit, "recipient cannot be empty"):
            wp.insert_task(conn, recipient="  ", subject="blank")
        with self.assertRaisesRegex(SystemExit, "owner cannot be empty"):
            wp.insert_task(conn, recipient="notice", subject="blank owner",
                           workflow="pr", owner_seat=" ",
                           reviewer_seat="reviewer")
        with self.assertRaisesRegex(SystemExit, "reviewer cannot be empty"):
            wp.insert_task(conn, recipient="author", subject="blank reviewer",
                           workflow="pr", owner_seat="author",
                           reviewer_seat=" ")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM dispatch").fetchone()[0],
                         0)

    def test_owed_party_follows_the_state(self):
        conn = wp.connect_writable()
        with conn:
            pr = wp.insert_task(conn, recipient="tmux1", subject="pr fixture",
                                workflow="pr", owner_seat="tmux1",
                                reviewer_seat="tmux2")
            plain = wp.insert_task(conn, recipient="tmux9", subject="plain",
                                   check_cmd="true")
        with conn:
            conn.execute("UPDATE dispatch SET state='awaiting-review'"
                         " WHERE id=?", (pr,))
        self.assertEqual(wp.owed_party(wp.fetch(conn, pr)), "tmux2",
                         "awaiting-review owes the REVIEWER")
        with conn:
            conn.execute("UPDATE dispatch SET state='fixing' WHERE id=?", (pr,))
        self.assertEqual(wp.owed_party(wp.fetch(conn, pr)), "tmux1",
                         "fixing owes the OWNER")
        self.assertEqual(wp.owed_party(wp.fetch(conn, plain)), "tmux9",
                         "plain dispatch falls back to the recipient")


class PrLifecycleTests(StoreTestCase):
    def test_full_pr_walk_operator_repo(self):
        self.run_cli(ORC, "open", "--to", "tmux1", "--subject", "walk pr",
                     "--workflow", "pr", "--repo", "example-app",
                     "--owner", "tmux1", "--reviewer", "tmux2",
                     "--ready-cmd", "true", "--done-cmd", "false",
                     "--check", "echo head-1")
        pr_id = self.task_ids()[0]["id"]

        self.run_cli(ORC, "tick")
        rows = {r["id"]: r for r in self.task_ids()}
        self.assertEqual(rows[pr_id]["state"], "awaiting-review")

        conn = wp.connect_writable()
        with conn:
            self.accept_current_responsibility(
                conn, pr_id, actual="tmux2", pane="%2")
        conn.close()
        self.env["ORC_SEAT_ID"] = "tmux2"
        self.run_cli(ORC, "verdict", pr_id, "clean", "--note",
                     "checked per the walk fixture")
        conn = wp.connect_writable()
        with conn:
            self.accept_current_responsibility(
                conn, pr_id, actual="tmux1", pane="%1")
        conn.close()
        self.env["ORC_SEAT_ID"] = "tmux1"
        receipt = Path(self.tmp.name) / "receipt.md"
        receipt.write_text("eight-item receipt body\n")
        out = self.run_cli(ORC, "receipt", pr_id, "--body-file", str(receipt))
        self.assertIn("merge key role: operator", out)


        self.assertEqual(len(self.task_ids()), 1)
        brief = self.run_cli(LEDGER, "brief")
        self.assertIn("eight-item receipt body", brief)
        self.assertIn("Verify and merge yourself", brief)

        conn = wp.connect_writable()
        with conn:
            conn.execute("UPDATE dispatch SET done_cmd='true' WHERE id=?", (pr_id,))
        self.run_cli(ORC, "tick")
        rows = {r["id"]: r for r in self.task_ids()}
        self.assertEqual(rows[pr_id]["state"], "closed")

        self.assertIn("nothing is waiting on the operator",
                      self.run_cli(LEDGER, "brief"))
        self.run_cli(ORC, "verify")

    def test_seat_keyed_merge_pending_stays_out_of_operator_brief(self):
        self.run_cli(ORC, "open", "--to", "tmux1", "--subject", "seat-keyed pr",
                     "--workflow", "pr", "--repo", "example-storage",
                     "--owner", "tmux1", "--reviewer", "tmux2",
                     "--ready-cmd", "true", "--done-cmd", "false",
                     "--check", "echo head-1")
        pr_id = self.task_ids()[0]["id"]
        self.run_cli(ORC, "tick")
        conn = wp.connect_writable()
        with conn:
            self.accept_current_responsibility(
                conn, pr_id, actual="tmux2", pane="%2")
        self.member_source(conn)
        conn.close()
        self.env["ORC_SEAT_ID"] = "tmux2"
        self.run_cli(ORC, "verdict", pr_id, "clean", "--note", "seat-key fixture")
        conn = wp.connect_writable()
        with conn:
            self.accept_current_responsibility(
                conn, pr_id, actual="tmux1", pane="%1")
            conn.execute(
                "INSERT OR REPLACE INTO seat(agent_id,handle,host,tmux,status,"
                " addressable,refreshed_ms) VALUES"
                " ('line-owner','test/line-owner','test-host','',"
                " 'active',1,?)", (wp.now(),),
            )
            conn.execute(
                "INSERT INTO role_assignment(role,agent_id,granted_by,granted_ms)"
                " VALUES ('line-owner-of-example-storage','line-owner','test',?)",
                (wp.now(),),
            )
        self.member_source(conn)
        conn.close()
        self.env["ORC_SEAT_ID"] = "tmux1"
        receipt = Path(self.tmp.name) / "receipt.md"
        receipt.write_text("seat-keyed receipt body\n")
        out = self.run_cli(ORC, "receipt", pr_id, "--body-file", str(receipt))
        self.assertIn("merge key role: line-owner-of-example-storage", out)
        conn = wp.connect_writable()
        with conn:
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-key',"
                " recipient_agent_id='line-owner' WHERE task_id=?"
                " AND purpose='receipt-to-keyholder'",
                (pr_id,),
            )
        self.member_source(conn)
        conn.close()
        rows = {r["id"]: r for r in self.task_ids()}
        self.assertEqual(rows[pr_id]["state"], "merge-pending")

        self.assertIn("nothing is waiting on the operator",
                      self.run_cli(LEDGER, "brief"))

    def test_blockers_verdict_bumps_round_and_head_move_rerooutes(self):
        self.run_cli(ORC, "open", "--to", "tmux1", "--subject", "rounds pr",
                     "--workflow", "pr", "--repo", "example-storage",
                     "--owner", "tmux1", "--reviewer", "tmux2",
                     "--ready-cmd", "true", "--done-cmd", "false",
                     "--check", "cat " + str(Path(self.tmp.name) / "head.txt"))
        head = Path(self.tmp.name) / "head.txt"
        head.write_text("head-sha-1\n")
        pr_id = self.task_ids()[0]["id"]
        self.run_cli(ORC, "tick")
        conn = wp.connect_writable()
        with conn:
            self.accept_current_responsibility(
                conn, pr_id, actual="tmux2", pane="%2")
        conn.close()
        self.env["ORC_SEAT_ID"] = "tmux2"
        self.run_cli(ORC, "verdict", pr_id, "blockers", "--note", "two highs")
        rows = {r["id"]: r for r in self.task_ids()}
        self.assertEqual(rows[pr_id]["state"], "fixing")
        self.assertEqual(rows[pr_id]["round"], 1)
        self.run_cli(ORC, "tick")
        head.write_text("head-sha-2\n")
        self.run_cli(ORC, "tick")
        rows = {r["id"]: r for r in self.task_ids()}
        self.assertEqual(rows[pr_id]["state"], "awaiting-review")
        self.run_cli(ORC, "verify")


class ParentLifecycleTests(StoreTestCase):
    def test_rollup_review_and_reopen(self):
        self.run_cli(ORC, "open", "--to", "role:goal-lead", "--subject", "goal",
                     "--workflow", "parent", "--no-check")
        goal = self.task_ids()[0]["id"]
        self.run_cli(LEDGER, "open", "--to", "tmux1", "--subject", "child A",
                     "--check", "true")
        child = [r for r in self.task_ids() if r["id"] != goal][0]["id"]

        self.run_cli(LEDGER, "close", goal, "--resolution", "done", expect=1)
        self.run_cli(LEDGER, "close", child, "--resolution", "done")


        self.run_cli(ORC, "open", "--to", "tmux1", "--subject", "child B",
                     "--check", "true", "--parent", goal)
        child_b = [r for r in self.task_ids()
                   if r["subject"] == "child B"][0]["id"]
        self.run_cli(LEDGER, "close", child_b, "--resolution", "done")
        conn = wp.connect_writable()
        with conn:
            conn.execute("UPDATE dispatch SET ask_flag=? WHERE id=?",
                         (wp.now(), goal))
        conn.close()
        self.run_cli(ORC, "tick")
        rows = {r["id"]: r for r in self.task_ids()}
        self.assertEqual(rows[goal]["state"], "ready-to-close")
        self.assertEqual(rows[goal]["ask_flag"], 0)

        conn = wp.connect_writable()
        with conn:
            conn.execute("UPDATE dispatch SET ask_flag=? WHERE id=?",
                         (wp.now(), goal))
        conn.close()
        self.run_cli(ORC, "open", "--to", "tmux1", "--subject", "child C repeat",
                     "--check", "true", "--parent", goal)
        rows = {r["id"]: r for r in self.task_ids()}
        self.assertEqual(rows[goal]["state"], "running")
        self.assertEqual(rows[goal]["ask_flag"], 0)
        self.run_cli(LEDGER, "close",
                     [r for r in self.task_ids()
                      if r["subject"] == "child C repeat"][0]["id"],
                     "--resolution", "done")
        self.run_cli(ORC, "tick")

        self.run_cli(LEDGER, "close", goal, "--resolution", "done")
        self.run_cli(ORC, "verify")

    def test_unheld_goal_role_falls_back_to_active_commander(self):
        self.run_cli(ORC, "open", "--to", "role:goal-lead", "--subject",
                     "goal with stale commander", "--workflow", "parent",
                     "--no-check")
        goal = self.task_ids()[0]["id"]
        self.run_cli(ORC, "open", "--to", "tmux1", "--subject", "only child",
                     "--check", "true", "--parent", goal)
        child = [r for r in self.task_ids() if r["id"] != goal][0]["id"]
        self.run_cli(LEDGER, "close", child, "--resolution", "done")

        conn = wp.connect_writable()
        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,aliases,host,tmux,status,"
                " addressable,updated_at,refreshed_ms) VALUES"
                " ('commander-id','example-host/commander-old','','host','',"
                " 'active',1,'old',1)"
            )
            conn.execute(
                "INSERT INTO role_assignment (role,agent_id,granted_by,"
                " granted_ms) VALUES ('commander','commander-id','test',0)"
            )
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "orc_for_stale_commander_test",
            ROOT / "scripts" / "fleet-orchestrator.py")
        orc = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = orc
        spec.loader.exec_module(orc)
        accepted = subprocess.CompletedProcess(
            ["matrix-bus", "send"], 0,
            stdout=("{\"msg_id\":\"goal-review\","
                    "\"recipient_agent_ids\":[\"commander-id\"]}\n"),
            stderr="",
        )
        with mock.patch.object(wp.subprocess, "run", return_value=accepted):
            orc.tick_parents(conn, dry=False)
        parent = wp.fetch(conn, goal)
        self.assertEqual(parent["state"], "ready-to-close")
        notice = conn.execute(
            "SELECT target,recipient_agent_id,send_state FROM task_msg WHERE task_id=?"
            " AND purpose='goal-review'", (goal,),
        ).fetchone()
        self.assertIsNotNone(notice, "the one-shot transition must record mail")
        self.assertEqual(
            (notice["target"], notice["recipient_agent_id"],
             notice["send_state"]),
            ("role:commander", "commander-id", "accepted"),
        )

    def test_ready_parent_with_no_reviewer_stays_on_the_original_goal(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "orc_for_parent_repair_test",
            ROOT / "scripts" / "fleet-orchestrator.py",
        )
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        conn = wp.connect_writable()
        with conn:
            goal = wp.insert_task(
                conn, recipient="role:goal-lead", subject="repair parent mail",
                workflow="parent",
            )
            child = wp.insert_task(
                conn, recipient="worker", subject="finished child",
                check_cmd="true", parent_id=goal,
            )
            conn.execute("UPDATE dispatch SET state='closed' WHERE id=?",
                         (child,))
            wp.record(conn, child, "close", "done")
        self.assertEqual(wp.rollup(conn, wp.fetch(conn, goal)),
                         "children-closed")
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM task_msg WHERE task_id=? AND purpose='goal-review'",
            (goal,),
        ).fetchone())
        with mock.patch.object(wp, "bus_send", return_value=False):
            mod.tick_parents(conn, dry=False)
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM task_msg WHERE task_id=? AND purpose='goal-review'",
            (goal,),
        ).fetchone())
        self.assertTrue(wp.waits_on_operator(conn, wp.fetch(conn, goal)))

    def test_untrusted_registry_parks_parent_review_without_sending(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "orc_for_untrusted_parent_registry",
            ROOT / "scripts" / "fleet-orchestrator.py",
        )
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        conn = wp.connect_writable()
        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,status,addressable,"
                " refreshed_ms) VALUES"
                " ('goal-a','test/goal-a','active',1,?)", (wp.now(),),
            )
            goal = wp.insert_task(
                conn, recipient="goal-a", subject="unverified parent",
                workflow="parent",
            )
            child = wp.insert_task(
                conn, recipient="worker", subject="done child",
                check_cmd="true", parent_id=goal,
            )
            conn.execute("UPDATE dispatch SET state='closed' WHERE id=?",
                         (child,))
            wp.record(conn, child, "close", "done")
        with mock.patch.object(wp, "bus_send") as send:
            mod.tick_parents(conn, dry=False, registry_trusted=False)
        send.assert_not_called()
        parent = wp.fetch(conn, goal)
        self.assertIsNotNone(wp.operator_queue_marker(conn, parent))
        self.assertTrue(wp.waits_on_operator(conn, parent))
        self.assertEqual(wp.repair_attention_notifications(
            conn, registry_trusted=True), 1)
        notices = conn.execute(
            "SELECT target FROM task_msg WHERE task_id=?"
            " AND purpose='goal-review' ORDER BY id", (goal,),
        ).fetchall()
        self.assertEqual([notice["target"] for notice in notices],
                         ["operator", "goal-a"])

    def test_delayed_operator_marker_cannot_shadow_recovered_goal_notice(self):
        conn = wp.connect_writable()
        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,status,addressable,"
                " refreshed_ms) VALUES"
                " ('goal-a','test/goal-a','active',1,?)", (wp.now(),),
            )
            goal = wp.insert_task(
                conn, recipient="goal-a", subject="recovered parent route",
                workflow="parent",
            )
            child = wp.insert_task(
                conn, recipient="worker", subject="done child",
                check_cmd="true", parent_id=goal,
            )
            conn.execute("UPDATE dispatch SET state='closed' WHERE id=?",
                         (child,))
            wp.record(conn, child, "close", "done")
            self.assertEqual(wp.rollup(conn, wp.fetch(conn, goal)),
                             "children-closed")
            event = conn.execute(
                "SELECT id FROM event WHERE dispatch_id=?"
                " AND kind='children-closed' ORDER BY id DESC LIMIT 1",
                (goal,),
            ).fetchone()
            good = self.record_current_message(
                conn, goal, "goal-review",
                f"goal-review:{goal}:attention-event={event['id']}",
                "goal-a", "current review", "current review",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='good',"
                " recipient_agent_id='goal-a' WHERE id=?", (good,),
            )

        with conn:
            stale = wp.record_operator_queue_marker(
                conn, goal, "goal-review",
                f"goal-review:{goal}:attention-event={event['id']}:"
                "operator:unverified:after:0",
                "stale", "stale", registry_trusted=False,
                expected_latest_id=0,
                expected_responsibility_version=
                wp.fetch(conn, goal)["responsibility_version"],
            )
        self.assertIsNone(stale)
        task = wp.fetch(conn, goal)
        message = conn.execute(
            "SELECT * FROM task_msg WHERE id=?", (good,),
        ).fetchone()
        self.assertTrue(wp.message_is_current_responsibility(
            conn, message, task))
        self.assertIsNone(wp.operator_queue_marker(conn, task))
        self.assertFalse(wp.waits_on_operator(conn, task))

    def test_operator_marker_owns_its_locked_message_snapshot(self):
        conn = wp.connect_writable()
        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,status,addressable,"
                " refreshed_ms) VALUES"
                " ('goal-a','test/goal-a','active',1,?)", (wp.now(),),
            )
            goal = wp.insert_task(
                conn, recipient="goal-a", subject="marker snapshot",
                workflow="parent",
            )
            event_id = wp.record(conn, goal, "children-closed", "ready")
            conn.execute(
                "UPDATE dispatch SET state='ready-to-close' WHERE id=?",
                (goal,),
            )
            notice = self.record_current_message(
                conn, goal, "goal-review",
                f"goal-review:{goal}:attention-event={event_id}",
                "goal-a", "review", "review",
            )
            row = wp.fetch(conn, goal)
            marker = wp.record_operator_queue_marker(
                conn, goal, "goal-review",
                f"goal-review:{goal}:attention-event={event_id}:after:0",
                "operator review", "operator review",
                registry_trusted=False,
                expected_latest_id=notice,
                expected_responsibility_version=
                row["responsibility_version"],
            )

        stored = conn.execute(
            "SELECT * FROM task_msg WHERE id=?", (marker,),
        ).fetchone()
        self.assertIsNotNone(stored)
        self.assertEqual(wp.operator_marker_snapshot_id(stored), notice)

    def test_existing_late_operator_marker_does_not_hide_recovered_goal_notice(self):
        conn = wp.connect_writable()
        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,status,addressable,"
                " refreshed_ms) VALUES"
                " ('goal-a','test/goal-a','active',1,?)", (wp.now(),),
            )
            goal = wp.insert_task(
                conn, recipient="goal-a", subject="legacy late marker",
                workflow="parent",
            )
            child = wp.insert_task(
                conn, recipient="worker", subject="done child",
                check_cmd="true", parent_id=goal,
            )
            conn.execute("UPDATE dispatch SET state='closed' WHERE id=?",
                         (child,))
            wp.record(conn, child, "close", "done")
            self.assertEqual(wp.rollup(conn, wp.fetch(conn, goal)),
                             "children-closed")
            event = conn.execute(
                "SELECT id FROM event WHERE dispatch_id=?"
                " AND kind='children-closed' ORDER BY id DESC LIMIT 1",
                (goal,),
            ).fetchone()
            good = self.record_current_message(
                conn, goal, "goal-review",
                f"goal-review:{goal}:attention-event={event['id']}",
                "goal-a", "current review", "current review",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='good',"
                " recipient_agent_id='goal-a' WHERE id=?", (good,),
            )
            stale = self.insert_legacy_message(
                conn, goal, "goal-review", "operator",
                dedup_key=(f"goal-review:{goal}:attention-event={event['id']}:"
                           "operator:unverified:after:0"),
            )
            conn.execute(
                "UPDATE task_msg SET send_state='operator-queue',"
                " processed='operator-queue' WHERE id=?", (stale,),
            )

        task = wp.fetch(conn, goal)
        message = conn.execute(
            "SELECT * FROM task_msg WHERE id=?", (good,),
        ).fetchone()
        self.assertTrue(wp.message_is_current_responsibility(
            conn, message, task))
        self.assertIsNone(wp.operator_queue_marker(conn, task))
        context = wp.continuation_context(conn, task)
        self.assertEqual(context.get("agent_id"), "goal-a")
        self.assertNotIn("deferred", context)
        self.assertFalse(wp.waits_on_operator(conn, task))

    def test_reopened_parent_gets_a_new_notice_in_the_same_day(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "orc_for_parent_round_test",
            ROOT / "scripts" / "fleet-orchestrator.py",
        )
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        conn = wp.connect_writable()
        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,status,addressable,"
                " refreshed_ms) VALUES"
                " ('goal-lead','test/goal-lead','active',1,?)", (wp.now(),),
            )
            goal = wp.insert_task(
                conn, recipient="goal-lead", subject="repeated parent",
                workflow="parent",
            )

        def add_child(subject, *, closed=True):
            with conn:
                child = wp.insert_task(
                    conn, recipient="worker", subject=subject,
                    check_cmd="true", parent_id=goal,
                )
                if closed:
                    conn.execute("UPDATE dispatch SET state='closed' WHERE id=?",
                                 (child,))
                    wp.record(conn, child, "close", "done")
            return child

        with mock.patch.object(wp, "bus_send", return_value=False):
            add_child("round one")
            mod.tick_parents(conn, dry=False)
            second = add_child("round two", closed=False)
        with mock.patch.object(wp, "bus_send") as send:
            self.assertEqual(wp.retry_unsent(conn), (0, 0))
        send.assert_not_called()
        with conn:
            conn.execute("UPDATE dispatch SET state='closed' WHERE id=?",
                         (second,))
            wp.record(conn, second, "close", "done")
        with mock.patch.object(wp, "bus_send", return_value=False):
            mod.tick_parents(conn, dry=False)
        keys = [r["dedup_key"] for r in conn.execute(
            "SELECT dedup_key FROM task_msg WHERE task_id=?"
            " AND purpose='goal-review' ORDER BY id", (goal,),
        )]
        self.assertEqual(len(keys), 2)
        self.assertNotEqual(keys[0], keys[1])
        with mock.patch.object(wp, "bus_send", return_value=True) as send:
            self.assertEqual(wp.retry_unsent(conn), (1, 0))
        self.assertEqual(send.call_count, 1,
                         "only the latest review round may retry")

    def test_ready_parent_notice_follows_a_new_active_goal_owner(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "orc_for_parent_retarget_test",
            ROOT / "scripts" / "fleet-orchestrator.py",
        )
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        conn = wp.connect_writable()
        with conn:
            for seat in ("goal-a", "goal-b"):
                conn.execute(
                    "INSERT INTO seat (agent_id,handle,status,addressable,"
                    " refreshed_ms) VALUES (?,?,?,?,?)",
                    (seat, f"test/{seat}", "active", 1, wp.now()),
                )
            goal = wp.insert_task(
                conn, recipient="goal-a", subject="retarget parent",
                workflow="parent",
            )
            child = wp.insert_task(
                conn, recipient="worker", subject="finished child",
                check_cmd="true", parent_id=goal,
            )
            conn.execute("UPDATE dispatch SET state='closed' WHERE id=?",
                         (child,))
            wp.record(conn, child, "close", "done")
        with mock.patch.object(wp, "bus_send", return_value=False):
            mod.tick_parents(conn, dry=False)
        with conn:
            conn.execute("UPDATE dispatch SET recipient=? WHERE id=?",
                         ("goal-b", goal))
        self.assertEqual(wp.repair_attention_notifications(conn), 1)
        with conn:
            conn.execute("UPDATE dispatch SET recipient=? WHERE id=?",
                         ("goal-a", goal))
        self.assertEqual(wp.repair_attention_notifications(conn), 1)
        notices = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=?"
            " AND purpose='goal-review' ORDER BY id", (goal,),
        ).fetchall()
        self.assertEqual([n["target"] for n in notices],
                         ["goal-a", "goal-b", "goal-a"])
        parent = wp.fetch(conn, goal)
        self.assertEqual([wp.message_is_current_responsibility(conn, n, parent)
                          for n in notices], [False, False, True])
        with mock.patch.object(wp, "bus_send", return_value=True) as send:
            self.assertEqual(wp.retry_unsent(conn), (1, 0))
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[1], notices[-1]["id"])

    def test_team_role_falls_back_to_old_a_without_notice_revival(self):
        conn = wp.connect_writable()
        with conn:
            for seat in ("goal-a", "goal-b"):
                conn.execute(
                    "INSERT INTO seat(agent_id,handle,status,addressable,"
                    " refreshed_ms) VALUES (?,?,?,?,?)",
                    (seat, f"test/{seat}", "active", 1, wp.now()),
                )
            goal = wp.insert_task(
                conn, recipient="role:goal-lead", subject="team role round trip",
                workflow="parent",
            )
            conn.execute(
                "INSERT INTO team_member(parent_task_id,agent_id,team_role,"
                " added_by,added_ms) VALUES (?,?,?,?,?)",
                (goal, "goal-a", "goal-lead", "test", wp.now()),
            )
            event_id = wp.record(conn, goal, "children-closed", "ready")
            conn.execute(
                "UPDATE dispatch SET state='ready-to-close' WHERE id=?",
                (goal,),
            )
            first = self.record_current_message(
                conn, goal, "goal-review",
                f"goal-review:{goal}:attention-event={event_id}",
                "role:goal-lead", "first A", "first A",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='team-a1',"
                " recipient_agent_id='goal-a' WHERE id=?", (first,),
            )
            conn.execute(
                "INSERT INTO team_member(parent_task_id,agent_id,team_role,"
                " added_by,added_ms) VALUES (?,?,?,?,?)",
                (goal, "goal-b", "goal-lead", "test", wp.now() + 1),
            )
        self.assertEqual(wp.repair_attention_notifications(conn), 1)
        second = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=?"
            " AND purpose='goal-review' ORDER BY id DESC LIMIT 1", (goal,),
        ).fetchone()
        with conn:
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='team-b',"
                " recipient_agent_id='goal-b' WHERE id=?", (second["id"],),
            )
            conn.execute(
                "UPDATE team_member SET team_role='observer'"
                " WHERE parent_task_id=? AND agent_id='goal-b'",
                (goal,),
            )
        self.assertEqual(wp.repair_attention_notifications(conn), 1)
        notices = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=?"
            " AND purpose='goal-review' ORDER BY id", (goal,),
        ).fetchall()
        parent = wp.fetch(conn, goal)
        self.assertEqual([wp.message_is_current_responsibility(
            conn, notice, parent) for notice in notices],
            [False, False, True])
        generations = [notice["dedup_key"].split(
            ":role-generation-", 1)[1] for notice in notices]
        self.assertEqual(len(set(generations)), 3)

    def test_role_notice_without_proven_recipient_stays_single_and_visible(self):
        conn = wp.connect_writable()
        with conn:
            conn.execute(
                "INSERT INTO seat(agent_id,handle,status,addressable,"
                " refreshed_ms) VALUES (?,?,?,?,?)",
                ("goal-a", "test/goal-a", "active", 1, wp.now()),
            )
            goal = wp.insert_task(
                conn, recipient="role:goal-lead",
                subject="unproven role recipient", workflow="parent",
            )
            conn.execute(
                "INSERT INTO team_member(parent_task_id,agent_id,team_role,"
                " added_by,added_ms) VALUES (?,?,?,?,?)",
                (goal, "goal-a", "goal-lead", "test", wp.now()),
            )
            event_id = wp.record(conn, goal, "children-closed", "ready")
            conn.execute(
                "UPDATE dispatch SET state='ready-to-close' WHERE id=?",
                (goal,),
            )
            notice = self.record_current_message(
                conn, goal, "goal-review",
                f"goal-review:{goal}:attention-event={event_id}",
                "role:goal-lead", "review", "review",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',"
                " msg_id='opaque-message-id',recipient_agent_id=''"
                " WHERE id=?", (notice,),
            )

        self.assertEqual(wp.repair_attention_notifications(conn), 0)
        self.assertEqual(wp.repair_attention_notifications(conn), 0)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM task_msg WHERE task_id=?"
            " AND purpose='goal-review'", (goal,),
        ).fetchone()[0], 1)
        self.assertTrue(wp.waits_on_operator(conn, wp.fetch(conn, goal)))

    def test_ready_parent_notice_does_not_revive_after_operator_round_trip(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "orc_for_parent_operator_round_trip",
            ROOT / "scripts" / "fleet-orchestrator.py",
        )
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        conn = wp.connect_writable()
        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,status,addressable,"
                " refreshed_ms) VALUES ('goal-a','test/goal-a','active',1,?)",
                (wp.now(),),
            )
            goal = wp.insert_task(
                conn, recipient="goal-a", subject="operator round trip goal",
                workflow="parent",
            )
            child = wp.insert_task(
                conn, recipient="worker", subject="closed child",
                check_cmd="true", parent_id=goal,
            )
            conn.execute("UPDATE dispatch SET state='closed' WHERE id=?",
                         (child,))
            wp.record(conn, child, "close", "done")
        with mock.patch.object(wp, "bus_send", return_value=False):
            mod.tick_parents(conn, dry=False)
        with conn:
            first_notice = conn.execute(
                "SELECT id FROM task_msg WHERE task_id=?"
                " AND purpose='goal-review' ORDER BY id LIMIT 1", (goal,),
            ).fetchone()["id"]
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='old-a',"
                " recipient_agent_id='goal-a' WHERE id=?", (first_notice,),
            )
            conn.execute("UPDATE seat SET status='retired'"
                         " WHERE agent_id='goal-a'")
        self.assertEqual(wp.repair_attention_notifications(conn), 1)
        with conn:
            conn.execute("UPDATE seat SET status='active'"
                         " WHERE agent_id='goal-a'")
        self.assertEqual(wp.repair_attention_notifications(conn), 1)
        notices = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=?"
            " AND purpose='goal-review' ORDER BY id", (goal,),
        ).fetchall()
        self.assertEqual([n["target"] for n in notices],
                         ["goal-a", "operator", "goal-a"])
        self.assertEqual(notices[1]["send_state"], "operator-queue")
        parent = wp.fetch(conn, goal)
        self.assertEqual([wp.message_is_current_responsibility(
            conn, notice, parent) for notice in notices],
            [False, False, True])
        self.assertIsNone(wp.operator_queue_marker(conn, parent))
        self.assertFalse(wp.waits_on_operator(conn, parent))
        with mock.patch.object(wp, "bus_send", return_value=True) as send:
            self.assertEqual(wp.retry_unsent(conn), (1, 0))
        self.assertEqual([call.args[1] for call in send.call_args_list],
                         [notices[-1]["id"]])

    def test_malformed_ready_parent_without_rollup_event_is_inert(self):

        conn = wp.connect_writable()
        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,status,addressable,"
                " refreshed_ms) VALUES ('goal-owner','test/goal-owner',"
                " 'active',1,?)", (wp.now(),),
            )
            goal = wp.insert_task(
                conn, recipient="goal-owner", subject="malformed parent",
                workflow="parent",
            )
            conn.execute("DROP TRIGGER dispatch_state_legal")
            conn.execute("UPDATE dispatch SET state='ready-to-close' WHERE id=?",
                         (goal,))
            msg_id = self.insert_legacy_message(
                conn, goal, "goal-review", "goal-owner",
                dedup_key=f"goal-review:{goal}:legacy",
                subject="review", body="body",
            )
        msg = conn.execute("SELECT * FROM task_msg WHERE id=?", (msg_id,)).fetchone()
        self.assertFalse(wp.message_is_current_responsibility(
            conn, msg, wp.fetch(conn, goal),
        ))
        self.assertEqual(wp.repair_attention_notifications(conn), 0)


class ResolutionTests(StoreTestCase):
    def test_recipient_forms(self):
        conn = wp.connect_writable()
        self.assertEqual(wp.resolve_recipient(conn, "tmux19")["window"], "19")
        self.assertEqual(
            wp.resolve_recipient(conn, "example-host/code-review-16-tmux16")["window"], "16")
        unheld = wp.resolve_recipient(conn, "role:nobody-holds-this")
        self.assertIsNone(unheld["agent_id"])
        self.assertIsNone(unheld["window"])
        with conn:
            conn.execute("INSERT INTO seat (agent_id, handle, aliases, host, tmux,"
                         " status, addressable, updated_at, refreshed_ms)"
                         " VALUES ('aid-1','example-host/worker-x','','otherhost',"
                         "'tmux=0:7.0 win=claude','active',1,'',0)")
            conn.execute("INSERT INTO role_assignment (role, agent_id, granted_by,"
                         " granted_ms) VALUES ('commander','aid-1','test',0)")
        via_handle = wp.resolve_recipient(conn, "example-host/worker-x")
        self.assertEqual(via_handle["agent_id"], "aid-1")
        via_role = wp.resolve_recipient(conn, "role:commander")
        self.assertEqual(via_role["seat"], "aid-1")

    def test_merge_key_defaults_toward_operator(self):
        self.assertEqual(wp.merge_key_role("example-app"), "operator")
        self.assertEqual(wp.merge_key_role("example-storage"), "line-owner-of-example-storage")
        self.assertEqual(wp.merge_key_role("some-new-repo"), "operator")

    def test_team_role_wins_over_global(self):
        conn = wp.connect_writable()
        with conn:
            goal = wp.insert_task(conn, recipient="role:goal-lead",
                                  subject="team goal", workflow="parent")
            conn.execute("INSERT INTO role_assignment (role, agent_id, granted_by,"
                         " granted_ms) VALUES ('reviewer','global-r','test',0)")
            conn.execute("INSERT INTO team_member (parent_task_id, agent_id,"
                         " team_role, added_by, added_ms)"
                         " VALUES (?,?,?,?,0)", (goal, "team-r", "reviewer", "test"))
        self.assertEqual(wp.role_holder(conn, "reviewer"), "global-r")
        self.assertEqual(wp.role_holder(conn, "reviewer", goal), "team-r")


class OutboxTests(StoreTestCase):
    def test_bus_send_missing_outbox_row_never_calls_transport(self):
        conn = wp.connect_writable()
        with mock.patch.object(wp.subprocess, "run") as transport:
            self.assertFalse(wp.bus_send(conn, 999999))
        transport.assert_not_called()

    def test_direct_send_refuses_a_superseded_responsibility_message(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="owner-a", subject="moves",
                                 check_cmd="true")
            old = self.record_current_message(conn, did, "dispatch", f"dispatch:{did}",
                                "owner-a", "s", "b")
            conn.execute("UPDATE dispatch SET recipient='owner-b' WHERE id=?",
                         (did,))
        with mock.patch.object(wp.subprocess, "run") as transport:
            self.assertFalse(wp.bus_send(conn, old))
        transport.assert_not_called()
        row = conn.execute(
            "SELECT send_state,attempts FROM task_msg WHERE id=?", (old,),
        ).fetchone()
        self.assertEqual(tuple(row), ("recorded", 0))

    def test_task_action_writes_require_the_observed_generations(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(
                conn, recipient="owner-a", subject="explicit observation",
                check_cmd="true",
            )
            with self.assertRaisesRegex(ValueError, "responsibility messages"):
                wp.record_msg(
                    conn, did, "dispatch", f"dispatch:{did}",
                    "owner-a", "subject", "body",
                )

            goal = wp.insert_task(
                conn, recipient="goal-a", subject="explicit message snapshot",
                workflow="parent",
            )
            conn.execute(
                "INSERT INTO seat(agent_id,handle,status,addressable,refreshed_ms)"
                " VALUES ('goal-a','test/goal-a','active',1,?)",
                (wp.now(),),
            )
            event_id = wp.record(conn, goal, "children-closed", "ready")
            conn.execute(
                "UPDATE dispatch SET state='ready-to-close' WHERE id=?",
                (goal,),
            )
            with self.assertRaisesRegex(ValueError, "attention routes"):
                wp.record_msg(
                    conn, goal, "goal-review",
                    f"goal-review:{goal}:attention-event={event_id}",
                    "goal-a", "subject", "body",
                    expected_responsibility_version=
                    wp.fetch(conn, goal)["responsibility_version"],
                )

    def test_stale_receipt_event_cannot_create_a_notice_or_operator_marker(self):
        conn = wp.connect_writable()
        with conn:
            conn.execute(
                "INSERT INTO seat(agent_id,handle,status,addressable,refreshed_ms)"
                " VALUES ('line-owner','test/line-owner','active',1,?)",
                (wp.now(),),
            )
            conn.execute(
                "INSERT INTO role_assignment(role,agent_id,granted_by,granted_ms)"
                " VALUES ('line-owner-of-example-storage','line-owner','test',?)",
                (wp.now(),),
            )
            did = wp.insert_task(
                conn, recipient="owner", subject="receipt event race",
                workflow="pr", repo="example-storage", owner_seat="owner",
                reviewer_seat="reviewer",
            )
            for state in ("awaiting-review", "receipt-due", "merge-pending"):
                conn.execute("UPDATE dispatch SET state=? WHERE id=?",
                             (state, did))
            event_one = wp.record(conn, did, "receipt", "first")
            first = self.record_current_message(
                conn, did, "receipt-to-keyholder",
                f"receipt-key:{did}:n1:attention-event={event_one}",
                "role:line-owner-of-example-storage", "first", "first",
            )
            event_two = wp.record(conn, did, "receipt", "second")
            row = wp.fetch(conn, did)
            stale_marker = wp.record_operator_queue_marker(
                conn, did, "receipt-to-keyholder",
                f"receipt-key:{did}:late-marker:attention-event={event_one}",
                "late marker", "late marker", registry_trusted=False,
                expected_latest_id=first,
                expected_responsibility_version=row["responsibility_version"],
            )
            stale_notice = self.record_current_message(
                conn, did, "receipt-to-keyholder",
                f"receipt-key:{did}:late-notice:attention-event={event_one}",
                "role:line-owner-of-example-storage", "late", "late",
                expected_latest_id=first,
                expected_responsibility_version=row["responsibility_version"],
            )
            current = self.record_current_message(
                conn, did, "receipt-to-keyholder",
                f"receipt-key:{did}:n2:attention-event={event_two}",
                "role:line-owner-of-example-storage", "second", "second",
                expected_latest_id=first,
                expected_responsibility_version=row["responsibility_version"],
            )
        self.assertIsNone(stale_marker)
        self.assertIsNone(stale_notice)
        messages = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=?"
            " AND purpose='receipt-to-keyholder' ORDER BY id", (did,),
        ).fetchall()
        self.assertEqual([message["id"] for message in messages],
                         [first, current])
        self.assertEqual([
            wp.message_is_current_responsibility(conn, message, wp.fetch(conn, did))
            for message in messages
        ], [False, True])

    def test_inflight_success_only_retires_its_exact_operator_marker(self):
        conn = wp.connect_writable()

        def exercise(label, send_stdout):
            with conn:
                seat = f"goal-{label}"
                conn.execute(
                    "INSERT INTO seat(agent_id,handle,status,addressable,"
                    " refreshed_ms) VALUES (?,?,?,?,?)",
                    (seat, f"test/{seat}", "active", 1, wp.now()),
                )
                goal = wp.insert_task(
                    conn, recipient=seat, subject=f"inflight {label}",
                    workflow="parent",
                )
                event_id = wp.record(conn, goal, "children-closed", "ready")
                conn.execute(
                    "UPDATE dispatch SET state='ready-to-close' WHERE id=?",
                    (goal,),
                )
                message = self.record_current_message(
                    conn, goal, "goal-review",
                    f"goal-review:{goal}:attention-event={event_id}",
                    seat, "review", "review",
                )

            marker_id = None

            def send_then_park(*_args, **_kwargs):
                nonlocal marker_id
                current = wp.fetch(conn, goal)
                with conn:
                    marker_id = wp.record_operator_queue_marker(
                        conn, goal, "goal-review",
                        f"goal-review:{goal}:attention-event={event_id}:"
                        "operator:unverified",
                        "uncertain", "uncertain", registry_trusted=False,
                        expected_latest_id=message,
                        expected_responsibility_version=
                        current["responsibility_version"],
                    )
                self.assertIsNotNone(marker_id)
                return subprocess.CompletedProcess(
                    ["bus", "send"], 0, stdout=send_stdout, stderr="")

            with mock.patch.object(wp.subprocess, "run",
                                   side_effect=send_then_park):
                self.assertTrue(wp.bus_send(conn, message))
            marker = conn.execute(
                "SELECT * FROM task_msg WHERE id=?", (marker_id,),
            ).fetchone()
            return goal, message, marker

        goal, message, marker = exercise(
            "proved",
            '{"msg_id":"m-proved",'
            '"recipient_agent_ids":["goal-proved"]}\n',
        )
        self.assertEqual(marker["send_state"], "superseded-after-accepted")
        self.assertIsNone(wp.operator_queue_marker(conn, wp.fetch(conn, goal)))
        accepted = conn.execute(
            "SELECT * FROM task_msg WHERE id=?", (message,),
        ).fetchone()
        self.assertTrue(wp.message_is_current_responsibility(
            conn, accepted, wp.fetch(conn, goal)))

        goal, _message, marker = exercise(
            "unproved", '{"msg_id":"m-unproved"}\n')
        self.assertEqual(marker["send_state"], "operator-queue")
        self.assertIsNotNone(
            wp.operator_queue_marker(conn, wp.fetch(conn, goal)))
        self.assertTrue(wp.waits_on_operator(conn, wp.fetch(conn, goal)))

    def test_second_round_receipt_commits_notice_before_transport(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "orc_for_atomic_receipt", ROOT / "scripts" / "fleet-orchestrator.py",
        )
        orc = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = orc
        spec.loader.exec_module(orc)
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(
                conn, recipient="owner", subject="two receipts",
                workflow="pr", repo="example-storage", owner_seat="owner",
                reviewer_seat="reviewer",
            )
            conn.execute("UPDATE dispatch SET state='awaiting-review' WHERE id=?",
                         (did,))
            conn.execute("UPDATE dispatch SET state='receipt-due' WHERE id=?",
                         (did,))
            conn.execute("UPDATE dispatch SET state='merge-pending' WHERE id=?",
                         (did,))
            first_event = wp.record(conn, did, "receipt", "first receipt")
            first = self.record_current_message(
                conn, did, "receipt-to-keyholder",
                f"receipt-key:{did}:n1:attention-event={first_event}",
                "role:line-owner-of-example-storage", "first", "first body",
            )
            conn.execute("UPDATE task_msg SET send_state='failed' WHERE id=?",
                         (first,))
            conn.execute("UPDATE dispatch SET state='awaiting-review' WHERE id=?",
                         (did,))
            conn.execute("UPDATE dispatch SET state='receipt-due' WHERE id=?",
                         (did,))
            conn.execute(
                "INSERT INTO seat(agent_id,handle,status,addressable,refreshed_ms)"
                " VALUES ('line-owner','test/line-owner','active',1,?)",
                (wp.now(),),
            )
            conn.execute(
                "INSERT INTO role_assignment(role,agent_id,granted_by,granted_ms)"
                " VALUES ('line-owner-of-example-storage','line-owner','test',?)",
                (wp.now(),),
            )
            self.accept_current_responsibility(
                conn, did, actual="owner", pane="%1")
        self.member_source(conn)
        with mock.patch.dict(os.environ, {"ORC_SEAT_ID": "owner"}), \
                mock.patch.object(
                    wp, "bus_send",
                    side_effect=RuntimeError("crash after commit")):
            with self.assertRaisesRegex(RuntimeError, "after commit"):
                orc.cmd_receipt(mock.Mock(
                    id=did, body_file="", body="second round body",
                ))
        current = wp.fetch(conn, did)
        latest = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=?"
            " AND purpose='receipt-to-keyholder' ORDER BY id DESC LIMIT 1",
            (did,),
        ).fetchone()
        self.assertEqual(current["state"], "merge-pending")
        self.assertEqual(latest["send_state"], "recorded")
        self.assertEqual(latest["recipient_version"],
                         current["responsibility_version"])
        self.assertIn("second round body", latest["body"])

    def test_stale_receipt_never_revives_in_a_later_merge_pending_round(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(
                conn, recipient="owner", subject="stale receipt",
                workflow="pr", repo="example-storage", owner_seat="owner",
                reviewer_seat="reviewer",
            )
            conn.execute("UPDATE dispatch SET state='awaiting-review' WHERE id=?",
                         (did,))
            conn.execute("UPDATE dispatch SET state='receipt-due' WHERE id=?",
                         (did,))
            conn.execute("UPDATE dispatch SET state='merge-pending' WHERE id=?",
                         (did,))
            first_event = wp.record(conn, did, "receipt", "first receipt")
            msg = self.record_current_message(
                conn, did, "receipt-to-keyholder",
                f"receipt-key:{did}:n1:attention-event={first_event}",
                "role:line-owner-of-example-storage", "s", "b",
            )
            conn.execute("UPDATE task_msg SET send_state='failed' WHERE id=?",
                         (msg,))
            conn.execute("UPDATE dispatch SET state='awaiting-review' WHERE id=?",
                         (did,))
            conn.execute("UPDATE dispatch SET state='receipt-due' WHERE id=?",
                         (did,))


            conn.execute("UPDATE dispatch SET state='merge-pending' WHERE id=?",
                         (did,))
        with mock.patch.object(wp, "bus_send") as send:
            self.assertEqual(wp.retry_unsent(conn, log=lambda *_: None), (0, 0))
        send.assert_not_called()

    def test_stale_review_desync_never_retries_after_verdict(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(
                conn, recipient="owner", subject="stale review chase",
                workflow="pr", repo="example-app", owner_seat="owner",
                reviewer_seat="reviewer",
            )
            conn.execute("UPDATE dispatch SET state='awaiting-review' WHERE id=?",
                         (did,))
            msg = self.record_current_message(
                conn, did, "review-desync", f"reconcile:{did}:head-1",
                "reviewer", "s", "b",
            )
            conn.execute("UPDATE task_msg SET send_state='failed' WHERE id=?",
                         (msg,))
            conn.execute("UPDATE dispatch SET state='receipt-due' WHERE id=?",
                         (did,))
        with mock.patch.object(wp, "bus_send") as send:
            self.assertEqual(wp.retry_unsent(conn, log=lambda *_: None), (0, 0))
        send.assert_not_called()

    def test_record_msg_is_idempotent(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="tmux1", subject="msg fixture",
                                 check_cmd="true")
            first = self.record_current_message(conn, did, "dispatch", f"dispatch:{did}",
                                  "tmux1", "s")
            second = self.record_current_message(conn, did, "dispatch", f"dispatch:{did}",
                                   "tmux1", "s")
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        rows = conn.execute("SELECT * FROM task_msg").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["send_state"], "recorded")

    def test_bus_send_failure_marks_the_row_and_returns_false(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="tmux1", subject="msg fixture",
                                 check_cmd="true")
            row_id = self.record_current_message(conn, did, "dispatch", f"d2:{did}", "tmux1", "s")

        os.environ["MATRIX_BUS_CFG"] = self.env["MATRIX_BUS_CFG"]
        try:
            ok = wp.bus_send(conn, row_id, timeout=30)
        finally:
            os.environ.pop("MATRIX_BUS_CFG", None)
        self.assertFalse(ok)
        row = conn.execute("SELECT * FROM task_msg WHERE id=?", (row_id,)).fetchone()
        self.assertEqual(row["send_state"], "failed")

    def test_unknown_service_identity_never_joins_from_the_send_path(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="tmux1", subject="missing sender",
                                 check_cmd="true")
            row_id = self.record_current_message(conn, did, "dispatch", f"missing:{did}",
                                   "tmux1", "s")
        missing = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="",
            stderr="agent-bus-v3: unknown local identity: service")
        with mock.patch.object(wp.subprocess, "run", return_value=missing) as run:
            ok = wp.bus_send(conn, row_id)
        self.assertFalse(ok)
        self.assertEqual(run.call_count, 1)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["bash", wp.bus_cli(), "send"])
        self.assertNotIn("join", argv)

    def test_dispatch_process_failure_keeps_task_and_retryable_message(self):
        env = dict(self.env)
        env["NW_BUS_CLI"] = str(Path(self.tmp.name) / "missing-bus-cli")
        out = subprocess.run(
            [sys.executable, ORC, "dispatch", "--no-handshake", "--to",
             "worker", "--subject", "atomic dispatch", "--check", "true"],
            text=True, capture_output=True, env=env,
        )
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        conn = wp.connect_writable()
        task = conn.execute(
            "SELECT id FROM dispatch WHERE subject='atomic dispatch'"
        ).fetchone()
        self.assertIsNotNone(task)
        msg = conn.execute(
            "SELECT target,send_state,attempts FROM task_msg WHERE task_id=?"
            " AND purpose='dispatch'", (task["id"],),
        ).fetchone()
        self.assertEqual(tuple(msg), ("worker", "failed", 1))

    def test_legacy_role_cannot_turn_single_judge_notice_into_broadcast(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="owner", subject="claim",
                                 check_cmd="true")
            wp.claim_open(conn, wp.fetch(conn, did), "ready")
            conn.execute(
                "INSERT INTO role_assignment"
                " (role,agent_id,granted_by,granted_ms) VALUES"
                " ('commander','all','old-version',1)"
            )
            row_id = self.insert_legacy_message(
                conn, did, "claim-notify", "role:commander",
                dedup_key=f"claim:{did}:1", subject="s", body="b",
            )
        with mock.patch.object(wp.subprocess, "run") as transport:
            self.assertFalse(
                wp.bus_send(conn, row_id)
            )
        transport.assert_not_called()
        row = conn.execute(
            "SELECT send_state,last_error FROM task_msg WHERE id=?", (row_id,),
        ).fetchone()
        self.assertEqual(row["send_state"], "invalid-target")
        self.assertIn("broadcast is allowed only", row["last_error"])

    def test_goal_review_role_send_uses_the_goal_team_holder(self):
        conn = wp.connect_writable()
        with conn:
            for seat in ("team-commander", "global-commander"):
                conn.execute(
                    "INSERT INTO seat (agent_id,handle,status,addressable,"
                    " refreshed_ms) VALUES (?,?,?,?,?)",
                    (seat, f"test/{seat}", "active", 1, wp.now()),
                )
            goal = wp.insert_task(
                conn, recipient="role:missing-goal-owner",
                subject="scoped review", workflow="parent",
            )
            conn.execute(
                "INSERT INTO role_assignment (role,agent_id,granted_by,"
                " granted_ms) VALUES ('commander','global-commander','test',?)",
                (wp.now(),),
            )
            conn.execute(
                "INSERT INTO team_member (parent_task_id,agent_id,team_role,"
                " added_by,added_ms) VALUES (?,?,?,?,?)",
                (goal, "team-commander", "commander", "test", wp.now()),
            )
            conn.execute("DROP TRIGGER dispatch_state_legal")
            conn.execute("UPDATE dispatch SET state='ready-to-close' WHERE id=?",
                         (goal,))
            event_id = wp.record(conn, goal, "children-closed", "fixture")
            msg_id = self.record_current_message(
                conn, goal, "goal-review",
                f"goal-review:{goal}:attention-event={event_id}",
                "role:commander", "review", "body",
            )
        accepted = subprocess.CompletedProcess(
            ["matrix-bus", "send"], 0,
            stdout='{"msg_id":"m-team","recipient_agent_ids":'
                   '["team-commander"]}\n', stderr="",
        )
        with mock.patch.object(wp.subprocess, "run", return_value=accepted) as send:
            self.assertTrue(wp.bus_send(conn, msg_id))
        self.assertEqual(send.call_args.args[0][4], "team-commander")


class MigrationTests(StoreTestCase):
    def test_pre_workflow_db_migrates_and_replays(self):

        old = sqlite3.connect(self.env["DISPATCH_LEDGER_DB"])
        old.executescript("""
            CREATE TABLE dispatch (
                id TEXT PRIMARY KEY, created_ms INTEGER NOT NULL,
                created_by TEXT NOT NULL, recipient TEXT NOT NULL,
                subject TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
                check_cmd TEXT NOT NULL DEFAULT '', links TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'open', resolution TEXT NOT NULL DEFAULT '',
                check_after INTEGER NOT NULL, chases INTEGER NOT NULL DEFAULT 0,
                chases_total INTEGER NOT NULL DEFAULT 0, last_event INTEGER NOT NULL);
            CREATE TABLE event (
                id INTEGER PRIMARY KEY AUTOINCREMENT, dispatch_id TEXT NOT NULL,
                at_ms INTEGER NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '');
            CREATE TABLE state_pair (
                from_state TEXT NOT NULL, to_state TEXT NOT NULL,
                PRIMARY KEY (from_state, to_state));
            INSERT INTO dispatch VALUES ('legacy01', 1, 'old', 'tmux3', 'legacy row',
                '', 'true', '', 'acked', '', 2, 0, 0, 2);
            INSERT INTO event (dispatch_id, at_ms, actor, kind, note)
                VALUES ('legacy01', 1, 'old', 'open', ''),
                       ('legacy01', 2, 'old', 'ack', '');
        """)
        old.commit()
        old.close()


        first = subprocess.run([sys.executable, LEDGER, "verify"], text=True,
                               capture_output=True, env=self.env)
        self.assertEqual(first.returncode, 2, first.stdout + first.stderr)
        self.assertIn("schema is not current enough", first.stderr)
        check = sqlite3.connect(self.env["DISPATCH_LEDGER_DB"])
        sp_cols = {r[1] for r in check.execute("PRAGMA table_info(state_pair)")}
        self.assertNotIn("workflow", sp_cols)
        check.close()


        self.run_cli(LEDGER, "note", "legacy01", "--note", "schema upgrade")
        out = self.run_cli(LEDGER, "verify")
        self.assertIn("OK", out.splitlines()[-1])
        rows = self.task_ids()
        self.assertEqual(rows[0]["workflow"], "dispatch")
        self.assertEqual(rows[0]["state"], "acked")

    def test_onboard_old_schema_reports_unknown_and_never_blocks_join(self):
        path = Path(self.env["DISPATCH_LEDGER_DB"])
        conn = sqlite3.connect(path)
        with conn:
            conn.execute(
                "CREATE TABLE dispatch ("
                "id TEXT PRIMARY KEY, recipient TEXT, state TEXT,"
                " created_ms INTEGER, last_event INTEGER, subject TEXT)"
            )
            conn.execute(
                "INSERT INTO dispatch VALUES"
                " ('legacy1','seat-x','open',1,1,'legacy task')"
            )
        conn.close()
        check = sqlite3.connect(path)
        before = tuple(check.iterdump())
        check.close()
        old_bus = self.env["NW_BUS_CLI"]
        self.env["NW_BUS_CLI"] = "/bin/false"
        try:
            out = self.run_cli(ORC, "onboard", "seat-x")
        finally:
            self.env["NW_BUS_CLI"] = old_bus
        self.assertIn("OWED — unknown", out)
        self.assertIn("ROLES — unknown", out)
        self.assertIn("not an empty queue", out)
        check = sqlite3.connect(path)
        after = tuple(check.iterdump())
        check.close()
        self.assertEqual(after, before)

    def test_recipient_versions_migrate_without_silencing_old_tasks(self):
        old = sqlite3.connect(self.env["DISPATCH_LEDGER_DB"])
        old.executescript("""
            CREATE TABLE dispatch (
                id TEXT PRIMARY KEY, created_ms INTEGER NOT NULL,
                created_by TEXT NOT NULL, recipient TEXT NOT NULL,
                subject TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
                check_cmd TEXT NOT NULL DEFAULT '', links TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'open', resolution TEXT NOT NULL DEFAULT '',
                check_after INTEGER NOT NULL, chases INTEGER NOT NULL DEFAULT 0,
                chases_total INTEGER NOT NULL DEFAULT 0, last_event INTEGER NOT NULL);
            CREATE TABLE event (
                id INTEGER PRIMARY KEY AUTOINCREMENT, dispatch_id TEXT NOT NULL,
                at_ms INTEGER NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '');
            CREATE TABLE task_msg (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                dedup_key TEXT NOT NULL UNIQUE, purpose TEXT NOT NULL,
                target TEXT NOT NULL, subject TEXT NOT NULL DEFAULT '',
                at_ms INTEGER NOT NULL, msg_id TEXT NOT NULL DEFAULT '',
                recipient_agent_id TEXT NOT NULL DEFAULT '',
                send_state TEXT NOT NULL DEFAULT 'recorded',
                delivered INTEGER NOT NULL DEFAULT 0,
                processed TEXT NOT NULL DEFAULT '', poll_count INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
                escalated_to_operator INTEGER NOT NULL DEFAULT 0);
            INSERT INTO dispatch VALUES
                ('offline1',1,'old','tmux3','out of band','','true','','open','',2,0,0,1),
                ('legacymsg',1,'old','seat-b','old reassignment','','true','','open','',2,0,0,1);
            INSERT INTO event(dispatch_id,at_ms,actor,kind,note) VALUES
                ('offline1',1,'old','open',''),
                ('legacymsg',1,'old','open','');
            INSERT INTO task_msg
                (task_id,dedup_key,purpose,target,subject,at_ms,msg_id,
                 recipient_agent_id,send_state,attempts,last_error,body)
            VALUES
                ('legacymsg','old-a','dispatch','seat-a','s',1,'',
                 '','failed',1,'old failure','b'),
                ('legacymsg','new-b','reassign-notify','seat-b','s',2,'m-b',
                 'seat-b','accepted',1,'','b');
        """)
        old.commit()
        old.close()

        conn = wp.connect_writable()
        versions = conn.execute(
            "SELECT id,responsibility_version FROM dispatch ORDER BY id"
        ).fetchall()
        self.assertEqual([(r["id"], r["responsibility_version"])
                          for r in versions],
                         [("legacymsg", 0), ("offline1", 0)])
        msg_versions = conn.execute(
            "SELECT recipient_version FROM task_msg ORDER BY id"
        ).fetchall()
        self.assertEqual([r[0] for r in msg_versions], [0, 0])
        marker = conn.execute(
            "SELECT status FROM schema_migration"
            " WHERE name='responsibility-versions-v1'"
        ).fetchone()
        self.assertEqual(marker["status"], "done")

        offline = wp.resolve_owed_recipient(conn, wp.fetch(conn, "offline1"))
        self.assertNotIn("deferred", offline)
        self.assertEqual(offline["window"], "3")
        self.assertFalse(wp.dispatch_undelivered(conn, "offline1"))

        current = wp.resolve_owed_recipient(conn, wp.fetch(conn, "legacymsg"))
        self.assertEqual(current["seat"], "seat-b")
        self.assertEqual(wp.repair_missing_responsibility_messages(
            conn, log=lambda *_: None), 0)
        with conn:
            conn.execute("UPDATE task_msg SET at_ms=? WHERE dedup_key='old-a'",
                         (wp.now(),))
        with mock.patch.object(wp, "bus_send") as send:
            self.assertEqual(wp.retry_unsent(conn), (0, 0))
        send.assert_not_called()
        with conn:
            conn.execute("UPDATE task_msg SET attempts=? WHERE dedup_key='old-a'",
                         (wp.MAX_SEND_ATTEMPTS,))
        self.assertEqual(wp.dead_letters(conn), [])
        conn.close()
        again = wp.connect_writable()
        self.assertEqual(
            again.execute("SELECT responsibility_version FROM dispatch"
                          " WHERE id='legacymsg'").fetchone()[0],
            0,
            "the completed migration must be idempotent",
        )
        self.assertEqual(wp.repair_missing_responsibility_messages(
            again, log=lambda *_: None), 0)

    def test_existing_version_columns_without_marker_migrate_once(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="seat-a",
                                 subject="interrupted migration",
                                 check_cmd="true")
            conn.execute("UPDATE dispatch SET responsibility_version=2"
                         " WHERE id=?", (did,))
            msg = self.record_current_message(conn, did, "dispatch", f"dispatch:{did}",
                                "seat-a", "s", "b")
            conn.execute("DELETE FROM schema_migration WHERE name="
                         " 'responsibility-versions-v1'")
        conn.close()
        repaired = wp.connect_writable()
        self.assertEqual(wp.fetch(repaired, did)["responsibility_version"], 2)
        self.assertEqual(repaired.execute(
            "SELECT status FROM schema_migration WHERE name="
            " 'responsibility-versions-v1'"
        ).fetchone()[0], "done")
        self.assertEqual(repaired.execute(
            "SELECT recipient_version FROM task_msg WHERE id=?", (msg,)
        ).fetchone()[0], 2)
        repaired.close()
        again = wp.connect_writable()
        self.assertEqual(wp.fetch(again, did)["responsibility_version"], 2)
        again.close()

    def test_upgrade_keeps_current_merge_receipt_retryable(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(
                conn, recipient="owner", subject="merge notice at upgrade",
                workflow="pr", repo="example-storage", owner_seat="owner",
                reviewer_seat="reviewer",
            )
            for state in ("awaiting-review", "receipt-due", "merge-pending"):
                conn.execute("UPDATE dispatch SET state=? WHERE id=?",
                             (state, did))
            conn.execute(
                "INSERT INTO seat(agent_id,handle,status,addressable,refreshed_ms)"
                " VALUES ('line-owner','test/line-owner','active',1,?)",
                (wp.now(),),
            )
            conn.execute(
                "INSERT INTO role_assignment(role,agent_id,granted_by,granted_ms)"
                " VALUES ('line-owner-of-example-storage','line-owner','test',?)",
                (wp.now(),),
            )
            receipt_event = wp.record(
                conn, did, "receipt", "current receipt")
            row = wp.fetch(conn, did)
            current_version = int(row["responsibility_version"])
            stale = self.record_current_message(
                conn, did, "receipt-to-keyholder",
                f"receipt-key:{did}:old:attention-event={receipt_event}",
                "role:line-owner-of-example-storage", "old", "old",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='failed',recipient_version=?"
                " WHERE id=?", (current_version - 1, stale),
            )
            current = self.record_current_message(
                conn, did, "receipt-to-keyholder",
                f"receipt-key:{did}:n1:attention-event={receipt_event}",
                "role:line-owner-of-example-storage", "s", "b",
            )
            conn.execute("UPDATE task_msg SET send_state='failed' WHERE id=?",
                         (current,))
            conn.execute("DELETE FROM schema_migration WHERE name="
                         " 'responsibility-versions-v1'")
        self.member_source(conn)
        conn.close()

        upgraded = wp.connect_writable()
        row = wp.fetch(upgraded, did)
        self.assertEqual(row["responsibility_version"], current_version,
                         "merge-pending has no owner/reviewer duty to remap")
        with mock.patch.object(wp, "bus_send", return_value=True) as send:
            self.assertEqual(wp.retry_unsent(upgraded, log=lambda *_: None),
                             (1, 0))
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[1], current)
        upgraded.close()

    def test_upgrade_keeps_current_review_desync_retryable(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(
                conn, recipient="owner", subject="review chase at upgrade",
                workflow="pr", repo="example-app", owner_seat="owner",
                reviewer_seat="reviewer",
            )
            conn.execute("UPDATE dispatch SET state='awaiting-review' WHERE id=?",
                         (did,))
            duty = self.record_current_message(
                conn, did, "review-request", f"review:{did}",
                "reviewer", "review", "review",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-review',"
                " recipient_agent_id='reviewer' WHERE id=?", (duty,),
            )
            old = self.record_current_message(
                conn, did, "review-desync", f"reconcile:{did}:head-1",
                "reviewer", "s", "b",
            )
            conn.execute("UPDATE task_msg SET send_state='failed' WHERE id=?",
                         (old,))
            conn.execute("DELETE FROM schema_migration WHERE name="
                         " 'responsibility-versions-v1'")
        conn.close()

        upgraded = wp.connect_writable()
        row = wp.fetch(upgraded, did)
        old_msg = upgraded.execute("SELECT * FROM task_msg WHERE id=?",
                                   (old,)).fetchone()
        self.assertTrue(
            wp.message_is_current_responsibility(upgraded, old_msg, row),
        )
        with mock.patch.object(wp, "bus_send", return_value=True) as send:
            self.assertEqual(wp.retry_unsent(upgraded, log=lambda *_: None),
                             (1, 0))
        self.assertEqual(send.call_args.args[1], old)
        upgraded.close()

    def test_legacy_cache_refusal_reopens_but_deliberate_refusal_stays(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="worker-short",
                                 subject="recover cached refusal",
                                 check_cmd="true")
            recover = self.record_current_message(conn, did, "dispatch", f"dispatch:{did}",
                                    "worker-short", "s", "b")
            wp.refuse_recorded_target(
                conn, recover,
                "recipient 'worker-short' is registered but not addressable",
            )
            terminal = self.record_current_message(conn, did, "terminal", f"terminal:{did}",
                                     "expired-requester", "s", "b")
            wp.refuse_recorded_target(
                conn, terminal,
                "terminal requester is not an active, unexpired, addressable"
                " Agent Bus identity in the local database; transport skipped",
            )
            conn.execute("DELETE FROM schema_migration WHERE name="
                         " 'cache-refusal-retry-v1'")
        conn.close()
        migrated = wp.connect_writable()
        states = {r["id"]: r["send_state"] for r in migrated.execute(
            "SELECT id,send_state FROM task_msg WHERE id IN (?,?)",
            (recover, terminal),
        )}
        self.assertEqual(states[recover], "recorded")
        self.assertEqual(states[terminal], "invalid-target")
        accepted = subprocess.CompletedProcess(
            ["matrix-bus", "send"], 0,
            stdout='{"msg_id":"m-recovered","recipient_agent_ids":["seat-b"]}\n',
            stderr="",
        )
        with mock.patch.object(wp.subprocess, "run",
                               return_value=accepted) as send:
            self.assertEqual(wp.retry_unsent(migrated), (1, 0))
        self.assertEqual(send.call_count, 1)
        row = migrated.execute(
            "SELECT send_state,recipient_agent_id FROM task_msg WHERE id=?",
            (recover,),
        ).fetchone()
        self.assertEqual(tuple(row), ("accepted", "seat-b"))

    def test_pre_addressability_seat_cache_is_deleted_not_misclassified(self):
        old = sqlite3.connect(self.env["DISPATCH_LEDGER_DB"])
        old.execute(
            "CREATE TABLE seat(agent_id TEXT PRIMARY KEY,handle TEXT NOT NULL,"
            " aliases TEXT NOT NULL DEFAULT '',host TEXT NOT NULL DEFAULT '',"
            " tmux TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT '',"
            " updated_at TEXT NOT NULL DEFAULT '',refreshed_ms INTEGER NOT NULL)"
        )
        old.execute(
            "INSERT INTO seat VALUES"
            " ('model-id','example-host/model-tmux7','example-host/model-old','host','',"
            "  'active','old',123)"
        )
        old.commit()
        old.close()

        conn = wp.connect_writable()
        self.assertIn(
            "addressable",
            {row[1] for row in conn.execute("PRAGMA table_info(seat)")},
        )
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM seat").fetchone()[0], 0)

        with conn:
            did = wp.insert_task(
                conn, recipient="example-host/model-old", subject="migration cache",
                check_cmd="true",
            )
        failed = subprocess.CompletedProcess(
            ["matrix-bus", "send"], 1, stdout="",
            stderr="agent-bus-v3: registry temporarily unavailable",
        )
        with mock.patch.object(wp.subprocess, "run", return_value=failed):
            self.assertFalse(self.route_current(
                conn, did, "dispatch", f"dispatch:{did}",
                "example-host/model-old", "subject", "body",
            ))
        row = conn.execute(
            "SELECT send_state,attempts FROM task_msg WHERE task_id=?", (did,),
        ).fetchone()
        self.assertEqual((row["send_state"], row["attempts"]), ("failed", 1))

        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,aliases,host,tmux,status,"
                " addressable,updated_at,refreshed_ms)"
                " VALUES ('model-id','example-host/model-tmux7','example-host/model-old',"
                " 'host','','active',1,'new',456)"
            )
        accepted = subprocess.CompletedProcess(
            ["matrix-bus", "send"], 0,
            stdout='{"msg_id":"migration-retry"}\n', stderr="",
        )
        with mock.patch.object(wp.subprocess, "run", return_value=accepted):
            self.assertEqual(wp.retry_unsent(conn), (1, 0))
        row = conn.execute(
            "SELECT send_state,attempts FROM task_msg WHERE task_id=?", (did,),
        ).fetchone()
        self.assertEqual((row["send_state"], row["attempts"]), ("accepted", 2))

    def test_old_member_cache_is_ignored_readonly_and_removed_on_write(self):
        old = sqlite3.connect(self.env["DISPATCH_LEDGER_DB"])
        old.execute(
            "CREATE TABLE seat(agent_id TEXT PRIMARY KEY,handle TEXT NOT NULL,"
            " aliases TEXT NOT NULL DEFAULT '',host TEXT NOT NULL DEFAULT '',"
            " tmux TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT '',"
            " addressable INTEGER NOT NULL DEFAULT 0,"
            " updated_at TEXT NOT NULL DEFAULT '',refreshed_ms INTEGER NOT NULL)"
        )
        old.execute(
            "INSERT INTO seat VALUES"
            " ('stale-id','host/stale','','host','','active',0,'old',1)"
        )
        old.commit()
        old.close()

        read_only = wp.connect_readonly()
        self.assertEqual(read_only.execute(
            "SELECT COUNT(*) FROM seat").fetchone()[0], 0)
        self.assertEqual(read_only.execute(
            "SELECT COUNT(*) FROM main.seat").fetchone()[0], 1)
        self.assertIsNone(read_only.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table'"
            " AND name='schema_migration'").fetchone())
        read_only.close()

        repaired = wp.connect_writable()
        self.assertEqual(repaired.execute(
            "SELECT COUNT(*) FROM seat").fetchone()[0], 0)
        self.assertIsNone(repaired.execute(
            "SELECT name FROM main.sqlite_master WHERE name='seat'").fetchone())
        repaired.close()
        again = wp.connect_writable()
        self.assertEqual(again.execute(
            "SELECT COUNT(*) FROM seat").fetchone()[0], 0)


class ReviewHotfixTests(StoreTestCase):


    def test_remote_seat_window_is_never_trusted_locally(self):


        import socket as _socket
        conn = wp.connect_writable()
        local_host = _socket.gethostname().split(".", 1)[0]
        with conn:
            conn.execute("INSERT INTO seat (agent_id, handle, aliases, host, tmux,"
                         " status, addressable, updated_at, refreshed_ms)"
                         " VALUES ('remote-1','vm2/worker-a','','some-other-box',"
                         "'tmux=0:7.0 win=codex','active',1,'',0)")
            conn.execute("INSERT INTO seat (agent_id, handle, aliases, host, tmux,"
                         " status, addressable, updated_at, refreshed_ms)"
                         " VALUES ('local-1','example-host/worker-b','',?,"
                         "'tmux=0:8.0 win=claude','active',1,'',0)", (local_host,))
        remote = wp.resolve_recipient(conn, "vm2/worker-a")
        self.assertIsNone(remote["window"])
        self.assertEqual(remote["agent_id"], "remote-1")
        local = wp.resolve_recipient(conn, "example-host/worker-b")
        self.assertEqual(local["window"], "8")

    def test_failed_progress_command_is_unknown_not_progress(self):
        verdict, digest = wp.run_progress("echo transient outage >&2; exit 3")
        self.assertEqual(verdict, wp.GUARD_UNKNOWN)
        self.assertEqual(digest, "")

    def test_ask_flag_expires_and_clears_on_transition(self):
        self.run_cli(ORC, "open", "--to", "tmux1", "--subject", "flag pr",
                     "--workflow", "pr", "--repo", "example-storage",
                     "--owner", "tmux1", "--reviewer", "tmux2",
                     "--ready-cmd", "true", "--done-cmd", "false",
                     "--check", "echo h1")
        pr_id = self.task_ids()[0]["id"]
        self.env["ORC_SEAT_ID"] = "tmux1"
        self.run_cli(ORC, "blocked", pr_id,
                     "--note", "waiting on the operator to pick a base branch")
        conn = wp.connect_writable()
        flag = conn.execute("SELECT ask_flag FROM dispatch WHERE id=?",
                            (pr_id,)).fetchone()[0]
        self.assertGreater(flag, 1)
        self.assertLessEqual(wp.now() - flag, wp.ASK_FLAG_TTL_S)

        self.assertGreater(wp.now() - 1, wp.ASK_FLAG_TTL_S)


        self.run_cli(ORC, "tick")
        conn = wp.connect_writable()
        flag = conn.execute("SELECT ask_flag FROM dispatch WHERE id=?",
                            (pr_id,)).fetchone()[0]
        self.assertEqual(flag, 0)

    def test_blocked_task_appears_in_brief_and_statusline_until_expiry(self):
        question = "choose the base branch before implementation continues"
        self.run_cli(ORC, "open", "--to", "tmux1", "--subject",
                     "needs a human choice", "--check", "true")
        task_id = self.task_ids()[0]["id"]
        self.env["ORC_SEAT_ID"] = "tmux1"
        self.run_cli(ORC, "blocked", task_id, "--note", question)

        brief = self.run_cli(ORC, "brief")
        self.assertIn(task_id, brief)
        self.assertIn(question, brief)
        statusline = self.run_cli(ORC, "statusline", "--no-color")
        self.assertIn("operator 1", statusline)

        conn = wp.connect_writable()
        with conn:
            conn.execute("UPDATE dispatch SET ask_flag=? WHERE id=?",
                         (wp.now() - wp.ASK_FLAG_TTL_S - 1, task_id))
        self.assertIn("nothing is waiting on the operator",
                      self.run_cli(ORC, "brief"))
        statusline = self.run_cli(ORC, "statusline", "--no-color")
        self.assertIn("operator 0", statusline)

    def test_blocked_task_with_active_requester_does_not_duplicate_operator_wait(self):
        conn = wp.connect_writable()
        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,status,addressable,"
                " refreshed_ms) VALUES ('requester','test/requester',"
                " 'active',1,?)", (wp.now(),),
            )
            did = wp.insert_task(
                conn, recipient="worker", requester_seat="requester",
                subject="requester can decide", check_cmd="true",
            )
            conn.execute("UPDATE dispatch SET ask_flag=? WHERE id=?",
                         (wp.now(), did))
            self.record_current_voice(
                conn, did, "note",
                f"{wp.ASK_NOTE_PREFIX}requester chooses the input")
        row = wp.fetch(conn, did)
        self.assertEqual(wp.attention_recipient(conn, row), "requester")
        self.assertFalse(wp.waits_on_operator(conn, row))
        self.member_source(conn)
        self.assertNotIn(did, self.run_cli(ORC, "brief"))

    def test_responsibility_moves_clear_the_human_question(self):
        actions = {
            "ack": ("ack", "--note", "accepted"),
            "chase": ("chase", "--note", "resume the normal reminders"),
            "reassign": ("reassign", "--to", "tmux2", "--note",
                         "new owner"),
            "close": ("close", "--resolution", "done"),
        }
        for name, action in actions.items():
            with self.subTest(action=name):
                self.run_cli(ORC, "open", "--to", "tmux1", "--subject",
                             f"clear ask on {name}", "--check", "true")
                task_id = next(row["id"] for row in self.task_ids()
                               if row["subject"] == f"clear ask on {name}")
                self.env["ORC_SEAT_ID"] = "tmux1"
                self.run_cli(ORC, "blocked", task_id, "--note",
                             "choose before responsibility moves")
                self.run_cli(ORC, action[0], task_id, *action[1:])
                conn = wp.connect_writable()
                flag = conn.execute(
                    "SELECT ask_flag FROM dispatch WHERE id=?", (task_id,),
                ).fetchone()[0]
                self.assertEqual(flag, 0)

    def test_routing_dedup_is_per_task_and_per_occurrence(self):

        for n in (1, 2):
            self.run_cli(ORC, "open", "--to", f"tmux{n}", "--subject", f"pr {n}",
                         "--workflow", "pr", "--repo", "example-storage",
                         "--owner", f"tmux{n}", "--reviewer", "tmux9",
                         "--ready-cmd", "true", "--done-cmd", "false",
                         "--check", "echo h1")
        self.run_cli(ORC, "tick")
        conn = wp.connect_writable()
        reqs = conn.execute("SELECT COUNT(*) FROM task_msg WHERE"
                            " purpose='review-request'").fetchone()[0]
        self.assertEqual(reqs, 2)


        pr_id = self.task_ids()[0]["id"]
        conn = wp.connect_writable()
        with conn:
            self.accept_current_responsibility(
                conn, pr_id, actual="tmux9", pane="%9")
        conn.close()
        self.env["ORC_SEAT_ID"] = "tmux9"
        self.run_cli(ORC, "verdict", pr_id, "clean", "--note",
                     "checked per the walk fixture")
        conn = wp.connect_writable()
        with conn:
            owner = wp.owed_party(wp.fetch(conn, pr_id))
            self.accept_current_responsibility(
                conn, pr_id, actual=owner, pane="%1")
        conn.close()
        self.env["ORC_SEAT_ID"] = owner
        receipt = Path(self.tmp.name) / "r.md"
        receipt.write_text("receipt one\n")
        self.run_cli(ORC, "receipt", pr_id, "--body-file", str(receipt))
        conn = wp.connect_writable()
        with conn:
            conn.execute("UPDATE dispatch SET state='awaiting-review'"
                         " WHERE id=?", (pr_id,))
            wp.record(conn, pr_id, "head-moved", "test")
            self.accept_current_responsibility(
                conn, pr_id, actual="tmux9", pane="%9")
        self.env["ORC_SEAT_ID"] = "tmux9"
        self.run_cli(ORC, "verdict", pr_id, "clean", "--note",
                     "checked per the walk fixture")
        conn = wp.connect_writable()
        reqs = conn.execute("SELECT COUNT(*) FROM task_msg WHERE"
                            " purpose='receipt-request' AND task_id=?",
                            (pr_id,)).fetchone()[0]
        self.assertEqual(reqs, 2)

    def test_route_refuses_to_run_inside_a_transaction(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="tmux1", subject="txn fixture",
                                 check_cmd="true")
        conn.execute("BEGIN")
        try:
            with self.assertRaises(SystemExit):
                self.route_current(conn, did, "dispatch", f"txn:{did}", "tmux1", "s", "b")
        finally:
            conn.rollback()

    def test_drive_prune_keys_on_task_liveness_not_seat_string(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="tmux1", subject="prune fixture",
                                 check_cmd="true")
            self.accept_current_responsibility(
                conn, did, actual="old-seat-key", pane="%7")
            self.set_current_drive(
                conn, did, state=wp.S_ESCALATED, cycles=3)
        env = dict(self.env)
        env["NW_TMUX_SERVER"] = "nw-test-none"
        out = subprocess.run([sys.executable, ORC, "tick"], text=True,
                             capture_output=True, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        conn = wp.connect_writable()
        kept = conn.execute("SELECT * FROM drive WHERE task_id=? AND"
                            " seat='old-seat-key'", (did,)).fetchone()
        self.assertIsNotNone(kept)
        self.assertEqual(kept["st"], wp.S_ESCALATED)


class SendLegTests(StoreTestCase):


    LIVE_SHAPED_SEATS = [
        ("a-w5", "example-host/worker-5-tmux5", "example-host/example-app-w5-tmux5"),
        ("a-w7", "example-host/worker-7-tmux7", "example-host/example-app-w7-tmux7"),
        ("a-perf", "example-host/storage-worker-tmux15", "example-host/example-storage-tmux15"),
        ("a-cr17", "example-host/code-review-17-tmux17", "example-host/agent-bus-join-tmux17"),
        ("a-fc", "example-host/fleet-command-tmux1", "example-host/oom-incident-recovery-tmux1"),
    ]

    def test_delayed_old_route_cannot_shadow_newer_delivered_recipient(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="seat-a", subject="race",
                                 check_cmd="true")
            conn.execute("UPDATE dispatch SET recipient='seat-b' WHERE id=?",
                         (did,))
            conn.execute("UPDATE dispatch SET recipient='seat-c' WHERE id=?",
                         (did,))
            current = self.record_current_message(
                conn, did, "reassign-notify", f"reassign:{did}:c",
                "seat-c", "current", "current",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-c',"
                " recipient_agent_id='seat-c' WHERE id=?", (current,),
            )
            stale = self.record_current_message(
                conn, did, "reassign-notify", f"reassign:{did}:b-late",
                "seat-b", "late", "late",
            )
        self.assertIsNone(stale)
        row = wp.fetch(conn, did)
        msg = conn.execute("SELECT * FROM task_msg WHERE id=?", (current,)).fetchone()
        self.assertTrue(wp.message_is_current_responsibility(conn, msg, row))
        self.assertFalse(wp.dispatch_undelivered(conn, did))

    def test_delayed_first_a_route_cannot_revive_after_a_to_b_to_a(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="seat-a", subject="ABA race",
                                 check_cmd="true")
            first_version = wp.fetch(conn, did)["responsibility_version"]
            conn.execute("UPDATE dispatch SET recipient='seat-b' WHERE id=?",
                         (did,))
            conn.execute("UPDATE dispatch SET recipient='seat-a' WHERE id=?",
                         (did,))
            current = self.record_current_message(
                conn, did, "reassign-notify", f"reassign:{did}:a2",
                "seat-a", "current A", "current A",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-a2',"
                " recipient_agent_id='seat-a' WHERE id=?", (current,),
            )
            stale = wp.record_msg(
                conn, did, "reassign-notify", f"reassign:{did}:a1-late",
                "seat-a", "old A", "old A",
                expected_responsibility_version=first_version,
            )
        self.assertIsNone(stale)
        row = wp.fetch(conn, did)
        self.assertEqual(row["responsibility_version"], first_version + 2)
        message = conn.execute(
            "SELECT * FROM task_msg WHERE id=?", (current,),
        ).fetchone()
        self.assertTrue(wp.message_is_current_responsibility(
            conn, message, row))
        self.assertFalse(wp.dispatch_undelivered(conn, did))

    def test_delayed_old_goal_review_cannot_shadow_newer_notice(self):
        conn = wp.connect_writable()
        with conn:
            for seat in ("goal-b", "goal-c"):
                conn.execute(
                    "INSERT INTO seat(agent_id,handle,status,addressable,"
                    " refreshed_ms) VALUES (?,?,?,?,?)",
                    (seat, f"test/{seat}", "active", 1, wp.now()),
                )
            did = wp.insert_task(
                conn, recipient="goal-b", subject="goal", workflow="parent")
            event_id = wp.record(conn, did, "children-closed", "ready")
            conn.execute("UPDATE dispatch SET state='ready-to-close',"
                         " recipient='goal-c' WHERE id=?", (did,))
            base = f"goal-review:{did}:attention-event={event_id}"
            current = self.record_current_message(
                conn, did, "goal-review", f"{base}:to:goal-c",
                "goal-c", "current", "current",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-goal-c',"
                " recipient_agent_id='goal-c' WHERE id=?", (current,),
            )
            stale = self.record_current_message(
                conn, did, "goal-review", f"{base}:to:goal-b-late",
                "goal-b", "late", "late",
            )
        self.assertIsNone(stale)
        row = wp.fetch(conn, did)
        msg = conn.execute("SELECT * FROM task_msg WHERE id=?", (current,)).fetchone()
        self.assertTrue(wp.message_is_current_responsibility(conn, msg, row))

    def seed_seats(self):
        conn = wp.connect_writable()
        with conn:
            for aid, handle, alias in self.LIVE_SHAPED_SEATS:
                conn.execute("INSERT INTO seat (agent_id, handle, aliases, host,"
                             " tmux, status, addressable, updated_at, refreshed_ms)"
                             " VALUES (?,?,?,'otherhost','','active',1,'',0)",
                             (aid, handle, alias))
        return conn

    def test_live_short_names_resolve_uniquely(self):
        conn = self.seed_seats()
        self.assertEqual(wp.resolve_recipient(conn, "worker-7")["agent_id"], "a-w7")
        self.assertEqual(
            wp.resolve_recipient(conn, "storage-worker")["agent_id"], "a-perf")
        self.assertEqual(wp.resolve_recipient(conn, "fleet-command")["agent_id"], "a-fc")

        self.assertEqual(wp.resolve_recipient(conn, "tmux7")["agent_id"], "a-w7")

    def test_failed_message_at_retry_deadline_moves_to_operator(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="worker-7",
                                 subject="expired retry", check_cmd="true")
            msg_id = self.record_current_message(
                conn, did, "dispatch", f"dispatch:{did}", "worker-7",
                "expired retry", "body",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='failed',attempts=1,"
                " at_ms=?,last_error='transport unavailable' WHERE id=?",
                (wp.now() - wp.SEND_RETRY_WINDOW_S - 1, msg_id),
            )

        with mock.patch.object(wp, "bus_send") as send:
            self.assertEqual(wp.retry_unsent(conn), (0, 0))
        send.assert_not_called()
        self.assertEqual([row["id"] for row in wp.dead_letters(conn)],
                         [msg_id])
        self.assertEqual(wp.escalate_dead_letters(
            conn, log=lambda *_: None), 1)
        self.assertEqual(wp.dead_letters(conn), [])
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM dispatch",
        ).fetchone()[0], 1, "the failed send stays on its original task")
        msg = conn.execute("SELECT escalated_to_operator FROM task_msg"
                           " WHERE id=?", (msg_id,)).fetchone()
        self.assertEqual(msg["escalated_to_operator"], 1)
        self.assertTrue(wp.waits_on_operator(conn, wp.fetch(conn, did)))
        brief = self.run_cli(LEDGER, "brief")
        self.assertIn(did, brief)
        self.assertIn("transport unavailable", brief)

    def test_exact_retry_deadline_has_no_gap(self):
        stamp = 1_800_000_000
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="worker-7",
                                 subject="exact retry boundary",
                                 check_cmd="true")
            msg_id = self.record_current_message(
                conn, did, "dispatch", f"dispatch:{did}", "worker-7",
                "exact retry boundary", "body",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='failed',attempts=1,at_ms=?"
                " WHERE id=?", (stamp - wp.SEND_RETRY_WINDOW_S, msg_id),
            )

        with mock.patch.object(wp, "now", return_value=stamp), \
                mock.patch.object(wp, "bus_send") as send:
            self.assertEqual(wp.retry_unsent(conn), (0, 0))
            self.assertEqual([row["id"] for row in wp.dead_letters(conn)],
                             [msg_id])
        send.assert_not_called()

    def test_transport_keeps_exact_and_short_names_for_live_bus_resolution(self):
        conn = self.seed_seats()
        exact_alias = "example-host/example-app-w7-tmux7"
        completed = subprocess.CompletedProcess(
            ["matrix-bus", "send"], 0,
            stdout='{"msg_id":"transport-target-test"}\n', stderr="",
        )
        sent_targets = []

        def capture(argv, **_kwargs):
            sent_targets.append(argv[4])
            return completed

        with mock.patch.object(wp.subprocess, "run", side_effect=capture):
            with conn:
                exact_task = wp.insert_task(
                    conn, recipient=exact_alias, subject="exact alias",
                    check_cmd="true",
                )
            self.assertTrue(self.route_current(
                conn, exact_task, "dispatch", f"dispatch:{exact_task}",
                exact_alias, "subject", "body",
            ))
            with conn:
                short_task = wp.insert_task(
                    conn, recipient="worker-7", subject="short name",
                    check_cmd="true",
                )
            self.assertTrue(self.route_current(
                conn, short_task, "dispatch", f"dispatch:{short_task}",
                "worker-7", "subject", "body",
            ))
        self.assertEqual(sent_targets, [exact_alias, "worker-7"])
        recorded = [r["target"] for r in conn.execute(
            "SELECT target FROM task_msg ORDER BY id"
        )]
        self.assertEqual(recorded, [exact_alias, "worker-7"],
                         "requested names remain audit evidence")

    def test_short_name_delivery_survives_later_cache_reassignment(self):
        conn = wp.connect_writable()
        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,aliases,host,tmux,status,"
                " addressable,updated_at,refreshed_ms) VALUES"
                " ('seat-b','host/worker-short','','otherhost','',"
                " 'active',1,'old',1)"
            )
            did = wp.insert_task(
                conn, recipient="worker-short", subject="short delivery",
                check_cmd="true",
            )
        accepted = subprocess.CompletedProcess(
            ["matrix-bus", "send"], 0,
            stdout=("{\"msg_id\":\"m-short\","
                    "\"recipient_agent_ids\":[\"seat-b\"]}\n"),
            stderr="",
        )
        with mock.patch.object(wp.subprocess, "run", return_value=accepted):
            self.assertTrue(self.route_current(
                conn, did, "dispatch", f"dispatch:{did}", "worker-short",
                "subject", "body",
            ))
        sent = conn.execute(
            "SELECT target,recipient_agent_id FROM task_msg WHERE task_id=?",
            (did,),
        ).fetchone()
        self.assertEqual((sent["target"], sent["recipient_agent_id"]),
                         ("worker-short", "seat-b"))

        with conn:
            conn.execute("DELETE FROM seat")
            conn.execute(
                "INSERT INTO seat (agent_id,handle,aliases,host,tmux,status,"
                " addressable,updated_at,refreshed_ms) VALUES"
                " ('seat-a','host/worker-short','',?,'tmux=0:7.0 win=model',"
                " 'active',1,'new',2)",
                (socket.gethostname().split('.', 1)[0],),
            )
        actual = wp.resolve_owed_recipient(conn, wp.fetch(conn, did))
        self.assertEqual((actual["seat"], actual["window"]), ("seat-b", None))

    def test_legacy_rewritten_short_target_cannot_choose_a_current_pane(self):
        conn = wp.connect_writable()
        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,aliases,host,tmux,status,"
                " addressable,updated_at,refreshed_ms) VALUES"
                " ('seat-a','host/worker-short','',?,'tmux=0:7.0 win=model',"
                " 'active',1,'new',2)",
                (socket.gethostname().split('.', 1)[0],),
            )
            did = wp.insert_task(
                conn, recipient="worker-short", subject="legacy short row",
                check_cmd="true",
            )
            row_id = self.insert_legacy_message(
                conn, did, "dispatch", "seat-b",
                dedup_key=f"dispatch:{did}", subject="subject", body="body",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-old-short'"
                " WHERE id=?", (row_id,),
            )
        bus = sqlite3.connect(self.env["AGENT_BUS_DB"])
        with bus:
            bus.execute("CREATE TABLE outbox_recipients"
                        " (msg_id TEXT,recipient_agent_id TEXT)")
            bus.execute("INSERT INTO outbox_recipients VALUES"
                        " ('m-old-short','seat-b')")
        bus.close()
        actual = wp.resolve_owed_recipient(conn, wp.fetch(conn, did))
        self.assertEqual((actual["seat"], actual["window"]),
                         ("worker-short", None))
        self.assertIn("deferred", actual)

    def test_reassign_commit_without_message_never_reuses_old_recipient(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="worker-old",
                                 subject="reassign crash window",
                                 check_cmd="true")
            old = self.record_current_message(conn, did, "dispatch", f"dispatch:{did}",
                                "worker-old", "s", "b")
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-old',"
                " recipient_agent_id='seat-old' WHERE id=?", (old,),
            )
            conn.execute("UPDATE dispatch SET recipient='worker-new' WHERE id=?",
                         (did,))
        actual = wp.resolve_owed_recipient(conn, wp.fetch(conn, did))
        self.assertEqual(actual["seat"], "worker-new")
        self.assertIsNone(actual["window"])
        self.assertIn("deferred", actual)
        self.assertTrue(wp.dispatch_undelivered(conn, did))

    def test_wrong_target_in_current_version_does_not_block_message_repair(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="seat-a",
                                 subject="wrong target repair",
                                 check_cmd="true")
            conn.execute("UPDATE dispatch SET recipient='seat-b' WHERE id=?",
                         (did,))
            self.insert_legacy_message(
                conn, did, "reassign-notify", "seat-a",
                dedup_key=f"wrong:{did}", subject="s", body="b")
        self.assertEqual(wp.fetch(conn, did)["responsibility_version"], 1)
        self.assertEqual(wp.repair_missing_responsibility_messages(
            conn, log=lambda *_: None), 1)
        rows = conn.execute(
            "SELECT target,recipient_version FROM task_msg WHERE task_id=?"
            " ORDER BY id", (did,),
        ).fetchall()
        self.assertEqual([tuple(r) for r in rows],
                         [("seat-a", 1), ("seat-b", 1)])

    def test_first_pr_review_message_gap_never_uses_reviewer_cache(self):
        conn = wp.connect_writable()
        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,aliases,host,tmux,status,"
                " addressable,updated_at,refreshed_ms) VALUES"
                " ('reviewer-id','host/reviewer','','%s',"
                " 'tmux=0:8.0 win=model','active',1,'',0)"
                % socket.gethostname().split('.', 1)[0]
            )
            did = wp.insert_task(
                conn, recipient="owner", subject="first review crash window",
                workflow="pr", repo="example-storage", owner_seat="owner",
                reviewer_seat="host/reviewer", check_cmd="echo h",
            )
            conn.execute("UPDATE dispatch SET state='awaiting-review'"
                         " WHERE id=?", (did,))
        actual = wp.resolve_owed_recipient(conn, wp.fetch(conn, did))
        self.assertEqual(actual["seat"], "host/reviewer")
        self.assertIsNone(actual["window"])
        self.assertIn("deferred", actual)

    def test_new_review_round_never_reuses_prior_round_message(self):
        conn = wp.connect_writable()
        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,aliases,host,tmux,status,"
                " addressable,updated_at,refreshed_ms) VALUES"
                " ('reviewer-current','host/reviewer','','%s',"
                " 'tmux=0:7.0 win=model','active',1,'',0)"
                % socket.gethostname().split('.', 1)[0]
            )
            did = wp.insert_task(
                conn, recipient="owner", subject="second review crash window",
                workflow="pr", repo="example-storage", owner_seat="owner",
                reviewer_seat="host/reviewer", check_cmd="echo h",
            )
            conn.execute("UPDATE dispatch SET state='awaiting-review'"
                         " WHERE id=?", (did,))
            first = self.record_current_message(conn, did, "review-request", f"rr:{did}:1",
                                  "host/reviewer", "s", "b")
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='rr-old',"
                " recipient_agent_id='reviewer-old' WHERE id=?", (first,),
            )
            conn.execute("UPDATE dispatch SET state='fixing' WHERE id=?", (did,))
            conn.execute("UPDATE dispatch SET state='awaiting-review'"
                         " WHERE id=?", (did,))
        actual = wp.resolve_owed_recipient(conn, wp.fetch(conn, did))
        self.assertEqual(actual["seat"], "host/reviewer")
        self.assertIsNone(actual["window"])
        self.assertIn("deferred", actual)

    def test_superseded_failed_message_never_retries_or_dead_letters(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="seat-a",
                                 subject="superseded retry", check_cmd="true")
            old = self.record_current_message(conn, did, "dispatch", f"dispatch:{did}",
                                "seat-a", "s", "b")
            conn.execute(
                "UPDATE task_msg SET send_state='failed',attempts=? WHERE id=?",
                (1, old),
            )
            conn.execute("UPDATE dispatch SET recipient='seat-b' WHERE id=?",
                         (did,))
            current = self.record_current_message(
                conn, did, "reassign-notify", f"reassign:{did}:1",
                "seat-b", "s", "b",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-b',"
                " recipient_agent_id='seat-b' WHERE id=?", (current,),
            )
        with mock.patch.object(wp.subprocess, "run") as send:
            self.assertEqual(wp.retry_unsent(conn), (0, 0))
        send.assert_not_called()
        self.assertFalse(wp.dispatch_undelivered(conn, did))
        with conn:
            conn.execute("UPDATE task_msg SET attempts=? WHERE id=?",
                         (wp.MAX_SEND_ATTEMPTS, old))
        self.assertEqual(wp.dead_letters(conn), [])
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "orc_for_superseded_message_view",
            ROOT / "scripts" / "fleet-orchestrator.py",
        )
        orc = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = orc
        spec.loader.exec_module(orc)
        task = wp.fetch(conn, did)
        self.assertFalse(any("SEND-FAILED" in flag
                             for flag in orc.task_flags(conn, task)))
        self.assertEqual(orc.attention_rows(conn, [task]), [])

    def test_pr_dispatch_to_another_seat_is_not_owner_delivery_evidence(self):
        conn = wp.connect_writable()
        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,aliases,host,tmux,status,"
                " addressable,updated_at,refreshed_ms) VALUES"
                " ('owner-a','host/owner-short','',?,'tmux=0:7.0 win=model',"
                " 'active',1,'new',2)",
                (socket.gethostname().split('.', 1)[0],),
            )
            did = wp.insert_task(
                conn, recipient="dispatch-seat", subject="split PR seats",
                workflow="pr", repo="example-storage", owner_seat="owner-short",
                reviewer_seat="reviewer", check_cmd="echo h",
            )
            msg_id = self.insert_legacy_message(
                conn, did, "dispatch", "dispatch-seat-id",
                dedup_key=f"dispatch:{did}", subject="subject", body="body",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-dispatch',"
                " recipient_agent_id='dispatch-seat-id' WHERE id=?", (msg_id,),
            )
        actual = wp.resolve_owed_recipient(conn, wp.fetch(conn, did))
        self.assertIn("deferred", actual,
                      "an ambiguous legacy PR dispatch cannot select a pane")
        self.assertEqual(wp.repair_missing_responsibility_messages(
            conn, log=lambda *_: None), 1)
        restored = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (did,),
        ).fetchone()
        self.assertEqual(restored["target"], "owner-short")
        with conn:
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-owner',"
                " recipient_agent_id='owner-a' WHERE id=?", (restored["id"],),
            )
        actual = wp.resolve_owed_recipient(conn, wp.fetch(conn, did))
        self.assertEqual((actual["seat"], actual["window"]), ("owner-a", "7"))

    def test_combined_pr_dispatch_refuses_split_recipient(self):
        self.run_cli(
            ORC, "dispatch", "--no-handshake", "--workflow", "pr",
            "--to", "notifier", "--owner", "owner",
            "--reviewer", "reviewer", "--subject", "split notice",
            "--no-check", expect=1,
        )
        self.assertFalse(Path(self.env["DISPATCH_LEDGER_DB"]).exists(),
                         "a refused dispatch must not create the store")

    def test_combined_pr_dispatch_records_owner_work_explicitly(self):
        self.run_cli(
            ORC, "dispatch", "--no-handshake", "--workflow", "pr",
            "--to", "owner", "--owner", "owner", "--reviewer", "reviewer",
            "--subject", "owner work", "--no-check",
        )
        conn = wp.connect_writable()
        task = conn.execute(
            "SELECT * FROM dispatch WHERE subject='owner work'"
        ).fetchone()
        msg = conn.execute(
            "SELECT purpose,target FROM task_msg WHERE task_id=?",
            (task["id"],),
        ).fetchone()
        self.assertEqual(tuple(msg), ("author-request", "owner"))

    def test_unknown_upgrade_recipient_never_falls_back_to_alias_pane(self):
        conn = wp.connect_writable()
        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,aliases,host,tmux,status,"
                " addressable,updated_at,refreshed_ms) VALUES"
                " ('old-seat','host/old','',?,'tmux=0:8.0 win=model',"
                " 'active',1,'old',1)",
                (socket.gethostname().split('.', 1)[0],),
            )
            did = wp.insert_task(
                conn, recipient="host/old", subject="upgrade row",
                check_cmd="true",
            )
            self.record_current_message(conn, did, "dispatch", f"dispatch:{did}",
                          "host/old", "subject", "body")
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-old'"
                " WHERE task_id=?", (did,)
            )
        with mock.patch.object(wp, "_agent_bus_rows", return_value=None):
            actual = wp.resolve_owed_recipient(conn, wp.fetch(conn, did))
        self.assertEqual(actual["seat"], "host/old")
        self.assertIsNone(actual["window"])
        self.assertIn("deferred", actual)
        with conn:
            conn.execute("UPDATE task_msg SET at_ms=? WHERE task_id=?",
                         (wp.now() - wp.DEAD_LETTER_PARK_S - 1, did))
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "orc_for_unknown_recipient_view",
            ROOT / "scripts" / "fleet-orchestrator.py",
        )
        orc = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = orc
        spec.loader.exec_module(orc)
        task = wp.fetch(conn, did)
        with mock.patch.object(wp, "_agent_bus_rows", return_value=None):
            self.assertEqual([m["task_id"] for m in wp.dead_letters(conn)],
                             [did])
            self.assertTrue(any("RECIPIENT-UNKNOWN" in flag
                                for flag in orc.task_flags(conn, task)))
            self.assertEqual(orc.attention_rows(conn, [task])[0][0]["id"], did)
            self.assertEqual(wp.escalate_dead_letters(
                conn, log=lambda *_: None), 1)
            self.assertEqual(wp.escalate_dead_letters(
                conn, log=lambda *_: None), 0)

    def test_system_message_dead_letter_is_visible_and_escalates_once(self):
        conn = wp.connect_writable()
        with conn:
            msg_id = self.record_current_message(
                conn, "liveness", "liveness-probe", "liveness:missing:1",
                "missing-seat", "probe", "body",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='failed',attempts=?,"
                " last_error='transport unavailable' WHERE id=?",
                (wp.MAX_SEND_ATTEMPTS, msg_id),
            )
        self.assertEqual([m["id"] for m in wp.dead_letters(conn)], [msg_id])
        self.assertEqual(wp.escalate_dead_letters(
            conn, log=lambda *_: None), 1)
        self.assertEqual(wp.escalate_dead_letters(
            conn, log=lambda *_: None), 0)
        raised = conn.execute(
            "SELECT subject FROM dispatch WHERE recipient='operator'"
        ).fetchone()
        self.assertIn("liveness-probe", raised["subject"])

    def test_retired_lifecycle_broadcasts_cannot_retry(self):
        conn = wp.connect_writable()
        with conn:
            for purpose in ("checkout", "succession-retire",
                            "liveness-retire"):
                self.record_current_message(
                    conn, "lifecycle", purpose, f"legacy:{purpose}",
                    "all", "legacy lifecycle message", "body",
                )
        unexpected_send = subprocess.CompletedProcess(
            ["matrix-bus", "send"], 0,
            stdout='{"msg_id":"unexpected","recipient_agent_ids":[]}\n',
            stderr="",
        )
        with mock.patch.object(wp.subprocess, "run",
                               return_value=unexpected_send) as transport:
            self.assertEqual(wp.retry_unsent(conn, log=lambda *_: None),
                             (0, 3))
        transport.assert_not_called()
        rows = conn.execute(
            "SELECT send_state,last_error FROM task_msg"
            " WHERE task_id='lifecycle' ORDER BY id"
        ).fetchall()
        self.assertEqual([row["send_state"] for row in rows],
                         ["invalid-target"] * 3)
        self.assertTrue(all(wp.BROADCAST_REFUSAL_PREFIX in row["last_error"]
                            for row in rows))
        conn.close()

    def test_at_all_remains_a_broadcast_even_if_cached_as_an_alias(self):
        conn = wp.connect_writable()
        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,aliases,host,tmux,status,"
                " addressable,updated_at,refreshed_ms)"
                " VALUES ('alias-all','host/seat','@all','otherhost','',"
                " 'active',1,'',0)"
            )
            did = wp.insert_task(conn, recipient="operator", subject="broadcast",
                                 check_cmd="true")
            row_id = self.record_current_message(conn, did, "announce", "announce:all",
                                   "@all", "subject", "body")
        completed = subprocess.CompletedProcess(
            ["matrix-bus", "send"], 0,
            stdout=('{"msg_id":"broadcast-test",'
                    '"recipient_agent_ids":["only-recipient"]}\n'),
            stderr="",
        )
        with mock.patch.object(wp.subprocess, "run",
                               return_value=completed) as transport:
            self.assertTrue(wp.bus_send(conn, row_id))
        self.assertEqual(transport.call_args.args[0][4], "@all")
        recorded = conn.execute(
            "SELECT recipient_agent_id FROM task_msg WHERE id=?", (row_id,)
        ).fetchone()
        self.assertEqual(recorded["recipient_agent_id"], "")
        bus = sqlite3.connect(self.env["AGENT_BUS_DB"])
        with bus:
            bus.execute("CREATE TABLE outbox_recipients"
                        " (msg_id TEXT,recipient_agent_id TEXT)")
            bus.execute("INSERT INTO outbox_recipients VALUES"
                        " ('broadcast-test','only-recipient')")
        bus.close()
        self.assertEqual(wp.message_recipient_agent_id(
            "broadcast-test", "", "@all"), "")
        delivery = subprocess.CompletedProcess(
            ["matrix-bus", "delivery"], 0,
            stdout=('{"recipients":[{"recipient_agent_id":"only-recipient",'
                    '"delivered_ms":1}]}\n'), stderr="",
        )
        with mock.patch.object(wp.subprocess, "run", return_value=delivery):
            self.assertEqual(wp.poll_receipts(conn), 1)
        recorded = conn.execute(
            "SELECT recipient_agent_id FROM task_msg WHERE id=?", (row_id,)
        ).fetchone()
        self.assertEqual(recorded["recipient_agent_id"], "")
        with conn:
            conn.execute("UPDATE task_msg SET at_ms=at_ms-? WHERE id=?",
                         (wp.DEAD_LETTER_PARK_S + 1, row_id))
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "orc_for_broadcast_view_test",
            ROOT / "scripts" / "fleet-orchestrator.py",
        )
        orc = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = orc
        spec.loader.exec_module(orc)
        task = wp.fetch(conn, did)
        self.assertFalse(any("RECIPIENT-UNKNOWN" in flag
                             for flag in orc.task_flags(conn, task)))
        self.assertEqual(orc.attention_rows(conn, [task]), [])

    def test_broadcast_cannot_transfer_task_responsibility(self):
        conn = wp.connect_writable()
        with self.assertRaises(SystemExit):
            with conn:
                wp.insert_task(conn, recipient="@all", subject="bad owner",
                               check_cmd="true")
        with conn:
            did = wp.insert_task(conn, recipient="worker",
                                 subject="legacy broadcast responsibility",
                                 check_cmd="true")


            conn.execute("UPDATE dispatch SET recipient='@all' WHERE id=?",
                         (did,))
            row_id = self.record_current_message(conn, did, "dispatch", f"dispatch:{did}",
                                   "@all", "s", "b")
        with mock.patch.object(wp.subprocess, "run") as transport:
            self.assertFalse(wp.bus_send(conn, row_id))
        transport.assert_not_called()
        row = conn.execute(
            "SELECT send_state,attempts,last_error FROM task_msg WHERE id=?",
            (row_id,),
        ).fetchone()
        self.assertEqual((row["send_state"], row["attempts"]),
                         ("invalid-target", 0))
        self.assertTrue(
            row["last_error"].startswith(wp.BROADCAST_REFUSAL_PREFIX),
            row["last_error"],
        )

        with conn:
            conn.execute(
                "INSERT INTO role_assignment(role,agent_id,granted_by,granted_ms)"
                " VALUES ('legacy-broadcast','all','old',0)"
            )
            role_did = wp.insert_task(
                conn, recipient="role:legacy-broadcast",
                subject="legacy role broadcast responsibility",
                check_cmd="true",
            )
            role_msg = self.record_current_message(
                conn, role_did, "reassign-notify",
                f"role-broadcast:{role_did}",
                "role:legacy-broadcast", "s", "b",
            )
        with mock.patch.object(wp.subprocess, "run") as transport:
            self.assertFalse(wp.bus_send(conn, role_msg))
        transport.assert_not_called()
        role_row = conn.execute(
            "SELECT send_state,recipient_agent_id FROM task_msg WHERE id=?",
            (role_msg,),
        ).fetchone()
        self.assertEqual(tuple(role_row), ("invalid-target", ""))
        self.assertIn(role_msg, [msg["id"] for msg in wp.dead_letters(conn)])
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "orc_for_invalid_broadcast_view",
            ROOT / "scripts" / "fleet-orchestrator.py",
        )
        orc = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = orc
        spec.loader.exec_module(orc)
        self.assertIn("INVALID-TARGET x1",
                      orc.task_flags(conn, wp.fetch(conn, role_did)))

    def test_ambiguous_short_names_are_refused_not_guessed(self):
        conn = self.seed_seats()

        self.assertIsNone(wp.resolve_recipient(conn, "worker")["agent_id"])

        self.assertIsNone(wp.resolve_recipient(conn, "perf-command-tmux")["agent_id"])

    def test_duplicate_cached_exact_alias_defers_to_current_bus_and_recovers(self):
        conn = wp.connect_writable()
        with conn:
            for agent_id, handle in (("dup-a", "host/one"),
                                     ("dup-b", "host/two")):
                conn.execute(
                    "INSERT INTO seat (agent_id,handle,aliases,host,tmux,status,"
                    " addressable,updated_at,refreshed_ms)"
                    " VALUES (?,?,?,'otherhost','','active',1,'',0)",
                    (agent_id, handle, "shared-alias"),
                )
        resolved = wp.resolve_recipient(conn, "shared-alias")
        self.assertIsNone(resolved["agent_id"])
        self.assertEqual(resolved["transport_target"], "shared-alias")
        self.assertNotIn("error", resolved)
        with conn:
            did = wp.insert_task(conn, recipient="shared-alias",
                                 subject="ambiguous target", check_cmd="true")
        failed = subprocess.CompletedProcess(
            ["matrix-bus", "send"], 1, stdout="",
            stderr="agent-bus-v3: target 'shared-alias' resolved to 2 active agents",
        )
        with mock.patch.object(wp.subprocess, "run",
                               return_value=failed) as transport:
            self.assertFalse(self.route_current(
                conn, did, "dispatch", f"dispatch:{did}",
                "shared-alias", "subject", "body",
            ))
        self.assertEqual(transport.call_args.args[0][4], "shared-alias")
        msg = conn.execute(
            "SELECT send_state,attempts,last_error FROM task_msg WHERE task_id=?",
            (did,),
        ).fetchone()
        self.assertEqual((msg["send_state"], msg["attempts"]),
                         ("failed", 1))
        self.assertIn("resolved to 2", msg["last_error"])


        with conn:
            conn.execute("DELETE FROM seat WHERE agent_id='dup-b'")
        accepted = subprocess.CompletedProcess(
            ["matrix-bus", "send"], 0,
            stdout='{"msg_id":"alias-retry"}\n', stderr="",
        )
        with mock.patch.object(wp.subprocess, "run",
                               return_value=accepted) as transport:
            self.assertEqual(wp.retry_unsent(conn), (1, 0))
        self.assertEqual(transport.call_args.args[0][4], "shared-alias")
        msg = conn.execute(
            "SELECT send_state,attempts FROM task_msg WHERE task_id=?", (did,),
        ).fetchone()
        self.assertEqual((msg["send_state"], msg["attempts"]), ("accepted", 2))

    def test_stale_ambiguous_short_cache_cannot_veto_live_bus_resolution(self):
        conn = wp.connect_writable()
        with conn:
            for agent_id, handle in (("old-a", "host/a-worker"),
                                     ("live-b", "host/b-worker")):
                conn.execute(
                    "INSERT INTO seat (agent_id,handle,aliases,host,tmux,status,"
                    " addressable,updated_at,refreshed_ms)"
                    " VALUES (?,?,'','otherhost','','active',1,'',0)",
                    (agent_id, handle),
                )
            did = wp.insert_task(conn, recipient="worker",
                                 subject="live registry wins",
                                 check_cmd="true")
        self.assertIn("deferred", wp.resolve_recipient(conn, "worker"))
        accepted = subprocess.CompletedProcess(
            ["matrix-bus", "send"], 0,
            stdout='{"msg_id":"m-live",'
                   '"recipient_agent_ids":["live-b"]}\n',
            stderr="",
        )
        with mock.patch.object(wp.subprocess, "run",
                               return_value=accepted) as transport:
            self.assertTrue(self.route_current(
                conn, did, "dispatch", f"dispatch:{did}", "worker", "s", "b",
            ))
        self.assertEqual(transport.call_args.args[0][4], "worker")
        msg = conn.execute(
            "SELECT target,recipient_agent_id FROM task_msg WHERE task_id=?",
            (did,),
        ).fetchone()
        self.assertEqual(tuple(msg), ("worker", "live-b"))

    def test_accepted_exact_alias_records_actual_recipient_for_presentation(self):
        conn = wp.connect_writable()
        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,aliases,host,tmux,status,"
                " addressable,updated_at,refreshed_ms) VALUES"
                " ('aid-1','host/current','host/old','host',"
                " 'tmux=0:1.0 win=model','active',1,'old',1)"
            )
            did = wp.insert_task(
                conn, recipient="host/old", subject="alias delivery",
                check_cmd="true",
            )
        accepted = subprocess.CompletedProcess(
            ["matrix-bus", "send"], 0,
            stdout=("{\"schema\":\"agent-bus/send-result/v3\","
                    "\"msg_id\":\"m-alias\",\"transport_state\":"
                    "\"accepted\",\"recipients\":1,"
                    "\"recipient_agent_ids\":[\"aid-1\"]}\n"),
            stderr="",
        )
        with mock.patch.object(wp.subprocess, "run", return_value=accepted):
            self.assertTrue(self.route_current(
                conn, did, "dispatch", f"dispatch:{did}", "host/old",
                "subject", "body",
            ))
        msg = conn.execute(
            "SELECT target,recipient_agent_id FROM task_msg WHERE task_id=?",
            (did,),
        ).fetchone()
        self.assertEqual((msg["target"], msg["recipient_agent_id"]),
                         ("host/old", "aid-1"))

        bus = sqlite3.connect(self.env["AGENT_BUS_DB"])
        with bus:
            bus.execute("CREATE TABLE inbox (agent_id TEXT,msg_id TEXT,state TEXT)")
            bus.execute("INSERT INTO inbox VALUES ('aid-1','m-alias','presented')")
        bus.close()
        self.assertEqual(wp.bus_inbox_state("m-alias", "aid-1"), "presented")
        self.assertEqual(wp.fetch(conn, did)["state"], "open",
                         "presentation proves visibility, not acceptance")
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM event WHERE dispatch_id=? AND kind='ack'",
            (did,),
        ).fetchone()[0], 0)

    def test_alias_sql_wildcards_are_literal_characters(self):
        conn = wp.connect_writable()
        with conn:
            for agent_id, alias in (("literal-underscore", "seat_a"),
                                    ("plain-x", "otherXa")):
                conn.execute(
                    "INSERT INTO seat (agent_id,handle,aliases,host,tmux,status,"
                    " addressable,updated_at,refreshed_ms)"
                    " VALUES (?,?,?,'otherhost','','active',1,'',0)",
                    (agent_id, f"host/{agent_id}", alias),
                )
        self.assertIsNone(wp.resolve_recipient(conn, "seat%a")["agent_id"])
        self.assertIsNone(wp.resolve_recipient(conn, "other_a")["agent_id"])
        self.assertEqual(wp.resolve_recipient(conn, "seat_a")["agent_id"],
                         "literal-underscore")

    def test_registry_addressability_guides_nudges_not_exact_bus_authority(self):
        conn = wp.connect_writable()
        members = [
            {
                "agent_id": "cron-id",
                "handle": "example-host/fleet-orchestrator-cron",
                "aliases": ["example-host/old-cron"],
                "host": "host", "tmux": "headless=cron", "status": "active",
                "addressable": False, "updated_at": "now",
            },
            {
                "agent_id": "model-id", "handle": "example-host/model-tmux7",
                "aliases": ["example-host/model-old"], "host": "otherhost",
                "tmux": "tmux=0:7.0 win=claude", "status": "active",
                "addressable": True, "updated_at": "now",
            },
            {
                "agent_id": "fallback-id",
                "handle": "example-host/fleet-command-tmux8", "aliases": [],
                "host": "otherhost", "tmux": "tmux=0:8.0 win=claude",
                "status": "active", "addressable": True,
                "updated_at": "now",
            },
        ]
        output = "\n".join(json.dumps(member) for member in members) + "\n"
        completed = subprocess.CompletedProcess(
            ["matrix-bus", "members"], 0, stdout=output, stderr=""
        )
        with mock.patch.object(wp, "read_members", return_value=members):
            conn = wp.connect_writable()
            self.assertTrue(wp.members_available(conn))

        cached = {
            row["agent_id"]: row["addressable"]
            for row in conn.execute("SELECT agent_id,addressable FROM seat")
        }
        self.assertEqual(cached,
                         {"cron-id": 0, "model-id": 1, "fallback-id": 1})
        for name in (
            "cron-id", "example-host/fleet-orchestrator-cron",
            "example-host/old-cron",
        ):
            with self.subTest(name=name):
                resolved = wp.resolve_recipient(conn, name)
                self.assertIsNone(resolved["agent_id"])
                self.assertEqual(resolved["transport_target"], name)
                self.assertNotIn("error", resolved)
        short = wp.resolve_recipient(conn, "fleet-orchestrator-cron")
        self.assertIsNone(short["agent_id"])
        self.assertIn("deferred", short)
        self.assertEqual(
            wp.resolve_recipient(conn, "example-host/model-tmux7")["agent_id"],
            "model-id",
        )
        with conn:
            did = wp.insert_task(
                conn, recipient="example-host/fleet-orchestrator-cron",
                subject="must not route to sender-only identity",
                check_cmd="true",
            )
        failed = subprocess.CompletedProcess(
            ["matrix-bus", "send"], 1, stdout="",
            stderr="agent-bus-v3: target resolved to 0 active agents",
        )
        with mock.patch.object(wp.subprocess, "run",
                               return_value=failed) as transport:
            self.assertFalse(self.route_current(
                conn, did, "dispatch", f"dispatch:{did}",
                "example-host/fleet-orchestrator-cron", "subject", "body",
            ))
        self.assertEqual(transport.call_args.args[0][4],
                         "example-host/fleet-orchestrator-cron")
        row = conn.execute(
            "SELECT send_state,attempts,last_error FROM task_msg WHERE task_id=?",
            (did,),
        ).fetchone()
        self.assertEqual((row["send_state"], row["attempts"]),
                         ("failed", 1))
        self.assertIn("0 active agents", row["last_error"])
        msg_row = conn.execute(
            "SELECT id FROM task_msg WHERE task_id=?", (did,)
        ).fetchone()["id"]
        with mock.patch.object(wp.subprocess, "run",
                               return_value=failed) as transport:
            self.assertFalse(wp.bus_send(conn, msg_row))
        self.assertEqual(
            transport.call_args.args[0][4],
            "example-host/fleet-orchestrator-cron",
            "a retry uses the immutable recorded target; there is no second"
            " caller-supplied target that can rewrite its actual recipient",
        )
        row = conn.execute(
            "SELECT send_state,attempts FROM task_msg WHERE task_id=?", (did,),
        ).fetchone()
        self.assertEqual((row["send_state"], row["attempts"]), ("failed", 2))
        with conn:
            conn.execute("UPDATE task_msg SET attempts=? WHERE task_id=?",
                         (wp.MAX_SEND_ATTEMPTS, did))


        with conn:
            short_task = wp.insert_task(
                conn, recipient="fleet-orchestrator-cron",
                subject="short cached name", check_cmd="true",
            )
        with mock.patch.object(wp.subprocess, "run",
                               return_value=failed) as transport:
            self.assertFalse(self.route_current(
                conn, short_task, "dispatch", f"dispatch:{short_task}",
                "fleet-orchestrator-cron", "subject", "body",
            ))
        self.assertEqual(transport.call_args.args[0][4],
                         "fleet-orchestrator-cron")
        short_row = conn.execute(
            "SELECT send_state,attempts,last_error FROM task_msg WHERE task_id=?",
            (short_task,),
        ).fetchone()
        self.assertEqual((short_row["send_state"], short_row["attempts"]),
                         ("failed", 1))
        self.assertIn("0 active agents", short_row["last_error"])
        accepted = subprocess.CompletedProcess(
            ["matrix-bus", "send"], 0,
            stdout='{"msg_id":"short-retry"}\n', stderr="",
        )
        with mock.patch.object(wp.subprocess, "run",
                               return_value=accepted) as transport:
            self.assertEqual(wp.retry_unsent(conn), (1, 0))
        self.assertEqual(transport.call_args.args[0][4],
                         "fleet-orchestrator-cron")
        short_row = conn.execute(
            "SELECT send_state,attempts FROM task_msg WHERE task_id=?",
            (short_task,),
        ).fetchone()
        self.assertEqual((short_row["send_state"], short_row["attempts"]),
                         ("accepted", 2))

    def test_failed_send_counts_attempt_and_keeps_evidence(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="no-such-seat",
                                 subject="dead letter", check_cmd="true")
        os.environ["MATRIX_BUS_CFG"] = self.env["MATRIX_BUS_CFG"]
        try:
            ok = self.route_current(conn, did, "dispatch", f"dispatch:{did}",
                          "no-such-seat", "s", "the body")
        finally:
            os.environ.pop("MATRIX_BUS_CFG", None)
        self.assertFalse(ok)
        row = conn.execute("SELECT * FROM task_msg WHERE task_id=?", (did,)).fetchone()
        self.assertEqual(row["send_state"], "failed")
        self.assertEqual(row["attempts"], 1)
        self.assertTrue(row["last_error"])
        self.assertEqual(row["body"], "the body")

    def test_tick_retries_failed_sends_up_to_the_cap(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="no-such-seat",
                                 subject="dead letter", check_cmd="true")
            self.record_current_message(conn, did, "dispatch", f"dispatch:{did}",
                          "no-such-seat", "s", "b")
        env = dict(self.env)
        for _ in range(2):
            out = subprocess.run([sys.executable, ORC, "tick"], text=True,
                                 capture_output=True, env=env)
            self.assertEqual(out.returncode, 0, out.stderr)
        row = conn.execute("SELECT attempts, send_state FROM task_msg"
                           " WHERE task_id=?", (did,)).fetchone()
        self.assertEqual(row["send_state"], "failed")
        self.assertEqual(row["attempts"], 2)

        with conn:
            conn.execute("UPDATE task_msg SET attempts=? WHERE task_id=?",
                         (wp.MAX_SEND_ATTEMPTS, did))
        out = subprocess.run([sys.executable, ORC, "tick"], text=True,
                             capture_output=True, env=env)
        self.assertEqual(out.returncode, 0, out.stderr)
        row = conn.execute("SELECT attempts FROM task_msg WHERE task_id=?",
                           (did,)).fetchone()
        self.assertEqual(row["attempts"], wp.MAX_SEND_ATTEMPTS)

    def test_board_flags_undelivered_sends(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="no-such-seat",
                                 subject="dead letter", check_cmd="true")
            self.record_current_message(conn, did, "dispatch", f"dispatch:{did}",
                          "no-such-seat", "s", "b")
        out = self.run_cli(ORC, "board")
        self.assertIn("Message delivery pending", out)

    def test_board_flags_invalid_target_separately(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="ambiguous-seat",
                                 subject="invalid recipient", check_cmd="true")
            row_id = self.record_current_message(conn, did, "dispatch", f"dispatch:{did}",
                                   "ambiguous-seat", "s", "b")
            wp.refuse_recorded_target(conn, row_id, "matches two identities")
        out = self.run_cli(ORC, "board")
        self.assertIn("Recipient could not be resolved", out)


class ReviewRound2Tests(StoreTestCase):


    def test_unheld_role_burns_no_attempts_and_reaches_the_operator(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="role:nobody", subject="role task",
                                 check_cmd="true")
            self.record_current_message(conn, did, "dispatch", f"dispatch:{did}",
                          "role:nobody", "s", "the parked body")

            conn.execute("UPDATE task_msg SET at_ms=? WHERE task_id=?",
                         (wp.now() - wp.DEAD_LETTER_PARK_S - 60, did))
        out = subprocess.run([sys.executable, ORC, "tick"], text=True,
                             capture_output=True, env=self.env)
        self.assertEqual(out.returncode, 0, out.stderr)
        conn = wp.connect_writable()
        row = conn.execute("SELECT * FROM task_msg WHERE task_id=?", (did,)).fetchone()
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["escalated_to_operator"], 1)
        brief = self.run_cli(LEDGER, "brief")
        self.assertIn("could not be delivered", brief)
        self.assertIn("the parked body", brief)
        self.assertIn(did, brief)

        subprocess.run([sys.executable, ORC, "tick"], text=True,
                       capture_output=True, env=self.env)
        conn = wp.connect_writable()
        n = conn.execute("SELECT COUNT(*) FROM dispatch").fetchone()[0]
        self.assertEqual(n, 1)

    def test_doctor_counts_dead_letters(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="no-such", subject="dl",
                                 check_cmd="true")
            self.record_current_message(conn, did, "dispatch", f"dispatch:{did}",
                          "no-such", "s", "b")
            conn.execute("UPDATE task_msg SET send_state='failed', attempts=?"
                         " WHERE task_id=?", (wp.MAX_SEND_ATTEMPTS, did))
        out = subprocess.run([sys.executable, LEDGER, "doctor"], text=True,
                             capture_output=True, env=self.env)
        self.assertEqual(out.returncode, 1)
        self.assertIn("dead letter", out.stdout)

    def test_closed_task_keeps_failed_send_as_audit_but_never_retries(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="no-such", subject="dl",
                                 check_cmd="true")
            self.record_current_message(conn, did, "dispatch", f"dispatch:{did}",
                          "no-such", "s", "b")
            conn.execute("UPDATE task_msg SET send_state='failed', attempts=2"
                         " WHERE task_id=?", (did,))
        self.run_cli(LEDGER, "close", did, "--resolution", "done",
                     "--note", "delivered manually and completed")
        resent, failing = wp.retry_unsent(conn, log=lambda _line: None)
        self.assertEqual((resent, failing), (0, 0))
        out = subprocess.run([sys.executable, LEDGER, "doctor"], text=True,
                             capture_output=True, env=self.env)
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertNotIn("dead letter", out.stdout)
        conn = wp.connect_writable()
        row = conn.execute("SELECT send_state, attempts FROM task_msg"
                           " WHERE task_id=?", (did,)).fetchone()
        self.assertEqual((row["send_state"], row["attempts"]),
                         ("failed", 2))

    def test_doctor_exempts_standing_tasks_from_the_age_check(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="self",
                                 subject="STANDING: run the sweep each wake",
                                 check_cmd="true")
            conn.execute("UPDATE dispatch SET created_ms=? WHERE id=?",
                         (wp.now() - 5 * 86400, did))
        out = subprocess.run([sys.executable, LEDGER, "doctor"], text=True,
                             capture_output=True, env=self.env)
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertNotIn(did, out.stdout.replace("NOTE", ""))
        self.assertIn("STANDING task(s) exempt", out.stdout)

    def test_doctor_does_not_duplicate_the_operator_brief_queue(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="operator",
                                 subject="human decision", body="choose A or B",
                                 check_cmd="true")
            answered = wp.insert_task(
                conn, recipient="operator", subject="many answered reminders",
                body="decide whether this remains open", check_cmd="true",
            )
            conn.execute("UPDATE dispatch SET created_ms=?,chases=2,"
                         " chases_total=2 WHERE id=?",
                         (wp.now() - 5 * 86400, did))
            conn.execute("UPDATE dispatch SET created_ms=?,chases=1,"
                         " chases_total=4 WHERE id=?",
                         (wp.now() - 5 * 86400, answered))
        out = subprocess.run([sys.executable, LEDGER, "doctor"], text=True,
                             capture_output=True, env=self.env)
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertNotIn(did, out.stdout)
        self.assertNotIn(answered, out.stdout)
        brief = self.run_cli(LEDGER, "brief")
        self.assertIn(did, brief)
        self.assertIn(answered, brief)

    def test_doctor_does_not_duplicate_an_escalated_original_task(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="worker",
                                 subject="unanswered original task",
                                 check_cmd="true")
            conn.execute("UPDATE dispatch SET created_ms=? WHERE id=?",
                         (wp.now() - 5 * 86400, did))
            self.set_current_drive(
                conn, did, state=wp.S_ESCALATED, cycles=1, idle_waits=6)
        out = subprocess.run([sys.executable, LEDGER, "doctor"], text=True,
                             capture_output=True, env=self.env)
        self.assertNotIn(f"{did} open for", out.stdout)
        self.assertIn(did, self.run_cli(LEDGER, "brief"))

    def test_brief_does_not_credit_a_question_on_an_undelivered_task(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="worker",
                                 subject="two live problems", check_cmd="true")
            msg_id = self.record_current_message(
                conn, did, "dispatch", f"dispatch:{did}", "worker",
                "assignment", "original assignment",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='failed',attempts=?,"
                " escalated_to_operator=1,last_error='network unavailable'"
                " WHERE id=?", (wp.MAX_SEND_ATTEMPTS, msg_id),
            )
            conn.execute("UPDATE dispatch SET ask_flag=? WHERE id=?",
                         (wp.now(), did))
            wp.record(conn, did, "note",
                      f"{wp.ASK_NOTE_PREFIX}choose the safe input")
        brief = self.run_cli(LEDGER, "brief")
        self.assertEqual(brief.count(f"--- {did}"), 1)
        self.assertIn("delivery failed", brief)
        self.assertNotIn("blocked on a human", brief)
        self.assertIn("network unavailable", brief)
        self.assertNotIn("choose the safe input", brief)

    def test_brief_does_not_assign_an_independent_question_to_operator(self):
        conn = wp.connect_writable()
        question = "requester chooses the migration input"
        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,status,addressable,"
                " refreshed_ms) VALUES ('requester','test/requester',"
                " 'active',1,?)", (wp.now(),),
            )
            did = wp.insert_task(
                conn, recipient="worker", requester_seat="requester",
                subject="delivery failure plus independent question",
                check_cmd="true",
            )
            msg_id = self.record_current_message(
                conn, did, "dispatch", f"dispatch:{did}", "worker",
                "assignment", "original assignment",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='failed',attempts=?,"
                " escalated_to_operator=1,last_error='network unavailable'"
                " WHERE id=?", (wp.MAX_SEND_ATTEMPTS, msg_id),
            )
            conn.execute("UPDATE dispatch SET ask_flag=? WHERE id=?",
                         (wp.now(), did))
            self.record_current_voice(
                conn, did, "note", f"{wp.ASK_NOTE_PREFIX}{question}")
        brief = self.run_cli(LEDGER, "brief")
        self.assertIn(did, brief)
        self.assertIn("delivery failed", brief)
        self.assertNotIn("blocked on a human", brief)
        self.assertNotIn(question, brief)

    def test_doctor_does_not_call_merge_pending_operator_wait_silent(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="owner", subject="review pr",
                                 check_cmd="true", workflow="pr", repo="example-app",
                                 owner_seat="owner", reviewer_seat="reviewer",
                                 ready_cmd="true", done_cmd="false")
            conn.execute("DROP TRIGGER dispatch_state_legal")
            conn.execute("UPDATE dispatch SET state='merge-pending', chases=3"
                         " WHERE id=?", (did,))
        out = subprocess.run([sys.executable, LEDGER, "doctor"], text=True,
                             capture_output=True, env=self.env)
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertNotIn(did, out.stdout)


class LedgerSpeechTests(StoreTestCase):


    def make_exhausted_pairing(self, note_fresh: bool):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="tmux1", subject="speech fixture",
                                 check_cmd="true")
            context = self.set_current_drive(
                conn, did, state=wp.S_PULLED,
                idle_waits=wp.IDLE_WAIT_LIMIT - 1)
            if note_fresh:
                self.record_current_voice(
                    conn, did, "note", "still on it, mid-migration")
            else:
                conn.execute("INSERT INTO event (dispatch_id, at_ms, actor,"
                             " kind, note, responsibility_version,"
                             " continuation_generation) VALUES (?,?,?,?,?,?,?)",
                             (did, wp.now() - wp.LEDGER_SPEECH_S - 60,
                              "tmux1", "note", "old note", 0,
                              context["generation"]))
        return did

    def test_fresh_note_defers_the_idle_escalation(self):
        did = self.make_exhausted_pairing(note_fresh=True)
        conn = wp.connect_writable()
        self.assertTrue(wp.seat_spoke_recently(conn, did))

    def test_stale_or_no_note_escalates(self):
        did = self.make_exhausted_pairing(note_fresh=False)
        conn = wp.connect_writable()
        self.assertFalse(wp.seat_spoke_recently(conn, did))

    def test_open_and_chase_do_not_count_as_speech(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="tmux1", subject="verbs fixture",
                                 check_cmd="true")
            wp.record(conn, did, "chase", "chaser voice")
        self.assertFalse(wp.seat_spoke_recently(conn, did))

    def test_requester_note_is_not_worker_progress(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="worker-a", subject="fixture",
                                 check_cmd="true", requester_seat="requester")
            wp.record(conn, did, "note", "coordination only",
                      actor="requester")
        self.assertFalse(wp.seat_spoke_recently(conn, did))
        with conn:
            self.record_current_voice(
                conn, did, "note", "implementation continues")
        self.assertTrue(wp.seat_spoke_recently(conn, did))

    def test_old_owner_voice_does_not_revive_after_a_b_a(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="worker-a", subject="fixture",
                                 check_cmd="true")
            self.record_current_voice(
                conn, did, "note", "old visit", actor="worker-a")
            conn.execute("UPDATE dispatch SET recipient='worker-b' WHERE id=?",
                         (did,))
            to_b = self.record_current_message(conn, did, "reassign-notify",
                                 f"reassign:{did}:b", "worker-b", "s", "b")
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-b',"
                " recipient_agent_id='worker-b' WHERE id=?", (to_b,),
            )
            conn.execute("UPDATE dispatch SET recipient='worker-a' WHERE id=?",
                         (did,))
            to_a = self.record_current_message(conn, did, "reassign-notify",
                                 f"reassign:{did}:a2", "worker-a", "s", "b")
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-a2',"
                " recipient_agent_id='worker-a' WHERE id=?", (to_a,),
            )
        self.assertEqual(wp.fetch(conn, did)["responsibility_version"], 2)
        self.assertFalse(wp.seat_spoke_recently(conn, did),
                         "the first visit to A is a different generation")
        with conn:
            self.record_current_voice(
                conn, did, "note", "current visit", actor="worker-a")
        self.assertTrue(wp.seat_spoke_recently(conn, did))


class MisfireDefenseTests(StoreTestCase):


    def test_ladder_suppressed_while_dispatch_undelivered(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="tmux1", subject="undelivered",
                                 check_cmd="true")
            self.record_current_message(conn, did, "dispatch", f"dispatch:{did}",
                          "tmux1", "s", "b")
            conn.execute("UPDATE task_msg SET send_state='failed' WHERE task_id=?",
                         (did,))
        self.assertTrue(wp.dispatch_undelivered(conn, did))
        out = subprocess.run([sys.executable, ORC, "tick", "--dry-run"], text=True,
                             capture_output=True, env=self.env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("current responsibility message is not accepted",
                      out.stdout)

        with conn:
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m1',"
                " recipient_agent_id='seat-1' WHERE task_id=?", (did,),
            )
        self.assertFalse(wp.dispatch_undelivered(conn, did))

    def test_plain_open_tasks_keep_the_ladder(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="tmux1", subject="plain open",
                                 check_cmd="true")
        self.assertFalse(wp.dispatch_undelivered(conn, did))

    def test_plain_open_with_ambiguous_cached_name_still_keeps_the_ladder(self):
        conn = wp.connect_writable()
        with conn:
            for agent_id in ("worker-a", "worker-b"):
                conn.execute(
                    "INSERT INTO seat (agent_id,handle,aliases,status,"
                    " addressable,refreshed_ms) VALUES (?,?,?,'active',1,?)",
                    (agent_id, f"test/shared-worker/{agent_id}", "", wp.now()),
                )
            did = wp.insert_task(
                conn, recipient="shared-worker",
                subject="out-of-band assignment", check_cmd="true",
            )
        self.assertIn("deferred", wp.resolve_owed_recipient(
            conn, wp.fetch(conn, did)))
        self.assertFalse(wp.dispatch_undelivered(conn, did))

    def test_changed_out_of_band_responsibility_requires_new_delivery(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="seat-a",
                                 subject="changed assignment", check_cmd="true")
            conn.execute("UPDATE dispatch SET recipient='seat-b' WHERE id=?",
                         (did,))
        self.assertEqual(wp.fetch(conn, did)["responsibility_version"], 1)
        self.assertTrue(wp.dispatch_undelivered(conn, did))

    def test_operator_responsibility_needs_no_bus_delivery(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="seat-a",
                                 subject="operator decision", check_cmd="true")
            conn.execute("UPDATE dispatch SET recipient='operator' WHERE id=?",
                         (did,))
        self.assertGreater(wp.fetch(conn, did)["responsibility_version"], 0)
        self.assertFalse(wp.dispatch_undelivered(conn, did))


class CheckoutHygieneTests(StoreTestCase):


    def load_orc(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "orc_for_hygiene_tests", ROOT / "scripts" / "fleet-orchestrator.py")
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    def make_repo(self, base, *, bare=False):
        import subprocess as sp
        path = Path(base) / ("hub.git" if bare else "co")
        args = ["git", "init", "-q"] + (["--bare"] if bare else []) + [str(path)]
        sp.run(args, check=True, capture_output=True)
        return path

    @staticmethod
    def grant_commander(conn, agent_id="commander-b"):
        conn.execute(
            "INSERT INTO role_assignment (role,agent_id,granted_by,granted_ms)"
            " VALUES ('commander',?,'test',?)",
            (agent_id, wp.now()),
        )

    def test_dirty_checkout_found_with_mtimes_and_exempts(self):
        orc = self.load_orc()
        repo = self.make_repo(self.tmp.name)
        (repo / "junk.log").write_text("x")
        (repo / "spool").mkdir()
        (repo / "spool" / "runtime-file").write_text("x")
        findings = orc.checkout_findings(
            {"path": str(repo), "kind": "checkout", "exempt": ("spool/",)})
        self.assertEqual(len(findings), 1)
        self.assertIn("junk.log", findings[0])
        self.assertIn("T", findings[0])

    def test_in_repo_worktree_dirs_are_flagged_not_excused(self):


        orc = self.load_orc()
        repo = self.make_repo(self.tmp.name)
        (repo / ".claude" / "worktrees" / "wt").mkdir(parents=True)
        (repo / ".claude" / "worktrees" / "wt" / "f").write_text("x")
        findings = orc.checkout_findings(
            {"path": str(repo), "kind": "checkout", "exempt": ()})
        self.assertEqual(len(findings), 1)
        self.assertIn(".claude/worktrees/wt/f", findings[0])

    def test_clean_and_absent_are_silent(self):
        orc = self.load_orc()
        repo = self.make_repo(self.tmp.name)
        self.assertEqual(orc.checkout_findings(
            {"path": str(repo), "kind": "checkout", "exempt": ()}), [])
        self.assertEqual(orc.checkout_findings(
            {"path": str(Path(self.tmp.name) / "nope"), "kind": "checkout",
             "exempt": ()}), [])

    def test_bare_hub_regression_flagged(self):
        orc = self.load_orc()
        hub = self.make_repo(self.tmp.name, bare=True)
        self.assertEqual(orc.checkout_findings(
            {"path": str(hub), "kind": "bare-hub", "exempt": ()}), [])
        nonbare = self.make_repo(self.tmp.name)
        self.assertEqual(orc.checkout_findings(
            {"path": str(nonbare), "kind": "bare-hub", "exempt": ()}),
            ["NON-BARE"])

    def test_one_alert_per_pathset_per_day(self):
        conn = wp.connect_writable()
        with conn:
            conn.execute("INSERT INTO seat (agent_id, handle, aliases, host,"
                         " tmux, status, addressable, updated_at, refreshed_ms)"
                         " VALUES ('cmd-1','example-host/fleet-command-x','','h','',"
                         "'active',1,'',0)")
            conn.execute("INSERT INTO role_assignment (role, agent_id,"
                         " granted_by, granted_ms) VALUES"
                         " ('commander','cmd-1','test',0)")
            first = self.record_current_message(conn, "hygiene", "checkout-dirty",
                                  "hygiene:/x:abc:123", "cmd-1", "s", "b")
            second = self.record_current_message(conn, "hygiene", "checkout-dirty",
                                   "hygiene:/x:abc:123", "cmd-1", "s", "b")
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_empty_commander_cache_still_records_retryable_hygiene_alert(self):
        conn = wp.connect_writable()
        orc = self.load_orc()
        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,status,addressable,"
                " refreshed_ms) VALUES"
                " ('departed-command','example-host/fleet-command-old',"
                " 'active',1,1)"
            )
        repo = {"path": "/shared/main", "kind": "checkout", "exempt": ()}
        with mock.patch.object(orc, "WATCHED_CHECKOUTS", [repo]), \
                mock.patch.object(orc, "checkout_findings",
                                  return_value=["?? leaked.tmp\t2026-08-26"]):
            orc.tick_checkout_hygiene(conn, dry=False)
        msg = conn.execute(
            "SELECT target,send_state,last_error FROM task_msg"
            " WHERE purpose='checkout-dirty'",
        ).fetchone()
        self.assertEqual(msg["target"], "role:commander")
        self.assertEqual(msg["send_state"], "recorded")
        self.assertIn("unheld role", msg["last_error"])

    def test_empty_commander_keeps_escalation_on_the_original_task(self):
        conn = wp.connect_writable()
        orc = self.load_orc()
        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,status,addressable,"
                " refreshed_ms) VALUES"
                " ('departed-command','example-host/fleet-command-old',"
                " 'active',1,1)"
            )
            did = wp.insert_task(conn, recipient="worker", subject="needs help",
                                 check_cmd="true")
        orc.escalate(conn, wp.fetch(conn, did), "fixture wait", dry=False)
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM task_msg WHERE task_id=? AND purpose='escalation'",
            (did,),
        ).fetchone())
        note = conn.execute(
            "SELECT note FROM event WHERE dispatch_id=? AND kind='auto-note'"
            " ORDER BY id DESC LIMIT 1", (did,),
        ).fetchone()["note"]
        self.assertIn("operator", note)
        with conn:
            self.set_current_drive(
                conn, did, state=wp.S_ESCALATED, idle_waits=6)
        self.assertTrue(wp.waits_on_operator(conn, wp.fetch(conn, did)))

    def test_old_seat_escalation_cannot_put_current_work_in_operator_brief(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="current-seat",
                                 subject="moved responsibility",
                                 check_cmd="true")
            self.accept_current_responsibility(
                conn, did, actual="current-seat", pane="%8")
            conn.execute(
                "INSERT INTO drive (task_id,seat,generation,st,cycles,"
                " grace_used,idle_waits,absent_ticks,updated_ms)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (did, "old-seat", "old-generation", wp.S_ESCALATED,
                 1, 0, 6, 0, wp.now() - 60),
            )
            context = self.set_current_drive(
                conn, did, state=wp.S_WORKING, cycles=1)
            wp.record(
                conn, did, "auto-chase", "engine: old seat was idle",
                continuation_generation="old-generation")
        row = wp.fetch(conn, did)
        self.assertEqual(wp.current_drive(conn, row)["seat"], "current-seat")
        self.assertFalse(wp.waits_on_operator(conn, row))
        self.assertEqual(wp.repair_attention_notifications(conn), 0)

    def test_task_voice_clears_escalation_even_when_pane_is_absent(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="tmux1",
                                 subject="answered without pane",
                                 check_cmd="true")
            self.set_current_drive(
                conn, did, state=wp.S_ESCALATED, cycles=1, idle_waits=6)
            self.record_current_voice(
                conn, did, "note", "working; concrete update")
        conn.close()
        orc = self.load_orc()

        class NoSend:
            @staticmethod
            def send_outcome(*_args, **_kwargs):
                raise AssertionError("a responding seat must not be reminded")

        noops = (
            "tick_parents", "tick_pr_guards", "tick_review_reconcile",
            "tick_checkout_hygiene", "tick_seat_liveness",
            "tick_reviewer_rotation",
        )
        patches = [mock.patch.object(orc, name, return_value=None)
                   for name in noops]
        patches += [
            mock.patch.object(orc, "snapshot_db", return_value=None),
            mock.patch.object(orc, "tick_breakers", return_value=0),
            mock.patch.object(orc, "tick_deps", return_value=0),
            mock.patch.object(orc, "tick_deadlines", return_value=0),
            mock.patch.object(orc, "load_script", return_value=NoSend),
            mock.patch.object(orc, "state_dir",
                              return_value=Path(self.tmp.name) / "rt"),
            mock.patch.object(orc.nw_paths, "lock_path",
                              return_value=Path(self.tmp.name) / "rt" / "tick.lock"),
            mock.patch.object(orc.pane_sense, "agent_panes", return_value=[]),
            mock.patch.object(wp, "members_available", return_value=True),
            mock.patch.object(wp, "wake_shadow_off", return_value=True),
            mock.patch.object(wp, "repair_missing_responsibility_messages",
                              return_value=0),
            mock.patch.object(wp, "repair_standing_claim_notifications",
                              return_value=0),
            mock.patch.object(wp, "retry_unsent", return_value=(0, 0)),
            mock.patch.object(wp, "escalate_dead_letters", return_value=0),
            mock.patch.object(wp, "poll_receipts", return_value=0),
        ]
        for patcher in patches:
            patcher.start()
        try:
            self.assertEqual(orc.cmd_tick(mock.Mock(dry_run=False)), 0)
        finally:
            for patcher in reversed(patches):
                patcher.stop()
        conn = wp.connect_writable()
        drive = wp.current_drive(conn, wp.fetch(conn, did))
        self.assertEqual(drive["st"], wp.S_PULLED)
        self.assertFalse(wp.waits_on_operator(conn, wp.fetch(conn, did)))

    def test_local_stable_identity_uses_bus_when_cached_pane_is_missing(self):
        conn = wp.connect_writable()
        with conn:
            conn.execute(
                "INSERT INTO seat(agent_id,handle,host,tmux,status,"
                " addressable,refreshed_ms) VALUES (?,?,?,?,?,?,?)",
                ("worker-local", "test/worker-local",
                 socket.gethostname().split(".", 1)[0],
                 "tmux=stage:1.0 win=codex", "active", 1, wp.now()),
            )
            did = wp.insert_task(
                conn, recipient="worker-local",
                subject="local cached pane disappeared", check_cmd="true",
            )
            assignment = self.record_current_message(
                conn, did, "dispatch", f"dispatch:{did}", "worker-local",
                "assignment", "body",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-local',"
                " recipient_agent_id='worker-local' WHERE id=?",
                (assignment,),
            )
        conn.close()
        orc = self.load_orc()

        class NoPaneSend:
            @staticmethod
            def send_outcome(*_args, **_kwargs):
                raise AssertionError("a missing pane must not receive tmux input")

        noops = (
            "tick_parents", "tick_pr_guards", "tick_review_reconcile",
            "tick_checkout_hygiene", "tick_seat_liveness",
            "tick_reviewer_rotation",
        )
        patches = [mock.patch.object(orc, name, return_value=None)
                   for name in noops]
        patches += [
            mock.patch.object(orc, "snapshot_db", return_value=None),
            mock.patch.object(orc, "tick_breakers", return_value=0),
            mock.patch.object(orc, "tick_deps", return_value=0),
            mock.patch.object(orc, "tick_deadlines", return_value=0),
            mock.patch.object(orc, "load_script", return_value=NoPaneSend),
            mock.patch.object(orc, "state_dir",
                              return_value=Path(self.tmp.name) / "rt"),
            mock.patch.object(orc.nw_paths, "lock_path",
                              return_value=Path(self.tmp.name) / "rt" / "tick.lock"),
            mock.patch.object(orc.pane_sense, "agent_panes", return_value=[]),
            mock.patch.object(wp, "members_available", return_value=True),
            mock.patch.object(wp, "wake_shadow_off", return_value=True),
            mock.patch.object(wp, "repair_missing_responsibility_messages",
                              return_value=0),
            mock.patch.object(wp, "repair_standing_claim_notifications",
                              return_value=0),
            mock.patch.object(wp, "bus_send", return_value=True),
            mock.patch.object(wp, "retry_unsent", return_value=(0, 0)),
            mock.patch.object(wp, "escalate_dead_letters", return_value=0),
            mock.patch.object(wp, "poll_receipts", return_value=0),
        ]
        for patcher in patches:
            patcher.start()
        try:
            self.assertEqual(orc.cmd_tick(mock.Mock(dry_run=False)), 0)
        finally:
            for patcher in reversed(patches):
                patcher.stop()

        conn = wp.connect_writable()
        reminder = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=?"
            " AND purpose='continuation-reminder'", (did,),
        ).fetchone()
        self.assertIsNotNone(reminder)
        self.assertEqual(reminder["target"], "worker-local")
        drive = wp.current_drive(conn, wp.fetch(conn, did))
        self.assertEqual(drive["absent_ticks"], 0)

    def test_remote_blocked_task_notifies_once_without_a_local_pane(self):
        conn = wp.connect_writable()
        question = "choose whether the data migration may change old records"
        with conn:
            for seat in ("worker-remote", "requester"):
                conn.execute(
                    "INSERT INTO seat (agent_id,handle,host,tmux,status,"
                    " addressable,refreshed_ms) VALUES (?,?,?,?,?,?,?)",
                    (seat, f"remote/{seat}", "another-host", "", "active", 1,
                     wp.now()),
                )
            did = wp.insert_task(
                conn, recipient="worker-remote", requester_seat="requester",
                subject="remote task needs a decision", check_cmd="true",
            )
            msg = self.record_current_message(
                conn, did, "dispatch", f"dispatch:{did}", "worker-remote",
                "assignment", "body",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-worker',"
                " recipient_agent_id='worker-remote' WHERE id=?", (msg,),
            )
            conn.execute("UPDATE dispatch SET ask_flag=? WHERE id=?",
                         (wp.now(), did))
            self.record_current_voice(
                conn, did, "note", f"{wp.ASK_NOTE_PREFIX}{question}")
        self.member_source(conn)
        conn.close()
        orc = self.load_orc()

        class NoPaneSend:
            @staticmethod
            def send_outcome(*_args, **_kwargs):
                raise AssertionError("a remote seat has no local pane to touch")

        noops = (
            "tick_parents", "tick_pr_guards", "tick_review_reconcile",
            "tick_checkout_hygiene", "tick_seat_liveness",
            "tick_reviewer_rotation",
        )
        patches = [mock.patch.object(orc, name, return_value=None)
                   for name in noops]
        patches += [
            mock.patch.object(orc, "snapshot_db", return_value=None),
            mock.patch.object(orc, "tick_breakers", return_value=0),
            mock.patch.object(orc, "tick_deps", return_value=0),
            mock.patch.object(orc, "tick_deadlines", return_value=0),
            mock.patch.object(orc, "load_script", return_value=NoPaneSend),
            mock.patch.object(orc, "state_dir",
                              return_value=Path(self.tmp.name) / "rt"),
            mock.patch.object(orc.nw_paths, "lock_path",
                              return_value=Path(self.tmp.name) / "rt" / "tick.lock"),
            mock.patch.object(orc.pane_sense, "agent_panes", return_value=[]),
            mock.patch.object(wp, "members_available", return_value=True),
            mock.patch.object(wp, "wake_shadow_off", return_value=True),
            mock.patch.object(wp, "repair_missing_responsibility_messages",
                              return_value=0),
            mock.patch.object(wp, "repair_standing_claim_notifications",
                              return_value=0),
            mock.patch.object(wp, "bus_send", return_value=True),
            mock.patch.object(wp, "retry_unsent", return_value=(0, 0)),
            mock.patch.object(wp, "escalate_dead_letters", return_value=0),
            mock.patch.object(wp, "poll_receipts", return_value=0),
        ]
        for patcher in patches:
            patcher.start()
        try:
            self.assertEqual(orc.cmd_tick(mock.Mock(dry_run=False)), 0)
            self.assertEqual(orc.cmd_tick(mock.Mock(dry_run=False)), 0)
            conn = wp.connect_writable()
            with conn:
                expired = wp.now() - wp.ASK_FLAG_TTL_S - 1
                conn.execute("UPDATE dispatch SET ask_flag=? WHERE id=?",
                             (expired, did))
                conn.execute(
                    "UPDATE event SET at_ms=? WHERE dispatch_id=?"
                    " AND note LIKE ?", (expired, did,
                                         f"{wp.ASK_NOTE_PREFIX}%"))
            conn.close()
            for _ in range(wp.IDLE_WAIT_LIMIT + 1):
                self.assertEqual(orc.cmd_tick(mock.Mock(dry_run=False)), 0)
        finally:
            for patcher in reversed(patches):
                patcher.stop()
        conn = wp.connect_writable()
        task = wp.fetch(conn, did)
        self.assertEqual(wp.current_drive(conn, task)["st"], wp.S_ESCALATED)
        self.assertEqual(task["chases_total"], 2)
        notices = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=? AND purpose='escalation'",
            (did,),
        ).fetchall()
        self.assertEqual(len(notices), 2)
        self.assertEqual([n["target"] for n in notices],
                         ["requester", "requester"])
        self.assertIn(question, notices[0]["body"])
        self.assertIn(f"{ROOT / 'scripts' / 'orc'} -t default task show {did}",
                      notices[0]["body"])
        self.assertFalse(wp.message_is_current_responsibility(
            conn, notices[0], task),
            "an expired question must release even a remote seat's wait")
        self.assertTrue(wp.message_is_current_responsibility(
            conn, notices[1], task))
        self.assertNotIn(question, notices[1]["body"])

    def test_stale_commander_escalation_is_not_replayed_over_the_requester(self):
        conn = wp.connect_writable()
        stamp = wp.now() - 60
        with conn:
            self.grant_commander(conn)
            conn.execute(
                "INSERT INTO seat (agent_id,handle,status,addressable,"
                " refreshed_ms) VALUES"
                " ('requester-1','test/requester-1','active',1,?)",
                (wp.now(),),
            )
            did = wp.insert_task(
                conn, recipient="worker", subject="old alert",
                check_cmd="true", requester_seat="requester-1",
            )
            old_id = self.insert_legacy_message(
                conn, did, "escalation", "commander-a",
                dedup_key=f"escalation:{did}:1",
                subject="old escalation", body="old reason",
            )
            self.set_current_drive(
                conn, did, state=wp.S_ESCALATED, cycles=1, idle_waits=6)
            conn.execute(
                "UPDATE task_msg SET send_state='failed',attempts=2,at_ms=?,"
                " last_error='old failure' WHERE id=?", (stamp, old_id),
            )
        old = conn.execute("SELECT * FROM task_msg WHERE id=?", (old_id,)).fetchone()
        self.assertFalse(wp.message_is_sendable(conn, old, wp.fetch(conn, did)))
        with mock.patch.object(wp.subprocess, "run") as transport:
            self.assertFalse(wp.bus_send(conn, old_id))
        transport.assert_not_called()

        accepted = subprocess.CompletedProcess(
            ["matrix-bus", "send"], 0,
            stdout='{"msg_id":"m-current","recipient_agent_ids":'
                   '["requester-1"]}\n',
            stderr="",
        )
        orc = self.load_orc()
        with mock.patch.object(wp.subprocess, "run", return_value=accepted) as send:
            orc.escalate(conn, wp.fetch(conn, did), "still waiting", dry=False)
        self.assertEqual(send.call_args.args[0][4], "requester-1")
        rows = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=? AND purpose='escalation'"
            " ORDER BY id", (did,),
        ).fetchall()
        self.assertEqual([row["target"] for row in rows],
                         ["commander-a", "requester-1"])
        self.assertEqual((rows[0]["at_ms"], rows[0]["attempts"],
                          rows[0]["send_state"]),
                         (stamp, 2, "failed"))
        self.assertEqual((rows[1]["target"], rows[1]["send_state"],
                          rows[1]["recipient_agent_id"]),
                         ("requester-1", "accepted", "requester-1"))
        conn.close()

    def test_supervisor_failure_flag_clears_on_delivery_or_task_activity(self):
        conn = wp.connect_writable()
        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,status,addressable,"
                " refreshed_ms) VALUES ('requester','test/requester',"
                " 'active',1,?)", (wp.now(),),
            )
            did = wp.insert_task(
                conn, recipient="worker", requester_seat="requester",
                subject="current escalation only", check_cmd="true",
            )
            context = self.set_current_drive(
                conn, did, state=wp.S_ESCALATED, cycles=1, idle_waits=6)
            event_id = wp.record(
                conn, did, "auto-chase", "engine: current wait",
                continuation_generation=context["generation"])
            msg_id = self.record_current_message(
                conn, did, "escalation",
                f"escalation:{did}:1:to:requester:"
                f"attention-event={event_id}",
                "requester", "waiting", "body",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='failed',attempts=1,"
                " last_error='transport down' WHERE id=?", (msg_id,),
            )
        orc = self.load_orc()
        row = wp.fetch(conn, did)
        self.assertIn("SUPERVISOR-UNREACHABLE", orc.task_flags(conn, row))
        with conn:
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-ok',"
                " recipient_agent_id='requester',last_error='' WHERE id=?",
                (msg_id,),
            )
        self.assertNotIn("SUPERVISOR-UNREACHABLE",
                         orc.task_flags(conn, wp.fetch(conn, did)))
        with conn:
            conn.execute(
                "UPDATE task_msg SET send_state='failed',last_error='again'"
                " WHERE id=?", (msg_id,),
            )
            conn.execute("UPDATE drive SET st=? WHERE task_id=?",
                         (wp.S_WORKING, did))
        self.assertNotIn("SUPERVISOR-UNREACHABLE",
                         orc.task_flags(conn, wp.fetch(conn, did)))

    def test_old_escalation_without_current_drive_never_replays(self):
        conn = wp.connect_writable()
        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,status,addressable,"
                " refreshed_ms) VALUES ('requester','test/requester',"
                " 'active',1,?)", (wp.now(),),
            )
            did = wp.insert_task(
                conn, recipient="worker", requester_seat="requester",
                subject="old escalation after cleanup", check_cmd="true",
            )
            msg_id = self.insert_legacy_message(
                conn, did, "escalation", "requester",
                dedup_key=f"escalation:{did}:1:to:requester",
                subject="old", body="old",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='failed',attempts=1 WHERE id=?",
                (msg_id,),
            )
        msg = conn.execute("SELECT * FROM task_msg WHERE id=?", (msg_id,)).fetchone()
        self.assertFalse(wp.message_is_current_responsibility(
            conn, msg, wp.fetch(conn, did),
        ))
        with mock.patch.object(wp, "bus_send") as send:
            self.assertEqual(wp.retry_unsent(conn), (0, 0))
        send.assert_not_called()

    def test_a_to_b_to_a_starts_with_fresh_drive_and_wake_state(self):
        conn = wp.connect_writable()
        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,status,addressable,"
                " refreshed_ms) VALUES ('requester','test/requester',"
                " 'active',1,?)", (wp.now(),),
            )
            did = wp.insert_task(
                conn, recipient="seat-a", requester_seat="requester",
                subject="round trip responsibility", check_cmd="true",
            )
            context = self.set_current_drive(
                conn, did, state=wp.S_ESCALATED, cycles=1, idle_waits=6)
            wp.wake_attempt_open(
                conn, did, "seat-a", "pull", context["generation"])
            msg_id = self.insert_legacy_message(
                conn, did, "escalation", "requester",
                dedup_key=f"escalation:{did}:1:to:requester",
                subject="old escalation", body="old escalation",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='failed',attempts=1 WHERE id=?",
                (msg_id,),
            )
            conn.execute("UPDATE dispatch SET recipient='seat-b' WHERE id=?",
                         (did,))
            conn.execute("UPDATE dispatch SET recipient='seat-a' WHERE id=?",
                         (did,))
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM drive WHERE task_id=?", (did,),
        ).fetchone()[0], 0)
        wake = conn.execute(
            "SELECT resolved_ms,outcome FROM wake_attempt WHERE task_id=?",
            (did,),
        ).fetchone()
        self.assertGreater(wake["resolved_ms"], 0)
        self.assertEqual(wake["outcome"], "responsibility-changed")
        row = wp.fetch(conn, did)
        msg = conn.execute("SELECT * FROM task_msg WHERE id=?", (msg_id,)).fetchone()
        self.assertFalse(wp.message_is_current_responsibility(conn, msg, row))
        self.assertFalse(wp.waits_on_operator(conn, row))
        with conn:
            self.assertTrue(wp.wake_attempt_open(
                conn, did, "seat-a", "pull",
                wp.continuation_context(conn, row)["generation"],
            ), "the old unresolved wake clock must not suppress the new duty")

    def test_legacy_checkout_alert_stays_inert_and_current_fact_records_once(self):
        conn = wp.connect_writable()
        stamp = wp.now() - 60
        with conn:
            self.grant_commander(conn)
            old_id = self.record_current_message(
                conn, "hygiene", "checkout-dirty", "hygiene:/x:old:1",
                "commander-a", "old checkout alert", "dirty paths",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='failed',attempts=1,at_ms=?,"
                " escalated_to_operator=1"
                " WHERE id=?", (stamp, old_id),
            )
        old = conn.execute("SELECT * FROM task_msg WHERE id=?", (old_id,)).fetchone()
        self.assertFalse(wp.message_is_sendable(conn, old))
        orc = self.load_orc()
        repo = {"path": "/shared/main", "kind": "checkout", "exempt": ()}
        with mock.patch.object(orc, "WATCHED_CHECKOUTS", [repo]), \
                mock.patch.object(orc, "checkout_findings",
                                  return_value=["?? leaked.tmp\t2026-08-26"]), \
                mock.patch.object(wp, "bus_send", return_value=True) as send:
            orc.tick_checkout_hygiene(conn, dry=False)
            orc.tick_checkout_hygiene(conn, dry=False)
        self.assertEqual(send.call_count, 1)
        rows = conn.execute(
            "SELECT * FROM task_msg WHERE purpose='checkout-dirty' ORDER BY id"
        ).fetchall()
        self.assertEqual([row["target"] for row in rows],
                         ["commander-a", "role:commander"])
        self.assertEqual((rows[0]["at_ms"], rows[0]["attempts"]), (stamp, 1))
        self.assertIn("role-generation-", rows[1]["dedup_key"])
        conn.close()

    def test_legacy_commander_notices_never_revive(self):
        conn = wp.connect_writable()
        stamp = wp.now()
        old_ids = []
        with conn:
            for suffix in ("closed", "expired", "exhausted"):
                did = wp.insert_task(
                    conn, recipient=f"worker-{suffix}", subject=suffix,
                    check_cmd="true",
                )
                old_id = self.insert_legacy_message(
                    conn, did, "escalation", "commander-a",
                    dedup_key=f"escalation:{did}:1",
                    subject=suffix, body=suffix,
                )
                old_ids.append(old_id)
                if suffix == "closed":
                    conn.execute(
                        "UPDATE dispatch SET state='closed',resolution='done'"
                        " WHERE id=?", (did,),
                    )
                elif suffix == "expired":
                    conn.execute(
                        "UPDATE task_msg SET send_state='failed',attempts=1,"
                        " at_ms=? WHERE id=?",
                        (stamp - wp.SEND_RETRY_WINDOW_S - 1, old_id),
                    )
                else:
                    conn.execute(
                        "UPDATE task_msg SET send_state='failed',attempts=?"
                        " WHERE id=?", (wp.MAX_SEND_ATTEMPTS, old_id),
                    )
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM task_msg WHERE target='role:commander'"
        ).fetchone()[0], 0)
        for old_id in old_ids:
            old = conn.execute("SELECT * FROM task_msg WHERE id=?",
                               (old_id,)).fetchone()
            task = conn.execute("SELECT * FROM dispatch WHERE id=?",
                                (old["task_id"],)).fetchone()
            self.assertFalse(wp.message_is_sendable(conn, old, task))
        with mock.patch.object(wp, "bus_send") as send:
            self.assertEqual(wp.retry_unsent(conn, log=lambda *_: None), (0, 0))
        send.assert_not_called()
        conn.close()


class DependencyGraphTests(StoreTestCase):


    def open_task(self, subject, *extra, verb="open", to="bus-only-seat",
                  check="true", expect=0):
        out = self.run_cli(ORC, verb, "--to", to, "--subject", subject,
                           "--check", check, *extra, expect=expect)
        return out

    def id_of(self, subject):
        rows = [r for r in self.task_ids() if r["subject"] == subject]
        self.assertEqual(len(rows), 1, f"{subject}: {rows}")
        return rows[0]["id"]

    def row(self, task_id):
        return {r["id"]: r for r in self.task_ids()}[task_id]

    def msgs(self, task_id, purpose="dispatch"):
        conn = wp.connect_writable()
        return conn.execute("SELECT * FROM task_msg WHERE task_id=? AND purpose=?",
                            (task_id, purpose)).fetchall()


    def test_unknown_predecessor_is_refused(self):
        out = self.run_cli(ORC, "open", "--to", "tmux1", "--subject", "orphan",
                           "--check", "true", "--needs", "deadbeef", expect=1)
        self.assertEqual(self.task_ids(), [], out)

    def test_cycle_is_refused_at_write_time(self):
        self.open_task("dep root")
        root = self.id_of("dep root")
        self.open_task("dep middle", "--needs", root)
        middle = self.id_of("dep middle")
        self.open_task("dep leaf", "--needs", middle)
        leaf = self.id_of("dep leaf")

        self.run_cli(ORC, "link", root, "needs", leaf, expect=1)
        self.run_cli(ORC, "link", root, "needs", middle, expect=1)
        conn = wp.connect_writable()
        self.assertTrue(wp.needs_would_cycle(conn, root, leaf))
        self.assertFalse(wp.needs_would_cycle(conn, "unrelated", leaf))

        self.assertEqual(wp.needs_ids(conn, root), [])
        self.assertEqual(wp.verify_relations() + wp.verify_store(conn), [])

    def test_parent_goal_takes_no_needs(self):
        self.open_task("plain")
        plain = self.id_of("plain")
        self.run_cli(ORC, "open", "--to", "role:lead", "--subject", "goal",
                     "--workflow", "parent", "--no-check", "--needs", plain,
                     expect=1)


    def test_waiting_task_holds_its_dispatch_message_until_it_advances(self):
        self.open_task("pred one")
        pred = self.id_of("pred one")
        self.open_task("held successor", "--needs", pred, verb="dispatch")
        succ = self.id_of("held successor")
        self.assertEqual(self.row(succ)["state"], wp.WAITING_STATE)


        self.assertEqual(self.msgs(succ), [])

        conn = wp.connect_writable()
        self.assertNotIn(wp.WAITING_STATE,
                         wp.WORKFLOWS["dispatch"]["owed"])
        self.assertEqual(wp.open_predecessors(conn, succ)[0]["id"], pred)

        self.run_cli(ORC, "tick")
        self.assertEqual(self.row(succ)["state"], wp.WAITING_STATE)
        self.assertEqual(self.msgs(succ), [])


        self.run_cli(LEDGER, "close", pred, "--resolution", "done")
        self.run_cli(ORC, "tick")
        self.assertEqual(self.row(succ)["state"], "open")
        self.assertEqual(len(self.msgs(succ)), 1)
        self.assertEqual(self.row(succ)["deferred_dispatch"], 0)
        events = [e["kind"] for e in wp.connect_writable().execute(
            "SELECT kind FROM event WHERE dispatch_id=? ORDER BY id", (succ,))]
        self.assertEqual(events, [wp.EVENT_OPEN_WAITING, wp.EVENT_DEPS_CLEARED])
        self.run_cli(ORC, "verify")

    def test_stranded_held_dispatch_is_rescued_by_the_next_tick(self):


        self.open_task("pred for crash")
        pred = self.id_of("pred for crash")
        self.open_task("crash successor", "--needs", pred, verb="dispatch")
        succ = self.id_of("crash successor")
        self.run_cli(LEDGER, "close", pred, "--resolution", "done")
        self.run_cli(ORC, "tick")
        self.assertEqual(self.row(succ)["state"], "open")
        conn = wp.connect_writable()
        with conn:
            conn.execute("UPDATE dispatch SET deferred_dispatch=1 WHERE id=?",
                         (succ,))
            conn.execute("DELETE FROM task_msg WHERE task_id=?", (succ,))
        self.assertEqual(self.msgs(succ), [])
        self.run_cli(ORC, "tick")
        self.assertEqual(len(self.msgs(succ)), 1)
        self.assertEqual(self.row(succ)["deferred_dispatch"], 0)
        self.run_cli(ORC, "verify")

    def test_stranded_flag_on_a_closed_task_clears_without_sending(self):


        self.open_task("pred for moot")
        pred = self.id_of("pred for moot")
        self.open_task("moot successor", "--needs", pred, verb="dispatch")
        succ = self.id_of("moot successor")
        self.run_cli(LEDGER, "close", pred, "--resolution", "done")
        self.run_cli(ORC, "tick")
        conn = wp.connect_writable()
        with conn:
            conn.execute("UPDATE dispatch SET deferred_dispatch=1 WHERE id=?",
                         (succ,))
            conn.execute("DELETE FROM task_msg WHERE task_id=?", (succ,))
        self.run_cli(LEDGER, "close", succ, "--resolution", "dropped")
        self.run_cli(ORC, "tick")
        self.assertEqual(self.msgs(succ), [])
        self.assertEqual(self.row(succ)["deferred_dispatch"], 0)
        self.run_cli(ORC, "verify")

    def test_open_verb_advances_without_ever_sending(self):
        self.open_task("quiet pred")
        pred = self.id_of("quiet pred")
        self.open_task("quiet successor", "--needs", pred)
        succ = self.id_of("quiet successor")
        self.run_cli(LEDGER, "close", pred, "--resolution", "done")
        self.run_cli(ORC, "tick")
        self.assertEqual(self.row(succ)["state"], "open")
        self.assertEqual(self.msgs(succ), [])

    def test_dropped_predecessor_still_counts_as_closed(self):
        self.open_task("abandoned pred")
        pred = self.id_of("abandoned pred")
        self.open_task("successor of dropped", "--needs", pred)
        succ = self.id_of("successor of dropped")
        self.run_cli(LEDGER, "close", pred, "--resolution", "dropped")
        self.run_cli(ORC, "tick")
        self.assertEqual(self.row(succ)["state"], "open")
        note = wp.connect_writable().execute(
            "SELECT note FROM event WHERE dispatch_id=? AND kind=?",
            (succ, wp.EVENT_DEPS_CLEARED)).fetchone()["note"]
        self.assertIn("dropped", note)
        self.assertIn(pred, note)

    def test_advance_waits_for_every_predecessor(self):
        self.open_task("fan-in a")
        self.open_task("fan-in b")
        a, b = self.id_of("fan-in a"), self.id_of("fan-in b")
        self.open_task("fan-in join", "--needs", a, "--needs", b)
        join = self.id_of("fan-in join")
        self.run_cli(LEDGER, "close", a, "--resolution", "done")
        self.run_cli(ORC, "tick")
        self.assertEqual(self.row(join)["state"], wp.WAITING_STATE)
        self.run_cli(LEDGER, "close", b, "--resolution", "superseded")
        self.run_cli(ORC, "tick")
        self.assertEqual(self.row(join)["state"], "open")

    def test_closed_predecessors_behave_exactly_as_today(self):
        self.open_task("already done")
        pred = self.id_of("already done")
        self.run_cli(LEDGER, "close", pred, "--resolution", "done")
        out = self.open_task("straight to open", "--needs", pred, verb="dispatch")
        succ = self.id_of("straight to open")
        self.assertEqual(self.row(succ)["state"], "open")
        self.assertIn("open, owed by", out)
        self.assertEqual(len(self.msgs(succ)), 1)
        kinds = [e["kind"] for e in wp.connect_writable().execute(
            "SELECT kind FROM event WHERE dispatch_id=?", (succ,))]
        self.assertEqual(kinds, ["open"])

    def test_pr_task_advances_into_authoring(self):
        self.open_task("pr pred")
        pred = self.id_of("pr pred")
        self.run_cli(ORC, "open", "--to", "bus-only-seat", "--subject", "pr held",
                     "--workflow", "pr", "--owner", "bus-only-seat",
                     "--reviewer", "bus-only-rev", "--check", "echo h",
                     "--needs", pred)
        pr_id = self.id_of("pr held")
        self.assertEqual(self.row(pr_id)["state"], wp.WAITING_STATE)
        self.run_cli(LEDGER, "close", pred, "--resolution", "done")
        self.run_cli(ORC, "tick")
        self.assertEqual(self.row(pr_id)["state"], "authoring")
        self.run_cli(ORC, "verify")

    def test_advance_restamps_the_check_from_the_stored_duration(self):
        self.open_task("cadence pred")
        pred = self.id_of("cadence pred")
        self.open_task("cadence successor", "--needs", pred, "--after", "2h")
        succ = self.id_of("cadence successor")
        conn = wp.connect_writable()
        with conn:
            conn.execute("UPDATE dispatch SET check_after=? WHERE id=?",
                         (wp.now() - 10_000, succ))
        self.run_cli(LEDGER, "close", pred, "--resolution", "done")
        self.run_cli(ORC, "tick")
        due_in = self.row(succ)["check_after"] - wp.now()
        self.assertGreater(due_in, wp.parse_after("2h") - 120)
        self.assertLessEqual(due_in, wp.parse_after("2h"))


    def overdue(self, task_id, seconds_ago=7200):
        conn = wp.connect_writable()
        with conn:
            conn.execute("UPDATE dispatch SET deadline_ms=? WHERE id=?",
                         (wp.now() - seconds_ago, task_id))

    def deadline_chases(self, task_id):


        return wp.connect_writable().execute(
            "SELECT COUNT(*) FROM event WHERE dispatch_id=? AND"
            " kind='auto-chase'"
            " AND note LIKE 'engine: DEADLINE OVERDUE:%'", (task_id,)).fetchone()[0]

    def test_deadline_escalates_once_per_cooldown(self):
        self.open_task("late task", "--deadline", "1d")
        late = self.id_of("late task")
        self.assertGreater(self.row(late)["deadline_ms"], wp.now())
        self.overdue(late)
        for _ in range(3):
            self.run_cli(ORC, "tick")
        self.assertEqual(self.deadline_chases(late), 1)
        self.assertEqual(self.row(late)["chases_total"], 1)

        conn = wp.connect_writable()
        with conn:
            conn.execute("UPDATE event SET at_ms=at_ms-? WHERE dispatch_id=?",
                         (wp.DEADLINE_COOLDOWN_S + 600, late))
        self.run_cli(ORC, "tick")
        self.assertEqual(self.deadline_chases(late), 2)
        self.run_cli(ORC, "verify")

    def test_second_deadline_cycle_notifies_commander_again(self):


        conn = wp.connect_writable()
        with conn:
            conn.execute("INSERT OR REPLACE INTO seat (agent_id, handle,"
                         " status, addressable, refreshed_ms) VALUES (?,?,?,?,?)",
                         ("cmdr-1", "example-host/fleet-command-tmux1", "active", 1,
                          wp.now()))
            conn.execute("INSERT INTO role_assignment (role, agent_id,"
                         " granted_by, granted_ms) VALUES (?,?,?,?)",
                         ("commander", "cmdr-1", "test", wp.now()))
        self.member_source(conn)
        conn.close()
        bus = Path(self.tmp.name) / "deadline-bus.sh"
        bus.write_text(
            "#!/usr/bin/env bash\n"
            "if [ \"$1\" = members ]; then\n"
            "  echo '{\"agent_id\":\"cmdr-1\",\"handle\":"
            "\"example-host/fleet-command-tmux1\",\"status\":\"active\","
            "\"addressable\":true}'\n"
            "else\n"
            "  printf '{\"msg_id\":\"m-%s\","
            "\"recipient_agent_ids\":[\"%s\"]}\\n' \"$3\" \"$3\"\n"
            "fi\n"
        )
        bus.chmod(0o755)
        self.env["NW_BUS_CLI"] = str(bus)
        self.open_task("late twice", "--deadline", "1d")
        late = self.id_of("late twice")
        self.overdue(late)
        self.run_cli(ORC, "tick")
        conn = wp.connect_writable()
        with conn:
            conn.execute("UPDATE event SET at_ms=at_ms-? WHERE dispatch_id=?",
                         (wp.DEADLINE_COOLDOWN_S + 600, late))
        self.member_source(conn)
        conn.close()
        self.run_cli(ORC, "tick")
        conn = wp.connect_writable()
        keys = [r["dedup_key"] for r in conn.execute(
            "SELECT dedup_key FROM task_msg WHERE task_id=? AND"
            " purpose='escalation' ORDER BY id", (late,))]
        self.member_source(conn)
        conn.close()
        self.assertEqual(len(keys), 2)
        self.assertIn(f"deadline:{late}:", keys[0])
        self.assertIn(":n1:attention-event=", keys[0])
        self.assertIn(f"deadline:{late}:", keys[1])
        self.assertIn(":n2:attention-event=", keys[1])

    def _age_events(self, task_id):
        conn = wp.connect_writable()
        with conn:
            conn.execute("UPDATE event SET at_ms=at_ms-? WHERE dispatch_id=?",
                         (wp.DEADLINE_COOLDOWN_S + 600, task_id))
        conn.close()

    def test_unanswered_deadline_keeps_refiring_until_the_task_changes(self):

        self.open_task("doomed promise", "--deadline", "1d")
        did = self.id_of("doomed promise")
        self.overdue(did)
        self.run_cli(ORC, "tick")
        self._age_events(did)
        self.run_cli(ORC, "tick")
        self._age_events(did)
        self.run_cli(ORC, "tick")
        row = self.row(did)
        self.assertEqual(row["state"], "open",
                         "the unfinished task must stay open and owed")
        self.assertEqual(row["resolution"], "", "no hidden closure")
        self.assertGreater(row["deadline_ms"], 0, "the alarm stays armed")
        self.assertEqual(self.deadline_chases(did), 3)
        self.assertIn(did, self.run_cli(LEDGER, "brief"))

        self.run_cli(ORC, "tick")
        self.assertEqual(self.deadline_chases(did), 3)
        self.run_cli(ORC, "verify")

    def test_answered_deadline_does_not_self_retire(self):


        self.open_task("slow but alive", "--deadline", "1d")
        did = self.id_of("slow but alive")
        self.overdue(did)
        self.run_cli(ORC, "tick")
        self._age_events(did)
        self.run_cli(ORC, "tick")
        self._age_events(did)
        conn = wp.connect_writable()
        with conn:
            wp.record(conn, did, "note", "still on it - blocked upstream")
        conn.close()
        self.run_cli(ORC, "tick")
        row = self.row(did)
        self.assertNotEqual(row["state"], "closed")
        self.assertEqual(self.deadline_chases(did), 3,
                         "the ladder continues for a task that answered")

    def test_deadline_on_a_waiting_task_names_the_planning_failure(self):
        self.open_task("slow pred")
        pred = self.id_of("slow pred")
        self.open_task("promised successor", "--needs", pred, "--deadline", "1d")
        succ = self.id_of("promised successor")
        self.overdue(succ)
        self.run_cli(ORC, "tick")
        self.assertEqual(self.row(succ)["state"], wp.WAITING_STATE)
        note = wp.connect_writable().execute(
            "SELECT note FROM event WHERE dispatch_id=? AND kind='auto-chase'"
            " ORDER BY id DESC LIMIT 1", (succ,)).fetchone()["note"]
        self.assertIn("DEADLINE OVERDUE", note)
        self.assertIn("has not even started", note)
        self.assertIn(pred, note)

    def test_deadline_is_silent_before_it_passes_and_after_close(self):
        self.open_task("on time", "--deadline", "1d")
        task = self.id_of("on time")
        self.run_cli(ORC, "tick")
        self.assertEqual(self.deadline_chases(task), 0)
        self.overdue(task)
        self.run_cli(LEDGER, "close", task, "--resolution", "done")
        self.run_cli(ORC, "tick")
        self.assertEqual(self.deadline_chases(task), 0)


    def breaker_task(self, subject, check):
        marker = Path(self.tmp.name) / f"{subject.replace(' ', '-')}.log"
        self.run_cli(ORC, "open", "--to", "bus-only-seat", "--subject", subject,
                     "--check", check, "--breaker",
                     f"echo fired >> {marker}")
        return self.id_of(subject), marker

    def fire_count(self, marker):
        return len(marker.read_text().splitlines()) if marker.exists() else 0

    def test_breaker_fires_once_after_three_consecutive_failures(self):
        task, marker = self.breaker_task("wedged pipeline", "false")
        for n in (1, 2):
            self.run_cli(ORC, "tick")
            self.assertEqual(self.fire_count(marker), 0, f"fired at {n} failures")
            self.assertEqual(self.row(task)["check_fail_streak"], n)
        self.run_cli(ORC, "tick")
        self.assertEqual(self.fire_count(marker), 1)
        self.assertEqual(self.row(task)["check_fail_streak"], 0)

        self.assertEqual(self.row(task)["state"], "open")
        ev = wp.connect_writable().execute(
            "SELECT * FROM event WHERE dispatch_id=? AND kind=?",
            (task, wp.EVENT_BREAKER_FIRED)).fetchall()
        self.assertEqual(len(ev), 1)
        self.assertIn("exit 0", ev[0]["note"])

        for _ in range(3):
            self.run_cli(ORC, "tick")
        self.assertEqual(self.fire_count(marker), 1)

        conn = wp.connect_writable()
        with conn:
            conn.execute("UPDATE event SET at_ms=at_ms-? WHERE dispatch_id=?",
                         (wp.BREAKER_COOLDOWN_S + 600, task))
        self.run_cli(ORC, "tick")
        self.assertEqual(self.fire_count(marker), 2)
        self.run_cli(ORC, "verify")

    def test_a_passing_check_resets_the_streak(self):
        flag = Path(self.tmp.name) / "gate"
        flag.write_text("fail\n")
        task, marker = self.breaker_task(
            "flapping check", f"grep -q ok {flag}")
        for _ in range(2):
            self.run_cli(ORC, "tick")
        self.assertEqual(self.row(task)["check_fail_streak"], 2)
        flag.write_text("ok\n")
        self.run_cli(ORC, "tick")
        self.assertEqual(self.row(task)["check_fail_streak"], 0)
        self.assertEqual(self.fire_count(marker), 0)
        flag.write_text("fail\n")
        for _ in range(2):
            self.run_cli(ORC, "tick")
        self.assertEqual(self.fire_count(marker), 0)

    def test_unknown_check_neither_counts_nor_clears(self):


        code, out = wp.run_breaker("sleep 5", timeout=1)
        self.assertEqual(code, 124)
        self.assertIn("timed out", out)
        verdict, _ = wp.run_guard("sleep 5", timeout=1)
        self.assertEqual(verdict, wp.GUARD_UNKNOWN)

    def test_breaker_without_a_check_is_refused(self):
        self.run_cli(ORC, "open", "--to", "tmux1", "--subject", "no check",
                     "--no-check", "--breaker", "true", expect=1)
        self.assertFalse(Path(self.env["DISPATCH_LEDGER_DB"]).exists(),
                         "a refused open must not create the store")

    def test_breaker_output_and_exit_code_are_recorded(self):
        self.run_cli(ORC, "open", "--to", "bus-only-seat", "--subject", "noisy breaker",
                     "--check", "false", "--breaker",
                     "echo requeued the job; exit 3")
        task = self.id_of("noisy breaker")
        for _ in range(3):
            self.run_cli(ORC, "tick")
        note = wp.connect_writable().execute(
            "SELECT note FROM event WHERE dispatch_id=? AND kind=?",
            (task, wp.EVENT_BREAKER_FIRED)).fetchone()["note"]
        self.assertIn("exit 3", note)
        self.assertIn("requeued the job", note)


    def test_verify_catches_a_hand_inserted_cycle(self):
        self.open_task("cycle a")
        self.open_task("cycle b")
        a, b = self.id_of("cycle a"), self.id_of("cycle b")
        conn = wp.connect_writable()
        with conn:
            for src, dst in ((a, b), (b, a)):
                conn.execute("INSERT OR REPLACE INTO edge (src, kind, dst, at_ms,"
                             " actor, note) VALUES (?,'needs',?,0,'hand','')",
                             (src, dst))
        out = self.run_cli(ORC, "verify", expect=1)
        self.assertIn("needs graph has a cycle", out)

    def test_verify_catches_a_stuck_advance(self):
        self.open_task("stuck pred")
        pred = self.id_of("stuck pred")
        self.open_task("stuck successor", "--needs", pred)
        succ = self.id_of("stuck successor")
        self.run_cli(ORC, "verify")
        self.run_cli(LEDGER, "close", pred, "--resolution", "done")

        out = self.run_cli(ORC, "verify", expect=1)
        self.assertIn("stuck-advance", out)
        self.assertIn(succ, out)
        self.run_cli(ORC, "tick")
        self.run_cli(ORC, "verify")

    def test_verify_catches_an_open_task_with_no_next_check(self):
        self.open_task("unscheduled")
        task = self.id_of("unscheduled")
        conn = wp.connect_writable()
        with conn:
            conn.execute("UPDATE dispatch SET check_after=0 WHERE id=?", (task,))
        out = self.run_cli(ORC, "verify", expect=1)
        self.assertIn("no next check", out)

    def test_frontier_lists_exactly_the_actionable_set(self):
        self.open_task("frontier root")
        root = self.id_of("frontier root")
        self.open_task("frontier next", "--needs", root)
        nxt = self.id_of("frontier next")
        self.open_task("frontier unrelated")
        unrelated = self.id_of("frontier unrelated")
        out = self.run_cli(ORC, "board")
        data = json.loads(self.run_cli(ORC, "board", "--json"))
        frontier = [t["id"] for t in data["tasks"] if t["frontier"]]
        self.assertIn("ready now", out)
        self.assertIn(root, frontier)
        self.assertNotIn(nxt, frontier)
        self.assertNotIn(unrelated, frontier)
        self.run_cli(LEDGER, "close", root, "--resolution", "done")
        self.run_cli(ORC, "tick")
        out = self.run_cli(ORC, "board")
        data = json.loads(self.run_cli(ORC, "board", "--json"))
        frontier = [t["id"] for t in data["tasks"] if t["frontier"]]
        self.assertIn("ready now", out)
        self.assertIn(nxt, frontier)
        self.assertNotIn(root, frontier)

    def test_board_never_says_a_waiting_task_is_owed(self):
        self.open_task("board pred")
        pred = self.id_of("board pred")
        self.open_task("board successor", "--needs", pred, to="tmux9")
        out = self.run_cli(ORC, "board")
        data = json.loads(self.run_cli(ORC, "board", "--json"))
        task = next(t for t in data["tasks"] if t["subject"] == "board successor")
        self.assertEqual(task["owner"], "nobody")
        self.assertEqual(task["blockers"], [pred])
        self.assertIn(f"waits on {pred}", out)

    def test_tree_renders_needs_edges_both_ways(self):
        self.open_task("tree root")
        root = self.id_of("tree root")
        self.open_task("tree leaf", "--needs", root)
        leaf = self.id_of("tree leaf")
        out = self.run_cli(ORC, "tree", root)
        self.assertIn(f"needed by {leaf}", out)
        out = self.run_cli(ORC, "tree", leaf)
        self.assertIn(f"needs {root} (open)", out)

        out = self.run_cli(ORC, "tree")
        self.assertIn(root, out)

    def test_dry_run_reports_the_dependency_passes_and_writes_nothing(self):
        self.open_task("dry pred")
        pred = self.id_of("dry pred")
        self.open_task("dry successor", "--needs", pred, verb="dispatch")
        succ = self.id_of("dry successor")
        self.run_cli(LEDGER, "close", pred, "--resolution", "done")
        out = self.run_cli(ORC, "tick", "--dry-run")
        self.assertIn(f"DRY would open {succ}", out)
        self.assertEqual(self.row(succ)["state"], wp.WAITING_STATE)
        self.assertEqual(self.msgs(succ), [])


class ReviewPoolTests(StoreTestCase):


    def grant_pool(self, conn, members=("rp-a", "rp-b", "rp-c")):
        with conn:
            for i, m in enumerate(members):
                conn.execute("INSERT INTO role_assignment (role, agent_id,"
                             " granted_by, granted_ms) VALUES"
                             " ('reviewer-pool', ?, 'test', ?)", (m, i))
                conn.execute(
                    "INSERT INTO seat (agent_id, handle, aliases, host, tmux,"
                    " status, addressable, updated_at, refreshed_ms) VALUES"
                    " (?,?,'','otherhost','','active',1,'',0)", (m, m))
        return members

    def load_orc(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "orc_for_pool_tests", ROOT / "scripts" / "fleet-orchestrator.py")
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    def make_pr(self, conn, reviewer, subject="pool pr"):
        with conn:
            did = wp.insert_task(conn, recipient="tmux1", subject=subject,
                                 workflow="pr", repo="example-storage",
                                 owner_seat="tmux1", reviewer_seat=reviewer,
                                 check_cmd="echo h1")
            msg = self.record_current_message(conn, did, "author-request", f"dispatch:{did}",
                                "tmux1", "s", "b")
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id=?,"
                " recipient_agent_id='owner-id' WHERE id=?",
                (f"owner-{did}", msg),
            )
            return did

    def test_pool_pick_is_least_loaded_with_deterministic_ties(self):
        conn = wp.connect_writable()
        a, b, c = self.grant_pool(conn)
        self.assertEqual(wp.pool_pick(conn, "reviewer-pool"), a)
        self.make_pr(conn, a)
        self.assertEqual(wp.pool_pick(conn, "reviewer-pool"), b)
        self.make_pr(conn, b); self.make_pr(conn, b)
        self.assertEqual(wp.pool_pick(conn, "reviewer-pool"), c)
        self.assertEqual(wp.pool_pick(conn, "reviewer-pool", exclude={c}), a)
        self.assertIsNone(wp.pool_pick(conn, "reviewer-pool",
                                       exclude={a, b, c}))

    def test_pool_role_resolves_via_recipient_resolution(self):
        conn = wp.connect_writable()
        a, *_ = self.grant_pool(conn)
        got = wp.resolve_recipient(conn, "role:reviewer-pool")
        self.assertEqual(got["agent_id"], a)

    def test_author_exclusion_uses_the_identity_that_received_the_pr(self):
        conn = wp.connect_writable()
        orc = self.load_orc()
        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,aliases,host,tmux,status,"
                " addressable,updated_at,refreshed_ms) VALUES"
                " ('seat-a','host/owner-short','',?,'tmux=0:7.0 win=model',"
                " 'active',1,'new',2)",
                (socket.gethostname().split('.', 1)[0],),
            )
            did = wp.insert_task(
                conn, recipient="owner-short", subject="author identity",
                workflow="pr", repo="example-storage", owner_seat="owner-short",
                reviewer_seat="role:reviewer-pool", check_cmd="echo h",
            )
            msg_id = self.record_current_message(
                conn, did, "author-request", f"dispatch:{did}", "owner-short",
                "subject", "body",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-owner',"
                " recipient_agent_id='seat-b' WHERE id=?", (msg_id,),
            )
            conn.execute("UPDATE dispatch SET state='awaiting-review'"
                         " WHERE id=?", (did,))
        self.assertEqual(orc.author_exclusion(conn, wp.fetch(conn, did)),
                         {"seat-a", "seat-b"})

    def test_author_exclusion_uses_latest_owner_work_handoff(self):
        conn = wp.connect_writable()
        orc = self.load_orc()
        with conn:
            did = wp.insert_task(
                conn, recipient="owner-short", subject="latest author handoff",
                workflow="pr", repo="example-storage", owner_seat="owner-short",
                reviewer_seat="role:reviewer-pool", check_cmd="echo h",
            )
            first = self.record_current_message(conn, did, "author-request", f"dispatch:{did}",
                                  "owner-short", "s", "b")
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-owner-b',"
                " recipient_agent_id='seat-b' WHERE id=?", (first,),
            )
            conn.execute("UPDATE dispatch SET state='awaiting-review'"
                         " WHERE id=?", (did,))
            conn.execute("UPDATE dispatch SET state='fixing' WHERE id=?",
                         (did,))
            latest = self.record_current_message(conn, did, "findings", f"findings:{did}:n1",
                                   "owner-short", "s", "b")
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-owner-a',"
                " recipient_agent_id='seat-a' WHERE id=?", (latest,),
            )
            conn.execute("UPDATE dispatch SET state='awaiting-review'"
                         " WHERE id=?", (did,))
        self.assertEqual(orc.author_exclusion(conn, wp.fetch(conn, did)),
                         {"seat-a", "seat-b"})

    def test_failed_later_handoff_keeps_last_actual_author_excluded(self):
        conn = wp.connect_writable()
        orc = self.load_orc()
        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,aliases,host,tmux,status,"
                " addressable,updated_at,refreshed_ms) VALUES"
                " ('seat-a','host/owner-short','','otherhost','',"
                " 'active',1,'',0)"
            )
            did = wp.insert_task(
                conn, recipient="owner-short", subject="last actual author",
                workflow="pr", repo="example-storage", owner_seat="owner-short",
                reviewer_seat="role:reviewer-pool", check_cmd="echo h",
            )
            first = self.record_current_message(conn, did, "author-request", f"dispatch:{did}",
                                  "owner-short", "s", "b")
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-owner-b',"
                " recipient_agent_id='seat-b' WHERE id=?", (first,),
            )
            conn.execute("UPDATE dispatch SET state='awaiting-review'"
                         " WHERE id=?", (did,))
            conn.execute("UPDATE dispatch SET state='fixing' WHERE id=?",
                         (did,))
            failed = self.record_current_message(conn, did, "findings", f"findings:{did}:n1",
                                   "owner-short", "s", "b")
            conn.execute("UPDATE task_msg SET send_state='failed' WHERE id=?",
                         (failed,))
            conn.execute("UPDATE dispatch SET state='awaiting-review'"
                         " WHERE id=?", (did,))
        self.assertEqual(orc.author_exclusion(conn, wp.fetch(conn, did)),
                         {"seat-a", "seat-b"})

    def test_author_a_to_b_to_a_gap_excludes_last_actual_b(self):
        conn = wp.connect_writable()
        seat_a, seat_b = self.grant_pool(conn, ("seat-a", "seat-b"))
        orc = self.load_orc()
        with conn:
            did = wp.insert_task(
                conn, recipient="owner-a", subject="a b a author gap",
                workflow="pr", repo="example-storage", owner_seat="owner-a",
                reviewer_seat="role:reviewer-pool", check_cmd="echo h",
            )
            first = self.record_current_message(conn, did, "author-request", f"dispatch:{did}",
                                  "owner-a", "s", "b")
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-a',"
                " recipient_agent_id=? WHERE id=?", (seat_a, first),
            )
            conn.execute("UPDATE dispatch SET state='awaiting-review'"
                         " WHERE id=?", (did,))
            conn.execute("UPDATE dispatch SET state='fixing' WHERE id=?",
                         (did,))
            conn.execute("UPDATE dispatch SET owner_seat='owner-b' WHERE id=?",
                         (did,))
            second = self.record_current_message(conn, did, "findings", f"findings:{did}:b",
                                   "owner-b", "s", "b")
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-b',"
                " recipient_agent_id=? WHERE id=?", (seat_b, second),
            )
            conn.execute("UPDATE dispatch SET owner_seat='owner-a' WHERE id=?",
                         (did,))
        row = wp.fetch(conn, did)
        self.assertIn("deferred", wp.resolve_owed_recipient(conn, row))
        self.assertEqual(orc.author_exclusion(conn, row), {seat_a, seat_b})
        pinned = orc.pin_pool_reviewer(conn, row)
        self.assertEqual(pinned["reviewer_seat"], "role:reviewer-pool")

    def test_new_author_round_rotates_a_pinned_reviewer(self):
        conn = wp.connect_writable()
        seat_a, seat_b = self.grant_pool(conn, ("seat-a", "seat-b"))
        orc = self.load_orc()
        did = self.make_pr(conn, "role:reviewer-pool",
                           subject="reviewer later authors fixes")
        first = orc.pin_pool_reviewer(conn, wp.fetch(conn, did))
        self.assertEqual(first["reviewer_seat"], seat_a)
        with conn:
            conn.execute("UPDATE dispatch SET state='awaiting-review' WHERE id=?",
                         (did,))
            conn.execute("UPDATE dispatch SET state='fixing',owner_seat=?"
                         " WHERE id=?", (seat_a, did))
            fixes = self.record_current_message(
                conn, did, "findings", f"findings:{did}:round2", seat_a,
                "fixes", "fixes",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-fixes',"
                " recipient_agent_id=? WHERE id=?", (seat_a, fixes),
            )
            conn.execute("UPDATE dispatch SET state='awaiting-review' WHERE id=?",
                         (did,))
        second = orc.pin_pool_reviewer(conn, wp.fetch(conn, did))
        self.assertEqual(second["reviewer_seat"], seat_b,
                         "an author in any round cannot remain the reviewer")

    def test_recipient_change_keeps_actual_owner_delivery_identity(self):
        conn = wp.connect_writable()
        seat_a, seat_b = self.grant_pool(conn, ("seat-a", "seat-b"))
        orc = self.load_orc()
        with conn:
            did = wp.insert_task(
                conn, recipient="owner-short", subject="recipient changed",
                workflow="pr", repo="example-storage", owner_seat="owner-short",
                reviewer_seat="role:reviewer-pool", check_cmd="echo h",
            )
            msg = self.record_current_message(conn, did, "author-request", f"dispatch:{did}",
                                "owner-short", "s", "b")
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-owner-a',"
                " recipient_agent_id=? WHERE id=?", (seat_a, msg),
            )


            conn.execute("UPDATE dispatch SET recipient='notifier' WHERE id=?",
                         (did,))
        row = wp.fetch(conn, did)
        self.assertEqual(orc.author_exclusion(conn, row), {seat_a})
        self.assertEqual(orc.pin_pool_reviewer(conn, row)["reviewer_seat"],
                         seat_b)

    def test_unknown_accepted_owner_identity_parks_pool_selection(self):
        conn = wp.connect_writable()
        self.grant_pool(conn, ("seat-a", "seat-b"))
        orc = self.load_orc()
        with conn:
            conn.execute("UPDATE seat SET handle='host/owner-short'"
                         " WHERE agent_id='seat-a'")
            did = wp.insert_task(
                conn, recipient="owner-short", subject="unknown author",
                workflow="pr", repo="example-storage", owner_seat="owner-short",
                reviewer_seat="role:reviewer-pool", check_cmd="echo h",
            )
            msg = self.record_current_message(conn, did, "author-request", f"dispatch:{did}",
                                "owner-short", "s", "b")
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-unknown',"
                " recipient_agent_id='' WHERE id=?", (msg,),
            )
        with mock.patch.object(wp, "_agent_bus_rows", return_value=None):
            row = wp.fetch(conn, did)
            self.assertEqual(orc.author_exclusion(conn, row), {"seat-a"})
            self.assertEqual(
                orc.pin_pool_reviewer(conn, row)["reviewer_seat"],
                "role:reviewer-pool",
            )
            self.assertTrue(wp.reviewer_pool_unavailable(conn, row))
        out = self.run_cli(LEDGER, "brief")
        self.assertIn("actual historical author identity is unknown", out)

    def test_role_placeholder_without_owner_delivery_parks_pool_selection(self):
        conn = wp.connect_writable()
        author, _other = self.grant_pool(conn, ("author-seat", "other-seat"))
        orc = self.load_orc()
        with conn:
            conn.execute(
                "INSERT INTO role_assignment(role,agent_id,granted_by,granted_ms)"
                " VALUES ('commander','commander-seat','test',9)"
            )
            did = wp.insert_task(
                conn, recipient="role:commander",
                subject="auto-registered unknown author",
                workflow="pr", repo="example-storage",
                owner_seat="role:commander",
                reviewer_seat="role:reviewer-pool",
                check_cmd="echo h",
            )
        row = wp.fetch(conn, did)
        identities, unknown = wp.owner_review_identities(conn, row)
        self.assertEqual(identities, set())
        self.assertTrue(unknown)
        self.assertEqual(
            orc.pin_pool_reviewer(conn, row)["reviewer_seat"],
            "role:reviewer-pool",
        )
        self.assertTrue(wp.reviewer_pool_unavailable(conn, row))
        self.assertEqual(wp.pool_pick(conn, "reviewer-pool"), author,
                         "without the unknown-author stop this fixture would"
                         " choose the real author for self-review")

    def test_legacy_dispatch_after_owner_change_never_guesses_author(self):
        conn = wp.connect_writable()
        self.grant_pool(conn, ("seat-a", "seat-b"))
        orc = self.load_orc()
        with conn:
            did = wp.insert_task(
                conn, recipient="seat-a", subject="legacy owner changed",
                workflow="pr", repo="example-storage", owner_seat="seat-a",
                reviewer_seat="role:reviewer-pool", check_cmd="echo h",
            )
            msg = self.insert_legacy_message(
                conn, did, "dispatch", "seat-a",
                dedup_key=f"dispatch:{did}", subject="s", body="b")
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-legacy',"
                " recipient_agent_id='seat-a' WHERE id=?", (msg,),
            )
            conn.execute("UPDATE dispatch SET owner_seat='seat-b' WHERE id=?",
                         (did,))
        row = wp.fetch(conn, did)
        identities, unknown = wp.owner_review_identities(conn, row)
        self.assertEqual(identities, {"seat-b"})
        self.assertTrue(unknown)
        self.assertEqual(
            orc.pin_pool_reviewer(conn, row)["reviewer_seat"],
            "role:reviewer-pool",
        )
        self.assertTrue(wp.reviewer_pool_unavailable(conn, row))

    def test_failed_owner_handoff_uses_cache_to_prevent_self_review(self):
        conn = wp.connect_writable()
        author, other = self.grant_pool(conn, ("author-id", "other-id"))
        orc = self.load_orc()
        with conn:
            conn.execute("UPDATE seat SET handle='host/owner-short'"
                         " WHERE agent_id=?", (author,))
            did = wp.insert_task(
                conn, recipient="owner-short", subject="failed owner handoff",
                workflow="pr", repo="example-storage", owner_seat="owner-short",
                reviewer_seat="role:reviewer-pool", check_cmd="echo h",
            )
            msg = self.record_current_message(conn, did, "author-request", f"dispatch:{did}",
                                "owner-short", "s", "b")
            conn.execute("UPDATE task_msg SET send_state='failed' WHERE id=?",
                         (msg,))
            conn.execute("UPDATE dispatch SET state='awaiting-review'"
                         " WHERE id=?", (did,))
        excluded = orc.author_exclusion(conn, wp.fetch(conn, did))
        self.assertEqual(excluded, {author})
        self.assertEqual(wp.pool_pick(conn, "reviewer-pool", exclude=excluded),
                         other)

    def test_pool_load_uses_actual_review_request_recipient(self):
        conn = wp.connect_writable()
        a, b = self.grant_pool(conn, ("seat-a", "seat-b"))
        with conn:
            conn.execute("UPDATE seat SET handle='host/reviewer'"
                         " WHERE agent_id=?", (a,))
            did = wp.insert_task(
                conn, recipient="owner", subject="actual reviewer load",
                workflow="pr", repo="example-storage", owner_seat="owner",
                reviewer_seat="host/reviewer", check_cmd="echo h",
            )
            conn.execute("UPDATE dispatch SET state='awaiting-review'"
                         " WHERE id=?", (did,))
            msg = self.record_current_message(conn, did, "review-request", f"rr:{did}",
                                "host/reviewer", "s", "b")
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-review-b',"
                " recipient_agent_id=? WHERE id=?", (b, msg),
            )
            conn.execute("UPDATE dispatch SET state='fixing' WHERE id=?", (did,))
        self.assertEqual(wp.pool_pick(conn, "reviewer-pool"), a)

    def test_checkout_obligation_matches_only_actual_accepted_recipient(self):
        conn = wp.connect_writable()
        orc = self.load_orc()
        with conn:
            did = wp.insert_task(conn, recipient="host/old",
                                 subject="actual checkout owner",
                                 check_cmd="true")
            msg = self.record_current_message(conn, did, "dispatch", f"dispatch:{did}",
                                "host/old", "s", "b")
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-seat-b',"
                " recipient_agent_id='seat-b' WHERE id=?", (msg,),
            )
        names_a = {"seat-a", "host/a", "host/old"}
        names_b = {"seat-b", "host/b"}
        self.assertEqual(orc._owed_by_seat(conn, names_a, "seat-a"), [])
        self.assertEqual([r[0]["id"] for r in
                          orc._owed_by_seat(conn, names_b, "seat-b")], [did])

    def test_checkout_does_not_guess_a_legacy_rewritten_recipient(self):
        conn = wp.connect_writable()
        orc = self.load_orc()
        with conn:
            did = wp.insert_task(conn, recipient="worker-short",
                                 subject="legacy checkout uncertainty",
                                 check_cmd="true")
            msg = self.record_current_message(conn, did, "dispatch", f"dispatch:{did}",
                                "seat-b", "s", "b")
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='legacy-m'"
                " WHERE id=?", (msg,),
            )
        with mock.patch.object(wp, "_agent_bus_rows", return_value=None):
            owed = orc._owed_by_seat(conn, {"seat-b", "host/seat-b"},
                                     "seat-b")
        self.assertEqual(owed, [])

    def test_pin_on_pr_ready_and_note(self):
        conn = wp.connect_writable()
        a, *_ = self.grant_pool(conn)
        orc = self.load_orc()
        did = self.make_pr(conn, "role:reviewer-pool")
        row = orc.pin_pool_reviewer(conn, wp.fetch(conn, did))
        self.assertEqual(row["reviewer_seat"], a)
        self.assertEqual(row["reviewer_pool"], "reviewer-pool")
        note = conn.execute("SELECT note FROM event WHERE dispatch_id=? AND"
                            " note LIKE 'reviewer-pinned%'", (did,)).fetchone()
        self.assertIn(a, note["note"])

    def test_empty_pool_stays_before_review_and_is_visible_in_brief(self):
        conn = wp.connect_writable()
        orc = self.load_orc()
        with conn:
            did = wp.insert_task(
                conn, recipient="owner", subject="needs a reviewer",
                workflow="pr", repo="example-storage", owner_seat="owner",
                reviewer_seat="role:reviewer-pool", ready_cmd="true",
                check_cmd="echo h",
            )
            owner_msg = self.record_current_message(
                conn, did, "author-request", f"dispatch:{did}",
                "owner", "s", "b",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-owner',"
                " recipient_agent_id='owner-id' WHERE id=?", (owner_msg,),
            )
        orc.tick_pr_guards(conn, dry=False, pool_registry_fresh=True)
        row = wp.fetch(conn, did)
        self.assertEqual(row["state"], "authoring")
        self.assertIn("REVIEWER-POOL-UNAVAILABLE",
                      orc.task_flags(conn, row))
        self.assertEqual(orc.attention_rows(conn, [row])[0][1],
                         "reviewer-pool-unavailable")
        brief = self.run_cli(ORC, "brief")
        self.assertIn(did, brief)
        self.assertIn("no eligible active member", brief)

    def _to_awaiting_review(self, conn, did):
        with conn:
            conn.execute("UPDATE dispatch SET state='awaiting-review' WHERE id=?",
                         (did,))
            wp.record(conn, did, "pr-ready", "test")
            row = wp.fetch(conn, did)
            msg = self.record_current_message(
                conn, did, "review-request", f"review-req:{did}:test",
                row["reviewer_seat"], "s", "b",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id=?,"
                " recipient_agent_id=? WHERE id=?",
                (f"m-review-{did}", row["reviewer_seat"], msg),
            )

    def _exhausted_pool_pr(self, conn, requester="requester-a"):
        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,status,addressable,"
                " refreshed_ms) VALUES (?,?,?,?,?)",
                (requester, f"test/{requester}", "active", 1, wp.now()),
            )
        did = self.make_pr(conn, "missing-reviewer",
                           subject=f"pool exhausted for {requester}")
        self._to_awaiting_review(conn, did)
        with conn:
            conn.execute(
                "UPDATE dispatch SET reviewer_pool='reviewer-pool',"
                " requester_seat=?,chases=2 WHERE id=?", (requester, did),
            )
        self.assertTrue(wp.reviewer_pool_unavailable(conn, wp.fetch(conn, did)))
        return did

    def test_pool_exhaustion_notice_retargets_and_stops_after_recovery(self):
        conn = wp.connect_writable()
        orc = self.load_orc()
        did = self._exhausted_pool_pr(conn)
        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,status,addressable,"
                " refreshed_ms) VALUES"
                " ('requester-b','test/requester-b','active',1,?)", (wp.now(),),
            )
        with mock.patch.object(wp, "bus_send", return_value=False):
            self.assertEqual(orc.tick_reviewer_rotation(conn, dry=False), 0)
        with conn:
            conn.execute("UPDATE dispatch SET requester_seat='requester-b'"
                         " WHERE id=?", (did,))
        self.assertEqual(wp.repair_attention_notifications(conn), 1)
        notices = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=? AND purpose='escalation'"
            " ORDER BY id", (did,),
        ).fetchall()
        self.assertEqual([n["target"] for n in notices],
                         ["requester-a", "requester-b"])
        self.assertFalse(wp.waits_on_operator(conn, wp.fetch(conn, did)))
        self.member_source(conn)
        self.assertNotIn(did, self.run_cli(LEDGER, "brief"))
        with conn:
            conn.execute(
                "INSERT INTO role_assignment (role,agent_id,granted_by,"
                " granted_ms) VALUES"
                " ('reviewer-pool','replacement-reviewer','test',?)",
                (wp.now(),),
            )
            conn.execute(
                "INSERT OR REPLACE INTO seat"
                " (agent_id,handle,status,addressable,refreshed_ms)"
                " VALUES ('replacement-reviewer','test/replacement-reviewer',"
                " 'active',1,?)", (wp.now(),),
            )
        with mock.patch.object(wp, "bus_send", return_value=False):
            self.assertEqual(orc.tick_reviewer_rotation(conn, dry=False), 1)
        task = wp.fetch(conn, did)
        self.assertEqual(task["reviewer_seat"], "replacement-reviewer")
        self.assertEqual(task["chases"], 0)
        self.assertFalse(wp.reviewer_pool_unavailable(conn, task))
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM event WHERE dispatch_id=?"
            " AND kind='auto-chase'", (did,),
        ).fetchone()[0], 1)
        review_request = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=?"
            " AND purpose='review-request' ORDER BY id DESC LIMIT 1", (did,),
        ).fetchone()
        self.assertEqual(review_request["target"], "replacement-reviewer")
        self.assertTrue(all(not wp.message_is_current_responsibility(
            conn, notice, task) for notice in notices))
        with mock.patch.object(wp, "bus_send", return_value=True) as send:
            self.assertEqual(wp.retry_unsent(conn), (1, 0))
        self.assertEqual([call.args[1] for call in send.call_args_list],
                         [review_request["id"]])
        self.assertEqual(wp.operator_delivery_failures(conn, task), [])

    def test_pool_exhaustion_notice_stops_when_review_returns_to_author(self):
        conn = wp.connect_writable()
        orc = self.load_orc()
        did = self._exhausted_pool_pr(conn)
        with mock.patch.object(wp, "bus_send", return_value=False):
            orc.tick_reviewer_rotation(conn, dry=False)
        notice = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=? AND purpose='escalation'",
            (did,),
        ).fetchone()
        with conn:
            wp.record(conn, did, "verdict-blockers", "author must fix it")
            conn.execute("UPDATE dispatch SET state='fixing' WHERE id=?", (did,))
        task = wp.fetch(conn, did)
        self.assertFalse(wp.message_is_current_responsibility(conn, notice, task))
        self.assertEqual(wp.repair_attention_notifications(conn), 0)
        self.assertNotIn("REVIEWER-POOL-UNAVAILABLE",
                         orc.task_flags(conn, task))
        self.assertFalse(any(row["id"] == did
                             for row, _reason in orc.attention_rows(
                                 conn, [task])))
        self.assertFalse(wp.waits_on_operator(conn, task))
        with mock.patch.object(wp, "bus_send") as send:
            self.assertEqual(wp.retry_unsent(conn), (0, 0))
        send.assert_not_called()

    def test_one_tick_combines_deadline_and_pool_exhaustion(self):
        conn = wp.connect_writable()
        orc = self.load_orc()
        did = self._exhausted_pool_pr(conn)
        with conn:
            conn.execute("UPDATE dispatch SET deadline_ms=? WHERE id=?",
                         (wp.now() - 1, did))
        floor = conn.execute("SELECT COALESCE(MAX(id),0) FROM event").fetchone()[0]
        with mock.patch.object(wp, "bus_send", return_value=False):
            self.assertEqual(orc.tick_deadlines(
                conn, dry=False, cycle_floor_event_id=floor), 1)
            self.assertEqual(orc.tick_reviewer_rotation(
                conn, dry=False, cycle_floor_event_id=floor), 0)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM event WHERE dispatch_id=?"
            " AND kind='auto-chase'", (did,),
        ).fetchone()[0], 1)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM task_msg WHERE task_id=?"
            " AND purpose='escalation'", (did,),
        ).fetchone()[0], 1)
        self.assertEqual(wp.fetch(conn, did)["chases_total"], 1)

    def test_fresh_question_supersedes_deadline_and_pool_warnings(self):
        conn = wp.connect_writable()
        orc = self.load_orc()
        did = self._exhausted_pool_pr(conn)
        with conn:
            conn.execute("UPDATE dispatch SET deadline_ms=?,ask_flag=?"
                         " WHERE id=?", (wp.now() - 1, wp.now(), did))
            self.record_current_voice(
                conn, did, "note",
                f"{wp.ASK_NOTE_PREFIX}choose the reviewer source")
        self.assertEqual(orc.tick_deadlines(conn, dry=False), 0)
        self.assertEqual(orc.tick_reviewer_rotation(conn, dry=False), 0)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM event WHERE dispatch_id=?"
            " AND kind='auto-chase'", (did,),
        ).fetchone()[0], 0)

    def test_rotation_on_two_silent_chases(self):
        conn = wp.connect_writable()
        a, b, _ = self.grant_pool(conn)
        orc = self.load_orc()
        did = self.make_pr(conn, "role:reviewer-pool")
        orc.pin_pool_reviewer(conn, wp.fetch(conn, did))
        self._to_awaiting_review(conn, did)
        with conn:
            conn.execute("UPDATE dispatch SET chases=2,ask_flag=? WHERE id=?",
                         (wp.now() - wp.ASK_FLAG_TTL_S - 1, did))
        n = orc.tick_reviewer_rotation(conn, dry=False)
        self.assertEqual(n, 1)
        row = wp.fetch(conn, did)
        self.assertEqual(row["reviewer_seat"], b)
        self.assertEqual(row["chases"], 0)
        self.assertEqual(row["ask_flag"], 0)
        msg = conn.execute("SELECT dedup_key FROM task_msg WHERE task_id=? AND"
                           " dedup_key LIKE '%rot%'", (did,)).fetchone()
        self.assertIsNotNone(msg)

    def test_rotation_stops_when_historical_author_identity_is_unknown(self):
        conn = wp.connect_writable()
        a, _b, _c = self.grant_pool(conn)
        orc = self.load_orc()
        did = self.make_pr(conn, "role:reviewer-pool")
        with conn:
            owner_msg = self.insert_legacy_message(
                conn, did, "dispatch", "tmux1",
                dedup_key=f"legacy-dispatch:{did}", subject="s", body="b",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-author'"
                " WHERE id=?", (owner_msg,),
            )

            conn.execute(
                "UPDATE dispatch SET reviewer_seat=?,reviewer_pool=? WHERE id=?",
                (a, "reviewer-pool", did),
            )
        self._to_awaiting_review(conn, did)
        with conn:
            conn.execute("UPDATE dispatch SET chases=2 WHERE id=?", (did,))
        with mock.patch.object(wp, "_agent_bus_rows", return_value=None):
            self.assertEqual(orc.tick_reviewer_rotation(conn, dry=False), 0)
            row = wp.fetch(conn, did)
            self.assertEqual(row["reviewer_seat"], a)
            self.assertTrue(wp.reviewer_pool_unavailable(conn, row))

    def test_fixed_reviewer_is_never_rotated(self):
        conn = wp.connect_writable()
        self.grant_pool(conn)
        orc = self.load_orc()
        did = self.make_pr(conn, "fixed-seat")
        self._to_awaiting_review(conn, did)
        with conn:
            conn.execute("UPDATE dispatch SET chases=5 WHERE id=?", (did,))
        self.assertEqual(orc.tick_reviewer_rotation(conn, dry=False), 0)
        self.assertEqual(wp.fetch(conn, did)["reviewer_seat"], "fixed-seat")


class ReviewFloorTests(StoreTestCase):


    def load_orc(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "orc_for_floor_tests", ROOT / "scripts" / "fleet-orchestrator.py")
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_close_records_the_reviewed_head_for_history(self):


        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="x", subject="review example-app#7: seven",
                                 workflow="pr", repo="example-app", owner_seat="x",
                                 reviewer_seat="y", links="example-app#7",
                                 check_cmd="echo deadbeef")
        self.assertEqual(wp.fetch(conn, did)["progress_hash"], "",
                         "an authoring task has no hash yet")
        self.run_cli(LEDGER, "close", did, "--resolution", "superseded",
                     "--note", "parked twice")
        row = wp.fetch(conn, did)
        self.assertEqual(row["state"], "closed")
        self.assertEqual(row["progress_hash"], wp.content_hash("deadbeef\n"),
                         "close ran the check once and kept the head")

    def _pr_at_review(self):
        self.run_cli(ORC, "open", "--to", "tmux1", "--subject", "floor pr",
                     "--workflow", "pr", "--repo", "example-storage",
                     "--owner", "tmux1", "--reviewer", "tmux2",
                     "--ready-cmd", "true", "--done-cmd", "false",
                     "--check", "echo h")
        pr_id = self.task_ids()[0]["id"]
        self.run_cli(ORC, "tick")
        conn = wp.connect_writable()
        with conn:
            self.accept_current_responsibility(
                conn, pr_id, actual="tmux2", pane="%2")
        conn.close()
        self.env["ORC_SEAT_ID"] = "tmux2"
        return pr_id

    def test_verdict_refuses_an_empty_note_and_teaches_the_pointer_form(self):
        pr_id = self._pr_at_review()
        out = subprocess.run([sys.executable, ORC, "verdict", pr_id, "clean"],
                             text=True, capture_output=True, env=self.env)
        self.assertEqual(out.returncode, 1)
        text = out.stdout + out.stderr
        self.assertIn("rubber stamp", text)
        self.assertIn("POINTER", text)

    def test_verdict_event_carries_the_elapsed_stamp(self):
        pr_id = self._pr_at_review()
        self.run_cli(ORC, "verdict", pr_id, "clean", "--note",
                     "full review on the PR: example-app#1 review comment 12345")
        conn = wp.connect_writable()
        ev = conn.execute("SELECT note FROM event WHERE dispatch_id=? AND"
                          " kind='verdict-clean'", (pr_id,)).fetchone()
        self.assertRegex(ev["note"], r"^\[\d+[smhd] from request to verdict\] ")
        self.assertIn("review comment 12345", ev["note"])

    def test_review_request_templates_advertise_the_note_flag(self):
        src = (ROOT / "scripts" / "fleet-orchestrator.py").read_text()
        self.assertGreaterEqual(
            src.count("<findings or PR-review link>"), 3)


class ReassignAuditTests(StoreTestCase):


    def test_reassign_verb_audits_and_clears_pool_rotation(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="role:commander",
                                 subject="reassign fixture", workflow="pr",
                                 repo="example-storage", owner_seat="role:commander",
                                 reviewer_seat="rp-a")
            conn.execute("UPDATE dispatch SET reviewer_pool='reviewer-pool'"
                         " WHERE id=?", (did,))
        self.run_cli(ORC, "reassign", did, "--to", "worker-7", "--owner",
                     "worker-7", "--reviewer", "picked-seat",
                     "--note", "real owner claimed it")
        row = wp.fetch(conn, did)
        self.assertEqual(row["recipient"], "worker-7")
        self.assertEqual(row["owner_seat"], "worker-7")
        self.assertEqual(row["reviewer_seat"], "picked-seat")
        self.assertEqual(row["reviewer_pool"], "")
        ev = conn.execute("SELECT kind,note FROM event WHERE dispatch_id=? AND"
                          " note LIKE 'reassigned:%'", (did,)).fetchone()
        self.assertEqual(ev["kind"], "auto-note")
        self.assertIn("role:commander -> worker-7", ev["note"])
        self.assertIn("real owner claimed it", ev["note"])
        self.assertFalse(wp.seat_spoke_recently(conn, did))
        self.assertEqual(wp.verify_store(wp.connect_readonly()), [])

    def test_reassign_refuses_closed_and_empty(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="x", subject="closed fixture",
                                 check_cmd="true")
            conn.execute("UPDATE dispatch SET state='closed',"
                         " resolution='done' WHERE id=?", (did,))
            wp.record(conn, did, "close:done", "t")
        out = subprocess.run([sys.executable, ORC, "reassign", did, "--to", "y"],
                             text=True, capture_output=True, env=self.env)
        self.assertEqual(out.returncode, 1)
        with conn:
            live = wp.insert_task(conn, recipient="x", subject="live fixture",
                                  check_cmd="true")
        out = subprocess.run([sys.executable, ORC, "reassign", live],
                             text=True, capture_output=True, env=self.env)
        self.assertEqual(out.returncode, 1)
        self.assertIn("nothing to change", out.stdout + out.stderr)
        before = wp.fetch(conn, live)["responsibility_version"]
        events_before = conn.execute(
            "SELECT COUNT(*) FROM event WHERE dispatch_id=?", (live,),
        ).fetchone()[0]
        out = subprocess.run(
            [sys.executable, ORC, "reassign", live, "--to", "x"],
            text=True, capture_output=True, env=self.env,
        )
        self.assertEqual(out.returncode, 1)
        self.assertIn("nothing to change", out.stdout + out.stderr)
        self.assertEqual(wp.fetch(conn, live)["responsibility_version"], before)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM event WHERE dispatch_id=?", (live,),
        ).fetchone()[0], events_before)


class DeadlineOperatorGateTests(StoreTestCase):
    def load_orc(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "orc_for_deadline_attention_tests",
            ROOT / "scripts" / "fleet-orchestrator.py",
        )
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_nonoperator_merge_verification_keeps_the_deadline_ladder(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "orc_for_dl_tests", ROOT / "scripts" / "fleet-orchestrator.py")
        orc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(orc)
        conn = wp.connect_writable()
        with conn:
            conn.execute(
                "INSERT INTO seat(agent_id,handle,status,addressable,refreshed_ms)"
                " VALUES ('line-owner','test/line-owner','active',1,?)",
                (wp.now(),),
            )
            conn.execute(
                "INSERT INTO role_assignment(role,agent_id,granted_by,granted_ms)"
                " VALUES ('line-owner-of-example-storage','line-owner','test',?)",
                (wp.now(),),
            )
            gated = wp.insert_task(conn, recipient="tmux1", subject="gated",
                                   workflow="pr", repo="example-storage",
                                   owner_seat="a", reviewer_seat="b",
                                   deadline_s=1)
            normal = wp.insert_task(conn, recipient="tmux1", subject="normal",
                                    workflow="pr", repo="example-storage",
                                    owner_seat="a", reviewer_seat="b",
                                    deadline_s=1)
            walks = {
                gated: (("pr-ready", "awaiting-review"),
                        ("verdict-clean", "receipt-due"),
                        ("receipt", "merge-pending")),
                normal: (("pr-ready", "awaiting-review"),),
            }
            receipt_events = {}
            for did, steps in walks.items():
                for ev, state in steps:
                    event_id = wp.record(conn, did, ev, "walk")
                    if ev == "receipt":
                        receipt_events[did] = event_id
                    conn.execute("UPDATE dispatch SET state=? WHERE id=?",
                                 (state, did))
                conn.execute("UPDATE dispatch SET deadline_ms=1 WHERE id=?",
                             (did,))
            receipt_notice = self.record_current_message(
                conn, gated, "receipt-to-keyholder",
                f"receipt-review:{gated}:test:"
                f"attention-event={receipt_events[gated]}",
                "role:line-owner-of-example-storage", "verify", "verify receipt",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-key',"
                " recipient_agent_id='line-owner' WHERE id=?",
                (receipt_notice,),
            )
            review = self.record_current_message(
                conn, normal, "review-request", f"review-req:{normal}:test",
                "b", "review", "review this task",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-review',"
                " recipient_agent_id='b' WHERE id=?", (review,),
            )
        fired = orc.tick_deadlines(conn, dry=False)
        self.assertEqual(fired, 2)
        chases = {r["id"]: r["chases_total"] for r in conn.execute(
            "SELECT id, chases_total FROM dispatch")}
        self.assertEqual(chases[gated], 1)
        self.assertEqual(chases[normal], 1)

    def test_operator_owned_merge_pending_is_the_only_deadline_exemption(self):
        orc = self.load_orc()
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(
                conn, recipient="owner", subject="operator merge decision",
                workflow="pr", repo="example-app", owner_seat="owner",
                reviewer_seat="reviewer", deadline_s=1,
            )
            for event, state in (
                    ("pr-ready", "awaiting-review"),
                    ("verdict-clean", "receipt-due"),
                    ("receipt", "merge-pending")):
                wp.record(conn, did, event, "walk")
                conn.execute("UPDATE dispatch SET state=? WHERE id=?",
                             (state, did))
            conn.execute("UPDATE dispatch SET deadline_ms=1 WHERE id=?",
                         (did,))
        self.assertEqual(orc.tick_deadlines(conn, dry=False), 0)
        row = wp.fetch(conn, did)
        self.assertEqual(row["chases_total"], 0)
        self.assertIsNone(wp.deadline_attention_event(conn, row))
        self.assertTrue(wp.waits_on_operator(conn, row))

    def test_overdue_task_without_supervisor_is_the_operator_item_itself(self):
        orc = self.load_orc()
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="worker", subject="late work",
                                 check_cmd="true", deadline_s=60)
            conn.execute("UPDATE dispatch SET deadline_ms=? WHERE id=?",
                         (wp.now() - 1, did))
        self.assertEqual(orc.tick_deadlines(conn, dry=False), 1)
        row = wp.fetch(conn, did)
        self.assertIsNotNone(wp.deadline_attention_event(conn, row))
        self.assertTrue(wp.waits_on_operator(conn, row))
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM task_msg WHERE task_id=? AND purpose='escalation'",
            (did,),
        ).fetchone())
        self.assertIn(did, self.run_cli(LEDGER, "brief"))

    def test_deadline_notice_retargets_without_a_second_chase(self):
        orc = self.load_orc()
        conn = wp.connect_writable()
        with conn:
            for seat in ("requester-a", "requester-b"):
                conn.execute(
                    "INSERT INTO seat (agent_id,handle,status,addressable,"
                    " refreshed_ms) VALUES (?,?,?,?,?)",
                    (seat, f"test/{seat}", "active", 1, wp.now()),
                )
            did = wp.insert_task(
                conn, recipient="worker", requester_seat="requester-a",
                subject="late retarget", check_cmd="true", deadline_s=60,
            )
            conn.execute("UPDATE dispatch SET deadline_ms=? WHERE id=?",
                         (wp.now() - 1, did))
        with mock.patch.object(wp, "bus_send", return_value=False):
            self.assertEqual(orc.tick_deadlines(conn, dry=False), 1)
        with conn:
            conn.execute("UPDATE dispatch SET requester_seat=? WHERE id=?",
                         ("requester-b", did))
        self.assertEqual(wp.repair_attention_notifications(conn), 1)
        row = wp.fetch(conn, did)
        self.assertEqual(row["chases_total"], 1)
        notices = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=? AND purpose='escalation'"
            " ORDER BY id", (did,),
        ).fetchall()
        self.assertEqual([n["target"] for n in notices],
                         ["requester-a", "requester-b"])
        self.assertEqual([wp.message_is_current_responsibility(conn, n, row)
                          for n in notices], [False, True])

    def test_task_voice_clears_deadline_attention(self):
        orc = self.load_orc()
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="worker", subject="answered late",
                                 check_cmd="true", deadline_s=60)
            conn.execute("UPDATE dispatch SET deadline_ms=? WHERE id=?",
                         (wp.now() - 1, did))
        self.assertEqual(orc.tick_deadlines(conn, dry=False), 1)
        conn.close()
        self.env["ORC_SEAT_ID"] = "worker"
        self.run_cli(LEDGER, "note", did, "--note",
                     "still working; dependency named")
        conn = wp.connect_writable()
        row = wp.fetch(conn, did)
        self.assertIsNone(wp.deadline_attention_event(conn, row))
        self.assertFalse(wp.waits_on_operator(conn, row))
        self.assertEqual(wp.repair_attention_notifications(conn), 0)

    def test_foreign_note_does_not_clear_deadline_attention(self):
        orc = self.load_orc()
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="worker", subject="late",
                                 check_cmd="true", deadline_s=60)
            conn.execute("UPDATE dispatch SET deadline_ms=? WHERE id=?",
                         (wp.now() - 1, did))
        self.assertEqual(orc.tick_deadlines(conn, dry=False), 1)
        with conn:
            wp.record(conn, did, "note", "requester is checking in",
                      actor="requester")
        self.assertIsNotNone(
            wp.deadline_attention_event(conn, wp.fetch(conn, did)))


class ReassignPoolPinTests(StoreTestCase):
    def test_reassign_into_a_pool_waits_for_fresh_ready_transition(self):
        conn = wp.connect_writable()
        with conn:
            for i, m in enumerate(("rp-a", "rp-b")):
                conn.execute("INSERT INTO role_assignment (role, agent_id,"
                             " granted_by, granted_ms) VALUES"
                             " ('reviewer-pool', ?, 'test', ?)", (m, i))
                conn.execute(
                    "INSERT INTO seat (agent_id, handle, aliases, host, tmux,"
                    " status, addressable, updated_at, refreshed_ms) VALUES"
                    " (?,?,'','otherhost','','active',1,'',0)", (m, m))
            did = wp.insert_task(conn, recipient="x", subject="repin fixture",
                                 workflow="pr", repo="example-storage", owner_seat="x",
                                 reviewer_seat="hand-picked-before")
        self.run_cli(ORC, "reassign", did, "--reviewer", "role:reviewer-pool",
                     "--note", "send it back to the pool")
        row = wp.fetch(conn, did)
        self.assertEqual(row["reviewer_seat"], "role:reviewer-pool")
        self.assertEqual(row["reviewer_pool"], "")
        pin = conn.execute("SELECT note FROM event WHERE dispatch_id=? AND"
                           " note LIKE 'reviewer-pinned:%at reassign%'",
                           (did,)).fetchone()
        self.assertIsNone(pin)

    def test_reassign_to_concrete_seat_still_disarms_rotation(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="x", subject="concrete fixture",
                                 workflow="pr", repo="example-storage", owner_seat="x",
                                 reviewer_seat="role:reviewer-pool")
            conn.execute("UPDATE dispatch SET reviewer_pool='reviewer-pool'"
                         " WHERE id=?", (did,))
        self.run_cli(ORC, "reassign", did, "--reviewer", "picked-seat")
        row = wp.fetch(conn, did)
        self.assertEqual(row["reviewer_seat"], "picked-seat")
        self.assertEqual(row["reviewer_pool"], "")


class DoneGuardCoverageTests(StoreTestCase):


    def open_pr(self, done_cmd="false"):
        self.run_cli(ORC, "open", "--to", "tmux1", "--subject", "early merge",
                     "--workflow", "pr", "--repo", "example-app",
                     "--owner", "tmux1", "--reviewer", "tmux2",
                     "--ready-cmd", "false", "--done-cmd", done_cmd)
        return self.task_ids()[0]["id"]

    def state_of(self, did):
        return {r["id"]: r for r in self.task_ids()}[did]["state"]

    def test_dry_tick_with_pr_guards_does_not_change_saved_state(self):
        did = self.open_pr()
        for field, state in (("ready_cmd", "authoring"),
                             ("done_cmd", "awaiting-review"),
                             ("check_cmd", "fixing")):
            for command in ("false", "exit 2", "printf new-head"):
                with self.subTest(field=field, command=command):
                    conn = wp.connect_writable()
                    with conn:
                        conn.execute(
                            "UPDATE dispatch SET state=?,done_cmd='',ready_cmd='',"
                            "check_cmd='',progress_hash='old-head',"
                            "guard_unknown_streak=4 WHERE id=?", (state, did))
                        conn.execute(f"UPDATE dispatch SET {field}=? WHERE id=?",
                                     (command, did))
                    before = list(conn.iterdump())
                    conn.close()
                    output = self.run_cli(ORC, "tick", "--dry-run")
                    self.assertIn("OK dry tick done", output)
                    conn = wp.connect_readonly()
                    self.assertEqual(list(conn.iterdump()), before)
                    conn.close()

    def test_merged_is_legal_from_every_open_pr_state(self):
        for state in wp.PR_STATES:
            if state == "closed":
                continue
            self.assertEqual(wp.step("pr", state, "merged"), "closed", state)

    def test_done_guard_closes_an_authoring_task(self):
        did = self.open_pr(done_cmd="true")
        self.run_cli(ORC, "tick")
        self.assertEqual(self.state_of(did), "closed")

    def test_done_guard_closes_an_awaiting_review_task(self):
        did = self.open_pr(done_cmd="true")
        conn = wp.connect_writable()
        with conn:
            conn.execute("UPDATE dispatch SET state='awaiting-review'"
                         " WHERE id=?", (did,))
            wp.record(conn, did, "pr-ready", "test fixture")
        self.run_cli(ORC, "tick")
        self.assertEqual(self.state_of(did), "closed")
        self.run_cli(ORC, "verify")

    def test_false_done_guard_still_moves_nothing(self):
        did = self.open_pr(done_cmd="false")
        self.run_cli(ORC, "tick")
        self.assertEqual(self.state_of(did), "authoring")


class ReassignNudgeTests(StoreTestCase):


    LOCAL_HOST = __import__("socket").gethostname().split(".", 1)[0]

    def seat(self, conn, agent_id, handle, host=None, window="7"):
        with conn:
            conn.execute("INSERT INTO seat (agent_id, handle, aliases, host,"
                         " tmux, status, addressable, updated_at, refreshed_ms) VALUES"
                         " (?,?,?,?,?,'active',1,'',0)",
                         (agent_id, handle, "", host or self.LOCAL_HOST,
                          f"tmux=0:{window}.0 win=claude"))

    def test_raw_agent_id_resolves_to_seat_and_window(self):
        conn = wp.connect_writable()
        self.seat(conn, "aid-uuid-1", "example-host/worker-y", window="9")
        got = wp.resolve_recipient(conn, "aid-uuid-1")
        self.assertEqual(got["agent_id"], "aid-uuid-1")
        self.assertEqual(got["window"], "9")

    def test_reassign_routes_review_request_to_new_reviewer(self):
        conn = wp.connect_writable()
        self.seat(conn, "aid-new-rev", "example-host/reviewer-z", window="5")
        self.run_cli(ORC, "open", "--to", "tmux1", "--subject", "moving review",
                     "--workflow", "pr", "--repo", "example-app",
                     "--owner", "tmux1", "--reviewer", "tmux2",
                     "--ready-cmd", "false", "--done-cmd", "false")
        did = self.task_ids()[0]["id"]
        with conn:
            conn.execute("UPDATE dispatch SET state='awaiting-review'"
                         " WHERE id=?", (did,))
            wp.record(conn, did, "pr-ready", "test fixture")
        self.run_cli(ORC, "reassign", did, "--reviewer", "aid-new-rev",
                     "--note", "test move")
        msg = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=? AND purpose='review-request'",
            (did,)).fetchone()
        self.assertIsNotNone(msg, "reassign must route the review request")
        self.assertEqual(msg["target"], "aid-new-rev")

    def test_reassign_before_review_is_requested_sends_nothing(self):
        self.run_cli(ORC, "open", "--to", "tmux1", "--subject", "still authoring",
                     "--workflow", "pr", "--repo", "example-app",
                     "--owner", "tmux1", "--reviewer", "tmux2",
                     "--ready-cmd", "false", "--done-cmd", "false")
        did = self.task_ids()[0]["id"]
        self.run_cli(ORC, "reassign", did, "--reviewer", "tmux3",
                     "--note", "early move")
        conn = wp.connect_writable()
        n = conn.execute(
            "SELECT COUNT(*) FROM task_msg WHERE task_id=? AND"
            " purpose='review-request'", (did,)).fetchone()[0]
        self.assertEqual(n, 0, "the readiness guard owns the first request")
class ReassignNotifyGapTests(StoreTestCase):


    def test_unregistered_agent_id_still_resolves_to_nothing(self):
        conn = wp.connect_writable()
        got = wp.resolve_recipient(conn, "00000000-0000-4000-8000-000000000009")
        self.assertIsNone(got["agent_id"])
        self.assertIsNone(got["window"])

    def test_recipient_reassign_on_open_dispatch_routes_the_brief(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="tmux1", subject="moved work",
                                 body="the actual brief", check_cmd="true")
        self.run_cli(ORC, "reassign", did, "--to", "tmux2")
        msg = conn.execute("SELECT * FROM task_msg WHERE task_id=? AND"
                           " purpose='reassign-notify'", (did,)).fetchone()
        self.assertIsNotNone(msg)
        self.assertEqual(msg["target"], "tmux2")
        self.assertIn("the actual brief", msg["body"])

    def test_repeat_reviewer_reassign_routes_again(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="tmux1", subject="review me",
                                 workflow="pr", owner_seat="tmux1",
                                 reviewer_seat="old-reviewer")
            conn.execute("UPDATE dispatch SET state='awaiting-review'"
                         " WHERE id=?", (did,))
            wp.record(conn, did, "pr-ready", "fixture")
        self.run_cli(ORC, "reassign", did, "--reviewer", "new-reviewer")
        self.run_cli(ORC, "reassign", did, "--reviewer", "third-reviewer")
        msgs = conn.execute("SELECT target FROM task_msg WHERE task_id=? AND"
                            " purpose='review-request'", (did,)).fetchall()
        self.assertEqual([m["target"] for m in msgs],
                         ["new-reviewer", "third-reviewer"])
        self.assertEqual(wp.verify_store(wp.connect_readonly()), [])

    def test_tick_names_an_unresolvable_owed_seat(self):
        conn = wp.connect_writable()
        with conn:
            wp.insert_task(conn, recipient="ghost-seat-nobody-registered",
                           subject="invisible pairing", check_cmd="true")
        out = self.run_cli(ORC, "tick", "--dry-run")
        self.assertIn("has no current addressable Agent Bus identity", out)


class HandshakeTests(StoreTestCase):


    def load_orc(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "orc_for_handshake_tests", ROOT / "scripts" / "fleet-orchestrator.py")
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    def open_task(self):
        self.run_cli(ORC, "open", "--to", "tmux1", "--subject", "hs task",
                     "--check", "true")
        return self.task_ids()[0]["id"]

    def notes(self, did):
        conn = wp.connect_writable()
        rows = [r["note"] for r in conn.execute(
            "SELECT note FROM event WHERE dispatch_id=?"
            " AND kind IN ('note','auto-note')", (did,))]
        conn.close()
        return rows

    def test_establishes_immediately_on_acked_task(self):
        orc = self.load_orc()
        did = self.open_task()
        self.env["ORC_SEAT_ID"] = "tmux1"
        self.run_cli(LEDGER, "ack", did)
        sleeps = []
        out = orc.dispatch_handshake(
            wp.connect_writable(), did, timeout_s=20,
            sleep=sleeps.append)
        self.assertEqual(out, "established")
        self.assertEqual(sleeps, [], "evidence on the first probe = no waiting")
        self.assertTrue(any("handshake: established" in n
                            for n in self.notes(did)))

    def test_old_recipient_pull_is_not_evidence_after_reassignment(self):
        orc = self.load_orc()
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="seat-a",
                                 subject="handshake reassignment",
                                 check_cmd="true")
            old = self.record_current_message(conn, did, "dispatch", f"dispatch:{did}",
                                "seat-a", "s", "b")
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-a',"
                " recipient_agent_id='seat-a' WHERE id=?", (old,),
            )
            conn.execute("UPDATE dispatch SET recipient='seat-b' WHERE id=?",
                         (did,))
            new = self.record_current_message(conn, did, "reassign-notify",
                                f"reassign:{did}:1", "seat-b", "s", "b")
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-b',"
                " recipient_agent_id='seat-b' WHERE id=?", (new,),
            )
        bus = sqlite3.connect(self.env["AGENT_BUS_DB"])
        with bus:
            bus.execute("CREATE TABLE inbox (agent_id TEXT,msg_id TEXT,state TEXT)")
            bus.execute("INSERT INTO inbox VALUES ('seat-a','m-a','presented')")
        bus.close()
        self.assertIsNone(orc.handshake_evidence(conn, did))

    def test_presented_message_establishes_handshake_without_acknowledging(self):
        orc = self.load_orc()
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="seat-a",
                                 subject="visible is not accepted",
                                 check_cmd="true")
            msg = self.record_current_message(conn, did, "dispatch", f"dispatch:{did}",
                                "seat-a", "s", "b")
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-a',"
                " recipient_agent_id='seat-a' WHERE id=?", (msg,),
            )
        bus = sqlite3.connect(self.env["AGENT_BUS_DB"])
        with bus:
            bus.execute("CREATE TABLE inbox (agent_id TEXT,msg_id TEXT,state TEXT)")
            bus.execute("INSERT INTO inbox VALUES ('seat-a','m-a','presented')")
        bus.close()

        evidence = orc.handshake_evidence(conn, did)
        self.assertEqual(evidence[0], "presented")
        self.assertEqual(wp.fetch(conn, did)["state"], "open")
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM event WHERE dispatch_id=? AND kind='ack'",
            (did,),
        ).fetchone()[0], 0)

    def test_silence_never_observes_or_touches_a_pane(self):
        orc = self.load_orc()
        did = self.open_task()
        with mock.patch.object(
                orc, "_pane_probe_for",
                side_effect=AssertionError("handshake observed a pane")) as probe:
            out = orc.dispatch_handshake(
                wp.connect_writable(), did, timeout_s=30, interval_s=5,
                evidence=lambda *_: None, sleep=lambda _s: None,
            )
        self.assertEqual(out, "timeout")
        probe.assert_not_called()
        self.assertTrue(any("no reaction after 30s" in n
                            for n in self.notes(did)))
        conn = wp.connect_writable()
        kinds = [r[0] for r in conn.execute(
            "SELECT kind FROM event WHERE dispatch_id=? AND note LIKE"
            " 'handshake:%'", (did,))]
        self.assertEqual(kinds, ["auto-note"])
        self.assertFalse(wp.seat_spoke_recently(conn, did),
                         "engine timeout bookkeeping is not seat speech")
        conn.close()

    def test_responsibility_change_stops_the_old_wait(self):
        orc = self.load_orc()
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="seat-a",
                                 subject="handoff while waiting",
                                 check_cmd="true")
        changed = False

        def reassign(_conn, _task_id):
            nonlocal changed
            if not changed:
                with conn:
                    conn.execute("UPDATE dispatch SET recipient='seat-b'"
                                 " WHERE id=?", (did,))
                changed = True
            return None

        out = orc.dispatch_handshake(
            conn, did, timeout_s=30, interval_s=5,
            evidence=reassign, sleep=lambda _s: None,
        )
        self.assertEqual(out, "superseded")
        self.assertTrue(any("responsibility changed" in n
                            for n in self.notes(did)))

    def test_pr_initial_state_without_inbox_is_not_evidence(self):
        orc = self.load_orc()
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(
                conn, recipient="owner", subject="pr has not been read",
                workflow="pr", owner_seat="owner", reviewer_seat="reviewer",
            )
            msg = self.record_current_message(conn, did, "author-request", f"dispatch:{did}",
                                "owner", "s", "b")
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-pr',"
                " recipient_agent_id='owner-id' WHERE id=?", (msg,),
            )
        self.assertIsNone(orc.handshake_evidence(conn, did))

    def test_accepted_recipient_id_prevents_nudge_to_stale_alias_pane(self):
        orc = self.load_orc()
        conn = wp.connect_writable()
        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,aliases,host,tmux,status,"
                " addressable,updated_at,refreshed_ms) VALUES"
                " ('old-id','host/old','',?,'tmux=0:1.0 win=model',"
                " 'active',1,'old',1)",
                (socket.gethostname().split('.', 1)[0],),
            )
            did = wp.insert_task(
                conn, recipient="host/old", subject="moved alias",
                check_cmd="true",
            )
            self.record_current_message(conn, did, "dispatch", f"dispatch:{did}",
                          "host/old", "subject", "body")
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-new',"
                " recipient_agent_id='new-id'"
                " WHERE task_id=?", (did,)
            )
        with mock.patch.object(
                orc, "_pane_probe_for",
                side_effect=AssertionError("handshake observed stale pane")) as probe:
            self.assertEqual(
                orc.dispatch_handshake(
                    conn, did, timeout_s=0, interval_s=5,
                    evidence=lambda *_: None, sleep=lambda _: None,
                ),
                "timeout",
            )
        probe.assert_not_called()

    def test_tick_uses_accepted_recipient_when_alias_cache_refresh_failed(self):
        orc = self.load_orc()
        conn = wp.connect_writable()
        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,aliases,host,tmux,status,"
                " addressable,updated_at,refreshed_ms) VALUES"
                " ('old-id','host/old','',?,'tmux=0:7.0 win=model',"
                " 'active',1,'old',1)",
                (socket.gethostname().split('.', 1)[0],),
            )
            did = wp.insert_task(
                conn, recipient="host/old", subject="accepted by new seat",
                check_cmd="true",
            )
            self.record_current_message(conn, did, "dispatch", f"dispatch:{did}",
                          "host/old", "subject", "body")
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-new',"
                " recipient_agent_id='new-id' WHERE task_id=?", (did,)
            )
        resolved = wp.resolve_owed_recipient(conn, wp.fetch(conn, did))
        self.assertEqual((resolved["seat"], resolved["window"]),
                         ("new-id", None))
        conn.close()
        logs = []
        noops = (
            "tick_parents", "tick_pr_guards", "tick_review_reconcile",
            "tick_checkout_hygiene", "tick_seat_liveness",
            "tick_reviewer_rotation",
        )
        patches = [mock.patch.object(orc, name, return_value=None)
                   for name in noops]
        patches += [
            mock.patch.object(orc, "tick_breakers", return_value=0),
            mock.patch.object(orc, "tick_deps", return_value=0),
            mock.patch.object(orc, "tick_deadlines", return_value=0),
            mock.patch.object(orc, "load_script", return_value=object()),
            mock.patch.object(orc, "log", side_effect=logs.append),
            mock.patch.object(orc.pane_sense, "agent_panes",
                              return_value=[("%7", "0:7.0")]),
            mock.patch.object(
                orc.pane_sense, "pane_for_window",
                side_effect=AssertionError("stale alias pane was observed")),
        ]
        for patcher in patches:
            patcher.start()
        try:
            self.assertEqual(orc.cmd_tick(mock.Mock(dry_run=True)), 0)
        finally:
            for patcher in reversed(patches):
                patcher.stop()
        self.assertTrue(any("bus-only seat" in line and "action=pull" in line
                            for line in logs), logs)

    def test_dispatch_cli_skips_handshake_when_asked(self):
        self.run_cli(ORC, "dispatch", "--no-handshake", "--to", "tmux1",
                     "--subject", "no hs", "--check", "true")
        did = self.task_ids()[0]["id"]
        self.assertFalse(any("handshake" in n for n in self.notes(did)))

    def test_handshake_cli_timeout_exits_nonzero(self):
        did = self.open_task()
        self.run_cli(ORC, "handshake", did, "--timeout", "0", expect=1)
        self.assertTrue(any("no reaction after 0s" in n
                            for n in self.notes(did)))


class ReceiptParseTests(StoreTestCase):


    V3_LINE = ('{"schema":"agent-bus/delivery-status/v3","msg_id":"m-1",'
               '"transport_state":"accepted","recipients":[{"msg_id":"m-1",'
               '"recipient_agent_id":"aid-1","handle_at_send":"example-host/w-1",'
               '"delivered_ms":1787279407641,"processed_ms":1787279463244,'
               '"processed_status":"ok"}]}')
    FLAT_LINE = ('{"schema":"agent-bus/delivery/v3","delivered": true,'
                 ' "processed": ""}')

    def poll_with(self, line):
        stub = Path(self.tmp.name) / "fake-delivery.sh"
        stub.write_text("#!/usr/bin/env bash\nprintf '%s\\n' '" + line + "'\n")
        stub.chmod(0o755)
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="aid-1",
                                 subject="receipt test", check_cmd="true")
            row_id = self.record_current_message(conn, did, "dispatch", f"d:{did}",
                                   "aid-1", "s", "b")
            conn.execute("UPDATE task_msg SET send_state='accepted',"
                         " msg_id='m-1' WHERE id=?", (row_id,))
        os.environ["NW_BUS_CLI"] = str(stub)
        try:
            wp.poll_receipts(conn, limit=5)
        finally:
            os.environ.pop("NW_BUS_CLI", None)
        row = conn.execute("SELECT * FROM task_msg WHERE id=?",
                           (row_id,)).fetchone()
        return conn, did, row

    def test_v3_recipients_shape_marks_delivered_and_processed(self):
        conn, did, row = self.poll_with(self.V3_LINE)
        self.assertEqual(row["delivered"], 1)
        self.assertEqual(row["processed"], "ok")
        self.assertEqual(row["recipient_agent_id"], "aid-1")
        note = conn.execute(
            "SELECT note FROM event WHERE dispatch_id=? AND"
            " note LIKE 'ack:bus processed-ok%'", (did,)).fetchone()
        self.assertIsNone(note, "message processing is not task progress")
        self.assertFalse(wp.seat_spoke_recently(conn, did))

    def test_escalation_recipient_processing_does_not_clear_worker_wait(self):
        stub = Path(self.tmp.name) / "fake-escalation-delivery.sh"
        stub.write_text("#!/usr/bin/env bash\nprintf '%s\\n' '" +
                        self.V3_LINE + "'\n")
        stub.chmod(0o755)
        conn = wp.connect_writable()
        with conn:
            conn.execute(
                "INSERT INTO seat (agent_id,handle,status,addressable,"
                " refreshed_ms) VALUES"
                " ('aid-1','test/aid-1','active',1,?)", (wp.now(),),
            )
            did = wp.insert_task(
                conn, recipient="worker", requester_seat="aid-1",
                subject="late", check_cmd="true", deadline_s=1,
            )
            conn.execute("UPDATE dispatch SET deadline_ms=1 WHERE id=?", (did,))
            context = self.set_current_drive(
                conn, did, state=wp.S_ESCALATED)
            event_id = wp.record(
                conn, did, "auto-chase",
                "engine: DEADLINE OVERDUE: still late",
                continuation_generation=context["generation"])
            msg = self.record_current_message(
                conn, did, "escalation",
                f"deadline:{did}:n1:attention-event={event_id}", "aid-1",
                "late", "inspect",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-1',"
                " recipient_agent_id='aid-1' WHERE id=?", (msg,),
            )
        os.environ["NW_BUS_CLI"] = str(stub)
        try:
            self.assertEqual(wp.poll_receipts(conn, limit=5), 1)
        finally:
            os.environ.pop("NW_BUS_CLI", None)
        task = wp.fetch(conn, did)
        self.assertFalse(wp.seat_spoke_recently(conn, did))
        self.assertIsNotNone(wp.deadline_attention_event(conn, task))
        self.assertEqual(wp.current_drive(conn, task)["st"], wp.S_ESCALATED)

    def test_flat_staging_shape_still_parses(self):
        conn, did, row = self.poll_with(self.FLAT_LINE)
        self.assertEqual(row["delivered"], 1)
        self.assertEqual(row["processed"], "",
                         "a flat line with no processed claim stays open")


class PoolLoadAndAuthorTests(StoreTestCase):


    def load_orc(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "orc_for_pool_load_tests", ROOT / "scripts" / "fleet-orchestrator.py")
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    def grant_pool(self, conn, members=("pl-a", "pl-b")):
        with conn:
            for i, m in enumerate(members):
                conn.execute("INSERT INTO role_assignment (role, agent_id,"
                             " granted_by, granted_ms) VALUES"
                             " ('reviewer-pool', ?, 'test', ?)", (m, i))
                conn.execute(
                    "INSERT INTO seat (agent_id, handle, aliases, host, tmux,"
                    " status, addressable, updated_at, refreshed_ms) VALUES"
                    " (?,?,'','otherhost','','active',1,'',0)", (m, m))
        return members

    def seat(self, conn, agent_id, handle):
        with conn:
            conn.execute("INSERT OR REPLACE INTO seat"
                         " (agent_id, handle, aliases, host, tmux, status,"
                         " addressable, updated_at, refreshed_ms) VALUES"
                         " (?,?,'','otherhost','','active',1,'',0)",
                         (agent_id, handle))

    def test_handle_shaped_review_load_counts_against_the_member(self):
        conn = wp.connect_writable()
        a, b = self.grant_pool(conn)
        self.seat(conn, a, "example-host/rev-a-tmux31")


        with conn:
            wp.insert_task(conn, recipient="tmux1", subject="handle load",
                           workflow="pr", repo="example-storage", owner_seat="tmux1",
                           reviewer_seat="example-host/rev-a-tmux31",
                           check_cmd="echo h")
        self.assertEqual(wp.pool_pick(conn, "reviewer-pool"), b)

    def test_unpinned_role_string_is_nobodys_load(self):
        conn = wp.connect_writable()
        a, b = self.grant_pool(conn)
        with conn:
            wp.insert_task(conn, recipient="tmux1", subject="unpinned",
                           workflow="pr", repo="example-storage", owner_seat="tmux1",
                           reviewer_seat="role:reviewer-pool",
                           check_cmd="echo h")


        self.assertEqual(wp.pool_pick(conn, "reviewer-pool"), a)

    def test_pin_never_picks_the_author(self):
        conn = wp.connect_writable()
        a, b = self.grant_pool(conn)
        self.seat(conn, a, "example-host/pl-a-tmux32")
        orc = self.load_orc()
        with conn:
            did = wp.insert_task(conn, recipient=a, subject="self-review",
                                 workflow="pr", repo="example-storage", owner_seat=a,
                                 reviewer_seat="role:reviewer-pool",
                                 check_cmd="echo h")
        row = orc.pin_pool_reviewer(conn, wp.fetch(conn, did))
        self.assertEqual(row["reviewer_seat"], b)

    def test_single_member_pool_where_member_authored_parks(self):
        conn = wp.connect_writable()
        (solo,) = self.grant_pool(conn, members=("solo",))
        self.seat(conn, solo, "example-host/solo-tmux33")
        orc = self.load_orc()
        with conn:
            did = wp.insert_task(conn, recipient=solo, subject="solo author",
                                 workflow="pr", repo="example-storage", owner_seat=solo,
                                 reviewer_seat="role:reviewer-pool",
                                 check_cmd="echo h")
        row = orc.pin_pool_reviewer(conn, wp.fetch(conn, did))


        self.assertEqual(row["reviewer_seat"], "role:reviewer-pool")


class StatuslineTests(StoreTestCase):


    def setUp(self):
        super().setUp()
        wp.connect_writable().close()

    def test_empty_board_is_one_calm_line(self):
        self.env.pop("NO_COLOR", None)
        out = self.run_cli(ORC, "statusline", "--no-color")
        self.assertIn("ORC idle", out)
        self.assertNotIn("ATTN", out)

        self.assertIn("tick NEVER", out)

    def test_counts_and_attention_line(self):
        conn = wp.connect_writable()
        with conn:
            wp.insert_task(conn, recipient="tmux1", subject="plain work",
                           check_cmd="true")
            esc = wp.insert_task(conn, recipient="tmux2", subject="stuck work",
                                 check_cmd="true")
            rev = wp.insert_task(conn, recipient="tmux1", subject="review me",
                                 workflow="pr", owner_seat="tmux1",
                                 reviewer_seat="tmux2")
            conn.execute("UPDATE dispatch SET state='awaiting-review'"
                         " WHERE id=?", (rev,))
            wp.record(conn, rev, "pr-ready", "fixture")
            wp.insert_task(conn, recipient="operator", subject="decide this",
                           check_cmd="true")
            self.set_current_drive(conn, esc, state=wp.S_ESCALATED)
        out = self.run_cli(ORC, "statusline", "--no-color")
        self.assertIn("ORC open 4", out)
        self.assertIn("attn 1", out)
        self.assertIn("review 1", out)
        self.assertIn("operator 2", out)
        self.assertIn("ATTN", out)
        self.assertIn(esc, out)
        self.assertIn("escalated", out)
        self.assertNotIn("\x1b[", out)

    def test_color_defaults_on_for_pipes(self):
        self.env.pop("NO_COLOR", None)

        out = self.run_cli(ORC, "statusline")
        self.assertIn("\x1b[", out)


class ReviewRequestUnclaimedTests(StoreTestCase):


    LOCAL_HOST = socket.gethostname().split(".", 1)[0]

    def load_orc(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "orc_for_unclaimed_tests", ROOT / "scripts" / "fleet-orchestrator.py")
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    def fixture(self, host=None, msg_age_s=3600):
        conn = wp.connect_writable()
        with conn:
            for i, m in enumerate(("uq-a", "uq-b")):
                conn.execute("INSERT INTO role_assignment (role, agent_id,"
                             " granted_by, granted_ms) VALUES"
                             " ('reviewer-pool', ?, 'test', ?)", (m, i))
            conn.execute("INSERT INTO seat (agent_id, handle, aliases, host,"
                         " tmux, status, addressable, updated_at, refreshed_ms) VALUES"
                         " ('uq-a','example-host/uq-a-tmux41','',?,"
                         "'tmux=0:41.0 win=claude','active',1,'',0)",
                         (host or self.LOCAL_HOST,))
            conn.execute("INSERT INTO seat (agent_id, handle, aliases, host,"
                         " tmux, status, addressable, updated_at, refreshed_ms) VALUES"
                         " ('uq-b','example-host/uq-b-tmux42','','otherhost','',"
                         "'active',1,'',0)")
            did = wp.insert_task(conn, recipient="tmux1", subject="unclaimed",
                                 workflow="pr", repo="example-storage",
                                 owner_seat="tmux1", reviewer_seat="uq-a",
                                 check_cmd="echo h")
            author = self.record_current_message(
                conn, did, "author-request", f"author:{did}",
                "tmux1", "author", "author",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-author',"
                " recipient_agent_id='author-id' WHERE id=?", (author,),
            )
            conn.execute("UPDATE dispatch SET state='awaiting-review',"
                         " reviewer_pool='reviewer-pool' WHERE id=?", (did,))
            wp.record(conn, did, "pr-ready", "test fixture")
            row_id = self.record_current_message(conn, did, "review-request", f"rr:{did}",
                                   "uq-a", "s", "b")
            conn.execute("UPDATE task_msg SET send_state='accepted',"
                         " msg_id='rr-msg-1',recipient_agent_id='uq-a',"
                         " at_ms=at_ms-? WHERE id=?",
                         (msg_age_s, row_id))
        return conn, did

    def test_old_unpresented_request_with_idle_pane_is_unclaimed(self):
        conn, did = self.fixture()
        orc = self.load_orc()
        row = wp.fetch(conn, did)
        self.assertTrue(orc.review_request_unclaimed(
            conn, row, pane_probe=lambda: False))

    def test_fresh_request_is_not_unclaimed(self):
        conn, did = self.fixture(msg_age_s=0)
        orc = self.load_orc()
        row = wp.fetch(conn, did)
        self.assertFalse(orc.review_request_unclaimed(
            conn, row, pane_probe=lambda: False))

    def test_remote_seat_never_triggers(self):
        conn, did = self.fixture(host="otherhost")
        orc = self.load_orc()
        row = wp.fetch(conn, did)
        self.assertFalse(orc.review_request_unclaimed(
            conn, row, pane_probe=lambda: False))

    def test_alias_cache_cannot_rotate_the_actual_remote_reviewer(self):
        conn, did = self.fixture()
        orc = self.load_orc()
        with conn:
            conn.execute("UPDATE dispatch SET reviewer_seat='host/reviewer'"
                         " WHERE id=?", (did,))
            conn.execute("UPDATE seat SET handle='host/reviewer'"
                         " WHERE agent_id='uq-a'")
            conn.execute(
                "UPDATE task_msg SET target='host/reviewer',"
                " recipient_agent_id='uq-b' WHERE task_id=?"
                " AND purpose='review-request'", (did,)
            )
        row = wp.fetch(conn, did)
        with mock.patch.object(
                orc, "_pane_probe_for",
                side_effect=AssertionError("stale reviewer pane was observed")):
            self.assertFalse(orc.review_request_unclaimed(conn, row))

    def test_new_failed_request_does_not_fall_back_to_old_accepted_reviewer(self):
        conn, did = self.fixture()
        orc = self.load_orc()
        with conn:
            conn.execute("UPDATE dispatch SET reviewer_seat='uq-b' WHERE id=?",
                         (did,))
            newest = self.record_current_message(conn, did, "review-request",
                                   f"rr:{did}:reassign", "uq-b", "s", "b")
            conn.execute("UPDATE task_msg SET send_state='failed',"
                         " at_ms=at_ms-3600 WHERE id=?", (newest,))
        self.assertFalse(orc.review_request_unclaimed(
            conn, wp.fetch(conn, did), pane_probe=lambda: False))

    def test_busy_or_unobservable_pane_never_triggers(self):
        conn, did = self.fixture()
        orc = self.load_orc()
        row = wp.fetch(conn, did)
        self.assertFalse(orc.review_request_unclaimed(
            conn, row, pane_probe=lambda: True))
        self.assertFalse(orc.review_request_unclaimed(
            conn, row, pane_probe=lambda: None))

    def test_processed_receipt_holds_the_guard_off(self):
        conn, did = self.fixture()
        orc = self.load_orc()
        with conn:
            conn.execute("UPDATE task_msg SET processed='ok' WHERE task_id=?"
                         " AND purpose='review-request'", (did,))
        row = wp.fetch(conn, did)
        self.assertFalse(orc.review_request_unclaimed(
            conn, row, pane_probe=lambda: False))

    def test_ack_after_the_send_holds_the_guard_off(self):
        conn, did = self.fixture()
        orc = self.load_orc()
        with conn:
            self.record_current_voice(
                conn, did, "ack", "reviewer accepted, reviewing head x")
        row = wp.fetch(conn, did)
        self.assertFalse(orc.review_request_unclaimed(
            conn, row, pane_probe=lambda: False))

    def test_presentation_alone_is_not_a_reaction(self):


        conn, did = self.fixture()
        orc = self.load_orc()
        row = wp.fetch(conn, did)
        self.assertTrue(orc.review_request_unclaimed(
            conn, row, pane_probe=lambda: False))

    def test_tick_rotation_fires_on_unclaimed(self):
        conn, did = self.fixture()
        orc = self.load_orc()
        orc.review_request_unclaimed = lambda conn, row, pane_probe=None: True
        n = orc.tick_reviewer_rotation(conn, dry=False)
        self.assertEqual(n, 1)
        row = wp.fetch(conn, did)
        self.assertEqual(row["reviewer_seat"], "uq-b")
        note = conn.execute(
            "SELECT kind,note FROM event WHERE dispatch_id=? AND note LIKE"
            " 'reviewer-rotated:%'", (did,)).fetchone()
        self.assertEqual(note["kind"], "auto-note")
        self.assertIn("frozen-seat guard", note["note"])
        self.assertFalse(wp.seat_spoke_recently(conn, did))

    def test_rotation_uses_actual_reviewer_not_stale_alias_cache(self):
        conn, did = self.fixture()
        orc = self.load_orc()
        with conn:
            conn.execute("UPDATE dispatch SET reviewer_seat='host/reviewer'"
                         " WHERE id=?", (did,))
            version = wp.fetch(conn, did)["responsibility_version"]
            conn.execute("UPDATE seat SET handle='host/reviewer'"
                         " WHERE agent_id='uq-a'")
            conn.execute(
                "UPDATE task_msg SET target='host/reviewer',"
                " recipient_agent_id='uq-b',recipient_version=?"
                " WHERE task_id=? AND purpose='review-request'",
                (version, did),
            )
            conn.execute("UPDATE dispatch SET chases=2 WHERE id=?", (did,))
        self.assertEqual(orc.tick_reviewer_rotation(conn, dry=False), 1)
        self.assertEqual(wp.fetch(conn, did)["reviewer_seat"], "uq-a")


class KanbanTests(StoreTestCase):


    def test_every_workflow_state_has_a_column(self):
        for wf, spec in wp.WORKFLOWS.items():
            for state in spec["states"]:
                self.assertIn((wf, state), wp.KANBAN, f"{wf}/{state} unmapped")
                self.assertIn(wp.KANBAN[(wf, state)], wp.KANBAN_COLUMNS)

    def test_columns_place_states_markers_and_resolutions(self):
        conn = wp.connect_writable()
        with conn:
            pred = wp.insert_task(conn, recipient="tmux1", subject="pred work",
                                  check_cmd="true")
            wp.insert_task(conn, recipient="tmux2", subject="held work",
                           check_cmd="true", needs=(pred,))
            esc = wp.insert_task(conn, recipient="tmux3", subject="stuck work",
                                 check_cmd="true")
            conn.execute("UPDATE dispatch SET state='acked' WHERE id=?", (esc,))
            wp.record(conn, esc, "ack", "fixture")
            self.set_current_drive(conn, esc, state=wp.S_ESCALATED)
            done = wp.insert_task(conn, recipient="tmux1", subject="finished",
                                  check_cmd="true")
            conn.execute("UPDATE dispatch SET state='closed',"
                         " resolution='dropped', last_event=? WHERE id=?",
                         (wp.now(), done))
            wp.record(conn, done, "close:dropped", "fixture")
        out = self.run_cli(ORC, "kanban", "--no-color")
        self.assertIn("BACKLOG (1)", out)
        self.assertIn("TODO (1)", out)
        self.assertIn("DOING (1)", out)
        self.assertIn("REVIEW (0)", out)
        self.assertIn("CLOSED-24H (1)", out)
        self.assertIn("held work", out)
        self.assertIn(f"!@{esc}", out)
        self.assertIn("(dropped)", out)

    def test_cells_pad_by_display_width_not_chars(self):
        import importlib.util
        import unicodedata
        spec = importlib.util.spec_from_file_location(
            "orc_for_kanban_tests", ROOT / "scripts" / "fleet-orchestrator.py")
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        for sample in ("主线任务 abc", "ascii only", "全中文标题超出宽度限制"):
            padded = mod.clip_pad_display(sample, 10)
            display = sum(2 if unicodedata.east_asian_width(c) in ("W", "F")
                          else 1 for c in padded)
            self.assertEqual(display, 10, repr(padded))

    def test_no_grid_line_starts_with_whitespace(self):


        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="tmux1", subject="only doing",
                                 check_cmd="true")
            conn.execute("UPDATE dispatch SET state='acked' WHERE id=?", (did,))
            wp.record(conn, did, "ack", "fixture")
        out = self.run_cli(ORC, "kanban", "--no-color")
        grid = [l for l in out.splitlines() if not l.startswith(("Fleet ", "WARN "))]
        self.assertTrue(grid)
        for line in grid:
            self.assertTrue(line.startswith("|"), repr(line))

    def test_max_rows_shows_overflow(self):
        conn = wp.connect_writable()
        with conn:
            for i in range(5):
                wp.insert_task(conn, recipient="tmux1",
                               subject=f"todo item {i}", check_cmd="true")
        out = self.run_cli(ORC, "kanban", "--no-color", "--max-rows", "3")
        self.assertIn("+3 more", out)
        self.assertNotIn("todo item 4", out)


class RotationCooldownTests(StoreTestCase):


    def grant(self, conn):
        with conn:
            for i, m in enumerate(("cd-a", "cd-b")):
                conn.execute("INSERT INTO role_assignment (role, agent_id,"
                             " granted_by, granted_ms) VALUES"
                             " ('reviewer-pool', ?, 'test', ?)", (m, i))
                conn.execute(
                    "INSERT INTO seat (agent_id, handle, aliases, host, tmux,"
                    " status, addressable, updated_at, refreshed_ms) VALUES"
                    " (?,?,'','otherhost','','active',1,'',0)", (m, m))

    def rotated_away(self, conn, member, age_s):
        with conn:
            did = wp.insert_task(conn, recipient="tmux1", subject="past review",
                                 workflow="pr", repo="example-storage", owner_seat="tmux1",
                                 reviewer_seat="cd-b", check_cmd="echo h")
            conn.execute(
                "INSERT INTO event (dispatch_id, at_ms, actor, kind, note)"
                " VALUES (?,?,?,?,?)",
                (did, wp.now() - age_s, "engine", "note",
                 f"reviewer-rotated: {member} -> cd-b (reviewer silent)"))

    def test_recent_rotation_sits_the_member_out(self):
        conn = wp.connect_writable()
        self.grant(conn)
        self.rotated_away(conn, "cd-a", age_s=60)
        self.assertEqual(wp.pool_pick(conn, "reviewer-pool"), "cd-b")

    def test_cooldown_expires(self):
        conn = wp.connect_writable()
        self.grant(conn)
        self.rotated_away(conn, "cd-a", age_s=wp.ROTATION_COOLDOWN_S + 60)
        self.assertEqual(wp.pool_pick(conn, "reviewer-pool"), "cd-a")


class BareRepoTests(unittest.TestCase):


    def test_all_three_live_shapes_reach_the_line_owner(self):
        for shape in ("example-storage", "example-org/example-storage",
                      "/srv/workspaces/example-storage", "/srv/workspaces/example-storage/"):
            self.assertEqual(wp.merge_key_role(shape),
                             "line-owner-of-example-storage", shape)

    def test_unknown_repo_still_fails_toward_the_operator(self):
        self.assertEqual(wp.merge_key_role("owner/never-heard-of-it"),
                         wp.OPERATOR_ROLE)
        self.assertEqual(wp.merge_key_role(""), wp.OPERATOR_ROLE)

    def test_bare_repo_shapes(self):
        self.assertEqual(wp.bare_repo("example-app"), "example-app")
        self.assertEqual(wp.bare_repo("example-org/example-app"), "example-app")
        self.assertEqual(wp.bare_repo("/home/u/src/example-app/"), "example-app")
        self.assertEqual(wp.bare_repo(""), "")


class CompletionClaimStateTests(StoreTestCase):


    CMDR = "cmdr-agent-1"

    def _add_seat(self, conn, agent_id, *, status="active", addressable=1):
        conn.execute(
            "INSERT OR REPLACE INTO seat (agent_id,handle,status,addressable,"
            " refreshed_ms) VALUES (?,?,?,?,?)",
            (agent_id, f"test/{agent_id}", status, addressable, wp.now()),
        )
        self.member_source(conn)

    def _grant_commander(self, conn):
        with conn:
            conn.execute("INSERT OR REPLACE INTO seat (agent_id, handle,"
                         " status, addressable, refreshed_ms) VALUES (?,?,?,?,?)",
                         (self.CMDR, "example-host/fleet-command-tmux1", "active", 1,
                          wp.now()))
            conn.execute("INSERT INTO role_assignment (role, agent_id,"
                         " granted_by, granted_ms) VALUES (?,?,?,?)",
                         ("commander", self.CMDR, "test", wp.now()))
        self.member_source(conn)

    def load_orc(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "orc_for_completion_claim_tests",
            ROOT / "scripts" / "fleet-orchestrator.py",
        )
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    def _bus_stub(self, name, script):
        stub = Path(self.tmp.name) / name
        stub.write_text("#!/usr/bin/env bash\n" + script)
        stub.chmod(0o755)
        return str(stub)

    def test_dispatch_owner_claim_notifies_commander_judge(self):
        conn = wp.connect_writable()
        self._grant_commander(conn)
        with conn:
            did = wp.insert_task(conn, recipient="tmux9", subject="s",
                                 check_cmd="true")
        conn.close()
        self.env["NW_BUS_CLI"] = self._bus_stub(
            "bus-ok.sh",
            "if [ \"$1\" = members ]; then\n"
            f"  echo '{{\"agent_id\":\"{self.CMDR}\",\"handle\":"
            f"\"test/{self.CMDR}\",\"status\":\"active\","
            "\"addressable\":true}'\n"
            "  echo '{\"agent_id\":\"tmux9\",\"handle\":\"test/tmux9\","
            "\"status\":\"active\",\"addressable\":true}'\n"
            "else\n"
            f"  echo '{{\"msg_id\":\"m-claim\","
            f"\"recipient_agent_ids\":[\"{self.CMDR}\"]}}'\n"
            "fi\n")
        self.env["ORC_SEAT_ID"] = "tmux9"
        out = self.run_cli(ORC, "claim-done", did, "--note", "all six done")
        self.assertIn("judge", out)
        conn = wp.connect_writable()
        claim = conn.execute("SELECT * FROM completion_claim WHERE task_id=?",
                             (did,)).fetchone()
        self.assertEqual(claim["status"], "standing")
        self.assertEqual(claim["round"], 1)
        self.assertEqual(claim["claimant"], "tmux9",
                         "claimant is the AUTHORIZED seat id, not the"
                         " runtime actor label")
        self.assertIn("responsibility-v0", claim["generation"])
        ev = conn.execute("SELECT note FROM event WHERE dispatch_id=? AND"
                          " kind='claim'", (did,)).fetchone()
        self.assertEqual(ev["note"], "all six done",
                         "the human note is payload, not protocol")
        msg = conn.execute("SELECT * FROM task_msg WHERE task_id=? AND"
                           " purpose='claim-notify'", (did,)).fetchone()
        self.assertEqual(msg["target"], "role:commander")
        self.assertEqual(msg["recipient_agent_id"], self.CMDR)
        self.assertEqual(msg["send_state"], "accepted")
        self.assertEqual(msg["processed"], "",
                         "delivery alone never counts as judged")
        conn.close()

    def test_pr_owner_claim_targets_pinned_reviewer(self):
        conn = wp.connect_writable()
        self._grant_commander(conn)
        with conn:
            self._add_seat(conn, "own-1")
            self._add_seat(conn, "rev-1")
            did = wp.insert_task(conn, recipient="own-1", subject="pr work",
                                 workflow="pr", repo="example-app",
                                 owner_seat="own-1", reviewer_seat="rev-1")
        row = wp.fetch(conn, did)
        self.assertEqual(wp.claim_judge(conn, row), "rev-1",
                         "a pinned reviewer judges the pr claim")
        with conn:
            claim = wp.claim_open(conn, row, "ready")
        self.assertEqual(claim["judge"], "rev-1")

        with conn:
            wp.record(conn, did, "verdict-blockers", "not yet")
        with conn:
            self.assertIsNone(wp.claim_standing(conn, wp.fetch(conn, did)))
        status = conn.execute("SELECT status, reason FROM completion_claim"
                              " WHERE task_id=?", (did,)).fetchone()
        self.assertEqual(status["status"], "rejected")
        self.assertIn("verdict-blockers", status["reason"])
        conn.close()

    def test_dispatch_claim_targets_the_recorded_requester(self):
        conn = wp.connect_writable()
        with conn:
            self._add_seat(conn, "requester-1")
            did = wp.insert_task(
                conn, recipient="worker-1", subject="requested work",
                check_cmd="true", requester_seat="requester-1",
            )
        row = wp.fetch(conn, did)
        self.assertEqual(wp.claim_judge(conn, row), "requester-1")
        with conn:
            claim = wp.claim_open(conn, row, "done")
        self.assertEqual(claim["judge"], "requester-1")

    def test_parent_goal_owner_precedes_the_direct_requester(self):
        conn = wp.connect_writable()
        with conn:
            self._add_seat(conn, "goal-lead")
            self._add_seat(conn, "requester-1")
            parent = wp.insert_task(
                conn, recipient="goal-lead", subject="goal", workflow="parent",
            )
            child = wp.insert_task(
                conn, recipient="worker-1", subject="child", check_cmd="true",
                parent_id=parent, requester_seat="requester-1",
            )
        self.assertEqual(wp.claim_judge(conn, wp.fetch(conn, child)),
                         "goal-lead")

    def test_no_independent_judge_keeps_the_claim_on_the_original_task(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(
                conn, recipient="worker-1", subject="legacy ownerless work",
                check_cmd="true",
            )
            claim = wp.claim_commit(conn, wp.fetch(conn, did), "done")
        self.assertEqual(claim["judge"], "operator")
        self.assertIsNone(claim["msg_row"])
        self.assertTrue(wp.waits_on_operator(conn, wp.fetch(conn, did)))
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM task_msg WHERE task_id=?",
            (did,),
        ).fetchone()[0], 0,
                         "no synthetic delivery or duplicate task is needed")

    def test_claim_done_registry_failure_records_claim_without_sending(self):
        conn = wp.connect_writable()
        with conn:
            self._add_seat(conn, "requester-a")
            did = wp.insert_task(
                conn, recipient="tmux9", requester_seat="requester-a",
                subject="claim while registry is unavailable", check_cmd="true",
            )
        conn.close()
        calls = Path(self.tmp.name) / "bus-calls"
        self.env["NW_BUS_CLI"] = self._bus_stub(
            "bus-members-down.sh",
            f"echo \"$1\" >> '{calls}'\nexit 1\n",
        )
        self.env["ORC_SEAT_ID"] = "tmux9"
        out = self.run_cli(ORC, "claim-done", did, "--note", "done safely")
        self.assertIn("judge operator", out)
        self.assertEqual(calls.read_text().splitlines(), ["members"],
                         "an unverified judge must receive no send attempt")
        conn = wp.connect_writable()
        self.assertIsNotNone(wp.claim_standing(conn, wp.fetch(conn, did)))
        marker = wp.operator_queue_marker(conn, wp.fetch(conn, did))
        self.assertIsNotNone(marker)
        self.assertEqual(marker["purpose"], "claim-notify")
        self.assertTrue(wp.waits_on_operator(conn, wp.fetch(conn, did)))
        self.assertEqual(wp.repair_standing_claim_notifications(
            conn, registry_trusted=True), 1)
        rows = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=?"
            " AND purpose='claim-notify' ORDER BY id", (did,),
        ).fetchall()
        self.assertEqual([row["target"] for row in rows],
                         ["operator", "requester-a"])
        self.assertEqual([wp.message_is_current_responsibility(
            conn, row, wp.fetch(conn, did)) for row in rows], [False, True])

    def test_requester_cannot_judge_their_own_claim(self):
        conn = wp.connect_writable()
        with conn:
            self._add_seat(conn, "worker-1")
            did = wp.insert_task(
                conn, recipient="worker-1", subject="self-opened",
                check_cmd="true", requester_seat="worker-1",
            )
        self.assertEqual(wp.claim_judge(conn, wp.fetch(conn, did)), "operator")

    def test_inactive_requester_falls_through_to_active_commander(self):
        conn = wp.connect_writable()
        self._grant_commander(conn)
        with conn:
            self._add_seat(conn, "requester-old", status="retired")
            did = wp.insert_task(
                conn, recipient="worker-1", subject="stale requester",
                check_cmd="true", requester_seat="requester-old",
            )
        self.assertEqual(wp.claim_judge(conn, wp.fetch(conn, did)),
                         "role:commander")

    def test_active_escalation_notice_follows_a_new_requester_without_a_chase(self):
        conn = wp.connect_writable()
        with conn:
            self._add_seat(conn, "worker-1")
            self._add_seat(conn, "requester-a")
            self._add_seat(conn, "requester-b")
            did = wp.insert_task(
                conn, recipient="worker-1", subject="stuck",
                check_cmd="true", requester_seat="requester-a",
            )
            context = self.set_current_drive(
                conn, did, state=wp.S_ESCALATED)
            event_id = wp.record(
                conn, did, "auto-chase", "engine: still idle",
                continuation_generation=context["generation"])
            self.record_current_message(
                conn, did, "escalation",
                f"old:{did}:attention-event={event_id}",
                "requester-a", "old", "old")
            conn.execute("UPDATE dispatch SET requester_seat=? WHERE id=?",
                         ("requester-b", did))
        before = wp.fetch(conn, did)["chases_total"]
        self.assertEqual(wp.repair_attention_notifications(conn), 1)
        messages = conn.execute(
            "SELECT target FROM task_msg WHERE task_id=?"
            " AND purpose='escalation' ORDER BY id", (did,),
        ).fetchall()
        self.assertEqual([m["target"] for m in messages],
                         ["requester-a", "requester-b"])
        self.assertEqual(wp.fetch(conn, did)["chases_total"], before)
        with conn:
            conn.execute("UPDATE task_msg SET send_state='failed'"
                         " WHERE task_id=? AND purpose='escalation'", (did,))
            conn.execute("UPDATE drive SET st=? WHERE task_id=?",
                         (wp.S_WORKING, did))
        current = [wp.message_is_current_responsibility(
            conn, msg, wp.fetch(conn, did),
        ) for msg in conn.execute(
            "SELECT * FROM task_msg WHERE task_id=?"
            " AND purpose='escalation' ORDER BY id", (did,),
        )]
        self.assertEqual(current, [False, False])
        with mock.patch.object(wp, "bus_send") as send:
            self.assertEqual(wp.retry_unsent(conn), (0, 0))
        send.assert_not_called()
        self.assertEqual(wp.operator_delivery_failures(
            conn, wp.fetch(conn, did)), [])

    def test_escalation_success_without_one_actual_recipient_stays_visible(self):
        conn = wp.connect_writable()
        with conn:
            self._add_seat(conn, "worker-1")
            self._add_seat(conn, "requester-a")
            did = wp.insert_task(
                conn, recipient="worker-1", requester_seat="requester-a",
                subject="unproved supervisor delivery", check_cmd="true",
            )
            self.accept_current_responsibility(
                conn, did, actual="worker-1", pane="%1")
            context = self.set_current_drive(
                conn, did, state=wp.S_ESCALATED)
            event_id = wp.record(
                conn, did, "auto-chase", "engine: still idle",
                continuation_generation=context["generation"])
            notice = self.record_current_message(
                conn, did, "escalation",
                f"escalation:{did}:attention-event={event_id}",
                "requester-a", "attention", "attention",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-unknown',"
                " recipient_agent_id='' WHERE id=?", (notice,),
            )
        row = wp.fetch(conn, did)
        self.assertEqual(
            wp.current_escalation_delivery_failure(conn, row)["id"], notice)
        self.assertTrue(wp.waits_on_operator(conn, row))
        self.assertEqual(wp.repair_attention_notifications(conn), 0)
        self.assertEqual(conn.execute(
            "SELECT COUNT(*) FROM task_msg WHERE task_id=?"
            " AND purpose='escalation'", (did,),
        ).fetchone()[0], 1)

    def test_escalation_notice_does_not_revive_after_operator_round_trip(self):
        conn = wp.connect_writable()
        with conn:
            self._add_seat(conn, "worker-1")
            self._add_seat(conn, "requester-a")
            did = wp.insert_task(
                conn, recipient="worker-1", requester_seat="requester-a",
                subject="attention round trip", check_cmd="true",
            )
            context = self.set_current_drive(
                conn, did, state=wp.S_ESCALATED)
            event_id = wp.record(
                conn, did, "auto-chase", "engine: still idle",
                continuation_generation=context["generation"])
            original = self.record_current_message(
                conn, did, "escalation",
                f"escalation:{did}:1:to:requester-a:"
                f"attention-event={event_id}",
                "requester-a", "old", "old",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='failed' WHERE id=?",
                (original,),
            )
            conn.execute("UPDATE seat SET status='retired'"
                         " WHERE agent_id='requester-a'")
        self.assertEqual(wp.repair_attention_notifications(conn), 1)
        self.member_source(conn)
        brief = self.run_cli(LEDGER, "brief")
        self.assertIn("continuation unanswered", brief)
        self.assertNotIn("current recipient could not be verified", brief)
        with conn:
            conn.execute("UPDATE seat SET status='active'"
                         " WHERE agent_id='requester-a'")
        self.assertEqual(wp.repair_attention_notifications(conn), 1)
        notices = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=?"
            " AND purpose='escalation' ORDER BY id", (did,),
        ).fetchall()
        self.assertEqual([n["target"] for n in notices],
                         ["requester-a", "operator", "requester-a"])
        self.assertEqual(notices[1]["send_state"], "operator-queue")
        task = wp.fetch(conn, did)
        self.assertEqual([wp.message_is_current_responsibility(
            conn, notice, task) for notice in notices],
            [False, False, True])

    def test_old_escalation_does_not_retry_during_a_to_b_to_a_delivery_gap(self):
        conn = wp.connect_writable()
        with conn:
            self._add_seat(conn, "worker-a")
            self._add_seat(conn, "worker-b")
            self._add_seat(conn, "requester-a")
            did = wp.insert_task(
                conn, recipient="worker-a", requester_seat="requester-a",
                subject="responsibility round trip", check_cmd="true",
            )
            old_alert = self.insert_legacy_message(
                conn, did, "escalation", "requester-a",
                dedup_key=f"escalation:{did}:old",
                subject="old alert", body="old alert",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='failed' WHERE id=?",
                (old_alert,),
            )
            self.set_current_drive(conn, did, state=wp.S_ESCALATED)
            conn.execute("UPDATE dispatch SET recipient='worker-b' WHERE id=?",
                         (did,))
            conn.execute("UPDATE dispatch SET recipient='worker-a' WHERE id=?",
                         (did,))
            current = self.record_current_message(
                conn, did, "reassign-notify", f"reassign:{did}:2",
                "worker-a", "current assignment", "current assignment",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='failed' WHERE id=?",
                (current,),
            )
        task = wp.fetch(conn, did)
        self.assertTrue(wp.dispatch_undelivered(conn, did))
        old = conn.execute("SELECT * FROM task_msg WHERE id=?",
                           (old_alert,)).fetchone()
        self.assertFalse(wp.message_is_current_responsibility(conn, old, task))
        with mock.patch.object(wp, "bus_send", return_value=True) as send:
            self.assertEqual(wp.retry_unsent(conn), (1, 0))
        self.assertEqual([call.args[1] for call in send.call_args_list],
                         [current])

    def test_second_escalation_never_reuses_the_first_rounds_body(self):
        conn = wp.connect_writable()
        orc = self.load_orc()
        with conn:
            self._add_seat(conn, "worker-a")
            self._add_seat(conn, "requester-a")
            did = wp.insert_task(
                conn, recipient="worker-a", requester_seat="requester-a",
                subject="two separate stalls", check_cmd="true",
            )
            context = self.set_current_drive(
                conn, did, state=wp.S_ESCALATED)
            first_event = wp.record(
                conn, did, "auto-chase", "engine: FIRST stall reason",
                continuation_generation=context["generation"])
            first = self.record_current_message(
                conn, did, "escalation",
                f"escalation:{did}:first:attention-event={first_event}",
                "requester-a", "FIRST subject", "FIRST body",
            )
            conn.execute("UPDATE task_msg SET send_state='failed' WHERE id=?",
                         (first,))
            conn.execute("UPDATE drive SET st=? WHERE task_id=?",
                         (wp.S_WORKING, did))
            conn.execute("UPDATE seat SET status='retired'"
                         " WHERE agent_id='requester-a'")
            conn.execute("UPDATE drive SET st=? WHERE task_id=?",
                         (wp.S_ESCALATED, did))
        self.assertTrue(orc.escalate(
            conn, wp.fetch(conn, did), "SECOND stall reason", dry=False))
        with conn:
            conn.execute("UPDATE seat SET status='active'"
                         " WHERE agent_id='requester-a'")
        self.assertEqual(wp.repair_attention_notifications(conn), 1)
        notices = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=? AND purpose='escalation'"
            " ORDER BY id", (did,),
        ).fetchall()
        self.assertEqual(len(notices), 2)
        self.assertFalse(wp.message_is_current_responsibility(
            conn, notices[0], wp.fetch(conn, did)))
        self.assertIn("SECOND stall reason", notices[1]["body"])
        self.assertNotIn("FIRST", notices[1]["body"])
        with mock.patch.object(wp, "bus_send", return_value=True) as send:
            self.assertEqual(wp.retry_unsent(conn), (1, 0))
        self.assertEqual([call.args[1] for call in send.call_args_list],
                         [notices[1]["id"]])

    def test_hex_task_id_cannot_be_mistaken_for_attention_event_id(self):
        conn = wp.connect_writable()
        orc = self.load_orc()
        with mock.patch.object(
                wp.uuid, "uuid4",
                return_value=mock.Mock(hex="e1234567deadbeef")):
            with conn:
                self._add_seat(conn, "worker-a")
                self._add_seat(conn, "requester-a")
                did = wp.insert_task(
                    conn, recipient="worker-a", requester_seat="requester-a",
                    subject="hex id parsing", check_cmd="true",
                )
                self.set_current_drive(conn, did, state=wp.S_ESCALATED)
        self.assertEqual(did, "e1234567")
        with mock.patch.object(wp, "bus_send", return_value=False):
            self.assertTrue(orc.escalate(
                conn, wp.fetch(conn, did), "current stall", dry=False))
        msg = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=? AND purpose='escalation'",
            (did,),
        ).fetchone()
        event = wp.current_attention_event(conn, wp.fetch(conn, did))
        self.assertEqual(wp.message_attention_event_id(msg), event["id"])
        self.assertTrue(wp.message_is_current_responsibility(
            conn, msg, wp.fetch(conn, did)))

    def test_untrusted_registry_parks_escalation_until_repaired(self):
        conn = wp.connect_writable()
        with conn:
            self._add_seat(conn, "worker-1")
            self._add_seat(conn, "requester-a")
            did = wp.insert_task(
                conn, recipient="worker-1", requester_seat="requester-a",
                subject="recipient must be current", check_cmd="true",
            )
            self.set_current_drive(conn, did, state=wp.S_ESCALATED)
        orc = self.load_orc()
        with mock.patch.object(wp, "bus_send") as send:
            self.assertTrue(orc.escalate(
                conn, wp.fetch(conn, did), "registry unavailable", dry=False,
                registry_trusted=False))
        send.assert_not_called()
        task = wp.fetch(conn, did)
        marker = wp.operator_queue_marker(conn, task)
        self.assertIsNotNone(marker)
        self.assertEqual(marker["target"], "operator")
        self.assertTrue(wp.waits_on_operator(conn, task))
        self.assertEqual(wp.repair_attention_notifications(
            conn, registry_trusted=True), 1)
        notices = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=? AND purpose='escalation'"
            " ORDER BY id", (did,),
        ).fetchall()
        self.assertEqual([notice["target"] for notice in notices],
                         ["operator", "requester-a"])
        self.assertEqual([wp.message_is_current_responsibility(
            conn, notice, wp.fetch(conn, did)) for notice in notices],
            [False, True])

    def test_untrusted_registry_replaces_an_old_operator_marker(self):
        conn = wp.connect_writable()
        orc = self.load_orc()
        with conn:
            self._add_seat(conn, "worker-1")
            self._add_seat(conn, "requester-a", status="retired")
            did = wp.insert_task(
                conn, recipient="worker-1", requester_seat="requester-a",
                subject="two unverified rounds", check_cmd="true",
            )
            self.set_current_drive(conn, did, state=wp.S_ESCALATED)
        with mock.patch.object(wp, "bus_send"):
            orc.escalate(conn, wp.fetch(conn, did), "FIRST round", dry=False,
                         registry_trusted=False)
        first_marker = wp.operator_queue_marker(
            conn, wp.fetch(conn, did), "escalation")
        self.assertIsNotNone(first_marker)
        with conn:
            conn.execute("UPDATE drive SET st=? WHERE task_id=?",
                         (wp.S_WORKING, did))
            conn.execute("UPDATE drive SET st=? WHERE task_id=?",
                         (wp.S_ESCALATED, did))
        with mock.patch.object(wp, "bus_send"):
            orc.escalate(conn, wp.fetch(conn, did), "SECOND round", dry=False,
                         registry_trusted=True)
        with conn:
            conn.execute("UPDATE seat SET status='active'"
                         " WHERE agent_id='requester-a'")
        self.assertEqual(wp.repair_attention_notifications(
            conn, registry_trusted=False), 1)
        current = wp.operator_queue_marker(
            conn, wp.fetch(conn, did), "escalation")
        self.assertIsNotNone(current)
        self.assertNotEqual(current["id"], first_marker["id"])
        self.assertIn("SECOND round", current["body"])
        self.member_source(conn)
        brief = self.run_cli(LEDGER, "brief")
        self.assertIn("current recipient could not be verified", brief)
        self.assertIn("SECOND round", brief)
        self.assertNotIn("FIRST round", brief)

    def test_reassign_invalidates_standing_claim(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="tmux9", subject="s",
                                 check_cmd="true")
            wp.claim_open(conn, wp.fetch(conn, did), "done here")
        conn.close()
        self.run_cli(ORC, "reassign", did, "--to", "tmux7",
                     "--note", "moving it")
        conn = wp.connect_writable()
        with conn:
            self.assertIsNone(wp.claim_standing(conn, wp.fetch(conn, did)),
                              "a reassigned task's claim cannot stand")
        status = conn.execute("SELECT status, reason FROM completion_claim"
                              " WHERE task_id=?", (did,)).fetchone()
        self.assertEqual(status["status"], "invalidated")
        self.assertIn("generation-moved", status["reason"])
        conn.close()

    def test_owner_a_b_a_does_not_revive_the_first_claim(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="owner-a", subject="s",
                                 check_cmd="true")
            wp.claim_open(conn, wp.fetch(conn, did), "done on first visit")
            conn.execute("UPDATE dispatch SET recipient='owner-b' WHERE id=?",
                         (did,))
            to_b = self.record_current_message(conn, did, "reassign-notify",
                                 f"reassign:{did}:b", "owner-b", "s", "b")
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-b',"
                " recipient_agent_id='owner-b' WHERE id=?", (to_b,),
            )
            conn.execute("UPDATE dispatch SET recipient='owner-a' WHERE id=?",
                         (did,))
            to_a = self.record_current_message(conn, did, "reassign-notify",
                                 f"reassign:{did}:a2", "owner-a", "s", "b")
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-a2',"
                " recipient_agent_id='owner-a' WHERE id=?", (to_a,),
            )
        row = wp.fetch(conn, did)
        self.assertEqual(row["responsibility_version"], 2)
        with conn:
            self.assertIsNone(wp.claim_standing(conn, row))
        claim = conn.execute(
            "SELECT status,reason FROM completion_claim WHERE task_id=?",
            (did,),
        ).fetchone()
        self.assertEqual(claim["status"], "invalidated")
        self.assertIn("responsibility-v2", claim["reason"])

    def test_upgrade_responsibility_gap_does_not_invalidate_claim(self):


        conn = wp.connect_writable()
        with conn:
            self._add_seat(conn, "requester-seat")
            did = wp.insert_task(conn, recipient="worker-short", subject="s",
                                 check_cmd="true",
                                 requester_seat="requester-seat")
            original = self.record_current_message(
                conn, did, "dispatch", f"dispatch:{did}", "worker-short",
                "s", "b",
            )
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-old',"
                " recipient_agent_id='seat-a' WHERE id=?", (original,),
            )
            claim = wp.claim_commit(conn, wp.fetch(conn, did), "done")
            conn.execute(
                "UPDATE task_msg SET send_state='failed',attempts=1 WHERE id=?",
                (claim["msg_row"],),
            )
            sync = self.record_current_message(
                conn, did, "reassign-notify",
                f"responsibility-sync:{did}:v0", "worker-short", "sync", "b",
            )

        row = wp.fetch(conn, did)
        self.assertIn("deferred", wp.resolve_owed_recipient(conn, row))
        self.assertIsNotNone(wp.claim_standing(conn, row, repair=False))
        claim_msg = conn.execute("SELECT * FROM task_msg WHERE id=?",
                                 (claim["msg_row"],)).fetchone()
        sync_msg = conn.execute("SELECT * FROM task_msg WHERE id=?",
                                (sync,)).fetchone()
        self.assertFalse(wp.message_is_current_responsibility(conn, claim_msg, row),
                         "the judge must not hear an identity-uncertain claim")
        self.assertTrue(wp.message_is_current_responsibility(conn, sync_msg, row),
                        "the unknown gap must still be allowed to resolve")

        with conn:
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='m-same',"
                " recipient_agent_id='seat-a' WHERE id=?", (sync,),
            )
        self.assertIsNotNone(
            wp.claim_standing(conn, wp.fetch(conn, did), repair=False),
            "the same actual recipient preserves the claim",
        )
        row = wp.fetch(conn, did)
        claim_msg = conn.execute("SELECT * FROM task_msg WHERE id=?",
                                 (claim["msg_row"],)).fetchone()
        self.assertTrue(wp.message_is_current_responsibility(conn, claim_msg, row))

        with conn:
            conn.execute(
                "UPDATE task_msg SET msg_id='m-new',recipient_agent_id='seat-b'"
                " WHERE id=?", (sync,),
            )
        self.assertIsNone(wp.claim_standing(conn, wp.fetch(conn, did)))
        fate = conn.execute(
            "SELECT status,reason FROM completion_claim WHERE task_id=?",
            (did,),
        ).fetchone()
        self.assertEqual(fate["status"], "invalidated")
        self.assertIn("responsibility-v0:to-seat-b", fate["reason"])
        self.assertFalse(wp.message_is_current_responsibility(
            conn, claim_msg, wp.fetch(conn, did),
        ))
        conn.close()

    def test_rejection_by_chase_resumes_claimant(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="tmux9", subject="s",
                                 check_cmd="true")
            row = wp.fetch(conn, did)
            wp.claim_open(conn, row, "claiming")
        with conn:
            self.assertIsNotNone(wp.claim_standing(conn, row),
                                 "claim stands before any judge reaction")
        with conn:
            wp.record(conn, did, "chase", "not done - the check still fails")
        with conn:
            self.assertIsNone(wp.claim_standing(conn, wp.fetch(conn, did)),
                              "a chase after the claim returns the work")
        status = conn.execute("SELECT status FROM completion_claim WHERE"
                              " task_id=?", (did,)).fetchone()["status"]
        self.assertEqual(status, "rejected")

        with conn:
            generation = wp.continuation_context(
                conn, wp.fetch(conn, did))["generation"]
            self.assertTrue(wp.wake_attempt_open(conn, did, "tmux9", "pull",
                                                 generation))
        conn.close()

    def test_claim_notify_failure_is_recorded_and_retried(self):
        conn = wp.connect_writable()
        self._grant_commander(conn)
        with conn:
            did = wp.insert_task(conn, recipient="tmux9", subject="s",
                                 check_cmd="true")
        conn.close()
        self.env["NW_BUS_CLI"] = self._bus_stub(
            "bus-down.sh",
            "if [ \"$1\" = members ]; then\n"
            f"  echo '{{\"agent_id\":\"{self.CMDR}\",\"handle\":"
            f"\"test/{self.CMDR}\",\"status\":\"active\","
            "\"addressable\":true}'\n"
            "  echo '{\"agent_id\":\"tmux9\",\"handle\":\"test/tmux9\","
            "\"status\":\"active\",\"addressable\":true}'\n"
            "else\n"
            "  echo 'bus is down' >&2\n"
            "  exit 1\n"
            "fi\n")
        self.env["ORC_SEAT_ID"] = "tmux9"
        out = self.run_cli(ORC, "claim-done", did, "--note", "done")
        self.assertIn("retries", out, "a failed notify is said out loud")
        conn = wp.connect_writable()
        msg = conn.execute("SELECT * FROM task_msg WHERE task_id=? AND"
                           " purpose='claim-notify'", (did,)).fetchone()
        self.assertEqual(msg["send_state"], "failed")
        self.assertEqual(msg["attempts"], 1)
        self.assertIn("bus is down", msg["last_error"])

        os.environ["NW_BUS_CLI"] = self._bus_stub(
            "bus-up.sh", "echo '{\"msg_id\":\"m-retry\"}'\n")
        try:
            ok, failing = wp.retry_unsent(conn, log=lambda *a: None)
        finally:
            os.environ.pop("NW_BUS_CLI", None)
        self.assertEqual((ok, failing), (1, 0))
        msg = conn.execute("SELECT send_state, msg_id FROM task_msg WHERE"
                           " task_id=? AND purpose='claim-notify'",
                           (did,)).fetchone()
        self.assertEqual(msg["send_state"], "accepted")
        self.assertEqual(msg["msg_id"], "m-retry")
        conn.close()

    def test_legacy_commander_claim_notice_is_replaced_not_replayed(self):
        conn = wp.connect_writable()
        self._grant_commander(conn)
        with conn:
            did = wp.insert_task(conn, recipient="owner", subject="legacy judge",
                                 check_cmd="true")
        claim = wp.claim_commit(conn, wp.fetch(conn, did), "done")
        with conn:
            conn.execute(
                "UPDATE task_msg SET target='old-commander',send_state='failed',"
                " attempts=1 WHERE id=?", (claim["msg_row"],),
            )
        old = conn.execute("SELECT * FROM task_msg WHERE id=?",
                           (claim["msg_row"],)).fetchone()
        self.assertFalse(
            wp.message_is_current_responsibility(conn, old, wp.fetch(conn, did)),
        )
        self.assertEqual(wp.repair_standing_claim_notifications(
            conn, log=lambda *_: None), 1)
        rows = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=? AND purpose='claim-notify'"
            " ORDER BY id", (did,),
        ).fetchall()
        self.assertEqual([r["target"] for r in rows],
                         ["old-commander", "role:commander"])
        self.assertFalse(wp.message_is_current_responsibility(
            conn, rows[0], wp.fetch(conn, did),
        ))
        self.assertTrue(wp.message_is_current_responsibility(
            conn, rows[1], wp.fetch(conn, did),
        ))
        with mock.patch.object(wp, "bus_send", return_value=True) as send:
            self.assertEqual(wp.retry_unsent(conn, log=lambda *_: None), (1, 0))
        self.assertEqual(send.call_args.args[1], rows[1]["id"])

    def test_claim_notice_follows_old_holder_after_newer_grant_revoked(self):
        conn = wp.connect_writable()
        self._grant_commander(conn)
        with conn:
            did = wp.insert_task(conn, recipient="owner", subject="role moved",
                                 check_cmd="true")
        claim = wp.claim_commit(conn, wp.fetch(conn, did), "done")
        with conn:
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='old-msg',"
                " recipient_agent_id=? WHERE id=?", (self.CMDR,
                                                       claim["msg_row"]),
            )
            self._add_seat(conn, "cmdr-agent-2")
            conn.execute(
                "INSERT INTO role_assignment (role,agent_id,granted_by,"
                " granted_ms) VALUES ('commander','cmdr-agent-2','test',?)",
                (wp.now() + 1,),
            )
        old = conn.execute("SELECT * FROM task_msg WHERE id=?",
                           (claim["msg_row"],)).fetchone()
        self.assertFalse(wp.message_is_current_responsibility(
            conn, old, wp.fetch(conn, did)))
        self.assertEqual(wp.repair_standing_claim_notifications(conn), 1)
        rows = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=?"
            " AND purpose='claim-notify' ORDER BY id", (did,),
        ).fetchall()
        self.assertEqual([r["target"] for r in rows],
                         ["role:commander", "role:commander"])
        with conn:
            conn.execute(
                "UPDATE task_msg SET send_state='accepted',msg_id='middle-msg',"
                " recipient_agent_id='cmdr-agent-2' WHERE id=?",
                (rows[-1]["id"],),
            )
            conn.execute("UPDATE role_assignment SET revoked_ms=?"
                         " WHERE role='commander'"
                         " AND agent_id='cmdr-agent-2' AND revoked_ms IS NULL",
                         (wp.now() + 2,),
            )
        self.assertFalse(wp.message_is_current_responsibility(
            conn, rows[-1], wp.fetch(conn, did)))
        self.assertEqual(wp.repair_standing_claim_notifications(conn), 1)
        rows = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=?"
            " AND purpose='claim-notify' ORDER BY id", (did,),
        ).fetchall()
        self.assertEqual(len(rows), 3)
        self.assertEqual([wp.message_is_current_responsibility(
            conn, message, wp.fetch(conn, did)) for message in rows],
            [False, False, True])
        generations = [message["dedup_key"].split(
            ":role-generation-", 1)[1] for message in rows]
        self.assertEqual(len(set(generations)), 3)

    def test_claim_notice_follows_reviewer_a_to_b_to_a_without_revival(self):
        conn = wp.connect_writable()
        with conn:
            for seat in ("owner", "reviewer-a", "reviewer-b"):
                self._add_seat(conn, seat)
            did = wp.insert_task(
                conn, recipient="owner", subject="moving judge", workflow="pr",
                repo="example-app", owner_seat="owner", reviewer_seat="reviewer-a",
            )
        claim = wp.claim_commit(conn, wp.fetch(conn, did), "done")
        with conn:
            conn.execute("UPDATE task_msg SET send_state='failed' WHERE id=?",
                         (claim["msg_row"],))
            conn.execute("UPDATE dispatch SET reviewer_seat='reviewer-b'"
                         " WHERE id=?", (did,))
        self.assertEqual(wp.repair_standing_claim_notifications(
            conn, log=lambda *_: None), 1)
        with conn:
            conn.execute("UPDATE dispatch SET reviewer_seat='reviewer-a'"
                         " WHERE id=?", (did,))
        self.assertEqual(wp.repair_standing_claim_notifications(
            conn, log=lambda *_: None), 1)
        rows = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=? AND purpose='claim-notify'"
            " ORDER BY id", (did,),
        ).fetchall()
        self.assertEqual([r["target"] for r in rows],
                         ["reviewer-a", "reviewer-b", "reviewer-a"])
        current = [wp.message_is_current_responsibility(
            conn, r, wp.fetch(conn, did),
        ) for r in rows]
        self.assertEqual(current, [False, False, True])
        conn.close()

    def test_claim_notice_does_not_revive_after_operator_round_trip(self):
        conn = wp.connect_writable()
        with conn:
            for seat in ("owner", "reviewer-a"):
                self._add_seat(conn, seat)
            did = wp.insert_task(
                conn, recipient="owner", subject="claim operator round trip",
                workflow="pr", repo="example-app", owner_seat="owner",
                reviewer_seat="reviewer-a",
            )
        claim = wp.claim_commit(conn, wp.fetch(conn, did), "done")
        with conn:
            conn.execute("UPDATE task_msg SET send_state='failed' WHERE id=?",
                         (claim["msg_row"],))
            conn.execute("UPDATE seat SET status='retired'"
                         " WHERE agent_id='reviewer-a'")
        self.assertEqual(wp.repair_standing_claim_notifications(conn), 1)
        with conn:
            conn.execute("UPDATE seat SET status='active'"
                         " WHERE agent_id='reviewer-a'")
        self.assertEqual(wp.repair_standing_claim_notifications(conn), 1)
        notices = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=?"
            " AND purpose='claim-notify' ORDER BY id", (did,),
        ).fetchall()
        self.assertEqual([n["target"] for n in notices],
                         ["reviewer-a", "operator", "reviewer-a"])
        self.assertEqual(notices[1]["send_state"], "operator-queue")
        task = wp.fetch(conn, did)
        self.assertEqual([wp.message_is_current_responsibility(
            conn, notice, task) for notice in notices],
            [False, False, True])
        self.assertIsNone(wp.operator_queue_marker(conn, task))
        self.assertFalse(wp.waits_on_operator(conn, task))
        with mock.patch.object(wp, "bus_send", return_value=True) as send:
            self.assertEqual(wp.retry_unsent(conn), (1, 0))
        self.assertEqual([call.args[1] for call in send.call_args_list],
                         [notices[-1]["id"]])
        conn.close()

    def test_historical_note_carries_no_authority(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="tmux9", subject="legacy",
                                 check_cmd="true")
            wp.record(conn, did, "note", "claims-done: the old text protocol")
        with conn:
            self.assertIsNone(wp.claim_standing(conn, wp.fetch(conn, did)),
                              "legacy notes are audit text, not claims")
        conn.close()
        data = json.loads(self.run_cli(ORC, "board", "--json"))
        task = next(t for t in data["tasks"] if t["id"] == did)
        self.assertFalse(any(flag.startswith("CLAIMS-DONE") for flag in task["flags"]),
                         "board flag must read the typed table only")
        self.assertNotIn("Completion claimed", self.run_cli(ORC, "board"))

    def test_terminal_close_settles_the_claim_in_the_same_transaction(self):


        conn = wp.connect_writable()
        with conn:
            done_id = wp.insert_task(conn, recipient="tmux9", subject="a",
                                     check_cmd="true")
            drop_id = wp.insert_task(conn, recipient="tmux9", subject="b",
                                     check_cmd="true")
            wp.claim_open(conn, wp.fetch(conn, done_id), "x")
            wp.claim_open(conn, wp.fetch(conn, drop_id), "y")
        conn.close()
        self.run_cli(ORC, "close", done_id, "--resolution", "done")
        self.run_cli(ORC, "close", drop_id, "--resolution", "dropped")
        conn = wp.connect_writable()
        for did, want in ((done_id, "accepted"), (drop_id, "consumed")):
            got = conn.execute("SELECT status FROM completion_claim WHERE"
                               " task_id=?", (did,)).fetchone()["status"]
            self.assertEqual(got, want,
                             "raw row settled by the close itself")
        conn.close()
        shown = self.run_cli(ORC, "show", done_id)
        self.assertIn("accepted", shown)
        self.assertNotIn("standing", shown)

    def test_tick_sweep_settles_claims_closed_before_the_helper(self):


        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="tmux9", subject="legacy",
                                 check_cmd="true")
            wp.claim_open(conn, wp.fetch(conn, did), "old world")

            conn.execute("UPDATE dispatch SET state='closed',"
                         " resolution='done' WHERE id=?", (did,))
        conn.close()
        self.run_cli(ORC, "tick")
        conn = wp.connect_writable()
        row = conn.execute("SELECT status, reason FROM completion_claim"
                           " WHERE task_id=?", (did,)).fetchone()
        conn.close()
        self.assertEqual(row["status"], "accepted")
        self.assertIn("close:done", row["reason"])

    def test_repeated_claim_supersedes_prior_round(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="tmux9", subject="s",
                                 check_cmd="true")
            wp.claim_open(conn, wp.fetch(conn, did), "first")
            second = wp.claim_open(conn, wp.fetch(conn, did), "second")
        self.assertEqual(second["round"], 2)
        rows = conn.execute("SELECT round, status FROM completion_claim WHERE"
                            " task_id=? ORDER BY round", (did,)).fetchall()
        self.assertEqual([(r["round"], r["status"]) for r in rows],
                         [(1, "superseded"), (2, "standing")])
        conn.close()

    def test_superseded_claim_notification_never_retries(self):
        conn = wp.connect_writable()
        self._grant_commander(conn)
        with conn:
            did = wp.insert_task(conn, recipient="tmux9", subject="s",
                                 check_cmd="true")
        first = wp.claim_commit(conn, wp.fetch(conn, did), "first")
        second = wp.claim_commit(conn, wp.fetch(conn, did), "second")
        with conn:
            conn.execute(
                "UPDATE task_msg SET send_state='failed',attempts=1"
                " WHERE id IN (?,?)", (first["msg_row"], second["msg_row"]),
            )
        accepted = subprocess.CompletedProcess(
            ["matrix-bus", "send"], 0,
            stdout='{"msg_id":"m-current",'
                   '"recipient_agent_ids":["judge"]}\n',
            stderr="",
        )
        with mock.patch.object(wp.subprocess, "run",
                               return_value=accepted) as send:
            self.assertEqual(wp.retry_unsent(conn, log=lambda *_: None),
                             (1, 0))
        self.assertEqual(send.call_count, 1)
        states = conn.execute(
            "SELECT dedup_key,send_state FROM task_msg WHERE task_id=?"
            " AND purpose='claim-notify' ORDER BY id", (did,),
        ).fetchall()
        self.assertEqual(
            [(row["dedup_key"].split(":role-generation-", 1)[0],
              row["send_state"]) for row in states],
            [(f"claim:{did}:1", "failed"),
             (f"claim:{did}:2", "accepted")],
        )

    def test_generation_moved_claim_notification_never_retries(self):
        conn = wp.connect_writable()
        self._grant_commander(conn)
        with conn:
            did = wp.insert_task(
                conn, recipient="owner", subject="receipt claim",
                workflow="pr", repo="example-app", owner_seat="owner",
                reviewer_seat="reviewer",
            )
            conn.execute("UPDATE dispatch SET state='awaiting-review' WHERE id=?",
                         (did,))
            conn.execute("UPDATE dispatch SET state='receipt-due' WHERE id=?",
                         (did,))
        claim = wp.claim_commit(conn, wp.fetch(conn, did), "ready")
        with conn:
            conn.execute(
                "UPDATE task_msg SET send_state='failed',attempts=1 WHERE id=?",
                (claim["msg_row"],),
            )
            conn.execute("UPDATE dispatch SET state='merge-pending' WHERE id=?",
                         (did,))
        stored = conn.execute(
            "SELECT status FROM completion_claim WHERE task_id=?", (did,),
        ).fetchone()["status"]
        self.assertEqual(stored, "standing", "the persisted cache still lags")
        with mock.patch.object(wp.subprocess, "run") as send:
            self.assertEqual(wp.retry_unsent(conn, log=lambda *_: None),
                             (0, 0))
        send.assert_not_called()

    def test_each_claim_event_resolves_to_its_own_round(self):


        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="tmux9", subject="s",
                                 check_cmd="true")
        with conn:
            first = wp.claim_commit(conn, wp.fetch(conn, did), "first try")
        with conn:
            second = wp.claim_commit(conn, wp.fetch(conn, did), "second try")
        rows = conn.execute(
            "SELECT c.round, c.generation, c.event_id, e.kind, e.note"
            " FROM completion_claim c JOIN event e ON e.id = c.event_id"
            " WHERE c.task_id=? ORDER BY c.round", (did,)).fetchall()
        conn.close()
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["kind"] for r in rows], ["claim", "claim"])
        self.assertEqual([r["note"] for r in rows],
                         ["first try", "second try"],
                         "each event resolves to exactly its own round")
        self.assertNotEqual(rows[0]["event_id"], rows[1]["event_id"])
        self.assertTrue(all(r["event_id"] > 0 for r in rows))
        self.assertEqual((first["event_id"], second["event_id"]),
                         (rows[0]["event_id"], rows[1]["event_id"]))
        shown = self.run_cli(ORC, "show", did)
        self.assertIn("claims ---", shown)
        self.assertIn(f"r1  event #{rows[0]['event_id']}", shown)
        self.assertIn(f"r2  event #{rows[1]['event_id']}", shown)

    def test_bus_database_pane_identity_authorizes_and_is_the_audit_id(self):


        seat_id = "agent-uuid-owner-1"
        self._seed_bus_identity(seat_id, pane="%77")
        conn = wp.connect_writable()
        self._grant_commander(conn)
        with conn:
            did = wp.insert_task(conn, recipient=seat_id, subject="s",
                                 check_cmd="true")
        conn.close()
        env = dict(self.env)
        env.pop("ORC_SEAT_ID", None)
        env["TMUX_PANE"] = "%77"
        env["NW_BUS_CLI"] = self._bus_stub(
            "bus-boot.sh", "echo '{\"msg_id\":\"m-boot\"}'\n")
        out = subprocess.run([sys.executable, ORC, "claim-done", did,
                              "--note", "documented shape"], text=True,
                             capture_output=True, env=env)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        conn = wp.connect_writable()
        claimant = conn.execute("SELECT claimant FROM completion_claim WHERE"
                                " task_id=?", (did,)).fetchone()["claimant"]
        actor = conn.execute("SELECT actor FROM event WHERE dispatch_id=?"
                             " AND kind='claim'", (did,)).fetchone()["actor"]
        conn.close()
        self.assertEqual(claimant, seat_id,
                         "the authorized id is the persisted claimant")
        self.assertEqual(actor, seat_id,
                         "the authorized id is the claim event's actor")

    def test_intruder_claim_in_owner_state_refused_at_cli(self):


        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="own-1", subject="pr",
                                 workflow="pr", repo="example-app",
                                 owner_seat="own-1", reviewer_seat="rev-1")
        conn.close()

        out = subprocess.run([sys.executable, ORC, "claim-done", did,
                              "--note", "x"], text=True, capture_output=True,
                             env=self.env)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("ORC_SEAT_ID", out.stdout + out.stderr)

        env = dict(self.env)
        env["ORC_SEAT_ID"] = "intruder-seat"
        out = subprocess.run([sys.executable, ORC, "claim-done", did,
                              "--note", "mine now"], text=True,
                             capture_output=True, env=env)
        self.assertNotEqual(out.returncode, 0)
        blob = out.stdout + out.stderr
        self.assertIn("intruder-seat", blob)
        self.assertIn("own-1", blob, "the refusal names the real owed seat")
        conn = wp.connect_writable()
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM completion_claim WHERE task_id=?",
            (did,)).fetchone(), "an intruder claim must leave no row")
        conn.close()

        env["ORC_SEAT_ID"] = "own-1"
        env["NW_BUS_CLI"] = self._bus_stub(
            "bus-owner.sh", "echo '{\"msg_id\":\"m-own\"}'\n")
        out = subprocess.run([sys.executable, ORC, "claim-done", did,
                              "--note", "actually done"], text=True,
                             capture_output=True, env=env)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)

    def test_claim_refused_where_reviewer_is_owed(self):


        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="own-1", subject="pr",
                                 workflow="pr", repo="example-app",
                                 owner_seat="own-1", reviewer_seat="rev-1")
            conn.execute("UPDATE dispatch SET state='awaiting-review'"
                         " WHERE id=?", (did,))
        row = wp.fetch(conn, did)
        with self.assertRaises(SystemExit):
            wp.claim_open(conn, row, "not mine to claim")
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM completion_claim WHERE task_id=?", (did,)).fetchone(),
            "a refused claim must leave no suppressive row")
        conn.close()


        env = dict(self.env)
        env["ORC_SEAT_ID"] = "rev-1"
        out = subprocess.run([sys.executable, ORC, "claim-done", did,
                              "--note", "x"], text=True, capture_output=True,
                             env=env)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("claim", out.stdout + out.stderr)

    def test_claim_and_judge_notify_commit_atomically(self):


        conn = wp.connect_writable()
        self._grant_commander(conn)
        with conn:
            did = wp.insert_task(conn, recipient="tmux9", subject="s",
                                 check_cmd="true")
        row = wp.fetch(conn, did)
        real = wp.record_msg
        wp.record_msg = lambda *a, **k: (_ for _ in ()).throw(
            sqlite3.OperationalError("disk gone"))
        try:
            with self.assertRaises(sqlite3.OperationalError):
                wp.claim_commit(conn, row, "will crash")
        finally:
            wp.record_msg = real
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM completion_claim WHERE task_id=?", (did,)).fetchone(),
            "crashed claim must roll back the claim row")
        self.assertIsNone(conn.execute(
            "SELECT 1 FROM event WHERE dispatch_id=? AND kind='claim'",
            (did,)).fetchone(), "crashed claim must roll back the event too")

        claim = wp.claim_commit(conn, row, "for real")
        msg = conn.execute("SELECT send_state FROM task_msg WHERE id=?",
                           (claim["msg_row"],)).fetchone()
        self.assertEqual(msg["send_state"], "recorded",
                         "notify row is durable before any send")
        conn.close()

    def test_mechanical_auto_chase_never_rejects(self):


        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="tmux9", subject="s",
                                 check_cmd="true")
            wp.claim_open(conn, wp.fetch(conn, did), "done")
        with conn:
            wp.record(conn, did, "auto-chase", "engine: DEADLINE OVERDUE: ...")
        with conn:
            self.assertIsNotNone(
                wp.claim_standing(conn, wp.fetch(conn, did)),
                "an engine chase must not reject a human-judged claim")
        with conn:
            wp.record(conn, did, "chase", "judge: check output still fails")
        with conn:
            self.assertIsNone(wp.claim_standing(conn, wp.fetch(conn, did)),
                              "a human chase does reject")
        conn.close()

    def test_claim_survives_new_connection_and_process(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="tmux9", subject="s",
                                 check_cmd="true")
            wp.claim_open(conn, wp.fetch(conn, did), "durable")
        conn.close()
        fresh = sqlite3.connect(self.env["DISPATCH_LEDGER_DB"])
        fresh.row_factory = sqlite3.Row
        row = fresh.execute("SELECT status FROM completion_claim WHERE"
                            " task_id=?", (did,)).fetchone()
        fresh.close()
        self.assertEqual(row["status"], "standing",
                         "the claim is visible to a brand-new connection")
        board = self.run_cli(ORC, "board")
        data = json.loads(self.run_cli(ORC, "board", "--json"))
        task = next(t for t in data["tasks"] if t["id"] == did)
        self.assertIn("CLAIMS-DONE r1", task["flags"])
        self.assertIn("Completion claimed; awaiting review", board)


class TurnEventTests(StoreTestCase):


    REPORTER = str(ROOT / "scripts" / "orc-turn-report.py")
    INSTALLER = str(ROOT / "scripts" / "install-turn-hooks.py")

    def _canary(self, *seats):
        f = Path(self.tmp.name) / "canary.json"
        f.write_text(json.dumps(list(seats)))
        return str(f)

    def test_presence_upserts_one_bounded_row_with_counters(self):
        conn = wp.connect_writable()
        with conn:
            wp.turn_record(conn, "s1", "start", pane="7", harness="claude")
            wp.turn_record(conn, "s1", "end")
            wp.turn_record(conn, "s1", "start")
        rows = conn.execute("SELECT * FROM seat_presence").fetchall()
        self.assertEqual(len(rows), 1, "bounded: one row per seat, ever")
        self.assertEqual((rows[0]["kind"], rows[0]["starts"], rows[0]["ends"]),
                         ("start", 2, 1),
                         "counters carry the volume/missed-end telemetry")
        self.assertEqual(wp.seat_turn_state(conn, "s1")[0], "start")
        self.assertEqual(wp.seat_turn_state(conn, "nobody"), (None, 0))
        with self.assertRaises(ValueError):
            wp.turn_record(conn, "s1", "banana")
        conn.close()

    def _run_reporter(self, *argv, stdin="", env_extra=None):
        env = dict(self.env)
        env.pop("ORC_SEAT_ID", None)
        env.pop("TMUX_PANE", None)
        env.pop("NW_TURN_REPORT_OFF", None)
        env.update(env_extra or {})
        return subprocess.run([sys.executable, self.REPORTER, *argv],
                              input=stdin, text=True, capture_output=True,
                              env=env)

    def _rows(self):
        conn = wp.connect_writable()
        rows = conn.execute("SELECT seat, kind, harness FROM seat_presence"
                            " ORDER BY seat").fetchall()
        conn.close()
        return [(r["seat"], r["kind"], r["harness"]) for r in rows]

    def test_reporter_records_canary_seat_only(self):
        canary = self._canary("seat-A")
        out = self._run_reporter("--kind", "start", "--harness", "opencode",
                                 env_extra={"ORC_SEAT_ID": "seat-A",
                                            "NW_TURN_CANARY_FILE": canary})
        self.assertEqual(out.returncode, 0, out.stderr)
        out = self._run_reporter("--kind", "start",
                                 env_extra={"ORC_SEAT_ID": "seat-NOT-CANARY",
                                            "NW_TURN_CANARY_FILE": canary})
        self.assertEqual(out.returncode, 0)
        self.assertEqual(self._rows(), [("seat-A", "start", "opencode")],
                         "non-canary seats stay silent, fail-closed")

    def test_reporter_has_no_independent_sqlite_writer(self):
        source = Path(self.REPORTER).read_text()
        self.assertNotIn("sqlite3.connect", source)
        self.assertIn("wp.connect_writable(timeout=2)", source)

    def test_kill_switch_and_missing_canary_file_fail_closed(self):
        out = self._run_reporter("--kind", "end",
                                 env_extra={"ORC_SEAT_ID": "seat-A",
                                            "NW_TURN_CANARY_FILE":
                                            self._canary("seat-A"),
                                            "NW_TURN_REPORT_OFF": "1"})
        self.assertEqual(out.returncode, 0)
        out = self._run_reporter("--kind", "end",
                                 env_extra={"ORC_SEAT_ID": "seat-A",
                                            "NW_TURN_CANARY_FILE":
                                            str(Path(self.tmp.name) / "absent")})
        self.assertEqual(out.returncode, 0)
        self.assertEqual(self._rows(), [], "off switch / no list = no rows")

    def test_reporter_maps_claude_hook_payloads(self):
        env = {"ORC_SEAT_ID": "seat-B",
               "NW_TURN_CANARY_FILE": self._canary("seat-B")}
        for name in ("UserPromptSubmit", "Stop"):
            out = self._run_reporter("--harness", "claude",
                                     stdin=json.dumps({"hook_event_name": name}),
                                     env_extra=env)
            self.assertEqual(out.returncode, 0, out.stderr)
        self._run_reporter("--harness", "claude",
                           stdin=json.dumps({"hook_event_name": "Stop",
                                             "stop_hook_active": True}),
                           env_extra=env)
        conn = wp.connect_writable()
        row = conn.execute("SELECT kind, starts, ends FROM seat_presence"
                           " WHERE seat='seat-B'").fetchone()
        conn.close()
        self.assertEqual((row["kind"], row["starts"], row["ends"]),
                         ("end", 1, 1), "re-fired Stop not double-counted")

    def test_reporter_is_silent_without_identity_or_on_garbage(self):
        canary = self._canary("whoever")
        poison_bin = Path(self.tmp.name) / "poison-bin"
        poison_bin.mkdir()
        tmux_marker = Path(self.tmp.name) / "tmux-was-called"
        fake_tmux = poison_bin / "tmux"
        fake_tmux.write_text(
            "#!/bin/sh\n: > \"${TURN_REPORT_TMUX_MARKER:?}\"\nexit 99\n")
        fake_tmux.chmod(0o755)
        out = self._run_reporter("--kind", "end",
                                 env_extra={
                                     "NW_TURN_CANARY_FILE": canary,
                                     "PATH": str(poison_bin),
                                     "TURN_REPORT_TMUX_MARKER":
                                     str(tmux_marker),
                                 })
        self.assertEqual(out.returncode, 0)
        self.assertFalse(tmux_marker.exists(),
                         "missing TMUX_PANE must not scan a tmux server")
        out = self._run_reporter("--harness", "claude", stdin="not json {{",
                                 env_extra={"ORC_SEAT_ID": "whoever",
                                            "NW_TURN_CANARY_FILE": canary})
        self.assertEqual(out.returncode, 0, "garbage stdin must not error")
        self.assertEqual(self._rows(), [])

    def test_reporter_resolves_seat_from_agent_bus_database(self):
        self._seed_bus_identity("seat-C", pane="%42")
        out = self._run_reporter("--kind", "start", "--harness", "codex",
                                 env_extra={"TMUX_PANE": "%42",
                                            "NW_TURN_CANARY_FILE":
                                            self._canary("seat-C")})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self._rows(), [("seat-C", "start", "codex")])

    def test_shadow_phase_changes_no_ladder_behavior(self):


        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="bus-only-seat", subject="t",
                                 check_cmd="true")
            generation = wp.continuation_context(
                conn, wp.fetch(conn, did))["generation"]
            wp.wake_attempt_open(conn, did, "bus-only-seat", "pull",
                                 generation)
            wp.turn_record(conn, "bus-only-seat", "start", harness="dsh")
        conn.close()
        self.run_cli(ORC, "tick")
        conn = wp.connect_writable()
        att = conn.execute("SELECT resolved_ms, outcome FROM wake_attempt"
                           " WHERE task_id=?", (did,)).fetchone()
        conn.close()
        self.assertEqual(att["resolved_ms"], 0,
                         "shadow phase: presence must not resolve attempts")

    def test_reporter_p95_overhead_measured(self):


        canary = self._canary("seat-P")
        env = dict(self.env)
        env.update({"ORC_SEAT_ID": "seat-P", "NW_TURN_CANARY_FILE": canary})
        times = []
        for _ in range(20):
            t0 = time.monotonic()
            subprocess.run([sys.executable, self.REPORTER, "--kind", "start"],
                           text=True, capture_output=True, env=env)
            times.append(time.monotonic() - t0)
        times.sort()
        p95 = times[int(len(times) * 0.95) - 1]
        print(f"\n  [turn-report p95 over 20 runs: {p95 * 1000:.0f}ms]")
        self.assertLess(p95, 2.0, "reporter must stay far under hook timeout")

    def test_legacy_installer_is_read_only(self):


        settings = Path(self.tmp.name) / "settings.json"
        original = json.dumps({"hooks": {"SessionStart": [
            {"matcher": "", "hooks": [{"type": "command",
                                       "command": "keep-me"}]}]}})
        settings.write_text(original)
        env = dict(self.env)
        env["TURN_HOOKS_CLAUDE_SETTINGS"] = str(settings)
        manifest = Path(self.tmp.name) / "artifacts.json"
        manifest.write_text(json.dumps({
            "$schema": "synthetic-schema.json", "schema": "orc-rollout/v1",
            "states": ["ABSENT", "STAGED", "INSTALLED", "ACTIVATION_REQUIRED",
                       "ACTIVE_UNVERIFIED", "VERIFIED", "BLOCKED_TRUST", "DRIFTED",
                       "FAILED", "UNKNOWN"],
            "artifacts": [{
                "id": "claude-turn-hooks", "class": "hooks",
                "source": ["scripts/orc-turn-report.py"], "harness": "claude",
                "seat": "machine", "activation": "process-restart",
                "target_state": "INSTALLED", "install": "status-only",
                "config": str(settings), "format": "claude-json", "events": ["Stop"],
                "command": "python3 synthetic-reporter",
            }],
        }))
        policy = json.loads(_TEST_POLICY.read_text())
        policy["rollout"] = {"manifest": str(manifest)}
        configuration = Path(self.tmp.name) / "runtime.json"
        configuration.write_text(json.dumps(policy))
        env["FLEET_ORCHESTRATOR_CONFIG"] = str(configuration)
        env["ROLLOUT_HOME"] = self.tmp.name
        out = subprocess.run([sys.executable, self.INSTALLER], text=True,
                             capture_output=True, env=env)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("deprecated; no files changed", out.stdout)
        self.assertEqual(settings.read_text(), original)


class NudgeCoalesceTests(StoreTestCase):


    def setUp(self):
        super().setUp()
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "orc_mod", ROOT / "scripts" / "fleet-orchestrator.py")
        self.orc = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.orc)

    def _plan(self, conn, n):
        due = []
        with conn:
            for i in range(n):
                did = wp.insert_task(conn, recipient="tmux9",
                                     subject=f"t{i}", check_cmd="true")
                context = self.set_current_drive(
                    conn, did, state=wp.S_PULLED)
                gen = context["generation"]
                wp.wake_attempt_open(conn, did, "tmux9", "pull", gen)
                due.append((did, gen))
        return {"tmux9": {"pane_id": "%9", "window": "9", "due": due}}

    def test_one_tap_carries_every_due_task(self):
        conn = wp.connect_writable()
        plan = self._plan(conn, 3)
        calls = []

        class FakeSend:
            @staticmethod
            def send_outcome(*a, **k):
                calls.append((a, k))
                return (SendOutcome.CONTACTED, "")
        sent = self.orc.flush_seat_nudges(conn, plan, FakeSend)
        self.assertEqual(len(calls), 1, "three due tasks, ONE pane touch")
        self.assertEqual(sent, 1)
        args, kwargs = calls[0]
        reminder = args[1]
        self.assertIn("inspect your assigned unfinished task", reminder)
        self.assertIn("This reminder grants no authority", reminder)
        for did, _generation in plan["tmux9"]["due"]:
            self.assertIn(did, reminder)
            self.assertIn(f"task show {did}", reminder)
            self.assertIn("orc -t ", reminder)
        self.assertNotIn("nudge_key", kwargs,
                         "an actionable reminder keeps the peer-message header")
        fails = conn.execute("SELECT SUM(fails) FROM wake_attempt").fetchone()[0]
        self.assertEqual(fails, 0)
        conn.close()

    def test_task_closed_after_pairing_is_removed_before_contact(self):
        conn = wp.connect_writable()
        plan = self._plan(conn, 2)
        stale, current = plan["tmux9"]["due"]
        with conn:
            conn.execute("UPDATE dispatch SET state='closed',resolution='done'"
                         " WHERE id=?", (stale[0],))
            wp.record(conn, stale[0], "close:done", "finished concurrently")
        calls = []

        class FakeSend:
            @staticmethod
            def send_outcome(*args, **kwargs):
                calls.append(args[1])
                return (SendOutcome.CONTACTED, "")

        self.assertEqual(self.orc.flush_seat_nudges(conn, plan, FakeSend), 1)
        self.assertEqual(len(calls), 1)
        self.assertNotIn(stale[0], calls[0])
        self.assertIn(current[0], calls[0])
        wake = conn.execute(
            "SELECT resolved_ms,outcome FROM wake_attempt WHERE task_id=?",
            (stale[0],),
        ).fetchone()
        self.assertGreater(wake["resolved_ms"], 0)
        self.assertEqual(wake["outcome"], "responsibility-changed")

    def test_task_voice_after_pairing_cancels_contact(self):
        conn = wp.connect_writable()
        plan = self._plan(conn, 1)
        task_id, _generation = plan["tmux9"]["due"][0]
        with conn:
            self.record_current_voice(
                conn, task_id, "note", "responded before pane contact")

        class MustNotSend:
            @staticmethod
            def send_outcome(*_args, **_kwargs):
                raise AssertionError("task voice must cancel the reminder")

        self.assertEqual(self.orc.flush_seat_nudges(conn, plan, MustNotSend), 0)
        wake = conn.execute(
            "SELECT resolved_ms,outcome FROM wake_attempt WHERE task_id=?",
            (task_id,),
        ).fetchone()
        self.assertGreater(wake["resolved_ms"], 0)
        self.assertEqual(wake["outcome"], "reacted-voice-before-contact")

    def test_failed_tap_counts_a_fail_on_every_carried_task(self):
        conn = wp.connect_writable()
        plan = self._plan(conn, 3)
        class Boom:
            @staticmethod
            def send_outcome(*a, **k):
                return (SendOutcome.ENTER_UNCONFIRMED, "NOT submitted")
        sent = self.orc.flush_seat_nudges(conn, plan, Boom)
        self.assertEqual(sent, 0)
        rows = conn.execute("SELECT fails FROM wake_attempt").fetchall()
        self.assertEqual([r["fails"] for r in rows], [1, 1, 1],
                         "each carried task backs off honestly")
        conn.close()


class TerminalNotifyTests(StoreTestCase):


    def _open(self, *extra, seat="req-1", seed=True):
        env = dict(self.env)
        if seat:
            env["ORC_SEAT_ID"] = seat
            if seed:
                self._seed_bus_identity(seat)
        out = subprocess.run([sys.executable, ORC, "open", "--to", "tmux9",
                              "--subject", "awaited work", "--check", "true",
                              *extra], text=True, capture_output=True, env=env)
        return out

    def _task_id(self):
        conn = wp.connect_writable()
        did = conn.execute("SELECT id FROM dispatch ORDER BY created_ms DESC"
                           " LIMIT 1").fetchone()["id"]
        conn.close()
        return did

    def _terminal_rows(self, did):
        conn = wp.connect_writable()
        rows = conn.execute("SELECT target, subject FROM task_msg WHERE"
                            " task_id=? AND purpose='terminal'",
                            (did,)).fetchall()
        conn.close()
        return rows

    def test_await_requires_a_stable_identity(self):
        out = self._open("--await", seat=None)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("Agent Bus identity", out.stdout + out.stderr)

    def test_await_rejects_an_override_that_is_not_an_active_object(self):
        self._seed_bus_identity("some-other-seat")
        out = self._open("--await", seat="missing-seat", seed=False)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("not an active, unexpired Agent Bus receiver",
                      out.stdout + out.stderr)
        conn = wp.connect_writable()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM dispatch")
                         .fetchone()[0], 0)
        conn.close()

    def test_awaited_close_notifies_the_requester_exactly_once(self):
        out = self._open("--await")
        self.assertEqual(out.returncode, 0, out.stderr)
        did = self._task_id()
        env = dict(self.env)
        env["ORC_SEAT_ID"] = "closer-2"
        env["NW_BUS_CLI"] = str(Path(self.tmp.name) / "no-bus")

        out = subprocess.run([sys.executable, ORC, "close", did,
                              "--resolution", "done"], text=True,
                             capture_output=True, env=env)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        rows = self._terminal_rows(did)
        self.assertEqual(len(rows), 1, "exactly one terminal notification")
        self.assertEqual(rows[0]["target"], "req-1",
                         "addressed to the stable requester seat id")
        self.assertIn("done", rows[0]["subject"])

    def test_closer_is_requester_means_silence(self):
        self._open("--await")
        did = self._task_id()
        env = dict(self.env)
        env["ORC_SEAT_ID"] = "req-1"
        subprocess.run([sys.executable, ORC, "close", did, "--resolution",
                        "dropped"], text=True, capture_output=True, env=env)
        self.assertEqual(self._terminal_rows(did), [],
                         "you do not get mail about your own close")

    def test_unawaited_close_notifies_nobody(self):
        self._open()
        did = self._task_id()
        env = dict(self.env)
        env["ORC_SEAT_ID"] = "closer-2"
        subprocess.run([sys.executable, ORC, "close", did, "--resolution",
                        "done"], text=True, capture_output=True, env=env)
        self.assertEqual(self._terminal_rows(did), [],
                         "no opt-in, no notification - the operator ruling")

    def test_mechanical_terminal_notifies_via_helper(self):
        self._seed_bus_identity("req-9")
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="own-1", subject="pr",
                                 workflow="pr", repo="example-app",
                                 owner_seat="own-1", reviewer_seat="rev-1",
                                 requester_seat="req-9", await_notify=1)
            conn.execute("UPDATE dispatch SET state='closed',"
                         " resolution='done' WHERE id=?", (did,))
            row = wp.fetch(conn, did)
            rid = wp.terminal_notify(conn, row, "done", closer="engine",
                                     via="merged")
        self.assertIsNotNone(rid)
        self.assertEqual(self._terminal_rows(did)[0]["target"], "req-9")

        with conn:
            self.assertIsNone(wp.terminal_notify(conn, row, "done",
                                                 closer="engine",
                                                 via="merged"))
        conn.close()
        self.assertEqual(len(self._terminal_rows(did)), 1)

    def test_expired_zero_attempt_terminal_message_moves_to_operator_once(self):
        requester = "00000000-0000-4000-8000-000000000005"
        self._seed_bus_identity(requester)
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(
                conn, recipient="tmux9", subject="historical terminal",
                check_cmd="true", requester_seat=requester, await_notify=1,
            )
            conn.execute(
                "UPDATE dispatch SET state='closed',resolution='done'"
                " WHERE id=?", (did,),
            )
            msg_id = self.record_current_message(
                conn, did, "terminal", f"terminal:{did}", requester,
                "task completed", "done",
            )
            conn.execute(
                "UPDATE task_msg SET at_ms=? WHERE id=?",
                (wp.now() - wp.SEND_RETRY_WINDOW_S - 60, msg_id),
            )

        self.assertEqual([row["id"] for row in wp.dead_letters(conn)],
                         [msg_id])
        self.assertEqual(wp.escalate_dead_letters(
            conn, log=lambda *_: None), 1)
        self.assertEqual(wp.dead_letters(conn), [])
        operator_rows = conn.execute(
            "SELECT body FROM dispatch WHERE recipient='operator'"
            " AND state!='closed'"
        ).fetchall()
        self.assertEqual(len(operator_rows), 1)
        body = operator_rows[0]["body"]
        self.assertIn("requires operator attention", body)
        self.assertIn(requester, body)
        self.assertIn("No transport error was recorded.", body)
        self.assertNotIn("unregistered name", body)
        self.assertNotIn("no resolvable recipient", body)

        out = subprocess.run([sys.executable, LEDGER, "doctor"], text=True,
                             capture_output=True, env=self.env)
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertNotIn("dead letter:", out.stdout)

    def test_historical_invalid_requester_stops_with_durable_evidence(self):

        self._seed_bus_identity("some-other-seat")
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(
                conn, recipient="tmux9", subject="historical bad requester",
                check_cmd="true", requester_seat="stale-object",
                await_notify=1)
        conn.close()
        env = dict(self.env)
        env["ORC_SEAT_ID"] = "closer-2"
        out = subprocess.run(
            [sys.executable, ORC, "close", did, "--resolution", "done"],
            text=True, capture_output=True, env=env)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("stopped before transport", out.stdout)
        conn = wp.connect_writable()
        msg = conn.execute(
            "SELECT target, send_state, attempts, last_error FROM task_msg"
            " WHERE task_id=? AND purpose='terminal'", (did,)).fetchone()
        saved = conn.execute(
            "SELECT requester_seat FROM dispatch WHERE id=?", (did,)
        ).fetchone()["requester_seat"]
        self.assertEqual(saved, "stale-object",
                         "the original requested address remains evidence")
        self.assertEqual((msg["target"], msg["send_state"], msg["attempts"]),
                         ("stale-object", "invalid-target", 0))
        self.assertIn("not an active, unexpired, addressable Agent Bus identity",
                      msg["last_error"])
        with mock.patch.object(wp, "bus_send") as send:
            self.assertEqual(wp.retry_unsent(conn), (0, 0))
            send.assert_not_called()
        conn.close()
        shown = self.run_cli(ORC, "show", did)
        self.assertIn("terminal notifications ---", shown)
        self.assertIn("invalid-target  attempts=0/", shown)
        self.assertIn("transport skipped", shown)

    def test_unreadable_identity_db_is_not_a_durable_negative(self):
        self._seed_bus_identity("req-recover")
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(
                conn, recipient="worker", subject="identity db outage",
                check_cmd="true", requester_seat="req-recover",
                await_notify=1,
            )
            conn.execute("UPDATE dispatch SET state='closed',resolution='done'"
                         " WHERE id=?", (did,))
            row = wp.fetch(conn, did)
        missing = str(Path(self.tmp.name) / "missing-agent-bus.sqlite3")
        with mock.patch.dict(os.environ, {"AGENT_BUS_DB": missing}):
            with conn:
                msg_id = wp.terminal_notify(
                    conn, row, "done", closer="closer", via="close",
                )
        msg = conn.execute(
            "SELECT send_state,attempts FROM task_msg WHERE id=?", (msg_id,),
        ).fetchone()
        self.assertEqual(tuple(msg), ("recorded", 0))
        accepted = subprocess.CompletedProcess(
            ["matrix-bus", "send"], 0,
            stdout='{"msg_id":"terminal-ok",'
                   '"recipient_agent_ids":["req-recover"]}\n',
            stderr="",
        )
        with mock.patch.object(wp.subprocess, "run",
                               return_value=accepted):
            self.assertEqual(wp.retry_unsent(conn), (1, 0))
        msg = conn.execute(
            "SELECT send_state,attempts,recipient_agent_id FROM task_msg"
            " WHERE id=?", (msg_id,),
        ).fetchone()
        self.assertEqual(tuple(msg), ("accepted", 1, "req-recover"))

    def test_sender_only_requester_is_not_reported_as_retrying(self):

        self._seed_bus_identity("requester-cron", harness="cron")
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(
                conn, recipient="tmux9", subject="sender-only requester",
                check_cmd="true", requester_seat="requester-cron",
                await_notify=1,
            )
            conn.execute(
                "INSERT INTO seat (agent_id,handle,aliases,host,tmux,status,"
                " addressable,updated_at,refreshed_ms)"
                " VALUES ('requester-cron','example-host/cron','','host','',"
                " 'active',0,'',0)"
            )
        conn.close()
        env = dict(self.env)
        env["ORC_SEAT_ID"] = "closer-2"
        out = subprocess.run(
            [sys.executable, ORC, "close", did, "--resolution", "done"],
            text=True, capture_output=True, env=env,
        )
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("stopped before transport", out.stdout)
        self.assertNotIn("tick retries", out.stdout)
        conn = wp.connect_writable()
        msg = conn.execute(
            "SELECT send_state,attempts FROM task_msg WHERE task_id=?"
            " AND purpose='terminal'", (did,),
        ).fetchone()
        self.assertEqual((msg["send_state"], msg["attempts"]),
                         ("invalid-target", 0))
        conn.close()


class ReviewReconcileTests(StoreTestCase):


    HEAD = "f81ada8338f81ada8338f81ada8338f81ada8338"

    def _gh_stub(self, review_state="APPROVED", commit=None,
                 login="rev-login"):
        commit = commit or self.HEAD
        stub = Path(self.tmp.name) / "gh-reconcile.sh"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            "if [ \"$1\" = api ]; then\n"
            f"  echo '[{{\"state\":\"{review_state}\",\"commit_id\":\"{commit}\","
            f"\"user\":{{\"login\":\"{login}\"}}}}]'\n"
            "elif [ \"$2\" = list ]; then echo '[]'\n"
            f"else echo '{self.HEAD}'\nfi\n")
        stub.chmod(0o755)
        self.env["NW_GH_CLI"] = str(stub)
        os.environ["NW_GH_CLI"] = str(stub)

    def _sanction(self, *logins):
        path = Path(self.tmp.name) / "sanctioned.json"
        path.write_text(json.dumps(list(logins)))
        self.env["NW_REVIEW_SANCTIONED_FILE"] = str(path)
        os.environ["NW_REVIEW_SANCTIONED_FILE"] = str(path)

    def _pr_task(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="own-1", subject="review",
                                 workflow="pr", repo="example-app",
                                 owner_seat="own-1", reviewer_seat="rev-1",
                                 links="example-app#54671")
            conn.execute("UPDATE dispatch SET state='awaiting-review'"
                         " WHERE id=?", (did,))
        conn.close()
        return did

    def _rows(self, did, purpose):
        conn = wp.connect_writable()
        msgs = conn.execute("SELECT target, body FROM task_msg WHERE"
                            " task_id=? AND purpose=?",
                            (did, purpose)).fetchall()
        events = conn.execute("SELECT COUNT(*) FROM event WHERE dispatch_id=?"
                              " AND kind=?", (did, purpose)).fetchone()[0]
        conn.close()
        return msgs, events

    def _desync_rows(self, did):
        return self._rows(did, "review-desync")

    def tearDown(self):
        os.environ.pop("NW_GH_CLI", None)
        os.environ.pop("NW_REVIEW_SANCTIONED_FILE", None)
        super().tearDown()

    def test_sanctioned_review_chases_without_presuming_ownership(self):
        self._sanction("rev-login")
        self._gh_stub()
        did = self._pr_task()
        self.run_cli(ORC, "tick")
        msgs, events = self._desync_rows(did)
        self.assertEqual(events, 1, "one typed review-desync event")
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["target"], "rev-1")
        body = msgs[0]["body"]
        self.assertIn(f"orc verdict {did}", body,
                      "the chase names the exact missing artifact")
        self.assertIn("rev-login", body, "the chase names the login")
        self.assertIn(self.HEAD, body, "the chase names the head")
        self.assertIn("VERIFY", body, "verify-before-any-verdict is the ask")

        self.run_cli(ORC, "tick")
        msgs, events = self._desync_rows(did)
        self.assertEqual((len(msgs), events), (1, 1),
                         "once per head, never a drumbeat")

    def test_historical_nudge_shape_is_unreproducible(self):


        for state in ("APPROVED", "CHANGES_REQUESTED"):
            with self.subTest(state=state):
                self._sanction("rev-login")
                self._gh_stub(review_state=state)
                did = self._pr_task()
                self.run_cli(ORC, "tick")
                msgs, _ = self._desync_rows(did)
                self.assertEqual(len(msgs), 1)
                body = msgs[0]["body"].lower()
                self.assertNotIn("your", body,
                                 "ownership is never presumed")
                self.assertNotIn("--clean", body,
                                 "no pre-filled verdict direction")
                self.assertNotIn("--blockers", body,
                                 "no pre-filled verdict direction")

    def test_foreign_review_alerts_commander_not_reviewer(self):
        self._sanction("rev-login")
        self._gh_stub(login="testaccount-lang")
        did = self._pr_task()
        self.run_cli(ORC, "tick")
        desync_msgs, desync_events = self._desync_rows(did)
        self.assertEqual((len(desync_msgs), desync_events), (0, 0),
                         "an unsanctioned review must not chase the seat")
        msgs, events = self._rows(did, "foreign-review")
        self.assertEqual(events, 1)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["target"], "role:commander")
        self.assertIn("testaccount-lang", msgs[0]["body"])
        self.assertIn(self.HEAD, msgs[0]["body"])

        self.run_cli(ORC, "tick")
        msgs, events = self._rows(did, "foreign-review")
        self.assertEqual((len(msgs), events), (1, 1))

    def test_empty_map_keeps_the_pass_inert(self):
        self._sanction()
        self._gh_stub(login="anyone-at-all")
        did = self._pr_task()
        self.run_cli(ORC, "tick")
        self.assertEqual(self._desync_rows(did), ([], 0))
        self.assertEqual(self._rows(did, "foreign-review"), ([], 0),
                         "empty map means the pass detects NOTHING")

    def test_missing_map_keeps_the_pass_inert(self):
        self.env["NW_REVIEW_SANCTIONED_FILE"] = str(
            Path(self.tmp.name) / "absent.json")
        os.environ["NW_REVIEW_SANCTIONED_FILE"] = self.env[
            "NW_REVIEW_SANCTIONED_FILE"]
        self._gh_stub()
        did = self._pr_task()
        self.run_cli(ORC, "tick")
        self.assertEqual(self._desync_rows(did), ([], 0))
        self.assertEqual(self._rows(did, "foreign-review"), ([], 0))

    def test_stale_head_review_is_not_a_discrepancy(self):
        self._sanction("rev-login")
        self._gh_stub(commit="0ld" + "0" * 37)
        did = self._pr_task()
        self.run_cli(ORC, "tick")
        msgs, events = self._desync_rows(did)
        self.assertEqual((len(msgs), events), (0, 0),
                         "a review of an older head proves nothing about"
                         " the current one")

    def test_forge_failure_claims_nothing(self):
        self._sanction("rev-login")
        stub = Path(self.tmp.name) / "gh-down.sh"
        stub.write_text("#!/usr/bin/env bash\nif [ \"$2\" = list ]; then"
                        " echo '[]'; else exit 1; fi\n")
        stub.chmod(0o755)
        self.env["NW_GH_CLI"] = str(stub)
        did = self._pr_task()
        self.run_cli(ORC, "tick")
        msgs, events = self._desync_rows(did)
        self.assertEqual((len(msgs), events), (0, 0),
                         "an unreachable forge is unknown, not evidence")


class StaleSendTests(StoreTestCase):


    BUS = str(ROOT / "scripts" / "agent-bus-v3.py")

    def _bus_db(self):
        import sqlite3 as s3
        db = Path(self.env["MATRIX_BUS_CFG"]) / "agent-bus-v3.sqlite3"
        conn = s3.connect(db)
        conn.row_factory = s3.Row
        conn.executescript(
            "CREATE TABLE IF NOT EXISTS identities (agent_id TEXT PRIMARY"
            " KEY, slot TEXT UNIQUE NOT NULL, handle TEXT UNIQUE NOT NULL,"
            " generation INTEGER NOT NULL, status TEXT NOT NULL, harness"
            " TEXT NOT NULL, mode TEXT NOT NULL, host TEXT NOT NULL, tmux"
            " TEXT NOT NULL, aliases_json TEXT NOT NULL DEFAULT '[]',"
            " created_ms INTEGER NOT NULL, updated_ms INTEGER NOT NULL,"
            " lease_until_ms INTEGER, heartbeat_fails INTEGER NOT NULL"
            " DEFAULT 0, heartbeat_last_error TEXT);"
            "CREATE TABLE IF NOT EXISTS inbox (agent_id TEXT, msg_id TEXT,"
            " sender_agent_id TEXT DEFAULT '', sender_handle TEXT DEFAULT '',"
            " subject TEXT DEFAULT '', body TEXT DEFAULT '', priority TEXT"
            " DEFAULT 'normal', created_ms INTEGER, expires_ms INTEGER,"
            " state TEXT, attempts INTEGER DEFAULT 0,"
            " lease_until_ms INTEGER);")
        return conn

    def test_expire_by_msg_tombstones_pending_not_done(self):
        conn = self._bus_db()
        with conn:
            conn.execute("INSERT INTO inbox (agent_id, msg_id, created_ms,"
                         " state) VALUES ('a1','m1',1,'available'),"
                         " ('a2','m1',1,'available'),"
                         " ('a1','m2',1,'done')")
        conn.close()
        out = subprocess.run([sys.executable, self.BUS, "expire", "--msg",
                              "m1", "--reason", "task closed"],
                             text=True, capture_output=True, env=self.env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('"tombstoned":2', out.stdout,
                      "m1 expires for BOTH recipients; the done row is"
                      " untouched history")
        conn = self._bus_db()
        rows = conn.execute("SELECT msg_id, state, expires_ms FROM inbox"
                            " ORDER BY msg_id, agent_id").fetchall()
        conn.close()
        self.assertTrue(all(r["expires_ms"] is not None
                            for r in rows if r["msg_id"] == "m1"))
        self.assertIsNone([r for r in rows if r["state"] == "done"][0]
                          ["expires_ms"], "done rows keep their history")

    def test_close_expires_the_tasks_unprocessed_sends(self):
        conn = wp.connect_writable()
        with conn:
            did = wp.insert_task(conn, recipient="tmux9", subject="s",
                                 check_cmd="true")
            rid = self.record_current_message(conn, did, "dispatch", f"d:{did}", "tmux9",
                                "subj", "body")
            conn.execute("UPDATE task_msg SET send_state='accepted',"
                         " msg_id='m-stale' WHERE id=?", (rid,))
        conn.close()
        calls = Path(self.tmp.name) / "expire-calls.log"
        stub = Path(self.tmp.name) / "bus-expire-stub.sh"
        stub.write_text("#!/usr/bin/env bash\n"
                        f"echo \"$@\" >> {calls}\n"
                        "echo '{\"tombstoned\":1}'\n")
        stub.chmod(0o755)
        env = dict(self.env)
        env["NW_BUS_CLI"] = str(stub)
        env["ORC_SEAT_ID"] = "closer-1"
        out = subprocess.run([sys.executable, ORC, "close", did,
                              "--resolution", "done"], text=True,
                             capture_output=True, env=env)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        logged = calls.read_text()
        self.assertIn("expire --msg m-stale", logged,
                      "the close tombstones the task's pending delivery")

    def test_retire_sweeps_the_seats_pending_inbox(self):
        conn = self._bus_db()
        with conn:
            conn.execute("INSERT INTO identities (agent_id, slot, handle,"
                         " generation, status, harness, mode, host, tmux,"
                         " created_ms, updated_ms) VALUES ('zomb-1',"
                         " 'host/zombie', 'host/zombie-tmux99', 1, 'active',"
                         " 'dsh', 'pull', 'host', 'tmux=0:99.0', 1, 1)")
            conn.execute("INSERT INTO inbox (agent_id, msg_id, created_ms,"
                         " state) VALUES ('zomb-1','z1',1,'available'),"
                         " ('zomb-1','z2',1,'available'),"
                         " ('zomb-1','z3',1,'done')")
        conn.close()


        (Path(self.env["MATRIX_BUS_CFG"]) / "auth.hdr").write_text(
            "Authorization: Bearer test-dummy\n")
        env = dict(self.env)
        env["MATRIX_BUS_HS"] = "http://127.0.0.1:1"
        env["AGENT_BUS_TRANSPORT"] = "matrix"
        env["MATRIX_BUS_ROOM"] = "!messages:example.invalid"
        env["MATRIX_BUS_REGISTRY_ROOM"] = "!registry:example.invalid"
        out = subprocess.run([sys.executable, self.BUS, "retire",
                              "host/zombie-tmux99"], text=True,
                             capture_output=True, env=env)
        self.assertEqual(out.returncode, 0,
                         "a durable local retire must not report failure"
                         f" just because the registry publish failed"
                         f" (stdout={out.stdout[:80]!r}"
                         f" stderr={out.stderr[-200:]!r})")
        self.assertIn("tombstoned 2 pending inbox message(s)", out.stdout)
        self.assertIn("Matrix state publication failed", out.stderr,
                      "the degraded publish is warned, not hidden")
        conn = self._bus_db()
        rows = conn.execute("SELECT msg_id, state, expires_ms FROM inbox"
                            " WHERE agent_id='zomb-1'"
                            " ORDER BY msg_id").fetchall()
        conn.close()
        now = int(time.time() * 1000)
        for row in rows:
            if row["state"] == "done":
                self.assertIsNone(row["expires_ms"],
                                  "done rows keep their history")
            else:
                self.assertIsNotNone(
                    row["expires_ms"],
                    f"{row['msg_id']}: the sweep must be durable BEFORE"
                    " the Matrix leg, dead homeserver or not")
                self.assertLessEqual(row["expires_ms"], now)


class ParkedDispatchTests(StoreTestCase):


    def test_open_parked_sets_the_flag_and_says_so(self):
        out = subprocess.run(
            [sys.executable, LEDGER, "open", "--to", "tmux9", "--subject",
             "queued work", "--no-check", "--parked"],
            text=True, capture_output=True, env=self.env)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("PARKED", out.stdout)
        conn = wp.connect_writable()
        row = conn.execute("SELECT id, no_chase FROM dispatch").fetchone()
        conn.close()
        self.assertEqual(row["no_chase"], 1)

    def test_explicit_chase_unparks(self):
        conn = wp.connect_writable()
        with conn:
            tid = wp.insert_task(conn, recipient="tmux9", subject="s",
                                 check_cmd="true", no_chase=1)
        conn.close()
        out = subprocess.run(
            [sys.executable, LEDGER, "chase", tid, "--note", "need it now"],
            text=True, capture_output=True, env=self.env)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        conn = wp.connect_writable()
        flag = conn.execute("SELECT no_chase FROM dispatch WHERE id=?",
                            (tid,)).fetchone()[0]
        conn.close()
        self.assertEqual(flag, 0,
                         "an explicit chase declares the ladder is wanted")


class WakeLeaseTests(StoreTestCase):


    def test_lease_exclusivity_one_winner_causes_attach(self):


        conn = wp.connect_writable()
        with conn:
            wid1, won1 = wp.wake_lease_acquire(conn, "seat-1", 5, "tick",
                                               [("task-a", "pull")])
            wid2, won2 = wp.wake_lease_acquire(conn, "seat-1", 5, "liveness",
                                               [("task-b", "nudge")])
        self.assertTrue(won1)
        self.assertFalse(won2)
        self.assertEqual(wid1, wid2, "the loser learns the winner's wake")
        causes = {(r["task_id"], r["purpose"]) for r in conn.execute(
            "SELECT task_id, purpose FROM wake_cause WHERE wake_id=?",
            (wid1,))}
        self.assertEqual(causes, {("task-a", "pull"), ("task-b", "nudge")},
                         "both causes ride the ONE wake")
        dedupe_rows = conn.execute(
            "SELECT detail FROM wake_event WHERE kind='would-have-deduped'"
        ).fetchall()
        conn.close()
        self.assertEqual(len(dedupe_rows), 1, "the evidence counter for the"
                         " expansion review")
        self.assertIn("open wake already held", dedupe_rows[0]["detail"],
                      "the READ layer deduped this one - pins that layer"
                      " distinctly from the PK-arbiter path")

    def test_db_pk_is_the_final_arbiter_when_the_read_misses(self):


        conn = wp.connect_writable()
        with conn:
            wid1, won1 = wp.wake_lease_acquire(conn, "seat-1", 5, "tick")
        with mock.patch.object(wp, "_wake_lease_read", return_value=None):
            with conn:
                wid2, won2 = wp.wake_lease_acquire(conn, "seat-1", 5,
                                                   "liveness",
                                                   [("task-b", "nudge")])
        self.assertTrue(won1)
        self.assertFalse(won2, "the DB refused the steal")
        self.assertEqual(wid1, wid2)
        races = conn.execute("SELECT COUNT(*) FROM wake_event WHERE"
                             " detail LIKE '%lost the acquire race%'"
                             ).fetchone()[0]
        leases = conn.execute("SELECT COUNT(*) FROM wake_lease").fetchone()[0]
        conn.close()
        self.assertEqual((races, leases), (1, 1))

    def test_record_before_act_the_lease_row_is_durable(self):


        conn = wp.connect_writable()
        with conn:
            wid, won = wp.wake_lease_acquire(conn, "seat-1", 5, "tick")
        other = wp.connect_writable()
        row = other.execute("SELECT state, holder FROM wake_lease WHERE"
                            " seat='seat-1' AND generation=5").fetchone()
        other.close(); conn.close()
        self.assertTrue(won)
        self.assertEqual((row["state"], row["holder"]), ("leased", "tick"))

    def test_crash_window_sweep_applies_the_typed_exit_never_silence(self):


        conn = wp.connect_writable()
        with conn:
            wp.wake_lease_acquire(conn, "seat-1", 5, "tick")
            conn.execute("UPDATE wake_lease SET opened_s=opened_s-?",
                         (wp.wake_start_timeout_s() + 1,))
        with conn:
            swept = wp.wake_sweep(conn)
        self.assertEqual(swept, 1)
        row = conn.execute("SELECT state, released_s FROM wake_lease"
                           " WHERE seat='seat-1'").fetchone()
        self.assertEqual(row["state"], "exit:start-timeout")
        self.assertIsNotNone(row["released_s"])
        named = conn.execute("SELECT COUNT(*) FROM wake_event WHERE"
                             " kind='exit:start-timeout'").fetchone()[0]
        conn.close()
        self.assertEqual(named, 1, "the exit is a recorded event")

    def test_no_rewake_inside_a_concluded_generation(self):


        conn = wp.connect_writable()
        with conn:
            wp.wake_lease_acquire(conn, "seat-1", 5, "tick")
            wp.wake_lease_release(conn, "seat-1", 5, "exit:start-timeout")
            wid, won = wp.wake_lease_acquire(conn, "seat-1", 5, "tick")
        conn.close()
        self.assertEqual((wid, won), (None, False))

    def test_supersession_reparents_causes_no_orphans(self):


        conn = wp.connect_writable()
        with conn:
            old_wid, _ = wp.wake_lease_acquire(conn, "seat-1", 5, "tick",
                                               [("task-a", "pull")])
            released = wp.wake_lease_supersede(conn, "seat-1", 6)
            new_wid, won = wp.wake_lease_acquire(conn, "seat-1", 6, "tick",
                                                 [("task-b", "pull")])
        self.assertEqual(released, 1)
        self.assertTrue(won)
        old_state = conn.execute("SELECT state FROM wake_lease WHERE"
                                 " generation=5").fetchone()["state"]
        self.assertEqual(old_state, "superseded")
        new_causes = {r["task_id"] for r in conn.execute(
            "SELECT task_id FROM wake_cause WHERE wake_id=?", (new_wid,))}
        conn.close()
        self.assertEqual(new_causes, {"task-a", "task-b"},
                         "the old wake's cause rides the new one")

    def test_kill_switch_silences_everything(self):


        conn = wp.connect_writable()
        with mock.patch.dict(os.environ, {"NW_WAKE_LEASE_OFF": "1"}):
            with conn:
                wid, won = wp.wake_lease_acquire(conn, "seat-1", 5, "tick",
                                                 [("task-a", "pull")])
                wp.wake_lease_supersede(conn, "seat-1", 6)
                wp.wake_sweep(conn)
                rode = wp.wake_cause_ride(conn, "seat-1", "task-a", "terminal")
        leases = conn.execute("SELECT COUNT(*) FROM wake_lease").fetchone()[0]
        events = conn.execute("SELECT COUNT(*) FROM wake_event").fetchone()[0]
        conn.close()
        self.assertEqual((wid, won, rode, leases, events),
                         (None, True, False, 0, 0))

    def test_release_must_be_typed(self):
        conn = wp.connect_writable()
        with conn:
            wp.wake_lease_acquire(conn, "seat-1", 5, "tick")
            with self.assertRaises(ValueError):
                wp.wake_lease_release(conn, "seat-1", 5, "whatever")
        conn.close()

    def test_notifier_rides_and_never_acquires(self):


        conn = wp.connect_writable()
        with conn:
            self.assertFalse(wp.wake_cause_ride(conn, "seat-1", "task-t",
                                                "terminal"))
            wid, _ = wp.wake_lease_acquire(conn, "seat-1", 5, "tick")
            self.assertTrue(wp.wake_cause_ride(conn, "seat-1", "task-t",
                                               "terminal"))
        rows = conn.execute("SELECT COUNT(*) FROM wake_lease").fetchone()[0]
        rode = conn.execute("SELECT COUNT(*) FROM wake_cause WHERE"
                            " task_id='task-t'").fetchone()[0]
        conn.close()
        self.assertEqual((rows, rode), (1, 1),
                         "riding created no second lease")

    def test_event_trace_is_bounded(self):
        conn = wp.connect_writable()
        with conn:
            wp.wake_event(conn, "seat-1", None, "leased", "old")
            conn.execute("UPDATE wake_event SET at_s=at_s-?",
                         (wp.wake_event_retention_s() + 1,))
            wp.wake_event(conn, "seat-1", None, "leased", "fresh")
            wp.wake_sweep(conn)
        kept = [r["detail"] for r in conn.execute(
            "SELECT detail FROM wake_event")]
        conn.close()
        self.assertEqual(kept, ["fresh"], "retention prunes, never grows")

    def test_unknowable_generation_is_none_never_fabricated(self):
        self.assertIsNone(wp.bus_inbox_generation("nobody"),
                          "absent bus DB reads as UNKNOWN, not zero")

    def test_all_released_wakes_reparent_not_just_the_latest(self):


        conn = wp.connect_writable()
        with conn:
            for gen, cause in ((1, "task-a"), (2, "task-b"), (3, "task-c")):
                wp.wake_lease_acquire(conn, "seat-1", gen, "tick",
                                      [(cause, "pull")])
                wp.wake_lease_release(conn, "seat-1", gen,
                                      "exit:start-timeout")
            new_wid, won = wp.wake_lease_acquire(conn, "seat-1", 4, "tick")
        causes = {r["task_id"] for r in conn.execute(
            "SELECT task_id FROM wake_cause WHERE wake_id=?", (new_wid,))}
        conn.close()
        self.assertTrue(won)
        self.assertEqual(causes, {"task-a", "task-b", "task-c"},
                         "no middle wake's cause is dropped")

    def test_ride_miss_pools_seat_scoped_and_next_acquire_adopts(self):
        conn = wp.connect_writable()
        with conn:
            self.assertFalse(wp.wake_cause_ride(conn, "seat-1", "task-t",
                                                "terminal"))
            other_wid, _ = wp.wake_lease_acquire(conn, "seat-2", 1, "tick")
            mine_wid, _ = wp.wake_lease_acquire(conn, "seat-1", 1, "tick")
        other = {r["task_id"] for r in conn.execute(
            "SELECT task_id FROM wake_cause WHERE wake_id=?", (other_wid,))}
        mine = {r["task_id"] for r in conn.execute(
            "SELECT task_id FROM wake_cause WHERE wake_id=?", (mine_wid,))}
        conn.close()
        self.assertNotIn("task-t", other,
                         "another seat's wake never adopts the pooled cause")
        self.assertIn("task-t", mine, "the seat's own next acquire does")

    def test_turn_started_then_process_timeout_sweep_leg(self):


        conn = wp.connect_writable()
        with conn:
            wp.wake_lease_acquire(conn, "seat-1", 5, "tick")
            self.assertTrue(wp.wake_lease_turn_started(conn, "seat-1", 5))
            conn.execute("UPDATE wake_lease SET updated_s=updated_s-?",
                         (wp.wake_process_timeout_s() + 1,))
            swept = wp.wake_sweep(conn)
        row = conn.execute("SELECT state FROM wake_lease").fetchone()
        conn.close()
        self.assertEqual((swept, row["state"]),
                         (1, "exit:process-timeout"))

    def test_kill_switch_survives_garbage_in_neighbor_vars(self):


        conn = wp.connect_writable()
        with mock.patch.dict(os.environ, {
                "NW_WAKE_LEASE_OFF": "1",
                "NW_WAKE_START_TIMEOUT_S": "not-a-number",
                "NW_WAKE_PROCESS_TIMEOUT_S": ""}):
            with conn:
                wid, won = wp.wake_lease_acquire(conn, "s", 1, "tick")
                wp.wake_sweep(conn)
            self.assertEqual((wid, won), (None, True))
        with mock.patch.dict(os.environ,
                             {"NW_WAKE_START_TIMEOUT_S": "garbage"}):
            self.assertEqual(wp.wake_start_timeout_s(), 120,
                             "garbage reads as the default")
        rows = conn.execute("SELECT COUNT(*) FROM wake_lease").fetchone()[0]
        conn.close()
        self.assertEqual(rows, 0)


class WakeContactTests(StoreTestCase):


    def _signal(self, seat, generation):


        db = Path(str(wp.CFG)) / "agent-bus-v3.sqlite3"
        db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db)
        conn.executescript("CREATE TABLE IF NOT EXISTS inbox_signal ("
                           "agent_id TEXT PRIMARY KEY, generation INTEGER"
                           " NOT NULL DEFAULT 0)")
        conn.execute("INSERT OR REPLACE INTO inbox_signal VALUES (?,?)",
                     (seat, generation))
        conn.commit(); conn.close()

    def test_record_before_act_lease_exists_when_send_runs(self):
        self._signal("seat-1", 5)
        conn = wp.connect_writable()
        seen = {}

        def send(progress):
            seen["lease"] = conn.execute(
                "SELECT state FROM wake_lease WHERE seat='seat-1'"
            ).fetchone()
            return (SendOutcome.CONTACTED, "sent")

        out = wp.wake_contact(conn, "seat-1", "%9", "tick",
                              [("task-a", "pull")], send)
        self.assertEqual(out, (SendOutcome.CONTACTED, "sent"))
        self.assertIsNotNone(seen["lease"],
                             "the lease row was durable BEFORE the contact")
        contacts = conn.execute("SELECT COUNT(*) FROM wake_event WHERE"
                                " kind='contact'").fetchone()[0]
        conn.close()
        self.assertEqual(contacts, 1,
                         "exactly one contact event per coalesced touch")

    def test_coalesced_touch_counts_once_with_all_causes(self):
        self._signal("seat-1", 5)
        conn = wp.connect_writable()
        wp.wake_contact(conn, "seat-1", "%9", "tick",
                        [("t1", "pull"), ("t2", "pull"), ("t3", "pull")],
                        lambda progress: (SendOutcome.CONTACTED, ""))
        contacts = conn.execute("SELECT COUNT(*) FROM wake_event WHERE"
                                " kind='contact'").fetchone()[0]
        causes = conn.execute("SELECT COUNT(*) FROM wake_cause").fetchone()[0]
        conn.close()
        self.assertEqual((contacts, causes), (1, 3),
                         "one touch, every carried task a cause")

    def test_typed_outcomes_map_to_their_own_exits(self):


        cases = [
            ((SendOutcome.HELD_FOCUS, "panel owns the pane"), "exit:held-focus", 0),
            ((SendOutcome.ENTER_UNCONFIRMED, "still sitting"),
             "exit:enter-unconfirmed", 0),
            ((SendOutcome.SENT_BUT_HELD, "submitted, record stays"),
             "exit:sent-but-held", 0),
            ((SendOutcome.REFUSED_STRAND, "stranded needle recorded"), "leased", 0),
            ((SendOutcome.DEAD_TARGET, "pane is dead"), "leased", 0),
        ]
        for i, (ret, want_state, want_contacts) in enumerate(cases):
            with self.subTest(outcome=ret[0]):
                seat = f"seat-{i}"
                self._signal(seat, 5)
                conn = wp.connect_writable()
                wp.wake_contact(conn, seat, "%9", "tick",
                                [("task-a", "pull")],
                                lambda progress, _r=ret: _r)
                row = conn.execute("SELECT state FROM wake_lease WHERE"
                                   " seat=?", (seat,)).fetchone()
                contacts = conn.execute(
                    "SELECT COUNT(*) FROM wake_event WHERE seat=? AND"
                    " kind='contact'", (seat,)).fetchone()[0]
                conn.close()
                self.assertEqual(row["state"], want_state)
                self.assertEqual(contacts, want_contacts)

    def test_unknown_exception_is_its_own_typed_exit(self):
        self._signal("seat-x", 5)
        conn = wp.connect_writable()

        def send(progress):
            raise RuntimeError("some unrecognized explosion")

        with self.assertRaises(RuntimeError):
            wp.wake_contact(conn, "seat-x", "%9", "tick", [], send)
        row = conn.execute("SELECT state, released_s FROM wake_lease"
                           " WHERE seat='seat-x'").fetchone()
        conn.close()
        self.assertEqual(row["state"], "exit:unknown-error",
                         "never a silent leased row, never a guess")
        self.assertIsNotNone(row["released_s"])

    def test_out_of_contract_outcome_is_a_loud_programming_error(self):
        self._signal("seat-y", 5)
        conn = wp.connect_writable()
        with self.assertRaises(ValueError):
            wp.wake_contact(conn, "seat-y", "%9", "tick", [],
                            lambda progress: ("MAYBE", ""))
        state = conn.execute("SELECT state FROM wake_lease WHERE"
                             " seat='seat-y'").fetchone()["state"]
        conn.close()
        self.assertEqual(state, "exit:unknown-error")

    def test_wrapper_refuses_non_physical_pane_keys(self):


        conn = wp.connect_writable()
        ran = []
        for bad in ("9", "%42x", "%", "4:2.0", "%09", "%foo", "%%9"):
            with self.subTest(pane=bad):
                with self.assertRaises(ValueError):
                    wp.wake_contact(conn, "seat-1", bad, "tick", [],
                                    lambda progress:
                                        ran.append(1) or (SendOutcome.CONTACTED, ""))
        conn.close()
        self.assertEqual(ran, [], "refusal precedes any contact")

    def test_refused_outcome_records_noop_never_contact(self):


        self._signal("seat-1", 5)
        conn = wp.connect_writable()
        wp.wake_contact(conn, "seat-1", "%9", "handshake", [],
                        lambda progress: (SendOutcome.REFUSED_TARGET, "not an agent"))
        kinds = [r["kind"] for r in conn.execute(
            "SELECT kind FROM wake_event WHERE kind IN"
            " ('contact', 'contact-noop')")]
        conn.close()
        self.assertEqual(kinds, ["contact-noop"])

    def test_real_process_death_classified_by_the_sweep(self):


        self._signal("seat-1", 5)
        code = (
            "import sys, os; sys.path.insert(0, %r)\n"
            "import importlib.util\n"
            "spec = importlib.util.spec_from_file_location('ats', %r)\n"
            "ats = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(ats)\n"
            "from unittest import mock\n"
            "def run(args, **k):\n"
            "    if 'display-message' in args:\n"
            "        return mock.Mock(returncode=0,"
            " stdout='4:2.0\\t%%35\\tclaude\\t0\\n', stderr='')\n"
            "    if 'send-keys' in args: os._exit(9)\n"
            "    return mock.Mock(returncode=0, stdout='', stderr='')\n"
            "sys.path.insert(0, %r)\n"
            "import workplane as wp\n"
            "conn = wp.connect_writable()\n"
            "with mock.patch.object(ats.subprocess, 'run', side_effect=run):\n"
            "    wp.wake_contact(conn, 'seat-1', '%%9', 'tick',"
            " [('task-a', 'pull')],\n"
            "        lambda progress: ats.send_outcome('4:2.0', '',"
            " nudge_key='pull', progress=progress))\n"
        ) % (str(ROOT / "scripts" / "lib"),
             str(ROOT / "scripts" / "agent-tmux-send.py"),
             str(ROOT / "scripts" / "lib"))
        env = dict(self.env)
        env["AGENT_TMUX_SEND_SUBMIT_DELAY_S"] = "0"
        out = subprocess.run([sys.executable, "-c", code], env=env,
                             text=True, capture_output=True)
        self.assertEqual(out.returncode, 9, out.stderr)
        conn = wp.connect_writable()
        row = conn.execute("SELECT state, wake_id FROM wake_lease WHERE"
                           " seat='seat-1'").fetchone()
        self.assertEqual(row["state"], "leased",
                         "the death left no in-process classification")
        pasted = conn.execute("SELECT COUNT(*) FROM wake_event WHERE"
                              " wake_id=? AND kind='pasted'",
                              (row["wake_id"],)).fetchone()[0]
        self.assertEqual(pasted, 1,
                         "the REAL paste boundary left its durable event")
        with conn:
            conn.execute("UPDATE wake_lease SET opened_s=opened_s-?",
                         (wp.wake_start_timeout_s() + 1,))
            wp.wake_sweep(conn)
        state = conn.execute("SELECT state FROM wake_lease WHERE"
                             " seat='seat-1'").fetchone()["state"]
        conn.close()
        self.assertEqual(state, "exit:enter-unconfirmed",
                         "pasted-then-died reads as enter-unconfirmed")

    def test_terminal_generation_contact_pools_causes(self):


        conn = wp.connect_writable()
        with conn:
            wp.wake_lease_acquire(conn, "seat-1", 5, "tick")
            wp.wake_lease_release(conn, "seat-1", 5, "exit:start-timeout")
            wid, won = wp.wake_lease_acquire(conn, "seat-1", 5, "notifier",
                                             [("task-late", "terminal")])
            self.assertEqual((wid, won), (None, False))
            new_wid, _ = wp.wake_lease_acquire(conn, "seat-1", 6, "tick")
        causes = {r["task_id"] for r in conn.execute(
            "SELECT task_id FROM wake_cause WHERE wake_id=?", (new_wid,))}
        conn.close()
        self.assertIn("task-late", causes,
                      "the concluded-generation cause rode the next wake")

    def test_shadow_never_gates_the_send(self):


        self._signal("seat-1", 0)
        conn = wp.connect_writable()
        with conn:
            wp.wake_lease_acquire(conn, "seat-1", 0, "tick")
            wp.wake_lease_release(conn, "seat-1", 0, "exit:start-timeout")
        ran = []
        wp.wake_contact(conn, "seat-1", "%9", "tick", [],
                        lambda progress: ran.append(1) or (SendOutcome.CONTACTED, ""))
        conn.close()
        self.assertEqual(ran, [1], "the shadow outcome never blocks v1")

    def test_flush_treats_sent_but_held_as_failure(self):


        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "orc_for_outcome_tests", ROOT / "scripts" / "fleet-orchestrator.py")
        orc = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = orc
        spec.loader.exec_module(orc)
        conn = wp.connect_writable()
        with conn:
            tid = wp.insert_task(conn, recipient="tmux9", subject="s",
                                 check_cmd="true")
            context = self.set_current_drive(
                conn, tid, state=wp.S_PULLED)
            generation = context["generation"]
            wp.wake_attempt_open(conn, tid, "tmux9", "pull", generation)
        plan = {"tmux9": {"pane_id": "%9", "window": "9",
                           "due": [(tid, generation)]}}

        class Held:
            @staticmethod
            def send_outcome(*a, **k):
                return (SendOutcome.SENT_BUT_HELD, "pane blocked")
        sent = orc.flush_seat_nudges(conn, plan, Held)
        self.assertEqual(sent, 0, "SENT_BUT_HELD is never a success")
        fails = conn.execute("SELECT SUM(fails) FROM wake_attempt"
                             ).fetchone()[0]
        conn.close()
        self.assertEqual(fails, 1, "the carried task backs off")

    def test_liveness_clock_advances_only_on_contacted(self):


        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "orc_for_liveness_outcome", ROOT / "scripts" / "fleet-orchestrator.py")
        orc = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = orc
        spec.loader.exec_module(orc)
        conn = wp.connect_writable()
        logs = []
        member = {"agent_id": "seat-9", "handle": "h/seat-tmux9",
                  "aliases": [], "host": "test-host", "status": "active",
                  "mode": "watch", "tmux": "tmux=0:9.0 win=claude"}
        with mock.patch.object(orc, "log", side_effect=logs.append):
            for now in (10_000_000_000, 10_000_000_000 + 40 * 60 * 1000):

                orc.tick_seat_liveness(
                    conn, dry=False, members=[member],
                    panes=[("%9", "0:9.0")],
                    watcher_alive=lambda aid: False,
                    unread_count=lambda aid: 3,
                    nudge=lambda p, progress=None:
                        (SendOutcome.SENT_BUT_HELD, "blocked"),
                    now_ms=now,
                    hostnames={"test-host"})
        clock = conn.execute("SELECT last_nudge_ms FROM seat_watch WHERE"
                             " agent_id='seat-9'").fetchone()[0]
        conn.close()
        self.assertEqual(clock, 0, "a typed failure never advances the"
                         " rate-limit clock")
        self.assertTrue(any(SendOutcome.SENT_BUT_HELD in l for l in logs),
                        f"the failure logs its own name: {logs}")
        self.assertFalse(any(l.startswith("OK seat-liveness: pull nudge")
                             for l in logs),
                         "never OK on a typed failure")

    def test_liveness_retirement_requires_an_accepted_probe_and_full_wait(self):
        orc = self._load_orc("orc_for_liveness_probe_gate")
        conn = wp.connect_writable()
        member = {"agent_id": "seat-gone", "handle": "h/gone-tmux9",
                  "aliases": [], "host": "test-host", "status": "active",
                  "mode": "watch", "tmux": "tmux=0:9.0 win=claude"}
        retired = []
        first = 10_000_000_000

        def run(stamp):
            orc.tick_seat_liveness(
                conn, dry=False, members=[member], panes=[],
                watcher_alive=lambda _aid: False,
                unread_count=lambda _aid: 0,
                retire=lambda aid: retired.append(aid) or True,
                now_ms=stamp, hostnames={"test-host"},
            )

        with mock.patch.object(orc, "log", lambda *_: None), \
                mock.patch.object(wp, "bus_send", return_value=False):
            run(first)
            run(first + 1)
            watch = conn.execute(
                "SELECT probe_ms FROM seat_watch WHERE agent_id='seat-gone'"
            ).fetchone()
            self.assertEqual(watch["probe_ms"], 0)
            run(first + orc.LIVENESS_RETIRE_AFTER_S + 10)
            self.assertEqual(retired, [],
                             "elapsed absence cannot replace delivery proof")

            with conn:
                conn.execute("UPDATE task_msg SET send_state='accepted'"
                             " WHERE purpose='liveness-probe'")
            accepted_at = first + orc.LIVENESS_RETIRE_AFTER_S + 20
            run(accepted_at)
            run(accepted_at + orc.LIVENESS_RETIRE_AFTER_S - 1)
            self.assertEqual(retired, [],
                             "the seat receives the full post-probe wait")
            run(accepted_at + orc.LIVENESS_RETIRE_AFTER_S + 1)
        self.assertEqual(retired, ["seat-gone"])
        conn.close()

    def _load_orc(self, name):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            name, ROOT / "scripts" / "fleet-orchestrator.py")
        orc = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = orc
        spec.loader.exec_module(orc)
        return orc

    def test_outcome_matrix_success_iff_contacted_at_every_caller(self):


        orc = self._load_orc("orc_for_matrix_tests")


        names = sorted(k for k in vars(SendOutcome)
                       if k.isupper() and k != "ALL")
        outcomes = [(k, getattr(SendOutcome, k)) for k in names]
        member = {"agent_id": "seat-m", "handle": "h/seat-tmux9",
                  "aliases": [], "host": "test-host", "status": "active",
                  "mode": "watch", "tmux": "tmux=0:9.0 win=claude"}
        for name, outcome in outcomes:
            want_success = name == SendOutcome.CONTACTED
            with self.subTest(caller="flush", outcome=name):
                conn = wp.connect_writable()
                with conn:
                    tid = wp.insert_task(conn, recipient="tmux9",
                                         subject=f"m-{outcome}",
                                         check_cmd="true")
                    context = self.set_current_drive(
                        conn, tid, state=wp.S_PULLED)
                    generation = context["generation"]
                    wp.wake_attempt_open(
                        conn, tid, "tmux9", "pull", generation,
                    )
                plan = {"tmux9": {"pane_id": "%9", "window": "9",
                                   "due": [(tid, generation)]}}

                class Fake:
                    @staticmethod
                    def send_outcome(*a, **k):
                        return (outcome, "matrix")
                sent = orc.flush_seat_nudges(conn, plan, Fake)
                fails = conn.execute(
                    "SELECT SUM(fails) FROM wake_attempt WHERE task_id=?",
                    (tid,)).fetchone()[0]
                conn.close()
                self.assertEqual(sent == 1, want_success)
                self.assertEqual(fails == 0, want_success)
            with self.subTest(caller="liveness", outcome=name):
                conn = wp.connect_writable()
                with conn:
                    conn.execute("DELETE FROM seat_watch")


                    conn.execute("DELETE FROM wake_attempt WHERE"
                                 " seat='seat-m'")
                with mock.patch.object(orc, "log", lambda *a: None):
                    for now in (10_000_000_000,
                                10_000_000_000 + 40 * 60 * 1000):
                        orc.tick_seat_liveness(
                            conn, dry=False, members=[member],
                            panes=[("%9", "0:9.0")],
                            watcher_alive=lambda aid: False,
                            unread_count=lambda aid: 3,
                            nudge=lambda p, progress=None,
                                _o=outcome: (_o, "matrix"),
                            now_ms=now, hostnames={"test-host"})
                clock = conn.execute(
                    "SELECT last_nudge_ms FROM seat_watch WHERE"
                    " agent_id='seat-m'").fetchone()[0]
                conn.close()
                self.assertEqual(clock > 0, want_success,
                                 f"liveness clock vs {outcome}")
    def test_ast_no_raw_outcome_literals_repo_wide(self):


        import ast
        forbidden = set(SendOutcome.ALL)
        offenders = []
        for base in (ROOT / "scripts", ROOT / "tests"):
            for path in sorted(base.rglob("*.py")):
                if path.name == "send_outcome.py":
                    continue
                try:
                    tree = ast.parse(path.read_text(), filename=str(path))
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if (isinstance(node, ast.Constant)
                            and isinstance(node.value, str)
                            and node.value in forbidden):
                        offenders.append(
                            f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertEqual(offenders, [],
                         "outcome words exist in ONE module; everyone else"
                         f" imports them: {offenders}")

    def test_pane_flock_serializes_concurrent_contacts(self):


        import fcntl
        import threading
        conn2 = wp.connect_writable()
        order = []
        lock_path = wp._wake_pane_lock_path("%9")
        holder = open(lock_path, "a")
        fcntl.flock(holder, fcntl.LOCK_EX)

        def contender():
            conn3 = wp.connect_writable()
            wp.wake_contact(conn3, "seat-2", "%9", "liveness", [],
                            lambda progress:
                                order.append("contender") or
                                (SendOutcome.CONTACTED, ""))
            conn3.close()

        t = threading.Thread(target=contender)
        t.start()
        import time as _t
        _t.sleep(0.2)
        self.assertEqual(order, [], "the contact waits for the pane lock")
        order.append("holder-released")
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()
        t.join(timeout=10)
        conn2.close()
        self.assertEqual(order, ["holder-released", "contender"],
                         "strict ordering across concurrent wakers")


class RoleTargetTests(StoreTestCase):
    def test_grant_refuses_broadcast_but_revoke_can_clean_legacy_row(self):
        self.run_cli(ORC, "role", "grant", "legacy-role", "all",
                     "--by", "test", expect=1)
        conn = wp.connect_writable()
        with conn:
            conn.execute(
                "INSERT INTO role_assignment"
                " (role,agent_id,granted_by,granted_ms) VALUES"
                " ('legacy-role','all','old-version',1)"
            )
        self.run_cli(ORC, "role", "revoke", "legacy-role", "all")
        active = conn.execute(
            "SELECT 1 FROM role_assignment WHERE role='legacy-role'"
            " AND agent_id='all' AND revoked_ms IS NULL"
        ).fetchone()
        self.assertIsNone(active)


class AnnounceTargetTests(StoreTestCase):


    def _announce(self, *argv):
        log = Path(self.tmp.name) / "announce-sends.log"
        stub = Path(self.tmp.name) / "bus-stub.sh"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            "if [ \"$1\" = members ]; then exit 0; fi\n"
            f"echo \"$@\" >> {log}\n"
            "case \"$*\" in *bad-seat*) exit 1;; esac\n"
            "echo '{\"schema\":\"agent-bus/send-result/v3\","
            "\"msg_id\":\"stub\",\"transport_state\":\"accepted\","
            "\"recipients\":1}'\n")
        stub.chmod(0o755)
        env = dict(self.env)
        env["NW_BUS_CLI"] = str(stub)
        out = subprocess.run(
            [sys.executable, ORC, "announce", "--subject", "s",
             "--body", "b", *argv], text=True, capture_output=True, env=env)
        sends = log.read_text().splitlines() if log.exists() else []
        return out, sends

    def _rows(self):
        conn = wp.connect_writable()
        rows = conn.execute("SELECT target, dedup_key FROM task_msg WHERE"
                            " purpose='announce' ORDER BY target").fetchall()
        conn.close()
        return rows

    def test_whole_batch_is_committed_before_first_transport(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "orc_for_announce_commit_test", ORC)
        orc = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = orc
        spec.loader.exec_module(orc)
        observed = []

        def send(conn, _row_id):
            observed.append((conn.in_transaction, conn.execute(
                "SELECT COUNT(*) FROM task_msg WHERE purpose='announce'"
            ).fetchone()[0]))
            return True

        args = SimpleNamespace(
            to="example-host/a-tmux1,example-host/b-tmux2", fleet_wide=False,
            subject="s", body="b",
        )
        with mock.patch.object(wp, "bus_send", side_effect=send):
            self.assertEqual(orc.cmd_announce(args), 0)
        self.assertEqual(observed, [(False, 2), (False, 2)])

    def test_bare_announce_is_refused_with_the_two_paths_named(self):
        out, sends = self._announce()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("--to", out.stdout)
        self.assertIn("--fleet-wide", out.stdout)
        self.assertIn("EVERY watch-mode seat", out.stdout,
                      "the flag's cost is stated where it is refused")
        self.assertEqual(sends, [], "a refusal sends nothing")
        self.assertEqual(self._rows(), [], "a refusal records nothing")

    def test_to_list_routes_one_recorded_row_per_recipient(self):
        out, sends = self._announce("--to", "example-host/a-tmux1, example-host/b-tmux2")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("2 named seat(s)", out.stdout)
        self.assertEqual(len(sends), 2)
        self.assertTrue(any("example-host/a-tmux1" in s for s in sends))
        self.assertTrue(any("example-host/b-tmux2" in s for s in sends))
        rows = self._rows()
        self.assertEqual([r["target"] for r in rows],
                         ["example-host/a-tmux1", "example-host/b-tmux2"],
                         "record-before-send, one outbox row per recipient")

    def test_fleet_wide_needs_its_explicit_flag(self):
        out, sends = self._announce("--fleet-wide")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("whole fleet", out.stdout)
        self.assertEqual(len(sends), 1)
        self.assertIn(" all ", sends[0])

    def test_all_alias_cannot_bypass_fleet_wide_confirmation(self):
        for target in ("all", "@all"):
            with self.subTest(target=target):
                out, sends = self._announce("--to", target)
                self.assertNotEqual(out.returncode, 0)
                self.assertIn("--fleet-wide", out.stdout)
                self.assertEqual(sends, [])

    def test_to_and_fleet_wide_are_mutually_exclusive(self):
        out, sends = self._announce("--to", "example-host/a-tmux1", "--fleet-wide")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("mutually exclusive", out.stdout)
        self.assertEqual(sends, [])
        self.assertEqual(self._rows(), [])

    def test_failed_recipient_stays_recorded_for_the_tick_retry(self):
        out, sends = self._announce("--to", "example-host/a-tmux1,bad-seat")
        self.assertEqual(out.returncode, 1)
        self.assertIn("bad-seat", out.stdout)
        self.assertIn("tick retries", out.stdout)
        self.assertEqual(len(sends), 2, "both attempts were made")
        self.assertIn("bad-seat", [r["target"] for r in self._rows()],
                      "the failed recipient's row survives for the retry"
                      " pass")
class ReviewIntentTests(StoreTestCase):


    def _task(self):
        conn = wp.connect_writable()
        with conn:
            tid = wp.insert_task(conn, recipient="tmux9", subject="s",
                                 check_cmd="true")
        conn.close()
        return tid

    def _ledger(self, *argv, seat=""):
        env = dict(self.env)
        env["ORC_SEAT_ID"] = seat
        return subprocess.run([sys.executable, LEDGER, *argv],
                              text=True, capture_output=True, env=env)

    def test_declare_requires_identity_and_is_idempotent(self):
        tid = self._task()
        out = self._ledger("review-intent", tid, seat="")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("seat identity", out.stdout + out.stderr)
        for scope in ("probes", "probes round 2"):
            out = self._ledger("review-intent", tid, "--scope", scope,
                               seat="rev-1")
            self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        conn = wp.connect_writable()
        intents = wp.open_review_intents(conn, tid)
        conn.close()
        self.assertEqual(len(intents), 1, "one OPEN intent per (task, seat)")
        self.assertEqual(intents[0]["scope"], "probes round 2",
                         "re-declaring refreshes the scope")

    def test_crossing_note_warns_but_never_blocks(self):
        tid = self._task()
        self._ledger("review-intent", tid, "--scope", "mutation probes",
                     seat="rev-1")
        out = self._ledger("note", tid, "--note", "VERDICT: CLEAN",
                           seat="judge-2")
        self.assertEqual(out.returncode, 0,
                         "later evidence wins - a warning never blocks")
        self.assertIn("WARN", out.stdout)
        self.assertIn("rev-1", out.stdout)
        self.assertIn("mutation probes", out.stdout)
        conn = wp.connect_writable()
        still_open = wp.open_review_intents(conn, tid)
        conn.close()
        self.assertEqual(len(still_open), 1,
                         "someone ELSE's post never closes the intent")

    def test_own_note_lands_the_review_and_closes_the_intent(self):
        tid = self._task()
        self._ledger("review-intent", tid, seat="rev-1")
        out = self._ledger("note", tid, "--note", "BLOCK: two findings",
                           seat="rev-1")
        self.assertEqual(out.returncode, 0)
        self.assertIn("review-intent on", out.stdout)
        self.assertNotIn("WARN", out.stdout, "your own intent never warns")
        conn = wp.connect_writable()
        self.assertEqual(wp.open_review_intents(conn, tid), [])
        conn.close()

    def test_crossing_close_warns_and_still_closes(self):
        tid = self._task()
        self._ledger("review-intent", tid, "--scope", "full replay",
                     seat="rev-1")
        out = self._ledger("close", tid, "--resolution", "done",
                           seat="judge-2")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("WARN", out.stdout)
        self.assertIn("closed as done", out.stdout)

    def test_show_surfaces_open_intents(self):
        tid = self._task()
        self._ledger("review-intent", tid, "--scope", "OSError replay",
                     seat="rev-1")
        out = self._ledger("show", tid)
        self.assertIn("review intents (open)", out.stdout)
        self.assertIn("rev-1", out.stdout)
        self.assertIn("OSError replay", out.stdout)
        self._ledger("review-intent", tid, "--done", seat="rev-1")
        out = self._ledger("show", tid)
        self.assertNotIn("review intents (open)", out.stdout,
                         "a withdrawn intent leaves the section")

    def test_board_flags_inflight_reviews(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "orc_for_intent_tests", ROOT / "scripts" / "fleet-orchestrator.py")
        orc = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = orc
        spec.loader.exec_module(orc)
        tid = self._task()
        self._ledger("review-intent", tid, seat="rev-1")
        conn = wp.connect_writable()
        row = conn.execute("SELECT * FROM dispatch WHERE id=?",
                           (tid,)).fetchone()
        flags = orc.task_flags(conn, row)
        conn.close()
        self.assertTrue(any(f.startswith("REVIEWING(") for f in flags),
                        f"board must show the in-flight review: {flags}")


class DuplicateSeatLifecycleTests(StoreTestCase):


    BUS_DDL = (
        "CREATE TABLE identities (agent_id TEXT PRIMARY KEY, slot TEXT,"
        " handle TEXT, generation INTEGER, status TEXT, harness TEXT,"
        " mode TEXT, host TEXT, tmux TEXT, pane_id TEXT, retired_kind TEXT,"
        " aliases_json TEXT DEFAULT '[]', created_ms INTEGER,"
        " updated_ms INTEGER, lease_until_ms INTEGER);"
        "CREATE TABLE inbox (agent_id TEXT, msg_id TEXT, subject TEXT,"
        " created_ms INTEGER, expires_ms INTEGER, state TEXT);")

    def _orc(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "orc_for_succession_tests", ROOT / "scripts" / "fleet-orchestrator.py")
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod

    def _bus_db(self, *rows):
        seq = self.__dict__.setdefault("_bus_seq", [0])
        seq[0] += 1
        path = Path(self.tmp.name) / f"bus-fixture-{seq[0]}.sqlite3"
        conn = sqlite3.connect(path)
        conn.executescript(self.BUS_DDL)
        for r in rows:
            conn.execute(
                "INSERT INTO identities (agent_id, slot, handle, generation,"
                " status, harness, mode, host, tmux, pane_id, created_ms,"
                " updated_ms) VALUES (?,?,?,1,'active',?,?,?,?,?,1,1)",
                (r["agent_id"], r["slot"], r["handle"], r["harness"],
                 r["mode"], r.get("host", "host"), r.get("tmux", "tmux=0:9.0"),
                 r.get("pane_id", "%9")))
        conn.commit(); conn.close()
        return path

    PRED = {"agent_id": "pred-1", "slot": "host/old", "handle": "host/old-tmux9",
            "harness": "dsh", "mode": "pull"}

    def _bus_stub(self):


        log = Path(self.tmp.name) / "bus-sends.log"
        if log.exists():
            log.unlink()
        stub = Path(self.tmp.name) / "bus-stub.sh"
        stub.write_text(
            "#!/usr/bin/env bash\n"
            f"echo \"$@\" >> {log}\n"
            "echo '{\"schema\":\"agent-bus/send-result/v3\","
            "\"msg_id\":\"stub\",\"transport_state\":\"accepted\","
            "\"recipients\":1}'\n")
        stub.chmod(0o755)
        return stub, log

    def _run(self, orc, bus_db, *, watcher_alive=False, pane_cmd="opencode",
             retire_rc=0):
        lines, retire_calls = [], []

        class R:
            returncode = retire_rc
            stdout = ("retired [succession] host/old-tmux9 (pred-1);"
                      " tombstoned 2 pending inbox message(s)")
            stderr = ""

        stub, log = self._bus_stub()
        conn = wp.connect_writable()
        with mock.patch.dict(os.environ, {
                "MATRIX_BUS_CFG": self.env["MATRIX_BUS_CFG"],
                "NW_BUS_CLI": str(stub)}):
            rc = orc.run_pane_succession(
                conn, "host", "%9", bus_db=bus_db,
                watcher_alive=lambda aid: watcher_alive,
                pane_command=lambda pane: pane_cmd,
                retire=lambda aid: retire_calls.append(aid) or R(),
                log=lines.append)
        conn.close()
        sends = log.read_text().splitlines() if log.exists() else []
        return rc, "\n".join(lines), retire_calls, sends

    def test_free_plus_owed_is_all_or_nothing(self):


        orc = self._orc()
        free = dict(self.PRED, agent_id="free-1", slot="h/free",
                    handle="h/free-tmux9")
        owed = dict(self.PRED, agent_id="owed-1", slot="h/owed",
                    handle="h/owed-tmux9")
        bus_db = self._bus_db(free, owed)
        conn = wp.connect_writable()
        with conn:
            wp.insert_task(conn, recipient="h/owed-tmux9",
                           subject="unfinished", check_cmd="true")
        conn.close()
        rc, out, retire_calls, sends = self._run(orc, bus_db)
        self.assertEqual(rc, 3)
        self.assertIn("ALL-OR-NOTHING", out)
        self.assertEqual(retire_calls, [],
                         "the free predecessor is NOT retired while any"
                         " other is blocked")
        self.assertEqual(sends, [], "and nothing is broadcast")

    def test_predecessor_with_owed_task_fail_closes_with_instructions(self):
        orc = self._orc()
        bus_db = self._bus_db(self.PRED)
        conn = wp.connect_writable()
        with conn:
            tid = wp.insert_task(conn, recipient="host/old-tmux9",
                                 subject="unfinished", check_cmd="true")
        conn.close()
        rc, out, retire_calls, sends = self._run(orc, bus_db)
        self.assertEqual(rc, 3)
        self.assertIn(tid, out, "the owed task is named")
        self.assertIn("reassign", out, "handoff instructions are printed")
        self.assertEqual(retire_calls, [], "succession NEVER drops work")
        self.assertEqual(sends, [], "a refusal broadcasts nothing")

    def test_predecessor_with_role_fail_closes(self):
        orc = self._orc()
        bus_db = self._bus_db(self.PRED)
        conn = wp.connect_writable()
        with conn:
            conn.execute("INSERT INTO role_assignment (role, agent_id,"
                         " granted_by, granted_ms) VALUES"
                         " ('reviewer','pred-1','test',1)")
        conn.close()
        rc, out, retire_calls, sends = self._run(orc, bus_db)
        self.assertEqual(rc, 3)
        self.assertIn("role:reviewer", out)
        self.assertEqual(retire_calls, [])

    def test_live_watcher_holds(self):
        orc = self._orc()
        pred = dict(self.PRED, harness="claude", mode="watch")
        bus_db = self._bus_db(pred)
        rc, out, retire_calls, sends = self._run(orc, bus_db, watcher_alive=True)
        self.assertEqual(rc, 4)
        self.assertIn("AGENT_BUS_SLOT=host/old", out,
                      "the resume path is shown")
        self.assertEqual(retire_calls, [])

    def test_ambiguous_pane_command_holds(self):
        orc = self._orc()
        for cmd in ("dsh", None, "zsh"):
            with self.subTest(pane_cmd=cmd):
                bus_db = self._bus_db(self.PRED)
                rc, out, retire_calls, sends = self._run(orc, bus_db, pane_cmd=cmd)
                self.assertEqual(rc, 4, f"{cmd!r} could still be the"
                                 " predecessor - ambiguity holds")
                self.assertEqual(retire_calls, [])

    def test_absent_predecessor_retires_without_broadcast(self):
        orc = self._orc()
        bus_db = self._bus_db(self.PRED)
        rc, out, retire_calls, sends = self._run(orc, bus_db, pane_cmd="opencode")
        self.assertEqual(rc, 0)
        self.assertEqual(retire_calls, ["pred-1"])
        self.assertIn("tombstoned 2", out,
                      "swept mail counts are reported, never silent")
        conn = wp.connect_writable()
        broadcasts = conn.execute(
            "SELECT COUNT(*) FROM task_msg WHERE purpose='succession-retire'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(broadcasts, 0,
                         "retirement records no fleet-wide message")
        self.assertEqual(sends, [], "retirement sends no fleet-wide message")

    def test_unread_broadcasts_alone_never_block_succession(self):
        orc = self._orc()
        bus_db = self._bus_db(self.PRED)
        conn = sqlite3.connect(bus_db)
        with conn:
            conn.execute("INSERT INTO inbox (agent_id, msg_id, subject,"
                         " created_ms, state) VALUES"
                         " ('pred-1','b1','fleet announce',1,'available'),"
                         " ('pred-1','b2','self-heal notice',1,'available')")
        conn.close()
        rc, _out, retire_calls, sends = self._run(orc, bus_db, pane_cmd="claude")
        self.assertEqual(rc, 0, "unread mail alone keeps no corpse active")
        self.assertEqual(retire_calls, ["pred-1"])

    def test_dead_watch_predecessor_with_foreign_pane_command_retires(self):
        orc = self._orc()
        pred = dict(self.PRED, harness="claude", mode="watch")
        bus_db = self._bus_db(pred)
        rc, _out, retire_calls, sends = self._run(orc, bus_db,
                                           watcher_alive=False,
                                           pane_cmd="dsh")
        self.assertEqual(rc, 0)
        self.assertEqual(retire_calls, ["pred-1"])

    def test_empty_pane_reports_clear(self):
        orc = self._orc()
        bus_db = self._bus_db()
        rc, out, retire_calls, sends = self._run(orc, bus_db)
        self.assertEqual(rc, 0)
        self.assertIn("no active seat", out)
        self.assertEqual(retire_calls, [])

    def test_legacy_row_without_pane_id_found_via_location(self):


        orc = self._orc()
        pred = dict(self.PRED, pane_id=None, tmux="tmux=0:9.0 win=dsh")
        bus_db = self._bus_db(pred)
        conn = wp.connect_writable()
        with mock.patch.dict(os.environ, {
                "MATRIX_BUS_CFG": self.env["MATRIX_BUS_CFG"],
                "NW_BUS_CLI": str(self._bus_stub()[0])}):
            rc = orc.run_pane_succession(
                conn, "host", "%9", location="tmux=0:9.0 win=dsh",
                bus_db=bus_db,
                watcher_alive=lambda aid: False,
                pane_command=lambda pane: "opencode",
                retire=lambda aid: __import__("types").SimpleNamespace(
                    returncode=0, stdout="retired [succession] x", stderr=""),
                log=lambda *_: None)
        conn.close()
        self.assertEqual(rc, 0, "legacy predecessor found and retired")

if __name__ == "__main__":
    unittest.main()
