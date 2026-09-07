# Command contract

Install with `uv sync --locked` from this package, or install its Python project.
The shell entry uses `REPOSITORY_PUBLISH_PYTHON` or `python3`. No Python runtime
dependencies or sibling Skill files are required.

```bash
scripts/publish --repo /private/repository --task messages \
  --paths 'archive/messages' --subject 'sync: messages' \
  --state-dir /private/state/messages --lock /private/locks/messages.lock \
  --scratch /private/cache/transactions \
  --publish-lock /private/locks/repository.lock \
  -- /path/to/installed-reader --config /private/reader.json
```

The writer runs inside a linked worktree of the selected repository. Its
environment contains `REPOSITORY_PUBLISH_WORKTREE`, `REPOSITORY_PUBLISH_STATE`,
`REPOSITORY_PUBLISH_REPOSITORY`, `REPOSITORY_PUBLISH_SUBJECT` and
`REPOSITORY_PUBLISH_AGENT`. `SYNC_STATE_DIR` also selects its staging directory.
Use `--worktree-env NAME` and `--state-env NAME` when adapting a writer that
already uses different variable names. The original environment is inherited;
these declared outputs override their values for the writer and policy commands.

| Option | Behavior |
|---|---|
| `--repo` | Existing checkout; defaults to the current directory |
| `--task` | Lock/state identity, using letters, digits, dots, underscores or hyphens |
| `--paths` | Space-separated literal relative paths owned by this writer |
| `--subject` | Default commit message; also supplied to message policy |
| `--agent` | Optional identity supplied to private message policy |
| `--state-dir` | Durable progress outside the checkout; default under XDG state, keyed by task and repository |
| `--lock` | Nonblocking task lock; default under XDG cache, keyed by task and repository |
| `--scratch` | Temporary worktree parent outside the checkout; defaults under XDG cache |
| `--publish-lock` | Shared lock for publishers; defaults under XDG cache |
| `--sparse` | Optional space-separated cone directories; a validation command requires a full checkout |
| `--remote`, `--branch` | Publication destination, default `origin`, `main` |
| `--attempts`, `--retry-delay` | Push attempts and increasing retry delay, defaults 5 and 2 seconds |
| `--lock-timeout` | Publication lock timeout, default 300 seconds |

Owned path entries cannot contain whitespace; filenames beneath them can.
Ownership uses exact path components, not prefix regular expressions or Git
pathspec patterns. Rename sources and destinations are both checked. Symlinks
and special files are not supported in progress state. State file replacement
is atomic per file; obsolete state files are retained, matching readers that
maintain independent progress files. Only after publication succeeds can any
durable progress file be replaced. An interrupted replacement is safe to retry
because its content was already published.

On a rejected push the temporary transaction is removed and the next run starts
from unchanged durable progress. A sibling `STATE_DIRECTORY.publish-pending.json`
records a candidate revision before push. On restart a revision already present
on the destination branch must pass LFS verification before that record clears.
An unpublished candidate is regenerated from the previous progress. Keep this
record with the private state when moving an installation.

## Private validation and commit-message commands

```bash
--validate-command '["/path/to/policy", "validate", "{worktree}"]' \
--message-command '["/path/to/policy", "message", "{worktree}"]'
```

Commands are JSON argument arrays. `{worktree}`, `{repository}` and `{state}`
are replaced inside each argument, without shell evaluation. A message command
writes the complete commit message to stdout; empty output or any nonzero exit
aborts publication. Keep its diagnostics on stderr. Validation must return zero
and must not modify tracked content. It runs before committing and after every
rebase. When rules change upstream, the message command runs again before push.
Private commands own repository-specific policy, not the generic Git engine.

## Existing worktrees and LFS verification

The same entry prepares dedicated worktrees for callers whose generation runs
span more than one invocation:

```bash
scripts/publish worktree prepare --repo /private/repository \
  --worktree /private/task-worktree --task-branch task/messages
scripts/publish worktree ahead --repo /private/task-worktree
scripts/publish worktree changed --repo /private/task-worktree
scripts/publish worktree committed --repo /private/task-worktree
scripts/publish worktree fetch --repo /private/task-worktree
scripts/publish worktree reset --repo /private/task-worktree --task-branch task/messages
```

`prepare` fetches the selected upstream and reuses the named linked worktree.
It preserves dirty files and unpublished commits, including reattaching an
unpublished branch after its checkout directory was removed. It refuses an
existing unregistered destination, another repository, a different branch, or
a destination inside the source checkout. It does not reset a branch already
attached elsewhere. The caller owns task locking across its complete generation
cycle and decides which destination directories are permitted.

`reset` requires a clean linked worktree on the expected branch with no
unpublished commits. It updates from the already-fetched upstream. `fetch`,
`prepare`, `ahead`, `committed` and `reset` accept `--remote` and `--branch`,
defaulting to `origin` and `main`. Path listings include both sides of renames;
use `--null` for NUL-delimited paths, including filenames containing newlines.
Without it, such names fail before any partial path list is printed.

A completed generated candidate can be committed without copying Git staging
logic into private policy:

```bash
scripts/publish worktree commit --repo /private/task-worktree \
  --source-repository /private/repository --task-branch task/messages \
  --paths archive/messages \
  --validate-command '["/private/policy", "validate", "{worktree}"]' \
  --message-command '["/private/policy", "message", "{worktree}"]'
```

