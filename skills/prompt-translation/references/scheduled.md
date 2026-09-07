# Scheduled translation and publication

`scripts/run` combines this package's translator with an explicitly configured
external `repository-publish/scripts/publish` executable. The publisher owns Git
worktree preparation, fetch, rebase, publication locking, push and LFS checks.
This package neither imports another Skill's source nor installs a schedule.

Use a persistent linked worktree so completed model work survives an interrupted
run or failed publication. A temporary transaction that discards failed output
would cause already completed translation to be purchased again.

```sh
/path/to/prompt-translation/scripts/run --config /private/translation-schedule.json --doctor
/path/to/prompt-translation/scripts/run --config /private/translation-schedule.json --dry-run
/path/to/prompt-translation/scripts/run --config /private/translation-schedule.json
```

The last command can call the configured model and publish commits. Run it only
within the caller's authorized source selection and publication scope.

## Schedule configuration

The default schedule path is `~/.config/prompt-translation/schedule.json`.
This is separate from the translator configuration described in
[configuration](configuration.md). `runtime_config` defaults to
`~/.config/prompt-translation/config.json` when omitted.

```json
{
  "schema_version": "prompt-translation-schedule/v1",
  "repository_root": "$HOME/work/repository",
  "worktree": "$HOME/work/translation-task",
  "task_branch": "translation-job",
  "lock": "~/.cache/prompt-translation/task.lock",
  "runtime_config": "~/.config/prompt-translation/config.json",
  "publisher_command": ["/path/to/repository-publish/scripts/publish"],
  "publication": {
    "remote": "origin",
    "branch": "main",
    "owned_paths": ["learning/pairs"],
    "subject": "sync: prompt translations",
    "agent": "prompt-translation",
    "publish_lock": "~/.cache/prompt-translation/publication.lock",
    "message_command": ["/private/policy", "message", "{worktree}", "{scope}"]
  },
  "job": {
    "validate_command": ["/private/policy", "validate", "{worktree}", "{scope}"],
    "commit_command": ["/private/policy", "commit", "{worktree}", "{scope}"]
  },
  "selection": {
    "since_date": "2025-01-01",
    "through_date": "2025-01-02",
    "limit_days": 25
  },
  "timeout_seconds": 2700,
  "environment": {
    "PROMPT_TRANSLATION_PYTHON": "$HOME/.venvs/prompt-translation/bin/python",
    "PATH": "/usr/local/bin:/usr/bin:/bin"
  }
}
```

All paths must resolve to absolute paths. `~`, `$HOME` and `${HOME}` use the
caller's home directory; other shell expressions are not evaluated. The
worktree must be separate from, and outside, the repository checkout. Its branch
must differ from the publication branch. Configuration and lock files must not
live in the worktree. Task and publication locks must be different files.

`environment` overrides inherited variables for the translator, publisher and
policy commands. `PROMPT_TRANSLATION_PYTHON` selects the translator's interpreter;
otherwise it uses the interpreter running the schedule. The shell entry also
accepts an inherited `PROMPT_TRANSLATION_PYTHON` for the schedule process itself.
Keep dependency installation, credentials, account selection and log locations
in the caller's environment or private configuration.

Commands are argument arrays, executed without a shell. Policy arguments expand
`{worktree}`, `{repository}` and `{scope}`. The publisher executable must support
the public `worktree prepare`, `changed --null`, `ahead`, `reset` and
`--existing-worktree` interfaces. `publisher_command` is the executable prefix;
the scheduler appends the operation's arguments itself.

Required publication fields are `owned_paths` and `subject`. `remote`, `branch`
and `agent` default to `origin`, `main` and an empty identity. `publish_lock`
defaults to the publisher's setting when omitted. `message_command` is optional.
Owned paths are literal relative paths without spaces, `.` or `..` components;
configure them to cover the translator's generated pair files.

## Date selection

`selection` requires exactly one of `date`, `since_date` or `days`.

