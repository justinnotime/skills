#!/usr/bin/env python3
"""Named-fleet profile tests: complete isolation or an explicit failure."""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "lib" / "fleet-profile.py"
SPEC = importlib.util.spec_from_file_location("fleet_profile", SCRIPT)
fleet_profile = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(fleet_profile)


class FleetProfileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.profiles = self.base / "profiles"
        self.runtime = self.base / "runtime"
        self.matrix = self.base / "matrix"
        self.profiles.mkdir()
        self.env = {
            "HOME": str(self.base / "home"),
            "NW_FLEET_PROFILE_DIR": str(self.profiles),
            "NW_FLEET_RUNTIME_ROOT": str(self.runtime),
            "NW_FLEET_MATRIX_CFG_ROOT": str(self.matrix),
        }
        self.fake_tmux_state = self.base / "fake-tmux-state"
        self.fake_tmux_log = self.base / "fake-tmux.log"
        self.fake_tmux = self.base / "fake-tmux"
        self.fake_tmux.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
state=${FAKE_TMUX_STATE:?}
log=${FAKE_TMUX_LOG:?}
[[ ${1:-} == -L && -n ${2:-} ]] || exit 90
server=$2
shift 2
command=${1:-}
shift || true
server_dir=$state/$server
printf '%s|%s|%s|%s|%s\\n' "$server" "$command $*" "${NW_FLEET-}" \
  "${NW_FLEET_PROFILE_APPLIED-}" "${TMUX-unset}" >>"$log"
case "$command" in
  list-sessions)
    [[ -d "$server_dir/sessions" ]]
    ;;
  new-session)
    session=
    while (( $# )); do
      case "$1" in
        -s) session=$2; shift 2 ;;
        *) shift ;;
      esac
    done
    [[ -n "$session" ]] || exit 91
    mkdir -p "$server_dir/sessions" "$server_dir/environment"
    : >"$server_dir/sessions/$session"
    ;;
  has-session)
    [[ ${1:-} == -t && -n ${2:-} ]] || exit 92
    session=${2#=}
    [[ -f "$server_dir/sessions/$session" ]]
    ;;
  show-environment)
    key=${2:-}
    [[ -f "$server_dir/environment/$key" ]] || exit 1
    printf '%s=%s\\n' "$key" "$(<"$server_dir/environment/$key")"
    ;;
  set-environment)
    mode=${1:-}
    key=${2:-}
    mkdir -p "$server_dir/environment"
    if [[ "$mode" == -gu ]]; then
      rm -f "$server_dir/environment/$key"
    else
      [[ "$mode" == -g && -n ${3:-} ]] || exit 93
      printf '%s' "$3" >"$server_dir/environment/$key"
    fi
    ;;
  *) exit 94 ;;
