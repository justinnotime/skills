# Multiple Source Roots

`backup.sh` can back up the native default state directory for each supported
tool and any number of additional state roots. Labels are opaque destination
names; the backup system does not create accounts, launch applications, or
assign meaning to a label.

This filename is retained for compatibility with existing links.

## Default behavior

With no per-machine config, the script discovers the tools' native locations:

| Tool | Default source |
|------|----------------|
| Claude Code | `~/.claude` |
| Codex | `~/.codex` |
| opencode | XDG `data/config/state` directories |
| DeepSeek Harness | `~/.dsh` |
| OpenClaw | `~/.openclaw` |
| Cursor | `~/.cursor` plus its platform user-config directory |

Missing sources are logged and skipped. A machine that uses only native
defaults needs no multi-source configuration.

## Source layouts

Additional roots must use the layout produced by the upstream tool:

| Tool | Root contract |
|------|---------------|
| Claude Code | directory selected by `CLAUDE_CONFIG_DIR` |
| Codex | directory selected by `CODEX_HOME` |
| opencode | a root containing `share/opencode`, `config/opencode`, and `state/opencode` |
| DeepSeek Harness | directory selected by `DSH_HOME` |

The state-backup command begins once a source directory exists and never creates
accounts, shell launchers, application roots, services, or Skill links. The
separate `agent-harness-profiles` Skill can explicitly create launchers, empty
profile roots, and its own shared/Claude Skill links from these same configured
values; merely running `backup.sh` never invokes that installer. Root selection
does not establish account identity, full Skill discovery, hooks, or extraction
access. Use `agent-harness-integration` for full onboarding with separately
approved backup coverage; neither Skill automatically adds sources or credentials.

## Per-machine configuration

Configuration lives at `~/.config/backup/config` and is sourced as shell data.
Existing variable names and formats are stable compatibility interfaces.

Claude Code, Codex, and opencode use space-separated `label:path` entries:

```bash
CLAUDE_PROFILES="alternate:$HOME/.claude-alt lab:$HOME/.claude-lab"
CODEX_PROFILES="alternate:$HOME/.codex-alt"
OPENCODE_PROFILES="lab:$HOME/.opencode-lab"
```

These three legacy list formats do not support whitespace inside a path.
Labels should contain lowercase letters, digits, underscores, or hyphens.

DeepSeek Harness uses newline-separated entries, so its paths may contain
spaces:

```bash
DSH_PROFILES="alternate:$HOME/.dsh-alt
lab:$HOME/.dsh-lab"
```

DSH labels must begin with a lowercase letter or digit and may then contain
lowercase letters, digits, underscores, or hyphens. Paths must be absolute.
The source directory name does not need to match its label.

## DeepSeek Harness default compatibility

`DSH_INCLUDE_DEFAULT` controls the native `DSH_HOME` source:

| Value | Behavior |
|-------|----------|
| `auto` (default) | Back up `DSH_HOME` only when `DSH_PROFILES` is empty |
| `true`, `yes`, or `1` | Back up `DSH_HOME` in addition to configured roots |
| `false`, `no`, or `0` | Do not back up `DSH_HOME` |

The `auto` rule preserves older multi-root deployments: a machine that already
sets `DSH_PROFILES` continues to back up only those explicit entries after an
upgrade. A new single-root installation gets the upstream `~/.dsh` default
without extra configuration.

Override the source or destination when necessary:

```bash
DSH_HOME="/absolute/path/to/dsh-state"
DSH_BACKUP_DIR="$BACKUP_ROOT/dsh"
DSH_BACKUP_PREFIX="$BACKUP_ROOT/dsh"
```

## Destination layout

Default sources keep the unsuffixed destination. Additional labels append a
suffix:

```text
backup/<machine>/
  claude/
  claude-alternate/
  codex/
  codex-alternate/
  opencode/
  opencode-lab/
  dsh/
  dsh-alternate/
```

All of these directories remain inside the same configured backup tree. A
suffix is organization, not an access-control boundary.

## Credential handling

Known credential stores are intentionally excluded:

- Claude Code credential files;
- Codex `auth.json`;
- opencode `auth.json` and `mcp-auth.json`;
- DSH `.credentials.yaml*`, `.oauth/`, and `.env*`;
- generated dependency trees and telemetry identity files.

Re-authenticate after restoring onto another host. Secrets embedded in custom
filenames, settings, or transcripts cannot be detected reliably by a generic
backup tool and remain the operator's responsibility.

## Consistency and safety

- Discovered opencode SQLite databases require `sqlite3 .backup`. Missing sqlite3,
  snapshot failures, and replacement failures mark that profile incomplete. The
  script continues other databases, profiles, and harnesses, then exits nonzero.
  It never falls back to copying a live database or its WAL companions.
- Each snapshot is created under a unique temporary name in its destination
  directory and atomically renamed only after `.backup` succeeds. Failure keeps
  the previous good snapshot; exit and handled signals clean up that invocation's
  temporary files. SIGKILL or power loss may leave an unpublished temporary file.
- Overlapping runs do not share temporary names or delete each other's files.
  The last successful rename wins; snapshots are not ordered by source freshness.
  The rest of the backup is not transactional. Serialize jobs if ordering is
  required, and restore from a separate copy rather than opening a destination
  database while backup runs.
- Old WAL/SHM files from earlier raw-copy backups are not migrated or deleted.
  Restore new standalone `.backup` snapshots without those legacy companions.
- DSH rejects unsafe labels, non-absolute additional roots, source/destination
  nesting, and symlinked destinations.
- The backup is incremental and does not delete old destination files.
- A source that does not exist is skipped, so roots may be declared before an
  application first creates them.

## Upgrade contract

The following interfaces remain supported:

- executable entrypoint: `backup.sh`;
- common symlink entrypoint: `~/bin/backup`;
- config path: `~/.config/backup/config`;
- `CLAUDE_PROFILES`, `CODEX_PROFILES`, `OPENCODE_PROFILES`, and
  `DSH_PROFILES` formats;
- suffixed destinations produced from existing labels.

Run `~/bin/backup` after an upgrade and inspect the normal log before changing
any local configuration.
