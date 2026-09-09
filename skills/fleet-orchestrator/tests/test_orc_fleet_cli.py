"""Public fleet navigation on real isolated tmux sessions and task stores."""
import json
import re
import shutil
import sqlite3

import pytest

import test_session_fleets as sessions


@pytest.fixture
def fleet():
    if not shutil.which("tmux"):
        pytest.skip("tmux is required")
    fixture = sessions.SessionFleetTest()
    fixture.setUp()
    try:
        yield fixture
    finally:
        fixture.doCleanups()


def orc(fleet, *args, **kwargs):
    return fleet.run_command([sessions.ORC, *args], **kwargs)


def open_task(fleet, name, subject, *args):
    result = orc(fleet, "fleet", name, "task", "open", "--to", "operator",
                 "--subject", subject, "--body", "Inspect this synthetic result.", "--no-check", *args)
    return re.search(r"\b[0-9a-f]{8}\b", result.stdout).group()


def test_help_explains_fleet_work_without_initializing_state(fleet):
    before = list(fleet.root.rglob("*"))
    text = orc(fleet, "--help").stdout
    assert "orc fleet NAME" in text and "tview -t" in text
    assert "admin import-state" in text
    assert "fleet-orchestrator.py" not in text
    assert "task open" in text and "task dispatch" in text
    assert "agent topology" in text and "task brief" in text
    assert "review verdict" in text and "review receipt" in text
    assert "goal team" in text and "agent role" in text
    assert "claim-done" in text and "does not close" in text
    assert orc(fleet, "help", "fleet").stdout == text
    assert "task" in orc(fleet, "fleet", "--help").stdout
    assert "claim-done" in orc(fleet, "fleet", "example", "task", "--help").stdout
    assert list(fleet.root.rglob("*")) == before


def test_help_explains_workflow_and_legacy_names_without_selecting_a_fleet(fleet):
    before = list(fleet.root.rglob("*"))
    text = orc(fleet, "fleet", "missing", "help", "task").stdout
    assert "orc fleet missing task COMMAND --help" in text
    assert "Record a task without sending" in text
    assert "Record a task and send" in text
    assert "task handshake" in text
    assert "open -> ack -> note -> claim-done" in text
    text = orc(fleet, "help", "review").stdout
    assert "review verdict" in text and "review receipt" in text
    assert "merge permission" in text
    text = orc(fleet, "help", "legacy").stdout
    assert "statusline" in text and "board --view summary" in text
    assert "topology" in text and "agent topology" in text
    assert list(fleet.root.rglob("*")) == before


def test_unknown_commands_show_grouped_help_without_flat_parser_choices(fleet):
    before = list(fleet.root.rglob("*"))
    for args, expected in [
        (("mistyped",), "task dispatch"),
        (("fleet", "missing", "mistyped"), "task dispatch"),
        (("fleet", "missing", "review", "mistyped"), "receipt"),
        (("help", "mistyped"), "task dispatch"),
    ]:
        result = orc(fleet, *args, check=False)
        assert result.returncode == 2
        assert "unknown" in result.stderr and expected in result.stderr
        assert "choose from" not in result.stderr
        assert "{open,dispatch" not in result.stderr
    assert list(fleet.root.rglob("*")) == before


def test_all_grouped_commands_offer_argument_help_without_writing_state(fleet):
    before = list(fleet.root.rglob("*"))
    # Help for task-changing commands must exit before reading or writing state.
    for group, action in [
        ("agent", "topology"), ("task", "brief"), ("task", "dispatch"),
        ("task", "ack"), ("task", "note"), ("task", "show"),
        ("task", "close"), ("task", "chase"), ("task", "handshake"),
        ("task", "claim-done"), ("review", "verdict"), ("review", "receipt"),
        ("agent", "role"), ("goal", "team"),
    ]:
        text = orc(fleet, "fleet", "default", group, action, "--help").stdout
        assert f"orc fleet default {group} {action}" in text
    goal = orc(fleet, "fleet", "default", "goal", "open", "--help").stdout
    task = orc(fleet, "fleet", "default", "task", "open", "--help").stdout
    for option in ("--workflow", "--owner", "--reviewer", "--ready-cmd", "--done-cmd"):
        assert option not in goal
        assert option in task
    assert "--to" in goal and "--subject" in goal and "--parent" in task
    onboard = orc(fleet, "fleet", "default", "agent", "onboard", "--help").stdout
    paths = re.findall(r"(/\S+/references/agent-bus\.md)", onboard)
    assert paths and all(sessions.Path(path).is_file() for path in paths)
    assert list(fleet.root.rglob("*")) == before


