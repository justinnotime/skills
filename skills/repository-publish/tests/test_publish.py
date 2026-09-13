from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "repository_publish", PACKAGE / "src/repository_publish.py"
)
rp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rp)


class Project:
    def __init__(self, base):
        self.base = base
        self.home = base / "home"
        self.home.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(
            HOME=str(self.home),
            XDG_CONFIG_HOME=str(self.home / "config"),
            XDG_STATE_HOME=str(self.home / "state"),
            XDG_CACHE_HOME=str(self.home / "cache"),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
            REPOSITORY_PUBLISH_PYTHON=sys.executable,
        )
        self.remote = base / "remote.git"
        self.repo = base / "checkout"
        self.state = base / "progress"
        self.state.mkdir()
        self.scratch = base / "scratch"
        self.run(["git", "init", "--bare", "--initial-branch=main", str(self.remote)])
        self.run(["git", "init", "--initial-branch=main", str(self.repo)])
        self.git("config", "user.name", "Example Writer")
        self.git("config", "user.email", "writer@example.invalid")
        self.git("config", "commit.gpgsign", "false")
        (self.repo / "archive").mkdir()
        (self.repo / "archive/seed.md").write_text("seed\n")
        (self.repo / "outside.md").write_text("outside\n")
        (self.repo / "policy.txt").write_text("v1\n")
        self.git("add", ".")
        self.git("commit", "-m", "seed")
        self.git("remote", "add", "origin", str(self.remote))
        self.git("push", "origin", "main")
        self.initial = self.git("rev-parse", "HEAD")

    def run(self, argv, **kw):
        result = subprocess.run(
            argv, env=self.env, capture_output=True, text=True, check=False, **kw
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout.strip()

    def git(self, *args, root=None):
        return self.run(["git", "-C", str(root or self.repo), *args])

    def writer(self, body, name="writer.py"):
        path = self.base / name
        path.write_text(
            "import os, sys\nfrom pathlib import Path\n"
            "root=Path(os.environ['REPOSITORY_PUBLISH_WORKTREE'])\n"
            "state=Path(os.environ['SYNC_STATE_DIR'])\n" + body
        )
        return [sys.executable, str(path)]

    def publish(self, writer=None, *, options=(), entry=None):
        argv = [
            str(entry or PACKAGE / "scripts/publish"),
            "--repo",
            str(self.repo),
            "--task",
            "messages",
            "--paths",
            "archive",
            "--subject",
            "sync: messages",
            "--state-dir",
            str(self.state),
            "--lock",
            str(self.base / "task.lock"),
            "--scratch",
            str(self.scratch),
            "--publish-lock",
            str(self.base / "publish.lock"),
            "--attempts",
            "2",
            "--retry-delay",
            "0",
            *options,
        ]
        if writer:
            argv += ["--", *writer]
        return subprocess.run(argv, env=self.env, capture_output=True, text=True, check=False)

    def counter_writer(self):
        return self.writer(
            "n=int((state/'count').read_text()) if (state/'count').exists() else 0\n"
            "(root/'archive'/f'message-{n+1}.md').write_text(f'message {n+1}\\n')\n"
            "(state/'count').write_text(str(n+1))\n"
        )

    def assert_clean(self):
        assert self.git("status", "--porcelain") == ""
        assert self.git("rev-parse", "HEAD") == self.initial
        assert self.git("worktree", "list", "--porcelain").count("worktree ") == 1
        assert self.git("branch", "--list", "publish/*") == ""
        assert not list(self.scratch.glob("publish-*"))


@pytest.fixture
def project(tmp_path):
    return Project(tmp_path)


def success(result):
    assert result.returncode == 0, result.stdout + result.stderr


def failure(result, fragment):
    assert result.returncode != 0, result.stdout + result.stderr
    assert fragment in result.stderr, result.stdout + result.stderr


def test_publish_and_recovery_leave_main_clean(project):
    writer = project.counter_writer()
    success(project.publish(writer))
    assert (project.state / "count").read_text() == "1"
    assert project.git("show", "main:archive/message-1.md", root=project.remote) == "message 1"
    hook = project.remote / "hooks/pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    failure(project.publish(writer), "publication failed")
    assert (project.state / "count").read_text() == "1"
    hook.unlink()
    success(project.publish(writer))
    assert (project.state / "count").read_text() == "2"
    assert project.git("show", "main:archive/message-2.md", root=project.remote) == "message 2"
    project.assert_clean()


def test_no_content_advances_only_state(project):
    success(project.publish(project.writer("(state/'count').write_text('9')\n")))
    assert (project.state / "count").read_text() == "9"
    assert project.git("rev-parse", "main", root=project.remote) == project.initial
    project.assert_clean()


def test_writer_failure_keeps_previous_state(project):
    (project.state / "count").write_text("1")
    failure(
        project.publish(project.writer("(state/'count').write_text('99')\nsys.exit(8)\n")),
        "writer exited 8",
    )
    assert (project.state / "count").read_text() == "1"
    project.assert_clean()


@pytest.mark.parametrize(
    "body",
    [
        "(root/'outside.md').write_text('changed')\n",
        "(root/'outside.md').rename(root/'archive/moved.md')\n",
        "import subprocess\nsubprocess.run(['git','mv','archive/seed.md','outside2.md'],check=True)\n",
    ],
)
def test_tracked_ownership_and_renames(project, body):
    failure(project.publish(project.writer(body)), "outside its ownership")
    assert not list(project.state.iterdir())
    project.assert_clean()


def test_similar_prefix_is_not_owned(project):
    writer = project.writer("(root/'archive/seed.md').write_text('changed')\n")
    failure(
        project.publish(
            writer,
            options=(
                "--paths",
                "arch",
            ),
        ),
        "outside its ownership",
    )


def test_unicode_spaces_and_untracked_scratch(project):
    body = "(root/'archive/中文 file.md').write_text('content')\n(root/'scratch.tmp').write_text('scratch')\n"
    success(project.publish(project.writer(body)))
    assert project.git("show", "main:archive/中文 file.md", root=project.remote) == "content"
    assert "scratch.tmp" not in project.git("ls-tree", "--name-only", "main", root=project.remote)
    project.assert_clean()


def test_writer_cannot_create_own_commit(project):
    body = "import subprocess\n(root/'archive/a.md').write_text('a')\n"
    body += "subprocess.run(['git','add','.'],check=True)\nsubprocess.run(['git','commit','-m','unexpected'],check=True)\n"
    failure(project.publish(project.writer(body)), "must not create commits")


def test_task_lock_skips_without_running_writer(project):
    with (project.base / "task.lock").open("w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        result = project.publish(project.writer("sys.exit(45)\n"))
        success(result)
        assert "another writer" in result.stdout
    assert not list(project.state.iterdir())


def test_publish_lock_failure_keeps_state(project):
    with (project.base / "publish.lock").open("w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        failure(
            project.publish(project.counter_writer(), options=("--lock-timeout", "0")),
            "publication lock",
        )
    assert not list(project.state.iterdir())


def test_policy_failure_prevents_commit(project):
    validate = [sys.executable, "-c", "raise SystemExit(17)"]
    failure(
        project.publish(
            project.counter_writer(), options=("--validate-command", json.dumps(validate))
        ),
        "policy command failed",
    )
    assert project.git("rev-parse", "main", root=project.remote) == project.initial
    assert not list(project.state.iterdir())


def test_policy_message_uses_private_settings(project):
    message = [
        sys.executable,
        "-c",
        "import os; print(os.environ['REPOSITORY_PUBLISH_SUBJECT']+'\\n\\nPolicy: checked')",
    ]
    success(
        project.publish(
            project.counter_writer(), options=("--message-command", json.dumps(message))
        )
    )
    assert "Policy: checked" in project.git("log", "-1", "--format=%B", root=project.remote)


def test_mutating_policy_is_refused(project):
    validate = [
        sys.executable,
        "-c",
        "from pathlib import Path; Path('outside.md').write_text('changed')",
    ]
    failure(
        project.publish(
            project.counter_writer(), options=("--validate-command", json.dumps(validate))
        ),
        "policy command modified tracked",
    )
    assert not list(project.state.iterdir())


def test_message_policy_refreshes_after_rebase(project):
    other = project.base / "other"
    project.run(["git", "clone", str(project.remote), str(other)])
    project.git("config", "user.name", "Other", root=other)
    project.git("config", "user.email", "other@example.invalid", root=other)
    marker = project.base / "message.marker"
    message = project.base / "message.py"
    message.write_text(
        "import pathlib, subprocess\n"
        f"marker=pathlib.Path({str(marker)!r})\nother=pathlib.Path({str(other)!r})\n"
        "print('sync: messages\\n\\nPolicy: '+pathlib.Path('policy.txt').read_text().strip())\n"
        "if not marker.exists():\n marker.touch()\n (other/'policy.txt').write_text('v2\\n')\n"
        " for args in [['add','.'],['commit','-m','policy'],['push','origin','main']]:\n"
        "  subprocess.run(['git','-C',str(other),*args],check=True,capture_output=True)\n"
    )
    validate = project.base / "validate-message.py"
    validate.write_text(
        "import os, pathlib, subprocess\n"
        "base=os.environ['REPOSITORY_PUBLISH_BASE_REF']\n"
        "assert len(base) in (40,64)\n"
        "head=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()\n"
        "if head != base:\n"
        " message=subprocess.check_output(['git','log','-1','--format=%B'],text=True)\n"
        " assert 'Policy: '+pathlib.Path('policy.txt').read_text().strip() in message\n"
    )
    success(
        project.publish(
            project.counter_writer(),
            options=(
                "--message-command",
                json.dumps([sys.executable, str(message)]),
                "--validate-command",
                json.dumps([sys.executable, str(validate)]),
            ),
        )
    )
    assert "Policy: v2" in project.git("log", "-1", "--format=%B", root=project.remote)
    assert (project.state / "count").read_text() == "1"


def test_validation_base_survives_concurrent_fetch_and_changes_after_rebase(project):
    other = project.base / "other"
    project.run(["git", "clone", str(project.remote), str(other)])
    project.git("config", "user.name", "Other", root=other)
    project.git("config", "user.email", "other@example.invalid", root=other)
    seen = project.base / "validation-bases"
    writer = project.writer(
        "import subprocess\n"
        f"other=Path({str(other)!r})\n"
        "(other/'outside.md').write_text('concurrent update\\n')\n"
        "for args in [['add','.'],['commit','-m','concurrent'],['push','origin','main']]:\n"
        " subprocess.run(['git','-C',str(other),*args],check=True,capture_output=True)\n"
        "subprocess.run(['git','fetch','origin'],check=True,capture_output=True)\n"
        "(root/'archive/new.md').write_text('new archive\\n')\n"
        "(state/'count').write_text('1')\n"
    )
    validate = [
        sys.executable,
        "-c",
        (
            "import os,pathlib,subprocess; "
            "base=os.environ['REPOSITORY_PUBLISH_BASE_REF']; "
            "subprocess.run(['git','merge-base','--is-ancestor',base,'HEAD'],check=True); "
            f"p=pathlib.Path({str(seen)!r}); "
            "p.open('a').write(base+'\\n')"
        ),
    ]
    success(project.publish(writer, options=("--validate-command", json.dumps(validate))))
    concurrent = project.git("rev-parse", "HEAD", root=other)
    assert seen.read_text().splitlines() == [project.initial, concurrent]
    assert project.git("show", "main:archive/new.md", root=project.remote) == "new archive"
    assert project.git("show", "main:outside.md", root=project.remote) == "concurrent update"
    assert (project.state / "count").read_text() == "1"
    project.assert_clean()


def test_push_race_retries_and_keeps_other_work(project):
    other = project.base / "other"
    project.run(["git", "clone", str(project.remote), str(other)])
    project.git("config", "user.name", "Other", root=other)
    project.git("config", "user.email", "other@example.invalid", root=other)
    marker = project.base / "race.marker"
    hook = project.repo / ".git/hooks/pre-push"
    hook.write_text(
        f"#!{sys.executable}\nimport os,pathlib,subprocess\nmarker=pathlib.Path({str(marker)!r})\n"
        f"other=pathlib.Path({str(other)!r})\n"
        "if not marker.exists():\n marker.touch()\n (other/'outside.md').write_text('other change\\n')\n"
        " env={k:v for k,v in os.environ.items() if not k.startswith('GIT_')}\n"
        " for args in [['add','.'],['commit','-m','concurrent change'],['push','origin','main']]:\n"
        "  subprocess.run(['git','-C',str(other),*args],check=True,capture_output=True,env=env)\n"
    )
    hook.chmod(0o755)
    result = project.publish(project.counter_writer())
    success(result)
    assert "publish attempt 2/2" in result.stdout
    assert project.git("show", "main:outside.md", root=project.remote) == "other change"
    assert project.git("show", "main:archive/message-1.md", root=project.remote) == "message 1"
    assert (project.state / "count").read_text() == "1"


def test_rebase_conflict_preserves_progress(project):
    other = project.base / "other"
    project.run(["git", "clone", str(project.remote), str(other)])
    project.git("config", "user.name", "Other", root=other)
    project.git("config", "user.email", "other@example.invalid", root=other)
    body = "import subprocess\n(root/'archive/seed.md').write_text('writer change\\n')\n"
    body += (
        f"other=Path({str(other)!r})\n(other/'archive/seed.md').write_text('remote change\\n')\n"
    )
    body += "for args in [['add','.'],['commit','-m','other'],['push','origin','main']]:\n"
    body += " subprocess.run(['git','-C',str(other),*args],check=True,capture_output=True)\n"
    body += "(state/'count').write_text('1')\n"
    failure(project.publish(project.writer(body)), "rebase conflict")
    assert not list(project.state.iterdir())
    assert project.git("show", "main:archive/seed.md", root=project.remote) == "remote change"
    project.assert_clean()


def test_corrupt_state_is_not_silently_skipped(project):
    (project.state / "socket").symlink_to(project.base / "missing")
    failure(project.publish(project.counter_writer()), "state contains a symlink")


def test_staged_path_with_metacharacters_is_literal(project):
    (project.repo / "archive/[name].md").write_text("old")
    (project.repo / "archive/n.md").write_text("other")
    project.git("add", ".")
    project.git("commit", "-m", "literal filenames")
    project.git("push", "origin", "main")
    result = project.publish(
        project.writer("(root/'archive/[name].md').write_text('new')\n"),
        options=("--paths", "archive/[name].md"),
    )
    success(result)
    assert project.git("show", "main:archive/[name].md", root=project.remote) == "new"
    assert project.git("show", "main:archive/n.md", root=project.remote) == "other"


def test_policy_is_rechecked_after_upstream_changes(project):
    other = project.base / "other"
    project.run(["git", "clone", str(project.remote), str(other)])
    project.git("config", "user.name", "Other", root=other)
    project.git("config", "user.email", "other@example.invalid", root=other)
    marker = project.base / "validation.marker"
    program = project.base / "policy.py"
    program.write_text(
        "import pathlib, subprocess, sys\n"
        f"marker=pathlib.Path({str(marker)!r})\nother=pathlib.Path({str(other)!r})\n"
        "root=pathlib.Path(sys.argv[1])\n"
        "if not marker.exists():\n marker.touch()\n (other/'policy.txt').write_text('v2\\n')\n"
        " for args in [['add','.'],['commit','-m','policy'],['push','origin','main']]:\n"
        "  subprocess.run(['git','-C',str(other),*args],check=True,capture_output=True)\n"
        "elif (root/'policy.txt').read_text().strip()!='v1':\n raise SystemExit(19)\n"
    )
    validate = [sys.executable, str(program), "{worktree}"]
    failure(
        project.publish(
            project.counter_writer(), options=("--validate-command", json.dumps(validate))
        ),
        "policy command failed",
    )
    assert not list(project.state.iterdir())
    assert "message-1.md" not in project.git(
        "ls-tree", "-r", "--name-only", "main", root=project.remote
    )


def test_copied_package_runs_without_siblings(project):
    copy = project.base / "independent-package"
    shutil.copytree(
        PACKAGE,
        copy,
        ignore=shutil.ignore_patterns(".venv", "__pycache__", ".pytest_cache", ".ruff_cache"),
    )
    success(project.publish(project.counter_writer(), entry=copy / "scripts/publish"))
    project.assert_clean()


@pytest.mark.parametrize("value", ["../archive", "/archive", ".", ".git/config", "a/../archive"])
def test_invalid_owned_paths(project, value):
    result = project.publish(project.counter_writer(), options=("--paths", value))
    assert result.returncode != 0
    assert not list(project.state.iterdir())


def test_state_symlink_is_rejected(project):
    (project.state / "outside").symlink_to(project.repo / "outside.md")
    failure(project.publish(project.counter_writer()), "state contains a symlink")
    assert (project.repo / "outside.md").read_text() == "outside\n"


def test_missing_new_output_directory_is_valid_noop(project):
    success(
        project.publish(
            project.writer("(state/'count').write_text('4')\n"), options=("--paths", "new-output")
        )
    )
    assert (project.state / "count").read_text() == "4"


def test_existing_worktree_mode_preserves_unpublished_commit(project):
    wt = project.base / "existing"
    project.git("worktree", "add", "-b", "feature/change", str(wt))
    (wt / "archive/new.md").write_text("new\n")
    project.git("add", ".", root=wt)
    project.git("commit", "-m", "change", root=wt)
    hook = project.remote / "hooks/pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    result = project.publish(
        options=("--repo", str(wt), "--existing-worktree", "--expected-branch", "feature/change")
    )
    failure(result, "publication failed")
    assert (wt / "archive/new.md").read_text() == "new\n"
    hook.unlink()
    success(
        project.publish(
            options=(
                "--repo",
                str(wt),
                "--existing-worktree",
                "--expected-branch",
                "feature/change",
            )
        )
    )
    assert project.git("show", "main:archive/new.md", root=project.remote) == "new"


def test_existing_worktree_runs_configured_message_policy(project):
    wt = project.base / "existing"
    project.git("worktree", "add", "-b", "feature/message-policy", str(wt))
    (wt / "archive/new.md").write_text("new\n")
    project.git("add", ".", root=wt)
    project.git("commit", "-m", "unrefreshed message", root=wt)
    message = [
        sys.executable,
        "-c",
        (
            "import os; print(os.environ['REPOSITORY_PUBLISH_SUBJECT']"
            "+'\\n\\nConfigured-Policy: checked')"
        ),
    ]
    success(
        project.publish(
            options=(
                "--repo",
                str(wt),
                "--existing-worktree",
                "--expected-branch",
                "feature/message-policy",
                "--message-command",
                json.dumps(message),
            )
        )
    )
    assert project.git("log", "-1", "--format=%B", "main", root=project.remote) == (
        "sync: messages\n\nConfigured-Policy: checked"
    )
    assert project.git("show", "main:archive/new.md", root=project.remote) == "new"


def test_verification_and_existing_publication_modes_are_mutually_exclusive(project):
    result = project.publish(options=("--existing-worktree", "--verify-lfs", "HEAD"))
    assert result.returncode == 2
    assert "not allowed with argument" in result.stderr
    assert project.git("rev-parse", "main", root=project.remote) == project.initial
    project.assert_clean()


def test_existing_main_checkout_refused(project):
    failure(
        project.publish(options=("--existing-worktree", "--expected-branch", "main")),
        "linked worktree",
    )


def enable_lfs(project):
    if not shutil.which("git-lfs"):
        pytest.skip("git-lfs is required for real LFS transport tests")
    project.git("lfs", "install", "--local")
    (project.repo / ".gitattributes").write_text(
        "archive/*.bin filter=lfs diff=lfs merge=lfs -text\n"
    )
    project.git("add", ".gitattributes")
    project.git("commit", "-m", "track binary files")
    project.git("push", "origin", "main")
    project.initial = project.git("rev-parse", "HEAD")


@pytest.mark.parametrize("filename", ["object.bin", "object,quoted.bin", "[object].bin"])
def test_lfs_real_download_verified(project, filename):
    enable_lfs(project)
    result = project.publish(
        project.writer(
            f"(root/'archive'/{filename!r}).write_bytes(b'synthetic binary content')\n(state/'count').write_text('1')\n"
        )
    )
    success(result)
    assert "verified servable" in result.stdout
    assert (project.state / "count").read_text() == "1"
    project.assert_clean()


def test_lfs_missing_remote_object_is_repaired(project):
    enable_lfs(project)
    payload = b"synthetic repair bytes"
    (project.repo / "archive/object.bin").write_bytes(payload)
    project.git("add", ".")
    project.git("commit", "-m", "object")
    project.git("push", "--no-verify", "origin", "main")
    oid = hashlib.sha256(payload).hexdigest()
    assert not list((project.remote / "lfs").rglob(oid))
    result = project.publish(options=("--verify-lfs", "HEAD"))
    success(result)
    assert "re-uploading" in result.stdout
    assert list((project.remote / "lfs").rglob(oid))


def test_lfs_unavailable_object_fails_and_keeps_progress(project):
    enable_lfs(project)
    pointer = "version https://git-lfs.github.com/spec/v1\noid sha256:" + "e" * 64 + "\nsize 12\n"
    body = (
        f"(root/'archive/missing.bin').write_text({pointer!r})\n(state/'count').write_text('99')\n"
    )
    # Skip the pre-push upload, so the post-push check exercises an unavailable object.
    project.env["GIT_LFS_SKIP_PUSH"] = "1"
    failure(project.publish(project.writer(body)), "LFS objects cannot be downloaded")
    assert not list(project.state.iterdir())
    assert project.state.with_name(project.state.name + ".publish-pending.json").exists()
    # A no-content retry may not bypass the incomplete previous publication.
    failure(
        project.publish(project.writer("(state/'count').write_text('99')\n")),
        "LFS objects cannot be downloaded",
    )
    assert not list(project.state.iterdir())


def test_pending_lfs_verification_recovers_before_noop_progress(project):
    enable_lfs(project)
    marker = project.base / "reject-download"
    marker.touch()
    tools = project.base / "tools"
    tools.mkdir()
    real_git = shutil.which("git")
    shim = tools / "git"
    shim.write_text(
        f"#!{sys.executable}\nimport os,sys\nfrom pathlib import Path\n"
        f"if Path({str(marker)!r}).exists() and 'lfs' in sys.argv and ('fetch' in sys.argv or '--object-id' in sys.argv):\n raise SystemExit(1)\n"
        f"os.execv({real_git!r},[{real_git!r},*sys.argv[1:]])\n"
    )
    shim.chmod(0o755)
    project.env["PATH"] = str(tools) + os.pathsep + project.env["PATH"]
    writer = project.writer(
        "(root/'archive/object.bin').write_bytes(b'recovery object')\n(state/'count').write_text('7')\n"
    )
    failure(project.publish(writer), "LFS objects cannot be downloaded")
    assert not list(project.state.iterdir())
    published = project.git("rev-parse", "main", root=project.remote)
    marker.unlink()
    result = project.publish(writer)
    success(result)
    assert "no content changes" in result.stdout
    assert "verified servable" in result.stdout
    assert project.git("rev-parse", "main", root=project.remote) == published
    assert (project.state / "count").read_text() == "7"
    assert not project.state.with_name(project.state.name + ".publish-pending.json").exists()


def test_malformed_pending_record_fails_without_advancing(project):
    project.state.with_name(project.state.name + ".publish-pending.json").write_text(
        '{"revision": [], "paths": []}'
    )
    failure(project.publish(project.counter_writer()), "invalid pending publication record")
    assert not list(project.state.iterdir())
    assert project.state.with_name(project.state.name + ".publish-pending.json").exists()
