#!/usr/bin/env bash
set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
readonly SKILL_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd -P)"
readonly ORIGINAL_HOME="${HOME}"
config_file="${BACKUP_CONFIG:-${HOME}/.config/backup/config}"
output_file="-"
temporary_file=""
check_only=false

usage() {
  printf 'usage: %s [--config FILE] [--output FILE|-] [--check]\n' "${0##*/}" >&2
}

fail() {
  printf 'launcher generation failed: %s\n' "$1" >&2
  exit 1
}

while (( $# > 0 )); do
  case "$1" in
    --config)
      (( $# >= 2 )) || { usage; exit 2; }
      config_file=$2
      shift 2
      ;;
    --output)
      (( $# >= 2 )) || { usage; exit 2; }
      output_file=$2
      shift 2
      ;;
    --check)
      check_only=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

[[ -f "${config_file}" ]] || fail "configuration file not found: ${config_file}"
bash -n "${config_file}" || fail 'configuration syntax is invalid'
# shellcheck disable=SC1090
source "${config_file}"
[[ "${HOME}" == "${ORIGINAL_HOME}" ]] || fail 'configuration must not change HOME'
readonly PROFILE_INSTALL_HOME="${ORIGINAL_HOME}"
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/profile-paths.sh"

CLAUDE_PROFILES="${CLAUDE_PROFILES:-}"
CODEX_PROFILES="${CODEX_PROFILES:-}"
OPENCODE_PROFILES="${OPENCODE_PROFILES:-}"
DSH_PROFILES="${DSH_PROFILES:-}"
CLAUDE_HOME="${CLAUDE_HOME:-${PROFILE_INSTALL_HOME}/.claude}"
CODEX_HOME="${CODEX_HOME:-${PROFILE_INSTALL_HOME}/.codex}"
DSH_HOME="${DSH_HOME:-${PROFILE_INSTALL_HOME}/.dsh}"
OPENCODE_DATA_DIR="${OPENCODE_DATA_DIR:-${XDG_DATA_HOME:-${PROFILE_INSTALL_HOME}/.local/share}/opencode}"
OPENCODE_CONFIG_SRC="${OPENCODE_CONFIG_SRC:-${XDG_CONFIG_HOME:-${PROFILE_INSTALL_HOME}/.config}/opencode}"
OPENCODE_STATE_DIR="${OPENCODE_STATE_DIR:-${XDG_STATE_HOME:-${PROFILE_INSTALL_HOME}/.local/state}/opencode}"

launcher_check() {
  local name=$1 executable=$2 variable=$3
  if type "${name}" >/dev/null 2>&1 || alias "${name}" >/dev/null 2>&1; then
    fail "launcher name already exists: ${name}; preserve it and choose another label"
  fi
  if [[ "${executable}" == /* ]]; then
    [[ -f "${executable}" && -x "${executable}" ]] ||
      fail "${variable} must name an absolute executable command"
    printf '  if [[ ! -f %q || ! -x %q ]]; then\n' "${executable}" "${executable}"
  else
    type -P "${executable}" >/dev/null ||
      fail "command unavailable: ${executable}; configure ${variable} as an absolute executable"
    printf '  if ! type -P %q >/dev/null; then\n' "${executable}"
  fi
  printf '    printf "%%s\\n" %q >&2; exit 1\n  fi\n' "Profile activation refused: command unavailable: ${executable}"
  printf '  if type %q >/dev/null 2>&1 || alias %q >/dev/null 2>&1; then\n' "${name}" "${name}"
  printf '    printf "%%s\\n" %q >&2; exit 1\n  fi\n' "Profile activation refused: launcher name already exists: ${name}; preserve it and choose another label"
}

emit_space_separated() {
  local tool=$1 command_name=$2 variable_name=$3 mode=$4
  local value entry label path seen="" command_variable executable
  value=${!variable_name}
  command_variable=${variable_name%_PROFILES}_COMMAND
  executable=${!command_variable:-${command_name}}
  if [[ -n "${value}" && -n "${!command_variable:-}" && "${executable}" != /* ]]; then
    fail "${command_variable} must name an absolute executable command"
  fi
  for entry in ${value}; do
    label=${entry%%:*}
    path=${entry#*:}
    [[ -n "${label}" && "${label}" != "${entry}" && -n "${path}" ]] ||
      fail "malformed ${variable_name} entry: ${entry}"
    profile_validate_root "${tool}" "${label}" "${path}"
    [[ " ${seen} " != *" ${label} "* ]] || fail "duplicate ${tool} label: ${label}"
    seen="${seen} ${label}"
    if [[ "${mode}" == check ]]; then
      launcher_check "${command_name}-${label}" "${executable}" "${command_variable}"
      continue
    fi
    case "${tool}" in
      claude)
        printf '%s-%s() {\n  CLAUDE_CONFIG_DIR=%q command %q "$@"\n}\n\n' \
          "${command_name}" "${label}" "${path}" "${executable}"
        ;;
      codex)
        printf '%s-%s() {\n  CODEX_HOME=%q command %q "$@"\n}\n\n' \
          "${command_name}" "${label}" "${path}" "${executable}"
        ;;
      opencode)
        printf '%s-%s() {\n  local profile_root=%q\n  XDG_DATA_HOME="$profile_root/share" XDG_STATE_HOME="$profile_root/state" XDG_CONFIG_HOME="$profile_root/config" command %q "$@"\n}\n\n' \
          "${command_name}" "${label}" "${path}" "${executable}"
        ;;
    esac
  done
}

emit_dsh() {
  local mode=$1 entry label path seen="" executable=${DSH_COMMAND:-dsh}
  if [[ -n "${DSH_PROFILES}" && -n "${DSH_COMMAND:-}" && "${executable}" != /* ]]; then
    fail 'DSH_COMMAND must name an absolute executable command'
  fi
  while IFS= read -r entry; do
    [[ -n "${entry}" ]] || continue
    label=${entry%%:*}
    path=${entry#*:}
    [[ -n "${label}" && "${label}" != "${entry}" && -n "${path}" ]] ||
      fail "malformed DSH_PROFILES entry: ${entry}"
    profile_validate_root dsh "${label}" "${path}"
    [[ " ${seen} " != *" ${label} "* ]] || fail "duplicate dsh label: ${label}"
    seen="${seen} ${label}"
    if [[ "${mode}" == check ]]; then
      launcher_check "dsh-${label}" "${executable}" DSH_COMMAND
    else
      printf 'dsh-%s() {\n  DSH_HOME=%q command %q "$@"\n}\n\n' "${label}" "${path}" "${executable}"
    fi
  done <<< "${DSH_PROFILES}"
}

render_entries() {
  local mode=$1
  profile_reset_root_registry
  profile_reserve_path 'Skill checkout' "$(git -C "${SKILL_DIR}" rev-parse --show-toplevel 2>/dev/null || printf '%s' "${SKILL_DIR}")"
  profile_reserve_fixed_install_roots
  profile_reserve_native_roots
  profile_reserve_path 'profile configuration' "${config_file}"
  emit_space_separated claude claude CLAUDE_PROFILES "${mode}"
  emit_space_separated codex codex CODEX_PROFILES "${mode}"
  emit_space_separated opencode opencode OPENCODE_PROFILES "${mode}"
  emit_dsh "${mode}"
}

render() {
  printf '%s\n' '# Generated by agent-harness-profiles. Edit the config, then regenerate.'
  printf '%s\n\n' '# Plain commands are unchanged; only the configured root variables are overridden.'
  # Check in the caller's shell before defining anything, including unexported aliases/functions.
  printf '%s\n' 'if ! (' ':'
  render_entries check
  printf '%s\n\n' '); then' '  return 1 2>/dev/null || exit 1' 'fi'
  render_entries define
}

validate_output() {
  local lexical_output resolved_output

  [[ "${output_file}" == - ]] && return
  [[ "${output_file}" == /* && "${output_file}" != / ]] || fail 'output path must be absolute and non-root'
  [[ "${output_file}" != *$'\n'* ]] || fail 'output path contains a newline'
  case "${output_file}/" in
    */../*|*/./*) fail 'output path contains a dot path component' ;;
  esac
  [[ ! -L "${output_file}" ]] || fail "refusing to replace symlinked output: ${output_file}"
  if [[ -f "${output_file}" ]] &&
    ! head -n 1 -- "${output_file}" | grep -Fqx '# Generated by agent-harness-profiles. Edit the config, then regenerate.'; then
    fail "refusing unmanaged output: ${output_file}"
  fi
  [[ ! -e "${output_file}" || -f "${output_file}" ]] ||
    fail "refusing non-file output: ${output_file}"
  lexical_output=$(profile_lexical_path "${output_file}") || fail 'output path cannot be normalized'
  resolved_output=$(profile_resolved_path "${output_file}") || fail 'output path cannot be resolved'
  profile_require_disjoint_root 'launcher output' "${lexical_output}" "${resolved_output}"
  profile_require_disjoint_paths 'launcher output' "${output_file}" \
    'shared Skill link' "${PROFILE_INSTALL_HOME}/.agents/skills/agent-harness-profiles"
  profile_require_disjoint_paths 'launcher output' "${output_file}" \
    'stable Backup command link' "${PROFILE_INSTALL_HOME}/bin/backup"
  profile_validate_writable_parent "${output_file}" "launcher output ${output_file}"
}

render >/dev/null
validate_output
if [[ "${check_only}" == true ]]; then
  exit 0
elif [[ "${output_file}" == - ]]; then
  render
else
  install -d -m 0700 "$(dirname -- "${output_file}")"
  temporary_file=$(mktemp "${output_file}.tmp.XXXXXX")
  trap '[[ -z "${temporary_file}" ]] || rm -f -- "${temporary_file}"' EXIT
  render > "${temporary_file}"
  chmod 0600 "${temporary_file}"
  mv -f -- "${temporary_file}" "${output_file}"
  temporary_file=""
  printf 'Generated launchers: %s\n' "${output_file}"
fi
