#!/usr/bin/env python3
"""Run the fleet work graph and its configured integrations. Zero model calls."""


from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
import runtime_config as cfg
import runtime_paths as nw_paths
import pane_sense
import tmux_runtime
import workplane as wp

try:
    PROCESS_STARTED_NS = Path("/proc/self").stat().st_mtime_ns
except OSError:
    PROCESS_STARTED_NS = None


def load_script(filename: str, modname: str):
    spec = importlib.util.spec_from_file_location(modname, SCRIPT_DIR / filename)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def log(msg: str) -> None:
    print(msg, flush=True)


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def state_dir() -> Path:
    return nw_paths.orchestrator_state_dir()


def _open(args: argparse.Namespace) -> str:


    body = ""
    if getattr(args, "body_file", None):
        body = Path(args.body_file).read_text(encoding="utf-8")[:wp.MAX_BODY_BYTES]
    elif getattr(args, "body", None):
        body = args.body[:wp.MAX_BODY_BYTES]
    if args.to.strip().lower() == "operator" and not body:
        raise SystemExit(
            "FAIL  an item owed by the operator needs --body: one self-contained\n"
            "      explanation he can act on without reading anything else."
        )
    workflow = getattr(args, "workflow", "dispatch")
    if workflow == "dispatch" and not args.check and not args.no_check:
        raise SystemExit(
            "FAIL  --check is required: give the command that answers 'did this move?'\n"
            "      Pass --no-check only when that is genuinely true, and say why in --body."
        )
    if workflow == "pr" and not (args.owner and args.reviewer):
        raise SystemExit("FAIL  a pr task needs --owner and --reviewer "
                         "(one task, one owner, one reviewer - the schema enforces it)")
    if (workflow == "pr" and getattr(args, "_record_dispatch", False)
            and args.to != args.owner):
        raise SystemExit(
            "FAIL  a combined PR dispatch must send directly to its owner:"
            " --to and --owner must be identical. Use `orc task open` when the"
            " owner learned the task out of band, and `orc task announce --to`"
            " for any separate informational notice."
        )
    if workflow == "pr" and not getattr(args, "deadline", None):


        args.deadline = "2h"
    if getattr(args, "breaker", "") and not args.check:
        raise SystemExit("FAIL  --breaker needs --check: the breaker fires on"
                         " consecutive FAILING checks, so without a check nothing"
                         " could ever fire it")
    deadline = getattr(args, "deadline", None)


    requester_seat = wp.caller_seat_id()
    if getattr(args, "await_notify", False):
        if not requester_seat:
            raise SystemExit(
                "FAIL  --await needs exactly one active, unexpired Agent Bus"
                " identity for this host and tmux pane (or an explicit"
                " ORC_SEAT_ID) - a notification with no unique address is"
                " a silent drop waiting")
        addressable = wp.agent_bus_identity_addressable(requester_seat)
        if addressable is not True:
            detail = ("the local Agent Bus database could not be read"
                      if addressable is None else
                      "that identity is not an active, unexpired Agent Bus"
                      " receiver")
            raise SystemExit(
                f"FAIL  --await requester {requester_seat!r} was not"
                f" verified: {detail}. Rejoin this pane before recording a"
                " completion notification")
    conn = wp.connect_writable()
    with conn:
        did = wp.insert_task(
            conn, recipient=args.to, subject=args.subject, body=body,
            check_cmd=args.check or "", links=",".join(args.link or []),
            after_s=wp.parse_after(args.after), workflow=workflow,
            parent_id=getattr(args, "parent", "") or "",
            repo=getattr(args, "repo", "") or "",
            owner_seat=getattr(args, "owner", "") or "",
            reviewer_seat=getattr(args, "reviewer", "") or "",
            ready_cmd=getattr(args, "ready_cmd", "") or "",
            done_cmd=getattr(args, "done_cmd", "") or "",
            needs=getattr(args, "needs", None) or (),
            deadline_s=wp.parse_after(deadline) if deadline else 0,
            breaker_cmd=getattr(args, "breaker", "") or "",
            requester_seat=requester_seat,
            await_notify=1 if getattr(args, "await_notify", False) else 0)
        if getattr(args, "_record_dispatch", False):
            opened = wp.fetch(conn, did)
            if opened["state"] == wp.WAITING_STATE:
                conn.execute("UPDATE dispatch SET deferred_dispatch=1 WHERE id=?",
                             (did,))
            else:
                subject, dispatch_body = dispatch_message(opened)
                wp.record_msg(conn, did, wp.dispatch_message_purpose(opened),
                              f"dispatch:{did}",
                              opened["recipient"], subject, dispatch_body,
                              expected_responsibility_version=
                              opened["responsibility_version"])
    row = wp.fetch(conn, did)
    if row["state"] == wp.WAITING_STATE:
        waiting_on = wp.open_predecessors(conn, did)
        print(f"OK    dispatch {did} opened waiting on"
              f" {len(waiting_on)} unfinished predecessor(s):"
              f" {', '.join(p['id'] for p in waiting_on)}")
        print(f"NOTE  nobody is asked to work on it yet. The tick opens it and"
              f" notifies {args.to} the moment the last predecessor closes,"
              f" then checks in {args.after}.")
    else:
        print(f"OK    dispatch {did} open, owed by {args.to}, check in {args.after}")
    if workflow != "dispatch":
        print(f"NOTE  workflow={workflow} state={row['state']}")
    if not args.check and workflow == "dispatch":
        print("NOTE  no check command recorded - this node can only be moved by its owner")
    for flag, cmd in (("--check", args.check or ""),
                      ("--done-cmd", getattr(args, "done_cmd", "") or "")):
        label = weak_check_label(cmd)
        if label:


            print(f"WARN  {flag} is a weak check ({label}): it proves that"
                  f" something exists or how many matches there are, not that"
                  f" the deliverable works. A count satisfied by a neighbouring"
                  f" change can close an unfinished task. Probe the artifact itself: field"
                  f" present, file exists, behaviour observable.")
    if deadline:
        print(f"NOTE  deadline in {deadline}: once it passes, the explicit"
              f" independent supervisor is notified once every"
              f" {wp.DEADLINE_COOLDOWN_S // 3600}h until it closes")
    if getattr(args, "breaker", ""):
        print(f"NOTE  breaker armed: {wp.BREAKER_FAIL_STREAK} consecutive failing"
              f" checks run it once (at most once per"
              f" {wp.BREAKER_COOLDOWN_S // 3600}h); it unblocks, it never changes"
              f" this task's state")
    return did


def cmd_open(args: argparse.Namespace) -> int:
    _open(args)
    return 0


def dispatch_message(row: sqlite3.Row) -> tuple[str, str]:


    body = row["body"] or row["subject"]
    return (f"dispatch {row['id']}: {row['subject']}"[:180],
            f"{body}\n\nTask {row['id']} in the fleet work graph; "
            f"ack with: {wp.orc_task_command(row['id'], 'ack')}")


def send_dispatch_message(conn, row: sqlite3.Row) -> bool:
    subject, body = dispatch_message(row)
    return wp.route(conn, row["id"], wp.dispatch_message_purpose(row),
                    f"dispatch:{row['id']}",
                    row["recipient"], subject, body, row["parent_id"],
                    expected_responsibility_version=
                    row["responsibility_version"])


def cmd_dispatch(args: argparse.Namespace) -> int:


    args._record_dispatch = True
    did = _open(args)
    conn = wp.connect_writable()
    row = wp.fetch(conn, did)
    if row["state"] == wp.WAITING_STATE:


        print("NOTE  bus message HELD until the predecessors close; the tick"
              " sends it then, to this same recipient")
        return 0
    msg = conn.execute(
        "SELECT id,subject,body FROM task_msg WHERE task_id=?"
        " AND purpose=? AND recipient_version=? ORDER BY id DESC LIMIT 1",
        (row["id"], wp.dispatch_message_purpose(row),
         row["responsibility_version"]),
    ).fetchone()
    sent = wp.bus_send(conn, msg["id"])
    if sent:
        print("OK    bus send accepted")
        timeout_s = getattr(args, "handshake_timeout", HANDSHAKE_TIMEOUT_S)
        if not getattr(args, "no_handshake", False) and timeout_s > 0 \
                and os.environ.get("NW_ORC_HANDSHAKE", "1") != "0":
            dispatch_handshake(conn, row["id"], timeout_s=timeout_s)
    else:
        msg = conn.execute("SELECT send_state, last_error, attempts FROM task_msg"
                           " WHERE task_id=? AND purpose=?"
                           " AND recipient_version=?"
                           " ORDER BY id DESC LIMIT 1",
                           (row["id"], wp.dispatch_message_purpose(row),
                            row["responsibility_version"]),).fetchone()
        detail = (msg["last_error"] or "no resolvable bus target") if msg else ""
        if msg and msg["send_state"] == "invalid-target":
            print(f"WARN  bus send refused before transport ({detail}); no"
                  f" retry will guess a recipient - the next engine tick"
                  f" marks this original task for the operator, and `orc board` shows"
                  f" INVALID-TARGET")
        else:
            print(f"WARN  bus send NOT delivered ({detail}); the row is recorded"
                  f" and the engine retries every tick up to"
                  f" {wp.MAX_SEND_ATTEMPTS} attempts - check `orc board` for"
                  f" the SEND-FAILED flag")
    return 0


HANDSHAKE_INTERVAL_S = 5
HANDSHAKE_TIMEOUT_S = 90


def handshake_evidence(conn, task_id: str):


    row = wp.fetch(conn, task_id)
    workflow = wp.row_workflow(row)
    if row["state"] in wp.workflow_spec(workflow)["terminal"]:
        return ("state", f"task state is {row['state']}")
    context = wp.continuation_context(conn, row)
    if (context is not None
            and wp.current_continuation_voice(
                conn, row, context, kinds=("ack",)) is not None):
        return ("state", "the current responsible seat acknowledged the task")


    current = wp.resolve_owed_recipient(conn, row)
    msg_id = current.get("message_id", "")
    actual = current.get("recipient_agent_id", "")
    if msg_id and actual and not current.get("deferred"):
        inbox = wp.bus_inbox_state(msg_id, actual)
        if inbox in ("presented", "done"):
            return ("presented", f"dispatch message presented to the seat"
                                  f" (inbox {inbox}); acknowledgment still"
                                  f" requires an explicit seat action")
    return None


def _registered_pane(member: dict, panes):
    """Use the identity's exact pane; a split-window neighbour is not its agent."""
    if member.get("terminal_presence") in {"unknown", "absent"}:
        return None
    pane_id = member.get("pane_id")
    if pane_id:
        return next((pane for pane in panes if pane[0] == pane_id), None)
    window = member.get("window") or wp.window_from_tmux_field(member.get("tmux", ""))
    return pane_sense.pane_for_window(window, panes) if window is not None else None


def _pane_probe_for(window: str | None, *, member: dict | None = None):


    def probe():
        if window is None:
            return None
        try:
            pane = _registered_pane(member or {"window": window},
                                    pane_sense.agent_panes())
            if pane is None:
                return None
            return pane_sense.detect_busy(pane_sense.capture(pane[0]))
        except RuntimeError:
            return None
    return probe


def _handshake_note(conn, task_id: str, note: str) -> None:
    row = wp.fetch(conn, task_id)
    if row["state"] in wp.workflow_spec(wp.row_workflow(row))["terminal"]:
        return
    with conn:


        wp.record(conn, task_id, "auto-note", note)


def _handshake_key(conn, row) -> tuple:
    context = wp.continuation_context(conn, row)
    return (
        int(row["responsibility_version"]),
        context["generation"] if context is not None else "",
        context.get("agent_id") if context is not None else "",
    )


def _handshake_note_if_current(conn, task_id: str, expected: tuple,
                               note: str) -> bool:

    with conn:
        locked = conn.execute(
            "UPDATE dispatch SET last_event=last_event WHERE id=?"
            " AND responsibility_version=?", (task_id, expected[0]),
        )
        if locked.rowcount != 1:
            return False
        current = wp.fetch(conn, task_id)
        if _handshake_key(conn, current) != expected:
            return False
        wp.record(conn, task_id, "auto-note", note)
    return True


def dispatch_handshake(conn, task_id: str, timeout_s: int = HANDSHAKE_TIMEOUT_S,
                       interval_s: int = HANDSHAKE_INTERVAL_S,
                       evidence=handshake_evidence, sleep=None) -> str:


    import time as _time
    sleep = sleep or _time.sleep
    row = wp.fetch(conn, task_id)
    start_key = _handshake_key(conn, row)
    waited = 0
    while True:
        current_row = wp.fetch(conn, task_id)
        if _handshake_key(conn, current_row) != start_key:
            _handshake_note(
                conn, task_id,
                "handshake: stopped because responsibility changed while"
                " the original handshake was waiting",
            )
            log("NOTE  handshake stopped: responsibility changed; the old"
                " pane will not be observed or touched")
            return "superseded"
        ev = evidence(conn, task_id)
        if ev:
            kind, detail = ev
            if not _handshake_note_if_current(
                    conn, task_id, start_key,
                    f"handshake: established after {waited}s - {detail}"):
                _handshake_note(
                    conn, task_id,
                    "handshake: stopped because responsibility changed while"
                    " evidence was being checked",
                )
                log("NOTE  handshake stopped: responsibility changed while"
                    " evidence was being checked")
                return "superseded"
            log(f"OK    handshake established after {waited}s: {detail}")
            return "established"


        current_row = wp.fetch(conn, task_id)
        if _handshake_key(conn, current_row) != start_key:
            _handshake_note(
                conn, task_id,
                "handshake: stopped because responsibility changed while"
                " the original handshake was waiting",
            )
            log("NOTE  handshake stopped: responsibility changed; the old"
                " pane will not be observed or touched")
            return "superseded"
        if waited >= timeout_s:
            if not _handshake_note_if_current(
                    conn, task_id, start_key,
                    f"handshake: no reaction after {timeout_s}s -"
                    f" the tick ladder takes over"):
                log("NOTE  handshake stopped: responsibility changed before"
                    " the timeout was recorded")
                return "superseded"
            log(f"WARN  handshake: no reaction from {row['recipient']} after"
                f" {timeout_s}s; the 5-minute tick ladder"
                f" takes over - check `orc board` if this repeats")
            return "timeout"
        sleep(interval_s)
        waited += interval_s


def cmd_handshake(args: argparse.Namespace) -> int:
    conn = wp.connect_writable()
    row = wp.fetch(conn, args.id)
    result = dispatch_handshake(conn, row["id"], timeout_s=args.timeout)
    return 1 if result == "timeout" else 0


def cmd_verdict(args: argparse.Namespace) -> int:
    conn = wp.connect_writable()
    row = wp.fetch(conn, args.id)
    caller = wp.require_owed_caller(conn, row, "verdict")
    event = f"verdict-{args.verdict}"
    if not (args.note or "").strip():


        raise SystemExit(
            "FAIL  a verdict needs --note: your findings, or a POINTER to them"
            " (a PR review comment link/id is enough - never duplicate)."
            " An empty verdict is indistinguishable from a rubber stamp,"
            " and the fleet has already caught one.")
    with conn:
        row, context = wp.lock_continuation_caller(
            conn, row, caller, "verdict", work_only=True)
        new_state = wp.step_row(row, event)


        req = conn.execute(
            "SELECT MAX(at_ms) FROM task_msg WHERE task_id=? AND"
            " purpose='review-request' AND target=?",
            (row["id"], row["reviewer_seat"])).fetchone()[0]
        base = (req or wp.last_event_at(conn, row["id"], "pr-ready")
                or row["created_ms"])
        took = wp.human_age(max(0, wp.now() - int(base)))

        n_this = conn.execute(
            "SELECT COUNT(*) FROM event WHERE dispatch_id=? AND kind=?",
            (row["id"], event)).fetchone()[0] + 1
        if args.verdict == "blockers":
            conn.execute("UPDATE dispatch SET state=?, round=round+1, ask_flag=0,"
                         " last_event=?, check_after=? WHERE id=? AND state=?"
                         " AND responsibility_version=?",
                         (new_state, wp.now(), wp.now() + wp.parse_after("2h"),
                          row["id"], row["state"],
                          row["responsibility_version"]))
        else:
            conn.execute("UPDATE dispatch SET state=?, ask_flag=0, last_event=?,"
                         " check_after=? WHERE id=? AND state=?"
                         " AND responsibility_version=?",
                         (new_state, wp.now(), wp.now() + wp.parse_after("2h"),
                          row["id"], row["state"],
                          row["responsibility_version"]))
        wp.record(conn, row["id"], event,
                  f"[{took} from request to verdict] " + args.note.strip()
                  + (("; links: " + ",".join(args.link)) if args.link else ""),
                  actor=caller,
                  continuation_generation=context["generation"])


    row = wp.fetch(conn, row["id"])
    if args.verdict == "blockers" and row["owner_seat"]:
        wp.route(conn, row["id"], "findings",
                 f"findings:{row['id']}:n{n_this}", row["owner_seat"],
                 f"review blockers on {row['id']}: {row['subject']}"[:180],
                 f"Blocking findings recorded: {args.note or ','.join(args.link or []) or '(see review threads)'}."
                 f" Fix and push; the head-moved guard re-routes to review.",
                 row["parent_id"], expected_responsibility_version=
                 row["responsibility_version"])
    if args.verdict == "clean" and row["owner_seat"]:
        wp.route(conn, row["id"], "receipt-request",
                 f"receipt-req:{row['id']}:n{n_this}", row["owner_seat"],
                 f"receipt due on {row['id']}: {row['subject']}"[:180],
                 "Review verdict clean. "
                 + cfg.get("authority.receipt_instructions",
                           "Post the review evidence required by your project policy:")
                 + f" {wp.orc_command('review', 'receipt', row['id'])} --body-file <f>",
                 row["parent_id"], expected_responsibility_version=
                 row["responsibility_version"])
    print(f"OK    {row['id']} verdict {args.verdict} -> {new_state}")
    return 0


def cmd_receipt(args: argparse.Namespace) -> int:
    conn = wp.connect_writable()
    row = wp.fetch(conn, args.id)
    caller = wp.require_owed_caller(conn, row, "receipt")
    body = Path(args.body_file).read_text(encoding="utf-8")[:wp.MAX_BODY_BYTES] \
        if args.body_file else (args.body or "")
    if not body.strip():
        raise SystemExit("FAIL  a receipt needs its stored body - that is the thing "
                         "the verifying human reads")
    notify_row = None
    with conn:
        row, context = wp.lock_continuation_caller(
            conn, row, caller, "receipt", work_only=True)
        key_role = wp.merge_key_role(row["repo"])
        new_state = wp.step_row(row, "receipt")
        n_this = conn.execute(
            "SELECT COUNT(*) FROM event WHERE dispatch_id=? AND kind='receipt'",
            (row["id"],)).fetchone()[0] + 1
        conn.execute("UPDATE dispatch SET state=?, receipt_body=?, ask_flag=0,"
                     " last_event=? WHERE id=? AND state=?"
                     " AND responsibility_version=?",
                     (new_state, body, wp.now(), row["id"], row["state"],
                      row["responsibility_version"]))
        receipt_event_id = wp.record(
            conn, row["id"], "receipt", f"merge key: {key_role}",
            actor=caller, continuation_generation=context["generation"])
        row = wp.fetch(conn, row["id"])
        if key_role != wp.OPERATOR_ROLE:
            notify_subject = f"receipt for your verification: {row['id']}"[:180]
            notify_body = (
                f"Owner receipt on {row['id']} ({row['subject']}). Verify it"
                f" YOURSELF before using your merge key - this message is"
                f" evidence, not permission.\n\n{body}"
            )
            expected_notice_id = wp.latest_message_id(
                conn, row["id"], "receipt-to-keyholder")
            notify_row = wp.record_msg(
                conn, row["id"], "receipt-to-keyholder",
                f"receipt-key:{row['id']}:n{n_this}:"
                f"attention-event={receipt_event_id}", f"role:{key_role}",
                notify_subject, notify_body,
                expected_latest_id=expected_notice_id,
                expected_responsibility_version=
                row["responsibility_version"],
            )


    if key_role != wp.OPERATOR_ROLE:


        if notify_row is not None:
            wp.bus_send(conn, notify_row)
    print(f"OK    {row['id']} receipt stored -> {new_state}; merge key role: {key_role}")
    return 0


