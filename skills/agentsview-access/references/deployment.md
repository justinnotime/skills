# Deployment and verification

1. Record the reviewed upstream release/full commit in external deployment config.
   Build with its locked dependencies. Keep source, binary, state and grants separate.
2. Prepare an explicit source manifest and read-only mount map. Enforce it with a
   dedicated account plus filesystem permissions, or an audited Linux systemd,
   container or bubblewrap sandbox. Test that ungranted synthetic sentinels cannot
   be indexed. Allocate only the index directory as writable persistent state.
3. Configure that version's remote URL and token mechanisms. Add an authenticated
   local proxy if the native token protects only APIs. The proxy must deny missing
   or incorrect credentials, unsupported methods and unreviewed API routes. Strip
   browser cookies and untrusted authority headers before forwarding. Ensure the raw viewer cannot bypass this proxy: use a private network
   namespace, permission-controlled Unix socket or another enforced boundary.
   A loopback TCP bind plus a separate OS UID does not prevent local TCP access.
   Test anonymous data denial at both the proxy and raw viewer layers. Keep a
   minimal route allowlist for the installed version; upgrades require review.
4. Choose a dedicated browser origin. A compatible gateway profile uses
   `isolated-readonly`, `content: application`, a loopback upstream and a private
   credential file. Titles, origins and source paths stay in external config.
   Browser isolation protects the login interface; the viewer can still read all
   of its own granted sessions, so upstream rendering remains trusted code.
5. Configure HTTPS ingress separately. Do not widen source grants, enable public
   sharing or remove authentication to make a failed probe pass.

| Synthetic check | Required result |
| --- | --- |
| Anonymous HTML, asset and API | Login redirect or denial; no content |
| Direct backend without credential | Denied |
| Cookie/client Authorization forwarding | Neither reaches upstream |
| Granted fixture | Readable after login |
| Ungranted source sentinel | Absent |
| Mutating/config/filesystem/unknown API | Denied |
| Cross-origin login/control request | Denied |
| Logout or revocation | Subsequent reads denied; active streams cancelled |
| External upstream redirect | Denied |

Run this matrix against the pinned build using synthetic content. Live health
checks should request deliberately safe status/denial endpoints and inspect codes
programmatically. Do not print response bodies, source filenames or tokens. A
version upgrade is a security boundary change and needs the same matrix again.
