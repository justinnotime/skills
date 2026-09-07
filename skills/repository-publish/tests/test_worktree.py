"""Persistent worktree preparation uses only synthetic local Git repositories."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_publish import PACKAGE, Project, failure, success


@pytest.fixture
def project(tmp_path):
    return Project(tmp_path)


def command(project, action, *arguments, repo=None, entry=None):
    return subprocess.run(
        [
            str(entry or PACKAGE / "scripts/publish"),
            "worktree",
            action,
            "--repo",
            str(repo or project.repo),
            *map(str, arguments),
        ],
        env=project.env,
        capture_output=True,
        text=True,
        check=False,
    )


def prepare(project, target=None, branch="writer/task"):
    target = target or project.base / "dedicated"
    result = command(project, "prepare", "--worktree", target, "--task-branch", branch)
    success(result)
    return target


def test_prepare_keeps_source_clean_and_existing_dirty_worktree_intact(project):
    target = prepare(project)
    assert project.git("branch", "--show-current", root=target) == "writer/task"
    (target / "draft.md").write_text("unfinished\n")
    assert prepare(project) == target
    assert (target / "draft.md").read_text() == "unfinished\n"
    assert project.git("rev-parse", "HEAD") == project.initial
    assert project.git("status", "--porcelain") == ""


def test_prepare_recovers_unpublished_branch_after_directory_loss(project):
    target = prepare(project)
    (target / "draft.md").write_text("completed but unpublished\n")
    project.git("add", "draft.md", root=target)
    project.git("commit", "-m", "pending", root=target)
    pending = project.git("rev-parse", "HEAD", root=target)
    shutil.rmtree(target)
    prepare(project)
    assert project.git("rev-parse", "HEAD", root=target) == pending
    assert (target / "draft.md").read_text() == "completed but unpublished\n"
    assert command(project, "ahead", repo=target).stdout.strip() == "1"


def test_prepare_refuses_unregistered_existing_path_and_source_subdirectory(project):
    target = project.base / "unregistered"
    target.mkdir()
    (target / "keep").write_text("unrelated\n")
    failure(
        command(project, "prepare", "--worktree", target, "--task-branch", "writer/task"),
        "unregistered",
    )
    assert (target / "keep").read_text() == "unrelated\n"
    failure(
        command(
            project,
            "prepare",
            "--worktree",
            project.repo / "nested",
            "--task-branch",
            "writer/task",
        ),
        "outside",
    )
    assert not (project.repo / "nested").exists()


def test_prepare_does_not_move_branch_already_attached_elsewhere(project):
    target = prepare(project)
    original = project.git("rev-parse", "HEAD", root=target)
    failure(
        command(
            project, "prepare", "--worktree", project.base / "other", "--task-branch", "writer/task"
        ),
        "git worktree failed",
    )
    assert project.git("rev-parse", "HEAD", root=target) == original
    failure(
        command(project, "prepare", "--worktree", target, "--task-branch", "different"),
        "unexpected branch",
    )


def test_failed_fetch_does_not_create_worktree(project):
    project.git("remote", "set-url", "origin", str(project.base / "missing.git"))
    target = project.base / "dedicated"
    failure(
        command(project, "prepare", "--worktree", target, "--task-branch", "writer/task"),
        "git fetch failed",
    )
    assert not target.exists()


def test_reset_preserves_dirty_files_and_unpublished_commits(project):
    target = prepare(project)
    (target / "draft.md").write_text("unfinished\n")
    failure(command(project, "reset", "--task-branch", "writer/task", repo=target), "dirty")
    project.git("add", "draft.md", root=target)
    project.git("commit", "-m", "pending", root=target)
    pending = project.git("rev-parse", "HEAD", root=target)
    failure(command(project, "reset", "--task-branch", "writer/task", repo=target), "unpublished")
    assert project.git("rev-parse", "HEAD", root=target) == pending


def test_reset_updates_clean_published_branch_and_refuses_main_or_wrong_branch(project):
    target = prepare(project)
    (project.repo / "upstream.md").write_text("upstream\n")
    project.git("add", "upstream.md")
    project.git("commit", "-m", "upstream")
    project.git("push", "origin", "main")
    success(command(project, "fetch", repo=target))
    failure(command(project, "reset", "--task-branch", "wrong", repo=target), "unexpected branch")
    success(command(project, "reset", "--task-branch", "writer/task", repo=target))
    assert (target / "upstream.md").read_text() == "upstream\n"
    failure(command(project, "reset", "--task-branch", "main"), "linked worktree")


def test_path_lists_preserve_unicode_and_show_both_rename_sides(project):
    target = prepare(project)
    project.git("mv", "outside.md", "renamed 中文.md", root=target)
    result = command(project, "changed", repo=target)
    success(result)
    assert set(result.stdout.splitlines()) == {"outside.md", "renamed 中文.md"}
    project.git("commit", "-m", "rename", root=target)
    result = command(project, "committed", repo=target)
    success(result)
    assert set(result.stdout.splitlines()) == {"outside.md", "renamed 中文.md"}


def test_line_path_output_rejects_newlines_before_partial_output(project):
    target = prepare(project)
    (target / "a-normal.md").write_text("normal")
    (target / "b-new\nline.md").write_text("newline")
    result = command(project, "changed", repo=target)
    failure(result, "use --null")
    assert result.stdout == ""
    result = command(project, "changed", "--null", repo=target)
    success(result)
    assert set(result.stdout.split("\0")) == {"a-normal.md", "b-new\nline.md", ""}


@pytest.mark.parametrize("exit_code", [0, 7])
def test_run_at_ref_executes_selected_revision_and_cleans_up(project, exit_code):
    scratch = project.base / "inspections"
    code = "import json,os,sys; from pathlib import Path; print(json.dumps({'content':Path('policy.txt').read_text(),'root':str(Path.cwd()),'env':os.environ['REPOSITORY_PUBLISH_WORKTREE']})); Path('temporary').write_text('test'); sys.exit(int(sys.argv[1]))"
    result = command(
        project,
        "run-at-ref",
        "--ref",
        project.initial,
        "--scratch",
        scratch,
        "--",
        sys.executable,
        "-c",
        code,
        str(exit_code),
    )
    assert result.returncode == exit_code, result.stderr
    value = json.loads(result.stdout)
    assert value["content"] == "v1\n" and value["root"] == value["env"]
    assert not Path(value["root"]).exists()
    assert not list(scratch.iterdir())
    assert project.git("worktree", "list", "--porcelain").count("worktree ") == 1
    assert project.git("status", "--porcelain") == ""


def test_run_at_ref_invalid_revision_or_checkout_scratch_has_no_effect(project):
    failure(
        command(
            project,
            "run-at-ref",
            "--ref",
            "absent",
            "--",
            sys.executable,
            "-c",
            "raise AssertionError()",
        ),
        "git rev-parse failed",
    )
    failure(
        command(
            project,
            "run-at-ref",
            "--scratch",
            project.repo / "nested",
            "--",
            sys.executable,
            "-c",
            "raise AssertionError()",
        ),
        "outside",
    )
    assert project.git("worktree", "list", "--porcelain").count("worktree ") == 1


def test_copied_public_package_prepares_without_private_repository(project):
    copied = project.base / "standalone"
    shutil.copytree(
        PACKAGE,
        copied,
        ignore=shutil.ignore_patterns(".venv", "__pycache__", ".pytest_cache", ".ruff_cache"),
    )
    target = project.base / "detached-consumer"
    result = command(
        project,
        "prepare",
        "--worktree",
        target,
        "--task-branch",
        "sample/task",
        entry=copied / "scripts/publish",
    )
    success(result)
    assert (target / "archive/seed.md").read_text() == "seed\n"


def test_task_branch_cannot_be_interpreted_as_a_git_option(project):
    failure(
        command(project, "prepare", "--worktree", project.base / "bad", "--task-branch=-force"),
        "cannot be an option",
    )


def commit(
    project,
    target,
    *,
    selected="archive",
    validation=None,
    message=None,
    source=None,
    branch="writer/task",
):
    return command(
        project,
        "commit",
        "--source-repository",
        source or project.repo,
        "--task-branch",
        branch,
        "--paths",
        selected,
        "--validate-command",
        json.dumps(validation or [sys.executable, "-c", "pass"]),
        "--message-command",
        json.dumps(message or [sys.executable, "-c", "print('update: generated')"]),
        repo=target,
    )


def test_commit_stages_exact_paths_and_runs_private_policy(project):
    target = prepare(project)
    (target / "archive/with spaces.md").write_text("paid result\n")
    (target / "archive/seed.md").unlink()
    check = [
        sys.executable,
        "-c",
        (
            "import os,subprocess; from pathlib import Path; "
            "assert Path(os.environ['REPOSITORY_PUBLISH_REPOSITORY']).name=='checkout'; "
            "assert subprocess.check_output(['git','diff','--cached','--name-only']).strip(); "
            "assert not subprocess.check_output(['git','diff','--name-only']).strip()"
        ),
    ]
    result = commit(
        project,
        target,
        validation=check,
        message=[sys.executable, "-c", "print('update: generated\\n\\nCaller: synthetic')"],
    )
    success(result)
    assert project.git("log", "-1", "--format=%B", root=target).endswith("Caller: synthetic")
    assert project.git("status", "--porcelain", root=target) == ""
    assert project.git("show", "HEAD:archive/with spaces.md", root=target) == "paid result"
    assert project.git("rev-parse", "origin/main", root=target) == project.initial
    assert commit(project, target).returncode == 2


@pytest.mark.parametrize(
    "problem",
    [
        "main",
        "wrong-branch",
        "other-repository",
        "unowned",
        "untracked",
        "rename",
        "validation",
        "message",
        "hook",
    ],
)
def test_commit_failure_preserves_files_and_revision(project, problem):
    target = prepare(project)
    output = target / "archive/result.md"
    output.write_text("paid result\n")
    options = {}
    expected = ""
    if problem == "main":
        target = project.repo
        expected = "linked worktree"
    elif problem == "wrong-branch":
        options["branch"] = "wrong"
        expected = "unexpected branch"
    elif problem == "other-repository":
        other = project.base / "unrelated"
        project.run(["git", "init", str(other)])
        options["source"] = other
        expected = "another repository"
    elif problem in {"unowned", "untracked"}:
        (target / ("outside.md" if problem == "unowned" else "other-draft.md")).write_text("keep\n")
        expected = "outside its ownership"
    elif problem == "rename":
        project.git("mv", "outside.md", "archive/moved.md", root=target)
        expected = "outside its ownership"
    elif problem == "validation":
        options["validation"] = [sys.executable, "-c", "raise SystemExit(7)"]
        expected = "policy command failed"
    elif problem == "message":
        options["message"] = [sys.executable, "-c", "pass"]
        expected = "empty commit message"
    else:
        hook = project.repo / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n")
        hook.chmod(0o755)
        expected = "commit failed"
    before = project.git("rev-parse", "HEAD", root=target)
    failure(commit(project, target, **options), expected)
    assert output.read_text() == "paid result\n"
    assert project.git("rev-parse", "HEAD", root=target) == before
    assert project.git("rev-parse", "origin/main") == project.initial


def test_commit_rejects_policy_that_changes_staged_content(project):
    target = prepare(project)
    (target / "archive/result.md").write_text("original paid result\n")
    validation = [
        sys.executable,
        "-c",
        ("from pathlib import Path; Path('archive/result.md').write_text('changed by policy\\n')"),
    ]
    failure(commit(project, target, validation=validation), "modified tracked repository content")
    assert project.git("rev-parse", "HEAD", root=target) == project.initial
    assert project.git("show", ":archive/result.md", root=target) == "original paid result"


@pytest.mark.parametrize("callback", ["validation", "message"])
def test_commit_rejects_callback_staging_an_unowned_file(project, callback):
    target = prepare(project)
    (target / "archive/result.md").write_text("paid result\n")
    options = {
        callback: [
            sys.executable,
            "-c",
            (
                "import subprocess; from pathlib import Path; "
                "Path('outside.md').write_text('unowned callback change\\n'); "
                "subprocess.run(['git','add','--','outside.md'], check=True); "
                "print('update: generated')"
            ),
        ]
    }
    failure(commit(project, target, **options), "modified tracked repository content")
    assert project.git("rev-parse", "HEAD", root=target) == project.initial
    assert project.git("show", ":archive/result.md", root=target) == "paid result"
    assert project.git("rev-parse", "origin/main") == project.initial
