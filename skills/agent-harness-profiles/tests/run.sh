#!/usr/bin/env bash
set -euo pipefail

readonly ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
readonly TEMP_ROOT="$(mktemp -d)"

cleanup() {
  rm -rf -- "${TEMP_ROOT}"
}
trap cleanup EXIT

fail() {
  printf 'profile setup test failed: %s\n' "$1" >&2
  exit 1
}

assert_file() {
  [[ -f "$1" ]] || fail "expected file: $1"
}

assert_not_exists() {
  [[ ! -e "$1" && ! -L "$1" ]] || fail "expected path to be absent: $1"
}

assert_link_target() {
  local link_path=$1 expected_target=$2
  [[ -L "${link_path}" ]] || fail "expected symbolic link: ${link_path}"
  [[ "$(readlink -f -- "${link_path}" 2>/dev/null || true)" == \
    "$(readlink -f -- "${expected_target}" 2>/dev/null || true)" ]] ||
    fail "symbolic link has the wrong target: ${link_path}"
}

copy_primary_checkout() {
  local checkout=$1
  install -d -m 0700 "${checkout}/skills"
  cp -a -- "${ROOT_DIR}" "${checkout}/skills/agent-harness-profiles"
  printf '#!/usr/bin/env sh\nexit 0\n' > "${checkout}/backup.sh"
  chmod +x "${checkout}/backup.sh"
  git -C "${checkout}" init -q -b main
  git -C "${checkout}" config user.name 'Synthetic Test'
  git -C "${checkout}" config user.email 'synthetic@example.invalid'
  git -C "${checkout}" add backup.sh skills
  git -C "${checkout}" commit -q -m 'synthetic primary checkout'
}

write_full_config() {
  local home_dir=$1 config_file=$2
  install -d -m 0700 "$(dirname -- "${config_file}")"
  cat > "${config_file}" <<EOF
MACHINE_ID="fixture-profile-host"
BACKUP_COMMAND="${PRIMARY_CHECKOUT}/backup.sh"
DSH_COMMAND="${fake_bin}/deepseek harness"
CLAUDE_PROFILES="alpha:${home_dir}/profiles/claude-alpha beta:${home_dir}/profiles/claude-beta"
CODEX_PROFILES="alpha:${home_dir}/profiles/codex-alpha beta:${home_dir}/profiles/codex-beta"
OPENCODE_PROFILES="alpha:${home_dir}/.config/opencode-profiles/alpha beta:${home_dir}/.config/opencode-profiles/beta"
DSH_PROFILES="alpha:${home_dir}/profiles/dsh-alpha
beta:${home_dir}/profiles/dsh-beta
gamma:${home_dir}/profiles/dsh gamma"
EOF
}

snapshot_tree() {
  local tree_root=$1 output_file=$2 relative item mode digest
  (
    cd -- "${tree_root}"
    while IFS= read -r -d '' relative; do
      item="${tree_root}/${relative}"
      mode=$(stat -c '%a' -- "${item}")
      if [[ -L "${item}" ]]; then
        printf 'link\t%s\t%s\t%s\n' "${mode}" "${relative}" "$(readlink -- "${item}")"
      elif [[ -f "${item}" ]]; then
        digest=$(sha256sum -- "${item}")
        printf 'file\t%s\t%s\t%s\n' "${mode}" "${relative}" "${digest%% *}"
      elif [[ -d "${item}" ]]; then
        printf 'directory\t%s\t%s\n' "${mode}" "${relative}"
      else
        printf 'other\t%s\t%s\n' "${mode}" "${relative}"
      fi
    done < <(find . -path './.git' -prune -o -mindepth 1 -printf '%P\0' | LC_ALL=C sort -z)
  ) > "${output_file}"
}

expect_failure_without_changes() {
  local name=$1 state_root=$2 home_dir=$3
  shift 3
  local before="${TEMP_ROOT}/snapshots/${name}.before"
  local after="${TEMP_ROOT}/snapshots/${name}.after"
  local stdout_file="${TEMP_ROOT}/output/${name}.stdout"
  local stderr_file="${TEMP_ROOT}/output/${name}.stderr"
  local status

  snapshot_tree "${state_root}" "${before}"
  set +e
  HOME="${home_dir}" "$@" >"${stdout_file}" 2>"${stderr_file}"
  status=$?
  set -e
  (( status != 0 )) || fail "${name} unexpectedly succeeded"
  snapshot_tree "${state_root}" "${after}"
  if ! cmp -s -- "${before}" "${after}"; then
    diff -u -- "${before}" "${after}" >&2 || true
    fail "${name} changed files before rejecting the request"
  fi
}

