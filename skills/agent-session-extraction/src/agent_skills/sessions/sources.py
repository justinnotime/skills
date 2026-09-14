"""Path confinement, candidate discovery, and stable source reads."""

from __future__ import annotations

import glob
import os
import sqlite3
import stat
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from pathlib import Path
from urllib.parse import quote

from .manifest import Discovery, RootPolicy, SourceSpec
from .model import SourceSnapshot


class SourceAccessError(RuntimeError):
    """A source could not be proven safe and stable."""


def _lexical(path: Path) -> Path:
    return Path(os.path.abspath(os.path.normpath(os.fspath(path))))


def _beneath(path: Path, roots: Iterable[Path]) -> bool:
    value = os.fspath(path)
    for root in roots:
        try:
            if os.path.commonpath((value, os.fspath(root))) == os.fspath(root):
                return True
        except ValueError:
            continue
    return False


def _has_forbidden(path: Path, policy: RootPolicy) -> bool:
    return any(part in policy.forbidden_components or any(
        fnmatchcase(part, pattern) for pattern in policy.forbidden_component_patterns
    ) for part in path.parts)


def _has_suffix(path: Path, suffixes: tuple[str, ...]) -> bool:
    if not suffixes:
        return True
    rendered = path.as_posix().rstrip("/")
    return any(rendered.endswith(suffix.rstrip("/")) for suffix in suffixes)


@dataclass(frozen=True, slots=True)
class ValidatedRoot:
    lexical: Path
    resolved: Path


def session_metadata_source(source: SourceSpec) -> SourceSpec | None:
    """Select a declared metadata file without inferring adjacent read authority."""
    path = source.decoder.get("sessions_metadata_path")
    if path is None:
        return None
    return replace(
        source,
        path=Path(path),
        path_kind="explicit",
        root_policy=replace(source.root_policy, required_suffixes=()),
        discovery=Discovery("file", (), ()),
        snapshot="stable-bytes",
    )


def validate_configured_path(source: SourceSpec) -> ValidatedRoot:
    policy = source.root_policy
    lexical = _lexical(source.path)
    if _has_forbidden(lexical, policy):
        raise SourceAccessError("configured source has a forbidden path component")
    if not _has_suffix(lexical, policy.required_suffixes):
        raise SourceAccessError("configured source does not have a required suffix")
    allowed_lexical = tuple(_lexical(root) for root in policy.allowed_lexical_roots)
    if not _beneath(lexical, allowed_lexical):
        raise SourceAccessError("configured source is outside its lexical policy")
    try:
        resolved = source.path.resolve(strict=True)
    except OSError as exc:
        raise SourceAccessError("configured source is missing or unreadable") from exc
    if _has_forbidden(resolved, policy):
        raise SourceAccessError("resolved source has a forbidden path component")
    if not _has_suffix(resolved, policy.required_suffixes):
        raise SourceAccessError("resolved source does not have a required suffix")
    allowed_resolved = tuple(
        root.resolve(strict=True) for root in policy.allowed_resolved_roots
    )
    if not _beneath(resolved, allowed_resolved):
        raise SourceAccessError("resolved source is outside its resolved-path policy")
    if policy.symlinks == "reject" and lexical != resolved:
        raise SourceAccessError("configured source traverses a symbolic link")
    return ValidatedRoot(lexical, resolved)


def discovery_roots(source: SourceSpec, root: ValidatedRoot) -> tuple[Path, ...]:
    bases = tuple(root.lexical / item for item in source.discovery.directories)
    if not bases:
        return (root.lexical,)
    for base in bases:
        validate_candidate(source, root, base)
        if base.is_symlink():
            raise SourceAccessError("selected discovery directory must not be a symlink")
        if not base.is_dir():
            raise SourceAccessError("selected discovery directory is missing or not a directory")
    return bases


