#!/usr/bin/env bash
# tview isolation test: a PRIVATE -L server keeps fixtures away from live
# terminals; sessions within it are separate fleets. Every assertion SAYS what failed, and the
# environment is scrubbed - the pre-rework version died silently at `wait`
# whenever it ran inside a tmux session, because script(1) inherited TMUX
# and tview asked a private server for a client it never had.

set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
TVIEW_RUNTIME="$ROOT/scripts/tview"
server="tview-test-$RANDOM-$$"
fleet_name="view-$RANDOM"
fleet_server="tview-fleet-$RANDOM-$$"
offline_server="tview-offline-$RANDOM-$$"
missing_server="tview-missing-$RANDOM-$$"
stage=$(mktemp -d /tmp/tview-test.XXXXXX)
# tmux leaves a -L socket pathname behind after kill-server. Keep it inside
# this test's existing temporary directory so the EXIT cleanup owns it too.
export TMUX_TMPDIR="$stage"
socket_dir="$TMUX_TMPDIR/tmux-$(id -u)"
cleanup() {
  local cleanup_failed=0 private_server
  for private_server in "$server" "$fleet_server" "$offline_server" "$missing_server"; do
    if [[ -S "$socket_dir/$private_server" ]] \
       || tmux -L "$private_server" has-session 2>/dev/null; then
      if ! tmux -L "$private_server" kill-server 2>/dev/null; then
        echo "FAIL: could not stop $private_server; preserved $stage" >&2
        cleanup_failed=1
      fi
    fi
  done
  (( cleanup_failed == 0 )) || return 1
  if [[ ${TVIEW_TEST_KEEP_ARTIFACTS:-0} == 1 ]]; then
    echo "tview test artifacts: $stage" >&2
  else
    rm -rf "$stage"
  fi
}
trap 'cleanup || exit 1' EXIT

fail() { echo "FAIL: $1" >&2; exit 1; }

# Every runtime invocation gets an isolated caller configuration. A wrapper
# also keeps those settings when invoked inside a real attached tmux shell.
mkdir -p "$stage/home" "$stage/fleets"
cat >"$stage/config.json" <<EOF
{
  "schema": "fleet-runtime/v1",
  "fleets": {"default_name": "primary"},
  "tmux": {"primary_session": "0"}
}
EOF
cat >"$stage/tview" <<EOF
#!/usr/bin/env bash
exec env -u NW_TMUX_SERVER -u NW_FLEET_PROFILE_APPLIED \
  -u MATRIX_BUS_ROOM -u MATRIX_BUS_REGISTRY_ROOM \
  HOME="$stage/home" XDG_CONFIG_HOME="$stage/home/.config" \
  XDG_STATE_HOME="$stage/home/.local/state" XDG_CACHE_HOME="$stage/home/.cache" \
  FLEET_ORCHESTRATOR_CONFIG="$stage/config.json" \
  NW_DEFAULT_TMUX_SERVER="$server" NW_FLEET_PROFILE_DIR="$stage/fleets" \
  NW_FLEET_RUNTIME_ROOT="$stage/runtime" NW_FLEET_MATRIX_CFG_ROOT="$stage/matrix" \
  TVIEW_FLEET_PROFILE="$ROOT/scripts/lib/fleet-profile.py" \
  TMUX_BIN="$(command -v tmux)" "$TVIEW_RUNTIME" "\$@"
EOF
chmod +x "$stage/tview"
TVIEW="$stage/tview"

tmux -f /dev/null -L "$server" new-session -d -s 0 -n user-shell 'sleep 300' \
  || fail "could not start the private server"
tmux -L "$server" set-option -t 0 destroy-unattached off
tmux -L "$server" new-window -d -t 0:1 -n one 'sleep 300'
tmux -L "$server" new-window -d -t 0:2 -n two 'sleep 300'
tmux -L "$server" new-session -d -s alternate -n alt-zero 'sleep 300'
tmux -L "$server" new-window -d -t '=alternate:5' -n alt-five 'sleep 300'

