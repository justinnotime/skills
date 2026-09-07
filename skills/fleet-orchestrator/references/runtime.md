# Standalone runtime

Requirements: Python 3.11 or newer on Linux, SQLite (in the Python standard
library), and Bash. Terminal operations also need tmux, jq, and the selected
agent harness. GitHub inspection uses an independently authenticated `gh` CLI.
No command in this package calls an LLM. No personal repository is required.

From this package directory:

```bash
python3 scripts/install
export PATH="$HOME/.local/bin:$PATH"
orc --help
agent-bus --help
orc open --to operator --subject "Review the sample output" \
  --body "Inspect the generated output and record the decision." --no-check
orc board
orc verify
```

Installation creates command launchers only. It does not register identities,
send messages, alter a crontab, install services, or restart agents. Existing
unmanaged commands require explicit replacement and are backed up when replaced.
Keep the package at its installed location, or rerun installation after moving it.

The `orc` command covers task creation, dependencies, roles, review, completion,
handoffs, periodic checks, and database inspection. Run `orc <command> --help`
for exact arguments. `orc tick --dry-run` previews scheduler actions without
sending them; a real `tick` may execute configured checks and send task reminders.
Scheduling belongs to the caller. Do not run a real tick for a status request.

Ordinary local fleets are native tmux session groups: the session name selects the
task and message stores. There is no separate local fleet configuration to
create, synchronize or delete. A session made with native tmux commands is
discovered too. Existing explicit local/Matrix profiles remain supported for
compatibility and configured network transports.

You do not need to create a tmux session first. Start or reuse a workgroup and
enter its windows with:

```bash
orc fleet start example
tview --fleet example
```

