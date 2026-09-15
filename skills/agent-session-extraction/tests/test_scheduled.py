from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from session_test_support import manifest_data, write_manifest
from test_session_pipeline import tree_digest, write_claude

SKILL = Path(__file__).resolve().parents[1]


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def scheduled(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)
    subprocess.run(["git", "config", "--global", "user.name", "Example"], check=True)
    subprocess.run(["git", "config", "--global", "user.email", "example@example.invalid"], check=True)
    repository = tmp_path / "repository"
    repository.mkdir()
    git(repository, "init", "--initial-branch=main")
    (repository / "protected.txt").write_text("preserved\n")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "Synthetic initial content")
    sources = tmp_path / "sources"
    sources.mkdir()
    write_claude(sources / "example.jsonl")
    other = tmp_path / "another-profile"
    other.mkdir()
    write_claude(other / "unselected.jsonl", text="unselected synthetic text")
    manifest = write_manifest(tmp_path / "manifest.json", manifest_data(
        sources, repository, publisher="filesystem-atomic", indexes="owner"))
    publisher = tmp_path / "publisher.py"
    publisher.write_text('''import os, subprocess, sys
from pathlib import Path
repository, worktree = map(Path, sys.argv[1:3])
def git(*args):
    return subprocess.check_output(['git', '-C', str(repository), *args], text=True)
if worktree.exists():
    git('worktree', 'remove', str(worktree))
git('worktree', 'add', '--detach', str(worktree), 'HEAD')
result = subprocess.run(sys.argv[3:], env={**os.environ, 'EXAMPLE_OUTPUT': str(worktree)})
if result.returncode:
    raise SystemExit(result.returncode)
subprocess.run(['git', '-C', str(worktree), 'add', '--', 'History', 'Prompts'], check=True)
changed = subprocess.run(['git', '-C', str(worktree), 'diff', '--cached', '--quiet']).returncode
if changed:
    subprocess.run(['git', '-C', str(worktree), 'commit', '-m', 'Synthetic extracted content'], check=True)
    commit = subprocess.check_output(['git', '-C', str(worktree), 'rev-parse', 'HEAD'], text=True).strip()
    git('update-ref', 'refs/heads/published', commit)
''')
    cfg = {
        "schema_version": "agent-session-schedule/v1", "manifest": str(manifest),
        "repository_root": str(repository),
        "failure_marker": str(tmp_path / "state" / "failure.json"),
        "publication": {"command": [sys.executable, str(publisher), "{repository_root}",
                                    str(tmp_path / "output")],
                        "output_root_environment": "EXAMPLE_OUTPUT"},
    }
    config = tmp_path / "schedule.json"

    def invoke(*args, extra_env=None):
        config.write_text(json.dumps(cfg))
        return subprocess.run([str(SKILL / "scripts/run"), "--config", str(config), *args],
                              env={**os.environ, **(extra_env or {})}, cwd=tmp_path,
                              capture_output=True, text=True, timeout=30)

    return cfg, invoke, repository, sources, tmp_path


def test_actual_extraction_uses_only_manifest_source_and_owned_paths(scheduled):
    cfg, invoke, repository, _, root = scheduled
    original = git(repository, "rev-parse", "HEAD")
    result = invoke()
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "ok" and report["session_count"] == 1
    published = git(repository, "rev-parse", "published")
    changed = git(repository, "diff", "--name-only", original, published).splitlines()
    assert changed and all(path.startswith(("History/", "Prompts/")) for path in changed)
    assert git(repository, "rev-parse", "HEAD") == original
    assert git(repository, "status", "--porcelain") == ""
    assert git(repository, "show", "published:protected.txt") == "preserved"
    output = "".join(path.read_text() for path in (root / "output").rglob("*.md"))
    assert "synthetic request" in output
    assert "unselected synthetic text" not in output
    assert "synthetic request" not in result.stdout + result.stderr
    assert not Path(cfg["failure_marker"]).exists()


