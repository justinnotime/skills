#!/usr/bin/env bash
# the tview reap may ONLY touch detached tview-* views
# idle >48h. These scenarios run against a recording tmux stub - no live
# server - and pin the untouchability of the primary and of anything
# attached BEFORE any live reaping happens (the primary-session deletion
# incident stays unrepeatable).

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

fail() { echo "FAIL: $1" >&2; exit 1; }

mkdir -p "$TMP/home" "$TMP/fleets"
printf '%s\n' '{"schema":"fleet-runtime/v1"}' > "$TMP/config.json"

NOW=$(date +%s)
OLD=$((NOW - 49 * 3600))     # idle 49h: past the 48h bar
FRESH=$((NOW - 3600))        # idle 1h: kept

cat > "$TMP/sessions.txt" <<EOF
0|0|$OLD
tview-user-old|0|$OLD
tview-user-attached|1|$OLD
tview-user-fresh|0|$FRESH
EOF

cat > "$TMP/tmux" <<'EOF'
#!/usr/bin/env bash
[[ "${1:-}" == "-L" ]] && shift 2
echo "$@" >> "$TMUX_STUB_LOG"
case "$1" in
  has-session) exit 0 ;;
  list-sessions)
    # -F '#{session_name}|#{session_group}' for the group lookup, or the
    # reap format with attached|activity - serve by requested format
    if [[ "$*" == *session_group* ]]; then
      awk -F'|' '{print $1"|0"}' "$TMUX_STUB_SESSIONS"
    else
      cat "$TMUX_STUB_SESSIONS"
    fi ;;
  kill-session) exit 0 ;;
  new-session|set-option) exit 0 ;;
  display-message) echo "0" ;;
  attach-session) exit 0 ;;
  *) exit 0 ;;
esac
EOF
chmod +x "$TMP/tmux"

run_tview() {
  : > "$TMP/calls.log"
  HOME="$TMP/home" FLEET_ORCHESTRATOR_CONFIG="$TMP/config.json" \
    NW_DEFAULT_TMUX_SERVER=test-default NW_FLEET_PROFILE_DIR="$TMP/fleets" \
    NW_TMUX_SERVER= NW_FLEET_PROFILE_APPLIED= \
    TMUX_BIN="$TMP/tmux" TMUX_STUB_LOG="$TMP/calls.log" \
    TMUX_STUB_SESSIONS="$TMP/sessions.txt" TMUX= NW_FLEET= \
    "$@" bash "$ROOT/scripts/tview" </dev/null >/dev/null 2>&1 || true
}

# scenario 1: default run reaps EXACTLY the detached idle tview view
run_tview env
kills=$(grep -c '^kill-session' "$TMP/calls.log" || true)
[ "$kills" = "1" ] || fail "expected exactly 1 kill, got $kills"
grep -q '^kill-session -t =tview-user-old$' "$TMP/calls.log" \
  || fail "the one kill must target the detached idle tview view"
grep -q 'kill-session -t =0' "$TMP/calls.log" \
  && fail "the primary session must NEVER be killed (even detached+ancient)"
grep -q 'kill-session -t =tview-user-attached' "$TMP/calls.log" \
  && fail "an ATTACHED view must never be killed"
grep -q 'kill-session -t =tview-user-fresh' "$TMP/calls.log" \
  && fail "a fresh detached view must not be killed"

# scenario 2: the kill switch disables the pass entirely
run_tview env TVIEW_REAP=off
grep -q '^kill-session' "$TMP/calls.log" \
  && fail "TVIEW_REAP=off must reap nothing"

# scenario 3: missing activity field never reaps (unknown is not idle)
printf '%s\n' "tview-user-noact|0|" > "$TMP/sessions.txt"
run_tview env
grep -q '^kill-session' "$TMP/calls.log" \
  && fail "a view with unknown activity must not be reaped"

# scenario 4: a configured primary may itself use the tview-* namespace.
# Its exact identity must protect it even when detached and ancient.
printf '%s\n' \
  '{"schema":"fleet-runtime/v1","tmux":{"primary_session":"tview-primary"}}' \
  > "$TMP/config.json"
cat > "$TMP/sessions.txt" <<EOF
tview-primary|0|$OLD
tview-user-old|0|$OLD
EOF
run_tview env
grep -q '^kill-session -t =tview-primary$' "$TMP/calls.log" \
  && fail "the configured tview-* primary must never be reaped"
grep -q '^kill-session -t =tview-user-old$' "$TMP/calls.log" \
  || fail "protecting a tview-* primary must still reap an old detached view"

echo "tview reap regression: all checks passed"
