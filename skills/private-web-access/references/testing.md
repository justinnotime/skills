# Standalone verification

Copy this package without siblings. With Go 1.27.1 and Node 24:

```sh
go test -race ./...
go vet ./...
go build -trimpath -o /external/bin/private-web ./cmd/private-web
npm ci --ignore-scripts --no-audit --no-fund
npx playwright install chromium
npm run test:browser
go run golang.org/x/vuln/cmd/govulncheck@v1.8.0 ./...
```

The browser fixture creates disposable TLS servers, synthetic content and a virtual
WebAuthn authenticator. Its certificate trust override is confined to the test
context. Never point it at an existing deployment. It verifies enrollment, login,
trusted application routing, read-only origin isolation and global logout.

Go regressions cover signed ES256 registration/assertions, exact origins, required
user verification/presence, replay and invitation expiry, concurrent state ownership,
slow-body isolation, capacity shedding without challenge eviction, session/stream
revocation, credential stripping, redirects, informational headers and trailers.
No test reads operator configuration, physical authenticators or actual content.

Run dependency checks for each release. Module-level findings can concern packages
that the application does not import; inspect the exact import/call graph and
record the disposition instead of claiming every dependency has no advisories.
These checks do not establish denial-of-service resistance, production correctness
or an independent security certification. External terminators and application
backends require their own version and boundary review.
