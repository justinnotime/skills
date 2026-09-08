#!/usr/bin/env python3
"""Task ledger commands backed by the shared fleet workflow store."""


from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR / "lib"))
import workplane as wp
from workplane import (
    CHECK_TIMEOUT_S, DB_PATH, DEFAULT_AFTER, EDGE_KINDS, MAX_BODY_BYTES,
    RESOLUTIONS, STATES, TERMINAL, TRANSITIONS, add_edge, connect_readonly,
    connect_writable, event_kind, fetch, human_age, now, parse_after, record,
    step_row, whoami,
)


def step(state: str, event: str) -> str:


    return wp.step("dispatch", state, event)


def cmd_open(args: argparse.Namespace) -> int:
    body = ""
    if args.body_file:
        body = Path(args.body_file).read_text(encoding="utf-8")[:MAX_BODY_BYTES]
    elif args.body:
        body = args.body[:MAX_BODY_BYTES]
    if args.to.strip().lower() == "operator" and not (args.body or args.body_file):
        raise SystemExit(
            "FAIL  an item owed by the operator needs --body: one self-contained\n"
            "      explanation he can act on without reading anything else.\n"
            "      The subject is a label for me; the body is the thing he reads.\n"
            "      Say what it is in a full sentence assuming he is hearing it for\n"
            "      the first time, what you want from him, and your recommendation.\n"
            "      Written down once here, it cannot decay into a bare number the\n"
            "      third time it appears in a report - which is exactly how it has\n"
            "      failed before."
        )
    if not args.check and not args.no_check:
        raise SystemExit(
            "FAIL  --check is required: give the command that answers 'did this move?'\n"
            "      A dispatch nobody can verify without asking its owner is the exact\n"
            "      shape that stalls. Pass --no-check only when that is genuinely true,\n"
            "      and say why in --body."
        )
    conn = connect_writable()
    with conn:
        did = wp.insert_task(conn, recipient=args.to, subject=args.subject,
                             body=body, check_cmd=args.check or "",
                             links=",".join(args.link or []),
                             after_s=parse_after(args.after),
                             requester_seat=wp.caller_seat_id(),
                             no_chase=1 if args.parked else 0)
    parked = " PARKED (no idle chases; an explicit chase re-arms the ladder)" \
        if args.parked else ""
    print(f"OK    dispatch {did} open, owed by {args.to}, check in"
          f" {args.after}{parked}")
    if not args.check:
        print("NOTE  no check command recorded - this node can only be moved by its owner")
    return 0


def cmd_ack(args: argparse.Namespace) -> int:
    conn = connect_writable()
    row = fetch(conn, args.id)
    caller, _context = wp.require_continuation_caller(conn, row, "ack")
    with conn:
        row, context = wp.lock_continuation_caller(
            conn, row, caller, "ack")


        new_state = (step_row(row, "ack") if context["kind"] == "work"
                     else row["state"])


        changed = conn.execute(
            "UPDATE dispatch SET state=?, chases=0, ask_flag=0, last_event=?,"
            " check_after=? WHERE id=? AND responsibility_version=?"
            " AND state=?",
            (new_state, now(), now() + parse_after(args.after), row["id"],
             row["responsibility_version"], row["state"]),
        )
        if changed.rowcount != 1:
            raise SystemExit(
                "FAIL  responsibility changed while ack was being recorded;"
                " inspect the task again and acknowledge only if it is still"
                " yours")
        record(conn, row["id"], "ack", args.note or "", actor=caller,
               continuation_generation=context["generation"])
    print(f"OK    {row['id']} current action acknowledged, still open until"
          f" resolved, next check in {args.after}")
    return 0


