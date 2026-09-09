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
orc task open --to operator --subject "Review the sample output" \
  --body "Inspect the output and decide; this needs human judgment with no automated progress check." --no-check
orc board
orc admin verify
```

Installation creates command launchers only. It does not register identities,
send messages, alter a crontab, install services, or restart agents. Existing
unmanaged commands require explicit replacement and are backed up when replaced.
Keep the package at its installed location, or rerun installation after moving it.

### Fleet work and terminal views

`orc` lists fleets with terminal availability and current work. `orc fleet NAME`
opens that fleet's work overview; `tview -t NAME` enters its terminal windows.
Both use the same names, default alias, session groups and saved history.
Grouped terminal viewers never become additional fleets. Stopped local fleets
with saved tasks or messages remain discoverable without starting a terminal.

```bash
orc
orc --json
orc fleet example
orc fleet example board
orc fleet example board --view columns
orc fleet example board --view summary
orc fleet example board --repo example-project --json
orc fleet example goals
orc fleet example goals --all
orc fleet example task show TASK_ID
orc fleet example agents
orc fleet example view 3
```

The short selector `orc -t example board` selects the same fleet as
`orc fleet example board`. Fleet names that collide with a legacy lifecycle
word (such as `tick`, `list` or `stop`) can always use `-t NAME`. Unlike tview,
ORC's `-t` selects only a fleet; a window is an argument of `view`.

### Discovering commands and their relationships

`orc --help` shows the complete command directory, grouped by purpose, with
one-line descriptions. `orc help task` and `orc fleet example task --help`
explain the same group; `orc fleet example task dispatch --help` shows exact
arguments. `orc help legacy` maps old flat spellings to their grouped forms.
Root and group help work without resolving a fleet. Argument help can require
a valid selected fleet; use `orc task dispatch --help` to discover arguments
before choosing one. Help does not initialize task stores.

| Group | Responsibility |
|---|---|
| `task` | Record, deliver, accept, update, connect and close work |
| `review` | Record a PR review verdict and the author's verification evidence |
| `goal` | Organize child tasks and assign people to one goal |
| `agent` | Inspect identities and terminals, manage roles, and perform handoffs |
| `admin` | Inspect configuration, diagnose state, back up or run the scheduler |

`task open` records work without sending it; `task dispatch` also delivers it.
The recipient uses `task ack` to accept it. `task handshake` can succeed when
the message is merely presented; inspect `task show` for explicit acceptance
or progress. `task note` reports progress. `task blocked`
records a required decision and its decision maker; `task reassign` changes who
owes the work. `task claim-done` requests independent verification and leaves
the task open. `task close` records the final resolution after that verification.

PR work uses `--workflow pr` and names its author and reviewer. The readiness
check hands it to the reviewer, who uses `review verdict`. Blockers return it
to the author; a clean review leads to `review receipt`, where the author
records verification evidence. The receipt supports the merge decision and
does not grant permission to merge.

For example, the author registers a PR in its owning fleet, then the independent
reviewer and author each record their part. Replace the identities, repository,
PR URL and task ID below. The three absolute command paths are placeholders for
trusted checks you supply, not bundled scripts: `pr-ready` exits 0 only when the
PR is ready for review; `pr-head` exits 0 and prints its current commit SHA;
`pr-merged` exits 0 only after it has actually merged. They run on the machine
and in the working directory of the ORC process that executes the checks.

```bash
# Author: record work already known to you; dispatch also sends a notification.
orc fleet example task open --workflow pr \
  --to AUTHOR --owner AUTHOR --reviewer REVIEWER --repo example/project \
  --subject "Review PR 123" --link https://github.com/example/project/pull/123 \
  --body "Review the PR against its stated acceptance conditions." \
  --ready-cmd '/absolute/path/pr-ready example/project 123' \
  --check '/absolute/path/pr-head example/project 123' \
  --done-cmd '/absolute/path/pr-merged example/project 123'

# Independent reviewer: after readiness and reviewing the actual PR revision.
orc fleet example review verdict TASK_ID clean \
  --note 'Reviewed HEAD_SHA; findings at PR_REVIEW_URL'

# Author: after the clean verdict, provide the evidence required by project policy.
orc fleet example review receipt TASK_ID \
  --body-file /absolute/path/review-evidence.md

