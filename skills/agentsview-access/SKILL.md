---
name: agentsview-access
description: Deploy and connect a scoped read-only AgentsView conversation viewer behind an independent authentication gateway. Use for browser access to explicitly granted session sources; not session extraction, source authorization or public archive publication.
---

# Scoped conversation viewer

Treat AgentsView as an independent application with its own pinned upstream,
state, source grants and service identity. This Skill provides deployment and
verification instructions, not a viewer fork. Read the upstream
[security model](https://github.com/kenn-io/agentsview/blob/main/SECURITY.md) and
[remote-access contract](https://github.com/kenn-io/agentsview/blob/main/docs/remote-access.md)
for the selected version, then follow [deployment and testing](references/deployment.md).

Obtain exact source grants from caller-owned external configuration. A harness
home, backup profile, extracted archive or reachable node does not grant access.
Do not discover or mount all home directories. Without a grant, build and test
with synthetic sessions while asking for the intended scope before connection.
Parse roots, endpoints, credentials and IDs in programs; return allowlisted
statuses, not raw CLI output, filenames or conversation contents.

Use a dedicated OS identity or an audited sandbox with only granted sources
mounted read-only. Give the viewer a separate writable index. Read-only mounts
alone do not restrict other host paths outside the sandbox. Disable unused actions
and integrations and restrict network egress. Pin a reviewed upstream release or
commit and its locked dependencies; never pipe an unpinned installer into a shell.

Protect the whole application, including HTML, assets and API, with an independent
gateway. `private-web-access` is an optional Passkey gateway; use its executable
and external profile, not sibling source imports. Use an isolated read-only browser
origin for session content. Tailscale Serve is an optional independent ingress.
Native viewer API tokens may not protect static assets. Do not place rendered
session content on the login origin or the result-library origin.

Require a gateway credential at the loopback backend, or an equivalent OS boundary.
Preserve explicit route and GET/HEAD allowlists. Do not expose arbitrary root
selection, configuration, filesystem or mutating endpoints. Review new routes on
upgrades instead of granting all `/api/*`. Test with synthetic content before
connecting real grants; successful loading of a shell alone is insufficient.

Connecting a viewer does not publish sessions or authorize an agent to read them.
Keep deployment manifests, operational history and review findings in the caller's
private operations repository.