def cmd_blocked(args: argparse.Namespace) -> int:


    if not (getattr(args, "note", "") or "").strip():


        raise SystemExit(
            "FAIL  blocked needs --note stating WHAT needs deciding and BY"
            " WHOM. A bare marker parks the task without telling the human"
            " what they are being asked.")
    conn = wp.connect_writable()
    row = wp.fetch(conn, args.id)
    caller, _context = wp.require_continuation_caller(conn, row, "blocked")
    with conn:
        row, context = wp.lock_continuation_caller(
            conn, row, caller, "blocked")
        wp.step_row(row, "note")


        stamp = wp.now()
        changed = conn.execute(
            "UPDATE dispatch SET ask_flag=?, last_event=?"
            " WHERE id=? AND responsibility_version=? AND state=?",
            (stamp, stamp, row["id"], row["responsibility_version"],
             row["state"]))
        if changed.rowcount != 1:
            raise SystemExit(
                "FAIL  responsibility changed while blocked was being"
                " recorded; inspect the task again and record the question"
                " only if it is still yours")
        wp.record(conn, row["id"], "note",
                  f"{wp.ASK_NOTE_PREFIX}{args.note.strip()}", actor=caller,
                  continuation_generation=context["generation"])
    print(f"OK    {row['id']} marked blocked on a human: its explicit independent"
          f" supervisor is alerted once, or the original task enters the"
          f" operator brief; the ladder holds quiet for"
          f" {wp.ASK_FLAG_TTL_S // 3600}h or until the task moves")
    return 0


