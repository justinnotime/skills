"""Publication backends; none of them push or deploy."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path, PurePosixPath

from .audit import GIT_CRYPT_MAGIC
from .manifest import Manifest
from .model import PublicationPlan


class PublishError(RuntimeError):
    pass


def _below(path: str, root: str) -> str | None:
    value = PurePosixPath(path)
    base = PurePosixPath(root)
    if value.is_absolute() or ".." in value.parts:
        raise PublishError("publication path escapes the output root")
    try:
        relative = value.relative_to(base)
    except ValueError:
        return None
    if relative == PurePosixPath("."):
        raise PublishError("publication path names an output subtree directory")
    return relative.as_posix()


def _check_plain_ancestors(root: Path, relative: str) -> None:
    current = root
    for component in PurePosixPath(relative).parent.parts:
        current = current / component
        if current.is_symlink():
            raise PublishError("owned output subtree has a symbolic-link ancestor")
        if current.exists() and not current.is_dir():
            raise PublishError("owned output subtree has a non-directory ancestor")


def _make_plain_parents(root: Path, relative: str, created: list[Path]) -> None:
    current = root
    for component in PurePosixPath(relative).parent.parts:
        current = current / component
        if current.is_symlink():
            raise PublishError("owned output subtree has a symbolic-link ancestor")
        if current.exists():
            if not current.is_dir():
                raise PublishError("owned output subtree has a non-directory ancestor")
            continue
        current.mkdir()
        created.append(current)


def _build_staged_tree(
    root: Path, subtree: str, plan: PublicationPlan
) -> tuple[Path, Path]:
    target = root / subtree
    _check_plain_ancestors(root, subtree)
    stage = Path(
        tempfile.mkdtemp(prefix=f".{target.name}.agent-session-stage-", dir=root)
    )
    try:
        if target.is_symlink():
            raise PublishError("owned output subtree is not a plain directory")
        if target.exists():
            if not target.is_dir():
                raise PublishError("owned output subtree is not a plain directory")
            if any(path.is_symlink() for path in target.rglob("*")):
                raise PublishError("owned output subtree contains a symbolic link")
            shutil.copytree(target, stage, dirs_exist_ok=True)
        for removal in plan.removals:
            relative = _below(removal.relative_path, subtree)
            if relative is None:
                continue
            destination = stage / relative
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
        for planned in plan.writes:
            relative = _below(planned.relative_path, subtree)
            if relative is None:
                continue
            destination = stage / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(planned.content)
        return target, stage
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def publish_filesystem(manifest: Manifest, plan: PublicationPlan) -> None:
    """Swap each owned subtree atomically and roll back cross-tree failures."""
    root = manifest.output.repository_root
    if root.is_symlink() or not root.is_dir():
        raise PublishError("output repository root must be an existing plain directory")
    prepared = []
    backups: list[tuple[Path, Path | None]] = []
    created_parents: list[Path] = []
    try:
        for subtree in manifest.publisher.owned_subtrees:
            prepared.append(_build_staged_tree(root, subtree, plan))
        for target, stage in prepared:
            _make_plain_parents(
                root,
                target.relative_to(root).as_posix(),
                created_parents,
            )
            backup = None
            if target.exists():
                backup = target.with_name(
                    f".{target.name}.agent-session-old-{os.getpid()}"
                )
                if backup.exists():
                    raise PublishError("publication rollback path already exists")
                os.replace(target, backup)
            try:
                os.replace(stage, target)
            except Exception:
                if backup is not None:
                    os.replace(backup, target)
                raise
            backups.append((target, backup))
    except Exception as exc:
        for target, backup in reversed(backups):
            if backup is None:
                if target.exists():
                    shutil.rmtree(target)
            elif backup.exists():
                if target.exists():
                    shutil.rmtree(target)
                os.replace(backup, target)
        for _target, stage in prepared:
            if stage.exists():
                shutil.rmtree(stage)
        for directory in reversed(created_parents):
            try:
                directory.rmdir()
            except OSError:
                pass
        if isinstance(exc, PublishError):
            raise
        raise PublishError("atomic publication failed") from exc
    for _target, backup in backups:
        if backup is not None:
            shutil.rmtree(backup, ignore_errors=True)


def _run_git(arguments: list[str], *, cwd: Path) -> str:
    process = subprocess.run(
        ["git", *arguments], cwd=cwd, text=True, capture_output=True, check=False
    )
    if process.returncode:
        raise PublishError("git worktree preparation failed")
    return process.stdout


def _run_git_bytes(arguments: list[str], *, cwd: Path) -> bytes:
    process = subprocess.run(
        ["git", *arguments], cwd=cwd, capture_output=True, check=False
    )
    if process.returncode:
        raise PublishError("git worktree preparation failed")
    return process.stdout


def require_git_worktree_inventory_at_head(manifest: Manifest) -> None:
    """Reject output inventory that a detached HEAD worktree cannot reproduce."""
    if manifest.publisher.strategy != "git-worktree":
        return
    root = manifest.output.repository_root
    try:
        _run_git_bytes(["rev-parse", "--verify", "HEAD^{commit}"], cwd=root)
        flags = _run_git_bytes(
            [
                "--literal-pathspecs",
                "ls-files",
                "-v",
                "-z",
                "--",
                *manifest.publisher.owned_subtrees,
            ],
            cwd=root,
        )
        status = _run_git_bytes(
            [
                "--literal-pathspecs",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--ignored=matching",
                "--ignore-submodules=none",
                "--",
                *manifest.publisher.owned_subtrees,
            ],
            cwd=root,
        )
    except OSError as exc:
        raise PublishError("git-worktree output repository is unavailable") from exc
    if any(
        len(entry) < 3 or entry[:2] != b"H "
        for entry in flags.split(b"\0")
        if entry
    ):
        raise PublishError("git-worktree owned output has hidden index flags")
    if status:
        raise PublishError("git-worktree owned output differs from HEAD")


def _git_crypt_filter_for_target(target: str | None) -> bytes:
    parts = PurePosixPath(target or "").parts
    if len(parts) != 3 or parts[:2] != ("git-crypt", "keys"):
        raise PublishError("git-crypt key target is invalid")
    name = parts[2]
    return os.fsencode("git-crypt" if name == "default" else f"git-crypt-{name}")


def _require_git_crypt_attributes(
    root: Path,
    owned_subtrees: tuple[str, ...],
    plan: PublicationPlan,
    expected_filter: bytes,
) -> None:
    tracked = _run_git_bytes(
        ["--literal-pathspecs", "ls-files", "-z", "--", *owned_subtrees],
        cwd=root,
    )
    paths = sorted(
        {
            *(os.fsdecode(path) for path in tracked.split(b"\0") if path),
            *(planned.relative_path for planned in plan.writes),
        }
    )
    for offset in range(0, len(paths), 256):
        batch = paths[offset : offset + 256]
        output = _run_git_bytes(
            ["check-attr", "--cached", "-z", "filter", "--", *batch], cwd=root
        )
        fields = output.split(b"\0")
        if fields and fields[-1] == b"":
            fields.pop()
        if len(fields) != len(batch) * 3:
            raise PublishError("git-crypt filter attribute check failed")
        for index, expected_path in enumerate(batch):
            path, attribute, value = fields[index * 3 : index * 3 + 3]
            if (
                path != os.fsencode(expected_path)
                or attribute != b"filter"
                or value != expected_filter
            ):
                raise PublishError("owned output is not covered by git-crypt")


def _index_blob(root: Path, relative_path: str) -> bytes:
    listing = _run_git_bytes(
        ["ls-files", "--stage", "-z", "--", relative_path], cwd=root
    )
    entries = [entry for entry in listing.split(b"\0") if entry]
    if len(entries) != 1:
        raise PublishError("planned output is missing from the git index")
    try:
        metadata, indexed_path = entries[0].split(b"\t", 1)
        _mode, object_id, stage = metadata.split(b" ")
    except ValueError as exc:
        raise PublishError("git index entry is malformed") from exc
    if indexed_path != os.fsencode(relative_path) or stage != b"0":
        raise PublishError("planned output has an unexpected git index entry")
    try:
        object_name = object_id.decode("ascii")
    except UnicodeDecodeError as exc:
        raise PublishError("git index object ID is malformed") from exc
    return _run_git_bytes(["cat-file", "blob", object_name], cwd=root)


def _verify_git_crypt_index(root: Path, plan: PublicationPlan) -> None:
    for planned in plan.writes:
        blob = _index_blob(root, planned.relative_path)
        if not blob.startswith(GIT_CRYPT_MAGIC) or (
            planned.content and planned.content in blob
        ):
            raise PublishError("planned output is not encrypted in the git index")


def _refuse_ciphertext(root: Path, subtrees: tuple[str, ...]) -> None:
    for subtree in subtrees:
        _check_plain_ancestors(root, subtree)
        directory = root / subtree
        if not directory.exists():
            continue
        if directory.is_symlink():
            raise PublishError("git worktree output subtree is a symbolic link")
        for path in directory.rglob("*"):
            if path.is_symlink():
                raise PublishError("git worktree output contains a symbolic link")
            if not path.is_file():
                continue
            with path.open("rb") as handle:
                if handle.read(len(GIT_CRYPT_MAGIC)) == GIT_CRYPT_MAGIC:
                    raise PublishError("git worktree contains ciphertext output")


def _plain_parent(root: Path, relative: str) -> Path:
    current = root
    for component in PurePosixPath(relative).parent.parts:
        current = current / component
        if current.is_symlink():
            raise PublishError("git-crypt key link parent is a symbolic link")
        if current.exists() and not current.is_dir():
            raise PublishError("git-crypt key link parent is not a directory")
        current.mkdir(exist_ok=True)
    return current


def _resolved_git_directory(root: Path, argument: str) -> Path:
    raw = _run_git(["rev-parse", argument], cwd=root).strip()
    if not raw or "\n" in raw or "\0" in raw:
        raise PublishError("git worktree directory is invalid")
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise PublishError("git worktree directory is unavailable") from exc
    if not resolved.is_dir():
        raise PublishError("git worktree directory is unavailable")
    return resolved


def _private_git_directory(root: Path) -> Path:
    private = _resolved_git_directory(root, "--git-dir")
    common = _resolved_git_directory(root, "--git-common-dir")
    if private == common:
        raise PublishError("throwaway worktree has no private git directory")
    return private


def _private_git_path(root: Path, private_git_dir: Path, relative: str) -> Path:
    raw = _run_git(["rev-parse", "--git-path", relative], cwd=root).strip()
    if not raw or "\n" in raw or "\0" in raw:
        raise PublishError("git private path is invalid")
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve(strict=False)
    expected = (private_git_dir / relative).resolve(strict=False)
    if resolved != expected:
        raise PublishError("git private path escaped the worktree git directory")
    return expected


def prepare_git_worktree(
    manifest: Manifest, plan: PublicationPlan, destination: Path,
    *, base_ref: str | None = None,
) -> tuple[str, ...]:
    """Prepare and stage an explicit throwaway worktree, without commit or push.

    The caller owns the resulting worktree and removes it with
    ``git worktree remove <destination>`` after inspection or publication.
    """
    repository = manifest.output.repository_root
    if destination.exists():
        raise PublishError("throwaway worktree destination already exists")
    repository_real = repository.resolve(strict=True)
    destination_lexical = Path(os.path.abspath(destination))
    destination_real = destination_lexical.resolve(strict=False)
    for candidate in (destination_lexical, destination_real):
        try:
            candidate.relative_to(repository_real)
        except ValueError:
            continue
        raise PublishError("throwaway worktree must be outside its source repository")
    if base_ref is None:
        require_git_worktree_inventory_at_head(manifest)
        revision = "HEAD"
    else:
        if not isinstance(base_ref, str) or not base_ref or "\0" in base_ref:
            raise PublishError("invalid worktree base reference")
        revision = _run_git(
            ["rev-parse", "--verify", "--end-of-options", base_ref + "^{commit}"],
            cwd=repository,
        ).strip()
    _run_git(
        [
            "worktree",
            "add",
            "--no-checkout",
            "--detach",
            os.fspath(destination),
            revision,
        ],
        cwd=repository,
    )
    try:
        # Populate only the private index. Unlike checkout/reset, read-tree
        # does not invoke clean or smudge filters before the key is available.
        _run_git(["read-tree", "HEAD"], cwd=destination)
        if manifest.publisher.encryption == "git-crypt":
            if (
                manifest.publisher.key_link_source is None
                or manifest.publisher.key_link_target is None
            ):
                raise PublishError("git-crypt publication requires a key link")
            if not manifest.publisher.key_link_source.is_file():
                raise PublishError("git-crypt key source is missing")
            _require_git_crypt_attributes(
                destination,
                manifest.publisher.owned_subtrees,
                plan,
                _git_crypt_filter_for_target(manifest.publisher.key_link_target),
            )
            git_directory = _private_git_directory(destination)
            link = _private_git_path(
                destination, git_directory, manifest.publisher.key_link_target
            )
            _plain_parent(git_directory, manifest.publisher.key_link_target)
            os.symlink(manifest.publisher.key_link_source, link)
        _run_git(["reset", "--hard", "HEAD"], cwd=destination)
        _refuse_ciphertext(destination, manifest.publisher.owned_subtrees)
        return stage_git_worktree(manifest, plan, destination)
    except Exception:
        _run_git(
            ["worktree", "remove", "--force", os.fspath(destination)], cwd=repository
        )
        raise


def discard_git_worktree(repository: Path, destination: Path) -> None:
    """Discard only a caller-owned reproducible extraction attempt."""
    _run_git(["worktree", "remove", "--force", os.fspath(destination)], cwd=repository)


def stage_git_worktree(
    manifest: Manifest, plan: PublicationPlan, destination: Path
) -> tuple[str, ...]:
    """Apply an audited plan to a prepared worktree and verify its staged blobs."""
    if manifest.publisher.encryption == "git-crypt":
        _require_git_crypt_attributes(
            destination, manifest.publisher.owned_subtrees, plan,
            _git_crypt_filter_for_target(manifest.publisher.key_link_target),
        )
    worktree_manifest = replace(
        manifest,
        output=replace(manifest.output, repository_root=destination),
        publisher=replace(manifest.publisher, strategy="filesystem-atomic"),
    )
    publish_filesystem(worktree_manifest, plan)
    _run_git(["add", "--", *manifest.publisher.owned_subtrees], cwd=destination)
    if manifest.publisher.encryption == "git-crypt":
        _verify_git_crypt_index(destination, plan)
    staged = tuple(
        line
        for line in _run_git(
            ["diff", "--cached", "--name-only", "-z"], cwd=destination
        ).split("\0")
        if line
    )
    planned_paths = {planned.relative_path for planned in plan.writes}
    planned_paths.update(removal.relative_path for removal in plan.removals)
    for name in staged:
        if not any(
            name == root or name.startswith(root + "/")
            for root in manifest.publisher.owned_subtrees
        ):
            raise PublishError("git staged a path outside the owned subtrees")
        if name not in planned_paths:
            raise PublishError("git staged a path outside the publication plan")
    return staged
