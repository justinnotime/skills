"""Synthetic external-command tests for paid-progress and publication ordering."""

from __future__ import annotations

import fcntl
import json
import sys
from pathlib import Path

import pytest

from prompt_translation import scheduled

EXTERNAL = r"""
import json
import sys
import time
from pathlib import Path

state_path = Path(sys.argv[1])
role, *args = sys.argv[2:]
state = json.loads(state_path.read_text())

def save():
    state_path.write_text(json.dumps(state))

def finish(code=0):
    save()
    raise SystemExit(code)

if role == "publisher":
    if args[0] == "worktree":
        action = args[1]
        state["events"].append([action, args])
        if action == "prepare":
            Path(args[args.index("--worktree") + 1]).mkdir(exist_ok=True)
        elif action == "changed":
            for name in state["changed"]:
                sys.stdout.buffer.write(name.encode() + b"\0")
        elif action == "ahead":
            print(state["ahead"])
        elif action == "reset":
            if state["changed"] or state["ahead"]:
                finish(1)
        finish()
    state["events"].append(["publish", args])
    if state.get("publish_status", 0):
        finish(state["publish_status"])
    state["published"] += state["ahead"]
    state["ahead"] = 0
    finish()

if role == "policy":
    action, worktree, scope = args
    state["events"].append([action + "_" + scope, worktree])
    if action == "validate":
        finish(state.get("validate_status", 0))
    if action == "commit":
        status = state.get("commit_status", 0)
        if status == 0:
            state["changed"] = []
            state["ahead"] += 1
        finish(status)
    if action == "recover":
        if state.get("recover_status", 0):
            finish(state["recover_status"])
        state["changed"] = []
        if scope == "committed":
            state["ahead"] = 0
            state["publish_status"] = 0
        state["validate_status"] = 0
        finish()

if role == "translator":
    state["events"].append(["translate", args])
    if "--doctor" in args or "--dry-run" in args:
        finish(state.get("inspection_status", 0))
    state["translations"] += 1
    if state.get("write", True):
        state["changed"] = ["learning/pairs/2025-01-01.md"]
    if state.get("fail_validation_after_translation"):
        state["validate_status"] = 1
    if state.get("stale_after_translation"):
        state["publish_status"] = 3
    save()
    if state.get("translation_delay", 0):
        time.sleep(state["translation_delay"])
    finish(state.get("translation_status", 0))
raise SystemExit(2)
"""


@pytest.fixture
def job(tmp_path, monkeypatch):
    repository = tmp_path / "repository"
    repository.mkdir()
    runtime_config = tmp_path / "runtime.json"
    runtime_config.write_text("{}")
    external = tmp_path / "external.py"
    external.write_text(EXTERNAL)
    state_path = tmp_path / "fake-state.json"
    state_path.write_text(
        json.dumps({"events": [], "changed": [], "ahead": 0, "published": 0, "translations": 0})
    )
    base = [sys.executable, str(external), str(state_path)]
    cfg = {
        "schema_version": scheduled.SCHEMA,
        "repository_root": str(repository),
        "worktree": str(tmp_path / "translation-worktree"),
        "runtime_config": str(runtime_config),
        "task_branch": "translation-job",
        "lock": str(tmp_path / "locks" / "translation.lock"),
        "publisher_command": base + ["publisher"],
        "publication": {
            "owned_paths": ["learning/pairs"],
            "subject": "sync: translations",
        },
        "job": {
            mode + "_command": base + ["policy", mode, "{worktree}", "{scope}"]
            for mode in ("validate", "commit", "recover")
        },
        "selection": {
            "since_date": "2025-01-01",
            "through_date": "2025-01-02",
            "limit_days": 2,
        },
        "timeout_seconds": 20,
    }
    path = tmp_path / "schedule.json"
    path.write_text(json.dumps(cfg))
    monkeypatch.setattr(
        scheduled,
        "translator_command",
        lambda _cfg, root, args: base + ["translator", "--root", root, *args],
    )

    class Job:
        config = cfg
        config_path = path

        def save_config(self):
            path.write_text(json.dumps(cfg))

        def state(self):
            return json.loads(state_path.read_text())

        def set_state(self, **values):
            state = self.state()
            state.update(values)
            state_path.write_text(json.dumps(state))

        def run(self, *args):
            self.save_config()
            return scheduled.main(["--config", str(path), *args])

        def events(self):
            return [event[0] for event in self.state()["events"]]

    return Job()