cat >"$stage/fleets/$fleet_name.json" <<EOF
{
  "schema": 1,
  "name": "$fleet_name",
  "tmux_server": "$fleet_server",
  "primary_session": "main",
  "matrix_homeserver": "https://matrix.example.test",
  "matrix_room": "!message-$RANDOM:example.test",
  "matrix_registry_room": "!registry-$RANDOM:example.test"
}
EOF
tmux -f /dev/null -L "$fleet_server" new-session -d -s main -n fleet-zero 'sleep 300'
tmux -L "$fleet_server" new-window -d -t '=main:7' -n fleet-seven 'sleep 300'

python3 - "$stage/fleets" "$fleet_name" "$offline_server" "$missing_server" <<'PYFIXTURE'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
source = json.loads((root / (sys.argv[2] + ".json")).read_text())
for name, server_name in zip(("offline", "missing"), sys.argv[3:]):
    profile = dict(source, name=name, tmux_server=server_name,
                   matrix_room=f"!message-{name}:example.test",
                   matrix_registry_room=f"!registry-{name}:example.test")
    (root / (name + ".json")).write_text(json.dumps(profile))
PYFIXTURE
tmux -f /dev/null -L "$missing_server" new-session -d -s unrelated -n shell 'sleep 300'

# Discovery must distinguish a stopped server from a running server whose
# configured primary is absent, without creating either sessions or state.
catalog_before=$(for private_server in "$server" "$fleet_server" "$missing_server"; do
  tmux -L "$private_server" list-sessions -F '#{session_name}|#{session_id}'
done)
TMUX= NW_FLEET= "$TVIEW" --list --json >"$stage/catalog.json"
TMUX= NW_FLEET= "$TVIEW" --list >"$stage/catalog.txt"
catalog_after=$(for private_server in "$server" "$fleet_server" "$missing_server"; do
  tmux -L "$private_server" list-sessions -F '#{session_name}|#{session_id}'
done)
[[ "$catalog_before" == "$catalog_after" ]] \
  || fail "listing fleets changed existing tmux sessions"
[[ ! -S "$socket_dir/$offline_server" && ! -d "$stage/runtime" && ! -d "$stage/matrix" ]] \
  || fail "listing fleets created a server or runtime state"
python3 - "$stage/catalog.json" "$fleet_name" <<'PYCATALOG'
import json
import sys

rows = json.load(open(sys.argv[1]))
by_name = {row["name"]: row for row in rows}
assert set(by_name) == {"primary", sys.argv[2], "offline", "missing", "alternate"}, rows
assert by_name["alternate"]["status"] == "online", rows
assert by_name["primary"]["status"] == "online", rows
assert by_name[sys.argv[2]]["status"] == "online", rows
assert by_name["offline"]["status"] == "offline", rows
assert by_name["missing"]["status"] == "missing-primary", rows
assert by_name["primary"]["primary_session"] == "0", rows
PYCATALOG
for fleet in primary "$fleet_name" offline missing; do
  grep -q "$fleet" "$stage/catalog.txt" || fail "fleet $fleet missing from readable list"
done
for unavailable_fleet in offline missing; do
  if TMUX= NW_FLEET= "$TVIEW" --fleet "$unavailable_fleet" \
      >"$stage/$unavailable_fleet.log" 2>&1; then
    fail "unavailable fleet $unavailable_fleet was accepted"
  fi
  grep -q -- 'tview --list' "$stage/$unavailable_fleet.log" \
    || fail "unavailable fleet $unavailable_fleet did not explain discovery"
done
[[ ! -S "$socket_dir/$offline_server" ]] \
  || fail "selecting a stopped fleet started its server"
[[ $(tmux -L "$missing_server" list-sessions -F '#{session_name}') == unrelated ]] \
  || fail "selecting a missing primary created or changed sessions"

for private_server in "$server" "$fleet_server" "$missing_server"; do
  actual_socket=$(tmux -L "$private_server" display-message -p '#{socket_path}')
  [[ "$actual_socket" == "$socket_dir/$private_server" ]] \
    || fail "private server $private_server used unexpected socket $actual_socket"
  [[ ! -e "/tmp/tmux-$(id -u)/$private_server" ]] \
    || fail "private server $private_server leaked a socket into global /tmp"
done

