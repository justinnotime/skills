---
name: agent-session-extraction
description: Extract, audit, reconcile, stage, and analyze token usage and activity in agent-harness sessions through the deterministic manifest-driven runtime. Use for Claude Code, Codex, OpenCode, DeepSeek Harness, Cursor, or OpenClaw session archives; do not use it to infer access from Backup profile labels or to switch a production writer without separate authorization.
---

# Agent Session Extraction

Use the scripts in this package as the behavior authority. A scheduler calls
the scripts directly; it does not invoke this Skill conversationally.

Read an optional consumer profile from `AGENT_SESSION_EXTRACTION_PROFILE` or
`${XDG_CONFIG_HOME:-$HOME/.config}/agent-session-extraction/profile.md` when
present. It can locate private manifests, publication policy, and deployment
commands. It does not replace the manifest or grant access to unconfigured
sources; current user instructions take precedence.

This directory is the complete runtime root: `src/agent_skills`, `tests/`,
`pyproject.toml`, and `uv.lock` belong to this package. It can be copied and
installed independently. A consumer importing the Python API adds this
directory's `src/` to its Python path; a configured runtime-root value names
this Skill directory, not the containing collection repository.

From this directory, install test dependencies and validate with:

```bash
uv sync --locked --extra test
uv run --no-sync pytest tests
```

Before reading a source, require a valid versioned manifest. Treat source
paths, node labels, ownership, project policy, cleanup authority, redaction,
and publication as explicit configuration. Never infer a consumer realm,
machine identity, or read authority from a hostname, current directory, or
Backup profile label.

For shared input roots, configure explicit `discovery.directories` in the
[source manifest](references/manifest.md). This selects literal project subtrees
before traversal. Test a future matching file in an unselected sibling; a passing
extraction with only currently selected files does not prove isolation.

## Operations

- Use `scripts/run --config PATH` for scheduled extraction with an explicitly
  configured external publisher. Its `--doctor` and `--dry-run` modes do not
  invoke that publisher. Both externally prepared and runtime-prepared encrypted
  worktrees are supported; configuration selects the existing manifest strategy. See [scheduled extraction](references/scheduled.md)
  when configuring or migrating a scheduler entry point.
- Use `scripts/analyze-usage --manifest PATH --start TIMESTAMP --end TIMESTAMP
  --output NEW_PRIVATE_DIRECTORY [--config PRIVATE_JSON]` for token categories,
  trigger origins, action candidates and configured tariff comparisons. Read
  [usage analysis](references/usage-analysis.md) first: it deliberately observes
  records before conversation retention, writes private evidence, and cannot
  establish complete invoices or causal savings. It makes no model calls and
  does not run the archive publisher.
- Use `scripts/summarize-usage --input LOCAL_USAGE_JSONL_GZ --output
  NEW_LOCAL_DIRECTORY` to recompute descriptive statistics without rereading
  session logs. Both analysis commands refuse output inside Git repositories.
  Keep raw evidence on local disk and use the [report prompt](references/analysis-report-prompt.md)
  for reviewed aggregate synthesis; generated output is never public by default.
- Run `scripts/doctor --manifest PATH` to check configuration, source-path
  policy, decoder availability, and redaction self-tests without decoding
  transcript text.
- Run `scripts/extract --manifest PATH --dry-run` for the complete read,
  decode, policy, render, redaction, audit, cleanup-plan, and reconciliation
  path with no persistent source/output, Git, marker, or cleanup mutation.
- Run `scripts/reconcile --manifest PATH [--failure-marker PATH]` to compare
  accepted source sessions with preserved and planned output. Its report may
  contain source/session identifiers and counts, never transcript text.
- Run `scripts/extract --manifest PATH` only when publication to the manifest's
  owned subtree is authorized. `git-worktree` publication also requires an
  explicit `--prepare-worktree PATH`; it stages but never commits or pushes.
  `--output-root ABSOLUTE_PATH` overrides only the manifest's output repository
  for that invocation, so a consumer can target its authorized disposable
  checkout without rewriting source policy or the manifest on disk.

For `git-crypt` publication, the key is linked into the throwaway worktree and
is never copied. Its target is relative to that worktree's private Git
directory: `git-crypt/keys/default` requires the `git-crypt` filter, while
`git-crypt/keys/<name>` requires `git-crypt-<name>`. The publisher creates a
no-checkout worktree, populates only its private index, validates cached
attributes for every tracked owned file and planned write, and links the key
before checkout can invoke a smudge filter. After checkout it refuses
ciphertext, and after staging every planned write's index blob must carry the
git-crypt ciphertext header without its planned plaintext. Any failure removes
the throwaway worktree and leaves the source repository untouched.

Because a throwaway worktree starts from `HEAD`, `git-worktree` publication
requires every owned output subtree to match `HEAD` immediately before and
after inventory is read. The same check runs again immediately before worktree
preparation. Tracked changes, untracked files, ignored files,
`skip-worktree`, and `assume-unchanged` state inside those subtrees fail
closed. Repository dirt outside the owned subtrees remains allowed.

Do not place real manifests, private patterns, account labels, host maps, or
source paths in this shared package. Keep them with the consumer that owns
them. A reconciliation or required-source failure blocks cleanup and
publication.

Use `sqlite-readonly` for a live or WAL-backed OpenCode database. Use
`sqlite-immutable` only for a checkpointed snapshot whose producer guarantees
immutability. Configure per-harness history directories and filename
strategies in the manifest rather than adding consumer-specific render paths.
Omit `output.prompt_directory` or set it to `null` for a history-only writer;
only the configured history directories are scanned, written and indexed.
For legacy OpenClaw files, explicitly configure ownership, original naming,
selected metadata, and static-file preservation as described in
[the manifest reference](references/manifest.md#adopting-legacy-openclaw-output).
Use a legacy compatibility rule only while adopting existing output; it does
not relax the contract for newly written output. The frozen legacy rule preserves
recognized old prompt session files byte-for-byte and keeps every recognized
legacy file out of cleanup; old history files are rendered again when their
session grows, and indexes continue to be rebuilt from the complete preserved
inventory. Remove that rule explicitly when dependent prompt readers are ready
for normal rewrite and cleanup.

Parity adapters that already hold caller-frozen byte snapshots may import
`decode_source_snapshots` from `agent_skills.sessions.api`. It applies the
shared decoder, policy, normalization, and deduplication contract without
source discovery, rendering, cleanup, or publication. It rejects direct-path
snapshots; production extraction remains responsible for path validation and
source revalidation.

## Contracts

Read [references/manifest.md](references/manifest.md) when creating or
reviewing a consumer manifest. Validate it against
[schemas/manifest-v1.json](schemas/manifest-v1.json).

Read [references/normalized-session.md](references/normalized-session.md)
when integrating a new decoder, shadow comparison, renderer, or indexer.
