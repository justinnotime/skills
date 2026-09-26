# Live viewer reference

This is an optional Linux/Docker Compose deployment recipe for a caller-approved
source. It uses FileBrowser Quantum v1.5.6-stable, source revision
`5d9b4df2a21d1ba4a6af481181a7402cb5cbb5ca`, from the
[Apache-2.0 upstream](https://github.com/gtsteffaniak/filebrowser/tree/v1.5.6-stable).
The [native Compose recipe](../assets/live/compose.yaml) pins both images by digest.
It contains no host service installation or account policy.

## Native inputs

Select these non-secret values in the caller's existing environment, service
unit or a Compose `--env-file`. All paths are absolute after native expansion:

| Variable | Meaning |
| --- | --- |
| `RESULT_SHARING_PACKAGE` | Installed path of this standalone Skill |
| `RESULT_SHARING_ROOT` | Approved browsable source, commonly `~/ops` |
| `RESULT_SHARING_STATE` | Private Quantum database/socket directory, outside the source |
| `RESULT_SHARING_AUTH` | Private backend credential directory, outside the source |
| `RESULT_SHARING_UID`, `RESULT_SHARING_GID` | Owner's numeric UID/GID |
| `RESULT_SHARING_PORT` | Nginx loopback port; default 8081 |

The source alias `ops` is independent of the host path. The browser route is
`/files/ops/`. Change the mounted root to customize storage without changing links.
Quantum [files.yaml](../assets/live/files.yaml) and Nginx
[files-server.conf](../assets/live/files-server.conf) are the native authorities
if another alias or route policy is needed. A caller-owned Compose file may
extend the `files`/`proxy` services or replace an individual bind mount. No
private choices are compiled into this package.

For a new authorized installation, create the approved root and owner-only state
and credential directories. Set the variables above before running:

```sh
mkdir -p "$RESULT_SHARING_ROOT"
install -d -m 700 "$RESULT_SHARING_STATE" "$RESULT_SHARING_STATE/run" "$RESULT_SHARING_AUTH"
python3 "$RESULT_SHARING_PACKAGE/scripts/live.py" init-auth \
  --directory "$RESULT_SHARING_AUTH" --root "$RESULT_SHARING_ROOT"
docker compose -p result-sharing -f "$RESULT_SHARING_PACKAGE/assets/live/compose.yaml" config --quiet
docker compose -p result-sharing -f "$RESULT_SHARING_PACKAGE/assets/live/compose.yaml" up -d
```

`init-auth` generates four native, mode-0600 inputs without printing secrets:

- `results.json`: gateway-to-Nginx Basic service authorization.
- `results.htpasswd`: its Nginx password verifier (a random 384-bit service secret).
- `quantum-auth.yaml`: separate reader-signing secret and disabled-login admin password.
- `quantum-upstream.conf`: signed reader assertion inserted by Nginx.

It preserves a complete existing set, refuses a partial set and does not initialize
Passkeys. A preserved set is not a claim that its contents are valid; service
verification checks that. Quantum's native loader combines `auth-files.yaml`
with `files.yaml`, as defined by its [versioned loader](https://github.com/gtsteffaniak/filebrowser/blob/v1.5.6-stable/backend/common/settings/yaml.go).
The internal assertion is a persistent backend credential, not a short-lived
browser token. Rotate each pair together in a separate authorized operation.
Do not move credentials into the browsable root or display rendered secret includes.

## Independent authentication and HTTPS

Use an existing independent `private-web-access` installation through its public
executable interface and native JSON. Select an application with:

- `mode: isolated-readonly`, `content: published`, `methods: [GET, HEAD]`;
- its own HTTPS origin, separate from the login and conversation-viewer origins;
- a numeric loopback listener and `upstream: http://127.0.0.1:<proxy-port>`;
- `credential_file` pointing to the generated `results.json`.

`published` selects the gateway's content policy; it does not imply copying or
publishing local files. It allows Quantum's UI while preserving same-origin
network restrictions. The browser's own policy is retained by the gateway.
Preserve the existing gateway owner, origin and credential records during migration.
For a new gateway, follow its own initialization/enrollment procedure; this
package neither initializes it nor imports its source.

Configure the authorized HTTPS terminator to forward to the Passkey listener,
never directly to Nginx or Quantum. For Tailscale, use the official Serve CLI
and the existing binding ownership procedure. Keep Funnel disabled for a
private tailnet deployment. Tailscale membership and Passkey authentication
are separate controls. Host-root and same-OS-user processes remain outside
this browser boundary; do not promise an absolute security guarantee.

## Runtime behavior

Quantum has `network_mode: none`, no published TCP port, a Unix socket,
read-only source/root mounts and no write/share/token permissions. Nginx on
loopback validates the gateway credential, inserts the separate reader assertion,
strips browser authority/cookies, disables caching and permits reviewed GET/HEAD
routes. The native socket also requires a signed assertion.

Symlinks are excluded, including links to documentation outside the source.
Do not weaken this rule to display a convenience symlink. Ordinary files are
read live; refresh the directory or file to see edits. Tests create a file after
startup to check this behavior.

Update checks, default external links and optional authentication integrations
are disabled. Quantum's container cannot route to external networks. The
independent gateway's content policy blocks remote browser assets/connections;
extra editor language support may therefore be unavailable. Markdown is sanitized.
HTML/PDF frame previews are restricted; download them for local viewing. This
recipe does not certify every dependency or guarantee future release behavior.

## Verification

Standard-library checks need Python 3.10+:

```sh
python3 -B -m unittest discover -s tests -v
```

The live integration requires Linux, Docker Compose, Node, OpenSSL, Chromium and
an explicitly selected reviewed `private-web` executable. From this package:

```sh
npm ci --ignore-scripts --no-audit --no-fund
npx playwright install chromium
PRIVATE_WEB_BINARY=/approved/bin/private-web npm run test:live
```

The test uses disposable containers, synthetic sources, private credentials,
local TLS and virtual WebAuthn. It checks actual Passkey login/logout, anonymous
rejection, new/changed files, Markdown/mobile, mutations, path confinement,
credential reflection and unavailable backend egress. It imports no sibling Skill.
To check a caller's native Compose selection using the same synthetic fixture:

```sh
PRIVATE_WEB_BINARY=/approved/bin/private-web python3 tests/live/check.py \
  --compose /private/deployment/compose.yaml --proxy-service gateway
```

This substitutes synthetic source/state/credential mounts and uses the selected
images and native viewer/route configuration. It never starts the caller's other
applications or reads their secret contents. Evidence goes to `.task-checks/` in
this package's test worktree. Do not run artifact-producing tests in a primary
checkout.

After deployment, separately check actual loopback listeners, readonly mounts,
unauthenticated HTTPS redirect/rejection, backend 401, source scope and absence
of Funnel. Synthetic success is not proof that a different running service has
been activated. Compare existing Passkey registration identifiers/public records
before and after without printing them.

## Update and rollback

Keep the previous package revision and caller-owned native configuration. Run
integration checks for an image or route-policy change, then recreate only the
selected viewer/proxy services. Recreate bind-mounted files after replacing
configuration inodes; a restart alone may retain the old file. Preserve source
files and credentials. A release that changes the database format needs a
stopped-writer state backup and its own migration check before activation.

Rollback restores the previous image/configuration and recreates those services;
restore state only from a compatible stopped-writer backup when needed. Never
replace Passkey owner state to roll back a viewer. Existing legacy publisher
content can remain untouched until a separately authorized cleanup.