view_flow() {  # $1 = log name, $2 = window keys
  # TMUX= scrubs the leaked outer-tmux variable: tview must key the view
  # off the pty, not ask the PRIVATE server about an outer client
  ( sleep 1; printf "$2"; sleep 1; printf '\002d' ) \
    | TERM=xterm-256color timeout 10 script -qec \
      "TERM=xterm-256color TMUX= NW_FLEET= $TVIEW" /dev/null \
      >"$stage/$1.log" 2>&1
}

view_flow a '\0022' & a=$!
view_flow b '\0021' & b=$!
wait "$a" || fail "view flow A exited nonzero: $(tail -3 "$stage/a.log" | tr '\n' ' ')"
wait "$b" || fail "view flow B exited nonzero: $(tail -3 "$stage/b.log" | tr '\n' ' ')"

fmt='#{session_name}|#{session_group}|#{session_attached}|#{destroy_unattached}'
sessions=$(tmux -L "$server" list-sessions -F "$fmt")
grep -q '^0|' <<<"$sessions" || fail "primary session missing: $sessions"
[[ $(grep -c '^tview-' <<<"$sessions") -eq 2 ]] \
  || fail "expected 2 tview views, got: $sessions"
grep -q '|on$' <<<"$sessions" \
  && fail "destroy-unattached must never be on: $sessions"
[[ $(tmux -L "$server" list-windows -t 0 | wc -l) -eq 3 ]] \
  || fail "primary window count changed"
[[ $(tmux -L "$server" list-panes -a -F '#{pane_id}' | sort -u | wc -l) -eq 5 ]] \
  || fail "grouped views must share panes, not copy them"

# The no-argument path above must still mean session 0. One positional
# argument now always selects a window in that internal primary session,
# first by index and then by exact name.
( sleep 1; printf '\002d' ) \
  | TERM=xterm-256color timeout 10 script -qec \
    "TERM=xterm-256color TMUX= NW_FLEET=default $TVIEW 2" \
    /dev/null >"$stage/positional-index.log" 2>&1 \
  || fail "default-fleet positional window-index flow failed: $(tail -3 "$stage/positional-index.log" | tr '\n' ' ')"

index_rows=$(tmux -L "$server" list-sessions \
  -F '#{session_name}|#{session_group}|#{window_index}')
grep -Eq '^tview-[^|]+\|0\|2$' <<<"$index_rows" \
  || fail "positional window index 2 was not selected in session 0: $index_rows"

( sleep 1; printf '\002d' ) \
  | TERM=xterm-256color timeout 10 script -qec \
    "TERM=xterm-256color TMUX= NW_FLEET= $TVIEW one" \
    /dev/null >"$stage/positional-name.log" 2>&1 \
  || fail "positional window-name flow failed: $(tail -3 "$stage/positional-name.log" | tr '\n' ' ')"

group_rows=$(tmux -L "$server" list-sessions \
  -F '#{session_name}|#{session_group}|#{window_index}')
grep -Eq '^tview-[^|]+\|0\|1$' <<<"$group_rows" \
  || fail "exact positional window name 'one' was not selected: $group_rows"
awk -F'|' '$1 ~ /^tview-/ && $2 != "0" {exit 1}' <<<"$group_rows" \
  || fail "a positional window unexpectedly selected another session: $group_rows"

# The configured default alias selects the same group even with a conflicting
# inherited named fleet. No duplicate profile or server is needed.
( sleep 1; printf '\002d' ) \
  | TERM=xterm-256color timeout 10 script -qec \
    "TERM=xterm-256color TMUX= NW_FLEET=$fleet_name $TVIEW --fleet primary 2" \
    /dev/null >"$stage/default-alias.log" 2>&1 \
  || fail "configured default alias failed: $(tail -3 "$stage/default-alias.log" | tr '\n' ' ')"
alias_rows=$(tmux -L "$server" list-sessions -F '#{session_name}|#{session_group}|#{window_index}')
grep -Eq '^tview-[^|]+\|0\|2$' <<<"$alias_rows" \
  || fail "configured default alias did not select the existing default group"

# Merely having a named-fleet profile must not redirect the no-argument path.
# The flows above used only the original private server; the fleet server is
# still untouched until --fleet is explicit.
if tmux -L "$fleet_server" list-sessions -F '#{session_name}' | grep -q '^tview-'; then
  fail "no-argument tview leaked into the named fleet server"
