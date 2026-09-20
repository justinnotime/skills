"""Forward an interactive command, consuming only OSC 52 clipboard writes."""
from __future__ import annotations

import base64
import binascii
import errno
import fcntl
import os
import pty
import re
import select
import signal
import termios
import time
import tty

START = re.compile(rb"\x1b[\]PX^_]")
OSC_END = re.compile(rb"\x07|\x1b\\")
STRING_END = re.compile(rb"\x1b\\")


class ClipboardFilter:
    def __init__(self, copy, report, limit=100_000):
        self.copy, self.report, self.limit = copy, report, limit
        self.pending = b''
        self.passthrough = None

    def feed(self, data):
        self.pending += data
        output = bytearray()
        while self.pending:
            if self.passthrough is not None:
                end = self.passthrough.search(self.pending)
                if end:
                    output.extend(self.pending[:end.end()])
                    self.pending = self.pending[end.end():]
                    self.passthrough = None
                    continue
                keep = 1 if self.pending.endswith(b'\x1b') else 0
                output.extend(self.pending[:-keep] if keep else self.pending)
                self.pending = self.pending[-keep:] if keep else b''
                break
            start = START.search(self.pending)
            if not start:
                keep = 1 if self.pending.endswith(b'\x1b') else 0
                output.extend(self.pending[:-keep] if keep else self.pending)
                self.pending = self.pending[-keep:] if keep else b''
                break
            output.extend(self.pending[:start.start()])
            self.pending = self.pending[start.start():]
            ending = OSC_END if self.pending[1:2] == b']' else STRING_END
            end = ending.search(self.pending, 2)
            if not end:
                if len(self.pending) > self.limit + 128:
                    self.passthrough = ending
                    if self.pending.startswith(b'\x1b]52;'):
                        self.report('OSC 52 payload exceeds 100 KB; use file transfer')
                    continue
                break
            sequence = self.pending[:end.end()]
            body = self.pending[2:end.start()]
            self.pending = self.pending[end.end():]
            if sequence.startswith(b'\x1b]52;') and self.clipboard(body[3:]):
                continue
            output.extend(sequence)
        return bytes(output)

    def clipboard(self, body):
        selection, separator, payload = body.partition(b';')
        # Default and explicit clipboard writes only. Never read the clipboard,
        # answer queries, or interpret a nested OSC inside a DCS string.
        if not separator or selection not in (b'', b'c') or payload == b'?':
            return False
        if len(payload) > self.limit:
            self.report('OSC 52 payload exceeds 100 KB; use file transfer')
            return True
        try:
            data = base64.b64decode(payload, validate=True)
        except (ValueError, binascii.Error):
            self.report('Invalid OSC 52 clipboard payload')
            return True
        try:
            self.copy(data)
        except Exception:
            self.report('Local clipboard write failed; connection remains open')
        return True

    def finish(self):
        data, self.pending = self.pending, b''
        return data


def write_all(fd, data):
    while data:
        written = os.write(fd, data)
        data = data[written:]


def run_terminal(command, copy):
    if not command or not os.isatty(0) or not os.isatty(1):
        raise ValueError('clip-terminal requires a command and an interactive local terminal')
    original = termios.tcgetattr(0)
    size = fcntl.ioctl(0, termios.TIOCGWINSZ, b'\0' * 8)
    pid, master = pty.fork()
    if pid == 0:
        try:
            fcntl.ioctl(0, termios.TIOCSWINSZ, size)
            os.execvp(command[0], command)
        except OSError:
            os.write(2, b'clip-terminal: could not start command\r\n')
            os._exit(127)
    previous = {}
    normal_exit = False
    def resize(*_):
        fcntl.ioctl(master, termios.TIOCSWINSZ,
                    fcntl.ioctl(0, termios.TIOCGWINSZ, b'\0' * 8))
    def stop(signum, _):
        raise SystemExit(128 + signum)
    def report(message):
        write_all(2, ('\r\nclip-terminal: ' + message + '\r\n').encode())
    parser = ClipboardFilter(copy, report)
    try:
        for signum, handler in ((signal.SIGWINCH, resize), (signal.SIGHUP, stop),
                                (signal.SIGTERM, stop)):
            previous[signum] = signal.signal(signum, handler)
        tty.setraw(0)
        resize()
        while True:
            readable, _, _ = select.select([0, master], [], [])
            if master in readable:
                try:
                    data = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    data = b''
                if not data:
                    break
                write_all(1, parser.feed(data))
            if 0 in readable:
                data = os.read(0, 65536)
                if not data:
                    break
                write_all(master, data)
        write_all(1, parser.finish())
        normal_exit = True
    finally:
        try:
            termios.tcsetattr(0, termios.TCSANOW, original)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)
            os.close(master)
            # Closing the PTY hangs up only this connection's child, never tmux's
            # remote server. Reap it even when the wrapper receives a signal.
            deadline = time.monotonic() + 2
            while True:
                waited, status = os.waitpid(pid, os.WNOHANG)
                if waited:
                    break
                if time.monotonic() >= deadline:
                    os.kill(pid, signal.SIGKILL)
                    _, status = os.waitpid(pid, 0)
                    break
                time.sleep(.02)
    if normal_exit:
        code = os.waitstatus_to_exitcode(status)
        return code if code >= 0 else 128 - code
