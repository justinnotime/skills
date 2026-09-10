---
name: state-backup
description: Back up local agent-harness state into the stable machine-scoped Syncthing layout. Use when running or changing backup source roots, exclusions, profile formats, or destination compatibility; do not use for human-readable session extraction or Syncthing health diagnosis.
---

# State Backup

Use `scripts/backup` as the deterministic behavior authority. Existing jobs may
call the repository-root `backup.sh` compatibility link or `~/bin/backup`;
preserve both paths, `~/.config/backup/config`, existing `*_PROFILES` formats,
and existing destination names.

Before changing backup behavior, read
[the source-root contract](references/profiles.md). Support native-default-only and
additional-root installations. Treat every configured label as an opaque
destination suffix, never as an account identity or extraction permission.
Full harness onboarding belongs to `agent-harness-integration`; backup does not
install discovery links, hooks, accounts, or approve new source coverage.

OpenCode database snapshots require sqlite3. A missing executable or failed
snapshot/replacement preserves the previous snapshot, marks the profile failed,
and produces a nonzero final exit after other harnesses run. Temporary snapshots
are unique and atomically renamed; concurrent runs are last-successful-rename
wins, not a guarantee that the freshest source snapshot wins.

Schedulers call the script directly rather than invoking this Skill
conversationally:

```bash
scripts/backup
```

From this Skill directory, run these checks after any backup behavior or
compatibility change:

```bash
bash tests/run.sh
python3 -B -m unittest discover -s tests -v
```

The tests and script work without other Skill packages. Keep machine launchers,
credentials, schedules, and account-specific policy outside this package.
