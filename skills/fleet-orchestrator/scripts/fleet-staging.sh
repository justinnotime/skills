#!/usr/bin/env bash

set -euo pipefail

CMD="${1:-}"
STAGE="${2:-${XDG_STATE_HOME:-$HOME/.local/state}/fleet-orchestrator/staging}"
STAGE="$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).expanduser().resolve())' "$STAGE")"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOCK="fleet-staging-$(python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.argv[1].encode()).hexdigest()[:12])' "$STAGE")"

env_exports() {
  export NW_TMUX_SERVER="$SOCK"
  export DISPATCH_LEDGER_DB="$STAGE/ledger.sqlite3"
  export NOTES_RUNTIME_DIR="$STAGE/runtime"
  export MATRIX_BUS_CFG="$STAGE/matrix-void"
  export AGENT_BUS_CFG="$STAGE/matrix-void"
  export FLEET_ORCHESTRATOR_CONFIG="$STAGE/config.json"
  export AGENT_BUS_DB="$STAGE/matrix-void/agent-bus-v3.sqlite3"
  export DISPATCH_LEDGER_ACTOR="staging-harness"
  export NW_BUS_CLI="$STAGE/bin/fake-bus.sh"
  export NW_GH_CLI="$STAGE/bin/gh-inert.sh"
}

write_fake_bus() {  # <stage-dir>
  local host; host="$(hostname -s)"
  cat >"$1/members.jsonl" <<EOF
{"agent_id": "fake-w1", "handle": "stage/worker-1-tmux1", "aliases": ["stage/project-w1-tmux1"], "host": "$host", "tmux": "tmux=stage:1.0 win=codex", "status": "active", "addressable": true, "updated_at": "2030-01-01T00:00:00Z"}
{"agent_id": "fake-perf", "handle": "stage/performance-worker-tmux2", "aliases": [], "host": "$host", "tmux": "tmux=stage:2.0 win=codex", "status": "active", "addressable": true, "updated_at": "2030-01-01T00:00:00Z"}
EOF
  cat >"$1/bin/fake-bus.sh" <<'FAKEBUS'
#!/usr/bin/env bash
set -u
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CMD="${1:-}"; shift || true
case "$CMD" in
  members) cat "$DIR/members.jsonl" ;;
  send)
    SENDER="$1"; TARGET="$2"; SUBJECT="$3"; BODY="$4"
    python3 - "$DIR" "$SENDER" "$TARGET" "$SUBJECT" "$BODY" <<'PY'
import json, re, sys, uuid
d, sender, target, subject, body = sys.argv[1:6]
members = [json.loads(l) for l in open(f"{d}/members.jsonl") if l.strip()]
if not target.strip():
    print("agent-bus-v3: target must not be empty")
    sys.exit(1)
hit = [m for m in members
       if target in {m["agent_id"], m["handle"]} or target in m.get("aliases", [])]
if not hit and target != "all":
    segment = re.compile(rf"(^|[/-]){re.escape(target)}([/-]|$)")
    hit = [m for m in members
           if any(segment.search(name)
                  for name in [m["handle"], *m.get("aliases", [])])]
if target != "all" and len(hit) != 1:
    print(f"agent-bus-v3: target {target!r} resolved to {len(hit)} active agents")
    sys.exit(1)
msg_id = uuid.uuid4().hex
with open(f"{d}/sends.log", "a") as f:
    f.write(json.dumps({"msg_id": msg_id, "sender": sender, "target": target,
                        "subject": subject, "body": body}) + "\n")
print(json.dumps({"schema": "agent-bus/send-result/v3", "msg_id": msg_id,
                  "transport_state": "accepted",
                  "recipient_agent_ids": ([m["agent_id"] for m in members]
                                          if target == "all"
                                          else [hit[0]["agent_id"]]),
                  "recipients": len(members) if target == "all" else 1}))
PY
    ;;
  delivery)
    printf '{"schema": "agent-bus/delivery/v3", "delivered": true, "processed": ""}\n'
    ;;
  *) echo "fake-bus: unsupported verb $CMD" >&2; exit 1 ;;
esac
FAKEBUS
  chmod +x "$1/bin/fake-bus.sh"
}