| Setting or option | Meaning |
| --- | --- |
| `date` / `--date DATE` | One UTC day's records |
| `since_date` / `--since-date DATE` | Inclusive daily backlog, oldest first |
| `through_date` / `--through-date DATE` | Inclusive backlog end; UTC yesterday when omitted |
| `days` / `--days N` | Legacy rolling source-file mode |
| `limit_days` / `--limit-days N` | Maximum changed work units; default 25 |
| `--limit-files N` | Alias for the limit; in legacy mode it counts source files |
| `--force` | Reprocess selected work even when the saved input matches |

Command-line selections override the configured mode. Limits are applied after
the translator checks reusable progress. All scheduled model runs use strict
failure reporting. Translation model, classifier, prompts, filters and source
paths come from `runtime_config`.

`--doctor` validates schedule syntax, configured command availability and the
translator's local configuration, SDK availability and selected credentials.
A required credential that is absent causes failure. It does not contact a
provider, invoke the publisher or private policies, prepare a worktree, or create
a lock. It does not prove that the remote accepts pushes or that policies pass.

`--dry-run` reads the repository's sources and reusable output to estimate work.
It makes no model calls, reads no credential values, and performs no Git writes,
worktree preparation, recovery, publication or lock creation. Neither inspection
mode resets the persistent worktree or its progress.

## Private policy contract

The schedule requires `validate_command` and `commit_command`. These are trusted
caller-owned programs; document text never selects them.

- `validate` receives scope `worktree` before committing new or recovered files,
  and `committed` when the publisher validates after rebasing. Return zero only
  when the complete change set is owned, permitted deletions have replacements,
  strict `scripts/validate` succeeds, and repository policy accepts the change.
  Validation must not modify content. Repository lint belongs here.
- `commit` receives scope `worktree`. It can delegate to the public publisher's
  `worktree commit` with explicit source repository, task branch, owned paths
  and private validate/message commands. Return zero on success
  or 2 for no differences. Remaining dirty files are still an error.
- Optional `message_command` receives scope `committed` and prints the complete
  commit message. The publisher invokes it after post-rebase validation to
  refresh metadata derived from current repository rules. A validator must not
  reject merely outdated metadata that this message command is meant to refresh.
- Older profiles may contain `recover_command`; it is accepted for compatibility
  but is not executed. Remove it when updating the private profile. Invalid
  results remain in place and the schedule stops before new model calls.

Use strict source validation for publication. `--allow-source-ahead` is for
separate structural inspections and does not establish that a publishable output
covers the current source. Keep repository-specific file rules, lint baselines,
acknowledgements out of the public package.

## Progress and failure behavior

After taking the task lock, the scheduler prepares the persistent worktree,
recovers valid interrupted files, and retries unpublished commits before any new
model work. Only a clean worktree with no outstanding commits is reset to the
fetched upstream. Committed pair files carry reusable progress, so removal of
the local ignored cache does not itself require translating completed records
again.

After translation, all completed files pass private validation, commit and
publication even when a later day fails. The original translator exit status
is returned after successful publication. A timeout sends termination to the
translator's process group, allows 30 seconds to exit, then kills remaining
processes if needed; completed files are still considered for publication.

A rejected push, rebase conflict, failed commit or failed validation preserves
completed files or unpublished commits for the next invocation. Source changes
are treated the same way: validation failure does not authorize discarding paid
output. A retry attempts validation/publication before any new model work. If a
candidate is genuinely obsolete, inspect the changed sources, retain the
completed translations, and have the caller decide how to correct or regenerate
it. Do not reset its branch or remove the worktree just to make a run pass.

Exit 0 means completion or a busy task-lock skip; the skip is logged explicitly.
A failed translation retains its nonzero status after any completed files are
published; translator timeout returns 124. Schedule or publication failures
return 1 with a diagnostic identifier. Argument parsing errors return 2.
Private policy and writer output belongs in private scheduler logs, which may
contain source paths and generated text. Inspect both the exit status and Git
history to distinguish a fully successful run from a partially successful one.
