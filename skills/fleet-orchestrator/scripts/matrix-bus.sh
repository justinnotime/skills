#!/usr/bin/env bash
# matrix-bus.sh — single CLI for one fleet's Agent Bus.
# Bundled local/Matrix transport; settings come from the runtime configuration.
#
#   --fleet <name> <verb>      select one fully isolated named fleet
#   handle <task-slug>         compose <host>/<slug>-tmux<N>
#   tmux-id                    print this process's tmux pane
#   setup <handle>             initialize the selected transport
#   join <handle> <slot> <harness> <watch|pull> <host> <tmux-id>
#   members                    list active agents
#   registry-migrate [--legacy-timeline]  one-time local identity publication
#   send <sender-id> <target> <subject> <body> [--priority ...]
#   pull <agent-id> [--max N] [--max-bytes N]
#   replay <agent-id> [--max N] [--max-bytes N]   re-show inbox WITHOUT consuming
#                               (pull leases what it shows, so a truncated pull
#                                looks like "no messages"; replay never consumes)
#   unread <agent-id>           ingest + report unread metadata without presenting
#   dispatch [--once]           ingest for local pull-only agents without presenting
#   watch <agent-id>           durable inbox watcher
#   ack <agent-id> <msg-id> <ok|rejected|failed> [detail]
#   revive <agent-id> <msg-id>   re-arm a PARKED message (presentation cap hit)
#   delivery <sender-id> <msg-id>
#   retry <sender-id>
#   retire <agent-id>

set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROFILE="$HERE/lib/fleet-profile.py"
entry_resolved=0
if [[ "${1:-}" == --resolved ]]; then entry_resolved=1; shift; fi
if [[ "${1:-}" == "--config" ]]; then
  (($# >= 3)) || { echo "--config requires a file and a command" >&2; exit 2; }
  export FLEET_ORCHESTRATOR_CONFIG=$2
  shift 2
fi
if [[ "$entry_resolved" == 0 ]]; then
  selection=()
  if [[ "${1:-}" == "--fleet" ]]; then
    (($# >= 2)) || { echo "--fleet requires a name" >&2; exit 2; }
    selection=(--fleet "$2")
    shift 2
  fi
  exec python3 "$PROFILE" exec-current "${selection[@]}" -- bash "$0" --resolved "$@"
fi

ADAPTER="$HERE/agent-bus-v3.py"
case "${1:-}" in
  join|retire|members|heartbeat|registry-migrate|send|retry|pull|replay|unread|dispatch|watch|ack|delivery|revive|notify-claim|environment|brief)
    exec python3 "$ADAPTER" "$@" ;;
esac
resolved=$(python3 "$ADAPTER" environment)
CFG=$(printf '%s' "$resolved" | jq -er .config_directory)
TRANSPORT=$(printf '%s' "$resolved" | jq -er .transport)
HDR=$(printf '%s' "$resolved" | jq -er .token_file)

TMUX_CMD=(tmux)
tmux_server=${NW_TMUX_SERVER:-$(printf '%s' "$resolved" | jq -r '.tmux_server // empty')}
if [[ -n "$tmux_server" ]]; then
  [[ "$tmux_server" =~ ^[A-Za-z0-9_.-]+$ ]] \
    || { echo "matrix-bus: invalid NW_TMUX_SERVER" >&2; exit 2; }
  TMUX_CMD+=(-L "$tmux_server")
fi

die() { echo "matrix-bus: $*" >&2; exit 1; }


usage() {
  awk 'NR>1 && /^#/ { sub(/^# ?/, ""); print; next } NR>1 { exit }' "$0"
  exit 1
}

host_prefix() {
  if [ -s "$CFG/host-prefix" ]; then tr -d ' \n' < "$CFG/host-prefix"; else hostname -s; fi
}

tmux_locate() {
  local pids="" p="$PPID" out
  local scope=(-a)
  if [[ -n "${NW_FLEET_PRIMARY_SESSION:-}" ]]; then
    scope=(-s -t "=$NW_FLEET_PRIMARY_SESSION")
  fi
  while [ -n "$p" ] && [ "$p" -gt 1 ] 2>/dev/null; do
    pids="$pids $p"
    p=$(ps -o ppid= -p "$p" 2>/dev/null | tr -d ' ')
  done
  out=$("${TMUX_CMD[@]}" list-panes "${scope[@]}" -F '#{pane_pid} #{session_name}:#{window_index}.#{pane_index} #{window_name}' 2>/dev/null \
    | while read -r pp loc win; do
        for ancestor in $pids; do
          [ "$ancestor" = "$pp" ] && echo "tmux=$loc win=$win"
        done
      done | head -1 || true)
  if [ -n "$out" ]; then echo "$out"; else echo "no-tmux"; fi
}

tmux_window() { tmux_locate | grep -oP '^tmux=[^:]+:\K[0-9]+' || true; }

cmd_handle() {
  local slug="${1:?usage: matrix-bus.sh handle <task-slug>}" window
  case "$slug" in */*) die "pass a bare task-slug; the host prefix is automatic" ;; esac
  window=$(tmux_window)
  if [ -n "$window" ]; then echo "$(host_prefix)/$slug-tmux$window"; else echo "$(host_prefix)/$slug"; fi
}

cmd_setup() {
  local handle="${1:?usage: matrix-bus.sh setup <handle>}"
  if [[ "$TRANSPORT" == "local" ]]; then
    mkdir -p "$CFG"
    chmod 700 "$CFG"
    echo "setup ok: handle=$handle transport=local"
    return
  fi
  mkdir -p "$CFG"
  if [ ! -s "$HDR" ] && [ -s "$CFG/agent.env" ]; then
    mkdir -p "$(dirname "$HDR")"
    (umask 077; awk -F= '$1=="MATRIX_ACCESS_TOKEN"{print "Authorization: Bearer " $2}' "$CFG/agent.env" > "$HDR")
  fi
  [ -s "$HDR" ] || die "Matrix credential file missing; configure matrix.token_file"
  chmod 600 "$HDR"
  echo "setup ok: handle=$handle"
}

case "${1:-}" in
  handle) shift; cmd_handle "$@" ;;
  tmux-id) shift; tmux_locate "$@" ;;
  setup) shift; cmd_setup "$@" ;;
  join|retire|members|heartbeat|registry-migrate|send|retry|pull|replay|unread|dispatch|watch|ack|delivery|revive|notify-claim|environment|brief)
    command="$1"; shift; exec python3 "$ADAPTER" "$command" "$@" ;;
  *) usage ;;
esac
