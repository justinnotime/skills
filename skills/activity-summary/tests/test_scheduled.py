import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from conftest import synthetic_config

from activity_summary import scheduled
from activity_summary.config import activate, load

MODEL = r"""
import json, os, sys
from pathlib import Path
log = Path(os.environ['CALL_LOG'])
with log.open('a') as stream:
    stream.write(json.dumps({'argv': sys.argv[1:], 'unexpected_env': 'UNEXPECTED_CREDENTIAL' in os.environ}) + '\n')
if '--auth' in sys.argv:
    print(json.dumps({'loggedIn': True}))
    raise SystemExit(0)
request = sys.stdin.read()
fields = dict(line.split('=', 1) for line in request.splitlines() if '=' in line and line.split('=', 1)[0] in {'target','start','hash','relative','missing'})
if fields['target'] == os.environ.get('FAIL_TARGET'):
    print('synthetic confidential response', file=sys.stderr)
    raise SystemExit(7)
target, start, digest = fields['target'], fields['start'], fields['hash']
if '/weekly/' in fields['relative']:
    text = f'''---
title: Weekly summary {start}..{target}
type: summary
created: {target}
updated: 2000-01-01T00:00:00Z
week: {start}..{target}
generator: weekly-summary
inputs_sha256: {digest}
missing_inputs: [{fields['missing']}]
sources: daily summaries
---

# Weekly summary

## Headlines

Synthetic events. Missing inputs: {fields['missing']}

## Projects

Synthetic progress.

## Commentary

Keep working on the selected source records.
'''
else:
    text = f'''---
title: Daily summary {target}
date: {target}
created: {target}
type: summary
window: {start}..{target}
generator: daily-summary
facts_sha256: {digest}
sources: deterministic activity facts and referenced local mirrors
updated: 2000-01-01T00:00:00Z
---

# {target}

## Facts

### PRs / Issues

### Agent work

## Projects

''' + 'Synthetic evidence supports this observation. ' * 25 + '''

## Commentary

Open item: complete the selected verification.

## Next

Review recorded facts.
'''
print(json.dumps({'structured_output': {'markdown': text}}))
"""


class Harness:
    def __init__(self, tmp_path, monkeypatch):
        self.root = tmp_path / "repository"
        self.root.mkdir()
        (self.root / ".git").mkdir()
        self.wt = tmp_path / "worktree"
        self.wt.mkdir()
        self.calls = tmp_path / "model.calls"
        self.model = tmp_path / "model.py"
        self.model.write_text(MODEL)
        self.template = tmp_path / "prompt.txt"
        self.template.write_text(
            "target={{target}}\nstart={{start}}\nhash={{input_hash}}\nrelative={{relative}}\nmissing={{missing_csv}}\n{{inputs}}"
        )
        cfg = synthetic_config(self.root)
        cfg["environment"] = {}
        cfg["publisher_command"] = [sys.executable, "synthetic-publisher.py"]
        for kind in ("daily", "weekly"):
            cfg[kind]["prompt_template"] = str(self.template)
            cfg[kind]["wait_inputs_seconds"] = 0
            cfg[kind]["schedule"] = {
                "worktree": str(self.wt),
                "task_branch": "summary-task",
                "lock": str(tmp_path / "summary.lock"),
                "model_command": [
                    sys.executable,
                    str(self.model),
                    "--no-session-persistence",
                    "--permission-mode",
                    "dontAsk",
                    "--tools",
                    "Read,Grep,Glob",
                    "--max-budget-usd",
                    "0.01",
                ],
                "auth_command": [sys.executable, str(self.model), "--auth"],
                "environment": {
                    "HOME": str(tmp_path),
                    "PATH": os.defpath,
                    "CALL_LOG": str(self.calls),
                },
                "timeout_seconds": 10,
                "publication": {
                    "owned_paths": ["summaries"],
                    "subject": "update synthetic summary",
                    "agent": "synthetic",
                },
                "policy": {
                    mode + "_command": [sys.executable, "synthetic-policy.py", mode]
                    for mode in ("validate", "commit", "message", "recover")
                },
            }
        self.path = tmp_path / "config.json"
        self.raw = cfg
        self.save()
        self.pending = []
        self.base = {}
        self.published = {}
        self.events = []
        self.fail_publish = False
        self.drift_once = False
        self.revision = 0
        monkeypatch.setenv("UNEXPECTED_CREDENTIAL", "synthetic-marker")
        monkeypatch.setattr(scheduled, "publisher", self.publisher)
        monkeypatch.setattr(scheduled, "policy", self.policy)
        monkeypatch.setattr(
            scheduled.facts,
            "extract",
            lambda target, *_: {
                "date": target,
                "revision": self.revision,
                "gh_touched_today": {},
                "session_clusters": [],
            },
        )

    def save(self):
        self.path.write_text(json.dumps(self.raw))

    def snapshot(self):
        return {str(path.relative_to(self.wt)): path.read_bytes() for path in self.wt.rglob("*.md")}

    def changed(self):
        current = self.snapshot()
        return sorted(
            path
            for path in self.base.keys() | current.keys()
            if self.base.get(path) != current.get(path)
        )

    def publisher(self, cfg, kind, *args, capture=False):
        output = b""
        code = 0
        if args[0] == "worktree":
            action = args[1]
            self.events.append(action)
            if action in {"changed", "committed"}:
                paths = self.changed() if action == "changed" else self.pending
                output = b"".join(path.encode() + b"\0" for path in paths)
            elif action == "ahead":
                output = ("1" if self.pending else "0").encode()
        else:
            self.events.append("publish")
            if self.drift_once:
                self.drift_once = False
                self.revision += 1
                code = 3
            elif self.fail_publish:
                code = 1
            elif not scheduled.content_valid(cfg, kind, "committed"):
                code = 3
            else:
                self.published = self.snapshot()
                self.pending = []
        return subprocess.CompletedProcess(args, code, output, b"")

    def policy(self, cfg, kind, mode, scope="worktree"):
        self.events.append(mode + ":" + scope)
        if mode == "commit":
            self.pending = sorted(set(self.pending) | set(self.changed()))
            self.base = self.snapshot()
        if mode == "recover":
            for path in self.wt.rglob("*.md"):
                path.unlink()
            for name, data in self.published.items():
                path = self.wt / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            self.base = self.snapshot()
            self.pending = []
        return 0

    def invoke(self, kind="daily", *arguments):
        return scheduled.main([kind, "--config", str(self.path), *arguments])

    def model_calls(self):
        return (
            [json.loads(line) for line in self.calls.read_text().splitlines()]
            if self.calls.exists()
            else []
        )