write_stub() {  # <stage-dir>
  mkdir -p "$1/bin" "$1/ctl" "$1/logs"
  write_fake_bus "$1"
  cat >"$1/bin/gh-inert.sh" <<'GHSTUB'
#!/usr/bin/env bash
if [ "${2:-}" = list ]; then echo '[]'; else echo ok; fi
GHSTUB
  chmod +x "$1/bin/gh-inert.sh"
  cp "$(command -v bash)" "$1/bin/codex"
  cat >"$1/bin/agent-loop.sh" <<'STUB'
pending=""
while true; do
  state="$(cat "$FAKE_CTL" 2>/dev/null || echo idle)"
  case "$state" in
    stall) echo "It remains unacknowledged and unexecuted because it is a peer command requiring your authorization." ;;
    busy)  echo "Working (1m 10s - Esc to interrupt)" ;;
    clear) printf '\033[2J\033[H' ;;
  esac
  line=""
  if IFS= read -t 2 -r line; then
    printf '%s%s\n' "$pending" "$line" >>"$FAKE_LOG"
    pending=""
    echo ">"
  else
    # A timed-out read still consumes its partial input. Keep that fragment
    # until the sender's newline arrives instead of dropping a message prefix.
    pending+="$line"
  fi
done
STUB
}

cmd_up() {
  if tmux -L "$SOCK" has-session 2>/dev/null; then
    echo "OK staging server already running (socket $SOCK); use down first for a clean world"
    return 0
  fi
  if [[ -d "$STAGE" && -n "$(ls -A "$STAGE")" && ! -f "$STAGE/.fleet-staging" ]]; then
    echo "FAIL staging directory is not owned by this harness: $STAGE" >&2
    return 1
  fi
  if [[ -f "$STAGE/.fleet-staging" ]]; then rm -rf -- "$STAGE"; fi
  mkdir -p "$STAGE"
  : >"$STAGE/.fleet-staging"
  python3 - "$STAGE/config.json" "$REPO" <<'CONFIG'
import json, sys
with open(sys.argv[1], "w") as stream:
    json.dump({"schema": "fleet-runtime/v1", "canonical_source_root": sys.argv[2],
               "github": {"owner": "example-org"}, "watched_repositories": []}, stream)
CONFIG
  write_stub "$STAGE"
  mkdir -p "$STAGE/runtime" "$STAGE/matrix-void"
  echo idle >"$STAGE/ctl/w1"
  echo idle >"$STAGE/ctl/w2"
  tmux -L "$SOCK" new-session -d -s stage -n seed -x 200 -y 50 "sleep infinity"
  tmux -L "$SOCK" new-window -t stage:1 \
    "FAKE_CTL='$STAGE/ctl/w1' FAKE_LOG='$STAGE/logs/w1.log' exec '$STAGE/bin/codex' '$STAGE/bin/agent-loop.sh'"
  tmux -L "$SOCK" new-window -t stage:2 \
    "FAKE_CTL='$STAGE/ctl/w2' FAKE_LOG='$STAGE/logs/w2.log' exec '$STAGE/bin/codex' '$STAGE/bin/agent-loop.sh'"
  sleep 1
  tmux -L "$SOCK" list-panes -a -F 'OK pane #{pane_id} #{session_name}:#{window_index} runs #{pane_current_command}'
  echo "OK staging fleet up at $STAGE (tmux socket $SOCK)"
}

cmd_down() {
  tmux -L "$SOCK" kill-server 2>/dev/null || true
  echo "OK staging server stopped; artifacts kept at $STAGE (delete by hand when done)"
}

