"""Install caller-selected links or a managed crontab block."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from itertools import pairwise
from pathlib import Path

from runtime_install.config import load_config


class InstallError(Exception):
    pass


def string(value, label="value"):
    if not isinstance(value, str) or not value or any(c in value for c in "\0\r\n"):
        raise InstallError(f"invalid {label}")
    return value


def path(value):
    value = string(value, "path")
    result = Path(value).expanduser()
    if not result.is_absolute():
        raise InstallError("installation paths must be absolute")
    return result


def argv(value):
    if not isinstance(value, list) or not value:
        raise InstallError("command must be a nonempty argument array")
    return [string(part, "command argument") for part in value]


def command(spec):
    if not isinstance(spec, dict):
        raise InstallError("invalid command configuration")
    args = argv(spec.get("argv"))
    environment = dict(os.environ)
    for name in spec.get("unset_environment", []):
        environment.pop(string(name), None)
    for name, value in spec.get("environment", {}).items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise InstallError("invalid environment name")
        environment[name] = string(value)
    try:
        result = subprocess.run(
            args,
            env=environment,
            capture_output=True,
            timeout=spec.get("timeout", 60),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError(spec.get("message", "configured command failed")) from exc
    if result.returncode:
        raise InstallError(spec.get("message", "configured command failed"))


def requirements(items):
    for item in items:
        target = path(item["path"])
        kind = item.get("kind", "file")
        if kind not in {"file", "executable", "directory"}:
            raise InstallError("unknown requirement kind")
        ok = target.is_dir() if kind == "directory" else target.is_file()
        if kind == "executable":
            ok = ok and os.access(target, os.X_OK)
        if not ok:
            raise InstallError(
                item.get("message", "required installation source is missing")
            )


def parents_ok(target):
    for parent in target.parents:
        if parent.exists() and not parent.is_dir():
            raise InstallError("installation parent is not a directory")
        if parent.is_symlink() and not parent.exists():
            raise InstallError("installation parent is a broken symlink")


@contextlib.contextmanager
def lock(filename):
    target = path(filename)
    parents_ok(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Follow no symlink at the lock itself: every installer must share one inode.
    fd = os.open(target, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def link_plan(config):
    packages = config.get("packages")
    if not isinstance(packages, dict) or not packages:
        raise InstallError("packages must be a nonempty object")
    operations = []
    sources = {}
    destinations = [path(value) for value in config["destinations"]]
    if not destinations:
        raise InstallError("at least one discovery destination is required")
    for name, package in packages.items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name):
            raise InstallError("invalid package name")
        source = path(package["source"])
        sources[name] = str(source)
        if not source.is_dir():
            raise InstallError(f"{name} is missing")
        checks = []
        for req in package["required"]:
            relative = Path(req["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise InstallError("required entry escapes its package")
            checks.append(
                {
                    "path": str(source / relative),
                    "kind": req.get("kind", "file"),
                    "message": f"{name} is missing",
                }
            )
        requirements(checks)
        for destination in destinations:
            target = destination / name
            action = "link" if target.is_symlink() or not target.exists() else "keep"
            operations.append(
                {"path": str(target), "target": str(source), "action": action}
            )
    for profile in config.get("profiles", []):
        source, target = path(profile["source"]), path(profile["destination"])
        if not source.is_file():
            raise InstallError("configured profile source is missing")
        owned = target.is_symlink() and target.resolve() == source.resolve()
        action = (
            "link" if owned or not (target.exists() or target.is_symlink()) else "keep"
        )
        operations.append(
            {
                "path": str(target),
                "target": os.path.relpath(source, target.parent),
                "action": action,
            }
        )
    for retired in config.get("retired_links", []):
        target = path(retired["path"])
        replacement = path(retired["replacement"])
        replacement_plan = next(
            (p for p in operations if p["path"] == str(replacement)), None
        )
        replacement_available = (
            replacement_plan and replacement_plan["action"] == "link"
        ) or replacement.is_symlink()
        if (
            replacement_available
            and target.is_symlink()
            and os.path.normpath(os.readlink(target))
            in [os.path.normpath(value) for value in retired["owned_targets"]]
        ):
            operations.append({"path": str(target), "action": "remove"})
    names = [item["path"] for item in operations]
    if len(set(names)) != len(names):
        raise InstallError("duplicate installation destination")
    for item in operations:
        parents_ok(Path(item["path"]))
    return sources, operations


def make_parents(target, created):
    missing = []
    parent = target.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for parent in reversed(missing):
        parent.mkdir()
        created.append(parent)


def set_link(target, value):
    if not target.is_symlink():
        # Creation is exclusive, never replace a file created since planning.
        os.symlink(value, target)
        return
    fd, temporary = tempfile.mkstemp(prefix=".runtime-install-", dir=target.parent)
    os.close(fd)
    os.unlink(temporary)
    try:
        os.symlink(value, temporary)
        if not target.is_symlink():
            raise InstallError("installation destination changed during installation")
        os.replace(temporary, target)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def apply_links(operations):
    changed = []
    created = []
    try:
        for item in operations:
            if item["action"] == "keep":
                continue
            target = Path(item["path"])
            before = os.readlink(target) if target.is_symlink() else None
            if target.exists() and before is None:
                raise InstallError(
                    "installation destination changed during installation"
                )
            make_parents(target, created)
            if item["action"] == "remove":
                target.unlink()
            else:
                set_link(target, item["target"])
            changed.append((target, before))
    except Exception:
        rollback_failed = False
        for target, before in reversed(changed):
            try:
                if before is None:
                    if target.is_symlink():
                        target.unlink()
                else:
                    set_link(target, before)
            except OSError:
                rollback_failed = True
        for directory in reversed(created):
            try:
                directory.rmdir()
            except OSError:
                pass
        if rollback_failed:
            raise InstallError(
                "link installation failed; rollback incomplete"
            ) from None
        raise


def tokens(line):
    try:
        result = shlex.split(line, comments=True)
    except ValueError:
        return []
    # A shell -c command is one shell argument; inspect that command once too.
    for i, value in enumerate(result[:-1]):
        if value in {"-c", "-lc"}:
            try:
                result += shlex.split(result[i + 1], comments=True)
            except ValueError:
                pass
            break
    return result


def job_line(job):
    """Quote a caller-selected schedule, argument vector and output path."""
    if not isinstance(job, dict):
        raise InstallError("invalid cron job")
    schedule = string(job.get("schedule"), "cron schedule")
    if len(schedule.split()) != 5:
        raise InstallError("cron schedule must have five fields")
    env = []
    for name, value in job.get("environment", {}).items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise InstallError("invalid cron environment name")
        if value:
            env.append(name + "=" + shlex.quote(string(value)))
    parts = [*env, shlex.join(argv(job.get("argv")))]
    if job.get("log"):
        parts.extend([">>", shlex.quote(str(path(job["log"]))), "2>&1"])
    line = schedule + " " + " ".join(parts)
    if "%" in line:
        raise InstallError("structured cron jobs do not support percent characters")
    return line


def cron_config(config):
    if "jobs" not in config:
        return config
    if "lines" in config:
        raise InstallError("configure either cron jobs or lines, not both")
    result = dict(config)
    jobs = config["jobs"]
    if not isinstance(jobs, list):
        raise InstallError("cron jobs must be an array")
    result["lines"] = [job_line(job) for job in jobs]
    return result


def cron_text(original, config):
    begin, end = [string(v, "marker") for v in config["markers"]]
    blocks = [[begin, end]] + config.get("legacy_markers", [])
    lines = original.splitlines(keepends=True)
    stripped = [line.rstrip("\r\n") for line in lines]
    intervals = []
    for markers in blocks:
        if len(markers) != 2 or markers[0] == markers[1]:
            raise InstallError("invalid cron markers")
        starts = [i for i, line in enumerate(stripped) if line == markers[0]]
        ends = [i for i, line in enumerate(stripped) if line == markers[1]]
        if len(starts) != len(ends) or len(starts) > 1:
            raise InstallError("refusing malformed cron marker block")
        if starts:
            a, b = starts[0], ends[0]
            if a >= b:
                raise InstallError("refusing reversed cron marker block")
            if any(
                line.startswith(
                    tuple(config.get("nested_marker_prefixes", ["# BEGIN ", "# END "]))
                )
                for line in stripped[a + 1 : b]
            ):
                raise InstallError("refusing nested cron marker block")
            intervals.append((a, b))
    intervals.sort()
    if any(a <= previous_b for (_, previous_b), (a, _) in pairwise(intervals)):
        raise InstallError("refusing overlapping cron marker blocks")
    remove = [argv(item) for item in config.get("remove_commands", [])]
    kept = []
    for i, line in enumerate(lines):
        if (any(a <= i <= b for a, b in intervals)
                or stripped[i] in config.get("remove_lines", [])
                or stripped[i] in config["lines"]):
            continue
        parts = tokens(stripped[i])
        if any(
            any(parts[j : j + len(match)] == match for j in range(len(parts)))
            for match in remove
        ):
            continue
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    body = [string(line, "cron line") for line in config["lines"]]
    if not body:
        raise InstallError("managed cron block must not be empty")
    if any(line in {marker for pair in blocks for marker in pair} for line in body):
        raise InstallError("managed content contains a marker")
    prefix = "".join(kept)
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    return (
        prefix
        + ("\n" if prefix or config.get("leading_blank", False) else "")
        + "\n".join([begin, *body, end, ""])
    )


def read_cron(executable):
    result = subprocess.run(executable + ["-l"], capture_output=True, check=False)
    if result.returncode:
        if result.returncode == 1 and b"no crontab" in result.stderr.lower():
            return b"", False
        raise InstallError("unable to read crontab")
    return result.stdout, True


def write_cron(executable, content):
    with tempfile.NamedTemporaryFile(prefix="runtime-install-cron-") as handle:
        handle.write(content)
        handle.flush()
        result = subprocess.run(
            executable + [handle.name], capture_output=True, check=False
        )
    if result.returncode:
        raise InstallError("crontab installation failed")


def install_cron(config, *, dry_run):
    executable = argv(config.get("crontab_command", ["crontab"]))
    requirements(config.get("requirements", []))
    for check in config.get("checks", []):
        command(check)
    # Configuration validation precedes every mutation, including lock creation.
    cron_text("", config)
    backup_dir = path(config["backup_directory"])
    directories = [path(value) for value in config.get("directories", [])]
    for target in [backup_dir / "backup", *[p / "entry" for p in directories]]:
        parents_ok(target)
    if dry_run:
        old, _ = read_cron(executable)
        print(cron_text(old.decode("utf-8", errors="surrogateescape"), config), end="")
        return
    with lock(config["lock"]):
        old, existed = read_cron(executable)
        new = cron_text(old.decode("utf-8", errors="surrogateescape"), config).encode(
            "utf-8", errors="surrogateescape"
        )
        for step in config.get("before_apply", []):
            command(step)
        for directory in [backup_dir, *directories]:
            directory.mkdir(parents=True, exist_ok=True)
        fd, backup = tempfile.mkstemp(prefix="crontab-before-install-", dir=backup_dir)
        with os.fdopen(fd, "wb") as handle:
            handle.write(old)
        try:
            write_cron(executable, new)
            installed, _ = read_cron(executable)
            if installed != new:
                raise InstallError("crontab verification failed")
        except Exception:  # noqa: BLE001 - Restore the old crontab after any failed write or verification.
            try:
                if existed:
                    write_cron(executable, old)
                elif subprocess.run(
                    executable + ["-r"], capture_output=True, check=False
                ).returncode:
                    raise InstallError("unable to restore absent crontab")
            except Exception:  # noqa: BLE001 - Report failed restoration without external diagnostics.
                raise InstallError(
                    "installation failed; previous crontab restoration failed"
                ) from None
            raise InstallError(
                "installation verification failed; previous crontab restored"
            ) from None
        print(
            f"OK installed managed cron block; previous crontab backed up at {backup}"
        )


def main(kind, args=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", required=True, help="explicit JSON file, or - for stdin"
    )
    parser.add_argument("--dry-run", action="store_true")
    if kind == "skills":
        parser.add_argument("--print-sources", action="store_true")
    else:
        parser.add_argument(
            "--print-job",
            help="print one configured cron job without checks or installation",
        )
    options = parser.parse_args(args)
    try:
        config = load_config(options.config)
        if (
            not isinstance(config, dict)
            or config.get("schema") != "runtime-install/v1"
            or config.get("kind") != kind
        ):
            raise InstallError("invalid installation configuration")
        if kind == "cron":
            if options.print_job:
                jobs = [
                    job
                    for job in config.get("jobs", [])
                    if job.get("id") == options.print_job
                ]
                if len(jobs) != 1:
                    raise InstallError(
                        "configured cron job selection is missing or ambiguous"
                    )
                print(job_line(jobs[0]))
            else:
                config = cron_config(config)
                install_cron(config, dry_run=options.dry_run)
        else:
            sources, operations = link_plan(config)
            if options.print_sources:
                print(json.dumps(sources, sort_keys=True))
            elif options.dry_run:
                print(json.dumps(operations, sort_keys=True, indent=2))
            else:
                with lock(config["lock"]):
                    _, operations = link_plan(config)
                    apply_links(operations)
                print("OK applied configured runtime links; custom entries preserved")
        return 0
    except (InstallError, OSError, ValueError, KeyError, TypeError) as exc:
        # External command output is intentionally not included in diagnostics.
        detail = (
            str(exc)
            if isinstance(exc, InstallError)
            else "invalid configuration or local filesystem failure"
        )
        print(f"FAIL: {detail}", file=sys.stderr)
        return 1
