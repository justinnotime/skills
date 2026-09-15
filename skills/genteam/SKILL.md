---
name: genteam
description: Archive, inspect, and send GenTeam channel or thread messages using an explicitly configured account. Includes cookie validation, incremental Markdown archives, preview-first sending, and local proposals. Reading or drafting does not authorize delivery.
---

Use the included Python 3.10+ commands with caller-owned JSON configuration.
The complete runtime and tests are in this package; no private repository or
other Skill source is imported. The archive and authentication commands use the
GenTeam REST API. Sending obtains channel authorization from that API, sends
through CometChat, and confirms the resulting message with GenTeam.

## Configuration

Select `--config FILE`, `GENTEAM_CONFIG`, or
`${XDG_CONFIG_HOME:-$HOME/.config}/genteam/config.json`. The caller provides the
site origin, cookie location, channel selection, output paths and optional
publisher command. Never put a cookie value in an argument, example or report.
See [configuration](references/configuration.md) and the
[synthetic example](references/config.example.json).

## Read or archive

```bash
scripts/auth --config /private/genteam.json --check
scripts/sync --config /private/genteam.json --list-channels
scripts/sync --config /private/genteam.json --list-selected
scripts/sync --config /private/genteam.json --peek '<channel match>'
scripts/sync --config /private/genteam.json --threads '<channel match>'
scripts/sync --config /private/genteam.json --peek-thread '<thread id>'
scripts/sync --config /private/genteam.json
scripts/sync --config /private/genteam.json --publish
```

Listing and peeking do not write archive progress. Archiving writes mechanical
monthly Markdown and JSON progress; it makes no model calls. Attachments remain
source links, not downloaded files. Selected channel/thread failures return
failure without saving advanced progress. Bootstrap pagination retains its
cursor across runs. Use the configured publisher for scheduled repository
writes so output and progress become durable together.

Set `archive.selection.mode` to `pinned` to archive only the authenticated
user's sidebar-pinned conversations. Each run reads the current pin IDs; no
static list is needed. Missing pin metadata fails the run, and an empty list
selects nothing. Unpinning stops future reads while retaining existing archives
and progress. `--list-selected` previews the same selection without archiving.

## Send only when authorized

Resolve the requested recipient and preview the exact text before an explicitly
authorized delivery. The default command only previews; adding `--yes` sends.
A preview or proposal for a reply does not create a thread.

```bash
scripts/send --config /private/genteam.json channels '<match>'
scripts/send --config /private/genteam.json send --to '<channel id>' --text 'Example text'
scripts/send --config /private/genteam.json send --to '<channel id>' --text 'Example text' --yes
scripts/send --config /private/genteam.json send --to '<channel id>' --reply-to '<message id>' --text 'Reply'
scripts/send --config /private/genteam.json send --thread '<thread id>' --text 'Reply'
```

For a local approval queue use `propose` with the same target/text flags, `list`,
`approve ID`, or `reject ID`. Approval prompts in a terminal. A configured marker
is added only when absent. Audit text is omitted by default.

If chat transport accepts a message but backend confirmation fails, the error
reports its message ID and preserves a pending confirmation locally. Inspect
that message before using `recover ID --yes`; recovery only retries backend
confirmation. Never resend an uncertain message merely because a command failed.

## Cookie setup and checks

`scripts/auth --config FILE` accepts a hidden terminal prompt, or `--stdin`
reads from a caller-authorized secret source. It verifies the account before
atomically storing a mode-0600 cookie. Failed validation preserves the old file.
`--check` verifies without replacing the file. These operations contact the
configured account; installing the package does not log in or send anything.

Run isolated tests with `uv run --group dev pytest` from this package. Tests use
synthetic HTTP responses, temporary files and an optional local fixture server;
they do not contact real accounts.