def cmd_reassign(args: argparse.Namespace) -> int:


    conn = wp.connect_writable()
    row = wp.fetch(conn, args.id)
    wp.step_row(row, "note")
    changes = []
    sets, vals = [], []
    for field, col in (("to", "recipient"), ("owner", "owner_seat"),
                       ("reviewer", "reviewer_seat")):
        val = getattr(args, field, None)
        if val:
            if val.strip().lower() in {"all", "@all"}:
                raise SystemExit(
                    f"FAIL  {field} cannot be all/@all: task responsibility"
                    " needs exactly one seat; use `orc task announce` for"
                    " broadcasts"
                )
            if val == row[col]:
                continue
            changes.append(f"{col}: {row[col] or '(empty)'} -> {val}")
            sets.append(f"{col}=?")
            vals.append(val)
    if not sets:
        raise SystemExit("FAIL  nothing to change: give --to, --owner and/or"
                         " --reviewer")
    reviewer_arg = getattr(args, "reviewer", None)
    pool_member = None
    pool_name = ""
    if (reviewer_arg and reviewer_arg.startswith("role:")
            and reviewer_arg.endswith(wp.POOL_SUFFIX)
            and row["state"] == "awaiting-review"):
        pool_name = reviewer_arg[5:]
        if not wp.members_available(conn):
            raise SystemExit(
                "FAIL  reviewer-pool reassignment needs a current Agent Bus"
                " registry; assignment was not changed")
        authors, author_unknown = wp.owner_review_identities(conn, row)
        if author_unknown:
            raise SystemExit(
                "FAIL  reviewer-pool reassignment cannot identify the actual"
                " historical author; assign one concrete reviewer or restore"
                " the Agent Bus recipient evidence")
        pool_member = wp.pool_pick(conn, pool_name, exclude=authors)
        if pool_member is None:
            raise SystemExit(
                f"FAIL  nobody currently holds {pool_name}; assignment was"
                " not changed")
    with conn:
        conn.execute(f"UPDATE dispatch SET {', '.join(sets)}, ask_flag=0,"
                     " last_event=?"
                     f" WHERE id=?", (*vals, wp.now(), row["id"]))
        wp.record(conn, row["id"], "auto-note",
                  "reassigned: " + "; ".join(changes)
                  + (f" ({args.note})" if args.note else ""))
        reviewer = reviewer_arg
        if reviewer:
            if reviewer.startswith("role:") and \
                    reviewer.endswith(wp.POOL_SUFFIX):


                pool = reviewer[5:]
                if pool_member:
                    conn.execute("UPDATE dispatch SET reviewer_seat=?,"
                                 " reviewer_pool=? WHERE id=?",
                                 (pool_member, pool, row["id"]))
                    wp.record(conn, row["id"], "auto-note",
                              f"reviewer-pinned: {pool_member} (least-loaded of"
                              f" {pool}, at reassign)")
                    print(f"NOTE  pool resolved now: reviewer pinned to"
                          f" {pool_member} (rotation stays on)")
                else:
                    conn.execute("UPDATE dispatch SET reviewer_pool='' WHERE id=?",
                                 (row["id"],))
                    print(f"NOTE  reviewer pool role retained until review"
                          f" becomes ready: {pool}")
            else:


                conn.execute("UPDATE dispatch SET reviewer_pool='' WHERE id=?",
                             (row["id"],))
    print(f"OK    {row['id']} reassigned: " + "; ".join(changes))


    row = wp.fetch(conn, row["id"])
    spec = wp.workflow_spec(wp.row_workflow(row))
    owed_col = spec["owed"].get(row["state"])
    changed_cols = {c.split(":", 1)[0] for c in changes}
    owed = row[owed_col] if owed_col else None
    if owed_col in changed_cols and owed and owed.strip().lower() != "operator":
        n_re = conn.execute(
            "SELECT COUNT(*) FROM event WHERE dispatch_id=?"
            " AND note LIKE 'reassigned:%'", (row["id"],)).fetchone()[0]
        if owed_col == "reviewer_seat":
            sent = wp.route(
                conn, row["id"], "review-request",
                f"review-req:{row['id']}:reassign{n_re}", owed,
                f"review request (reassigned to you): {row['subject']}"[:180],
                f"Task {row['id']} reassigned to you as reviewer."
                f" Record your verdict: {wp.orc_command('review', 'verdict', row['id'])} blockers|clean --note"
                f" '<findings or PR-review link>'", row["parent_id"],
                expected_responsibility_version=
                row["responsibility_version"])
        else:
            subject, body = dispatch_message(row)
            sent = wp.route(conn, row["id"], "reassign-notify",
                            f"reassign:{row['id']}:{n_re}", owed,
                            f"reassigned to you - {subject}"[:180], body,
                            row["parent_id"],
                            expected_responsibility_version=
                            row["responsibility_version"])
        if sent:
            print("OK    bus message to the new seat accepted")
        else:
            msg = conn.execute(
                "SELECT send_state,last_error FROM task_msg WHERE task_id=?"
                " AND purpose IN ('review-request','reassign-notify')"
                " ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
            if msg and msg["send_state"] == "invalid-target":
                print("WARN  bus message to the new seat was refused before"
                      f" transport ({msg['last_error']}); no retry will guess"
                      " a recipient, and the next engine tick marks the"
                      " original task for the operator")
            else:
                print("WARN  bus message to the new seat not accepted yet;"
                      " the tick retries it and marks the original task if"
                      " it never lands")
    return 0


def cmd_claim_done(args: argparse.Namespace) -> int:


    conn = wp.connect_writable()
    row = wp.fetch(conn, args.id)
    registry_fresh = wp.members_available(conn)


    caller = wp.require_owed_caller(conn, row, "claim-done")


    os.environ["DISPATCH_LEDGER_ACTOR"] = caller


    if not registry_fresh:
        log("WARN current Agent Bus recipients could not be verified; the"
            " completion claim stays on the original task for the operator")
    claim = wp.claim_commit(
        conn, row, args.note or "(no detail)",
        registry_trusted=registry_fresh, claimant=caller)
    sent = (wp.bus_send(conn, claim["msg_row"])
            if claim["msg_row"] else False)
    outcome = ("entered the operator brief on the original task"
               if claim["judge"] == "operator" else
               "notified" if sent else
               "not reached - the notification is recorded; the tick retries"
               " retryable errors, while an invalid target requires a named"
               " recipient correction")
    print(f"OK    {row['id']} completion claim r{claim['round']} recorded;"
          f" judge {claim['judge']} {outcome}")
    return 0


def cmd_role(args: argparse.Namespace) -> int:
    conn = (wp.connect_readonly() if args.action == "list"
            else wp.connect_writable())
    if args.action == "list":
        rows = conn.execute("SELECT * FROM role_assignment WHERE revoked_ms IS NULL"
                            " ORDER BY role, granted_ms DESC").fetchall()
        if not rows:
            print("OK    no active role assignments")
            return 0
        for r in rows:
            print(f"  {r['role']:<28} {r['agent_id']}  granted {wp.human_age(wp.now() - r['granted_ms'])} ago by {r['granted_by']}")
        return 0
    if not args.agent_id:
        raise SystemExit("FAIL  grant/revoke need <role> <agent-id>")
    if (args.action == "grant"
            and args.agent_id.strip().lower() in {"all", "@all"}):
        raise SystemExit("FAIL  a role needs one agent id, never all/@all")
    if args.action == "grant":
        if not args.by:
            raise SystemExit("FAIL  --by is required: role changes are operator/"
                             "commander rulings and the ruling is the audit record")
        with conn:
            conn.execute("INSERT INTO role_assignment (role, agent_id, granted_by,"
                         " granted_ms) VALUES (?,?,?,?)",
                         (args.role, args.agent_id, args.by, wp.now()))
        print(f"OK    role {args.role} -> {args.agent_id} (by: {args.by})")
        return 0
    with conn:
        cur = conn.execute("UPDATE role_assignment SET revoked_ms=? WHERE role=?"
                           " AND agent_id=? AND revoked_ms IS NULL",
                           (wp.now(), args.role, args.agent_id))
    print(f"OK    revoked {cur.rowcount} assignment(s) of {args.role} from {args.agent_id}")
    return 0


def cmd_team(args: argparse.Namespace) -> int:
    conn = (wp.connect_readonly() if args.action == "list"
            else wp.connect_writable())
    if args.action == "list":
        rows = conn.execute("SELECT * FROM team_member ORDER BY parent_task_id,"
                            " added_ms").fetchall()
        for r in rows:
            print(f"  goal {r['parent_task_id']}  {r['team_role']:<20} {r['agent_id']}")
        if not rows:
            print("OK    no team assignments")
        return 0
    parent = wp.fetch(conn, args.parent)
    if wp.row_workflow(parent) != "parent":
        raise SystemExit(f"FAIL  {parent['id']} is not a parent goal")
    if args.agent_id.strip().lower() in {"all", "@all"}:
        raise SystemExit("FAIL  a team role needs one agent id, never all/@all")
    with conn:
        conn.execute("INSERT OR REPLACE INTO team_member (parent_task_id, agent_id,"
                     " team_role, added_by, added_ms) VALUES (?,?,?,?,?)",
                     (parent["id"], args.agent_id, args.team_role, wp.whoami(), wp.now()))
    print(f"OK    {args.agent_id} joins goal {parent['id']} as {args.team_role}")
    return 0


def cmd_announce(args: argparse.Namespace) -> int:


    targets = [t.strip() for t in (args.to or "").split(",") if t.strip()]
    if any(target.lower() in {"all", "@all"} for target in targets):
        print("FAIL  all/@all is not a named seat; omit --to and use the"
              " explicit --fleet-wide flag for a whole-fleet announcement")
        return 2
    if targets and args.fleet_wide:
        print("FAIL  --to and --fleet-wide are mutually exclusive: name"
              " recipients OR deliberately wake the whole fleet, not both")
        return 2
    if not targets and not args.fleet_wide:
        print("FAIL  announce needs --to seat1,seat2,... (preferred) or the"
              " explicit --fleet-wide flag (it wakes EVERY watch-mode seat)")
        return 2
    conn = wp.connect_writable()
    import uuid as _uuid
    batch = _uuid.uuid4().hex[:8]
    failed = []
    recorded = []
    with conn:
        for target in (targets or ["all"]):
            row_id = wp.record_msg(
                conn, "announce", "announce", f"announce:{batch}:{target}",
                target, args.subject, args.body,
            )
            recorded.append((target, row_id))
    for target, row_id in recorded:
        ok = row_id is not None and wp.bus_send(conn, row_id)
        if not ok:
            failed.append(target)
    if failed:
        print(f"OK    announce recorded for all; send FAILED for:"
              f" {', '.join(failed)}. For retryable errors the tick retries;"
              f" invalid targets require a named recipient correction")
        return 1
    scope = f"{len(targets)} named seat(s)" if targets else "the whole fleet"
    print(f"OK    announce accepted for {scope}")
    return 0


def _bus_members(timeout: int = 45) -> list[dict] | None:
    return wp.read_members(timeout)


def _local_hostnames() -> set[str]:
    import socket
    full = socket.gethostname()
    return {full, full.split(".", 1)[0]}


def _watcher_exceptions() -> set[str]:
    path = cfg.path("watcher_exceptions_file")
    if path is None:
        return set()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return set()
    return {e["agent_id"] for e in data.get("exceptions", [])
            if e.get("agent_id")}


def _watcher_alive(agent_id: str) -> bool:


    if agent_id in _watcher_exceptions():
        return True
    r = subprocess.run(["pgrep", "-f", f"agent-bus-v3\\.py watch {agent_id}"],
                       capture_output=True)
    if r.returncode == 0:
        return True
    r = subprocess.run(["pgrep", "-f", f"unread {agent_id}"],
                       capture_output=True, text=True)
    pids = [int(p) for p in r.stdout.split() if p.strip().isdigit()]
    return any(p != os.getpid() for p in pids)


def _seat_names(seat: dict) -> set[str]:
    names = {seat["agent_id"], seat.get("handle", "")}
    names.update(seat.get("aliases") or [])
    window = wp.window_from_tmux_field(seat.get("tmux", ""))
    if window:
        names.add(f"tmux{window}")
    names.discard("")
    return names


CHECKOUT_TRACE_DAYS = 14
CHECKOUT_TRACE_MAX = 30


def _seat_ledger_trace(conn, names: set, agent_id: str, now: int) -> str:


    since = now - CHECKOUT_TRACE_DAYS * 86400
    rows = conn.execute("SELECT * FROM dispatch WHERE last_event >= ?"
                        " ORDER BY last_event DESC", (since,)).fetchall()
    lines = []
    for r in rows:
        if len(lines) >= CHECKOUT_TRACE_MAX:
            lines.append(f"- … trace capped at {CHECKOUT_TRACE_MAX} rows")
            break
        fields = [r["recipient"]]
        for col in ("owner", "reviewer"):
            try:
                fields.append(r[col])
            except (IndexError, KeyError):
                pass
        if not any((f or "").strip() in names for f in fields):
            continue
        when = datetime.fromtimestamp(r["last_event"],
                                      timezone.utc).strftime("%m-%d")
        res = f"/{r['resolution']}" if r["resolution"] else ""
        lines.append(f"- {r['id']} {r['state']}{res} ({when}): {r['subject'][:70]}")
    for r in conn.execute("SELECT role, granted_ms, revoked_ms FROM"
                          " role_assignment WHERE agent_id=?"
                          " ORDER BY granted_ms DESC LIMIT 10", (agent_id,)):
        g = datetime.fromtimestamp(r["granted_ms"],
                                   timezone.utc).strftime("%m-%d")
        end = (datetime.fromtimestamp(r["revoked_ms"],
                                      timezone.utc).strftime("%m-%d")
               if r["revoked_ms"] else "?")
        lines.append(f"- role:{r['role']} held {g} -> {end}")
    return "\n".join(lines) if lines else "- (no ledger activity in the window)"


def children_breakdown(conn, parent_id: str) -> str:


    kids = wp.children(conn, parent_id)
    done = superseded = dropped = reassigned = waiting = active = 0
    for k in kids:
        if k["state"] in wp.workflow_spec(wp.row_workflow(k))["terminal"]:
            res = k["resolution"] or "done"
            if res == "done":
                done += 1
            elif res == "superseded":
                superseded += 1
            elif res == "dropped":
                dropped += 1
            else:
                reassigned += 1
        elif k["state"] == wp.WAITING_STATE:
            waiting += 1
        else:
            active += 1
    parts = [f"{n} {label}" for n, label in
             ((done, "done"), (superseded, "superseded"), (dropped, "dropped"),
              (reassigned, "reassigned"), (active, "active"), (waiting, "waiting"))
             if n]
    return f"{', '.join(parts) or 'no children'} /{len(kids)}"


WEAK_CHECK_SHAPES = (
    ("pr-title-exists", r"pr list.*(?:in:title|--search)"),
    ("branch-exists", r"branch\s+(?:--list|-r)\b|ls-remote.*refs/heads"),
    ("grep-log-exists", r"log\s+--oneline.*--grep"),
    ("asserts-nothing", r"^\s*true\s*$"),


    ("counts-matches", r"\bwc\s+-\w*[lwc]|\bwc\s+--(?:lines|words|chars)\b"
                       r"|\b(?:grep|rg)\b(?:\s+-\S+)*\s+-[A-Za-z]*c[A-Za-z]*\s"
                       r"|--count\b|\buniq\s+-c\b"
                       r"|\bjq\b.*(?<![.\w])length\b|--jq\s+.*(?<![.\w])length\b"),
)


def weak_check_label(cmd: str) -> str | None:


    cc = (cmd or "").strip()
    if not cc:
        return None
    for label, rx in WEAK_CHECK_SHAPES:
        if re.search(rx, cc):
            return label
    return None


def doctor_truthfulness(conn, panes, members) -> int:


    fails = 0
    for row in open_tasks(conn):
        label = weak_check_label(row["check_cmd"])
        if label:
            print(f"NOTE  {row['id']} has a weak check ({label}): it asserts"
                  f" existence, not the promised result — completion"
                  f" evidence must come from elsewhere"
                  f" ({row['subject'][:48]})")
    claims_cutoff = wp.now() - 24 * 3600
    for row in open_tasks(conn):


        claim = wp.claim_standing(conn, row, repair=False)
        if claim and claim["claimed_ms"] < claims_cutoff:
            print(f"WARN  {row['id']} completion claim r{claim['round']} has"
                  f" waited {wp.human_age(wp.now() - claim['claimed_ms'])} for"
                  f" judgment — judge it or the claim rots"
                  f" ({row['subject'][:48]})")
    if members:
        by_loc: dict[str, list[str]] = {}
        for m in members:
            if m.get("status") == "active" and m.get("tmux"):
                by_loc.setdefault(m["tmux"], []).append(m.get("handle", "?"))
        for loc, handles in sorted(by_loc.items()):
            if len(handles) > 1:
                fails += 1
                print(f"FAIL  {len(handles)} active identities at the same"
                      f" location {loc}: {', '.join(handles)} — ownership is"
                      f" ambiguous; the seat itself must retire/checkout the"
                      f" stale one (never retire another's without operator"
                      f" authority)")
        registered = {w for m in by_loc
                      for w in [wp.window_from_tmux_field(m)] if w}
        if panes is not None:
            pane_wins = {loc.split(":", 1)[1].split(".", 1)[0]
                         for _, loc in panes if ":" in loc}
            for w in sorted(pane_wins - registered, key=lambda x: int(x) if x.isdigit() else 0):
                print(f"NOTE  agent pane at window {w} has no bus registration"
                      f" — unreachable by dispatch until it boots")
    else:
        print("WARN  registry unavailable; duplicate/unregistered checks skipped")
    for row in conn.execute("SELECT * FROM dispatch WHERE state=? AND"
                            " recipient GLOB 'tmux[0-9]*'",
                            (wp.WAITING_STATE,)):
        print(f"WARN  {row['id']} is deferred but addressed to the literal"
              f" window '{row['recipient']}' — seats reseat/checkout; prefer a"
              f" durable agent id or role: recipient ({row['subject'][:48]})")
    return fails


def idle_wait_limit_for(subject: str, seat_active: bool) -> int:


    if seat_active or (subject or "").startswith("STANDING:"):
        return wp.IDLE_WAIT_LIMIT_ACTIVE
    return wp.IDLE_WAIT_LIMIT


def _owed_by_seat(conn, names: set, agent_id: str) -> list[tuple]:

    out = []
    for row in open_tasks(conn):
        context = wp.continuation_context(conn, row)
        if context is None or context["seat"] == "operator":
            continue
        matches = (context.get("agent_id") == agent_id
                   or context.get("seat") in names
                   or (context.get("deferred")
                       and context.get("requested") in names))
        if matches:
            out.append((row, context["label"]))
    return out


def _automatic_local_fleet() -> bool:
    return (os.environ.get("AGENT_BUS_TRANSPORT") == "local"
            and bool(os.environ.get("NW_FLEET_PROFILE_APPLIED"))
            and bool(os.environ.get("NW_FLEET_PRIMARY_SESSION"))
            and not os.environ.get("NW_FLEET_PROFILE_PATH"))


def _handoff_dir() -> Path:
    local = nw_paths.orchestrator_state_dir() / "handoffs"
    return local if _automatic_local_fleet() else cfg.path("handoff.directory", local)


def cmd_topology(args: argparse.Namespace) -> int:


    conn = wp.connect_readonly()
    members = conn.members
    if members is None:
        print("WARN  Agent Bus registry unavailable; registered membership is unknown")
    try:
        panes = pane_sense.agent_panes()
    except (RuntimeError, pane_sense.tmux_runtime.TmuxRuntimeConfigError) as exc:
        panes = None
        print(f"WARN  tmux observation unavailable ({exc}); showing registry only")
    roles: dict[str, list[str]] = {}
    for r in conn.execute("SELECT role, agent_id FROM role_assignment"
                          " WHERE revoked_ms IS NULL"):
        roles.setdefault(r["agent_id"], []).append(r["role"])
    pane_wins: dict[str, str] = {}
    for pane_id, loc in panes or []:
        if ":" in loc:
            pane_wins[loc.split(":", 1)[1].split(".", 1)[0]] = pane_id
    active = [m for m in members or [] if m.get("status") == "active"]
    count = len(active) if members is not None else "?"
    print(f"=== fleet topology: {count} active seat(s),"
          f" {len(panes) if panes is not None else '?'} agent pane(s) ===")
    for m in sorted(active, key=lambda m: int(wp.window_from_tmux_field(
            m.get("tmux", "")) or 999)):
        win = wp.window_from_tmux_field(m.get("tmux", "")) or "?"
        if m.get("host", "") not in _local_hostnames():
            pane_state = "remote"
        elif m.get("terminal_presence") == "unknown":
            pane_state = "location unknown"
        else:
            pane_state = "pane" if _registered_pane(m, panes or []) else "NO PANE"
        role_txt = (" roles: " + ",".join(roles.get(m["agent_id"], [])))\
            if roles.get(m["agent_id"]) else ""
        print(f"  win {win:>3}  {m.get('handle','?'):<42} {m.get('harness','?'):<8}"
              f" {m.get('mode','?'):<5} {pane_state}{role_txt}")
    if panes is not None and members is not None:
        registered = {wp.window_from_tmux_field(m.get("tmux", "")) for m in active}
        orphan = sorted(set(pane_wins) - registered,
                        key=lambda x: int(x) if x.isdigit() else 0)
        if orphan:
            print(f"  -- agent panes with NO registration: windows"
                  f" {', '.join(orphan)}")
    hdir = _handoff_dir()
    recent = sorted(hdir.glob("*.md"), reverse=True)[:5] if hdir.is_dir() else []
    if recent:
        print("  -- recent checkouts (handoffs):")
        for p in recent:
            print(f"     {p.name}")
    return 0


def cmd_onboard(args: argparse.Namespace) -> int:


    ident = args.identity.strip()
    members = _bus_members()
    seat = None
    if members:
        seat = next((m for m in members if ident == m["agent_id"]
                     or ident == m.get("handle")
                     or ident in (m.get("aliases") or [])), None)
    if seat:
        agent_id, handle = seat["agent_id"], seat.get("handle", seat["agent_id"])
        names = _seat_names(seat)
    else:
        agent_id = handle = ident
        names = {ident}
        print("  NOTE " + ("registry has no such seat"
                           if members is not None else "registry unavailable")
              + "; matching on the literal only")


    conn = None
    owed = roles = None
    try:
        conn = wp.connect_readonly()
        candidate_owed = _owed_by_seat(conn, names, agent_id)
        candidate_roles = [r["role"] for r in conn.execute(
            "SELECT role FROM role_assignment WHERE revoked_ms IS NULL"
            " AND agent_id=?", (agent_id,))]
        owed, roles = candidate_owed, candidate_roles
    except (sqlite3.Error, LookupError, TypeError, ValueError, SystemExit) as exc:
        print(f"  NOTE ledger unavailable or incompatible ({exc}); obligations"
              " and roles are unknown, not an empty queue")
    finally:
        if conn is not None:
            conn.close()

    if owed is None:
        print("  OWED — unknown; ledger unavailable")
    elif owed:
        print(f"  OWED — {len(owed)} open task(s) already on your name:")
        for row, _ in owed:
            quiet = wp.human_age(wp.now() - row["last_event"])
            print(f"    {row['id']}  [{row['state']}] quiet={quiet}  {row['subject'][:64]}")
        print("    take each with `orc ack <id>` or hand it off with"
              " `orc reassign <id> --to <seat>`")
    else:
        print("  OWED — nothing; clean slate")
    print("  ROLES — " + ("unknown; ledger unavailable" if roles is None else
          (", ".join("role:" + r for r in roles) if roles else "none")))
    import re as _re
    slot_base = _re.sub(r"-tmux\d+$", "", handle.split("/")[-1])
    hdir = _handoff_dir()
    matches = sorted((p for p in hdir.glob("*.md") if slot_base in p.stem),
                     reverse=True)[:3] if hdir.is_dir() else []
    if matches:
        print(f"  HANDOFFS — predecessor notes for '{slot_base}', read before starting:")
        for p in matches:
            print(f"    {p}")
    else:
        print(f"  HANDOFFS — none recorded for '{slot_base}'"
              f" (browse: {hdir})")
    return 0


def cmd_checkout(args: argparse.Namespace) -> int:


    members = _bus_members()
    if members is None:
        print("FAIL  bus registry unavailable; checkout needs `members`")
        return 1
    ident = (args.identity or "").strip()
    if not ident:
        ident = wp.caller_seat_id()
        if not ident:
            pane = os.environ.get("TMUX_PANE", "").strip()
            where = f" {pane}" if pane else ""
            print(f"FAIL  current tmux pane{where} has no unique active Agent Bus"
                  " seat; nothing was retired")
            print("      join or resume this pane with `agent-boot.sh <task-slug>`,"
                  " then re-run checkout; never infer identity from the attached"
                  " client's current window")
            return 1
    seat = next((m for m in members if ident == m["agent_id"]
                 or ident == m.get("handle")
                 or ident in (m.get("aliases") or [])), None)
    if seat is None:
        print(f"FAIL  no registered seat matches {ident!r}")
        return 1
    agent_id, handle = seat["agent_id"], seat.get("handle", seat["agent_id"])
    if seat.get("status") != "active":
        print(f"FAIL  {handle} is not active (status={seat.get('status')});"
              f" nothing to check out")
        return 1
    names = _seat_names(seat)
    conn = wp.connect_writable()


    owed_open = _owed_by_seat(conn, names, agent_id)
    roles = [r["role"] for r in conn.execute(
        "SELECT role FROM role_assignment WHERE revoked_ms IS NULL"
        " AND agent_id=?", (agent_id,))]
    if owed_open or roles:
        print(f"FAIL  {handle} cannot check out with obligations standing:")
        for row, owed in owed_open:
            print(f"      owes {row['id']}  [{row['state']}] {row['subject'][:64]}")
        for role in roles:
            print(f"      holds role:{role}")
        print("      hand over first: `close <id> --resolution ...` or"
              " `reassign <id> --to <seat>`; `role revoke <role> ...`"
              " (re-grant to the successor). Then re-run checkout.")
        return 1


    when = datetime.now(timezone.utc)
    dst_rel = ""
    if not args.no_vault_note:
        import tempfile
        slug = handle.split("/")[-1]
        dst_rel = f"{when.strftime('%Y-%m-%d')}-{slug}.md"
        body_md = (
            "---\n"
            f"title: \"seat checkout: {handle}\"\n"
            "type: handoff\n"
            f"created: {when.strftime('%Y-%m-%d')}\n"
            "---\n\n"
            f"# Seat checkout: {handle}\n\n"
            f"- agent_id: `{agent_id}`\n"
            f"- location: `{seat.get('tmux', '')}` on `{seat.get('host', '')}`\n"
            f"- checked out by: {wp.whoami()} at {when.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
            f"- obligations at exit: 0 owed tasks, 0 held roles (verified)\n\n"
            f"## Handoff summary (agent-authored)\n\n{args.summary}\n\n"
            f"## Ledger trace (auto-generated, last {CHECKOUT_TRACE_DAYS} days)\n\n"
            f"{_seat_ledger_trace(conn, names, agent_id, wp.now())}\n")
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            fh.write(body_md)
            src = fh.name
        subject = f"handoff: {handle} checkout {when.strftime('%Y-%m-%d')}"
        actor = f"orc checkout ({wp.whoami()})"
        publisher = [] if _automatic_local_fleet() else cfg.command("handoff.publish_command")
        env = dict(os.environ, ORC_HANDOFF_SRC=src, ORC_HANDOFF_DST=dst_rel,
                   ORC_HANDOFF_DIRECTORY=str(_handoff_dir()),
                   ORC_HANDOFF_SUBJECT=subject, ORC_HANDOFF_AGENT=actor)
        try:
            if publisher:
                result = subprocess.run(publisher, env=env, text=True,
                                        capture_output=True, timeout=600)
                if result.returncode != 0:
                    print("FAIL  handoff publish failed; seat left ACTIVE (re-run checkout):")
                    for line in (result.stdout + result.stderr).strip().splitlines()[-3:]:
                        print(f"      {line}")
                    return 1
            else:
                destination = _handoff_dir() / dst_rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile("w", dir=destination.parent,
                                                 prefix=".handoff-", delete=False) as staged:
                    staged.write(body_md)
                    staged_path = Path(staged.name)
                try:
                    staged_path.replace(destination)
                finally:
                    staged_path.unlink(missing_ok=True)
        except (OSError, subprocess.TimeoutExpired) as exc:
            print(f"FAIL  handoff publish failed ({type(exc).__name__}); seat left ACTIVE")
            return 1
        finally:
            os.unlink(src)
        print(f"OK    handoff note published: {dst_rel}")

    r = subprocess.run(["bash", wp.bus_cli(), "retire", agent_id,
                        "--kind", "checkout"],
                       text=True, capture_output=True, timeout=45)
    if r.returncode != 0:
        print(f"FAIL  retire failed (seat remains registered — re-run checkout):"
              f" {(r.stdout + r.stderr).strip()[:200]}")
        return 1
    print(f"OK    {(r.stdout or '').strip() or f'retired {handle}'}")
    if seat.get("host", "") in _local_hostnames() and _watcher_alive(agent_id):
        subprocess.run(["pkill", "-f", f"agent-bus-v3\\.py watch {agent_id}"],
                       capture_output=True)
        print("OK    watcher stopped")
    print(f"OK    checkout complete: {handle}")
    return 0


_HARNESS_PANE_COMMANDS = {
    "claude": {"claude", "node"},
    "opencode": {"opencode", "node"},
    "codex": {"codex", "node"},
    "dsh": {"dsh"},
}


def _pane_current_command(pane_id: str) -> str | None:


    try:
        r = subprocess.run([*tmux_runtime.base_cmd(), "display", "-p", "-t", pane_id,
                            "#{pane_current_command}"],
                           text=True, capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    out = r.stdout.strip()
    return out or None


def run_pane_succession(conn, host: str, pane_id: str, *,
                        location: str = "", bus_db: Path | None = None,
                        watcher_alive=None, pane_command=None,
                        retire=None, log=print) -> int:


    watcher_alive = watcher_alive or _watcher_alive
    pane_command = pane_command or _pane_current_command
    retire = retire or (lambda aid: subprocess.run(
        ["bash", wp.bus_cli(), "retire", aid, "--kind", "succession"],
        text=True, capture_output=True, timeout=45))
    db_path = bus_db or wp.agent_bus_db_path()
    if not db_path.exists():
        log(f"OK    pane-succession: no bus DB at {db_path}; nothing to do")
        return 0
    bus_conn = sqlite3.connect(db_path)
    bus_conn.row_factory = sqlite3.Row
    try:


        preds = bus_conn.execute(
            "SELECT * FROM identities WHERE status='active' AND host=?"
            " AND (pane_id=? OR (pane_id IS NULL AND ?!='' AND tmux=?))",
            (host, pane_id, location, location)).fetchall()
    finally:
        bus_conn.close()
    if not preds:
        log(f"OK    pane-succession: no active seat bound to {pane_id}")
        return 0


    cmd = pane_command(pane_id)
    blockers: list[str] = []
    obligations_blocked = False
    for pred in preds:
        aid, handle = pred["agent_id"], pred["handle"]
        seat = {"agent_id": aid, "handle": handle,
                "aliases": json.loads(pred["aliases_json"] or "[]"),
                "tmux": pred["tmux"] or ""}
        owed_open = _owed_by_seat(conn, _seat_names(seat), aid)
        roles = [r["role"] for r in conn.execute(
            "SELECT role FROM role_assignment WHERE revoked_ms IS NULL"
            " AND agent_id=?", (aid,))]
        if owed_open or roles:
            obligations_blocked = True
            blockers.append(f"{handle} still has obligations - succession"
                            f" NEVER drops work:")
            for row, owed in owed_open:
                blockers.append(f"      owes {row['id']}  [{row['state']}]"
                                f" {row['subject'][:64]}")
            for role in roles:
                blockers.append(f"      holds role:{role}")
            blockers.append(f"      `orc reassign <id> --to <seat>` or close"
                            f" them, then `orc checkout {aid} --summary ...`")
            continue


        could_be = _HARNESS_PANE_COMMANDS.get(pred["harness"], set())
        pane_ambiguous = (cmd is None or cmd in {"bash", "zsh", "sh"}
                          or cmd in could_be or not could_be)
        if pred["mode"] == "watch" and watcher_alive(aid):
            blockers.append(f"{handle} still has a live watcher - that"
                            f" session may be alive. Resuming it? re-run"
                            f" boot with AGENT_BUS_SLOT={pred['slot']}."
                            f" Certain it is dead? the seat itself must"
                            f" exit via `orc checkout`.")
        elif pane_ambiguous:
            blockers.append(f"pane {pane_id} shows {cmd or 'unreadable'!r},"
                            f" which could still be {handle}"
                            f" ({pred['harness']}). Ambiguity holds - a"
                            f" hold is recoverable, retiring a live seat is"
                            f" not. Same-harness restart? RESUME:"
                            f" AGENT_BUS_SLOT={pred['slot']}.")
    if blockers:
        log(f"FAIL  pane-succession: {len(preds)} predecessor(s) on"
            f" {pane_id}, at least one blocked - ALL-OR-NOTHING, nothing"
            f" was retired:")
        for line in blockers:
            log(f"      {line}")
        return 3 if obligations_blocked else 4


    for pred in preds:
        aid, handle = pred["agent_id"], pred["handle"]
        r = retire(aid)
        if getattr(r, "returncode", 1) != 0:
            log(f"FAIL  pane-succession: retire failed for {handle}:"
                f" {((getattr(r, 'stdout', '') or '') + (getattr(r, 'stderr', '') or '')).strip()[:200]}")
            return 1
        log(f"OK    {(getattr(r, 'stdout', '') or '').strip() or f'retired {handle}'}")
    log(f"OK    pane-succession complete: {pane_id} is clear")
    return 0


def cmd_pane_succession(args: argparse.Namespace) -> int:
    import socket
    pane = (args.pane or os.environ.get("TMUX_PANE", "")).strip()
    if not pane.startswith("%"):
        print("FAIL  pane-succession needs a tmux pane id (%N); pass --pane"
              " or run inside the pane taking over")
        return 2
    host = args.host or socket.gethostname().split(".", 1)[0]
    return run_pane_succession(wp.connect_writable(), host, pane,
                               location=args.location or "")


LIVENESS_NUDGE_GAP_S = 30 * 60
LIVENESS_RETIRE_AFTER_S = 60 * 60


def _unread_count(agent_id: str) -> int | None:
    try:
        out = subprocess.run(["bash", wp.bus_cli(), "unread", agent_id],
                             text=True, capture_output=True, timeout=45)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    for line in out.stdout.splitlines():
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict) and "count" in payload:
            return int(payload["count"])
    return None


def tick_seat_liveness(conn, dry: bool, *, members=None, panes=None,
                       watcher_alive=None, unread_count=None, nudge=None,
                       retire=None, now_ms=None, hostnames=None) -> None:


    watcher_alive = watcher_alive or _watcher_alive
    unread_count = unread_count or _unread_count
    retire = retire or (lambda aid: subprocess.run(
        ["bash", wp.bus_cli(), "retire", aid, "--kind", "reaper"], text=True,
        capture_output=True, timeout=45).returncode == 0)
    now = now_ms if now_ms is not None else wp.now()
    hosts = hostnames or _local_hostnames()
    if members is None:
        members = _bus_members()
    if members is None:
        log("WARN seat-liveness: bus members unavailable; pass skipped")
        return
    mine = [m for m in members
            if m.get("status") == "active" and m.get("mode") == "watch"
            and m.get("host", "") in hosts
            and m.get("handle") != wp.SERVICE_HANDLE]
    if not mine:
        return
    if panes is None:
        try:
            panes = pane_sense.agent_panes()
        except RuntimeError as exc:
            log(f"WARN seat-liveness: tmux observation unavailable ({exc});"
                f" pass skipped")
            return
    if nudge is None:
        def nudge(pane_id, progress=None):
            tmux_send = load_script("agent-tmux-send.py",
                                    "agent_tmux_send_for_liveness")
            return tmux_send.send_outcome(pane_id, "", nudge_key="pull",
                                          progress=progress)
    if not dry and mine:


        keep = {m["agent_id"] for m in mine}
        with conn:
            conn.execute(
                "DELETE FROM seat_watch WHERE agent_id NOT IN (%s)"
                % ",".join("?" * len(keep)), tuple(keep))
    for seat in mine:
        aid, handle = seat["agent_id"], seat.get("handle", seat["agent_id"])
        if seat.get("terminal_presence") == "unknown":
            log(f"WARN seat-liveness: {handle} terminal location unknown; pass skipped")
            continue
        if watcher_alive(aid):


            if not dry:
                with conn:
                    conn.execute("UPDATE seat_watch SET first_dead_ms=0,"
                                 " probe_ms=0 WHERE agent_id=?", (aid,))
                    wp.wake_attempt_resolve(conn, f"seat-liveness:{aid}",
                                            aid, "reacted-alive")
            continue
        row = conn.execute("SELECT * FROM seat_watch WHERE agent_id=?",
                           (aid,)).fetchone()
        if row is None:
            if not dry:
                with conn:
                    conn.execute("INSERT INTO seat_watch (agent_id, first_dead_ms)"
                                 " VALUES (?,?)", (aid, now))
            log(f"NOTE seat-liveness: {handle} watcher dead (first seen)")
            continue
        if row["first_dead_ms"] == 0:


            if not dry:
                with conn:
                    conn.execute("UPDATE seat_watch SET first_dead_ms=?,"
                                 " probe_ms=0 WHERE agent_id=?", (now, aid))
            log(f"NOTE seat-liveness: {handle} watcher dead again")
            continue
        pane = _registered_pane(seat, panes)
        if pane is not None:
            unread = unread_count(aid)
            if unread and now - row["last_nudge_ms"] >= LIVENESS_NUDGE_GAP_S:
                if dry:
                    log(f"DRY seat-liveness: would nudge {handle}"
                        f" ({unread} unread, watcher dead)")
                    continue
                with conn:
                    allowed = wp.wake_attempt_open(
                        conn, f"seat-liveness:{aid}", aid, "pull",
                        "dead-watcher", now_s=now)
                if not allowed:
                    continue
                try:
                    outcome, detail = wp.wake_contact(
                        conn, aid, str(pane[0]), "liveness",
                        [("liveness", "dead-watcher-nudge")],
                        lambda progress, _p=pane[0]:
                            nudge(_p, progress=progress))
                    if outcome != wp.SendOutcome.CONTACTED:


                        log(f"WARN seat-liveness: pull nudge {outcome} for"
                            f" {handle}: {detail}")
                        continue
                    with conn:
                        conn.execute("UPDATE seat_watch SET last_nudge_ms=?"
                                     " WHERE agent_id=?", (now, aid))
                    log(f"OK seat-liveness: pull nudge -> {handle}"
                        f" ({unread} unread, watcher dead)")
                except (RuntimeError, ValueError) as e:
                    log(f"WARN seat-liveness: nudge failed for {handle}: {e}")
                    with conn:
                        wp.wake_attempt_fail(conn, f"seat-liveness:{aid}",
                                             aid, "pull", "dead-watcher")
            continue

        if row["probe_ms"] == 0:
            subject = "liveness probe: watcher dead, pane gone - rejoin or retire"
            body = (f"Your registration {handle} has a dead watcher and no"
                    f" observable pane at {seat.get('tmux', '')}. Rejoin with"
                    f" your original slot, or this registration retires"
                    f" after this probe is accepted and the absence remains"
                    f" continuous for {LIVENESS_RETIRE_AFTER_S // 60} minutes"
                    f" (revive = rejoin with the same slot).")
            if dry:
                log(f"DRY seat-liveness: would probe {handle} (pane gone)")
                continue
            dedup = f"liveness:{aid}:{row['first_dead_ms']}"
            with conn:
                row_id = wp.record_msg(conn, "liveness", "liveness-probe",
                                       dedup, aid, subject, body)
            if row_id is not None:
                wp.bus_send(conn, row_id)
            probe = conn.execute(
                "SELECT send_state FROM task_msg WHERE dedup_key=?", (dedup,)
            ).fetchone()
            if probe is None or probe["send_state"] != "accepted":
                log(f"WARN seat-liveness: probe to {handle} is not accepted;"
                    " retirement clock remains stopped and resend is bounded"
                    " by the normal outbox pass")
                continue
            with conn:
                conn.execute("UPDATE seat_watch SET probe_ms=? WHERE agent_id=?",
                             (now, aid))
            log(f"OK seat-liveness: probe accepted for {handle} (pane gone)")
            continue
        probe = conn.execute(
            "SELECT send_state FROM task_msg WHERE dedup_key=?",
            (f"liveness:{aid}:{row['first_dead_ms']}",),
        ).fetchone()
        if probe is None or probe["send_state"] != "accepted":
            if not dry:
                with conn:
                    conn.execute("UPDATE seat_watch SET probe_ms=0"
                                 " WHERE agent_id=?", (aid,))
            log(f"WARN seat-liveness: {handle} has no accepted probe;"
                " retirement suppressed")
            continue
        if now - max(row["first_dead_ms"], row["probe_ms"]) \
                >= LIVENESS_RETIRE_AFTER_S:
            if dry:
                log(f"DRY seat-liveness: would retire {handle}"
                    f" (dead {wp.human_age(now - row['first_dead_ms'])},"
                    f" pane gone, probe unanswered)")
                continue
            if retire(aid):
                with conn:
                    conn.execute("DELETE FROM seat_watch WHERE agent_id=?", (aid,))
                log(f"OK seat-liveness: retired {handle} (dead >1h, pane gone)")
            else:
                log(f"WARN seat-liveness: retire failed for {handle}; next tick retries")


def open_tasks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM dispatch WHERE state != 'closed'"
                        " ORDER BY workflow, check_after ASC").fetchall()


