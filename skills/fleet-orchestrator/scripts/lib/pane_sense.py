"""Minimal tmux pane lookup for the fleet orchestrator. 0 LLM calls.

The work graph never derives task state from terminal prose. This module only
finds agent panes and detects the harness's explicit busy indicator so the
orchestrator does not queue a harmless inbox nudge into an active turn.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import tmux_runtime

TMUX_TIMEOUT_S = 15  # a wedged tmux server must not hang the cron tick

BUSY_MARKERS = ("esc to interrupt",)
BUSY_TAIL_LINES = 15  # busy markers only count near the prompt

AGENT_COMMANDS = ("claude", "codex", "opencode")


def detect_busy(captured: str) -> bool:
    """True when a busy marker sits within the trailing BUSY_TAIL_LINES."""
    lines = [ln.strip().lower() for ln in captured.splitlines() if ln.strip()]
    tail = lines[-BUSY_TAIL_LINES:]
    return any(marker in ln for ln in tail for marker in BUSY_MARKERS)


def tmux_out(args: list[str]) -> str:
    # The machine-local runtime config follows named-server migrations; the
    # environment override remains the staging/one-shot escape hatch.
    try:
        base = tmux_runtime.base_cmd()
    except tmux_runtime.TmuxRuntimeConfigError as exc:
        raise RuntimeError(str(exc)) from exc
    try:
        result = subprocess.run([*base, *args], text=True, capture_output=True,
                                check=False, timeout=TMUX_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"tmux command timed out after {TMUX_TIMEOUT_S}s") from None
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "tmux command failed")
    return result.stdout


def parse_agent_pane_rows(output: str) -> list[tuple[str, str]]:
    """[(pane_id, location)] of live panes running ANY agent harness,
    deduplicated by pane id.

    Grouped tmux sessions (clones named like `0-33`) make `list-panes -a`
    report the SAME pane once per session in the group — on the fleet box
    that turned 5 real agent panes into 90 rows. The pane id is the physical
    pane; first location wins."""
    panes: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in output.splitlines():
        try:
            pane_id, location, command, dead = line.split("\t")
        except ValueError:
            continue
        if Path(command).name not in AGENT_COMMANDS or dead == "1" or pane_id in seen:
            continue
        seen.add(pane_id)
        panes.append((pane_id, location))
    return panes


def agent_panes() -> list[tuple[str, str]]:
    """All live agent panes on the selected tmux server.

    An unreachable configured server is an observation failure, not an empty
    fleet. Callers that make lifecycle decisions must see the RuntimeError.
    """
    out = tmux_out(["list-panes", *tmux_runtime.pane_scope(), "-F",
                    "#{pane_id}\t#{session_name}:#{window_index}.#{pane_index}"
                    "\t#{pane_current_command}\t#{pane_dead}"])
    return parse_agent_pane_rows(out)


def window_titles() -> list[tuple[str, str]]:
    """[(window_index, window_name)] on the selected server.

    Window names are seat-authored labels. An unreachable server is UNKNOWN and
    raises; it must not be confused with a reachable server having no windows.
    """
    out = tmux_out(["list-windows", *tmux_runtime.window_scope(), "-F",
                    "#{window_index}\t#{window_name}"])
    rows = []
    for line in out.splitlines():
        try:
            idx, name = line.split("\t", 1)
        except ValueError:
            continue
        rows.append((idx, name))
    return rows


def pane_for_window(window: str,
                    panes: list[tuple[str, str]] | None = None) -> tuple[str, str] | None:
    """Resolve one agent pane in the canonical session. Grouped viewer sessions have independent window indexes and must not be used to resolve a target."""
    for pane_id, location in panes if panes is not None else agent_panes():
        try:
            session, rest = location.split(":", 1)
            win = rest.split(".", 1)[0]
        except (IndexError, ValueError):
            continue
        if session.startswith("tview-"):
            continue
        if win == window:
            return pane_id, location
    return None


def capture(pane_id: str) -> str:
    return tmux_out(["capture-pane", "-p", "-t", pane_id, "-S",
                     f"-{BUSY_TAIL_LINES}"])
