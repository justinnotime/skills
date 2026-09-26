# Configuration and responsibilities

Use one task root per machine and OS user, shared by all agents. `~/ops/` is a
default, not a requirement that every machine have identical paths. A caller
may keep non-secret native configuration in a private repository, or keep it
outside Git. Neither arrangement needs a machine-ID registry.

## Agent profile

The optional `profile.md` records the selected task root, delivery subdirectory,
audience/source grant and how to locate the viewer's native settings. It may
link to existing machine instructions instead of copying them. For example:

```markdown
Task root: ~/ops
Results: result-sharing/<project>/; working evidence: tasks/<task>/.
Audience: this owner may browse the whole task root.
Viewer: Quantum source ops, mounted read-only from the task root.
Gateway: ~/.config/private-web-access/settings.json, application files.
Deployment: /private/deployment/compose.yaml and its native environment file.
```

Agents read the profile as prose. Programs do not parse it. The URL helper takes
explicit arguments and reads the existing gateway JSON; it does not load a
second result-sharing JSON. A machine with no browser needs only the task-root
instruction. Install the Skill and profile into the caller's selected harness
roots with their normal installer; already running sessions must reread changed
instructions and may need a new session to rediscover Skills.

## Where configuration becomes behavior

| Input | Consumer and effect |
| --- | --- |
| Skill plus private Markdown profile | Agent chooses the output path and returns a link |
| Installer's package/profile selections | Installer creates discovery/configuration links only |
| Caller-owned service unit or supervisor | Starts the explicitly selected native commands |
| Compose file plus native environment | Docker resolves paths, UID/GID and mounts; starts the viewer/proxy |
| Quantum YAML plus private `auth-files.yaml` | Quantum loads sources, read permissions and signed backend authentication |
| Nginx native files plus private assertion | Nginx checks the gateway credential and permits reviewed read routes |
| Passkey gateway native JSON | Gateway selects origins, upstreams and credentials; authenticates the owner |
| HTTPS binding configuration | Agent/operator applies the terminator's official interface, such as `tailscale serve` |

No additional lowering or configuration compilation happens between these
layers. Compose's `extends` and bind mounts can reuse this package's recipe;
custom paths belong in native environment/overrides. The source name in Quantum
determines the URL prefix; changing it also requires updating legacy redirects
and the agent profile. Changing only the host root preserves URLs.

## Boundaries

File writing, source exposure and recipient distribution are separate grants.
Configure only authorized roots; never infer a grant from another machine,
backup membership or a directory's existence. All Passkeys in a single-owner
gateway access all its configured applications. Multiple recipients with
different permissions require a different authorization design.

The live viewer opens current bytes and has no immutable result history.
Existing repository or file-backup policy provides recovery. Optional
[legacy publication](legacy-publication.md) provides versioned copies when
explicitly selected; the two modes never require each other.