# Read the request, stored evidence and history together.
orc fleet example task show TASK_ID
```

Use the task ID printed by `task open`. A reviewer who finds blocking issues
uses `blockers` instead of `clean` and describes them in `--note`; the author
fixes the work before another review. The configured scheduler runs the checks;
see `task open --help` for their omission behavior and execution requirements.

`agent role` assigns a fleet responsibility; `goal team` associates people with
one goal. `agent onboard` reads an existing identity's obligations and handoff;
Agent Bus/agent-boot performs registration. `agent topology` relates identities
to current panes and roles. Creating a fleet terminal does not register an agent.

`board`, `goals` and `agents` remain short inspection entries. `statusline` and
`kanban` are compatible forms of `board --view summary` and
`board --view columns`; `tree` maps to `goal list` or `goal show ID`. Prefer
these grouped forms in new instructions rather than learning a second workflow.

### Selecting work

Inside a real tmux pane, `orc board`, `orc task show TASK_ID` and `orc agent
onboard ID` follow that pane's actual fleet. An explicit selector overrides it
for the command and its descendants. Outside tmux the existing configured
default/environment selection applies. `orc` without arguments always lists
fleets, and `orc -t NAME` shows one fleet's overview.

Work is organized into `task`, `goal`, `agent` and `review` commands. `goals`
and `agents` are the list views for their respective groups. `goal open` uses
the existing parent-task workflow; it does not create a second goal store.
`goals` hides closed goals and children by default; `--all` or an explicit goal
ID includes history. `agent role` and `goal team` preserve existing assignments.
Run `orc fleet NAME task --help` or a leaf command's `--help` for arguments.

Board table, columns, summary and JSON share one read-only selection, including
repository filtering. Counts include parent tasks, as the existing task store
does; `goals` counts that subset separately. Empty fleets can be inspected
without creating task or message databases. JSON board output is an object
with `fleet`, `tasks`, `goals`, `counts`, `recently_closed`, `scheduler` and
`warnings`; unknown work is reported as unknown, never zero, in the fleet list.
`task list --json` retains its existing newline-delimited task objects.

Maintenance belongs under `orc fleet NAME admin`. `orc admin tick` runs the
configured scheduler across fleets; `orc fleet NAME admin tick` runs only that
fleet. Add `--dry-run` to inspect without executing checks or sending reminders.
A status request never authorizes a real scheduler tick.

Existing flat commands (`open`, `tree`, `kanban`, `statusline`, etc.),
`orc --fleet NAME COMMAND`, `orc tview`, and verb-before-name lifecycle forms
continue to forward to the same handlers. They have no separate state or
implementation. `tree` now defaults to current goals too; use `tree --all` for
its historical output. Use `board --view columns|summary` in new integrations.

Ordinary local fleets are native tmux session groups: the session name selects the
task and message stores. There is no separate local fleet configuration to
create, synchronize or delete. A session made with native tmux commands is
discovered too. Existing explicit local/Matrix profiles remain supported for
compatibility and configured network transports.

You do not need to create a tmux session first. Start or reuse a workgroup and
enter its windows with:

```bash
orc fleet example start
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
| Start or resume a workgroup | `orc fleet NAME start` | Create or reuse its named session |
| Enter its terminals | `tview --fleet NAME` | View the same shared windows |
| Add a terminal | `orc fleet NAME window` | Create a window in the selected session |
| Rename a workgroup | `orc fleet OLD rename NEW` | Rename the session and retain its history association |
| End a workgroup | `orc fleet NAME stop` | Terminate all its shared windows and the processes in them; retain saved work |

The legacy `orc fleet create NAME` remains an alias for `start`. Stop closes the session's shared windows
so grouped viewer sessions cannot keep its agents running. It retires the
stopped panes' registered identities and retains task/message history. Follow
any caller-owned checkout/handoff requirements before stopping. Reopening the
same session name reuses its saved work. Ending a session does not delete its
history or leave a local configuration file to remove. An ordinary new window
inherits its workgroup through its session; no environment export or agent
restart is needed for command selection. Agent registration and model startup
still use the normal onboarding procedure.

Each used local session stores one immutable `@orc-runtime` history key on
tmux itself. `orc fleet OLD rename NEW` records the new name as a relative
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
`orc fleet NAME stop` to close the whole group and its agents.

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

Configure one machine schedule to call `orc admin tick`. It discovers the
default and live local fleets with existing task databases, runs them with
bounded concurrency, and uses each task store's existing lock. A slow or failed
fleet does not prevent another from starting. Newly created shell-only sessions
need no database or scheduling job. Local fleets process their own recorded work
and do not run the default fleet's global repository patrol. No fleet imports
open PRs as new tasks: register each PR explicitly in its owning fleet with
`task open` or `task dispatch`, using `--workflow pr`. GitHub account membership
and merge responsibility do not establish task ownership.
Explicit `orc fleet NAME admin tick` remains a single-fleet
command. `orc admin tick --dry-run` inspects without scheduling writes or sends.
Explicit legacy/network profiles keep their separately configured schedules.

### Finding and entering fleets

```bash
tview -l
tview -l -j
tview -t default
tview -t example:3
tview -t example:editor
tview 3
orc tview -t example:3
```

`tview` is the short entry; `orc tview` forwards the same arguments. Like
`tmux attach -t SESSION:WINDOW` (or `tmux a -t SESSION:WINDOW`), `-t` selects a
session and optionally a window index or exact window name. `-t SESSION` enters
that session's view; `-t :WINDOW` selects a window in the current/default fleet.
The session selector also accepts a configured fleet alias. Both short and
long forms are supported: `-f`/`--fleet NAME`, `-w`/`--window WINDOW`, `-l`/`--list`,
`-j`/`--json`, `-h`/`--help`; the long form of `-t` is `--target`.
For example, `tview -f example -w 3` and
`tview --fleet example --window 3` select the same target as `tview -t example:3`.
`orc --fleet example tview -w 3` also preserves the explicit fleet.

Ordinary `tmux a -t SESSION` clients share that session's current window.
Tmux's native `new-session -t SESSION -s VIEW` creates a grouped session with
the same windows and processes but an independent current window. Tview
automates that native operation for each terminal and attaches to its view.
Window navigation is independent; pane contents, input, window layout and
processes remain shared. This is an interactive terminal, not tmux read-only
mode. The `-t` spelling follows tmux; tview is not a pass-through for every
tmux flag or target expression.

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
server map directly to their names. Inherited `TMUX` and `TMUX_PANE` are trusted
only while the server that minted `TMUX` still owns that pane, verified by server
process ID and pane ID; a stale pair, such as one passed down by a daemon that
was started in a pane and outlived its server, is ignored, because tmux would
otherwise answer "current" queries with an arbitrary attached client. Outside
tmux, `NW_FLEET` selects a configured fleet when present; otherwise `tview`
enters the default fleet. A positional
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