fi

# An explicit default selector can switch from an unrelated session group.
# It selects the requested window in session 0's grouped view.
cat >"$stage/same-server-command" <<EOF
#!/usr/bin/env bash
exec env TERM=xterm-256color NW_FLEET= $TVIEW --fleet default --window 1
EOF
chmod +x "$stage/same-server-command"
cat >"$stage/unknown-command" <<EOF
#!/usr/bin/env bash
NW_FLEET=$fleet_name "$TVIEW" >"$stage/unknown.log" 2>&1
printf '%s\n' "\$?" >"$stage/unknown.status"
EOF
chmod +x "$stage/unknown-command"
tmux -L "$server" new-window -d -t '=alternate:1' -n alt-one \
  'exec bash --noprofile --norc -i'
same_input="$stage/same-server-input"
mkfifo "$same_input"
exec {same_input_fd}<>"$same_input"
TERM=xterm-256color timeout 15 script -qec \
  "TERM=xterm-256color TMUX= $(command -v tmux) -L $server attach-session -t 'alternate:1'" \
  /dev/null <"$same_input" >"$stage/same-server.log" 2>&1 &
same_pid=$!

same_source_row=
for _ in {1..50}; do
  same_source_row=$(tmux -L "$server" list-clients \
    -F '#{client_tty}|#{session_name}|#{window_index}' 2>/dev/null \
    | head -n 1 || true)
  [[ "$same_source_row" == *'|alternate|1' ]] && break
  sleep 0.1
done
[[ "$same_source_row" == *'|alternate|1' ]] \
  || fail "client never reached alternate window 1: $same_source_row"
same_client_tty=${same_source_row%%|*}
tmux -L "$server" send-keys -t 'alternate:1' "$stage/unknown-command" Enter
for _ in {1..50}; do
  [[ -f "$stage/unknown.status" ]] && break
  sleep 0.1
done
[[ -f "$stage/unknown.status" && $(cat "$stage/unknown.status") == 0 ]] \
  || fail "bare tview did not recognize the native session as its fleet"
auto_group=$(tmux -L "$server" list-clients -F '#{client_tty}|#{session_group}' \
  | awk -F'|' -v tty="$same_client_tty" '$1 == tty {print $2}')
[[ "$auto_group" == alternate ]] \
  || fail "bare tview followed a stale fleet variable instead of the actual session"
tmux -L "$server" send-keys -t 'alternate:1' "$stage/same-server-command" Enter

same_target_row=
for _ in {1..50}; do
  same_target_row=$(tmux -L "$server" list-clients \
    -F '#{client_tty}|#{session_name}|#{session_group}|#{window_index}' \
    2>/dev/null | head -n 1 || true)
  [[ "$same_target_row" == *'|0|1' ]] && break
  sleep 0.1
done
IFS='|' read -r same_target_tty same_target_session same_target_group \
  same_target_window <<<"$same_target_row"
[[ "$same_target_tty" == "$same_client_tty" \
   && "$same_target_session" == tview-* \
   && "$same_target_group" == 0 \
   && "$same_target_window" == 1 ]] \
  || fail "explicit default did not preserve window 1 across session groups: $same_target_row"
tmux -L "$server" detach-client -t "$same_target_tty"
wait "$same_pid" \
  || fail "same-server client flow exited nonzero: $(tail -3 "$stage/same-server.log" | tr '\n' ' ')"
exec {same_input_fd}>&-

( sleep 1; printf '\002d' ) \
  | TERM=xterm-256color timeout 10 script -qec \
    "TERM=xterm-256color TMUX= NW_FLEET= $TVIEW --fleet $fleet_name 7" \
    /dev/null >"$stage/named-fleet.log" 2>&1 \
  || fail "named-fleet flow failed: $(tail -3 "$stage/named-fleet.log" | tr '\n' ' ')"

fleet_rows=$(tmux -L "$fleet_server" list-sessions \
  -F '#{session_name}|#{session_group}|#{window_index}')
