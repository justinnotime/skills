# The harness × node × profile model

Keep the existing owners and native formats. These dimensions compose; they do
not require a new central registry or one script per node/harness/profile tuple.

A **harness** is a native application and its formats/interfaces. A **node** is
an explicitly selected deployment context, usually a machine/account home. A
**harness profile** selects an instance's roots and executable on that node.
Labels can repeat across unrelated harnesses or nodes; they are not global
session identities. Only configured combinations exist: this model does not
enable the full Cartesian product or promise every native capability.

| Extension | Change at its existing authority | Reuse unchanged |
|---|---|---|
| Node: another deployment/account home | Explicit machine ID, installed selector links, paths, job assignments, timezone and receiving-device policy | Harness readers, native credential handling, backup layout and scheduling runtime |
| Harness: another native application or format | Native root variables, credential store, consistent state snapshot, session decoder and any supported hook adapter in their owning packages | Node selection, installation, task locking and publication |
| Profile: another selected instance of a supported harness | Explicit root, executable override if required, credential reference, permitted sources and output identity | The same harness adapter and node schedule; extend an existing source list |

A harness profile selects native roots; a data-pipeline profile is a local alias
for a repository's node selector. A Skill's private `profile.md` is caller
guidance. These are separate uses of the word profile; none implies an account
or grants source access. A new combination requires no runtime changes when
its formats and capabilities are already supported. Add code only for an actual
new behavior, including an unsupported platform or native interface.

## How the mechanisms compose

The [owning Skills](../SKILL.md#use-existing-owners) retain their own native
configuration. The integration is the agreement between those selections:

- Launchers select the harness instance; `runtime-install` exposes its selected
  Skills and private configuration.
- `state-backup` selects original state and writes the configured backup layout.
  Replication has its own service and receiving-device checks.
- `agent-session-extraction` selects authorized live roots, snapshots or mirrors
  and produces configured histories and raw human prompts. Backup is not a
  mandatory intermediate for every source, and backup scope grants no read access.
- `data-pipeline` selects where and when configured commands run. Its catalog
  declares inputs, dependencies and outputs; processing packages still select
  what they read. `repository-publish` handles configured Git transactions.

The executing node and a session's origin are different identities. A mirrored
session retains its original configured identity even when another node reads
it. Independent origins need distinct output identities when native IDs collide.
Use the existing output-ownership contract for owners and aggregators; assigning
another scheduler node must not create a second writer for the same partition.

## Add one dimension

**Node:** select its explicit ID, repository and supported platform; configure
its paths, environment, dependencies and job assignments. Select only its
authorized roots, backup destinations, source origins and output ownership.
Install its configuration/discovery links and existing pipeline trigger with
`runtime-install`. Verify on that node: paths resolved on another machine are
not evidence of a usable local deployment. A moved writer requires retirement
on its former node as part of activation.

**Harness:** inspect its native root, credentials, state consistency, transcript
format and required discovery/hooks. Extend only missing adapters in the owning
packages, with package-local synthetic tests; leave supported mechanisms alone.
Then select a concrete node/profile through private configuration and exercise
the applicable acceptance path. Another version or mode using the same formats
may require configuration only. Record unsupported native capabilities explicitly.

**Profile:** select another supported harness instance's roots, executable and
credential reference in its repository. Extend discovery, backup selection and
extraction sources independently; select output identity and shared-project
subtrees explicitly. Extend the existing job's inputs/dependencies and source
list instead of creating a per-profile cron. Test both the new selection and an
unselected sibling. A separate repository consumer may select its own pipeline
profile; that alias is not the harness profile.

## Repository ownership and shared inputs

Keep each repository's profile definitions self-contained. The node selects the
file through the existing `--config` interface; it does not import all repositories
into a fragment owned by one of them. Generate a separate launcher output for each
repository, preserving global command-name collision checks. Backup also accepts
`--config FILE`; repository-only selections set `BACKUP_INCLUDE_DEFAULT=false`
and list their explicit roots. Add these invocations to existing repository jobs.
A missing unselected repository must not stop the selected repository's commands.

Cursor is another harness, and each Cursor profile is another source selection.
The profiles package supports the native Agent CLI `CURSOR_CONFIG_DIR` override;
verify actual transcript, IDE and credential paths separately. On a shared
`projects/` directory, extraction's `discovery.directories` selects exact project
subtrees before traversal. Backup has its own per-profile
`CURSOR_PROJECT_ALLOWLIST`. Neither a root-selection launcher nor a project-output
filter establishes input isolation. Do not infer access by decoding project names.

For another supported combination, change configuration only. Prove this with
synthetic independent nodes and profiles, including identical native session IDs
in distinct configured origins. Add a matching transcript to an unselected
sibling and require unchanged selected output. Remove a required selected directory
and require failure, not empty success. Repeat with a symlink into the unselected
sibling. Preserve existing identities and inspect the normal cleanup plan when
narrowing a deployed input selection.

## Configuration and credentials

Maintain each non-secret choice once under its owning private configuration.
Install a symlink or a minimal native selector; do not create independently
editable machine copies. Use native relative-path behavior, explicit XDG roots
and configured overrides. Resolve symlinks before deciding where a file lives.

Separate three kinds of material when provisioning or restoring:

- Declarative settings, selected sources, schedules and install choices are
  versioned private configuration.
- Authentication stays in native private stores, selected explicitly per root.
  Record paths and renewal methods, never values. Fresh nodes authenticate
  independently; restoring a settings backup does not restore an account.
- Mutable sessions, databases, cursors, logs and generated indexes stay outside
  Git. Back up only the selected state with the owning runtime's consistency rules.

Inspect the generator as well as its output. A one-time cleanup is undone if
the normal refresh command writes inline credentials again. Generate mixed
output in a private temporary directory and use its owning credential-separating
installer before moving settings into replicated paths. Do not add a second
credential-copy daemon or a generic redaction layer to keep unsafe copies current.
For arbitrary new native formats, establish the credential boundary before
enabling their configuration backup.

## Scheduling and failure

Extend existing repository job configuration. Keep cron as the existing clock
trigger (and selected repository profile where applicable). Put task arguments,
environment, resources and schedules in the owning repository. When migrating,
retire the precise old invocation together with activation of the new job;
do not leave both running. Independent command wrappers are removable when a
short `data-pipeline` `commands` list expresses the complete ordered operation.

Read the real subprocess exit contract. If a tool consumes failures internally,
invoke the required native operation directly. Verify failure before publication,
timeout, repeated invocation and missing required input. A failure test must
assert absence of downstream writes, not only a nonzero final status.

## Request template

Invoke `agent-harness-integration` with the owning repository and one concrete
operation, for example:

> Add node `<node>` using the existing harnesses and selected profiles.

> Add harness `<harness>` on `<node>` with profile `<profile>`.

> Add profile `<profile>` for `<harness>` on `<node>`.

Supply known roots and permitted source scope; infer existing selections from
the caller's configuration and ask only for missing decisions. Cover the chosen
discovery, backup, session/raw-prompt and scheduling capabilities through their
owners. Report the configuration diff, applicable acceptance evidence and gaps.
For an existing deployment moving to newer mechanisms, use
[the upgrade procedure and request](upgrades.md) instead of treating it as a new node.

Keep deployment evidence in the caller's operations records. Derive current
coverage from those existing configuration authorities; do not maintain a
second changing support matrix in the workflow.
