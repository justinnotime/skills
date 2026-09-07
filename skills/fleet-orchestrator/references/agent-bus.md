# Durable messaging

`scripts/matrix-bus.sh` and the installed `agent-bus` command operate the bundled
transport. Both local and Matrix modes keep identity, inbox, outgoing messages,
delivery leases, and processing acknowledgments in SQLite. Local mode writes
directly to the shared host database. Matrix mode additionally requires the
explicit private service and room configuration described in
[configuration](configuration.md).

Use `agent-bus --help` for the current commands. `members` and `unread` inspect
state; joining, sending, pulling a leased presentation, acknowledging, reviving,
or retiring an identity changes that state. Keep identity selection explicit.
Do not treat a registered identity as proof that a model is responsive.

The transport distinguishes accepted, delivered, presented, and processed.
Repeated presentation is bounded; a repeatedly unacknowledged message can be
parked until explicitly revived. Callers must deduplicate external side effects
by message identity. A processed acknowledgment belongs after actual handling,
not merely after fetching the message.

`scripts/agent-boot.sh` coordinates current-pane identity recovery, ORC
onboarding, and the configured startup briefing. It requires a real known pane
for terminal identities. `scripts/install-agent-bus-pull-notify.py` installs
only the explicitly selected hook/transport integration. Local mode requires
no Matrix dispatcher service. Matrix installation requires a caller-owned
service template; inspect its help for the supported substitutions.

No configuration, transport receipt, or peer message grants operator approval.
Only send messages within the user's existing authorization.

## Join the current session

Select `FLEET_ORCHESTRATOR_CONFIG` first. Run the package's
`scripts/agent-boot.sh <task-slug>` from the actual agent pane, using a short
task name without a host prefix or terminal suffix. For a named fleet, retain
`--fleet <name>` on its commands. Inspect the returned identity, harness, mode
and pane before reusing a registration.

In ordinary local tmux sessions, boot and bus commands discover the session's
fleet automatically. An explicit `--fleet` still selects exactly that fleet.
Use different task names for simultaneous agents; a window belongs to its
session but does not become a registered message recipient until onboarding.

Local member inspection reads the existing database without creating or
migrating it. A missing database means no registrations; an unreadable or
incomplete database is an error. Current terminal locations are derived from
the exact registered pane and tmux server generation, so renaming a session or
renumbering windows does not move a message to another agent. `registration_tmux`
preserves the original fact; `terminal_presence` reports present, absent or
unknown. Present means that terminal exists, not that the model is responsive.

Old local registrations without a server binding report unknown until normal
onboarding binds the real terminal again. No peer identity is silently changed.
Legacy Matrix registrations retain their previous exact terminal checks until
normal rejoin supplies the stronger binding. Resident watchers refresh a
derived session name only after their durable database/transport selection
still matches; a rename does not authorize a move to a different message store.

Onboarding selects `pull` for Codex and `watch` for Claude Code or OpenCode.
Set `AGENT_BUS_HARNESS` explicitly when the terminal name does not identify the
harness. A custom harness also requires `AGENT_BUS_MODE`; for a DeepSeek Harness
installation without a watcher integration, use `AGENT_BUS_HARNESS=dsh` and
`AGENT_BUS_MODE=pull`. Registering that mode does not install a wake mechanism.

The bundled Codex integration handles a Stop event before the turn finishes;
it can request another turn for pending work. It cannot wake a session after
that boundary has passed. OpenCode's plugin manages its watcher, while Claude
Code uses the watcher selected during onboarding. Follow any missing integration
diagnostic and activate the selected harness configuration before claiming
delivery works. Do not start another session's watcher or copy its slot.

## Inspect and process the inbox

Use the same configured `scripts/matrix-bus.sh` or installed `agent-bus` entry:

| Command | Effect |
|---|---|
| `members` | Inspect registered identities |
| `unread <agent-id>` | Ingest pending delivery and count inbox states without presenting messages |
| `replay <agent-id>` | Ingest and display available or presented messages without acquiring presentation leases or acknowledging them |
| `pull <agent-id>` | Present a bounded batch and acquire its leases |
| `ack <agent-id> <message-id> ok` | Record processing of a previously presented message |

Read the complete `pull` output; piping it through `head` can hide messages
that already hold a lease. Recover obscured content with `replay`. Replay alone
does not make an unpresented message eligible for processing acknowledgment;
present it through `pull`, handle it, then acknowledge it. Neither `unread` nor
`replay` is a guarantee of zero database writes because both can ingest delivery.

## Leave the session

`scripts/orc checkout --summary "<handoff>"` checks outstanding work and roles,
uses the configured handoff publication command, and retires the selected
identity only after successful publication. Without a publication command,
it writes the handoff to the configured local directory. `--no-vault-note` skips the handoff
write; use it only when the caller's policy explicitly permits that omission.
It does not skip the outstanding-work checks. Repository destinations and
handoff requirements belong in private configuration and operating policy.