grep -Eq '^tview-[^|]+\|main\|7$' <<<"$fleet_rows" \
  || fail "--fleet did not attach its configured server/window: $fleet_rows"
grep -q '^main|' <<<"$fleet_rows" \
  || fail "named fleet primary session disappeared: $fleet_rows"
tmux -L "$server" has-session -t '=main' 2>/dev/null \
  && fail "named fleet session leaked into the original server"

# Outside tmux, an explicitly inherited NW_FLEET remains a supported selector;
# callers only name the window and tview resolves its server and primary.
( sleep 1; printf '\002d' ) \
  | TERM=xterm-256color timeout 10 script -qec \
    "TERM=xterm-256color TMUX= NW_FLEET=$fleet_name $TVIEW fleet-seven" \
    /dev/null >"$stage/inherited-fleet.log" 2>&1 \
  || fail "inherited-fleet flow failed: $(tail -3 "$stage/inherited-fleet.log" | tr '\n' ' ')"

inherited_rows=$(tmux -L "$fleet_server" list-sessions \
  -F '#{session_name}|#{session_group}|#{window_index}')
grep -Eq '^tview-[^|]+\|main\|7$' <<<"$inherited_rows" \
  || fail "NW_FLEET did not select the named fleet and exact window: $inherited_rows"

# Exercise the cross-server handoff from a real attached tmux client. The
# command runs inside server A, so tview must use detach-client -E to replace
# that terminal's client with an attachment to server B's selected window.
cat >"$stage/handoff-command" <<EOF
#!/usr/bin/env bash
exec env TERM=xterm-256color $TVIEW --fleet $fleet_name --window 7
EOF
chmod +x "$stage/handoff-command"
cat >"$stage/stale-default-command" <<EOF
#!/usr/bin/env bash
NW_FLEET=$fleet_name "$TVIEW" --window 9
printf '%s\n' "\$?" >"$stage/stale-default.status"
EOF
cat >"$stage/stale-named-command" <<EOF
#!/usr/bin/env bash
NW_FLEET=default "$TVIEW" --window 7
printf '%s\n' "\$?" >"$stage/stale-named.status"
EOF
cat >"$stage/return-command" <<EOF
#!/usr/bin/env bash
exec "$TVIEW" --fleet default --window 9
EOF
chmod +x "$stage/stale-default-command" "$stage/stale-named-command" "$stage/return-command"
# The destination pane needs a shell to exercise a second call from its
# grouped view and then return to the default fleet on the same terminal.
tmux -L "$fleet_server" respawn-window -k -t '=main:7' \
  'exec bash --noprofile --norc -i'

tmux -L "$server" new-window -d -t '0:9' -n handoff-shell \
  'exec bash --noprofile --norc -i'
a_sessions_before=$(tmux -L "$server" list-sessions -F '#{session_name}' | sort)
b_sessions_before=$(tmux -L "$fleet_server" list-sessions -F '#{session_name}' | sort)

handoff_input="$stage/handoff-input"
mkfifo "$handoff_input"
exec {handoff_input_fd}<>"$handoff_input"
TERM=xterm-256color timeout 15 script -qec \
  "TERM=xterm-256color TMUX= $(command -v tmux) -L $server attach-session -t '0:9'" \
  /dev/null <"$handoff_input" >"$stage/handoff.log" 2>&1 &
handoff_pid=$!

a_client_tty=
for _ in {1..50}; do
  a_client_tty=$(tmux -L "$server" list-clients -F '#{client_tty}' \
    2>/dev/null | head -n 1 || true)
  [[ -n "$a_client_tty" ]] && break
  sleep 0.1
done
[[ -n "$a_client_tty" ]] \
  || fail "client never attached to server A: $(tail -3 "$stage/handoff.log" | tr '\n' ' ')"

# A real tmux client is authoritative even when its shell inherited another
# fleet name. Exercise primary-session discovery before the cross-server call.
tmux -L "$server" send-keys -t '0:9' "$stage/stale-default-command" Enter
for _ in {1..50}; do
  [[ -f "$stage/stale-default.status" ]] && break
  sleep 0.1
done
[[ -f "$stage/stale-default.status" && $(cat "$stage/stale-default.status") == 0 ]] \
  || fail "automatic default-fleet association did not finish successfully"