def test_invalid_leaf_arguments_keep_the_selected_command_usage(fleet):
    before = list(fleet.root.rglob("*"))
    result = orc(fleet, "fleet", "default", "task", "show", "example-id",
                 "--unknown-option", check=False)
    assert result.returncode == 2
    assert "orc fleet default task show" in result.stderr
    assert "unrecognized arguments" in result.stderr
    assert "{open,dispatch" not in result.stderr
    assert list(fleet.root.rglob("*")) == before


def test_agent_bus_help_does_not_require_a_resolvable_fleet_or_configuration(fleet):
    before = list(fleet.root.rglob("*"))
    for args in [("--help",), ("-h",), ("help",),
                 ("--fleet", "missing", "--help"),
                 ("--config", str(fleet.root / "missing.json"), "--fleet", "missing", "--help")]:
        result = fleet.run_command([sessions.BUS, *args])
        assert result.stdout.count("single CLI") == 1
        assert not result.stderr
    assert list(fleet.root.rglob("*")) == before


def test_inventory_and_empty_views_are_read_only(fleet):
    orc(fleet, "fleet", "alpha", "start")
    view = fleet.view("alpha")
    # Starting a terminal associates history; browsing must not create either DB.
    before = {str(p): p.read_bytes() for p in fleet.root.rglob("*") if p.is_file()}
    rows = json.loads(orc(fleet, "--json").stdout)
    alpha = next(row for row in rows if row["name"] == "alpha")
    assert alpha["work"] == {"tasks": 0, "goals": 0, "operator": 0, "attention": 0}
    for args in [("board",), ("goals",), ("board", "--view", "columns"),
                 ("board", "--view", "summary"), ("agents",), ()]:
        text = orc(fleet, "fleet", "alpha", *args).stdout
        assert "Fleet alpha | session alpha" in text
    assert not sessions.Path(view["dispatch_ledger_db"]).exists()
    assert not sessions.Path(view["agent_bus_db"]).exists()
    assert {str(p): p.read_bytes() for p in fleet.root.rglob("*") if p.is_file()} == before


def test_board_views_and_short_selector_share_one_fleet(fleet):
    for name in ("alpha", "beta"):
        orc(fleet, "fleet", name, "start")
    first = open_task(fleet, "alpha", "alpha work", "--repo", "one")
    open_task(fleet, "alpha", "another repository", "--repo", "two")
    other = open_task(fleet, "beta", "beta work")
    before = sessions.Path(fleet.view("alpha")["dispatch_ledger_db"]).read_bytes()
    data = json.loads(orc(fleet, "fleet", "alpha", "board", "--json", "--repo", "one").stdout)
    assert data["fleet"] == {"name": "alpha", "session": "alpha"}
    assert [task["id"] for task in data["tasks"]] == [first]
    assert data["counts"]["operator"] == 1
    for args in [("--fleet", "alpha", "board"), ("-t", "alpha", "board"),
                 ("fleet", "alpha", "board"), ("fleet", "alpha")]:
        text = orc(fleet, *args).stdout
        assert first in text and other not in text
        assert "Fleet alpha" in text
    for view, legacy in [("summary", "statusline"), ("columns", "kanban")]:
        text = orc(fleet, "fleet", "alpha", "board", "--view", view, "--no-color").stdout
        assert text == orc(fleet, "--fleet", "alpha", legacy, "--no-color").stdout
        assert "beta work" not in text
    assert sessions.Path(fleet.view("alpha")["dispatch_ledger_db"]).read_bytes() == before


