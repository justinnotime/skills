#!/usr/bin/env bash
# The statusline wrapper is fail-open: the hud part and the orc part run
# independently, a failing or missing part is skipped silently, and the
# exit code is always 0 (a status bar must never surface an error).
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
WRAPPER="$ROOT/scripts/claude-statusline-orc.sh"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

cat > "$tmp/hud" <<'EOF'
#!/usr/bin/env bash
cat >/dev/null
echo "HUDLINE"
EOF
cat > "$tmp/orc" <<'EOF'
#!/usr/bin/env bash
[ "$*" = "board --view summary" ] && echo "ORCLINE"
EOF
cat > "$tmp/hud-broken" <<'EOF'
#!/usr/bin/env bash
exit 3
EOF
chmod +x "$tmp/hud" "$tmp/orc" "$tmp/hud-broken"

out=$(echo '{}' | NW_HUD_CMD="$tmp/hud" NW_ORC_BIN="$tmp/orc" bash "$WRAPPER")
[ "$(printf '%s\n' "$out" | sed -n 1p)" = "HUDLINE" ] || {
    echo "FAIL the hud line must come first"; exit 1; }
printf '%s\n' "$out" | grep -q ORCLINE || {
    echo "FAIL the orc line is missing from combined output"; exit 1; }
echo "OK   both parts present, hud first"

out=$(echo '{}' | NW_HUD_CMD="$tmp/hud-broken" NW_ORC_BIN="$tmp/orc" bash "$WRAPPER")
printf '%s\n' "$out" | grep -q ORCLINE || {
    echo "FAIL the orc line must survive a failing hud"; exit 1; }
echo "OK   orc line survives a failing hud, exit stayed 0"

out=$(echo '{}' | NW_HUD_CMD="$tmp/hud" NW_ORC_BIN="$tmp/does-not-exist" bash "$WRAPPER")
printf '%s\n' "$out" | grep -q HUDLINE || {
    echo "FAIL the hud line must survive a missing orc"; exit 1; }
printf '%s\n' "$out" | grep -q ORCLINE && {
    echo "FAIL a missing orc must produce no orc line"; exit 1; }
echo "OK   hud line survives a missing orc, exit stayed 0"

cat > "$tmp/orc-echo" <<'EOF'
#!/usr/bin/env bash
echo "VERB:$*"
EOF
chmod +x "$tmp/orc-echo"
out=$(echo '{}' | NW_HUD_CMD="$tmp/hud" NW_ORC_BIN="$tmp/orc-echo" bash "$WRAPPER")
printf '%s\n' "$out" | grep -q "VERB:board --view summary" || {
    echo "FAIL default mode must call the summary view"; exit 1; }
out=$(echo '{}' | NW_HUD_CMD="$tmp/hud" NW_ORC_BIN="$tmp/orc-echo" \
    NW_ORC_STATUSLINE_FULL=1 bash "$WRAPPER")
printf '%s\n' "$out" | grep -q "VERB:board --view columns" || {
    echo "FAIL NW_ORC_STATUSLINE_FULL=1 must call the columns view"; exit 1; }
echo "OK   NW_ORC_STATUSLINE_FULL switches the view"

echo "OK   statusline wrapper contract holds"