default_client_row=$(tmux -L "$server" list-clients \
  -F '#{client_tty}|#{session_name}|#{session_group}|#{window_index}')
IFS='|' read -r default_tty default_view default_group default_window <<<"$default_client_row"
[[ "$default_tty" == "$a_client_tty" && "$default_view" == tview-* \
   && "$default_group" == 0 && "$default_window" == 9 ]] \
  || fail "stale NW_FLEET redirected a real default-fleet client: $default_client_row"

tmux -L "$server" send-keys -t '0:9' "$stage/handoff-command" Enter

b_client_row=
for _ in {1..50}; do
  b_client_row=$(tmux -L "$fleet_server" list-clients \
    -F '#{client_tty}|#{session_name}|#{window_index}' 2>/dev/null \
    | head -n 1 || true)
  [[ "$b_client_row" == *'|7' ]] && break
  sleep 0.1
done
[[ "$b_client_row" == *'|7' ]] \
  || fail "client did not reach server B window 7: $b_client_row; $(tail -3 "$stage/handoff.log" | tr '\n' ' ')"
IFS='|' read -r b_client_tty b_client_session b_client_window <<<"$b_client_row"
[[ "$b_client_tty" == "$a_client_tty" ]] \
  || fail "cross-server handoff changed terminal: A=$a_client_tty B=$b_client_tty"
[[ "$b_client_session" == tview-* && "$b_client_window" == 7 ]] \
  || fail "server B client is not in the requested tview window: $b_client_row"

# The association must also recognize a grouped tview session, not only the
# primary session, and ignore an inherited default label in that named fleet.
tmux -L "$fleet_server" send-keys -t '=main:7' "$stage/stale-named-command" Enter
for _ in {1..50}; do
  [[ -f "$stage/stale-named.status" ]] && break
  sleep 0.1
done
[[ -f "$stage/stale-named.status" && $(cat "$stage/stale-named.status") == 0 ]] \
  || fail "automatic named-fleet association did not finish successfully"
named_client_row=$(tmux -L "$fleet_server" list-clients \
  -F '#{client_tty}|#{session_name}|#{session_group}|#{window_index}')
IFS='|' read -r named_tty named_view named_group named_window <<<"$named_client_row"
[[ "$named_tty" == "$a_client_tty" && "$named_view" == tview-* \
   && "$named_group" == main && "$named_window" == 7 ]] \
  || fail "stale NW_FLEET redirected a named-fleet grouped client: $named_client_row"

tmux -L "$fleet_server" send-keys -t '=main:7' "$stage/return-command" Enter
return_row=
for _ in {1..50}; do
  return_row=$(tmux -L "$server" list-clients \
    -F '#{client_tty}|#{session_name}|#{session_group}|#{window_index}' 2>/dev/null \
    | head -n 1 || true)
  [[ "$return_row" == *'|0|9' ]] && break
  sleep 0.1
done
IFS='|' read -r return_tty return_view return_group return_window <<<"$return_row"
[[ "$return_tty" == "$a_client_tty" && "$return_view" == tview-* \
   && "$return_group" == 0 && "$return_window" == 9 ]] \
  || fail "explicit default did not return on the same terminal: $return_row"
[[ -z $(tmux -L "$fleet_server" list-clients -F '#{client_tty}') ]] \
  || fail "return to default left an attached named-fleet client"
tmux -L "$server" detach-client -t "$return_tty"
wait "$handoff_pid" \
  || fail "cross-server client flow exited nonzero: $(tail -3 "$stage/handoff.log" | tr '\n' ' ')"
exec {handoff_input_fd}>&-

a_sessions_after=$(tmux -L "$server" list-sessions -F '#{session_name}' | sort)
b_sessions_after=$(tmux -L "$fleet_server" list-sessions -F '#{session_name}' | sort)
missing_a=$(comm -23 <(printf '%s\n' "$a_sessions_before") \
  <(printf '%s\n' "$a_sessions_after"))
missing_b=$(comm -23 <(printf '%s\n' "$b_sessions_before") \
  <(printf '%s\n' "$b_sessions_after"))
