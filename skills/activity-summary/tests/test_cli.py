import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import synthetic_config

from activity_summary.config import activate, load
from activity_summary.scheduled import source_input

PACKAGE = Path(__file__).resolve().parents[1]


def invoke(entry, *args, env=None):
    environment = dict(os.environ, ACTIVITY_SUMMARY_PYTHON=sys.executable)
    if env:
        environment.update(env)
    return subprocess.run(
        [str(PACKAGE / "scripts" / entry), *map(str, args)],
        env=environment,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    "entry",
    [
        "extract-facts",
        "eval-facts",
        "render-issue-section",
        "validate-daily",
        "validate-weekly",
        "run-daily",
        "run-weekly",
    ],
)
def test_all_entrypoints_are_executable(entry):
    result = invoke(entry, "--help")
    assert result.returncode == 0, result.stderr.decode()


def test_extract_real_local_git_is_repeatable_and_writes_nothing(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    env = dict(
        os.environ,
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Synthetic",
        GIT_AUTHOR_EMAIL="synthetic@example.invalid",
        GIT_COMMITTER_NAME="Synthetic",
        GIT_COMMITTER_EMAIL="synthetic@example.invalid",
        GIT_AUTHOR_DATE="2024-01-02T10:00:00Z",
        GIT_COMMITTER_DATE="2024-01-02T10:00:00Z",
    )
    subprocess.run(["git", "init", "-q", str(root)], check=True, env=env)
    path = root / "sources/issues/example-org_alpha/7.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nrepo: example-org/alpha\nnumber: 7\ncreated: '2024-01-02T09:00:00Z'\ntitle: Synthetic issue\nurl: https://github.com/example-org/alpha/issues/7\ntype: gh-issue\n---\n"
    )
    history = root / "sources/selected-history/2024-01-02_session.md"
    history.parent.mkdir(parents=True)
    history.write_text(
        "# Synthetic work\n\n"
        "- Session ID: `01234567-89ab-cdef-0123-456789abcdef`\n\n"
        "### 2024-01-02 10:00:00Z -- user\n\n> Review the synthetic change.\n\n"
        "### 2024-01-02 10:01:00Z -- assistant\n\nSynthetic response.\n"
    )
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, env=env)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-qm", "sync: synthetic source"], check=True, env=env
    )
    cfg = tmp_path / "config.json"
    selected = synthetic_config(root)
    selected["facts"]["session_sources"] = [
        {"directory": "sources/selected-history", "label": "selected", "format": "history"}
    ]
    cfg.write_text(json.dumps(selected))
    before = {
        str(item.relative_to(root)): hashlib.sha256(item.read_bytes()).hexdigest()
        for item in root.rglob("*")
        if item.is_file()
    }
    first = invoke("extract-facts", "2024-01-02", "--config", cfg)
    second = invoke("extract-facts", "2024-01-02", "--config", cfg)
    assert first.returncode == second.returncode == 0, first.stderr.decode()
    assert first.stdout == second.stdout
    data = json.loads(first.stdout)
    assert list(data["gh_touched_today"]) == ["example-org/alpha#7"]
    loaded = load(cfg)
    activate(loaded)
    scheduled_blob, scheduled_data, missing = source_input(loaded, "daily", root, "2024-01-02")
    assert scheduled_data["human_cluster_count"] == 1
    cluster = scheduled_data["session_clusters"][0]
    assert cluster["user_prompts"] == ["Review the synthetic change."]
    assert [session["source"] for session in cluster["sessions"]] == ["selected"]
    assert missing == []
    assert first.stdout == scheduled_blob
    after = {
        str(item.relative_to(root)): hashlib.sha256(item.read_bytes()).hexdigest()
        for item in root.rglob("*")
        if item.is_file()
    }
    assert before == after
    facts = tmp_path / "facts.json"
    facts.write_bytes(first.stdout)
    rendered = invoke("render-issue-section", facts, root, "--config", cfg)
    assert rendered.returncode == 0, rendered.stderr.decode()
    assert b"alpha#7" in rendered.stdout
    summary = tmp_path / "summary.md"
    summary.write_bytes(rendered.stdout)
    evaluated = invoke("eval-facts", summary, facts, "--config", cfg)
    assert evaluated.returncode == 0, evaluated.stderr.decode()
    assert json.loads(evaluated.stdout)["issue_recall"] == 1.0
