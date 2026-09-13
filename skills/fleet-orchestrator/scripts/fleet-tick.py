#!/usr/bin/env python3
"""Run the scheduler for every live fleet, then the machine-level checkout patrol.

Discovery is a tmux observation, not a second fleet registry: a fleet is a
session group that `orc fleet NAME start` bound to saved work. Each child keeps
the task engine's existing per-store lock; stopped fleets retain history without
being scheduled. The checkout patrol belongs to the machine, not to a fleet: it
writes a status file and log lines, never fleet tasks. Zero model calls.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
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
    selected = profile._base_environment(env)
    for key in ("TMUX", "TMUX_PANE", "ORC_SEAT_ID", "AGENT_BUS_SLOT"):
        selected.pop(key, None)
    selected["PYTHONDONTWRITEBYTECODE"] = "1"
    return selected


def machine_runtime(env: Mapping[str, str]) -> Path:
    if env.get("NOTES_RUNTIME_DIR"):
        return Path(cfg.expand(env["NOTES_RUNTIME_DIR"], env))
    return cfg.path(
        "runtime_dir", Path(env.get("XDG_STATE_HOME", str(cfg.home(env) / ".local/state")))
        / "fleet-orchestrator", env=env)


def machine_state_dir(env: Mapping[str, str]) -> Path:
    return cfg.path("paths.orchestrator_state",
                    machine_runtime(env) / "state/fleet-orchestrator", env=env)


def ledger_path(env: Mapping[str, str]) -> Path:
    explicit = env.get("DISPATCH_LEDGER_DB")
    if explicit:
        return Path(cfg.expand(explicit, env)).resolve()
    return cfg.path("paths.ledger", machine_state_dir(env) / "dispatch-ledger.sqlite3",
                    env=env).resolve()


def select_environment(name: str, base: Mapping[str, str]) -> dict[str, str]:
    """Read a complete store and pane scope without assigning tmux options."""
    return profile.command_env(name, base)


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
    name = env.get("NW_FLEET") or "standalone"
    for line in (result.stdout or "").splitlines():
        log(f"[{name}] {line}")
    for line in (result.stderr or "").splitlines():
        print(f"[{name}] {line}", file=sys.stderr, flush=True)
    return result.returncode


def checkout_findings(repo: Mapping[str, object], env: Mapping[str, str] | None = None) -> list[str]:
    """Paths that make a watched checkout unclean, each with its modification time."""
    root = Path(cfg.expand(str(repo["path"]), env or os.environ))
    if not root.exists():
        return ["MISSING CHECKOUT"]
    try:
        if repo.get("kind") == "bare-hub":
            out = subprocess.run(["git", "-C", str(root), "rev-parse", "--is-bare-repository"],
                                 text=True, capture_output=True, timeout=15)
            if out.returncode == 0 and out.stdout.strip() == "false":
                return ["NON-BARE"]
            if out.returncode:
                return [f"CHECK FAILED: git rev-parse exited {out.returncode}"]
            return []
        out = subprocess.run(["git", "-C", str(root), "status", "--porcelain",
                              "--ignored=no", "--untracked-files=all"],
                             text=True, capture_output=True, timeout=30)
        if out.returncode != 0:
            return [f"CHECK FAILED: git status exited {out.returncode}"]
    except (OSError, subprocess.TimeoutExpired):
        return ["CHECK FAILED: git inspection unavailable"]
    exempt = tuple(str(prefix) for prefix in (repo.get("exempt") or ()))
    findings = []
    for line in out.stdout.splitlines():
        if len(line) < 4:
            continue
        rel = line[3:].split(" -> ")[-1].strip().strip('"')
        if any(rel.startswith(prefix) for prefix in exempt):
            continue
        try:
            mtime = datetime.fromtimestamp((root / rel).stat().st_mtime, timezone.utc) \
                .isoformat(timespec="seconds")
        except OSError:
            mtime = "(gone)"
        findings.append(f"{line[:2]} {rel}\t{mtime}")
    return findings


def checkout_patrol(base: Mapping[str, str], dry_run: bool) -> int:
    """Machine-level check of the configured checkouts: a status file and log lines."""
    repositories = cfg.get("watched_repositories", [], env=base)
    if not repositories:
        return 0
    report: dict[str, dict[str, object]] = {}
    dirty = 0
    for repo in repositories:
        if not isinstance(repo, dict) or not repo.get("path"):
            log("FAIL checkout patrol: a watched_repositories entry has no path")
            return 1
        findings = checkout_findings(repo, base)
        report[str(repo["path"])] = {"kind": repo.get("kind", "checkout"), "findings": findings}
        if findings:
            dirty += 1
            shown = "; ".join(item.split("\t")[0] for item in findings[:5])
            more = " ..." if len(findings) > 5 else ""
            log(f"WARN checkout patrol: {repo['path']} is not clean"
                f" ({len(findings)} finding(s)): {shown}{more}")
    log(f"OK checkout patrol: {len(repositories) - dirty} clean, {dirty} not clean")
    if not dry_run:
        state = machine_state_dir(base)
        state.mkdir(parents=True, exist_ok=True)
        path = state / "checkout-patrol.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps({
            "schema": "checkout-patrol/v1",
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "repositories": report,
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)
    return 0


def run(env: Mapping[str, str], *, dry_run: bool = False) -> int:
    base = scheduler_environment(env)
    failures = 0
    if not profile.fleet_mode(base):
        # No fleet runtime is configured: one standalone store, scheduled as itself.
        log(f"{'DRY' if dry_run else 'RUN'} standalone store")
        try:
            failures += int(bool(run_tick(base, dry_run)))
        except (ValueError, OSError, sqlite3.Error, RuntimeError,
                subprocess.TimeoutExpired) as exc:
            log(f"FAIL standalone store: {exc}")
            failures += 1
        failures += checkout_patrol(base, dry_run)
        return int(bool(failures))
    names: list[str] = []
    try:
        names = sorted(profile.local_sessions(base))
    except (ValueError, OSError) as exc:
        log(f"FAIL local fleet discovery: {exc}")
        failures += 1

    seen: set[Path] = set()
    pending: list[tuple[str, dict[str, str]]] = []
    for name in names:
        try:
            selected = select_environment(name, base)
            if selected.get("NW_FLEET_PROFILE_PATH"):
                # Explicit profiles keep their configured scheduler.
                log(f"SKIP fleet {name}: explicit profile is not automatically scheduled")
                continue
            database = ledger_path(selected)
            if database in seen:
                log(f"SKIP fleet {name}: task store already scheduled")
                continue
            if not database.is_file():
                log(f"SKIP fleet {name}: no saved tasks")
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
    failures += checkout_patrol(base, dry_run)
    return int(bool(failures))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="inspect existing fleet work without sending or scheduling changes")
    args = parser.parse_args(argv)
    return run(os.environ, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
