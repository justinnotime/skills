#!/usr/bin/env bash
# Regression tests for fail-closed Agent Bus harness/mode registration.
# Enter after fleet selection: this fixture supplies a synthetic terminal
# location. Real selector and onboarding isolation are covered by
# test_session_fleets.py; do not construct a second partial fleet runtime here.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
# the wake-channel verification reads the harness config under HOME only
export CODEX_HOME="$TMP/home/.codex"
export XDG_CONFIG_HOME="$TMP/home/.config"

make_fixture() {
  rm -rf "$TMP/repo" "$TMP/cfg" "$TMP/home" "$TMP/join.log"
  mkdir -p "$TMP/repo/scripts" "$TMP/cfg" "$TMP/home" "$TMP/home/.codex"
  cp "$ROOT/scripts/agent-boot.sh" "$TMP/repo/scripts/agent-boot.sh"
  cp "$ROOT/scripts/agent-bus-v3.py" "$TMP/repo/scripts/agent-bus-v3.py"
  mkdir -p "$TMP/repo/scripts/lib"
  cp "$ROOT/scripts/lib/runtime_config.py" "$TMP/repo/scripts/lib/runtime_config.py"
  cp "$ROOT/scripts/lib/tmux_runtime.py" "$TMP/repo/scripts/lib/tmux_runtime.py"
  cp "$ROOT/scripts/lib/runtime_paths.py" "$TMP/repo/scripts/lib/runtime_paths.py"
  printf 'test\n' > "$TMP/cfg/agent.env"
  printf 'host-b\n' > "$TMP/cfg/host-prefix"
  # a Codex seat's wake channel: the Stop hook entry the installer writes
  printf '[[hooks.Stop]]\n# agent-bus-stop-hook\n' > "$TMP/home/.codex/config.toml"

  cat > "$TMP/repo/scripts/matrix-bus.sh" <<'EOF'
#!/usr/bin/env bash
case "$1" in
  tmux-id) printf '%s\n' "${MOCK_TMUX:-no-tmux}" ;;
  handle) printf 'host-b/%s-tmux20\n' "$2" ;;
  setup) printf 'setup ok\n' ;;
  join)
    printf '%s\n' "$*" > "$JOIN_LOG"
    reported_harness=${MOCK_RETURN_HARNESS:-$4}
    printf '{"schema":"agent-bus/join-result/v3","agent_id":"id-test","handle":"%s","slot":"%s","generation":1,"status":"active","harness":"%s","mode":"%s","host":"%s","tmux":"%s","event_id":"event-test"}\n' \
      "$2" "$3" "$reported_harness" "$5" "$6" "$7"
    ;;
  pull) : ;;
  *) printf 'unexpected bus verb: %s\n' "$1" >&2; exit 2 ;;
esac
EOF
  chmod +x "$TMP/repo/scripts/matrix-bus.sh"

  cat > "$TMP/repo/scripts/agent-bus-restart-guard.py" <<'EOF'
#!/usr/bin/env python3
raise SystemExit(0)
EOF

}

fail() { printf 'FAIL: %s\n' "$1" >&2; exit 1; }

run_boot() {
  set +e
  output=$(HOME="$TMP/home" MATRIX_BUS_CFG="$TMP/cfg" JOIN_LOG="$TMP/join.log" \
    MOCK_TMUX="$1" AGENT_BUS_HARNESS="${2:-}" AGENT_BUS_MODE="${3:-}" \
    MOCK_RETURN_HARNESS="${4:-}" AGENT_BUS_TRANSPORT="${5:-matrix}" \
    NW_FLEET="${6:-}" NW_FLEET_PROFILE_APPLIED="${6:-}" \
    bash "$TMP/repo/scripts/agent-boot.sh" --resolved demo 2>&1)
  rc=$?
  set -e
}

make_fixture
run_boot 'tmux=0:20.0 win=codex' '' ''
[ "$rc" = 0 ] || fail "Codex auto-detect failed: $output"
grep -q 'join host-b/demo-tmux20 host-b/demo codex pull ' "$TMP/join.log" \
  || fail "Codex registration metadata is wrong"
printf '%s' "$output" | grep -q 'harness=codex, mode=pull, tmux=0:20.0' \
  || fail "Codex identity verification line missing"

# A local named fleet must not require Matrix credentials.
make_fixture
rm "$TMP/cfg/agent.env"
run_boot 'tmux=0:20.0 win=codex' '' '' '' local alpha
[ "$rc" = 0 ] || fail "local transport required Matrix credentials: $output"
printf '%s' "$output" | grep -q 'no Matrix room or credential required' \
  || fail "local transport prerequisite was not explained"

# The default local transport needs no named profile or credentials.
make_fixture
rm "$TMP/cfg/agent.env"
run_boot 'tmux=0:20.0 win=codex' '' '' '' local
[ "$rc" = 0 ] || fail "default local transport failed: $output"

# The unchanged default transport still fails closed without credentials.
make_fixture
rm "$TMP/cfg/agent.env"
run_boot 'tmux=0:20.0 win=codex' '' '' '' matrix
[ "$rc" != 0 ] || fail "Matrix transport accepted missing credentials"
[ ! -e "$TMP/join.log" ] || fail "missing Matrix credentials reached join"

make_fixture
run_boot 'tmux=0:21.0 win=claude' '' ''
[ "$rc" = 0 ] || fail "Claude auto-detect failed: $output"
grep -q 'join host-b/demo-tmux20 host-b/demo claude watch ' "$TMP/join.log" \
  || fail "Claude registration metadata is wrong"

