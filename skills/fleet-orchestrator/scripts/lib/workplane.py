"""Persistent fleet tasks, workflow transitions, and delivery state."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

import tmux_runtime
import runtime_paths as nw_paths
import runtime_config as cfg

SCRIPT_DIR = Path(__file__).resolve().parent.parent
CANONICAL_REPO_ROOT = cfg.path("canonical_source_root", SCRIPT_DIR.parent).resolve()
_DEFAULT_LEDGER = nw_paths.orchestrator_state_dir() / "dispatch-ledger.sqlite3"
PRODUCTION_DB_PATH = cfg.path("paths.ledger", _DEFAULT_LEDGER)
PROTECTED_DATABASES = tuple(Path(cfg.expand(value)) for value in
                            cfg.get("protected_databases", []))
PRODUCTION_NAMED_DB_ROOTS = tuple(Path(cfg.expand(value)) for value in
                                cfg.get("protected_named_database_roots", []))
CFG = Path(os.environ.get(
    "AGENT_BUS_CFG", os.environ.get("MATRIX_BUS_CFG",
        cfg.path("bus.config_directory", Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "fleet-orchestrator" / "bus"))))
DB_PATH = Path(os.environ.get("DISPATCH_LEDGER_DB", PRODUCTION_DB_PATH))

RESOLUTIONS = ("done", "dropped", "reassigned", "superseded")
EDGE_KINDS = ("supersedes", "blocks", "reassigned-to", "derived-from", "needs")
DEFAULT_AFTER = "45m"
CHECK_TIMEOUT_S = 45
MAX_BODY_BYTES = 64 * 1024


WAITING_STATE = "waiting-on-deps"
EVENT_OPEN_WAITING = "open-waiting"
EVENT_DEPS_CLEARED = "deps-cleared"
EVENT_BREAKER_FIRED = "breaker-fired"


NO_STATE_CHANGE_EVENTS = frozenset({EVENT_BREAKER_FIRED})

BREAKER_TIMEOUT_S = 60
BREAKER_FAIL_STREAK = 3
BREAKER_COOLDOWN_S = 6 * 3600
DEADLINE_COOLDOWN_S = 12 * 3600


DISPATCH_LEGACY_TRANSITIONS = {
    ("_new", "open"): "open",
    ("open", "ack"): "acked",
    ("open", "chase"): "open",

    ("open", "note"): "open",
    ("acked", "note"): "acked",
    ("open", "close"): "closed",
    ("acked", "ack"): "acked",
    ("acked", "chase"): "acked",
    ("acked", "close"): "closed",
}


DISPATCH_DEPS_TRANSITIONS = {
    ("_new", EVENT_OPEN_WAITING): WAITING_STATE,
    (WAITING_STATE, EVENT_DEPS_CLEARED): "open",


    (WAITING_STATE, "note"): WAITING_STATE,
    (WAITING_STATE, "chase"): WAITING_STATE,
    (WAITING_STATE, "close"): "closed",
    ("open", EVENT_BREAKER_FIRED): "open",
    ("acked", EVENT_BREAKER_FIRED): "acked",


    ("open", "auto-chase"): "open",
    ("acked", "auto-chase"): "acked",
    (WAITING_STATE, "auto-chase"): WAITING_STATE,


    ("open", "auto-note"): "open",
    ("acked", "auto-note"): "acked",
    (WAITING_STATE, "auto-note"): WAITING_STATE,
}


DISPATCH_CLAIM_TRANSITIONS = {
    ("open", "claim"): "open",
    ("acked", "claim"): "acked",
}

DISPATCH_TRANSITIONS = {**DISPATCH_LEGACY_TRANSITIONS, **DISPATCH_DEPS_TRANSITIONS,
                        **DISPATCH_CLAIM_TRANSITIONS}


def _self_loops(states: tuple[str, ...], events: tuple[str, ...],
                terminal: tuple[str, ...]) -> dict:
    return {(s, e): s for s in states if s not in terminal for e in events}


PR_STATES = (WAITING_STATE, "authoring", "awaiting-review", "fixing",
             "receipt-due", "merge-pending", "closed")
PR_TRANSITIONS = {
    ("_new", "open"): "authoring",


    ("_new", EVENT_OPEN_WAITING): WAITING_STATE,
    (WAITING_STATE, EVENT_DEPS_CLEARED): "authoring",

    ("authoring", "pr-ready"): "awaiting-review",

    ("awaiting-review", "verdict-blockers"): "fixing",
    ("awaiting-review", "verdict-clean"): "receipt-due",

    ("fixing", "head-moved"): "awaiting-review",

    ("receipt-due", "receipt"): "merge-pending",
    ("merge-pending", "head-moved"): "awaiting-review",


    **{(s, "merged"): "closed" for s in PR_STATES if s != "closed"},
    **_self_loops(PR_STATES, ("note", "chase", "auto-chase", "auto-note",
                              "ack", "review-desync", EVENT_BREAKER_FIRED),
                  ("closed",)),


    **{(s, "claim"): s for s in ("authoring", "fixing", "receipt-due")},
    **{(s, "close"): "closed" for s in PR_STATES if s != "closed"},
}

PARENT_STATES = ("running", "ready-to-close", "closed")
PARENT_TRANSITIONS = {
    ("_new", "open"): "running",


    ("running", "children-closed"): "ready-to-close",

    ("ready-to-close", "child-opened"): "running",

    ("ready-to-close", "close"): "closed",
    **_self_loops(PARENT_STATES, ("note", "chase", "auto-chase", "auto-note",
                                  "ack", EVENT_BREAKER_FIRED), ("closed",)),
}

WORKFLOWS = {
    "dispatch": {
        "transitions": DISPATCH_TRANSITIONS,
        "states": (WAITING_STATE, "open", "acked", "closed"),
        "terminal": ("closed",),
        "initial_event": "open",
        "mechanical": frozenset({EVENT_DEPS_CLEARED, EVENT_BREAKER_FIRED,
                                 "auto-chase", "auto-note"}),
        "grants_permission": frozenset(),


        "owed": {"open": "recipient", "acked": "recipient"},
    },
    "pr": {
        "transitions": PR_TRANSITIONS,
        "states": PR_STATES,
        "terminal": ("closed",),
        "initial_event": "open",
        "mechanical": frozenset({"pr-ready", "head-moved", "merged",
                                 EVENT_DEPS_CLEARED, EVENT_BREAKER_FIRED,
                                 "auto-chase", "auto-note",
                                 "review-desync"}),
        "grants_permission": frozenset(),


        "operator_gated": frozenset({"merge-pending"}),
        "owed": {"authoring": "owner_seat", "fixing": "owner_seat",
                 "receipt-due": "owner_seat", "awaiting-review": "reviewer_seat"},
    },
    "parent": {
        "transitions": PARENT_TRANSITIONS,
        "states": PARENT_STATES,
        "terminal": ("closed",),
        "initial_event": "open",
        "mechanical": frozenset({"children-closed", "child-opened",
                                 EVENT_BREAKER_FIRED, "auto-chase",
                                 "auto-note"}),
        "grants_permission": frozenset(),
        "owed": {},
    },
}


KANBAN_COLUMNS = ("backlog", "todo", "doing", "review", "closed")
KANBAN = {
    ("dispatch", WAITING_STATE): "backlog",
    ("dispatch", "open"): "todo",
    ("dispatch", "acked"): "doing",
    ("dispatch", "closed"): "closed",
    ("pr", WAITING_STATE): "backlog",
    ("pr", "authoring"): "doing",
    ("pr", "awaiting-review"): "review",
    ("pr", "fixing"): "doing",
    ("pr", "receipt-due"): "review",
    ("pr", "merge-pending"): "review",
    ("pr", "closed"): "closed",
    ("parent", "running"): "doing",
    ("parent", "ready-to-close"): "review",
    ("parent", "closed"): "closed",
}


def kanban_column(row: sqlite3.Row) -> str:
    return KANBAN[(row_workflow(row), row["state"])]


TRANSITIONS = DISPATCH_TRANSITIONS
STATES = WORKFLOWS["dispatch"]["states"]
TERMINAL = WORKFLOWS["dispatch"]["terminal"]


MERGE_KEYS = cfg.get("authority.merge_keys", {})
OPERATOR_ROLE = "operator"

RECIPIENT_RE = re.compile(r"tmux(\d+)\b")
SERVICE_HANDLE = cfg.get("authority.service_handle", "fleet-orchestrator")
MAX_SEND_ATTEMPTS = 5
SEND_RETRY_WINDOW_S = 6 * 3600
COMMANDER_ROLE_PURPOSES = frozenset({"checkout-dirty"})
ROLE_GENERATION_PURPOSES = frozenset({
    "dispatch", "author-request", "reassign-notify", "review-request",
    "findings", "receipt-request", "escalation", "claim-notify",
    "goal-review", "receipt-to-keyholder", "review-desync",
    "foreign-review", "checkout-dirty",
})


def bus_cli() -> str:


    return os.environ.get("NW_BUS_CLI") or str(SCRIPT_DIR / "matrix-bus.sh")


MAX_CYCLES = 0
IDLE_WAIT_LIMIT = 6


SEAT_ACTIVITY_WINDOW_S = 3600
IDLE_WAIT_LIMIT_ACTIVE = 24
PANE_ABSENT_LIMIT = 6
ASK_FLAG_TTL_S = 2 * 3600
ASK_NOTE_PREFIX = "blocked-on-authorization (seat verb): "
LEDGER_SPEECH_S = 1800

S_DISPATCHED = "dispatched"
S_WORKING = "working"
S_PULLED = "pulled"
S_AUTHORIZED = "authorized"
S_ESCALATED = "escalated"


S_WAITING = "waiting-external"


def step_drive(entry: dict, busy: bool, idle_wait_limit: int = IDLE_WAIT_LIMIT,
               spoke: bool = False) -> tuple[str | None, dict]:


    st = entry.get("st", S_DISPATCHED)
    cycles = int(entry.get("cycles", 0))
    if busy:
        return None, {"st": S_WORKING, "cycles": cycles}
    if spoke:
        return None, {
            "st": S_PULLED if st in (S_AUTHORIZED, S_ESCALATED) else st,
            "cycles": cycles,
        }
    if st == S_ESCALATED:


        return "pull", entry
    if st == S_DISPATCHED:
        return "pull", {"st": S_PULLED, "cycles": cycles}
    idle_waits = int(entry.get("idle_waits", 0)) + 1
    if idle_waits >= idle_wait_limit:
        return "escalate", {"st": S_ESCALATED, "cycles": cycles}
    next_entry = dict(entry)

    next_entry["st"] = S_PULLED if st == S_AUTHORIZED else st
    next_entry["idle_waits"] = idle_waits
    return None, next_entry


SEAT_VOICE_KINDS = ("ack", "note", "receipt", "verdict-clean",
                    "verdict-blockers", "claim")


WAKE_ATTEMPT_TTL_S = 30 * 60
WAKE_ATTEMPT_MAX_BACKOFF_S = 4 * 3600


def _wake_ttl_s(fails: int) -> int:
    return min(WAKE_ATTEMPT_TTL_S * (2 ** max(0, fails)),
               WAKE_ATTEMPT_MAX_BACKOFF_S)


def wake_attempt_open(conn: sqlite3.Connection, task_id: str, seat: str,
                      purpose: str, generation: str,
                      now_s: int | None = None) -> bool:


    now_s = now() if now_s is None else now_s


    conn.execute(
        "UPDATE wake_attempt SET resolved_ms=?, outcome='superseded'"
        " WHERE task_id=? AND seat=? AND purpose=? AND generation!=?"
        " AND resolved_ms=0",
        (now_s, task_id, seat, purpose, generation))
    cur = conn.execute(
        "INSERT INTO wake_attempt (task_id, seat, purpose, generation, at_ms)"
        " VALUES (?,?,?,?,?)"
        " ON CONFLICT(task_id, seat, purpose, generation) DO UPDATE SET"
        "  at_ms=excluded.at_ms,"
        "  fails=CASE WHEN wake_attempt.resolved_ms>0 THEN 0"
        "             ELSE wake_attempt.fails END,"
        "  resolved_ms=0, outcome=''"
        " WHERE wake_attempt.resolved_ms>0"
        "    OR excluded.at_ms - wake_attempt.at_ms >="
        "       MIN(? * (CASE WHEN wake_attempt.fails<=0 THEN 1"
        "                     WHEN wake_attempt.fails=1 THEN 2"
        "                     WHEN wake_attempt.fails=2 THEN 4"
        "                     ELSE 8 END), ?)",
        (task_id, seat, purpose, generation, now_s,
         WAKE_ATTEMPT_TTL_S, WAKE_ATTEMPT_MAX_BACKOFF_S))
    return cur.rowcount > 0


def wake_attempt_fail(conn: sqlite3.Connection, task_id: str, seat: str,
                      purpose: str, generation: str) -> None:


    conn.execute(
        "UPDATE wake_attempt SET fails=fails+1 WHERE task_id=? AND seat=?"
        " AND purpose=? AND generation=?", (task_id, seat, purpose, generation))


def wake_attempt_resolve(conn: sqlite3.Connection, task_id: str, seat: str,
                         outcome: str) -> None:


    conn.execute(
        "UPDATE wake_attempt SET resolved_ms=?, outcome=? WHERE task_id=?"
        " AND seat=? AND resolved_ms=0", (now(), outcome, task_id, seat))


def seat_active_recently(conn: sqlite3.Connection, task_ids: list[str],
                         window_s: int = SEAT_ACTIVITY_WINDOW_S) -> bool:


    if not task_ids:
        return False
    return any(seat_spoke_recently(conn, task_id, window_s=window_s)
               for task_id in task_ids)


def owed_party(row: sqlite3.Row) -> str:


    owed_col = workflow_spec(row_workflow(row))["owed"].get(row["state"])
    if owed_col and row[owed_col]:
        return row[owed_col]
    return row["recipient"]


TURN_ACTIVE_MAX_S = 2 * 3600


def turn_record(conn: sqlite3.Connection, seat: str, kind: str,
                pane: str = "", harness: str = "") -> None:


    if kind not in ("start", "end"):
        raise ValueError(f"seat_presence kind must be start|end, not {kind!r}")
    conn.execute(
        "INSERT INTO seat_presence (seat, kind, at_ms, pane, harness,"
        " starts, ends) VALUES (?,?,?,?,?,?,?)"
        " ON CONFLICT(seat) DO UPDATE SET kind=excluded.kind,"
        "  at_ms=excluded.at_ms, pane=excluded.pane,"
        "  harness=excluded.harness,"
        "  starts=seat_presence.starts+excluded.starts,"
        "  ends=seat_presence.ends+excluded.ends",
        (seat, kind, now(), pane, harness,
         1 if kind == "start" else 0, 1 if kind == "end" else 0))


def seat_turn_state(conn: sqlite3.Connection, seat: str):


    row = conn.execute("SELECT kind, at_ms FROM seat_presence WHERE seat=?",
                       (seat,)).fetchone()
    if row is None:
        return None, 0
    return row["kind"], row["at_ms"]


def claim_generation(conn: sqlite3.Connection, row: sqlite3.Row) -> str:


    resolved = resolve_owed_recipient(conn, row)
    seat = str(resolved.get("recipient_agent_id")
               or resolved.get("agent_id") or resolved.get("seat") or "")
    source = f"responsibility-v{row['responsibility_version']}"
    role_generation = role_target_generation(
        conn, owed_party(row), row["parent_id"] or "")
    if role_generation:
        source += f":{role_generation}"
    return f"work:{source}:to-{seat}"


def claim_open(conn: sqlite3.Connection, row: sqlite3.Row,
               payload: str = "", *, registry_trusted: bool = True) -> dict:


    step(row_workflow(row), row["state"], "claim")
    gen = claim_generation(conn, row)
    prior = conn.execute("SELECT COALESCE(MAX(round),0) FROM completion_claim"
                         " WHERE task_id=?", (row["id"],)).fetchone()[0]
    conn.execute("UPDATE completion_claim SET status='superseded',"
                 " resolved_ms=?, reason=? WHERE task_id=? AND status='standing'",
                 (now(), f"superseded-by-round-{prior + 1}", row["id"]))
    conn.execute("INSERT INTO completion_claim (task_id, round, claimant,"
                 " generation, claimed_ms, payload) VALUES (?,?,?,?,?,?)",
                 (row["id"], prior + 1, whoami(), gen, now(), payload))
    return {"round": prior + 1, "generation": gen,
            "judge": claim_judge(conn, row,
                                 registry_trusted=registry_trusted)}


def _addressable_attention_candidate(conn: sqlite3.Connection,
                                     candidate: str,
                                     parent_id: str = "", *,
                                     registry_trusted: bool = True) -> str:


    candidate = (candidate or "").strip()
    if not candidate:
        return ""
    if candidate.lower() == "operator":
        return "operator"
    if not registry_trusted:
        return ""
    if candidate.startswith("role:") and role_holder(
            conn, candidate[5:], parent_id) is None:
        return ""
    target = resolve_recipient(conn, candidate, parent_id)
    agent_id = target.get("agent_id")
    if not agent_id:
        return ""
    active = conn.execute(
        "SELECT 1 FROM seat WHERE agent_id=? AND addressable=1"
        " AND lower(status)='active' LIMIT 1", (agent_id,),
    ).fetchone()
    return candidate if active is not None else ""


def _independent_attention_candidate(conn: sqlite3.Connection,
                                     row: sqlite3.Row,
                                     candidate: str, *,
                                     registry_trusted: bool = True) -> str:


    parent_id = row["parent_id"] or ""
    candidate = _addressable_attention_candidate(
        conn, candidate, parent_id, registry_trusted=registry_trusted)
    if not candidate:
        return ""
    current = resolve_owed_recipient(conn, row)
    if current.get("deferred"):


        return ""
    target = resolve_recipient(conn, candidate, parent_id)
    current_id = current.get("agent_id") or current.get("seat")
    target_id = target.get("agent_id") or target.get("seat")
    if current_id and target_id == current_id:
        return ""
    return candidate


def attention_recipient(conn: sqlite3.Connection, row: sqlite3.Row, *,
                        registry_trusted: bool = True) -> str:


    parent_id = (row["parent_id"] or "").strip()
    if parent_id:
        parent = conn.execute(
            "SELECT * FROM dispatch WHERE id=?", (parent_id,),
        ).fetchone()
        if parent is not None:
            candidate = _independent_attention_candidate(
                conn, row, parent["recipient"],
                registry_trusted=registry_trusted,
            )
            if candidate:
                return candidate
    candidate = _independent_attention_candidate(
        conn, row, row["requester_seat"] or "",
        registry_trusted=registry_trusted,
    )
    if candidate:
        return candidate
    if role_holder(conn, "commander", parent_id) is not None:
        candidate = _independent_attention_candidate(
            conn, row, "role:commander",
            registry_trusted=registry_trusted,
        )
        if candidate:
            return candidate
    return "operator"


def goal_review_recipient(conn: sqlite3.Connection, row: sqlite3.Row, *,
                          registry_trusted: bool = True) -> str:

    for candidate in (row["recipient"] or "", row["requester_seat"] or "",
                      "role:commander"):
        target = _addressable_attention_candidate(
            conn, candidate, row["id"],
            registry_trusted=registry_trusted,
        )
        if target:
            return target
    return "operator"


def claim_judge(conn: sqlite3.Connection, row: sqlite3.Row, *,
                registry_trusted: bool = True) -> str:

    if row_workflow(row) == "pr" and row["reviewer_seat"]:
        reviewer = _independent_attention_candidate(
            conn, row, row["reviewer_seat"],
            registry_trusted=registry_trusted,
        )
        if reviewer:
            return reviewer
    return attention_recipient(
        conn, row, registry_trusted=registry_trusted)


def record_operator_queue_marker(conn: sqlite3.Connection, task_id: str,
                                 purpose: str, dedup_key: str,
                                 subject: str, body: str, *,
                                 registry_trusted: bool = True,
                                 expected_latest_id: int | None = None,
                                 expected_responsibility_version: int | None = None
                                 ) -> int | None:


    if expected_latest_id is None or expected_responsibility_version is None:
        raise ValueError(
            "operator action markers require the observed message and"
            " responsibility generations")
    conn.execute(
        "UPDATE dispatch SET last_event=last_event WHERE id=?", (task_id,))
    task = conn.execute(
        "SELECT * FROM dispatch WHERE id=?", (task_id,),
    ).fetchone()
    if (task is not None and expected_responsibility_version is not None
            and int(task["responsibility_version"]) !=
            int(expected_responsibility_version)):
        return None
    if task is None or not operator_action_is_current(
            conn, task, purpose, registry_trusted=registry_trusted):
        return None
    latest = conn.execute(
        "SELECT * FROM task_msg WHERE task_id=? AND purpose=?"
        " ORDER BY id DESC LIMIT 1", (task_id, purpose),
    ).fetchone()
    latest_id = int(latest["id"]) if latest is not None else 0
    if (expected_latest_id is not None
            and latest_id != int(expected_latest_id)):
        return None
    if (latest is not None and latest["target"] != "operator"
            and latest["send_state"] == "accepted"
            and message_is_current_responsibility(conn, latest, task)):
        return None


    parts = dedup_key.strip(":").split(":")
    base_parts: list[str] = []
    index = 0
    while index < len(parts):
        if (parts[index] == "after" and index + 1 < len(parts)
                and parts[index + 1].isdigit()):
            index += 2
            continue
        base_parts.append(parts[index])
        index += 1
    dedup_key = f"{':'.join(base_parts)}:after:{latest_id}"
    marker = record_msg(
        conn, task_id, purpose, dedup_key, "operator", subject, body,
        expected_latest_id=latest_id,
        expected_responsibility_version=expected_responsibility_version)
    if marker is not None:
        conn.execute(
            "UPDATE task_msg SET send_state='operator-queue',"
            " processed='operator-queue' WHERE id=?", (marker,),
        )
        stored = conn.execute(
            "SELECT * FROM task_msg WHERE id=?", (marker,),
        ).fetchone()
        if not operator_marker_shape_is_current(conn, task, stored):
            conn.execute("DELETE FROM task_msg WHERE id=?", (marker,))
            return None
    return marker


def claim_commit(conn: sqlite3.Connection, row: sqlite3.Row,
                 payload: str = "", *, registry_trusted: bool = True,
                 claimant: str = "") -> dict:


    with conn:
        if claimant:
            row, work_context = lock_continuation_caller(
                conn, row, claimant, "claim-done", work_only=True)
        else:


            locked = conn.execute(
                "UPDATE dispatch SET last_event=last_event WHERE id=?"
                " AND responsibility_version=? AND state=?",
                (row["id"], row["responsibility_version"], row["state"]),
            )
            if locked.rowcount != 1:
                raise SystemExit(
                    "FAIL  responsibility changed while the completion claim"
                    " was being recorded; inspect the task and claim again"
                    " only if it is still yours")
            row = fetch(conn, row["id"])
            work_context = continuation_context(conn, row)
        conn.execute("UPDATE dispatch SET last_event=? WHERE id=?",
                     (now(), row["id"]))
        claim = claim_open(
            conn, row, payload, registry_trusted=registry_trusted)


        claim["event_id"] = record(
            conn, row["id"], "claim", payload,
            actor=claimant,
            continuation_generation=(work_context["generation"]
                                     if work_context else ""),
        )
        conn.execute("UPDATE completion_claim SET event_id=? WHERE task_id=?"
                     " AND round=?",
                     (claim["event_id"], row["id"], claim["round"]))

        wake_attempt_resolve(
            conn, row["id"], resolve_owed_recipient(conn, row)["seat"],
            "claim-standing")
        claim["subject"] = (f"completion claim r{claim['round']}:"
                            f" {row['id']} {row['subject']}")
        claim["body"] = (
            f"Task {row['id']} round {claim['round']}: {whoami()} claims done"
            f" at generation {claim['generation']}.\n\n{payload}\n\n"
            f"Judge it: `orc show {row['id']}`; accept with"
            f" `orc close {row['id']} --resolution done`, or return the work"
            f" with `orc chase {row['id']}` / a blockers verdict.")
        claim["msg_row"] = None
        if claim["judge"] != "operator":
            expected_notice_id = latest_message_id(
                conn, row["id"], "claim-notify")
            claim["msg_row"] = record_msg(
                conn, row["id"], "claim-notify",
                f"claim:{row['id']}:{claim['round']}", claim["judge"],
                claim["subject"], claim["body"],
                expected_latest_id=expected_notice_id,
                expected_responsibility_version=row["responsibility_version"],
            )
        elif not registry_trusted:
            latest_notice = conn.execute(
                "SELECT id FROM task_msg WHERE task_id=?"
                " AND purpose='claim-notify' ORDER BY id DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            record_operator_queue_marker(
                conn, row["id"], "claim-notify",
                f"claim:{row['id']}:{claim['round']}:judge:operator:unverified",
                claim["subject"], claim["body"],
                registry_trusted=registry_trusted,
                expected_latest_id=(latest_notice["id"]
                                    if latest_notice else 0),
                expected_responsibility_version=row["responsibility_version"],
            )
    return claim


def claim_standing(conn: sqlite3.Connection, row: sqlite3.Row,
                   repair: bool = True):


    claim = conn.execute(
        "SELECT * FROM completion_claim WHERE task_id=? AND status='standing'"
        " ORDER BY round DESC LIMIT 1", (row["id"],)).fetchone()
    if claim is None:
        return None
    status, reason = _claim_fate(conn, row, claim)
    if status == "standing":
        if repair and reason.startswith("migrate-generation:"):
            conn.execute(
                "UPDATE completion_claim SET generation=?"
                " WHERE task_id=? AND round=? AND status='standing'",
                (reason.removeprefix("migrate-generation:"), row["id"],
                 claim["round"]),
            )
            claim = conn.execute(
                "SELECT * FROM completion_claim WHERE task_id=? AND round=?",
                (row["id"], claim["round"]),
            ).fetchone()
        return claim
    if repair:
        conn.execute("UPDATE completion_claim SET status=?, resolved_ms=?,"
                     " reason=? WHERE task_id=? AND round=?",
                     (status, now(), reason, row["id"], claim["round"]))
    return None


def repair_standing_claim_notifications(conn: sqlite3.Connection,
                                        log=print, *,
                                        registry_trusted: bool = True,
                                        route_observation_id: int | None = None
                                        ) -> int:


    repaired = 0
    tasks = conn.execute(
        "SELECT * FROM dispatch WHERE state!='closed' ORDER BY created_ms"
    ).fetchall()
    for task in tasks:
        with conn:
            claim = claim_standing(conn, task)
        if claim is None:
            continue
        base = f"claim:{task['id']}:{claim['round']}"
        rows = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=? AND purpose='claim-notify'"
            " AND (dedup_key=? OR dedup_key LIKE ?) ORDER BY id",
            (task["id"], base, f"{base}:%"),
        ).fetchall()
        prior = rows[-1] if rows else None
        subject = ((prior["subject"] if prior else "") or
                   f"completion claim r{claim['round']}:"
                   f" {task['id']} {task['subject']}")
        body = ((prior["body"] if prior else "") or
                f"Task {task['id']} has a standing completion claim round"
                f" {claim['round']}. Inspect it with `orc show {task['id']}`"
                " before judging.")
        judge = claim_judge(
            conn, task, registry_trusted=registry_trusted)
        if judge == "operator":
            if ((prior is not None and prior["target"] != "operator")
                    or (not registry_trusted
                        and operator_queue_marker(
                            conn, task, "claim-notify") is None)):
                expected_notice_id = latest_message_id(
                    conn, task["id"], "claim-notify",
                    at_or_before=route_observation_id)
                with conn:
                    marker = record_operator_queue_marker(
                        conn, task["id"], "claim-notify",
                        f"{base}:judge:operator",
                        subject, body,
                        registry_trusted=registry_trusted,
                        expected_latest_id=expected_notice_id,
                        expected_responsibility_version=
                        task["responsibility_version"])
                if marker is not None:
                    log(f"OK moved completion-claim review for {task['id']}"
                        " to the operator's original-task list")
                    repaired += 1
            continue
        if (rows and message_is_current_responsibility(
                conn, rows[-1], task)):
            continue
        with conn:
            expected_notice_id = latest_message_id(
                conn, task["id"], "claim-notify",
                at_or_before=route_observation_id)
            row_id = record_msg(
                conn, task["id"], "claim-notify",
                f"{base}:judge:{len(rows) + 1}", judge, subject, body,
                expected_latest_id=expected_notice_id,
                expected_responsibility_version=
                task["responsibility_version"],
            )
        if row_id is not None:
            log(f"OK restored completion-claim notice for {task['id']}"
                f" round {claim['round']} -> {judge}")
            repaired += 1
    return repaired


def agent_bus_db_path() -> Path:

    explicit = os.environ.get("AGENT_BUS_DB", "").strip()
    return Path(explicit).expanduser() if explicit else cfg.path("bus.database", CFG / "agent-bus-v3.sqlite3")


def _agent_bus_rows(sql: str, params: tuple = ()) -> list[tuple] | None:


    path = agent_bus_db_path()
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=0.25)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()
    except (OSError, sqlite3.Error):
        return None


def agent_bus_identity_active(agent_id: str) -> bool | None:


    if not agent_id.strip():
        return False
    rows = _agent_bus_rows(
        "SELECT 1 FROM identities WHERE agent_id=? AND status='active'"
        " AND lease_until_ms>=? LIMIT 1",
        (agent_id.strip(), int(time.time() * 1000)))
    return None if rows is None else bool(rows)


def agent_bus_identity_addressable(agent_id: str) -> bool | None:


    if not agent_id.strip():
        return False
    rows = _agent_bus_rows(
        "SELECT 1 FROM identities WHERE agent_id=? AND status='active'"
        " AND lease_until_ms>=? AND lower(harness)!='cron' LIMIT 1",
        (agent_id.strip(), int(time.time() * 1000)))
    return None if rows is None else bool(rows)


def caller_seat_id(pane: str | None = None) -> str:


    explicit = os.environ.get("ORC_SEAT_ID", "").strip()
    if explicit:
        return explicit
    raw_pane = os.environ.get("TMUX_PANE", "") if pane is None else pane
    pane_id = raw_pane.strip()
    if not pane_id:
        return ""
    pane_id = f"%{pane_id.lstrip('%')}"
    full_host = socket.gethostname()
    hosts = sorted({full_host, full_host.split(".", 1)[0]})
    placeholders = ",".join("?" for _ in hosts)
    rows = _agent_bus_rows(
        f"SELECT agent_id FROM identities WHERE status='active'"
        f" AND lease_until_ms>=? AND host IN ({placeholders})"
        " AND pane_id=? ORDER BY agent_id LIMIT 2",
        (int(time.time() * 1000), *hosts, pane_id))
    if rows is None or len(rows) != 1:
        return ""
    return str(rows[0][0]).strip()


def _require_continuation_actor(conn: sqlite3.Connection, row: sqlite3.Row,
                                verb: str, caller: str, *,
                                work_only: bool = False) -> dict:

    context = continuation_context(conn, row)
    if context is None:
        raise SystemExit(
            f"FAIL  nobody currently owes an action on {row['id']} while it"
            f" is {row['state']}; {verb} cannot manufacture activity")
    if work_only and context["kind"] != "work":
        raise SystemExit(
            f"FAIL  {verb} belongs to the task worker, but {row['id']} now"
            f" waits for someone to {context['label']}")
    owed = str(context.get("agent_id") or "")
    if not caller:
        raise SystemExit(
            f"FAIL  {verb} is a seat verb and needs your stable identity:"
            " join this pane to the Agent Bus or export"
            " ORC_SEAT_ID=<your agent id>")
    if context.get("deferred"):
        raise SystemExit(
            f"FAIL  {verb} cannot verify who currently owes {row['id']}:"
            f" {context['deferred']}. Restore its current delivery record"
            " before recording worker progress")
    if (not owed and context["kind"] == "work"
            and not str(context.get("requested") or "").startswith("role:")):


        owed = str(context.get("requested") or "")
        if caller == owed:
            context = {**context, "seat": owed, "agent_id": owed,
                       "recipient_agent_id": owed}
    if not owed:
        raise SystemExit(
            f"FAIL  {verb} cannot verify the stable Agent Bus identity that"
            f" currently owes {row['id']}; restore its current delivery or"
            " registry record before recording worker progress")
    if caller != owed:
        raise SystemExit(
            f"FAIL  {caller} does not owe {row['id']} - the current owed seat"
            f" is {owed}. A foreign {verb} would hide that seat's silence")
    return context


def require_continuation_caller(conn: sqlite3.Connection, row: sqlite3.Row,
                                verb: str) -> tuple[str, dict]:

    caller = caller_seat_id()
    return caller, _require_continuation_actor(
        conn, row, verb, caller, work_only=False)


def require_owed_caller(conn: sqlite3.Connection, row: sqlite3.Row,
                        verb: str) -> str:

    caller = caller_seat_id()
    _require_continuation_actor(conn, row, verb, caller, work_only=True)
    return caller


def lock_continuation_caller(conn: sqlite3.Connection, row: sqlite3.Row,
                             caller: str, verb: str, *,
                             work_only: bool = False) -> tuple[sqlite3.Row, dict]:


    locked = conn.execute(
        "UPDATE dispatch SET last_event=last_event WHERE id=?"
        " AND responsibility_version=?",
        (row["id"], row["responsibility_version"]),
    )
    if locked.rowcount != 1:
        raise SystemExit(
            f"FAIL  responsibility changed while {verb} was being recorded;"
            " inspect the task and try again only if the action is still yours")
    current = fetch(conn, row["id"])
    context = _require_continuation_actor(
        conn, current, verb, caller, work_only=work_only)
    return current, context


def _env_int(name: str, default: int) -> int:


    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def wake_start_timeout_s() -> int:
    return _env_int("NW_WAKE_START_TIMEOUT_S", 120)


def wake_process_timeout_s() -> int:
    return _env_int("NW_WAKE_PROCESS_TIMEOUT_S", 1800)


def wake_event_retention_s() -> int:
    return _env_int("NW_WAKE_EVENT_RETENTION_S", 7 * 24 * 3600)


WAKE_TERMINAL_STATES = ("turn-started", "superseded",
                        "exit:start-timeout", "exit:process-timeout",
                        "exit:held-focus", "exit:enter-unconfirmed",
                        "exit:unknown-error", "exit:sent-but-held")

WAKE_RELEASE_STATES = ("superseded", "exit:start-timeout",
                       "exit:process-timeout", "exit:held-focus",
                       "exit:enter-unconfirmed", "exit:unknown-error",
                       "exit:sent-but-held")


from send_outcome import SendOutcome
def _wake_cause_pool(seat: str) -> str:


    return f"pool:{seat}"


def wake_shadow_off() -> bool:


    return os.environ.get("NW_WAKE_LEASE_OFF") == "1"


def wake_event(conn: sqlite3.Connection, seat: str, wake_id: str | None,
               kind: str, detail: str = "") -> None:
    if wake_shadow_off():
        return
    conn.execute("INSERT INTO wake_event (at_s, seat, wake_id, kind, detail)"
                 " VALUES (?,?,?,?,?)", (now(), seat, wake_id, kind, detail))


def _wake_lease_read(conn: sqlite3.Connection, seat: str, generation: int):


    return conn.execute(
        "SELECT wake_id, state FROM wake_lease WHERE seat=? AND generation=?",
        (seat, generation)).fetchone()


def wake_lease_acquire(conn: sqlite3.Connection, seat: str, generation: int,
                       holder: str, causes=()) -> tuple[str | None, bool]:


    if wake_shadow_off():
        return None, True
    existing = _wake_lease_read(conn, seat, generation)
    if existing:
        if existing["state"] in WAKE_TERMINAL_STATES:


            for task_id, purpose in causes:
                conn.execute(
                    "INSERT OR IGNORE INTO wake_cause (wake_id, task_id,"
                    " purpose) VALUES (?,?,?)",
                    (_wake_cause_pool(seat), task_id, purpose))
            wake_event(conn, seat, existing["wake_id"], "would-have-deduped",
                       f"{holder}: generation {generation} already concluded"
                       f" ({existing['state']}); causes pooled")
            return None, False
        for task_id, purpose in causes:
            conn.execute("INSERT OR IGNORE INTO wake_cause (wake_id, task_id,"
                         " purpose) VALUES (?,?,?)",
                         (existing["wake_id"], task_id, purpose))
        wake_event(conn, seat, existing["wake_id"], "would-have-deduped",
                   f"{holder}: open wake already held")
        return existing["wake_id"], False
    wake_id = uuid.uuid4().hex
    try:
        conn.execute(
            "INSERT INTO wake_lease (seat, generation, wake_id, state,"
            " holder, opened_s, updated_s) VALUES (?,?,?,'leased',?,?,?)",
            (seat, generation, wake_id, holder, now(), now()))
    except sqlite3.IntegrityError:


        row = conn.execute(
            "SELECT wake_id FROM wake_lease WHERE seat=? AND generation=?",
            (seat, generation)).fetchone()
        for task_id, purpose in causes:
            conn.execute("INSERT OR IGNORE INTO wake_cause (wake_id, task_id,"
                         " purpose) VALUES (?,?,?)",
                         (row["wake_id"], task_id, purpose))
        wake_event(conn, seat, row["wake_id"], "would-have-deduped",
                   f"{holder}: lost the acquire race")
        return row["wake_id"], False
    for task_id, purpose in causes:
        conn.execute("INSERT OR IGNORE INTO wake_cause (wake_id, task_id,"
                     " purpose) VALUES (?,?,?)", (wake_id, task_id, purpose))


    conn.execute(
        "INSERT OR IGNORE INTO wake_cause (wake_id, task_id, purpose)"
        " SELECT ?, c.task_id, c.purpose FROM wake_cause c"
        " WHERE c.wake_id = ? OR c.wake_id IN"
        "  (SELECT wake_id FROM wake_lease WHERE seat=? AND released_s IS"
        "   NOT NULL AND wake_id != ?)",
        (wake_id, _wake_cause_pool(seat), seat, wake_id))
    conn.execute("DELETE FROM wake_cause WHERE wake_id=?",
                 (_wake_cause_pool(seat),))
    wake_event(conn, seat, wake_id, "leased", holder)
    return wake_id, True


def wake_lease_release(conn: sqlite3.Connection, seat: str, generation: int,
                       state: str) -> bool:

    if wake_shadow_off():
        return False
    if state not in WAKE_RELEASE_STATES:
        raise ValueError(f"wake release must be typed, not {state!r}")
    cur = conn.execute(
        "UPDATE wake_lease SET state=?, updated_s=?, released_s=? WHERE"
        " seat=? AND generation=? AND released_s IS NULL",
        (state, now(), now(), seat, generation))
    if cur.rowcount:
        row = conn.execute("SELECT wake_id FROM wake_lease WHERE seat=? AND"
                           " generation=?", (seat, generation)).fetchone()
        wake_event(conn, seat, row["wake_id"], state)
    return bool(cur.rowcount)


def wake_lease_supersede(conn: sqlite3.Connection, seat: str,
                         new_generation: int) -> int:


    if wake_shadow_off():
        return 0
    released = 0
    for row in conn.execute(
            "SELECT generation FROM wake_lease WHERE seat=? AND"
            " generation<? AND released_s IS NULL", (seat, new_generation)):
        if wake_lease_release(conn, seat, row["generation"], "superseded"):
            released += 1
    return released


def wake_lease_turn_started(conn: sqlite3.Connection, seat: str,
                            generation: int) -> bool:


    if wake_shadow_off():
        return False
    cur = conn.execute(
        "UPDATE wake_lease SET state='turn-started', updated_s=? WHERE"
        " seat=? AND generation=? AND released_s IS NULL",
        (now(), seat, generation))
    if cur.rowcount:
        row = conn.execute("SELECT wake_id FROM wake_lease WHERE seat=? AND"
                           " generation=?", (seat, generation)).fetchone()
        wake_event(conn, seat, row["wake_id"], "turn-started")
    return bool(cur.rowcount)


def wake_sweep(conn: sqlite3.Connection) -> int:


    if wake_shadow_off():
        return 0
    swept = 0
    for row in conn.execute(
            "SELECT seat, generation, wake_id FROM wake_lease WHERE"
            " released_s IS NULL AND state='leased' AND opened_s < ?",
            (now() - wake_start_timeout_s(),)):


        pasted = conn.execute(
            "SELECT 1 FROM wake_event WHERE wake_id=? AND"
            " kind='pasted' LIMIT 1",
            (row["wake_id"],)).fetchone()
        exit_kind = ("exit:enter-unconfirmed" if pasted
                     else "exit:start-timeout")
        if wake_lease_release(conn, row["seat"], row["generation"],
                              exit_kind):
            swept += 1
    for row in conn.execute(
            "SELECT seat, generation FROM wake_lease WHERE released_s IS"
            " NULL AND state='turn-started' AND updated_s < ?",
            (now() - wake_process_timeout_s(),)):
        if wake_lease_release(conn, row["seat"], row["generation"],
                              "exit:process-timeout"):
            swept += 1
    conn.execute("DELETE FROM wake_event WHERE at_s < ?",
                 (now() - wake_event_retention_s(),))
    return swept


def _wake_pane_lock_path(pane: str):


    import runtime_paths as nw_paths
    try:
        import tmux_runtime
        server, _src = tmux_runtime.configured_server()
    except Exception:
        server = None
    key = f"{server or 'default'}-{pane}".replace("/", "-").replace("%", "")
    d = nw_paths.runtime_root() / "wake-locks"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"pane-{key}.lock"


def wake_contact(conn: sqlite3.Connection, seat: str, pane: str,
                 holder: str, causes, send):


    if not re.fullmatch(r"%(0|[1-9][0-9]*)", str(pane)):


        raise ValueError(
            f"wake_contact takes a canonical pane id (%N, no leading"
            f" zeros), got {pane!r} - resolve the window once at the call"
            f" site")
    shadow_off = wake_shadow_off()
    generation = None
    wid = None
    if not shadow_off:
        try:
            generation = bus_inbox_generation(seat)
            with conn:
                if generation is None:
                    wake_event(conn, seat, None, "generation-unknown", holder)
                else:
                    wake_lease_supersede(conn, seat, generation)
                    wid, _won = wake_lease_acquire(conn, seat, generation,
                                                   holder, causes)
        except sqlite3.Error:
            pass

    def _shadow(kind, detail=""):
        if shadow_off:
            return
        try:
            with conn:
                wake_event(conn, seat, wid, kind, detail)
        except sqlite3.Error:
            pass

    def _release(state):
        if generation is None:
            return
        try:
            with conn:
                wake_lease_release(conn, seat, generation, state)
        except sqlite3.Error:
            pass

    def _progress(kind, detail=""):


        if kind in ("pasted", "entered"):
            _shadow(kind, detail)

    import fcntl
    with open(_wake_pane_lock_path(pane), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            outcome, detail = send(_progress)
        except BaseException:


            _release("exit:unknown-error")
            raise
        if outcome not in SendOutcome.ALL:
            _release("exit:unknown-error")
            raise ValueError(
                f"send callback returned {outcome!r}, not a member of the"
                f" closed sender contract - the wrapper consumes TYPES")
        if outcome == SendOutcome.CONTACTED:
            _shadow("contact", f"{holder} -> {pane}")
        elif outcome == SendOutcome.SENT_BUT_HELD:


            _release("exit:sent-but-held")
            _shadow("contact-noop", f"{holder} -> {pane} ({outcome})")
        elif outcome == SendOutcome.HELD_FOCUS:
            _release("exit:held-focus")
            _shadow("contact-noop", f"{holder} -> {pane} ({outcome})")
        elif outcome == SendOutcome.ENTER_UNCONFIRMED:
            _release("exit:enter-unconfirmed")
            _shadow("contact-noop", f"{holder} -> {pane} ({outcome})")
        else:


            _shadow("contact-noop", f"{holder} -> {pane} ({outcome})")
        return outcome, detail


def wake_cause_ride(conn: sqlite3.Connection, seat: str, task_id: str,
                    purpose: str) -> bool:


    if wake_shadow_off():
        return False
    row = conn.execute(
        "SELECT wake_id FROM wake_lease WHERE seat=? AND released_s IS NULL"
        " ORDER BY opened_s DESC LIMIT 1", (seat,)).fetchone()
    if not row:


        conn.execute("INSERT OR IGNORE INTO wake_cause (wake_id, task_id,"
                     " purpose) VALUES (?,?,?)",
                     (_wake_cause_pool(seat), task_id, purpose))
        wake_event(conn, seat, None, "notifier-no-open-wake",
                   f"{task_id}:{purpose} (pooled for the next acquire)")
        return False
    conn.execute("INSERT OR IGNORE INTO wake_cause (wake_id, task_id,"
                 " purpose) VALUES (?,?,?)", (row["wake_id"], task_id, purpose))
    wake_event(conn, seat, row["wake_id"], "cause-rode",
               f"{task_id}:{purpose}")
    return True


def bus_inbox_generation(agent_id: str) -> int | None:


    rows = _agent_bus_rows(
        "SELECT generation FROM inbox_signal WHERE agent_id=?", (agent_id,))
    if not rows:
        return None
    try:
        return int(rows[0][0])
    except (TypeError, ValueError):
        return None


def review_intent_open(conn: sqlite3.Connection, task_id: str, seat: str,
                       scope: str = "") -> int:


    existing = conn.execute(
        "SELECT id FROM review_intent WHERE task_id=? AND seat=?"
        " AND closed_s IS NULL", (task_id, seat)).fetchone()
    if existing:
        if scope:
            conn.execute("UPDATE review_intent SET scope=? WHERE id=?",
                         (scope, existing["id"]))
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO review_intent (task_id, seat, scope, started_s)"
        " VALUES (?,?,?,?)", (task_id, seat, scope, now()))
    return cur.lastrowid


def review_intent_close(conn: sqlite3.Connection, task_id: str,
                        seat: str) -> int:

    return conn.execute(
        "UPDATE review_intent SET closed_s=? WHERE task_id=? AND seat=?"
        " AND closed_s IS NULL", (now(), task_id, seat)).rowcount


def open_review_intents(conn: sqlite3.Connection,
                        task_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM review_intent WHERE task_id=? AND closed_s IS NULL"
        " ORDER BY started_s", (task_id,)).fetchall()


def review_intent_pass(conn: sqlite3.Connection, task_id: str,
                       actor_seat: str) -> tuple[int, list[str]]:


    closed = review_intent_close(conn, task_id, actor_seat) if actor_seat else 0
    warnings = []
    for intent in open_review_intents(conn, task_id):
        age = human_age(now() - intent["started_s"])
        scope = f" (scope: {intent['scope']})" if intent["scope"] else ""
        warnings.append(
            f"WARN  {intent['seat']} has an OPEN review-intent on this task"
            f" since {age} ago{scope} - this post may cross an in-flight"
            f" independent review; later evidence wins")
    return closed, warnings


def terminal_notify(conn: sqlite3.Connection, row: sqlite3.Row,
                    resolution: str, closer: str, via: str = "close"):


    try:
        awaited = row["await_notify"]
        requester = (row["requester_seat"] or "").strip()
    except (IndexError, KeyError):
        return None
    if not awaited or not requester or requester == closer:
        return None
    subject = f"terminal: {row['id']} {resolution or via}: {row['subject']}"
    body = (f"Task {row['id']} you awaited reached its terminal state via"
            f" {via}: resolution '{resolution or 'none'}'.\n"
            f"Full trail: orc show {row['id']}")
    row_id = record_msg(
        conn, row["id"], "terminal", f"terminal:{row['id']}", requester,
        subject, body,
        expected_responsibility_version=row["responsibility_version"])
    if row_id and agent_bus_identity_addressable(requester) is False:
        refuse_recorded_target(
            conn, row_id,
            "terminal requester is not an active, unexpired, addressable"
            " Agent Bus identity in the local database; transport skipped",
        )
    return row_id


def expire_task_msgs(conn: sqlite3.Connection, task_id: str) -> int:


    rows = conn.execute(
        "SELECT msg_id FROM task_msg WHERE task_id=? AND msg_id!=''"
        " AND send_state='accepted' AND processed=''", (task_id,)).fetchall()
    bus = Path(__file__).resolve().parent.parent / "agent-bus-v3.py"
    cli = os.environ.get("NW_BUS_CLI", "")
    attempted = 0
    for r in rows:
        cmd = (["bash", cli] if cli else [sys.executable, str(bus)])
        cmd += ["expire", "--msg", r["msg_id"], "--reason",
                f"task {task_id} closed"]
        try:
            subprocess.run(cmd, text=True, capture_output=True, timeout=15)
            attempted += 1
        except (OSError, subprocess.TimeoutExpired):
            continue
    return attempted


def claim_settle_terminal(conn: sqlite3.Connection, task_id: str,
                          resolution: str, via: str = "close") -> None:


    status = "accepted" if (resolution == "done" or via == "merged") else "consumed"
    conn.execute("UPDATE completion_claim SET status=?, resolved_ms=?, reason=?"
                 " WHERE task_id=? AND status='standing'",
                 (status, now(), f"{via}:{resolution or 'no-resolution'}",
                  task_id))


def claim_sweep_terminal(conn: sqlite3.Connection) -> int:


    cur = conn.execute(
        "UPDATE completion_claim SET"
        "  status = CASE WHEN (SELECT resolution FROM dispatch d"
        "                      WHERE d.id=task_id)='done'"
        "                THEN 'accepted' ELSE 'consumed' END,"
        "  resolved_ms=?,"
        "  reason='close:'||COALESCE(NULLIF((SELECT resolution FROM dispatch d"
        "                                    WHERE d.id=task_id),''),"
        "                            'no-resolution')"
        " WHERE status='standing' AND (SELECT state FROM dispatch d"
        "                              WHERE d.id=task_id)='closed'", (now(),))
    return cur.rowcount


def _claim_fate(conn: sqlite3.Connection, row: sqlite3.Row,
                claim: sqlite3.Row) -> tuple[str, str]:
    if is_closed(row):
        res = row["resolution"] or "no-resolution"
        return (("accepted", f"close:{res}") if res == "done"
                else ("consumed", f"close:{res}"))


    returned = conn.execute(
        "SELECT kind FROM event WHERE dispatch_id=? AND at_ms>=? AND kind IN"
        " ('chase','verdict-blockers') ORDER BY at_ms LIMIT 1",
        (row["id"], claim["claimed_ms"])).fetchone()
    if returned:
        return "rejected", f"work-returned:{returned['kind']}"
    resolved = resolve_owed_recipient(conn, row)
    if resolved.get("deferred"):
        if int(row["responsibility_version"]) == 0:


            return "standing", ""
        return ("invalidated",
                f"generation-moved:{row['state']}:{owed_party(row)}")
    gen = claim_generation(conn, row)
    stored = claim["generation"]
    if stored == gen:
        return "standing", ""
    seat = str(resolved.get("recipient_agent_id")
               or resolved.get("agent_id") or resolved.get("seat") or "")
    legacy = {f"{row['state']}:{seat}", f"v0:{row['state']}:{seat}"}
    if (int(row["responsibility_version"]) == 0
            and int(claim["event_id"] or 0) > 0
            and stored in legacy):


        delivered = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=? AND target=?"
            " AND recipient_version=0 AND send_state='accepted'"
            " AND purpose IN (%s) ORDER BY id DESC LIMIT 1"
            % ",".join("?" for _ in RESPONSIBILITY_PURPOSES),
            (row["id"], owed_party(row), *RESPONSIBILITY_PURPOSES),
        ).fetchone()
        actual = (message_recipient_agent_id(
            delivered["msg_id"], delivered["recipient_agent_id"],
            delivered["target"])
            if delivered is not None else "")
        moved_message = conn.execute(
            "SELECT 1 FROM task_msg WHERE task_id=? AND at_ms>=?"
            " AND purpose IN (%s) LIMIT 1"
            % ",".join("?" for _ in RESPONSIBILITY_PURPOSES),
            (row["id"], claim["claimed_ms"], *RESPONSIBILITY_PURPOSES),
        ).fetchone()
        reassigned = conn.execute(
            "SELECT 1 FROM event WHERE dispatch_id=? AND id>?"
            " AND kind IN ('note','auto-note')"
            " AND note LIKE 'reassigned:%' LIMIT 1",
            (row["id"], claim["event_id"]),
        ).fetchone()
        if (delivered is not None
                and int(delivered["at_ms"]) < int(claim["claimed_ms"])
                and actual == seat
                and moved_message is None and reassigned is None):
            return "standing", f"migrate-generation:{gen}"
    return "invalidated", f"generation-moved:{gen}"


def current_continuation_voice(conn: sqlite3.Connection,
                               row: sqlite3.Row,
                               context: dict | None = None, *,
                               after_event_id: int = 0,
                               window_s: int | None = None,
                               kinds: tuple[str, ...] = SEAT_VOICE_KINDS,
                               at_or_after: int = 0) -> sqlite3.Row | None:


    context = context or continuation_context(conn, row)
    if context is None or context.get("deferred"):
        return None
    actor = str(context.get("agent_id") or "")
    if not actor or not kinds:
        return None


    if (not int(context.get("message_id") or 0)
            and context.get("requested") != actor):
        return None
    sql = (
        "SELECT id,at_ms,kind FROM event WHERE dispatch_id=?"
        " AND responsibility_version=? AND actor=?"
        " AND continuation_generation=? AND id>?"
        " AND kind IN (%s)" % ",".join("?" for _ in kinds)
    )
    params: list = [row["id"], row["responsibility_version"], actor,
                    context["generation"], after_event_id, *kinds]
    if at_or_after:
        sql += " AND at_ms>=?"
        params.append(at_or_after)
    if window_s is not None:
        sql += " AND at_ms>?"
        params.append(now() - window_s)
    sql += " ORDER BY id DESC LIMIT 1"
    return conn.execute(sql, params).fetchone()


def continuation_spoke_recently(conn: sqlite3.Connection, task_id: str,
                                context: dict | None = None,
                                window_s: int = LEDGER_SPEECH_S) -> bool:
    row = fetch(conn, task_id)
    return current_continuation_voice(
        conn, row, context, window_s=window_s) is not None


def seat_spoke_recently(conn: sqlite3.Connection, task_id: str,
                        window_s: int = LEDGER_SPEECH_S) -> bool:

    return continuation_spoke_recently(conn, task_id, window_s=window_s)


def now() -> int:
    return int(time.time())


def parse_after(text: str) -> int:

    match = re.fullmatch(r"(\d+)([smhd]?)", text.strip())
    if not match:
        raise ValueError(f"not a duration: {text!r} (use 45m, 2h, 1d)")
    value = int(match.group(1))
    return value * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]


def human_age(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60}m"
    if seconds < 172800:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def ask_is_fresh(row: sqlite3.Row, at_s: int | None = None) -> bool:

    stamp = int(row["ask_flag"] or 0)
    return bool(stamp) and (now() if at_s is None else at_s) - stamp \
        <= ASK_FLAG_TTL_S


def current_ask_event(conn: sqlite3.Connection, row: sqlite3.Row,
                      at_s: int | None = None) -> sqlite3.Row | None:

    if not ask_is_fresh(row, at_s):
        return None
    context = continuation_context(conn, row)
    if context is None or context.get("deferred"):
        return None
    if context["kind"] == "work" and dispatch_undelivered(conn, row["id"]):
        return None
    actor = str(context.get("agent_id") or "")
    if not actor:
        return None
    return conn.execute(
        "SELECT id,note,at_ms FROM event WHERE dispatch_id=?"
        " AND responsibility_version=? AND actor=?"
        " AND continuation_generation=? AND kind='note'"
        " AND note LIKE ? AND at_ms>=? ORDER BY id DESC LIMIT 1",
        (row["id"], row["responsibility_version"], actor,
         context["generation"], f"{ASK_NOTE_PREFIX}%",
         int(row["ask_flag"]) - 1),
    ).fetchone()


def credible_ask(conn: sqlite3.Connection, row: sqlite3.Row,
                 at_s: int | None = None) -> bool:
    return current_ask_event(conn, row, at_s) is not None


def whoami() -> str:


    explicit = os.environ.get("DISPATCH_LEDGER_ACTOR")
    if explicit:
        return explicit
    pane = os.environ.get("TMUX_PANE")
    if pane:
        try:
            out = subprocess.run(
                [*tmux_runtime.base_cmd(), "display-message", "-p", "-t", pane, "-F",
                 "#{pane_current_command}@#{session_name}:#{window_index}.#{pane_index}"],
                capture_output=True, text=True, check=False, timeout=5,
            )
            if out.returncode == 0 and out.stdout.strip():
                return out.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return f"unknown@{socket.gethostname().split('.', 1)[0]}"


def canonicalize(text: str) -> str:


    text = text.lower()
    text = re.sub(r"\d{4}-\d{2}-\d{2}[t ]?[\d:.+z-]*", " ", text)
    text = re.sub(r"\b\d+(\.\d+)?\s*(s|m|h|d|sec|secs|min|mins|hours?|days?|ago)\b",
                  " ", text)
    return re.sub(r"\s+", " ", text).strip()


def content_hash(text: str) -> str:
    return hashlib.sha1(canonicalize(text).encode("utf-8", "replace")).hexdigest()


def configured_db_path() -> Path:

    explicit = os.environ.get("DISPATCH_LEDGER_DB", "").strip()
    return Path(explicit).expanduser() if explicit else DB_PATH


class Connection(sqlite3.Connection):
    """Task history on disk; Agent Bus membership belongs only to this read."""

    members: list[dict] | None = None


def read_members(timeout: int = 45) -> list[dict] | None:
    """An empty registry is valid; unavailable or malformed output is unknown."""
    try:
        out = subprocess.run(["bash", bus_cli(), "members"], text=True,
                             capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    rows = []
    seen = set()
    try:
        for line in out.stdout.splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if (not isinstance(row, dict)
                    or not isinstance(row.get("agent_id"), str)
                    or not row["agent_id"]
                    or row["agent_id"] in seen
                    or not isinstance(row.get("aliases", []), list)
                    or any(not isinstance(alias, str)
                           for alias in row.get("aliases", []))):
                return None
            seen.add(row["agent_id"])
            rows.append(row)
    except (ValueError, TypeError):
        return None
    return rows


def _member_snapshot(conn: Connection) -> None:
    """SQL consumers share one connection-local read, never a persisted copy."""
    conn.execute("""
        CREATE TEMP TABLE seat (
            agent_id TEXT PRIMARY KEY,
            handle TEXT NOT NULL,
            aliases TEXT NOT NULL DEFAULT '',
            host TEXT NOT NULL DEFAULT '',
            tmux TEXT NOT NULL DEFAULT '',
            pane_id TEXT NOT NULL DEFAULT '',
            terminal_presence TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            addressable INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL DEFAULT '',
            refreshed_ms INTEGER NOT NULL
        )
    """)
    conn.members = read_members()
    conn.executemany(
        "INSERT INTO temp.seat (agent_id,handle,aliases,host,tmux,pane_id,"
        " terminal_presence,status,addressable,updated_at,refreshed_ms)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [(row["agent_id"], str(row.get("handle", "")),
          ",".join(row.get("aliases") or []), str(row.get("host", "")),
          str(row.get("tmux", "")), str(row.get("pane_id") or ""),
          str(row.get("terminal_presence") or ""), str(row.get("status", "")),
          int(row.get("addressable") is True),
          str(row.get("updated_at", "")), now())
         for row in conn.members or []])
    conn.commit()


def members_available(conn: Connection) -> bool:
    """No refresh operation: membership was read when this connection opened."""
    return conn.members is not None


def connect_readonly() -> sqlite3.Connection:

    path = configured_db_path()
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=15, factory=Connection)
    conn.row_factory = sqlite3.Row
    _member_snapshot(conn)
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def _targets_production_db(path: Path) -> bool:
    """Recognize protected files through relative paths, symlinks and hard links."""
    candidate = path.expanduser().resolve()
    for configured in (PRODUCTION_DB_PATH, *PROTECTED_DATABASES):
        production = configured.expanduser().resolve()
        if candidate == production:
            return True
        try:
            if candidate.samefile(production):
                return True
        except (FileNotFoundError, OSError):
            pass
    return False


def _targets_named_production_db(path: Path) -> bool:
    """Protect named ledgers even when another directory holds a hard link."""
    candidate = path.expanduser().resolve()
    for configured_root in PRODUCTION_NAMED_DB_ROOTS:
        root = configured_root.expanduser().resolve()
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            relative = None
        if relative is not None:
            if (len(relative.parts) == 2
                    and relative.parts[1] == "dispatch-ledger.sqlite3"):
                return True
            if (len(relative.parts) == 4
                    and relative.parts[1:] == (
                        "state", "fleet-orchestrator", "dispatch-ledger.sqlite3"
                    )):
                return True
        try:
            fleets = list(root.iterdir())
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ValueError("cannot inspect protected named database roots") from exc
        for fleet in fleets:
            for suffix in ("dispatch-ledger.sqlite3",
                           "state/fleet-orchestrator/dispatch-ledger.sqlite3"):
                try:
                    if candidate.samefile(fleet / suffix):
                        return True
                except (FileNotFoundError, NotADirectoryError):
                    continue
                except OSError as exc:
                    raise ValueError("cannot inspect a protected named database") from exc
    return False


def _bind_local_history() -> None:
    """First task write fixes the session's history key; queries stay read-only."""
    name = os.environ.get("NW_FLEET", "")
    if (not name or name == "default"
            or os.environ.get("AGENT_BUS_TRANSPORT") != "local"
            or os.environ.get("NW_FLEET_PROFILE_PATH")
            or not os.environ.get("NW_FLEET_PRIMARY_SESSION")):
        return
    path = SCRIPT_DIR / "lib" / "fleet-profile.py"
    spec = importlib.util.spec_from_file_location("workplane_fleet_profile", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load fleet profile resolver: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.bind_local_session(name, os.environ)


def connect_writable(*, timeout: float = 15) -> sqlite3.Connection:
    """A configured installation can write live state; development copies need isolated state."""


    explicit = os.environ.get("DISPATCH_LEDGER_DB", "").strip()
    repo_root = SCRIPT_DIR.parent.resolve()
    path = configured_db_path()
    noncanonical = repo_root != CANONICAL_REPO_ROOT
    if noncanonical and (
        not explicit
        or _targets_production_db(path)
        or _targets_named_production_db(path)
    ):
        raise ValueError(
            "refusing a production-ledger write from a non-canonical"
            f" checkout ({repo_root}); DISPATCH_LEDGER_DB must name an"
            " isolated database, never the production file or an alias of it,"
            " including a named production file"
        )


    _bind_local_history()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=timeout, factory=Connection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={max(0, int(timeout * 1000))}")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS dispatch (
            id           TEXT PRIMARY KEY,
            created_ms   INTEGER NOT NULL,
            created_by   TEXT NOT NULL,
            recipient    TEXT NOT NULL,
            subject      TEXT NOT NULL,
            body         TEXT NOT NULL DEFAULT '',
            check_cmd    TEXT NOT NULL DEFAULT '',
            links        TEXT NOT NULL DEFAULT '',
            state        TEXT NOT NULL DEFAULT 'open',
            resolution   TEXT NOT NULL DEFAULT '',
            check_after  INTEGER NOT NULL,
            chases       INTEGER NOT NULL DEFAULT 0,
            chases_total INTEGER NOT NULL DEFAULT 0,
            last_event   INTEGER NOT NULL,
            responsibility_version INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS event (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            dispatch_id TEXT NOT NULL,
            at_ms       INTEGER NOT NULL,
            actor       TEXT NOT NULL,
            kind        TEXT NOT NULL,
            note        TEXT NOT NULL DEFAULT '',
            responsibility_version INTEGER NOT NULL DEFAULT -1,
            continuation_generation TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS edge (
            src    TEXT NOT NULL,
            kind   TEXT NOT NULL,
            dst    TEXT NOT NULL,
            at_ms  INTEGER NOT NULL,
            actor  TEXT NOT NULL,
            note   TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (src, kind, dst)
        );
        CREATE TABLE IF NOT EXISTS task_msg (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id    TEXT NOT NULL,
            dedup_key  TEXT NOT NULL UNIQUE,
            purpose    TEXT NOT NULL,
            target     TEXT NOT NULL,
            subject    TEXT NOT NULL DEFAULT '',
            at_ms      INTEGER NOT NULL,
            msg_id     TEXT NOT NULL DEFAULT '',
            recipient_agent_id TEXT NOT NULL DEFAULT '',
            recipient_version INTEGER NOT NULL DEFAULT 0,
            send_state TEXT NOT NULL DEFAULT 'recorded',
            delivered  INTEGER NOT NULL DEFAULT 0,
            processed  TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS schema_migration (
            name   TEXT PRIMARY KEY,
            status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS drive (
            task_id      TEXT NOT NULL,
            seat         TEXT NOT NULL,
            generation   TEXT NOT NULL DEFAULT '',
            st           TEXT NOT NULL DEFAULT 'dispatched',
            cycles       INTEGER NOT NULL DEFAULT 0,
            grace_used   INTEGER NOT NULL DEFAULT 0,
            idle_waits   INTEGER NOT NULL DEFAULT 0,
            absent_ticks INTEGER NOT NULL DEFAULT 0,
            updated_ms   INTEGER NOT NULL,
            PRIMARY KEY (task_id, seat)
        );
        CREATE TABLE IF NOT EXISTS wake_attempt (
            task_id     TEXT NOT NULL,
            seat        TEXT NOT NULL,
            purpose     TEXT NOT NULL,
            generation  TEXT NOT NULL,
            at_ms       INTEGER NOT NULL,
            fails       INTEGER NOT NULL DEFAULT 0,
            resolved_ms INTEGER NOT NULL DEFAULT 0,
            outcome     TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (task_id, seat, purpose, generation)
        );
        CREATE TABLE IF NOT EXISTS seat_presence (
            seat    TEXT PRIMARY KEY,
            kind    TEXT NOT NULL,
            at_ms   INTEGER NOT NULL,
            pane    TEXT NOT NULL DEFAULT '',
            harness TEXT NOT NULL DEFAULT '',
            starts  INTEGER NOT NULL DEFAULT 0,
            ends    INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS seat_watch (
            agent_id      TEXT PRIMARY KEY,
            first_dead_ms INTEGER NOT NULL,
            last_nudge_ms INTEGER NOT NULL DEFAULT 0,
            probe_ms      INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS completion_claim (
            task_id     TEXT NOT NULL,
            round       INTEGER NOT NULL,
            claimant    TEXT NOT NULL,
            generation  TEXT NOT NULL,
            claimed_ms  INTEGER NOT NULL,
            payload     TEXT NOT NULL DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'standing',
            resolved_ms INTEGER NOT NULL DEFAULT 0,
            reason      TEXT NOT NULL DEFAULT '',
            event_id    INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (task_id, round)
        );
        CREATE TABLE IF NOT EXISTS wake_lease (
            seat       TEXT NOT NULL,
            generation INTEGER NOT NULL,
            wake_id    TEXT NOT NULL,
            state      TEXT NOT NULL,
            holder     TEXT NOT NULL,
            opened_s   INTEGER NOT NULL,
            updated_s  INTEGER NOT NULL,
            released_s INTEGER,
            PRIMARY KEY (seat, generation)
        );
        CREATE TABLE IF NOT EXISTS wake_cause (
            wake_id TEXT NOT NULL,
            task_id TEXT NOT NULL,
            purpose TEXT NOT NULL,
            PRIMARY KEY (wake_id, task_id, purpose)
        );
        CREATE TABLE IF NOT EXISTS wake_event (
            at_s    INTEGER NOT NULL,
            seat    TEXT NOT NULL,
            wake_id TEXT,
            kind    TEXT NOT NULL,
            detail  TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS review_intent (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id    TEXT NOT NULL,
            seat       TEXT NOT NULL,
            scope      TEXT NOT NULL DEFAULT '',
            started_s  INTEGER NOT NULL,
            closed_s   INTEGER
        );
        CREATE TABLE IF NOT EXISTS role_assignment (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            role       TEXT NOT NULL,
            agent_id   TEXT NOT NULL,
            granted_by TEXT NOT NULL,
            granted_ms INTEGER NOT NULL,
            revoked_ms INTEGER
        );
        CREATE TABLE IF NOT EXISTS team_member (
            parent_task_id TEXT NOT NULL,
            agent_id       TEXT NOT NULL,
            team_role      TEXT NOT NULL,
            added_by       TEXT NOT NULL,
            added_ms       INTEGER NOT NULL,
            PRIMARY KEY (parent_task_id, agent_id)
        );
        CREATE INDEX IF NOT EXISTS dispatch_state ON dispatch(state, check_after);
        CREATE INDEX IF NOT EXISTS event_dispatch ON event(dispatch_id, at_ms);
        CREATE INDEX IF NOT EXISTS edge_dst ON edge(dst, kind);
        CREATE INDEX IF NOT EXISTS task_msg_task ON task_msg(task_id);
        """
    )


    have = {r[1] for r in conn.execute("PRAGMA table_info(dispatch)")}
    with conn:
        if "chases_total" not in have:
            conn.execute("ALTER TABLE dispatch ADD COLUMN chases_total INTEGER NOT NULL DEFAULT 0")
            conn.execute("UPDATE dispatch SET chases_total = chases")
        for col, decl in (
            ("workflow", "TEXT NOT NULL DEFAULT 'dispatch'"),
            ("parent_id", "TEXT NOT NULL DEFAULT ''"),
            ("repo", "TEXT NOT NULL DEFAULT ''"),
            ("owner_seat", "TEXT NOT NULL DEFAULT ''"),
            ("reviewer_seat", "TEXT NOT NULL DEFAULT ''"),
            ("round", "INTEGER NOT NULL DEFAULT 0"),
            ("ready_cmd", "TEXT NOT NULL DEFAULT ''"),
            ("done_cmd", "TEXT NOT NULL DEFAULT ''"),
            ("progress_hash", "TEXT NOT NULL DEFAULT ''"),
            ("guard_unknown_streak", "INTEGER NOT NULL DEFAULT 0"),
            ("ask_flag", "INTEGER NOT NULL DEFAULT 0"),
            ("receipt_body", "TEXT NOT NULL DEFAULT ''"),


            ("after_s", "INTEGER NOT NULL DEFAULT 0"),
            ("deadline_ms", "INTEGER NOT NULL DEFAULT 0"),
            ("breaker_cmd", "TEXT NOT NULL DEFAULT ''"),
            ("check_fail_streak", "INTEGER NOT NULL DEFAULT 0"),


            ("deferred_dispatch", "INTEGER NOT NULL DEFAULT 0"),


            ("reviewer_pool", "TEXT NOT NULL DEFAULT ''"),


            ("requester_seat", "TEXT NOT NULL DEFAULT ''"),
            ("await_notify", "INTEGER NOT NULL DEFAULT 0"),


            ("no_chase", "INTEGER NOT NULL DEFAULT 0"),


            ("responsibility_version", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in have:
                conn.execute(f"ALTER TABLE dispatch ADD COLUMN {col} {decl}")
        cc_cols = {r[1] for r in conn.execute("PRAGMA table_info(completion_claim)")}
        if cc_cols and "event_id" not in cc_cols:
            conn.execute("ALTER TABLE completion_claim ADD COLUMN event_id"
                         " INTEGER NOT NULL DEFAULT 0")
        tm_cols = {r[1] for r in conn.execute("PRAGMA table_info(task_msg)")}
        for col, decl in (
            ("poll_count", "INTEGER NOT NULL DEFAULT 0"),


            ("attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("last_error", "TEXT NOT NULL DEFAULT ''"),
            ("body", "TEXT NOT NULL DEFAULT ''"),


            ("recipient_agent_id", "TEXT NOT NULL DEFAULT ''"),


            ("recipient_version", "INTEGER NOT NULL DEFAULT 0"),


            ("escalated_to_operator", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in tm_cols:
                conn.execute(f"ALTER TABLE task_msg ADD COLUMN {col} {decl}")
        event_cols = {r[1] for r in conn.execute("PRAGMA table_info(event)")}
        reset_drive = False
        if "responsibility_version" not in event_cols:
            conn.execute("ALTER TABLE event ADD COLUMN responsibility_version"
                         " INTEGER NOT NULL DEFAULT -1")
            reset_drive = True
        if "continuation_generation" not in event_cols:
            conn.execute("ALTER TABLE event ADD COLUMN continuation_generation"
                         " TEXT NOT NULL DEFAULT ''")
            reset_drive = True
        if reset_drive:


            conn.execute("DELETE FROM drive")
        drive_cols = {r[1] for r in conn.execute("PRAGMA table_info(drive)")}
        if "generation" not in drive_cols:
            conn.execute("ALTER TABLE drive ADD COLUMN generation"
                         " TEXT NOT NULL DEFAULT ''")


        responsibility_migration = conn.execute(
            "SELECT status FROM schema_migration"
            " WHERE name='responsibility-versions-v1'"
        ).fetchone()
        if responsibility_migration is None:
            conn.execute(
                "INSERT INTO schema_migration(name,status)"
                " VALUES ('responsibility-versions-v1','pending')"
            )
            responsibility_migration = {"status": "pending"}
        if (responsibility_migration is not None
                and responsibility_migration["status"] == "pending"):


            conn.execute(
                "UPDATE schema_migration SET status='done'"
                " WHERE name='responsibility-versions-v1'"
            )


        cache_refusal_migration = conn.execute(
            "SELECT status FROM schema_migration"
            " WHERE name='cache-refusal-retry-v1'"
        ).fetchone()
        if cache_refusal_migration is None:
            conn.execute(
                "INSERT INTO schema_migration(name,status)"
                " VALUES ('cache-refusal-retry-v1','pending')"
            )
            cache_refusal_migration = {"status": "pending"}
        if (cache_refusal_migration is not None
                and cache_refusal_migration["status"] == "pending"):
            conn.execute(
                "UPDATE task_msg SET send_state='recorded'"
                " WHERE send_state='invalid-target' AND attempts=0"
                " AND (last_error LIKE '%registered but not addressable%'"
                " OR last_error LIKE"
                " '%matches % addressable Agent Bus identities%')"
            )
            conn.execute(
                "UPDATE schema_migration SET status='done'"
                " WHERE name='cache-refusal-retry-v1'"
            )
        # Membership is owned by Agent Bus. Discard the former durable copy;
        # old read-only ledgers are shadowed by the TEMP table without mutation.
        conn.execute("DROP TABLE IF EXISTS main.seat")


        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS state_pair (
                workflow   TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state   TEXT NOT NULL,
                PRIMARY KEY (workflow, from_state, to_state)
            )
            """
        )

        conn.execute("CREATE INDEX IF NOT EXISTS dispatch_parent ON"
                     " dispatch(parent_id) WHERE parent_id != ''")
        conn.execute("DROP TRIGGER IF EXISTS dispatch_responsibility_version")
        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS dispatch_responsibility_version
            AFTER UPDATE OF workflow,state,recipient,owner_seat,reviewer_seat
            ON dispatch
            WHEN
              (CASE
                 WHEN OLD.workflow='dispatch'
                      AND OLD.state IN ('open','acked')
                   THEN 'recipient:' || OLD.recipient
                 WHEN OLD.workflow='pr'
                      AND OLD.state IN ('authoring','fixing','receipt-due')
                   THEN 'owner:' || OLD.owner_seat
                 WHEN OLD.workflow='pr' AND OLD.state='awaiting-review'
                   THEN 'reviewer:' || OLD.reviewer_seat
                 ELSE ''
               END)
              IS NOT
              (CASE
                 WHEN NEW.workflow='dispatch'
                      AND NEW.state IN ('open','acked')
                   THEN 'recipient:' || NEW.recipient
                 WHEN NEW.workflow='pr'
                      AND NEW.state IN ('authoring','fixing','receipt-due')
                   THEN 'owner:' || NEW.owner_seat
                 WHEN NEW.workflow='pr' AND NEW.state='awaiting-review'
                   THEN 'reviewer:' || NEW.reviewer_seat
                 ELSE ''
               END)
            BEGIN
              UPDATE dispatch
                 SET responsibility_version=OLD.responsibility_version+1
               WHERE id=NEW.id;
              DELETE FROM drive WHERE task_id=NEW.id;
              UPDATE wake_attempt
                 SET resolved_ms=CAST(strftime('%s','now') AS INTEGER),
                     outcome='responsibility-changed'
               WHERE task_id=NEW.id AND resolved_ms=0;
            END
            """
        )

    with conn:


        sp_cols = {r[1] for r in conn.execute("PRAGMA table_info(state_pair)")}
        if sp_cols and "workflow" not in sp_cols:
            conn.execute("DROP TRIGGER IF EXISTS dispatch_state_legal")
            conn.execute("DROP TABLE state_pair")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS state_pair (
                workflow   TEXT NOT NULL,
                from_state TEXT NOT NULL,
                to_state   TEXT NOT NULL,
                PRIMARY KEY (workflow, from_state, to_state)
            )
            """
        )
        conn.execute("DELETE FROM state_pair")
        conn.executemany(
            "INSERT INTO state_pair (workflow, from_state, to_state) VALUES (?,?,?)",
            sorted({(wf, s, t)
                    for wf, spec in WORKFLOWS.items()
                    for (s, _e), t in spec["transitions"].items() if s != "_new"}),
        )


        conn.execute(
            """
            CREATE TRIGGER IF NOT EXISTS dispatch_state_legal
            BEFORE UPDATE OF state ON dispatch
            WHEN NOT EXISTS (
                SELECT 1 FROM state_pair
                WHERE workflow = OLD.workflow
                  AND from_state = OLD.state AND to_state = NEW.state
            )
            BEGIN
                SELECT RAISE(ABORT,
                    'illegal state change: the transition table has no move between these two states');
            END;
            """
        )
    _member_snapshot(conn)
    return conn


def workflow_spec(workflow: str) -> dict:
    try:
        return WORKFLOWS[workflow]
    except KeyError:
        raise SystemExit(f"FAIL  unknown workflow {workflow!r}; "
                         f"known: {', '.join(sorted(WORKFLOWS))}") from None


def step(workflow: str, state: str, event: str) -> str:

    spec = workflow_spec(workflow)
    try:
        return spec["transitions"][(state, event)]
    except KeyError:
        legal = ", ".join(sorted(e for (s, e) in spec["transitions"] if s == state))
        raise SystemExit(
            f"FAIL  {event!r} is not a legal move from state {state!r}\n"
            f"      legal from here: " + (legal or "nothing")
        ) from None


def row_workflow(row: sqlite3.Row) -> str:
    try:
        return row["workflow"] or "dispatch"
    except (IndexError, KeyError):
        return "dispatch"


def step_row(row: sqlite3.Row, event: str) -> str:

    wf = row_workflow(row)
    if row["state"] in workflow_spec(wf)["terminal"]:
        raise SystemExit(
            f"FAIL  {row['id']} is {row['state']} ({row['resolution'] or 'no resolution'}) "
            f"- {event!r} is not a legal move\n"
            f"      a terminal dispatch is final; open a new one and link it with "
            f"`link <new> supersedes {row['id']}` rather than reviving this id"
        )
    return step(wf, row["state"], event)


def event_kind(stored: str) -> str:


    return stored.split(":", 1)[0]


def record(conn: sqlite3.Connection, did: str, kind: str, note: str = "", *,
           actor: str = "", continuation_generation: str = "") -> int:


    cur = conn.execute(
        "INSERT INTO event (dispatch_id, at_ms, actor, kind, note,"
        " responsibility_version,continuation_generation)"
        " VALUES (?,?,?,?,?,COALESCE((SELECT responsibility_version FROM"
        " dispatch WHERE id=?),-1),?)",
        (did, now(), actor or whoami(), kind, note, did,
         continuation_generation),
    )
    return cur.lastrowid


def add_edge(conn: sqlite3.Connection, src: str, kind: str, dst: str, note: str = "") -> None:
    if kind not in EDGE_KINDS:
        raise SystemExit(f"FAIL  edge kind must be one of {', '.join(EDGE_KINDS)}")
    if src == dst:
        raise SystemExit("FAIL  a dispatch cannot point at itself")
    if kind == "needs" and needs_would_cycle(conn, src, dst):
        raise SystemExit(
            f"FAIL  {src} needs {dst} would close a dependency cycle - every task"
            f" in that loop would wait forever on the one behind it.\n"
            f"      Refused at write time; the graph never holds a deadlock,"
            f" rather than reporting one afterwards.")
    conn.execute(
        "INSERT OR REPLACE INTO edge (src, kind, dst, at_ms, actor, note) VALUES (?,?,?,?,?,?)",
        (src, kind, dst, now(), whoami(), note),
    )


def is_closed(row: sqlite3.Row) -> bool:


    return row["state"] in workflow_spec(row_workflow(row))["terminal"]


def needs_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:

    return [r["dst"] for r in conn.execute(
        "SELECT dst FROM edge WHERE src=? AND kind='needs' ORDER BY dst",
        (task_id,))]


def needed_by_ids(conn: sqlite3.Connection, task_id: str) -> list[str]:

    return [r["src"] for r in conn.execute(
        "SELECT src FROM edge WHERE dst=? AND kind='needs' ORDER BY src",
        (task_id,))]


def predecessors(conn: sqlite3.Connection, task_id: str) -> list[sqlite3.Row]:


    rows = []
    for pid in needs_ids(conn, task_id):
        row = conn.execute("SELECT * FROM dispatch WHERE id=?", (pid,)).fetchone()
        if row is not None:
            rows.append(row)
    return rows


def open_predecessors(conn: sqlite3.Connection, task_id: str) -> list[sqlite3.Row]:
    return [r for r in predecessors(conn, task_id) if not is_closed(r)]


def needs_would_cycle(conn: sqlite3.Connection, src: str, dst: str) -> bool:


    if src == dst:
        return True
    seen, frontier = {dst}, [dst]
    while frontier:
        here = frontier.pop()
        if here == src:
            return True
        for nxt in needs_ids(conn, here):
            if nxt not in seen:
                seen.add(nxt)
                frontier.append(nxt)
    return False


def last_event_at(conn: sqlite3.Connection, task_id: str, kind: str,
                  note_prefix: str = "") -> int | None:


    sql = "SELECT MAX(at_ms) FROM event WHERE dispatch_id=? AND kind=?"
    params: list = [task_id, kind]
    if note_prefix:
        sql += " AND note LIKE ?"
        params.append(note_prefix + "%")
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row and row[0] else None


def run_breaker(cmd: str, timeout: int = BREAKER_TIMEOUT_S) -> tuple[int, str]:


    try:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                             timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"breaker timed out after {timeout}s"
    except OSError as e:
        return 127, str(e)[:200]
    text = (out.stdout or "").strip() or (out.stderr or "").strip()
    return out.returncode, (text[:200] if text else "(no output)")


def fetch(conn: sqlite3.Connection, did: str) -> sqlite3.Row:

    row = conn.execute("SELECT * FROM dispatch WHERE id = ?", (did,)).fetchone()
    if row:
        return row
    rows = conn.execute(
        "SELECT * FROM dispatch WHERE id LIKE ?", (did + "%",)
    ).fetchall()
    if not rows:
        raise SystemExit(f"FAIL  no dispatch matches {did!r}")
    if len(rows) > 1:
        ids = ", ".join(r["id"] for r in rows[:6])
        raise SystemExit(f"FAIL  {did!r} matches {len(rows)} dispatches: {ids}")
    return rows[0]


def insert_task(conn: sqlite3.Connection, *, recipient: str, subject: str,
                body: str = "", check_cmd: str = "", links: str = "",
                after_s: int = 2700, workflow: str = "dispatch",
                parent_id: str = "", repo: str = "", owner_seat: str = "",
                reviewer_seat: str = "", ready_cmd: str = "",
                done_cmd: str = "", needs=(), deadline_s: int = 0,
                breaker_cmd: str = "", requester_seat: str = "",
                await_notify: int = 0, no_chase: int = 0) -> str:
    if not (recipient or "").strip():
        raise SystemExit("FAIL  recipient cannot be empty: every task needs"
                         " one durable destination")
    if workflow == "pr" and not (owner_seat or "").strip():
        raise SystemExit("FAIL  owner cannot be empty: a new PR task starts"
                         " with authoring responsibility")
    if workflow == "pr" and not (reviewer_seat or "").strip():
        raise SystemExit("FAIL  reviewer cannot be empty: a PR task must name"
                         " who receives review responsibility")
    for field, value in (("recipient", recipient), ("owner", owner_seat),
                         ("reviewer", reviewer_seat)):
        if (value or "").strip().lower() in {"all", "@all"}:
            raise SystemExit(
                f"FAIL  {field} cannot be all/@all: task responsibility needs"
                " exactly one seat; use `orc announce` for broadcasts"
            )
    spec = workflow_spec(workflow)


    pred_ids = list(dict.fromkeys(fetch(conn, raw)["id"] for raw in (needs or ())))
    if pred_ids and ("_new", EVENT_OPEN_WAITING) not in spec["transitions"]:
        raise SystemExit(
            f"FAIL  the {workflow} workflow takes no --needs: a parent goal already"
            f" waits for its children, and its rollup is that wait.\n"
            f"      Put the needs edges on the child tasks instead.")
    open_preds = [p for p in (fetch(conn, pid) for pid in pred_ids)
                  if not is_closed(p)]
    initial_event = EVENT_OPEN_WAITING if open_preds else spec["initial_event"]
    initial = spec["transitions"][("_new", initial_event)]
    if parent_id:
        parent = fetch(conn, parent_id)
        parent_id = parent["id"]
        if row_workflow(parent) != "parent":
            raise SystemExit(f"FAIL  --parent {parent_id} is a "
                             f"{row_workflow(parent)} task, not a parent goal")
    did = uuid.uuid4().hex[:8]
    conn.execute(
        "INSERT INTO dispatch (id, created_ms, created_by, recipient, subject, body,"
        " check_cmd, links, state, check_after, last_event, workflow, parent_id,"
        " repo, owner_seat, reviewer_seat, ready_cmd, done_cmd, after_s,"
        " deadline_ms, breaker_cmd, requester_seat, await_notify, no_chase)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (did, now(), whoami(), recipient, subject, body, check_cmd, links,
         initial, now() + after_s, now(), workflow, parent_id, repo,
         owner_seat, reviewer_seat, ready_cmd, done_cmd, after_s,
         (now() + deadline_s) if deadline_s else 0, breaker_cmd,
         requester_seat, await_notify, no_chase),
    )
    record(conn, did, initial_event, subject)
    for pid in pred_ids:
        add_edge(conn, did, "needs", pid,
                 "waiting on it" if any(p["id"] == pid for p in open_preds)
                 else "already closed at open time")
    if parent_id:
        add_edge(conn, did, "derived-from", parent_id)

        parent = fetch(conn, parent_id)
        if parent["state"] == "ready-to-close":
            conn.execute("UPDATE dispatch SET state=?, ask_flag=0,"
                         " last_event=? WHERE id=?",
                         (step("parent", "ready-to-close", "child-opened"),
                          now(), parent_id))
            record(conn, parent_id, "child-opened", did)
    return did


POOL_SUFFIX = "-pool"


def role_holders(conn: sqlite3.Connection, role: str) -> list[str]:

    return [r["agent_id"] for r in conn.execute(
        "SELECT agent_id FROM role_assignment WHERE role=? AND revoked_ms IS NULL"
        " ORDER BY granted_ms ASC", (role,))]


ROTATION_COOLDOWN_S = 6 * 3600


def pool_pick(conn: sqlite3.Connection, role: str,
              exclude=()) -> str | None:


    loads: dict[str, int] = {}
    for row in conn.execute(
            "SELECT * FROM dispatch WHERE workflow='pr' AND"
            " state != 'closed' AND reviewer_seat != ''"):
        seat = row["reviewer_seat"]
        if seat.startswith("role:"):
            continue
        delivered = resolve_last_delivered_recipient(
            conn, row, seat, ("review-request",))
        if delivered.get("deferred"):
            current = resolve_recipient(conn, seat, row["parent_id"])
            key = current["agent_id"] or seat
        else:
            key = delivered["agent_id"] or delivered["seat"]
        loads[key] = loads.get(key, 0) + 1
    best, best_load = None, None
    for agent_id in role_holders(conn, role):
        if agent_id in exclude:
            continue
        if not conn.execute(
                "SELECT 1 FROM seat WHERE agent_id=? AND addressable=1",
                (agent_id,)).fetchone():
            continue


        if conn.execute(
                "SELECT 1 FROM event WHERE note LIKE ? AND at_ms > ? LIMIT 1",
                (f"reviewer-rotated: {agent_id} -> %",
                 now() - ROTATION_COOLDOWN_S)).fetchone():
            continue
        load = loads.get(agent_id, 0)
        if best_load is None or load < best_load:
            best, best_load = agent_id, load
    return best


def role_holder_generation(conn: sqlite3.Connection, role: str,
                           parent_task_id: str = "") -> tuple[str | None, str]:


    team_change = 0
    if parent_task_id:
        team_change = int(conn.execute(
            "SELECT COALESCE(MAX(rowid),0) FROM team_member"
            " WHERE parent_task_id=?", (parent_task_id,),
        ).fetchone()[0])
        row = conn.execute(
            "SELECT rowid,agent_id FROM team_member WHERE parent_task_id=?"
            " AND team_role=? ORDER BY added_ms DESC,rowid DESC LIMIT 1",
            (parent_task_id, role)).fetchone()
        if row:
            return (row["agent_id"],
                    f"team-{parent_task_id}-{role}-holder-{row['rowid']}"
                    f"-change-{team_change}")
    holder = conn.execute(
        "SELECT id,agent_id FROM role_assignment WHERE role=?"
        " AND revoked_ms IS NULL"
        " ORDER BY granted_ms DESC,id DESC LIMIT 1", (role,)).fetchone()
    latest = conn.execute(
        "SELECT id,revoked_ms FROM role_assignment WHERE role=?"
        " ORDER BY id DESC LIMIT 1", (role,)).fetchone()
    holder_id = int(holder["id"]) if holder else 0
    if latest is None:
        change = "none"
    elif latest["revoked_ms"] is None:
        change = f"{latest['id']}-active"
    else:
        change = f"{latest['id']}-revoked-{latest['revoked_ms']}"
    generation = f"role-{role}-holder-{holder_id}-change-{change}"
    if parent_task_id:
        generation += f"-team-scope-{parent_task_id}-change-{team_change}"
    return (holder["agent_id"] if holder else None), generation


def role_holder(conn: sqlite3.Connection, role: str,
                parent_task_id: str = "") -> str | None:

    return role_holder_generation(conn, role, parent_task_id)[0]


def role_target_generation(conn: sqlite3.Connection, target: str,
                           parent_task_id: str = "") -> str:

    if not (target or "").startswith("role:"):
        return ""
    return role_holder_generation(conn, target[5:], parent_task_id)[1]


def role_target_token(conn: sqlite3.Connection, target: str,
                      parent_task_id: str = "") -> str:
    generation = role_target_generation(conn, target, parent_task_id)
    return continuation_token(generation) if generation else ""


def bare_repo(repo: str) -> str:


    return (repo or "").rstrip("/").rsplit("/", 1)[-1]


def merge_key_role(repo: str) -> str:


    return MERGE_KEYS.get(bare_repo(repo), OPERATOR_ROLE)


def window_from_tmux_field(tmux_field: str) -> str | None:

    m = re.search(r"tmux=[^:\s]+:(\d+)\.", tmux_field or "")
    return m.group(1) if m else None


def resolve_recipient(conn: sqlite3.Connection, recipient: str,
                      parent_task_id: str = "") -> dict:


    recipient = (recipient or "").strip()
    agent_id = None
    transport_target = recipient
    deferred = ""
    exact_match = False
    if recipient.startswith("role:"):
        role = recipient[5:]
        if role.endswith(POOL_SUFFIX):
            agent_id = pool_pick(conn, role)
        else:
            agent_id = role_holder(conn, role, parent_task_id)
        if agent_id is None:
            return {"seat": recipient, "window": None, "agent_id": None}
        transport_target = agent_id
    else:


        row = conn.execute("SELECT agent_id,addressable FROM seat"
                           " WHERE agent_id=?", (recipient,)).fetchone()
        if row is not None:
            exact_match = True
            if row["addressable"]:
                agent_id = row["agent_id"]
                transport_target = agent_id
        else:
            rows = conn.execute(
                "SELECT agent_id,addressable FROM seat WHERE"
                " handle=? OR instr(','||aliases||',', ','||?||',') > 0",
                (recipient, recipient)).fetchall()
            exact_match = bool(rows)
            exact = {match["agent_id"] for match in rows
                     if match["addressable"]}
            if len(exact) == 1:
                agent_id = exact.pop()


        if agent_id is None and not exact_match and recipient:


            seg = re.compile(rf"(^|[/-]){re.escape(recipient)}([/-]|$)")
            hits, all_hits = set(), set()
            for s in conn.execute(
                    "SELECT agent_id,handle,aliases,addressable FROM seat"):
                names = [s["handle"]] + [a for a in (s["aliases"] or "").split(",") if a]
                if any(seg.search(n) for n in names):
                    all_hits.add(s["agent_id"])
                    if s["addressable"]:
                        hits.add(s["agent_id"])
            if len(hits) == 1:
                agent_id = hits.pop()


            elif len(hits) > 1:
                deferred = (f"recipient {recipient!r} matches {len(hits)}"
                            " addressable Agent Bus identities")
            elif all_hits:
                deferred = (f"recipient {recipient!r} only matches Agent Bus"
                            " identities that are not addressable")
    if agent_id:
        seat_row = conn.execute("SELECT tmux,host,addressable,pane_id,terminal_presence FROM seat"
                                " WHERE agent_id=?",
                                (agent_id,)).fetchone()
        if seat_row is None:


            return {"seat": agent_id, "window": None, "agent_id": None,
                    "transport_target": transport_target}
        if not seat_row["addressable"]:


            return {"seat": agent_id, "window": None, "agent_id": None,
                    "transport_target": transport_target}
    if agent_id:
        window = window_from_tmux_field(seat_row["tmux"]) if seat_row else None
        local = bool(seat_row) and seat_row["host"] == socket.gethostname().split(".", 1)[0]


        return {"seat": agent_id, "window": window if local else None,
                "pane_id": seat_row["pane_id"] if local else "",
                "terminal_presence": seat_row["terminal_presence"] if local else "",
                "agent_id": agent_id, "transport_target": transport_target}
    if deferred:
        return {"seat": recipient or "unknown", "window": None,
                "agent_id": None, "deferred": deferred}
    if exact_match:
        return {"seat": recipient or "unknown", "window": None,
                "agent_id": None, "transport_target": transport_target}
    m = RECIPIENT_RE.search(recipient)
    if m:
        return {"seat": recipient, "window": m.group(1), "agent_id": None}
    return {"seat": recipient or "unknown", "window": None, "agent_id": None}


def message_recipient_agent_id(msg_id: str, stored: str = "",
                               target: str = "") -> str:


    if target in {"all", "@all"}:
        return ""
    if stored:
        return stored
    if msg_id:
        rows = _agent_bus_rows(
            "SELECT recipient_agent_id FROM outbox_recipients WHERE msg_id=?",
            (msg_id,),
        )
        if rows is not None and len(rows) == 1 and rows[0][0]:
            return str(rows[0][0])
    return ""


def resolve_delivered_recipient(conn: sqlite3.Connection, row: sqlite3.Row,
                                requested: str,
                                purposes: tuple[str, ...] = ()) -> dict:


    resolved = resolve_recipient(conn, requested, row["parent_id"])
    version = int(row["responsibility_version"])
    if purposes:


        msg = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=? AND recipient_version=?"
            " AND purpose IN (%s)"
            " ORDER BY id DESC LIMIT 1"
            % ",".join("?" for _ in RESPONSIBILITY_PURPOSES),
            (row["id"], version, *RESPONSIBILITY_PURPOSES),
        ).fetchone()
    else:
        msg = conn.execute(
            "SELECT * FROM task_msg"
            " WHERE task_id=? AND target=? AND recipient_version=?"
            " ORDER BY id DESC LIMIT 1",
            (row["id"], requested, version),
        ).fetchone()
    if msg is None and purposes:
        prior = conn.execute(
            "SELECT 1 FROM task_msg WHERE task_id=? AND purpose IN (%s)"
            " ORDER BY id DESC LIMIT 1"
            % ",".join("?" for _ in RESPONSIBILITY_PURPOSES),
            (row["id"], *RESPONSIBILITY_PURPOSES),
        ).fetchone()
        if prior is not None:


            return {"seat": requested or "unknown", "window": None,
                    "agent_id": None, "deferred":
                    "current responsibility has no recorded message"}
        if version > 0:


            return {"seat": requested or "unknown", "window": None,
                    "agent_id": None, "deferred":
                    "current responsibility message is not recorded yet"}
    if msg is None:
        return resolved
    if purposes and (msg["purpose"] not in purposes
                     or msg["target"] != requested):
        return {"seat": requested or "unknown", "window": None,
                "agent_id": None, "deferred":
                "latest message does not match the current responsibility"}
    if not message_matches_role_generation(conn, msg, row):
        return {"seat": requested or "unknown", "window": None,
                "agent_id": None, "deferred":
                "the current role responsibility has no notice for its"
                " current assignment generation"}
    if msg["send_state"] != "accepted":
        return {"seat": requested or "unknown", "window": None,
                "agent_id": None, "deferred":
                "the current responsibility message is not accepted"}
    actual = message_recipient_agent_id(msg["msg_id"],
                                        msg["recipient_agent_id"],
                                        msg["target"])
    if not actual:


        return {"seat": requested or "unknown", "window": None,
                "agent_id": None, "deferred":
                "accepted Agent Bus recipient identity is temporarily unknown"}
    if actual in {"all", "@all"}:
        return resolved
    actual_resolved = resolve_recipient(conn, actual, row["parent_id"])
    return {**actual_resolved, "message_id": msg["msg_id"],
            "recipient_agent_id": actual}


def resolve_last_delivered_recipient(conn: sqlite3.Connection,
                                     row: sqlite3.Row, requested: str,
                                     purposes: tuple[str, ...]) -> dict:


    current = resolve_recipient(conn, requested, row["parent_id"])
    if not purposes:
        return current
    messages = conn.execute(
        "SELECT msg_id,recipient_agent_id,target,send_state,purpose"
        " FROM task_msg"
        " WHERE task_id=? AND send_state='accepted'"
        " AND purpose IN (%s)"
        " ORDER BY id DESC"
        % ",".join("?" for _ in purposes),
        (row["id"], *purposes),
    ).fetchall()
    if not messages:
        if requested.startswith("role:"):


            return {"seat": requested, "window": None, "agent_id": None,
                    "deferred":
                    "role placeholder cannot prove the historical author"}
        return current
    msg = messages[0]
    if msg["purpose"] == "dispatch" and row_workflow(row) == "pr":


        return {"seat": requested or "unknown", "window": None,
                "agent_id": None, "deferred":
                "legacy PR dispatch cannot prove the historical author"}
    actual = message_recipient_agent_id(msg["msg_id"],
                                        msg["recipient_agent_id"], msg["target"])
    if not actual:
        return {"seat": requested or "unknown", "window": None,
                "agent_id": None, "deferred":
                "historical recipient identity is unknown"}
    return resolve_recipient(conn, actual, row["parent_id"])


def owner_review_identities(conn: sqlite3.Connection,
                            row: sqlite3.Row) -> tuple[set[str], bool]:


    owner = (row["owner_seat"] or "").strip()
    if not owner:
        return set(), False
    identities: set[str] = set()
    unknown = False
    latest_owner_target = ""
    latest_owner_known = False
    messages = conn.execute(
        "SELECT msg_id,recipient_agent_id,target,purpose,recipient_version,"
        " send_state FROM task_msg WHERE task_id=?"
        " AND purpose IN ('receipt-request','findings','reassign-notify',"
        " 'author-request','dispatch') ORDER BY id",
        (row["id"],),
    ).fetchall()
    for msg in messages:


        latest_owner_target = msg["target"]
        latest_owner_known = False
        if msg["send_state"] != "accepted":
            continue
        if msg["purpose"] == "dispatch" and row_workflow(row) == "pr":


            unknown = True
            continue
        actual = message_recipient_agent_id(
            msg["msg_id"], msg["recipient_agent_id"], msg["target"])
        if not actual or actual in {"all", "@all"}:
            unknown = True
            continue
        identities.add(actual)
        latest_owner_known = True

    current_proven = latest_owner_known and latest_owner_target == owner


    current_id = ""
    if not (owner.startswith("role:") and owner.endswith(POOL_SUFFIX)):
        current = resolve_recipient(conn, owner, row["parent_id"])
        current_id = current.get("agent_id") or ""
        if current_id:
            identities.add(current_id)


    current_exact_id = bool(current_id and current_id == owner)
    if not current_exact_id and not current_proven:
        unknown = True
    if owner.startswith("role:") and not current_proven:

        unknown = True
    return identities, unknown


def reviewer_pool_unavailable(conn: sqlite3.Connection,
                              row: sqlite3.Row) -> bool:


    reviewer = (row["reviewer_seat"] or "").strip()
    pool = (row["reviewer_pool"] or "").strip()
    role_pool = (reviewer.startswith("role:")
                 and reviewer.endswith(POOL_SUFFIX))
    if not (row_workflow(row) == "pr" and row["state"] != "closed"
            and (role_pool or pool)):
        return False
    authors, author_unknown = owner_review_identities(conn, row)
    if author_unknown:


        return True
    if not role_pool:
        holders = set(role_holders(conn, pool)) if pool else set()
        return (reviewer in authors or reviewer not in holders
                or conn.execute(
                    "SELECT 1 FROM seat WHERE agent_id=? AND addressable=1",
                    (reviewer,),
                ).fetchone() is None)
    return pool_pick(conn, reviewer[5:], exclude=authors) is None


def reviewer_pool_wait_active(conn: sqlite3.Connection,
                              row: sqlite3.Row) -> bool:

    return (row_workflow(row) == "pr"
            and row["state"] == "awaiting-review"
            and reviewer_pool_unavailable(conn, row))


def reviewer_pool_attention_active(conn: sqlite3.Connection,
                                   row: sqlite3.Row) -> bool:

    return (row_workflow(row) == "pr"
            and row["state"] in {"authoring", "awaiting-review"}
            and reviewer_pool_unavailable(conn, row))


def operator_delivery_failures(conn: sqlite3.Connection,
                               row: sqlite3.Row) -> list[sqlite3.Row]:


    failures = []
    for msg in conn.execute(
            "SELECT * FROM task_msg WHERE task_id=?"
            " AND escalated_to_operator=1 ORDER BY id", (row["id"],)):
        if not message_is_current_responsibility(conn, msg, row):
            continue
        actual = message_recipient_agent_id(
            msg["msg_id"], msg["recipient_agent_id"], msg["target"],
        )
        if msg["send_state"] == "accepted" and actual:
            continue
        failures.append(msg)
    return failures


def current_escalation_delivery_failure(conn: sqlite3.Connection,
                                        row: sqlite3.Row) -> sqlite3.Row | None:

    for msg in conn.execute(
            "SELECT * FROM task_msg WHERE task_id=? AND purpose='escalation'"
            " ORDER BY id DESC", (row["id"],)):
        if not message_is_current_responsibility(conn, msg, row):
            continue
        actual = message_recipient_agent_id(
            msg["msg_id"], msg["recipient_agent_id"], msg["target"])
        if (msg["send_state"] in {"failed", "invalid-target"}
                or (msg["send_state"] == "accepted" and not actual)
                or (msg["send_state"] == "recorded" and msg["last_error"])):
            return msg
        return None
    return None


def escalation_recipient_unproven(conn: sqlite3.Connection,
                                  row: sqlite3.Row) -> bool:

    failure = current_escalation_delivery_failure(conn, row)
    return bool(failure is not None and failure["send_state"] == "accepted")


def continuation_obligation(conn: sqlite3.Connection, row: sqlite3.Row, *,
                            registry_trusted: bool = True) -> dict | None:


    def routed(obligation: dict, scope: str = "") -> dict:
        generation = role_target_generation(
            conn, obligation["requested"], scope)
        if generation:
            obligation["route_generation"] = generation
            obligation["source"] += f":{generation}"
        return obligation

    if is_closed(row) or row["state"] == WAITING_STATE:
        return None
    claim = claim_standing(conn, row, repair=False)
    if claim is not None:
        return routed({
            "kind": "claim-review",
            "requested": claim_judge(
                conn, row, registry_trusted=registry_trusted),
            "purpose": "claim-notify",
            "source": f"claim-event-{claim['event_id'] or claim['round']}",
            "label": f"judge completion claim r{claim['round']}",
        }, row["parent_id"] or "")
    if row_workflow(row) == "parent" and row["state"] == "ready-to-close":
        event = conn.execute(
            "SELECT id FROM event WHERE dispatch_id=?"
            " AND kind='children-closed' ORDER BY id DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        if event is None:
            return None
        return routed({
            "kind": "goal-review",
            "requested": goal_review_recipient(
                conn, row, registry_trusted=registry_trusted),
            "purpose": "goal-review",
            "source": f"children-closed-{event['id']}",
            "label": "review the completed child tasks and decide the goal",
        }, row["id"])
    if row_workflow(row) == "pr" and row["state"] == "merge-pending":
        event = conn.execute(
            "SELECT id FROM event WHERE dispatch_id=? AND kind='receipt'"
            " ORDER BY id DESC LIMIT 1", (row["id"],),
        ).fetchone()
        role = merge_key_role(row["repo"])
        if role == OPERATOR_ROLE:
            requested = "operator"
        else:
            requested = _addressable_attention_candidate(
                conn, f"role:{role}", row["parent_id"] or "",
                registry_trusted=registry_trusted,
            ) or "operator"
        return routed({
            "kind": "merge-review",
            "requested": requested,
            "purpose": "receipt-to-keyholder",
            "source": f"receipt-{event['id'] if event else 0}",
            "label": ("verify the receipt; merge only under separately"
                      " recorded authority"),
        }, row["parent_id"] or "")
    spec = workflow_spec(row_workflow(row))
    col = spec["owed"].get(row["state"])
    if not col:
        return None
    requested = row[col] if col != "recipient" else row["recipient"]
    if not requested:
        return None
    return routed({
        "kind": "work",
        "requested": requested,
        "purpose": "",
        "source": f"responsibility-v{row['responsibility_version']}",
        "label": "continue the assigned work",
    }, row["parent_id"] or "")


def current_continuation_message(conn: sqlite3.Connection,
                                 row: sqlite3.Row,
                                 obligation: dict) -> sqlite3.Row | None:

    purpose = obligation["purpose"]
    if not purpose:
        purposes = responsibility_purposes(row)
        if not purposes:
            return None
        rows = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=? AND purpose IN (%s)"
            " ORDER BY id DESC" % ",".join("?" for _ in purposes),
            (row["id"], *purposes),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=? AND purpose=?"
            " ORDER BY id DESC", (row["id"], purpose),
        ).fetchall()
    return next((msg for msg in rows
                 if message_is_current_responsibility(conn, msg, row)), None)


def continuation_context(conn: sqlite3.Connection, row: sqlite3.Row, *,
                         registry_trusted: bool = True) -> dict | None:

    obligation = continuation_obligation(
        conn, row, registry_trusted=registry_trusted)
    if obligation is None:
        return None
    requested = obligation["requested"]
    if requested.strip().lower() == "operator":
        return {**obligation, "seat": "operator", "agent_id": None,
                "window": None, "generation":
                f"{obligation['kind']}:{obligation['source']}:operator"}
    if obligation["kind"] == "work":
        resolved = resolve_owed_recipient(conn, row, requested)
        msg = current_continuation_message(conn, row, obligation)
        if (msg is None and resolved.get("agent_id")
                and requested != resolved.get("agent_id")):
            return {**obligation, "seat": requested, "agent_id": None,
                    "window": None, "deferred":
                    "an out-of-band task cannot use a mutable alias; assign"
                    " the stable Agent Bus id or deliver it with orc dispatch",
                    "generation":
                    f"{obligation['kind']}:{obligation['source']}:unproven-alias"}
        actual = str(resolved.get("recipient_agent_id") or "")
    else:
        msg = current_continuation_message(conn, row, obligation)
        if msg is None or msg["send_state"] != "accepted":
            return {**obligation, "seat": requested, "agent_id": None,
                    "window": None, "deferred":
                    "the current continuation notice is not accepted",
                    "generation":
                    f"{obligation['kind']}:{obligation['source']}:undelivered"}
        actual = message_recipient_agent_id(
            msg["msg_id"], msg["recipient_agent_id"], msg["target"])
        if not actual:
            return {**obligation, "seat": requested, "agent_id": None,
                    "window": None, "deferred":
                    "the continuation notice has no proven recipient",
                    "generation":
                    f"{obligation['kind']}:{obligation['source']}:unknown"}
        resolved = resolve_recipient(conn, actual, row["parent_id"] or "")
    stable_id = str(actual or resolved.get("agent_id") or "")
    seat = str(stable_id or resolved.get("seat") or requested)
    notice = (f":notice-{msg['id']}"
              if obligation["kind"] != "work" and msg is not None else "")
    generation = (f"{obligation['kind']}:{obligation['source']}{notice}:"
                  f"to-{seat}")
    if (not stable_id and obligation["kind"] == "work"
            and requested == seat and not requested.startswith("role:")
            and conn.execute(
                "SELECT 1 FROM event WHERE dispatch_id=?"
                " AND responsibility_version=? AND actor=?"
                " AND continuation_generation=?"
                " AND kind IN (%s) LIMIT 1"
                % ",".join("?" for _ in SEAT_VOICE_KINDS),
                (row["id"], row["responsibility_version"], requested,
                 generation, *SEAT_VOICE_KINDS),
            ).fetchone() is not None):


        stable_id = requested
    return {**obligation, **resolved, "seat": seat,
            "agent_id": stable_id or None,
            "recipient_agent_id": stable_id,
            "generation": generation, "message_id": msg["id"] if msg else 0}


def continuation_token(generation: str) -> str:

    return hashlib.sha1(generation.encode("utf-8")).hexdigest()[:16]


def escalation_recipient(conn: sqlite3.Connection, row: sqlite3.Row, *,
                         registry_trusted: bool = True) -> str:

    context = continuation_context(
        conn, row, registry_trusted=registry_trusted)
    current_id = ""
    if context is not None and context["seat"] != "operator":
        current_id = str(context.get("agent_id") or context.get("seat") or "")
    parent_id = (row["parent_id"] or "").strip()
    candidates = []
    if parent_id:
        parent = conn.execute(
            "SELECT recipient FROM dispatch WHERE id=?", (parent_id,)
        ).fetchone()
        if parent is not None:
            candidates.append(parent["recipient"])
    candidates.extend((row["requester_seat"] or "", "role:commander"))
    for candidate in candidates:
        candidate = _addressable_attention_candidate(
            conn, candidate, parent_id, registry_trusted=registry_trusted)
        if not candidate:
            continue
        resolved = resolve_recipient(conn, candidate, parent_id)
        candidate_id = str(resolved.get("agent_id") or resolved.get("seat") or "")
        if current_id and candidate_id == current_id:
            continue
        return candidate
    return "operator"


def activate_continuation_generation(conn: sqlite3.Connection, task_id: str,
                                     seat: str, generation: str) -> None:

    conn.execute(
        "DELETE FROM drive WHERE task_id=?"
        " AND (seat!=? OR generation!=?)", (task_id, seat, generation))
    conn.execute(
        "UPDATE wake_attempt SET resolved_ms=?,outcome='obligation-changed'"
        " WHERE task_id=? AND generation!=? AND resolved_ms=0",
        (now(), task_id, generation))


def continuation_route_missing(conn: sqlite3.Connection,
                               row: sqlite3.Row) -> bool:

    obligation = continuation_obligation(conn, row)
    if obligation is None:
        return False
    if obligation["kind"] == "work":
        if dispatch_undelivered(conn, row["id"]):
            return False
        context = continuation_context(conn, row)
        return bool(context is not None
                    and not context.get("agent_id")
                    and context.get("window") is None)
    if obligation["requested"] == "operator":
        return True
    message = current_continuation_message(conn, row, obligation)
    if message is None:
        return True
    if message["send_state"] == "accepted":
        actual = message_recipient_agent_id(
            message["msg_id"], message["recipient_agent_id"],
            message["target"])
        if not actual:
            return True
    return False


def current_drive(conn: sqlite3.Connection,
                  row: sqlite3.Row) -> sqlite3.Row | None:


    context = continuation_context(conn, row)
    if not context or context.get("deferred") or context["seat"] == "operator":
        return None
    return conn.execute(
        "SELECT * FROM drive WHERE task_id=? AND seat=? AND generation=?"
        " ORDER BY updated_ms DESC LIMIT 1",
        (row["id"], context["seat"], context["generation"]),
    ).fetchone()


def drive_needs_attention(conn: sqlite3.Connection, row: sqlite3.Row,
                          drive: sqlite3.Row | None = None) -> bool:

    drive = drive if drive is not None else current_drive(conn, row)
    return bool(
        drive and (drive["st"] == S_ESCALATED
                   or (drive["st"] == S_WAITING
                       and credible_ask(conn, row))))


def deadline_attention_event(conn: sqlite3.Connection,
                             row: sqlite3.Row) -> sqlite3.Row | None:

    deadline = int(row["deadline_ms"] or 0)
    if deadline <= 0 or deadline > now():
        return None
    context = continuation_context(conn, row)
    generation = context["generation"] if context is not None else ""
    event = conn.execute(
        "SELECT id,note,at_ms FROM event WHERE dispatch_id=?"
        " AND kind='auto-chase' AND note LIKE 'engine: DEADLINE OVERDUE:%'"
        " AND responsibility_version=? AND continuation_generation=?"
        " AND at_ms>=?"
        " ORDER BY id DESC LIMIT 1",
        (row["id"], row["responsibility_version"], generation, deadline),
    ).fetchone()
    if event is None:
        return None
    voice = current_continuation_voice(
        conn, row, context, after_event_id=event["id"])
    return None if voice is not None else event


def current_attention_event(conn: sqlite3.Connection,
                            row: sqlite3.Row) -> sqlite3.Row | None:

    candidates = []
    drive = current_drive(conn, row)
    if drive_needs_attention(conn, row, drive):
        event = conn.execute(
            "SELECT id,note,at_ms FROM event WHERE dispatch_id=?"
            " AND kind='auto-chase'"
            " AND responsibility_version=?"
            " AND continuation_generation=?"
            " AND note NOT LIKE 'engine: DEADLINE OVERDUE:%'"
            " AND note NOT LIKE 'engine: review rotation exhausted:%'"
            " ORDER BY id DESC LIMIT 1",
            (row["id"], row["responsibility_version"], drive["generation"]),
        ).fetchone()
        if event is not None:
            candidates.append(event)
    deadline = deadline_attention_event(conn, row)
    if deadline is not None:
        candidates.append(deadline)
    if reviewer_pool_wait_active(conn, row):
        event = conn.execute(
            "SELECT id,note,at_ms FROM event WHERE dispatch_id=?"
            " AND kind='auto-chase'"
            " AND responsibility_version=?"
            " AND note LIKE 'engine: review rotation exhausted:%'"
            " ORDER BY id DESC LIMIT 1",
            (row["id"], row["responsibility_version"]),
        ).fetchone()
        if event is not None:
            candidates.append(event)
    return max(candidates, key=lambda event: event["id"]) if candidates else None


def operator_action_is_current(conn: sqlite3.Connection, row: sqlite3.Row,
                               purpose: str, *,
                               registry_trusted: bool = True) -> bool:

    if purpose == "escalation":
        return (current_attention_event(conn, row) is not None
                and escalation_recipient(
                    conn, row, registry_trusted=registry_trusted)
                == "operator")
    obligation = continuation_obligation(
        conn, row, registry_trusted=registry_trusted)
    return bool(obligation
                and obligation["purpose"] == purpose
                and obligation["requested"] == "operator")


def message_attention_event_id(msg: sqlite3.Row) -> int | None:

    match = re.search(
        r"(?:^|:)attention-event=([1-9][0-9]*)(?::|$)",
        msg["dedup_key"] or "",
    )
    return int(match.group(1)) if match else None


def operator_marker_snapshot_id(msg: sqlite3.Row) -> int | None:

    matches = re.findall(r"(?:^|:)after:([0-9]+)(?::|$)",
                         msg["dedup_key"] or "")
    return int(matches[-1]) if matches else None


def operator_marker_shape_is_current(conn: sqlite3.Connection,
                                     row: sqlite3.Row,
                                     marker: sqlite3.Row) -> bool:

    if (marker["target"] != "operator"
            or marker["send_state"] != "operator-queue"
            or int(marker["recipient_version"]) !=
            int(row["responsibility_version"])):
        return False
    if marker["purpose"] == "claim-notify":
        claim = claim_standing(conn, row, repair=False)
        return bool(claim is not None and marker["dedup_key"].startswith(
            f"claim:{row['id']}:{claim['round']}"))
    if marker["purpose"] == "goal-review":
        event = conn.execute(
            "SELECT id FROM event WHERE dispatch_id=?"
            " AND kind='children-closed' ORDER BY id DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        return bool(row_workflow(row) == "parent"
                    and row["state"] == "ready-to-close"
                    and event is not None
                    and message_attention_event_id(marker) == event["id"])
    if marker["purpose"] == "receipt-to-keyholder":
        event = conn.execute(
            "SELECT id FROM event WHERE dispatch_id=? AND kind='receipt'"
            " ORDER BY id DESC LIMIT 1", (row["id"],),
        ).fetchone()
        return bool(row_workflow(row) == "pr"
                    and row["state"] == "merge-pending"
                    and event is not None
                    and message_attention_event_id(marker) == event["id"])
    if marker["purpose"] == "escalation":
        event = current_attention_event(conn, row)
        return bool(event is not None
                    and message_attention_event_id(marker) == event["id"])
    return False


def attention_message_shape_is_current(conn: sqlite3.Connection,
                                       msg: sqlite3.Row,
                                       task: sqlite3.Row) -> bool:

    if (msg["target"] == "operator"
            or msg["purpose"] not in ATTENTION_ROUTE_PURPOSES
            or (msg["purpose"] in COMMANDER_ROLE_PURPOSES
                and msg["target"] != "role:commander")
            or not message_matches_role_generation(conn, msg, task)):
        return False
    if msg["target"].startswith("role:"):
        scope = (task["id"] if msg["purpose"] == "goal-review"
                 else (task["parent_id"] or ""))
        desired = resolve_recipient(
            conn, msg["target"], scope).get("agent_id")
        if not desired:
            return False
        if msg["send_state"] == "accepted":
            actual = message_recipient_agent_id(
                msg["msg_id"], msg["recipient_agent_id"], msg["target"])
            if actual and actual != desired:
                return False
    if msg["purpose"] == "escalation":
        if (int(msg["recipient_version"]) !=
                int(task["responsibility_version"])
                or dispatch_undelivered(conn, task["id"])):
            return False
        event = current_attention_event(conn, task)
        return bool(event is not None
                    and message_attention_event_id(msg) == event["id"]
                    and escalation_recipient(conn, task) != "operator"
                    and msg["target"] == escalation_recipient(conn, task))
    if msg["purpose"] == "goal-review":
        if (row_workflow(task) != "parent"
                or task["state"] != "ready-to-close"
                or msg["target"] != goal_review_recipient(conn, task)
                or int(msg["recipient_version"]) !=
                int(task["responsibility_version"])):
            return False
        event = conn.execute(
            "SELECT id FROM event WHERE dispatch_id=?"
            " AND kind='children-closed' ORDER BY id DESC LIMIT 1",
            (task["id"],),
        ).fetchone()
        return bool(event is not None
                    and message_attention_event_id(msg) == event["id"])
    if msg["purpose"] == "claim-notify":
        claim = claim_standing(conn, task, repair=False)
        if claim is None or resolve_owed_recipient(conn, task).get("deferred"):
            return False
        base = f"claim:{task['id']}:{claim['round']}"
        return bool(msg["target"] == claim_judge(conn, task)
                    and int(msg["recipient_version"]) ==
                    int(task["responsibility_version"])
                    and (msg["dedup_key"] == base
                         or msg["dedup_key"].startswith(f"{base}:")))
    if msg["purpose"] == "receipt-to-keyholder":
        role = merge_key_role(task["repo"])
        holder = role_holder(conn, role, task["parent_id"] or "")
        obligation = continuation_obligation(conn, task)
        event = conn.execute(
            "SELECT id FROM event WHERE dispatch_id=? AND kind='receipt'"
            " ORDER BY id DESC LIMIT 1", (task["id"],),
        ).fetchone()
        if (row_workflow(task) != "pr" or task["state"] != "merge-pending"
                or role == OPERATOR_ROLE or not holder
                or obligation is None
                or obligation["kind"] != "merge-review"
                or obligation["requested"] != f"role:{role}"
                or (f":role-generation-"
                    f"{continuation_token(obligation['route_generation'])}:"
                    not in msg["dedup_key"])
                or msg["target"] != f"role:{role}"
                or int(msg["recipient_version"]) !=
                int(task["responsibility_version"])
                or event is None
                or message_attention_event_id(msg) != event["id"]):
            return False
        return True
    return False


def newest_effective_action_message(conn: sqlite3.Connection,
                                    row: sqlite3.Row,
                                    purpose: str) -> sqlite3.Row | None:

    messages = conn.execute(
        "SELECT * FROM task_msg WHERE task_id=? AND purpose=? ORDER BY id DESC",
        (row["id"], purpose),
    ).fetchall()
    latest_nonoperator_id = next(
        (int(msg["id"]) for msg in messages if msg["target"] != "operator"),
        0,
    )
    for msg in messages:
        if msg["target"] != "operator":
            return msg
        if not operator_marker_shape_is_current(conn, row, msg):
            continue
        snapshot = operator_marker_snapshot_id(msg)
        if snapshot is not None and latest_nonoperator_id > snapshot:
            continue
        return msg
    return None


def operator_queue_marker(conn: sqlite3.Connection,
                          row: sqlite3.Row,
                          purpose: str = "") -> sqlite3.Row | None:

    for marker in conn.execute(
            "SELECT * FROM task_msg WHERE task_id=?"
            " AND send_state='operator-queue' ORDER BY id DESC", (row["id"],)):
        if purpose and marker["purpose"] != purpose:
            continue
        effective = newest_effective_action_message(
            conn, row, marker["purpose"])
        if effective is None or effective["id"] != marker["id"]:
            continue
        if operator_marker_shape_is_current(conn, row, marker):
            return marker
    return None


def waits_on_operator(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:

    claim = claim_standing(conn, row, repair=False)
    drive = current_drive(conn, row)
    return (row["recipient"].strip().lower() == "operator"
            or continuation_route_missing(conn, row)
            or (row_workflow(row) == "parent"
                and row["state"] == "ready-to-close"
                and goal_review_recipient(conn, row) == "operator")
            or bool(reviewer_pool_attention_active(conn, row)
                    and attention_recipient(conn, row) == "operator")
            or bool(credible_ask(conn, row)
                    and escalation_recipient(conn, row) == "operator")
            or bool(claim and claim_judge(conn, row) == "operator")
            or bool(drive_needs_attention(conn, row, drive)
                    and escalation_recipient(conn, row) == "operator")
            or bool(deadline_attention_event(conn, row)
                    and escalation_recipient(conn, row) == "operator")
            or escalation_recipient_unproven(conn, row)
            or operator_queue_marker(conn, row) is not None
            or bool(operator_delivery_failures(conn, row)))


def dispatch_message_purpose(row: sqlite3.Row) -> str:


    if row_workflow(row) == "pr":
        return "author-request"
    return "dispatch"


RESPONSIBILITY_PURPOSES = frozenset({
    "dispatch", "author-request", "reassign-notify", "review-request",
    "findings", "receipt-request",
})
CURRENT_ACTION_PURPOSES = RESPONSIBILITY_PURPOSES | frozenset({
    "escalation", "claim-notify", "goal-review", "receipt-to-keyholder",
    "review-desync", "continuation-reminder",
})
ATTENTION_ROUTE_PURPOSES = frozenset({
    "escalation", "claim-notify", "goal-review", "receipt-to-keyholder",
})
BROADCAST_REFUSAL_PREFIX = "broadcast is allowed only"


def responsibility_purposes(row: sqlite3.Row) -> tuple[str, ...]:

    workflow = row_workflow(row)
    state = row["state"]
    if workflow == "dispatch":
        return ("reassign-notify", "dispatch")
    if workflow == "pr":
        return {
            "authoring": ("reassign-notify", "author-request"),
            "awaiting-review": ("review-request",),
            "fixing": ("findings", "reassign-notify", "author-request"),
            "receipt-due": ("receipt-request", "reassign-notify",
                            "author-request"),
        }.get(state, ())
    return ()


def message_matches_role_generation(conn: sqlite3.Connection,
                                    msg: sqlite3.Row,
                                    task: sqlite3.Row | None) -> bool:

    target = msg["target"] or ""
    if (not target.startswith("role:")
            or msg["purpose"] not in ROLE_GENERATION_PURPOSES):
        return True
    scope = (task["id"] if task is not None
             and msg["purpose"] == "goal-review"
             else (task["parent_id"] if task is not None else ""))
    token = role_target_token(conn, target, scope or "")
    return f":role-generation-{token}:" in f":{msg['dedup_key']}:"


def message_is_current_responsibility(conn: sqlite3.Connection,
                                      msg: sqlite3.Row,
                                      task: sqlite3.Row) -> bool:


    if (msg["purpose"] in COMMANDER_ROLE_PURPOSES
            and msg["target"] != "role:commander"):


        return False
    if not message_matches_role_generation(conn, msg, task):
        return False
    if msg["purpose"] in ATTENTION_ROUTE_PURPOSES:
        if not attention_message_shape_is_current(conn, msg, task):
            return False
        latest = newest_effective_action_message(
            conn, task, msg["purpose"])
        return latest is not None and latest["id"] == msg["id"]
    if msg["purpose"] == "continuation-reminder":
        context = continuation_context(conn, task)
        if (context is None or context.get("deferred")
                or context["seat"] == "operator"
                or not context.get("agent_id")
                or msg["target"] != context["agent_id"]):
            return False
        token = continuation_token(context["generation"])
        if f":generation-{token}:" not in msg["dedup_key"]:
            return False
        if msg["send_state"] == "accepted":
            actual = message_recipient_agent_id(
                msg["msg_id"], msg["recipient_agent_id"], msg["target"])
            if not actual or actual != context.get("agent_id"):
                return False
        latest = conn.execute(
            "SELECT id FROM task_msg WHERE task_id=?"
            " AND purpose='continuation-reminder'"
            " AND dedup_key LIKE ? ORDER BY id DESC LIMIT 1",
            (task["id"], f"%:generation-{token}:%"),
        ).fetchone()
        return latest is not None and latest["id"] == msg["id"]
    if msg["purpose"] == "review-desync":
        if (row_workflow(task) != "pr"
                or task["state"] != "awaiting-review"
                or msg["target"] != task["reviewer_seat"]
                or int(msg["recipient_version"]) !=
                int(task["responsibility_version"])):
            return False
        latest = conn.execute(
            "SELECT id FROM task_msg WHERE task_id=?"
            " AND purpose='review-desync' AND target=?"
            " AND recipient_version=? ORDER BY id DESC LIMIT 1",
            (task["id"], task["reviewer_seat"],
             task["responsibility_version"]),
        ).fetchone()
        return latest is not None and latest["id"] == msg["id"]
    if msg["purpose"] not in RESPONSIBILITY_PURPOSES:
        return True
    invalid_broadcast = (msg["send_state"] == "invalid-target"
                         and (msg["last_error"] or "").startswith(
                             BROADCAST_REFUSAL_PREFIX))
    if msg["target"] in {"all", "@all"} or invalid_broadcast:


        purposes = responsibility_purposes(task)
        if (msg["purpose"] not in purposes
                or int(msg["recipient_version"]) !=
                int(task["responsibility_version"])):
            return False
        newest = conn.execute(
            "SELECT id FROM task_msg WHERE task_id=? AND recipient_version=?"
            " AND purpose IN (%s) ORDER BY id DESC LIMIT 1"
            % ",".join("?" for _ in RESPONSIBILITY_PURPOSES),
            (task["id"], task["responsibility_version"],
             *RESPONSIBILITY_PURPOSES),
        ).fetchone()
        return newest is not None and newest["id"] == msg["id"]
    purposes = responsibility_purposes(task)
    if (not purposes or msg["purpose"] not in purposes
            or int(msg["recipient_version"]) !=
            int(task["responsibility_version"])
            or msg["target"] != owed_party(task)):
        return False
    newest = conn.execute(
        "SELECT id FROM task_msg WHERE task_id=? AND recipient_version=?"
        " AND purpose IN (%s) ORDER BY id DESC LIMIT 1"
        % ",".join("?" for _ in RESPONSIBILITY_PURPOSES),
        (task["id"], task["responsibility_version"],
         *RESPONSIBILITY_PURPOSES),
    ).fetchone()
    return newest is not None and newest["id"] == msg["id"]


def message_is_sendable(conn: sqlite3.Connection, msg: sqlite3.Row,
                        task: sqlite3.Row | None = None) -> bool:


    if (msg["purpose"] in COMMANDER_ROLE_PURPOSES
            and msg["target"] != "role:commander"):
        return False
    if not message_matches_role_generation(conn, msg, task):
        return False
    return task is None or message_is_current_responsibility(conn, msg, task)


def repair_attention_notifications(conn: sqlite3.Connection,
                                   log=print, *,
                                   registry_trusted: bool = True,
                                   route_observation_id: int | None = None
                                   ) -> int:


    repaired = 0
    tasks = conn.execute(
        "SELECT * FROM dispatch WHERE state!='closed' ORDER BY created_ms",
    ).fetchall()
    for task in tasks:
        purpose = ""
        target = "operator"
        dedup_key = subject = body = ""
        if (row_workflow(task) == "parent"
                and task["state"] == "ready-to-close"):
            event = conn.execute(
                "SELECT id FROM event WHERE dispatch_id=?"
                " AND kind='children-closed' ORDER BY id DESC LIMIT 1",
                (task["id"],),
            ).fetchone()
            if event is None:
                continue
            purpose = "goal-review"
            target = goal_review_recipient(
                conn, task, registry_trusted=registry_trusted)
            dedup_key = (f"goal-review:{task['id']}:"
                         f"attention-event={event['id']}:"
                         f"to:{target}")
            subject = f"goal ready for review: {task['subject']}"[:180]
            body = (f"All children of goal {task['id']} are closed."
                    " Re-review the goal; close it if actually met, or open"
                    " new child tasks for remaining work.")
        elif (row_workflow(task) == "pr"
              and task["state"] == "merge-pending"
              and merge_key_role(task["repo"]) != OPERATOR_ROLE):
            event = conn.execute(
                "SELECT id FROM event WHERE dispatch_id=? AND kind='receipt'"
                " ORDER BY id DESC LIMIT 1", (task["id"],),
            ).fetchone()
            if event is None:
                continue
            role = merge_key_role(task["repo"])
            target = _addressable_attention_candidate(
                conn, f"role:{role}", task["parent_id"] or "",
                registry_trusted=registry_trusted,
            ) or "operator"
            purpose = "receipt-to-keyholder"
            dedup_key = (f"receipt-review:{task['id']}:"
                         f"attention-event={event['id']}:to:{target}")
            subject = f"receipt for your verification: {task['id']}"[:180]
            body = (
                f"Owner receipt on {task['id']} ({task['subject']}). Verify it"
                " yourself before using your merge key; this message is"
                f" evidence, not permission.\n\n{task['receipt_body']}"
            )
        else:
            if dispatch_undelivered(conn, task["id"]):


                continue
            event = current_attention_event(conn, task)
            if event is None:
                continue
            purpose = "escalation"
            target = escalation_recipient(
                conn, task, registry_trusted=registry_trusted)
            reason = (event["note"] or "").removeprefix("engine: ")
            if reason.startswith("DEADLINE OVERDUE:"):
                key_prefix = "deadline-attention"
            elif reason.startswith("review rotation exhausted:"):
                key_prefix = "review-pool-exhausted"
            else:
                key_prefix = "escalation"
            dedup_key = (f"{key_prefix}:{task['id']}:"
                         f"attention-event={event['id']}:"
                         f"to:{target}")
            subject = f"ESCALATION {task['id']}: {reason}"[:180]
            body = (f"{reason}. Subject: {task['subject'][:200]}."
                    " Chase already recorded; inspect the original task"
                    " before deciding what changes are authorized.")
        latest = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=? AND purpose=?"
            " ORDER BY id DESC LIMIT 1", (task["id"], purpose),
        ).fetchone()
        expected_latest_id = latest_message_id(
            conn, task["id"], purpose,
            at_or_before=route_observation_id)
        if target == "operator":
            if ((latest is not None and latest["target"] != "operator")
                    or (not registry_trusted
                        and operator_queue_marker(
                            conn, task, purpose) is None)):
                marker_key = f"{dedup_key}:operator"
                with conn:
                    row_id = record_operator_queue_marker(
                        conn, task["id"], purpose, marker_key, subject, body,
                        registry_trusted=registry_trusted,
                        expected_latest_id=expected_latest_id,
                        expected_responsibility_version=
                        task["responsibility_version"])
                if row_id is not None:
                    log(f"OK moved {purpose} attention for {task['id']}"
                        " to the operator's original-task list")
                    repaired += 1
            continue
        if (latest is not None
                and message_is_current_responsibility(conn, latest, task)):
            continue
        dedup_key = (f"{dedup_key}:after:"
                     f"{expected_latest_id}")
        with conn:
            row_id = record_msg(
                conn, task["id"], purpose, dedup_key, target, subject, body,
                expected_latest_id=expected_latest_id,
                expected_responsibility_version=
                task["responsibility_version"],
            )
        if row_id is not None:
            log(f"OK restored {purpose} notice for {task['id']} -> {target}")
            repaired += 1
    return repaired


def repair_missing_responsibility_messages(conn: sqlite3.Connection,
                                           log=print) -> int:


    repaired = 0
    for task in conn.execute(
            "SELECT * FROM dispatch WHERE state!='closed'"
            " ORDER BY created_ms").fetchall():
        purposes = responsibility_purposes(task)
        owed = owed_party(task)
        if not purposes or not owed or owed.strip().lower() == "operator":
            continue
        latest = conn.execute(
            "SELECT * FROM task_msg WHERE task_id=?"
            " AND recipient_version=? AND purpose IN (%s)"
            " ORDER BY id DESC LIMIT 1"
            % ",".join("?" for _ in RESPONSIBILITY_PURPOSES),
            (task["id"], task["responsibility_version"],
             *RESPONSIBILITY_PURPOSES),
        ).fetchone()
        if (latest and latest["target"] == owed
                and latest["purpose"] in purposes
                and message_is_current_responsibility(conn, latest, task)):
            continue
        history = latest or conn.execute(
            "SELECT 1 FROM task_msg WHERE task_id=? AND purpose IN (%s)"
            " LIMIT 1"
            % ",".join("?" for _ in RESPONSIBILITY_PURPOSES),
            (task["id"], *RESPONSIBILITY_PURPOSES),
        ).fetchone()
        if not history and int(task["responsibility_version"]) == 0:
            continue
        purpose = {
            "awaiting-review": "review-request",
            "receipt-due": "receipt-request",
        }.get(task["state"], "reassign-notify")
        subject = f"responsibility sync: {task['subject']}"[:160]
        body = (f"ORC task {task['id']} is currently {task['state']} and is"
                f" assigned to you after a responsibility transition."
                f" Inspect the durable task before acting: orc show"
                f" {task['id']}")
        with conn:
            row_id = record_msg(
                conn, task["id"], purpose,
                f"responsibility-sync:{task['id']}:v"
                f"{task['responsibility_version']}",
                owed, subject, body,
                expected_responsibility_version=
                task["responsibility_version"],
            )
        if row_id is not None:
            log(f"OK restored missing responsibility message for"
                f" {task['id']} version {task['responsibility_version']}")
            repaired += 1
    return repaired


def resolve_owed_recipient(conn: sqlite3.Connection, row: sqlite3.Row,
                           owed: str | None = None) -> dict:

    requested = owed_party(row) if owed is None else owed
    purposes = responsibility_purposes(row)
    return resolve_delivered_recipient(conn, row, requested, purposes)


def latest_message_id(conn: sqlite3.Connection, task_id: str, purpose: str,
                      *, at_or_before: int | None = None) -> int:

    sql = "SELECT id FROM task_msg WHERE task_id=? AND purpose=?"
    params: tuple = (task_id, purpose)
    if at_or_before is not None:
        sql += " AND id<=?"
        params += (int(at_or_before),)
    row = conn.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()
    return int(row["id"]) if row is not None else 0


def record_msg(conn: sqlite3.Connection, task_id: str, purpose: str,
               dedup_key: str, target: str, subject: str,
               body: str = "", *,
               expected_latest_id: int | None = None,
               expected_responsibility_version: int | None = None) -> int | None:


    conn.execute(
        "UPDATE dispatch SET last_event=last_event WHERE id=?", (task_id,))
    task = conn.execute(
        "SELECT * FROM dispatch WHERE id=?", (task_id,),
    ).fetchone()
    if (task is not None and purpose in RESPONSIBILITY_PURPOSES
            and expected_responsibility_version is None):
        raise ValueError(
            "responsibility messages require an observed responsibility"
            " generation")
    if (task is not None and purpose in ATTENTION_ROUTE_PURPOSES
            and (expected_responsibility_version is None
                 or expected_latest_id is None)):
        raise ValueError(
            "attention routes require the observed responsibility and"
            " message generations")
    if (task is not None and expected_responsibility_version is not None
            and int(task["responsibility_version"]) !=
            int(expected_responsibility_version)):
        return None
    if (expected_latest_id is not None
            and latest_message_id(conn, task_id, purpose) !=
            int(expected_latest_id)):
        return None
    if purpose in RESPONSIBILITY_PURPOSES:
        if (task is None
                or purpose not in responsibility_purposes(task)
                or target != owed_party(task)):
            return None
    recipient_version = (int(expected_responsibility_version)
                         if task is not None
                         and expected_responsibility_version is not None
                         else int(task["responsibility_version"])
                         if task else 0)
    if (target.startswith("role:")
            and purpose in ROLE_GENERATION_PURPOSES):
        scope = (task["id"] if task is not None and purpose == "goal-review"
                 else (task["parent_id"] if task is not None else ""))
        token = role_target_token(conn, target, scope or "")
        marker = f"role-generation-{token}"
        if f":{marker}:" not in f":{dedup_key}:":
            dedup_key = f"{dedup_key.rstrip(':')}:{marker}:"
    cur = conn.execute(
        "INSERT OR IGNORE INTO task_msg (task_id, dedup_key, purpose, target,"
        " subject, at_ms, body, recipient_version) VALUES (?,?,?,?,?,?,?,?)",
        (task_id, dedup_key, purpose, target, subject, now(),
         body[:MAX_BODY_BYTES], recipient_version))
    if not cur.rowcount:
        return None
    row_id = cur.lastrowid
    if (task is not None and target != "operator"
            and purpose in CURRENT_ACTION_PURPOSES):
        message = conn.execute(
            "SELECT * FROM task_msg WHERE id=?", (row_id,)).fetchone()
        if not message_is_current_responsibility(conn, message, task):
            conn.execute("DELETE FROM task_msg WHERE id=?", (row_id,))
            return None
    return row_id


def refuse_recorded_target(conn: sqlite3.Connection, row_id: int,
                           error: str) -> None:

    conn.execute(
        "UPDATE task_msg SET send_state='invalid-target',last_error=? WHERE id=?",
        (error[:200], row_id),
    )


def bus_send(conn: sqlite3.Connection, msg_row_id: int,
             timeout: int = 45) -> bool:


    message = conn.execute(
        "SELECT * FROM task_msg WHERE id=?",
        (msg_row_id,),
    ).fetchone()
    if message is None:
        return False
    task = conn.execute(
        "SELECT * FROM dispatch WHERE id=?", (message["task_id"],),
    ).fetchone()
    role_scope = (task["id"] if task is not None
                  and message["purpose"] == "goal-review"
                  else (task["parent_id"] if task is not None else ""))
    if message["target"].startswith("role:"):
        if role_holder(conn, message["target"][5:], role_scope) in {"all", "@all"}:
            with conn:
                refuse_recorded_target(
                    conn, msg_row_id,
                    f"{BROADCAST_REFUSAL_PREFIX} for explicit fleet"
                    " announcements; this message needs one recipient",
                )
            return False
    if not message_is_sendable(conn, message, task):
        return False


    target = message["target"]
    subject = message["subject"]
    body = message["body"]
    broadcast = target in {"all", "@all"}
    if not broadcast and target.startswith("role:"):
        if target[5:].endswith(POOL_SUFFIX):


            if not members_available(conn):
                with conn:
                    conn.execute("UPDATE task_msg SET last_error=? WHERE id=?",
                                 ("reviewer pool awaits a current Agent Bus"
                                  " registry", msg_row_id))
                return False
        resolved = resolve_recipient(conn, target, role_scope)
        if resolved.get("deferred"):
            with conn:
                conn.execute("UPDATE task_msg SET last_error=? WHERE id=?",
                             (resolved["deferred"][:200], msg_row_id))
            return False
        canonical = resolved.get("transport_target") or resolved["agent_id"] or target
        if canonical.startswith("role:"):
            with conn:
                conn.execute("UPDATE task_msg SET last_error=? WHERE id=?",
                             (f"unheld role: {target}"[:200], msg_row_id))
            return False
        target = canonical


    broadcast = target in {"all", "@all"}
    if broadcast and message["purpose"] != "announce":
        with conn:
            refuse_recorded_target(
                conn, msg_row_id,
                f"{BROADCAST_REFUSAL_PREFIX} for explicit fleet announcements"
                "; this message needs one recipient",
            )
        return False
    bus = bus_cli()
    send = ["bash", bus, "send", SERVICE_HANDLE, target, subject, body]
    try:
        out = subprocess.run(send, text=True, capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        with conn:
            conn.execute("UPDATE task_msg SET send_state='failed',"
                         " attempts=attempts+1, last_error=? WHERE id=?",
                         (str(e)[:200], msg_row_id))
        return False
    ok = out.returncode == 0
    msg_id = ""
    recipient_agent_ids = []
    for line in out.stdout.splitlines():
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("msg_id"):
            msg_id = payload["msg_id"]
            ids = payload.get("recipient_agent_ids")
            if isinstance(ids, list):
                recipient_agent_ids = [str(aid) for aid in ids if aid]
    error = ""
    if not ok:
        text = (out.stderr or "").strip() or (out.stdout or "").strip()
        error = text.splitlines()[0][:200] if text else "(no transport output)"


    accepted_recipient = (recipient_agent_ids[0]
                          if ok and not broadcast
                          and len(recipient_agent_ids) == 1 else "")
    with conn:
        conn.execute("UPDATE task_msg SET send_state=?, msg_id=?,"
                     " recipient_agent_id=?, attempts=attempts+1,"
                     " last_error=? WHERE id=?",
                     ("accepted" if ok else "failed", msg_id,
                      accepted_recipient, error, msg_row_id))


        if ok and task is not None and message["purpose"] in ATTENTION_ROUTE_PURPOSES:
            current_task = conn.execute(
                "SELECT * FROM dispatch WHERE id=?", (message["task_id"],),
            ).fetchone()
            accepted_message = conn.execute(
                "SELECT * FROM task_msg WHERE id=?", (msg_row_id,),
            ).fetchone()
            newest = conn.execute(
                "SELECT * FROM task_msg WHERE task_id=? AND purpose=?"
                " ORDER BY id DESC LIMIT 1",
                (message["task_id"], message["purpose"]),
            ).fetchone()
            if (accepted_recipient and current_task is not None
                    and accepted_message is not None
                    and newest is not None
                    and newest["target"] == "operator"
                    and newest["send_state"] == "operator-queue"
                    and operator_marker_snapshot_id(newest) == msg_row_id
                    and operator_marker_shape_is_current(
                        conn, current_task, newest)
                    and attention_message_shape_is_current(
                        conn, accepted_message, current_task)):
                conn.execute(
                    "UPDATE task_msg SET send_state='superseded-after-accepted',"
                    " processed='superseded-after-accepted' WHERE id=?",
                    (newest["id"],),
                )
    return ok


def retry_unsent(conn: sqlite3.Connection, log=print) -> tuple[int, int]:


    rows = conn.execute(
        "SELECT m.*,d.responsibility_version AS current_recipient_version"
        " FROM task_msg m"
        " LEFT JOIN dispatch d ON d.id=m.task_id"
        " WHERE m.send_state IN ('recorded','failed')"
        " AND m.attempts < ? AND m.at_ms > ?"


        " AND (d.id IS NULL OR d.state!='closed' OR m.purpose='terminal')"
        " ORDER BY m.at_ms ASC",
        (MAX_SEND_ATTEMPTS, now() - SEND_RETRY_WINDOW_S)).fetchall()
    ok_n = fail_n = 0
    for r in rows:
        task = (conn.execute("SELECT * FROM dispatch WHERE id=?",
                             (r["task_id"],)).fetchone()
                if r["current_recipient_version"] is not None else None)
        if not message_is_sendable(conn, r, task):


            continue
        target = r["target"]
        if bus_send(conn, r["id"]):
            log(f"OK resend {r['purpose']} for task {r['task_id']} ->"
                f" {target} (attempt {r['attempts'] + 1})")
            ok_n += 1
        else:
            row = conn.execute("SELECT attempts, last_error FROM task_msg"
                               " WHERE id=?", (r["id"],)).fetchone()
            log(f"WARN send still failing: {r['purpose']} for task"
                f" {r['task_id']} -> {target} (attempt {row['attempts']}"
                f" of {MAX_SEND_ATTEMPTS}): {row['last_error']}")
            fail_n += 1
    return ok_n, fail_n


def route(conn: sqlite3.Connection, task_id: str, purpose: str, dedup_key: str,
          recipient: str, subject: str, body: str,
          parent_task_id: str = "", *,
          expected_responsibility_version: int | None = None) -> bool:


    if conn.in_transaction:
        raise SystemExit("FAIL  route() called inside an open transaction -"
                         " commit first; sends must never hold the write lock")
    with conn:


        row_id = record_msg(conn, task_id, purpose, dedup_key, recipient, subject,
                            body,
                            expected_responsibility_version=
                            expected_responsibility_version)
    if row_id is None:
        return False
    return bus_send(conn, row_id)


DEAD_LETTER_PARK_S = 1800


def dispatch_undelivered(conn: sqlite3.Connection, task_id: str) -> bool:


    task = conn.execute("SELECT * FROM dispatch WHERE id=?", (task_id,)).fetchone()
    if (task is None or not responsibility_purposes(task)
            or not owed_party(task)
            or owed_party(task).strip().lower() == "operator"):
        return False
    prior = conn.execute(
        "SELECT 1 FROM task_msg WHERE task_id=?"
        " AND purpose IN (%s) LIMIT 1"
        % ",".join("?" for _ in RESPONSIBILITY_PURPOSES),
        (task_id, *RESPONSIBILITY_PURPOSES),
    ).fetchone()


    requires_delivery = (int(task["responsibility_version"]) > 0
                         or prior is not None)
    if not requires_delivery:
        return False
    resolved = resolve_owed_recipient(conn, task)
    return not bool(resolved.get("message_id")
                    and not resolved.get("deferred"))


def dead_letters(conn: sqlite3.Connection) -> list[sqlite3.Row]:


    rows = conn.execute(
        "SELECT m.* FROM task_msg m"
        " LEFT JOIN dispatch d ON d.id=m.task_id"
        " WHERE (d.id IS NULL OR d.state!='closed' OR m.purpose='terminal')"
        " AND m.escalated_to_operator=0"
        " AND (m.send_state='invalid-target' OR"
        " (m.send_state='failed' AND (m.attempts >= ? OR m.at_ms <= ?))"
        " OR (m.send_state='recorded' AND m.at_ms < ?)"
        " OR (m.send_state='accepted' AND m.recipient_agent_id=''"
        " AND m.purpose IN ('dispatch','author-request','reassign-notify',"
        " 'review-request',"
        " 'findings','receipt-request') AND m.at_ms < ?))"
        " ORDER BY m.at_ms ASC",
        (MAX_SEND_ATTEMPTS, now() - SEND_RETRY_WINDOW_S,
         now() - DEAD_LETTER_PARK_S,
         now() - DEAD_LETTER_PARK_S)).fetchall()
    actionable = []
    for msg in rows:
        task = conn.execute("SELECT * FROM dispatch WHERE id=?",
                            (msg["task_id"],)).fetchone()
        if not message_is_sendable(conn, msg, task):
            continue
        if (msg["send_state"] == "accepted"
                and message_recipient_agent_id(
                    msg["msg_id"], msg["recipient_agent_id"], msg["target"])):
            continue
        actionable.append(msg)
    return actionable


def escalate_dead_letters(conn: sqlite3.Connection, log=print) -> int:


    raised = 0
    for r in dead_letters(conn):
        task = conn.execute("SELECT * FROM dispatch WHERE id=?",
                            (r["task_id"],)).fetchone()
        task_subject = task["subject"] if task else "(task not found)"
        err_text = r["last_error"] or "No transport error was recorded."
        if task is not None and not is_closed(task):
            with conn:
                conn.execute("UPDATE task_msg SET escalated_to_operator=1"
                             " WHERE id=?", (r["id"],))
                record(
                    conn, task["id"], "auto-note",
                    f"operator-attention: {r['purpose']} message to"
                    f" {r['target']} could not be delivered after"
                    f" {r['attempts']} attempt(s): {err_text}",
                )
            log(f"OK dead letter attached to original task {task['id']}:"
                f" {r['purpose']} -> {r['target']}")
            raised += 1
            continue
        with conn:
            insert_task(
                conn, recipient="operator",
                subject=f"undeliverable fleet message: {r['purpose']} for"
                        f" task {r['task_id']}"[:180],
                body=(f"A coordination message could not be delivered and now"
                      f" requires operator attention.\n\n"
                      f"Task {r['task_id']}: {task_subject}\n"
                      f"Purpose: {r['purpose']}\nTarget: {r['target']}\n"
                      f"Attempts: {r['attempts']} (state {r['send_state']})\n"
                      f"Last recorded error: {err_text}\n\n"
                      f"Fix is usually one of: make the target name exactly"
                      f" one receiver, grant the missing role (orc role grant),"
                      f" correct the task's recipient, or"
                      f" deliver the content manually. Original message body"
                      f" below.\n---\n{r['body']}"),
                check_cmd="true", after_s=parse_after("4h"))
            conn.execute("UPDATE task_msg SET escalated_to_operator=1"
                         " WHERE id=?", (r["id"],))
        log(f"OK dead letter raised to the operator: {r['purpose']} for"
            f" task {r['task_id']} -> {r['target']}")
        raised += 1
    return raised


MAX_RECEIPT_POLLS = 12


def bus_inbox_state(msg_id: str, agent_id: str) -> str | None:


    rows = _agent_bus_rows(
        "SELECT state FROM inbox WHERE agent_id=? AND msg_id=?",
        (agent_id, msg_id))
    return str(rows[0][0]) if rows else None


def poll_receipts(conn: sqlite3.Connection, limit: int = 20,
                  timeout: int = 20) -> int:


    bus = bus_cli()
    rows = conn.execute(
        "SELECT * FROM task_msg WHERE send_state='accepted' AND processed=''"
        " AND msg_id != '' AND poll_count < ? ORDER BY at_ms ASC LIMIT ?",
        (MAX_RECEIPT_POLLS, limit)).fetchall()
    updated = 0
    for r in rows:
        try:
            out = subprocess.run(["bash", bus, "delivery", SERVICE_HANDLE, r["msg_id"]],
                                 text=True, capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            continue
        delivered = r["delivered"]
        processed = ""
        recipient_ids: set[str] = set()
        for line in out.stdout.splitlines():
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            state = str(payload.get("state", payload.get("delivery_state", "")))
            if payload.get("delivered") is True or state.startswith("delivered"):
                delivered = 1
            proc = payload.get("processed", payload.get("processed_status"))
            if proc in (True, "ok", "processed-ok"):
                processed = "ok"
            elif proc not in (None, "", False):
                processed = "seen"


            for rcpt in payload.get("recipients", []):
                if not isinstance(rcpt, dict):
                    continue
                if rcpt.get("recipient_agent_id"):
                    recipient_ids.add(str(rcpt["recipient_agent_id"]))
                if rcpt.get("delivered_ms"):
                    delivered = 1
                rproc = rcpt.get("processed_status")
                if rproc in (True, "ok", "processed-ok"):
                    processed = "ok"
                elif rproc not in (None, "", False) and processed != "ok":
                    processed = "seen"
        polls = r["poll_count"] + 1
        if not processed and polls >= MAX_RECEIPT_POLLS:
            processed = "unknown"
        with conn:
            actual = (next(iter(recipient_ids))
                      if r["target"] not in {"all", "@all"}
                      and len(recipient_ids) == 1
                      else r["recipient_agent_id"])
            if r["target"] in {"all", "@all"}:
                actual = ""
            conn.execute("UPDATE task_msg SET delivered=?, processed=?,"
                         " poll_count=?,recipient_agent_id=? WHERE id=?",
                         (delivered, processed, polls, actual, r["id"]))
        updated += 1
    return updated


GUARD_TRUE, GUARD_FALSE, GUARD_UNKNOWN = "true", "false", "unknown"


def run_guard(cmd: str, timeout: int = CHECK_TIMEOUT_S) -> tuple[str, str]:


    if not cmd:
        return GUARD_FALSE, "(no command)"
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                             timeout=timeout)
    except subprocess.TimeoutExpired:
        return GUARD_UNKNOWN, f"timed out after {timeout}s"
    except OSError as e:
        return GUARD_UNKNOWN, str(e)
    text = (out.stdout or "").strip() or (out.stderr or "").strip()
    first = text.splitlines()[0][:160] if text else "(no output)"
    return (GUARD_TRUE if out.returncode == 0 else GUARD_FALSE), first


def run_progress(cmd: str, timeout: int = CHECK_TIMEOUT_S) -> tuple[str, str]:


    if not cmd:
        return GUARD_FALSE, ""
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                             timeout=timeout)
    except subprocess.TimeoutExpired:
        return GUARD_UNKNOWN, ""
    except OSError:
        return GUARD_UNKNOWN, ""
    if out.returncode != 0:
        return GUARD_UNKNOWN, ""
    return GUARD_TRUE, content_hash(out.stdout or "")


def children(conn: sqlite3.Connection, parent_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM dispatch WHERE parent_id=?",
                        (parent_id,)).fetchall()


def rollup(conn: sqlite3.Connection, parent_row: sqlite3.Row) -> str | None:


    kids = children(conn, parent_row["id"])
    open_kids = [k for k in kids
                 if k["state"] not in workflow_spec(row_workflow(k))["terminal"]]
    if parent_row["state"] == "running" and kids and not open_kids:
        with conn:
            conn.execute("UPDATE dispatch SET state=?, ask_flag=0,"
                         " last_event=? WHERE id=?",
                         (step("parent", "running", "children-closed"), now(),
                          parent_row["id"]))
            record(conn, parent_row["id"], "children-closed",
                   f"{len(kids)} children all closed; goal back to review")
        return "children-closed"
    if parent_row["state"] == "ready-to-close" and open_kids:
        with conn:
            conn.execute("UPDATE dispatch SET state=?, ask_flag=0,"
                         " last_event=? WHERE id=?",
                         (step("parent", "ready-to-close", "child-opened"), now(),
                          parent_row["id"]))
            record(conn, parent_row["id"], "child-opened",
                   ",".join(k["id"] for k in open_kids))
        return "child-opened"
    return None


def verify_relations() -> list[str]:
    problems = []
    for wf, spec in WORKFLOWS.items():
        states, terminal = spec["states"], spec["terminal"]
        for (state, event), target in spec["transitions"].items():
            for name in (state, target):
                if name not in states and name != "_new":
                    problems.append(f"{wf}: transition table names unknown state {name!r}")


            if event in spec["mechanical"] and target in spec["grants_permission"]:
                problems.append(
                    f"{wf}: mechanical event {event!r} targets permission-granting"
                    f" state {target!r} - the machine may never emit a permission")


            if event in NO_STATE_CHANGE_EVENTS and target != state:
                problems.append(
                    f"{wf}: {event!r} moves {state!r} -> {target!r}; this event may"
                    f" only ever be a self loop - an unblocking action that also"
                    f" advanced the state would be the machine granting progress")
        for state in states:
            if state in terminal:
                continue
            reachable, frontier = {state}, [state]
            while frontier:
                here = frontier.pop()
                for (s, _e), t in spec["transitions"].items():
                    if s == here and t not in reachable:
                        reachable.add(t)
                        frontier.append(t)
            if not any(t in reachable for t in terminal):
                problems.append(f"{wf}: state {state!r} cannot reach a terminal state - dead end")
        if "grants_permission" not in spec or "mechanical" not in spec:
            problems.append(f"{wf}: workflow spec is missing its safety declarations")
    return problems


def verify_store(conn: sqlite3.Connection) -> list[str]:
    problems = []
    sp_cols = {r[1] for r in conn.execute("PRAGMA table_info(state_pair)")}
    if "workflow" not in sp_cols:


        problems.append("state_pair predates the workflow column - run any"
                        " normal ledger command once to migrate the mirror")
        have_pairs = None
    else:
        have_pairs = {(r["workflow"], r["from_state"], r["to_state"])
                      for r in conn.execute("SELECT * FROM state_pair")}
    if have_pairs is not None:
        want_pairs = {(wf, s, t) for wf, spec in WORKFLOWS.items()
                      for (s, _e), t in spec["transitions"].items() if s != "_new"}
        for missing in sorted(want_pairs - have_pairs):
            problems.append(f"state_pair is missing the legal move "
                            f"[{missing[0]}] {missing[1]} -> {missing[2]}")
        for extra in sorted(have_pairs - want_pairs):
            problems.append(f"state_pair allows [{extra[0]}] {extra[1]} -> {extra[2]},"
                            f" which the transition tables do not")
    trigger = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'"
        " AND name='dispatch_state_legal'").fetchone()[0]
    if not trigger:
        problems.append("the dispatch_state_legal trigger is absent - illegal state "
                        "writes would only be detected afterwards, not refused")

    rows = conn.execute("SELECT * FROM dispatch").fetchall()
    for row in rows:
        wf = row_workflow(row)
        transitions = workflow_spec(wf)["transitions"]
        events = conn.execute(
            "SELECT kind FROM event WHERE dispatch_id=? ORDER BY at_ms ASC, id ASC",
            (row["id"],)).fetchall()
        state = "_new"
        for ev in events:
            key = (state, event_kind(ev["kind"]))
            if key not in transitions:
                problems.append(f"{row['id']} log contains an illegal move: "
                                f"{event_kind(ev['kind'])!r} from {state!r} [{wf}]")
                state = None
                break
            state = transitions[key]
        if state is None:
            continue
        if not events:
            problems.append(f"{row['id']} has no events at all - it cannot be replayed")
        elif state != row["state"]:
            problems.append(f"{row['id']} stored state {row['state']!r} but the log "
                            f"replays to {state!r}")

    edges = conn.execute("SELECT * FROM edge").fetchall()
    ids = {r["id"] for r in rows}
    by_id = {r["id"]: r for r in rows}
    for e in edges:
        for side in ("src", "dst"):
            if e[side] not in ids:
                problems.append(f"edge {e['src']} {e['kind']} {e['dst']} names a "
                                f"dispatch that does not exist ({e[side]})")
    for e in edges:
        if e["kind"] in ("supersedes", "reassigned-to"):
            target = by_id.get(e["dst"])
            if target is not None and \
                    target["state"] not in workflow_spec(row_workflow(target))["terminal"]:
                problems.append(f"{e['src']} {e['kind']} {e['dst']} but {e['dst']} is "
                                f"still {target['state']} - a superseded node must be closed")
    blocks: dict[str, list[str]] = {}
    for e in edges:
        if e["kind"] == "blocks":
            blocks.setdefault(e["src"], []).append(e["dst"])
    colour: dict[str, int] = {}

    def has_cycle(node: str) -> bool:
        colour[node] = 1
        for nxt in blocks.get(node, []):
            if colour.get(nxt) == 1 or (colour.get(nxt, 0) == 0 and has_cycle(nxt)):
                return True
        colour[node] = 2
        return False

    for node in list(blocks):
        if colour.get(node, 0) == 0 and has_cycle(node):
            problems.append(f"the blocks graph has a cycle reachable from {node} - "
                            f"nothing in that cycle can ever start")
            break


    needs: dict[str, list[str]] = {}
    for e in edges:
        if e["kind"] == "needs":
            needs.setdefault(e["src"], []).append(e["dst"])
    needs_colour: dict[str, int] = {}

    def needs_has_cycle(node: str) -> bool:
        needs_colour[node] = 1
        for nxt in needs.get(node, []):
            if needs_colour.get(nxt) == 1 or \
                    (needs_colour.get(nxt, 0) == 0 and needs_has_cycle(nxt)):
                return True
        needs_colour[node] = 2
        return False

    for node in list(needs):
        if needs_colour.get(node, 0) == 0 and needs_has_cycle(node):
            problems.append(f"the needs graph has a cycle reachable from {node} - "
                            f"every task in that loop waits on the one behind it")
            break

    for row in rows:
        if row["state"] in workflow_spec(row_workflow(row))["terminal"]:
            continue


        if not row["check_after"]:
            problems.append(f"{row['id']} is {row['state']} with no next check"
                            f" timestamp - nothing is scheduled to look at it again")
        if row["state"] != WAITING_STATE:
            continue
        preds = [by_id.get(pid) for pid in needs.get(row["id"], [])]
        if not [p for p in preds if p is not None and not is_closed(p)]:
            problems.append(
                f"{row['id']} is {WAITING_STATE} with no predecessor still open -"
                f" stuck-advance: the tick should have opened it, so either the"
                f" tick is not running or its advance pass is broken")


    for row in rows:
        pid = row["parent_id"] if "parent_id" in row.keys() else ""
        if not pid:
            continue
        parent = by_id.get(pid)
        if parent is None:
            problems.append(f"{row['id']} names parent {pid} which does not exist")
        elif row_workflow(parent) != "parent":
            problems.append(f"{row['id']} parent {pid} is a {row_workflow(parent)}"
                            f" task, not a parent goal")
        seen, cursor = {row["id"]}, pid
        while cursor:
            if cursor in seen:
                problems.append(f"parent chain cycle reachable from {row['id']}")
                break
            seen.add(cursor)
            up = by_id.get(cursor)
            cursor = up["parent_id"] if up and "parent_id" in up.keys() else ""
    return problems
