# Manifest v1

The schema identifier is `agent-session-extraction-manifest/v1`. The file is
strict JSON: missing and unknown fields are errors. Every source entry must
state `enabled`; no source is selected because a Backup profile happens to
exist.

Native configuration can set `expand_environment: true` to expand `$NAME`,
`${NAME}` and leading `~/` in filesystem fields using the explicitly supplied
environment. This applies to source paths, allowed roots, selected metadata,
output repository and key-link source. Missing or empty variables fail; expansion
does not execute a shell or infer node labels, authority, patterns or source
selection. `$$` represents a literal dollar. Leave the option absent to retain
literal path behavior in an existing manifest. Every expanded path must still
be absolute and pass the same lexical/resolved confinement checks.

Set `require_external_config: true` when the consumer requires its concrete
manifest to be a regular file outside the output repository. The runtime then
rejects repository-local manifest paths and manifest symlinks. Copy and review
such files outside Git; the runtime never installs or rewrites them.

## Source authority

Each `sources[]` entry names an opaque ID, harness, explicit or explicitly
selected native-default path, `owner` or `mirror` authority, required status,
output node, stable-byte or read-only-SQLite snapshot mode, explicit file/glob
discovery, decoder options, and whether an empty source is authoritative.

`root_policy` checks four values separately: configured lexical path,
configured resolved path, every candidate lexical path, and every candidate
resolved path. `symlinks=confined` permits only targets that remain under both
the configured source and allowed resolved roots. `symlinks=reject` refuses
any traversal. Use `forbidden_components` and `required_suffixes` as additional
consumer defenses; they never replace root containment.

The optional `forbidden_component_patterns` array adds case-sensitive shell-style
patterns such as `excluded-*` for each path component. These patterns apply to
configured paths, resolved targets, candidate files and separately selected
metadata, including revalidation before reads. Existing `forbidden_components`
remain exact matches. The consumer owns both lists; the package has no account
or directory classification rules.

`native-default` uses only the explicitly supplied `HOME` value. It is not a
claim that the source belongs to a particular consumer.

DeepSeek Harness sources that may contain the actively appended final frame
must explicitly set `decoder.allow_torn_current_frame=true`. The default is
false, so a truncated completed snapshot fails instead of being treated as an
in-progress file.

Decoder options are strict and harness-specific; unknown names and wrong types
invalidate the manifest. Shared synthetic-prefix and conversational-subagent
retention rules live only in `event_policy` and are injected into decoders by
the runtime.

A Claude source may list historical malformed JSONL records in
`decoder.grandfathered_malformed_line_sha256`. Each entry is the complete
lowercase SHA-256 of the raw record bytes, excluding its line terminator. Only
JSON parse failures with an exact match are ignored. Valid non-object JSON,
unlisted malformed records, invalid hashes, and duplicate hashes still fail
closed. Reports expose only ignored or malformed counts; they never print the
configured hashes, source paths, or record bytes. Keep non-empty lists in the
owning consumer's private manifest, not in the shared repository.

Each source may include an `event_policy` object that overrides any of
`min_direct_user_events`, `min_user_chars`,
`retain_conversational_subagents`, and `retention_mode` for that source only.
Missing values inherit the global policy. With `count-or-long` (the global
default), a session is retained when it meets the direct-user event count or
has a direct-user event at least `min_user_chars` long. `count-only` requires
the event count regardless of message length. This retention decision uses
synthetic-filtered user-like events before peer-agent relabeling.

OpenCode has two explicit read-only modes:

- `sqlite-readonly` validates and reads the database and any WAL sidecar
  without opening the source through SQLite. It rechecks both byte streams,
  recovers committed WAL content through a private mode-0600 temporary copy,
  and removes that copy before returning.
- `sqlite-immutable` is only for a checkpointed snapshot whose producer has
  made it immutable, such as a Backup-produced database. It rejects a
  non-empty WAL or rollback journal, opens the validated database with SQLite
  `mode=ro&immutable=1`, and revalidates its inode, size, modification time,
  and sidecar state after decoding.

A moving database, escaped sidecar, or mode precondition failure makes the
source unreadable; existing output is preserved and cleanup is blocked.