def actionable_task_messages(conn, row: sqlite3.Row) -> list[sqlite3.Row]:

    return [
        msg for msg in conn.execute(
            "SELECT * FROM task_msg WHERE task_id=?", (row["id"],)
        ).fetchall()
        if wp.message_is_current_responsibility(conn, msg, row)
    ]


def unknown_recipient_messages(messages: list[sqlite3.Row]) -> list[sqlite3.Row]:

    cutoff = wp.now() - wp.DEAD_LETTER_PARK_S
    return [
        msg for msg in messages
        if msg["send_state"] == "accepted"
        and msg["purpose"] in wp.RESPONSIBILITY_PURPOSES
        and msg["target"] not in {"all", "@all"}
        and msg["at_ms"] < cutoff
        and not wp.message_recipient_agent_id(
            msg["msg_id"], msg["recipient_agent_id"], msg["target"]
        )
    ]


def task_flags(conn: sqlite3.Connection, row: sqlite3.Row) -> list[str]:
    flags = []
    if wp.credible_ask(conn, row):
        flags.append("ASKING(verb)")
    if row["guard_unknown_streak"] >= 3:
        flags.append(f"GUARD-UNKNOWN x{row['guard_unknown_streak']}")
    drv = wp.current_drive(conn, row)
    if drv:
        flags.append(f"rung={drv['st']}"
                     + (f" cycles={drv['cycles']}" if drv["cycles"] else "")
                     + (f" pane-absent={drv['absent_ticks']}" if drv["absent_ticks"] else ""))
    if wp.deadline_attention_event(conn, row):
        flags.append("DEADLINE-OVERDUE")
    claim = wp.claim_standing(conn, row, repair=False)
    if claim:
        flags.append(f"CLAIMS-DONE r{claim['round']}")


    for intent in wp.open_review_intents(conn, row["id"]):
        flags.append(f"REVIEWING({intent['seat'][:20]})")
    if wp.current_escalation_delivery_failure(conn, row):
        flags.append("SUPERVISOR-UNREACHABLE")
    if wp.reviewer_pool_attention_active(conn, row):
        flags.append("REVIEWER-POOL-UNAVAILABLE")
    actionable_messages = actionable_task_messages(conn, row)
    unsent = sum(msg["send_state"] in ("recorded", "failed")
                 for msg in actionable_messages)
    if unsent:
        flags.append(f"SEND-FAILED x{unsent}")
    invalid = sum(msg["send_state"] == "invalid-target"
                  for msg in actionable_messages)
    if invalid:
        flags.append(f"INVALID-TARGET x{invalid}")
    unknown = len(unknown_recipient_messages(actionable_messages))
    if unknown:
        flags.append(f"RECIPIENT-UNKNOWN x{unknown}")
    return flags


def frontier_tasks(conn, rows) -> list[sqlite3.Row]:


    out = []
    for r in rows:
        if r["state"] == wp.WAITING_STATE:
            continue
        if not wp.needs_ids(conn, r["id"]) and not wp.needed_by_ids(conn, r["id"]):
            continue
        if wp.open_predecessors(conn, r["id"]):
            continue
        out.append(r)
    return out


def fleet_label() -> dict:
    name = os.environ.get("NW_FLEET") or cfg.get("fleets.default_name", "default")
    if name == "default":
        name = cfg.get("fleets.default_name", "default")
    return {"name": name, "session": os.environ.get("NW_FLEET_PRIMARY_SESSION")
            or cfg.get("tmux.primary_session", "0")}


def fleet_heading() -> str:
    fleet = fleet_label()
    return f"Fleet {fleet['name']} | session {fleet['session']}"


def board_data(repo=None) -> dict:
    """One read-only work selection for table, columns, summary and JSON."""
    data = {"fleet": fleet_label(), "tasks": [], "goals": [], "recently_closed": [],
            "counts": {"tasks": 0, "goals": 0, "operator": 0, "attention": 0},
            "warnings": [], "scheduler": tick_staleness()}
    if not wp.configured_db_path().exists():
        data["scheduler"] = None
        return data
    conn = wp.connect_readonly()
    try:
        if not wp.members_available(conn):
            data["warnings"].append("Agent Bus registry unavailable; current recipients are unknown")
        rows = open_tasks(conn)
        if repo:
            rows = [r for r in rows if wp.bare_repo(r["repo"]) == wp.bare_repo(repo)]
        attention = {row["id"]: reason for row, reason in attention_rows(conn, rows)}
        frontier = {row["id"] for row in frontier_tasks(conn, rows)}
        for row in rows:
            item = dict(row)
            waiting = row["state"] == wp.WAITING_STATE
            context = None if waiting else wp.continuation_context(conn, row)
            blockers = [r["id"] for r in wp.open_predecessors(conn, row["id"])]
            owner = ("nobody" if context is None else
                     f"unresolved:{context['requested']}" if context.get("deferred")
                     else context["seat"])
            item.update(owner=owner,
                        next_action=("waits on " + ", ".join(blockers) if waiting else
                                     context["label"] if context else ""),
                        blockers=blockers, column=wp.kanban_column(row),
                        operator=wp.waits_on_operator(conn, row),
                        attention=attention.get(row["id"]),
                        flags=task_flags(conn, row), frontier=row["id"] in frontier)
            data["tasks"].append(item)
            if wp.row_workflow(row) == "parent":
                item["children_summary"] = children_breakdown(conn, row["id"])
                data["goals"].append(item)
        closed = conn.execute("SELECT * FROM dispatch WHERE state='closed' AND"
                              " last_event >= ? ORDER BY last_event DESC",
                              (wp.now() - 86400,)).fetchall()
        data["recently_closed"] = [dict(r) for r in closed
                                  if not repo or wp.bare_repo(r["repo"]) == wp.bare_repo(repo)]
        data["counts"] = {"tasks": len(rows), "goals": len(data["goals"]),
                          "operator": sum(t["operator"] for t in data["tasks"]),
                          "attention": len(attention)}
        return data
    finally:
        conn.close()


def task_notices(flags):
    messages = {"ASKING": "Needs a decision", "GUARD-UNKNOWN": "Progress check unavailable",
                "DEADLINE-OVERDUE": "Deadline passed", "CLAIMS-DONE": "Completion claimed; awaiting review",
                "REVIEWING": "Review in progress", "SUPERVISOR-UNREACHABLE": "Coordinator could not be reached",
                "REVIEWER-POOL-UNAVAILABLE": "No reviewer is available", "SEND-FAILED": "Message delivery pending",
                "INVALID-TARGET": "Recipient could not be resolved", "RECIPIENT-UNKNOWN": "Recipient identity is unknown"}
    return [message for prefix, message in messages.items() if any(flag.startswith(prefix) for flag in flags)]


def render_board(data, *, overview=False):
    print(f"{fleet_heading()} | {data['counts']['tasks']} open task(s)")
    if overview or data["goals"]:
        print("GOALS")
        for goal in data["goals"]:
            print(f"  {goal['id']}  {goal['subject']} [{goal['children_summary']}]")
        if not data["goals"]:
            print("  No active goals. Use goals --all to inspect history.")
    for label, tasks in (
        ("NEEDS YOU", [t for t in data["tasks"] if t["operator"]]),
        ("IN PROGRESS", [t for t in data["tasks"] if not t["operator"]]),
    ):
        if not tasks:
            continue
        print(label)
        for task in tasks:
            print(f"  {task['id']}  {task['subject']}")
            ready = " | ready now" if task["frontier"] else ""
            print(f"    owner: {task['owner']} | {task['state']} | {task['next_action']}{ready}")
            notices = task_notices(task["flags"])
            if notices:
                print("    " + "; ".join(notices))
    if not data["tasks"]:
        print("No open tasks.")
    for warning in data["warnings"]:
        print(f"WARN  {warning}")
    if data["scheduler"]:
        print(f"WARN  {data['scheduler']}")
    if overview:
        print("Views: board | goals | agents | task show ID | view [WINDOW]")


def cmd_board(args: argparse.Namespace) -> int:
    data = board_data(getattr(args, "repo", None))
    if getattr(args, "json", False):
        print(json.dumps(data, ensure_ascii=False))
    elif getattr(args, "view", "table") == "summary":
        render_summary(data, args)
    elif getattr(args, "view", "table") == "columns":
        render_columns(data, args)
    else:
        render_board(data)
    return 0


def cmd_overview(args: argparse.Namespace) -> int:
    data = board_data()
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
    else:
        render_board(data, overview=True)
    return 0


def cmd_agents(args: argparse.Namespace) -> int:
    members = _bus_members()
    warnings = []
    if members is None:
        warnings.append("Agent Bus registry unavailable; membership is unknown")
    try:
        windows = [{"index": index, "name": name} for index, name in pane_sense.window_titles()]
    except (RuntimeError, OSError) as exc:
        windows = None
        warnings.append(f"Terminal windows unavailable: {exc}")
    roles = {}
    if wp.configured_db_path().exists():
        conn = wp.connect_readonly()
        try:
            for row in conn.execute("SELECT role,agent_id FROM role_assignment WHERE revoked_ms IS NULL"):
                roles.setdefault(row["agent_id"], []).append(row["role"])
        finally:
            conn.close()
    active = [{**member, "roles": roles.get(member["agent_id"], [])}
              for member in members or [] if member.get("status") == "active"]
    data = {"fleet": fleet_label(), "agents": active if members is not None else None,
            "windows": windows, "warnings": warnings}
    if args.json:
        print(json.dumps(data, ensure_ascii=False))
    else:
        print(fleet_heading())
        print("WINDOWS")
        for window in windows or []:
            print(f"  {window['index']:>3}  {window['name']}")
        print("AGENTS")
        for member in active:
            window = wp.window_from_tmux_field(member.get("tmux", "")) or "?"
            print(f"  window {window}  {member.get('handle', member['agent_id'])}"
                  f"  {member.get('harness', '?')} | terminal {member.get('terminal_presence', 'unknown')}"
                  f"  {', '.join(member['roles'])}")
        if members is not None and not active:
            print("  No registered agents.")
        for warning in warnings:
            print(f"WARN  {warning}")
    return int(members is None)


def tick_staleness() -> str | None:

    p = state_dir() / "tick-last.json"
    try:
        last = json.loads(p.read_text())["at_s"]
    except (OSError, ValueError, KeyError):
        return "the tick has never run on this box (no tick-last.json)"
    age = wp.now() - int(last)
    if age > 900:
        return f"the tick has not run for {wp.human_age(age)} - the engine may be dead"
    return None


def clip_pad_display(text: str, width: int) -> str:


    out, used = [], 0
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + w > width:
            break
        out.append(ch)
        used += w
    return "".join(out) + " " * (width - used)


def make_paint(no_color: bool):


    color = not (no_color or os.environ.get("NO_COLOR"))

    def paint(code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if color else text
    return paint


def attention_rows(conn, rows) -> list:


    attn = []
    for r in rows:
        drv = wp.current_drive(conn, r)
        if drv and drv["st"] == wp.S_ESCALATED:
            attn.append((r, "escalated"))
            continue
        if wp.deadline_attention_event(conn, r):
            attn.append((r, "deadline-overdue"))
            continue
        if wp.credible_ask(conn, r):
            attn.append((r, "asks-human"))
            continue
        if wp.reviewer_pool_attention_active(conn, r):
            attn.append((r, "reviewer-pool-unavailable"))
            continue
        messages = actionable_task_messages(conn, r)
        invalid = sum(msg["send_state"] == "invalid-target"
                      for msg in messages)
        if invalid:
            attn.append((r, f"invalid-target x{invalid}"))
            continue
        unsent = sum(msg["send_state"] in ("recorded", "failed")
                     for msg in messages)
        if unsent:
            attn.append((r, f"send-failed x{unsent}"))
            continue
        unknown = len(unknown_recipient_messages(messages))
        if unknown:
            attn.append((r, f"recipient-unknown x{unknown}"))
    return attn


def render_summary(data, args):
    paint = make_paint(getattr(args, "no_color", False))
    stale = data["scheduler"]
    if stale:
        tick_part = paint("31", "tick STALE" if "not run for" in stale else "tick NEVER")
    else:
        tick_part = "scheduler current" if wp.configured_db_path().exists() else "no recorded work"
    tasks = data["tasks"]
    prefix = f"{fleet_heading()} | ORC"
    if not tasks:
        print(f"{prefix} idle | {tick_part}")
    else:
        counts = {column: sum(t["column"] == column for t in tasks) for column in wp.KANBAN_COLUMNS}
        columns = " ".join(f"{column} {count}" for column, count in counts.items() if count)
        print(f"{prefix} open {len(tasks)} | {columns} | attn {data['counts']['attention']}"
              f" | operator {data['counts']['operator']} | {tick_part}")
        attention = [f"{t['id']} {t['attention']} {t['subject']}" for t in tasks if t["attention"]]
        if attention:
            width = max(int(os.environ.get("COLUMNS") or 200), 40)
            print(paint("31", ("ATTN " + " | ".join(attention))[:width]))
    for warning in data["warnings"]:
        print(f"WARN  {warning}")


def cmd_statusline(args: argparse.Namespace) -> int:
    args.view = "summary"
    return cmd_board(args)


def render_columns(data, args):
    render_summary(data, args)
    paint = make_paint(getattr(args, "no_color", False))
    cells = {column: [] for column in wp.KANBAN_COLUMNS}
    for task in data["tasks"]:
        marker = ("!" if task["attention"] else "") + ("@" if task["operator"] else "")
        cells[task["column"]].append((marker, f"{task['id']} {task['subject']}"))
    for task in data["recently_closed"]:
        cells["closed"].append(("", f"{task['id']} {task['subject']} ({task['resolution']})"))
    width = int(os.environ.get("COLUMNS") or 160)
    colw = max(16, (width - 1 - 2 * (len(cells) - 1)) // len(cells))
    def emit(values):
        print("|" + "  ".join(values).rstrip())
    emit([clip_pad_display(f"{('closed-24h' if c == 'closed' else c).upper()} ({len(cells[c])})", colw)
          for c in cells])
    height = max((len(v) for v in cells.values()), default=0)
    limit = getattr(args, "max_rows", 0)
    shown = min(height, limit) if limit else height
    for index in range(shown):
        values = []
        for items in cells.values():
            marker, text = items[index] if index < len(items) else ("", "")
            if limit and index == shown - 1 and len(items) > shown:
                marker, text = "", f"+{len(items) - shown + 1} more"
            value = clip_pad_display(marker + text, colw)
            values.append(paint("31" if "!" in marker else "33", value) if marker else value)
        emit(values)


def cmd_kanban(args: argparse.Namespace) -> int:
    args.view = "columns"
    return cmd_board(args)


def cmd_tree(args: argparse.Namespace) -> int:
    historical = getattr(args, "all", False) or bool(args.id)
    result = {"fleet": fleet_label(), "goals": []}
    if not wp.configured_db_path().exists():
        if args.id:
            raise ValueError("this fleet has no recorded goals")
    else:
        conn = wp.connect_readonly()
        try:
            if args.id:
                parents = [wp.fetch(conn, args.id)]
            else:
                parents = conn.execute("SELECT * FROM dispatch WHERE workflow='parent'"
                                       + ("" if historical else " AND state != 'closed'")
                                       + " ORDER BY created_ms").fetchall()
                if not parents:
                    parents = [r for r in open_tasks(conn)
                               if wp.needed_by_ids(conn, r["id"]) and not wp.needs_ids(conn, r["id"])]
            def collect(row):
                kids = wp.children(conn, row["id"])
                return {**dict(row), "children_summary": children_breakdown(conn, row["id"]),
                        "needs": [{"id": p["id"], "state": p["state"]} for p in wp.predecessors(conn, row["id"])],
                        "needed_by": wp.needed_by_ids(conn, row["id"]),
                        "children": [collect(child) for child in kids if historical or not wp.is_closed(child)]}
            result["goals"] = [collect(row) for row in parents]
        finally:
            conn.close()
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False))
        return 0
    print(fleet_heading() + (" | goal history" if historical else " | current goals"))
    def show(row, depth=0):
        pad = "  " * depth
        print(f"{pad}{row['id']}  {row['state']}  {row['subject']} [{row['children_summary']}]")
        for pred in row["needs"]:
            print(f"{pad}  needs {pred['id']} ({pred['state']})")
        if row["needed_by"]:
            print(f"{pad}  needed by {', '.join(row['needed_by'])}")
        for child in row["children"]:
            show(child, depth + 1)
    for goal in result["goals"]:
        show(goal)
    if not result["goals"]:
        print("No active goals or dependency roots. Use goals --all to inspect history.")
    return 0


def snapshot_db(reason: str = "daily") -> Path | None:


    snaps = state_dir() / "snapshots"
    snaps.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    dest = snaps / f"dispatch-ledger-{day}.sqlite3"
    if dest.exists() and reason == "daily":
        return None
    src = wp.connect_readonly()
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    dst.close()
    for old in sorted(snaps.glob("dispatch-ledger-*.sqlite3"))[:-14]:
        old.unlink()
    return dest


def cmd_snapshot(args: argparse.Namespace) -> int:
    dest = snapshot_db(reason="manual")
    print(f"OK    snapshot at {dest}")
    return 0


