---
name: activity-summary
description: Extract deterministic activity facts from selected Git and session archives, generate daily summaries, or compile weekly summaries with source-hash validation and resumable publication. Use for configured activity reporting and summary provenance checks; normal generation uses a caller-selected Claude CLI account and budget.
---

# Activity summary

This bundle contains the fact extractor, issue reference parser, deterministic
section renderer, evaluator, daily and weekly validators, and both scheduled
runners. Sources, editorial instructions, account selection, model arguments,
repository paths and commit policy are caller-owned configuration.

**LLM calls > 0** for normal generation. Fact extraction, rendering, evaluation,
validation, `--doctor` and `--dry-run` make zero model calls. Doctor does not query
account status; it checks local configuration, the prompt template and executable
availability. A normal run checks the configured account before generation.

Read [configuration](references/configuration.md) when configuring the bundle and
[scheduling and recovery](references/scheduling.md) before changing a schedule.
Use the [synthetic example](references/example.json) as a schema reference; replace
its source selection, templates and external policy commands locally.

```sh
uv sync --project /path/to/activity-summary --locked
/path/to/activity-summary/scripts/run-daily --config "$HOME/.config/activity-summary/config.json" --doctor
/path/to/activity-summary/scripts/run-daily --config "$HOME/.config/activity-summary/config.json" --dry-run --target yesterday
/path/to/activity-summary/scripts/run-weekly --config "$HOME/.config/activity-summary/config.json" --dry-run --end 2024-01-07
```

The runtime uses Python 3.10 or later and the standard library. Normal scheduled
publication also needs Git, the configured Claude CLI, a configured external
`repository-publish` executable and caller-provided validation/commit/message
commands. This package does not import another Skill or a private
repository library. Set `ACTIVITY_SUMMARY_PYTHON` to choose its interpreter.

For mechanical inspection:

```sh
scripts/extract-facts 2024-01-02 --config /private/config.json > /private/facts.json
scripts/render-issue-section /private/facts.json /path/to/repository --config /private/config.json
scripts/eval-facts /private/summary.md /private/facts.json --config /private/config.json
scripts/validate-daily /private/summary.md 2024-01-02 INPUT_SHA256 /private/facts.json --config /private/config.json
scripts/validate-weekly /private/weekly.md 2024-01-07 INPUT_SHA256 /private/inputs.md '' --config /private/config.json
```

`render-issue-section --install FILE` edits the selected candidate. Without
`--install` it prints the rendered section. Both validators fail on incorrect
source hashes, structure or issue identities; daily validation can also check the
complete selected fact set. Evaluation reports coverage diagnostics and is not a
publication validator.

Before authorizing a normal run, inspect the selected dates and token estimate
from `--dry-run`, the model command's per-call budget, and the account command.
Keep stable instructions before `{{inputs}}` in the template so the selected CLI
and provider can reuse prompt prefixes. Each daily call batches that day's
facts, and each weekly call batches its available daily inputs. See
[cost and verification](references/cost-and-verification.md) for estimate limits.

Normal commands publish to the configured remote. Running doctor or preparing
configuration does not by itself authorize publication, spending, installation
or changes to an existing crontab. No schedule is installed by this package.
