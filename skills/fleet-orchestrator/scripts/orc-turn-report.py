#!/usr/bin/env python3
"""Record configured harness turn boundaries without blocking the harness."""


from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))

EVENT_KINDS = {"UserPromptSubmit": "start", "Stop": "end"}


def _enrolled(seat: str, value) -> bool:
    if value is None:
        return False
    path = Path(value)
    try:
        seats = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(seats, list) and seat in seats


def main() -> int:
    if os.environ.get("NW_TURN_REPORT_OFF", "").strip() == "1":
        return 0
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", type=Path)
    ap.add_argument("--fleet")
    ap.add_argument("--resolved", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--kind", choices=("start", "end"), default="")
    ap.add_argument("--harness", default="")
    args, _ = ap.parse_known_args()
    if not (os.environ.get("ORC_SEAT_ID", "").strip()
            or os.environ.get("TMUX_PANE", "").strip()):
        return 0
    if args.config:
        os.environ["FLEET_ORCHESTRATOR_CONFIG"] = str(args.config.resolve())
    if not args.resolved:
        # Direct Python hooks and installed launchers use the bus's selector.
        # The one-shot argument, not an inherited environment flag, ends re-entry.
        selection = ["--fleet", args.fleet] if args.fleet is not None else []
        subprocess.run([
            sys.executable, "-B", str(HERE / "lib/fleet-profile.py"),
            "exec-current", *selection, "--", sys.executable, "-B",
            str(HERE / "orc-turn-report.py"), "--resolved", *sys.argv[1:],
        ], timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return 0

    import runtime_config as cfg

    enabled = cfg.get("turn_report.enabled")
    if enabled is not None and enabled is not True:
        return 0
    kind = args.kind
    if not kind and not sys.stdin.isatty():
        try:
            payload = json.load(sys.stdin)
            kind = EVENT_KINDS.get(payload.get("hook_event_name", ""), "")
            if payload.get("stop_hook_active") is True:
                return 0
        except (json.JSONDecodeError, OSError, AttributeError):
            return 0
    if not kind:
        return 0
    import workplane as wp

    pane = os.environ.get("TMUX_PANE", "").strip().lstrip("%")
    seat = wp.caller_seat_id()
    if not seat:
        return 0
    if enabled is True:
        if wp.agent_bus_identity_active(seat) is not True:
            return 0
    elif not _enrolled(seat, os.environ.get("NW_TURN_CANARY_FILE")
                       or cfg.path("turn_report.seats_file")):
        return 0
    conn = wp.connect_writable(timeout=2)
    try:
        with conn:
            wp.turn_record(conn, seat, kind, pane=pane, harness=args.harness)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
