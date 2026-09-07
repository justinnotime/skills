"""Explicit source selection, formatting policy and external command configuration."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

SCHEMA = "activity-summary/v1"
DEFAULT_CONFIG = "~/.config/activity-summary/config.json"


class ConfigurationError(ValueError):
    """A configuration diagnostic without configuration values."""


def home(value: str) -> str:
    return os.path.expanduser(
        re.sub(r"\$\{HOME\}|\$HOME(?![A-Za-z0-9_])", lambda _: str(Path.home()), value)
    )


def absolute(value: object) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ConfigurationError("invalid_path")
    path = Path(home(value))
    if not path.is_absolute():
        raise ConfigurationError("absolute_path_required")
    return str(path.resolve())


def relative(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or "\0" in value
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ConfigurationError("relative_path_required")
    return value


def rooted(root: str | Path, value: str) -> Path:
    """Reject symlinks and path traversal at every existing path component."""
    base = Path(root).resolve()
    result = base
    for part in Path(relative(value)).parts:
        result = result / part
        if result.is_symlink():
            raise ConfigurationError("symlink_path_rejected")
    if not result.resolve().is_relative_to(base):
        raise ConfigurationError("path_outside_repository")
    return result


def command(value: object) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item or "\0" in item for item in value)
    ):
        raise ConfigurationError("invalid_command")
    return [home(item) for item in value]


def environment(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or any(
        not isinstance(key, str)
        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
        or not isinstance(item, str)
        or "\0" in item
        for key, item in value.items()
    ):
        raise ConfigurationError("invalid_environment")
    return {key: home(item) for key, item in value.items()}


def integer(value: object, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigurationError("invalid_integer")
    return value


def load(path: str | Path, root: str | Path | None = None) -> dict:
    config_path = Path(absolute(str(path)))
    cfg = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(cfg, dict) or cfg.get("schema") != SCHEMA:
        raise ConfigurationError("invalid_schema")
    cfg["repository_root"] = absolute(str(root) if root is not None else cfg["repository_root"])
    cfg["config_path"] = str(config_path)
    cfg["environment"] = environment(cfg.get("environment", {}))
    facts = cfg.setdefault("facts", {})
    if not isinstance(facts, dict):
        raise ConfigurationError("invalid_facts_configuration")
    for key in (
        "issue_directory",
        "document_directory",
        "wiki_project_directory",
        "summary_directory",
    ):
        facts[key] = relative(facts[key])
        rooted(cfg["repository_root"], facts[key])
    facts["commit_directories"] = [
        relative(item.rstrip("/")) for item in facts["commit_directories"]
    ]
    if not facts["commit_directories"]:
        raise ConfigurationError("empty_commit_selection")
    from .issue_refs import canonical

    canonical(facts["default_issue_repository"], 1)
    for pattern in facts.get("project_patterns", []):
        if re.compile(pattern).groups < 1:
            raise ConfigurationError("project_pattern_requires_capture")
    for prefix, label in facts.get("source_project_labels", []):
        relative(prefix)
        if not isinstance(label, str) or not label:
            raise ConfigurationError("invalid_project_label")
    labels = set()
    for source in facts["session_sources"]:
        rooted(cfg["repository_root"], relative(source["directory"]))
        if (
            source["format"] not in {"history", "claw"}
            or not source["label"]
            or source["label"] in labels
        ):
            raise ConfigurationError("invalid_session_source")
        labels.add(source["label"])
    facts["gap_minutes"] = integer(facts.get("gap_minutes", 45), 1)
    for kind in ("daily", "weekly"):
        if kind not in cfg:
            continue
        section = cfg[kind]
        section["output_directory"] = relative(section["output_directory"])
        rooted(cfg["repository_root"], section["output_directory"])
        if "prompt_template" in section:
            value = home(section["prompt_template"])
            section["prompt_template"] = value if Path(value).is_absolute() else relative(value)
        for pattern in section.get("validation", {}).values():
            if isinstance(pattern, dict):
                if any(not isinstance(item, str) for item in pattern.values()):
                    raise ConfigurationError("invalid_validation_template")
        schedule = section.get("schedule")
        if schedule is None:
            continue
        for key in ("worktree", "lock"):
            schedule[key] = absolute(schedule[key])
        wt = Path(schedule["worktree"])
        repository = Path(cfg["repository_root"])
        if root is None and (wt.is_relative_to(repository) or repository.is_relative_to(wt)):
            raise ConfigurationError("separate_worktree_required")
        if config_path.is_relative_to(wt):
            raise ConfigurationError("configuration_inside_worktree")
        lock = Path(schedule["lock"])
        if lock == config_path or lock.is_relative_to(repository) or lock.is_relative_to(wt):
            raise ConfigurationError("separate_lock_required")
        for key in ("model_command", "auth_command"):
            schedule[key] = command(schedule[key])
        schedule["environment"] = environment(schedule.get("environment", {}))
        schedule["timeout_seconds"] = integer(schedule.get("timeout_seconds", 2700), 1)
        schedule["auth_attempts"] = integer(schedule.get("auth_attempts", 3), 1)
        schedule["auth_retry_seconds"] = integer(schedule.get("auth_retry_seconds", 20))
        schedule["auth_timeout_seconds"] = integer(schedule.get("auth_timeout_seconds", 60), 1)
        if "failure_directory" in schedule:
            schedule["failure_directory"] = absolute(schedule["failure_directory"])
            failure_directory = Path(schedule["failure_directory"])
            if failure_directory.is_relative_to(repository) or failure_directory.is_relative_to(wt):
                raise ConfigurationError("failure_directory_must_be_external")
        publication = schedule["publication"]
        for key, default in (("remote", "origin"), ("branch", "main"), ("agent", "")):
            publication.setdefault(key, default)
        for key in ("remote", "branch"):
            if not isinstance(publication[key], str) or publication[key].startswith("-"):
                raise ConfigurationError("invalid_git_reference")
        if (
            not isinstance(schedule["task_branch"], str)
            or schedule["task_branch"].startswith("-")
            or schedule["task_branch"] == publication["branch"]
        ):
            raise ConfigurationError("separate_task_branch_required")
        publication["owned_paths"] = [relative(item) for item in publication["owned_paths"]]
        if not publication["owned_paths"] or not any(
            section["output_directory"] == item
            or section["output_directory"].startswith(item + "/")
            for item in publication["owned_paths"]
        ):
            raise ConfigurationError("output_not_owned")
        if "publish_lock" in publication:
            publication["publish_lock"] = absolute(publication["publish_lock"])
            plock = Path(publication["publish_lock"])
            if (
                plock in {lock, config_path}
                or plock.is_relative_to(repository)
                or plock.is_relative_to(wt)
            ):
                raise ConfigurationError("separate_publish_lock_required")
        for key in ("validate_command", "commit_command", "message_command"):
            schedule["policy"][key] = command(schedule["policy"][key])
        # Older profiles may contain recover_command. It is intentionally ignored:
        # validation failure never authorizes discarding completed model output.
        schedule["policy"].pop("recover_command", None)
        selection = section.setdefault("selection", {})
        for key, default in (("lookback_days", 14), ("repair_days", 3), ("max_dates", 3)):
            selection[key] = integer(selection.get(key, default), 0 if key == "repair_days" else 1)
        section["wait_inputs_seconds"] = integer(section.get("wait_inputs_seconds", 1500))
    if "publisher_command" in cfg:
        cfg["publisher_command"] = command(cfg["publisher_command"])
    return cfg


def activate(cfg: dict) -> None:
    """Configure local pure-function modules; no I/O or model initialization."""
    from . import evaluation, facts, issue_refs, issue_section, validation

    options = cfg["facts"]
    issue_refs.DEFAULT_REPO = options["default_issue_repository"]
    facts.DEFAULT_REPO = issue_refs.DEFAULT_REPO
    evaluation.DEFAULT_REPO = issue_refs.DEFAULT_REPO
    facts.configure(options)
    issue_section.configure(
        cfg.get("daily", {}).get("issue_section", {}), options["issue_directory"]
    )
    validation.configure(cfg.get("daily", {}).get("validation", {}), issue_section.OPTIONS)
    evaluation.AGENT_WORK_PATTERN = validation.AGENT_WORK_PATTERN
