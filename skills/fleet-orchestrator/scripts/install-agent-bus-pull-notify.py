#!/usr/bin/env python3
"""Install a user dispatcher and the machine-local Codex Stop hook."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts" / "lib"))
import runtime_config as cfg  # noqa: E402
# Codex honours CODEX_HOME; so must the installer, or agent-boot's wake-channel
# check (which reads the same path) would refuse forever on such a host.
CODEX = Path(os.environ.get("TURN_HOOKS_CODEX_CONFIG") or
             (Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"))
UNIT_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"
UNIT = UNIT_DIR / "agent-bus-dispatcher.service"
FLEET_UNIT = UNIT_DIR / "agent-bus-dispatcher@.service"
FLEET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
HOOK_MARKER = "# agent-bus-stop-hook"
def hook_lines() -> list[str]:
    command = [sys.executable, str(ROOT / "scripts/agent-bus-codex-stop-hook.py")]
    configured = os.environ.get("FLEET_ORCHESTRATOR_CONFIG")
    if configured:
        command = ["env", f"FLEET_ORCHESTRATOR_CONFIG={configured}", *command]
    return [
        "[[hooks.Stop]]", "", "[[hooks.Stop.hooks]]", 'type = "command"',
        'command = ' + json.dumps(shlex.join(command)), "timeout = 15", HOOK_MARKER,
    ]


def replace_managed_hook(text: str) -> str:
    """Replace the marked hook only; keep unrelated notifications and hooks."""
    desired = tomllib.loads("\n".join(hook_lines()))["hooks"]["Stop"][0]["hooks"][0]
    existing = tomllib.loads(text).get("hooks", {}).get("Stop", [])
    for group in existing:
        for hook in group.get("hooks", []):
            if (hook.get("type") == "command"
                    and shlex.split(hook.get("command", "")) == shlex.split(desired["command"])):
                # Codex can move comments when persisting trust. The parsed
                # command still identifies an installed hook without its marker.
                return text
    lines = text.splitlines()
    output: list[str] = []
    i = 0
    while i < len(lines):
        if lines[i].strip() == "[[hooks.Stop]]":
            end = i + 1
            while end < len(lines) and (not lines[end].lstrip().startswith("[")
                                        or lines[end].strip() == "[[hooks.Stop.hooks]]"):
                end += 1
            block = lines[i:end]
            marker = next((n for n, line in enumerate(block) if line.strip() == HOOK_MARKER), None)
            if marker is not None and sum(line.strip() == "[[hooks.Stop.hooks]]" for line in block) == 1:
                # Old installs prepend this block; content following its marker
                # may contain unrelated settings and must survive the upgrade.
                output.extend(block[marker + 1:])
                i = end
                continue
        output.append(lines[i])
        i += 1
    updated = "\n".join(output).rstrip() + "\n\n" + "\n".join(hook_lines()) + "\n"
    tomllib.loads(updated)
    return updated


def render_unit(template: Path) -> str:
    """Render package paths into an otherwise machine-neutral user unit."""
    def unit_arg(value: str) -> str:
        return json.dumps(value.replace("%", "%%"))
    configured = os.environ.get("FLEET_ORCHESTRATOR_CONFIG")
    config_env = ('Environment=' + unit_arg('FLEET_ORCHESTRATOR_CONFIG=' + configured)
                  if configured else '')
    return (template.read_text()
            .replace("@PYTHON@", unit_arg(sys.executable))
            .replace("@BUS@", unit_arg(str(ROOT / "scripts/agent-bus-v3.py")))
            .replace("@BUS_CLI@", unit_arg(str(ROOT / "scripts/matrix-bus.sh")))
            .replace("@CONFIG_ENV@", config_env))


def atomic_write(path: Path, text: str) -> None:
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(text)
    os.replace(temp, path)


def fleet_name(value: str) -> str:
    if value == "default" or not FLEET_NAME_RE.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "fleet name must match [a-z0-9][a-z0-9-]{0,31} and must not be default"
        )
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--fleet",
        type=fleet_name,
        help="install the dispatcher instance for one named fleet",
    )
    parser.add_argument("--dispatcher-template", type=Path,
                        help="caller-owned systemd unit template, required for Matrix")
    parser.add_argument("--config", type=Path, help="private runtime configuration")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.config:
        os.environ["FLEET_ORCHESTRATOR_CONFIG"] = str(args.config.resolve())
    transport = os.environ.get("AGENT_BUS_TRANSPORT", str(cfg.get("bus.transport", "local")))
    if args.fleet:
        resolved = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "lib" / "fleet-profile.py"),
             "resolve", args.fleet, "--field", "agent_bus_transport"],
            check=True, capture_output=True, text=True,
        )
        transport = resolved.stdout.strip()
        if transport not in {"local", "matrix"}:
            raise RuntimeError(
                f"fleet {args.fleet} resolved unsupported Agent Bus transport "
                f"{transport!r}"
            )
    template_key = "bus.named_dispatcher_template" if args.fleet else "bus.dispatcher_template"
    template = args.dispatcher_template or cfg.path(template_key)
    if transport == "matrix":
        if template is None or not template.is_file():
            raise ValueError("Matrix dispatcher installation requires a caller-owned unit template")
    CODEX.parent.mkdir(parents=True, exist_ok=True)
    text = CODEX.read_text() if CODEX.exists() else ""
    unit_name = None
    unit_path = None
    unit_text = None
    if args.fleet and transport == "matrix":
        unit_name = f"agent-bus-dispatcher@{args.fleet}.service"
        unit_path = FLEET_UNIT
        unit_text = render_unit(template)
    elif not args.fleet and transport == "matrix":
        unit_name = UNIT.name
        unit_path = UNIT
        unit_text = render_unit(template)
    updated = replace_managed_hook(text)
    if updated != text:
        atomic_write(CODEX, updated)
    if unit_name and unit_path and unit_text:
        UNIT_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write(unit_path, unit_text)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "enable", "--now", unit_name], check=True)
    if transport == "local":
        print(
            f"installed Codex Stop hook for local fleet {args.fleet or 'default'};"
            " no pull dispatcher is needed; restart Codex and trust it via /hooks"
        )
    elif args.fleet:
        print(
            f"installed Agent Bus dispatcher for fleet {args.fleet} and Codex Stop hook;"
            " restart Codex and trust it via /hooks"
        )
    else:
        print("installed Agent Bus dispatcher and Codex Stop hook; restart Codex and trust it via /hooks")


if __name__ == "__main__":
    main()
