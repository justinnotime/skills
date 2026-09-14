# Repository-owned selections and Cursor

A repository owns only its own labels, roots and executable selections. Point
`--config` directly at that repository's file and use a separate `--output`
launcher file for each repository. Do not assemble every repository's profiles
in a fragment owned by one of them, or automatically load all neighboring files.
An unavailable unselected repository must not block the selected repository.

```bash
scripts/render-launchers.sh --config /srv/repo-a/config/profiles.conf \
  --output "$HOME/.config/agent-harness-profiles/repo-a.sh"
scripts/doctor.sh --config /srv/repo-a/config/profiles.conf
```

Labels still need distinct command names in a shared shell. Existing collision
checks apply across separately generated files when sourced. Select a new label
or a fresh shell; never silently replace another repository's functions.
A node can use existing data-pipeline jobs to call state-backup once per selected
repository with `--config FILE`. Keep backup destination ownership disjoint and
serialize writers that share a destination. No combined configuration generator
or additional cron is needed. Legacy `~/.config/backup/config` remains supported.

## Cursor Agent CLI

`CURSOR_PROFILES` uses newline-separated `label:/absolute/root` entries, with
spaces allowed in paths. `CURSOR_COMMAND` optionally selects an absolute Agent
CLI executable; its default is `cursor-agent`. Generated `cursor-agent-LABEL`
functions set `CURSOR_CONFIG_DIR` for that child and preserve arguments, working
directory and exit status. They create no native directories or credentials.

```bash
CURSOR_PROFILES="alpha:$HOME/.cursor-alpha
beta:$HOME/.cursor-beta"
# CURSOR_COMMAND=/absolute/path/to/agent
```

Cursor documents `CURSOR_CONFIG_DIR` as its CLI configuration override in the
[official configuration reference](https://cursor.com/docs/cli/reference/configuration).
This is a configuration-root contract, not proof that every Cursor release
stores transcripts, IDE data or authentication beneath that directory. Verify
actual native paths on the selected node before assigning backup or extraction.
The launcher does not override `HOME`, clear inherited `CURSOR_API_KEY`, select an
IDE profile, or claim that changing a CLI root relocates the IDE's shared project
archive. Keep an existing IDE invocation with its own native data selection;
select the actual transcript projects independently in the extraction manifest.

For supported Cursor project JSONL archives, add another source entry for each
selected native root. Reuse the Cursor decoder. A shared `projects/` root uses
`discovery.directories` to list the exact authorized project subdirectories.
Backup uses its independent `CURSOR_PROJECT_ALLOWLIST` selection. Neither list
is generated from a label, launcher or account name.
