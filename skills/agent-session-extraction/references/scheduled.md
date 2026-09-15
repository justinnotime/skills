# Scheduled extraction

`scripts/run --config /path/to/private/schedule.json` invokes the same
extraction API as `scripts/extract`. Its external publisher is selected by
configuration. No consumer repository, source directory, account, schedule,
lock location, or publication convention is built into the package.

The schedule JSON contains:

| Field | Meaning |
|---|---|
| `schema_version` | `agent-session-schedule/v1` |
| `manifest` | Absolute path to the private source manifest |
| `repository_root` | Absolute source checkout, matching the manifest |
| `publication.command` | External publisher command as an argument array |
| `publication.output_root_environment` | Variable in which that publisher supplies its output worktree |
| `publication.base_ref_environment` | Optional variable supplying the Git commit/reference from which a `git-worktree` writer starts |
| `environment` | Optional explicit environment overrides for the publisher |
| `failure_marker` | Optional absolute path for a sanitized failure report |
| `preflight_command` | Optional read-only argument array checking consumer policy before every mode, including doctor and dry-run |
| `validate_command` | Optional argument array validating the generated output |
| `expand_environment` | Optional boolean enabling explicit environment references in paths, environment values and command arguments |
| `require_external_config` | Optional boolean rejecting a config inside the selected repository or a config symlink |

Concrete native JSON can be installed without a configuration generator. With
`expand_environment: true`, `$NAME`, `${NAME}`, and leading `~/` expand once;
missing or empty referenced variables fail before publication. `$$` is a literal
dollar. Shell expressions, default-value operators, command substitutions and
recursive expansion are not evaluated. Environment values expand against the
process environment first; paths and command arguments then use that environment
plus the configured overrides. Omit the option to preserve literal strings in
an existing schedule. Keep node identity and source authority in the separately
reviewed manifest; environment expansion does not choose sources.

Copy a native configuration to a private regular file before enabling
`require_external_config`. A selected config is never rewritten by the runtime.
An existing scheduler calling `--config` does not need a new entry merely because
its deployment stopped using a generator. Other readers of fields such as
`failure_marker` must use the same explicit environment or retain concrete paths.

The publisher receives the extraction command appended to its argument array.
For a publisher using `--` before its writer, include that delimiter at the end
of `publication.command`. Arguments are passed directly, without shell
evaluation. A shell can be explicitly selected as an executable when needed.

Command arguments support `{manifest}`, `{repository_root}`, `{owned_paths}`,
and, for validation, `{output_root}`. The owned paths come from the manifest;
they are not independently copied into the schedule. `{owned_paths}` expands
to one space-separated argument and therefore accepts only unambiguous path
components. Source selection, node ownership, cleanup, indexes, redaction,
retention, and output naming remain in the existing manifest.

For `filesystem-atomic`, the publisher creates a linked Git worktree and invokes
the appended command. For `git-worktree`, it reserves an unused absolute path
outside the source repository and passes that path through the configured
output environment variable without creating it. The appended command uses the
existing runtime to prepare, encrypt, audit, and stage that worktree.

With `publication.base_ref_environment`, the publisher must also supply a
nonempty commit/reference in that variable. The runtime prepares the worktree
from that revision **before** reading output inventory or decoding sources;
dirty, staged, ignored or untracked files in the source checkout do not enter
the attempt. The caller still selects the manifest and source authority.
The publisher owns fetching, commit selection, push races and any optional
update of the interactive checkout. The runtime never changes that checkout.
Omitting the field preserves the existing HEAD/inventory contract. Doctor and
dry-run do not require the variable and remain read-only.

In both cases the publisher invokes the appended command,
and commits/pushes only after that command succeeds. The extraction command
refuses the main checkout and worktrees belonging to another repository.
The external publisher owns locking, Git conflict handling, repository policy,
attachment delivery, and progress-state publication. Existing publisher
implementations can be reused without copying their private policy here.

The manifest must use `filesystem-atomic` or `git-worktree`. The latter retains
the manifest's key link and ciphertext-index checks; commit and push still belong
to the external publisher. A configured preflight runs before reading sources
in every mode; its output is suppressed and failure aborts the invocation.
Scheduled extraction requires redaction, output auditing, reconciliation, and
prepublication scanning. Failure in extraction or configured validation must
abort the publisher. Publisher failures produce a nonzero exit status and the
configured failure marker without relaying its potentially sensitive output.

`--doctor` checks the manifest and source capabilities without executing the
publisher. `--dry-run` performs the extraction plan against existing output;
it does not create a worktree, write a marker, or execute the publisher.

```sh
scripts/run --config /path/to/private/schedule.json --doctor
scripts/run --config /path/to/private/schedule.json --dry-run
scripts/run --config /path/to/private/schedule.json
```

Keep real schedule files and credentials outside this package. Test with
synthetic transcripts and publishers operating only on temporary repositories.
