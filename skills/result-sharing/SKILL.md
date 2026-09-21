---
name: result-sharing
description: Publish selected agent deliverables to a configured durable result hub and return browsable links. Use when sharing generated reports, interactive pages, diagrams or downloadable files, or setting up a common result destination across nodes. Supports local and SSH publishing, project history and related conversation links.
---

# Share durable results

When the user wants to inspect or share a completed artifact, publish its selected
files with this package's `scripts/share.py`. Return the receipt's `result_url`
and, when useful, `entry_url`. A temporary directory, terminal log, or ad hoc web
server is not the final delivery destination when a hub is configured.

Read the explicitly selected `--config`, `RESULT_SHARING_CONFIG`, or default
`~/.config/result-sharing/config.json`. Configuration defines source grants,
destination and transport; this Skill does not infer account or node scope.
If there is no configuration, finish preparing the result and ask for the
intended destination. Do not invent a public host or upload private material.

Prepare an explicit file list, including the entry page's local dependencies.
Review those files under the originating repository's publication policy.
Publish the deliverable, not an entire checkout, harness home or conversation
archive. Hidden paths and symlinks are rejected; content scanning remains the
caller's responsibility. Ordinary authorization to share a result covers the
configured destination, not public release or sending messages to third parties.

```sh
python3 scripts/share.py publish --source /approved/output \
  --project example --title 'Example result' --entry index.html \
  --files index.html app.js style.css assets/chart.svg
```

Use `--dry-run` for selection and local boundary validation without network or
writes. It does not prove receiver reachability. The same project slug groups
revisions; changed content or metadata creates an immutable version, and an
identical publish returns the same URL. The hub index and project history update
automatically. Keep the source until a successful receipt; a failed transport
must not be described as a published result. A retry is safe for the same bundle.

Use `--summary` to explain the outcome and `--conversation-url` for an existing,
authorized conversation-viewer link. Do not embed login tokens in links. Full
conversation indexing belongs to a separately configured viewer such as
AgentsView; the publisher copies only the selected result files.

For setup, node onboarding, HTTP serving and upgrade boundaries, read
[the configuration and deployment contract](references/configuration.md).
Each node needs only this standalone package and its own private configuration.
Choose node-local publication when each node keeps its own results: use the same
relative directory contract and a local viewer per node. Use a central hub only
when explicitly configured; then publishers can use SSH without extra web
services. Neither mode needs a new preview server for each result.

Runtime: Python 3.10+ on Linux/macOS; SSH for remote transport. No third-party
Python dependencies. Run `python3 -B -m unittest discover -s tests -v` from this
package, and validate `SKILL.md` with the Skill validator.
