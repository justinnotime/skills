# Deployment procedure

Use the official [Serve CLI reference](https://tailscale.com/docs/reference/tailscale-cli/serve).
The node must already be enrolled in the caller's tailnet, with HTTPS certificates
and Serve enabled. Pin and review the deployed CLI version. Read-only discovery
uses `tailscale serve status --json`; capture and parse the result in a program,
never pass raw output to a model.

The external profile records an absolute CLI path, private receipt path and an
explicit list of HTTPS origin / numeric-loopback HTTP upstream bindings. For
example, `https://node.example.test` to `http://127.0.0.1:9100` is a synthetic pair,
not a request to register a public domain. Reject credentials in URLs, nonloopback
upstreams, unknown options, duplicate listeners and path-based login-origin mixing.

1. Acquire a private local deployment lock. Read the live Serve configuration and
   save a mode-0600 snapshot outside Git. Reject public Funnel settings and unknown
   configuration shapes. Confirm each selected HTTPS listener is empty or exactly
   matches a caller-owned receipt. Do not claim ownership of an unrelated binding.
2. Validate the backend and authentication gateway before exposing them. The
   backend must deny direct access without a gateway credential or equivalent OS
   boundary. The gateway must require login for data and use exact origin checks.
3. For each empty selected listener, run the CLI with separate argv elements:
   `serve --bg --yes --https=PORT http://127.0.0.1:BACKEND_PORT`.
   Use profile values, not shell interpolation. Capture stdout/stderr privately.
   Do not log a generated URL. If approval is needed, stop before claiming success.
4. Read back configuration after every change and compare it structurally against
   the snapshot plus exactly the intended delta. Check TLS and anonymous denial
   using an endpoint chosen by the application contract, without reading content.
5. Write an ownership receipt only after successful verification. On failure,
   disable only listeners created by this invocation, using
   `serve --https=PORT off`, and only when their current values still match the
   expected owned state. Verify the original snapshot is restored. If drift
   prevents safe rollback, report incomplete cleanup rather than resetting all.
6. Removal follows the same ownership comparison and only removes selected owned
   listeners. Reconfiguration must serialize cooperating deployments; the CLI
   does not provide a multi-command transaction against external administrators.

Private Serve does not require a public IP or a DNS-provider API token. Use the
HTTPS name provisioned by the selected tailnet setup. Additional ports are
separate browser origins, but cookies have no port scope: all TLS ports sharing
that hostname must terminate at trusted credential-stripping gateway listeners.

Trust: Tailscale handles node enrollment, routing and TLS termination here.
Passkeys do not protect against a compromised TLS terminator or host administrator.
If that actor is outside the trust boundary, use an independently controlled TLS
terminator and certificate trust path. Do not claim Serve plus Passkeys eliminates
control-plane trust or provides application end-to-end encryption.
