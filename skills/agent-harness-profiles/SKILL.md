---
name: agent-harness-profiles
description: Configure or inspect named root-selection launchers for Claude Code, Codex, OpenCode, and DeepSeek Harness. Use for caller-configured roots and executable selection; full onboarding, accounts, discovery, hooks, and backup authorization belong to agent-harness-integration.
---

# Agent Harness Profiles

This Skill is generic. Treat labels as opaque and never infer account type,
trust, ownership, or machine identity from a label or path. Real names, meanings,
roots, ports, repository choices, and schedules belong in caller-owned
configuration.

The stable configuration interface is `~/.config/backup/config` and its existing
`CLAUDE_PROFILES`, `CODEX_PROFILES`, `OPENCODE_PROFILES`, and `DSH_PROFILES`
variables. A private repository may own that file or source a tracked private
fragment from it. Claude Code, Codex, and OpenCode lists use space-separated
`label:/absolute/root` entries. DSH uses newline-separated entries and permits
spaces inside the path. Labels begin with a lowercase letter or digit and use
lowercase letters, digits, underscores, or hyphens. Labels have no implied
account meaning.
The configuration is trusted local shell code; path checks prevent accidental
misconfiguration, not hostile commands in that file.
The scripts require Bash 4+, Git, rsync, and a `realpath` implementation with
GNU-compatible `-m` and `-s` options.

## Install or update

Inspect the current config, generated launchers, links, and shell functions.
Then run:

```bash
scripts/install.sh --config "$HOME/.config/backup/config"
```

The installer:

1. validates the checkout, launcher target, every configured root, and every
   planned link before writing;
2. renders `~/.config/agent-harness-profiles/launchers.sh`;
3. prepares only `share/opencode`, `state/opencode`, and `config/opencode`
   inside each configured OpenCode root, without copying native configuration
   or credentials;
4. links this canonical Skill into the shared and configured Claude discovery
   roots;
5. when `BACKUP_COMMAND` names an absolute executable in the configuration,
   links `~/bin/backup` to that command; otherwise leaves that entry unchanged;
6. runs the doctor.

It does not source launchers, edit shell startup files, install services or
schedulers, start a process, move state roots, or copy authentication data.
Review the generated launcher file before sourcing it in Bash. Plain upstream
commands are unchanged and continue to honor their inherited environment.

## Executable selection

For each configured harness, the default executable name is `claude`, `codex`,
`opencode`, or `dsh` on PATH. If that name is unavailable or not the intended
executable, explicitly configure one absolute executable path per harness:

```bash
CLAUDE_COMMAND="/absolute/path/to/claude"
CODEX_COMMAND="/absolute/path/to/codex"
OPENCODE_COMMAND="/absolute/path/to/opencode"
DSH_COMMAND="/absolute/path/to/deepseek-harness"
```

These optional values accept neither command strings nor argument lists. They
apply only to configured profiles, not plain commands, and do not select accounts.
The installer does not create or replace executable wrappers. A caller-selected
existing wrapper must itself honor the supplied root variables; checking that a
file is executable does not verify its behavior or authentication.

Rendering, installer preflight, and the doctor reject unavailable executables and
launcher names already visible to their process. Sourcing the generated file
checks again in the calling shell, including its unexported functions and aliases,
before defining any launchers. For example, an existing `dsh-work` executable,
alias, or function is preserved and activation is refused. Choose another opaque
label or deliberately resolve the conflict yourself; a command override does not
bypass name conflicts. Sourcing twice also refuses the existing functions: use a
fresh shell for a regenerated file rather than silently replacing active functions.

Launchers override only `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `DSH_HOME`, or OpenCode's
three XDG roots for the child command. All other inherited environment, arguments,
working directory, and the command's exit status are preserved. In particular,
inherited authentication and explicit OpenCode config overrides are not cleared.

## Authority boundaries

Root selection is not account selection, a guarantee of isolated authentication,
full Skill discovery, hook setup, or authorization to back up or extract sessions.
This installer links only this Skill into shared and Claude roots; it does not
populate Codex, OpenCode, or DeepSeek Harness discovery roots with other Skills.
Use `agent-harness-integration` for full onboarding and verification of those
separate choices. This delegation is by Skill name, not a sibling code import.

The shared legacy `*_PROFILES` values also declare sources to an independently
run backup command. Review that coverage explicitly before changing those values;
creating or selecting a launcher does not itself run or expand a backup. Linking
`BACKUP_COMMAND` does not authorize schedules, new coverage, or credential copying.

For diagnosis without mutation, run `scripts/doctor.sh --config FILE`. Use
`scripts/render-launchers.sh --config FILE` to inspect generated text on stdout.

## Safety

- Refuse unmanaged output files and divergent links.
- Refuse roots that use dot components, resolve to native roots, overlap one
  another, or redirect managed children outside their configured root.
- Never derive a machine label from host or hardware state; require an opaque
  value in local configuration.
- Profile isolation prevents accidental cross-use but is not a same-user
  security boundary.
- Keep service ports and lifecycles in their owner configuration.
- Install only from the primary `main` checkout. Linked worktrees, detached
  checkouts, topic branches, and uncommitted files in this Skill package are
  rejected before any persistent write. Git cannot identify which independent
  clone an operator considers long-lived, so invoke the installer only from the
  selected durable checkout.

The package has no dependency on a neighboring backup implementation. To
manage the optional command link, set `BACKUP_COMMAND=/absolute/path/to/backup.sh`
in the local configuration. The installer checks the command's existence and
executable permission without reading its source or managing its checkout.

After changing this Skill, run `bash tests/run.sh` from this directory and the
Skill Creator validator against this directory. Tests use a synthetic external
command and require no sibling Skill packages.