def test_doctor_and_dry_run_never_invoke_publisher_or_write(scheduled):
    cfg, invoke, repository, _, root = scheduled
    cfg["publication"]["command"] = [sys.executable, "-c", "raise SystemExit(99)"]
    for mode in ("--doctor", "--dry-run"):
        before = tree_digest(repository)
        result = invoke(mode)
        assert result.returncode == 0, result.stdout + result.stderr
        assert json.loads(result.stdout)["status"] == "ok"
        assert tree_digest(repository) == before
        assert not (root / "output").exists()
        assert not Path(cfg["failure_marker"]).exists()


def test_main_checkout_output_is_rejected(scheduled):
    _, invoke, repository, _, _ = scheduled
    result = invoke("--write", extra_env={"EXAMPLE_OUTPUT": str(repository)})
    assert result.returncode != 0
    assert json.loads(result.stdout)["code"] == "isolated_worktree_required"
    assert git(repository, "status", "--porcelain") == ""


def test_foreign_worktree_is_rejected(scheduled):
    _, invoke, _, _, root = scheduled
    other = root / "foreign"
    other.mkdir()
    git(other, "init")
    result = invoke("--write", extra_env={"EXAMPLE_OUTPUT": str(other)})
    assert result.returncode != 0
    assert json.loads(result.stdout)["code"] == "foreign_worktree"


def test_primary_checkout_refused_when_config_names_a_linked_checkout(scheduled):
    cfg, invoke, repository, _, root = scheduled
    linked = root / "linked-source"
    git(repository, "worktree", "add", "--detach", str(linked))
    cfg["repository_root"] = str(linked)
    manifest = Path(cfg["manifest"])
    data = json.loads(manifest.read_text())
    data["output"]["repository_root"] = str(linked)
    manifest.write_text(json.dumps(data))
    result = invoke("--write", extra_env={"EXAMPLE_OUTPUT": str(repository)})
    assert result.returncode != 0
    assert json.loads(result.stdout)["code"] == "isolated_worktree_required"
    assert git(repository, "status", "--porcelain") == ""


def test_missing_source_does_not_publish(scheduled):
    cfg, invoke, repository, sources, _ = scheduled
    (sources / "example.jsonl").unlink()
    result = invoke()
    assert result.returncode != 0
    assert git(repository, "status", "--porcelain") == ""
    assert subprocess.run(["git", "-C", str(repository), "rev-parse", "--verify", "published"],
                          capture_output=True).returncode != 0
    failure = json.loads(Path(cfg["failure_marker"]).read_text())
    assert failure["status"] == "failed"
    report = json.loads(result.stdout)
    assert report["code"] != "publication_failed"
    assert report["diagnostics"]
    assert Path(cfg["failure_marker"]).stat().st_mode & 0o777 == 0o600


def test_publisher_failure_is_sanitized_and_marked(scheduled):
    cfg, invoke, _, _, _ = scheduled
    cfg["publication"]["command"] = [sys.executable, "-c",
        "import sys; print('private simulated publisher text'); raise SystemExit(7)"]
    result = invoke()
    assert result.returncode != 0
    assert "private simulated publisher text" not in result.stdout + result.stderr
    assert json.loads(result.stdout)["code"] == "publication_failed"
    assert Path(cfg["failure_marker"]).is_file()


def test_busy_publisher_is_reported_as_skipped(scheduled):
    cfg, invoke, _, _, root = scheduled
    cfg["publication"]["command"] = [sys.executable, "-c", "pass"]
    result = invoke()
    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "skipped"
    assert not (root / "output").exists()


@pytest.mark.parametrize("field", ["require_output_audit", "require_reconciliation",
                                    "require_redaction_self_test", "require_prepublication_scan"])
def test_disabled_required_check_is_rejected(scheduled, field):
    cfg, invoke, _, _, root = scheduled
    manifest = Path(cfg["manifest"])
    data = json.loads(manifest.read_text())
    data["gates"][field] = False
    manifest.write_text(json.dumps(data))
    result = invoke()
    assert result.returncode != 0
    assert not (root / "output").exists()