expect_failure_without_home_or_repo_changes() {
  local name=$1 state_root=$2 home_dir=$3 repository=$4
  shift 4
  local before="${TEMP_ROOT}/snapshots/${name}.repo-before"
  local after="${TEMP_ROOT}/snapshots/${name}.repo-after"

  snapshot_tree "${repository}" "${before}"
  expect_failure_without_changes "${name}" "${state_root}" "${home_dir}" "$@"
  snapshot_tree "${repository}" "${after}"
  if ! cmp -s -- "${before}" "${after}"; then
    diff -u -- "${before}" "${after}" >&2 || true
    fail "${name} changed the Backup checkout before rejecting the request"
  fi
}

readonly PRIMARY_CHECKOUT="${TEMP_ROOT}/primary"
readonly INSTALLER="${PRIMARY_CHECKOUT}/skills/agent-harness-profiles/scripts/install.sh"
readonly DOCTOR="${PRIMARY_CHECKOUT}/skills/agent-harness-profiles/scripts/doctor.sh"
install -d -m 0700 "${TEMP_ROOT}/snapshots" "${TEMP_ROOT}/output" "${TEMP_ROOT}/cases"
fake_bin="${TEMP_ROOT}/fake-bin"
install -d -m 0700 "${fake_bin}"
for executable in claude codex opencode 'deepseek harness'; do
  cat > "${fake_bin}/${executable}" <<'EOF'
#!/bin/bash
if [[ -n "${PROFILE_TEST_CAPTURE:-}" ]]; then
  printf '%s\n' "$HOME" "$PWD" "$CLAUDE_CONFIG_DIR" "$CODEX_HOME" "$DSH_HOME" \
    "$XDG_DATA_HOME" "$XDG_STATE_HOME" "$XDG_CONFIG_HOME" "$INHERITED_CANARY" \
    "$OPENCODE_CONFIG" "$@" > "$PROFILE_TEST_CAPTURE"
fi
exit "${PROFILE_TEST_STATUS:-0}"
EOF
  chmod 0700 "${fake_bin}/${executable}"
done
export PATH="${fake_bin}:${PATH}"
copy_primary_checkout "${PRIMARY_CHECKOUT}"
export BACKUP_COMMAND="${PRIMARY_CHECKOUT}/backup.sh"

# A primary main checkout is the durable source for installed repository links.
success_case="${TEMP_ROOT}/cases/success"
success_home="${success_case}/home"
success_config="${success_home}/.config/backup/config"
preserved_opencode_config="${success_home}/.config/opencode-profiles/alpha/config/opencode/opencode.json"
install -d -m 0700 \
  "${success_home}/.config/example-tool" \
  "${success_home}/.config/opencode" \
  "$(dirname -- "${preserved_opencode_config}")" \
  "${success_home}/.local/share/opencode"
printf 'NATIVE_CONFIG_CANARY\n' > "${success_home}/.config/example-tool/settings.json"
printf 'NATIVE_OPENCODE_CANARY\n' > "${success_home}/.config/opencode/opencode.json"
printf 'SYNTHETIC_AUTH_CANARY_DO_NOT_COPY\n' > \
  "${success_home}/.local/share/opencode/auth.json"
printf '%s\n' '{"synthetic":"preserve-this-config"}' > "${preserved_opencode_config}"
chmod 0751 "$(dirname -- "${preserved_opencode_config}")"
preserved_opencode_mode=$(stat -c '%a' -- "$(dirname -- "${preserved_opencode_config}")")
preserved_opencode_digest=$(sha256sum -- "${preserved_opencode_config}")
preserved_opencode_digest=${preserved_opencode_digest%% *}
write_full_config "${success_home}" "${success_config}"
HOME="${success_home}" "${INSTALLER}" --config "${success_config}" >/dev/null

launcher_file="${success_home}/.config/agent-harness-profiles/launchers.sh"
assert_file "${launcher_file}"
bash -n "${launcher_file}" || fail 'generated launchers have invalid shell syntax'
for launcher in \
  claude-alpha claude-beta codex-alpha codex-beta \
  opencode-alpha opencode-beta dsh-alpha dsh-beta dsh-gamma; do
  grep -q "^${launcher}()" "${launcher_file}" ||
    fail "generated launcher is missing: ${launcher}"
done
printf -v escaped_dsh_path '%q' "${success_home}/profiles/dsh gamma"
printf -v escaped_dsh_command '%q' "${fake_bin}/deepseek harness"
grep -Fq "DSH_HOME=${escaped_dsh_path} command ${escaped_dsh_command}" "${launcher_file}" ||
  fail 'DSH launcher did not preserve a configured path containing spaces'
