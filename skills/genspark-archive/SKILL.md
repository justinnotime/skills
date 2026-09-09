---
name: genspark-archive
description: Archive Outlook email, calendar events and meeting transcripts as local Markdown through a caller-configured Genspark CLI account. Use for incremental collection and local archive inspection; does not send messages, edit remote records or generate summaries.
---

# Genspark archives

This package owns the complete email, calendar and meeting archive programs,
including Markdown conversion, file naming, pagination and incremental state.
It makes no model calls. The external Genspark CLI owns service access and
credentials; install the published `@genspark/cli` and use an already configured
account. Never copy credentials into this package.

Read [configuration and operation](references/configuration.md) for the private
JSON schema, executable interfaces, preserved archive formats and coverage
limits. Use the caller's selected accounts and date ranges. Reading access does
not authorize sending email, changing calendar events or editing meetings.

Install this package's locked dependencies with `uv sync --locked`, then use
`uv run --locked genspark-sync-emails --config /private/archive.json --doctor`
or the matching `genspark-sync-calendar` / `genspark-sync-meetings` entry.
The `scripts/sync-*` commands use `GENSPARK_ARCHIVE_PYTHON` or `python3`; that
interpreter must have the declared package dependencies. Each command supports
`--doctor` and `--dry-run` without network requests or data writes.

For collection, select a caller-owned worktree with `--root` and a staged
external `--state-file` for email or meeting progress. State advances only after
all selected pages validate and the planned files are written. Email bodies
explicitly marked incomplete remain in `pending_bodies`; other full messages
can be published. Such a run exits zero with `WARN`, and leaves `last_sync`
unchanged until every pending body is complete. A failed operation can leave partial
local output, which must not be published. Transactional Git publication and
schedule management belong to the caller.

Do not treat a service error or a full capped result as an empty or complete
archive. Outlook email and meeting collection follow explicit continuation tokens and
refuse incomplete pagination. Calendar collection fails when the event listing
is truncated or reaches its requested limit; narrow the selected dates or adjust
the configured limit within the service's supported range.

The default email window includes the previous complete interval and pending
body retry dates, so outages and unresolved bodies cannot silently age out of
the configured lookback. Pending bodies that disappear from a listing stay
pending; absence is not proof of a complete archive.

Email attachment metadata and source-provided meeting notes are preserved, but no
attachment download or AI summary is requested. Explicit `--skip-read` writes
email metadata without marking those records as fully archived, allowing a later
normal run to fetch their full text. Calendar output replaces the current
quarter's snapshot only after a complete event listing. The service only returns
calendar description and participant previews; this is not a lossless export of
all calendar fields. Historical archive
files are not deleted when records disappear from service listings.

Verify changes from this package with `uv run --locked pytest tests -q`,
`uv run --locked ruff check .` and `uv run --locked agentskills validate "$PWD"`.
