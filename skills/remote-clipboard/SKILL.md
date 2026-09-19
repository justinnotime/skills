---
name: remote-clipboard
description: Configure and troubleshoot local, SSH, mosh, and tmux clipboard copy/paste on Linux and macOS. Use for mouse selection, system clipboard integration, OSC 52, and reproducible tmux clipboard setup; use file transfer for large payloads.
---

# Remote Clipboard

One implementation owns shell copying, native desktop backends, and tmux
clipboard bindings. Keep this Skill directory together. Runtime requires
Python 3.10+; tmux integration targets tmux 3.3+.

## Choose the clipboard destination

The destination is the user's **client terminal**, which may be on another
machine. A desktop running on the tmux server does not establish that it is
the destination. Multiple clients can view the same session simultaneously.

- Local Linux: `xclip` through X11/XWayland, or `wl-copy`/`wl-paste`. The runtime
  prefers XWayland when available; some Wayland compositors require focus for
  wl-clipboard's fallback. GNOME desktop environment recovery handles tmux
  servers whose Xauthority path predates the current login.
- Local macOS: `pbcopy`/`pbpaste`.
- SSH/mosh: OSC 52 writes to the client terminal. Its support and clipboard
  permissions must be enabled. Native backend selection never takes priority
  over a detected remote shell, unless the user explicitly selects a backend.
- Remote paste: use the terminal's Cmd+V / Ctrl+Shift+V. Do not substitute the
  server's desktop clipboard or assume OSC 52 clipboard queries are supported.

Do not read existing clipboard content to diagnose connectivity. Use synthetic
text for an authorized roundtrip; do not print or save the user's clipboard.

## Shell commands

Source `scripts/clip.sh` from bash or zsh. The root compatibility `clip.sh`
symlink continues to work. Existing `clip` stdin/file calls are preserved:

```sh
. /path/to/remote-clipboard/scripts/clip.sh
printf '%s\n' 'sample' | clip
clip path/to/file
clip-paste                         # local desktop text to stdout
clip-tmux -- new-session -A -s main
```

`clip-tmux` registers this connection's local or remote backend after attaching.
From an SSH shell it selects OSC 52 even if the server has a local desktop.
Without this launcher, an unregistered tmux client defaults to OSC 52.
`scripts/clip-tmux` is the same launcher as a standalone executable and may
be linked into a user bin directory when persistent installation is requested.
An existing local connection can be explicitly registered as described in
[tmux setup](references/tmux.md).

The Python entrypoint also supports explicit choices:

```sh
python3 scripts/clipboard.py copy --backend pbcopy < file.txt
python3 scripts/clipboard.py copy --backend osc52 < file.txt
python3 scripts/clipboard.py doctor
```

Inside tmux, automatic shell copying requires one unambiguous attached client
for the originating session; use `--client /dev/pts/N` when there are several.
The mouse/key bindings always pass the initiating client explicitly.
Direct `--backend osc52` retains the passthrough path for nested setups;
tmux 3.3+ needs `allow-passthrough all` for that path. The managed tmux bindings
use targeted `load-buffer -w` and do not require enabling passthrough.

## Persistent tmux configuration

For installation, migration, rollback, nested tmux, or compatibility analysis,
read [tmux setup](references/tmux.md). Preview with:

```sh
python3 scripts/tmux-config.py
```

Persistent installation is appropriate when the user requests it. Preserve
unrelated configuration. Forced mouse selection and system-paste bindings are
separate opt-ins; explain that they replace application mouse handling and
right-click menus. Install from the stable package location, not a disposable
editing worktree. Do not restart or kill the user's tmux server to reload.

Keep clipboard behavior here; session orchestration, pane lifecycle, and titles
remain outside this Skill. Caller-specific paths, backend choices, and desktop
preferences belong in local configuration, not the public package.

## Verification

```sh
bash tests/run.sh
```

Tests use synthetic clipboard backends and real isolated tmux servers with two
terminal clients. They verify routing, actual copy-mode bindings, Unicode and
multiline paste, configuration preservation, and live rollback. macOS native
API behavior and real SSH/mosh terminal permissions still require verification
on those clients. OSC 52 is limited to 100 KB of encoded payload; larger content
should use file transfer. No background clipboard watcher is installed.
