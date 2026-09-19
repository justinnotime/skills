"""Real tmux servers and pseudoterminals; no desktop, account or network access."""
from __future__ import annotations

import base64
import fcntl
import os
from pathlib import Path
import pty
import select
import shutil
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'


def wait_for(check):
    limit = time.monotonic() + 5
    while time.monotonic() < limit:
        if check(): return
        time.sleep(.025)
    raise AssertionError('condition not reached')


@unittest.skipUnless(shutil.which('tmux'), 'tmux is required for integration checks')
class TmuxTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='clipboard-test-')
        self.root = Path(self.temporary.name)
        self.socket = str(self.root / 'socket')
        self.env = {k: v for k, v in os.environ.items() if k not in ('TMUX', 'TMUX_PANE', 'DISPLAY', 'XAUTHORITY', 'WAYLAND_DISPLAY', 'SSH_CONNECTION', 'SSH_CLIENT', 'SSH_TTY')}
        self.env.update(TERM='xterm-256color', PATH=str(self.root) + ':' + self.env['PATH'])
        self.env['TEST_CLIPBOARD'] = str(self.root / 'clipboard')
        authority = self.root / 'authority'
        authority.touch()
        self.fake('systemctl', f"print('DISPLAY=:test\\nXAUTHORITY={authority}')")
        self.fake('xclip', "import sys,os\nfrom pathlib import Path\np=Path(os.environ['TEST_CLIPBOARD']+'-'+sys.argv[2])\nif '-in' in sys.argv: p.write_bytes(sys.stdin.buffer.read())\nelse: sys.stdout.buffer.write(p.read_bytes())")
        self.receiver = self.root / 'receiver.py'
        self.received = self.root / 'received'
        self.receiver.write_text("import os,sys,tty\ntty.setraw(0)\nos.write(1,b'\\x1b[?2004hclipboard-test')\nf=open(sys.argv[1],'wb',buffering=0)\nwhile True:\n data=os.read(0,4096)\n if not data: break\n f.write(data)\n")
        import shlex
        command = shlex.join([sys.executable, str(self.receiver), str(self.received)])
        self.tm('-f', '/dev/null', 'new-session', '-d', '-s', 'test', '-x', '100', '-y', '30', command)
        self.env['TMUX'] = self.socket + ',' + self.tm('display-message', '-p', '#{pid}').strip() + ',0'
        self.env['TMUX_PANE'] = '%0'
        self.attached = []
        wait_for(self.received.exists)

    def fake(self, name, body):
        path = self.root / name
        path.write_text('#!' + sys.executable + '\n' + body + '\n')
        path.chmod(0o755)

    def tm(self, *args, data=None):
        return subprocess.run(['tmux', '-S', self.socket, *args], input=data, env=self.env, capture_output=True, check=True, timeout=5).stdout.decode()

    def cli(self, *args, data=None):
        return subprocess.run([sys.executable, str(SCRIPTS / 'clipboard.py'), *args], input=data, env=self.env, capture_output=True, check=True, timeout=5)

    def attach(self):
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 100, 0, 0))
        tty = os.ttyname(slave)
        env = dict(self.env)
        env.pop('TMUX', None)
        process = subprocess.Popen(['tmux', '-S', self.socket, 'attach-session', '-t', 'test'], stdin=slave, stdout=slave, stderr=slave, env=env)
        os.close(slave)
        self.attached.append((master, process))
        wait_for(lambda: tty in self.tm('list-clients', '-F', '#{client_tty}'))
        self.drain(master)
        return tty, master

    def drain(self, master, duration=.12):
        data = bytearray()
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], max(0, deadline-time.monotonic()))
            if ready:
                try: data.extend(os.read(master, 65536))
                except OSError: break
        return bytes(data)

    def install(self, *extra):
        conf = self.root / 'tmux.conf'
        if not conf.exists(): conf.write_text('set -g history-limit 98765\n')
        result = subprocess.run([sys.executable, str(SCRIPTS / 'tmux-config.py'), '--config', str(conf), '--apply', '--reload', *extra], env=self.env, capture_output=True, check=True, timeout=10)
        return conf, result.stdout.decode()

    def tearDown(self):
        subprocess.run(['tmux', '-S', self.socket, 'kill-server'], capture_output=True, timeout=5)
        for master, process in self.attached:
            process.wait(timeout=5)
            os.close(master)
        self.temporary.cleanup()

    def test_launcher_registers_each_new_connection(self):
        master, slave = pty.openpty()
        tty = os.ttyname(slave)
        env = dict(self.env)
        env.pop('TMUX', None)
        env.pop('TMUX_PANE', None)
        env['SSH_TTY'] = '/dev/pts/synthetic'
        process = subprocess.Popen([sys.executable, str(SCRIPTS/'clipboard.py'), 'tmux', '--', '-S', self.socket, 'attach-session', '-t', 'test'], stdin=slave, stdout=slave, stderr=slave, env=env)
        os.close(slave)
        self.attached.append((master, process))
        wait_for(lambda: tty in self.tm('list-clients', '-F', '#{client_tty}'))
        wait_for(lambda: 'remote-clipboard-client-' in self.tm('show-options', '-s'))
        self.assertIn('osc52', self.tm('show-options', '-s'))
        self.assertFalse((self.root/'clipboard-clipboard').exists())

    def test_copy_reaches_only_initiating_client(self):
        local, first = self.attach()
        remote, second = self.attach()
        self.install('--mouse', 'select', '--paste-bindings')
        self.cli('client', '--client', local, '--backend', 'xclip', '--primary')
        self.cli('tmux-copy', '--client', local, data='本地\n'.encode())
        self.assertEqual((self.root/'clipboard-clipboard').read_bytes(), '本地\n'.encode())
        self.assertEqual((self.root/'clipboard-primary').read_bytes(), '本地\n'.encode())
        self.drain(first)
        self.drain(second)
        payload = b'remote-client-only'
        self.cli('tmux-copy', '--client', remote, data=payload)
        encoded = base64.b64encode(payload)
        self.assertIn(encoded, self.drain(second))
        self.assertNotIn(encoded, self.drain(first))
        self.assertEqual((self.root/'clipboard-clipboard').read_bytes(), '本地\n'.encode())

    def test_actual_copy_mode_binding_uses_triggering_client(self):
        local, first = self.attach()
        remote, second = self.attach()
        self.install()
        self.cli('client', '--client', local, '--backend', 'xclip')
        self.tm('copy-mode', '-t', '%0')
        for action in ('history-top', 'start-of-line', 'begin-selection', 'end-of-line'):
            self.tm('send-keys', '-t', '%0', '-X', action)
        self.drain(first)
        self.drain(second)
        os.write(second, b'\r')
        output = self.drain(second, 1)
        self.assertIn(base64.b64encode(b'clipboard-test'), output)
        self.assertNotIn(base64.b64encode(b'clipboard-test'), self.drain(first))
        self.assertFalse((self.root/'clipboard-clipboard').exists())

    def test_local_paste_preserves_unicode_lines_and_brackets(self):
        local, _ = self.attach()
        self.install('--paste-bindings')
        self.cli('client', '--client', local, '--backend', 'xclip')
        payload = '中文\nnext\n\n'.encode()
        (self.root/'clipboard-clipboard').write_bytes(payload)
        self.cli('tmux-paste', '--client', local, '--pane', '%0')
        expected = b'\x1b[200~' + payload.replace(b'\n', b'\r') + b'\x1b[201~'
        wait_for(lambda: self.received.read_bytes() == expected)
        self.assertNotIn('remote-clipboard-', self.tm('list-buffers', '-F', '#{buffer_name}'))

    def test_remote_paste_does_not_inject_server_clipboard(self):
        remote, _ = self.attach()
        self.install('--paste-bindings')
        (self.root/'clipboard-clipboard').write_bytes(b'must-not-paste')
        self.cli('tmux-paste', '--client', remote, '--pane', '%0')
        self.assertEqual(self.received.read_bytes(), b'')

    def test_install_idempotence_and_live_rollback(self):
        self.tm('bind-key', '-T', 'root', 'MouseDown3Pane', 'display-message', 'custom original')
        original = self.tm('list-keys', '-T', 'root', 'MouseDown3Pane')
        before_clipboard = self.tm('show-options', '-s', 'set-clipboard')
        conf, output = self.install('--mouse', 'select', '--paste-bindings')
        installed = conf.read_bytes()
        self.install('--mouse', 'select', '--paste-bindings')
        self.assertEqual(conf.read_bytes(), installed)
        backup = output.strip().split('rollback backup: ', 1)[1]
        subprocess.run([sys.executable, str(SCRIPTS/'tmux-config.py'), '--restore', backup, '--apply'], env=self.env, check=True, capture_output=True, timeout=5)
        self.assertEqual(conf.read_text(), 'set -g history-limit 98765\n')
        self.assertEqual(self.tm('list-keys', '-T', 'root', 'MouseDown3Pane'), original)
        self.assertEqual(self.tm('show-options', '-s', 'set-clipboard'), before_clipboard)

    def test_rollback_refuses_later_user_edits(self):
        conf, output = self.install()
        conf.write_text(conf.read_text() + '# later user edit\n')
        backup = output.strip().split('rollback backup: ', 1)[1]
        result = subprocess.run([sys.executable, str(SCRIPTS/'tmux-config.py'), '--restore', backup, '--apply'], env=self.env, capture_output=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(conf.read_text().endswith('# later user edit\n'))


if __name__ == '__main__': unittest.main()
