---
name: google-docs-authority
description: Mirror Google Docs with tabs and original-resolution images, publish configured local sources, obtain separate read/write credentials, render PDF pages, and compare exports or document authority. Includes the complete runtime; accounts, selected documents, paths and publication rules stay in caller-owned configuration. Does not perform bidirectional merging.
---

# Google Docs authority

Read an optional private profile from `GOOGLE_DOCS_AUTHORITY_PROFILE`, or
`${XDG_CONFIG_HOME:-$HOME/.config}/google-docs-authority/profile.md`. It selects
repository policy and existing mirror operations; do not copy its contents into
this package. Missing configuration is a constraint, not a reason to guess an
account or document ID. Follow the source repository's write protocol.

Use [configuration and commands](references/config.md). Install this package's
own dependencies with `uv sync --locked`; command wrappers use
`GOOGLE_DOCS_AUTHORITY_PYTHON` or `python3`. Select an explicit private config
with `--config`, `GOOGLE_DOCS_AUTHORITY_CONFIG`, or the default configuration
path described in the reference.

The package includes mirroring, authorization, rendering and export comparison;
it does not depend on a personal repository's implementation. Mirror with
`scripts/sync --config FILE --root /selected/worktree --state-file /private/staged-state.json`.
Use the caller's transaction publisher when the output belongs in Git. See
[mirror configuration](references/mirror.md) for incremental state, image
preservation, redaction and failure behavior. `--doctor` checks local dependencies;
`--dry-run` lists selected documents. Neither proves live document access.
With private `mirror.status_file` configured, `--status` inspects failure streaks
and recovery independently of publication progress. Repeated 404 responses do
not establish deletion; even an explicit trash report retains the local mirror.

Use [authorization and inspection tools](references/tools.md) to obtain a
user-authorized credential, render PDF pages, or compare native and HTML exports.
Reading a document does not authorize publishing it, changing sharing, replacing
credentials or altering the selected document list. An inaccessible selected
document is an error; preserve its archive and identify the access issue.

Read the registry and exact source `gdoc` metadata before acting. There is one
authoritative side:

- `published`: the local source is authoritative. The publisher checks the last
  recorded live text fingerprint before replacing Google content.
- `mirror`: Google is authoritative. Read the caller's existing mirror; never
  publish that mirror back to Google.
- `handed-off`: Google became authoritative; the local source is frozen. Do not
  republish it. Handoff requires the caller's explicit source/mirror procedure;
  this package does not automate it.

Start a publication review with `scripts/publish source.md --dry-run`; this
reads local inputs only. To update an existing document, pass its exact ID as
`--update DOCUMENT_ID`. An actual publication needs the user's authorization to
write that document. An inspection or sync request does not authorize discarding
online edits. Use `--force` only when the user has explicitly chosen that result.
It still requires a readable live export and cannot bypass document identity or
authority checks.

After an authorized upload, the publisher re-exports the live document and
records its fingerprint only when it matches the source. If a failure occurs
after upload, Google may already have changed. Use the reported document ID to
inspect the outcome before retrying; never blindly create another document.

The fingerprint compares normalized text. It excludes presentation, image
bytes, link destinations, whitespace and some punctuation. Equality is not
proof that images, formatting or every semantic distinction are unchanged.
Use `scripts/fingerprint source.md` and the shared Python API described in the
reference; do not create a second normalization algorithm.

Regenerate the derived registry with `scripts/registry --write`, then verify
with `scripts/registry --check`. These commands read configured local records
and make no Google requests. Commit source metadata and registry changes through
the caller's normal process. Report the exact document, authoritative side,
whether Google changed, verification outcome and remaining action.
