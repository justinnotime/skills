# Configuration and operation

Requires Go 1.27.1 or a compatible maintained release. Build from this package:

```sh
go build -trimpath -o /external/bin/private-web ./cmd/private-web
private-web check --config /external/config/gateway.json
private-web init --config /external/config/gateway.json
private-web enroll --config /external/config/gateway.json --output /external/private/invitation.json
private-web serve --config /external/config/gateway.json
```

The state directory must already exist with mode 0700. Configuration and credential
files must be regular owner-only files, outside Git and application content roots.
Use a dedicated unprivileged OS user when other local users/processes are untrusted.
`init` refuses existing owner state; `serve` never creates a replacement identity.
The enrollment command writes a URL to the selected file and prints only status.
Keep shell history, access logs, screenshots and agent transcripts free of that URL.

A synthetic example (not a deployment binding):

```json
{
  "schema": "private-web-access/v1",
  "origin": "https://gateway.example.test",
  "listen": "127.0.0.1:9000",
  "state_directory": "/external/private/state",
  "applications": [
    {
      "id": "console", "title": "Control console", "description": "Trusted application",
      "mode": "trusted-prefix", "content": "application",
      "upstream": "http://127.0.0.1:9100",
      "credential_file": "/external/private/console.json",
      "methods": ["GET", "HEAD", "POST"], "forward_headers": ["X-Application-Action"]
    },
    {
      "id": "library", "title": "Published results", "description": "Selected deliverables",
      "mode": "isolated-readonly", "content": "published",
      "origin": "https://gateway.example.test:8443", "listen": "127.0.0.1:9001",
      "upstream": "http://127.0.0.1:9101",
      "credential_file": "/external/private/library.json", "methods": ["GET", "HEAD"]
    }
  ]
}
```

The credential file contains exactly an `authorization` string, either a validated
Basic value or an independently generated Bearer value with at least 32 characters.
Generate it in a program or secret provider; never place a real value on argv or
in a published example. The backend independently validates the same credential.
Only explicitly listed `X-` application headers can be forwarded; credential and
proxy-identity headers remain reserved. One credential should grant only one backend.

The primary origin serves `/`, `/login`, `/auth/*`, and `/apps/<id>/` launchers.
A trusted app is mounted at `/<id>/`; its upstream receives the prefix stripped.
That application must support the prefix in its own asset/API URLs. It shares the
primary origin's authority and must not execute untrusted content. Every write
requires a login verified within five minutes. Requests are bounded to 1 MiB and
noncanonical write paths are refused; WebSocket/Upgrade is not supported.

An isolated app uses the complete root of its configured HTTPS origin. Only GET
and HEAD are accepted; no auth API is served there. Exact-origin and Fetch Metadata
checks plus browser policy separate it from the primary and other app origins.
The current shared-session deployment accepts the same hostname on distinct ports;
independent hostnames/SSO require a separate supported design, not ad hoc cookie sharing.

`content: application` permits scripts only from the app origin. `published`
explicitly permits inline artifact scripts, while blocking external connections,
frames, workers and forms. Package external dependencies locally. Published files
on the same artifact origin share a trust domain; this is not per-artifact isolation.

Run under a service manager with a private umask, no core dumps, no new privileges,
read-only program/config paths and write access only to authentication state.
Grant only required address families. Runtime logging is fixed codes; do not add
reverse-proxy access logs containing queries, cookies or enrollment fragments.
Provision actual units and network routing in the caller-owned deployment configuration.

For backups preserve encrypted, access-controlled owner state. Authenticator private
keys remain on authenticators; the server stores public credentials and an opaque
user handle. Sessions live only in memory for at most one hour; restart logs browsers
out. Rotate backend credentials independently and restart to load replacements.
