# Runtime configuration

Use `orc --config FILE ...` and `agent-bus --config FILE ...`, or set
`FLEET_ORCHESTRATOR_CONFIG` for every selected entry point. The default file is
`${XDG_CONFIG_HOME:-$HOME/.config}/fleet-orchestrator/config.json`. An absent
default file enables standalone local defaults; an explicitly selected missing
or invalid file is an error. Duplicate JSON fields are rejected.

Minimal configuration:

```json
{
  "schema": "fleet-runtime/v1",
  "runtime_dir": "~/state/example-fleet",
  "bus": {
    "transport": "local",
    "config_directory": "~/state/example-fleet/bus",
    "database": "~/state/example-fleet/bus/inbox.sqlite3"
  },
  "paths": {
    "ledger": "~/state/example-fleet/tasks.sqlite3"
  }
}
```

Paths accept `~`, `$HOME`, and other explicitly provided environment variables.
Commands are JSON argument arrays, never interpolated shell programs. Use
separate private scripts when a caller needs shell behavior.

Optional fields:

| Field | Purpose |
|---|---|
| `canonical_source_root` | Formal package installation permitted to write protected databases |
| `protected_databases`, `protected_named_database_roots` | Explicit production database identities and named-fleet roots protected from development copies |
| `paths.orchestrator_state`, `paths.lock_directory`, `paths.lock_prefix` | Runtime observations, snapshots, and default-fleet lock locations; an optional filename prefix preserves an existing lock identity |
| `paths.legacy_drive_state` | Source directory for an explicit legacy-state import |
| `fleets.profile_directory`, `fleets.runtime_directory`, `fleets.matrix_config_directory` | Optional explicit profiles and separate session task/message storage roots; ordinary local sessions need no profile |
| `fleets.default_name` | Optional alias for the existing default fleet; defaults to `default`, without creating a profile or changing storage |
| `tmux.server_file` | Optional terminal server selector |
| `tmux.primary_session` | Default fleet's exact primary session name; defaults to `0` |
| `matrix.homeserver`, `matrix.room`, `matrix.registry_room`, `matrix.token_file` | Required caller-selected Matrix service, distinct rooms, and private authorization-header file |
| `bus.event_namespace` | Matrix event namespace; preserve it when upgrading an existing transport |
| `bus.dispatcher_template`, `bus.named_dispatcher_template` | Caller-owned service template for an explicitly requested Matrix dispatcher install |
| `authority.merge_keys` | Repository-to-responsible-role mapping; unspecified repositories route to the operator |
| `authority.service_handle`, `authority.receipt_instructions` | Caller identity and review instructions |
| `github.owner`, `github.sanctioned_logins_file` | GitHub selection and recognized review authors for already registered tasks |
| `github.whole_repositories`, `github.mixed_repositories`, `github.path_substrings` | Review-inspection scope |
| `github.automatic_review_markers` | Comments excluded from substantive review evidence |
| `watched_repositories` | List of objects containing `path`, `kind` (`checkout` or `bare-hub`), and optional `exempt` paths |
| `watcher_exceptions_file`, `bus.watcher_exceptions` | Caller-approved watcher exceptions for task and transport inspection |
| `turn_report.enabled` | `true` reports the caller's active registration in the selected fleet without an enrollment list; `false` disables reporting; omitted retains explicit list enrollment |
| `turn_report.seats_file` | JSON array of enrolled identity IDs, used only when `turn_report.enabled` is omitted; no file means no reporting |
| `seat_trailer` | Explicit ledger, member command, window vocabulary, host selector and Git trailer key; see [commit attribution](commit-attribution.md) |
| `commands.brief` | Optional caller-owned startup briefing command |
| `handoff.directory`, `handoff.publish_command` | Local handoff storage and optional external publication |
| `rollout.manifest`, `rollout.source_root`, `rollout.canonical_root`, `rollout.skill_sources` | Artifact manifest, source checkouts, and explicit Skill-name-to-directory mapping |
| `paths.topology`, `backup.config`, `backup.syncthing_configs`, `backup.folder_label` | Optional topology audit inputs |

Existing `NW_*`, `AGENT_BUS_*`, `MATRIX_BUS_*`, `NOTES_RUNTIME_DIR`, and
`DISPATCH_LEDGER_DB` overrides remain accepted for installed callers. They do
not require a particular repository. Named profile commands apply their complete
environment before importing runtime code; keep the same selector throughout
an operation.

