"""Run translation in a persistent checkout through an external publisher."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

SCHEMA = "prompt-translation-schedule/v1"
DEFAULT_CONFIG = "~/.config/prompt-translation/schedule.json"
DEFAULT_RUNTIME_CONFIG = "~/.config/prompt-translation/config.json"


class ScheduleError(RuntimeError):
    """A scheduler diagnostic that contains no command or document contents."""


def home(value: str) -> str:
    return os.path.expanduser(
        re.sub(r"\$\{HOME\}|\$HOME(?![A-Za-z0-9_])", lambda _: str(Path.home()), value)
    )


def absolute(value: object) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ScheduleError("invalid_path")
    path = Path(home(value))
    if not path.is_absolute():
        raise ScheduleError("absolute_path_required")
    return str(path.resolve())


def command(value: object) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item or "\0" in item for item in value)
    ):
        raise ScheduleError("invalid_command")
    return [home(item) for item in value]


def positive(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ScheduleError("positive_integer_required")
    return value


def load_schedule(path: Path) -> dict:
    cfg = json.loads(path.read_text())
    required = {
        "schema_version",
        "repository_root",
        "worktree",
        "task_branch",
        "lock",
        "publisher_command",
        "publication",
        "job",
        "selection",
    }
    if (
        not isinstance(cfg, dict)
        or not required <= cfg.keys()
        or cfg.keys() - required - {"runtime_config", "environment", "timeout_seconds"}
        or cfg["schema_version"] != SCHEMA
    ):
        raise ScheduleError("invalid_schedule")
    cfg["runtime_config"] = cfg.get("runtime_config", DEFAULT_RUNTIME_CONFIG)
    for key in ("repository_root", "worktree", "lock", "runtime_config"):
        cfg[key] = absolute(cfg[key])
    repository, worktree = Path(cfg["repository_root"]), Path(cfg["worktree"])
    if (
        worktree == repository
        or worktree.is_relative_to(repository)
        or repository.is_relative_to(worktree)
    ):
        raise ScheduleError("separate_worktree_required")
    for key in ("lock", "runtime_config"):
        if Path(cfg[key]).is_relative_to(worktree):
            raise ScheduleError("configuration_and_lock_must_be_outside_worktree")
    if path.resolve().is_relative_to(worktree):
        raise ScheduleError("schedule_must_be_outside_worktree")
    if cfg["lock"] in {cfg["runtime_config"], str(path.resolve())} or Path(
        cfg["lock"]
    ).is_relative_to(repository):
        raise ScheduleError("separate_lock_required")
    cfg["publisher_command"] = command(cfg["publisher_command"])
    branch = cfg["task_branch"]
    if (
        not isinstance(branch, str)
        or not branch
        or branch.startswith("-")
        or any(c.isspace() for c in branch)
    ):
        raise ScheduleError("invalid_task_branch")
    publication = cfg["publication"]
    if (
        not isinstance(publication, dict)
        or not {"owned_paths", "subject"} <= publication.keys()
        or publication.keys()
        - {
            "remote",
            "branch",
            "owned_paths",
            "subject",
            "agent",
            "publish_lock",
            "message_command",
        }
    ):
        raise ScheduleError("invalid_publication")
    for key, default in (("remote", "origin"), ("branch", "main"), ("agent", "")):
        publication.setdefault(key, default)
    for key in ("remote", "branch", "subject", "agent"):
        value = publication[key]
        if not isinstance(value, str) or "\0" in value or (key != "agent" and not value):
            raise ScheduleError("invalid_publication")
    if (
        publication["remote"].startswith("-")
        or publication["branch"].startswith("-")
        or branch == publication["branch"]
    ):
        raise ScheduleError("separate_task_branch_required")
    paths = publication["owned_paths"]
    if (
        not isinstance(paths, list)
        or not paths
        or any(
            not isinstance(item, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", item)
            or any(part in {"", ".", ".."} for part in item.split("/"))
            for item in paths
        )
    ):
        raise ScheduleError("invalid_owned_paths")
    if "publish_lock" in publication:
        publication["publish_lock"] = absolute(publication["publish_lock"])
        publish_lock = Path(publication["publish_lock"])
        if (
            publication["publish_lock"] in {cfg["lock"], cfg["runtime_config"], str(path.resolve())}
            or publish_lock.is_relative_to(repository)
            or publish_lock.is_relative_to(worktree)
        ):
            raise ScheduleError("separate_publication_lock_required")
    if "message_command" in publication:
        publication["message_command"] = command(publication["message_command"])
    job = cfg["job"]
    if (
        not isinstance(job, dict)
        or not {"validate_command", "commit_command"} <= job.keys()
        or job.keys() - {"validate_command", "commit_command", "recover_command"}
    ):
        raise ScheduleError("invalid_job_policy")
    # Read old profiles without invoking their former output-discard command.
    job.pop("recover_command", None)
    for key in job:
        job[key] = command(job[key])
    environment = cfg.get("environment", {})
    if not isinstance(environment, dict) or any(
        not isinstance(key, str)
        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
        or not isinstance(value, str)
        or "\0" in value
        for key, value in environment.items()
    ):
        raise ScheduleError("invalid_environment")
    cfg["environment"] = {key: home(value) for key, value in environment.items()}
    cfg["timeout_seconds"] = positive(cfg.get("timeout_seconds", 2700))
    if not isinstance(cfg["selection"], dict) or cfg["selection"].keys() - {
        "date",
        "since_date",
        "through_date",
        "days",
        "limit_days",
    }:
        raise ScheduleError("invalid_selection")
    return cfg


def translate_arguments(cfg: dict, args: argparse.Namespace) -> list[str]:
    selection = dict(cfg["selection"])
    for key in ("date", "since_date", "days"):
        if getattr(args, key) is not None:
            selection = {
                name: value
                for name, value in selection.items()
                if name not in {"date", "since_date", "days"}
            }
            selection[key] = getattr(args, key)
    for key in ("through_date", "limit_days"):
        if getattr(args, key) is not None:
            selection[key] = getattr(args, key)
    if sum(key in selection for key in ("date", "since_date", "days")) != 1:
        raise ScheduleError("one_date_selection_required")
    limit = positive(selection.get("limit_days", 25))
    for key in ("date", "since_date", "through_date"):
        if key in selection:
            value = selection[key]
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ScheduleError("invalid_date")
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ScheduleError("invalid_date") from exc
    result = ["--strict"]
    if "days" in selection:
        result += [
            "--days",
            str(positive(selection["days"])),
            "--limit-files",
            str(limit),
        ]
    elif "date" in selection:
        result += ["--date", selection["date"], "--limit-days", str(limit)]
    else:
        through = selection.get(
            "through_date",
            (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat(),
        )
        if selection["since_date"] > through:
            raise ScheduleError("invalid_date_range")
        result += [
            "--since-date",
            selection["since_date"],
            "--through-date",
            through,
            "--oldest-first",
            "--limit-days",
            str(limit),
        ]
    if args.force:
        result.append("--force")
    return result


def expand(argv: list[str], cfg: dict, scope: str = "worktree") -> list[str]:
    values = {
        "{worktree}": cfg["worktree"],
        "{repository}": cfg["repository_root"],
        "{scope}": scope,
    }
    for token, value in values.items():
        argv = [arg.replace(token, value) for arg in argv]
    return argv


def environment(cfg: dict) -> dict[str, str]:
    env = {**os.environ, **cfg["environment"], "PYTHONDONTWRITEBYTECODE": "1"}
    env["PYTHONPATH"] = (
        str(Path(__file__).resolve().parents[1]) + os.pathsep + env.get("PYTHONPATH", "")
    )
    return env


def call(
    argv: list[str], cfg: dict, *, capture: bool = False, cwd: str | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        cwd=cwd or cfg["repository_root"],
        env=environment(cfg),
        capture_output=capture,
        check=False,
    )


def publisher(cfg: dict, *args: str, capture: bool = False) -> subprocess.CompletedProcess:
    return call(cfg["publisher_command"] + list(args), cfg, capture=capture)


def inspect(cfg: dict, action: str) -> bytes:
    publication = cfg["publication"]
    args = [
        "worktree",
        action,
        "--repo",
        cfg["worktree"],
        "--remote",
        publication["remote"],
        "--branch",
        publication["branch"],
    ]
    if action == "changed":
        args.append("--null")
    result = publisher(cfg, *args, capture=True)
    if result.returncode:
        raise ScheduleError("worktree_inspection_failed")
    return result.stdout


def ahead(cfg: dict) -> int:
    value = inspect(cfg, "ahead").strip()
    if not re.fullmatch(rb"[0-9]+", value):
        raise ScheduleError("invalid_ahead_count")
    return int(value)


def policy(cfg: dict, mode: str, scope: str = "worktree") -> int:
    argv = cfg["job"][mode + "_command"]
    return call(expand(argv, cfg, scope), cfg, cwd=cfg["worktree"]).returncode


def commit_completed(cfg: dict) -> None:
    if not inspect(cfg, "changed"):
        return
    if policy(cfg, "validate"):
        raise ScheduleError("translation_validation_failed_outputs_retained")
    if policy(cfg, "commit") not in {0, 2} or inspect(cfg, "changed"):
        raise ScheduleError("commit_failed_outputs_retained")


def publish_completed(cfg: dict) -> None:
    if not ahead(cfg):
        return
    publication = cfg["publication"]
    argv = [
        "--repo",
        cfg["worktree"],
        "--existing-worktree",
        "--expected-branch",
        cfg["task_branch"],
        "--remote",
        publication["remote"],
        "--branch",
        publication["branch"],
        "--paths",
        " ".join(publication["owned_paths"]),
        "--subject",
        publication["subject"],
        "--agent",
        publication["agent"],
        "--validate-command",
        json.dumps(expand(cfg["job"]["validate_command"], cfg, "committed")),
    ]
    if "publish_lock" in publication:
        argv += ["--publish-lock", publication["publish_lock"]]
    if "message_command" in publication:
        argv += [
            "--message-command",
            json.dumps(expand(publication["message_command"], cfg, "committed")),
        ]
    result = publisher(cfg, *argv)
    if result.returncode:
        raise ScheduleError("publication_failed_outputs_retained")
    if inspect(cfg, "changed") or ahead(cfg):
        raise ScheduleError("publication_incomplete_outputs_retained")


def translator_command(cfg: dict, root: str, arguments: list[str]) -> list[str]:
    return [
        cfg["environment"].get("PROMPT_TRANSLATION_PYTHON", sys.executable),
        "-B",
        "-m",
        "prompt_translation.translate",
        "--config",
        cfg["runtime_config"],
        "--root",
        root,
        *arguments,
    ]


def translate(cfg: dict, root: str, arguments: list[str]) -> int:
    with subprocess.Popen(
        translator_command(cfg, root, arguments),
        cwd=root,
        env=environment(cfg),
        start_new_session=True,
    ) as process:
        try:
            code = process.wait(timeout=cfg["timeout_seconds"])
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            return 124
    return code if code >= 0 else 128 - code


def doctor(cfg: dict) -> int:
    commands = [cfg["publisher_command"], *cfg["job"].values()]
    if "message_command" in cfg["publication"]:
        commands.append(cfg["publication"]["message_command"])
    for argv in commands:
        if shutil.which(expand(argv, cfg)[0], path=environment(cfg).get("PATH")) is None:
            raise ScheduleError("configured_command_unavailable")
    if not Path(cfg["runtime_config"]).is_file():
        raise ScheduleError("runtime_configuration_unavailable")
    return translate(cfg, cfg["repository_root"], ["--doctor"])


def run(cfg: dict, arguments: list[str]) -> int:
    publication = cfg["publication"]
    result = publisher(
        cfg,
        "worktree",
        "prepare",
        "--repo",
        cfg["repository_root"],
        "--worktree",
        cfg["worktree"],
        "--task-branch",
        cfg["task_branch"],
        "--remote",
        publication["remote"],
        "--branch",
        publication["branch"],
    )
    if result.returncode:
        raise ScheduleError("worktree_preparation_failed")
    commit_completed(cfg)
    publish_completed(cfg)
    result = publisher(
        cfg,
        "worktree",
        "reset",
        "--repo",
        cfg["worktree"],
        "--task-branch",
        cfg["task_branch"],
        "--remote",
        publication["remote"],
        "--branch",
        publication["branch"],
    )
    if result.returncode:
        raise ScheduleError("worktree_reset_failed")
    status = translate(cfg, cfg["worktree"], arguments)
    # Each completed file contains its own progress. A later LLM failure or a
    # timeout must not discard earlier paid results or hide the failing status.
    commit_completed(cfg)
    publish_completed(cfg)
    print(f"translation schedule complete: translator_status={status}", flush=True)
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=DEFAULT_CONFIG)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--doctor", action="store_true")
    modes.add_argument("--dry-run", action="store_true")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--date")
    selection.add_argument("--since-date")
    selection.add_argument("--days", type=int)
    parser.add_argument("--through-date")
    parser.add_argument("--limit-days", "--limit-files", dest="limit_days", type=int)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        cfg = load_schedule(Path(absolute(args.config)))
        arguments = translate_arguments(cfg, args)
        if args.doctor:
            return doctor(cfg)
        if args.dry_run:
            return translate(cfg, cfg["repository_root"], arguments + ["--dry-run"])
        os.umask(0o077)
        path = Path(cfg["lock"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("translation schedule skipped: another run is active", flush=True)
                return 0
            return run(cfg, arguments)
    except (ScheduleError, OSError, ValueError, KeyError, TypeError) as exc:
        code = str(exc) if isinstance(exc, ScheduleError) else "schedule_failed"
        print(f"FAIL: {code}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
