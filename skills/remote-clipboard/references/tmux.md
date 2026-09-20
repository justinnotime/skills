# tmux clipboard setup

## Install and choose mouse behavior

Use Python 3.10+ and tmux 3.3+. Install the backend needed on the **local desktop**:
`xclip` for X11/GNOME XWayland, `wl-clipboard` for supported Wayland compositors,
or the built-in `pbcopy`/`pbpaste` on macOS. SSH-only servers do not need a
desktop clipboard package.

From the stable Skill package location, preview, then install:

```sh
python3 scripts/tmux-config.py --config ~/.tmux.conf
python3 scripts/tmux-config.py --config ~/.tmux.conf --apply --reload
```

`--apply` edits only its marked block and saves a private backup beside the
configuration. It validates syntax in an isolated server first. `--reload`
selects the tmux server through the ordinary `TMUX` environment and snapshots
its affected settings and bindings before loading the configuration. Without
`--reload`, a running server is not required. If the config is a generated file
or symlink, edit its owning source instead.

Optional flags:

| Flag | Behavior |
| --- | --- |
| `--mouse preserve` (default) | Preserve mouse enablement, root drag handling, wheel and paste bindings; update copy bindings with explicit client routing |
| `--mouse select` | Enable mouse; make drag/double/triple click select even in full-screen applications, replacing their handling of those gestures |
| `--paste-bindings` | Replace prefix `]`, middle-click and right-click; local registered clients paste the desktop clipboard, remote clients receive a terminal-paste hint |

For drag-to-copy inside a full-screen agent, first confirm that the client
terminal supports OSC 52 (or has an intended native backend), then install with:

```sh
python3 scripts/tmux-config.py --config ~/.tmux.conf --mouse select --apply --reload
```

This replaces application drag/double/triple-click selection. An application
requesting mouse events otherwise receives the drag, so installing with the
default `--mouse preserve` can leave the reported problem unchanged. Remote
right/middle-click paste needs the client terminal's own paste action or menu;
adding `--paste-bindings` only replaces those clicks with a shortcut hint.

Both copy-mode key tables get mouse-release, word/line, and ordinary keyboard
copy bindings. Emacs Enter and vi `y` copy as well. Root double/triple click
preserves application mouse routing unless `--mouse select` is requested.
Default installation still replaces custom bindings for these copy actions;
review the preview and use the backup if those bindings should be restored.
Wheels, history limit, prefix selection, pane creation, and session management
are not changed. Alt+right-click retains the existing tmux pane menu.

The block sets `set-clipboard off` to prevent copy-mode from broadcasting an
additional OSC 52 write to every viewer, and supplies a default `copy-command`.
It explicitly sends copied content to the initiating client. It does not change
`allow-passthrough` or advertise unsupported terminal capabilities. Other tools
that change these same options/bindings must have one agreed owner and load order.

## Apple Terminal over SSH/mosh

Apple Terminal.app does not implement OSC 52 clipboard writes. A successful
remote helper, a highlighted tmux selection, or `clipboard` in tmux's client
features does not mean text reached the Mac clipboard. Remote `pbcopy` would
also address the wrong machine. Do not keep changing remote bindings or
terminal features to repair a client capability that is absent.

For ordinary drag-to-copy, start the connection through the local wrapper from
the stable package on the Mac:

```sh
scripts/clip-terminal -- ssh -t user@example 'tmux -T clipboard new-session -A -s main'
```

Replace the SSH command with the user's existing connection command and session;
`clip-terminal -- az ssh vm ...` and `clip-terminal -- mosh ...` also wrap those
commands without taking over their authentication. Start it outside the remote
shell. The `-T clipboard` tmux client flag is appropriate **inside this wrapper**,
which really implements clipboard writes, even though Terminal.app does not.
On the remote server, install the `--mouse select` bindings described above.
Then a plain drag and release copies locally, and Cmd+V pastes as usual.

The wrapper runs only for the lifetime of its child connection. It forwards
keyboard/mouse input and window sizes, consumes OSC 52 writes with default or
`c` selection, and calls the existing native clipboard backend. It never reads
the clipboard or answers clipboard queries. Other terminal output is preserved.
No port, background watcher, SSH key, or remote desktop clipboard is added.
Existing SSH/mosh connections cannot be retrofitted: reconnect through the
wrapper and reattach the same tmux session; do not restart the remote agents.
Exiting the wrapper restores local terminal settings. To stop using it, connect
with the original command.

For native copying without the wrapper:

1. Hold **Fn** while dragging over the text, then release it.
2. Press **Cmd+C** to copy the local selection.
3. Use **Cmd+V** to paste into the remote application or another Mac app.

Fn temporarily bypasses mouse reporting; normal tmux wheel handling can remain
enabled. Apple Terminal uses Fn for this action; do not substitute another
terminal's Option modifier. Native selection also requires Cmd+C.

If the keyboard has no usable Fn key, temporarily turn off **View > Allow Mouse
Reporting**, select and copy locally, then restore it for tmux scrolling. The
**Cmd+R** shortcut toggles that setting. Do not permanently disable mouse
reporting as a copy fix when the user also needs tmux wheel scrolling.

