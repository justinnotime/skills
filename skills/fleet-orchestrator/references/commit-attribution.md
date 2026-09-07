# Commit participant attribution

`scripts/seat-trailer.py [--config FILE] COMMIT_MESSAGE_FILE` is an optional
`prepare-commit-msg` helper. It uses the same fleet configuration and tmux
server selector as the other package commands. It writes only the supplied
message file and makes zero model calls. It does not install Git hooks, register
participants, send messages, or update fleet databases.

Configure the complete `seat_trailer` object to enable attribution:

```json
{
  "seat_trailer": {
    "members_command": ["/path/to/fleet/scripts/matrix-bus.sh", "members"],
    "agent_windows": ["example-agent"],
    "host": "{short_hostname}",
    "trailer_key": "Seat"
  }
}
```

`members_command` may be empty. All displayed fields are explicit. The legacy
`ledger` field is accepted but ignored: ORC no longer stores current membership.
Command arguments use the fleet configuration's `~` and environment expansion. `host` is an exact registry host label;
`{short_hostname}` explicitly selects the local hostname before the first dot.
The window vocabulary and trailer key are consumer policy. An absent object
leaves commit messages unchanged.

Resolution starts with the inherited `TMUX_PANE`. Every grouped-session alias
of that pane is considered; a failed configured tmux server is never replaced
by a different server. The configured Agent Bus member command is the only
membership source and supplies JSON-lines objects with `agent_id`, `handle`,
`host`, `tmux`, and `status`. Only active participants on the selected host match.
When a source supplies `pane_id`, it must match the inherited pane exactly;
otherwise the exact `tmux=<session:window.pane> ` prefix is used. Members with an
unknown or absent terminal location cannot claim the current pane.

One match produces `Seat: <handle> (<id>)` with the configured key. Multiple
matches produce an ambiguity record. No match records an unregistered pane only
when its window name belongs to the configured agent-window vocabulary. A plain
shell or absent tmux pane adds nothing. Registry and command failures leave the
message unchanged and emit a diagnostic category without registry output.

An existing trailer with the selected key makes the helper a no-op. Git's
`interpret-trailers` places new attribution above template comments; if it is
unavailable, a plain trailing paragraph is used. The final bytes are installed
atomically, preserving the original message if preparation or replacement fails.
Non-UTF-8 message bytes are preserved. Symlink message files are refused.

Rebase, cherry-pick and revert replay are skipped when Git exposes the applicable
`CHERRY_PICK_HEAD`/`REVERT_HEAD` marker or replay action in `GIT_REFLOG_ACTION`.
Ordinary commits, amendments, merges and a new commit at a rebase edit stop remain
eligible. A clean revert that Git performs without those markers is a newly
attributed commit by the reverting participant. The helper does not infer replay
from commit-message text or rewrite existing attribution.

A caller-owned hook may invoke the helper with an explicit configuration and
ignore its status. The helper itself returns success on attribution failures so
optional attribution never prevents a commit. Installing it or copying a hook
requires the caller's normal repository authorization; configuration alone does
not change an installed hook.

Synthetic validation, including real Git replay and failure recovery:

```bash
uv run --locked pytest -q tests/test_seat_trailer.py
```
