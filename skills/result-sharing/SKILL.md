---
name: result-sharing
description: Write agent deliverables to the machine's shared task root and return live file-browser links. Use for reports, diagrams, downloadable results, or configuring their read-only viewer behind separate authentication. Immutable local or SSH publication remains an explicitly selected compatibility mode.
---

# Write and browse results

Read the caller-selected profile, `$RESULT_SHARING_PROFILE`, or
`${XDG_CONFIG_HOME:-$HOME/.config}/result-sharing/profile.md` when present.
It is guidance for the agent, not a program configuration format. Reuse the
machine's selected task root across agents and conversations; default to
`~/ops/` only when no existing choice applies. An unreadable explicitly selected
profile is a configuration error, not permission to invent another destination.

Write deliverables directly to `<task-root>/result-sharing/<project>/` and keep
working evidence in `<task-root>/tasks/<task>/`. Continue existing task folders.
Repository edits and tests still follow their worktree rules. Keep credentials,
application databases and authentication state in their owning applications'
private directories, outside any browsable root.

For an already authorized live viewer, saving the file makes it available;
refresh the directory or file to see changes. There is no `output/` or `site/`
stage, manifest, copy, publish command or per-report server. The viewer has a
metadata index, not a second copy of the documents. An existing open page need
not update until refreshed.

Return the local path and, when configured, a URL relative to the approved
viewer source. With the bundled Quantum recipe, the route is
`/files/<source-name>/<percent-encoded-relative-path>`. The optional helper reads
only the origin from the gateway's native configuration, avoiding a duplicate URL:

```sh
python3 scripts/live.py url --root /approved/task-root \
  --gateway-config /private/gateway.json --app files --source ops \
  /approved/task-root/result-sharing/example/report.md
```

If no viewer exists, deliver the local file and state that a browser link is
not configured. Do not install a service or widen a source grant merely to
finish a report. A configured whole-root viewer exposes working evidence as
well as final reports to its owner; a final-only grant uses a narrower mount.
Owner access does not authorize public publication or sending files to others.

## Setup and maintenance

For deployment, read [configuration and responsibilities](references/configuration.md),
then the [live viewer recipe](references/live-viewer.md). The layers are:

- This Skill defines writing and link delivery, and owns the optional file-browser
  recipe and its tests.
- FileBrowser Quantum displays the selected local files read-only.
- An independent `private-web-access` gateway provides Passkey login. An
  independently configured HTTPS terminator, optionally `tailscale-serve`,
  forwards only to that gateway.

Native application configuration remains the authority. There is no combined
configuration generator, and installing Skill links does not deploy services.
Keep the existing owner, origins and Passkey records when changing the viewer.
The recipe is tested on Linux with Docker Compose; other service managers and
operating systems require their own deployment checks.

Markdown, text, images and downloads are supported. This read-only recipe does
not host arbitrary interactive HTML; embedded PDF/HTML frames and external code
dependencies are restricted by the gateway policy. Use a separately authorized
application deployment when executable report pages are required.

For a caller who explicitly wants immutable revisions or a central SSH hub, use
[legacy publication](references/legacy-publication.md) and `scripts/share.py`.
Do not select it merely because an old JSON file still exists. Preserve existing
legacy data during migration; switching workflows does not authorize deletion.

Run the package's standard-library tests and Skill validator after changes.
Deployment changes also require the [real viewer and Passkey checks](references/live-viewer.md#verification).