def discover_candidates(source: SourceSpec, root: ValidatedRoot) -> tuple[Path, ...]:
    if source.discovery.mode == "file":
        candidates = [root.lexical]
    else:
        # ``glob`` treats an unreadable directory like an empty one. Audit the
        # declared tree first so ``allow_empty`` can never convert an access
        # failure into cleanup authority.
        def raise_walk_error(error: OSError) -> None:
            raise SourceAccessError("source tree is unreadable") from error

        # Authorize literal subtrees before walking or globbing. Keep the outer
        # root for source_ref so narrowing selection does not rename sessions.
        bases = discovery_roots(source, root)
        candidates = []
        for base in bases:
            validate_candidate(source, root, base)
            if not base.is_dir():
                raise SourceAccessError("selected discovery directory is missing or not a directory")
            try:
                for directory, subdirectories, _filenames in os.walk(
                    base, onerror=raise_walk_error
                ):
                    if source.discovery.directories and any(
                        (Path(directory) / name).is_symlink() for name in subdirectories
                    ):
                        # glob follows directory links. Refuse them before globbing
                        # so an allowed project cannot traverse a sibling tree.
                        raise SourceAccessError("selected discovery tree contains a directory symlink")
            except OSError as exc:
                raise SourceAccessError("source tree is unreadable") from exc
            for pattern in source.discovery.patterns:
                candidates.extend(
                    Path(item)
                    for item in glob.iglob(os.fspath(base / pattern), recursive=True)
                )
    unique = sorted(set(candidates), key=lambda item: item.as_posix())
    return tuple(item for item in unique if item.is_file() or item.is_symlink())


def _validate_selected_directory(source: SourceSpec, root: ValidatedRoot, resolved: Path) -> None:
    if source.discovery.directories and not _beneath(
        resolved, (root.resolved / item for item in source.discovery.directories)
    ):
        raise SourceAccessError("candidate escaped selected discovery directories")


def validate_candidate(
    source: SourceSpec, root: ValidatedRoot, candidate: Path
) -> tuple[Path, str]:
    policy = source.root_policy
    lexical = _lexical(candidate)
    if _has_forbidden(lexical, policy):
        raise SourceAccessError("candidate has a forbidden path component")
    if policy.candidate_beneath_root and not _beneath(lexical, (root.lexical,)):
        raise SourceAccessError("candidate is outside its configured source")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise SourceAccessError("candidate is missing or unreadable") from exc
    allowed_resolved = tuple(
        item.resolve(strict=True) for item in policy.allowed_resolved_roots
    )
    if not _beneath(resolved, allowed_resolved):
        raise SourceAccessError("candidate resolves outside its policy")
    if policy.candidate_beneath_root and not _beneath(resolved, (root.resolved,)):
        raise SourceAccessError("candidate resolves outside its configured source")
    _validate_selected_directory(source, root, resolved)
    if _has_forbidden(resolved, policy):
        raise SourceAccessError("resolved candidate has a forbidden path component")
    if policy.symlinks == "reject" and lexical != resolved:
        raise SourceAccessError("candidate traverses a symbolic link")
    relative = (
        lexical.relative_to(root.lexical).as_posix()
        if lexical != root.lexical
        else lexical.name
    )
    source_ref = f"{source.source_id}/{relative}"
    return resolved, source_ref


def _validate_open_descriptor(
    candidate: Path, descriptor: int, source: SourceSpec, root: ValidatedRoot
) -> os.stat_result:
    opened = os.fstat(descriptor)
    current_path = candidate.resolve(strict=True)
    current = os.stat(candidate, follow_symlinks=False)
    if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode):
        raise SourceAccessError("opened candidate is not a regular file")
    if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
        raise SourceAccessError("opened candidate no longer matches its path")
    allowed = tuple(
        item.resolve(strict=True) for item in source.root_policy.allowed_resolved_roots
    )
    if not _beneath(current_path, allowed):
        raise SourceAccessError("opened candidate escaped its resolved-path policy")
    if source.root_policy.candidate_beneath_root and not _beneath(
        current_path, (root.resolved,)
    ):
        raise SourceAccessError("opened candidate escaped its configured source")
    if _has_forbidden(current_path, source.root_policy):
        raise SourceAccessError("opened candidate has a forbidden path component")
    _validate_selected_directory(source, root, current_path)
    return opened


