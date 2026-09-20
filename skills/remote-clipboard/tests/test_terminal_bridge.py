from __future__ import annotations

import base64
import fcntl
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
from terminal_bridge import ClipboardFilter


class FilterTests(unittest.TestCase):
    def test_split_sequences_repeat_unicode_and_ordinary_output(self):
        copies, errors = [], []
        parser = ClipboardFilter(copies.append, errors.append)
        payload = '中文\nline\n'.encode()
        encoded = base64.b64encode(payload)
        unrelated = b'\x1b[31mred\x1b[0m\x1b]0;title\x07'
        sequence = b'\x1b]52;c;' + encoded + b'\x07'
        default = b'\x1b]52;;' + encoded + b'\x1b\\'
        stream = unrelated + sequence + b'middle' + default + b'end\x1b'
        result = b''.join(parser.feed(bytes([value])) for value in stream) + parser.finish()
        self.assertEqual(result, unrelated + b'middleend\x1b')
        self.assertEqual(copies, [payload, payload])
        self.assertEqual(errors, [])

    def test_queries_and_other_control_strings_do_not_read_or_write_clipboard(self):
        copies, errors = [], []
        parser = ClipboardFilter(copies.append, errors.append)
        stream = (b'\x1b]52;c;?\x07' + b'\x1b]52;p;dGV4dA==\x07'
                  + b'\x1bPtmux;\x1b\x1b]52;c;dGV4dA==\x07\x1b\\')
        output = b''.join(parser.feed(bytes([value])) for value in stream)
        self.assertEqual(output + parser.finish(), stream)
        self.assertEqual(copies, [])
        self.assertEqual(errors, [])

    def test_invalid_and_oversized_payloads_leave_connection_usable(self):
        copies, errors = [], []
        parser = ClipboardFilter(copies.append, errors.append, limit=8)
        parser.feed(b'\x1b]52;c;invalid*\x07')
        parser.feed(b'\x1b]52;c;' + b'A' * 12 + b'\x07')
        for _ in range(200):
            parser.feed(b'A' if parser.pending else b'\x1b]52;c;' + b'A' * 200)
            self.assertLessEqual(len(parser.pending), 136)
        parser.feed(b'\x07')
        self.assertEqual(parser.feed(b'after\x1b]52;c;T0s=\x07'), b'after')
        self.assertEqual(copies, [b'OK'])
        self.assertTrue(errors)

    def test_failed_copy_reports_no_payload_and_keeps_output(self):
        errors = []
        def fail(data):
            raise RuntimeError(data.decode())
        parser = ClipboardFilter(fail, errors.append)
        output = parser.feed(b'before\x1b]52;c;c2VjcmV0\x07after')
        self.assertEqual(output, b'beforeafter')
        self.assertEqual(len(errors), 1)
        self.assertNotIn('secret', errors[0])


class TerminalTests(unittest.TestCase):
    def test_resize_input_exit_status_and_terminal_restoration(self):
        with tempfile.TemporaryDirectory(prefix='clipboard-bridge-test-') as directory:
            root = Path(directory)
            child = root / 'child.py'
            child.write_text("import os,signal,sys,tty\n"
                             "tty.setraw(0)\n"
                             "def size(*args):\n"
                             " s=os.get_terminal_size(0); print(f'SIZE={s.columns}x{s.lines}',flush=True)\n"
                             "signal.signal(signal.SIGWINCH,size)\nsize()\n"
                             "data=os.read(0,5)\nprint('INPUT='+data.hex(),flush=True)\nsys.exit(7)\n")
            master, slave = pty.openpty()
            original = termios.tcgetattr(slave)
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 80, 0, 0))
            env = {k:v for k,v in os.environ.items() if k not in ('TMUX','SSH_CONNECTION','SSH_CLIENT','SSH_TTY','MOSH_CONNECTION','MOSH_IP')}
            process = subprocess.Popen([str(SCRIPTS/'clip-terminal'), '--backend', 'xclip', '--', sys.executable, str(child)], stdin=slave, stdout=slave, stderr=slave, env=env)
            output = bytearray()
            def until(marker):
                deadline = time.monotonic() + 5
                while marker not in output and time.monotonic() < deadline:
                    if select.select([master], [], [], .1)[0]:
                        output.extend(os.read(master,65536))
                self.assertIn(marker, output)
            try:
                until(b'SIZE=80x24')
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 120, 0, 0))
                process.send_signal(signal.SIGWINCH)
                until(b'SIZE=120x40')
                os.write(master,b'hello')
                until(b'INPUT=68656c6c6f')
                self.assertEqual(process.wait(timeout=5), 7)
                restored = termios.tcgetattr(slave)
                # macOS marks input for reprocessing when canonical mode returns.
                restored[3] &= ~getattr(termios, 'PENDIN', 0)
                original[3] &= ~getattr(termios, 'PENDIN', 0)
                self.assertEqual(restored, original)
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                os.close(master)
                os.close(slave)

    def test_interrupted_wrapper_restores_terminal(self):
        master, slave = pty.openpty()
        original = termios.tcgetattr(slave)
        env = {k:v for k,v in os.environ.items() if k not in ('TMUX','SSH_CONNECTION','SSH_CLIENT','SSH_TTY','MOSH_CONNECTION','MOSH_IP')}
        process = subprocess.Popen([str(SCRIPTS/'clip-terminal'), '--backend', 'xclip', '--',
            sys.executable, '-c', 'import signal; print("READY",flush=True); signal.pause()'],
            stdin=slave, stdout=slave, stderr=slave, env=env)
        output = b''
        try:
            deadline = time.monotonic() + 5
            while b'READY' not in output and time.monotonic() < deadline:
                if select.select([master], [], [], .1)[0]:
                    output += os.read(master,65536)
            self.assertIn(b'READY', output)
            process.terminate()
            self.assertEqual(process.wait(timeout=5), 128 + signal.SIGTERM)
            restored = termios.tcgetattr(slave)
            restored[3] &= ~getattr(termios, 'PENDIN', 0)
            original[3] &= ~getattr(termios, 'PENDIN', 0)
            self.assertEqual(restored, original)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            os.close(master)
            os.close(slave)

    def test_launcher_follows_symlink_and_requires_interactive_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            link = Path(directory)/'clip-terminal'
            link.symlink_to(SCRIPTS/'clip-terminal')
            result = subprocess.run([str(link),'--help'],capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertIn(b'--backend',result.stdout)
            result = subprocess.run([str(link),'--backend','xclip','--','true'],capture_output=True)
            self.assertNotEqual(result.returncode,0)


if __name__ == '__main__':
    unittest.main()
