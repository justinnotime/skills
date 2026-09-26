# Security boundary and review model

The gateway, its OS account, configuration, TLS terminator and trusted-prefix
applications are the trusted computing base. Network attackers, unauthenticated
Tailnet members and script running on isolated app origins are outside that base.
Compromise of the host or any same-privilege process is outside this boundary;
use separate OS identities and restrictive filesystem/socket access where needed.

WebAuthn requires the exact configured origin, resident credentials and user
verification. Challenges expire, enrollment invitations are single-use, successful
assertions update the stored credential, and duplicate session cookies are refused.
No forwarded identity/host header substitutes for WebAuthn or exact Host validation.
A TLS origin change requires an explicit authenticator migration decision.

Cookies are Secure, HttpOnly, host-only, SameSite=Strict and prefixed `__Host-`.
They are shared across ports: a port is an origin boundary, not a cookie boundary.
All HTTPS endpoints on that hostname must be controlled by the same gateway.
Never expose a raw untrusted backend on another HTTPS port of that hostname.
The gateway removes browser Cookie, Authorization, Origin, Referer and forwarding
headers before upstream requests. It also removes upstream Set-Cookie, CORS,
Refresh and Link headers, constrains redirects and disables document.domain.

Unsafe primary-origin requests require exact Origin and recent verification.
Sibling-origin requests cannot call primary APIs even though they are same-site.
Applications have separate CSP, CORP and COOP policies; auth APIs exist only on the
primary origin. CSP is defense in depth, not a replacement for safe rendering.
The gateway retains upstream Content-Security-Policy headers as additional
restrictions. Browsers enforce them together with the mandatory gateway policy;
an application's nonce or download sandbox cannot loosen the gateway policy.
Generated HTML is executable content: isolate it and publish only deliberately
selected artifacts. Same-origin artifacts can read each other's content. Browser
navigation/downloads and a user's ability to save content are not exfiltration prevention.

The caller must retain backend authentication and a route/method policy appropriate
to that backend. A GET endpoint can still execute commands, export credentials or
read arbitrary files; this gateway cannot infer its semantics. Do not grant access
to native session roots merely because they exist or are backed up.

Private endpoint names and passwords are not the only publication risks. Public
history, examples, comments, CI artifacts and release claims can reveal deployment
relations or unremediated defects. Review the final candidate before its first push;
keep deployment topology and incident provenance in caller-owned private records.
Do not publish a known exploitable defect as a documented limitation.

Primary references:
- [WebAuthn](https://www.w3.org/TR/webauthn-3/)
- [Cookie isolation limitations](https://www.rfc-editor.org/rfc/rfc6265#section-8.5)
- [go-webauthn security advisories](https://github.com/go-webauthn/webauthn/security/advisories)
- [Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve)

The authentication state has one exclusive process owner (local filesystem, Linux/macOS flock). A second instance using the same state fails closed. Do not place state on a filesystem without reliable advisory locking. Pending ceremonies and request-body readers are bounded. Excess new work is rejected without evicting another client's active challenge; authorized enrollment has reserved capacity. Network availability still requires ingress-level resource controls.
