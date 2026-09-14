# Upgrade an existing integration

Use this procedure for a selected deployment moving to newer public mechanisms
or repository configuration. The [extension model](extensions.md) still applies:
reuse existing owners and update only affected selections. Do not create a
second registry, per-node upgrade program or copied implementation.

## Establish the installed and target contracts

Identify the selected repository, executing node, harnesses/profiles and requested
capabilities. Inspect actual command paths, resolved configuration links, cron or
service entries, and active native versions. Record public/private revisions and
the intended target revision. If no target is specified, inspect the relevant
upstream changes before selecting a verified target; do not assume a branch name
means deployed code. Unknown installed versions remain an evidence gap.

Compare the owning packages' instructions, configuration references, source and
tests between those versions. A private dated handoff is migration evidence, not
the current behavior authority. Check only affected contracts:

| Change | What must remain aligned |
|---|---|
| Command or configuration schema | Scheduled argv, environment, selectors and required executable dependencies |
| Root or profile selection | Native paths, credential references, discovery links and separate backup/extraction permissions |
| Source or identity rules | Selected input subtrees, stable origins, writer partitions and cleanup scope |
| Output or state format | Existing archives, checkpoints, backup names and downstream readers |
| Scheduling or installation | One active invocation per job, existing task locks and matching service/hook activation |

Translate the differences into the smallest repository configuration change.
Remove superseded wrappers, copied settings and duplicate invocations when their
replacement is complete. Preserve credentials, source authority, published
identities, destinations and writer responsibility unless changing them is part
of the request. A required source missing after upgrade is a failure to resolve,
not a reason to broaden discovery or mark it optional.

## Verify compatibility before activation

Stage changes in the repositories' task worktrees. Use the owning packages'
focused checks and selected private configuration checks. For scheduled work,
inspect `data-pipeline` plan and doctor with the explicit target selector; for
source changes inspect the extractor's proposed output and cleanup. Preview
installation with the existing installer. These checks do not establish live
credentials, native event delivery, fresh data or successful publication.

Check whether the new runtime reads the installed configuration and durable state.
Deploy backward-compatible public commands before configuration that needs them.
If versions cannot coexist, coordinate the affected writer's code/config switch
under its existing locks, and coordinate a moved writer on both nodes. Replacing
a file path does not change an already running process. Preserve unrelated jobs;
do not manufacture native hook trust or interrupt an active conversation.

Keep the previous code/configuration selection and affected installation entries
recoverable. State-format changes need a supported migration/restore method;
switching code back is not sufficient when the older reader cannot read newer
state. Never run old and new writers against one output store.

## Demonstrate the changed behavior

Read back installed selectors and the actual scheduled invocation. Exercise an
authorized job through its normal pipeline entry so its configured environment
and lock apply. Inspect the native result, output, publication and durable state,
including whether expected data was selected rather than skipped. Use the
[acceptance checks](acceptance.md) for affected mechanisms and failure cases;
do not rerun unrelated capabilities merely to make a longer checklist.

Record versions, selected scope, commands, evidence paths, rollback limits and
any pending process activation in the caller's operations records. Separate
local verification from unvisited nodes and receivers. Narrowing a source does
not erase old backup copies: review retained data and extractor cleanup separately.
Historical regeneration and model-calling downstream work retain their own scope.

## Keep the procedure useful for the next upgrade

When changing public behavior, update the owning package's configuration and
compatibility instructions with the code. Update this workflow only when the
cross-package contract changes. Keep deployment decisions and revision-bound
evidence private; derive current coverage from configuration and runtime checks.
No copied fleet status table or central release counter is required.

Suggested request:

> Use `agent-harness-integration` to upgrade the selected deployment in
> `<repository>` on `<node>` to `<target revision or mechanism>`. Read its private
> integration profile, compare installed and target contracts, update affected
> configuration, retire replaced invocations, and verify through the normal job
> entry. Preserve existing source permissions, identities and destinations except
> for the requested changes. Report evidence, rollback and remaining gaps.
