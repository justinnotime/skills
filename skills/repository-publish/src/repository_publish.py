"""Run a writer in an isolated Git worktree; advance progress only after publication."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath


class Failure(RuntimeError):
    """An operation failed without advancing durable writer progress."""

    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


def log(message: str) -> None:
    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] {message}", flush=True)


def git(
    root: Path, *args: str, check: bool = True, env: dict | None = None
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        env=env,
        check=False,
    )
    if check and result.returncode:
        raise Failure(f"git {args[0]} failed (exit {result.returncode})")
    return result


def text(root: Path, *args: str) -> str:
    return git(root, *args).stdout.decode().strip()


def absolute(value: str | Path) -> Path:
    return Path(os.path.expandvars(str(value))).expanduser().resolve()


def paths(value: str) -> list[str]:
    result = value.split()
    if not result:
        raise Failure("at least one owned path is required")
    for item in result:
        p = PurePosixPath(item)
        if p.is_absolute() or ".." in p.parts or ".git" in p.parts or item.startswith("-"):
            raise Failure("owned paths must be relative literal paths outside Git metadata")
        if item in {".", ""} or str(p) != item.rstrip("/"):
            raise Failure("owned paths must be normalized and cannot select the whole repository")
    return [item.rstrip("/") for item in result]


def owned(path: str, selected: list[str]) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in selected)


def changed(root: Path) -> list[tuple[str, str]]:
    records = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout.split(
        b"\0"
    )
    output = []
    i = 0
    while i < len(records):
        record = records[i]
        i += 1
        if not record:
            continue
        status, name = record[:2].decode(), os.fsdecode(record[3:])
        output.append((status, name))
        if "R" in status or "C" in status:
            if i >= len(records) or not records[i]:
                raise Failure("incomplete rename status")
            output.append((status, os.fsdecode(records[i])))
            i += 1
    return output


def check_ownership(root: Path, selected: list[str]) -> list[str]:
    changes = changed(root)
    for status, name in changes:
        if status != "??" and not owned(name, selected):
            raise Failure("writer changed a tracked path outside its ownership")
    return [name for _, name in changes if owned(name, selected)]


@contextlib.contextmanager
def lock(path: Path, wait: float):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    yield False
                    return
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        try:
            yield True
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".publish-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def state_files(root: Path) -> list[Path]:
    if root.is_symlink():
        raise Failure("state directory must not be a symlink")
    output = []
    for path in root.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise Failure("state contains a symlink or special file")
        if path.is_file():
            output.append(path)
    return output


def promote(staged: Path, durable: Path) -> None:
    files = state_files(staged)
    state_files(durable)
    for source in files:
        atomic_write(durable / source.relative_to(staged), source.read_bytes())


def command(value: str | None) -> list[str] | None:
    if value is None:
        return None
    try:
        result = json.loads(value)
    except json.JSONDecodeError as exc:
        raise Failure("policy command must be a JSON argument array") from exc
    if (
        not isinstance(result, list)
        or not result
        or not all(isinstance(x, str) and x for x in result)
    ):
        raise Failure("policy command must be a nonempty JSON string array")
    return result


def policy(argv: list[str] | None, root: Path, env: dict, *, capture: bool = False) -> str:
    if not argv:
        return ""
    replacement = {
        "{worktree}": str(root),
        "{repository}": env["REPOSITORY_PUBLISH_REPOSITORY"],
        "{state}": env.get("REPOSITORY_PUBLISH_STATE", ""),
    }
    args = []
    for arg in argv:
        for key, value in replacement.items():
            arg = arg.replace(key, value)
        args.append(arg)
    proc = subprocess.run(
        args, cwd=root, env=env, check=False, stdout=subprocess.PIPE if capture else None
    )
    if proc.returncode:
        raise Failure(f"private policy command failed (exit {proc.returncode})")
    return proc.stdout.decode() if capture else ""


def checked_policy(argv: list[str] | None, root: Path, env: dict, *, capture=False) -> str:
    if not argv:
        return ""
    before = (text(root, "rev-parse", "HEAD"), git(root, "diff", "--cached", "--raw", "-z").stdout)
    result = policy(argv, root, env, capture=capture)
    after = (text(root, "rev-parse", "HEAD"), git(root, "diff", "--cached", "--raw", "-z").stdout)
    if after != before or git(root, "diff", "--quiet", check=False).returncode != 0:
        raise Failure("policy command modified tracked repository content")
    return result


def lfs_objects(root: Path, revision: str, selected: list[str]) -> tuple[list[str], list[str]]:
    parent = git(root, "rev-parse", "--verify", revision + "^", check=False)
    base = (
        parent.stdout.decode().strip()
        if parent.returncode == 0
        else text(root, "hash-object", "-t", "tree", "/dev/null")
    )
    changed_names = git(
        root,
        "--literal-pathspecs",
        "diff",
        "--name-only",
        "--no-renames",
        "-z",
        "--diff-filter=AM",
        base,
        revision,
        "--",
        *selected,
    ).stdout
    names, oids = [], []
    for raw in changed_names.split(b"\0"):
        if not raw:
            continue
        name = os.fsdecode(raw)
        size = int(text(root, "cat-file", "-s", f"{revision}:{name}"))
        if size > 1024:
            continue
        content = git(root, "cat-file", "blob", f"{revision}:{name}").stdout
        if not content.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
            continue
        match = re.search(rb"^oid sha256:([0-9a-f]{64})$", content, re.MULTILINE)
        if not match or not re.search(rb"^size [0-9]+$", content, re.MULTILINE):
            raise Failure("invalid LFS pointer")
        names.append(name)
        oids.append(match[1].decode())
    return names, sorted(set(oids))


def verify_lfs(root: Path, remote: str, revision: str, selected: list[str]) -> None:
    names, oids = lfs_objects(root, revision, selected)
    if not oids:
        return
    log(f"verifying {len(oids)} pushed LFS object(s)")
    # --refetch forces a remote transfer even when a local object already exists.
    # Do not override lfs.storage through `git -c`: the file transport passes
    # that setting to the remote helper and changes its object location too.
    for attempt in range(2):
        # An include list cannot represent commas or glob metacharacters as
        # literal filenames. In that uncommon case fetch the revision's objects.
        include = "" if any(re.search(r"[,\[\]*?]", name) for name in names) else ",".join(names)
        downloaded = git(
            root,
            "-c",
            "lfs.fetchexclude=",
            "-c",
            f"lfs.fetchinclude={include}",
            "-c",
            "lfs.skipdownloaderrors=false",
            "lfs",
            "fetch",
            "--refetch",
            remote,
            revision,
            check=False,
        )
        if downloaded.returncode == 0:
            log("all pushed LFS objects verified servable")
            return
        if attempt == 0:
            log("LFS download failed; re-uploading referenced objects once")
            git(root, "lfs", "push", "--object-id", remote, *oids, check=False)
    raise Failure(
        "pushed LFS objects cannot be downloaded after one repair upload: " + ", ".join(oids)
    )


def fetch(root: Path, remote: str, branch: str) -> str:
    git(root, "fetch", "--quiet", remote, f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}")
    return f"refs/remotes/{remote}/{branch}"


def prepare_worktree(root: Path, target: Path, task_branch: str, remote: str, branch: str):
    """Reuse a dedicated checkout without losing unpublished commits or dirty files."""
    if target == root or root in target.parents:
        raise Failure("dedicated worktree must be outside the source checkout")
    git(root, "check-ref-format", "refs/heads/" + task_branch)
    upstream = fetch(root, remote, branch)
    git(root, "worktree", "prune")
    entries = git(root, "worktree", "list", "--porcelain", "-z").stdout.split(b"\0")
    registered = {
        os.fsdecode(item[len(b"worktree ") :]) for item in entries if item.startswith(b"worktree ")
    }
    if str(target) not in registered:
        if target.exists() or target.is_symlink():
            raise Failure("unregistered worktree path already exists; refusing to remove it")
        target.parent.mkdir(parents=True, exist_ok=True)
        existing = git(
            root, "show-ref", "--verify", "--quiet", "refs/heads/" + task_branch, check=False
        )
        if existing.returncode not in {0, 1}:
            raise Failure("cannot inspect dedicated worktree branch")
        ahead = (
            int(text(root, "rev-list", "--count", upstream + ".." + task_branch))
            if existing.returncode == 0
            else 0
        )
        if ahead:
            git(root, "worktree", "add", str(target), task_branch)
        else:
            git(root, "worktree", "add", "-B", task_branch, str(target), upstream)
    if not (target / ".git").is_file():
        raise Failure("dedicated worktree is unavailable")
    if absolute(target / text(target, "rev-parse", "--git-common-dir")) != absolute(
        root / text(root, "rev-parse", "--git-common-dir")
    ):
        raise Failure("dedicated worktree belongs to another repository")
    if text(target, "branch", "--show-current") != task_branch:
        raise Failure("dedicated worktree is on an unexpected branch")


def reset_worktree(root: Path, task_branch: str, upstream: str):
    if not (root / ".git").is_file():
        raise Failure("reset requires a linked worktree")
    if text(root, "branch", "--show-current") != task_branch:
        raise Failure("refusing to reset an unexpected branch")
    if changed(root):
        raise Failure("cannot reset a dirty dedicated worktree")
    if text(root, "rev-list", "--count", upstream + "..HEAD") != "0":
        raise Failure("cannot reset an unpublished commit")
    git(root, "checkout", "--quiet", "-B", task_branch, upstream)
    git(root, "reset", "--hard", "--quiet", upstream)


def commit_worktree(root: Path, args) -> int:
    """Commit selected generated files without publishing or discarding output."""
    if not (root / ".git").is_file():
        raise Failure("commit requires a linked worktree")
    if not args.source_repository:
        raise Failure("--source-repository is required for commit")
    source = absolute(args.source_repository)
    if source == root or not (source / ".git").exists():
        raise Failure("source repository must be a separate Git checkout")
    if absolute(root / text(root, "rev-parse", "--git-common-dir")) != absolute(
        source / text(source, "rev-parse", "--git-common-dir")
    ):
        raise Failure("dedicated worktree belongs to another repository")
    if (
        args.task_branch == args.branch
        or text(root, "branch", "--show-current") != args.task_branch
    ):
        raise Failure("refusing to commit an unexpected branch")
    selected = paths(args.paths or "")
    changes = changed(root)
    if any(not owned(name, selected) for _, name in changes):
        raise Failure("dedicated worktree includes a path outside its ownership")
    if not changes:
        return 2
    validation, message = command(args.validate_command), command(args.message_command)
    if not validation or not message:
        raise Failure("commit requires validation and message commands")
    # Exact observed paths include deletions and both rename sides. Never use a
    # broad add or reset: a failed policy/commit leaves the paid output in place.
    git(root, "--literal-pathspecs", "add", "--", *dict.fromkeys(name for _, name in changes))
    env = dict(
        os.environ,
        REPOSITORY_PUBLISH_WORKTREE=str(root),
        REPOSITORY_PUBLISH_REPOSITORY=str(source),
    )
    checked_policy(validation, root, env)
    result = checked_policy(message, root, env, capture=True)
    if not result.strip():
        raise Failure("message command returned an empty commit message")
    # Policy is caller-controlled, but cannot widen the selected publication.
    if any(not owned(name, selected) for _, name in changed(root)):
        raise Failure("policy added a path outside its ownership")
    proc = subprocess.run(
        ["git", "-C", str(root), "commit", "--quiet", "-F", "-"],
        input=result.encode(),
        capture_output=True,
        env=env,
        check=False,
    )
    if proc.returncode:
        raise Failure("commit failed; generated output was retained")
    return 0


def run_at_ref(root: Path, reference: str, scratch: Path, argv: list[str]) -> int:
    if not argv:
        raise Failure("a command after -- is required")
    if scratch == root or root in scratch.parents:
        raise Failure("temporary worktree storage must be outside the checkout")
    if reference.startswith("-"):
        raise Failure("reference cannot be an option")
    revision = text(root, "rev-parse", "--verify", reference + "^{commit}")
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="inspect-", dir=scratch) as temporary:
        target = Path(temporary) / "worktree"
        git(root, "worktree", "add", "--detach", str(target), revision)
        try:
            env = dict(
                os.environ,
                REPOSITORY_PUBLISH_WORKTREE=str(target),
                REPOSITORY_PUBLISH_REPOSITORY=str(root),
            )
            return subprocess.run(argv, cwd=target, env=env, check=False).returncode
        finally:
            git(root, "worktree", "remove", "--force", str(target))


def worktree_main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description="Prepare, inspect or reset caller-owned Git worktrees")
    p.add_argument(
        "action",
        choices=[
            "prepare",
            "fetch",
            "changed",
            "committed",
            "ahead",
            "reset",
            "commit",
            "run-at-ref",
        ],
    )
    p.add_argument("--repo", default=".")
    p.add_argument("--worktree")
    p.add_argument("--task-branch")
    p.add_argument("--source-repository")
    p.add_argument("--paths")
    p.add_argument("--validate-command")
    p.add_argument("--message-command")
    p.add_argument("--remote", default="origin")
    p.add_argument("--branch", default="main")
    p.add_argument("--ref", default="HEAD")
    p.add_argument(
        "--scratch",
        default=str(
            Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
            / "repository-publish/inspections"
        ),
    )
    p.add_argument("--null", action="store_true", help="NUL-separated path output")
    if "--" in argv:
        split = argv.index("--")
        arguments, command_argv = argv[:split], argv[split + 1 :]
    else:
        arguments, command_argv = argv, []
    args = p.parse_args(arguments)
    try:
        root = absolute(args.repo)
        if not (root / ".git").exists():
            raise Failure("repository must be a Git checkout")
        if args.remote.startswith("-") or args.branch.startswith("-"):
            raise Failure("remote and branch cannot be options")
        git(root, "check-ref-format", "refs/heads/" + args.branch)
        upstream = f"refs/remotes/{args.remote}/{args.branch}"
        if args.action in {"prepare", "reset", "commit"} and not args.task_branch:
            raise Failure("--task-branch is required")
        if args.task_branch and args.task_branch.startswith("-"):
            raise Failure("task branch cannot be an option")
        if command_argv and args.action != "run-at-ref":
            raise Failure("only run-at-ref accepts an external command")
        if args.action == "prepare":
            if not args.worktree:
                raise Failure("--worktree is required")
            prepare_worktree(
                root, absolute(args.worktree), args.task_branch, args.remote, args.branch
            )
        elif args.action == "fetch":
            fetch(root, args.remote, args.branch)
        elif args.action == "reset":
            reset_worktree(root, args.task_branch, upstream)
        elif args.action == "commit":
            return commit_worktree(root, args)
        elif args.action == "ahead":
            print(text(root, "rev-list", "--count", upstream + "..HEAD"))
        elif args.action == "run-at-ref":
            return run_at_ref(root, args.ref, absolute(args.scratch), command_argv)
        else:
            names = (
                [name for _, name in changed(root)]
                if args.action == "changed"
                else [
                    os.fsdecode(name)
                    for name in git(
                        root, "diff", "--name-only", "--no-renames", "-z", upstream + "..HEAD"
                    ).stdout.split(b"\0")
                    if name
                ]
            )
            if not args.null and any("\n" in name or "\r" in name for name in names):
                raise Failure("line-based path output cannot represent newlines; use --null")
            delimiter = b"\0" if args.null else b"\n"
            sys.stdout.buffer.write(b"".join(os.fsencode(name) + delimiter for name in names))
        return 0
    except (Failure, OSError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


def push_existing(
    root: Path,
    args,
    selected: list[str],
    env: dict,
    pending: Path | None = None,
    message: list[str] | None = None,
) -> str:
    if text(root, "branch", "--show-current") != args.expected_branch:
        raise Failure("refusing to publish an unexpected branch")
    with lock(args.publish_lock, args.lock_timeout) as acquired:
        if not acquired:
            raise Failure("timed out waiting for publication lock")
        for attempt in range(args.attempts):
            log(f"publish attempt {attempt + 1}/{args.attempts}")
            try:
                upstream = fetch(root, args.remote, args.branch)
            except Failure:
                if attempt + 1 < args.attempts:
                    time.sleep(args.retry_delay * (attempt + 1))
                continue
            rebase = git(root, "rebase", upstream, check=False)
            if rebase.returncode:
                git(root, "rebase", "--abort", check=False)
                raise Failure("rebase conflict; progress was not advanced", 4)
            try:
                checked_policy(args.validate, root, env)
            except Failure as exc:
                raise Failure(str(exc), 3) from exc
            if message and text(root, "rev-list", "--count", upstream + "..HEAD") != "0":
                result = checked_policy(message, root, env, capture=True)
                if not result.strip():
                    raise Failure("message command returned an empty commit message")
                proc = subprocess.run(
                    ["git", "-C", str(root), "commit", "--quiet", "--amend", "-F", "-"],
                    input=result.encode(),
                    capture_output=True,
                    env=env,
                    check=False,
                )
                if proc.returncode:
                    raise Failure("commit message refresh failed")
            revision = text(root, "rev-parse", "HEAD")
            if selected:
                committed = git(
                    root, "diff", "--no-renames", "--name-only", "-z", upstream, revision
                ).stdout.split(b"\0")
                if any(not owned(os.fsdecode(name), selected) for name in committed if name):
                    raise Failure("commit includes a path outside its ownership")
            if pending:
                atomic_write(
                    pending, json.dumps({"revision": revision, "paths": selected}).encode()
                )
            pushed = git(
                root, "push", "--quiet", args.remote, f"HEAD:refs/heads/{args.branch}", check=False
            )
            if pushed.returncode == 0:
                verify_lfs(root, args.remote, revision, selected)
                # Update the local tracking reference used by persistent callers
                # to distinguish already-published commits on their next run.
                fetch(root, args.remote, args.branch)
                return revision
            if attempt + 1 < args.attempts:
                time.sleep(args.retry_delay * (attempt + 1))
        raise Failure("publication failed after configured retry attempts")


def prepare_args(args):
    args.repo = absolute(args.repo)
    if not (args.repo / ".git").exists():
        raise Failure("repository must be a Git checkout")
    args.selected = paths(args.paths) if args.paths else []
    args.validate = command(args.validate_command)
    args.message = command(args.message_command)
    args.scratch = absolute(args.scratch)
    args.publish_lock = absolute(args.publish_lock)
    if args.attempts < 1 or args.retry_delay < 0 or args.lock_timeout < 0:
        raise Failure("invalid retry or lock settings")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", args.task):
        raise Failure("task must be a simple identifier")
    key = args.task + "-" + hashlib.sha256(os.fsencode(args.repo)).hexdigest()[:12]
    state_root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    cache_root = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    args.state_dir = args.state_dir or str(state_root / "repository-publish" / key)
    args.lock = args.lock or str(cache_root / "repository-publish" / (key + ".lock"))
    for value in (args.worktree_env, args.state_env):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) or value in {
            "HOME",
            "PATH",
            "CODEX_HOME",
        }:
            raise Failure("invalid writer environment variable")
    if args.remote.startswith("-") or args.branch.startswith("-"):
        raise Failure("remote and branch cannot be options")
    git(args.repo, "check-ref-format", "refs/heads/" + args.branch)
    git(args.repo, "remote", "get-url", args.remote)
    return args


def job_value(value, env: dict[str, str], context: dict[str, str]):
    """Expand declared environment references, without evaluating shell code."""
    if isinstance(value, dict):
        if set(value) - {"env", "default"} or not isinstance(value.get("env"), str):
            raise Failure("invalid environment selection")
        value = env.get(value["env"]) or value.get("default")
    if not isinstance(value, str) or "\0" in value:
        raise Failure("job values must be strings")
    for key, replacement in context.items():
        value = value.replace("{" + key + "}", replacement)

    def variable(match):
        key = match.group(1) or match.group(2)
        if key not in env:
            raise Failure(f"required environment variable is unset: {key}")
        return env[key]

    value = re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)", variable, value)
    if value == "~" or value.startswith("~/"):
        value = env["HOME"] + value[1:]
    return value


def job_environment(values, env, context):
    if not isinstance(values, dict):
        raise Failure("job environment must be an object")
    result = dict(env)
    for key, value in values.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise Failure("invalid job environment name")
        if key in {"HOME", "CODEX_HOME"} or key.startswith("REPOSITORY_PUBLISH_"):
            raise Failure("job cannot replace reserved environment variables")
        result[key] = job_value(value, result, context)
    return result


def load_job(args):
    config = absolute(args.config)
    data = json.loads(config.read_text(encoding="utf-8"))
    fields = {
        "repo",
        "task",
        "paths",
        "sparse",
        "subject",
        "agent",
        "state_dir",
        "lock",
        "scratch",
        "publish_lock",
        "remote",
        "branch",
        "attempts",
        "retry_delay",
        "lock_timeout",
        "worktree_env",
        "state_env",
        "validate_command",
        "message_command",
    }
    if not isinstance(data, dict) or data.get("schema") != "repository-publish-job/v1":
        raise Failure("unsupported publication job schema")
    if set(data) - fields - {"schema", "environment", "steps", "selection"}:
        raise Failure("unknown publication job field")
    if args.existing_worktree or args.verify_lfs:
        raise Failure("a configured job cannot be combined with another publication mode")
    context = {
        "config_dir": str(config.parent),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    args.job_env = job_environment(data.get("environment", {}), os.environ, context)
    args.repo = job_value(data.get("repo", str(config.parent)), args.job_env, context)
    context["repository"] = str(absolute(args.repo))
    selection = args.steps or job_value(data.get("selection", "all"), args.job_env, context)
    context["selection"] = selection
    for key in fields & data.keys():
        value = data[key]
        if key in {"attempts", "retry_delay", "lock_timeout"}:
            value = (
                job_value(value, args.job_env, context)
                if not isinstance(value, (int, float))
                else value
            )
            value = int(value) if key == "attempts" else float(value)
        elif key in {"validate_command", "message_command"}:
            if not isinstance(value, list) or not value:
                raise Failure("policy command must be a nonempty argument array")
            value = json.dumps([job_value(item, args.job_env, context) for item in value])
        elif key in {"paths", "sparse"} and isinstance(value, list):
            values = [job_value(item, args.job_env, context) for item in value]
            if any(not item or re.search(r"\s", item) for item in values):
                raise Failure("owned and sparse path entries cannot contain whitespace")
            value = " ".join(values)
        else:
            value = job_value(value, args.job_env, context)
        setattr(args, key, value)
    steps = data.get("steps")
    args.job_context = context
    if steps is None and args.writer and not args.steps:
        args.job_steps = None
        return args
    if args.writer:
        raise Failure("choose configured steps or an argument-array writer, not both")
    if not isinstance(steps, list) or not steps:
        raise Failure("job requires a nonempty steps array")
    identities, groups = set(), set()
    for step in steps:
        if not isinstance(step, dict) or set(step) - {
            "id",
            "group",
            "argv",
            "copy",
            "environment",
            "on_error",
        }:
            raise Failure("invalid job step")
        name = step.get("id", "")
        group = step.get("group", name)
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name)
            or name in identities
        ):
            raise Failure("step identifiers must be unique simple names")
        if not isinstance(group, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", group):
            raise Failure("invalid step group")
        identities.add(name)
        groups.add(group)
        if ("argv" in step) == ("copy" in step):
            raise Failure("each step requires exactly one argv or copy operation")
        if "argv" in step and (not isinstance(step["argv"], list) or not step["argv"]):
            raise Failure("step argv must be a nonempty array")
        if not isinstance(step.get("environment", {}), dict):
            raise Failure("step environment must be an object")
        if step.get("on_error", "fail") not in ("fail", "continue"):
            raise Failure("invalid step failure policy")
    chosen = set(selection.split(","))
    if selection != "all" and ("" in chosen or chosen - identities - groups):
        raise Failure("selection contains an unknown step or group")
    args.job_steps = [
        s
        for s in steps
        if selection == "all" or s["id"] in chosen or s.get("group", s["id"]) in chosen
    ]
    return args


def configured_steps(args, root: Path, env: dict, *, inspect=False):
    context = dict(args.job_context, worktree=str(root), state=env["REPOSITORY_PUBLISH_STATE"])
    for step in args.job_steps:
        if {args.worktree_env, args.state_env} & step.get("environment", {}).keys():
            raise Failure("step cannot replace transaction environment aliases")
        selected_env = job_environment(step.get("environment", {}), env, context)
        if "copy" in step:
            operation = step["copy"]
            if not isinstance(operation, dict) or set(operation) != {"source", "directory", "name"}:
                raise Failure("copy requires source, directory and name")
            values = {k: job_value(v, selected_env, context) for k, v in operation.items()}
            name = values["name"]
            if not name or name.startswith(".") or Path(name).name != name or "\\" in name:
                raise Failure("copy name must be a non-hidden filename")
            directory = paths(values["directory"])
            if len(directory) != 1:
                raise Failure("copy directory must be one relative path")
            target = root / directory[0] / name
            if root.resolve() not in target.resolve().parents:
                raise Failure("copy destination escapes worktree")
            source = Path(values["source"])
            if source.is_symlink() or not source.is_file():
                raise Failure("copy source must be a regular file")
            if not owned(str(target.relative_to(root)), args.selected):
                raise Failure("copy destination is outside declared ownership")
            if not inspect:
                atomic_write(target, source.read_bytes())
                target.chmod(0o644)
            continue
        argv = [job_value(item, selected_env, context) for item in step["argv"]]
        if not argv[0] or shutil.which(argv[0], path=selected_env.get("PATH")) is None:
            raise Failure(f"step {step['id']}: executable unavailable")
        if inspect:
            continue
        log(f"step/{step['id']}: running")
        result = subprocess.run(argv, cwd=root, env=selected_env, check=False)
        if result.returncode:
            message = f"step {step['id']} exited {result.returncode}"
            if step.get("on_error", "fail") != "continue":
                raise Failure(message + "; progress was not advanced")
            log("WARN: " + message + "; continuing as configured")


def transaction(args) -> int:
    if not args.selected or not args.subject or not (args.writer or args.config):
        raise Failure("--paths, --subject and a writer command after -- are required")
    durable = absolute(args.state_dir)
    if durable == args.repo or args.repo in durable.parents:
        raise Failure("durable state must be outside the checkout")
    if args.scratch == args.repo or args.repo in args.scratch.parents:
        raise Failure("scratch must be outside the checkout")
    args.scratch.mkdir(parents=True, exist_ok=True)
    durable.mkdir(parents=True, exist_ok=True)
    pending = durable.with_name(durable.name + ".publish-pending.json")
    with lock(absolute(args.lock), 0) as acquired:
        if not acquired:
            log("another writer for this task is running; exiting")
            return 0
        upstream = fetch(args.repo, args.remote, args.branch)
        if pending.exists():
            record = json.loads(pending.read_text())
            if not isinstance(record, dict):
                raise Failure("invalid pending publication record")
            revision = record["revision"]
            if (
                not isinstance(revision, str)
                or not re.fullmatch(r"[0-9a-f]{40,64}", revision)
                or not isinstance(record["paths"], list)
                or not all(isinstance(item, str) for item in record["paths"])
            ):
                raise Failure("invalid pending publication record")
            paths(" ".join(record["paths"]))
            if (
                git(
                    args.repo, "merge-base", "--is-ancestor", revision, upstream, check=False
                ).returncode
                == 0
            ):
                verify_lfs(args.repo, args.remote, revision, record["paths"])
            pending.unlink()
        run = Path(tempfile.mkdtemp(prefix=f"publish-{args.task}-", dir=args.scratch))
        worktree, staged = run / "worktree", run / "state"
        branch = "publish/" + args.task + "-" + uuid.uuid4().hex
        args.expected_branch = branch
        try:
            git(
                args.repo, "worktree", "add", "--no-checkout", "-b", branch, str(worktree), upstream
            )
            if args.sparse and not args.validate:
                git(worktree, "sparse-checkout", "set", "--cone", "--", *paths(args.sparse))
            else:
                git(worktree, "sparse-checkout", "disable")
            git(worktree, "checkout", "--quiet", branch)
            initial = text(worktree, "rev-parse", "HEAD")
            state_files(durable)
            shutil.copytree(durable, staged)
            env = dict(args.job_env if args.config else os.environ)
            env.update(
                {
                    args.worktree_env: str(worktree),
                    args.state_env: str(staged),
                    "REPOSITORY_PUBLISH_WORKTREE": str(worktree),
                    "REPOSITORY_PUBLISH_STATE": str(staged),
                    "REPOSITORY_PUBLISH_REPOSITORY": str(args.repo),
                    "REPOSITORY_PUBLISH_SUBJECT": args.subject,
                    "REPOSITORY_PUBLISH_AGENT": args.agent,
                }
            )
            log(f"txn/{args.task}: running writer")
            if args.config and args.job_steps:
                configured_steps(args, worktree, env)
            else:
                writer = args.writer[1:] if args.writer[0] == "--" else args.writer
                proc = subprocess.run(writer, cwd=worktree, env=env, check=False)
                if proc.returncode:
                    raise Failure(f"writer exited {proc.returncode}; progress was not advanced")
            if text(worktree, "rev-parse", "HEAD") != initial:
                raise Failure("writer must not create commits or change the checkout revision")
            selected = check_ownership(worktree, args.selected)
            # Stage literal changed paths; unmatched declared paths are valid
            # for empty initial runs, and Git pathspec metacharacters stay literal.
            if selected:
                git(worktree, "--literal-pathspecs", "add", "--", *selected)
            checked_policy(args.validate, worktree, env)
            check_ownership(worktree, args.selected)
            difference = git(worktree, "diff", "--cached", "--quiet", check=False).returncode
            if difference not in {0, 1}:
                raise Failure("cannot inspect staged changes")
            if difference:
                message = (
                    checked_policy(args.message, worktree, env, capture=True)
                    if args.message
                    else args.subject
                )
                if not message.strip():
                    raise Failure("message command returned an empty commit message")
                proc = subprocess.run(
                    ["git", "-C", str(worktree), "commit", "--quiet", "-F", "-"],
                    input=message.encode(),
                    capture_output=True,
                    env=env,
                    check=False,
                )
                if proc.returncode:
                    raise Failure("commit failed")
                push_existing(worktree, args, args.selected, env, pending, args.message)
            else:
                log(f"txn/{args.task}: no content changes")
            promote(staged, durable)
            pending.unlink(missing_ok=True)
            log(f"txn/{args.task}: complete; state advanced")
            return 0
        finally:
            git(args.repo, "worktree", "remove", "--force", str(worktree), check=False)
            git(args.repo, "branch", "-D", branch, check=False)
            shutil.rmtree(run, ignore_errors=True)


def parser() -> argparse.ArgumentParser:
    cache = (
        Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "repository-publish"
    )
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", help="private repository-publish-job/v1 JSON")
    p.add_argument("--steps", help="comma-separated configured step/group selection")
    p.add_argument("--doctor", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--repo", default=os.getcwd())
    p.add_argument("--task", default="writer")
    p.add_argument("--paths", default="")
    p.add_argument("--sparse", default="")
    p.add_argument("--subject", default="")
    p.add_argument("--agent", default="")
    p.add_argument("--state-dir")
    p.add_argument("--lock")
    p.add_argument("--scratch", default=str(cache / "transactions"))
    p.add_argument("--publish-lock", default=str(cache / "publish.lock"))
    p.add_argument("--remote", default="origin")
    p.add_argument("--branch", default="main")
    p.add_argument("--attempts", type=int, default=5)
    p.add_argument("--retry-delay", type=float, default=2)
    p.add_argument("--lock-timeout", type=float, default=300)
    p.add_argument("--worktree-env", default="REPOSITORY_PUBLISH_WORKTREE")
    p.add_argument("--state-env", default="SYNC_STATE_DIR")
    p.add_argument("--validate-command")
    p.add_argument("--message-command")
    modes = p.add_mutually_exclusive_group()
    modes.add_argument("--existing-worktree", action="store_true")
    p.add_argument("--expected-branch", default="")
    modes.add_argument("--verify-lfs", metavar="REVISION")
    p.add_argument("writer", nargs=argparse.REMAINDER)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["worktree"]:
        return worktree_main(argv[1:])
    args = parser().parse_args(argv)
    try:
        if args.config:
            load_job(args)
        elif args.steps or args.doctor or args.dry_run:
            raise Failure("--steps, --doctor and --dry-run require --config")
        prepare_args(args)
        env = dict(
            args.job_env if args.config else os.environ,
            REPOSITORY_PUBLISH_REPOSITORY=str(args.repo),
            REPOSITORY_PUBLISH_WORKTREE=str(args.repo),
            REPOSITORY_PUBLISH_SUBJECT=args.subject,
            REPOSITORY_PUBLISH_AGENT=args.agent,
        )
        if args.config and (args.doctor or args.dry_run):
            inspect_env = dict(env, REPOSITORY_PUBLISH_STATE=str(absolute(args.state_dir)))
            if args.job_steps:
                configured_steps(args, args.repo, inspect_env, inspect=True)
            else:
                writer = args.writer[1:] if args.writer[0] == "--" else args.writer
                if not writer or not shutil.which(writer[0], path=inspect_env.get("PATH")):
                    raise Failure("writer executable unavailable")
            print(
                json.dumps(
                    {
                        "schema": "repository-publish-inspection/v1",
                        "steps": [s["id"] for s in args.job_steps or []],
                        "valid": True,
                    }
                )
            )
        elif args.verify_lfs:
            verify_lfs(args.repo, args.remote, args.verify_lfs, args.selected)
        elif args.existing_worktree:
            if not args.expected_branch or not (args.repo / ".git").is_file():
                raise Failure("existing publication requires a linked worktree and expected branch")
            push_existing(args.repo, args, args.selected, env, message=args.message)
        else:
            return transaction(args)
        return 0
    except (Failure, OSError, ValueError, KeyError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return exc.code if isinstance(exc, Failure) and args.existing_worktree else 1


if __name__ == "__main__":
    raise SystemExit(main())