# Every launcher routes two roots without clearing unrelated inherited settings,
# changing the caller's environment, or losing arguments/exit status.
(
  export HOME="${success_home}" CLAUDE_CONFIG_DIR=inherited-claude CODEX_HOME=inherited-codex
  export DSH_HOME=inherited-dsh XDG_DATA_HOME=inherited-data XDG_STATE_HOME=inherited-state
  export XDG_CONFIG_HOME=inherited-config INHERITED_CANARY=keep-me OPENCODE_CONFIG=keep-config-override
  export PROFILE_TEST_CAPTURE="${TEMP_ROOT}/output/launcher.env"
  expected="${TEMP_ROOT}/output/launcher.expected"
  # shellcheck disable=SC1090
  source "${launcher_file}"
  # The generated functions call executables rather than an unrelated base function.
  claude() { fail 'base function was called'; }
  codex() { fail 'base function was called'; }
  opencode() { fail 'base function was called'; }
  for tool in claude codex opencode dsh; do
    for label in alpha beta; do
      values=(inherited-claude inherited-codex inherited-dsh inherited-data inherited-state inherited-config)
      root="${success_home}/profiles/${tool}-${label}"
      case "${tool}" in
        claude) values[0]="${root}" ;;
        codex) values[1]="${root}" ;;
        dsh) values[2]="${root}" ;;
        opencode)
          root="${success_home}/.config/opencode-profiles/${label}"
          values[3]="${root}/share"; values[4]="${root}/state"; values[5]="${root}/config"
          ;;
      esac
      "${tool}-${label}" --synthetic-fixture 'argument with spaces' ''
      printf '%s\n' "$HOME" "$PWD" "${values[@]}" keep-me keep-config-override \
        --synthetic-fixture 'argument with spaces' '' > "${expected}"
      cmp -s -- "${expected}" "${PROFILE_TEST_CAPTURE}" ||
        fail "${tool}-${label} changed routing, inherited environment, or arguments"
    done
  done
  dsh-gamma
  grep -Fxq "${success_home}/profiles/dsh gamma" "${PROFILE_TEST_CAPTURE}" || fail 'DSH spaced root was not selected'
  [[ "$CLAUDE_CONFIG_DIR $CODEX_HOME $DSH_HOME $XDG_DATA_HOME $XDG_STATE_HOME $XDG_CONFIG_HOME" == \
    'inherited-claude inherited-codex inherited-dsh inherited-data inherited-state inherited-config' ]] ||
    fail 'launcher changed the parent environment'
  status=0
  PROFILE_TEST_STATUS=23 dsh-alpha || status=$?
  [[ "${status}" == 23 ]] || fail 'launcher lost executable exit status'
)

# Sourcing checks the caller's actual aliases/functions/PATH before any definitions.
for conflict in function alias executable; do
  (
    case "${conflict}" in
      function) dsh-alpha() { printf 'SPECIAL_WRAPPER\n'; } ;;
      alias) alias dsh-alpha='printf SPECIAL_ALIAS' ;;
      executable)
        printf '#!/bin/sh\nprintf SPECIAL_EXECUTABLE\n' > "${fake_bin}/dsh-alpha"
        chmod 0700 "${fake_bin}/dsh-alpha"
        ;;
    esac
    before=$(type dsh-alpha 2>/dev/null || alias dsh-alpha)
    if source "${launcher_file}" 2>"${TEMP_ROOT}/output/conflict-${conflict}.stderr"; then
      fail "activation accepted a ${conflict} collision"
    fi
    [[ "$(type dsh-alpha 2>/dev/null || alias dsh-alpha)" == "${before}" ]] || fail 'activation replaced a special wrapper'
    ! declare -F claude-alpha >/dev/null || fail 'activation installed functions before refusing a conflict'
    grep -Fq 'launcher name already exists: dsh-alpha' "${TEMP_ROOT}/output/conflict-${conflict}.stderr" ||
      fail 'activation did not identify the conflict'
  )
done
expect_failure_without_changes executable-launcher-conflict "${success_case}" "${success_home}" \
  "${INSTALLER}" --config "${success_config}"
[[ "$("${fake_bin}/dsh-alpha")" == SPECIAL_EXECUTABLE ]] || fail 'installer changed special executable'
rm -- "${fake_bin}/dsh-alpha"

# A command may disappear after rendering; activation still refuses it.
mv -- "${fake_bin}/deepseek harness" "${fake_bin}/held-command"
(
  if source "${launcher_file}" 2>"${TEMP_ROOT}/output/missing-command.stderr"; then
    fail 'activation accepted a missing command'
  fi
  ! declare -F claude-alpha >/dev/null || fail 'missing command caused partial activation'
)
expect_failure_without_changes missing-profile-command "${success_case}" "${success_home}" \
  "${INSTALLER}" --config "${success_config}"
