---
name: agent-harness-integration
description: Integrate a new agent harness, profile, or machine with shared Skills, state backup, session and prompt extraction, ORC hooks, and existing schedules. Use for harness onboarding and cross-mechanism acceptance; delegates implementation to existing packages rather than creating another installer or scheduler.
---

# Agent Harness Integration

Use this workflow when adding a harness or checking whether an existing one is
fully integrated. This is an instruction-only Skill, not an installation
command. Read optional caller instructions from `AGENT_HARNESS_INTEGRATION_PROFILE`
or `${XDG_CONFIG_HOME:-$HOME/.config}/agent-harness-integration/profile.md`.
Missing private instructions do not grant access to accounts or session roots.

## Establish scope

Distinguish a new harness format from another profile of a supported harness,
and both from installing an existing integration on another machine. Inspect
the selected executable's version, native documentation, installed configuration,
running process, and existing package interfaces before proposing changes.

First ask what duplicate mechanism can be removed. Reuse existing installers,
source readers, publishers and schedules. Do not create a combined registry,
configuration generator, per-harness cron, or private copy of public code.

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
| Registration and messages | `agent-bus`, implemented by `fleet-orchestrator` | Stable session binding, native delivery adapter, explicit processed acknowledgment and restart behavior |
| Tasks, lifecycle and turn hooks | `fleet-orchestrator` | Consistent fleet selection, native hook compatibility/trust, obligation review and departure |
| Git publication | `repository-publish` | Existing transaction, owned paths, validation, locks and recoverable progress |
| Downstream views | `prompt-translation`, `activity-summary`, selected usage analysis | Explicit input selection and compatible timestamps/identities; no automatic coverage assumption |

Load each selected owner's Skill and use its documented public commands. An
uninstalled package is a missing prerequisite, not permission to reconstruct
its implementation here. Packages must remain independently installable; use
configured executable interfaces, never sibling-package internal imports.

## Keep the contracts aligned

1. **Roots and authority:** launch, backup and extraction must agree on actual
   paths, including environment overrides and symlink targets. A profile label
   is not an account, project, security boundary or extraction permission.
2. **Private configuration:** changing XDG roots can redirect child tools too.
   Explicitly select shared configuration where intended. Do not copy credentials
   or replace special launchers with a generic function without review.
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