def cmd_close(args: argparse.Namespace) -> int:
    if args.resolution not in RESOLUTIONS:
        raise SystemExit(f"FAIL  resolution must be one of {', '.join(RESOLUTIONS)}")
    conn = connect_writable()
    row = fetch(conn, args.id)
    new_state = step_row(row, "close")
    caller = wp.caller_seat_id()


    closing_hash = ""
    if (row["workflow"] == "pr" and not (row["progress_hash"] or "")
            and (row["check_cmd"] or "").strip()):
        _verdict, closing_hash = wp.run_progress(row["check_cmd"])
        if not closing_hash:
            print("NOTE  the task's check did not answer, so no head is recorded"
                  " at closure; if its PR is still open the auto-registrar may"
                  " open one fresh review task for it")
    with conn:
        conn.execute(
            "UPDATE dispatch SET state=?, resolution=?, ask_flag=0,"
            " last_event=? WHERE id=?",
            (new_state, args.resolution, now(), row["id"]),
        )
        if closing_hash:
            conn.execute("UPDATE dispatch SET progress_hash=? WHERE id=?"
                         " AND progress_hash=''", (closing_hash, row["id"]))
        if args.by:
            try:
                target = fetch(conn, args.by)
            except SystemExit:


                raise SystemExit(
                    f"FAIL  --by takes the DISPATCH ID that supersedes or takes over"
                    f" this one (an edge is recorded), not an identity - and no"
                    f" dispatch matches {args.by!r}.\n"
                    f"      Your identity is recorded automatically as"
                    f" {whoami()!r}; to just close, drop --by."
                ) from None
            kind = "reassigned-to" if args.resolution == "reassigned" else "supersedes"
            add_edge(conn, target["id"], kind, row["id"], args.note or "")
        record(conn, row["id"], f"close:{args.resolution}", args.note or "")


        _closed, intent_warnings = wp.review_intent_pass(
            conn, row["id"], caller)


        wp.claim_settle_terminal(conn, row["id"], args.resolution)


        closed_row = fetch(conn, row["id"])
        notify_row = wp.terminal_notify(conn, closed_row, args.resolution,
                                        closer=caller)


    wp.expire_task_msgs(conn, row["id"])
    if notify_row:
        msg = conn.execute("SELECT target, subject, body, send_state,"
                           " attempts, last_error FROM task_msg"
                           " WHERE id=?", (notify_row,)).fetchone()
        if msg["send_state"] == "invalid-target":
            print(f"WARN  awaiting requester {msg['target']} was not"
                  " notified; terminal send stopped before transport:"
                  f" {msg['last_error']}")
        else:
            sent = wp.bus_send(conn, notify_row)
            current = conn.execute(
                "SELECT send_state,last_error FROM task_msg WHERE id=?",
                (notify_row,),
            ).fetchone()
            if current["send_state"] != "invalid-target":
                with conn:


                    wp.wake_cause_ride(conn, msg["target"], row["id"],
                                       "terminal")
            if sent:
                print(f"OK    awaiting requester {msg['target']} notified")
            elif current["send_state"] == "invalid-target":
                print(f"WARN  awaiting requester {msg['target']} was not"
                      " notified; terminal send stopped before transport:"
                      f" {current['last_error']}")
            else:
                print(f"OK    awaiting requester {msg['target']} notification"
                      " recorded; the tick retries up to"
                      f" {wp.MAX_SEND_ATTEMPTS} total attempts")
    for line in intent_warnings:
        print(line)
    print(f"OK    {row['id']} closed as {args.resolution}")
    return 0


