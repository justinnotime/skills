from __future__ import annotations

import base64
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
import clipboard as cb
spec = importlib.util.spec_from_file_location('config', SCRIPTS / 'tmux-config.py')
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)


class RoutingTests(unittest.TestCase):
    def test_ssh_overrides_desktop_presence(self):
        with patch.dict(os.environ, {'SSH_TTY': '/dev/pts/9', 'DISPLAY': ':test'}, clear=True), patch.object(cb, 'desktop_env') as desktop, patch.object(cb, 'terminal_copy') as terminal, patch.object(cb, 'read_input', return_value=b'synthetic'):
            cb.main(['copy'])
        terminal.assert_called_once_with(b'synthetic')
        desktop.assert_not_called()

    def test_macos_uses_native_tools(self):
        with patch.object(cb.sys, 'platform', 'darwin'), patch.object(cb.shutil, 'which', return_value='/usr/bin/pbcopy'):
            self.assertEqual(cb.local_backend({}), 'pbcopy')
        with patch.object(cb, 'run', return_value=b'line\n\n') as run:
            self.assertEqual(cb.native_paste('pbcopy', {}), b'line\n\n')
            self.assertEqual(run.call_args.args, ('pbpaste',))

    def test_native_selection_preserves_bytes_and_does_not_capture_daemon_stdout(self):
        with patch.object(cb, 'run') as run:
            cb.native_copy('xclip', '中文\n\n'.encode(), {}, True)
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs['data'], '中文\n\n'.encode())
            self.assertFalse(call.kwargs['output'])

    def test_wayland_avoids_adding_a_newline(self):
        with patch.object(cb, 'run', return_value=b'text\n\n') as run:
            self.assertEqual(cb.native_paste('wayland', {}), b'text\n\n')
        self.assertIn('--no-newline', run.call_args.args)

    def test_remote_paste_never_reads_server_desktop(self):
        row = ['/dev/pts/9', '10', '20', 'test', 'clipboard']
        with patch.object(cb, 'client_info', return_value=row), patch.object(cb, 'client_route', return_value=('osc52', {}, False)), patch.object(cb, 'tmux') as tmux, patch.object(cb, 'native_paste') as native:
            cb.selected_paste(row[0], '%1')
        native.assert_not_called()
        self.assertEqual(tmux.call_args.args[0], 'display-message')

    def test_unregistered_client_never_inherits_native_backend(self):
        row = ['/dev/pts/9', '10', '20', 'test', 'clipboard']
        with patch.object(cb, 'tmux', return_value=''):
            self.assertEqual(cb.client_route(row), ('osc52', {}, False))

    def test_multiple_clients_require_explicit_target(self):
        rows = [['/dev/pts/1', '10', '20', 'test', 'clipboard'], ['/dev/pts/2', '11', '20', 'test', 'clipboard']]
        with patch.dict(os.environ, {}, clear=True), patch.object(cb, 'clients', return_value=rows):
            with self.assertRaises(cb.ClipboardError): cb.client_info()
            self.assertEqual(cb.client_info('/dev/pts/2'), rows[1])

    def test_reconnected_client_has_new_registration_identity(self):
        self.assertNotEqual(cb.client_key(['tty', '10', '20']), cb.client_key(['tty', '10', '21']))

    def test_osc52_roundtrip_and_passthrough(self):
        data = '中文\nsecond line\n'.encode()
        plain = cb.osc_sequence(data)
        self.assertEqual(base64.b64decode(plain[7:-1]), data)
        self.assertEqual(cb.osc_sequence(data, True), b'\x1bPtmux;\x1b' + plain + b'\x1b\\')
        with self.assertRaises(cb.ClipboardError): cb.osc_sequence(b'a' * 100001)

    def test_missing_terminal_capability_fails_without_buffer(self):
        with patch.object(cb, 'client_info', return_value=['tty', '10', '20', 's', '']), patch.object(cb, 'client_route', return_value=('osc52', {}, False)), patch.object(cb, 'tmux') as tmux:
            with self.assertRaises(cb.ClipboardError): cb.selected_copy(b'text')
        tmux.assert_not_called()

    def test_failed_paste_read_does_not_paste_stale_buffer(self):
        with patch.object(cb, 'client_info', return_value=['tty', '10', '20', 's', '']), patch.object(cb, 'client_route', return_value=('xclip', {}, False)), patch.object(cb, 'desktop_env', return_value={}), patch.object(cb, 'native_paste', side_effect=cb.ClipboardError('unavailable')), patch.object(cb, 'tmux') as tmux:
            with self.assertRaises(cb.ClipboardError): cb.selected_paste('tty', '%1')
        tmux.assert_not_called()

    def test_stale_desktop_authority_is_resolved_without_copying_other_environment(self):
        with patch.object(cb.sys, 'platform', 'linux'), patch.object(cb.shutil, 'which', return_value='/usr/bin/systemctl'), patch.object(cb, 'run', return_value=b'DISPLAY=:test\nXAUTHORITY=/test/authority\nUNRELATED_SETTING=ignored\n'):
            result = cb.desktop_env({'XAUTHORITY': '/does/not/exist'})
        self.assertEqual(result['XAUTHORITY'], '/test/authority')
        self.assertNotIn('UNRELATED_SETTING', result)


class ConfigTests(unittest.TestCase):
    def test_managed_edit_preserves_user_content_and_is_idempotent(self):
        before = '# user settings\nset -g history-limit 98765\n'
        block, _ = config.render(SCRIPTS / 'clipboard.py')
        after = config.replace_block(before, block)
        self.assertTrue(after.startswith(before))
        self.assertEqual(config.replace_block(after, block), after)

    def test_malformed_markers_refuse(self):
        for text in (config.BEGIN, config.END, config.BEGIN + config.BEGIN + config.END + config.END):
            with self.assertRaises(cb.ClipboardError): config.replace_block(text, '')

    def test_default_leaves_mouse_drag_paste_and_wheel_ownership(self):
        _, keys = config.render(SCRIPTS / 'clipboard.py')
        self.assertNotIn(('root', 'MouseDrag1Pane'), keys)
        self.assertNotIn(('root', 'MouseDown3Pane'), keys)
        self.assertNotIn(('prefix', ']'), keys)
        self.assertFalse(any('Wheel' in key for _, key in keys))

    def test_standalone_launcher_follows_deployment_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            link = Path(directory) / 'clip-tmux'
            link.symlink_to(SCRIPTS / 'clip-tmux')
            result = subprocess.run([str(link), '--help'], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(b'--backend', result.stdout)

    def test_source_compatibility_link_from_bash_and_zsh(self):
        import shutil
        with tempfile.TemporaryDirectory() as directory:
            link = Path(directory) / 'clip.sh'
            link.symlink_to(SCRIPTS / 'clip.sh')
            for shell in ('bash', 'zsh'):
                if not shutil.which(shell): continue
                result = subprocess.run([shell, '-c', '. "$1"; test -f "$_REMOTE_CLIPBOARD_COMMAND"; typeset -f clip >/dev/null', 'shell', str(link)], capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__': unittest.main()
