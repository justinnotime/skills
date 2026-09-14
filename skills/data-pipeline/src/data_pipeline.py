"""Repository-owned schedules and data ownership; configured commands own processing."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from string import Template
from zoneinfo import ZoneInfo


class Invalid(ValueError):
    pass


def expand(value, environment):
    if not isinstance(value, str):
        raise Invalid("expected a string")
    try:
        return Template(value).substitute(environment)
    except (KeyError, ValueError) as exc:
        raise Invalid(f"undefined or invalid variable in {value!r}") from exc


def read_json(path):
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def inside(path, root):
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise Invalid(f"configuration must be inside the repository: {path}")
    return resolved


def pointer(value, parts):
    if not parts:
        return value if isinstance(value, list) else [value]
    part, *rest = parts
    part = part.replace("~1", "/").replace("~0", "~")
    if part == "*":
        children = value.values() if isinstance(value, dict) else value
        if not isinstance(value, (dict, list)) or not value:
            raise Invalid("JSON pointer wildcard must select a nonempty collection")
        return [item for child in children for item in pointer(child, rest)]
    try:
        return pointer(
            value[int(part)] if isinstance(value, list) else value[part], rest
        )
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise Invalid(f"JSON pointer component not found: {part}") from exc


def reference(spec, root, environment):
    path = inside(root / expand(spec["file"], environment), root)
    key = spec["pointer"]
    if not key.startswith("/"):
        raise Invalid("a reference requires an absolute JSON pointer")
    values = pointer(read_json(path), key[1:].split("/"))
    if not values or any(not isinstance(x, str) or not x for x in values):
        raise Invalid(f"reference must select nonempty strings: {path}#{key}")
    return list(dict.fromkeys(expand(x, environment) for x in values)), str(path)


def cron_field(expression, low, high):
    values = set()
    for item in expression.split(","):
        base, slash, step_text = item.partition("/")
        step = int(step_text) if slash else 1
        if step < 1:
            raise Invalid("cron step must be positive")
        if base == "*":
            start, end = low, high
        elif "-" in base:
            start, end = map(int, base.split("-"))
        else:
            start = int(base)
            end = high if slash else start
        if not low <= start <= end <= high:
            raise Invalid(f"cron value outside {low}..{high}: {expression}")
        values.update(range(start, end + 1, step))
    return values


def schedule(expression):
    parts = expression.split()
    if len(parts) != 5:
        raise Invalid("schedule requires five numeric cron fields")
    fields = [
        cron_field(p, *limits)
        for p, limits in zip(parts, [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)])
    ]
    fields[4] = {x % 7 for x in fields[4]}
    return parts, fields


def due(expression, at):
    parts, fields = schedule(expression)
    minute, hour, day, month, weekday = fields
    day_matches = at.day in day
    week_matches = (at.weekday() + 1) % 7 in weekday
    # Vixie cron: restricted day-of-month and day-of-week are alternatives.
    calendar_match = (
        day_matches and week_matches
        if parts[2].startswith("*") or parts[4].startswith("*")
        else day_matches or week_matches
    )
    return (
        at.minute in minute and at.hour in hour and at.month in month and calendar_match
    )


def resource_paths(resource, root, environment):
    referenced = []
    if "paths_from" in resource:
        values, path = reference(resource["paths_from"], root, environment)
        referenced.append(path)
    else:
        values = [expand(p, environment) for p in resource.get("paths", [])]
    paths = [str((root / p).resolve()) for p in values]
    if resource["kind"] == "config":
        for p in paths:
            inside(Path(p), root)
            if not Path(p).is_file():
                raise Invalid(f"missing repository configuration: {p}")
    return paths, referenced


def load(selector_path, home=None):
    selector_path = selector_path.expanduser().resolve()
    selector = read_json(selector_path)
    if selector.get("schema") != "data-pipeline-node/v1":
        raise Invalid("unsupported node selector schema")
    catalog_path = (selector_path.parent / selector["catalog"]).resolve()
    catalog = read_json(catalog_path)
    if catalog.get("schema") != "data-pipeline/v1":
        raise Invalid("unsupported pipeline schema")
    root = (catalog_path.parent / catalog["repository"]).resolve()
    inside(selector_path, root)
    inside(catalog_path, root)
    nodes, resources = catalog["nodes"], catalog["resources"]
    selected = selector["node"]
    if selected not in nodes:
        raise Invalid(f"unknown node: {selected}")
    jobs, seen, claims = [], set(), []
    for item in catalog["jobs"]:
        identity, node = item["id"], item["node"]
        if (
            not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", identity)
            or identity in seen
        ):
            raise Invalid(f"invalid or duplicate job id: {identity}")
        seen.add(identity)
        if node not in nodes:
            raise Invalid(f"unknown job node: {node}")
        environment = {
            "HOME": str(home or Path.home()),
            "REPO": str(root),
            "NODE": node,
        }
        for settings in (
            catalog.get("environment", {}),
            nodes[node].get("environment", {}),
            item.get("environment", {}),
        ):
            for key, value in settings.items():
                if key in {"HOME", "REPO", "NODE"}:
                    raise Invalid(f"reserved environment variable: {key}")
                environment[key] = expand(value, environment)
        if not environment.get("PATH"):
            raise Invalid("configure the child PATH explicitly")
        zone = ZoneInfo(nodes[node]["timezone"])
        schedule(item["schedule"])
        if ("argv" in item) == ("commands" in item):
            raise Invalid("a job requires exactly one of argv or commands")
        configured = [item["argv"]] if "argv" in item else item["commands"]
        if not isinstance(configured, list) or not configured:
            raise Invalid("commands must be a nonempty list of argument lists")
        commands = []
        for arguments in configured:
            if not isinstance(arguments, list) or not arguments:
                raise Invalid("each command must be a nonempty argument list")
            command = [expand(a, environment) for a in arguments]
            if not Path(command[0]).is_absolute():
                raise Invalid("the job executable must be an absolute configured path")
            commands.append(command)
        closure, visiting = {}, set()

        def collect(name):
            if name in visiting:
                raise Invalid(f"resource dependency cycle: {name}")
            if name in closure:
                return
            if name not in resources:
                raise Invalid(f"unknown resource: {name}")
            visiting.add(name)
            resource = resources[name]
            if resource["kind"] not in {
                "config",
                "data",
                "state",
                "credential",
                "executable",
                "service",
            }:
                raise Invalid(f"invalid resource kind: {name}")
            paths, refs = resource_paths(resource, root, environment)
            for dependency in resource.get("requires", []):
                collect(dependency)
            closure[name] = {**resource, "paths": paths, "reference_files": refs}
            visiting.remove(name)

        for name in item["inputs"] + item["dependencies"]:
            collect(name)
        required_resources = list(closure)
        outputs = []
        for output in item["outputs"]:
            name = output["resource"]
            collect(name)
            partitions = None
            if "partitions_from" in output:
                partitions, ref = reference(
                    output["partitions_from"], root, environment
                )
                closure[name]["reference_files"].append(ref)
            elif "partitions" in output:
                partitions = output["partitions"]
                if not partitions or any(
                    not isinstance(p, str) or not p for p in partitions
                ):
                    raise Invalid("output partitions must be nonempty strings")
            resource = closure[name]
            if resource["kind"] not in {"data", "state"} or not resource["paths"]:
                raise Invalid(f"output must name data or state paths: {name}")
            if partitions is not None and not resource.get("partition_contract"):
                raise Invalid(
                    f"partitioned output requires its writer contract: {name}"
                )
            outputs.append(
                {"resource": name, "paths": resource["paths"], "partitions": partitions}
            )
            if resource.get("scope") == "repository":
                for path in resource["paths"]:
                    inside(Path(path), root)
                    claims.append(
                        (identity, path, partitions, resource.get("partition_contract"))
                    )
        timeout = item["timeout_seconds"]
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or timeout <= 0
        ):
            raise Invalid("timeout_seconds must be positive")
        for runtime_path in (
            expand(item["log"], environment),
            expand(catalog["state_directory"], environment),
        ):
            location = Path(runtime_path)
            if not location.is_absolute() or location.resolve().is_relative_to(root):
                raise Invalid(
                    "log and state paths must be absolute and outside the repository"
                )
        jobs.append(
            {
                **item,
                **({"argv": commands[0]} if "argv" in item else {}),
                "commands": commands,
                "environment": environment,
                "timezone": str(zone),
                "resources": closure,
                "outputs": outputs,
                "required_resources": required_resources,
                "cwd": str(root),
                "log": expand(item["log"], environment),
                "state_directory": expand(catalog["state_directory"], environment),
            }
        )
    if not jobs:
        raise Invalid("catalog must contain jobs")
    for i, (job, path, parts, contract) in enumerate(claims):
        for other, target, other_parts, other_contract in claims[i + 1 :]:
            if job == other:
                continue
            overlap = Path(path).is_relative_to(target) or Path(target).is_relative_to(
                path
            )
            if overlap and not (
                path == target
                and parts is not None
                and other_parts is not None
                and contract == other_contract
                and set(parts).isdisjoint(other_parts)
            ):
                raise Invalid(
                    f"overlapping output ownership: {job} and {other}: {path}"
                )
    return {
        "node": selected,
        "repository": str(root),
        "catalog": str(catalog_path),
        "jobs": jobs,
    }


def doctor(job):
    failures = []
    for command in job["commands"]:
        if not os.access(command[0], os.X_OK):
            failures.append(f"job executable unavailable: {command[0]}")
    for name, resource in job["resources"].items():
        if (
            name not in job["required_resources"]
            or resource.get("optional", False)
            or resource["kind"] == "state"
        ):
            continue
        for path in resource["paths"]:
            if not Path(path).exists():
                failures.append(f"{name}: missing {path}")
            elif resource["kind"] == "executable" and not os.access(path, os.X_OK):
                failures.append(f"{name}: not executable {path}")
    return failures


@contextmanager
def locked(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield None
            return
        try:
            yield stream
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def execute(job, at, force=False):
    state = Path(job["state_directory"])
    slot = at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    identity = job["id"]
    with locked(state / (identity + ".lock")) as lock:
        if lock is None:
            return {"job": identity, "status": "busy"}
        record_path = state / (identity + ".json")
        previous = read_json(record_path) if record_path.exists() else {}
        if not force and previous.get("slot", "") >= slot:
            return {"job": identity, "status": "already-attempted", "slot": slot}
        result = {
            "job": identity,
            "node": job["node"],
            "slot": slot,
            "status": "running",
            "started": datetime.now(timezone.utc).isoformat(),
            "log": job["log"],
        }
        atomic_json(record_path, result)
        log = Path(job["log"])
        log.parent.mkdir(parents=True, exist_ok=True)
        failures = doctor(job)
        with log.open("a") as output:
            output.write(json.dumps(result) + "\n")
            output.flush()
            if failures:
                result.update(status="failed", errors=failures)
            else:
                process = None
                try:
                    deadline = time.monotonic() + job["timeout_seconds"]
                    result["completed_commands"] = 0
                    for index, command in enumerate(job["commands"], start=1):
                        result["command_index"] = index
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            result.pop("returncode", None)
                            result.update(status="failed", error="timeout")
                            break
                        process = subprocess.Popen(
                            command,
                            cwd=job["cwd"],
                            env=job["environment"],
                            stdin=subprocess.DEVNULL,
                            stdout=output,
                            stderr=output,
                            start_new_session=True,
                            pass_fds=(lock.fileno(),),
                        )
                        code = process.wait(timeout=remaining)
                        result.update(
                            status="ok" if code == 0 else "failed", returncode=code
                        )
                        if code != 0:
                            break
                        result["completed_commands"] = index
                except subprocess.TimeoutExpired:
                    result.pop("returncode", None)
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                    # A wrapper can exit while its descendants ignore SIGTERM.
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                    result.update(status="failed", error="timeout")
                except OSError as exc:
                    result.pop("returncode", None)
                    result.update(status="failed", error=str(exc))
            result["finished"] = datetime.now(timezone.utc).isoformat()
            output.write(json.dumps(result) + "\n")
        atomic_json(record_path, result)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    config_directory = (
        Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        / "data-pipeline"
    )
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--config", type=Path, help="explicit repository node selector path"
    )
    selection.add_argument(
        "--profile", metavar="NAME", help="select data-pipeline/profiles/NAME.json"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--plan",
        action="store_true",
        help="show all nodes and resolved dependencies; no writes",
    )
    mode.add_argument(
        "--doctor",
        action="store_true",
        help="check this node locally; no commands or writes",
    )
    mode.add_argument(
        "--run", metavar="JOB", help="explicitly run one configured job on this node"
    )
    args = parser.parse_args(argv)
    try:
        selector = args.config or config_directory / "config.json"
        if args.profile is not None:
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", args.profile):
                raise Invalid(
                    "profile must start with a letter or digit and contain only "
                    "letters, digits, underscores, dots or hyphens"
                )
            selector = config_directory / "profiles" / (args.profile + ".json")
        config = load(selector)
        local = [job for job in config["jobs"] if job["node"] == config["node"]]
        if args.plan:
            print(json.dumps(config, indent=2))
            return 0
        if args.doctor:
            results = [{"job": job["id"], "errors": doctor(job)} for job in local]
            print(json.dumps(results, indent=2))
            return int(any(r["errors"] for r in results))
        at = datetime.now(timezone.utc)
        if args.run:
            chosen = [job for job in local if job["id"] == args.run]
            if not chosen:
                raise Invalid("--run must name a job assigned to the selected node")
        else:
            chosen = [
                job
                for job in local
                if due(job["schedule"], at.astimezone(ZoneInfo(job["timezone"])))
            ]
        os.umask(0o077)
        with ThreadPoolExecutor(max_workers=max(1, len(chosen))) as pool:
            results = list(
                pool.map(lambda job: execute(job, at, bool(args.run)), chosen)
            )
        for result in results:
            if args.run or result["status"] == "failed":
                print(json.dumps(result, sort_keys=True))
        return int(any(r["status"] == "failed" for r in results))
    except (Invalid, OSError, KeyError, ValueError, TypeError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
