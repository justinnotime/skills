---
name: private-web-access
description: Deploy a single-owner Passkey gateway and application portal in front of explicitly configured HTTP services. Use for private browser access, isolated result viewers, enrollment, revocation and gateway verification; not arbitrary TCP authentication or multi-user SSO.
---

# Private web access

Keep authentication, application backends and network ingress independently owned.
This package contains its Go runtime, browser assets, tests and build dependencies;
it works without any sibling Skill or application repository.

Start with [configuration and deployment](references/configuration.md). Obtain
application grants, upstreams, HTTPS origins and private state paths from the
caller's external configuration. Parse private values in programs and output only
bounded status summaries. Do not infer source grants from profile names, backup
membership or a reachable service.

Use a stable, validated HTTPS origin. The included runtime listens on numeric
loopback addresses behind an explicitly configured TLS terminator that preserves
Host. Tailscale Serve is one optional terminator, configured independently through
its public CLI or the `tailscale-serve` Skill. Network membership does not replace
Passkey login; forwarded identity headers do not confer authority.

Build `cmd/private-web`, validate the external configuration, initialize a new
private state directory explicitly, and issue enrollment to a private file.
An invitation is one-use and valid for ten minutes. Show it only in the operator's
local terminal when requested, never in model output. Existing owners add a
credential with recent verified login; OS-owner enrollment is an explicit recovery
operation. Do not silently initialize missing or corrupt authentication state.

Use `trusted-prefix` only for application code trusted as part of the login origin.
Use `isolated-readonly` for conversation and published-content viewers. Arbitrary
HTML belongs in a separate browser origin; do not mount it under the login origin.
Read [security boundaries](references/security.md) before selecting content mode,
sharing a hostname across ports, or granting write methods. All TLS ports on a
shared hostname must terminate at the trusted gateway; cookies have no port scope.

Existing backends must require their own gateway credential or an equivalent OS
boundary. The gateway strips browser cookies and authority headers, then inserts
an application-specific credential from a private file. Preserve backend method,
source and path restrictions. Read-only GET is not proof of an endpoint being safe.

Before release or deployment, run the [standalone checks](references/testing.md),
including race tests, synthetic browser verification and dependency review. Test unauthenticated access,
origin confusion, credential/header isolation, enrollment replay, recent-login
requirements, session revocation and backend bypass. For migration, compare
registration handles, origin and public credential records programmatically;
never rebind authenticators just to move server files.

The default is one owner: every enrolled credential accesses every configured app.
Separate owners or trust domains need separate deployments and OS/data authority.
Do not claim multi-tenant authorization, arbitrary-content containment or an
independent security certification. Follow the [release checks](references/release.md)
for a selected publication set; keep private provenance and incident records private.
