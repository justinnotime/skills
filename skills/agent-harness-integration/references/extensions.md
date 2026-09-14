# Extending by node, harness and profile

Keep the existing owners and native formats. These dimensions compose; they do
not require a new central registry or one script per node/harness/profile tuple.

| Extension | Change at its existing authority | Reuse unchanged |
|---|---|---|
| Node: another deployment/account home | Explicit machine ID, installed selector links, paths, job assignments, timezone and receiving-device policy | Harness readers, native credential handling, backup layout and scheduling runtime |
| Harness: another native application or format | Native root variables, credential store, consistent state snapshot, session decoder and any supported hook adapter in their owning packages | Node selection, installation, task locking and publication |
| Profile: another selected instance of a supported harness | Explicit root, executable override if required, credential reference, permitted sources and output identity | The same harness adapter and node schedule; extend an existing source list |

A harness profile selects native roots; a data-pipeline profile is a local alias
for a repository's node selector. Neither implies an account or grants source
access. A new combination requires no runtime changes when its formats and
capabilities are already supported. Add code only for an actual new behavior.

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

Use this as an onboarding prompt, filling known selections from the caller's
actual configuration. Ask only for required information that cannot be inferred.

> Integrate the selected node, harness and profile using the existing owners.
> Identify which dimension changed and which native roots and credential source
> are authorized. Reuse the current runtimes; change private configuration for
> supported combinations. Keep each repository independent of unselected repositories,
> and select shared project inputs before scanning. Test a newly added transcript in
> an unselected profile and a missing selected input. Remove superseded copies and invocations. Keep
> credentials separate through generation, installation, backup and restore.
> Run the applicable acceptance cases, including upstream failure with no
> downstream publication. Report code support, installed configuration, active
> behavior, exact evidence and any unverified receiving node separately.

Keep deployment evidence in the caller's operations records. Derive current
coverage from those existing configuration authorities; do not maintain a
second changing support matrix in the workflow.
