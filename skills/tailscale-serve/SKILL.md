---
name: tailscale-serve
description: Configure and verify private HTTPS ingress with Tailscale Serve using caller-owned bindings and ownership receipts. Use for tailnet-only HTTP exposure; browser authentication and application deployment are separate capabilities.
---

# Private HTTPS ingress

Read [the deployment procedure](references/deployment.md) before changing a
listener. Use the separately installed official Tailscale CLI. This Skill is an
independent operational procedure, not a wrapper around an application repository.

Obtain origins, loopback upstreams, CLI location, state path and listener ownership
from the caller's external configuration. Parse those values programmatically;
capture CLI output privately and emit only allowlisted status codes. Never print
raw Serve/status JSON, addresses, certificate names or node identifiers. Do not
infer the intended node or a source grant from a reachable service.

Default to private Serve. Do not enable Funnel, reset all Serve settings, replace
another application's binding or edit tailnet policy as a side effect. If an
administrator approval is required, report the specific blocked operation and
provide the official console's generic entry point. A request to open a browser
is not proof that the operator saw the approval page.

Serve provides HTTPS termination and private transport, not application login.
Use an independently configured authentication service; `private-web-access` is
one optional Passkey gateway, addressed through its executable and external
profile. Never import another Skill's source or depend on an application-specific
setup script. Forwarded user/host headers are not an authentication authority.

Test only anonymous login/denial endpoints. Never fetch real frames, sessions or
published documents to test reachability. An HTTP 200 shell alone is not proof
of authorization. Verify direct backend denial through the application's own
contract. Preserve unrelated bindings and configuration on failure or rollback.

Before claiming success, verify the actual Serve state, private-only ingress,
expected TLS origin and anonymous data denial. Save detailed evidence only to the
caller's private runtime directory. A safe status summary belongs in the report.