def test_partial_translation_publishes_completed_files_and_returns_failure(job):
    job.set_state(translation_status=1)
    assert job.run() == 1
    state = job.state()
    assert (
        state["translations"],
        state["published"],
        state["ahead"],
        state["changed"],
    ) == (1, 1, 0, [])
    events = job.events()
    assert (
        events.index("translate")
        < events.index("validate_worktree")
        < events.index("commit_worktree")
        < events.index("publish")
    )


@pytest.mark.parametrize("status", [0, 1, 2])
def test_no_change_run_preserves_translator_status_without_commit(job, status):
    job.set_state(write=False, translation_status=status)
    assert job.run() == status
    assert job.state()["translations"] == 1
    assert job.state()["published"] == 0
    assert "commit_worktree" not in job.events()
    assert "publish" not in job.events()


def test_failed_publication_retries_before_any_new_paid_work(job):
    job.set_state(publish_status=1)
    assert job.run() == 1
    assert job.state()["ahead"] == 1
    assert job.state()["translations"] == 1
    assert job.run() == 1
    assert job.state()["translations"] == 1
    job.set_state(publish_status=0, events=[], write=False)
    assert job.run() == 0
    assert job.events().index("publish") < job.events().index("translate")
    assert job.state()["published"] == 1


def test_interrupted_completed_output_is_published_before_new_work(job):
    job.set_state(changed=["learning/pairs/2025-01-01.md"], write=False)
    assert job.run() == 0
    events = job.events()
    assert (
        events.index("validate_worktree")
        < events.index("commit_worktree")
        < events.index("publish")
        < events.index("translate")
    )
    assert job.state()["published"] == 1


def test_invalid_interrupted_output_is_retained_without_new_model_work(job):
    job.set_state(changed=["learning/pairs/incomplete.md"], validate_status=1)
    assert job.run() == 1
    assert job.run() == 1
    assert job.state()["changed"] == ["learning/pairs/incomplete.md"]
    assert "recover_worktree" not in job.events()
    assert "translate" not in job.events()


def test_missing_recovery_policy_keeps_output_and_stops(job):
    del job.config["job"]["recover_command"]
    job.set_state(changed=["learning/pairs/incomplete.md"], validate_status=1)
    assert job.run() == 1
    assert job.state()["changed"] == ["learning/pairs/incomplete.md"]
    assert "translate" not in job.events()


def test_recovery_rejection_never_discards_foreign_output(job):
    job.set_state(changed=["unowned.md"], validate_status=1, recover_status=1)
    assert job.run() == 1
    assert job.state()["changed"] == ["unowned.md"]
    assert "translate" not in job.events()


def test_stale_previous_commit_is_retained_without_new_model_work(job):
    job.set_state(ahead=1, publish_status=3)
    assert job.run() == 1
    assert job.run() == 1
    assert job.state()["ahead"] == 1
    assert "recover_committed" not in job.events()
    assert "translate" not in job.events()


def test_stale_new_translation_reports_failure_without_second_model_call(job):
    job.set_state(stale_after_translation=True)
    assert job.run() == 1
    assert job.state()["translations"] == 1
    assert "recover_committed" not in job.events()
    assert job.state()["ahead"] == 1
    assert job.run() == 1
    assert job.state()["translations"] == 1


def test_failed_new_validation_preserves_completed_output_for_next_run(job):
    job.set_state(fail_validation_after_translation=True)
    assert job.run() == 1
    assert job.state()["changed"] == ["learning/pairs/2025-01-01.md"]
    assert "recover_worktree" not in job.events()
    assert "publish" not in job.events()


def test_commit_no_difference_cannot_hide_remaining_dirty_output(job):
    job.set_state(commit_status=2)
    assert job.run() == 1
    assert job.state()["changed"]
    assert "publish" not in job.events()


def test_timeout_still_publishes_completed_files_and_reports_timeout(job):
    job.config["timeout_seconds"] = 1
    job.set_state(translation_delay=5)
    assert job.run() == 124
    assert job.state()["published"] == 1
    assert job.state()["translations"] == 1


@pytest.mark.parametrize("mode", ["--doctor", "--dry-run"])
def test_inspection_never_prepares_git_or_creates_lock(job, mode):
    assert job.run(mode) == 0
    assert job.events() == ["translate"]
    assert job.state()["translations"] == 0
    assert not Path(job.config["lock"]).parent.exists()
    assert not Path(job.config["worktree"]).exists()
    args = job.state()["events"][0][1]
    assert mode in args
    assert args[args.index("--root") + 1] == job.config["repository_root"]


def test_inspection_failure_is_not_reported_as_success(job):
    job.set_state(inspection_status=2)
    assert job.run("--doctor") == 2


