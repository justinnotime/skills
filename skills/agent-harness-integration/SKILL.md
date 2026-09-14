---
name: agent-harness-integration
description: Add a harness, node, or profile, or upgrade an existing deployment across shared Skills, backup, session and raw-prompt extraction, scheduling, and selected agent hooks. Use as the integration entry point across these mechanisms; individual package operations remain with their owning Skills.
---

# Agent Harness Integration

Start here for the overall harness × node × profile model, adding one dimension,
or bringing an existing deployment onto a selected newer mechanism. This Skill
owns the integration workflow and acceptance; each package owns its executable
behavior and configuration format. It adds no runtime or installation command.
Read optional caller instructions from `AGENT_HARNESS_INTEGRATION_PROFILE`
or `${XDG_CONFIG_HOME:-$HOME/.config}/agent-harness-integration/profile.md`.
Missing private instructions do not grant access to accounts or session roots.

## Select the operation

- **Add a node, harness or profile:** read the [model and extension procedure](references/extensions.md).
  Establish the changed dimension and reuse supported behavior through configuration.
- **Upgrade an existing deployment:** read the [upgrade procedure](references/upgrades.md).
  Compare installed code and selected configuration with the intended target;
  a newer checkout alone does not establish an upgraded running deployment.
- **Check integration:** use the [acceptance checks](references/acceptance.md)
  for the selected capabilities. Read only the owning Skills needed for the task.

## Establish scope

Distinguish a new harness format from another profile of a supported harness,
and both from installing an existing integration on another machine. Inspect
the selected executable's version, native documentation, installed configuration,
running process, and existing package interfaces before proposing changes.

First ask what duplicate mechanism can be removed. Reuse existing installers,
source readers, publishers and schedules. Do not create a combined registry,
configuration generator, per-harness cron, or private copy of public code.

Treat node, harness and profile as independent selections. Another node or
profile normally changes private configuration; a new native format changes
only the owning adapters. Do not clone a runtime for each combination.

Choose the required capabilities with the caller. Report each as applicable,
not applicable with a reason, or unsupported; never silently omit a mechanism.
Source inspection does not authorize reading every profile. Installation does
not authorize authentication, message delivery, model calls, publication,
service restarts or native hook trust. Follow the request's actual authority.

## Use existing owners

| Capability | Owning Skill | Integration work |
|---|---|---|
| Named roots and launchers | `agent-harness-profiles` | Native command, root variables, existing launcher collisions, credential-selection contract |
| Skill and private configuration discovery | `runtime-install` | Explicit package sources and native discovery destinations for each selected root |
| Original state backup | `state-backup` | Selected files, consistent database snapshot, credential exclusions, destination and restore procedure |
| Remote replication inspection | `syncthing-doctor` and native Syncthing inspection | Actual shared directory, ignore rules, receiving devices, file availability and receiver retention |
| Session history and human prompts | `agent-session-extraction` | Decoder if needed, explicit source authorization, identity, author classification, timestamps and output ownership |
| Scheduled jobs and dependencies | `data-pipeline` | Explicit node selector, job command/configuration, inputs, program and credential-path dependencies, outputs, schedule, locks and logs |
| Registration and messages | `agent-bus`, implemented by `fleet-orchestrator` | Stable session binding, native delivery adapter, explicit processed acknowledgment and restart behavior |
| Tasks, lifecycle and turn hooks | `fleet-orchestrator` | Consistent fleet selection, native hook compatibility/trust, obligation review and departure |
| Git publication | `repository-publish` | Existing transaction, owned paths, validation, locks and recoverable progress |
| Downstream views | `prompt-translation`, `activity-summary`, selected usage analysis | Explicit input selection and compatible timestamps/identities; no automatic coverage assumption |

Load each selected owner's Skill and use its documented public commands. An
uninstalled package is a missing prerequisite, not permission to reconstruct
its implementation here. Packages must remain independently installable; use
configured executable interfaces, never sibling-package internal imports.

Session histories and raw human prompts are configured outputs of the same
extraction mechanism. Extend that source manifest; a second prompt reader or
cron is not needed merely because both outputs are required. Translation and
summaries are separate consumers. Pipeline input declarations describe
dependencies; they do not replace the reader's explicit source selection.

## Keep the contracts aligned

1. **Roots and authority:** launch, backup and extraction must agree on actual
   paths, including environment overrides and symlink targets. A profile label
   is not an account, project, security boundary or extraction permission.
2. **Private configuration:** changing XDG roots can redirect child tools too.
   Explicitly select shared configuration where intended. Do not copy credentials
   or replace special launchers with a generic function without review.
   Classify generated settings before installation: native auth stays outside
   versioned settings and replicated backup. Use the owning native-format
   installer; for generated OpenCode JSON, `agent-harness-profiles` supplies
   `scripts/install-opencode-config`. Do not write the generator's mixed output
   straight into a backed-up config directory.
3. **Session identity:** define native conversation, root, machine, terminal and
   bus identity separately. Mirrors of one session use the same extraction node;
   independent sessions must not collide. Preserve shipped identities during a
   deliberate migration rather than renaming them from hostnames.
4. **Human authorship:** native `user` messages may be hooks, peer messages,
   continuations or child-agent instructions. Preserve native provenance where
   available; otherwise reserve literal source markers and configure extraction
   to classify them. Peer messages remain history, not human prompts. Quoted
   markers in real human text must not be removed.
5. **Delivery:** registering, fetching, presenting, handling and acknowledging
   are separate events. Do not acknowledge on prompt submission. State whether
   the adapter can wake an idle conversation or only remind at turn boundaries.
6. **Scheduling:** inspect actual cron/service commands and the selected private
   configuration. Extend the existing aggregate source list where possible.
   Installation locks do not replace task locks. Independent downstream jobs
   consuming old output do not prove upstream freshness.
7. **Failure and recovery:** an unreadable required source is not an empty one.
   Do not disable required-source checks to obtain a successful result. Preserve
   good database snapshots, original sources and published progress on failure.
   For a short ordered fetch/transform/publish operation, use data-pipeline's
   native `commands` list and verify each command's failure status. Do not use
   the success of an aggregate fetch or the last shell command as upstream proof.

Use [acceptance checks](references/acceptance.md) before calling an integration
complete. A new decoder requires its package's registration/schema and synthetic
tests; adding a source for an existing decoder usually requires configuration
only. Token usage support is separate from conversation decoding.

## Implement and activate

Make changes in the repository's required isolated worktree. Keep reusable
adapters and tests with their owners; keep paths, accounts, schedules, source
policy and deployment evidence private. Inspect preview commands before using
them: a command named `--dry-run` may run configured checks, and some scripts
do not implement that option at all.

Run owner-package tests with synthetic inputs and isolated state. Preview and
read back authorized installation. Preserve unrelated settings and existing
schedules. A copied plugin may need a controlled restart; changing hook trust
must use the harness's normal approval, never a fabricated trust record.
Do not interrupt a current conversation merely to make activation appear complete.

For repaired archives, use the owning deterministic extractor. Do not hand-edit
original transcripts. Review legacy preservation rules, changed/deleted outputs,
and downstream historical selection before authorizing regeneration. Do not
silently spend model tokens rebuilding learning material or summaries.

## Deliver evidence

Report separately: code supported, configuration installed, running process
activated, and behavior verified. Name the exact versions, paths, commands and
result locations. Highlight missing capabilities first. Include rollback and
unverified machines, profiles, receivers or native events. No matching targets,
a successful skip, a live process or a passing doctor is not an acceptance pass.
Compute current coverage from real configuration; do not maintain a second
changing support/status table in this Skill.