make_fixture
run_boot 'tmux=0:22.0 win=misc' '' ''
[ "$rc" != 0 ] || fail "unknown harness must fail closed"
[ ! -e "$TMP/join.log" ] || fail "unknown harness reached join"
printf '%s' "$output" | grep -q 'AGENT_BUS_HARNESS=codex AGENT_BUS_MODE=pull' \
  || fail "unknown-harness remedy missing"

make_fixture
run_boot 'tmux=0:22.0 win=codex-old' '' ''
[ "$rc" != 0 ] || fail "near-match window name must not auto-detect Codex"
[ ! -e "$TMP/join.log" ] || fail "near-match window name reached join"

make_fixture
run_boot 'tmux=0:22.0 win=renamed' codex pull
[ "$rc" = 0 ] || fail "explicit Codex metadata in a renamed window failed: $output"
grep -q 'join host-b/demo-tmux20 host-b/demo codex pull ' "$TMP/join.log" \
  || fail "explicit Codex registration metadata is wrong"

make_fixture
run_boot 'tmux=0:20.0 win=codex' codex watch
[ "$rc" != 0 ] || fail "Codex watch mode must be rejected"
[ ! -e "$TMP/join.log" ] || fail "invalid Codex mode reached join"

make_fixture
run_boot 'tmux=0:20.0 win=codex' unknown pull
[ "$rc" != 0 ] || fail "explicit unknown harness must be rejected"
[ ! -e "$TMP/join.log" ] || fail "explicit unknown harness reached join"

make_fixture
run_boot 'no-tmux' codex pull
[ "$rc" != 0 ] || fail "Codex without a concrete tmux pane must be rejected"
[ ! -e "$TMP/join.log" ] || fail "pane-less Codex registration reached join"
printf '%s' "$output" | grep -q 'requires a concrete tmux pane' \
  || fail "pane-less Codex remedy missing"

make_fixture
run_boot 'tmux=0:20.0 win=codex' codex pull claude
[ "$rc" != 0 ] || fail "persisted identity mismatch must fail verification"

printf '%s' "$output" | grep -q 'persisted identity does not match' \
  || fail "persisted identity mismatch was not explained"

# Boot must not widen skill publication. Rollout-control owns installation.
make_fixture
run_boot 'tmux=0:20.0 win=codex' codex pull
[ "$rc" -eq 0 ] || fail "boot failed without skill publication: $output"
! grep -q 'link-global-skills' "$TMP/repo/scripts/agent-boot.sh" \
  || fail "boot still references the retired skill publisher"
for skill_dir in .claude/skills .codex/skills .opencode/skills .dsh-work/skills; do
  [ ! -e "$TMP/home/$skill_dir" ] \
    || fail "boot wrote the retired skill directory $skill_dir"
done

# The Agent Bus database is the only pane identity store. Boot must not create
# a second runtime store even when the harness exports TMUX_PANE.
make_fixture
set +e
output=$(HOME="$TMP/home" MATRIX_BUS_CFG="$TMP/cfg" JOIN_LOG="$TMP/join.log" \
  NOTES_RUNTIME_DIR="$TMP/rt" MOCK_TMUX='tmux=0:20.0 win=codex' \
  TMUX_PANE='%9' bash "$TMP/repo/scripts/agent-boot.sh" --resolved demo 2>&1)
rc=$?
set -e
[ "$rc" -eq 0 ] || fail "boot with TMUX_PANE failed: $output"
[ ! -e "$TMP/rt/seat-identity" ] \
  || fail "boot created the retired pane identity store"

make_fixture
rm "$TMP/home/.codex/config.toml"
run_boot 'tmux=0:20.0 win=codex' '' ''
[ "$rc" != 0 ] || fail "Codex without the Stop hook entry must fail closed"
[ ! -e "$TMP/join.log" ] || fail "hook-less Codex reached join"
printf '%s' "$output" | grep -q 'install-agent-bus-pull-notify.py' \
  || fail "hook-less Codex remedy missing"
[ ! -e "$TMP/home/.codex/config.toml" ] || fail "boot installed the hook itself"
make_fixture
run_boot 'tmux=0:23.0 win=opencode' '' ''
[ "$rc" != 0 ] || fail "OpenCode without the agent-bus plugin must fail closed"
[ ! -e "$TMP/join.log" ] || fail "plugin-less OpenCode reached join"
printf '%s' "$output" | grep -q 'opencode-agent-bus-plugin' \
  || fail "plugin-less OpenCode remedy missing"
make_fixture
mkdir -p "$TMP/home/.config/opencode/plugins"
printf 'plugin\n' > "$TMP/home/.config/opencode/plugins/agent-bus.ts"
run_boot 'tmux=0:23.0 win=opencode' '' ''
[ "$rc" = 0 ] || fail "OpenCode with the plugin file failed: $output"
grep -q 'join host-b/demo-tmux20 host-b/demo opencode watch ' "$TMP/join.log" \
  || fail "OpenCode registration metadata is wrong"
# Claude Code arms its watcher from inside the session: boot cannot verify it
# and must not refuse; the digest tells the seat to arm before anything else.
make_fixture
run_boot 'tmux=0:21.0 win=claude' '' ''
[ "$rc" = 0 ] || fail "Claude boot must still succeed: $output"
printf '%s' "$output" | grep -q 'cannot receive notifications' \
  || fail "Claude arming instruction lost its deafness warning"

echo "agent-boot harness guard: all checks passed"
