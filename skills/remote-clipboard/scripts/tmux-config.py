#!/usr/bin/env python3
"""Preview/install a managed tmux clipboard block and restore its saved state."""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

from clipboard import ClipboardError, tmux

BEGIN = '# BEGIN remote-clipboard managed block'
END = '# END remote-clipboard managed block'
COPY_KEYS = {
    'copy-mode': {'MouseDragEnd1Pane': 'copy-pipe-and-cancel', 'M-w': 'copy-pipe-and-cancel',
                  'C-w': 'copy-pipe-and-cancel', 'Enter': 'copy-pipe-and-cancel',
                  'C-k': 'copy-pipe-end-of-line-and-cancel'},
    'copy-mode-vi': {'MouseDragEnd1Pane': 'copy-pipe-and-cancel', 'Enter': 'copy-pipe-and-cancel',
                     'C-j': 'copy-pipe-and-cancel', 'y': 'copy-pipe-and-cancel',
                     'D': 'copy-pipe-end-of-line-and-cancel'},
}


def quote(value):
    # A tmux config double-quoted string, not shell quoting.
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$') + '"'


def render(command, mouse='preserve', paste=False):
    invocation = shlex.join([sys.executable, str(command.resolve())])
    copy = invocation + ' tmux-copy --client #{q:client_tty}'
    paste_command = invocation + ' tmux-paste --client #{q:client_tty} --pane #{q:pane_id}'
    lines = [BEGIN, '# Copy targets the initiating client; unknown clients use OSC 52.',
             'set-option -s set-clipboard off',
             'set-option -s copy-command ' + quote(invocation + ' copy')]
    keys = []
    for table, bindings in COPY_KEYS.items():
        for key, action in bindings.items():
            lines.append(f'bind-key -T {table} {key} send-keys -X {action} {quote(copy)}')
            keys.append((table, key))
        for key, select in [('DoubleClick1Pane', 'select-word'), ('TripleClick1Pane', 'select-line')]:
            lines.append(f'bind-key -T {table} {key} select-pane \\; send-keys -X {select} \\; run-shell -d 0.3 \\; send-keys -X copy-pipe-and-cancel {quote(copy)}')
            keys.append((table, key))
    for key, select in [('DoubleClick1Pane', 'select-word'), ('TripleClick1Pane', 'select-line')]:
        action = f'copy-mode -H ; send-keys -X {select} ; run-shell -d 0.3 ; send-keys -X copy-pipe-and-cancel {quote(copy)}'
        if mouse == 'preserve':
            action = 'if-shell -F "#{||:#{pane_in_mode},#{mouse_any_flag}}" { send-keys -M } { ' + action + ' }'
        lines.append(f'bind-key -T root {key} {{ select-pane -t = ; ' + action + ' }')
        keys.append(('root', key))
    if mouse == 'select':
        lines.extend(['set-option -g mouse on', 'bind-key -T root MouseDrag1Pane select-pane -t = \\; copy-mode -M'])
        keys.append(('root', 'MouseDrag1Pane'))
    if paste:
        lines.append('bind-key -T prefix ] run-shell ' + quote(paste_command))
        keys.append(('prefix', ']'))
        for key in ('MouseDown2Pane', 'MouseDown3Pane'):
            lines.append(f'bind-key -T root {key} select-pane -t = \\; run-shell -t = ' + quote(paste_command))
            keys.append(('root', key))
    lines.append(END)
    return '\n'.join(lines) + '\n', keys


def replace_block(text, block):
    if text.count(BEGIN) != text.count(END) or text.count(BEGIN) > 1:
        raise ClipboardError('Malformed managed block; resolve it before installation')
    if BEGIN in text:
        start = text.index(BEGIN)
        end = text.index(END, start) + len(END)
        if text[end:end + 1] == '\n':
            end += 1
        return text[:start] + block + text[end:]
    return text + ('' if not text or text.endswith('\n') else '\n') + '\n' + block


