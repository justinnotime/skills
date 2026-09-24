// Package browserauth provides single-owner Passkey authentication. It never
// receives authentication private keys or application secrets.
package browserauth

import (
	"errors"
	"net"
	"net/url"
	"strings"

	"github.com/go-webauthn/webauthn/protocol"
	"github.com/go-webauthn/webauthn/webauthn"
)

var ErrAuth = errors.New("AUTH_REJECTED")

const Mode = "passkey"

type Config struct {
	Mode   string `json:"mode,omitempty"`
	Origin string `json:"origin,omitempty"`
}

func (c Config) Validate() error {
	if c == (Config{}) {
		return nil
	}
	u, e := url.Parse(c.Origin)
	if e != nil || c.Mode != Mode || u.Scheme != "https" || u.User != nil || u.Host == "" || u.Path != "" || u.RawQuery != "" || u.ForceQuery || u.Fragment != "" || u.Opaque != "" || net.ParseIP(u.Hostname()) != nil || u.Hostname() == "localhost" || strings.ToLower(c.Origin) != c.Origin || strings.HasSuffix(u.Hostname(), ".") {
		return ErrAuth
	}
	if protocol.ValidateRPID(u.Hostname()) != nil {
		return ErrAuth
	}
	if _, e = newWebAuthn(c); e != nil {
		return ErrAuth
	}
	return nil
}
func newWebAuthn(c Config) (*webauthn.WebAuthn, error) {
	u, e := url.Parse(c.Origin)
	if e != nil {
		return nil, ErrAuth
	}
	return webauthn.New(&webauthn.Config{RPID: u.Hostname(), RPDisplayName: "Private Web", RPOrigins: []string{c.Origin}, AttestationPreference: protocol.PreferNoAttestation, AuthenticatorSelection: protocol.AuthenticatorSelection{ResidentKey: protocol.ResidentKeyRequirementRequired, UserVerification: protocol.VerificationRequired}, Timeouts: webauthn.TimeoutsConfig{Login: webauthn.TimeoutConfig{Enforce: true}, Registration: webauthn.TimeoutConfig{Enforce: true}}})
}