def cmd_note(args: argparse.Namespace) -> int:


    conn = connect_writable()
    row = fetch(conn, args.id)
    caller = wp.caller_seat_id()
    with conn:
        locked = conn.execute(
            "UPDATE dispatch SET last_event=last_event WHERE id=?"
            " AND state=? AND responsibility_version=?",
            (row["id"], row["state"], row["responsibility_version"]),
        )
        if locked.rowcount != 1:
            raise SystemExit(
                "FAIL  task responsibility changed while the note was being"
                " recorded; inspect it and post the note again")
        row = fetch(conn, row["id"])
        new_state = step_row(row, "note")
        conn.execute(
            "UPDATE dispatch SET state=?, last_event=?, check_after=? WHERE id=?",
            (new_state, now(), now() + parse_after(args.after), row["id"]),
        )
        context = wp.continuation_context(conn, row)
        expected = (str(context.get("agent_id") or "")
                    if context is not None else "")
        if (context is not None and not expected
                and context.get("kind") == "work"
                and not str(context.get("requested") or "").startswith("role:")
                and caller == context.get("requested")):


            expected = caller
        generation = (
            context["generation"] if context is not None
            and not context.get("deferred")
            and caller
            and caller == expected else ""
        )
        record(conn, row["id"], "note", args.note or "", actor=caller,
               continuation_generation=generation)


        closed, intent_warnings = wp.review_intent_pass(
            conn, row["id"], wp.caller_seat_id())
    if closed:
        print(f"OK    your review-intent on {row['id']} closed with this note")
    for line in intent_warnings:
        print(line)
    print(f"OK    {row['id']} noted, still {new_state}, next check in {args.after}")
    return 0


def cmd_chase(args: argparse.Namespace) -> int:
    conn = connect_writable()
    row = fetch(conn, args.id)
    new_state = step_row(row, "chase")
    chases = row["chases"] + 1
    total = row["chases_total"] + 1
    with conn:
        conn.execute(
            "UPDATE dispatch SET state=?, chases=?, chases_total=?, ask_flag=0,"
            " last_event=?,"


            " check_after=?, no_chase=0 WHERE id=?",
            (new_state, chases, total, now(), now() + parse_after(args.after), row["id"]),
        )
        record(conn, row["id"], "chase", args.note or "")
    tail = f" ({total} total)" if total != chases else ""
    print(f"OK    {row['id']} chased {chases}x since its last answer{tail},"
          f" next check in {args.after}")
    if chases >= 2:
        print("NOTE  two or more chases with no answer between them: reassign rather than"
              " asking again - a seat that has gone silent twice is not about to answer a third time")
    return 0


