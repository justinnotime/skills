package browserauth

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/fxamacker/cbor/v2"
)

func reviewB64(b []byte) string { return base64.RawURLEncoding.EncodeToString(b) }
func reviewJSON(t *testing.T, v any) string {
	t.Helper()
	b, e := json.Marshal(v)
	if e != nil {
		t.Fatal(e)
	}
	return string(b)
}
func reviewClient(t *testing.T, challenge, origin, kind string) []byte {
	return []byte(reviewJSON(t, map[string]any{"type": "webauthn." + kind, "challenge": challenge, "origin": origin, "crossOrigin": false}))
}
func reviewAuthData(host string, flags byte, count uint32) []byte {
	h := sha256.Sum256([]byte(host))
	b := append([]byte{}, h[:]...)
	b = append(b, flags)
	b = binary.BigEndian.AppendUint32(b, count)
	return b
}
func reviewBegin(t *testing.T, a *Auth, kind, token string) (string, *http.Cookie) {
	t.Helper()
	w := httptest.NewRecorder()
	a.Wrap(http.NotFoundHandler()).ServeHTTP(w, req("POST", "/auth/"+kind+"/begin", reviewJSON(t, map[string]string{"token": token})))
	if w.Code != 200 {
		t.Fatalf("begin %s: %d %s", kind, w.Code, w.Body.String())
	}
	var p struct {
		PublicKey struct {
			Challenge string `json:"challenge"`
		} `json:"publicKey"`
	}
	if json.Unmarshal(w.Body.Bytes(), &p) != nil {
		t.Fatal("options")
	}
	return p.PublicKey.Challenge, w.Result().Cookies()[0]
}
func reviewFinish(t *testing.T, a *Auth, kind, body string, c *http.Cookie) *httptest.ResponseRecorder {
	t.Helper()
	r := req("POST", "/auth/"+kind+"/finish", body)
	r.AddCookie(c)
	w := httptest.NewRecorder()
	a.Wrap(http.NotFoundHandler()).ServeHTTP(w, r)
	return w
}

func TestCryptographicEnrollmentLoginOriginUVReplay(t *testing.T) {
	a := fixture(t)
	key, e := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if e != nil {
		t.Fatal(e)
	}
	id := []byte("synthetic-credential-for-gateway-review")
	out := filepath.Join(a.state, "invite.json")
	if IssueInvitation(a.c, a.state, out) != nil {
		t.Fatal("issue")
	}
	ib, _ := os.ReadFile(out)
	var invite struct {
		URL string `json:"url"`
	}
	json.Unmarshal(ib, &invite)
	token := strings.Split(invite.URL, "#enroll=")[1]
	challenge, ceremony := reviewBegin(t, a, "register", token)
	pk, e := cbor.Marshal(map[int]any{1: 2, 3: -7, -1: 1, -2: key.X.FillBytes(make([]byte, 32)), -3: key.Y.FillBytes(make([]byte, 32))})
	if e != nil {
		t.Fatal(e)
	}
	ad := reviewAuthData("gateway.example.test", 0x45, 0)
	ad = append(ad, make([]byte, 16)...)
	ad = binary.BigEndian.AppendUint16(ad, uint16(len(id)))
	ad = append(ad, id...)
	ad = append(ad, pk...)
	att, e := cbor.Marshal(map[string]any{"fmt": "none", "attStmt": map[string]any{}, "authData": ad})
	if e != nil {
		t.Fatal(e)
	}
	register := reviewJSON(t, map[string]any{"id": reviewB64(id), "rawId": reviewB64(id), "type": "public-key", "response": map[string]any{"attestationObject": reviewB64(att), "clientDataJSON": reviewB64(reviewClient(t, challenge, a.c.Origin, "create")), "transports": []string{"internal"}}, "clientExtensionResults": map[string]any{}})
	w := reviewFinish(t, a, "register", register, ceremony)
	if w.Code != 200 {
		t.Fatalf("registration: %d %s", w.Code, w.Body.String())
	}
	if len(a.user.Credentials) != 1 || len(a.sessions) != 1 || a.validInvitation(digest(token)) {
		t.Fatal("registration state")
	}
	if reviewFinish(t, a, "register", register, ceremony).Code != 403 {
		t.Fatal("registration replay")
	}
	for _, test := range []struct {
		name, origin string
		flags        byte
		valid        bool
	}{
		{"valid", a.c.Origin, 0x05, true},
		{"wrong-port", "https://gateway.example.test:8443", 0x05, false},
		{"wrong-origin", "https://attacker.example.test", 0x05, false},
		{"no-user-verification", a.c.Origin, 0x01, false},
		{"no-user-presence", a.c.Origin, 0x04, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			challenge, ceremony := reviewBegin(t, a, "login", "")
			cd := reviewClient(t, challenge, test.origin, "get")
			cdh := sha256.Sum256(cd)
			ad := reviewAuthData("gateway.example.test", test.flags, 0)
			signed := sha256.Sum256(append(append([]byte{}, ad...), cdh[:]...))
			sig, e := ecdsa.SignASN1(rand.Reader, key, signed[:])
			if e != nil {
				t.Fatal(e)
			}
			body := reviewJSON(t, map[string]any{"id": reviewB64(id), "rawId": reviewB64(id), "type": "public-key", "response": map[string]string{"authenticatorData": reviewB64(ad), "clientDataJSON": reviewB64(cd), "signature": reviewB64(sig), "userHandle": reviewB64(a.user.ID)}, "clientExtensionResults": map[string]any{}})
			w := reviewFinish(t, a, "login", body, ceremony)
			if test.valid && w.Code != 200 || !test.valid && w.Code != 403 {
				t.Fatalf("unexpected result: %d %s", w.Code, w.Body.String())
			}
			if reviewFinish(t, a, "login", body, ceremony).Code != 403 {
				t.Fatal("assertion replay")
			}
		})
	}
}
