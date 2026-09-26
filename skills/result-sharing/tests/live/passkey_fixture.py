"""Run an explicitly selected gateway with disposable state and a local TLS terminator."""
from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import threading
import time


@contextmanager
def passkey_fixture(directory, upstream, authorization):
    binary = Path(os.environ['PRIVATE_WEB_BINARY'])
    assert binary.is_file(), 'Set PRIVATE_WEB_BINARY to the reviewed private-web executable'
    state = directory / 'passkey-state'
    state.mkdir(mode=0o700)
    credential = directory / 'gateway-credential.json'
    credential.write_text(json.dumps({'authorization': authorization}))
    credential.chmod(0o600)
    cert, key = directory / 'cert.pem', directory / 'key.pem'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:P-256', '-nodes', '-keyout', str(key), '-out', str(cert), '-days', '1', '-subj', '/CN=gateway.example.test', '-addext', 'subjectAltName=DNS:gateway.example.test'], check=True, capture_output=True)

    def free_port():
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            return s.getsockname()[1]

    ports = [free_port(), free_port()]
    servers = []
    for port in ports:
        class Proxy(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'
            def log_message(self, *_):
                pass
            def forward(self):
                client = http.client.HTTPConnection('127.0.0.1', self.server.backend_port, timeout=10)
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                client.request(self.command, self.path, body=body, headers={k:v for k,v in self.headers.items() if k.lower() not in ['connection','transfer-encoding']})
                response = client.getresponse()
                data = response.read()
                self.send_response(response.status)
                for k,v in response.getheaders():
                    if k.lower() not in ['connection','transfer-encoding','content-length']:
                        self.send_header(k,v)
                self.send_header('Connection', 'close')
                self.close_connection = True
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(data)
                client.close()
            do_GET = forward
            do_HEAD = forward
            do_POST = forward
        server = ThreadingHTTPServer(('127.0.0.1', 0), Proxy)
        server.backend_port = port
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
    origins = ['https://gateway.example.test:' + str(s.server_port) for s in servers]
    config = directory / 'gateway.json'
    config.write_text(json.dumps({'schema':'private-web-access/v1','origin':origins[0],'listen':'127.0.0.1:'+str(ports[0]),'state_directory':str(state),'applications':[{'id':'files','title':'Files','mode':'isolated-readonly','content':'published','origin':origins[1],'listen':'127.0.0.1:'+str(ports[1]),'upstream':'http://'+upstream,'credential_file':str(credential),'methods':['GET','HEAD']}]}))
    config.chmod(0o600)
    invitation = directory / 'invite.json'
    for command in [['init'], ['enroll','--output',str(invitation)]]:
        subprocess.run([str(binary),*command,'--config',str(config)],check=True,capture_output=True)
    log = (directory / 'gateway.log').open('w')
    child = subprocess.Popen([str(binary),'serve','--config',str(config)],stdout=log,stderr=log)
    try:
        for _ in range(100):
            try:
                with socket.create_connection(('127.0.0.1',ports[0]),timeout=.1):
                    break
            except OSError:
                assert child.poll() is None, 'Synthetic gateway failed to start'
                time.sleep(.05)
        yield {'origin':origins[1],'loginOrigin':origins[0],'enrollment':json.loads(invitation.read_text())['url']}
    finally:
        child.terminate()
        child.wait(timeout=10)
        log.close()
        for server in servers:
            server.shutdown()
            server.server_close()