@pytest.fixture
def harness(tmp_path, monkeypatch):
    return Harness(tmp_path, monkeypatch)


def test_doctor_and_dry_run_do_not_create_lock_call_account_or_publisher(harness):
    before = sorted(str(path.relative_to(harness.root)) for path in harness.root.rglob("*"))
    assert harness.invoke("daily", "--doctor") == 0
    assert harness.invoke("daily", "--dry-run", "--target", "2024-01-02") == 0
    assert harness.model_calls() == []
    assert harness.events == []
    assert not Path(harness.raw["daily"]["schedule"]["lock"]).exists()
    assert sorted(str(path.relative_to(harness.root)) for path in harness.root.rglob("*")) == before


def test_generate_publish_and_hash_reuse_preserve_readonly_budget_flags(harness):
    assert harness.invoke("daily", "--target", "2024-01-02") == 0
    assert list(harness.published) == ["summaries/2024-01-02.md"]
    assert harness.invoke("daily", "--target", "2024-01-02") == 0
    calls = harness.model_calls()
    assert len(calls) == 2
    assert calls[0]["argv"] == ["--auth"]
    assert calls[1]["argv"] == harness.raw["daily"]["schedule"]["model_command"][2:]
    assert all(not item["unexpected_env"] for item in calls)


def test_push_failure_keeps_complete_output_and_retry_only_publishes(harness):
    harness.fail_publish = True
    assert harness.invoke("daily", "--target", "2024-01-02") == 1
    paid = harness.snapshot()
    assert harness.pending == ["summaries/2024-01-02.md"]
    harness.fail_publish = False
    assert harness.invoke("daily", "--target", "2024-01-02") == 0
    assert harness.published == paid
    assert len(harness.model_calls()) == 2


def test_partial_later_day_failure_retains_earlier_publication(harness, monkeypatch, capsys):
    harness.raw["daily"]["schedule"]["environment"]["FAIL_TARGET"] = "2024-01-03"
    harness.save()
    monkeypatch.setattr(scheduled, "daily_targets", lambda *_: ["2024-01-02", "2024-01-03"])
    assert harness.invoke() == 1
    assert list(harness.published) == ["summaries/2024-01-02.md"]
    assert "synthetic confidential response" not in capsys.readouterr().err