References: [Apple's mouse reporting setting](https://support.apple.com/guide/terminal/turn-on-mouse-reporting-trmlc69728a5/mac),
[Apple's copy/paste and mouse-reporting shortcuts](https://support.apple.com/guide/terminal/keyboard-shortcuts-trmlshtcts/mac),
and [a firsthand Terminal.app reproduction and Fn workaround](https://github.com/anthropics/claude-code/issues/78751).

## Local and remote clients on one server

Use the shell helper's launcher outside tmux:

```sh
clip-tmux -- new-session -A -s main
clip-tmux -- attach-session -t main
clip-tmux --backend wayland -- attach-session -t main
clip-tmux --primary -- attach-session -t main
```

It picks the backend in the attaching shell, then registers it for that exact
connection. SSH/mosh chooses OSC 52 before any desktop detection. `--primary`
also updates Linux's selection clipboard when using a native backend.

Registration is keyed by tmux client PID and creation time, not session or pane.
A newly attached client cannot inherit another connection's native desktop route.
Register an existing connection explicitly when installing into a live server:

```sh
python3 scripts/clipboard.py client --backend xclip --primary --client /dev/pts/N
```

Obtain the target from `tmux list-clients -F '#{client_tty}'`; identify the desired
local terminal before selecting it. With exactly one attached client in the
originating session, `--client` may be omitted. macOS uses `--backend pbcopy`.
Native registration grants access to the server's desktop clipboard, so perform
it only for a local desktop connection or an explicitly intended server-side
clipboard operation. It stores display/authentication-file **references**, not
cookies or clipboard text, in tmux user options. Registrations end with that
server; newly attached terminals use the launcher or register again.

An ordinary `tmux attach` without registration uses OSC 52. This is safe for SSH,
but GNOME Terminal versions without OSC 52 need the local launcher/registration.
The remote client needs OSC 52 support and permission. A `clipboard` tmux feature
alone is not proof that the actual terminal accepts it. Failure does not fall
back to the server desktop. Remote copy clears then updates the targeted
terminal clipboard so repeated identical text can traverse mosh's state sync.

Remote paste uses the client terminal's standard shortcut. The optional tmux
paste bindings display a hint for remote clients; they never query or paste
from the server desktop. This intentionally avoids nonportable remote clipboard
read requests. Normal tmux internal buffers remain available through its commands.

## Nested tmux and the legacy OSC 52 path

The inner and outer tmux layers each have their own mouse handling and clipboard
policy. Forced mouse selection on the outer layer intercepts inner gestures.
Do not claim an SSH hop or nested configuration works based only on local tests.

`copy --backend osc52` writes a passthrough-wrapped sequence inside tmux. Like the
legacy helper, that route requires `set -g allow-passthrough all` on tmux 3.3+;
this permission is not enabled by the installer. Multiple nesting layers may
need additional forwarding setup. A terminal that rejects OSC 52 cannot be
fixed simply by changing tmux's terminfo features.

## Rollback and migration

The installer prints the exact backup directory. Restore both its configuration
and the captured live settings with:

```sh
python3 scripts/tmux-config.py --restore /path/to/printed-backup --apply
```

Restore refuses to overwrite configuration edited after installation, or to
apply captured live bindings to a different server. Backups are retained. A
successful reload does not require detaching or restarting clients.

When migrating an older ad-hoc helper, first identify and preserve its exact
configuration block and executable. Remove only the owned old block, install
this managed block, register the current local client, and verify synthetic
copy/paste. Retire the old helper after checking that no active bindings or
shell callers reference it. Do not ship private paths or machine policy in the
public Skill. Keep this package at a stable path, or regenerate the block after
moving it or its Python interpreter.

## Evidence and troubleshooting

Check the running server, not just whether the Skill files are up to date:

```sh
tmux display-message -p 'server=#{version} socket=#{socket_path} application_mouse=#{mouse_any_flag}'
tmux show-options -Av mouse
tmux show-options -s set-clipboard
tmux list-keys -T root
tmux list-keys -T copy-mode
tmux list-keys -T copy-mode-vi
```

The installed copy bindings name `clipboard.py tmux-copy`. In tmux 3.7c,
`list-keys -T TABLE KEY` can return success with empty output for an existing
binding; inspect the whole table. The installer snapshots whole tables and
restores only the bindings it changes.

`clipboard.py doctor` reports tool availability and backend categories without
printing clipboard text or environment values. Backend errors/timeouts never
fall back to stale tmux buffers. Clipboard content travels on stdin, not in
shell command arguments.

Run `bash tests/run.sh` from a standalone copy of the package. Real terminal
clients in the tests verify that a remote copy reaches only its initiating
client and does not update a simultaneous local client's fake desktop clipboard.
Native macOS behavior, real compositor behavior, and actual SSH/mosh transport
are separate live checks. Use synthetic text and never paste tests into a user's
active shell pane.

Upstream references: [tmux clipboard](https://github.com/tmux/tmux/wiki/Clipboard)
and [tmux manual](https://man.openbsd.org/tmux.1).
