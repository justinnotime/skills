# Normalized session contract

The shared API version is `agent-session/v1`. Its stable identity is the
three-part tuple `(harness, node_label, complete session_id)`.

## Session

| Field | Contract |
|---|---|
| `schema_version` | Exactly `agent-session/v1` |
| `harness` | `claude-code`, `codex`, `opencode`, `dsh`, `cursor`, or `openclaw` |
| `session_id` | Complete harness identifier, never a shortened filename suffix |
| `source_ref` | Manifest source ID plus candidate-relative reference; never absolute |
| `node_label` | Supplied by `sources[].output_node`, never inferred |
| `cwd` | Optional source metadata; it does not grant access or identify a node |
| `project` | Policy-normalized project name |
| `started_at`, `ended_at` | UTC-aware timestamps or null |
| `events` | Ordered normalized events |
| `metadata` | Decoder observations that contain no credential or transcript copy |

## Event

An event has a stable sequence number, UTC-aware or absent timestamp,
`exact`/`approximate`/`unknown` timestamp quality, `user`/`assistant`/
`peer-agent` role, text, raw record kind, and non-sensitive metadata.

Decoders emit `user-like` before policy. The engine makes the retention
decision first and only then relabels configured peer-agent messages. Changing
the label policy therefore cannot change whether a session is retained.

History and prompt views are rendered from this same value. Prompt extraction
must never reparse rendered Markdown.

### Input role is not authorship

A native `user` role means input to the harness, not necessarily a person.
For example, an OpenCode Agent Bus wake can be an ordinary SQLite message with
`{"role":"user"}` and a text part beginning with `[Agent Bus wake]`. Those
fields alone do not distinguish a human request from machine-delivered input.
The decoder preserves it as `user-like`; the caller's existing
`event_policy.peer_agent_prefixes` can classify the literal marker as
`peer-agent`. There is no built-in Agent Bus text rule or dependency on its
implementation.

Prefix matching is case-sensitive `stripped_text.startswith(prefix)`, not a
substring search, regular expression, or natural-language guess. With
`[Agent Bus wake]` configured, a wake at the start of trimmed text is a peer
event. `Explain "[Agent Bus wake]"`, `"[Agent Bus wake]"`, a Markdown blockquote
`> [Agent Bus wake]`, and a fenced example remain human input with their text
intact. An assistant explanation remains `assistant`, even if it begins with
the marker. A person entering the exact marker unquoted at the start is
indistinguishable under this text-only contract and will also be classified
as a peer. Callers must reserve a sufficiently specific machine prefix;
generic words such as `agent`, `wake`, or `tool` are not provenance evidence.

Peer classification preserves event text, order, timestamps, timestamp quality,
and raw kind. History retains the event labeled `peer-agent`; prompts select
only normalized `user` events. A retained peer-only session or day still has
history but no prompt file. Retention and decoder observation counts refer to
pre-classification user-like events, not a count of human requests.

Downstream consumers should use normalized roles and event timestamps instead
of reclassifying text or assigning every event the session start time. OpenCode
orders text parts by message creation time/ID and then part creation time/ID.
An event uses the part creation timestamp, falling back to message creation
time only when the part time is null; invalid or missing time stays unknown.
Valid native millisecond timestamps become UTC-aware values with `exact`
quality. Day slicing follows event timestamps regardless of role, including
days with peer input but no human prompt.

### Multiple declared roots

When several declared sources contain the same normalized identity, an exact
role/text prefix is treated as an older append-only generation and the longest
generation wins. Event timestamps do not distinguish generations because a
harness may expose approximate assistant times. Any non-prefix role/text
difference is reported as `DUPLICATE_SESSION_DIVERGENCE` and blocks
publication through reconciliation.

The source ID, root directory, and `owner`/`mirror` authority do not form part
of session identity. A declared owner and its mirror use the same output node
to deduplicate; neither authority automatically wins over a longer generation.
Different complete session IDs remain distinct even when their filename
suffixes coincide. Identical IDs on different output nodes remain distinct.
Independent roots reusing the same complete ID on the same node cannot be
distinguished by their path: the caller must select output nodes representing
the intended identity namespace rather than expecting path-based separation.

## Decoder boundary

A decoder receives a stable `SourceSnapshot` and returns `DecodeBatch`.
It does not discover undeclared sources, decide cleanup, infer a consumer,
write files, or print transcript text. An unrecognized record whose shape or
name says it may carry conversation text (for example a Claude Code record with
a `message` envelope, or a Codex payload with a `message`, `item`, or `content`
key) appears in `FormatObservations.unknown_record_counts` and makes the batch
incomplete. Any other unrecognized record is session bookkeeping and is counted
under `recognized_record_counts` as `ignored.<type>`, so a harness adding a new
bookkeeping record does not stop extraction. Required malformed input makes the
batch incomplete or invalid and blocks publication.

`agent_skills.sessions.api.decode_source_snapshots` is the public parity
boundary for a caller that has already frozen byte-backed `SourceSnapshot`
values. It verifies source identity, decodes, applies manifest policy,
normalizes, deduplicates, and returns an `ExtractionSnapshot` with format
observations. It deliberately performs no discovery, rendering, cleanup, or
publication and rejects snapshots that require direct-path revalidation.

### New harness checklist

1. Declare every authorized root, reader mode, output node, and ownership in
   the consumer manifest; backup labels and local paths do not grant access.
2. Establish which native records actually identify machine-origin input.
   Exclude by native metadata only when that harness contract supports it and
   synthetic tests distinguish direct human input. Plain input-role text
   without such metadata goes through the existing caller-owned event policy;
   do not infer authorship from a session title, agent/model label, or generic
   words.
3. Test raw native records through decoder, normalization, history, and prompts,
   including literal human discussion of machine markers, peer-only sessions,
   event timestamps, and owner/mirror copies. Keep fixtures synthetic and the
   package independent of sibling Skills.
4. Exercise a policy-only change against existing output with unchanged source
   bytes, including removal of obsolete prompt files and a converged second run.
   Review [legacy compatibility](manifest.md#policies-and-output): frozen legacy
   prompts are deliberately not refreshed until the caller ends that freeze.

`tests/test_opencode_provenance.py` exercises this contract using synthetic
SQLite records. It does not establish compatibility with a live deployment.