def test_goals_hide_closed_history_and_show_explicit_goal(fleet):
    orc(fleet, "fleet", "alpha", "start")
    def goal(subject):
        result = orc(fleet, "fleet", "alpha", "goal", "open", "--to", "operator",
                     "--subject", subject, "--body", "Synthetic goal.")
        return re.search(r"\b[0-9a-f]{8}\b", result.stdout).group()
    old = goal("historical goal")
    # A historical fixture only: do not exercise or bypass the live completion workflow.
    with sqlite3.connect(fleet.view("alpha")["dispatch_ledger_db"]) as db:
        db.execute("UPDATE dispatch SET state='ready-to-close' WHERE id=?", (old,))
        db.execute("UPDATE dispatch SET state='closed', resolution='done' WHERE id=?", (old,))
    current = goal("current goal")
    child = open_task(fleet, "alpha", "goal child", "--parent", current)
    data = json.loads(orc(fleet, "fleet", "alpha", "goals", "--json").stdout)
    assert [g["id"] for g in data["goals"]] == [current]
    assert data["goals"][0]["children"][0]["id"] == child
    text = orc(fleet, "fleet", "alpha", "goals").stdout
    assert "historical goal" not in text
    assert "historical goal" in orc(fleet, "fleet", "alpha", "goals", "--all").stdout
    assert "historical goal" in orc(fleet, "fleet", "alpha", "goal", "show", old).stdout
    assert "current goal" in orc(fleet, "fleet", "alpha").stdout


def test_rename_stop_and_reopen_keep_history_discoverable(fleet):
    orc(fleet, "fleet", "alpha", "start")
    task = open_task(fleet, "alpha", "saved alpha work")
    database = fleet.view("alpha")["dispatch_ledger_db"]
    fleet.tmux("new-session", "-d", "-t", "alpha", "-s", "tview-private")
    orc(fleet, "fleet", "alpha", "rename", "renamed")
    rows = json.loads(orc(fleet, "--json").stdout)
    assert [r["name"] for r in rows] == ["default", "renamed"]
    orc(fleet, "fleet", "renamed", "stop")
    rows = json.loads(orc(fleet, "--json").stdout)
    assert [r["name"] for r in rows] == ["default", "renamed"]
    row = rows[1]
    assert row["work"]["tasks"] == 1 and row["status"] != "online"
    assert task in orc(fleet, "fleet", "renamed", "board").stdout
    catalog = json.loads(fleet.run_command([sessions.ROOT / "scripts/tview", "--list", "--json"]).stdout)
    assert [r["name"] for r in catalog] == [r["name"] for r in rows]
    orc(fleet, "fleet", "renamed", "start")
    assert fleet.view("renamed")["dispatch_ledger_db"] == database
    assert task in orc(fleet, "fleet", "renamed", "board").stdout


def test_invalid_selection_and_corrupt_work_are_not_an_empty_fleet(fleet):
    orc(fleet, "fleet", "alpha", "start")
    task = open_task(fleet, "alpha", "do not touch")
    before = sessions.Path(fleet.view("alpha")["dispatch_ledger_db"]).read_bytes()
    result = orc(fleet, "fleet", "missing", "board", check=False)
    assert result.returncode != 0
    assert task not in result.stdout
    assert sessions.Path(fleet.view("alpha")["dispatch_ledger_db"]).read_bytes() == before
    orc(fleet, "fleet", "broken", "start")
    path = sessions.Path(fleet.view("broken")["dispatch_ledger_db"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE unrelated(value TEXT)")
    result = orc(fleet, "--json", check=False)
    assert result.returncode != 0
    rows = json.loads(result.stdout)
    assert next(r for r in rows if r["name"] == "broken")["work"] is None
    assert next(r for r in rows if r["name"] == "alpha")["work"]["tasks"] == 1


def test_agent_windows_and_task_details_name_the_selected_fleet(fleet):
    for name in ("alpha", "beta"):
        orc(fleet, "fleet", name, "start")
        fleet.join(name, "same-worker")
    orc(fleet, "fleet", "alpha", "window")
    data = json.loads(orc(fleet, "fleet", "alpha", "agents", "--json").stdout)
    assert len(data["windows"]) == 2
    assert len(data["agents"]) == 1
    assert data["fleet"]["name"] == "alpha"
    task = open_task(fleet, "alpha", "inspect work")
    assert "Fleet alpha" in orc(fleet, "fleet", "alpha", "task", "show", task).stdout


def test_legacy_words_never_reinterpret_lifecycle_actions(fleet):
    orc(fleet, "-t", "start", "start")
    # This old spelling creates a fleet named stop. It must not stop start.
    orc(fleet, "fleet", "start", "stop")
    assert fleet.tmux("has-session", "-t", "=start", check=False).returncode == 0
    assert fleet.tmux("has-session", "-t", "=stop", check=False).returncode == 0
    assert "Fleet stop" in orc(fleet, "-t", "stop", "board").stdout
