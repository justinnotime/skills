# Configuration and deployment

Private configuration is separate from this distributable package. A local hub:

```json
{
  "schema": "result-sharing/v1",
  "allowed_source_roots": ["/approved/project"],
  "root": "/private/runtime/results",
  "base_url": "http://127.0.0.1:8081",
  "transport": {"kind": "local"}
}
```

`root` must be a dedicated durable directory, outside source worktrees and
scratch space. `base_url` is the viewer-facing artifact URL; it can be reached
through an SSH tunnel. It must not contain credentials. Scope source roots to
approved projects. An explicit file selection is still required under those
roots. Source roots grant publication only; they do not grant a session reader
access to anything else. `max_files` (default 2000) and `max_bytes` (default
100 MiB) can lower limits. Raise the implementation ceilings only after review.

A publishing node uses the same schema with `allowed_source_roots` and:

```json
{
  "transport": {
    "kind": "ssh",
    "target": "result-hub",
    "receiver_command": [
      "/usr/bin/python3", "/opt/result-sharing/scripts/share.py",
      "--config", "/private/receiver.json", "receive"
    ]
  }
}
```

Merge these fields into a complete configuration. The receiver uses the local
hub configuration; it does not need `allowed_source_roots` because it receives
explicit bytes rather than reading sender paths. Set `allowed_projects` on a
receiver to restrict an individual publisher's project slugs. Use an existing
authorized SSH credential/host configuration. Host verification is never disabled.
The command uses stdin JSON with base64 file bytes, validates paths and checksums,
and writes under a lock; no remote shell interpolation of result metadata.
A remote receipt proves durable publication, not browser reachability from the
user's device. Validate that access separately when setting up a node.

For a dedicated publish-only SSH credential, the operator may configure a forced
receiver command with forwarding/PTY disabled. This package does not create keys,
modify SSH policy or grant new node access. An ordinary SSH credential retains
its existing account privileges. Nodes under different privacy boundaries need
separate profiles, roots and access policy; sharing this public Skill grants no
cross-boundary access.

## Hub layout and serving

```text
root/
  index.html                 searchable latest-project catalog
  catalog.json               structured catalog
  projects/<slug>/
    index.html               revision history
    <content-sha256>/
      index.html             result details and file links
      manifest.json          hashes, sizes and publication time
      files/...              immutable selected content
```

`reindex` repairs generated navigation from published manifests. No automatic
pruning or remote deletion is provided. Retain source files and back up the
configured runtime root under the operator's existing policy. The publisher
rejects changed bytes at a previously published content address.

Use an existing read-only static server for `root`, with authentication and the
operator's chosen transport. Keep services on loopback when SSH forwarding is
the access model. Permit GET/HEAD; publishing goes through the CLI/SSH receiver,
not through the web server. Serve executable HTML/JS artifacts on a different
origin/port from conversation history. Do not mount the entire filesystem or
harness credentials. A private HTTP endpoint and a public Skill are independent
choices: this package does not make uploaded content public.

For a conversation companion such as AgentsView:

1. Install a pinned upstream release after checksum and source review. Define
   approved native session directories and any project filters in private config.
2. Use read-only source mounts. Keep its writable index/runtime state separate.
   Do not treat backup membership as conversation-read authorization.
3. Configure the browser gateway to permit reading only, including blocking
   mutation API methods. Hiding buttons alone is insufficient. Keep backend
   credentials server-side and use an authenticated frontend.
4. Disable unneeded external update/telemetry requests, verify language search,
   a real conversation, artifact interaction and mobile navigation. Verify blocked
   write requests against an isolated synthetic service before real deployment.
5. Keep service units, actual addresses, credentials, release pins and source
   mounts in the caller's owning configuration repository. Package upgrades do
   not implicitly change grants or overwrite service policy.

AgentsView indexes conversations; this publisher maintains durable deliverables
and their navigation. A conversation URL is optional because viewer URL formats
and retention policies belong to that separately versioned application.