Tmux still owns the terminals and their processes. ORC coordinates their
lifecycle with saved work; tview provides a grouped view of the existing
windows. A viewer can have its own tmux session name while sharing those same
windows and agents. It is not another workgroup or a new set of agents.
Starting/registering an agent remains a separate
[onboarding operation](agent-bus.md#join-the-current-session).

These are independent operations, not a sequence to run together:

| Operation | Command | Effect on tmux |
|---|---|---|
| Start or resume a workgroup | `orc fleet start NAME` | Create or reuse its named session |
| Enter its terminals | `tview --fleet NAME` | View the same shared windows |
| Add a terminal | `orc fleet window [NAME]` | Create a window in the selected session |
| Rename a workgroup | `orc fleet rename OLD NEW` | Rename the session and retain its history association |
| End a workgroup | `orc fleet stop [NAME]` | Terminate all its shared windows and the processes in them; retain saved work |

`create` remains an alias for `start`. Stop closes the session's shared windows
so grouped viewer sessions cannot keep its agents running. It retires the
stopped panes' registered identities and retains task/message history. Follow
any caller-owned checkout/handoff requirements before stopping. Reopening the
same session name reuses its saved work. Ending a session does not delete its
history or leave a local configuration file to remove. An ordinary new window
inherits its workgroup through its session; no environment export or agent
restart is needed for command selection. Agent registration and model startup
still use the normal onboarding procedure.

Each used local session stores one immutable `@orc-runtime` history key on
tmux itself. `orc fleet rename OLD NEW` records the new name as a relative
filesystem alias to the original runtime directory and renames the session.
It never moves a running SQLite database. Old names still reach the same saved
work; reopening the new name after stop does too. A conflicting session or
different saved history rejects the rename. History aliases cannot escape the
configured runtime root.

A raw `tmux rename-session` changes the current display name and keeps its
running store through that key. It does not record a durable history rename;
use the ORC rename command when the new name must survive session destruction.
Raw `tmux kill-session` follows native tmux semantics: linked windows can remain
alive in grouped viewers. Such a surviving group remains discoverable. Use
`orc fleet stop` to close the whole group and its agents.

### State authority and scheduling

| State | Authority | Derived view |
|---|---|---|
| Live sessions, groups, windows and panes | tmux | Fleet discovery and tview |
| Identity, registration and messages | Agent Bus | Current terminal location and heartbeat age |
| Tasks, roles, dependencies and history | ORC task store | Board and continuation decisions |
| Member lookup for a command | Current Agent Bus response | Private temporary SQL table, discarded with the connection |

There is no member-cache synchronization command. Empty membership is a valid
observation and replaces any previous view; source failure is unknown, never
an empty fleet or a reason to trust old cached identities. Opening a read-only
board does not create or migrate the bus database. A window's existence does
not register a model, transfer its tasks, or prove that it responds to messages.

Configure one machine schedule to call `orc fleet tick`. It discovers the
default and live local fleets with existing task databases, runs them with
bounded concurrency, and uses each task store's existing lock. A slow or failed
fleet does not prevent another from starting. Newly created shell-only sessions
need no database or scheduling job. Local fleets process their own recorded work
and do not import the default fleet's configured GitHub projects or run its
global repository patrol. Explicit `orc --fleet NAME tick` remains a single-fleet
command. `orc fleet tick --dry-run` inspects without scheduling writes or sends.
Explicit legacy/network profiles keep their separately configured schedules.

### Finding and entering fleets

```bash
tview --list
tview --list --json
tview --fleet default
tview --fleet example 3
tview 3
```

The list derives ordinary local fleets from live tmux session groups and also
includes the default configuration and existing explicit profiles. It distinguishes
an online primary, an offline server, a missing primary session, and invalid or
unavailable configuration. It never starts a server or creates a session.
Availability here means tmux availability, not agent responsiveness or task
completion. Grouped terminal views are not additional fleets.

Set `fleets.default_name` in the private runtime configuration to give the
existing default fleet a recognizable alias. Both that alias and `default`
select the same fleet in `tview`, `orc` and `agent-bus`; no database or message
transport is moved. `tmux.primary_session` selects its primary session, with
`0` as the compatible default. This preserves an existing deployment's default
data and transport while new local sessions receive their own stores.

An explicit `--fleet` always selects that target. For ORC and Agent Bus it stays
in effect through that command's child processes, including an explicit default
selection. A process-scoped marker prevents an old exported selector from
redirecting a later unrelated command. Inside tmux, an unselected
`tview` identifies the fleet from the actual socket and exact primary session
or its session group, ignoring a stale `NW_FLEET`. Local sessions on the configured
server map directly to their names. Outside tmux, `NW_FLEET` selects a configured
fleet when present; otherwise `tview` enters the default fleet. A positional
argument remains a window index or exact window name, so `tview 3` keeps its
meaning. Use `tview --fleet default` to return from a named fleet.

Each attached terminal keeps its own grouped view and selected window. Switching
between servers replaces that terminal's client without destroying either
primary session or disconnecting other clients. Selecting an offline fleet fails
explicitly; use the normal authorized fleet creation/start procedure separately.

### Other terminal and deployment commands

See [Agent Bus operations](agent-bus.md) for durable messages. `scripts/agent-tmux-send.py`
is a separate best-effort terminal sender. It does not provide durable inboxes.
`scripts/configure-tmux-server.py` verifies a selected server before writing its
selector; `scripts/tmux-fleet-manifest.py` records exact recovery identities.

`fleet-rollout` inspects or installs explicitly selected artifacts described by
a caller-owned manifest. It distinguishes files on disk, active processes, trust,
and observed behavior. The manifest schema is
`scripts/rollout-artifacts.schema.json`; `--manifest PATH validate` checks it.
Different artifact source directories require explicit `source_roots.working`
and `source_roots.canonical`; installation compares their contents. Personal
services and schedules remain outside this package.

Harness integrations are in `plugins/`. Turn-report plugins call the installed
`orc-turn-report` executable, or `ORC_TURN_REPORT_COMMAND` when set. Reporting is
limited by the configured enrolled-identity file. Installing a plugin does not
prove an existing process loaded it; reload through the harness's normal process.

Run package checks with synthetic inputs:

```bash
uv sync --locked
uv run --no-sync pytest tests -q
uv run --no-sync ruff check scripts tests --select E9,F63,F7,F82
uv run --no-sync skills-ref validate "$PWD"
bash tests/test-dispatch-ledger-guards.sh
bash tests/test-tview.sh
bash tests/test-tview-regression.sh
bash tests/test-fleet-staging.sh
```

The tests include temporary SQLite databases, copied-package execution,
identity and delivery failures, task transitions, configuration boundaries,
and isolated terminal behavior. Live network permissions and existing agent
processes require separate deployment checks.

`scripts/fleet-staging.sh e2e [directory]` rehearses the actual task dispatch,
reminder, dependency, and terminal write paths against fake agents and an inert
GitHub command. It also runs the isolated Agent Bus convergence test. The
rehearsal needs tmux, jq, and `ss` (the `iproute2` package on Debian/Ubuntu).
It uses a separate terminal server, database, runtime root, and synthetic policy.
No live service credentials are required. `up`, `down`, and `status` support
manual inspection. The default directory is
`${XDG_STATE_HOME:-$HOME/.local/state}/fleet-orchestrator/staging`; a nonempty
directory without the harness's ownership marker is refused. Each successful
new setup replaces only an already marked rehearsal directory. `down` stops
the rehearsal server and preserves its logs; the test wrapper removes its own
temporary directory after successful shutdown verification.