`commit` requires a linked worktree belonging to the explicitly selected source
repository, the expected task branch distinct from the publication branch, and
literal owned paths. Every changed path, including untracked files and both
sides of a rename, must be owned. It stages the exact observed paths, runs the
read-only validator and message command, then creates the commit. Validation and
message commands are required; they keep repository rules and metadata private.
Exit 2 means no changes. A failed command leaves generated files and any staged
content available for inspection; it never resets or removes output, fetches,
pushes, or calls a model. The caller holds its task lock across generation,
commit and publication. Existing-worktree publication remains a separate step.

To run an explicit validation command against a historical revision in a
temporary, complete checkout:

```bash
scripts/publish worktree run-at-ref --repo /private/repository \
  --ref origin/main --scratch /private/cache/inspection \
  -- /path/to/validator --mode baseline
```

The command runs with that checkout as its working directory and receives
`REPOSITORY_PUBLISH_WORKTREE` and `REPOSITORY_PUBLISH_REPOSITORY`. Its stdout,
stderr and exit status pass through. Temporary files and Git registration are
removed on completion or command failure. This command does not commit or push;
the explicitly selected external command remains responsible for its own effects.

For a caller that owns a persistent task worktree and already created its commit:

```bash
scripts/publish --repo /private/task-worktree --existing-worktree \
  --expected-branch feature/task --publish-lock /private/locks/repository.lock \
  --validate-command '["/path/to/policy", "validate", "{worktree}"]'
```

This mode refuses a main checkout or a different branch. It fetches, rebases,
validates, pushes and verifies new LFS objects. It leaves the caller's checkout
and unpublished commits intact on failure. `--paths` optionally limits committed
changes; otherwise the caller owns the complete commit.

To verify one already-pushed commit independently:

```bash
scripts/publish --repo /private/repository --verify-lfs COMMIT --remote origin
```

Exit 0 means completion or task-lock contention. Exit 1 means the requested
operation did not complete; writer progress is not advanced before publication.
Existing-worktree publication distinguishes post-rebase validation rejection
(3) and rebase conflict (4), retaining the caller's worktree in both cases.
Invalid command syntax exits 2. Writer and policy output may contain private
data and belongs in the caller's private logs.

## Native configured jobs

Use `scripts/publish --config /private/job.json`. `--doctor` or `--dry-run`
checks local configuration and selected executables without creating locks or
worktrees, running writers, fetching Git, or publishing. It does not test remote
credentials or inspect the contents of a reader's private configuration.

```json
{
  "schema": "repository-publish-job/v1",
  "repo": "~/projects/example",
  "task": "archive",
  "paths": ["archive"],
  "subject": "sync: archive {timestamp}",
  "state_dir": "~/.local/state/example/archive",
  "lock": "~/.cache/example/archive.lock",
  "publish_lock": "~/.cache/example/repository.lock",
  "selection": "messages",
  "steps": [
    {
      "id": "messages",
      "argv": ["/path/to/reader", "--output", "{worktree}/archive", "--state", "{state}/cursor.json"]
    }
  ]
}
```

Configuration fields use the existing command-line names with underscores:
`repo`, `task`, `paths`, `sparse`, `subject`, `agent`, `state_dir`, `lock`,
`scratch`, `publish_lock`, `remote`, `branch`, `attempts`, `retry_delay`,
`lock_timeout`, `worktree_env`, `state_env`, `validate_command` and
`message_command`. Paths/sparse can be arrays; policy commands are argument
arrays, never JSON nested inside a string. Configured fields take precedence
when also supplied on the command line. Omitted fields keep CLI/default values.

An options-only configuration can omit `steps` and accept an external writer:

```bash
scripts/publish --config /private/publication.json --paths archive -- /path/to/writer
```

`steps` and an external writer are mutually exclusive. Configured jobs cannot
be combined with existing-worktree publication or independent LFS verification.

`environment` is an object of explicitly selected values. Strings support `~`,
`$NAME`, `${NAME}`, `{config_dir}`, `{repository}` and `{timestamp}`. An
`{"env": "NAME", "default": "value"}` value uses the nonempty caller variable
or its declared default. Environment entries are resolved in their declared
order; unset references fail. HOME and transaction-owned environment names
cannot be replaced. Expansion never executes shell expressions. Put credentials
in private files and pass references rather than embedding them in arguments.

Step arguments additionally support `{worktree}` and `{state}`. Each step has
a unique `id`, optional `group`, its `argv`, optional `environment`, and an
`on_error` policy. `--steps name,group` overrides configured `selection`; `all`
selects every step. Selection preserves declaration order. Unknown names fail.
Unselected commands and their environment requirements are not executed.

The default `on_error: "fail"` prevents publication and durable state promotion.
`"continue"` logs a warning and allows the remaining steps and publication.
Use it only when the caller intentionally accepts that reader's partial result;
the reader must preserve its own checkpoint on failure. An unavailable executable
or malformed operation always fails. All steps share one staged transaction.

A step may replace `argv` with a simple file-copy operation:

```json
{"id": "archive-file", "copy": {
  "source": "$EXPORT_SOURCE",
  "directory": "archive/handoffs",
  "name": "$EXPORT_FILENAME"
}}
```

The source must be a regular file; it remains intact on success or failure.
The destination directory is a relative owned path, and `name` must be a
non-hidden filename without directory separators. Symlink escapes are rejected.
Publication and LFS verification use the same transaction as command writers.
