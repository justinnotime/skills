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

`orc fleet create NAME` creates an isolated local fleet and a tmux session.
`orc --fleet NAME ...`, `agent-bus --fleet NAME ...`, and `tview --fleet NAME`
select that same fleet. Named fleet profiles own separate database paths and
terminal servers; local profiles are bound to the host that created them.

### Finding and entering fleets

```bash
tview --list
tview --list --json
tview --fleet default
tview --fleet example 3
tview 3
```

The list derives fleet names and primary sessions from the default configuration
and existing named profiles, then queries those tmux servers. It distinguishes
an online primary, an offline server, a missing primary session, and invalid or
unavailable configuration. It never starts a server or creates a session.
Availability here means tmux availability, not agent responsiveness or task
completion. Grouped terminal views are not additional fleets.

Set `fleets.default_name` in the private runtime configuration to give the
existing default fleet a recognizable alias. Both that alias and `default`
select the same fleet in `tview`, `orc` and `agent-bus`; no database or message
transport is moved. `tmux.primary_session` selects its primary session, with
`0` as the compatible default. Named profiles already record their server and
primary session, so no second mapping file is needed.

An explicit `--fleet` always selects that target. Inside tmux, an unselected
`tview` identifies the fleet from the actual socket and exact primary session
or its session group, ignoring a stale `NW_FLEET`. An unrelated session is not
assigned to a fleet by guessing its name: the command displays the list and
requires an explicit selection. Outside tmux, `NW_FLEET` selects a configured
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