def test_source_drift_preserves_paid_output_and_stops_before_new_model_work(harness, monkeypatch):
    harness.drift_once = True
    monkeypatch.setattr(scheduled, "daily_targets", lambda *_: ["2024-01-02", "2024-01-03"])
    assert harness.invoke() == 1
    candidate = harness.snapshot()
    assert list(candidate) == ["summaries/2024-01-02.md"]
    assert harness.published == {}
    assert not any(event.startswith("recover") for event in harness.events)
    calls = len(harness.model_calls())
    assert calls == 2
    assert harness.invoke() == 1
    assert harness.snapshot() == candidate
    assert len(harness.model_calls()) == calls


def test_existing_dirty_complete_result_is_published_before_any_model(harness):
    assert harness.invoke("daily", "--target", "2024-01-02") == 0
    harness.base = {}
    harness.published = {}
    assert harness.invoke("daily", "--target", "2024-01-02") == 0
    assert len(harness.model_calls()) == 2
    assert list(harness.published) == ["summaries/2024-01-02.md"]


def test_private_validation_failure_never_discards_paid_output(harness, monkeypatch):
    previous = harness.policy
    monkeypatch.setattr(
        scheduled,
        "policy",
        lambda cfg, kind, mode, scope="worktree": (
            1 if mode == "validate" else previous(cfg, kind, mode, scope)
        ),
    )
    assert harness.invoke("daily", "--target", "2024-01-02") == 1
    assert list(harness.snapshot()) == ["summaries/2024-01-02.md"]
    assert not any(event.startswith("recover") for event in harness.events)


def test_weekly_inputs_are_legacy_exact_bytes_and_order_with_declared_gaps(harness):
    directory = harness.wt / "summaries"
    directory.mkdir()
    (directory / "2024-01-02.md").write_bytes(b"second without newline")
    (directory / "2024-01-01.md").write_bytes("first: multilingual source\n".encode())
    cfg = load(harness.path)
    blob, present, missing = scheduled.weekly_inputs(cfg, harness.wt, "2024-01-07")
    assert (
        blob
        == b"\n===== DAILY 2024-01-01 (summaries/2024-01-01.md) =====\n\nfirst: multilingual source\n\n===== DAILY 2024-01-02 (summaries/2024-01-02.md) =====\n\nsecond without newline"
    )
    assert present == ["2024-01-01", "2024-01-02"]
    assert missing == ["2024-01-03", "2024-01-04", "2024-01-05", "2024-01-06", "2024-01-07"]


def test_weekly_generation_hash_reuse_and_push_retry(harness):
    source = harness.wt / "summaries/2024-01-01.md"
    source.parent.mkdir()
    source.write_text("# Synthetic daily\n")
    harness.base = harness.snapshot()
    harness.published = harness.snapshot()
    harness.fail_publish = True
    assert harness.invoke("weekly", "--end", "2024-01-07") == 1
    harness.fail_publish = False
    assert harness.invoke("weekly", "--end", "2024-01-07") == 0
    assert "summaries/weekly/2024-01-07.md" in harness.published
    assert len(harness.model_calls()) == 2


def test_weekly_empty_inputs_fail_without_auth_or_model(harness):
    assert harness.invoke("weekly", "--end", "2024-01-07") == 1
    assert harness.model_calls() == []


def test_planning_caps_candidates_before_hash_skip_and_preserves_legacy(harness):
    cfg = load(harness.path)
    activate(cfg)
    cfg["daily"]["selection"] = {"lookback_days": 5, "repair_days": 3, "max_dates": 3}
    directory = harness.root / "summaries"
    directory.mkdir()
    (directory / "2024-01-02.md").write_text("legacy output without hash")
    (directory / "2024-01-03.md").write_text("facts_sha256: " + "a" * 64 + "\n")
    args = argparse.Namespace(target=None, max_dates=None, force=False)
    assert scheduled.daily_targets(cfg, harness.root, args, date(2024, 1, 6)) == [
        "2024-01-01",
        "2024-01-03",
        "2024-01-04",
    ]
    args.force = True
    with pytest.raises(scheduled.ScheduleError, match="force_requires_target"):
        scheduled.daily_targets(cfg, harness.root, args, date(2024, 1, 6))