expect_failure_without_changes doctor-missing-profile-command "${success_case}" "${success_home}" \
  "${DOCTOR}" --config "${success_config}"
mv -- "${fake_bin}/held-command" "${fake_bin}/deepseek harness"

for tool in CLAUDE CODEX OPENCODE DSH; do
  invalid_case="${TEMP_ROOT}/cases/invalid-${tool}-command"
  invalid_home="${invalid_case}/home"
  invalid_config="${invalid_home}/.config/backup/config"
  write_full_config "${invalid_home}" "${invalid_config}"
  printf '%s_COMMAND=relative-command\n' "${tool}" >> "${invalid_config}"
  expect_failure_without_changes "invalid-${tool}-command" "${invalid_case}" "${invalid_home}" \
    "${INSTALLER}" --config "${invalid_config}"
done

# Overrides work for every harness without any default executable on PATH.
mapping_home="${TEMP_ROOT}/cases/command-mapping/home"
mapping_config="${mapping_home}/.config/backup/config"
mapping_launchers="${TEMP_ROOT}/output/command-mapping.sh"
write_full_config "${mapping_home}" "${mapping_config}"
for tool in CLAUDE CODEX OPENCODE; do
  printf '%s_COMMAND=%q\n' "${tool}" "${fake_bin}/deepseek harness" >> "${mapping_config}"
done
command_path="${TEMP_ROOT}/commands-without-harnesses"
install -d -m 0700 "${command_path}"
for executable in bash dirname git realpath; do
  ln -s -- "$(type -P "${executable}")" "${command_path}/${executable}"
done
HOME="${mapping_home}" PATH="${command_path}" \
  "${PRIMARY_CHECKOUT}/skills/agent-harness-profiles/scripts/render-launchers.sh" \
  --config "${mapping_config}" > "${mapping_launchers}"
(
  export PATH="${command_path}"
  source "${mapping_launchers}"
  for tool in claude codex opencode dsh; do
    "${tool}-alpha"
    "${tool}-beta"
  done
)
printf 'unset DSH_COMMAND\n' >> "${mapping_config}"
expect_failure_without_changes missing-default-dsh "${mapping_home}" "${mapping_home}" \
  env PATH="${command_path}" "${PRIMARY_CHECKOUT}/skills/agent-harness-profiles/scripts/render-launchers.sh" \
  --config "${mapping_config}" --check
grep -Fq 'command unavailable: dsh; configure DSH_COMMAND' "${TEMP_ROOT}/output/missing-default-dsh.stderr" ||
  fail 'missing default dsh did not give an explicit executable selection remedy'
for label in alpha beta; do
  opencode_root="${success_home}/.config/opencode-profiles/${label}"
  assert_file "${opencode_root}/config/opencode/opencode.json"
  if find "${opencode_root}" -type l -print -quit | grep -q .; then
    fail "OpenCode profile contains a symbolic link: ${label}"
  fi
  assert_not_exists "${opencode_root}/config/example-tool"
  if grep -R -F -q -e 'NATIVE_CONFIG_CANARY' -e 'NATIVE_OPENCODE_CANARY' \
    -e 'SYNTHETIC_AUTH_CANARY_DO_NOT_COPY' "${opencode_root}"; then
    fail "OpenCode profile copied native configuration or authentication data: ${label}"
  fi
  assert_link_target \
    "${success_home}/profiles/claude-${label}/skills/agent-harness-profiles" \
    "${PRIMARY_CHECKOUT}/skills/agent-harness-profiles"
done
assert_link_target \
  "${success_home}/.agents/skills/agent-harness-profiles" \
  "${PRIMARY_CHECKOUT}/skills/agent-harness-profiles"
assert_link_target "${success_home}/bin/backup" "${PRIMARY_CHECKOUT}/backup.sh"
current_opencode_digest=$(sha256sum -- "${preserved_opencode_config}")
current_opencode_digest=${current_opencode_digest%% *}
[[ "${current_opencode_digest}" == "${preserved_opencode_digest}" ]] ||
  fail 'initial installation replaced an existing OpenCode configuration file'
[[ "$(stat -c '%a' -- "$(dirname -- "${preserved_opencode_config}")")" == \
  "${preserved_opencode_mode}" ]] ||
  fail 'initial installation changed permissions on an existing OpenCode directory'

# Repeating setup must preserve the same filesystem result.
snapshot_tree "${success_case}" "${TEMP_ROOT}/snapshots/success.before-repeat"
HOME="${success_home}" "${INSTALLER}" --config "${success_config}" >/dev/null
snapshot_tree "${success_case}" "${TEMP_ROOT}/snapshots/success.after-repeat"
current_opencode_digest=$(sha256sum -- "${preserved_opencode_config}")
current_opencode_digest=${current_opencode_digest%% *}
[[ "${current_opencode_digest}" == "${preserved_opencode_digest}" ]] ||
  fail 'repeated installation replaced an existing OpenCode configuration file'
