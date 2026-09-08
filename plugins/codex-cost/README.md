# Codex Cost

An independently maintained Codex plugin, not an OpenAI product. Requires
Python 3.11 or newer and a Codex CLI with plugin hooks.

Local Codex CLI cost estimates, per user turn and per conversation. Reads the
selected conversation's existing usage records; makes no model or network calls.
A Stop or Interrupt hook prints the completed/interrupted turn's cost,
conversation total and input token cache-hit percentages inside that Codex
conversation. No assistant instructions are injected. The plugin does not
configure tmux or display a shared terminal status row.

```sh
python3 scripts/cost.py turns --thread YOUR_THREAD_ID
python3 scripts/cost.py status --thread YOUR_THREAD_ID
python3 scripts/cost.py turns --thread YOUR_THREAD_ID --json
```

The executable defaults to `CODEX_THREAD_ID` when run by Codex. Thread selection
is explicit outside Codex; it never guesses from the most recently changed file.

Run these commands from this plugin directory. `CODEX_HOME` selects the Codex
data directory (default `~/.codex`). Tier snapshots stay in
`~/.local/share/codex-cost/turn-settings/`; they contain no prompts or credentials.
The plugin reads the selected thread from `state_5.sqlite` and its rollout file.
It does not read authentication files or send session data anywhere.

The hook prints a line like this synthetic example:

```text
Turn ~$0.0031 | Total ~$0.0092 | Cache 60.0%/70.0% turn/all | 3 calls | API list-price estimate
```

End-of-turn messages and CLI reports are supported. The plugin does not add a
persistent custom cost field to the native Codex footer.

## Accounting

The dated `prices.json` records the official pricing source and collection time;
it is a bundled snapshot, not a live price feed. Rates are USD per million tokens,
in the order ordinary input, cache reads, cache writes and output. A null rate
means unavailable. Review the source and update the snapshot when pricing changes.
Each response is priced separately, using its recorded model and token counts.
Input includes ordinary input, cache reads and cache writes; reasoning tokens
are already included in output and are not added again. Cache-hit rate is the
sum of cached input divided by the sum of all input, not a mean of percentages.
The long-context threshold applies to each request, not the session aggregate.
Response IDs deduplicate usage records. The parallel `token_count` stream is
not added to modern `token_usage_record` entries.

Service tier is taken from the transcript when available. Otherwise the prompt
hook captures the user config's tier. Session overrides or a tier change midway
through a turn may not be reflected in that config snapshot. Historical turns
without tier evidence display a Standard-to-Fast range. Unknown models, missing
rates and invalid usage never silently become zero-dollar usage. Estimates
exclude tool fees, taxes, discounts, unrecorded requests and other conversations
(including separate agent threads). Vendor invoices remain the billing authority.

The supported transcript layout is based on Codex CLI 0.153.4. The local
transcript format is not a stable upstream interface. Interrupted writes are
ignored until the writer completes the final line; malformed complete records
make the report unavailable.

## Installation and rollback

Install from the public repository:

```sh
codex plugin marketplace add justinnotime/skills
codex plugin add codex-cost@skills
```

Codex requires exact hook definitions to be trusted; inspect them with `/hooks`.
Start a new or resumed CLI session after installation. Each hook selects the
conversation from its own `session_id`. `UserPromptSubmit` captures tier settings;
`Stop` and `Interrupt` display the estimate. Installing a second copy under another
marketplace can duplicate these messages; keep one enabled installation.

```sh
codex plugin remove codex-cost@skills
```

Removal leaves existing tier snapshots on disk. This plugin does not change
the user's native status-line selection.

## Development

From this plugin directory, run the synthetic accounting and hook tests without
access to real sessions or model calls:

```sh
python3 -B -m unittest discover -s tests -v
```
