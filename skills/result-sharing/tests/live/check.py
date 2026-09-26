"""Exercise the native deployment with synthetic data, never the owner's files."""
import argparse
import base64
import http.client
import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time
from urllib.parse import urlencode
from passkey_fixture import passkey_fixture

PACKAGE = Path(__file__).resolve().parents[2]
CONFIG = PACKAGE / 'assets/live'
ENV = dict(os.environ, RESULT_SHARING_UID=str(os.getuid()), RESULT_SHARING_GID=str(os.getgid()))


def run(args, **kwargs):
    return subprocess.run(args, env=ENV, check=True, capture_output=True, text=True, **kwargs).stdout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--compose', type=Path, default=CONFIG / 'compose.yaml')
    parser.add_argument('--proxy-service', default='proxy')
    args = parser.parse_args()
    if not os.environ.get('PRIVATE_WEB_BINARY'):
        parser.error('PRIVATE_WEB_BINARY must explicitly select the gateway executable')
    scratch = PACKAGE / '.task-checks'
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='quantum-', dir=scratch) as temp:
        temp = Path(temp)
        source = temp / 'sources'
        (source / 'tasks/demo').mkdir(parents=True)
        (source / 'result-sharing/demo').mkdir(parents=True)
        state = temp / 'state'
        state.mkdir(mode=0o700)
        (state / 'run').mkdir(mode=0o700)
        (source / 'root-report.md').write_text('# Root report\n')
        (source / 'result-sharing/demo/result.md').write_text('# Result report\n')
        (source / 'tasks/demo/report.md').write_text('# Live report\n\n**Bold**\n\n| Item | Value |\n| --- | --- |\n| Status | Ready |\n\n<script>window.injected=true</script>\n<img src="https://example.invalid/tracker" onerror="window.injected=true">\n<a href="javascript:window.injected=true">bad</a>\n')
        (source / 'tasks/demo/code.py').write_text('print("local only")\n')
        (source / 'tasks/demo/outside').symlink_to('/etc/passwd')
        (source / 'tasks/demo/private-config').symlink_to('/cfg/auth-files.yaml')
        ENV.update(RESULT_SHARING_PACKAGE=str(PACKAGE), RESULT_SHARING_AUTH=str(temp), RESULT_SHARING_STATE=str(state), RESULT_SHARING_ROOT=str(source))
        run(['python3', str(PACKAGE / 'scripts/live.py'), 'init-auth', '--directory', str(temp), '--root', str(source)])
        auth = json.loads((temp / 'results.json').read_text())['authorization']
        password = base64.b64decode(auth.split()[1]).decode().split(':', 1)[1]
        secret = json.loads((temp / 'quantum-auth.yaml').read_text().splitlines()[0].split('&backend_jwt_secret ', 1)[1])
        token = (temp / 'quantum-upstream.conf').read_text().split('"')[1]
        spec = json.loads(run(['docker', 'compose', '-f', str(args.compose), 'config', '--format', 'json']))

        def mount(service, target):
            matches = [v for v in spec['services'][service]['volumes'] if v['target'] == target]
            assert len(matches) == 1 and matches[0]['read_only'], target
            return Path(matches[0]['source'])

        files_config = mount('files', '/cfg/files.yaml')
        style = mount('files', '/cfg/viewer.css')
        server_config = mount(args.proxy_service, '/etc/nginx/files-server.conf')
        proxy_config = mount(args.proxy_service, '/etc/nginx/files-proxy.conf')
        (temp / 'nginx.conf').write_text((CONFIG / 'nginx.conf').read_text())
        files = spec['services']['files']
        assert files['network_mode'] == 'none' and not files.get('ports')
        assert files['read_only']
        files['volumes'] = [f'{files_config}:/cfg/files.yaml:ro', f'{style}:/cfg/viewer.css:ro', f'{temp}/quantum-auth.yaml:/cfg/auth-files.yaml:ro', f'{state}:/state', f'{source}:/files:ro']
        for key in ['restart', 'logging', 'container_name']:
            files.pop(key, None)
        gateway = {
            'image': spec['services'][args.proxy_service]['image'],
            'user': files['user'], 'entrypoint': 'nginx', 'command': ['-g', 'daemon off;'],
            'read_only': True, 'cap_drop': ['ALL'], 'security_opt': ['no-new-privileges:true'],
            'tmpfs': ['/tmp:rw,noexec,nosuid,size=32m,mode=1777'],
            'volumes': [f'{temp}/nginx.conf:/etc/nginx/nginx.conf:ro', f'{temp}/results.htpasswd:/etc/nginx/results.htpasswd:ro', f'{temp}/quantum-upstream.conf:/etc/nginx/quantum-upstream.conf:ro', f'{proxy_config}:/etc/nginx/files-proxy.conf:ro', f'{server_config}:/etc/nginx/files-server.conf:ro', f'{state}/run:/filebrowser:ro'],
            'ports': ['127.0.0.1::8081'], 'depends_on': ['files'],
        }
        fixture_file = temp / 'compose.json'
        fixture_file.write_text(json.dumps({'name': 'live-' + secrets.token_hex(4), 'services': {'files': files, 'gateway': gateway}}))
        compose = ['docker', 'compose', '-f', str(fixture_file)]
        try:
            run(compose + ['up', '-d'])
            address = run(compose + ['port', 'gateway', '8081']).strip()

            def request(path, method='GET', authenticated=True):
                connection = http.client.HTTPConnection(address, timeout=10)
                connection.request(method, path, headers={'Authorization': auth} if authenticated else {})
                response = connection.getresponse()
                result = (response.status, response.read(), dict(response.getheaders()))
                connection.close()
                return result

            def resource(path, endpoint='resources'):
                return '/api/' + endpoint + '?' + urlencode({'source': 'ops', 'path': path, 'content': 'true'})

            for _ in range(100):
                try:
                    if request('/api/users?id=self')[0] == 200:
                        break
                except (OSError, http.client.HTTPException):
                    pass
                time.sleep(0.2)
            assert request('/api/users?id=self')[0] == 200
            assert request('/files/ops/', authenticated=False)[0] == 401
            user = json.loads(request('/api/users?id=self')[1])
            assert not user['permissions']['admin'] and not user['permissions']['modify']
            for path in ['/root-report.md', '/result-sharing/demo/result.md']:
                body = request(resource(path))[1]
                assert b'report' in body
            for method in ['PUT', 'POST', 'PATCH', 'DELETE', 'MOVE', 'MKCOL', 'PROPFIND']:
                assert request(resource('/tasks/demo/report.md'), method)[0] == 405, method
            for path in ['/api/auth/token', '/api/settings/config', '/api/share', '/public/api/resources', '/api/tools/duplicateFinder']:
                assert request(path)[0] == 403, path
            for path in ['/tasks/demo/outside', '/tasks/demo/private-config', '/../cfg/auth-files.yaml']:
                for endpoint in ['resources', 'resources/download']:
                    status, body, _ = request(resource(path, endpoint))
                    assert status in [400, 401, 403, 404, 500], (path, endpoint, status)
                    assert secret.encode() not in body and b'root:x:' not in body
            for path in ['/files/ops/', '/api/users?id=self', resource('/tasks/demo/report.md')]:
                status, body, headers = request(path)
                assert status == 200 and 'no-store' in headers['Cache-Control']
                assert not any(x.encode() in body for x in [secret, token, password, auth])
                assert 'Set-Cookie' not in headers
            # Native API still authenticates even if a local process reaches the socket.
            cid = run(compose + ['ps', '-q', 'files']).strip()
            native_status = run(['docker', 'exec', cid, 'curl', '--silent', '--output', '/dev/null', '--write-out', '%{http_code}', '--unix-socket', '/state/run/http.sock', 'http://localhost' + resource('/root-report.md')])
            assert native_status == '401'
            info = json.loads(run(['docker', 'inspect', cid]))[0]
            assert info['HostConfig']['NetworkMode'] == 'none'
            assert not any(n.get('IPAddress') for n in info['NetworkSettings']['Networks'].values())
            egress = subprocess.run(['docker', 'exec', cid, 'curl', '--connect-timeout', '2', '--max-time', '3', 'https://192.0.2.1'], capture_output=True)
            assert egress.returncode != 0
            with passkey_fixture(temp, address, auth) as gateway:
                fixture = temp / 'browser.json'
                fixture.write_text(json.dumps(dict(gateway, tasks=str(source / 'tasks'), artifacts=str(scratch))))
                fixture.chmod(0o600)
                try:
                    result = run(['node', str(Path(__file__).with_name('browser.mjs')), str(fixture)])
                except subprocess.CalledProcessError as error:
                    print(error.stderr)
                    raise
                print(result.strip())
            print('OK backend: authenticated socket, no network interface, read-only access, route restrictions, symlink confinement, no credential reflection')
        except Exception:
            # Synthetic fixture only; never print a production container log.
            logs = run(compose + ['logs', '--tail', '12'])
            for value in [secret, token, password, auth]:
                logs = logs.replace(value, '[REDACTED]')
            print(logs, flush=True)
            raise
        finally:
            run(compose + ['down', '--remove-orphans'])


if __name__ == '__main__':
    main()