cmp -s -- \
  "${TEMP_ROOT}/snapshots/success.before-repeat" \
  "${TEMP_ROOT}/snapshots/success.after-repeat" ||
  fail 'repeated installation changed the resulting filesystem tree'

# A linked worktree is disposable, so it cannot become a durable link source.
linked_checkout="${TEMP_ROOT}/linked"
git -C "${PRIMARY_CHECKOUT}" worktree add -q --detach "${linked_checkout}" HEAD
linked_case="${TEMP_ROOT}/cases/linked-worktree"
linked_home="${linked_case}/home"
linked_config="${linked_home}/.config/backup/config"
write_full_config "${linked_home}" "${linked_config}"
expect_failure_without_changes linked-worktree "${linked_case}" "${linked_home}" \
  "${linked_checkout}/skills/agent-harness-profiles/scripts/install.sh" \
  --config "${linked_config}"

# Every validation failure below must happen before launchers, roots, or links are written.
divergent_case="${TEMP_ROOT}/cases/divergent-backup"
divergent_home="${divergent_case}/home"
divergent_config="${divergent_home}/.config/backup/config"
write_full_config "${divergent_home}" "${divergent_config}"
install -d -m 0700 "${divergent_home}/bin"
printf 'unmanaged backup command\n' > "${divergent_home}/bin/backup"
expect_failure_without_changes divergent-backup "${divergent_case}" "${divergent_home}" \
  "${INSTALLER}" --config "${divergent_config}"

blocked_parent_case="${TEMP_ROOT}/cases/blocked-link-parent"
blocked_parent_home="${blocked_parent_case}/home"
blocked_parent_config="${blocked_parent_home}/.config/backup/config"
write_full_config "${blocked_parent_home}" "${blocked_parent_config}"
printf 'not a directory\n' > "${blocked_parent_home}/.agents"
expect_failure_without_changes blocked-link-parent \
  "${blocked_parent_case}" "${blocked_parent_home}" \
  "${INSTALLER}" --config "${blocked_parent_config}"

shared_escape_case="${TEMP_ROOT}/cases/shared-link-parent-escape"
shared_escape_home="${shared_escape_case}/home"
shared_escape_config="${shared_escape_home}/.config/backup/config"
shared_escape_outside="${shared_escape_case}/outside"
install -d -m 0700 "$(dirname -- "${shared_escape_config}")" "${shared_escape_outside}"
ln -s -- "${shared_escape_outside}" "${shared_escape_home}/.agents"
printf 'MACHINE_ID="fixture-profile-host"\n' > "${shared_escape_config}"
expect_failure_without_changes shared-link-parent-escape \
  "${shared_escape_case}" "${shared_escape_home}" \
  "${INSTALLER}" --config "${shared_escape_config}"

backup_escape_case="${TEMP_ROOT}/cases/backup-link-parent-escape"
backup_escape_home="${backup_escape_case}/home"
backup_escape_config="${backup_escape_home}/.config/backup/config"
backup_escape_outside="${backup_escape_case}/outside"
install -d -m 0700 "$(dirname -- "${backup_escape_config}")" "${backup_escape_outside}"
ln -s -- "${backup_escape_outside}" "${backup_escape_home}/bin"
printf 'MACHINE_ID="fixture-profile-host"\n' > "${backup_escape_config}"
expect_failure_without_changes backup-link-parent-escape \
  "${backup_escape_case}" "${backup_escape_home}" \
  "${INSTALLER}" --config "${backup_escape_config}"

repo_claude_case="${TEMP_ROOT}/cases/repo-claude-root"
repo_claude_home="${repo_claude_case}/home"
repo_claude_config="${repo_claude_home}/.config/backup/config"
install -d -m 0700 "$(dirname -- "${repo_claude_config}")"
cat > "${repo_claude_config}" <<EOF
CLAUDE_PROFILES="alpha:${PRIMARY_CHECKOUT}/synthetic-claude-profile"
EOF
expect_failure_without_home_or_repo_changes repo-claude-root \
  "${repo_claude_case}" "${repo_claude_home}" "${PRIMARY_CHECKOUT}" \
  "${INSTALLER}" --config "${repo_claude_config}"

