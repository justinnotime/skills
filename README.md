# Shared Agent Harness Skills

`skills` is a collection of independent, deterministic agent-harness Skills.
It contains state backup, Syncthing diagnostics, a remote clipboard helper, a
shared session-extraction runtime, and a configurable GitHub archive. It also
provides generic setup helpers for named harness roots; callers retain all
label meaning in their own configuration.

The repository works in both shapes:

- native defaults only, with no named-root configuration;
- any number of additional labeled roots for supported harnesses.

Labels are opaque. This repository does not assign account, organization, or
trust semantics to them.

For moving an existing implementation into an independent Skill, see the
[runtime migration guide](docs/runtime-migration.md). It covers private
configuration, duplicate-code removal, installation and verification on each
machine.

## Repository name transition

The GitHub repository was renamed from `backup` to `skills`, and the examples
below use `$HOME/src/skills` as the local checkout. A clone that still lives in
a directory named `backup` keeps working: `backup.sh`, `~/bin/backup`,
`~/.config/backup/config`, and backup destination names remain stable
interfaces. To move such a clone, repoint its remote, rename the directory,
refresh the shell link, and update any scheduler entry that names the old path:

```bash
git -C "$HOME/src/backup" remote set-url origin https://github.com/justinnotime/skills.git
mv "$HOME/src/backup" "$HOME/src/skills"
git -C "$HOME/src/skills" worktree repair
ln -sfn "$HOME/src/skills/backup.sh" "$HOME/bin/backup"
```