`sources[].discovery.superseded_sha256` may list exact hashes of immutable
historical candidate bytes that an owning consumer has explicitly replaced.
Every matching candidate still passes path validation and a stable read before
it is excluded. The run reports `SOURCE_CANDIDATE_SUPERSEDED` without the hash,
path, or transcript. If those bytes change, the hash no longer matches and the
candidate is decoded normally. Non-empty values are valid only with
`stable-bytes` snapshots and belong in the consumer's private manifest.

| Harness | Optional `decoder` fields |
|---|---|
| Claude Code | `session_id`, `project_hint`, `conversation_kind`, `conversational_subagent_min_user_events`, `grandfathered_malformed_line_sha256` |
| Codex | `session_id`, `project_hint` |
| OpenCode | `minimum_user_events`, `excluded_cwd_prefixes` |
| DeepSeek Harness | `compression`, `allow_torn_current_frame` |
| Cursor | `session_id`, `project_hint`, `minimum_user_events` |
| OpenClaw | session/project hints, `minimum_user_events`, `minimum_total_events`, cron/notification/channel filters, and optional channel/session metadata fields |

OpenClaw can read label/channel metadata from an explicitly configured absolute
`decoder.sessions_metadata_path`. The file uses either a list of objects or an
object containing a `sessions` list; each row has a unique non-empty `id` and
optional string `label` and `channel`. No adjacent file is read by default. The
file must satisfy the source's allowed lexical/resolved roots, forbidden path
components, and symlink policy. `required_suffixes` constrains the main source
root only. A configured metadata file that is missing or invalid fails the
source; its contents are read through the same stable-byte reader as transcripts.
Only label/channel fields are merged into selected session metadata. Enable
`include_session_metadata` with `session_metadata_fields=["timestamp", "label"]`
and `include_channel_metadata` to retain those fields. An explicit `channel`
value may supply the fallback when a record has no channel.

`output.metadata_headers` maps non-reserved output header names to selected
scalar metadata fields, for example `{"Label":"label","Channel":"channel"}`.
Absent values are omitted; multiline values are rejected. Rendered metadata
passes through the same redactor and output audit as the transcript. A selected
metadata change updates the existing session file even if its events are equal.
This option is empty by default.

OpenClaw applies `minimum_user_events` and `minimum_total_events` after its
configured synthetic, operational-notification, and channel-forward filters;
the latter counts the retained user and assistant events together.

## Policies and output

