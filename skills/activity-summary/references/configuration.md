# Configuration

All entry points accept `--config FILE`. Scheduled entries also recognize
`ACTIVITY_SUMMARY_CONFIG`; their default is
`~/.config/activity-summary/config.json`. Paths accept `~`, `$HOME` and `${HOME}`.
Other shell expressions are never evaluated. A command is an argument array,
never shell text. Use an explicit shell executable only for a caller-owned script.

The schema is `activity-summary/v1`. See [example.json](example.json).

| Field | Meaning |
| --- | --- |
| `repository_root` | Absolute path of the main local repository |
| `facts.issue_directory` | Relative directory of `owner_repo/number.md` GitHub mirrors |
| `facts.default_issue_repository` | Explicit `owner/repository` used for legacy number-only records and unresolved bare references |
| `facts.document_directory` | Relative directory with one subdirectory per document mirror |
| `facts.wiki_project_directory` | Relative directory with one subdirectory per project |
| `facts.commit_directories` | Relative Git history path selections; trailing `/` is accepted |
| `facts.summary_directory` | Excluded generated-summary path prefix |
| `facts.project_patterns` | Ordered regexes with the project in their first capture |
| `facts.source_project_labels` | Ordered `[path_prefix, label]` pairs |
| `facts.session_sources` | Explicit `{directory, label, format}` selections; format is `history` or `claw` |
| `facts.gap_minutes` | Session clustering gap, default 45 |
| `facts.commit_kind_patterns` | Optional `[regex, kind]` commit classifications after generic sync/automatic extraction checks |
| `facts.anti_echo_job_name` | Optional case-insensitive own-job marker excluded from human prompts |
| `facts.anti_echo_summary_path` | Optional exact own-output path marker excluded with a `scan` instruction |
| `facts.machine_prompt_patterns` | Additional explicit regex exclusions |
| `daily.output_directory`, `weekly.output_directory` | Relative output directories; filenames are `YYYY-MM-DD.md` |
| `daily.prompt_template`, `weekly.prompt_template` | Explicit template file, absolute or relative to the active repository |
| `publisher_command` | External public repository publisher executable and fixed arguments |
| `environment` | Explicit additions to the publisher/private-policy environment |

Missing source directories contribute no records, matching incremental archives
that have not received any selected records yet. The configured root and path
boundaries must still be valid. Git command errors fail extraction rather than
producing an empty fact set. GitHub activity requires a target-date source
timestamp in the current mirror or a historical Git blob. Commit/import times
alone are not evidence of upstream activity.

Issue titles are intentionally retained in the fact payload; a later title edit
can therefore change an older daily hash. Later counters, latest-update times,
state changes and appended session days do not enter an earlier day's facts.
Session sources preserve the configured labels and understand timestamped
`history` messages and date-headed `claw` records. Duplicate flat/bucket files
use the larger copy, then the deeper path on ties.

## Editorial policy

`daily.validation` accepts `required_headings`, `frontmatter`, `title_heading`,
`min_chars` (default 800), `commentary_heading`,
`commentary_first_line_pattern` and `agent_work_pattern`. The latter captures the
Agent-work section body in group one. `frontmatter` and `title_heading` use
`{target}`, `{start}`, `{end}` and `{input_hash}` scalar substitutions.

`daily.issue_section` accepts `heading`, `facts_heading`, `agent_heading`,
`agent_heading_pattern`, `projects_heading`, `external_reference_replacement`,
`empty_agent_template` and `agent_summary_template`. Agent templates can use
`{human_count}`, `{session_count}`, `{prompt_count}` and `{machine_count}`.
The renderer installs the exact ordered source-proven issue set and missing
human interaction times. It preserves existing prose while normalizing headings
and removing issue identities outside the selected set from narrative sections.

`weekly.validation` accepts `required_headings`, `frontmatter`, `min_chars`,
`commentary_heading`, `commentary_replacement` and `missing_label`. Weekly issue
references must appear in the selected daily inputs; commentary excludes raw
identities. Missing dates must match frontmatter and be acknowledged in prose.

Default language and headings are generic English. Put personal editorial rules
and business prompts in private files and configuration.

Templates use one-pass `{{root}}`, `{{target}}`, `{{start}}`, `{{end}}`,
`{{generation_date}}`, `{{relative}}`, `{{input_hash}}`, `{{missing_csv}}` and
`{{inputs}}` substitutions. Ordinary JSON braces remain literal. Inserted source
text is never interpreted as template syntax. A template must contain both
`{{inputs}}` and `{{input_hash}}`. It is responsible for describing the configured
document structure and treating source records as data rather than instructions.

## Schedule fields

Each `daily.schedule` or `weekly.schedule` has:

- `worktree`: persistent, separate linked worktree path; `task_branch`: its branch.
- `lock`: nonblocking per-job lock outside both repository directories.
- `model_command`: the complete CLI argument array including model, fallback,
  effort, per-call budget, structured output and read-only tools.
- `auth_command`: a read-only account-status command returning JSON with
  `loggedIn: true`. Defaults: three attempts on command failure, 20 seconds
  between attempts, 60-second timeout. Override `auth_attempts`,
  `auth_retry_seconds` and `auth_timeout_seconds` as needed.
- `environment`: the entire model/auth subprocess environment. It is isolated
  from the launcher's environment, including unrelated API credentials. Provide
  the desired `HOME`, `PATH`, locale and account-specific settings explicitly.
- `timeout_seconds`: per-model-call timeout, default 2700. Timeout sends the
  process group TERM, then KILL if it does not exit within 30 seconds.
- `failure_directory`: optional external private directory for failed responses
  and invalid candidates; artifacts have mode 0600. Doctor and dry run create no
  files there. Without this setting, raw failure contents are not retained.
- `publication`: `owned_paths`, `subject`, optional `remote` (origin), `branch`
  (main), `agent` and external `publish_lock`.
- `policy`: `validate_command`, `commit_command` and `message_command` arrays.
  Older `recover_command` entries are ignored. See [scheduling](scheduling.md)
  for the policy contracts and retained-output behavior.

`daily.selection` defaults to `lookback_days: 14`, `repair_days: 3`, `max_dates: 3`.
`weekly.wait_inputs_seconds` defaults to 1500. None of these settings installs a
cron schedule or changes a remote account.