cmd_status() {
  tmux -L "$SOCK" list-panes -a -F 'pane #{pane_id} #{session_name}:#{window_index}.#{pane_index} cmd=#{pane_current_command}' 2>/dev/null \
    || echo "NOTE no staging server on socket $SOCK"
  for f in "$STAGE"/ctl/*; do [ -f "$f" ] && echo "ctl $(basename "$f") = $(cat "$f")"; done
  for f in "$STAGE"/logs/*; do [ -f "$f" ] && echo "log $(basename "$f"): $(wc -l <"$f") lines"; done
}

tick() { python3 "$REPO/scripts/fleet-orchestrator.py" tick; }

fail() { echo "FAIL $1"; exit 1; }

cmd_bus_e2e() {
  echo "--- phase 0: Agent Bus residents converge on changed staged source"
  python3 "$REPO/tests/test_agent_bus_self_converge.py"
}

cmd_e2e() {
  cmd_bus_e2e
  cmd_down >/dev/null 2>&1 || true
  cmd_up
  env_exports
  local LOG1="$STAGE/logs/w1.log" LOG2="$STAGE/logs/w2.log" LEDGER="$REPO/scripts/dispatch-ledger.py"
  : >"$LOG1"; : >"$LOG2"
  printf 'staging-positive-control\n' | python3 "$REPO/scripts/agent-tmux-send.py" stage:1.0 >/dev/null
  printf 'staging-positive-control\n' | python3 "$REPO/scripts/agent-tmux-send.py" stage:2.0 >/dev/null
  sleep 2
  grep -qF 'staging-positive-control' "$LOG1" || fail "window 1 log sink positive control failed"
  grep -qF 'staging-positive-control' "$LOG2" || fail "window 2 log sink positive control failed"
  : >"$LOG1"; : >"$LOG2"

  echo "--- phase 1: two owed tasks on one idle pane -> one actionable continuation reminder"
  node1="$(python3 "$LEDGER" open --to fake-w1 --subject "staging rehearsal happy path" --check true | awk 'NR==1{print $3}')"
  node2="$(python3 "$LEDGER" open --to fake-w1 --subject "staging rehearsal coalesced path" --check true | awk 'NR==1{print $3}')"
  tick
  sleep 3
  grep -qF "ORC reminder:" "$LOG1" || fail "continuation reminder did not arrive at the fake pane"
  grep -qF "task show $node1" "$LOG1" || fail "continuation reminder did not name the owed task and inspection command"
  grep -qF "task show $node2" "$LOG1" || fail "one reminder did not cover the second task on the same seat"
  [ "$(grep -cF 'ORC reminder:' "$LOG1")" -eq 1 ] || fail "continuation reminder fired more than once in one tick"
  peer_submissions="$(grep -cF '[agent-tmux-send from ' "$LOG1" || true)"
  [ "$peer_submissions" -eq 1 ] || fail "expected one peer-message submission, recorded $peer_submissions"
  [ "$(grep -cF "task show $node1" "$LOG1")" -eq 1 ] || fail "first task appeared more than once in the reminder"
  [ "$(grep -cF "task show $node2" "$LOG1")" -eq 1 ] || fail "second task appeared more than once in the reminder"
  if grep -q "批准" "$LOG1"; then fail "authorize fired without ask-evidence"; fi
  python3 "$LEDGER" close "$node2" --resolution done --note "coalescing assertion complete" >/dev/null

  echo "--- phase 2: terminal prose does not change the bounded silence path"
  echo stall >"$STAGE/ctl/w1"
  sleep 3   # let the stub render the stall line into the pane
  for i in 1 2 3 4 5 6; do tick; sleep 1; done
  if grep -qF "批准 立即执行!" "$LOG1"; then fail "operator authorization was typed by the engine"; fi
  python3 - "$DISPATCH_LEDGER_DB" "$node1" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
task = conn.execute("SELECT chases_total FROM dispatch WHERE id=?", (sys.argv[2],)).fetchone()
drive = conn.execute("SELECT st FROM drive WHERE task_id=?", (sys.argv[2],)).fetchone()
assert task == (1,), f"silent seat escalation count was not exactly one: {task}"
assert drive == ("escalated",), f"silent seat did not retain the escalation marker: {drive}"
PY

  echo "--- phase 2b: wake clock suppresses early retries, then repeats the reminder; busy work recovers"
  chases_before="$(python3 "$LEDGER" list --json | python3 -c 'import json,sys; print([json.loads(l)["chases_total"] for l in sys.stdin if "happy path" in l][0])')"
  tick
  sleep 3
  [ "$(grep -cF 'ORC reminder:' "$LOG1")" -eq 1 ] || fail "unexpired wake clock allowed an early reminder"
  chases_early="$(python3 "$LEDGER" list --json | python3 -c 'import json,sys; print([json.loads(l)["chases_total"] for l in sys.stdin if "happy path" in l][0])')"
  [ "$chases_early" -eq "$chases_before" ] || fail "unexpired wake clock created another escalation"
  python3 - "$DISPATCH_LEDGER_DB" "$node1" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
with conn:
    conn.execute("UPDATE wake_attempt SET at_ms=0 WHERE task_id=?", (sys.argv[2],))
PY
  tick
  sleep 3
  [ "$(grep -cF 'ORC reminder:' "$LOG1")" -eq 2 ] || fail "expired wake clock did not produce exactly one second reminder"
  chases_after="$(python3 "$LEDGER" list --json | python3 -c 'import json,sys; print([json.loads(l)["chases_total"] for l in sys.stdin if "happy path" in l][0])')"
  [ "$chases_after" -eq "$chases_before" ] || fail "second reminder created another escalation"
  python3 - "$DISPATCH_LEDGER_DB" "$node1" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
row = conn.execute("SELECT st FROM drive WHERE task_id=?", (sys.argv[2],)).fetchone()
assert row == ("escalated",), f"second reminder hid the escalation marker: {row}"
wake = conn.execute("SELECT COUNT(*),MAX(resolved_ms) FROM wake_attempt WHERE task_id=?", (sys.argv[2],)).fetchone()
assert wake == (1, 0), f"second reminder did not reuse the same unresolved wake clock: {wake}"
PY
  echo busy >"$STAGE/ctl/w1"
  sleep 3
  tick
  python3 - "$DISPATCH_LEDGER_DB" "$node1" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
row = conn.execute("SELECT st FROM drive WHERE task_id=?", (sys.argv[2],)).fetchone()
assert row == ("working",), f"real busy work did not clear the escalation marker: {row}"
wake = conn.execute("SELECT resolved_ms,outcome FROM wake_attempt WHERE task_id=?", (sys.argv[2],)).fetchone()
assert wake and wake[0] > 0 and wake[1] == "reacted-busy", f"busy work did not resolve the wake attempt: {wake}"
PY
  [ "$(grep -cF 'ORC reminder:' "$LOG1")" -eq 2 ] || fail "busy work triggered another reminder"
  if python3 "$LEDGER" brief | grep -qF "$node1"; then fail "recovered busy task remained in the operator brief"; fi

  echo "--- phase 3: closing the nodes prunes drive state to empty"
  for id in $(python3 "$LEDGER" list --json | python3 -c 'import json,sys; [print(json.loads(l)["id"]) for l in sys.stdin]'); do
    python3 "$LEDGER" close "$id" --resolution done --note "staging rehearsal over" >/dev/null
  done
  tick
  python3 - "$DISPATCH_LEDGER_DB" <<'EOF'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
rows = conn.execute("SELECT task_id, seat, st FROM drive").fetchall()
assert rows == [], f"drive state not pruned: {rows}"
EOF

  echo "--- phase 4: bus send leg - a short name resolves and DELIVERS; an unknown target fails loud and retries bounded"
  local ORC="$REPO/scripts/fleet-orchestrator.py"
  python3 "$ORC" dispatch --no-handshake --to performance-worker --subject "staging send leg" --check true >/dev/null
  grep -q '"target": "performance-worker"' "$STAGE/sends.log" \
    || fail "short-name dispatch did not reach Agent Bus unchanged"
  python3 "$ORC" dispatch --no-handshake --to no-such-seat --subject "staging dead letter" --check true \
    | grep -q "NOT delivered" || fail "unknown target did not warn loudly at dispatch time"
  tick >/dev/null; tick >/dev/null
  python3 - "$DISPATCH_LEDGER_DB" <<'EOF'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
ok = conn.execute("SELECT target,recipient_agent_id,send_state FROM task_msg"
                  " WHERE target='performance-worker'").fetchone()
assert ok and tuple(ok) == ("performance-worker", "fake-perf", "accepted"), \
    f"resolved dispatch not accepted by the recorded actual recipient: {ok}"
row = conn.execute("SELECT attempts, send_state, last_error FROM task_msg"
                   " WHERE target='no-such-seat'").fetchone()
assert row is not None, "dead-letter row missing"
attempts, state, err = row
assert state == "failed" and attempts >= 3, \
    f"failed send was not retried by the tick (attempts={attempts}, state={state})"
assert attempts <= 5, f"retry cap exceeded: {attempts}"
assert err, "no error evidence stored on the failed send"
EOF

  dead5="$(python3 "$LEDGER" list --json | python3 -c 'import json,sys; print([json.loads(l)["id"] for l in sys.stdin if "dead letter" in l][0])')"
  attempts_before_close="$(python3 - "$DISPATCH_LEDGER_DB" "$dead5" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
print(conn.execute("SELECT attempts FROM task_msg WHERE task_id=?", (sys.argv[2],)).fetchone()[0])
PY
)"
  for id in $(python3 "$LEDGER" list --json | python3 -c 'import json,sys; [print(json.loads(l)["id"]) for l in sys.stdin if "send leg" in l or "dead letter" in l]'); do
    python3 "$LEDGER" close "$id" --resolution done --note "phase 4 over" >/dev/null
  done
  tick >/dev/null
  python3 - "$DISPATCH_LEDGER_DB" "$dead5" "$attempts_before_close" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
row = conn.execute("SELECT send_state, attempts FROM task_msg WHERE task_id=?", (sys.argv[2],)).fetchone()
assert row == ("failed", int(sys.argv[3])), f"closed task was retried or audit evidence changed: {row}"
PY
  python3 "$ORC" doctor >/dev/null || fail "doctor stayed red on a handled closed-task send failure"
  echo "--- phase 5: blocked-on-human escalates ONCE, then the ladder holds quiet"
  echo idle >"$STAGE/ctl/w1"
  tmux -L "$SOCK" respawn-pane -k -t stage:1.0 \
    "FAKE_CTL='$STAGE/ctl/w1' FAKE_LOG='$STAGE/logs/w1.log' exec '$STAGE/bin/codex' '$STAGE/bin/agent-loop.sh'"
  sleep 3
  python3 "$LEDGER" open --to fake-w1 --subject "staging blocked hold" --check true >/dev/null
  node6="$(python3 "$LEDGER" list --json | python3 -c 'import json,sys; print([json.loads(l)["id"] for l in sys.stdin if "blocked hold" in l][0])')"
  ORC_SEAT_ID=fake-w1 python3 "$ORC" blocked "$node6" --note "staging rehearsal: operator must choose the hold path (fixture note per the blocked --note contract)" >/dev/null
  A1=$(grep -cF '批准' "$LOG1" || true); P1=$(grep -cF 'ORC reminder:' "$LOG1" || true)
  tick >/dev/null; tick >/dev/null
  chases="$(python3 "$LEDGER" list --json | python3 -c 'import json,sys; print([json.loads(l)["chases_total"] for l in sys.stdin if "blocked hold" in l][0])')"
  [ "$chases" -eq 1 ] || fail "blocked marker escalated $chases times, wanted exactly once"
  A2=$(grep -cF '批准' "$LOG1" || true); P2=$(grep -cF 'ORC reminder:' "$LOG1" || true)
  [ "$A2" -eq "$A1" ] || fail "authorize fired off the blocked verb alone"
  [ "$P2" -eq "$P1" ] || fail "ladder not held quiet while the blocked marker stands"

  echo "--- phase 6: a needs edge holds the dispatch message; the tick advances the successor with no seat awake"
  pred7="$(python3 "$ORC" open --to fake-w1 --subject "staging dep predecessor" --check true | awk 'NR==1{print $3}')"
  [ -n "$pred7" ] || fail "could not read the predecessor id out of the open output"
  python3 "$ORC" dispatch --to worker-1 --subject "staging dep successor" --check true --needs "$pred7" \
    | grep -q "HELD" || fail "dispatch did not report holding the message behind the predecessor"
  if grep -q "staging dep successor" "$STAGE/sends.log"; then fail "a waiting task's dispatch message went out anyway"; fi
  tick >/dev/null
  if grep -q "staging dep successor" "$STAGE/sends.log"; then fail "the held message went out while the predecessor was still open"; fi
  python3 "$LEDGER" close "$pred7" --resolution done --note "staging predecessor finished" >/dev/null
  tick >/dev/null
  grep -q "staging dep successor" "$STAGE/sends.log" || fail "the advance did not put the held message on the bus"
  python3 - "$DISPATCH_LEDGER_DB" <<'EOF'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
row = conn.execute("SELECT id, state FROM dispatch WHERE subject='staging dep successor'").fetchone()
assert row and row[1] == "open", f"successor did not advance past its dependency: {row}"
msg = conn.execute("SELECT target,recipient_agent_id,send_state FROM task_msg WHERE task_id=?"
                   " AND purpose='dispatch'", (row[0],)).fetchone()
assert msg is not None and tuple(msg) == ("worker-1", "fake-w1", "accepted"), \
    f"held dispatch message was not delivered on advance: {msg}"
kinds = [r[0] for r in conn.execute("SELECT kind FROM event WHERE dispatch_id=?"
                                    " ORDER BY id", (row[0],))]
assert kinds[:2] == ["open-waiting", "deps-cleared"], f"unexpected event log: {kinds}"
EOF

  cmd_down >/dev/null
  echo "OK e2e rehearsal passed: actionable reminder names its tasks, never types operator authorization, bounded silence escalates once, the existing wake clock repeats the reminder while preserving the marker, busy work recovers, close prunes state, the send leg delivers on short names and retries dead letters bounded, blocked-on-human escalates once then holds quiet, and a needs edge holds its dispatch message until the tick advances the successor"
}

case "$CMD" in
  up) cmd_up ;;
  down) cmd_down ;;
  status) cmd_status ;;
  bus-e2e) cmd_bus_e2e ;;
  e2e) cmd_e2e ;;
  *) echo "Usage: fleet-staging.sh up|down|status|bus-e2e|e2e [stage-dir]"; exit 2 ;;
esac
