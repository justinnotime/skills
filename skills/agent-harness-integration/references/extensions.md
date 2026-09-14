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
> supported combinations. Remove superseded copies and invocations. Keep
> credentials separate through generation, installation, backup and restore.
> Run the applicable acceptance cases, including upstream failure with no
> downstream publication. Report code support, installed configuration, active
> behavior, exact evidence and any unverified receiving node separately.

Keep deployment evidence in the caller's operations records. Derive current
coverage from those existing configuration authorities; do not maintain a
second changing support matrix in the workflow.