def stable_read(candidate: Path, source: SourceSpec, root: ValidatedRoot) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise SourceAccessError("candidate could not be opened safely") from exc
    try:
        before = _validate_open_descriptor(candidate, descriptor, source, root)
        actual_link = Path(f"/proc/self/fd/{descriptor}")
        if actual_link.exists():
            actual = actual_link.resolve(strict=True)
            _validate_selected_directory(source, root, actual)
            allowed = tuple(
                item.resolve(strict=True)
                for item in source.root_policy.allowed_resolved_roots
            )
            if not _beneath(actual, allowed):
                raise SourceAccessError(
                    "opened candidate escaped its resolved-path policy"
                )
            if source.root_policy.candidate_beneath_root and not _beneath(
                actual, (root.resolved,)
            ):
                raise SourceAccessError(
                    "opened candidate escaped its configured source"
                )
        # Session logs are append-only and often live. The bytes that existed
        # when the descriptor was opened are a consistent prefix snapshot: later
        # appends belong to the next run, and a torn final line is dropped by
        # the JSONL decoders. Only a file that shrinks under us is unstable.
        chunks = []
        remaining = before.st_size
        while remaining > 0:
            block = os.read(descriptor, min(remaining, 1024 * 1024))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        _validate_open_descriptor(candidate, descriptor, source, root)
        if remaining:
            raise SourceAccessError("candidate shrank during its stable read")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _open_read_only(path: Path, label: str) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        return os.open(path, flags)
    except OSError as exc:
        raise SourceAccessError(f"{label} could not be opened safely") from exc


def _read_descriptor(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks = []
    while True:
        block = os.read(descriptor, 1024 * 1024)
        if not block:
            return b"".join(chunks)
        chunks.append(block)


def _validate_sqlite_sidecar(
    sidecar: Path, descriptor: int, source: SourceSpec, candidate: Path
) -> os.stat_result:
    opened = os.fstat(descriptor)
    current = os.stat(sidecar, follow_symlinks=False)
    try:
        resolved = sidecar.resolve(strict=True)
    except OSError as exc:
        raise SourceAccessError("SQLite sidecar is missing or unreadable") from exc
    if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode):
        raise SourceAccessError("SQLite sidecar is not a regular file")
    if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
        raise SourceAccessError("opened SQLite sidecar no longer matches its path")
    if resolved.parent != candidate.resolve(strict=True).parent:
        raise SourceAccessError("SQLite sidecar escaped the database directory")
    allowed = tuple(
        item.resolve(strict=True) for item in source.root_policy.allowed_resolved_roots
    )
    if not _beneath(resolved, allowed):
        raise SourceAccessError("SQLite sidecar escaped its resolved-path policy")
    if _has_forbidden(resolved, source.root_policy):
        raise SourceAccessError("SQLite sidecar has a forbidden path component")
    return opened