[GitHub redirects the old repository URL](https://docs.github.com/en/repositories/creating-and-managing-repositories/renaming-a-repository),
including clone, fetch, and push. Its documentation states no fixed transition
period. Do not create another repository under the former `backup` name:
reusing that name removes the redirect.

## Repository map

| Path | Purpose |
|---|---|
| `skills/` | Installable deterministic behavior packages |
| `plugins/codex-cost/` | Codex hook plugin for per-turn cost, conversation totals and cache statistics |
| `skills/state-backup/` | State-backup Skill and canonical backup script |
| `backup.sh` | Stable compatibility link retained for existing jobs and links |
| `skills/syncthing-doctor/` | Syncthing diagnostic Skill and canonical doctor script |
| `PROFILES.md` | Backup source-root, destination, exclusion, and upgrade contract |
| `syncthing-doctor.sh` | Stable compatibility link for Syncthing diagnostics |
| `skills/remote-clipboard/` | Remote-to-local clipboard Skill and shell function |
| `clip.sh` | Stable compatibility link for the clipboard shell helper |
| `skills/agent-harness-profiles/` | Configuration-driven launcher and Skill-link setup |
| `skills/agent-session-extraction/` | Manifest-driven extraction Skill and command wrappers |
| `skills/github-archive/` | Configured GitHub archives with local issue dependency graphs, timelines and inventories |
| `skills/teams-archive/` | Caller-configured Teams chat, card, and attachment archive |
| `skills/whatsapp-archive/` | Receive-only WhatsApp linked-device spool and selected monthly archives |
| `skills/slack-archive/` | Caller-configured Slack conversation archive, including late thread replies |
| `skills/matrix-bridge/` | Text and file transfer through a privately configured Matrix room |
| `skills/google-chat-archive/` | Caller-configured Google Chat space and direct-message archive |
| `skills/happy-tmux-title/` | Exact tmux window lookup for Happy conversation titles |
| `skills/cross-ref-lint/` | Local Markdown link validation with caller-owned exclusions |
| `skills/draft-human-reply/` | Natural reply drafting with optional private writing preferences |
| `skills/pr-status-table/` | Current pull request reporting with an optional private format |
| `skills/realign/` | Evidence-based comparison of recent work with the user's objective |
| `skills/secret-lint/` | Credential scanner, controlled text redaction and audit instructions |
| `skills/privacy-lint/` | Publication privacy review with optional private identifier patterns |
| `skills/teams-send/` | Authorized Teams chat sending through caller-owned credentials or an external connector |
| `skills/chat-mentions/` | Read-only Teams attention collection and a local reply-draft box |
| `skills/agent-bus/` | Messaging workflow for the public fleet runtime and caller-owned identity policy |
| `skills/fleet-orchestrator/` | Standalone ORC engine, durable local/Matrix transport, terminal helpers, and configurable private policy |
| `skills/genteam/` | Configurable channel and thread archives, cookie setup and authorized message sending |
| `skills/repository-publish/` | Git worktree preparation, transactional publication, durable state, and verified LFS delivery |
| `skills/google-docs-authority/` | Complete document mirroring, authorization, publication, page rendering and authority/export comparison |
| `skills/genspark-archive/` | Configured Outlook email, calendar and meeting archives through the Genspark CLI |
| `skills/prompt-translation/` | Incremental Chinese-to-English prompt learning, provenance validation and scheduled publication |
| `skills/document-facts/` | Structured document facts, digests, timelines and thematic synthesis from configured sources |
| `skills/activity-summary/` | Configured daily and weekly summaries with deterministic evidence, validation and scheduled publication |
| `skills/markdown-issues/` | Configured local Markdown issue creation, history validation, due reviews and watched-path signals |
| `skills/runtime-install/` | Configured Skill discovery links and managed cron installation with preview and preservation of unrelated entries |
| `skills/runtime-layout/` | Configured runtime path resolution and explicit local layout migration plans |
| `skills/workspace-brief/` | Read-only workspace briefings from selected local records, checks and configured commands |
| `skills/structure-lint/` | Runnable repository structure checks with caller-owned metadata, layout and reference rules |
| `skills/chat-draft/` | Targeted chat reading and reply drafting through configured readers |
| `skills/agent-session-extraction/src/agent_skills/sessions/` | Package-owned normalized-session runtime |
| `skills/<name>/tests/` | Tests owned and runnable by that Skill |

The three root shell paths are relative symbolic links into their Skill
packages. This keeps existing scheduler and shell configuration working while
making each implementation part of its behavior package.

## Codex cost plugin

[Codex Cost](plugins/codex-cost/README.md) displays API cost estimates and cache
statistics after each Codex turn. It uses local usage records and makes no model
or network calls. Install it with:

```bash
codex plugin marketplace add justinnotime/skills
codex plugin add codex-cost@skills
```

The plugin needs Python 3.11 or newer. Its source, pricing snapshot and synthetic
tests are contained in `plugins/codex-cost/`. Reproduce its checks with:

```bash
(cd plugins/codex-cost && python3 -B -m unittest discover -s tests -v)
```

## Backup layout

```text
<syncthing-root>/backup/<machine>/
  openclaw/
  claude/
  claude-<label>/
  codex/
  codex-<label>/
  opencode/
  opencode-<label>/
  dsh/
  dsh-<label>/
  cursor/
```

Default roots keep an unsuffixed destination. Additional roots append their
configured label. All destinations are in the same Syncthing tree; suffixes do
not create an access-control boundary.

## Quick start: native defaults

```bash
git clone https://github.com/justinnotime/skills.git "$HOME/src/skills"
mkdir -p "$HOME/bin"
ln -s "$HOME/src/skills/backup.sh" "$HOME/bin/backup"
"$HOME/bin/backup"
```

Without a config file, the script uses the upstream default state locations,
`$HOME/syncthing`, the current hostname as its machine directory, and
`$HOME/.local/log/backup.log`.

## Quick start: additional roots

```bash
mkdir -p "$HOME/.config/backup"
${EDITOR:-vi} "$HOME/.config/backup/config"
"$HOME/src/skills/backup.sh"
```

See [PROFILES.md](PROFILES.md) for the exact config, source-root, exclusion,
destination, and compatibility contract. Machine-specific launchers, account
policy, service definitions, and orchestration belong outside this module.

## Supported state

- **OpenClaw:** sessions, memory, selected workspace documents, and config.
- **Claude Code:** projects, history, and settings from the default and labeled
  `CLAUDE_CONFIG_DIR` roots.
- **Codex:** sessions, history, and config from the default and labeled
  `CODEX_HOME` roots.
- **OpenCode:** SQLite or legacy session data, config, and prompt history from
  default and labeled XDG roots.
- **DeepSeek Harness:** sessions, attachments, storages, settings, skills, and
  profile manifests from default and labeled `DSH_HOME` roots.
- **Cursor:** project agent transcripts and user settings.

Known authentication files, OAuth state, environment files, generated
dependencies, and selected telemetry identifiers are excluded. Custom secrets
stored under arbitrary names cannot be detected reliably; review local config
before synchronizing a new source.

OpenCode databases use `sqlite3 .backup` when available. Without `sqlite3`, the
script falls back to copying the database and WAL companions with a warning.

## Syncthing

Configure one Syncthing folder for `<syncthing-root>/backup`. A source machine
that should upload only its own directory can place this in the folder root's
`.stignore`:

```text
!<machine>
!<machine>/**
*
```

A receiver intended to hold every machine directory should leave that ignore
file absent or empty.

Run diagnostics with:

```bash
./syncthing-doctor.sh
```

## Scheduling

Existing and new scheduler entries should use `backup.sh` or `~/bin/backup`.
Both are compatibility interfaces:

```cron
*/30 * * * * /absolute/path/to/backup/backup.sh >> /absolute/path/to/backup-cron.log 2>&1
```

The scheduler calls a deterministic script. It does not invoke an interactive
agent.

## Upgrade safety

The project retains existing configuration variable names, list formats,
destination suffixes, `backup.sh`, `~/bin/backup`, and `PROFILES.md`. Backup
validation rejects unsafe DSH sources, destinations, labels, and links.

Before deploying an update:

```bash
bash skills/state-backup/tests/run.sh
python3 -B -m unittest discover -s skills/state-backup/tests -v
```

Then run the existing backup command manually and inspect its log.

Each Skill contains its own source, tests, and dependency declarations and can
be copied without sibling packages. There is no repository-wide Python project
or shared source directory. Run additional checks from the affected package:

```bash
(cd skills/agent-harness-profiles && bash tests/run.sh)
(cd skills/agent-session-extraction && uv sync --locked --extra test && uv run --no-sync pytest tests)
(cd skills/github-archive && uv run --locked pytest tests)
(cd skills/teams-archive && uv run --locked pytest tests)
(cd skills/matrix-bridge && uv run --locked pytest tests)
(cd skills/google-chat-archive && uv run --locked pytest tests)
(cd skills/happy-tmux-title && uv run --locked pytest tests) # requires tmux
(cd skills/cross-ref-lint && uv run --locked pytest tests)
(cd skills/draft-human-reply && uv run --locked skills-ref validate "$PWD") # format check, no runtime code
(cd skills/pr-status-table && uv run --locked skills-ref validate "$PWD")
(cd skills/realign && uv run --locked skills-ref validate "$PWD")
(cd skills/secret-lint && uv run --locked pytest tests)
(cd skills/privacy-lint && uv run --locked skills-ref validate "$PWD")
(cd skills/teams-send && uv run --locked pytest tests)
(cd skills/chat-mentions && uv run --locked pytest tests)
(cd skills/agent-bus && uv run --locked skills-ref validate "$PWD")
(cd skills/fleet-orchestrator && uv run --locked pytest tests -q)
(cd skills/repository-publish && uv run --locked pytest tests -q)
(cd skills/genteam && uv run --locked pytest tests)
(cd skills/google-docs-authority && uv run --locked pytest tests)
(cd skills/genspark-archive && uv run --locked pytest tests)
(cd skills/prompt-translation && uv run --locked pytest tests)
(cd skills/document-facts && uv run --locked pytest tests)
(cd skills/activity-summary && uv run --locked pytest tests)
(cd skills/markdown-issues && uv run --locked pytest tests)
(cd skills/runtime-install && uv run --locked pytest tests)
(cd skills/runtime-layout && uv run --locked pytest tests)
(cd skills/workspace-brief && uv run --locked pytest tests)
(cd skills/structure-lint && uv run --locked pytest tests)
(cd skills/chat-draft && uv run --locked skills-ref validate "$PWD")
(cd skills/slack-archive && uv run --locked pytest tests)
(cd skills/whatsapp-archive && uv run --locked pytest tests && npm ci --prefix bridge && npm test --prefix bridge)
```

Consumers of the session Python API configure the runtime root as
`/absolute/path/to/skills/agent-session-extraction`; its import directory is
`<runtime-root>/src`. The package's command wrappers resolve this themselves.
The profiles installer accepts an optional `BACKUP_COMMAND` in local
configuration instead of discovering another Skill's source files.

Validate every new or changed Skill with the Skill Creator validator before
publishing it. Dependency installation and validation artifacts belong in the
individual package or a disposable workspace, never a root shared environment.

## Continuous integration

[Skill CI](.github/workflows/skills-ci.yml) runs on pushes, pull requests, and
manual requests. Each tested package is copied to an isolated directory and
uses only its own source, tests, and declared Python dependencies. The workflow
does not need private configuration, accounts, archive data, or credentials.

Python packages run on their minimum supported version and Python 3.14. The
session job installs `git-crypt`, and Python 3.14 also exercises standard-library
Zstandard support. Backup and profile installation run their package-owned
tests with synthetic temporary homes and external commands. Clipboard and
Syncthing helpers currently receive shell syntax checks only; their real
desktop/network integrations are not exercised by CI.

Use the package commands above to reproduce checks. GitHub's Actions and pull
request pages show the actual run results. With working CI, pull requests may
be used for future changes; bootstrapping CI does not require a pull request.

## GitHub archive

The `github-archive` Skill contains its own Python package, dependencies, and
tests. Select repositories and archive paths in a private YAML file outside
this repository, following [its configuration reference](skills/github-archive/references/config.md).
Authenticate `gh` separately, then run:

```bash
uv sync --project skills/github-archive --locked
skills/github-archive/scripts/sync --config /path/to/private/config.yaml --dry-run
skills/github-archive/scripts/sync --config /path/to/private/config.yaml
```

The command uses read-only GitHub requests. Callers own configuration, archive,
state, scheduling, and publication. No other Skill is required.

## Clipboard helper

`clip.sh` defines a `clip` shell function that uses `wl-copy` locally and OSC 52
over SSH, mosh, or tmux. Install it separately if needed:

```bash
ln -s "$HOME/src/skills/clip.sh" "$HOME/.clip.sh"
```

Source `$HOME/.clip.sh` from the applicable shell startup file. tmux 3.3 or
newer should use `set -g allow-passthrough all`.

## License

MIT
