#!/usr/bin/env bash

# Shared validation for caller-configured profile roots. Callers must define
# fail() and PROFILE_INSTALL_HOME before invoking these helpers.

declare -A PROFILE_ROOT_LEXICAL_OWNERS=()
declare -A PROFILE_ROOT_RESOLVED_OWNERS=()

profile_reset_root_registry() {
  PROFILE_ROOT_LEXICAL_OWNERS=()
  PROFILE_ROOT_RESOLVED_OWNERS=()
}

profile_path_is_within() {
  local parent=$1 candidate=$2
  if [[ "${parent}" == / ]]; then
    [[ "${candidate}" == /* ]]
    return
  fi
  [[ "${candidate}" == "${parent}" || "${candidate}" == "${parent}/"* ]]
}

profile_lexical_path() {
  realpath -ms -- "$1"
}

profile_resolved_path() {
  realpath -m -- "$1"
}

profile_require_path_tools() {
  command -v realpath >/dev/null 2>&1 || fail 'required command is unavailable: realpath'
  realpath -m -- / >/dev/null 2>&1 && realpath -ms -- / >/dev/null 2>&1 ||
    fail 'realpath must support the -m and -s options'
}

profile_validate_contained_path() {
  local policy_root=$1 candidate=$2 description=$3
  local lexical_root lexical_candidate resolved_root resolved_candidate

  lexical_root=$(profile_lexical_path "${policy_root}") ||
    fail "cannot normalize policy root for ${description}"
  lexical_candidate=$(profile_lexical_path "${candidate}") ||
    fail "cannot normalize ${description}"
  resolved_root=$(profile_resolved_path "${policy_root}") ||
    fail "cannot resolve policy root for ${description}"
  resolved_candidate=$(profile_resolved_path "${candidate}") ||
    fail "cannot resolve ${description}"
  profile_path_is_within "${lexical_root}" "${lexical_candidate}" ||
    fail "${description} is outside its permitted root"
  profile_path_is_within "${resolved_root}" "${resolved_candidate}" ||
    fail "${description} resolves outside its permitted root"
}

profile_require_disjoint_root() {
  local owner=$1 lexical_path=$2 resolved_path=$3
  local existing_path existing_owner

  for existing_path in "${!PROFILE_ROOT_LEXICAL_OWNERS[@]}"; do
    if profile_path_is_within "${existing_path}" "${lexical_path}" ||
      profile_path_is_within "${lexical_path}" "${existing_path}"; then
      existing_owner=${PROFILE_ROOT_LEXICAL_OWNERS["${existing_path}"]}
      fail "${owner} overlaps ${existing_owner}"
    fi
  done
  for existing_path in "${!PROFILE_ROOT_RESOLVED_OWNERS[@]}"; do
    if profile_path_is_within "${existing_path}" "${resolved_path}" ||
      profile_path_is_within "${resolved_path}" "${existing_path}"; then
      existing_owner=${PROFILE_ROOT_RESOLVED_OWNERS["${existing_path}"]}
      fail "${owner} overlaps ${existing_owner}"
    fi
  done
}

profile_require_disjoint_paths() {
  local owner=$1 path=$2 other_owner=$3 other_path=$4
  local lexical_path resolved_path other_lexical other_resolved

  profile_require_path_tools
  lexical_path=$(profile_lexical_path "${path}") || fail "cannot normalize ${owner}"
  resolved_path=$(profile_resolved_path "${path}") || fail "cannot resolve ${owner}"
  other_lexical=$(profile_lexical_path "${other_path}") ||
    fail "cannot normalize ${other_owner}"
  other_resolved=$(profile_resolved_path "${other_path}") ||
    fail "cannot resolve ${other_owner}"
  if profile_path_is_within "${lexical_path}" "${other_lexical}" ||
    profile_path_is_within "${other_lexical}" "${lexical_path}" ||
    profile_path_is_within "${resolved_path}" "${other_resolved}" ||
    profile_path_is_within "${other_resolved}" "${resolved_path}"; then
    fail "${owner} overlaps ${other_owner}"
  fi
}

profile_validate_writable_parent() {
  local target=$1 description=$2 parent existing_parent

  parent=$(dirname -- "${target}")
  existing_parent=${parent}
  while [[ ! -e "${existing_parent}" && ! -L "${existing_parent}" ]]; do
    [[ "${existing_parent}" != / ]] || break
    existing_parent=$(dirname -- "${existing_parent}")
  done
  [[ -d "${existing_parent}" ]] || fail "${description} has a non-directory parent"
  [[ -w "${existing_parent}" ]] || fail "${description} has no writable parent"
}

profile_reserve_path() {
  local owner=$1 path=$2 lexical_path resolved_path home_lexical home_resolved

  profile_require_path_tools
  [[ "${path}" == /* && "${path}" != / ]] ||
    fail "reserved path must be absolute and non-root: ${owner}"
  [[ "${path}" != *$'\n'* ]] || fail "reserved path contains a newline: ${owner}"
  case "${path}/" in
    */../*|*/./*) fail "reserved path contains a dot path component: ${owner}" ;;
  esac
  lexical_path=$(profile_lexical_path "${path}") ||
    fail "cannot normalize reserved path: ${owner}"
  resolved_path=$(profile_resolved_path "${path}") ||
    fail "cannot resolve reserved path: ${owner}"
  home_lexical=$(profile_lexical_path "${PROFILE_INSTALL_HOME}") ||
    fail 'HOME cannot be normalized'
  home_resolved=$(profile_resolved_path "${PROFILE_INSTALL_HOME}") ||
    fail 'HOME cannot be resolved'
  [[ "${lexical_path}" != / && "${lexical_path}" != "${home_lexical}" ]] ||
    fail "reserved path reaches HOME or /: ${owner}"
  [[ "${resolved_path}" != / && "${resolved_path}" != "${home_resolved}" ]] ||
    fail "reserved path resolves to HOME or /: ${owner}"
  profile_require_disjoint_root "${owner}" "${lexical_path}" "${resolved_path}"
  PROFILE_ROOT_LEXICAL_OWNERS["${lexical_path}"]="${owner}"
  PROFILE_ROOT_RESOLVED_OWNERS["${resolved_path}"]="${owner}"
}

profile_reserve_native_roots() {
  local excluded_tool=${1:-}

  [[ "${excluded_tool}" == claude ]] ||
    profile_reserve_path 'native Claude state' "${CLAUDE_HOME}"
  [[ "${excluded_tool}" == codex ]] ||
    profile_reserve_path 'native Codex state' "${CODEX_HOME}"
  [[ "${excluded_tool}" == dsh ]] ||
    profile_reserve_path 'native DeepSeek Harness state' "${DSH_HOME}"
  if [[ -n "${CURSOR_PROFILES:-}" && "${excluded_tool}" != cursor ]]; then
    profile_reserve_path 'native Cursor configuration' "${CURSOR_HOME:-${PROFILE_INSTALL_HOME}/.cursor}"
  fi
  if [[ "${excluded_tool}" != opencode ]]; then
    profile_reserve_path 'native OpenCode data' "${OPENCODE_DATA_DIR}"
    profile_reserve_path 'native OpenCode config' "${OPENCODE_CONFIG_SRC}"
    profile_reserve_path 'native OpenCode state' "${OPENCODE_STATE_DIR}"
  fi
}

profile_reserve_fixed_install_roots() {
  profile_reserve_path 'shared Skill discovery root' "${PROFILE_INSTALL_HOME}/.agents"
  profile_reserve_path 'Backup command directory' "${PROFILE_INSTALL_HOME}/bin"
}

profile_validate_root() {
  local tool=$1 label=$2 configured_path=$3
  local lexical_path resolved_path home_lexical home_resolved
  local owner

  [[ "${label}" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || fail "${tool} label is unsafe: ${label}"
  [[ "${configured_path}" == /* && "${configured_path}" != / ]] ||
    fail "${tool} root must be an absolute non-root path for label ${label}"
  [[ "${configured_path}" != *$'\n'* ]] ||
    fail "${tool} root contains a newline for label ${label}"
  case "${configured_path}/" in
    */../*|*/./*) fail "${tool} root contains a dot path component for label ${label}" ;;
  esac

  profile_require_path_tools
  lexical_path=$(profile_lexical_path "${configured_path}") ||
    fail "${tool} root cannot be normalized for label ${label}"
  resolved_path=$(profile_resolved_path "${configured_path}") ||
    fail "${tool} root cannot be resolved for label ${label}"
  home_lexical=$(profile_lexical_path "${PROFILE_INSTALL_HOME}") ||
    fail 'HOME cannot be normalized'
  home_resolved=$(profile_resolved_path "${PROFILE_INSTALL_HOME}") ||
    fail 'HOME cannot be resolved'

  [[ "${lexical_path}" != / && "${lexical_path}" != "${home_lexical}" ]] ||
    fail "${tool} root reaches HOME or / for label ${label}"
  [[ "${resolved_path}" != / && "${resolved_path}" != "${home_resolved}" ]] ||
    fail "${tool} root resolves to HOME or / for label ${label}"
  if [[ -e "${configured_path}" || -L "${configured_path}" ]]; then
    [[ -d "${configured_path}" ]] || fail "${tool} root is not a directory for label ${label}"
  fi

  owner="configured ${tool} root ${label}"
  profile_require_disjoint_root "${owner}" "${lexical_path}" "${resolved_path}"
  PROFILE_ROOT_LEXICAL_OWNERS["${lexical_path}"]="${owner}"
  PROFILE_ROOT_RESOLVED_OWNERS["${resolved_path}"]="${owner}"
  PROFILE_VALIDATED_ROOT=${resolved_path}
}

profile_validate_managed_directory() {
  local resolved_root=$1 target=$2 description=$3
  local resolved_target

  resolved_target=$(profile_resolved_path "${target}") ||
    fail "cannot resolve ${description}"
  profile_path_is_within "${resolved_root}" "${resolved_target}" ||
    fail "${description} escapes its configured profile root"
  [[ ! -L "${target}" ]] || fail "${description} must not be a symlink"
  if [[ -e "${target}" ]]; then
    [[ -d "${target}" ]] || fail "${description} is not a directory"
  else
    profile_validate_writable_parent "${target}" "${description}"
  fi
}

profile_validate_managed_file() {
  local resolved_root=$1 target=$2 description=$3
  local resolved_target

  resolved_target=$(profile_resolved_path "${target}") ||
    fail "cannot resolve ${description}"
  profile_path_is_within "${resolved_root}" "${resolved_target}" ||
    fail "${description} escapes its configured profile root"
  [[ ! -L "${target}" ]] || fail "${description} must not be a symlink"
  if [[ -e "${target}" ]]; then
    [[ -f "${target}" ]] || fail "${description} is not a regular file"
  else
    profile_validate_writable_parent "${target}" "${description}"
  fi
}

profile_require_stable_checkout() {
  local skill_root=$1 top git_dir common_dir branch skill_relative

  command -v git >/dev/null 2>&1 || fail 'required command is unavailable: git'
  profile_require_path_tools
  top=$(git -C "${skill_root}" rev-parse --show-toplevel 2>/dev/null) ||
    fail 'installation requires a stable Git checkout'
  [[ -d "${top}/.git" ]] || fail 'installation is refused from a linked Git worktree'
  git_dir=$(git -C "${top}" rev-parse --absolute-git-dir) || fail 'Git directory cannot be resolved'
  common_dir=$(git -C "${top}" rev-parse --git-common-dir) || fail 'common Git directory cannot be resolved'
  [[ "${common_dir}" == /* ]] || common_dir="${top}/${common_dir}"
  [[ "$(profile_resolved_path "${git_dir}")" == "$(profile_resolved_path "${common_dir}")" ]] ||
    fail 'installation is refused from a linked Git worktree'
  branch=$(git -C "${top}" symbolic-ref --quiet --short HEAD 2>/dev/null) ||
    fail 'installation requires the main branch of a stable checkout'
  [[ "${branch}" == main ]] || fail 'installation requires the main branch of a stable checkout'
  if [[ "${skill_root}" == "${top}" ]]; then
    skill_relative=.
  else
    skill_relative=${skill_root#"${top}/"}
  fi
  git -C "${top}" ls-files --error-unmatch -- "${skill_relative}/SKILL.md" >/dev/null 2>&1 ||
    fail 'Skill package is not tracked by Git'
  [[ -z "$(git -C "${top}" status --porcelain --untracked-files=all -- "${skill_relative}")" ]] ||
    fail 'Skill package contains uncommitted changes'
}

profile_validate_backup_command() {
  [[ -n "${BACKUP_COMMAND:-}" ]] || return 0
  [[ "${BACKUP_COMMAND}" == /* && -f "${BACKUP_COMMAND}" && -x "${BACKUP_COMMAND}" ]] ||
    fail 'BACKUP_COMMAND must name an absolute executable command'
}
