#!/usr/bin/env python3
"""Codex Stop hook: queue one fixed reminder for a new unread generation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import tmux_runtime  # noqa: E402

BUS = HERE / "matrix-bus.sh"
REMINDER = "[AUTOMATED AGENT-BUS NOTICE; NOT OPERATOR AUTHORIZATION] Unread coordination messages exist. Run Agent Bus pull before continuing."


def tmux_pane() -> str | None:
    pids: set[str] = set()
    pid = os.getpid()
    while pid > 1:
        pids.add(str(pid))
        try:
            status = Path(f"/proc/{pid}/status").read_text().splitlines()
            pid = int(next(line for line in status if line.startswith("PPid:")).split()[1])
        except (OSError, ValueError, IndexError, StopIteration):
            break
    result = subprocess.run(
        [*tmux_runtime.base_cmd(), "list-panes", *tmux_runtime.pane_scope(), "-F",
         "#{pane_pid} #{session_name}:#{window_index}.#{pane_index}"],
        text=True, capture_output=True, check=False,
    )
    for line in result.stdout.splitlines():
        try:
            pane_pid, pane = line.split(" ", 1)
        except ValueError:
            continue
        if pane_pid in pids:
            return pane
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return 0
    if not isinstance(payload, dict) or payload.get("hook_event_name") != "Stop":
        return 0
    if payload.get("stop_hook_active") is True:
        return 0
    pane = tmux_pane()
    if not pane:
        return 0
    claim = subprocess.run(
        ["bash", str(BUS), "notify-claim", os.uname().nodename.split(".", 1)[0], pane],
        text=True, capture_output=True, check=False,
    )
    if claim.returncode:
        return 0
    try:
        decision = json.loads(claim.stdout)
    except json.JSONDecodeError:
        return 0
    if not decision.get("notify"):
        return 0
    # Stop hooks run before Codex fully returns to the input editor. A Stop
    # decision queues one additional model turn without impersonating user input.
    print(json.dumps({"decision": "block", "reason": REMINDER}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