repo_opencode_case="${TEMP_ROOT}/cases/repo-opencode-root"
repo_opencode_home="${repo_opencode_case}/home"
repo_opencode_config="${repo_opencode_home}/.config/backup/config"
install -d -m 0700 "$(dirname -- "${repo_opencode_config}")"
cat > "${repo_opencode_config}" <<EOF
OPENCODE_PROFILES="alpha:${PRIMARY_CHECKOUT}/synthetic-opencode-profile"
EOF
expect_failure_without_home_or_repo_changes repo-opencode-root \
  "${repo_opencode_case}" "${repo_opencode_home}" "${PRIMARY_CHECKOUT}" \
  "${INSTALLER}" --config "${repo_opencode_config}"

repo_launcher_case="${TEMP_ROOT}/cases/repo-launcher-output"
repo_launcher_home="${repo_launcher_case}/home"
repo_launcher_config="${repo_launcher_home}/.config/backup/config"
install -d -m 0700 "$(dirname -- "${repo_launcher_config}")"
printf 'MACHINE_ID="fixture-profile-host"\n' > "${repo_launcher_config}"
expect_failure_without_home_or_repo_changes repo-launcher-output \
  "${repo_launcher_case}" "${repo_launcher_home}" "${PRIMARY_CHECKOUT}" \
  "${INSTALLER}" --config "${repo_launcher_config}" \
  --launchers "${PRIMARY_CHECKOUT}/synthetic-output/launchers.sh"

opencode_launcher_case="${TEMP_ROOT}/cases/opencode-launcher-collision"
opencode_launcher_home="${opencode_launcher_case}/home"
opencode_launcher_config="${opencode_launcher_home}/.config/backup/config"
opencode_launcher_root="${opencode_launcher_home}/profiles/opencode-alpha"
install -d -m 0700 "$(dirname -- "${opencode_launcher_config}")"
cat > "${opencode_launcher_config}" <<EOF
OPENCODE_PROFILES="alpha:${opencode_launcher_root}"
EOF
expect_failure_without_changes opencode-launcher-collision \
  "${opencode_launcher_case}" "${opencode_launcher_home}" \
  "${INSTALLER}" --config "${opencode_launcher_config}" \
  --launchers "${opencode_launcher_root}/config/opencode/opencode.json"

opencode_config_case="${TEMP_ROOT}/cases/opencode-config-collision"
opencode_config_home="${opencode_config_case}/home"
opencode_config_root="${opencode_config_home}/profiles/opencode-alpha"
opencode_config_file="${opencode_config_root}/config/opencode/opencode.json"
install -d -m 0700 "$(dirname -- "${opencode_config_file}")"
cat > "${opencode_config_file}" <<EOF
OPENCODE_PROFILES="alpha:${opencode_config_root}"
EOF
expect_failure_without_changes opencode-config-collision \
  "${opencode_config_case}" "${opencode_config_home}" \
  "${INSTALLER}" --config "${opencode_config_file}"

backup_launcher_case="${TEMP_ROOT}/cases/backup-launcher-collision"
backup_launcher_home="${backup_launcher_case}/home"
backup_launcher_config="${backup_launcher_home}/.config/backup/config"
install -d -m 0700 "$(dirname -- "${backup_launcher_config}")"
printf 'MACHINE_ID="fixture-profile-host"\n' > "${backup_launcher_config}"
expect_failure_without_changes backup-launcher-collision \
  "${backup_launcher_case}" "${backup_launcher_home}" \
  "${INSTALLER}" --config "${backup_launcher_config}" \
  --launchers "${backup_launcher_home}/bin/backup"

claude_escape_case="${TEMP_ROOT}/cases/claude-skills-escape"
claude_escape_home="${claude_escape_case}/home"
claude_escape_config="${claude_escape_home}/.config/backup/config"
claude_escape_outside="${claude_escape_case}/outside"
install -d -m 0700 \
  "$(dirname -- "${claude_escape_config}")" \
  "${claude_escape_home}/.claude" \
  "${claude_escape_outside}"
ln -s -- "${claude_escape_outside}" "${claude_escape_home}/.claude/skills"
printf 'MACHINE_ID="fixture-profile-host"\n' > "${claude_escape_config}"
expect_failure_without_changes claude-skills-escape \
  "${claude_escape_case}" "${claude_escape_home}" \
  "${INSTALLER}" --config "${claude_escape_config}"

unmanaged_case="${TEMP_ROOT}/cases/unmanaged-launcher"
unmanaged_home="${unmanaged_case}/home"
unmanaged_config="${unmanaged_home}/.config/backup/config"
write_full_config "${unmanaged_home}" "${unmanaged_config}"
install -d -m 0700 "${unmanaged_home}/.config/agent-harness-profiles"
printf 'unmanaged launcher\n' > \
  "${unmanaged_home}/.config/agent-harness-profiles/launchers.sh"
