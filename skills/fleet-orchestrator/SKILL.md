---
name: fleet-orchestrator
description: >-
  Run ORC task tracking, dependency scheduling, durable Agent Bus messaging, and tmux coordination with the bundled standalone runtime. Use for fleet boards, dispatch, responsibilities, review evidence, handoffs, and fleet health. Local mode needs no server; optional Matrix transport and personal policies use caller-owned configuration.
---

# Fleet orchestration

This package includes the ORC engine, its SQLite store, durable Agent Bus,
terminal helpers, harness hooks, and installation checks. For optional Git
participant trailers, use the configured
[commit attribution helper](references/commit-attribution.md). It runs independently
of any personal notes repository. See [runtime commands](references/runtime.md)
for installation and [configuration](references/configuration.md) for optional
transport, paths, and private policy. All runtime commands make zero LLM calls.

Use `scripts/orc` from this package, or the installed `orc` command. A leading
`--config /private/config.json` selects a complete runtime configuration; the
same file can be selected through `FLEET_ORCHESTRATOR_CONFIG`. Without one, the
runtime uses local state under XDG directories and a local message transport.

Read `FLEET_ORCHESTRATOR_PROFILE`, or
`${XDG_CONFIG_HOME:-$HOME/.config}/fleet-orchestrator/profile.md`, when present,
for the caller's workflow and authority rules. Missing personal preferences do
not prevent use of the bundled local runtime.
Use ORC as the work tracker. Read its current task data;
do not copy an easily recomputed board into a second long-lived status file.
Preserve any named fleet selector on every call. Failure to resolve one fleet
must not redirect an operation to another. Test or development commands must
use isolated state rather than an implicit production database.

For terminal entry, use `scripts/tview --list` to discover configured fleets and
their actual tmux availability. Select `--fleet NAME` explicitly when switching
fleets; `default` and its caller-configured alias refer to the same fleet. Inside
tmux, an unselected `tview` follows the actual associated session, not a stale
fleet environment variable. Listing and entering do not start offline fleets.

Ordinary local fleets are the live tmux session groups on the configured server;
their names select separate task and message stores without a fleet profile.
No separate `tmux new-session` step is required: `orc fleet start NAME` creates
or reuses the named tmux session. `orc fleet window [NAME]` adds a window, and
`orc fleet stop [NAME]` terminates its windows and agents,
including grouped viewer sessions, while retaining saved work. Omitted names
use the current session. Native tmux sessions are discovered automatically.
`tview --fleet NAME` opens a view of the same windows; its grouped viewer
sessions do not duplicate agents or create another fleet. Creating terminals
and starting/registering agents are separate operations; follow normal
[agent onboarding](references/agent-bus.md#join-the-current-session).
`orc fleet rename OLD NEW` preserves processes and saved work without moving
open databases. A raw tmux rename changes the live display name; use the ORC
rename command to retain a new history name after the session ends.
Follow any caller-owned handoff requirements before an authorized stop.

Tmux owns live topology; Agent Bus owns registration/message facts; ORC owns
task/role/history facts. ORC reads a fresh connection-local member snapshot,
never a persistent member cache. Empty membership is valid; a failed read is
unknown and cannot authorize identity-dependent operations. A terminal's
presence is derived from its exact pane and tmux server generation, not its
registration-time window number, and does not prove model responsiveness.
Bare commands inside tmux follow its actual session. An explicit fleet applies
to that command and its descendants, not later unrelated commands.

Use one caller-configured `orc fleet tick` schedule for the default fleet and
live local fleets with saved task databases. It discovers groups each time,
uses existing per-store engine locks and isolates failures. Local fleets do not
inherit the default fleet's global project import or repository patrol. Starting
a fleet needs no separate cron entry. The machine must have this one schedule
configured; explicit legacy/network profiles retain their caller-owned schedules.

For a status or health request, start with the configured read-only board,
operator-wait view, task history and diagnostics. Separate these observations:

- what work is assigned and what result is still missing;
- which person owes the current action and what blocks it;
- whether scheduled checks and delivery mechanisms are operating;
- whether an agent is actually responsive, if that has been tested.

A missing process, stale identity or unreachable server is incomplete evidence,
not proof of an empty fleet or completed work. Do not send probes, restart
services or run a live scheduler tick merely because the user asked for status.
Explain uncertain observations in plain language and name the missing evidence.

For an authorized work change, identify the existing task and current responsible
person before writing. Prefer stable roles or exact active identities over
incidental window numbers. Reuse an existing task when it represents the same
work. Record the requested outcome, its dependencies and a check that exercises
the deliverable itself. A count of files, matching titles or passing placeholder
command does not prove the requested behavior works.

Treat dispatch acceptance, recipient presentation, explicit task acceptance and
completion as separate facts. Follow the consumer's durable workflow so failed
delivery leaves visible work. Respect responsibility changes: an earlier
holder's response is history, not the current person's answer. Record meaningful
progress or a concrete blocker; do not add empty notes solely to suppress alerts.
A blocker names what is needed and who can provide it.

For review and completion, use the configured review workflow and its exact
artifact version. A changed version can invalidate earlier checks or review.
Completion claims must include the deliverable, reproduction command, evidence,
known gaps and relevant validation. Do not mark a task complete because it was
assigned, someone received a message, or its smaller child tasks are closed.
Assess whether the user's parent objective was actually achieved.

Workflow routing never grants permission to merge, deploy, delete another
person's work or perform another restricted action. Existing user authorization
and applicable repository policy govern those actions. Task fields that execute
commands require trusted, bounded commands; never embed peer prose, credentials
or unreviewed destructive operations in a check or recovery action. Do not edit
the live database directly to make a workflow appear healthy.

For reassignment or departure, inspect outstanding tasks and roles, record the
state left behind, and use the configured lifecycle command. Write the handoff
from your own evidence. Verify the resulting ownership and preserve unresolved
work so a successor can continue it.