def test_repository_mismatch_rejected_before_publication(scheduled):
    cfg, invoke, _, _, root = scheduled
    cfg["repository_root"] = str(root)
    result = invoke()
    assert result.returncode != 0
    assert json.loads(result.stdout)["code"] == "repository_mismatch"


def test_configured_validation_failure_prevents_commit(scheduled):
    cfg, invoke, repository, _, _ = scheduled
    cfg["validate_command"] = [sys.executable, "-c", "raise SystemExit(4)"]
    assert invoke().returncode != 0
    assert subprocess.run(["git", "-C", str(repository), "rev-parse", "--verify", "published"],
                          capture_output=True).returncode != 0


def test_no_implicit_configuration(tmp_path):
    result = subprocess.run([str(SKILL / "scripts/run")], cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "--config" in result.stderr


def test_native_environment_configuration_runs_in_another_home(scheduled):
    cfg, invoke, repository, _, root = scheduled
    cfg["expand_environment"] = True
    cfg["environment"] = {"SELECTED_ROOT": str(root), "SELECTED_REPOSITORY": str(repository)}
    cfg["repository_root"] = "${SELECTED_REPOSITORY}"
    cfg["manifest"] = "$SELECTED_ROOT/manifest.json"
    cfg["failure_marker"] = "~/state/failure.json"
    cfg["publication"]["command"][1] = "$SELECTED_ROOT/publisher.py"
    result = invoke()
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["session_count"] == 1
    assert git(repository, "rev-parse", "published")
    assert not (root / "home/state/failure.json").exists()


def test_missing_environment_reference_fails_before_publisher(scheduled, monkeypatch):
    cfg, invoke, repository, _, root = scheduled
    monkeypatch.delenv("MISSING_EXAMPLE_REPOSITORY", raising=False)
    cfg["expand_environment"] = True
    cfg["repository_root"] = "${MISSING_EXAMPLE_REPOSITORY}"
    result = invoke()
    assert json.loads(result.stdout)["code"] == "invalid_environment_reference"
    assert result.returncode != 0
    assert not (root / "output").exists()
    assert git(repository, "status", "--porcelain") == ""


@pytest.mark.parametrize("kind", ["inside", "symlink"])
def test_selected_external_configuration_policy_is_enforced(scheduled, kind):
    cfg, _, repository, _, root = scheduled
    cfg["require_external_config"] = True
    original = root / "private-schedule.json"
    original.write_text(json.dumps(cfg))
    path = repository / "schedule.json" if kind == "inside" else root / "linked-schedule.json"
    if kind == "inside":
        path.write_bytes(original.read_bytes())
    else:
        path.symlink_to(original)
    result = subprocess.run([str(SKILL / "scripts/run"), "--config", str(path), "--doctor"],
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert json.loads(result.stdout)["code"] == "external_config_required"
    assert not (root / "output").exists()


@pytest.mark.parametrize("mode", ["--doctor", "--dry-run", "--write", "publish"])
def test_preflight_failure_blocks_every_mode_without_relaying_output(scheduled, mode):
    cfg, invoke, repository, _, root = scheduled
    cfg["preflight_command"] = [sys.executable, "-c",
        "print('private synthetic policy output'); raise SystemExit(7)"]
    result = invoke(*([] if mode == "publish" else [mode]))
    assert result.returncode != 0
    assert json.loads(result.stdout)["code"] == "preflight_failed"
    assert "private synthetic" not in result.stdout + result.stderr
    assert git(repository, "status", "--porcelain") == ""
    assert not (root / "output").exists()
    assert Path(cfg["failure_marker"]).exists() == (mode in {"--write", "publish"})


def use_runtime_worktree(cfg):
    manifest = Path(cfg["manifest"])
    data = json.loads(manifest.read_text())
    data["publisher"]["strategy"] = "git-worktree"
    manifest.write_text(json.dumps(data))
    publisher = Path(cfg["publication"]["command"][1])
    text = publisher.read_text().replace(
        "git('worktree', 'add', '--detach', str(worktree), 'HEAD')", "# Runtime prepares the reserved path")
    publisher.write_text(text)


def test_runtime_prepares_and_stages_scheduled_worktree(scheduled):
    cfg, invoke, repository, _, root = scheduled
    use_runtime_worktree(cfg)
    original = git(repository, "rev-parse", "HEAD")
    result = invoke()
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["session_count"] == 1
    assert git(repository, "rev-parse", "published") != original
    assert git(repository, "rev-parse", "HEAD") == original
    assert git(repository, "status", "--porcelain") == ""
    assert (root / "output/.git").is_file()


@pytest.mark.parametrize("target", ["main", "inside", "existing", "relative", "symlink"])
def test_runtime_worktree_refuses_unsafe_destinations(scheduled, target):
    cfg, invoke, repository, _, root = scheduled
    use_runtime_worktree(cfg)
    destinations = {"main": repository, "inside": repository / "new-worktree",
                    "existing": root, "relative": Path("relative-output"),
                    "symlink": root / "alias"}
    (root / "alias").symlink_to(repository, target_is_directory=True)
    result = invoke("--write", extra_env={"EXAMPLE_OUTPUT": str(destinations[target])})
    assert result.returncode != 0
    assert json.loads(result.stdout)["code"] == "unused_external_worktree_required"
    assert git(repository, "status", "--porcelain") == ""


def test_runtime_worktree_dry_modes_do_not_prepare_or_publish(scheduled):
    cfg, invoke, repository, _, root = scheduled
    use_runtime_worktree(cfg)
    cfg["publication"]["command"] = [sys.executable, "-c", "raise SystemExit(99)"]
    for mode in ("--doctor", "--dry-run"):
        assert invoke(mode).returncode == 0
        assert not (root / "output").exists()
        assert not Path(cfg["failure_marker"]).exists()
        assert git(repository, "status", "--porcelain") == ""


@pytest.mark.skipif(not shutil.which("git-crypt"), reason="git-crypt unavailable")
@pytest.mark.parametrize("explicit_ref", [False, True])
def test_scheduled_runtime_worktree_encrypts_real_index(scheduled, explicit_ref):
    cfg, invoke, repository, _, root = scheduled
    use_runtime_worktree(cfg)
    subprocess.run(["git-crypt", "init"], cwd=repository, capture_output=True, check=True)
    (repository / ".gitattributes").write_text(
        "History/** filter=git-crypt diff=git-crypt\nPrompts/** filter=git-crypt diff=git-crypt\n")
    git(repository, "add", ".gitattributes")
    git(repository, "commit", "-m", "Synthetic encryption attributes")
    manifest = Path(cfg["manifest"])
    data = json.loads(manifest.read_text())
    key = repository / ".git/git-crypt/keys/default"
    data["publisher"].update(encryption="git-crypt", key_link={
        "source": str(key), "target": "git-crypt/keys/default"})
    manifest.write_text(json.dumps(data))
    extra_env = {}
    if explicit_ref:
        cfg["publication"]["base_ref_environment"] = "EXAMPLE_BASE"
        extra_env["EXAMPLE_BASE"] = git(repository, "rev-parse", "HEAD")
        (repository / "History").mkdir()
        (repository / "History/private-draft.md").write_text("uncommitted synthetic draft")
    result = invoke(extra_env=extra_env)
    assert result.returncode == 0, result.stdout + result.stderr
    worktree = root / "output"
    private = Path(git(worktree, "rev-parse", "--absolute-git-dir"))
    link = private / "git-crypt/keys/default"
    assert link.is_symlink() and link.resolve() == key
    paths = git(repository, "ls-tree", "-r", "--name-only", "published", "History", "Prompts").splitlines()
    assert paths
    for path in paths:
        blob = subprocess.check_output(["git", "-C", str(repository), "show", "published:" + path])
        assert blob.startswith(b"\x00GITCRYPT")
        assert b"synthetic request" not in blob
        assert not (worktree / path).read_bytes().startswith(b"\x00GITCRYPT")


def test_explicit_ref_ignores_dirty_output_and_local_commits(scheduled):
    cfg, invoke, repository, _, root = scheduled
    use_runtime_worktree(cfg)
    base = git(repository, "rev-parse", "HEAD")
    (repository / "local-only.txt").write_text("unpublished local content")
    git(repository, "add", "local-only.txt")
    git(repository, "commit", "-m", "Synthetic local-only change")
    local_head = git(repository, "rev-parse", "HEAD")
    (repository / "History").mkdir()
    (repository / "History/local-draft.md").write_text("not an extraction record")
    (repository / "protected.txt").write_text("staged local change")
    git(repository, "add", "protected.txt")
    (repository / "protected.txt").write_text("unstaged local change")
    status = git(repository, "status", "--porcelain")
    index = git(repository, "diff", "--cached")
    cfg["publication"]["base_ref_environment"] = "EXAMPLE_BASE"
    result = invoke(extra_env={"EXAMPLE_BASE": base})
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["session_count"] == 1
    assert git(repository, "rev-parse", "published^") == base
    assert git(repository, "rev-parse", "HEAD") == local_head
    assert git(repository, "status", "--porcelain") == status
    assert git(repository, "diff", "--cached") == index
    assert (repository / "protected.txt").read_text() == "unstaged local change"
    assert not (root / "output/local-only.txt").exists()
    assert not (root / "output/History/local-draft.md").exists()


def test_explicit_ref_failure_removes_only_its_prepared_worktree(scheduled):
    cfg, invoke, repository, sources, root = scheduled
    use_runtime_worktree(cfg)
    cfg["publication"]["base_ref_environment"] = "EXAMPLE_BASE"
    base = git(repository, "rev-parse", "HEAD")
    (repository / "draft.txt").write_text("preserve this")
    shutil.rmtree(sources)
    result = invoke(extra_env={"EXAMPLE_BASE": base})
    assert result.returncode != 0
    assert not (root / "output").exists()
    assert (repository / "draft.txt").read_text() == "preserve this"
    assert git(repository, "rev-parse", "HEAD") == base
    assert Path(cfg["failure_marker"]).exists()


def test_configured_ref_is_required_only_for_write(scheduled):
    cfg, invoke, _, _, root = scheduled
    use_runtime_worktree(cfg)
    cfg["publication"]["base_ref_environment"] = "EXAMPLE_BASE"
    for mode in ("--doctor", "--dry-run"):
        assert invoke(mode).returncode == 0
    result = invoke("--write", extra_env={"EXAMPLE_OUTPUT": str(root / "output")})
    assert result.returncode != 0
    assert json.loads(result.stdout)["code"] == "publisher_base_ref_missing"
    assert not (root / "output").exists()


@pytest.mark.parametrize("value", ["", "BAD-NAME", "EXAMPLE_OUTPUT", None])
def test_invalid_ref_environment_is_rejected(scheduled, value):
    cfg, invoke, _, _, root = scheduled
    use_runtime_worktree(cfg)
    cfg["publication"]["base_ref_environment"] = value
    result = invoke()
    assert result.returncode != 0
    assert json.loads(result.stdout)["code"] == "invalid_base_ref_environment"
    assert not (root / "output").exists()



def test_invalid_ref_fails_before_sources_and_does_not_create_a_worktree(scheduled):
    cfg, invoke, repository, sources, root = scheduled
    use_runtime_worktree(cfg)
    cfg["publication"]["base_ref_environment"] = "EXAMPLE_BASE"
    shutil.rmtree(sources)
    result = invoke(extra_env={"EXAMPLE_BASE": "refs/heads/missing-synthetic-ref"})
    assert result.returncode != 0
    assert json.loads(result.stdout)["code"] == "GIT_WORKTREE_PREPARATION_FAILED"
    assert not (root / "output").exists()
    assert git(repository, "status", "--porcelain") == ""
