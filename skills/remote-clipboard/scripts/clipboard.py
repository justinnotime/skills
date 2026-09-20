#!/usr/bin/env python3
"""Local and terminal clipboard operations, with explicit tmux client routing."""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid

BACKENDS = ('auto', 'xclip', 'wayland', 'pbcopy', 'osc52')
ENV_KEYS = ('DISPLAY', 'XAUTHORITY', 'WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR')
LIMIT = 100_000


class ClipboardError(Exception):
    pass


def run(*args, data=None, env=None, output=True):
    try:
        return subprocess.run(args, input=data, env=env, check=True, timeout=5,
                              stdout=subprocess.PIPE if output else subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        # Never include clipboard text, environment values, or command arguments.
        raise ClipboardError(f'{Path(args[0]).name} failed or timed out') from exc


def tmux(*args, data=None):
    return run('tmux', *args, data=data).decode()


def remote(env):
    return any(env.get(key) for key in ('SSH_CONNECTION', 'SSH_CLIENT', 'SSH_TTY', 'MOSH_CONNECTION', 'MOSH_IP'))


def desktop_env(env):
    result = dict(env)
    # This is called only for an explicitly local operation, never for an
    # unregistered tmux client or an SSH/mosh shell.
    stale = not result.get('XAUTHORITY') or not Path(result['XAUTHORITY']).is_file()
    if sys.platform.startswith('linux') and (not result.get('DISPLAY') or stale):
        if shutil.which('systemctl'):
            try:
                text = run('systemctl', '--user', 'show-environment').decode()
                for line in text.splitlines():
                    key, sep, value = line.partition('=')
                    if sep and key in ENV_KEYS:
                        result[key] = value
            except ClipboardError:
                pass
    return result


def local_backend(env):
    if sys.platform == 'darwin' and shutil.which('pbcopy'):
        return 'pbcopy'
    # GNOME's XWayland bridge avoids wl-clipboard's focus-dependent fallback.
    if env.get('DISPLAY') and shutil.which('xclip'):
        return 'xclip'
    if env.get('WAYLAND_DISPLAY') and shutil.which('wl-copy'):
        return 'wayland'
    return 'osc52'


def native_copy(backend, data, env, primary=False):
    if backend == 'pbcopy':
        run('pbcopy', data=data, env=env, output=False)
    elif backend == 'xclip':
        for selection in (('clipboard', 'primary') if primary else ('clipboard',)):
            # xclip forks and retains descriptors: do not capture its stdout.
            run('xclip', '-selection', selection, '-in', data=data, env=env, output=False)
    elif backend == 'wayland':
        run('wl-copy', '--type', 'text/plain;charset=utf-8', data=data, env=env, output=False)
        if primary:
            run('wl-copy', '--primary', '--type', 'text/plain;charset=utf-8', data=data, env=env, output=False)
    else:
        raise ClipboardError('A native clipboard backend is required')


def native_paste(backend, env):
    if backend == 'pbcopy':
        return run('pbpaste', env=env)
    if backend == 'xclip':
        return run('xclip', '-selection', 'clipboard', '-out', env=env)
    if backend == 'wayland':
        return run('wl-paste', '--no-newline', '--type', 'text', env=env)
    raise ClipboardError('Paste using the client terminal (Cmd+V or Ctrl+Shift+V); remote clipboard reads are disabled')


def osc_sequence(data, wrapped=False):
    payload = base64.b64encode(data)
    if len(payload) > LIMIT:
        raise ClipboardError('OSC 52 payload exceeds 100 KB; use file transfer')
    sequence = b'\x1b]52;c;' + payload + b'\x07'
    return b'\x1bPtmux;\x1b' + sequence + b'\x1b\\' if wrapped else sequence


def terminal_copy(data):
    wrapped = bool(os.environ.get('TMUX'))
    sequence = osc_sequence(data, wrapped)
    # Retain the clear/update pair needed by mosh for identical repeated copies.
    try:
        with open('/dev/tty', 'wb', buffering=0) as terminal:
            terminal.write(osc_sequence(b'', wrapped))
            time.sleep(.1)
            terminal.write(sequence)
    except OSError as exc:
        raise ClipboardError('No controlling terminal for OSC 52') from exc


def clients():
    rows = tmux('list-clients', '-F', '#{client_tty}\t#{client_pid}\t#{client_created}\t#{client_session}\t#{client_termfeatures}')
    return [line.split('\t') for line in rows.splitlines() if len(line.split('\t')) == 5]


def client_info(target=None):
    rows = clients()
    if target:
        rows = [row for row in rows if row[0] == target]
    elif os.environ.get('TMUX_PANE'):
        session = tmux('display-message', '-p', '-t', os.environ['TMUX_PANE'], '#{session_name}').strip()
        rows = [row for row in rows if row[3] == session]
    if len(rows) != 1:
        raise ClipboardError('Select exactly one attached client with --client; refusing to guess')
    row = rows[0]
    if not re.fullmatch(r'[0-9]+', row[1]) or not re.fullmatch(r'[0-9]+', row[2]):
        raise ClipboardError('Invalid tmux client identity')
    return row


def client_key(row):
    return '@remote-clipboard-client-' + row[1] + '-' + row[2]


def client_route(row):
    raw = tmux('show-options', '-sqv', client_key(row)).strip()
    if not raw:
        return 'osc52', {}, False
    try:
        value = json.loads(raw)
        backend = value['backend']
        if backend not in BACKENDS[1:]:
            raise ValueError()
        env = {k: str(v) for k, v in value.get('env', {}).items() if k in ENV_KEYS}
        return backend, env, bool(value.get('primary'))
    except (ValueError, KeyError, TypeError) as exc:
        raise ClipboardError('Invalid registered client route') from exc


def register_client(target, backend, primary=False):
    row = client_info(target)
    # Explicit native registration is the boundary; never infer it from a
    # server/session environment left behind by another attached client.
    env = desktop_env(os.environ) if backend != 'osc52' else {}
    value = {'backend': backend, 'primary': primary,
             'env': {k: env[k] for k in ENV_KEYS if k in env}}
    tmux('set-option', '-s', client_key(row), json.dumps(value))


def selected_copy(data, target=None):
    row = client_info(target)
    backend, selected_env, primary = client_route(row)
    if backend != 'osc52':
        env = dict(os.environ)
        for key in ENV_KEYS:
            env.pop(key, None)
        env.update(selected_env)
        native_copy(backend, data, desktop_env(env), primary)
        return
    osc_sequence(data)  # enforce the transfer limit before creating a buffer
    if 'clipboard' not in row[4].split(','):
        raise ClipboardError('Client lacks tmux clipboard capability; check terminal OSC 52 support and terminal-features')
    if not data:
        return
    # mosh only forwards changed clipboard state. Clear the exact validated
    # client's tty first; tmux's load-buffer -w cannot send an empty selection.
    if not row[0].startswith('/dev/'):
        raise ClipboardError('OSC 52 requires a terminal client')
    with open(row[0], 'wb', buffering=0) as terminal:
        if not os.isatty(terminal.fileno()):
            raise ClipboardError('Client is not a terminal')
        terminal.write(osc_sequence(b''))
    time.sleep(.1)
    name = 'remote-clipboard-' + uuid.uuid4().hex
    try:
        tmux('load-buffer', '-w', '-t', row[0], '-b', name, '-', data=data)
    finally:
        tmux('delete-buffer', '-b', name)


def selected_paste(target, pane):
    if not re.fullmatch(r'%[0-9]+', pane):
        raise ClipboardError('Expected an exact tmux pane ID')
    row = client_info(target)
    backend, selected_env, _ = client_route(row)
    if backend == 'osc52':
        tmux('display-message', '-c', row[0], 'Paste with your terminal: Cmd+V / Ctrl+Shift+V. Server desktop clipboard is not used.')
        return
    env = dict(os.environ)
    for key in ENV_KEYS:
        env.pop(key, None)
    env.update(selected_env)
    data = native_paste(backend, desktop_env(env))
    if not data:
        return
    name = 'remote-clipboard-' + uuid.uuid4().hex
    try:
        tmux('load-buffer', '-b', name, '-', data=data)
        tmux('paste-buffer', '-p', '-d', '-b', name, '-t', pane)
    finally:
        # paste-buffer -d normally already removed it.
        subprocess.run(['tmux', 'delete-buffer', '-b', name], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=5)


def read_input(files):
    return b''.join(Path(f).read_bytes() if f != '-' else sys.stdin.buffer.read() for f in files) if files else sys.stdin.buffer.read()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    for action in ('copy', 'paste'):
        sub = commands.add_parser(action)
        sub.add_argument('--backend', choices=BACKENDS, default='auto')
        sub.add_argument('--client')
        if action == 'copy':
            sub.add_argument('--primary', action='store_true')
            sub.add_argument('files', nargs='*')
    for action in ('tmux-copy', 'tmux-paste', 'client'):
        sub = commands.add_parser(action)
        sub.add_argument('--client')
        if action == 'tmux-paste':
            sub.add_argument('--pane', required=True)
        if action == 'client':
            sub.add_argument('--backend', choices=BACKENDS[1:], required=True)
            sub.add_argument('--primary', action='store_true')
    sub = commands.add_parser('tmux', help='start/attach tmux and register this connection')
    sub.add_argument('--backend', choices=BACKENDS, default='auto')
    sub.add_argument('--primary', action='store_true')
    sub.add_argument('args', nargs=argparse.REMAINDER)
    sub = commands.add_parser('terminal', help='run a connection with local OSC 52 clipboard support')
    sub.add_argument('--backend', choices=('auto', 'pbcopy', 'xclip', 'wayland'), default='auto')
    sub.add_argument('args', nargs=argparse.REMAINDER)
    commands.add_parser('doctor')
    args = parser.parse_args(argv)
    if args.action == 'client':
        register_client(args.client, args.backend, args.primary)
    elif args.action == 'tmux-copy':
        selected_copy(sys.stdin.buffer.read(), args.client)
    elif args.action == 'tmux-paste':
        selected_paste(args.client, args.pane)
    elif args.action == 'tmux':
        if os.environ.get('TMUX'):
            raise ClipboardError('Run the tmux launcher outside tmux; use client to register an existing connection')
        env = dict(os.environ)
        backend = args.backend
        if backend == 'auto':
            env = env if remote(env) else desktop_env(env)
            backend = 'osc52' if remote(env) else local_backend(env)
        os.environ.update({k: env[k] for k in ENV_KEYS if k in env})
        command = shlex.join([sys.executable, str(Path(__file__).resolve()), 'client', '--backend', backend])
        command += ' --client #{q:client_tty}' + (' --primary' if args.primary else '')
        tail = args.args[1:] if args.args[:1] == ['--'] else args.args
        if not tail:
            tail = ['new-session', '-A', '-s', 'main']
        os.execvp('tmux', ['tmux', *tail, ';', 'run-shell', command])
    elif args.action == 'terminal':
        if remote(os.environ):
            raise ClipboardError('Run clip-terminal on the local desktop, before connecting over SSH/mosh')
        env = desktop_env(os.environ)
        backend = local_backend(env) if args.backend == 'auto' else args.backend
        if backend == 'osc52':
            raise ClipboardError('clip-terminal needs a local native clipboard backend')
        command = args.args[1:] if args.args[:1] == ['--'] else args.args
        from terminal_bridge import run_terminal
        try:
            code = run_terminal(command, lambda data: native_copy(backend, data, env))
        except ValueError as exc:
            raise ClipboardError(str(exc)) from exc
        raise SystemExit(code)
    elif args.action == 'doctor':
        report = {'python': sys.version.split()[0], 'remote_shell': remote(os.environ),
                  'inside_tmux': bool(os.environ.get('TMUX')),
                  'commands': {c: bool(shutil.which(c)) for c in ('tmux', 'xclip', 'wl-copy', 'wl-paste', 'pbcopy', 'pbpaste')}}
        if os.environ.get('TMUX'):
            report['clients'] = [{'backend': client_route(r)[0], 'osc52_capability': 'clipboard' in r[4].split(',')} for r in clients()]
        print(json.dumps(report, indent=2))
    else:
        backend = args.backend
        if args.client or (backend == 'auto' and os.environ.get('TMUX')):
            if args.action == 'paste':
                raise ClipboardError('Use terminal paste or tmux-paste with an explicit target pane')
            selected_copy(read_input(args.files), args.client)
            return
        env = dict(os.environ)
        if backend == 'auto':
            env = env if remote(env) else desktop_env(env)
            backend = 'osc52' if remote(env) else local_backend(env)
        elif backend != 'osc52':
            env = desktop_env(env)
        if args.action == 'paste':
            sys.stdout.buffer.write(native_paste(backend, env))
        else:
            data = read_input(args.files)
            if backend == 'osc52':
                terminal_copy(data)
            else:
                native_copy(backend, data, env, args.primary)


if __name__ == '__main__':
    try:
        main()
    except (ClipboardError, OSError, subprocess.SubprocessError) as exc:
        print(f'clipboard: {exc if isinstance(exc, ClipboardError) else "operation failed"}', file=sys.stderr)
        sys.exit(1)
