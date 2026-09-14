---
name: data-pipeline
description: Run and inspect repository-owned data jobs through a backward-compatible default or explicit profile entry. Use for explicit node assignments, schedules, input and output dependencies, ownership checks, and migration from command-heavy cron entries; configured processing and publication commands retain their own behavior.
---

# Repository-owned data pipelines

This package owns scheduling and dispatch. Each configured command owns its
actual reading, transformation, credentials, cleanup and publication. Invoke
commands through their executable interfaces; do not import sibling Skills.

For a short ordered operation, use a job's `commands` argument arrays instead
of a shell wrapper. They share one environment, lock and total timeout. Every
command must succeed before the next starts; the first failure stops the job.
Prefer native commands whose exit status represents the operation. An aggregate
tool that swallows child failures cannot establish that a later push is safe.
Keep complex transactions and recovery inside their existing owning command;
this is an ordered list, not dependencies between independently scheduled jobs.

Use `scripts/run` as the periodic trigger. With no arguments it continues to
read `~/.config/data-pipeline/config.json` (or the normal XDG equivalent), so
existing cron entries work unchanged. For multiple repositories under one
account, use `scripts/run --profile NAME` to select
`~/.config/data-pipeline/profiles/NAME.json`; `--config PATH` remains supported.
The two selection options are mutually exclusive. Each installed selection is
a symlink to a node selector inside its consumer repository. No mode discovers
or runs other profiles, and a missing selection fails without falling back.

A selector names one node in its repository catalog. Profile names are opaque
local aliases, not node identities or source permissions. Never infer a node,
account or source permission from the hostname or a profile label. Cron supplies
a clock pulse and optional configuration selection; job selection, schedules
and environment belong in the catalog. Read the configuration reference for
installation and gradual migration without changing existing defaults.

Read [configuration.md](references/configuration.md) when configuring or
migrating jobs. Keep actual accounts, paths, schedules, source selections and
node identities in the consumer repository. Credentials and mutable runtime
state remain outside Git and have declared path dependencies. Installation can
use `runtime-install`; this package does not edit cron, install credentials or
enable jobs by discovery.

Inspect before migration:

```sh
scripts/run --config /consumer/repo/config/data-pipeline/nodes/example.json --plan
scripts/run --config /consumer/repo/config/data-pipeline/nodes/example.json --doctor
```

Both commands are read-only and invoke no processing commands. The plan covers
all declared nodes and their transitive resources, while doctor checks local
requirements only for the selected node. A successful doctor proves path and
configuration availability, not credentials' validity or successful publication.
An explicit `--run JOB` executes one job assigned to that node and requires the
same authorization as its normal scheduled operation. Existing authorization
persists; inspection does not authorize new data access or a new writer.

A normal trigger considers only the current minute in each node's configured
timezone. It does not replay missed minutes. Each job has an independent lock
and a durable attempt record; repeated triggers in a minute do not run it again,
and long jobs skip overlapping triggers. Failures retry at the next configured
schedule. Commands have bounded runtimes, literal argument arrays, stdin closed,
and a clean environment populated only by HOME and repository configuration.
Unrelated due jobs run independently. A zero exit code is the command's reported
success; it is not a separate claim that new data was published.

Overlapping repository output paths are rejected across the whole catalog.
Disjoint partitions are permitted only with an explicit writer contract;
partition labels may be read directly from a referenced JSON manifest. Such
labels describe logical ownership, not a filesystem security boundary. Preserve
the existing writer's scope enforcement and test that it agrees with these
claims. An unregistered machine cannot be detected by a local configuration
check; retire old scheduled entries during deployment before enabling duplicates.

Development from this package: `uv sync --locked --all-extras`,
`uv run --no-sync pytest tests -q`, `uv run --no-sync ruff check src tests`,
and `uv run --no-sync agentskills validate "$PWD"` on Python 3.11 or later.
