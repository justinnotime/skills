"""Scheduled extraction with an explicitly configured external publisher."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from . import api
from .configuration import expand_environment, require_external_config
from .manifest import ManifestError
from .model import Diagnostic, ReconcileReport
from .reconcile import write_failure_marker

SCHEMA = "agent-session-schedule/v1"
REPORT = "agent-session-scheduled-report/v1"


class ScheduleError(RuntimeError):
    """A diagnostic code safe for a scheduler log."""


def emit(status: str, **values: object) -> None:
    print(json.dumps({"at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "schema_version": REPORT, "status": status, **values}, sort_keys=True))


def load_schedule(path: Path) -> dict:
    cfg = json.loads(path.read_text())
    required = {"schema_version", "manifest", "repository_root", "publication"}
    if (not isinstance(cfg, dict) or not required <= cfg.keys()
            or cfg.keys() - required - {"environment", "failure_marker", "validate_command", "preflight_command",
                                                      "expand_environment", "require_external_config"}
            or cfg["schema_version"] != SCHEMA):
        raise ScheduleError("invalid_schedule")
    for option in ("expand_environment", "require_external_config"):
        if option in cfg and not isinstance(cfg[option], bool):
            raise ScheduleError("invalid_schedule")
    if cfg.get("expand_environment"):
        try:
            cfg = expand_schedule_environment(cfg)
        except ValueError as exc:
            raise ScheduleError("invalid_environment_reference") from exc
    for field in ("manifest", "repository_root", "failure_marker"):
        if field in cfg and (not isinstance(cfg[field], str) or not Path(cfg[field]).is_absolute()):
            raise ScheduleError("absolute_path_required")
    publication = cfg["publication"]
    if (not isinstance(publication, dict)
            or not {"command", "output_root_environment"} <= publication.keys()
            or publication.keys() - {"command", "output_root_environment", "base_ref_environment"}):
        raise ScheduleError("invalid_publisher")
    for command in (publication["command"], cfg.get("validate_command", []),
                    cfg.get("preflight_command", [])):
        if (not isinstance(command, list)
                or any(not isinstance(arg, str) or "\0" in arg for arg in command)):
            raise ScheduleError("invalid_command")
    if not publication["command"]:
        raise ScheduleError("publisher_required")
    name = publication["output_root_environment"]
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ScheduleError("invalid_output_environment")
    if "base_ref_environment" in publication:
        reference_name = publication["base_ref_environment"]
        if (not isinstance(reference_name, str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", reference_name)
                or reference_name == name):
            raise ScheduleError("invalid_base_ref_environment")
    env = cfg.get("environment", {})
    if not isinstance(env, dict) or any(
        not isinstance(k, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k)
        or not isinstance(v, str) or "\0" in v for k, v in env.items()
    ):
        raise ScheduleError("invalid_environment")
    if cfg.get("require_external_config"):
        try:
            require_external_config(path, Path(cfg["repository_root"]))
        except ValueError as exc:
            raise ScheduleError("external_config_required") from exc
    return cfg


def expand_schedule_environment(cfg: dict) -> dict:
    """Expand explicit values once; existing configurations retain literal argv."""
    env = cfg.get("environment", {})
    if not isinstance(env, dict) or any(not isinstance(v, str) for v in env.values()):
        raise ScheduleError("invalid_environment")
    cfg["environment"] = {key: expand_environment(value, os.environ) for key, value in env.items()}
    environment = {**os.environ, **cfg["environment"]}
    for field in ("manifest", "repository_root", "failure_marker"):
        if isinstance(cfg.get(field), str):
            cfg[field] = expand_environment(cfg[field], environment)
    publication = cfg.get("publication")
    commands = [(cfg, "validate_command"), (cfg, "preflight_command")]
    if isinstance(publication, dict):
        commands.append((publication, "command"))
    for mapping, key in commands:
        command = mapping.get(key)
        if isinstance(command, list):
            mapping[key] = [expand_environment(arg, environment) if isinstance(arg, str)
                            else arg for arg in command]
    return cfg


def context(cfg: dict):
    manifest = api.load_manifest(cfg["manifest"], environ={**os.environ, **cfg.get("environment", {})})
    if manifest.output.repository_root.resolve() != Path(cfg["repository_root"]).resolve():
        raise ScheduleError("repository_mismatch")
    # The external publisher always owns commit/push. With git-worktree,
    # the runtime also prepares and audits the explicitly reserved checkout.
    if manifest.publisher.strategy not in {"filesystem-atomic", "git-worktree"}:
        raise ScheduleError("external_publication_requires_mutating_output")
    if not manifest.redaction.required or not all((
        manifest.gates.require_redaction_self_test, manifest.gates.require_output_audit,
        manifest.gates.require_reconciliation, manifest.gates.require_prepublication_scan,
    )):
        raise ScheduleError("required_extraction_checks_disabled")
    return manifest


def substitutions(cfg: dict, manifest, output: Path | None = None) -> dict[str, str]:
    # owned_paths is convenient for publishers accepting one list argument.
    # Reject ambiguous names rather than silently changing that list.
    paths = manifest.publisher.owned_subtrees
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", path) for path in paths):
        raise ScheduleError("unsupported_publisher_path")
    return {"{manifest}": cfg["manifest"], "{repository_root}": cfg["repository_root"],
            "{owned_paths}": " ".join(paths), "{output_root}": str(output) if output else ""}


def expand(command: list[str], values: dict[str, str]) -> list[str]:
    args = []
    for arg in command:
        for token, value in values.items():
            arg = arg.replace(token, value)
        args.append(arg)
    return args


def require_worktree(root: Path, repository: Path) -> None:
    if not root.is_absolute() or root.resolve() == repository.resolve():
        raise ScheduleError("isolated_worktree_required")
    def git(path: Path, *args: str) -> str:
        result = subprocess.run(["git", "-C", str(path), *args], check=True,
                                capture_output=True, text=True)
        return result.stdout.strip()
    try:
        top = Path(git(root, "rev-parse", "--show-toplevel")).resolve()
        expected = Path(git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        actual = Path(git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"))
        if top != root.resolve() or actual.resolve() != expected.resolve():
            raise ScheduleError("foreign_worktree")
        git_dir = Path(git(root, "rev-parse", "--absolute-git-dir"))
        if git_dir.resolve() == actual.resolve():
            raise ScheduleError("isolated_worktree_required")
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ScheduleError("worktree_unavailable") from exc


def extract(cfg: dict, manifest, *, dry_run: bool) -> dict:
    prepare_worktree = not dry_run and manifest.publisher.strategy == "git-worktree"
    base_ref = None
    reference_name = cfg["publication"].get("base_ref_environment")
    if not dry_run and reference_name is not None:
        if not prepare_worktree:
            raise ScheduleError("base_ref_requires_git_worktree")
        base_ref = os.environ.get(reference_name)
        if not base_ref:
            raise ScheduleError("publisher_base_ref_missing")
    if dry_run:
        output = Path(cfg["repository_root"])
    else:
        value = os.environ.get(cfg["publication"]["output_root_environment"])
        if not value:
            raise ScheduleError("publisher_output_missing")
        output = Path(value)
        if prepare_worktree:
            if (not output.is_absolute() or output.exists() or output.is_symlink()
                    or output.resolve().is_relative_to(Path(cfg["repository_root"]).resolve())):
                raise ScheduleError("unused_external_worktree_required")
        else:
            require_worktree(output, Path(cfg["repository_root"]))
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        report = api.run(cfg["manifest"], dry_run=dry_run,
                         output_root=None if prepare_worktree else output,
                         git_worktree_destination=output if prepare_worktree else None,
                         git_worktree_ref=base_ref,
                         environ={**os.environ, **cfg.get("environment", {})},
                         failure_marker=None if dry_run else marker(cfg))
    if prepare_worktree:
        require_worktree(output, Path(cfg["repository_root"]))
    if not dry_run and cfg.get("validate_command"):
        result = subprocess.run(expand(cfg["validate_command"], substitutions(cfg, manifest, output)),
                                cwd=output, capture_output=True, check=False)
        if result.returncode:
            raise ScheduleError("output_validation_failed")
    return asdict(report)


def marker(cfg: dict | None) -> Path | None:
    return Path(cfg["failure_marker"]) if cfg and cfg.get("failure_marker") else None


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog="agent-session-extraction run", description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--doctor", action="store_true")
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--write", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    cfg = None
    try:
        cfg = load_schedule(args.config)
        manifest = context(cfg)
        if cfg.get("preflight_command"):
            preflight = subprocess.run(
                expand(cfg["preflight_command"], substitutions(cfg, manifest)),
                cwd=cfg["repository_root"],
                env={**os.environ, **cfg.get("environment", {})},
                capture_output=True, check=False)
            if preflight.returncode:
                raise ScheduleError("preflight_failed")
        if args.doctor:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = api.doctor(cfg["manifest"], environ={**os.environ, **cfg.get("environment", {})})
            emit(result["status"], mode="doctor", source_count=len(result["sources"]))
            return 0 if result["status"] == "ok" else 1
        if args.dry_run or args.write:
            result = extract(cfg, manifest, dry_run=args.dry_run)
            emit(result["status"], mode="dry-run" if args.dry_run else "extract",
                 **{key: result[key] for key in ("source_count", "session_count", "write_count", "removal_count")})
            return 0
        command = expand(cfg["publication"]["command"], substitutions(cfg, manifest))
        command += [sys.executable, "-B", "-m", "agent_skills.sessions.scheduled",
                    "--config", str(args.config.resolve()), "--write"]
        environment = {**os.environ, **cfg.get("environment", {}), "PYTHONDONTWRITEBYTECODE": "1"}
        source_root = str(Path(__file__).resolve().parents[2])
        environment["PYTHONPATH"] = source_root + os.pathsep + environment.get("PYTHONPATH", "")
        result = subprocess.run(command, env=environment, capture_output=True, text=True, check=False)
        # Forward only this module's bounded reports, never publisher output or
        # arbitrary source text. A busy/no-op publisher may not invoke a reader.
        reports = []
        for line in result.stdout.splitlines():
            try:
                record = json.loads(line.strip())
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("schema_version") == REPORT:
                reports.append(record)
        if reports and reports[-1].get("status") == "failed":
            print(json.dumps(reports[-1], sort_keys=True))
            return 2
        if result.returncode:
            raise ScheduleError("publication_failed")
        if reports:
            print(json.dumps(reports[-1], sort_keys=True))
        else:
            emit("skipped", mode="publish")
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, ScheduleError) else "scheduled_extraction_failed"
        if isinstance(exc, ManifestError):
            code = "invalid_manifest"
        diagnostics = ()
        checks = ()
        if isinstance(exc, api.PipelineError):
            code, diagnostics, checks = exc.code, exc.diagnostics, exc.checks
        if not args.doctor and not args.dry_run and marker(cfg):
            try:
                write_failure_marker(marker(cfg), ReconcileReport(
                    False, checks, diagnostics or (Diagnostic(code, "scheduler"),)))
            except OSError:
                code = "failure_marker_write_failed"
        emit("failed", code=code, diagnostics=[
            {"code": item.code, "source_id": item.source_id} for item in diagnostics])
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