def run_check(row: sqlite3.Row) -> tuple[int, str]:
    if not row["check_cmd"]:
        return (0, "(no check command)")
    try:
        out = subprocess.run(
            row["check_cmd"], shell=True, capture_output=True, text=True,
            timeout=CHECK_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return (124, f"check timed out after {CHECK_TIMEOUT_S}s")
    text = (out.stdout or "").strip() or (out.stderr or "").strip()
    return (out.returncode, text.splitlines()[0][:160] if text else "(no output)")


def print_row(row: sqlite3.Row, verbose: bool = False) -> None:
    age = human_age(now() - row["created_ms"])
    since = human_age(now() - row["last_event"])
    due = row["check_after"] - now()
    due_txt = "DUE" if due <= 0 else f"in {human_age(due)}"
    chase = f" chased={row['chases']}" if row["chases"] else ""
    if row["chases_total"] != row["chases"]:
        chase += f"({row['chases_total']} total)"
    state = row["state"] if row["state"] != "closed" else f"closed:{row['resolution']}"
    print(f"  {row['id']}  {state:<8} {row['recipient']:<34} age={age:<5} quiet={since:<5}"
          f" {due_txt:<9}{chase}  {row['subject'][:64]}")
    if verbose:
        if row["links"]:
            print(f"      links: {row['links']}")
        if row["check_cmd"]:
            print(f"      check: {row['check_cmd']}")


def cmd_list(args: argparse.Namespace) -> int:
    if not wp.configured_db_path().exists():
        if not args.json:
            print("OK    no matching dispatches")
        return 0
    conn = connect_readonly()
    where, params = [], []
    if not args.all:
        where.append("state != 'closed'")
    if args.mine:
        where.append("created_by = ?")
        params.append(whoami())
    sql = "SELECT * FROM dispatch"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY check_after ASC"
    rows = conn.execute(sql, params).fetchall()
    if args.to:
        needle = args.to.lower()
        selected = []
        for row in rows:
            context = wp.continuation_context(conn, row)
            values = [row["recipient"]]
            if context is not None:
                values.extend((str(context.get("requested") or ""),
                               str(context.get("seat") or ""),
                               str(context.get("agent_id") or "")))
            if any(needle in value.lower() for value in values):
                selected.append(row)
        rows = selected
    if args.json:


        for row in rows:
            print(json.dumps({k: row[k] for k in row.keys()}, ensure_ascii=False))
        return 0
    if not rows:
        print("OK    no matching dispatches")
        return 0
    print(f"{len(rows)} dispatch(es):")
    for row in rows:
        print_row(row, verbose=args.verbose)
        context = wp.continuation_context(conn, row)
        if context is not None:
            print(f"      next: {context['seat']} — {context['label']}")
    return 0


def cmd_overdue(args: argparse.Namespace) -> int:

    conn = connect_readonly()
    rows = conn.execute(
        "SELECT * FROM dispatch WHERE state != 'closed' AND check_after <= ?"
        " ORDER BY check_after ASC", (now(),)
    ).fetchall()
    total_open = conn.execute(
        "SELECT COUNT(*) FROM dispatch WHERE state != 'closed'"
    ).fetchone()[0]
    if not rows:
        print(f"OK    nothing due; {total_open} open dispatch(es) still tracked")
        return 0
    print(f"{len(rows)} of {total_open} open dispatch(es) DUE for a check:")
    for row in rows:
        print_row(row, verbose=True)
        if args.run_checks:
            code, first = run_check(row)
            verdict = "check-ok" if code == 0 else f"check-exit-{code}"
            print(f"      {verdict}: {first}")
            print("      NOTE  a check that exits nonzero is not a finding by itself -"
                  " read the output before concluding anything")
    return 0


def cmd_review_intent(args: argparse.Namespace) -> int:


    conn = connect_writable()
    row = fetch(conn, args.id)
    seat = wp.caller_seat_id()
    if not seat:
        raise SystemExit(
            "FAIL  review-intent needs the caller's stable seat identity:"
            " export ORC_SEAT_ID or run from a pane that joined the bus")
    with conn:
        if args.done:
            closed = wp.review_intent_close(conn, row["id"], seat)
            if closed:
                record(conn, row["id"], "review-intent-done",
                       args.scope or "withdrawn without a posted result")
                print(f"OK    review-intent closed on {row['id']}")
            else:
                print(f"OK    no open review-intent of yours on {row['id']}")
        else:
            wp.review_intent_open(conn, row["id"], seat, args.scope or "")
            record(conn, row["id"], "review-intent", args.scope or "")
            print(f"OK    review-intent declared on {row['id']} - visible in"
                  f" show/board until your next note on it lands")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    conn = connect_readonly()
    row = fetch(conn, args.id)
    print(f"id         {row['id']}")
    print(f"state      {row['state']}{(' / ' + row['resolution']) if row['resolution'] else ''}")
    print(f"recipient  {row['recipient']}")
    context = wp.continuation_context(conn, row)
    if context is not None:
        status = (f"unresolved: {context['deferred']}"
                  if context.get("deferred") else context["seat"])
        print(f"next       {status} — {context['label']}")
    print(f"created    {human_age(now() - row['created_ms'])} ago by {row['created_by']}")
    print(f"next check {'DUE' if row['check_after'] <= now() else 'in ' + human_age(row['check_after'] - now())}")
    print(f"chases     {row['chases']} since its last answer, {row['chases_total']} total")
    if row["links"]:
        print(f"links      {row['links']}")
    if row["check_cmd"]:
        print(f"check      {row['check_cmd']}")
    print(f"subject    {row['subject']}")
    if row["body"]:
        print("body ---")
        print(row["body"])
    intents = wp.open_review_intents(conn, row["id"])
    if intents:
        print("review intents (open) ---")
        for i in intents:
            scope = f"  scope: {i['scope']}" if i["scope"] else ""
            print(f"  {i['seat']}  reviewing since"
                  f" {human_age(now() - i['started_s'])} ago{scope}")
    claims = conn.execute(
        "SELECT * FROM completion_claim WHERE task_id=? ORDER BY round",
        (row["id"],)).fetchall()
    if claims:


        print("claims ---")
        for c in claims:
            state = c["status"] + (f": {c['reason']}" if c["reason"] else "")
            print(f"  r{c['round']}  event #{c['event_id']}  {c['claimant']}"
                  f"  generation {c['generation']}  {state}")
    terminal_msgs = conn.execute(
        "SELECT target, send_state, attempts, last_error FROM task_msg"
        " WHERE task_id=? AND purpose='terminal' ORDER BY id", (row["id"],)
    ).fetchall()
    if terminal_msgs:
        print("terminal notifications ---")
        for msg in terminal_msgs:
            error = f"  {msg['last_error']}" if msg["last_error"] else ""
            print(f"  {msg['target']}  {msg['send_state']}"
                  f"  attempts={msg['attempts']}/{wp.MAX_SEND_ATTEMPTS}{error}")
    print("events ---")
    for ev in conn.execute(
        "SELECT * FROM event WHERE dispatch_id=? ORDER BY at_ms ASC", (row["id"],)
    ):
        note = f"  {ev['note']}" if ev["note"] else ""
        print(f"  {human_age(now() - ev['at_ms']):>5} ago  {ev['kind']:<16} {ev['actor']}{note}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:

    conn = connect_readonly()
    problems = 0
    no_check = conn.execute(
        "SELECT COUNT(*) FROM dispatch WHERE state!='closed' AND check_cmd=''"
        " AND workflow='dispatch'"
    ).fetchone()[0]
    if no_check:
        print(f"WARN  {no_check} open dispatch(es) carry no check command -"
              " those can only be moved by their owner")
    silent = conn.execute(
        "SELECT * FROM dispatch WHERE state!='closed' AND chases>=2"
        " AND NOT (workflow='pr' AND state='merge-pending')"
        " ORDER BY chases DESC"
    ).fetchall()
    for row in silent:
        if wp.waits_on_operator(conn, row):
            continue
        problems += 1
        print(f"FAIL  {row['id']} chased {row['chases']}x with no answer between them -"
              f" reassign it ({row['recipient']}: {row['subject'][:50]})")


    talkative = conn.execute(
        "SELECT * FROM dispatch WHERE state!='closed' AND chases<2 AND chases_total>=4"
        " ORDER BY chases_total DESC"
    ).fetchall()
    for row in talkative:
        if wp.waits_on_operator(conn, row):
            continue
        problems += 1
        print(f"FAIL  {row['id']} answered {row['chases_total']} chases and closed none of"
              f" them - the answers are not the work ({row['recipient']}:"
              f" {row['subject'][:50]})")


    ancient = [row for row in conn.execute(
        "SELECT * FROM dispatch WHERE state!='closed' AND created_ms < ?"
        " AND workflow='dispatch' AND subject NOT LIKE 'STANDING:%'"
        " AND lower(recipient)!='operator'",
        (now() - 86400,),
    ).fetchall() if not wp.waits_on_operator(conn, row)]
    for row in ancient:
        problems += 1
        print(f"FAIL  {row['id']} open for {human_age(now() - row['created_ms'])} -"
              f" close it or say why it is still live ({row['subject'][:50]})")
    standing = conn.execute(
        "SELECT COUNT(*) FROM dispatch WHERE state!='closed' AND"
        " subject LIKE 'STANDING:%'").fetchone()[0]
    if standing:
        print(f"NOTE  {standing} STANDING task(s) exempt from the age check by design")
    for r in wp.dead_letters(conn):
        problems += 1
        print(f"FAIL  dead letter: {r['purpose']} for task {r['task_id']} ->"
              f" {r['target']} undeliverable after {r['attempts']} attempt(s)")
    for task in conn.execute(
            "SELECT * FROM dispatch WHERE state!='closed' ORDER BY created_ms"):
        for msg in wp.operator_delivery_failures(conn, task):
            problems += 1
            print(f"FAIL  delivery failure on original task {task['id']}:"
                  f" {msg['purpose']} -> {msg['target']} after"
                  f" {msg['attempts']} attempt(s)")
    counts = dict(conn.execute(
        "SELECT state, COUNT(*) FROM dispatch GROUP BY state"
    ).fetchall())
    print(f"OK    ledger at {wp.configured_db_path()}: " +
          ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "empty")
    return 1 if problems else 0


def cmd_brief(args: argparse.Namespace) -> int:


    conn = connect_readonly()
    rows = [row for row in conn.execute(
        "SELECT * FROM dispatch WHERE state!='closed' ORDER BY created_ms ASC"
    ).fetchall() if wp.waits_on_operator(conn, row)]
    if not rows:
        print("OK    nothing is waiting on the operator")
        return 0
    for row in rows:
        no_supervisor = wp.escalation_recipient(conn, row) == "operator"
        direct = row["recipient"].strip().lower() == "operator"
        merge = (wp.row_workflow(row) == "pr"
                 and row["state"] == "merge-pending"
                 and wp.merge_key_role(row["repo"]) == wp.OPERATOR_ROLE)
        pool = bool(wp.reviewer_pool_attention_active(conn, row)
                    and no_supervisor)
        goal_review = (wp.row_workflow(row) == "parent"
                       and row["state"] == "ready-to-close"
                       and wp.goal_review_recipient(conn, row) == "operator")
        delivery = wp.operator_delivery_failures(conn, row)
        claim = wp.claim_standing(conn, row, repair=False)
        operator_claim = bool(claim and wp.claim_judge(conn, row) == "operator")
        route_missing = wp.continuation_route_missing(conn, row)
        unrouted_action = bool(
            route_missing and not (merge or goal_review or operator_claim)
        )
        drive = wp.current_drive(conn, row)
        deadline_attention = wp.deadline_attention_event(conn, row)
        route_marker = wp.operator_queue_marker(conn, row)
        idle_escalation = bool(
            drive and drive["st"] == wp.S_ESCALATED and no_supervisor
        )
        deadline_escalation = bool(deadline_attention and no_supervisor)
        ask_event = wp.current_ask_event(conn, row)
        blocked = bool(ask_event and no_supervisor)
        dynamic_reason = any((direct, merge, pool, goal_review, bool(delivery),
                              operator_claim, idle_escalation,
                              deadline_escalation, unrouted_action, blocked))
        unverified_route = bool(route_marker and not dynamic_reason)
        reasons = []
        for active, label in (
                (direct, "open"),
                (merge, "merge-pending"),
                (pool, "reviewer pool unavailable"),
                (goal_review, "parent goal needs review"),
                (bool(delivery), "delivery failed"),
                (operator_claim, "completion claim needs review"),
                (idle_escalation, "continuation unanswered"),
                (deadline_escalation, "deadline overdue"),
                (unrouted_action, "next action has no verified recipient"),
                (unverified_route, "current recipient could not be verified"),
                (blocked, "blocked on a human")):
            if active:
                reasons.append(label)
        print(f"--- {row['id']}  {'; '.join(reasons)}"
              f" {human_age(now() - row['created_ms'])}"
              f"{'  links ' + row['links'] if row['links'] else ''}")
        if direct:
            print(row["body"].strip() if row["body"].strip()
                  else f"(no body stored) {row['subject']}")
        if merge:
            print(f"PR task {row['id']} (repo {row['repo'] or 'unknown'}) is"
                  f" merge-pending: {row['subject']}\n"
                  f"Reviewer verdict clean; owner receipt below."
                  f" Verify and merge yourself if it holds - merging clears"
                  f" this item on the next tick.")
            receipt = (row["receipt_body"] or "").strip()
            print(receipt if receipt else "(no receipt body stored)")
        if pool:
            _authors, author_unknown = wp.owner_review_identities(conn, row)
            if author_unknown:
                print(f"PR task {row['id']} cannot choose or rotate a pool"
                      " reviewer because the actual historical author identity"
                      " is unknown. Restore the Agent Bus recipient evidence or"
                      " assign one concrete reviewer by hand."
                      f"\n{row['subject']}")
            else:
                print(f"PR task {row['id']} cannot enter review because"
                      f" {row['reviewer_seat']} has no eligible active member."
                      f" Assign a reviewer or restore the pool, then the next"
                      f" tick will re-check readiness.\n{row['subject']}")
        if goal_review:
            print(f"All children of parent goal {row['id']} are closed, but"
                  f" no explicit active reviewer is recorded: {row['subject']}")
            print(f"Inspect with `orc tree {row['id']}`; close the goal if it"
                  f" is met, or add child tasks for remaining work.")
        if delivery:
            print(f"Task {row['id']} still needs work, but its current"
                  f" coordination message could not be delivered:"
                  f" {row['subject']}")
            for msg in delivery:
                error = msg["last_error"] or "No transport error was recorded."
                print(f"{msg['purpose']} -> {msg['target']}:"
                      f" {msg['send_state']} after {msg['attempts']} attempt(s);"
                      f" {error}")
                if (msg["body"] or "").strip():
                    print(msg["body"].strip())
        if operator_claim:
            print(f"Task {row['id']} has a completion claim but no independent"
                  f" recorded reviewer: {row['subject']}")
            print((claim["payload"] or "(no completion detail stored)").strip())
            print(f"Inspect with `{wp.orc_task_command(row['id'])}`; close it to accept,"
                  f" or chase it to return the work.")
        if unrouted_action:
            obligation = wp.continuation_obligation(conn, row)
            action = (obligation["label"] if obligation is not None
                      else "inspect the current task")
            print(f"Task {row['id']} has no verified recipient for its next"
                  f" action: {action}.\n{row['subject']}"
                  f"\nInspect with `{wp.orc_task_command(row['id'])}` and assign the"
                  " action to an active seat, or handle it directly.")
        if idle_escalation:
            idle_event = conn.execute(
                "SELECT note FROM event WHERE dispatch_id=? AND kind='auto-chase'"
                " AND note NOT LIKE ? ORDER BY id DESC LIMIT 1",
                (row["id"], "engine: DEADLINE OVERDUE:%"),
            ).fetchone()
            detail = ((idle_event["note"] if idle_event else "")
                      .removeprefix("engine: "))
            print(f"Continuation reminder was not answered and no independent"
                  f" supervisor is recorded: {row['subject']}"
                  f"\n{detail or '(no escalation detail stored)'}"
                  f"\nInspect with `{SCRIPT_DIR / 'orc'} show {row['id']}`.")
        if deadline_escalation:
            detail = (deadline_attention["note"] or "").removeprefix("engine: ")
            print(f"Deadline escalation has no independent supervisor:"
                  f" {row['subject']}"
                  f"\n{detail or '(no deadline detail stored)'}"
                  f"\nInspect with `{SCRIPT_DIR / 'orc'} show {row['id']}`.")
        if unverified_route:
            print(f"Task {row['id']} stays on the original-task list because"
                  " no current independent recipient was verified.")
            if (route_marker["body"] or "").strip():
                print(route_marker["body"].strip())
        if blocked:
            print(f"Task {row['id']} is waiting on a human: {row['subject']}")
            print(ask_event["note"] if ask_event else
                  (row["body"].strip() or "(no question stored)"))
        print()
    print(f"{len(rows)} item(s) waiting on him")
    return 0


def cmd_link(args: argparse.Namespace) -> int:
    conn = connect_writable()
    src = fetch(conn, args.src)
    dst = fetch(conn, args.dst)
    with conn:
        add_edge(conn, src["id"], args.kind, dst["id"], args.note or "")
    print(f"OK    {src['id']} {args.kind} {dst['id']}")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:


    problems = wp.verify_relations()
    conn = connect_readonly()
    problems += wp.verify_store(conn)
    for p in problems:
        print(f"FAIL  {p}")
    rows = conn.execute("SELECT COUNT(*) FROM dispatch").fetchone()[0]
    edges = conn.execute("SELECT COUNT(*) FROM edge").fetchone()[0]
    n_transitions = sum(len(s["transitions"]) for s in wp.WORKFLOWS.values())
    print(f"OK    replayed {rows} dispatch(es), {edges} edge(s), "
          f"{n_transitions} declared transitions"
          if not problems else f"FAIL  {len(problems)} problem(s)")
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("open", help="record a dispatch that stays open until closed")
    p.add_argument("--to", required=True, help="recipient: bus handle or tmux target")
    p.add_argument("--subject", required=True)
    p.add_argument("--body")
    p.add_argument("--body-file")
    p.add_argument("--check", help="shell command that answers 'did this move?'")
    p.add_argument("--no-check", action="store_true",
                   help="assert that no check exists without asking the owner")
    p.add_argument("--after", default=DEFAULT_AFTER, help="when to check (45m, 2h, 1d)")
    p.add_argument("--link", action="append", help="related PR/issue, repeatable")
    p.add_argument("--parked", action="store_true",
                   help="machine-readable 'queued, no chase': the idle"
                        " ladder skips this task entirely until an explicit"
                        " chase re-arms it")
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("ack", help="recipient acknowledged / took it")
    p.add_argument("id")
    p.add_argument("--note")
    p.add_argument("--after", default="2h")
    p.set_defaults(func=cmd_ack)

    p = sub.add_parser("close", help="close a node with a resolution")
    p.add_argument("id")
    p.add_argument("--resolution", required=True, choices=RESOLUTIONS)
    p.add_argument("--by", help="the dispatch that supersedes or takes over this one; "
                                "records the edge instead of leaving it in a note")
    p.add_argument("--note")
    p.set_defaults(func=cmd_close)

    p = sub.add_parser("chase", help="record a chase and re-arm the check")
    p.add_argument("id")
    p.add_argument("--note")
    p.add_argument("--after", default="30m")
    p.set_defaults(func=cmd_chase)

    p = sub.add_parser("note", help="record something and re-arm the check, "
                                    "WITHOUT claiming the owner has gone quiet")
    p.add_argument("id")
    p.add_argument("--note")
    p.add_argument("--after", default="30m")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("review-intent", help="declare an in-flight"
                       " independent review on a task so judges see it"
                       " before posting a verdict (advisory: crossing"
                       " verdicts WARN, never block); auto-closes when your"
                       " next note on the task lands")
    p.add_argument("id")
    p.add_argument("--scope", help="what the review covers")
    p.add_argument("--done", action="store_true",
                   help="withdraw/close your open intent without posting")
    p.set_defaults(func=cmd_review_intent)

    p = sub.add_parser("list", help="list dispatches (open by default)")
    p.add_argument("--all", action="store_true")
    p.add_argument("--to")
    p.add_argument("--mine", action="store_true")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--json", action="store_true",
                   help="one JSON object per row (for tooling; empty = no rows)")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("overdue", help="THE routine command: what is due for a check")
    p.add_argument("--run-checks", action="store_true",
                   help="execute each due node's check command")
    p.set_defaults(func=cmd_overdue)

    p = sub.add_parser("show", help="full history of one dispatch")
    p.add_argument("id")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("link", help="typed edge between two dispatches")
    p.add_argument("src")
    p.add_argument("kind", choices=EDGE_KINDS)
    p.add_argument("dst")
    p.add_argument("--note")
    p.set_defaults(func=cmd_link)

    p = sub.add_parser("verify", help="replay the log against stored state; check the relation")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("brief", help="what the operator owes an answer on, with the full explanation")
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("doctor", help="self-checks over the ledger")
    p.set_defaults(func=cmd_doctor)

    args = parser.parse_args()
    try:
        return args.func(args)
    except ValueError as exc:
        print(f"FAIL  {exc}", file=sys.stderr)
        return 2
    except sqlite3.IntegrityError as exc:


        print(f"FAIL  the database refused this write: {exc}\n"
              f"      a writer changed state without going through step() - that is a bug "
              f"in this script, not bad input", file=sys.stderr)
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
