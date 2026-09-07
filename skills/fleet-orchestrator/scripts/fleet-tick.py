#!/usr/bin/env python3
"""Run the existing scheduler for the default and live local fleets.

Discovery is a tmux observation, not a second fleet registry. Each child keeps
the task engine's existing per-store lock; stopped local fleets retain history
without being scheduled. This command makes zero model calls.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Mapping

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import runtime_config as cfg

SPEC = importlib.util.spec_from_file_location("fleet_tick_profile", HERE / "lib/fleet-profile.py")
profile = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(profile)


def log(message: str) -> None:
    print(message, flush=True)


def scheduler_environment(env: Mapping[str, str]) -> dict[str, str]:
    """The scheduler is headless; terminal selection cannot redirect its work."""
    selected = profile._default_environment(env)
    for key in ("TMUX", "TMUX_PANE", "ORC_SEAT_ID", "AGENT_BUS_SLOT"):
        selected.pop(key, None)
    selected["PYTHONDONTWRITEBYTECODE"] = "1"
    return selected


def ledger_path(env: Mapping[str, str]) -> Path:
    explicit = env.get("DISPATCH_LEDGER_DB")
    if explicit:
        return Path(cfg.expand(explicit, env)).resolve()
    runtime = (Path(cfg.expand(env["NOTES_RUNTIME_DIR"], env)) if env.get("NOTES_RUNTIME_DIR")
               else cfg.path(
                   "runtime_dir", Path(env.get("XDG_STATE_HOME", str(cfg.home(env) / ".local/state")))
                   / "fleet-orchestrator", env=env))
    state = cfg.path("paths.orchestrator_state", runtime / "state/fleet-orchestrator", env=env)
    return cfg.path("paths.ledger", state / "dispatch-ledger.sqlite3", env=env).resolve()


def select_environment(name: str, base: Mapping[str, str]) -> dict[str, str]:
    """Read a complete store and pane scope without assigning tmux options."""
    if profile._canonical_name(name, base) == "default":
        selected = profile.command_env("default", base)
        target = profile._terminal_target("default", base)
        selected["NW_FLEET_PRIMARY_SESSION"] = target["primary_session"]
        return selected
    selected = dict(base)
    resolved = profile.resolve(name, base)
    for key in profile.PROFILE_ENV_KEYS:
        selected.pop(key, None)
    selected.update(resolved)
    return selected


def ensure_local_sender(env: Mapping[str, str]) -> None:
    """Create only this scheduler's local sender, without messaging peers."""
    transport = env.get("AGENT_BUS_TRANSPORT") or cfg.get("bus.transport", "local", env=env)
    if transport != "local":
        return
    handle = cfg.get("authority.service_handle", "fleet-orchestrator", env=env)
    if not isinstance(handle, str) or not handle.strip():
        raise ValueError("authority.service_handle must be a nonempty identity")
    host = profile.local_hostname()
    config_home = Path(env.get("XDG_CONFIG_HOME", str(cfg.home(env) / ".config")))
    bus_cfg = (env.get("AGENT_BUS_CFG") or env.get("MATRIX_BUS_CFG")
               or cfg.path("bus.config_directory", config_home / "fleet-orchestrator/bus", env=env))
    database = (Path(env["AGENT_BUS_DB"]) if env.get("AGENT_BUS_DB")
                else cfg.path("bus.database", Path(bus_cfg) / "agent-bus-v3.sqlite3", env=env))
    if database.is_file():
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            conn.row_factory = sqlite3.Row
            existing = conn.execute(
                "SELECT * FROM identities WHERE slot=? OR handle=?", (handle, handle),
            ).fetchall()
        if existing:
            if (len(existing) != 1 or existing[0]["slot"] != handle
                    or existing[0]["handle"] != handle
                    or existing[0]["harness"] != "cron"
                    or existing[0]["mode"] != "pull"
                    or existing[0]["host"] != host
                    or existing[0]["pane_id"]):
                raise ValueError("scheduler identity conflicts with an existing local agent")
            if existing[0]["status"] == "active":
                # The send command renews its sender's lease when needed.
                return
    result = subprocess.run([
        sys.executable, str(HERE / "agent-bus-v3.py"), "join",
        handle, handle, "cron", "pull", host, "headless=cron service=fleet-orchestrator",
    ], env=dict(env), text=True, capture_output=True, check=False, timeout=45)
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "could not prepare the local scheduler identity")


def run_tick(env: Mapping[str, str], dry_run: bool) -> int:
    if not dry_run:
        ensure_local_sender(env)
    command = [sys.executable, str(HERE / "fleet-orchestrator.py"), "tick"]
    if dry_run:
        command.append("--dry-run")
    result = subprocess.run(command, env=dict(env), text=True, capture_output=True, check=False)
    name = env.get("NW_FLEET") or "default"
    for line in (result.stdout or "").splitlines():
        log(f"[{name}] {line}")
    for line in (result.stderr or "").splitlines():
        print(f"[{name}] {line}", file=sys.stderr, flush=True)
    return result.returncode


def run(env: Mapping[str, str], *, dry_run: bool = False) -> int:
    base = scheduler_environment(env)
    failures = 0
    names = ["default"]
    try:
        default_alias = profile.default_name(base)
        primary = cfg.get("tmux.primary_session", "0", env=base)
        names.extend(name for name in profile.local_sessions(base)
                     if name not in {"default", default_alias, primary})
    except (ValueError, OSError) as exc:
        # The configured default still runs when native tmux discovery fails.
        log(f"FAIL local fleet discovery: {exc}")
        failures += 1

    seen: set[Path] = set()
    pending: list[tuple[str, dict[str, str]]] = []
    for name in names:
        try:
            selected = select_environment(name, base)
            database = ledger_path(selected)
            if database in seen:
                log(f"SKIP fleet {name}: task store already scheduled")
                continue
            if name != "default":
                # Explicit legacy profiles keep their configured scheduler.
                # Discovery never adds another scheduler for those profiles.
                if selected.get("NW_FLEET_PROFILE_PATH"):
                    log(f"SKIP fleet {name}: explicit profile is not automatically scheduled")
                    continue
                if not database.is_file():
                    log(f"SKIP fleet {name}: no saved tasks")
                    continue
                if not dry_run:
                    profile.bind_local_session(name, base)
            elif dry_run and not database.is_file():
                log("SKIP default fleet: no saved tasks to inspect")
                continue
            seen.add(database)
            pending.append((name, selected))
        except (ValueError, OSError, sqlite3.Error, RuntimeError,
                subprocess.TimeoutExpired) as exc:
            log(f"FAIL fleet {name}: {exc}")
            failures += 1
    # Network checks in one fleet must not delay another fleet's local work.
    # The existing task-store locks remain the only mutation locks.
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {}
        for name, selected in pending:
            log(f"{'DRY' if dry_run else 'RUN'} fleet {name}")
            futures[pool.submit(run_tick, selected, dry_run)] = name
        for future in as_completed(futures):
            name = futures[future]
            try:
                code = future.result()
                if code:
                    log(f"FAIL fleet {name}: scheduler exited {code}")
                    failures += 1
            except (ValueError, OSError, sqlite3.Error, RuntimeError,
                    subprocess.TimeoutExpired) as exc:
                log(f"FAIL fleet {name}: {exc}")
                failures += 1
    return int(bool(failures))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="inspect existing fleet work without sending or scheduling changes")
    args = parser.parse_args(argv)
    return run(os.environ, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
