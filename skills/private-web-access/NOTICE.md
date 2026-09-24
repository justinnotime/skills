# Dependencies

This package uses Go's standard library and the modules pinned in `go.mod` and
`go.sum`. WebAuthn verification is provided by `github.com/go-webauthn/webauthn`;
its cryptographic and serialization dependencies retain their own licenses.
Browser verification uses Playwright under Apache-2.0, pinned by the npm lockfile.
No dependency source or browser binary is vendored. Distributing compiled bundles
must include the applicable notices and license texts from the pinned dependency
modules and any separately distributed browser. Review dependency changes before
upgrading; the package license does not replace upstream licenses.
