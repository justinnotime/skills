#!/usr/bin/env bash
# Claude Code statusline command: claude-hud's own line(s) first, then the
# fleet work-graph digest (`orc board --view summary`). Configure in
# ~/.claude/settings.json as statusLine.command, with refreshInterval so the
# digest stays current while a session idles.
#
# Fail-open by design: a status bar must never show a stack trace or block
# the harness, so each part that cannot run (hud not installed, ledger
# absent on this machine, node missing) is skipped silently and the exit
# code is always 0. NW_HUD_CMD / NW_ORC_BIN override the two parts for
# tests and for machines with nonstandard layouts.
set -u

input=$(cat 2>/dev/null || true)

hud_cmd=${NW_HUD_CMD:-}
if [ -z "$hud_cmd" ]; then
    plugin_dir=$(ls -d "$HOME"/.claude/plugins/cache/claude-hud/claude-hud/*/ 2>/dev/null | sort -V | tail -1)
    node_bin=$(command -v node 2>/dev/null || true)
    if [ -z "$node_bin" ]; then
        node_bin=$(ls "$HOME"/.nvm/versions/node/*/bin/node 2>/dev/null | sort -V | tail -1)
    fi
    if [ -n "$plugin_dir" ] && [ -n "$node_bin" ] && [ -f "${plugin_dir}dist/index.js" ]; then
        hud_cmd="$node_bin ${plugin_dir}dist/index.js"
    fi
fi
if [ -n "$hud_cmd" ]; then
    # shellcheck disable=SC2086 - hud_cmd is deliberately word-split (bin + script)
    printf '%s' "$input" | timeout 5 $hud_cmd 2>/dev/null || true
fi

# NW_ORC_STATUSLINE_FULL=1 renders the kanban board (columns derived from
# the workflow state machines) instead of the compact 1-2 line digest;
# NW_ORC_STATUSLINE_ROWS bounds its height (default 3 rows per column).
orc_bin=${NW_ORC_BIN:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/orc}
if [ -x "$orc_bin" ]; then
    if [ "${NW_ORC_STATUSLINE_FULL:-0}" = "1" ]; then
        timeout 5 "$orc_bin" board --view columns --max-rows "${NW_ORC_STATUSLINE_ROWS:-3}" 2>/dev/null || true
    else
        timeout 5 "$orc_bin" board --view summary 2>/dev/null || true
    fi
fi
exit 0
