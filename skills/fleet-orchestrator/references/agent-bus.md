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

The read-only `members` output includes `slot` only for identities with an
existing local row in the selected database. Its value comes directly from that
row, including when other member facts arrive through Matrix. A remote-only
member has no `slot` in this output; remote metadata cannot supply or override
it. Reading members does not create identities, repair schemas, or migrate data.

Old local registrations without a server binding report unknown until normal
onboarding binds the real terminal again. No peer identity is silently changed.
For an operator-authorized Matrix-to-local migration, retain the existing task
and message databases, stop the network dispatcher, and set `bus.transport` to
`local`. After checking the original registered panes, run
`agent-bus --fleet NAME registry-migrate --bind-local-terminals` to bind only
same-host, active legacy identities whose exact pane and registered location
still match. Missing panes remain unbound; identities, messages and processing
states are preserved. Back up the databases first and ensure resident watchers
load the local configuration before declaring the migration complete.
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

### OpenCode conversation binding

The OpenCode plugin requires a dedicated terminal-backed instance. It does not
choose the latest stored conversation. The first chat message or bus-tool call
whose session the SDK confirms is a root session in the plugin's project and
directory selects the conversation. The selection is permanent for that plugin
instance; child sessions and other roots cannot redirect delivery or use its bus
tools. Merely opening an idle saved conversation does not register or lease mail
until a qualifying message/tool call occurs. A shared server receiving unrelated
root conversations cannot identify which attached terminal owns the first event;
that deployment is not supported.

For new registrations without `AGENT_BUS_SLOT`, the resume slot hashes the hostname, resolved bus
database, tmux socket/server generation and exact pane, working directory, and
OpenCode root session ID. The bus environment and tmux session observation are
the authorities; there is no separate identity registry. Restarting OpenCode in
the same pane with the same root reuses the slot despite a different OpenCode
process ID, window title, or window number. A different pane, fleet store, tmux
server generation, directory, or root produces a different slot. The bus still
enforces one active identity per pane. Starting a different root does not retire
the predecessor or bypass checkout/succession requirements.

For a shipped directory-only default, the plugin first reads members from the
selected database. It adopts exactly `opencode:${directory}` only when that
registration is active, identifies OpenCode in watch mode, and matches the
hostname, exact pane, and observed tmux server generation. Local transport also
requires `terminal_presence=present`. The database and transport must still
match after the read. Adoption uses the existing `agent_id` without `setup` or
`join`, preserving its handle, inbox, and presentation leases. Other panes or
directories are never adopted; new registrations keep hashed slots. An
unavailable/ambiguous member snapshot or a same-pane record without a local
`slot` is not permission to guess ownership.

This is restart-time continuation, not a concurrent takeover of an old plugin
process. Expired registrations omitted by `members`, retired identities, and
registrations without a verified server generation require explicit recovery.
The old slot contains no historical root conversation ID, so adoption can only
bind the newly selected, validated root; it cannot prove which root was used
before the upgrade. No cross-directory or cross-pane relocation is inferred.

An explicit `AGENT_BUS_SLOT` overrides adoption and is passed unchanged. Its
owner is responsible for selecting the intended continuation. Default handles
for new registrations include the scope hash so split panes do not collide on
handle uniqueness; adoption keeps the old handle. Supplied handles/slugs retain
their meaning on the normal join path. The existing process lock is keyed
by a hash of the bus database and slot, avoiding punctuation and cross-fleet
filename collisions. A confirmed dead owner can be reclaimed; invalid locks or
permission-denied liveness checks fail closed. PID reuse and simultaneous stale-
lock reclamation remain limitations of this process-lock scheme.

Automatic delivery presents bounded batches, continuing until a pull returns no
messages. Acknowledgments request another refill. A periodic pull recovers
expired presentation leases even without a watcher event. Failed SDK delivery
retains the current batch and retries it before acquiring more leases. Automatic
delivery and manual pull/ack calls are serialized. Delivery is still at least
once: SDK acceptance does not prove model handling, and an ambiguous request
failure or lease expiry can repeat a message. Bus attempt limits and oversized
message limits still apply; there is no automatic acknowledgment or revival.
Injected text and manual pull output retain `[Agent Bus wake]` and explicitly
identify peer content as not operator authorization; no extra SDK provenance
fields are assumed.

The plugin validates the join result and checks the existing identity with
`heartbeat` before pull/ack or watcher restart. The adapter's explicit retired or
unknown identity error stops the watcher and timers without rejoining. A changed
bus database/transport also stops the instance instead of redirecting it.
Transient adapter errors retry; retirement detection depends on the bundled
adapter's heartbeat error contract, not a new status API. Root deletion and
plugin disposal stop local delivery without performing an operator checkout.
An already accepted prompt or in-flight subprocess cannot be recalled, and
store/retirement checks are not atomic with a subsequent command. No hot reload
or current-session restart is performed by editing the source; activate it only
through an authorized install and a new OpenCode process.

Behavioral tests evaluate the shipped TypeScript with stub SDK, subprocess,
filesystem and timer dependencies. From this package, run:

```bash
node --experimental-vm-modules --test tests/opencode_agent_bus.test.mjs
uv run --locked pytest tests/test_opencode_agent_bus_plugin.py -q
```

The pytest tests invoke the same Node suite and an isolated native integration
using a private tmux server, empty tmux configuration, and synthetic local bus
database. They pass real `members` output to the plugin and check identity,
lease and inbox preservation through acknowledgment. The plugin's `tmux -u`
query is also exercised under `LC_ALL=C` to verify tab-delimited parsing.
Node 22.13+ and tmux are required; missing dependencies fail rather than skip.
No JavaScript package dependency, live fleet, hook or model is used.

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

`scripts/orc agent checkout --summary "<handoff>"` checks outstanding work and roles,
uses the configured handoff publication command, and retires the selected
identity only after successful publication. Without a publication command,
it writes the handoff to the configured local directory. `--no-vault-note` skips the handoff
write; use it only when the caller's policy explicitly permits that omission.
It does not skip the outstanding-work checks. Repository destinations and
handoff requirements belong in private configuration and operating policy.