def escalate(conn, row, reason: str, dry: bool, prefix: str = "",
             dedup_key: str | None = None,
             cycle_floor_event_id: int | None = None, *,
             registry_trusted: bool = True,
             route_observation_id: int | None = None,
             expected_generation: str = "",
             expected_voice_event_id: int | None = None,
             expected_ask_event_id: int | None = None,
             drive_update: tuple | None = None) -> bool:


    if dry:
        log(f"DRY would escalate {row['id']}: {prefix}{reason}")
        return True
    initial_context = wp.continuation_context(
        conn, row, registry_trusted=registry_trusted)
    if not expected_generation and initial_context is not None:
        expected_generation = initial_context["generation"]
    if cycle_floor_event_id is not None:
        already = conn.execute(
            "SELECT 1 FROM event WHERE dispatch_id=? AND kind='auto-chase'"
            " AND id>? LIMIT 1", (row["id"], cycle_floor_event_id),
        ).fetchone()
        if already is not None:
            log(f"NOTE combined another escalation reason for {row['id']}"
                " with the alert already recorded in this tick")
            return False


    with conn:
        locked = conn.execute(
            "UPDATE dispatch SET last_event=last_event WHERE id=? AND state=?"
            " AND responsibility_version=?",
            (row["id"], row["state"], row["responsibility_version"]),
        )
        if locked.rowcount != 1:
            log(f"NOTE skipped stale escalation for {row['id']}: task state"
                " or responsibility changed")
            return False
        current = wp.fetch(conn, row["id"])
        current_context = wp.continuation_context(
            conn, current, registry_trusted=registry_trusted)
        current_generation = (current_context["generation"]
                              if current_context is not None else "")
        if expected_generation and current_generation != expected_generation:
            log(f"NOTE skipped stale escalation for {row['id']}: the person"
                " responsible for the next action changed")
            return False
        if expected_voice_event_id is not None:
            latest_voice = wp.current_continuation_voice(
                conn, current, current_context)
            latest_voice_id = int(latest_voice["id"]) if latest_voice else 0
            if latest_voice_id != expected_voice_event_id:
                log(f"NOTE skipped stale escalation for {row['id']}: the"
                    " responsible person responded during the check")
                return False
        if expected_ask_event_id is not None:
            latest_ask = wp.current_ask_event(conn, current)
            latest_ask_id = int(latest_ask["id"]) if latest_ask else 0
            if latest_ask_id != expected_ask_event_id:
                log(f"NOTE skipped stale escalation for {row['id']}: the"
                    " human question changed during the check")
                return False
        row = current
        new_state = wp.step_row(row, "auto-chase")
        conn.execute("UPDATE dispatch SET state=?, chases=chases+1,"
                     " chases_total=chases_total+1, last_event=?, check_after=?"
                     " WHERE id=? AND state=? AND responsibility_version=?",
                     (new_state, wp.now(), wp.now() + wp.parse_after("30m"),
                      row["id"], row["state"], row["responsibility_version"]))
        if drive_update is not None:
            drive_seat, drive_generation, drive_entry, absent_ticks = drive_update
            conn.execute(
                "INSERT OR REPLACE INTO drive (task_id,seat,generation,st,"
                " cycles,grace_used,idle_waits,absent_ticks,updated_ms)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (row["id"], drive_seat, drive_generation,
                 drive_entry.get("st", wp.S_DISPATCHED),
                 int(drive_entry.get("cycles", 0)),
                 int(bool(drive_entry.get("grace_used"))),
                 int(drive_entry.get("idle_waits", 0)), absent_ticks, wp.now()),
            )
        attention_event_id = wp.record(
            conn, row["id"], "auto-chase", f"engine: {prefix}{reason}",
            continuation_generation=current_generation)
    subject = f"{prefix}ESCALATION {row['id']}: {reason}"[:180]
    supervisor = wp.escalation_recipient(
        conn, row, registry_trusted=registry_trusted)
    sent = False
    row_id = None
    question = wp.current_ask_event(conn, row)
    decision = ""
    if question is not None:
        decision = ("\n\nDecision needed: "
                    f"{question['note'].removeprefix(wp.ASK_NOTE_PREFIX)}")
    inspect = wp.orc_task_command(row['id'], command=str(SCRIPT_DIR / 'orc'))
    body = (f"{prefix}{reason}. Subject: {row['subject'][:200]}."
            f"{decision}\n\nInspect the original task: `{inspect}`."
            " Decide only within your existing authority; otherwise"
            " record who must decide on the original task.")
    message_key = (dedup_key or
                   f"escalation:{row['id']}:{row['chases_total'] + 1}:"
                   f"to:{supervisor}")
    message_key = f"{message_key}:attention-event={attention_event_id}"
    expected_notice_id = wp.latest_message_id(
        conn, row["id"], "escalation",
        at_or_before=route_observation_id)
    if supervisor != "operator":
        with conn:
            row_id = wp.record_msg(
                conn, row["id"], "escalation",
                message_key,
                supervisor, subject, body,
                expected_latest_id=expected_notice_id,
                expected_responsibility_version=
                row["responsibility_version"])
    elif not registry_trusted:
        with conn:
            wp.record_operator_queue_marker(
                conn, row["id"], "escalation",
                message_key + ":operator:unverified",
                subject, body, registry_trusted=registry_trusted,
                expected_latest_id=expected_notice_id,
                expected_responsibility_version=
                row["responsibility_version"])


    if row_id is not None:
        sent = wp.bus_send(conn, row_id)
    if supervisor == "operator":
        with conn:
            wp.record(conn, row["id"], "auto-note",
                      "operator-attention: escalation has no independent"
                      " parent owner, requester, or active commander;"
                      " original task is the operator item")
    elif not sent:


        with conn:
            wp.record(conn, row["id"], "auto-note",
                      "supervisor-unreachable: escalation alert not confirmed;"
                      " chase is the record; board flags this row")
    alert = ("original task placed in operator brief"
             if supervisor == "operator" else
             "alert accepted" if sent else
             "alert NOT confirmed - board flag set")
    log(f"OK escalated {row['id']} ({prefix}{reason}); {alert}")
    return True


def fire_mechanical(conn, row, event: str, note: str, dry: bool, *,
                    progress_hash: str | None = None) -> sqlite3.Row:
    if dry:
        log(f"DRY would fire {event} on {row['id']} ({note})")
        return row
    new_state = wp.step_row(row, event)
    terminal_failure = ""
    with conn:


        if progress_hash is None:
            changed = conn.execute(
                "UPDATE dispatch SET state=?, ask_flag=0, last_event=?"
                " WHERE id=? AND state=? AND responsibility_version=?",
                (new_state, wp.now(), row["id"], row["state"],
                 row["responsibility_version"]),
            )
        else:


            changed = conn.execute(
                "UPDATE dispatch SET state=?, ask_flag=0, last_event=?,"
                " progress_hash=? WHERE id=? AND state=?"
                " AND responsibility_version=?"
                " AND COALESCE(progress_hash,'')=?",
                (new_state, wp.now(), progress_hash, row["id"], row["state"],
                 row["responsibility_version"], row["progress_hash"] or ""),
            )
        if changed.rowcount != 1:
            log(f"NOTE skipped stale {event} for {row['id']}: task state,"
                " responsibility, or checked head changed")
            return wp.fetch(conn, row["id"])
        wp.record(conn, row["id"], event, note)
        if new_state in wp.workflow_spec(wp.row_workflow(row))["terminal"]:


            wp.claim_settle_terminal(conn, row["id"],
                                     row["resolution"] or "", via=event)
            closed_row = wp.fetch(conn, row["id"])
            notify_row = wp.terminal_notify(
                conn, closed_row, closed_row["resolution"] or "",
                closer="engine", via=event)
            if notify_row:
                msg = conn.execute(
                    "SELECT send_state, attempts, last_error FROM task_msg"
                    " WHERE id=?", (notify_row,)).fetchone()
                if msg["send_state"] == "invalid-target":
                    terminal_failure = msg["last_error"]
                else:


                    wp.wake_cause_ride(
                        conn, (row["requester_seat"] or "").strip(),
                        row["id"], "terminal")
    if new_state in wp.workflow_spec(wp.row_workflow(row))["terminal"]:


        wp.expire_task_msgs(conn, row["id"])
    if terminal_failure:
        log(f"WARN {row['id']} terminal notification stopped before"
            f" transport: {terminal_failure}")
    log(f"OK {row['id']} {event} -> {new_state}")
    return wp.fetch(conn, row["id"])


def guard_streak(conn, row, verdict: str) -> None:
    with conn:
        if verdict == wp.GUARD_UNKNOWN:
            conn.execute("UPDATE dispatch SET guard_unknown_streak ="
                         " guard_unknown_streak+1 WHERE id=?", (row["id"],))
        else:
            conn.execute("UPDATE dispatch SET guard_unknown_streak=0 WHERE id=?",
                         (row["id"],))


def tick_pr_guards(conn, dry: bool, *, pool_registry_fresh: bool = True) -> None:


    for row in conn.execute("SELECT * FROM dispatch WHERE workflow='pr' AND"
                            " state NOT IN ('closed')").fetchall():
        if row["done_cmd"]:


            verdict, first = wp.run_guard(row["done_cmd"])
            if not dry:
                guard_streak(conn, row, verdict)
            if verdict == wp.GUARD_TRUE:
                fire_mechanical(conn, row, "merged",
                                f"done guard (a human merged): {first}", dry)
                continue
        if row["state"] == "authoring" and row["ready_cmd"]:
            verdict, first = wp.run_guard(row["ready_cmd"])
            if not dry:
                guard_streak(conn, row, verdict)
            if verdict == wp.GUARD_TRUE:
                reviewer = row["reviewer_seat"] or ""
                pool_managed = bool(row["reviewer_pool"]) or (
                    reviewer.startswith("role:")
                    and reviewer.endswith(wp.POOL_SUFFIX))
                if (not dry and not pool_registry_fresh
                        and pool_managed):
                    log(f"WARN {row['id']} is ready for review but reviewer"
                        " pool selection awaits a current Agent Bus registry;"
                        " readiness will be checked again next tick")
                    continue
                if not dry and pool_managed:


                    row = pin_pool_reviewer(conn, row)
                    if wp.reviewer_pool_unavailable(conn, row):
                        log(f"WARN {row['id']} is ready for review but nobody"
                            f" currently holds the reviewer pool; readiness"
                            f" will be checked again next tick")
                        continue
                row = fire_mechanical(conn, row, "pr-ready",
                                      f"readiness guard: {first}", dry)
                if not dry and row["reviewer_seat"]:
                    wp.route(conn, row["id"], "review-request",
                             f"review-req:{row['id']}:r{row['round']}",
                             row["reviewer_seat"],
                             f"review requested: {row['subject']}"[:180],
                             f"Task {row['id']} is ready for review"
                             f" (readiness guard: {first}). Record your verdict:"
                             f" {wp.orc_command('review', 'verdict', row['id'])}"
                             f" blockers|clean --note '<findings or PR-review link>'",
                             row["parent_id"],
                             expected_responsibility_version=
                             row["responsibility_version"])
        elif row["state"] in ("fixing", "merge-pending") and row["check_cmd"]:
            verdict, new_hash = wp.run_progress(row["check_cmd"])
            if not dry:
                guard_streak(conn, row, verdict)
            if verdict == wp.GUARD_TRUE:
                moved = bool(row["progress_hash"]) and new_hash != row["progress_hash"]
                reviewer = row["reviewer_seat"] or ""
                pool_managed = bool(row["reviewer_pool"]) or (
                    reviewer.startswith("role:")
                    and reviewer.endswith(wp.POOL_SUFFIX))
                if (moved and not dry and not pool_registry_fresh
                        and pool_managed):
                    log(f"WARN {row['id']} has a new head but reviewer pool"
                        " selection awaits a current Agent Bus registry; the"
                        " old progress hash is retained for the next tick")
                    continue
                if moved:
                    stale_note = ("head moved while merge-pending; receipt stale"
                                  if row["state"] == "merge-pending"
                                  else "head moved after fixes")
                    if not dry and pool_managed:
                        row = pin_pool_reviewer(conn, row)
                        if wp.reviewer_pool_unavailable(conn, row):
                            log(f"WARN {row['id']} has a new head but nobody"
                                f" currently holds the reviewer pool; the old"
                                f" progress hash is retained for the next tick")
                            continue
                    row = fire_mechanical(
                        conn, row, "head-moved", stale_note, dry,
                        progress_hash=new_hash,
                    )
                    if not dry and row["reviewer_seat"]:
                        wp.route(conn, row["id"], "review-request",
                                 f"review-req:{row['id']}:r{row['round']}:h{new_hash[:8]}",
                                 row["reviewer_seat"],
                                 f"re-review: {row['subject']}"[:180],
                                 f"Task {row['id']}: {stale_note}. Record your verdict"
                                 f" when done: {wp.orc_command('review', 'verdict', row['id'])} blockers|clean --note"
                                 f" '<findings or PR-review link>'", row["parent_id"],
                                 expected_responsibility_version=
                                 row["responsibility_version"])
                elif not dry and not row["progress_hash"]:


                    with conn:
                        conn.execute(
                            "UPDATE dispatch SET progress_hash=? WHERE id=?",
                            (new_hash, row["id"]),
                        )


