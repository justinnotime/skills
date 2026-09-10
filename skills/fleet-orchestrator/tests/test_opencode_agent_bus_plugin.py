"""Run the real TypeScript plugin with synthetic SDK, bus and terminal I/O."""

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import uuid


ROOT = Path(__file__).resolve().parents[1]


def test_opencode_agent_bus_behavior() -> None:
    node = shutil.which("node")
    assert node, "OpenCode plugin behavioral tests require Node >=22.13"
    result = subprocess.run(
        [node, "--experimental-vm-modules", "--test",
         "tests/opencode_agent_bus.test.mjs"],
        cwd=ROOT,
        env={key: value for key, value in os.environ.items()
             if key not in {"NODE_OPTIONS", "NODE_PATH"}
             and not key.startswith("OPENCODE_TEST_")},
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_opencode_tmux_c_locale(tmp_path: Path) -> None:
    node = shutil.which("node")
    tmux = shutil.which("tmux")
    assert node and tmux, "OpenCode native terminal test requires Node >=22.13 and tmux"
    # Explicit server, private socket root and empty config: no inherited live
    # terminal, hooks, bus configuration, or user tmux configuration is used.
    server = "opencode-test-" + uuid.uuid4().hex[:12]
    config = tmp_path / "config.json"
    config.write_text('{"schema":"fleet-runtime/v1"}\n')
    env = {
        "PATH": os.environ.get("PATH", os.defpath), "HOME": str(tmp_path),
        "TMUX_TMPDIR": str(tmp_path), "LC_ALL": "C", "LANG": "C",
    }
    base = [tmux, "-L", server, "-f", os.devnull]
    try:
        started = subprocess.run(
            [*base, "new-session", "-d", "-s", "plugin-test", "-P", "-F",
             "#{pane_id}", "sleep 60"],
            env=env, cwd=tmp_path, text=True, capture_output=True, timeout=10,
        )
        assert started.returncode == 0, started.stdout + started.stderr
        env.update({
            "OPENCODE_TEST_TMUX_SERVER": server,
            "OPENCODE_TEST_TMUX_PANE": started.stdout.strip(),
            "OPENCODE_TEST_TMUX_BIN": tmux,
            "OPENCODE_TEST_PYTHON": sys.executable,
            "FLEET_ORCHESTRATOR_CONFIG": str(config),
            "AGENT_BUS_TRANSPORT": "local",
            "AGENT_BUS_CFG": str(tmp_path / "bus"),
            "AGENT_BUS_DB": str(tmp_path / "bus" / "bus.sqlite3"),
            "NW_TMUX_SERVER": server,
            "NW_FLEET_PRIMARY_SESSION": "plugin-test",
            "TMUX_PANE": started.stdout.strip(),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        bus = [sys.executable, str(ROOT / "scripts/agent-bus-v3.py")]

        def run_bus(*args: str) -> str:
            result = subprocess.run([*bus, *args], cwd=tmp_path, env=env,
                                    text=True, capture_output=True, timeout=10)
            assert result.returncode == 0, result.stdout + result.stderr
            return result.stdout

        joined = json.loads(run_bus(
            "join", "host/legacy-opencode", "opencode:/project", "opencode",
            "watch", "host", "tmux=plugin-test:0.0 win=opencode",
        ))
        identity = joined["agent_id"]
        # A real local send seeds delivery bookkeeping as well as the inbox.
        leased = json.loads(run_bus("send", identity, identity, "leased", "previously presented"))["msg_id"]
        available = json.loads(run_bus("send", identity, identity, "available", "pending upgrade"))["msg_id"]
        presented = [json.loads(line) for line in run_bus("pull", identity, "--max", "1").splitlines()]
        assert presented[0]["msg_id"] == leased
        database = Path(env["AGENT_BUS_DB"])
        with sqlite3.connect(database) as conn:
            registration_before = conn.execute("SELECT * FROM identities").fetchall()
            lease_before = conn.execute("SELECT * FROM inbox WHERE msg_id=?", (leased,)).fetchone()
        before = database.read_bytes()
        members = [json.loads(line) for line in run_bus("members").splitlines()]
        assert len(members) == 1
        assert members[0]["slot"] == "opencode:/project"
        assert members[0]["agent_id"] == identity
        assert members[0]["terminal_presence"] == "present"
        assert database.read_bytes() == before, "members must not modify the database"
        env.update({"OPENCODE_TEST_AGENT_ID": identity, "OPENCODE_TEST_MESSAGE_ID": available})
        result = subprocess.run(
            [node, "--experimental-vm-modules", "--test",
             "--test-name-pattern=native tmux C locale|real members legacy upgrade",
             "tests/opencode_agent_bus.test.mjs"],
            cwd=ROOT, env=env, text=True, capture_output=True, timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "# pass 2" in result.stdout, result.stdout
        with sqlite3.connect(database) as conn:
            assert conn.execute("SELECT * FROM identities").fetchall() == registration_before
            assert conn.execute("SELECT * FROM inbox WHERE msg_id=?", (leased,)).fetchone() == lease_before
            assert conn.execute("SELECT state FROM inbox WHERE msg_id=?", (available,)).fetchone() == ("done",)
            assert conn.execute("SELECT processed_status FROM outbox_recipients WHERE msg_id=?",
                                (available,)).fetchone() == ("ok",)
    finally:
        subprocess.run([*base, "kill-server"], env=env, cwd=tmp_path,
                       text=True, capture_output=True, timeout=10)
