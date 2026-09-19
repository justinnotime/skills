# Source from bash or zsh. The root clip.sh symlink remains supported.
# Keep the whole Skill together; runtime uses Python 3.10+ standard library.
if [ -n "${BASH_VERSION:-}" ]; then
    _remote_clipboard_source=${BASH_SOURCE[0]}
elif [ -n "${ZSH_VERSION:-}" ]; then
    _remote_clipboard_source=${(%):-%x}
else
    printf '%s\n' 'clip: source this helper from bash or zsh' >&2
    return 1
fi
_REMOTE_CLIPBOARD_COMMAND=$(python3 -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve().with_name("clipboard.py"))' "$_remote_clipboard_source")
unset _remote_clipboard_source
clip() {
    python3 "$_REMOTE_CLIPBOARD_COMMAND" copy -- "$@"
}
clip-paste() {
    python3 "$_REMOTE_CLIPBOARD_COMMAND" paste
}
clip-tmux() {
    python3 "$_REMOTE_CLIPBOARD_COMMAND" tmux "$@"
}