[[ -z "$missing_a" && -z "$missing_b" ]] \
  || fail "cross-server handoff deleted sessions: A=[$missing_a] B=[$missing_b]"
tmux -L "$server" has-session -t '=0' \
  || fail "server A primary session disappeared during handoff"
tmux -L "$fleet_server" has-session -t '=main' \
  || fail "server B primary session disappeared during handoff"

if TERM=xterm-256color TMUX= NW_FLEET= "$TVIEW" 99 \
    >"$stage/missing-window.log" 2>&1; then
  fail "missing window was accepted"
fi
grep -q "window '99' not found in session '0'" "$stage/missing-window.log" \
  || fail "missing-window error was not explicit"

if TERM=xterm-256color TMUX= NW_FLEET= \
    "$TVIEW" alternate 99 >"$stage/old-two-positionals.log" 2>&1; then
  fail "the retired session/window positional form was accepted"
fi
grep -q "session selection was removed; expected at most one WINDOW" \
  "$stage/old-two-positionals.log" \
  || fail "the retired two-positional form did not explain the replacement"

if TERM=xterm-256color TMUX= NW_FLEET= \
    "$TVIEW" --session alternate --window 5 >"$stage/old-session-option.log" 2>&1; then
  fail "the retired --session option was accepted"
fi
grep -q -- "--session was removed; choose a fleet and window only" \
  "$stage/old-session-option.log" \
  || fail "the retired --session option did not explain the replacement"

# ---- reap prong on the REAL private server ----
# both views are now detached; with a 1s idle bar and 2s of quiet they are
# reapable - but the kill switch must hold everything, and the primary
# must survive every pass by construction
sleep 2
TVIEW_REAP=off view_flow c '\0020' \
  || fail "kill-switch flow exited nonzero: $(tail -3 "$stage/c.log" | tr '\n' ' ')"
[[ $(tmux -L "$server" list-sessions -F '#{session_name}' | grep -c '^tview-') -ge 2 ]] \
  || fail "TVIEW_REAP=off must reap nothing"

sleep 2
TVIEW_REAP_IDLE_S=1 view_flow d '\0020' \
  || fail "reap flow exited nonzero: $(tail -3 "$stage/d.log" | tr '\n' ' ')"
after=$(tmux -L "$server" list-sessions -F "$fmt")
grep -q '^0|' <<<"$after" || fail "the PRIMARY session was reaped: $after"
live_views=$(grep -c '^tview-' <<<"$after") || true
# the reap pass ran before flow-d created/attached its own view: the two
# idle detached views from earlier must be gone, flow-d's own view remains
[[ "$live_views" -le 1 ]] \
  || fail "detached idle views survived the reap: $after"

# ---- source-code contract (updated for the sanctioned reap) ----
# kill-session may appear ONLY inside the marked reap block; kill-server
# and destroy-unattached-on stay banned everywhere in the executable path
source_code=$(awk '
  /^  cat <<.USAGE./ {in_help=1; next}
  in_help && /^USAGE$/ {in_help=0; next}
  /^# Reap ONLY detached tview-/ {in_reap=1}
  in_reap && /^fi$/ {in_reap=0; next}
  !in_help && !in_reap && $0 !~ /^[[:space:]]*#/ {print}
' "$TVIEW_RUNTIME")
grep -Eq '(^|[;&|[:space:]])kill-server([;&|[:space:]]|$)' <<<"$source_code" \
  && fail "kill-server found in tview executable path"
grep -Eq '(^|[;&|[:space:]])kill-session([;&|[:space:]]|$)' <<<"$source_code" \
  && fail "kill-session found OUTSIDE the sanctioned reap block"
grep -Eq 'destroy-unattached[[:space:]]+on' <<<"$source_code" \
  && fail "destroy-unattached on found in tview executable path"
reap_block=$(awk '/^# Reap ONLY detached tview-/,/^fi$/' "$TVIEW_RUNTIME")
grep -q 'tview-\*)' <<<"$reap_block" \
  || fail "the reap block lost its tview-* namespace filter"
grep -q '"$attached" == "0"' <<<"$reap_block" \
  || fail "the reap block lost its detached-only filter"

echo "tview integration test: ok (views grouped, reap bounded, primary untouchable)"