expect_failure_without_changes unmanaged-launcher "${unmanaged_case}" "${unmanaged_home}" \
  "${INSTALLER}" --config "${unmanaged_config}"

dotdot_case="${TEMP_ROOT}/cases/dotdot-root"
dotdot_home="${dotdot_case}/home"
dotdot_config="${dotdot_home}/.config/backup/config"
install -d -m 0700 "$(dirname -- "${dotdot_config}")" "${dotdot_home}/profiles"
cat > "${dotdot_config}" <<EOF
CLAUDE_PROFILES="alpha:${dotdot_home}/profiles/../escape"
EOF
expect_failure_without_changes dotdot-root "${dotdot_case}" "${dotdot_home}" \
  "${INSTALLER}" --config "${dotdot_config}"

home_link_case="${TEMP_ROOT}/cases/root-links-home"
home_link_home="${home_link_case}/home"
home_link_config="${home_link_home}/.config/backup/config"
install -d -m 0700 "$(dirname -- "${home_link_config}")"
ln -s -- "${home_link_home}" "${home_link_home}/profile-root"
cat > "${home_link_config}" <<EOF
CLAUDE_PROFILES="alpha:${home_link_home}/profile-root"
EOF
expect_failure_without_changes root-links-home "${home_link_case}" "${home_link_home}" \
  "${INSTALLER}" --config "${home_link_config}"

config_escape_case="${TEMP_ROOT}/cases/config-symlink-escape"
config_escape_home="${config_escape_case}/home"
config_escape_config="${config_escape_home}/.config/backup/config"
config_escape_root="${config_escape_home}/profiles/opencode-alpha"
config_escape_outside="${config_escape_case}/outside"
install -d -m 0700 \
  "$(dirname -- "${config_escape_config}")" \
  "${config_escape_root}" \
  "${config_escape_outside}"
ln -s -- "${config_escape_outside}" "${config_escape_root}/config"
cat > "${config_escape_config}" <<EOF
OPENCODE_PROFILES="alpha:${config_escape_root}"
EOF
expect_failure_without_changes config-symlink-escape \
  "${config_escape_case}" "${config_escape_home}" \
  "${INSTALLER}" --config "${config_escape_config}"

same_root_case="${TEMP_ROOT}/cases/same-resolved-root"
same_root_home="${same_root_case}/home"
same_root_config="${same_root_home}/.config/backup/config"
same_root_real="${same_root_home}/profiles/opencode-shared"
same_root_alias="${same_root_home}/profiles/opencode-alias"
install -d -m 0700 "$(dirname -- "${same_root_config}")" "${same_root_real}"
ln -s -- "${same_root_real}" "${same_root_alias}"
cat > "${same_root_config}" <<EOF
OPENCODE_PROFILES="alpha:${same_root_real} beta:${same_root_alias}"
EOF
expect_failure_without_changes same-resolved-root \
  "${same_root_case}" "${same_root_home}" \
  "${INSTALLER}" --config "${same_root_config}"

native_opencode_case="${TEMP_ROOT}/cases/native-opencode-overlap"
native_opencode_home="${native_opencode_case}/home"
native_opencode_config="${native_opencode_home}/.config/backup/config"
install -d -m 0700 "$(dirname -- "${native_opencode_config}")"
cat > "${native_opencode_config}" <<EOF
OPENCODE_PROFILES="alpha:${native_opencode_home}/.local"
EOF
expect_failure_without_changes native-opencode-installer \
  "${native_opencode_case}" "${native_opencode_home}" \
  "${INSTALLER}" --config "${native_opencode_config}"
expect_failure_without_changes native-opencode-doctor \
  "${native_opencode_case}" "${native_opencode_home}" \
  "${DOCTOR}" --config "${native_opencode_config}"

cross_tool_case="${TEMP_ROOT}/cases/cross-tool-native-root"
cross_tool_home="${cross_tool_case}/home"
cross_tool_config="${cross_tool_home}/.config/backup/config"
install -d -m 0700 "$(dirname -- "${cross_tool_config}")"
cat > "${cross_tool_config}" <<EOF
CLAUDE_PROFILES="alpha:${cross_tool_home}/.codex"
EOF
expect_failure_without_changes cross-tool-native-root \
  "${cross_tool_case}" "${cross_tool_home}" \
  "${INSTALLER}" --config "${cross_tool_config}"

shared_native_case="${TEMP_ROOT}/cases/shared-native-root"
shared_native_home="${shared_native_case}/home"
shared_native_config="${shared_native_home}/.config/backup/config"
install -d -m 0700 "$(dirname -- "${shared_native_config}")"
cat > "${shared_native_config}" <<EOF
CLAUDE_HOME="${shared_native_home}/shared-state"
CODEX_HOME="${shared_native_home}/shared-state"
EOF
expect_failure_without_changes shared-native-root \
  "${shared_native_case}" "${shared_native_home}" \
  "${INSTALLER}" --config "${shared_native_config}"