Turn reporting remains opt-in. To stop maintaining a list of identity IDs that
changes on re-registration, explicitly set `"turn_report": {"enabled": true}`.
This takes precedence over `seats_file` and the compatible `NW_TURN_CANARY_FILE`
override; it requires an active, unexpired Agent Bus registration, not just an
`ORC_SEAT_ID` value. Leave `enabled` absent to retain existing list-based policy.
`NW_TURN_REPORT_OFF=1` disables every mode. Neither installing hooks nor a missing
enrollment file enables reporting. These are same-user reporting preferences,
not a security boundary or proof that an agent is responsive.

PRs must be registered explicitly in their owning fleet. The former
`github.owner_defaults_file`, `github.excluded_title_prefixes`,
`github.excluded_branch_prefixes`, and `github.owner_branch_pattern` settings
are unused and can be removed. Merge authority and the read-only review
inspection scope do not create tasks.

The default alias uses the same name syntax as named fleets and cannot collide
with a named profile. Both `--fleet default` and `--fleet <default_name>` select
the original default configuration, including when leaving an inherited named
fleet environment. Do not create another named profile to label an existing
default fleet: a named profile selects separate databases and transport state.
Terminal selection uses the configured default server selector, or the explicit
tmux socket name `default` when no selector exists. `NW_DEFAULT_TMUX_SERVER`
can select that server independently of a named fleet's environment.

Selecting `AGENT_BUS_CFG` or `MATRIX_BUS_CFG` also selects that directory's
`agent-bus-v3.sqlite3` and `auth.hdr`; an explicit `AGENT_BUS_DB` still takes
precedence. This keeps named-fleet credentials and state separate from default
configuration. Named-fleet locks live in `cache/locks` under the selected
fleet runtime, without the default fleet's optional lock prefix.

For a handoff publisher, ORC supplies `ORC_HANDOFF_SRC`, `ORC_HANDOFF_DST`
(a basename), `ORC_HANDOFF_DIRECTORY`, `ORC_HANDOFF_SUBJECT`, and
`ORC_HANDOFF_AGENT`. The command must finish successfully before ORC retires the
identity. Without it, ORC writes an atomic local handoff. An external publisher
is only needed when the caller requires publication beyond local storage.
Automatic local fleets store handoffs under their own runtime's
`state/fleet-orchestrator/handoffs`; they do not inherit `handoff.directory` or
`handoff.publish_command` from the default configuration. Topology and onboarding
read that same fleet-local directory. The default fleet and explicitly configured
legacy fleets retain their configured handoff directory and publisher.

A runtime configuration can name privileged commands and real identities.
Keep it private and owner-writable, review changes, and do not import an
untrusted peer's configuration. Do not include credential contents in the file.

Artifact entries may set `source_roots` with absolute `working` and `canonical`
paths when caller-owned deployment files live outside the public package.
Staging reads the working source; installation requires matching published
content from the canonical source. Both roots remain private configuration.

## Native configuration and installation observations

A caller can maintain `fleet-runtime/v1` directly in a private repository and
link it to the selected configuration path. `${CONFIG_DIR}` refers to the actual
source file's directory after following links. HOME, XDG config/state/cache
paths and explicit environment references remain caller-owned. A value may use
`{"env":"EXAMPLE_ROOT","default":"~/example","suffix":"/file.json"}`;
the optional slash-prefixed suffix is joined and normalized. A nested default
can select another environment value. This only selects values and never runs
configuration code.

For rollout observations, `rollout.skill_sources_command` optionally selects an
explicit read-only argument vector that prints the existing name-to-source JSON
object. This removes the need to store a second copy of a discovery installer's
package selection. It takes precedence over `rollout.skill_sources`; a failed
command or invalid/missing source fails the observation instead of returning an
empty inventory. No command is discovered or inferred from a repository.

A rollout artifact may provide `command_argv` and optional
`command_environment` instead of a shell-quoted `command`. The reader resolves
native configuration values and quotes the argument vector before the existing
hook adapters inspect/install it. The schema accepts native environment values
in command arguments, environment selections and source roots.
An artifact may use `cron_exact_line_command` instead of `cron_exact_line` to
query a caller-selected read-only command. Its output must be exactly one
nonempty line. Failure is `UNKNOWN`; the observer does not substitute a guessed
schedule. These queries do not grant permission to run jobs, restart services,
or change hook trust.
