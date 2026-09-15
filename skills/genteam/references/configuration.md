# Configuration and publication

All commands share one JSON document with `schema: genteam/v1`. Paths expand
`~`, environment variables and `{home}`; relative paths use the configuration
file's directory. No endpoint, account, channel, cookie or personal marker is
embedded in the runtime. The site origin must use HTTPS, except local HTTP
fixture servers. `cookie_name` defaults to `session_id`; the cookie file holds
the raw value, never a browser-cookie export.

| Field | Meaning |
| --- | --- |
| `base_url`, `cookie_file` | Required site origin and cookie path |
| `archive.output_directory`, `archive.state_file` | Required for direct archiving |
| `archive.repository_path` | Relative owned subtree for transactional publication |
| `archive.selection` | `enabled` defaults false; `mode` is `whitelist`, `blacklist`, or `pinned` |
| `archive.selection.chats` | Objects containing `match` and optional output `alias` |
| `archive.selection.bootstrap_days` | Initial history window, default 90 days |
| `archive.selection.threads` | Include channel threads, default false |
| `archive.rate_delay` | Delay between pages/channels, default 0.5 seconds |
| `archive.max_pages_per_run` | Per-channel page cap, default 40; backlog cursor persists |
| `archive.missing_cookie` | `fail` by default; caller may explicitly choose `skip` |
| `send.state_directory` | Proposal, pending-confirmation and audit location; default XDG state |
| `send.audit_file` | Optional override, default `genteam-send.log` in sender state |
| `send.marker` | Optional caller-owned marker, default empty |
| `send.audit_text_prefix_length` | Optional retained text prefix length, default zero |
| `send.proposal_ttl_seconds` | Local proposal lifetime, default 3600 seconds |
| `send.require_tty` | Optional interactive-only policy for direct sends, default false |
| `publisher.command` | External publisher argument array, required for `--publish` |

Whitelist and blacklist modes match channel labels by case-insensitive substring.
Pinned mode instead uses `viewer.pinned_channel_ids` from each workspace's
`/servers/resolve` response, matching the authenticated user's sidebar pins
(including any service-provided default pins). It does not use `chats` entries.
Pins are read anew on every run, for both group conversations and DMs. A missing
or malformed list fails before any messages are fetched; an empty list selects
nothing. Unpinned archives and progress are retained. Re-pinning resumes from
the saved message ID; a never-synced conversation uses `bootstrap_days`.
`--list-selected` shows the currently selected conversations without writing
archive files or progress. `threads` continues to control fetching replies
within selected conversations; sidebar-pinned threads are not separate selections.

`--yes` is the explicit direct-send switch. A caller that sets
`send.require_tty: true` may use `GENTEAM_SEND_NO_TTY_OK=1` for an independently
authorized unattended send. Queued approval always prompts in a real terminal.
Changing configuration is not recipient authorization.

## External publisher contract

`--publish` appends `--` and the exact archive writer arguments to
`publisher.command`. If the array already ends in `--`, no second separator is
added. An optional exact `{command}` array element is replaced by writer argv.
The writer receives the absolute configuration path and does not recurse into
publication.

For the independently installed `repository-publish` CLI, a synthetic command
could be:

```json
[
  "/opt/example-skills/repository-publish/scripts/publish",
  "--repo", "~/example-archive",
  "--task", "example-chat",
  "--paths", "archive/example-chat",
  "--subject", "sync: example chat",
  "--state-dir", "~/.local/state/example-chat/published",
  "--lock", "~/.local/state/example-chat/archive.lock"
]
```

The archive writer reads `REPOSITORY_PUBLISH_WORKTREE` and writes only the
configured `archive.repository_path` below it. `REPOSITORY_PUBLISH_STATE`
(or compatibility `SYNC_STATE_DIR`) supplies staged progress; the configured
state filename is retained. Credentials keep their original path. Publisher
failure is returned unchanged and progress is promoted only by that publisher.
No sibling package source is imported, and callers can use another publisher
implementing the same process contract.

Direct archive retries recognize message markers already written in monthly
files. Existing monthly files are appended without rewriting historical text.
The progress schema retains per-channel `newest_id`, `alias` and `last_sync`;
large initial histories also retain `bootstrap_before_id` and
`bootstrap_cutoff` until the backlog is complete. Do not delete existing state
when switching scheduled writers.

A partially accepted send stores only the plan and accepted message identifier,
not the short-lived transport token, under `pending-intercepts/`. Retry that
confirmation with `recover ID --yes`; CometChat is not called again. A transport
failure without a returned message ID remains uncertain and must be inspected
at the destination before retrying.