def tick_parents(conn, dry: bool, *, registry_trusted: bool = True,
                 route_observation_id: int | None = None) -> None:
    for row in conn.execute("SELECT * FROM dispatch WHERE workflow='parent' AND"
                            " state != 'closed'").fetchall():
        if dry:
            kids = wp.children(conn, row["id"])
            open_kids = [k for k in kids if k["state"] != "closed"]
            if row["state"] == "running" and kids and not open_kids:
                log(f"DRY would roll up {row['id']} -> ready-to-close")
            continue
        fired = wp.rollup(conn, row)
        row = wp.fetch(conn, row["id"])
        if row["state"] == "ready-to-close":


            review_event = conn.execute(
                "SELECT id FROM event WHERE dispatch_id=?"
                " AND kind='children-closed' ORDER BY id DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            if review_event is not None:
                expected_notice_id = wp.latest_message_id(
                    conn, row["id"], "goal-review",
                    at_or_before=route_observation_id)
                reviewer = wp.goal_review_recipient(
                    conn, row, registry_trusted=registry_trusted)
                body = (f"All children of goal {row['id']} are closed."
                        f" Re-review the goal; close it if actually met,"
                        f" or open new child tasks if more work is needed.")
                subject = f"goal ready for review: {row['subject']}"[:180]
                if reviewer != "operator":
                    with conn:
                        row_id = wp.record_msg(
                            conn, row["id"], "goal-review",
                            f"goal-review:{row['id']}:"
                            f"attention-event={review_event['id']}",
                            reviewer, subject, body,
                            expected_latest_id=expected_notice_id,
                            expected_responsibility_version=
                            row["responsibility_version"],
                        )
                    if row_id is not None:
                        wp.bus_send(conn, row_id)
                elif not registry_trusted:
                    with conn:
                        wp.record_operator_queue_marker(
                            conn, row["id"], "goal-review",
                            f"goal-review:{row['id']}:"
                            f"attention-event={review_event['id']}:"
                            "operator:unverified", subject, body,
                            registry_trusted=registry_trusted,
                            expected_latest_id=expected_notice_id,
                            expected_responsibility_version=
                            row["responsibility_version"])
        if fired == "children-closed":
            log(f"OK parent {row['id']} -> ready-to-close (re-review, never auto-close)")


def author_exclusion(conn, row) -> set:


    identities, _unknown = wp.owner_review_identities(conn, row)
    return identities


def pin_pool_reviewer(conn, row) -> "sqlite3.Row":


    reviewer = row["reviewer_seat"] or ""
    role_pool = (reviewer.startswith("role:")
                 and reviewer.endswith(wp.POOL_SUFFIX))
    pool = reviewer[5:] if role_pool else (row["reviewer_pool"] or "")
    if not pool:
        return row
    authors, author_unknown = wp.owner_review_identities(conn, row)
    if author_unknown:
        log(f"WARN {row['id']} reviewer pool waits: the actual historical"
            " author identity is unknown")
        return row
    if (not role_pool and reviewer not in authors
            and reviewer in set(wp.role_holders(conn, pool))
            and conn.execute(
                "SELECT 1 FROM seat WHERE agent_id=? AND addressable=1",
                (reviewer,),
            ).fetchone() is not None):
        return row
    member = wp.pool_pick(conn, pool, exclude=authors)
    if member is None:


        return row
    with conn:
        changed = conn.execute(
            "UPDATE dispatch SET reviewer_seat=?, reviewer_pool=?"
            " WHERE id=? AND state=? AND responsibility_version=?"
            " AND reviewer_seat=? AND reviewer_pool=?",
            (member, pool, row["id"], row["state"],
             row["responsibility_version"], row["reviewer_seat"],
             row["reviewer_pool"]),
        )
        if changed.rowcount != 1:
            log(f"NOTE skipped stale reviewer pin for {row['id']}:"
                " the review assignment changed")
            return wp.fetch(conn, row["id"])
        wp.record(conn, row["id"], "auto-note",
                  f"reviewer-pinned: {member} (least-loaded of {pool})")
    log(f"OK {row['id']} reviewer pinned to {member} (least-loaded of {pool})")
    return wp.fetch(conn, row["id"])


def review_request_unclaimed(conn, row, pane_probe=None) -> bool:


    msg = conn.execute(
        "SELECT msg_id,target,recipient_agent_id,at_ms,processed,send_state"
        " FROM task_msg WHERE task_id=?"
        " AND purpose='review-request'"
        " ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
    if (not msg or msg["send_state"] != "accepted" or not msg["msg_id"]
            or wp.now() - msg["at_ms"] < HANDSHAKE_TIMEOUT_S):
        return False
    if msg["processed"] == "ok":
        return False
    actual = wp.message_recipient_agent_id(
        msg["msg_id"], msg["recipient_agent_id"], msg["target"])
    if not actual:
        return False
    current = wp.resolve_owed_recipient(conn, row)
    current_id = (current.get("recipient_agent_id")
                  or current.get("agent_id") or "")
    if current.get("deferred") or not current_id or actual != current_id:
        return False
    context = wp.continuation_context(conn, row)
    if (context is not None and wp.current_continuation_voice(
            conn, row, context,
            at_or_after=msg["at_ms"]) is not None):
        return False
    resolved = wp.resolve_recipient(conn, actual, row["parent_id"])
    window = resolved["window"]
    if window is None:
        return False
    if pane_probe is None:
        pane_probe = _pane_probe_for(window, member=resolved)
    return pane_probe() is False


def tick_reviewer_rotation(conn, dry: bool,
                           cycle_floor_event_id: int | None = None, *,
                           registry_trusted: bool = True,
                           route_observation_id: int | None = None) -> int:


    rotated = 0
    for row in conn.execute(
            "SELECT * FROM dispatch WHERE workflow='pr' AND"
            " state='awaiting-review' AND reviewer_pool != ''").fetchall():
        if wp.dispatch_undelivered(conn, row["id"]):
            continue


        ask_event = wp.current_ask_event(conn, row)
        if ask_event is not None:
            continue
        current_label = row["reviewer_seat"]
        current_resolved = wp.resolve_delivered_recipient(
            conn, row, current_label, ("review-request",))
        if current_resolved.get("deferred"):
            log(f"WARN reviewer rotation of {row['id']} waits: the actual"
                " current reviewer identity is unknown")
            continue
        current = current_resolved["agent_id"] or current_resolved["seat"]
        context = wp.continuation_context(conn, row)
        if context is None or context.get("deferred"):
            continue
        observed_voice = wp.current_continuation_voice(conn, row, context)
        observed_voice_id = int(observed_voice["id"]) if observed_voice else 0
        last_chase = conn.execute(
            "SELECT COALESCE(MAX(id),0) FROM event WHERE dispatch_id=?"
            " AND responsibility_version=? AND kind IN ('chase','auto-chase')",
            (row["id"], row["responsibility_version"]),
        ).fetchone()[0]
        silent = (row["chases"] >= 2
                  and observed_voice_id <= int(last_chase or 0))
        unclaimed = not silent and review_request_unclaimed(conn, row)
        if not (silent or unclaimed):
            continue
        if silent:
            reason = f"reviewer silent through {row['chases']} chases"
        else:
            reason = (f"review request unclaimed {HANDSHAKE_TIMEOUT_S}s past"
                      f" send with the pane idle (frozen-seat guard)")
        authors, author_unknown = wp.owner_review_identities(conn, row)
        if author_unknown:
            log(f"WARN reviewer rotation of {row['id']} waits: the actual"
                " historical author identity is unknown")
            continue
        nxt = wp.pool_pick(conn, row["reviewer_pool"],
                           exclude={current, *authors})
        if dry:
            log(f"DRY would rotate reviewer of {row['id']}: {reason} ->"
                f" {nxt or 'POOL EXHAUSTED'}")
            continue
        if nxt is None:
            escalate(conn, row, f"review rotation exhausted: {reason} and no"
                     f" other {row['reviewer_pool']} member is available -"
                     f" grow the pool or assign a reviewer by hand", dry,
                     dedup_key=(f"review-pool-exhausted:{row['id']}:"
                                f"{row['chases_total'] + 1}"),
                     cycle_floor_event_id=cycle_floor_event_id,
                     registry_trusted=registry_trusted,
                     route_observation_id=route_observation_id,
                     expected_generation=context["generation"],
                     expected_voice_event_id=observed_voice_id)
            continue
        with conn:
            locked = conn.execute(
                "UPDATE dispatch SET last_event=last_event WHERE id=?"
                " AND state='awaiting-review'"
                " AND responsibility_version=? AND reviewer_seat=?"
                " AND reviewer_pool=?",
                (row["id"], row["responsibility_version"], current_label,
                 row["reviewer_pool"]),
            )
            if locked.rowcount != 1:
                log(f"NOTE skipped stale reviewer rotation for {row['id']}:"
                    " the review responsibility changed")
                continue
            current_row = wp.fetch(conn, row["id"])
            current_context = wp.continuation_context(conn, current_row)
            latest_voice = wp.current_continuation_voice(
                conn, current_row, current_context)
            latest_voice_id = int(latest_voice["id"]) if latest_voice else 0
            if (current_context is None
                    or current_context.get("deferred")
                    or current_context["generation"] != context["generation"]
                    or latest_voice_id != observed_voice_id
                    or wp.current_ask_event(conn, current_row) is not None):
                log(f"NOTE skipped stale reviewer rotation for {row['id']}:"
                    " the reviewer responded or the next action changed")
                continue
            changed = conn.execute(
                "UPDATE dispatch SET reviewer_seat=?, chases=0, ask_flag=0,"
                " last_event=? WHERE id=? AND state='awaiting-review'"
                " AND responsibility_version=? AND reviewer_seat=?"
                " AND reviewer_pool=?",
                (nxt, wp.now(), row["id"], row["responsibility_version"],
                 current_label, row["reviewer_pool"]),
            )
            if changed.rowcount != 1:
                log(f"NOTE skipped stale reviewer rotation for {row['id']}:"
                    " the review responsibility changed")
                continue
            n_rot = conn.execute(
                "SELECT COUNT(*) FROM event WHERE dispatch_id=? AND note LIKE"
                " 'reviewer-rotated:%'", (row["id"],)).fetchone()[0] + 1
            wp.record(conn, row["id"], "auto-note",
                      f"reviewer-rotated: {current_label} -> {nxt} ({reason})")
            conn.execute("DELETE FROM drive WHERE task_id=? AND seat=?",
                         (row["id"], current))
        routed_row = wp.fetch(conn, row["id"])
        wp.route(conn, routed_row["id"], "review-request",
                 f"review-req:{routed_row['id']}:rot{n_rot}", nxt,
                 f"review request (rotated to you):"
                 f" {routed_row['subject']}"[:180],
                 f"Task {routed_row['id']} rotated to you: the previous reviewer"
                 f" ({current_label}) {reason}. Record your verdict:"
                 f" {wp.orc_command('review', 'verdict', routed_row['id'])}"
                 " blockers|clean"
                 f" --note '<findings or PR-review link>'",
                 routed_row["parent_id"],
                 expected_responsibility_version=
                 routed_row["responsibility_version"])
        log(f"OK rotated reviewer of {row['id']}: {current_label} -> {nxt} ({reason})")
        rotated += 1
    return rotated


def gh_cli() -> str:


    return os.environ.get("NW_GH_CLI") or "gh"


GH_OWNER = cfg.get("github.owner", "")


RECONCILE_CAP_PER_TICK = 5


SANCTIONED_LOGINS_FILE = cfg.path("github.sanctioned_logins_file")


def _sanctioned_logins() -> set[str]:


    value = os.environ.get("NW_REVIEW_SANCTIONED_FILE") or SANCTIONED_LOGINS_FILE
    if value is None:
        return set()
    path = Path(value)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return set()
    if not isinstance(data, list):
        return set()
    return {x for x in data if isinstance(x, str) and x}


def tick_review_reconcile(conn, dry: bool) -> int:


    sanctioned = _sanctioned_logins()
    if not GH_OWNER or not sanctioned:
        return 0
    fired = 0
    looked = 0
    for row in conn.execute(
            "SELECT * FROM dispatch WHERE workflow='pr' AND"
            " state='awaiting-review' ORDER BY last_event ASC").fetchall():
        if looked >= RECONCILE_CAP_PER_TICK:
            break
        m = re.match(r"^([a-z0-9-]+)#(\d+)$", (row["links"] or "").strip())
        if not m or m.group(1) not in wp.MERGE_KEYS:
            continue
        repo, num = m.group(1), m.group(2)
        looked += 1
        try:
            out = subprocess.run(
                [gh_cli(), "api",
                 f"repos/{GH_OWNER}/{repo}/pulls/{num}/reviews"],
                text=True, capture_output=True, timeout=30)
            head_out = subprocess.run(
                [gh_cli(), "pr", "view", num, "--repo", f"{GH_OWNER}/{repo}",
                 "--json", "headRefOid", "--jq", ".headRefOid"],
                text=True, capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if out.returncode != 0 or head_out.returncode != 0:
            continue
        head = (head_out.stdout or "").strip()
        try:
            reviews = json.loads(out.stdout or "[]")
        except ValueError:
            continue
        at_head = [r for r in reviews
                   if isinstance(r, dict)
                   and r.get("state") in ("APPROVED", "CHANGES_REQUESTED")
                   and r.get("commit_id") == head]
        if not head or not at_head:
            continue

        def _login(r: dict) -> str:
            return (r.get("user") or {}).get("login") or "?"

        hit = next((r for r in at_head if _login(r) in sanctioned), None)
        foreign = sorted({_login(r) for r in at_head
                          if _login(r) not in sanctioned})
        if foreign:


            if dry:
                log(f"DRY {row['id']} foreign-review: unsanctioned"
                    f" login(s) {', '.join(foreign)} at head {head[:9]}")
            else:
                with conn:
                    msg_row = wp.record_msg(
                        conn, row["id"], "foreign-review",
                        f"foreign-review:{row['id']}:{head}",
                        "role:commander",
                        f"foreign forge review on {repo}#{num} at the"
                        f" current head (task {row['id']})"[:180],
                        f"Login(s) {', '.join(foreign)} - not in"
                        f" review-sanctioned-logins.json - left"
                        f" {'/'.join(sorted({r['state'] for r in at_head if _login(r) not in sanctioned}))}"
                        f" review(s) at the CURRENT head {head} of"
                        f" {repo}#{num} while task {row['id']} is"
                        f" awaiting-review. No reviewer seat was messaged."
                        f" Decide whether the login is sanctioned (edit the"
                        f" map) or the review is noise/hostile.",
                        expected_responsibility_version=
                        row["responsibility_version"])
                    if msg_row is not None:
                        wp.record(conn, row["id"], "foreign-review",
                                  f"engine: unsanctioned login(s)"
                                  f" {', '.join(foreign)} reviewed head"
                                  f" {head}; commander alerted, reviewer"
                                  f" seat NOT messaged")
                        log(f"OK {row['id']} foreign-review:"
                            f" {', '.join(foreign)} at {head[:9]},"
                            f" commander alerted")
                        fired += 1
        if hit is None:
            continue
        login = _login(hit)
        if dry:
            log(f"DRY {row['id']} review-desync: {hit['state']} by"
                f" sanctioned {login} at head {head[:9]} with no ledger"
                f" verdict")
            continue
        with conn:


            expected_notice_id = wp.latest_message_id(
                conn, row["id"], "review-desync")
            msg_row = wp.record_msg(
                conn, row["id"], "review-desync",
                f"reconcile:{row['id']}:{head}:v"
                f"{row['responsibility_version']}", row["reviewer_seat"],
                f"review-desync on {repo}#{num}: forge review by {login}"
                f" has no ledger verdict (task {row['id']})"[:180],
                f"The forge shows {hit['state']} at the CURRENT head"
                f" {head} by sanctioned login {login}, while task"
                f" {row['id']} is still awaiting-review - the ledger"
                f" verdict is the missing artifact. VERIFY the actual"
                f" review before recording any verdict: confirm the"
                f" review really happened from this seat and re-derive"
                f" the direction from its content, then record:\n"
                f"  orc verdict {row['id']} <clean|blockers> --note '...'\n"
                f"If this seat made no such review, say so on the task"
                f" instead of recording anything.",
                expected_latest_id=expected_notice_id,
                expected_responsibility_version=
                row["responsibility_version"],
            )
            if msg_row is None:
                continue
            wp.record(conn, row["id"], "review-desync",
                      f"engine: forge shows {hit['state']} by sanctioned"
                      f" {login} at head {head} with no ledger verdict;"
                      f" named-artifact chase sent to"
                      f" {row['reviewer_seat']}")


        log(f"OK {row['id']} review-desync: {hit['state']} by {login} at"
            f" {head[:9]}, reviewer chased BY NAME for the missing verdict")
        fired += 1
    return fired


DEADLINE_PREFIX = "DEADLINE OVERDUE: "


def tick_deps(conn, dry: bool) -> int:


    advanced = 0
    for row in conn.execute("SELECT * FROM dispatch WHERE state=?",
                            (wp.WAITING_STATE,)).fetchall():
        preds = wp.predecessors(conn, row["id"])
        still_open = [p for p in preds if not wp.is_closed(p)]
        if still_open:
            if dry:
                log(f"DRY {row['id']} still waits on {len(still_open)} of"
                    f" {len(preds)} predecessor(s):"
                    f" {', '.join(p['id'] for p in still_open)}")
            continue
        if preds:
            note = f"{len(preds)} predecessor(s) closed: " + ", ".join(
                f"{p['id']}:{p['resolution'] or 'no resolution'}" for p in preds)
            odd = [p["id"] for p in preds
                   if p["resolution"] in ("dropped", "superseded")]
            if odd:
                note += (f"; note the work ahead ended without being done"
                         f" ({', '.join(odd)}) - read this task in that light")
        else:


            note = "no predecessor rows remain"
        if dry:
            log(f"DRY would open {row['id']} ({note})")
            continue
        wf = wp.row_workflow(row)
        new_state = wp.step(wf, wp.WAITING_STATE, wp.EVENT_DEPS_CLEARED)
        after_s = row["after_s"] or wp.parse_after(wp.DEFAULT_AFTER)
        with conn:
            conn.execute("UPDATE dispatch SET state=?, ask_flag=0, last_event=?,"
                         " check_after=? WHERE id=?",
                         (new_state, wp.now(), wp.now() + after_s, row["id"]))
            wp.record(conn, row["id"], wp.EVENT_DEPS_CLEARED, note)
        log(f"OK {row['id']} deps-cleared -> {new_state} ({note})")
        advanced += 1
        row = wp.fetch(conn, row["id"])
        if row["deferred_dispatch"]:

            sent = send_dispatch_message(conn, row)
            with conn:
                conn.execute("UPDATE dispatch SET deferred_dispatch=0 WHERE id=?",
                             (row["id"],))
            log(f"OK held dispatch message for {row['id']} "
                + ("accepted" if sent else
                   "NOT delivered - the resend pass keeps trying"))


    for row in conn.execute(
            "SELECT * FROM dispatch WHERE deferred_dispatch=1 AND state!=?",
            (wp.WAITING_STATE,)).fetchall():
        if dry:
            log(f"DRY would rescue the stranded held dispatch of {row['id']}")
            continue
        if wp.is_closed(row):


            with conn:
                conn.execute("UPDATE dispatch SET deferred_dispatch=0"
                             " WHERE id=?", (row["id"],))
            log(f"NOTE stranded held dispatch of closed {row['id']}"
                " cleared unsent (moot)")
            continue

        sent = send_dispatch_message(conn, row)
        with conn:
            conn.execute("UPDATE dispatch SET deferred_dispatch=0 WHERE id=?",
                         (row["id"],))
            wp.record(conn, row["id"], "auto-note",
                      "stranded held dispatch rescued by the tick sweep"
                      " (crash between deps-cleared and send)")
        log(f"OK rescued the stranded held dispatch of {row['id']} "
            + ("accepted" if sent else
               "NOT delivered - the resend pass keeps trying"))
    return advanced


def tick_deadlines(conn, dry: bool,
                   cycle_floor_event_id: int | None = None, *,
                   registry_trusted: bool = True,
                   route_observation_id: int | None = None) -> int:


    fired = 0
    for row in conn.execute(
            "SELECT * FROM dispatch WHERE deadline_ms > 0 AND deadline_ms <= ?"
            " ORDER BY deadline_ms ASC", (wp.now(),)).fetchall():
        if wp.is_closed(row):
            continue
        spec = wp.workflow_spec(wp.row_workflow(row))
        if (row["state"] in spec.get("operator_gated", ())
                and wp.merge_key_role(row["repo"]) == wp.OPERATOR_ROLE):


            continue
        context = wp.continuation_context(
            conn, row, registry_trusted=registry_trusted)
        if (context is not None and context["kind"] == "work"
                and wp.dispatch_undelivered(conn, row["id"])):
            continue
        ask_event = wp.current_ask_event(conn, row)
        if ask_event is not None:


            continue
        generation = context["generation"] if context is not None else ""
        observed_voice = wp.current_continuation_voice(conn, row, context)
        observed_voice_id = int(observed_voice["id"]) if observed_voice else 0


        last_row = conn.execute(
            "SELECT MAX(at_ms) FROM event WHERE dispatch_id=?"
            " AND responsibility_version=? AND kind IN ('auto-chase','chase')"
            " AND continuation_generation=?"
            " AND note LIKE ?",
            (row["id"], row["responsibility_version"], generation,
             f"engine: {DEADLINE_PREFIX}%"),
        ).fetchone()
        last = int(last_row[0]) if last_row and last_row[0] else None
        if last is not None and (wp.now() - last) < wp.DEADLINE_COOLDOWN_S:
            continue
        over = wp.human_age(wp.now() - int(row["deadline_ms"]))
        if row["state"] == wp.WAITING_STATE:
            blockers = ", ".join(p["id"] for p in
                                 wp.open_predecessors(conn, row["id"]))
            reason = (f"deadline passed {over} ago and this task has not even"
                      f" started - it is still waiting on"
                      f" {blockers or 'predecessors that never closed'}."
                      f" The plan promised a date the dependency graph could not"
                      f" meet; re-plan it rather than chasing a seat")
        else:
            action = (f"{context['seat']} must {context['label']}"
                      if context is not None else
                      "no current next-action recipient is recorded")
            reason = (f"deadline passed {over} ago with the task still"
                      f" {row['state']}; {action}")


        n_before = conn.execute(
            "SELECT COUNT(*) FROM event WHERE dispatch_id=? AND kind IN"
            " ('chase','auto-chase') AND responsibility_version=?"
            " AND continuation_generation=?"
            " AND note LIKE ?",
            (row["id"], row["responsibility_version"], generation,
             f"engine: {DEADLINE_PREFIX}%")).fetchone()[0]
        generation_key = (wp.continuation_token(generation)
                          if generation else "no-action")
        if escalate(conn, row, reason, dry, prefix=DEADLINE_PREFIX,
                    dedup_key=(f"deadline:{row['id']}:generation-"
                               f"{generation_key}:n{n_before + 1}"),
                    cycle_floor_event_id=cycle_floor_event_id,
                    registry_trusted=registry_trusted,
                    route_observation_id=route_observation_id,
                    expected_generation=generation,
                    expected_voice_event_id=observed_voice_id):
            fired += 1
    return fired


def tick_breakers(conn, dry: bool) -> int:


    fired = 0
    for row in conn.execute(
            "SELECT * FROM dispatch WHERE breaker_cmd != '' AND check_cmd != ''"
            " AND state NOT IN ('closed', ?)", (wp.WAITING_STATE,)).fetchall():
        verdict, first = wp.run_guard(row["check_cmd"])
        streak = int(row["check_fail_streak"])
        if verdict == wp.GUARD_TRUE:
            new_streak = 0
        elif verdict == wp.GUARD_FALSE:
            new_streak = streak + 1
        else:
            new_streak = streak
        if dry:
            log(f"DRY {row['id']} breaker check {verdict}: streak {streak} ->"
                f" {new_streak} of {wp.BREAKER_FAIL_STREAK} ({first})")
            continue
        if new_streak != streak:
            with conn:
                conn.execute("UPDATE dispatch SET check_fail_streak=? WHERE id=?",
                             (new_streak, row["id"]))
        if new_streak < wp.BREAKER_FAIL_STREAK:
            continue
        last = wp.last_event_at(conn, row["id"], wp.EVENT_BREAKER_FIRED)
        if last is not None and (wp.now() - last) < wp.BREAKER_COOLDOWN_S:
            log(f"NOTE breaker held for {row['id']}: it fired"
                f" {wp.human_age(wp.now() - last)} ago and fires at most once"
                f" per {wp.BREAKER_COOLDOWN_S // 3600}h")
            continue
        code, out = wp.run_breaker(row["breaker_cmd"])
        with conn:
            wp.record(conn, row["id"], wp.EVENT_BREAKER_FIRED,
                      f"exit {code} after {new_streak} consecutive failing checks;"
                      f" last check said: {first}; breaker output: {out}")
            conn.execute("UPDATE dispatch SET check_fail_streak=0, last_event=?"
                         " WHERE id=?", (wp.now(), row["id"]))
        log(f"OK breaker fired for {row['id']}: exit {code} ({out[:80]})")
        fired += 1
    return fired


WATCHED_CHECKOUTS = tuple(cfg.get("watched_repositories", []))


def checkout_findings(repo: dict) -> list[str]:


    import subprocess as sp
    from datetime import datetime, timezone as tz
    root = Path(cfg.expand(repo["path"]))
    if not root.exists():
        return []
    try:
        if repo["kind"] == "bare-hub":
            out = sp.run(["git", "-C", str(root), "rev-parse", "--is-bare-repository"],
                         text=True, capture_output=True, timeout=15)
            if out.returncode == 0 and out.stdout.strip() == "false":
                return ["NON-BARE"]
            return []


        out = sp.run(["git", "-C", str(root), "status", "--porcelain",
                      "--ignored=no", "--untracked-files=all"],
                     text=True, capture_output=True, timeout=30)
        if out.returncode != 0:
            return []
    except (OSError, sp.TimeoutExpired):
        return []
    findings = []
    for line in out.stdout.splitlines():
        if len(line) < 4:
            continue
        rel = line[3:].split(" -> ")[-1].strip().strip('"')
        if any(rel.startswith(e) for e in repo.get("exempt", ())):
            continue
        f = root / rel
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz.utc)                 .isoformat(timespec="seconds")
        except OSError:
            mtime = "(gone)"
        findings.append(f"{line[:2]} {rel}\t{mtime}")
    return findings


def tick_checkout_hygiene(conn, dry: bool) -> None:
    import hashlib as hl
    for repo in WATCHED_CHECKOUTS:
        findings = checkout_findings(repo)
        if not findings:
            continue
        digest = hl.sha1("\n".join(sorted(findings)).encode()).hexdigest()[:12]
        day = wp.now() // 86400
        dedup = f"hygiene:{repo['path']}:{digest}:{day}"
        if dry:
            log(f"DRY hygiene: {repo['path']} dirty ({len(findings)} finding(s))")
            continue
        commander = "role:commander"
        shown = findings[:50]
        more = f"\n... and {len(findings) - 50} more" if len(findings) > 50 else ""
        body = (f"The shared checkout {repo['path']} is not clean. Paths"
                f" with mtimes (fresh evidence for attribution - correlate"
                f" with pane activity now, it decays fast):\n\n"
                + "\n".join(shown) + more
                + "\n\nExempt prefixes honored. One alert per path-set"
                f" per day; changes to the set re-alert.")
        with conn:
            row_id = wp.record_msg(conn, "hygiene", "checkout-dirty", dedup,
                                   commander,
                                   f"shared checkout dirty: {repo['path']}"[:160],
                                   body)
        if row_id is None:
            continue
        wp.bus_send(conn, row_id)
        log(f"OK hygiene alert: {repo['path']} ({len(findings)} finding(s))")


def continuation_pairs(conn, *, registry_trusted: bool = True) -> list[tuple]:

    pairs = []
    for row in open_tasks(conn):
        context = wp.continuation_context(
            conn, row, registry_trusted=registry_trusted)
        if context and context["requested"].strip().lower() != "operator":
            pairs.append((row, context))
    return pairs


def cmd_tick(args: argparse.Namespace) -> int:
    dry = args.dry_run
    if not dry:
        lock_file = nw_paths.lock_path("fleet-orchestrator")
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = lock_file.open("w")
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_fd.close()
            log("SKIP another fleet-orchestrator tick holds the lock")
            return 0

    conn = wp.connect_readonly() if dry else wp.connect_writable()
    cycle_floor_event_id = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM event"
    ).fetchone()[0]
    route_observation_id = conn.execute(
        "SELECT COALESCE(MAX(id), 0) FROM task_msg"
    ).fetchone()[0]
    registry_fresh = wp.members_available(conn)
    if not dry:
        snap = snapshot_db()
        if snap:
            log(f"OK daily DB snapshot: {snap}")
    if not registry_fresh:
        log("WARN Agent Bus registry unavailable; membership is unknown and"
            " identity-dependent actions are skipped")

    tick_parents(
        conn, dry, registry_trusted=(dry or registry_fresh),
        route_observation_id=route_observation_id)
    tick_pr_guards(conn, dry, pool_registry_fresh=(dry or registry_fresh))
    tick_review_reconcile(conn, dry)
    local_session = _automatic_local_fleet()
    if not local_session:
        tick_checkout_hygiene(conn, dry)
    if dry or registry_fresh:
        tick_seat_liveness(conn, dry, members=conn.members)
    if not dry and not wp.wake_shadow_off():


        with conn:
            wp.wake_sweep(conn)


    breakers_fired = tick_breakers(conn, dry)
    advanced = tick_deps(conn, dry)
    deadlines = tick_deadlines(
        conn, dry, cycle_floor_event_id=cycle_floor_event_id,
        registry_trusted=(dry or registry_fresh),
        route_observation_id=route_observation_id)
    if dry or registry_fresh:
        tick_reviewer_rotation(
            conn, dry, cycle_floor_event_id=cycle_floor_event_id,
            registry_trusted=registry_fresh,
            route_observation_id=route_observation_id)
    restored_messages = 0
    if not dry:
        restored_messages = wp.repair_missing_responsibility_messages(conn, log)
        restored_messages += wp.repair_standing_claim_notifications(
            conn, log, registry_trusted=registry_fresh,
            route_observation_id=route_observation_id)
        restored_messages += wp.repair_attention_notifications(
            conn, log, registry_trusted=registry_fresh,
            route_observation_id=route_observation_id)

    tmux_send = load_script("agent-tmux-send.py", "agent_tmux_send_for_orc")

    pairs = continuation_pairs(
        conn, registry_trusted=(dry or registry_fresh))


    tmux_observable = dry or registry_fresh
    tmux_error = ""
    pane_snapshot: list[tuple[str, str]] = []
    if tmux_observable:
        try:
            pane_snapshot = pane_sense.agent_panes()
        except RuntimeError as exc:
            tmux_observable = False
            tmux_error = str(exc)
            log(f"WARN tmux observation unavailable on {pane_sense.tmux_runtime.identity()}:"
                f" {tmux_error}; pane nudges and absence counters suppressed")


    sensed: dict[str, tuple[str, bool] | None] = {}
    unknown_windows: set[str] = set()
    for row, context in pairs:
        window = context["window"]
        target = context.get("pane_id") or window
        if window is None or target in sensed or not tmux_observable:
            continue
        pane = _registered_pane(context, pane_snapshot)
        if pane is None:
            sensed[target] = None
            continue
        pane_id, location = pane
        try:
            text = pane_sense.capture(pane_id)
        except RuntimeError as e:
            log(f"WARN capture failed for window {window}: {e};"
                f" observation unknown, absence counter preserved")
            unknown_windows.add(target)
            continue
        busy = pane_sense.detect_busy(text)
        sensed[target] = (pane_id, busy)

    fired = escalated = 0


    seat_nudges: dict[str, dict] = {}
    live_tasks = {row["id"] for row, _ in pairs}


    seat_open_tasks: dict[str, list[str]] = {}
    for row, context in pairs:
        seat_open_tasks.setdefault(context["seat"], []).append(row["id"])
    seat_activity: dict[str, bool] = {}
    for row, context in pairs:
        owed = context["requested"]
        seat_key = context["seat"]
        generation = context["generation"]
        if not dry:
            with conn:
                wp.activate_continuation_generation(
                    conn, row["id"], seat_key, generation)
                wp.claim_standing(conn, row)
        drv = conn.execute(
            "SELECT * FROM drive WHERE task_id=? AND seat=? AND generation=?",
            (row["id"], seat_key, generation),
        ).fetchone()
        entry = {k: drv[k] for k in ("st", "cycles", "grace_used", "idle_waits")} \
            if drv else {}
        absent_ticks = drv["absent_ticks"] if drv else 0


        if entry.get("st") in {"seat-cannot", "decision-pending"}:
            entry = {}
        if context.get("deferred"):
            if dry:
                log(f"DRY {row['id']}: continuation recipient unresolved -"
                    f" {context['deferred']}")
            continue
        if (context["kind"] == "work"
                and wp.dispatch_undelivered(conn, row["id"])):


            if dry:
                log(f"DRY {row['id']}: dispatch undelivered - ladder suppressed")
            continue


        ask_event = wp.current_ask_event(conn, row)
        if ask_event is not None:
            first_wait = entry.get("st") != wp.S_WAITING
            waiting_entry = {"st": wp.S_WAITING,
                             "cycles": entry.get("cycles", 0)}
            if first_wait:
                if escalate(
                        conn, row,
                        f"seat {owed} reports blocked on a human"
                        " (ask marker active); continuation reminders are"
                        " held while it stands",
                        dry, cycle_floor_event_id=cycle_floor_event_id,
                        registry_trusted=(dry or registry_fresh),
                        route_observation_id=route_observation_id,
                        expected_generation=generation,
                        expected_ask_event_id=int(ask_event["id"]),
                        drive_update=(seat_key, generation, waiting_entry, 0)):
                    escalated += 1
            elif dry:
                log(f"DRY {row['id']} seat {owed}: blocked marker active ->"
                    " holding quiet")
            continue
        if entry.get("st") == wp.S_WAITING:


            entry = {}
            if not dry:
                save_drive(conn, row["id"], seat_key, generation, entry, 0)
        if context["kind"] == "work" and row["no_chase"]:


            continue


        observed_voice = wp.current_continuation_voice(conn, row, context)
        observed_voice_id = int(observed_voice["id"]) if observed_voice else 0
        spoke = bool(observed_voice
                     and int(observed_voice["at_ms"])
                     > wp.now() - wp.LEDGER_SPEECH_S)
        if spoke:
            _ignored, entry = wp.step_drive(entry, False, spoke=True)
            if not dry:
                with conn:
                    wp.wake_attempt_resolve(conn, row["id"], seat_key,
                                            "reacted-voice")
        window = context["window"]
        target = context.get("pane_id") or window
        pane_info = sensed.get(target) if target else None

        if (context.get("terminal_presence") == "unknown"
                or (window is not None
                    and (not tmux_observable or target in unknown_windows))):
            if spoke and not dry:
                save_drive(conn, row["id"], seat_key, generation, entry,
                           absent_ticks)
            if dry:
                log(f"DRY {row['id']} seat {owed}: tmux observation unknown;"
                    f" preserving pane/drive state")
            continue

        if pane_info is None:


            if window is None or context.get("agent_id") is not None:
                if context.get("agent_id") is None:
                    log(f"WARN {row['id']}: continuation recipient '{owed}'"
                        " has no current addressable Agent Bus identity;"
                        " no reminder target will be guessed")
                    if spoke and not dry:
                        save_drive(conn, row["id"], seat_key, generation,
                                   entry, absent_ticks)
                    continue
                if seat_key not in seat_activity:
                    seat_activity[seat_key] = any(
                        wp.continuation_spoke_recently(
                            conn, task_id, window_s=wp.SEAT_ACTIVITY_WINDOW_S)
                        for task_id in seat_open_tasks.get(seat_key, [])
                    )
                idle_limit = idle_wait_limit_for(
                    row["subject"], seat_activity[seat_key])
                action, next_entry = wp.step_drive(
                    entry, False, idle_wait_limit=idle_limit, spoke=spoke)
                if dry:
                    log(f"DRY {row['id']} bus-only seat {owed}:"
                        f" state={entry.get('st', 'dispatched')}"
                        f" -> action={action}")
                    continue
                if action != "escalate":
                    save_drive(conn, row["id"], seat_key, generation,
                               next_entry, 0)
                if action == "pull":
                    fired += send_bus_continuation_reminder(
                        conn, row, context)
                elif action == "escalate":
                    if escalate(
                            conn, row,
                            f"seat {owed} has not responded while it must"
                            f" {context['label']} after"
                            f" {entry.get('idle_waits', 0)} continuation"
                            " checks",
                            dry, cycle_floor_event_id=cycle_floor_event_id,
                            registry_trusted=registry_fresh,
                            route_observation_id=route_observation_id,
                            expected_generation=generation,
                            expected_voice_event_id=observed_voice_id,
                            drive_update=(seat_key, generation,
                                          next_entry, 0)):
                        escalated += 1
                continue
            absent_ticks += 1
            pane_escalation = ""
            if (absent_ticks >= wp.PANE_ABSENT_LIMIT
                    and entry.get("st") != wp.S_ESCALATED and not spoke):
                entry = {"st": wp.S_ESCALATED,
                         "cycles": entry.get("cycles", 0)}
                pane_escalation = (f"seat {owed} has had no observable pane"
                                   f" for {absent_ticks} ticks (pane-absent)")
            elif dry:
                log(f"DRY {row['id']} seat {owed}: pane absent"
                    f" ({absent_ticks}/{wp.PANE_ABSENT_LIMIT})")
            if not dry and not pane_escalation:
                save_drive(conn, row["id"], seat_key, generation, entry,
                           absent_ticks)
            if pane_escalation:
                if escalate(
                        conn, row, pane_escalation, dry,
                        cycle_floor_event_id=cycle_floor_event_id,
                        registry_trusted=(dry or registry_fresh),
                        route_observation_id=route_observation_id,
                        expected_generation=generation,
                        expected_voice_event_id=observed_voice_id,
                        drive_update=(seat_key, generation, entry,
                                      absent_ticks)):
                    escalated += 1
            continue

        pane_id, busy = pane_info
        if seat_key not in seat_activity:
            seat_activity[seat_key] = any(
                wp.continuation_spoke_recently(
                    conn, task_id, window_s=wp.SEAT_ACTIVITY_WINDOW_S)
                for task_id in seat_open_tasks.get(seat_key, [])
            )
        idle_limit = idle_wait_limit_for(row["subject"], seat_activity[seat_key])
        action, next_entry = wp.step_drive(entry, busy,
                                           idle_wait_limit=idle_limit,
                                           spoke=spoke)
        if dry:
            log(f"DRY {row['id']} seat {owed} win {window} ({pane_id}): busy={busy}"
                f" state={entry.get('st', 'dispatched')}"
                f" -> action={action}")
            continue


        tstate, t_at = wp.seat_turn_state(conn, seat_key)
        if tstate is not None:
            turn_busy = (tstate == "start"
                         and (wp.now() - t_at) < wp.TURN_ACTIVE_MAX_S)
            if turn_busy != busy:
                log(f"SHADOW {row['id']} seat {seat_key}: turn-sensor says"
                    f" {'busy' if turn_busy else 'idle'} ({tstate}"
                    f" {wp.human_age(wp.now() - t_at)} ago), pane says"
                    f" {'busy' if busy else 'idle'}")
        if busy:


            with conn:
                wp.wake_attempt_resolve(conn, row["id"], seat_key, "reacted-busy")
        if action != "escalate":
            save_drive(conn, row["id"], seat_key, generation, next_entry, 0)
        if action == "pull":


            with conn:
                allowed = wp.wake_attempt_open(conn, row["id"], seat_key,
                                               "pull", generation)
            if not allowed:
                log(f"NOTE {row['id']}: unresolved wake attempt stands for"
                    f" {seat_key} - not touching the pane (re-arms on"
                    f" reaction, generation change, or ttl)")
                continue


            plan = seat_nudges.setdefault(
                seat_key, {"pane_id": pane_id, "window": window, "due": []})
            plan["due"].append((row["id"], generation))
        elif action == "escalate":


            if escalate(
                    conn, row,
                    f"seat {owed} idle while it must {context['label']}"
                    f" after pull and {entry.get('idle_waits', 0)} consecutive"
                    " speech-free idle tick(s)", dry,
                    cycle_floor_event_id=cycle_floor_event_id,
                    registry_trusted=(dry or registry_fresh),
                    route_observation_id=route_observation_id,
                    expected_generation=generation,
                    expected_voice_event_id=observed_voice_id,
                    drive_update=(seat_key, generation, next_entry, 0)):
                escalated += 1

    fired += flush_seat_nudges(conn, seat_nudges, tmux_send)

    if not dry:
        with conn:


            for r in conn.execute("SELECT DISTINCT task_id FROM drive").fetchall():
                if r["task_id"] not in live_tasks:
                    conn.execute("DELETE FROM drive WHERE task_id=?",
                                 (r["task_id"],))
            healed = wp.claim_sweep_terminal(conn)
            if healed:
                log(f"OK settled {healed} standing claim(s) on already-"
                    f"terminal tasks (legacy rows)")
        restored_messages += wp.repair_attention_notifications(
            conn, log, registry_trusted=registry_fresh,
            route_observation_id=route_observation_id)


        resent, still_failing = wp.retry_unsent(conn, log)
        if resent or still_failing:
            log(f"OK resend pass: {resent} delivered, {still_failing} still failing")
        wp.escalate_dead_letters(conn, log)
        wp.poll_receipts(conn)
        state_dir().mkdir(parents=True, exist_ok=True)
        (state_dir() / "tick-last.json").write_text(
            json.dumps({"at_s": wp.now(), "at": iso_now()}))
        receipt = (f", process_started_ns={PROCESS_STARTED_NS},"
                   f" completed_ns={time.time_ns()}"
                   if PROCESS_STARTED_NS is not None else "")
        log(f"OK tick done: {len(pairs)} owed pairs, {fired} nudges,"
            f" {escalated} escalations, {advanced} task(s) advanced past their"
            f" dependencies, {deadlines} deadline escalation(s),"
            f" {breakers_fired} breaker firing(s),"
            f" {restored_messages} missing responsibility message(s) restored"
            f"{receipt}")
    else:
        log(f"OK dry tick done: {len(pairs)} owed pairs inspected, nothing sent")
    if not dry:
        lock_fd.close()
    return 0


def refresh_reminder_due(conn, seat_key: str, plan: dict) -> list[tuple[str, str]]:


    current = []
    for task_id, generation in plan["due"]:
        spoke = False
        task = conn.execute(
            "SELECT * FROM dispatch WHERE id=?", (task_id,),
        ).fetchone()
        valid = task is not None and not wp.is_closed(task)
        if valid:
            context = wp.continuation_context(conn, task)
            drive = wp.current_drive(conn, task)
            spoke = bool(context and wp.continuation_spoke_recently(
                conn, task_id, context))
            valid = bool(
                context and not context.get("deferred")
                and context["requested"].strip().lower() != "operator"
                and (context["kind"] != "work" or not task["no_chase"])
                and not wp.credible_ask(conn, task)
                and not spoke
                and (context["kind"] != "work"
                     or not wp.dispatch_undelivered(conn, task_id))
                and context["seat"] == seat_key
                and str(context.get("window") or "") == str(plan["window"])
                and (not context.get("pane_id")
                     or context["pane_id"] == plan["pane_id"])
                and context.get("terminal_presence") != "unknown"
                and context["generation"] == generation
                and drive is not None
                and drive["st"] in (wp.S_PULLED, wp.S_ESCALATED)
            )
        if valid:
            current.append((task_id, generation))
            continue
        with conn:
            wp.wake_attempt_resolve(
                conn, task_id, seat_key,
                ("reacted-voice-before-contact" if spoke
                 else "superseded-before-contact"),
            )
        log(f"NOTE {task_id}: reminder skipped because responsibility changed"
            " before pane contact")
    return current


def continuation_reminder_text(task_id: str, context: dict) -> str:

    orc_bin = str(SCRIPT_DIR / "orc")
    return (
        f"ORC continuation reminder for {task_id}: {context['label']}.\n"
        f"Inspect the durable record: {wp.orc_task_command(task_id, command=str(orc_bin))}\n"
        "This reminder grants no authority. Continue only work already"
        " authorized by the task. Do not merge, deploy, delete, or"
        " communicate externally unless separately authorized. If another"
        " person must decide, record the exact blocker on the original task"
        " instead of waiting silently."
    )


def send_bus_continuation_reminder(conn, row, context: dict) -> int:

    generation = context["generation"]
    with conn:
        allowed = wp.wake_attempt_open(
            conn, row["id"], context["seat"], "pull", generation)
    if not allowed:
        log(f"NOTE {row['id']}: remote continuation reminder remains within"
            " its existing wake interval")
        return 0
    attempt = conn.execute(
        "SELECT at_ms FROM wake_attempt WHERE task_id=? AND seat=?"
        " AND purpose='pull' AND generation=?",
        (row["id"], context["seat"], generation),
    ).fetchone()
    token = wp.continuation_token(generation)
    expected_notice_id = wp.latest_message_id(
        conn, row["id"], "continuation-reminder")
    with conn:
        msg_row = wp.record_msg(
            conn, row["id"], "continuation-reminder",
            f"continue:{row['id']}:generation-{token}:at-{attempt['at_ms']}",
            context["agent_id"],
            f"ORC continuation reminder: {row['id']}",
            continuation_reminder_text(row["id"], context),
            expected_latest_id=expected_notice_id,
            expected_responsibility_version=
            row["responsibility_version"],
        )
    if msg_row is not None and wp.bus_send(conn, msg_row):
        log(f"OK remote continuation reminder -> {context['agent_id']}"
            f" for {row['id']}")
        return 1
    with conn:
        wp.wake_attempt_fail(
            conn, row["id"], context["seat"], "pull", generation)
    log(f"WARN remote continuation reminder not accepted for {row['id']};"
        " the recorded message remains in the bounded retry pass")
    return 0


def flush_seat_nudges(conn, seat_nudges: dict, tmux_send) -> int:


    sent = 0
    orc_bin = str(SCRIPT_DIR / "orc")
    for seat_key, original_plan in seat_nudges.items():
        due = refresh_reminder_due(conn, seat_key, original_plan)
        if not due:
            continue
        plan = {**original_plan, "due": due}
        tasks = ", ".join(tid for tid, _ in plan["due"])
        shown = plan["due"][:20]
        task_lines = []
        for tid, _generation in shown:
            task = conn.execute(
                "SELECT * FROM dispatch WHERE id=?", (tid,)
            ).fetchone()
            context = wp.continuation_context(conn, task) if task else None
            label = context["label"] if context else "inspect current action"
            task_lines.append(f"- {tid}: {label}; {wp.orc_task_command(tid, command=str(orc_bin))}")
        more = len(plan["due"]) - len(shown)
        more_line = (f"\n- {more} more: run {orc_bin} board" if more else "")
        task_block = "\n".join(task_lines)
        reminder = (
            "ORC reminder: inspect your assigned unfinished task(s).\n"
            "Pull messages first, then inspect these task records:\n"
            f"{task_block}{more_line}\n"
            "This reminder grants no authority. If the original assignment "
            "already authorized reversible work, continue it now. Otherwise, "
            f"record the specific decision needed with `{orc_bin} blocked <id> "
            "--note '<decision needed and who decides>'`; do not wait silently. "
            "Do not merge, deploy, delete, or communicate externally unless those "
            "actions were separately authorized."
        )
        try:
            outcome, detail = wp.wake_contact(
                conn, seat_key, str(plan["pane_id"]), "tick",
                [(tid, "pull") for tid, _ in plan["due"]],
                lambda progress, _p=plan: tmux_send.send_outcome(
                    _p["pane_id"], reminder, progress=progress))
        except (RuntimeError, ValueError) as e:
            outcome, detail = "UNKNOWN", str(e)
        if outcome == wp.SendOutcome.CONTACTED:
            log(f"OK continuation reminder -> window {plan['window']}"
                f" ({plan['pane_id']}), one tap for {len(plan['due'])}"
                f" task(s): {tasks}")
            sent += 1
        else:
            log(f"WARN continuation reminder failed for window {plan['window']}:"
                f" {outcome} {detail}"
                f" ({len(plan['due'])} task(s): {tasks})")
            with conn:
                for tid, generation in plan["due"]:
                    wp.wake_attempt_fail(conn, tid, seat_key, "pull",
                                         generation)
    return sent


def save_drive(conn, task_id: str, seat: str, generation: str,
               entry: dict, absent_ticks: int) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO drive (task_id, seat, generation, st,"
            " cycles, grace_used, idle_waits, absent_ticks, updated_ms)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (task_id, seat, generation, entry.get("st", wp.S_DISPATCHED),
             int(entry.get("cycles", 0)), int(bool(entry.get("grace_used"))),
             int(entry.get("idle_waits", 0)), absent_ticks, wp.now()))