def test_busy_lock_skips_without_invoking_external_commands(job):
    path = Path(job.config["lock"])
    path.parent.mkdir()
    with path.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert job.run() == 0
    assert job.events() == []


@pytest.mark.parametrize(
    "arguments, expected",
    [
        (
            ["--date", "2025-01-02", "--limit-files", "3"],
            ["--strict", "--date", "2025-01-02", "--limit-days", "3"],
        ),
        (
            ["--days", "7", "--limit-files", "4", "--force"],
            ["--strict", "--days", "7", "--limit-files", "4", "--force"],
        ),
        (
            ["--since-date", "2025-01-02", "--through-date", "2025-01-03"],
            [
                "--strict",
                "--since-date",
                "2025-01-02",
                "--through-date",
                "2025-01-03",
                "--oldest-first",
                "--limit-days",
                "2",
            ],
        ),
    ],
)
def test_selection_compatibility(job, arguments, expected):
    assert job.run("--dry-run", *arguments) == 0
    assert job.state()["events"][0][1][2:] == expected + ["--dry-run"]


@pytest.mark.parametrize(
    "arguments",
    [
        ["--date", "2025-02-30"],
        ["--date", "20250101"],
        ["--days", "0"],
        ["--limit-days", "0"],
        ["--since-date", "2025-01-03"],
    ],
)
def test_invalid_selection_fails_before_any_effect(job, arguments):
    assert job.run(*arguments) == 1
    assert job.events() == []
    assert not Path(job.config["lock"]).parent.exists()


@pytest.mark.parametrize("key", ["repository_root", "runtime_config", "worktree", "lock"])
def test_home_paths_are_resolved_from_private_configuration(job, monkeypatch, key):
    root = job.config_path.parent
    monkeypatch.setenv("HOME", str(root))
    old = Path(job.config[key])
    job.config[key] = "$HOME/" + str(old.relative_to(root))
    job.save_config()
    loaded = scheduled.load_schedule(job.config_path)
    assert loaded[key] == str(old)


@pytest.mark.parametrize("replacement", ["same", "inside", "parent"])
def test_worktree_cannot_cover_or_live_within_repository(job, replacement):
    repository = Path(job.config["repository_root"])
    job.config["worktree"] = str(
        {"same": repository, "inside": repository / "wt", "parent": repository.parent}[replacement]
    )
    assert job.run() == 1
    assert job.events() == []


def test_untrusted_looking_subject_remains_one_literal_argument(job):
    subject = "sync: literal $(no-command) `no-command` ; no-command"
    job.config["publication"]["subject"] = subject
    assert job.run() == 0
    args = next(event[1] for event in job.state()["events"] if event[0] == "publish")
    assert args[args.index("--subject") + 1] == subject
    validate = json.loads(args[args.index("--validate-command") + 1])
    assert validate[-2:] == [job.config["worktree"], "committed"]


def test_configuration_errors_do_not_print_private_values(job, capsys):
    sentinel = "private-value-never-print"
    job.config["publisher_command"] = sentinel
    assert job.run() == 1
    output = capsys.readouterr()
    assert sentinel not in output.out + output.err


def test_configured_interpreter_is_used_for_translation_and_doctor(tmp_path):
    cfg = {
        "environment": {"PROMPT_TRANSLATION_PYTHON": str(tmp_path / "venv/bin/python")},
        "runtime_config": str(tmp_path / "config.json"),
    }
    for args in (["--doctor"], ["--strict"]):
        argv = scheduled.translator_command(cfg, str(tmp_path), args)
        assert argv[0] == cfg["environment"]["PROMPT_TRANSLATION_PYTHON"]
        assert argv[-len(args) :] == args
    cfg["environment"] = {}
    assert scheduled.translator_command(cfg, str(tmp_path), [])[0] == sys.executable


def test_other_environment_variable_names_are_not_misexpanded(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert (
        scheduled.home("$HOME/bin:${HOME}/lib:$HOMELY/bin")
        == f"{tmp_path}/bin:{tmp_path}/lib:$HOMELY/bin"
    )


def test_message_policy_uses_rebased_worktree_and_committed_scope(job):
    job.config["publication"]["message_command"] = [
        sys.executable,
        "synthetic-message.py",
        "{worktree}",
        "{scope}",
    ]
    assert job.run() == 0
    args = next(event[1] for event in job.state()["events"] if event[0] == "publish")
    message = json.loads(args[args.index("--message-command") + 1])
    assert message[-2:] == [job.config["worktree"], "committed"]