def test_request_substitutes_once_and_never_interprets_source_placeholders(harness):
    cfg = load(harness.path)
    request = scheduled.request_text(cfg, "daily", harness.root, "2024-01-02", b"{{target}}", [])
    assert request.endswith("{{target}}")
    assert "hash=" + hashlib.sha256(b"{{target}}").hexdigest() in request


def test_unowned_dirty_file_prevents_generation_and_recovery(harness):
    (harness.wt / "other.md").write_text("unrelated user edit")
    assert harness.invoke("daily", "--target", "2024-01-02") == 1
    assert harness.model_calls() == []
    assert (harness.wt / "other.md").read_text() == "unrelated user edit"


def test_weekly_wait_refreshes_inputs_before_hashing(harness, monkeypatch):
    harness.raw["weekly"]["wait_inputs_seconds"] = 60
    harness.save()
    times = iter([0, 0, 0, 60])
    monkeypatch.setattr(scheduled.time, "monotonic", lambda: next(times))

    def arrive(_):
        directory = harness.wt / "summaries"
        directory.mkdir()
        (directory / "2024-01-01.md").write_text("arrived input")
        harness.base = harness.snapshot()

    monkeypatch.setattr(scheduled.time, "sleep", arrive)
    assert harness.invoke("weekly", "--end", "2024-01-07") == 0
    assert "fetch" in harness.events
    assert "summaries/weekly/2024-01-07.md" in harness.published


@pytest.mark.parametrize("structured", [None, [], "unsupported"])
def test_null_or_nonobject_structured_output_uses_valid_result(harness, structured):
    harness.model.write_text(
        MODEL.replace(
            "print(json.dumps({'structured_output': {'markdown': text}}))",
            f"print(json.dumps({{'structured_output': {structured!r}, 'result': json.dumps({{'markdown': text}})}}))",
        )
    )
    assert harness.invoke("daily", "--target", "2024-01-02") == 0
    assert list(harness.published) == ["summaries/2024-01-02.md"]


def test_error_response_never_installs_even_with_markdown(harness):
    harness.model.write_text(
        MODEL.replace(
            "print(json.dumps({'structured_output': {'markdown': text}}))",
            "print(json.dumps({'is_error': True, 'structured_output': {'markdown': text}}))",
        )
    )
    assert harness.invoke("daily", "--target", "2024-01-02") == 1
    assert harness.snapshot() == {}


def test_model_timeout_terminates_process_group_without_install(harness, monkeypatch):
    class TimedOut:
        pid = 12345
        returncode = 0

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def communicate(self, data=None, timeout=None):
            if data is not None:
                raise subprocess.TimeoutExpired("synthetic", timeout)
            return b"", b""

    cfg = load(harness.path)
    signals = []
    monkeypatch.setattr(scheduled.subprocess, "Popen", lambda *a, **kw: TimedOut())
    monkeypatch.setattr(scheduled.os, "killpg", lambda pid, signal: signals.append((pid, signal)))
    with pytest.raises(scheduled.ScheduleError, match="model_timeout"):
        scheduled.model_response(cfg, "daily", "synthetic prompt")
    assert len(signals) == 1
    assert harness.snapshot() == {}


def test_failure_artifacts_only_use_explicit_private_directory(harness, tmp_path):
    directory = tmp_path / "private-failures"
    harness.raw["daily"]["schedule"]["failure_directory"] = str(directory)
    harness.raw["daily"]["schedule"]["environment"]["FAIL_TARGET"] = "2024-01-02"
    harness.save()
    assert harness.invoke("daily", "--doctor") == 0
    assert not directory.exists()
    assert harness.invoke("daily", "--target", "2024-01-02") == 1
    artifacts = list(directory.iterdir())
    assert len(artifacts) == 1
    assert artifacts[0].stat().st_mode & 0o777 == 0o600
    assert harness.snapshot() == {}


@pytest.mark.parametrize(
    "kind,arguments",
    [
        ("daily", ["--end", "2024-01-02"]),
        ("weekly", ["--target", "2024-01-02"]),
        ("daily", ["--max-dates", "0"]),
        ("daily", ["--force"]),
        ("daily", ["--target", "2999-01-01"]),
    ],
)
def test_invalid_selection_fails_before_lock_or_worktree_changes(harness, kind, arguments):
    assert harness.invoke(kind, *arguments) == 1
    assert harness.events == []
    assert harness.model_calls() == []
    assert not Path(harness.raw[kind]["schedule"]["lock"]).exists()
