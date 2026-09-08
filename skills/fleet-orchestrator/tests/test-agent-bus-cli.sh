#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/bin"

cat > "$TMP/bin/python3" <<'EOF'
#!/usr/bin/env bash
if [[ "${2:-}" == environment ]]; then
  printf '{"config_directory":"%s","transport":"%s","token_file":"%s/auth.hdr"}\n' "$TEST_BUS_CFG" "${AGENT_BUS_TRANSPORT:-local}" "$TEST_BUS_CFG"
else
  printf '%s\n' "$*"
fi
EOF
chmod +x "$TMP/bin/python3"
export PATH="$TMP/bin:$PATH"
export TEST_BUS_CFG="$TMP/config"

# This test checks bus verb dispatch after fleet resolution. The Python stub
# deliberately cannot run the selector; real selection is covered by the
# session-fleet integration tests.
check() {
  expected="$1"
  shift
  actual=$(bash "$ROOT/scripts/matrix-bus.sh" --resolved "$@")
  case "$actual" in
    *"agent-bus-v3.py $expected"*) ;;
    *) printf 'expected %s dispatch, got: %s\n' "$expected" "$actual" >&2; exit 1 ;;
  esac
}

check join join h s claude pull host tmux
check members members
check registry-migrate registry-migrate
check send send a b subject body
check pull pull a
check unread unread a
check dispatch dispatch --once
check watch watch a
check ack ack a m ok
check delivery delivery a m
check retry retry a
check retire retire a

AGENT_BUS_TRANSPORT=local bash "$ROOT/scripts/matrix-bus.sh" --resolved setup host/test >/dev/null
[[ -d "$TEST_BUS_CFG" ]] || { echo "local setup did not create config directory" >&2; exit 1; }

echo "agent-bus simple CLI dispatch passed"

# tmux-id must not disappear under `set -o pipefail` when head closes the
# multi-pane producer after its first match.
mkdir -p "$TMP/tmux-bin"
cat > "$TMP/tmux-bin/tmux" <<'EOF'
#!/usr/bin/env bash
printf '%s\n' "$$ 0:4.0 opencode" "999999 0:8.0 codex"
EOF
chmod +x "$TMP/tmux-bin/tmux"
PATH="$TMP/tmux-bin:$PATH" bash "$ROOT/scripts/matrix-bus.sh" --resolved tmux-id >/dev/null