`event_policy.peer_agent_prefixes` and `peer_agent_exact` are caller-owned,
case-sensitive rules applied to trimmed user-like text after retention.
Prefixes match only the beginning of the text; exact rules match the entire
text. They relabel events without discarding history. For an integration that
reserves the literal `[Agent Bus wake]` prefix, add that string to
`peer_agent_prefixes` alongside the caller's other machine prefixes. Do not add
it to `synthetic_prefixes` if the wake must remain in history. These rules are
global to the manifest, not per-source overrides; all declared harnesses must
agree on their meaning. See [input role versus authorship](normalized-session.md#input-role-is-not-authorship)
for quoting behavior and the limit of text-only classification.

- `ownership.mode`: `owner` or `aggregator`.
- `event_policy`: synthetic prefixes, peer-agent rules, retention thresholds,
  and conversational-subagent retention.
- `project_policy`: all/allowlist/denylist, aliases, and unknown handling.
  Optional `resolvers` run in declaration order for their listed source IDs.
  Each selects `cwd`, `source_ref`, or `project_hint` with a Python regular
  expression that must contain a named `project` group. The first non-empty
  match wins before the ordinary project hint/cwd fallback and alias mapping.
  Resolver patterns that encode consumer conventions belong only in the
  consumer manifest.
- `project_policy.prompt_by_harness`: optional per-harness
  all/allowlist/denylist policy for prompt rendering. Its `unknown` behavior
  and lists affect the prompt view only; an excluded prompt still retains its
  history view.
- `output.history_directory_by_harness`: optional strict map from a supported
  harness name to its history directory. Unlisted harnesses use
  `history_directory`; history and prompt directories must remain distinct,
  and every selected directory must be covered by `publisher.owned_subtrees`.
- `output.layout`: `flat` or `monthly`. This is the steady-state destination
  for new output. Move or remove existing files as a separately reviewed,
  one-time consumer operation before enabling the scheduled writer; the
  runtime stores only the steady-state destination.
- `output.filename_strategy`: default deterministic basename strategy.
  `filename_strategy_by_harness` overrides it for named harnesses. Supported
  values are `project-session-suffix`, `session-prefix-8`,
  `session-last-component-prefix-8`, `session-suffix-8`, and
  `node-session-sha256-12`, and `session-date-prefix-8`; collision suffixes are still assigned from the
  complete normalized identity set.
- `output.day_split`: `off` (default), `hybrid`, or `all`. With `hybrid`, a
  session that already has a whole-session history file keeps that file for
  the days it already covers (up to the last day before the current UTC day;
  on the switch-over day the current day's events move out of it, after which
  the file never changes again), and every later day is written as one history
  file and one prompt file per UTC day (`<day>_<name>.md`, header
  `Day: <day>`, identity `<session>@<day>`). A session first seen after the
  switch is per-day from its first day. The cut is at UTC midnight, so a
  day file is final once its day is over even when an agent keeps replying
  without new input; files of past days therefore stop changing while the
  session stays alive. `all` also slices sessions that already have output and is meant for
  a one-time evaluation or migration run, not the scheduled writer. The
  `extract` and `reconcile` commands accept `--day-split` to override the
  manifest for one run.
- `output.compatibility.rule_version`: `legacy-output/v1` grandfathers only
  exact SHA-256 values listed in `unchanged_sha256`.
  `legacy-agent-markdown/v1` instead recognizes the prior agent history,
  prompt, and README shapes, derives their ownership and semantic identity,
  and adopts unchanged content in place without a bulk rewrite. New or
  semantically changed output is rendered under the current contract.
  `legacy-agent-markdown-frozen/v1` additionally keeps recognized legacy
  prompt session files byte-for-byte and excludes both legacy history and
  legacy prompt files from cleanup while a consumer moves dependent prompt
  readers to the current format. A recognized legacy history file is not
  frozen: when its session later grows, the file is rendered again under the
  current contract at its existing path, and an unchanged one is adopted in
  place. New sessions still use the current contract. Managed indexes are
  rebuilt from both preserved and new session files, so index bytes are not
  frozen. Returning to `legacy-agent-markdown/v1` explicitly ends the freeze
  and permits normal prompt rewrite and authority-scoped cleanup. While
  frozen, an old prompt file whose session later grows remains at its
  preserved snapshot; reconciliation proves that an output identity exists
  but does not claim that snapshot is current.
- `cleanup.scope`: `none`, `owner`, or `aggregator`. A node is eligible only
  when every enabled source assigned to that output node succeeded. A source's
  `owner`/`mirror` label describes where bytes came from; it never grants
  cleanup authority by itself.
- `indexes.mode`: `none`, `owner`, `every-node`, or `aggregator-only`. Indexes
  are built from preserved disk inventory plus planned changes.
- `publisher.strategy`: `none`, `filesystem-atomic`, or `git-worktree`.
  Publication and Git staging are limited to `owned_subtrees`. Before a
  `git-worktree` run reads inventory, every owned subtree must match `HEAD`,
  and the check repeats immediately after that read. Tracked changes,
  untracked files, ignored files, `skip-worktree`, and `assume-unchanged` index
  state are all rejected. The same check runs again immediately before
  preparing the worktree. Dirt outside the owned subtrees does not block
  extraction.
- `publisher.encryption=git-crypt`: requires `strategy=git-worktree` and links,
  rather than copies, the configured key into the throwaway worktree. Every
  `key_link.target` is relative to that worktree's private Git directory and
  must be `git-crypt/keys/default` or `git-crypt/keys/<safe-name>`. The default
  target requires the `git-crypt` filter; a safe name begins with an ASCII
  letter or digit and then contains only letters, digits, hyphens, or
  underscores. A named target requires exactly `git-crypt-<safe-name>`. The
  publisher creates the worktree without checkout, loads `HEAD` into its
  private index without filters, validates cached filter attributes for every
  tracked owned file and planned write, then links the key before checkout.
  Checkout must yield plaintext. Every planned write's staged index blob must
  be git-crypt ciphertext without the planned plaintext.
  Missing attributes, mismatched key names, inactive filters, ciphertext
  checkouts, and plaintext index blobs fail publication and remove the
  throwaway worktree.
- `gates`: required source behavior plus mandatory redaction, output audit,
  reconciliation, and pre-publication scan controls.

### Reclassifying existing archives

A peer-rule change is applied again to source records on each extraction; it
does not require new source bytes or a policy-version cache. Managed history
and prompt files are rendered again as needed at their existing paths, even
when `legacy-agent-markdown-frozen/v1` is configured. If no normalized human
input remains in a session or day, its managed prompt file is removed and its
history is retained. Indexes reflect the resulting inventory. The original
source database or transcript is never removed by this output operation.

The freeze applies to recognized legacy prompt files without the shared
`Managed-By` header, not to current managed output. It deliberately preserves
old prompt bytes even after a role-policy change. To repair these files, the
caller must explicitly switch to `legacy-agent-markdown/v1`, retaining legacy
recognition while allowing rewrite/removal. Switching directly to `none` does
not adopt unmanaged files and can fail output audit. Preview the change with
`scripts/extract --manifest PATH --dry-run` before authorized publication.

Legacy prompts paired to history by ownership and filename can be refreshed
or removed after the freeze ends. An identity-less legacy prompt whose only
match was its old human-event content may no longer match after reclassification;
it remains preserved rather than being guessed into a session. Such orphans
need a separately reviewed consumer migration. Reconciliation establishes
history coverage, not correctness or freshness of preserved legacy prompts.

Every custom redaction regex has a public synthetic canary. The regex and
canary may live in a private consumer manifest; do not encode a real
credential as either one. A required redactor with no working pattern fails
before sources are opened.

See `manifest.example.json` for placeholder structure. It is intentionally not
runnable until every `/absolute/...` path is replaced by a consumer-owned
location.

## Adopting legacy OpenClaw output

Omit `output.prompt_directory`, or set it to `null`, when only session history
is owned by this writer. No prompt view is rendered, scanned, cleaned up or
indexed, and `publisher.owned_subtrees` needs only the selected history paths.
Existing prompt files outside those paths remain untouched. Index modes still
apply to the selected history directories. A configured prompt directory keeps
the normal prompt behavior and must remain disjoint from every history directory.

An owning consumer can adopt old `# Claw Session` Markdown without copying an
extractor. Choose a legacy Markdown compatibility rule and explicitly set
`output.compatibility.legacy_openclaw_node` to the owning opaque node. Also
route `output.history_directory_by_harness.openclaw` to the existing archive.
The runtime recognizes the bold `Session ID` header and legacy user/assistant
sections; it never infers the node from a machine name, file name, or body text.
A contradictory explicit host or malformed session file fails inventory.

Use `filename_strategy_by_harness.openclaw="session-date-prefix-8"` for new
`session-YYYY-MM-DD_<id-prefix>.md` files. The date uses selected source-header
`timestamp` metadata, then the first event date; a missing date uses
`unknown-date` and monthly layout places it in `unknown/`. Existing recognized
files stay at their existing paths. Equal event text and selected metadata keep
old bytes unchanged. Growing sessions use the normal managed renderer at the
same path; readers must accept both formats. `activity-summary` accepts both
when the consumer selects a `claw` source. Use `day_split="off"` when preserving
whole-session files is required; cleanup and index ownership remain independent
explicit choices.

Non-session Markdown stored with the archive can be preserved with
`output.compatibility.static_paths` (literal repo-relative paths) or
`static_patterns` (repo-relative non-recursive glob patterns). For example, a
consumer could select `History/memo-????-??-??.md`. Every pattern is anchored to
the full relative path and must lie inside a configured output directory.
Matching files remain byte-for-byte and outside session cleanup; the runtime
does not copy or refresh their source. A matching file containing a managed
session marker, session identity header, or recognized conversation sections
is rejected rather than hidden from reconciliation. Unconfigured files remain
subject to ordinary inventory validation. Keep the owning consumer's actual
paths, node labels, and patterns in private configuration.

A caller of `decode_source_snapshots` that selects a sidecar must also supply
its already-frozen `sessions_metadata` mapping in each snapshot's decoder
options. The API never opens the configured metadata file behind a frozen-byte
caller's back. Production extraction constructs this mapping itself.
