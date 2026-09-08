"""Fleet terminal resolution never guesses a different server or database."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/lib/fleet-profile.py"
SPEC = importlib.util.spec_from_file_location("terminal_fleet_profile", SCRIPT)
profile = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(profile)


@pytest.fixture
def selection(tmp_path):
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    config = tmp_path / "config.json"
    config.write_text(json.dumps({
        "schema": "fleet-runtime/v1",
        "fleets": {"default_name": "primary", "profile_directory": str(profiles)},
        "runtime_dir": {"env": "NOTES_RUNTIME_DIR", "default": str(tmp_path / "default")},
        "tmux": {"primary_session": "main"},
    }))
    env = {
        "HOME": str(tmp_path / "home"),
        "PATH": os.environ["PATH"],
        "FLEET_ORCHESTRATOR_CONFIG": str(config),
        "NW_DEFAULT_TMUX_SERVER": "primary-server",
    }
    for name in ("alpha", "beta"):
        (profiles / f"{name}.json").write_text(json.dumps({
            "schema": 2,
            "name": name,
            "tmux_server": f"named-{name}",
            "primary_session": "main",
            "agent_bus_transport": "local",
            "local_host": profile.local_hostname(),
        }))
    return env, config, profiles


def completed(stdout="", code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


def test_default_alias_preserves_existing_environment_and_does_not_create_profile(selection):
    env, _, profiles = selection
    manual = {**env, "DISPATCH_LEDGER_DB": "/manual/existing.sqlite3"}
    assert profile.resolve("primary", manual) == profile.resolve("default", manual) == {}
    assert profile.command_env("primary", manual) == profile.command_env("default", manual)
    assert profile.command_env("primary", manual)["DISPATCH_LEDGER_DB"] == "/manual/existing.sqlite3"
    expected = {"name": "primary", "tmux_server": "primary-server", "primary_session": "main"}
    assert profile.public_view("default", manual) == expected
    assert profile.public_view("primary", manual) == expected
    assert profile.terminal_target("primary", manual) == expected
    with pytest.raises(profile.FleetProfileError, match="has no profile"):
        profile.create_local_profile("primary", env=env)
    assert not (profiles / "primary.json").exists()


def test_explicit_default_alias_scrubs_named_runtime_before_reading_default_selector(selection, tmp_path):
    env, _, _ = selection
    env.pop("NW_DEFAULT_TMUX_SERVER")
    selector = tmp_path / "default/state/fleet-orchestrator/tmux-server"
    selector.parent.mkdir(parents=True)
    selector.write_text("original-server\n")
    named = profile.command_env("alpha", env)
    target = profile.terminal_target("primary", named)
    assert target["tmux_server"] == "original-server"
    cleared = profile.command_env("primary", named)
    for key in profile.PROFILE_ENV_KEYS:
        if key != "NW_TMUX_SERVER":
            assert key not in cleared
    assert cleared["NW_TMUX_SERVER"] == "original-server"


def test_partial_named_environment_cannot_redirect_explicit_default(selection):
    env, _, _ = selection
    partial = {**env, "NW_FLEET": "alpha", "NW_TMUX_SERVER": "named-alpha",
               "NOTES_RUNTIME_DIR": "/wrong/runtime", "DISPATCH_LEDGER_DB": "/wrong/database"}
    target = profile.terminal_target("primary", partial)
    assert target["tmux_server"] == "primary-server"
    cleared = profile.command_env("default", partial)
    assert cleared["NW_TMUX_SERVER"] == "primary-server"
    assert "NOTES_RUNTIME_DIR" not in cleared
    assert "DISPATCH_LEDGER_DB" not in cleared


def test_ordinary_manual_default_server_override_is_retained(selection):
    env, _, _ = selection
    env.pop("NW_DEFAULT_TMUX_SERVER")
    env["NW_TMUX_SERVER"] = "manual-server"
    assert profile.terminal_target("default", env)["tmux_server"] == "manual-server"


def test_terminal_profile_discovery_ignores_named_environment_derived_roots(selection):
    env, config, profiles = selection
    data = json.loads(config.read_text())
    data["fleets"]["profile_directory"] = {
        "env": "NOTES_RUNTIME_DIR", "default": str(profiles.parent), "suffix": "/profiles",
    }
    config.write_text(json.dumps(data))
    inherited = {**env, "NW_FLEET": "alpha", "NW_FLEET_PROFILE_APPLIED": "alpha",
                 "NOTES_RUNTIME_DIR": "/wrong/named/runtime", "NW_TMUX_SERVER": "named-alpha"}
    with mock.patch.object(profile, "_terminal_tmux", return_value=completed(code=1)):
        rows = profile.terminal_inventory(inherited)
    assert [row["name"] for row in rows] == ["primary", "alpha", "beta"]
    assert all(row["status"] != "invalid" for row in rows)
    assert profile.terminal_target("beta", inherited)["tmux_server"] == "named-beta"


def test_default_alias_collision_is_an_error_for_both_spellings(selection):
    env, _, profiles = selection
    (profiles / "primary.json").write_text("{}")
    for name in ("default", "primary", "alpha"):
        with pytest.raises(profile.FleetProfileError, match="conflicts with a named profile"):
            profile.resolve(name, env)
    assert all(row["status"] == "invalid" for row in profile.terminal_inventory(env))


@pytest.mark.parametrize("field,value", [
    ("default_name", "invalid name"), ("default_name", 1),
    ("primary_session", "main:2"), ("primary_session", None),
])
def test_invalid_default_configuration_does_not_select_a_terminal(selection, field, value):
    env, config, _ = selection
    data = json.loads(config.read_text())
    data["fleets" if field == "default_name" else "tmux"][field] = value
    config.write_text(json.dumps(data))
    with pytest.raises(profile.FleetProfileError):
        profile.terminal_target("default", env)


def test_default_fallback_always_names_a_server(tmp_path):
    assert profile.terminal_target("default", {"HOME": str(tmp_path)}) == {
        "name": "default", "tmux_server": "default", "primary_session": "0",
    }


def test_explicit_target_wins_over_actual_tmux_and_stale_environment(selection):
    env, _, _ = selection
    env.update(TMUX="/current/socket,1,0", NW_FLEET="beta")
    with mock.patch.object(profile, "_terminal_tmux", side_effect=AssertionError("must not probe")):
        assert profile.terminal_target("alpha", env)["tmux_server"] == "named-alpha"


def test_outside_tmux_retains_explicit_environment_selection(selection):
    env, _, _ = selection
    assert profile.terminal_target(env={**env, "NW_FLEET": "alpha"})["name"] == "alpha"
    assert profile.terminal_target(env=env)["name"] == "primary"
    with pytest.raises(profile.FleetProfileError, match="does not exist"):
        profile.terminal_target(env={**env, "NW_FLEET": "absent"})


@pytest.mark.parametrize("current_session,current_group", [("main", ""), ("tview-terminal", "group-1")])
def test_actual_socket_and_primary_group_override_stale_fleet(selection, current_session, current_group):
    env, _, _ = selection
    env.update(TMUX="/tmp/named-beta,4,0", TMUX_PANE="%5", NW_FLEET="alpha")

    def tmux(args, process_env):
        if args[0] == "display-message":
            assert args[2:4] == ["-t", "%5"]
            assert process_env["TMUX"] == env["TMUX"]
            return completed(f"/tmp/named-beta\t4\t%5\t{current_session}\t{current_group}\n")
        assert args[:1] == ["-L"]
        assert "TMUX" not in process_env and "TMUX_PANE" not in process_env
        return completed(f"main\tgroup-1\t/tmp/{args[1]}\n")

    with mock.patch.object(profile, "_terminal_tmux", side_effect=tmux):
        assert profile.terminal_target(env=env)["name"] == "beta"


def test_unknown_current_session_refuses_inherited_or_default_fallback(selection):
    env, _, _ = selection
    env.update(TMUX="/tmp/named-beta,4,0", TMUX_PANE="%5", NW_FLEET="alpha")

    def tmux(args, process_env):
        if args[0] == "display-message":
            return completed("/tmp/named-beta\t4\t%5\tunrelated\tother-group\n")
        return completed(f"main\tmain-group\t/tmp/{args[1]}\n")

    with mock.patch.object(profile, "_terminal_tmux", side_effect=tmux):
        with pytest.raises(profile.FleetProfileError, match="not associated.*tview --list"):
            profile.terminal_target(env=env)


STALE_PAIR = {"TMUX": "/tmp/live,1,0", "TMUX_PANE": "%5"}


@pytest.mark.parametrize("answer", [
    pytest.param(completed(code=1, stderr="no server running on /tmp/live"), id="server-gone"),
    pytest.param(completed("/tmp/live\t1\t\t\t\n"), id="pane-gone-tmux-3.7-empty-fields"),
    pytest.param(completed(code=1, stderr="can't find pane: %5"), id="pane-gone-older-tmux"),
    pytest.param(completed("/tmp/live\t2\t%5\tmain\t\n"), id="restarted-server-reused-pane-number"),
    pytest.param(completed("/tmp/live\t1\t%6\tmain\t\n"), id="another-pane-answered"),
    pytest.param(completed("/tmp/live\t1\t%5\n"), id="truncated-record"),
])
def test_stale_tmux_pair_means_outside_tmux(selection, answer):
    """A daemon started in a pane hands TMUX/TMUX_PANE to every shell it opens,
    long after that server or pane is gone. Such a pair selects nothing: the
    terminal is outside tmux, so NW_FLEET or the default applies, exactly as
    when the variables are absent."""
    env, _, _ = selection

    def tmux(args, process_env):
        if args[0] == "display-message":
            # Never an unqualified "current" query: tmux would answer it with
            # an arbitrary attached client.
            assert args[2:4] == ["-t", "%5"]
            return answer
        return completed(f"main\tgroup\t/tmp/{args[1]}\n")

    with mock.patch.object(profile, "_terminal_tmux", side_effect=tmux):
        assert profile.current_pane({**env, **STALE_PAIR}) is None
        assert profile.terminal_target(env={**env, **STALE_PAIR})["name"] == "primary"
        assert profile.terminal_target(env={**env, **STALE_PAIR, "NW_FLEET": "alpha"})["name"] == "alpha"
        with pytest.raises(profile.FleetProfileError, match="does not exist"):
            profile.terminal_target(env={**env, **STALE_PAIR, "NW_FLEET": "absent"})


@pytest.mark.parametrize("pair", [
    pytest.param({"TMUX": "/tmp/live,1,0"}, id="no-pane"),
    pytest.param({"TMUX": "/tmp/live,1,0", "TMUX_PANE": "5"}, id="not-a-pane-id"),
    pytest.param({"TMUX": "/tmp/live", "TMUX_PANE": "%5"}, id="tmux-without-server-pid"),
    pytest.param({"TMUX": "/tmp/live,pid,0", "TMUX_PANE": "%5"}, id="non-numeric-server-pid"),
    pytest.param({"TMUX": ",1,0", "TMUX_PANE": "%5"}, id="empty-socket"),
])
def test_unverifiable_tmux_pair_is_never_probed(selection, pair):
    env, _, _ = selection

    def tmux(args, process_env):
        assert args[0] != "display-message", "an unverifiable pair must not ask tmux for a current client"
        return completed(f"main\tgroup\t/tmp/{args[1]}\n")

    with mock.patch.object(profile, "_terminal_tmux", side_effect=tmux):
        assert profile.current_pane({**env, **pair}) is None
        assert profile.terminal_target(env={**env, **pair})["name"] == "primary"
        assert profile.terminal_target(env={**env, **pair, "NW_FLEET": "alpha"})["name"] == "alpha"


def test_unanswered_pane_probe_is_reported_not_guessed(selection):
    env, _, _ = selection
    failure = profile.FleetProfileError("tmux inspection timed out")
    with mock.patch.object(profile, "_terminal_tmux", side_effect=failure):
        with pytest.raises(profile.FleetProfileError, match="timed out.*tview --list"):
            profile.terminal_target(env={**env, **STALE_PAIR})


def test_terminal_cli_ignores_stale_pair_from_a_daemon(selection, tmp_path):
    env, _, _ = selection
    stub = tmp_path / "tmux"
    stub.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *display-message*) printf '/tmp/live\\t1\\t\\t\\t\\n' ;;\n"  # tmux 3.7 shape for a missing pane
        "  *) echo 'no server running on /tmp/live' >&2; exit 1 ;;\n"
        "esac\n"
    )
    stub.chmod(0o755)
    env = {**env, "TMUX_BIN": str(stub), "TMUX": "/tmp/live,999999,166", "TMUX_PANE": "%195"}
    result = subprocess.run([sys.executable, "-B", str(SCRIPT), "terminal"],
                            env=env, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "primary\tprimary-server\tmain\n"
    result = subprocess.run([sys.executable, "-B", str(SCRIPT), "terminal"],
                            env={**env, "NW_FLEET": "alpha"}, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "alpha\tnamed-alpha\tmain\n"


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_real_tmux_pane_is_trusted_only_for_its_own_server_generation(selection, tmp_path):
    env, _, _ = selection
    env = {**env, "TMUX_TMPDIR": str(tmp_path), "LC_ALL": "C"}
    command = [shutil.which("tmux"), "-L", "terminal-pane-test"]
    started = subprocess.run([*command, "new-session", "-d", "-s", "main", "sleep 30"],
                             env=env, text=True, capture_output=True, check=False)
    assert started.returncode == 0, started.stderr
    try:
        # A detached server replaces tab delimiters without -u; use "|" here.
        shown = subprocess.run([*command, "display-message", "-p", "-t", "main",
                                "#{socket_path}|#{pid}|#{pane_id}"],
                               env=env, text=True, capture_output=True, check=False)
        assert shown.returncode == 0, shown.stderr
        socket_path, server_pid, pane = shown.stdout.rstrip("\n").split("|")
        live = {**env, "TMUX": f"{socket_path},{server_pid},0", "TMUX_PANE": pane}
        assert profile.current_pane(live) == (socket_path, "main", "")
        restarted = {**live, "TMUX": f"{socket_path},{int(server_pid) + 1},0"}
        assert profile.current_pane(restarted) is None
        assert profile.current_pane({**live, "TMUX_PANE": "%999999"}) is None
    finally:
        stopped = subprocess.run([*command, "kill-server"], env=env, text=True,
                                 capture_output=True, check=False)
        assert stopped.returncode == 0, stopped.stderr
    assert profile.current_pane(live) is None


def test_inventory_distinguishes_online_offline_and_missing_primary_without_mutation(selection):
    env, _, profiles = selection
    before = {p.name: p.read_bytes() for p in profiles.iterdir()}
    seen = []

    def tmux(args, process_env):
        seen.append(args)
        assert args[0] == "-L" and args[2] == "list-sessions"
        if args[1] == "primary-server":
            return completed("main\tgroup\t/tmp/primary-server\ntview-a\tgroup\t/tmp/primary-server\n")
        if args[1] == "named-alpha":
            return completed(code=1, stderr="no server running on /tmp/named-alpha")
        return completed("unrelated\t\t/tmp/named-beta\n")

    with mock.patch.object(profile, "_terminal_tmux", side_effect=tmux):
        rows = profile.terminal_inventory(env)
    assert [(row["name"], row["status"]) for row in rows] == [
        ("primary", "online"), ("alpha", "offline"), ("beta", "missing-primary"),
    ]
    assert len(seen) == 5  # Discovery, default group recovery, and three target observations.
    assert before == {p.name: p.read_bytes() for p in profiles.iterdir()}
    assert rows[0]["command"] == "tview -t primary"
    assert all(set(row) == {"name", "tmux_server", "primary_session", "status", "command", "detail"}
               for row in rows)


def test_invalid_profile_details_and_transport_paths_do_not_leak_in_inventory(selection):
    env, _, profiles = selection
    bad = profiles / "alpha.json"
    value = json.loads(bad.read_text())
    value["private"] = "/sensitive/path/credential"
    bad.write_text(json.dumps(value))
    with mock.patch.object(profile, "_terminal_tmux", return_value=completed(code=1)):
        rows = profile.terminal_inventory(env)
    output = json.dumps(rows)
    assert "/sensitive" not in output and str(profiles) not in output
    assert next(row for row in rows if row["name"] == "alpha")["status"] == "invalid"


def test_probe_timeout_is_bounded_and_reported_without_raw_output(selection):
    env, _, _ = selection
    timeout = subprocess.TimeoutExpired("tmux", 3, output="private token")
    with mock.patch.object(profile.subprocess, "run", side_effect=timeout) as run:
        rows = profile.terminal_inventory(env)
    assert all(row["status"] == "unavailable" for row in rows)
    assert all(call.kwargs["timeout"] == 3 for call in run.call_args_list)
    assert "private token" not in json.dumps(rows)


@pytest.mark.parametrize("result,status", [
    (completed(code=1, stderr="error connecting to socket (No such file or directory)"), "offline"),
    (completed(code=1, stderr="error connecting to socket (Connection refused)"), "offline"),
    (completed(code=1, stderr="private path: Permission denied"), "unavailable"),
    (completed(code=1), "unavailable"),
    (completed("not usable session data\n"), "unavailable"),
    (completed(), "unavailable"),
])
def test_unavailable_probe_does_not_claim_absent_server_or_primary(selection, result, status):
    env, _, _ = selection
    with mock.patch.object(profile, "_terminal_tmux", return_value=result):
        rows = profile.terminal_inventory(env)
    assert all(row["status"] == status for row in rows)
    assert "private path" not in json.dumps(rows)


def test_probes_force_stable_diagnostic_language(selection):
    env, _, _ = selection
    with mock.patch.object(profile.subprocess, "run", return_value=completed(code=1)) as run:
        profile.terminal_inventory({**env, "LC_ALL": "fr_FR.UTF-8"})
    assert all(call.kwargs["env"]["LC_ALL"] == "C" for call in run.call_args_list)
    assert all(call.args[0][1] == "-u" for call in run.call_args_list)


def test_default_alias_pins_orc_terminal_observation_while_preserving_actual_client_context(selection):
    env, _, _ = selection
    named = profile.command_env("alpha", env)
    named["TMUX"] = "/tmp/named-alpha,1,0"
    cleared = profile.command_env("primary", named)
    assert cleared["TMUX"] == named["TMUX"]
    assert "DISPATCH_LEDGER_DB" not in cleared
    code = (
        "import json,sys; sys.path.insert(0,sys.argv[1]); "
        "import tmux_runtime; print(json.dumps(tmux_runtime.base_cmd()))"
    )
    result = subprocess.run([sys.executable, "-B", "-c", code, str(SCRIPT.parent)],
                            env=cleared, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == ["tmux", "-L", "primary-server"]


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
def test_real_detached_tmux_listing_keeps_tab_delimiters_in_c_locale(selection, tmp_path):
    env, _, _ = selection
    env = {**env, "TMUX_TMPDIR": str(tmp_path), "LC_ALL": "C"}
    command = [shutil.which("tmux"), "-L", "terminal-delimiter-test"]
    started = subprocess.run([*command, "new-session", "-d", "-s", "main", "sleep 30"],
                             env=env, text=True, capture_output=True, check=False)
    assert started.returncode == 0, started.stderr
    try:
        status, metadata = profile._terminal_observation({
            "name": "synthetic", "tmux_server": "terminal-delimiter-test", "primary_session": "main",
        }, env)
        assert status == "online"
        assert metadata["session"] == "main"
        assert metadata["socket"] == str(tmp_path / f"tmux-{os.getuid()}/terminal-delimiter-test")
    finally:
        stopped = subprocess.run([*command, "kill-server"], env=env, text=True,
                                 capture_output=True, check=False)
        assert stopped.returncode == 0, stopped.stderr


def test_terminal_cli_emits_one_safe_tsv_record(selection):
    env, _, _ = selection
    result = subprocess.run([sys.executable, "-B", str(SCRIPT), "terminal", "primary"],
                            env=env, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "primary\tprimary-server\tmain\n"


def test_list_cli_reports_missing_tmux_without_config_paths(selection):
    env, _, _ = selection
    env["TMUX_BIN"] = "/not-installed/tmux"
    result = subprocess.run([sys.executable, "-B", str(SCRIPT), "list", "--json"],
                            env=env, text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr
    rows = json.loads(result.stdout)
    assert all(row["status"] == "unavailable" for row in rows)
    assert "/not-installed" not in result.stdout
