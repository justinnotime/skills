# Private configuration

Install the external CLI with `npm install -g @genspark/cli` as documented by
the [published package](https://www.npmjs.com/package/%40genspark/cli).
Its authentication and connected accounts stay in caller-owned CLI settings.
The archive programs only execute read operations and never inspect that
credential file. A custom command prefix can select another CLI installation or
an explicit compatible adapter; arguments are passed without a shell.

```json
{
  "schema": "genspark-archive/v1",
  "repository_root": "~/documents",
  "command": ["gsk"],
  "timeout": 120,
  "rate_delay": 1,
  "emails": {
    "account": "reader@example.invalid",
    "output_directory": "archive/email",
    "state_file": "~/.local/state/document-archive/emails.json",
    "folders": ["inbox", "sent"],
    "lookback_days": 7,
    "page_size": 50
  },
  "calendar": {
    "account": "reader@example.invalid",
    "output_directory": "archive/calendar",
    "days_back": 90,
    "days_forward": 90,
    "list_limit": 1000
  },
  "meetings": {
    "output_directory": "archive/meetings",
    "state_file": "~/.local/state/document-archive/meetings.json",
    "page_size": 50,
    "give_up_days": 3
  }
}
```

Only sections that will be used are required. Email and calendar require an
explicit connected account. Meetings use the selected CLI identity because that
service interface has no account-selection argument. Paths and command arguments
expand HOME, `~` and environment variables. Output directories stay under
`repository_root`; `--root` redirects output to an existing transaction worktree.
State must be outside that repository and separate from the configuration file.
`--state-file` supplies a publisher's staged copy. Preserve existing archive
files and progress together during migration.

```bash
scripts/sync-emails --config /private/archive.json --doctor
scripts/sync-calendar --config /private/archive.json --dry-run
scripts/sync-emails --config /private/archive.json --root /private/worktree \
  --state-file /private/staged/emails.json --after 2026-01-01 --before 2026-01-08
scripts/sync-calendar --config /private/archive.json --root /private/worktree \
  --days-back 30 --days-forward 30
scripts/sync-meetings --config /private/archive.json --root /private/worktree \
  --state-file /private/staged/meetings.json --page-size 50
```

`GENSPARK_ARCHIVE_CONFIG` can supply the configuration path. No default repository,
account, output location or credential is inferred from this package's checkout.

## Coverage and archive compatibility

Outlook email uses the selected date interval and folders, follows the existing
`outlook list_folders` and `outlook list_emails` cursors, and receives structured
full text without a separate read or summarization call. Folder configuration
accepts exact folder IDs or uniquely matching display names; `inbox` and `sent`
retain their established aliases. Unknown or ambiguous folders fail instead of
broadening selection. Each page's coverage and the final completion flag are
checked. Dropped records and repeated cursors prevent publication. A message
explicitly marked `preview` or `missing` is retained in the external state's
`pending_bodies` with its retry start date; it is neither written as a full
archive nor added to `synced_ids`. Complete messages still publish. These
runs return zero with `WARN` and the unresolved IDs, while `last_sync`,
`last_after` and `last_before` remain at the last fully complete collection.
Malformed records, failed listings and write failures still return nonzero
without advancing state. No additional prose-reading or model call is made.

Without an explicit `--after`, the query includes the previous complete
interval's last day and every pending retry start date. This prevents a long
outage or unresolved body from falling out of the configured lookback. A
pending ID is cleared only after a full body is archived (or an existing
full archive is confirmed), not merely because it disappears from a listing.
Explicit date and folder overrides still control the requested collection;
they do not discard unresolved IDs outside that selection.

`--skip-read` is an explicit metadata-only operation. Email files retain their
date, subject slug and shortened immutable-ID hash in month directories; existing
`synced_ids` state is supported. Missing archived files cause their remembered
IDs to be fetched again when they occur in the selected date range.

Calendar requests its explicit limit and writes `YYYY-QN-events.md` after
validation. It refuses a response reaching the limit or marked truncated by the service. This is a rolling-range
snapshot stored under the current quarter's label, not a promise to cover every
event in that quarter. The service returns description and participant previews rather than complete
calendar fields. The archive preserves those available fields, including the
participant total and preview note; it cannot reconstruct omitted source text.
Nothing is edited remotely.

Meetings paginate with `page_size` and `continuation_token`, fetch full details,
and retain the established date/title/hash filenames and `synced_ids` state.
Newly created meetings can precede their transcript. Pending records remain
eligible for later reads until their configured waiting period expires; failed
API requests are never marked complete. Existing completed archive files remain
untouched unless their state requires a fresh read. There are no model calls or
new generated summaries.

Errors use diagnostic categories without echoing service responses or credentials.
Collection logs and archive contents may still contain caller-owned records and
must be stored privately. Only synthetic data belongs in package tests or public
examples.