def write_atomic(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.clipboard-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            output.write(data)
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def snapshot(keys, mouse):
    lines = [tmux('show-options', '-s', option).strip() for option in ('set-clipboard', 'copy-command')]
    lines = ['set-option -s ' + line for line in lines]
    if mouse == 'select':
        lines.append('set-option -g ' + tmux('show-options', '-g', 'mouse').strip())
    for table, key in keys:
        result = subprocess.run(['tmux', 'list-keys', '-T', table, key], capture_output=True, text=True, timeout=5)
        lines.append(result.stdout.strip() if result.returncode == 0 else f'unbind-key -T {table} {key}')
    return '\n'.join(lines) + '\n'


def apply_install(config, command, mouse, paste, reload):
    if config.is_symlink():
        raise ClipboardError('Config is a symlink; select its owning source explicitly')
    old = config.read_bytes() if config.exists() else b''
    block, keys = render(command, mouse, paste)
    new = replace_block(old.decode(), block).encode()
    if new == old:
        if reload:
            tmux('source-file', str(config))
        print('Configuration already current')
        return
    if not command.is_file():
        raise ClipboardError('Runtime command does not exist')
    # Validate using a disposable server, including on a fresh installation.
    with tempfile.TemporaryDirectory(prefix='clipboard-config-') as temporary:
        socket = str(Path(temporary) / 'socket')
        prefix = ['tmux', '-S', socket]
        try:
            subprocess.run([*prefix, '-f', '/dev/null', 'new-session', '-d', '-s', 'check', 'sleep 30'], check=True, capture_output=True, timeout=5)
            subprocess.run([*prefix, 'source-file', '-n', '-'], input=new, check=True, capture_output=True, timeout=5)
        except subprocess.SubprocessError as exc:
            raise ClipboardError('tmux configuration validation failed') from exc
        finally:
            subprocess.run([*prefix, 'kill-server'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    rollback = snapshot(keys, mouse) if reload else ''
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = config.parent / (config.name + '.remote-clipboard-' + stamp)
    backup.mkdir(mode=0o700)
    write_atomic(backup / 'original.conf', old)
    write_atomic(backup / 'runtime.conf', rollback.encode())
    receipt = {'config': str(config), 'existed': config.exists(), 'mode': config.stat().st_mode & 0o777 if config.exists() else 0o600,
               'installed_sha256': hashlib.sha256(new).hexdigest(), 'reload': reload,
               'server_pid': tmux('display-message', '-p', '#{pid}').strip() if reload else None,
               'socket': tmux('display-message', '-p', '#{socket_path}').strip() if reload else None}
    write_atomic(backup / 'receipt.json', json.dumps(receipt).encode())
    write_atomic(config, new, receipt['mode'])
    if reload:
        try:
            tmux('source-file', str(config))
        except ClipboardError:
            write_atomic(config, old, receipt['mode'])
            tmux('source-file', str(backup / 'runtime.conf'))
            raise
    print('Installed; rollback backup: ' + str(backup))


def restore(backup):
    receipt = json.loads((backup / 'receipt.json').read_text())
    config = Path(receipt['config'])
    if config.is_symlink() or hashlib.sha256(config.read_bytes()).hexdigest() != receipt['installed_sha256']:
        raise ClipboardError('Configuration changed since installation; refusing to overwrite later edits')
    if receipt['reload']:
        current = (tmux('display-message', '-p', '#{pid}').strip(), tmux('display-message', '-p', '#{socket_path}').strip())
        if current != (receipt['server_pid'], receipt['socket']):
            raise ClipboardError('Select the original running server before restoring its bindings')
        tmux('source-file', str(backup / 'runtime.conf'))
    if receipt['existed']:
        write_atomic(config, (backup / 'original.conf').read_bytes(), receipt['mode'])
    else:
        config.unlink()
    print('Original config and selected runtime bindings restored')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path.home() / '.tmux.conf')
    parser.add_argument('--command', type=Path, default=Path(__file__).with_name('clipboard.py'))
    parser.add_argument('--mouse', choices=('preserve', 'select'), default='preserve')
    parser.add_argument('--paste-bindings', action='store_true')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--reload', action='store_true')
    parser.add_argument('--restore', type=Path)
    args = parser.parse_args()
    if args.restore:
        if not args.apply:
            raise ClipboardError('Restoring requires --apply')
        restore(args.restore.resolve())
    elif args.apply:
        apply_install(args.config.absolute(), args.command.resolve(), args.mouse, args.paste_bindings, args.reload)
    else:
        print(render(args.command, args.mouse, args.paste_bindings)[0], end='')


if __name__ == '__main__':
    try:
        main()
    except (ClipboardError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f'tmux-config: {exc if isinstance(exc, ClipboardError) else "operation failed"}', file=sys.stderr)
        sys.exit(1)
