#!/usr/bin/env bash
# Stage the public turn reporter. Trust acceptance belongs to the caller.
set -euo pipefail
if [[ "${1:-}" == --config ]]; then
  [[ $# -ge 2 ]] || { echo 'FAIL: --config requires a file' >&2; exit 2; }
  export FLEET_ORCHESTRATOR_CONFIG="$2"
  shift 2
fi
[[ $# -eq 0 ]] || { echo 'Usage: stage-codex-turn-hooks.sh [--config FILE]' >&2; exit 2; }
config="${TURN_HOOKS_CODEX_CONFIG:-${CODEX_HOME:-$HOME/.codex}/config.toml}"
reporter="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/orc-turn-report.py"
exec "${FLEET_ORCHESTRATOR_PYTHON:-python3}" -B - "$config" "$reporter" <<'PY'
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tomllib

config, reporter = map(Path, sys.argv[1:])
if not config.is_file():
    raise SystemExit("FAIL: configured Codex configuration file does not exist")
text = config.read_text()
hooks = tomllib.loads(text).get("hooks", {})
command = [sys.executable, "-B", str(reporter), "--harness", "codex"]
selected = os.environ.get("FLEET_ORCHESTRATOR_CONFIG")
if selected:
    command = ["env", "FLEET_ORCHESTRATOR_CONFIG=" + str(Path(selected).resolve()), *command]
missing = []
for event in ("UserPromptSubmit", "Stop"):
    matches = []
    for group in hooks.get(event, []):
        for hook in group.get("hooks", []):
            argv = shlex.split(hook.get("command", ""))
            if any(Path(arg).name in {"orc-turn-report", "orc-turn-report.py"} for arg in argv):
                matches.append(hook)
    if len(matches) > 1 or any(hook.get("type") != "command" for hook in matches):
        raise SystemExit(f"FAIL: inspect conflicting {event} turn reporter entries before staging")
    if not matches:
        missing.append(event)
if not missing:
    print("OK turn reporter entries already exist; configuration preserved")
    raise SystemExit(0)
backup = config.with_name(config.name + ".bak.turn-hooks")
if backup.exists():
    raise SystemExit("FAIL: turn-hook backup already exists; inspect it before staging")
shutil.copy2(config, backup)
with config.open("a") as output:
    output.write("\n# Public fleet turn reporter; the caller accepts harness trust.\n")
    for event in missing:
        output.write(f"[[hooks.{event}]]\n[[hooks.{event}.hooks]]\n")
        output.write('type = "command"\ncommand = ' + json.dumps(shlex.join(command)) + "\ntimeout = 5\n\n")
print("OK turn reporter entries staged; original configuration backed up")
print("Reload the harness through its normal process and review its trust prompt. No trust was granted by this script.")
PY