esac
""",
            encoding="utf-8",
        )
        self.fake_tmux.chmod(0o700)
        self.tmux_env = {
            "TMUX_BIN": str(self.fake_tmux),
            "FAKE_TMUX_STATE": str(self.fake_tmux_state),
            "FAKE_TMUX_LOG": str(self.fake_tmux_log),
        }
        self.addCleanup(self.tmp.cleanup)

    def write_profile(self, name="alpha", **overrides):
        value = {
            "schema": 1,
            "name": name,
            "tmux_server": f"nw-{name}",
            "primary_session": "0",
            "matrix_homeserver": "https://matrix.example.test",
            "matrix_room": f"!messages-{name}:example.test",
            "matrix_registry_room": f"!registry-{name}:example.test",
        }
        value.update(overrides)
        path = self.profiles / f"{name}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def write_local_profile(self, name="alpha", **overrides):
        value = {
            "schema": 2,
            "name": name,
            "tmux_server": f"nw-{name}",
            "primary_session": "0",
            "agent_bus_transport": "local",
            "local_host": fleet_profile.local_hostname(),
        }
        value.update(overrides)
        path = self.profiles / f"{name}.json"
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_resolve_selects_every_isolation_resource_together(self):
        path = self.write_profile()
        values = fleet_profile.resolve("alpha", self.env)
        self.assertEqual(values["NW_FLEET"], "alpha")
        self.assertEqual(values["AGENT_BUS_TRANSPORT"], "matrix")
        self.assertEqual(values["NW_TMUX_SERVER"], "nw-alpha")
        self.assertEqual(values["NW_FLEET_PRIMARY_SESSION"], "0")
        self.assertEqual(values["NOTES_RUNTIME_DIR"], str(self.runtime / "alpha"))
        self.assertEqual(values["MATRIX_BUS_CFG"], str(self.matrix / "alpha"))
        self.assertEqual(
            values["DISPATCH_LEDGER_DB"],
            str(self.matrix / "alpha" / "dispatch-ledger.sqlite3"),
        )
        self.assertEqual(
            values["AGENT_BUS_DB"],
            str(self.matrix / "alpha" / "agent-bus-v3.sqlite3"),
        )
        self.assertEqual(values["NW_FLEET_PROFILE_PATH"], str(path))

    def test_local_resolve_uses_transport_neutral_state_and_no_matrix_values(self):
        path = self.write_local_profile()
        values = fleet_profile.resolve("alpha", self.env)
        state = self.runtime / "alpha" / "state"
        self.assertEqual(values["NW_FLEET"], "alpha")
        self.assertEqual(values["AGENT_BUS_TRANSPORT"], "local")
        self.assertEqual(values["AGENT_BUS_CFG"], str(state / "agent-bus"))
        self.assertEqual(
            values["AGENT_BUS_DB"],
            str(state / "agent-bus" / "agent-bus-v3.sqlite3"),
        )
        self.assertEqual(
            values["DISPATCH_LEDGER_DB"],
            str(state / "fleet-orchestrator" / "dispatch-ledger.sqlite3"),
        )
        self.assertEqual(values["NW_FLEET_PROFILE_PATH"], str(path))
        self.assertFalse(any(key.startswith("MATRIX_BUS_") for key in values))

    def test_selecting_local_clears_inherited_matrix_environment(self):
        self.write_local_profile()
        inherited = {
            **self.env,
            "AGENT_BUS_TRANSPORT": "matrix",
            "MATRIX_BUS_CFG": "/old/matrix",
            "MATRIX_BUS_HS": "https://old.example.test",
            "MATRIX_BUS_ROOM": "!old:example.test",
            "MATRIX_BUS_REGISTRY_ROOM": "!old-registry:example.test",
        }
        selected = fleet_profile.command_env("alpha", inherited)
        self.assertEqual(selected["AGENT_BUS_TRANSPORT"], "local")
        self.assertIn("AGENT_BUS_CFG", selected)
        for key in (
            "MATRIX_BUS_CFG", "MATRIX_BUS_HS", "MATRIX_BUS_ROOM",
            "MATRIX_BUS_REGISTRY_ROOM",
        ):
            self.assertNotIn(key, selected)

    def test_selecting_matrix_clears_inherited_local_environment(self):
        self.write_profile()
        inherited = {
            **self.env,
            "AGENT_BUS_TRANSPORT": "local",
            "AGENT_BUS_CFG": "/old/local",
        }
        selected = fleet_profile.command_env("alpha", inherited)
        self.assertEqual(selected["AGENT_BUS_TRANSPORT"], "matrix")
        self.assertNotIn("AGENT_BUS_CFG", selected)
        self.assertEqual(selected["MATRIX_BUS_CFG"], str(self.matrix / "alpha"))

    def test_default_keeps_legacy_environment(self):
        original = {
            "NW_TMUX_SERVER": "manual-default",
            "DISPATCH_LEDGER_DB": "/tmp/manual.sqlite3",
        }
        selected = fleet_profile.command_env("default", original)
        self.assertEqual(selected["NW_TMUX_SERVER"], "manual-default")
        self.assertEqual(selected["DISPATCH_LEDGER_DB"], "/tmp/manual.sqlite3")
        self.assertNotIn("NW_FLEET", selected)
        self.assertNotIn("NW_FLEET_PROFILE_APPLIED", selected)

    def test_default_drops_an_unmarked_local_bus_selection(self):
        selected = fleet_profile.command_env("default", {
            "AGENT_BUS_TRANSPORT": "local",
            "AGENT_BUS_CFG": "/tmp/local-bus",
            "AGENT_BUS_DB": "/tmp/local-bus/db.sqlite3",
            "DISPATCH_LEDGER_DB": "/tmp/manual-ledger.sqlite3",
        })
        self.assertNotIn("AGENT_BUS_TRANSPORT", selected)
        self.assertNotIn("AGENT_BUS_CFG", selected)
        self.assertNotIn("AGENT_BUS_DB", selected)
        self.assertEqual(
            selected["DISPATCH_LEDGER_DB"], "/tmp/manual-ledger.sqlite3"
        )

    def test_explicit_default_removes_an_inherited_named_profile(self):
        self.write_profile()
        named = fleet_profile.command_env("alpha", self.env)
        selected = fleet_profile.command_env("default", named)
        self.assertEqual(selected["NW_TMUX_SERVER"], "default")
        for key in ("NW_FLEET", "NW_FLEET_PROFILE_APPLIED",
                    "NOTES_RUNTIME_DIR", "MATRIX_BUS_CFG", "DISPATCH_LEDGER_DB",
                    "AGENT_BUS_TRANSPORT", "AGENT_BUS_CFG", "AGENT_BUS_DB",
                    "MATRIX_BUS_ROOM", "MATRIX_BUS_REGISTRY_ROOM"):
            self.assertNotIn(key, selected)

    def test_local_profile_is_bound_to_its_creating_host(self):
        self.write_local_profile(local_host="some-other-host")
        with self.assertRaisesRegex(fleet_profile.FleetProfileError,
                                    "belongs to host 'some-other-host'"):
            fleet_profile.resolve("alpha", self.env)

    def test_foreign_local_profile_does_not_block_other_profiles(self):
        self.write_profile("alpha")
        self.write_local_profile("beta", local_host="some-other-host")
        self.assertEqual(fleet_profile.resolve("alpha", self.env)["NW_FLEET"], "alpha")

        (self.profiles / "alpha.json").unlink()
        self.write_local_profile("alpha")
        self.assertEqual(fleet_profile.resolve("alpha", self.env)["NW_FLEET"], "alpha")

    def test_local_hostname_uses_the_agent_bus_short_host_identity(self):
        with mock.patch.object(fleet_profile.socket, "gethostname",
                               return_value="worker.example.test"):
            self.assertEqual(fleet_profile.local_hostname(), "worker")
            path, created = fleet_profile.create_local_profile("gamma", env=self.env)
            self.assertTrue(created)
            self.assertEqual(json.loads(path.read_text())["local_host"], "worker")

    def test_local_schema_fields_are_exact(self):
        path = self.write_local_profile()
        value = json.loads(path.read_text())
        value["matrix_room"] = "!not-local:example.test"
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(fleet_profile.FleetProfileError,
                                    "unknown=matrix_room"):
            fleet_profile.resolve("alpha", self.env)

    def test_exec_exports_profile_without_shell_evaluation(self):
        self.write_profile()
        env = {**os.environ, **self.env}
        code = "import json,os; print(json.dumps({k:os.environ[k] for k in ('NW_FLEET','NW_TMUX_SERVER','DISPATCH_LEDGER_DB')}))"
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "exec", "alpha", "--",
             sys.executable, "-c", code],
            env=env, text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        values = json.loads(result.stdout)
        self.assertEqual(values["NW_FLEET"], "alpha")
        self.assertEqual(values["NW_TMUX_SERVER"], "nw-alpha")
        self.assertEqual(
            values["DISPATCH_LEDGER_DB"],
            str(self.matrix / "alpha" / "dispatch-ledger.sqlite3"),
        )

    def test_direct_bus_adapter_cannot_fall_back_from_an_unresolved_name(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "agent-bus-v3.py"),
             "source-identity"],
            env={**os.environ, "NW_FLEET": "alpha"},
            text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("profile is invalid", result.stderr)

    def test_direct_local_adapter_cannot_fall_back_without_named_paths(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "agent-bus-v3.py"),
             "source-identity"],
            env={
                **os.environ,
                "NW_FLEET": "alpha",
                "NW_FLEET_PROFILE_APPLIED": "alpha",
                "AGENT_BUS_TRANSPORT": "local",
            },
            text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("profile is invalid", result.stderr)

    def test_default_adapter_supports_configured_local_transport(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "agent-bus-v3.py"),
             "source-identity"],
            env={
                **os.environ,
                "AGENT_BUS_TRANSPORT": "local",
                "AGENT_BUS_CFG": str(self.base / "leaked-local"),
                "AGENT_BUS_DB": str(self.base / "leaked-local" / "bus.sqlite3"),
            },
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_matrix_commands_ignore_removed_local_poll_override(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "agent-bus-v3.py"),
             "source-identity"],
            env={**os.environ, "AGENT_BUS_LOCAL_WATCH_POLL": "not-a-number"},
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_direct_local_adapter_refuses_another_fleets_database(self):
        self.write_local_profile("alpha")
        self.write_local_profile("beta")
        env = fleet_profile.command_env("alpha", {**os.environ, **self.env})
        beta = fleet_profile.resolve("beta", self.env)
        env["AGENT_BUS_CFG"] = beta["AGENT_BUS_CFG"]
        env["AGENT_BUS_DB"] = beta["AGENT_BUS_DB"]
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "agent-bus-v3.py"),
             "source-identity"],
            env=env, text=True, capture_output=True, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not fully resolved", result.stderr)
        self.assertIn("AGENT_BUS_CFG", result.stderr)

    def test_existing_matrix_process_without_transport_selector_remains_valid(self):
        self.write_profile("alpha")
        env = fleet_profile.command_env("alpha", {**os.environ, **self.env})
        env.pop("AGENT_BUS_TRANSPORT")
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "agent-bus-v3.py"),
             "source-identity"],
            env=env, text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("sha256:"))

    def test_orc_named_fleets_have_separate_work_graphs(self):
        self.write_local_profile("alpha")
        self.write_local_profile("beta")
        env = {**os.environ, **self.env, "DISPATCH_LEDGER_ACTOR": "profile-test"}

        def orc(fleet, *args):
            return subprocess.run(
                [str(ROOT / "scripts" / "orc"), "--fleet", fleet, *args],
                env=env, text=True, capture_output=True, check=False,
            )

        alpha = orc("alpha", "open", "--to", "tmux14",
                    "--subject", "alpha-only", "--no-check")
        beta = orc("beta", "open", "--to", "tmux14",
                   "--subject", "beta-only", "--no-check")
        self.assertEqual(alpha.returncode, 0, alpha.stderr + alpha.stdout)
        self.assertEqual(beta.returncode, 0, beta.stderr + beta.stdout)
        alpha_board = orc("alpha", "board")
        beta_board = orc("beta", "board")
        self.assertEqual(alpha_board.returncode, 0, alpha_board.stderr)
        self.assertEqual(beta_board.returncode, 0, beta_board.stderr)
        self.assertIn("alpha-only", alpha_board.stdout)
        self.assertNotIn("beta-only", alpha_board.stdout)
        self.assertIn("beta-only", beta_board.stdout)
        self.assertNotIn("alpha-only", beta_board.stdout)
        ledger = Path("state/fleet-orchestrator/dispatch-ledger.sqlite3")
        self.assertTrue((self.runtime / "alpha" / ledger).is_file())
        self.assertTrue((self.runtime / "beta" / ledger).is_file())

    def test_local_agent_bus_is_isolated_between_named_fleets(self):
        env = {**os.environ, **self.env, **self.tmux_env}
        env.pop("TMUX_PANE", None)
        bus_script = ROOT / "scripts" / "matrix-bus.sh"

        def bus(fleet, *args):
            return subprocess.run(
                ["bash", str(bus_script), "--fleet", fleet, *args],
                env=env, text=True, capture_output=True, check=False,
            )

        for fleet in ("alpha", "beta"):
            created = subprocess.run(
                [str(ROOT / "scripts" / "orc"), "fleet", "create", fleet],
                env=env, text=True, capture_output=True, check=False,
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            setup = bus(fleet, "setup", "host/sender")
            self.assertEqual(setup.returncode, 0, setup.stderr)
            self.assertIn("transport=local", setup.stdout)
            self.assertFalse(
                (self.runtime / fleet / "state" / "agent-bus" / "agent.env").exists()
            )

        identities = {}
        for fleet in ("alpha", "beta"):
            identities[fleet] = {}
            for role in ("sender", "receiver"):
                joined = bus(
                    fleet, "join", f"host/{role}", f"slot/{role}",
                    "test", "pull", "host", "no-tmux",
                )
                self.assertEqual(joined.returncode, 0, joined.stderr)
                identities[fleet][role] = json.loads(joined.stdout)["agent_id"]

        sent = bus(
            "alpha", "send", identities["alpha"]["sender"],
            identities["alpha"]["receiver"], "alpha-only", "local body",
        )
        self.assertEqual(sent.returncode, 0, sent.stderr)
        self.assertEqual(json.loads(sent.stdout)["transport_state"], "accepted")

        alpha_pull = bus("alpha", "pull", identities["alpha"]["receiver"])
        self.assertEqual(alpha_pull.returncode, 0, alpha_pull.stderr)
        self.assertIn("alpha-only", alpha_pull.stdout)

        beta_unread = bus("beta", "unread", identities["beta"]["receiver"])
        self.assertEqual(beta_unread.returncode, 0, beta_unread.stderr)
        self.assertEqual(json.loads(beta_unread.stdout)["count"], 0)

        bus_rel = Path("state/agent-bus/agent-bus-v3.sqlite3")
        self.assertTrue((self.runtime / "alpha" / bus_rel).is_file())
        self.assertTrue((self.runtime / "beta" / bus_rel).is_file())
        self.assertNotEqual(
            (self.runtime / "alpha" / bus_rel).resolve(),
            (self.runtime / "beta" / bus_rel).resolve(),
        )

    def test_orc_without_fleet_keeps_legacy_database_override(self):
        db = self.base / "legacy" / "ledger.sqlite3"
        env = {
            **os.environ,
            "DISPATCH_LEDGER_DB": str(db),
            "MATRIX_BUS_CFG": str(self.base / "legacy" / "bus"),
            "NOTES_RUNTIME_DIR": str(self.base / "legacy" / "runtime"),
            "AGENT_BUS_DB": str(self.base / "legacy" / "agent-bus.sqlite3"),
            "DISPATCH_LEDGER_ACTOR": "profile-test",
        }
        opened = subprocess.run(
            [str(ROOT / "scripts" / "orc"), "open", "--to", "tmux3",
             "--subject", "legacy-default", "--no-check"],
            env=env, text=True, capture_output=True, check=False,
        )
        self.assertEqual(opened.returncode, 0, opened.stderr + opened.stdout)
        board = subprocess.run(
            [str(ROOT / "scripts" / "orc"), "board"], env=env,
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(board.returncode, 0, board.stderr)
        self.assertIn("legacy-default", board.stdout)

    def test_orc_fleet_create_is_atomic_idempotent_and_starts_primary_session(self):
        env = {**os.environ, **self.env, **self.tmux_env, "TMUX": "leaked-client"}

        command = [str(ROOT / "scripts" / "orc"), "fleet", "create", "gamma"]
        first = subprocess.run(command, env=env, text=True,
                               capture_output=True, check=False)
        second = subprocess.run(command, env=env, text=True,
                                capture_output=True, check=False)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("created fleet profile", first.stdout)
        self.assertIn("validated existing fleet profile", second.stdout)
        self.assertIn("attach with: tview --fleet gamma", first.stdout)
        path = self.profiles / "gamma.json"
        self.assertEqual(json.loads(path.read_text()), {
            "schema": 2,
            "name": "gamma",
            "tmux_server": "nw-gamma",
            "primary_session": "gamma",
            "agent_bus_transport": "local",
            "local_host": fleet_profile.local_hostname(),
        })
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(list(self.profiles.glob(".gamma.*.tmp")), [])
        log = self.fake_tmux_log.read_text()
        starts = [line for line in log.splitlines() if "|new-session " in line]
        self.assertEqual(len(starts), 1, log)
        self.assertIn("-s gamma -n main|gamma|gamma|unset", starts[0])

    def test_orc_fleet_create_honors_explicit_tmux_and_session(self):
        result = subprocess.run(
            [str(ROOT / "scripts" / "orc"), "fleet", "create", "gamma",
             "--tmux-server", "private-server", "--primary-session", "main"],
            env={**os.environ, **self.env, **self.tmux_env}, text=True,
            capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads((self.profiles / "gamma.json").read_text())
        self.assertEqual(value["tmux_server"], "private-server")
        self.assertEqual(value["primary_session"], "main")

    def test_orc_fleet_create_preserves_an_existing_primary_session(self):
        path = self.write_local_profile(
            "gamma", tmux_server="custom-server", primary_session="0"
        )
        before = path.read_bytes()
        result = subprocess.run(
            [str(ROOT / "scripts" / "orc"), "fleet", "create", "gamma"],
            env={**os.environ, **self.env, **self.tmux_env}, text=True,
            capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(
            (self.fake_tmux_state / "custom-server" / "sessions" / "0").is_file()
        )

    def test_orc_fleet_create_refuses_a_tmux_server_owned_by_another_fleet(self):
        server_dir = self.fake_tmux_state / "nw-gamma"
        (server_dir / "sessions").mkdir(parents=True)
        (server_dir / "environment").mkdir()
        (server_dir / "sessions" / "gamma").touch()
        (server_dir / "environment" / "NW_FLEET_PROFILE_APPLIED").write_text(
            "beta", encoding="utf-8"
        )
        result = subprocess.run(
            [str(ROOT / "scripts" / "orc"), "fleet", "create", "gamma"],
            env={**os.environ, **self.env, **self.tmux_env}, text=True,
            capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("belongs to fleet 'beta'", result.stderr)
        self.assertEqual(
            (server_dir / "environment" / "NW_FLEET_PROFILE_APPLIED").read_text(),
            "beta",
        )
        self.assertFalse((self.profiles / "gamma.json").exists())

    def test_orc_fleet_create_refuses_an_unmarked_existing_tmux_server(self):
        server_dir = self.fake_tmux_state / "nw-gamma"
        (server_dir / "sessions").mkdir(parents=True)
        (server_dir / "sessions" / "unrelated").touch()
        result = subprocess.run(
            [str(ROOT / "scripts" / "orc"), "fleet", "create", "gamma"],
            env={**os.environ, **self.env, **self.tmux_env}, text=True,
            capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("already exists without a valid fleet owner", result.stderr)
        self.assertFalse((self.profiles / "gamma.json").exists())
        self.assertTrue((server_dir / "sessions" / "unrelated").is_file())
        self.assertFalse((server_dir / "sessions" / "gamma").exists())
        self.assertFalse((server_dir / "environment").exists())

    def test_orc_fleet_create_can_retry_after_tmux_start_failure(self):
        command = [str(ROOT / "scripts" / "orc"), "fleet", "create", "gamma"]
        failed = subprocess.run(
            command,
            env={
                **os.environ,
                **self.env,
                "TMUX_BIN": str(self.base / "missing-tmux"),
            },
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(failed.returncode, 2)
        self.assertFalse((self.profiles / "gamma.json").exists())

        retried = subprocess.run(
            command,
            env={**os.environ, **self.env, **self.tmux_env},
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertIn("created fleet profile", retried.stdout)
        self.assertTrue(
            (self.fake_tmux_state / "nw-gamma" / "sessions" / "gamma").is_file()
        )

    def test_orc_fleet_show_reports_transport_neutral_local_paths(self):
        self.write_local_profile("gamma")
        result = subprocess.run(
            [str(ROOT / "scripts" / "orc"), "fleet", "show", "gamma"],
            env={**os.environ, **self.env}, text=True,
            capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        view = json.loads(result.stdout)
        self.assertEqual(view["agent_bus_transport"], "local")
        self.assertEqual(
            view["agent_bus_config_dir"],
            str(self.runtime / "gamma" / "state" / "agent-bus"),
        )
        self.assertNotIn("matrix_config_dir", view)
        self.assertEqual(view["local_host"], fleet_profile.local_hostname())

    def test_concurrent_local_create_publishes_one_complete_profile(self):
        env = {**os.environ, **self.env, **self.tmux_env}
        command = [str(ROOT / "scripts" / "orc"), "fleet", "create", "gamma"]
        processes = [
            subprocess.Popen(command, env=env, text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            for _ in range(8)
        ]
        results = [process.communicate(timeout=10) + (process.returncode,)
                   for process in processes]
        self.assertTrue(all(returncode == 0 for _, _, returncode in results), results)
        value = json.loads((self.profiles / "gamma.json").read_text())
        self.assertEqual(value["schema"], 2)
        self.assertEqual(value["name"], "gamma")
        self.assertEqual(list(self.profiles.glob(".gamma.*.tmp")), [])

    def test_repeated_create_refuses_different_existing_settings(self):
        self.write_local_profile("gamma", tmux_server="custom-server")
        result = subprocess.run(
            [str(ROOT / "scripts" / "orc"), "fleet", "create", "gamma",
             "--tmux-server", "different-server"],
            env={**os.environ, **self.env, **self.tmux_env}, text=True,
            capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("already uses tmux server 'custom-server'", result.stderr)

    def test_local_and_matrix_fleets_cannot_reuse_tmux_server(self):
        self.write_profile("alpha")
        self.write_local_profile("beta", tmux_server="nw-alpha")
        with self.assertRaisesRegex(fleet_profile.FleetProfileError,
                                    "reuse tmux server"):
            fleet_profile.resolve("beta", self.env)

    @unittest.skipUnless(shutil.which("tmux"), "tmux is required")
    def test_two_servers_can_reuse_session_window_and_pane_ids(self):
        suffix = f"{os.getpid()}-{self.base.name[-6:]}"
        servers = [f"nw-alpha-{suffix}", f"nw-beta-{suffix}"]
        self.write_profile("alpha", tmux_server=servers[0])
        self.write_profile("beta", tmux_server=servers[1])
        runner = self.base / "locate.sh"
        runner.write_text(
            "#!/usr/bin/env bash\n"
            f"bash {shlex.quote(str(ROOT / 'scripts' / 'matrix-bus.sh'))} tmux-id >\"$1\"\n"
            "sleep 5\n",
            encoding="utf-8",
        )
        runner.chmod(0o700)
        # tmux leaves a -L socket pathname behind after kill-server. Keep the
        # real servers under one owned temporary directory, and delete it only
        # after every server with a socket was stopped successfully.
        tmux_tmpdir = Path(tempfile.mkdtemp(prefix="fleet-profile-tmux-"))
        env = {**os.environ, **self.env, "TMUX_TMPDIR": str(tmux_tmpdir)}
        socket_dir = tmux_tmpdir / f"tmux-{os.getuid()}"
        try:
            for name, server in zip(("alpha", "beta"), servers, strict=True):
                started = subprocess.run(
                    ["tmux", "-L", server, "new-session", "-d", "-s", "0",
                     "-n", "seed", "sleep 30"],
                    env=env, text=True, capture_output=True, check=False,
                )
                self.assertEqual(started.returncode, 0, started.stderr)
                self.assertTrue((socket_dir / server).is_socket())
                global_socket = Path("/tmp") / f"tmux-{os.getuid()}" / server
                self.assertFalse(global_socket.exists())
                applied = subprocess.run(
                    [sys.executable, str(SCRIPT), "apply-tmux", name], env=env,
                    text=True, capture_output=True, check=False,
                )
                self.assertEqual(applied.returncode, 0, applied.stderr)
                output = self.base / f"{name}.location"
                command = shlex.join([str(runner), str(output)])
                launched = subprocess.run(
                    ["tmux", "-L", server, "new-window", "-d", "-t", "0:14",
                     "-n", name, command],
                    env=env, text=True, capture_output=True, check=False,
                )
                self.assertEqual(launched.returncode, 0, launched.stderr)

            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and not all(
                (self.base / f"{name}.location").is_file()
                and (self.base / f"{name}.location").stat().st_size > 0
                for name in ("alpha", "beta")
            ):
                time.sleep(0.05)
            self.assertEqual(
                (self.base / "alpha.location").read_text().strip(),
                "tmux=0:14.0 win=alpha",
            )
            self.assertEqual(
                (self.base / "beta.location").read_text().strip(),
                "tmux=0:14.0 win=beta",
            )
            pane_ids = []
            for server in servers:
                result = subprocess.run(
                    ["tmux", "-L", server, "display-message", "-p", "-t", "0:14",
                     "#{pane_id}"], env=env, text=True, capture_output=True,
                    check=True,
                )
                pane_ids.append(result.stdout.strip())
            self.assertEqual(pane_ids[0], pane_ids[1],
                             "separate servers should safely reuse pane ids")
        finally:
            cleanup_failures = []
            for server in servers:
                socket_path = socket_dir / server
                if not socket_path.is_socket():
                    continue
                stopped = subprocess.run(
                    ["tmux", "-L", server, "kill-server"], env=env,
                    text=True, capture_output=True, check=False,
                )
                if stopped.returncode != 0:
                    cleanup_failures.append(f"{server}: {stopped.stderr.strip()}")
            if cleanup_failures:
                self.fail(
                    f"tmux cleanup failed; preserved {tmux_tmpdir}: "
                    + "; ".join(cleanup_failures)
                )
            shutil.rmtree(tmux_tmpdir)

    def test_partial_and_unknown_fields_are_refused(self):
        path = self.write_profile()
        value = json.loads(path.read_text())
        del value["matrix_registry_room"]
        value["surprise"] = True
        path.write_text(json.dumps(value))
        with self.assertRaisesRegex(fleet_profile.FleetProfileError,
                                    "missing=matrix_registry_room unknown=surprise"):
            fleet_profile.resolve("alpha", self.env)

    def test_duplicate_fields_and_non_origin_homeserver_are_refused(self):
        path = self.write_profile()
        raw = path.read_text().replace(
            '"schema": 1,', '"schema": 1, "schema": 1,'
        )
        path.write_text(raw)
        with self.assertRaisesRegex(fleet_profile.FleetProfileError,
                                    "duplicate field 'schema'"):
            fleet_profile.resolve("alpha", self.env)
        self.write_profile(matrix_homeserver="https://example.test/path")
        with self.assertRaisesRegex(fleet_profile.FleetProfileError,
                                    "must be one https origin"):
            fleet_profile.resolve("alpha", self.env)

    def test_default_matrix_rooms_are_refused(self):
        config = self.base / "config.json"
        config.write_text(json.dumps({"matrix": {"room": "!default:example.test"}}))
        self.env["FLEET_ORCHESTRATOR_CONFIG"] = str(config)
        self.write_profile(matrix_room="!default:example.test")
        with self.assertRaisesRegex(fleet_profile.FleetProfileError,
                                    "reuses a default Matrix room"):
            fleet_profile.resolve("alpha", self.env)

    def test_legacy_default_matrix_override_is_also_refused(self):
        self.write_profile(matrix_room="!legacy-default:example.test")
        env = {
            **self.env,
            "MATRIX_BUS_ROOM": "!legacy-default:example.test",
        }
        with self.assertRaisesRegex(fleet_profile.FleetProfileError,
                                    "reuses a default Matrix room"):
            fleet_profile.resolve("alpha", env)

    def test_room_or_server_reuse_between_named_fleets_is_refused(self):
        self.write_profile("alpha")
        self.write_profile("beta", tmux_server="nw-alpha")
        with self.assertRaisesRegex(fleet_profile.FleetProfileError,
                                    "reuse tmux server"):
            fleet_profile.resolve("alpha", self.env)
        (self.profiles / "beta.json").unlink()
        self.write_profile("beta", matrix_room="!messages-alpha:example.test")
        with self.assertRaisesRegex(fleet_profile.FleetProfileError,
                                    "reuse a Matrix room"):
            fleet_profile.resolve("alpha", self.env)

    def test_default_tmux_server_reuse_is_refused(self):
        selector = (Path(self.env["HOME"]) / ".local/state/fleet-orchestrator"
                    / "state/fleet-orchestrator/tmux-server")
        selector.parent.mkdir(parents=True)
        selector.write_text("nw-alpha\n")
        self.write_profile("alpha")
        with self.assertRaisesRegex(fleet_profile.FleetProfileError,
                                    "reuses the default tmux server"):
            fleet_profile.resolve("alpha", self.env)

    def test_inherited_legacy_tmux_server_reuse_is_refused(self):
        self.write_profile("alpha")
        env = {**self.env, "NW_TMUX_SERVER": "nw-alpha"}
        with self.assertRaisesRegex(fleet_profile.FleetProfileError,
                                    "reuses the default tmux server"):
            fleet_profile.resolve("alpha", env)

    def test_profile_symlink_is_refused(self):
        real = self.base / "outside.json"
        path = self.write_profile()
        path.replace(real)
        path.symlink_to(real)
        with self.assertRaisesRegex(fleet_profile.FleetProfileError, "symlink"):
            fleet_profile.resolve("alpha", self.env)

    def test_name_validation_blocks_paths_and_reserved_default_has_no_file(self):
        for name in ("../alpha", "Alpha", "a_b", "a" * 33):
            with self.subTest(name=name):
                with self.assertRaises(fleet_profile.FleetProfileError):
                    fleet_profile.profile_path(name, self.env)
        self.assertEqual(fleet_profile.resolve("default", self.env), {})


if __name__ == "__main__":
    unittest.main()