def cmd_import_state(args: argparse.Namespace) -> int:


    legacy = nw_paths.codex_drive_state_dir() / "state.json"
    try:
        state = json.loads(legacy.read_text())
    except (OSError, ValueError):
        print(f"OK    nothing to import (no readable {legacy})")
        return 0
    conn = wp.connect_writable()
    imported = skipped = 0
    for key, entry in state.items():
        node_id, _, _pane = key.partition("|")
        row = conn.execute("SELECT * FROM dispatch WHERE id=?", (node_id,)).fetchone()
        if row is None or row["state"] == "closed":
            skipped += 1
            continue
        context = wp.continuation_context(conn, row)
        if context is None or context.get("deferred"):
            skipped += 1
            continue
        seat_key = context["seat"]
        exists = conn.execute("SELECT 1 FROM drive WHERE task_id=? AND seat=?",
                              (node_id, seat_key)).fetchone()
        if exists:
            skipped += 1
            continue
        save_drive(conn, node_id, seat_key, context["generation"], entry, 0)
        imported += 1
    print(f"OK    imported {imported} drive row(s), skipped {skipped}"
          f" (closed/missing/already present)")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    problems = wp.verify_relations()
    conn = wp.connect_readonly()
    problems += wp.verify_store(conn)
    for p in problems:
        print(f"FAIL  {p}")
    total_rows = conn.execute("SELECT COUNT(*) FROM dispatch").fetchone()[0]
    n_transitions = sum(len(s["transitions"]) for s in wp.WORKFLOWS.values())
    print(f"OK    replayed {total_rows} task(s) across {len(wp.WORKFLOWS)} workflows,"
          f" {n_transitions} declared transitions"
          if not problems else f"FAIL  {len(problems)} problem(s)")
    return 1 if problems else 0


