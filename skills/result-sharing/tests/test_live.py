import base64
import hashlib
import hmac
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/live.py'
spec = importlib.util.spec_from_file_location('live', SCRIPT)
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'custom-task-root'
        self.root.mkdir()
        self.gateway = self.base / 'gateway.json'
        self.gateway.write_text(json.dumps({'applications': [{'id': 'reports', 'origin': 'https://files.example.test:9443'}]}))

    def test_url_reads_native_origin_and_quotes_path(self):
        file = self.root / '结果 #1%.md'
        file.write_text('report')
        result = live.live_url(self.root, file, self.gateway, 'reports', 'team')
        self.assertEqual(result, 'https://files.example.test:9443/files/team/%E7%BB%93%E6%9E%9C%20%231%25.md')
        self.assertEqual(live.live_url(self.root, self.root, self.gateway, 'reports', 'ops'), 'https://files.example.test:9443/files/ops/')

    def test_url_refuses_outside_missing_and_symlink_targets(self):
        outside = self.base / 'private.txt'
        outside.write_text('outside')
        (self.root / 'link').symlink_to(outside)
        for target in (outside, self.root / 'absent', self.root / 'link'):
            with self.subTest(target=target), self.assertRaises((ValueError, OSError)):
                live.live_url(self.root, target, self.gateway, 'reports', 'ops')

    def test_origin_and_source_validation(self):
        for origin in ('http://files.example.test', 'https://user:secret@files.example.test', 'https://files.example.test/?token=secret'):
            self.gateway.write_text(json.dumps({'applications': [{'id': 'reports', 'origin': origin}]}))
            with self.assertRaises(ValueError):
                live.live_url(self.root, self.root, self.gateway, 'reports', 'ops')
        with self.assertRaises(ValueError):
            live.live_url(self.root, self.root, self.gateway, 'absent', '../escape')

    def test_credentials_are_paired_private_and_preserved(self):
        directory = self.base / 'auth'
        first = subprocess.run([sys.executable, str(SCRIPT), 'init-auth', '--directory', str(directory), '--root', str(self.root)], capture_output=True, text=True, check=True)
        original = {p.name: p.read_bytes() for p in directory.iterdir()}
        basic = json.loads(original['results.json'])['authorization']
        password = base64.b64decode(basic.split()[1]).decode().split(':', 1)[1]
        digest = base64.b64encode(hashlib.sha1(password.encode()).digest()).decode()
        self.assertEqual(original['results.htpasswd'].decode(), 'gateway:{SHA}' + digest + '\n')
        secret = json.loads(original['quantum-auth.yaml'].decode().splitlines()[0].split('&backend_jwt_secret ', 1)[1])
        token = original['quantum-upstream.conf'].decode().split('"')[1]
        header, claims, signature = token.split('.')
        expected = base64.urlsafe_b64encode(hmac.new(secret.encode(), (header + '.' + claims).encode(), hashlib.sha256).digest()).rstrip(b'=').decode()
        self.assertEqual(signature, expected)
        self.assertEqual(json.loads(base64.urlsafe_b64decode(claims + '===')), {'sub': 'reader'})
        for value in (secret, password, basic, token):
            self.assertNotIn(value, first.stdout + first.stderr)
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        for path in directory.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(live.init_auth(directory, self.root).startswith('EXISTS'))
        self.assertEqual(original, {p.name: p.read_bytes() for p in directory.iterdir()})

    def test_unsafe_and_partial_sets_are_not_overwritten(self):
        directory = self.base / 'partial'
        directory.mkdir(mode=0o700)
        sentinel = directory / 'results.json'
        sentinel.write_text('private sentinel')
        sentinel.chmod(0o600)
        with self.assertRaises(ValueError):
            live.init_auth(directory, self.root)
        self.assertEqual(sentinel.read_text(), 'private sentinel')
        self.assertEqual(list(directory.iterdir()), [sentinel])
        for path in (self.root / 'auth', self.root):
            with self.assertRaises(ValueError):
                live.init_auth(path, self.root)
        unsafe = self.base / 'unsafe'
        unsafe.mkdir(mode=0o755)
        with self.assertRaises(ValueError):
            live.init_auth(unsafe, self.root)

    def test_error_does_not_echo_configuration(self):
        self.gateway.write_text('broken configuration with private sentinel')
        run = subprocess.run([sys.executable, str(SCRIPT), 'url', '--root', str(self.root), '--gateway-config', str(self.gateway), '--app', 'reports', str(self.root)], capture_output=True, text=True)
        self.assertEqual(run.returncode, 1)
        self.assertNotIn('private sentinel', run.stdout + run.stderr)


if __name__ == '__main__':
    unittest.main()
