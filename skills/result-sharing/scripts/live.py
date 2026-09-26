#!/usr/bin/env python3
"""Live URL construction and explicit first-time backend credential setup."""
import argparse
import base64
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sys
from urllib.parse import quote, urlsplit


def live_url(root, target, gateway, app, source):
    root = Path(root).expanduser().resolve(strict=True)
    target = Path(os.path.abspath(Path(target).expanduser()))
    relative = target.relative_to(root)
    if not root.is_dir() or not target.exists():
        raise ValueError('source root or target is missing')
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError('the live viewer excludes symlinks')
    config = json.loads(Path(gateway).expanduser().read_text())
    matches = [a for a in config['applications'] if a['id'] == app]
    if len(matches) != 1:
        raise ValueError('select exactly one configured application')
    origin = matches[0]['origin']
    parsed = urlsplit(origin)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in ('', '/')):
        raise ValueError('application origin must be credential-free HTTPS')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', source):
        raise ValueError('invalid source name')
    path = '' if relative == Path('.') else quote(relative.as_posix(), safe='/')
    return origin.rstrip('/') + '/files/' + source + '/' + path


def init_auth(directory, root):
    """Create paired native inputs, never rotate or overwrite existing credentials."""
    root = Path(root).expanduser().resolve(strict=True)
    directory = Path(directory).expanduser()
    resolved = directory.resolve()
    if resolved == root or root in resolved.parents:
        raise ValueError('credential directory must be outside the browsable root')
    if directory.is_symlink():
        raise ValueError('credential directory must not be a symlink')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = directory.stat()
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        raise ValueError('credential directory must be owner-only (0700)')
    names = ('results.json', 'results.htpasswd', 'quantum-auth.yaml', 'quantum-upstream.conf')
    # Lock the directory itself; no extra state file or shared configuration loader.
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    created = []
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        present = [os.path.lexists(directory / name) for name in names]
        if any(present):
            if not all(present):
                raise ValueError('partial credential set; inspect it without overwriting')
            for name in names:
                path = directory / name
                st = path.lstat()
                if path.is_symlink() or not path.is_file() or st.st_uid != os.getuid() or st.st_mode & 0o077:
                    raise ValueError('existing credential files must be regular and owner-only')
            return 'EXISTS: all four files preserved; credential contents were not revalidated'
        password, secret, admin = (secrets.token_urlsafe(48) for _ in range(3))
        encode = lambda b: base64.urlsafe_b64encode(b).rstrip(b'=')
        payload = encode(b'{"alg":"HS256","typ":"JWT"}') + b'.' + encode(b'{"sub":"reader"}')
        token = (payload + b'.' + encode(hmac.new(secret.encode(), payload, hashlib.sha256).digest())).decode()
        basic = base64.b64encode(('gateway:' + password).encode()).decode()
        # {SHA} is the Nginx htpasswd format; input is a random 384-bit service secret.
        hashed = base64.b64encode(hashlib.sha1(password.encode()).digest()).decode()
        contents = (
            json.dumps({'authorization': 'Basic ' + basic}) + '\n',
            'gateway:{SHA}' + hashed + '\n',
            'backend_jwt_secret: &backend_jwt_secret ' + json.dumps(secret) + '\n'
            + 'admin_password: &admin_password ' + json.dumps(admin) + '\n',
            'proxy_set_header X-Filebrowser-Assertion "' + token + '";\n',
        )
        for name, content in zip(names, contents):
            path = directory / name
            out = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created.append(path)
            with os.fdopen(out, 'w') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        return 'CREATED: four owner-only backend credential files; no Passkey state changed'
    except BaseException:
        for path in created:
            path.unlink()
        raise
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    url = commands.add_parser('url')
    url.add_argument('--root', required=True)
    url.add_argument('--gateway-config', required=True)
    url.add_argument('--app', required=True)
    url.add_argument('--source', default='ops')
    url.add_argument('path')
    init = commands.add_parser('init-auth')
    init.add_argument('--directory', required=True)
    init.add_argument('--root', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'url':
            print(live_url(args.root, args.path, args.gateway_config, args.app, args.source))
        else:
            print(init_auth(args.directory, args.root))
    except (OSError, ValueError, KeyError, TypeError):
        # Config values and credential material must not be reflected in errors.
        print('ERROR: invalid path, source, configuration or credential directory; existing files were preserved', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
