# Configuration and run records

The installed selector is a symlink, not a copied job configuration:

```json
{"schema":"data-pipeline-node/v1","catalog":"../pipelines.json","node":"example"}
```

`catalog` is relative to the selector's resolved location. The shared catalog
contains `schema: data-pipeline/v1`, `repository` (relative to the catalog),
`state_directory`, `environment`, `nodes`, `resources`, and `jobs`.
Both selector and catalog must reside inside `repository`.

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