normalized_native_case="${TEMP_ROOT}/cases/normalized-native-root"
normalized_native_home="${normalized_native_case}/home"
normalized_native_config="${normalized_native_home}/.config/backup/config"
install -d -m 0700 "$(dirname -- "${normalized_native_config}")"
cat > "${normalized_native_config}" <<EOF
CODEX_HOME="/synthetic/.."
EOF
expect_failure_without_changes normalized-native-root \
  "${normalized_native_case}" "${normalized_native_home}" \
  "${INSTALLER}" --config "${normalized_native_config}"

home_native_case="${TEMP_ROOT}/cases/home-native-root"
home_native_home="${home_native_case}/home"
home_native_config="${home_native_home}/.config/backup/config"
install -d -m 0700 "$(dirname -- "${home_native_config}")"
cat > "${home_native_config}" <<EOF
DSH_HOME="${home_native_home}"
EOF
expect_failure_without_changes home-native-root \
  "${home_native_case}" "${home_native_home}" \
  "${INSTALLER}" --config "${home_native_config}"

non_executable_case="${TEMP_ROOT}/cases/non-executable-backup"
non_executable_home="${non_executable_case}/home"
non_executable_config="${non_executable_home}/.config/backup/config"
install -d -m 0700 "$(dirname -- "${non_executable_config}")"
printf 'MACHINE_ID="fixture-profile-host"\n' > "${non_executable_config}"
chmod -x "${PRIMARY_CHECKOUT}/backup.sh"
expect_failure_without_changes non-executable-backup \
  "${non_executable_case}" "${non_executable_home}" \
  "${INSTALLER}" --config "${non_executable_config}"
chmod +x "${PRIMARY_CHECKOUT}/backup.sh"

dirty_skill_checkout="${TEMP_ROOT}/dirty-skill-primary"
copy_primary_checkout "${dirty_skill_checkout}"
printf '\n# synthetic tracked change\n' >> \
  "${dirty_skill_checkout}/skills/agent-harness-profiles/SKILL.md"
dirty_skill_case="${TEMP_ROOT}/cases/dirty-skill-checkout"
dirty_skill_home="${dirty_skill_case}/home"
dirty_skill_config="${dirty_skill_home}/.config/backup/config"
install -d -m 0700 "$(dirname -- "${dirty_skill_config}")"
printf 'MACHINE_ID="fixture-profile-host"\n' > "${dirty_skill_config}"
expect_failure_without_changes dirty-skill-checkout \
  "${dirty_skill_case}" "${dirty_skill_home}" \
  "${dirty_skill_checkout}/skills/agent-harness-profiles/scripts/install.sh" \
  --config "${dirty_skill_config}"

untracked_skill_checkout="${TEMP_ROOT}/untracked-skill-primary"
copy_primary_checkout "${untracked_skill_checkout}"
printf '#!/usr/bin/env bash\nexit 0\n' > \
  "${untracked_skill_checkout}/skills/agent-harness-profiles/scripts/synthetic-untracked.sh"
untracked_skill_case="${TEMP_ROOT}/cases/untracked-skill-checkout"
untracked_skill_home="${untracked_skill_case}/home"
untracked_skill_config="${untracked_skill_home}/.config/backup/config"
install -d -m 0700 "$(dirname -- "${untracked_skill_config}")"
printf 'MACHINE_ID="fixture-profile-host"\n' > "${untracked_skill_config}"
expect_failure_without_changes untracked-skill-checkout \
  "${untracked_skill_case}" "${untracked_skill_home}" \
  "${untracked_skill_checkout}/skills/agent-harness-profiles/scripts/install.sh" \
  --config "${untracked_skill_config}"

# With no configured external command, an existing local backup is untouched.
no_backup_home="${TEMP_ROOT}/cases/no-backup-command/home"
no_backup_config="${no_backup_home}/.config/backup/config"
install -d -m 0700 "$(dirname -- "${no_backup_config}")" "${no_backup_home}/bin"
printf 'MACHINE_ID="fixture-profile-host"\n' > "${no_backup_config}"
printf 'UNMANAGED_BACKUP_CANARY\n' > "${no_backup_home}/bin/backup"
env -u BACKUP_COMMAND HOME="${no_backup_home}" "${INSTALLER}" --config "${no_backup_config}" >/dev/null
[[ "$(cat "${no_backup_home}/bin/backup")" == UNMANAGED_BACKUP_CANARY ]] ||
  fail 'unconfigured external backup was modified'

printf 'All Agent Harness Profiles tests passed.\n'
