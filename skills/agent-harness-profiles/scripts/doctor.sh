#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SKILL_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
config_file="${BACKUP_CONFIG:-${HOME}/.config/backup/config}"
errors=0
warnings=0

usage() {
  printf 'usage: %s [--config FILE]\n' "${0##*/}" >&2
}

if (( $# > 0 )); then
  [[ $# -eq 2 && $1 == --config ]] || { usage; exit 2; }
  config_file=$2
fi

ok() { printf 'ok: %s\n' "$1"; }
warn() { printf 'warning: %s\n' "$1" >&2; warnings=$((warnings + 1)); }
error() { printf 'error: %s\n' "$1" >&2; errors=$((errors + 1)); }

if (( BASH_VERSINFO[0] >= 4 )); then
  ok 'Bash 4+'
else
  error 'Bash 4+ is required'
fi
for required in git realpath rsync; do
  if command -v "${required}" >/dev/null 2>&1; then
    ok "command available: ${required}"
  else
    error "required command unavailable: ${required}"
  fi
done
if command -v realpath >/dev/null 2>&1; then
  if realpath -m -- / >/dev/null 2>&1 && realpath -ms -- / >/dev/null 2>&1; then
    ok 'realpath supports required options'
  else
    error 'realpath must support the -m and -s options'
  fi
fi
command -v sqlite3 >/dev/null 2>&1 || warn 'sqlite3 is unavailable; discovered OpenCode databases will fail Backup without replacing previous snapshots'

for script in "${SCRIPT_DIR}"/*.sh; do
  if bash -n "${script}"; then
    ok "script syntax: ${script##*/}"
  else
    error "script syntax: ${script##*/}"
  fi
done

if [[ -f "${config_file}" ]]; then
  if bash -n "${config_file}"; then
    ok 'configuration syntax'
    # The same trusted local shell configuration is read by every command.
    source "${config_file}"
  else
    error 'configuration syntax'
  fi
  if "${SCRIPT_DIR}/render-launchers.sh" --config "${config_file}" --check >/dev/null &&
    "${SCRIPT_DIR}/prepare-opencode-roots.sh" --config "${config_file}" --check >/dev/null; then
    ok 'configured profile entries and launcher executables; source-time shell conflicts are checked on activation'
  else
    error 'configured profile entries'
  fi
else
  error "configuration not found: ${config_file}"
fi

shared_target="${HOME}/.agents/skills/agent-harness-profiles"
if [[ -L "${shared_target}" && "$(readlink -f -- "${shared_target}" 2>/dev/null || true)" == "${SKILL_DIR}" ]]; then
  ok 'shared Skill link'
else
  warn 'shared Skill link is not installed from this checkout'
fi

if [[ -n "${BACKUP_COMMAND:-}" ]]; then
  backup_target="${HOME}/bin/backup"
  if [[ "${BACKUP_COMMAND}" != /* || ! -x "${BACKUP_COMMAND}" || ! -f "${BACKUP_COMMAND}" ]]; then
    error 'BACKUP_COMMAND must name an absolute executable command'
  elif [[ -L "${backup_target}" && "$(readlink -f -- "${backup_target}" 2>/dev/null || true)" == "$(readlink -f -- "${BACKUP_COMMAND}")" ]]; then
    ok 'configured Backup command link'
  elif [[ -e "${backup_target}" || -L "${backup_target}" ]]; then
    error "divergent Backup command target: ${backup_target}"
  else
    warn 'configured Backup command link is not installed'
  fi
fi

printf 'Doctor summary: %d error(s), %d warning(s)\n' "${errors}" "${warnings}"
(( errors == 0 ))
