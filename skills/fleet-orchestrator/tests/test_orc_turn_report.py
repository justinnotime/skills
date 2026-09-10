"""Run turn reporting with synthetic HOME, registrations and task stores only."""

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/orc-turn-report.py"


@pytest.fixture
def reporting(tmp_path):
    database = tmp_path / "bus.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE identities (agent_id, status, lease_until_ms, host, pane_id)")
        conn.execute("INSERT INTO identities VALUES (?, ?, ?, ?, ?)",
                     ("current", "active", int(time.time() * 1000) + 60000,
                      socket.gethostname(), "%9"))
    config = tmp_path / "runtime.json"
    value = {"bus": {"database": str(database)},
             "paths": {"ledger": str(tmp_path / "tasks.sqlite3")}}
    # No live tmux, inherited selections, credentials, or private config.
    env = {"HOME": str(tmp_path), "PATH": os.environ["PATH"],
           "TMUX_BIN": "/bin/false", "TMUX_PANE": "%9",
           "PYTHONDONTWRITEBYTECODE": "1"}

    def run(policy=None, payload=None, **overrides):
        value["turn_report"] = policy or {}
        config.write_text(json.dumps(value))
        return subprocess.run(
            [sys.executable, "-B", str(SCRIPT), "--config", str(config), "--harness", "codex"],
            input=json.dumps(payload if payload is not None else {"hook_event_name": "Stop"}),
            env={**env, **overrides}, text=True, capture_output=True, timeout=10, check=False)

    return run, tmp_path


@pytest.mark.parametrize("policy", [{}, {"enabled": False}, {"enabled": "true"}, {"enabled": 1}])
def test_reporting_is_explicit_opt_in(reporting, policy):
    run, root = reporting
    result = run(policy)
    assert result.returncode == 0 and result.stdout == result.stderr == ""
    assert not (root / "tasks.sqlite3").exists()


def test_enabled_records_active_identity_without_saved_enrollment(reporting):
    run, root = reporting
    policy = {"enabled": True, "seats_file": str(root / "obsolete-list.json")}
    for event in ("UserPromptSubmit", "Stop"):
        result = run(policy, {"hook_event_name": event})
        assert result.returncode == 0 and result.stdout == result.stderr == ""
    with sqlite3.connect(root / "tasks.sqlite3") as conn:
        assert conn.execute("SELECT seat, kind, harness, pane, starts, ends FROM seat_presence").fetchall() == [
            ("current", "end", "codex", "9", 1, 1)]


def test_direct_python_hook_uses_default_config_without_environment_prefix(reporting):
    run, root = reporting
    assert run({"enabled": True}).returncode == 0
    default = root / ".config/fleet-orchestrator/config.json"
    default.parent.mkdir(parents=True)
    default.write_text((root / "runtime.json").read_text())
    result = subprocess.run(
        [sys.executable, "-B", str(SCRIPT), "--harness", "codex"],
        input='{"hook_event_name":"Stop"}', text=True, capture_output=True, timeout=10, check=False,
        env={"HOME": str(root), "PATH": os.environ["PATH"], "TMUX_BIN": "/bin/false", "TMUX_PANE": "%9"})
    assert result.returncode == 0 and result.stdout == result.stderr == ""
    with sqlite3.connect(root / "tasks.sqlite3") as conn:
        assert conn.execute("SELECT seat, ends FROM seat_presence").fetchall() == [("current", 2)]


@pytest.mark.parametrize("status,expiry,explicit", [
    ("retired", 9999999999999, "current"), ("active", 0, "current"),
    ("active", 9999999999999, "not-registered"),
])
def test_enabled_does_not_record_inactive_or_unknown_explicit_identity(reporting, status, expiry, explicit):
    run, root = reporting
    with sqlite3.connect(root / "bus.sqlite3") as conn:
        conn.execute("UPDATE identities SET status=?, lease_until_ms=?", (status, expiry))
    assert run({"enabled": True}, ORC_SEAT_ID=explicit).returncode == 0
    assert not (root / "tasks.sqlite3").exists()


@pytest.mark.parametrize("state", ["missing", "ambiguous"])
def test_enabled_requires_an_unambiguous_registration_source(reporting, state):
    run, root = reporting
    if state == "missing":
        (root / "bus.sqlite3").unlink()
    else:
        with sqlite3.connect(root / "bus.sqlite3") as conn:
            conn.execute("INSERT INTO identities SELECT 'duplicate', status, lease_until_ms, host, pane_id FROM identities")
    assert run({"enabled": True}).returncode == 0
    assert not (root / "tasks.sqlite3").exists()


def test_unresolvable_fleet_never_records_in_default_store(reporting):
    run, root = reporting
    assert run({"enabled": True}, NW_FLEET="missing-fleet").returncode == 0
    assert not (root / "tasks.sqlite3").exists()


@pytest.mark.parametrize("override", [False, True])
def test_existing_enrollment_and_environment_override_remain_supported(reporting, override):
    run, root = reporting
    seats = root / "seats.json"
    seats.write_text('["old-identity"]')
    policy = {"seats_file": str(seats)}
    env = {"NW_TURN_CANARY_FILE": str(seats)} if override else {}
    if override:
        policy = {"seats_file": str(root / "absent.json")}
    assert run(policy, **env).returncode == 0
    assert not (root / "tasks.sqlite3").exists()
    seats.write_text('["current"]')
    assert run({**policy, "enabled": False}, **env).returncode == 0
    assert not (root / "tasks.sqlite3").exists()
    assert run(policy, **env).returncode == 0
    with sqlite3.connect(root / "tasks.sqlite3") as conn:
        assert conn.execute("SELECT seat, ends FROM seat_presence").fetchall() == [("current", 1)]


@pytest.mark.parametrize("payload", [[], {"hook_event_name": "PreToolUse"},
                                     {"hook_event_name": "Stop", "stop_hook_active": True}])
def test_unrelated_or_recursive_native_event_does_not_record(reporting, payload):
    run, root = reporting
    assert run({"enabled": True}, payload).returncode == 0
    assert not (root / "tasks.sqlite3").exists()


def test_off_override_prevents_reporting(reporting):
    run, root = reporting
    assert run({"enabled": True}, NW_TURN_REPORT_OFF="1").returncode == 0
    assert not (root / "tasks.sqlite3").exists()