def _write_private(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(path, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written == 0:
                raise SourceAccessError("private SQLite snapshot write stopped early")
            view = view[written:]
    finally:
        os.close(descriptor)


def sqlite_snapshot(candidate: Path, source: SourceSpec, root: ValidatedRoot) -> bytes:
    """Capture a logical SQLite view without opening the source through SQLite.

    SQLite's documented read-only mode can still update an existing ``-shm``
    file.  Read and revalidate the database and WAL ourselves, then let SQLite
    recover a private, mode-0600 copy.  The source tree is byte-for-byte
    untouched and a committed WAL transaction remains visible.
    """
    descriptor = _open_read_only(candidate, "SQLite candidate")
    wal = Path(f"{candidate}-wal")
    journal = Path(f"{candidate}-journal")
    wal_present = os.path.lexists(wal)
    if os.path.lexists(journal):
        os.close(descriptor)
        raise SourceAccessError("SQLite rollback journal requires a stable snapshot")
    wal_descriptor: int | None = None
    connection: sqlite3.Connection | None = None
    snapshot: sqlite3.Connection | None = None
    try:
        opened = _validate_open_descriptor(candidate, descriptor, source, root)
        database_payload = _read_descriptor(descriptor)
        wal_payload: bytes | None = None
        wal_opened: os.stat_result | None = None
        if wal_present:
            wal_descriptor = _open_read_only(wal, "SQLite WAL sidecar")
            wal_opened = _validate_sqlite_sidecar(
                wal, wal_descriptor, source, candidate
            )
            wal_payload = _read_descriptor(wal_descriptor)

        # A second complete pass makes a moving database fail closed.  This
        # also catches in-place changes that preserve size and timestamps.
        if database_payload != _read_descriptor(descriptor):
            raise SourceAccessError("SQLite candidate changed during its snapshot")
        finished = os.fstat(descriptor)
        _validate_open_descriptor(candidate, descriptor, source, root)
        if (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ) != (
            finished.st_dev,
            finished.st_ino,
            finished.st_size,
            finished.st_mtime_ns,
        ):
            raise SourceAccessError("SQLite candidate changed during its snapshot")
        if wal_descriptor is not None and wal_opened is not None:
            if wal_payload != _read_descriptor(wal_descriptor):
                raise SourceAccessError("SQLite WAL changed during its snapshot")
            wal_finished = os.fstat(wal_descriptor)
            _validate_sqlite_sidecar(wal, wal_descriptor, source, candidate)
            if (
                wal_opened.st_dev,
                wal_opened.st_ino,
                wal_opened.st_size,
                wal_opened.st_mtime_ns,
            ) != (
                wal_finished.st_dev,
                wal_finished.st_ino,
                wal_finished.st_size,
                wal_finished.st_mtime_ns,
            ):
                raise SourceAccessError("SQLite WAL changed during its snapshot")
        if os.path.lexists(wal) != wal_present or os.path.lexists(journal):
            raise SourceAccessError("SQLite sidecar set changed during its snapshot")

        with tempfile.TemporaryDirectory(prefix="agent-session-sqlite-") as directory:
            private_database = Path(directory) / "snapshot.db"
            _write_private(private_database, database_payload)
            if wal_payload is not None:
                _write_private(Path(f"{private_database}-wal"), wal_payload)
            encoded = quote(os.fspath(private_database), safe="/")
            connection = sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)
            connection.execute("PRAGMA query_only = ON")
            snapshot = sqlite3.connect(":memory:")
            connection.backup(snapshot)
            connection.close()
            connection = None
            if not hasattr(snapshot, "serialize"):
                raise SourceAccessError("SQLite serialization support is unavailable")
            payload_bytes = bytearray(snapshot.serialize())
        if (
            not payload_bytes.startswith(b"SQLite format 3\0")
            or len(payload_bytes) < 20
        ):
            raise SourceAccessError("SQLite snapshot has an invalid header")
        # A backup of a WAL database contains every committed page but keeps
        # the header's WAL flags. The in-memory decoder has no sidecar file, so
        # normalize only those two header bytes to rollback-journal format.
        payload_bytes[18] = 1
        payload_bytes[19] = 1
        return bytes(payload_bytes)
    except sqlite3.Error as exc:
        raise SourceAccessError("SQLite snapshot failed") from exc
    finally:
        if snapshot is not None:
            snapshot.close()
        if connection is not None:
            connection.close()
        if wal_descriptor is not None:
            os.close(wal_descriptor)
        os.close(descriptor)


def _sqlite_immutable_token(
    candidate: Path, source: SourceSpec, root: ValidatedRoot
) -> tuple[int, ...]:
    """Validate a checkpointed database for direct immutable SQLite access."""
    descriptor = _open_read_only(candidate, "SQLite candidate")
    wal = Path(f"{candidate}-wal")
    journal = Path(f"{candidate}-journal")
    wal_descriptor: int | None = None
    try:
        before = _validate_open_descriptor(candidate, descriptor, source, root)
        if hasattr(os, "pread"):
            header = os.pread(descriptor, 100, 0)
        else:  # pragma: no cover - supported platforms expose pread
            header = _read_descriptor(descriptor)[:100]
        if not header.startswith(b"SQLite format 3\0"):
            raise SourceAccessError("SQLite snapshot has an invalid header")
        if os.path.lexists(journal):
            raise SourceAccessError(
                "SQLite rollback journal requires a stable snapshot"
            )
        wal_token: tuple[int, ...] = (0, 0, 0, 0, 0)
        if os.path.lexists(wal):
            wal_descriptor = _open_read_only(wal, "SQLite WAL sidecar")
            wal_stat = _validate_sqlite_sidecar(wal, wal_descriptor, source, candidate)
            if wal_stat.st_size:
                raise SourceAccessError("immutable SQLite access requires an empty WAL")
            wal_token = (
                1,
                wal_stat.st_dev,
                wal_stat.st_ino,
                wal_stat.st_size,
                wal_stat.st_mtime_ns,
            )
        after = os.fstat(descriptor)
        _validate_open_descriptor(candidate, descriptor, source, root)
        database_token = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        if database_token != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise SourceAccessError("SQLite candidate changed during validation")
        if os.path.lexists(journal):
            raise SourceAccessError("SQLite sidecar set changed during validation")
        if bool(wal_token[0]) != os.path.lexists(wal):
            raise SourceAccessError("SQLite sidecar set changed during validation")
        return (*database_token, *wal_token)
    finally:
        if wal_descriptor is not None:
            os.close(wal_descriptor)
        os.close(descriptor)


def sqlite_immutable_snapshot(
    candidate: Path, source: SourceSpec, root: ValidatedRoot
) -> tuple[int, ...]:
    """Return a stability token for a checkpointed, immutable database.

    This mode is intended for Backup-produced SQLite snapshots. SQLite opens
    the validated database with ``mode=ro&immutable=1`` and therefore never
    creates or updates source sidecars. The caller must revalidate the token
    after decoding before accepting any session.
    """
    return _sqlite_immutable_token(candidate, source, root)


def revalidate_snapshot(
    snapshot: SourceSnapshot, source: SourceSpec, root: ValidatedRoot
) -> None:
    if snapshot.access_mode != "sqlite-immutable":
        return
    if snapshot.stability_token is None:
        raise SourceAccessError("immutable SQLite snapshot has no stability token")
    if _sqlite_immutable_token(snapshot.path, source, root) != snapshot.stability_token:
        raise SourceAccessError("SQLite source changed during decoding")


def snapshot_candidate(
    source: SourceSpec, root: ValidatedRoot, candidate: Path
) -> SourceSnapshot:
    resolved, source_ref = validate_candidate(source, root, candidate)
    access_mode = "bytes"
    stability_token = None
    if source.snapshot == "sqlite-readonly":
        payload = sqlite_snapshot(resolved, source, root)
    elif source.snapshot == "sqlite-immutable":
        payload = None
        access_mode = "sqlite-immutable"
        stability_token = sqlite_immutable_snapshot(resolved, source, root)
    else:
        payload = stable_read(resolved, source, root)
    return SourceSnapshot(
        source.source_id,
        source.harness,  # type: ignore[arg-type]
        source.output_node,
        source_ref,
        resolved,
        payload,
        source.decoder,
        access_mode,  # type: ignore[arg-type]
        stability_token,
    )