def cmd_doctor(args: argparse.Namespace) -> int:
    ledger = load_script("dispatch-ledger.py", "dispatch_ledger_for_orc")
    rc = ledger.cmd_doctor(args)
    panes = None
    try:
        pane_sense.tmux_runtime.configured_server()
        panes = pane_sense.agent_panes()
    except (RuntimeError, pane_sense.tmux_runtime.TmuxRuntimeConfigError) as exc:
        print(f"FAIL  tmux observation unavailable on"
              f" {pane_sense.tmux_runtime.identity()}: {exc}")
        rc = 1
    else:
        print(f"OK    tmux observation: {pane_sense.tmux_runtime.identity()},"
              f" {len(panes)} live agent pane(s)")
    conn = wp.connect_readonly()
    if conn.members is None:
        print("FAIL  Agent Bus registry unavailable; membership could not be checked")
        rc = 1
    if doctor_truthfulness(conn, panes, conn.members):
        rc = 1
    stale = tick_staleness()
    if stale:
        print(f"FAIL  {stale}")
        return 1
    print("OK    tick freshness: engine ran within the last 15m")
    return rc


def main() -> int:
    parser = argparse.ArgumentParser(prog=os.environ.get("ORC_CLI_PROG", "orc"), description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    def add_open_args(p, dispatchable=False):
        goal_help = parser.prog.endswith(" goal")
        p.description = ("Create a parent goal. Attach child tasks with task open --parent ID."
                         if goal_help else
                         "Record work" + (" and send it to its recipient." if dispatchable
                                          else " without sending it to its recipient."))
        p.epilog = (
            "Checks run as shell commands on the machine running ORC, in that process's "
            "working directory, including scheduled checks. They do not run in the "
            "recipient's terminal and --repo does not change directory. Use absolute "
            "paths or an explicit cd; required tools and credentials must be available there."
        ) if not goal_help else None
        p.add_argument("--await", dest="await_notify", action="store_true",
                       help="notify MY seat once when this task reaches its"
                            " terminal state (opt-in; requires a resolvable"
                            " seat identity)")
        p.add_argument("--to", required=True,
                       help="recipient: bus handle, role:<name>, or tmux<N>;"
                            + (" PR dispatch requires the same value as --owner"
                               if dispatchable else " owns the requested work"))
        p.add_argument("--subject", required=True, help="short description of the requested result")
        p.add_argument("--body", help="request and acceptance conditions; required for operator tasks")
        p.add_argument("--body-file", help="read the request from this UTF-8 file instead of --body")
        p.add_argument("--check", help="ordinary tasks require this or --no-check; exit 0 stdout"
                       " is compared for progress. For PRs print the current head SHA;"
                       " omission disables automatic re-review on head changes")
        p.add_argument("--no-check", action="store_true",
                       help="ordinary task has no automated progress check; explain why in --body")
        p.add_argument("--after", default=wp.DEFAULT_AFTER,
                       help="next check delay, e.g. 45m, 2h, 1d (default: %(default)s)")
        p.add_argument("--link", action="append", help="related PR, issue or evidence URL; repeatable")
        p.add_argument("--workflow", default="dispatch",
                       choices=sorted(wp.WORKFLOWS),
                       help=argparse.SUPPRESS if goal_help else
                       "task lifecycle (default: %(default)s); pr requires --owner and --reviewer")
        p.add_argument("--parent", help="parent goal task id")
        p.add_argument("--repo", default="",
                       help="repository name for filtering and PR merge-decision routing; not a directory")
        p.add_argument("--owner", default="", help=argparse.SUPPRESS if goal_help else
                       "required for PR work: author identity")
        p.add_argument("--reviewer", default="", help=argparse.SUPPRESS if goal_help else
                       "required for PR work: independent reviewer identity or role")
        p.add_argument("--ready-cmd", default="",
                       help=argparse.SUPPRESS if goal_help else
                       "PR readiness check: exit 0 hands work to the reviewer;"
                       " omission disables automatic review handoff")
        p.add_argument("--done-cmd", default="",
                       help=argparse.SUPPRESS if goal_help else
                       "PR merge check: exit 0 means actually merged and closes the task;"
                       " omission disables automatic merge closure")
        p.add_argument("--needs", action="append", metavar="TASK-ID",
                       help="predecessor task id, repeatable: this task waits"
                            " unnotified until every predecessor closes")
        p.add_argument("--deadline", metavar="DURATION",
                       help="promise clock (45m/2h/1d): once past, the commander"
                            " hears about it once every 12h until it closes")
        p.add_argument("--breaker", default="", metavar="CMD",
                       help="unblocking command, run once after 3 consecutive"
                            " failing --check runs; never changes task state")

    p = sub.add_parser("open", help="open a task in any workflow")
    add_open_args(p)
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("dispatch", help="open + record-before-send + bus send"
                                        " + bounded durable-evidence wait")
    add_open_args(p, dispatchable=True)
    p.add_argument("--no-handshake", action="store_true",
                   help="skip the post-send handshake (batch/staging use;"
                        " NW_ORC_HANDSHAKE=0 does the same)")
    p.add_argument("--handshake-timeout", type=int,
                   default=HANDSHAKE_TIMEOUT_S, metavar="SECONDS",
                   help=f"poll durable task/inbox evidence every"
                        f" {HANDSHAKE_INTERVAL_S}s, up to this bound"
                        f" (0 = skip; default"
                        f" {HANDSHAKE_TIMEOUT_S})")
    p.set_defaults(func=cmd_dispatch)

    p = sub.add_parser("handshake", help="wait for durable acknowledgement or"
                                         " Agent Bus inbox presentation",
                       description="Wait for durable message presentation or task-response evidence."
                       " Presentation alone can succeed; this does not prove explicit acceptance,"
                       " progress or completion. The recipient uses task ack to accept;"
                       " inspect task show for the recorded evidence.")
    p.add_argument("id", help="dispatched task ID")
    p.add_argument("--timeout", type=int, default=HANDSHAKE_TIMEOUT_S,
                   help="maximum seconds to wait (default: %(default)s)")
    p.set_defaults(func=cmd_handshake)

    p = sub.add_parser("verdict", help="seat verb: review verdict",
                       description="The current PR reviewer records findings after the task"
                       " becomes ready for review. Blockers return work to the author;"
                       " clean requests the author's receipt.")
    p.add_argument("id", help="PR task ID")
    p.add_argument("verdict", choices=("blockers", "clean"), help="review outcome")
    p.add_argument("--link", action="append", help="additional evidence URL; does not replace --note")
    p.add_argument("--note", help="required: findings or a pointer, e.g. 'Reviewed SHA: PR review URL'")
    p.set_defaults(func=cmd_verdict)

    p = sub.add_parser("receipt", help="seat verb: record the owner review evidence",
                       description="After a clean verdict, the PR author records verification"
                       " evidence required by project policy. Moves the task to merge-pending"
                       " and supplies the configured decision maker with evidence, not merge permission."
                       " Read the stored evidence with task show ID.")
    p.add_argument("id", help="PR task ID")
    p.add_argument("--body", help="required unless --body-file is supplied: nonempty verification evidence")
    p.add_argument("--body-file", help="read evidence from a UTF-8 file instead of --body")
    p.set_defaults(func=cmd_receipt)

    p = sub.add_parser("blocked", help="seat verb: blocked-on-authorization"
                                       " (ask-evidence on record)")
    p.add_argument("id")
    p.add_argument("--note", help="REQUIRED content: what needs deciding and"
                                  " by whom (a bare marker is unjudgeable)")
    p.set_defaults(func=cmd_blocked)

    p = sub.add_parser("reassign", help="change recipient/owner/reviewer,"
                                        " audited; the blessed path instead"
                                        " of raw DB edits")
    p.add_argument("id")
    p.add_argument("--to", help="new recipient")
    p.add_argument("--owner", help="new owner seat (pr)")
    p.add_argument("--reviewer", help="new reviewer seat (pr); clears pool"
                                      " rotation - a hand-picked reviewer is"
                                      " a human's choice")
    p.add_argument("--note")
    p.set_defaults(func=cmd_reassign)

    p = sub.add_parser("claim-done", help="seat verb: claim completion;"
                                          " explicit independent reviewer judges",
                       description="Request independent verification without closing. A PR uses"
                       " its independent reviewer when available; otherwise try the parent"
                       " recipient, requester, commander role, then operator. Agent candidates"
                       " must be reachable and distinct from the current worker. The verifier"
                       " reads task show, accepts with task close --resolution done, or returns"
                       " incomplete work with task chase --note 'remaining work'.")
    p.add_argument("id", help="task ID whose current work you own")
    p.add_argument("--note", help="deliverable, reproduction command, evidence, known gaps and validation")
    p.set_defaults(func=cmd_claim_done)

    p = sub.add_parser("role", help="dynamic role graph: grant/revoke/list",
                       description="Assign fleet responsibilities used by role:NAME routing.")
    p.add_argument("action", choices=("grant", "revoke", "list"))
    p.add_argument("role", nargs="?", help="role name, required for grant/revoke")
    p.add_argument("agent_id", nargs="?", help="one registered agent ID, required for grant/revoke")
    p.add_argument("--by", help="required for grant: record existing operator/commander authorization,"
                   " e.g. 'operator approved this assignment'")
    p.set_defaults(func=cmd_role)

    p = sub.add_parser("team", help="per-goal small team membership",
                       description="For tasks under this goal, a matching team role takes"
                       " precedence over the fleet role when choosing a recipient.")
    p.add_argument("action", choices=("add", "list"))
    p.add_argument("parent", nargs="?", help="parent goal ID, required for add; list shows all goals")
    p.add_argument("agent_id", nargs="?", help="one registered agent ID, required for add")
    p.add_argument("team_role", nargs="?", help="role name used for routing in this goal, required for add")
    p.set_defaults(func=cmd_team)

    p = sub.add_parser("topology", help="one-page CURRENT topology: active"
                       " seats x panes x roles, duplicate/unregistered gaps,"
                       " recent checkouts. Derived from live facts, read-only")
    p.set_defaults(func=cmd_topology)

    p = sub.add_parser("onboard", help="entry-side self-brief: what this seat"
                       " owes, roles it holds, predecessor handoff notes to"
                       " read. Read-only; agent-boot runs it on every join",
                       formatter_class=argparse.RawDescriptionHelpFormatter,
                       description="Read an existing registration's tasks, roles and handoff.\n"
                       "This does not register an agent. Joining procedure:\n"
                       f"{SCRIPT_DIR.parent / 'references/agent-bus.md'}\n"
                       "See 'Join the current session'.")
    p.add_argument("identity", help="agent_id, handle, or alias of the seat")
    p.set_defaults(func=cmd_onboard)

    p = sub.add_parser("checkout", help="seat exit protocol: verify no owed"
                       " tasks or held roles, publish a vault handoff note,"
                       " retire the registration, stop the watcher")
    p.add_argument("identity", nargs="?", help="agent_id, handle, or alias of"
                   " another seat; omit for this process's exact active Agent"
                   " Bus identity, resolved from host + TMUX_PANE")
    p.add_argument("--summary", required=True,
                   help="handoff summary WRITTEN BY THE EXITING AGENT ITSELF"
                        " from its own session review (what I did / state"
                        " left where / successor must know / loose threads);"
                        " the operator never dictates it. A ledger trace is"
                        " appended automatically")
    p.add_argument("--no-vault-note", action="store_true",
                   help="skip the vault handoff file (registration-only exit)")
    p.set_defaults(func=cmd_checkout)

    p = sub.add_parser("pane-succession", help="sanctioned takeover of a pane"
                       " holding a leftover ACTIVE seat: fail-closes on"
                       " obligations or signs of life; retires only a provably"
                       " absent, obligation-free predecessor")
    p.add_argument("--pane", help="tmux pane id (%%N); default $TMUX_PANE")
    p.add_argument("--host", help="registry host (default: this machine)")
    p.add_argument("--location", help="rendered tmux location string, for"
                   " matching pre-migration registry rows without a pane_id")
    p.set_defaults(func=cmd_pane_succession)


    p = sub.add_parser("announce", help="bus send wrapper with task_msg"
                       " rows; name recipients with --to, or --fleet-wide"
                       " deliberately")
    p.add_argument("--subject", required=True)
    p.add_argument("--body", required=True)
    p.add_argument("--to", help="comma-separated recipient seats (handles or"
                   " agent ids); one recorded outbox row per recipient")
    p.add_argument("--fleet-wide", action="store_true",
                   help="send to EVERY active seat - this wakes every"
                        " watch-mode session on the bus; use --to unless"
                        " the whole fleet genuinely must hear it")
    p.set_defaults(func=cmd_announce)

    p = sub.add_parser("overview", help="the selected fleet's current goals and work")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_overview)

    p = sub.add_parser("agents", help="members and terminal windows in this fleet")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_agents)

    p = sub.add_parser("board", help="current work in the selected fleet")
    p.add_argument("--repo")
    p.add_argument("--view", choices=("table", "columns", "summary"), default="table")
    p.add_argument("--json", action="store_true")
    p.add_argument("--no-color", action="store_true")
    p.add_argument("--max-rows", type=int, default=0)
    p.set_defaults(func=cmd_board)

    p = sub.add_parser("tree", help="current goals; include history with --all")
    p.add_argument("id", nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_tree)

    p = sub.add_parser("tick", help="the 5-minute engine tick")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_tick)

    p = sub.add_parser("import-state", help="one-shot import of codex-drive state")
    p.set_defaults(func=cmd_import_state)

    p = sub.add_parser("snapshot", help="copy the DB aside now")
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("verify", help="relation + mirror + replay verification")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("doctor", help="ledger doctor + tick freshness")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("statusline",
                       help="1-2 line board digest for harness status bars")
    p.add_argument("--no-color", action="store_true",
                   help="plain text (NO_COLOR in the environment also works)")
    p.set_defaults(func=cmd_statusline)

    p = sub.add_parser("kanban", help="the board as columns derived from the"
                       " workflow state machines (wp.KANBAN)")
    p.add_argument("--no-color", action="store_true",
                   help="plain text (NO_COLOR in the environment also works)")
    p.add_argument("--max-rows", type=int, default=0,
                   help="cap rows per column, overflow shown as '+N more'"
                   " (0 = unlimited)")
    p.set_defaults(func=cmd_kanban)


    ledger = None

    def lazy_ledger():
        nonlocal ledger
        if ledger is None:
            ledger = load_script("dispatch-ledger.py", "dispatch_ledger_for_orc_cli")
        return ledger

    for verb, help_text in (("ack", "Accept the task's current action without claiming completion."),
                            ("chase", "Return incomplete work with findings and schedule another check."),
                            ("note", "Record progress without claiming completion.")):
        p = sub.add_parser(verb, help=help_text, description=help_text)
        p.add_argument("id", help="task ID")
        p.add_argument("--note", help={
            "ack": "optional acceptance details, e.g. 'Accepted; checking the reported failure'",
            "chase": "remaining work or verification findings for the responsible person",
            "note": "progress, evidence or a next step, e.g. 'Reproduced failure; test output at PATH'",
        }[verb])
        p.add_argument("--after", default="2h" if verb == "ack" else "30m",
                       help="next check delay, e.g. 45m, 2h, 1d (default: %(default)s)")
        p.set_defaults(func=lambda a, v=verb: getattr(lazy_ledger(), f"cmd_{v}")(a))

    p = sub.add_parser("close", help="close a task with a resolution",
                       description="Record the final resolution. For done, independently verify"
                       " the deliverable first; verification is the closer's responsibility."
                       " Use task claim-done to request review, or task chase to return incomplete work.")
    p.add_argument("id", help="task ID")
    p.add_argument("--resolution", required=True, choices=wp.RESOLUTIONS)
    p.add_argument("--by", help="the dispatch ID that supersedes or takes over"
                                " this one (records the edge); NOT an identity -"
                                " the closer is recorded automatically")
    p.add_argument("--note", help="verification evidence for done, or reason for the other resolution")
    p.set_defaults(func=lambda a: lazy_ledger().cmd_close(a))

    p = sub.add_parser("list", help="list tasks (open by default)")
    p.add_argument("--all", action="store_true")
    p.add_argument("--to")
    p.add_argument("--mine", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=lambda a: lazy_ledger().cmd_list(a))

    p = sub.add_parser("overdue", help="what is due for a check")
    p.add_argument("--run-checks", action="store_true")
    p.set_defaults(func=lambda a: lazy_ledger().cmd_overdue(a))

    p = sub.add_parser("show", help="full history of one task",
                       description="Read the task's request, responsibility, checks, stored review"
                       " evidence and event history.")
    p.add_argument("id", help="task ID from task list or a dispatch message")
    p.set_defaults(func=lambda a: lazy_ledger().cmd_show(a))

    p = sub.add_parser("link", help="typed edge between two tasks")
    p.add_argument("src")
    p.add_argument("kind", choices=wp.EDGE_KINDS)
    p.add_argument("dst")
    p.add_argument("--note")
    p.set_defaults(func=lambda a: lazy_ledger().cmd_link(a))

    p = sub.add_parser("brief", help="what waits on the operator, verbatim bodies")
    p.set_defaults(func=lambda a: lazy_ledger().cmd_brief(a))

    display_command = os.environ.get("ORC_CLI_COMMAND")
    if display_command and len(sys.argv) > 1 and sys.argv[1] in sub.choices:
        sub.choices[sys.argv[1]].prog = parser.prog + " " + display_command
    args, unknown = parser.parse_known_args()
    if unknown:
        sub.choices[args.cmd].error("unrecognized arguments: " + " ".join(unknown))
    try:
        if (os.environ.get("ORC_CLI_COMMAND")
                and args.cmd not in {"board", "overview", "tree", "agents"}
                and not getattr(args, "json", False)):
            print(fleet_heading())
        return args.func(args)
    except ValueError as exc:
        print(f"FAIL  {exc}", file=sys.stderr)
        return 2
    except sqlite3.IntegrityError as exc:
        print(f"FAIL  the database refused this write: {exc}\n"
              f"      a writer changed state without going through step() - that is"
              f" a bug in this script, not bad input", file=sys.stderr)
        return 2
    except sqlite3.OperationalError as exc:
        print("FAIL  the ledger schema is not current enough for this"
              f" read-only command: {exc}\n"
              "      run an intentional write command from the canonical"
              " checkout, or point DISPATCH_LEDGER_DB at an isolated copy,"
              " to apply migrations", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
