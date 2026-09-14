# Configuration and run records

## Default and named profiles

The installed selector is a symlink, not a copied job configuration:

```json
{"schema":"data-pipeline-node/v1","catalog":"../pipelines.json","node":"example"}
```

`catalog` is relative to the selector's resolved location. The shared catalog
contains `schema: data-pipeline/v1`, `repository` (relative to the catalog),
`state_directory`, `environment`, `nodes`, `resources`, and `jobs`.
Both selector and catalog must reside inside `repository`.

Selection is explicit and backward compatible:

| Invocation | Selected file below `$XDG_CONFIG_HOME/data-pipeline` |
| --- | --- |
| `scripts/run` | `config.json`, the unchanged default |
| `scripts/run --profile example` | `profiles/example.json` |
| `scripts/run --config /absolute/selector.json` | The supplied selector, independent of installed links |

When `XDG_CONFIG_HOME` is unset, the base is `~/.config`. `--profile` and
`--config` are mutually exclusive and work with `--plan`, `--doctor`, `--run JOB`
and scheduled execution. Profile names start with an ASCII letter or digit and
contain only ASCII letters, digits, `_`, `.` or `-`; they are aliases, not paths.
An absent or invalid named profile fails even when the default exists. Without
a default, a no-argument invocation fails even if exactly one named profile is
installed. No invocation enumerates profiles or schedules all repositories.

A single-repository machine can keep only its existing `config.json` link.
For multiple repositories, install additional links such as
`profiles/example.json` to each repository's selected node file. The optional
`runtime-install` Skill can manage these through its existing `profiles`
source/destination entries. Keep the current default link in place while
adding named links; no catalog schema or installed-default migration is needed.
Each repository owns its catalog and processing configuration. Give separate
catalogs separate state directories and logs; job IDs need only be unique within
a catalog. Profile selection does not namespace or move existing state files.
Output ownership is checked within the selected catalog, not across separately
invoked profiles; those profiles must retain their writers' scope enforcement.

Upgrade the public package first. Old zero-argument cron entries continue using
the same default. Nodes can then migrate independently:

1. Add a named link to the same repository node selector as the default.
2. Compare `scripts/run --plan` with `scripts/run --profile example --plan`, then
   check `scripts/run --profile example --doctor` on that node.
3. Replace that consumer's existing trigger with
   `scripts/run --profile example` using its managed cron configuration. Keep
   the default link until its remaining callers have explicitly migrated.

Default, named and explicit-path invocations of the same catalog use its same
job locks and attempt records. Switching entry syntax preserves in-flight locks
and minute deduplication. Schedule an additional repository only through its
own explicitly authorized trigger. The cron installer needs no new format:
its existing `argv` array can hold the executable, `--profile`, and the name.
Rollback restores the previous trigger; the preserved default still works.

## Catalog resources and jobs

Each node defines a `timezone` (IANA name) and optional `environment`. Job
arguments and environment expand `$NAME` or `${NAME}` strictly. Built-ins are
`HOME` (this account's home), `REPO` (resolved repository) and `NODE` (explicit
node label). Environment layers are catalog, node, then job, in declaration
order; undefined variables fail. Escape a literal dollar sign as `$$`. PATH must be configured. Ambient account,
proxy, executable and source-selection variables are not inherited. Use secret
files supported by the processing command; secret values never belong in this
catalog or its plan output.

Each resource declares `kind` (`config`, `data`, `state`, `credential`,
`executable`, or `service`), `scope` (`repository` or `node`), `paths`, and optional
`requires` resource IDs. Paths are repository-relative or absolute after variable
expansion. Service entries may instead carry a descriptive `description` with
no filesystem path; their health belongs to the processing command. `optional`
means local doctor does not require those paths; document the writer's skip
behavior. Config resources must exist inside the repository. State resources
are created by their owner. Doctor does not open credential contents.

Instead of repeating paths already held in a JSON config, use:

```json
{"paths_from":{"file":"config/processor/settings.json","pointer":"/outputs/*"}}
```

References use JSON Pointer escaping and support `*` to expand a list or object.
They must select nonempty strings in a repository file. Returned values expand
variables; relative paths remain relative to the repository. Referenced file
paths appear in the plan. Resource dependency cycles and missing references fail
before any job runs. Dependencies describe data and files, not an execution DAG:
upstream completion, incremental selection and publication stay with the existing
processing command. A clock pulse does not force upstream refreshes.

Each job has:

- `id`, `node`, `schedule`: globally unique stable ID, assigned node, numeric
  five-field cron expression. Lists, ranges and positive steps are supported.
  Sunday is 0 or 7. Restricted month-day and week-day use normal cron OR behavior.
- `argv`: literal strings; the first is an absolute executable. No implicit shell.
  Alternatively, `commands` is a nonempty list of such argument arrays. Specify
  exactly one form. Commands run sequentially with the same cwd/environment,
  existing job lock and one total `timeout_seconds` budget. Failure, timeout or
  spawn error prevents later commands. All executable paths are checked before
  the first command runs. Records include the one-based `command_index` and
  `completed_commands`; no command arguments are copied into run records.
- `inputs`, `dependencies`: resource IDs, including transitive file dependencies.
- `outputs`: objects naming a `resource`, optionally `partitions` (strings) or
  `partitions_from` (the same file/pointer reference shape). Partitioned resources
  need a `partition_contract` describing how the writer enforces those labels.
- `timeout_seconds`, `log`, optional `environment`: runtime bound, append log,
  and explicitly configured environment. Log and state paths must be external
  to the repository. Preserve the existing writer's own locks during migration.

Jobs writing overlapping paths conflict even if they use different resource
names. Exact shared roots can be partitioned when contracts match and labels
are disjoint. Parent/child overlaps cannot be waved away with different labels.
Represent an explicitly owned derived index in the writer's partition contract
and enforce that assignment in consumer tests.

Run records are `<state_directory>/<job>.json` and lock files use `.lock`.
Records identify the UTC minute, start/finish, status, return code and configured
log path. An attempt is saved before spawning so repeated triggers cannot retry
the same minute after an interruption. A crash may leave a `running` record;
the lock determines whether a job still runs, and the next scheduled minute can
retry when it is free. Persistent loss of the state directory loses deduplication
history. These are execution records, not publication receipts.

Deployment order: validate the whole catalog, check each selected node, preserve
the old cron bytes and selected links, synchronize the verified package and
consumer revisions, replace only this consumer's old entries under the existing
cron-install lock, then exercise an authorized configured job and inspect its
native publication evidence. An installer should preserve unrelated jobs. Restore
the old entries and matching configuration together for rollback; never leave
both old and new triggers active. A node reassignment also requires disabling
that job on its previous node; local file locks are not distributed leases.

For example, replace a fetch-and-publish shell wrapper with native commands:

```json
{"commands": [
  ["/usr/bin/git", "-C", "$HOME/src/example", "fetch", "origin"],
  ["/usr/bin/git", "-C", "$HOME/src/example", "push", "mirror", "refs/remotes/origin/main:refs/heads/main"]
]}
```

Retain the caller's chosen ref and overwrite policy; the runner does not add a
force flag. Test an actual nonzero upstream result and assert that no downstream
write occurred. A wrapper returning success after a failed subprocess remains a
wrapper defect; sequence execution cannot recover an error the command erased.
