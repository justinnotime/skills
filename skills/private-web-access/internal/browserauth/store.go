package browserauth

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"os"
	"path/filepath"
	"time"

	"github.com/go-webauthn/webauthn/webauthn"
	"github.com/justinnotime/skills/skills/private-web-access/internal/config"
)

const schema = "privateweb.passkeys.v1"
const maxCredentials = 8

type owner struct {
	Schema         string                `json:"schema"`
	Origin         string                `json:"origin"`
	ID             []byte                `json:"user_id"`
	Credentials    []webauthn.Credential `json:"credentials"`
	UsedInvitation string                `json:"used_invitation,omitempty"`
}

func (u owner) WebAuthnID() []byte                         { return u.ID }
func (u owner) WebAuthnName() string                       { return "owner" }
func (u owner) WebAuthnDisplayName() string                { return "Private Web owner" }
func (u owner) WebAuthnCredentials() []webauthn.Credential { return u.Credentials }

type invitation struct {
	Origin  string    `json:"origin"`
	Digest  string    `json:"digest"`
	Expires time.Time `json:"expires"`
}

func token() string {
	b := make([]byte, 32)
	if _, e := rand.Read(b); e != nil {
		panic("RANDOM_UNAVAILABLE")
	}
	return base64.RawURLEncoding.EncodeToString(b)
}
func digest(s string) string {
	h := sha256.Sum256([]byte(s))
	return base64.RawURLEncoding.EncodeToString(h[:])
}
func authDir(state string) string { return filepath.Join(state, "browser-auth") }
func privateDir(dir string) error {
	i, e := os.Lstat(dir)
	if e != nil || !i.IsDir() || i.Mode()&os.ModeSymlink != 0 || i.Mode().Perm()&0077 != 0 {
		return ErrAuth
	}
	return nil
}
func atomicJSON(path string, v any) error {
	b, e := json.Marshal(v)
	if e != nil {
		return ErrAuth
	}
	f, e := os.CreateTemp(filepath.Dir(path), ".passkey-")
	if e != nil {
		return ErrAuth
	}
	defer os.Remove(f.Name())
	if _, e = f.Write(b); e != nil {
		f.Close()
		return ErrAuth
	}
	if e = f.Sync(); e != nil {
		f.Close()
		return ErrAuth
	}
	if f.Close() != nil {
		return ErrAuth
	}
	if os.Rename(f.Name(), path) != nil {
		return ErrAuth
	}
	d, e := os.Open(filepath.Dir(path))
	if e != nil {
		return ErrAuth
	}
	defer d.Close()
	if d.Sync() != nil {
		return ErrAuth
	}
	return nil
}
func readOwner(c Config, state string) (owner, error) {
	var u owner
	if privateDir(authDir(state)) != nil {
		return u, ErrAuth
	}
	b, e := config.ReadFile(filepath.Join(authDir(state), "credentials.json"), 1<<20, true)
	if e != nil || json.Unmarshal(b, &u) != nil || u.Schema != schema || u.Origin != c.Origin || len(u.ID) != 32 || u.Credentials == nil || len(u.Credentials) > maxCredentials {
		return owner{}, ErrAuth
	}
	seen := map[string]bool{}
	for _, v := range u.Credentials {
		if len(v.ID) == 0 || len(v.PublicKey) == 0 || seen[string(v.ID)] {
			return owner{}, ErrAuth
		}
		seen[string(v.ID)] = true
	}
	return u, nil
}

// Initialize is explicit; missing credentials never silently reset an owner.
func Initialize(c Config, state string) error {
	if c.Validate() != nil || c.Mode != Mode || privateDir(state) != nil {
		return ErrAuth
	}
	dir := authDir(state)
	if e := os.Mkdir(dir, 0700); e != nil {
		return ErrAuth
	}
	u := owner{Schema: schema, Origin: c.Origin, ID: make([]byte, 32), Credentials: []webauthn.Credential{}}
	// Independent full-entropy opaque user handle, unrelated to machine identity.
	if _, e := rand.Read(u.ID); e != nil {
		return ErrAuth
	}
	return atomicJSON(filepath.Join(dir, "credentials.json"), u)
}

// IssueInvitation is an explicit SSH/OS-owner operation, also usable for account
// recovery. It grants one new owner credential, without removing existing ones.
// No secret is printed: the caller supplies a private output path outside Git.
func IssueInvitation(c Config, state, out string) error {
	u, e := readOwner(c, state)
	if e != nil || len(u.Credentials) >= maxCredentials || !filepath.IsAbs(out) || privateDir(filepath.Dir(out)) != nil {
		return ErrAuth
	}
	t := token()
	expires := time.Now().Add(10 * time.Minute)
	f, e := os.OpenFile(out, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if e != nil {
		return ErrAuth
	}
	defer f.Close()
	if atomicJSON(filepath.Join(authDir(state), "invitation.json"), invitation{c.Origin, digest(t), expires}) != nil {
		os.Remove(out)
		return ErrAuth
	}
	b, _ := json.Marshal(struct {
		URL     string    `json:"url"`
		Expires time.Time `json:"expires"`
	}{c.Origin + "/login#enroll=" + t, expires})
	if _, e = f.Write(b); e != nil {
		return ErrAuth
	}
	if f.Sync() != nil {
		return ErrAuth
	}
	return nil
}
