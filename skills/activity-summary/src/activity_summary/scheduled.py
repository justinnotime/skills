"""Generate and publish summaries while retaining complete paid output on failure.

LLM calls > 0 during normal generation. Doctor, dry run and publication validation
perform no model or account-status calls.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import facts, issue_section, validation, weekly_validation
from .config import DEFAULT_CONFIG, ConfigurationError, activate, home, load, rooted


class ScheduleError(RuntimeError):
    """A diagnostic identifier without private response contents."""


def expand(argv, cfg, kind, scope="worktree"):
    values = {
        "worktree": cfg[kind]["schedule"]["worktree"],
        "repository": cfg["repository_root"],
        "scope": scope,
        "kind": kind,
    }
    result = []
    for item in argv:
        for key, value in values.items():
            item = item.replace("{" + key + "}", value)
        result.append(item)
    return result


def process_environment(cfg):
    env = dict(os.environ)
    env.update(cfg["environment"])
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def call(argv, cfg, kind, *, capture=False):
    return subprocess.run(
        argv,
        cwd=cfg["repository_root"],
        env=process_environment(cfg),
        capture_output=capture,
        check=False,
    )


def publisher(cfg, kind, *args, capture=False):
    return call(cfg["publisher_command"] + list(args), cfg, kind, capture=capture)


def inspect(cfg, kind, action):
    schedule = cfg[kind]["schedule"]
    publication = schedule["publication"]
    argv = [
        "worktree",
        action,
        "--repo",
        schedule["worktree"],
        "--remote",
        publication["remote"],
        "--branch",
        publication["branch"],
    ]
    if action in {"changed", "committed"}:
        argv.append("--null")
    result = publisher(cfg, kind, *argv, capture=True)
    if result.returncode:
        raise ScheduleError("worktree_inspection_failed")
    return result.stdout


def ahead(cfg, kind):
    value = inspect(cfg, kind, "ahead").strip()
    if not re.fullmatch(rb"[0-9]+", value):
        raise ScheduleError("invalid_ahead_count")
    return int(value)


def policy(cfg, kind, mode, scope="worktree"):
    argv = cfg[kind]["schedule"]["policy"][mode + "_command"]
    return call(expand(argv, cfg, kind, scope), cfg, kind).returncode


def weekly_inputs(cfg, root, end):
    finish = date.fromisoformat(end)
    present, missing, chunks = [], [], []
    for offset in range(6, -1, -1):
        day = str(finish - timedelta(days=offset))
        relative = f"{cfg['daily']['output_directory']}/{day}.md"
        path = rooted(root, relative)
        if not path.is_file():
            missing.append(day)
            continue
        present.append(day)
        chunks.extend([f"\n===== DAILY {day} ({relative}) =====\n\n".encode(), path.read_bytes()])
    return b"".join(chunks), present, missing


def source_input(cfg, kind, root, target):
    if kind == "daily":
        data = facts.extract(target, str(root), cfg["facts"]["gap_minutes"])
        blob = facts.serialize(data)
        return blob, data, []
    blob, present, missing = weekly_inputs(cfg, root, target)
    if not present:
        raise ScheduleError("no_daily_inputs")
    return blob, None, missing


def candidate_errors(cfg, kind, path, target, blob, data, missing):
    digest = hashlib.sha256(blob).hexdigest()
    if kind == "daily":
        return validation.validate(path, target, digest, data)
    return weekly_validation.validate(
        path, target, digest, blob.decode("utf-8"), missing, cfg[kind].get("validation", {})
    )


def content_valid(cfg, kind, scope):
    """Validate current-source provenance before both initial and rebased publication."""
    schedule = cfg[kind]["schedule"]
    root = schedule["worktree"]
    paths = inspect(cfg, kind, "committed" if scope == "committed" else "changed")
    prefix = cfg[kind]["output_directory"] + "/"
    for encoded in paths.split(b"\0"):
        if not encoded:
            continue
        relative = encoded.decode("utf-8")
        if not relative.startswith(prefix) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}\.md", relative[len(prefix) :]
        ):
            raise ScheduleError("unowned_output_preserved")
        path = rooted(root, relative)
        if not path.is_file():
            raise ScheduleError("deleted_output_preserved")
        target = path.stem
        blob, data, missing = source_input(cfg, kind, root, target)
        if candidate_errors(cfg, kind, path, target, blob, data, missing):
            return False
    return True


def commit_completed(cfg, kind):
    if not inspect(cfg, kind, "changed"):
        return
    if not content_valid(cfg, kind, "worktree"):
        raise ScheduleError("candidate_changed_output_preserved")
    if policy(cfg, kind, "validate"):
        raise ScheduleError("private_validation_failed_output_preserved")
    if policy(cfg, kind, "commit") not in {0, 2} or inspect(cfg, kind, "changed"):
        raise ScheduleError("commit_failed_output_preserved")


def publish_completed(cfg, kind):
    if not ahead(cfg, kind):
        return
    schedule = cfg[kind]["schedule"]
    publication = schedule["publication"]
    validator = [
        sys.executable,
        "-B",
        "-m",
        "activity_summary.scheduled",
        kind,
        "--config",
        cfg["config_path"],
        "--validate-worktree",
        schedule["worktree"],
        "--scope",
        "committed",
    ]
    argv = [
        "--repo",
        schedule["worktree"],
        "--existing-worktree",
        "--expected-branch",
        schedule["task_branch"],
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
        json.dumps(validator),
        "--message-command",
        json.dumps(expand(schedule["policy"]["message_command"], cfg, kind, "committed")),
    ]
    if "publish_lock" in publication:
        argv += ["--publish-lock", publication["publish_lock"]]
    result = publisher(cfg, kind, *argv)
    if result.returncode:
        raise ScheduleError("publication_failed_output_preserved")
    if inspect(cfg, kind, "changed") or ahead(cfg, kind):
        raise ScheduleError("publication_incomplete_output_preserved")


def reset(cfg, kind):
    schedule = cfg[kind]["schedule"]
    pub = schedule["publication"]
    if publisher(
        cfg,
        kind,
        "worktree",
        "reset",
        "--repo",
        schedule["worktree"],
        "--task-branch",
        schedule["task_branch"],
        "--remote",
        pub["remote"],
        "--branch",
        pub["branch"],
    ).returncode:
        raise ScheduleError("worktree_reset_failed")


def completed_date(value, today):
    result = date.fromisoformat(value)
    if str(result) != value or result >= today:
        raise ScheduleError("target_must_be_completed_utc_date")
    return result


def daily_targets(cfg, root, args, today):
    selection = cfg["daily"].get("selection", {})
    yesterday = today - timedelta(days=1)
    if args.force and not args.target:
        raise ScheduleError("force_requires_target")
    if args.target:
        values = [yesterday if args.target == "yesterday" else completed_date(args.target, today)]
    else:
        values = [
            yesterday - timedelta(days=offset)
            for offset in range(selection.get("lookback_days", 14) - 1, -1, -1)
        ]
    repair_start = yesterday - timedelta(days=max(0, selection.get("repair_days", 3) - 1))
    candidates = []
    for value in values:
        path = rooted(root, f"{cfg['daily']['output_directory']}/{value}.md")
        hashed = path.is_file() and re.search(
            r"^facts_sha256: [0-9a-f]{64}$", path.read_text(encoding="utf-8"), re.MULTILINE
        )
        if args.force or not path.exists() or (hashed and (args.target or value >= repair_start)):
            candidates.append(str(value))
    maximum = args.max_dates if args.max_dates is not None else selection.get("max_dates", 3)
    if maximum < 1:
        raise ScheduleError("max_dates_must_be_positive")
    return candidates[:maximum]


def template_text(cfg, kind, root):
    value = cfg[kind]["prompt_template"]
    path = Path(value) if Path(value).is_absolute() else rooted(root, value)
    return path.read_text(encoding="utf-8")


def request_text(cfg, kind, root, target, blob, missing):
    start = str(date.fromisoformat(target) - timedelta(days=2 if kind == "daily" else 6))
    values = {
        "root": str(root),
        "target": target,
        "start": start,
        "end": target,
        "generation_date": str(datetime.now(timezone.utc).date()),
        "relative": f"{cfg[kind]['output_directory']}/{target}.md",
        "input_hash": hashlib.sha256(blob).hexdigest(),
        "missing_csv": ",".join(missing),
    }
    template = template_text(cfg, kind, root)
    # A single substitution pass prevents source text from becoming template syntax.
    values["inputs"] = blob.decode("utf-8")
    return re.sub(r"\{\{([a-z_]+)\}\}", lambda match: values.get(match[1], match[0]), template)


def authenticate(cfg, kind):
    schedule = cfg[kind]["schedule"]
    for attempt in range(schedule["auth_attempts"]):
        try:
            result = subprocess.run(
                schedule["auth_command"],
                env=schedule["environment"],
                cwd=schedule["worktree"],
                capture_output=True,
                check=False,
                timeout=schedule["auth_timeout_seconds"],
            )
        except subprocess.TimeoutExpired:
            result = None
        if result is not None and result.returncode == 0:
            try:
                if json.loads(result.stdout).get("loggedIn") is True:
                    return
            except (ValueError, AttributeError):
                pass
            raise ScheduleError("account_not_authenticated")
        if attempt + 1 < schedule["auth_attempts"]:
            time.sleep(schedule["auth_retry_seconds"])
    raise ScheduleError("account_status_unavailable")


def preserve_failure(cfg, kind, label, content):
    directory = cfg[kind]["schedule"].get("failure_directory")
    if directory is None:
        return
    # Response files can contain source material. They only go to the explicitly
    # configured private directory, with owner-only permissions.
    try:
        path = Path(directory)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, _name = tempfile.mkstemp(prefix=f"{kind}-{label}-", dir=path)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
    except OSError:
        print("WARN private failure artifact could not be saved", file=sys.stderr)


def model_response(cfg, kind, request):
    schedule = cfg[kind]["schedule"]
    with subprocess.Popen(
        schedule["model_command"],
        cwd=schedule["worktree"],
        env=schedule["environment"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    ) as process:
        try:
            output, _stderr = process.communicate(
                request.encode("utf-8"), timeout=schedule["timeout_seconds"]
            )
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                output, _stderr = process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                output, _stderr = process.communicate()
            preserve_failure(cfg, kind, "timeout-response", output)
            raise ScheduleError("model_timeout_no_candidate_installed") from None
    if process.returncode:
        preserve_failure(cfg, kind, "failed-response", output)
        raise ScheduleError("model_failed_no_candidate_installed")
    try:
        payload = json.loads(output)
        structured = payload.get("structured_output")
        markdown = structured.get("markdown") if isinstance(structured, dict) else None
        if not isinstance(markdown, str):
            markdown = payload.get("result")
            if isinstance(markdown, str):
                with contextlib.suppress(json.JSONDecodeError):
                    nested = json.loads(markdown)
                    if isinstance(nested, dict):
                        markdown = nested.get("markdown")
        if payload.get("is_error") or not isinstance(markdown, str) or not markdown.strip():
            raise ValueError
    except (ValueError, AttributeError):
        preserve_failure(cfg, kind, "invalid-response", output)
        raise ScheduleError("invalid_model_response") from None
    markdown = re.sub(
        r"^\s*<markdown>\s*|\s*</markdown>\s*$", "", markdown.strip(), flags=re.IGNORECASE
    )
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        re.sub(r"^updated:.*$", f"updated: {stamp}", markdown, count=1, flags=re.MULTILINE).rstrip()
        + "\n"
    )


def generate(cfg, kind, root, target, blob, data, missing):
    markdown = model_response(cfg, kind, request_text(cfg, kind, root, target, blob, missing))
    if kind == "daily":
        markdown = issue_section.install_issue_section(
            markdown, issue_section.render_issue_section(data, Path(root))
        )
        markdown = issue_section.install_agent_work_section(markdown, data)
        markdown = issue_section.sanitize_external_github_references(markdown, data)
    else:
        markdown = weekly_validation.sanitize(markdown, cfg[kind].get("validation", {}))
    output = rooted(root, f"{cfg[kind]['output_directory']}/{target}.md")
    # Validation occurs before any owned file is changed. The temporary file is
    # outside the repository, so process interruption cannot create a dirty input.
    with tempfile.TemporaryDirectory(prefix="activity-summary-") as directory:
        candidate = Path(directory) / output.name
        candidate.write_text(markdown, encoding="utf-8")
        if candidate_errors(cfg, kind, candidate, target, blob, data, missing):
            preserve_failure(cfg, kind, "invalid-candidate", markdown.encode("utf-8"))
            raise ScheduleError("candidate_validation_failed_no_output_installed")
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix=".activity-summary-", dir=output.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(markdown)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, output)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(name)


def doctor(cfg, kind):
    schedule = cfg[kind]["schedule"]
    if (
        not Path(cfg["repository_root"]).is_dir()
        or not (Path(cfg["repository_root"]) / ".git").exists()
    ):
        raise ScheduleError("repository_unavailable")
    template = template_text(cfg, kind, cfg["repository_root"])
    if "{{inputs}}" not in template or "{{input_hash}}" not in template:
        raise ScheduleError("prompt_template_requires_inputs_and_hash")
    commands = [cfg["publisher_command"], *schedule["policy"].values()]
    for argv in commands:
        if (
            shutil.which(expand(argv, cfg, kind)[0], path=process_environment(cfg).get("PATH"))
            is None
        ):
            raise ScheduleError("configured_command_unavailable")
    for key in ("model_command", "auth_command"):
        if shutil.which(schedule[key][0], path=schedule["environment"].get("PATH", "")) is None:
            raise ScheduleError("configured_model_command_unavailable")
    print("OK local configuration and executables; account authentication was not queried")
    return 0


def run(cfg, kind, args):
    schedule = cfg[kind]["schedule"]
    pub = schedule["publication"]
    root = cfg["repository_root"] if args.dry_run else schedule["worktree"]
    today = datetime.now(timezone.utc).date()
    if not args.dry_run:
        if publisher(
            cfg,
            kind,
            "worktree",
            "prepare",
            "--repo",
            cfg["repository_root"],
            "--worktree",
            root,
            "--task-branch",
            schedule["task_branch"],
            "--remote",
            pub["remote"],
            "--branch",
            pub["branch"],
        ).returncode:
            raise ScheduleError("worktree_preparation_failed")
        commit_completed(cfg, kind)
        publish_completed(cfg, kind)
        reset(cfg, kind)
    if kind == "daily":
        targets = daily_targets(cfg, root, args, today)
    else:
        target = args.end or str(today - timedelta(days=1))
        completed_date(target, today)
        targets = [target]
        deadline = time.monotonic() + cfg[kind]["wait_inputs_seconds"]
        while (
            not args.dry_run and weekly_inputs(cfg, root, target)[2] and time.monotonic() < deadline
        ):
            time.sleep(min(60, max(0, deadline - time.monotonic())))
            if publisher(
                cfg,
                kind,
                "worktree",
                "fetch",
                "--repo",
                root,
                "--remote",
                pub["remote"],
                "--branch",
                pub["branch"],
            ).returncode:
                raise ScheduleError("input_refresh_failed")
            reset(cfg, kind)
    authenticated = False
    for target in targets:
        blob, data, missing = source_input(cfg, kind, root, target)
        digest = hashlib.sha256(blob).hexdigest()
        output = rooted(root, f"{cfg[kind]['output_directory']}/{target}.md")
        hash_field = "facts_sha256" if kind == "daily" else "inputs_sha256"
        if (
            not args.force
            and output.is_file()
            and re.search(
                r"^" + hash_field + ": " + digest + "$",
                output.read_text(encoding="utf-8"),
                re.MULTILINE,
            )
        ):
            print(f"OK {kind} {target}: existing input hash matches")
            continue
        if args.dry_run:
            request = request_text(cfg, kind, root, target, blob, missing)
            print(
                json.dumps(
                    {
                        "kind": kind,
                        "target": target,
                        "input_bytes": len(blob),
                        "input_sha256": digest,
                        "estimated_input_tokens": (len(request) + 2) // 3,
                        "estimate_method": "characters/3; excludes tool reads and output",
                        "missing_inputs": missing,
                        "model_calls": 0,
                    }
                )
            )
            continue
        if not authenticated:
            authenticate(cfg, kind)
            authenticated = True
        generate(cfg, kind, root, target, blob, data, missing)
        commit_completed(cfg, kind)
        publish_completed(cfg, kind)
    print(f"OK {kind}: selected={len(targets)}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("daily", "weekly"))
    parser.add_argument(
        "--config", default=os.environ.get("ACTIVITY_SUMMARY_CONFIG", DEFAULT_CONFIG)
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--doctor", action="store_true")
    modes.add_argument("--dry-run", action="store_true")
    modes.add_argument("--validate-worktree")
    parser.add_argument("--scope", choices=("worktree", "committed"), default="committed")
    parser.add_argument("--target")
    parser.add_argument("--end")
    parser.add_argument("--max-dates", type=int)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        cfg = load(home(args.config))
        activate(cfg)
        if args.validate_worktree:
            if Path(args.validate_worktree).resolve() != Path(
                cfg[args.kind]["schedule"]["worktree"]
            ):
                raise ScheduleError("unexpected_validation_worktree")
            if not content_valid(cfg, args.kind, args.scope):
                return 3
            return policy(cfg, args.kind, "validate", args.scope)
        if args.doctor:
            return doctor(cfg, args.kind)
        today = datetime.now(timezone.utc).date()
        if args.kind == "daily":
            if args.end is not None or (args.force and not args.target):
                raise ScheduleError("daily_selection_requires_target_not_end")
            if args.target and args.target != "yesterday":
                completed_date(args.target, today)
            if args.max_dates is not None and args.max_dates < 1:
                raise ScheduleError("max_dates_must_be_positive")
        else:
            if args.target is not None or args.max_dates is not None:
                raise ScheduleError("weekly_selection_requires_end")
            if args.end:
                completed_date(args.end, today)
        if args.dry_run:
            return run(cfg, args.kind, args)
        os.umask(0o077)
        lock = Path(cfg[args.kind]["schedule"]["lock"])
        lock.parent.mkdir(parents=True, exist_ok=True)
        with lock.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("SKIP another summary run is active")
                return 0
            return run(cfg, args.kind, args)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        subprocess.SubprocessError,
        ScheduleError,
    ) as exc:
        identifier = (
            str(exc) if isinstance(exc, (ScheduleError, ConfigurationError)) else type(exc).__name__
        )
        print(f"ERROR activity-summary: {identifier}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
