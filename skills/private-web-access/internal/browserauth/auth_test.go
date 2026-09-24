package browserauth

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/go-webauthn/webauthn/webauthn"
)

func fixture(t *testing.T) *Auth {
	t.Helper()
	state := t.TempDir()
	if os.Chmod(state, 0700) != nil {
		t.Fatal("mode")
	}
	c := Config{Mode: Mode, Origin: "https://gateway.example.test"}
	if Initialize(c, state) != nil {
		t.Fatal("initialize")
	}
	a, e := New(c, state)
	if e != nil {
		t.Fatal("new")
	}
	t.Cleanup(a.Close)
	return a
}
func req(method, path, body string) *http.Request {
	r := httptest.NewRequest(method, "https://gateway.example.test"+path, strings.NewReader(body))
	r.Header.Set("Origin", "https://gateway.example.test")
	r.Header.Set("Content-Type", "application/json")
	return r
}
func TestOriginAndStateFailClosed(t *testing.T) {
	a := fixture(t)
	for _, c := range []Config{{Mode: "unknown"}, {Mode: Mode}, {Origin: a.c.Origin}, {Mode: Mode, Origin: "http://gateway.example.test"}, {Mode: Mode, Origin: "https://127.0.0.1"}, {Mode: Mode, Origin: "https://gateway.example.test/"}, {Mode: Mode, Origin: "https://gateway.example.test?"}, {Mode: Mode, Origin: "https://gateway.example.test#fragment"}} {
		if c.Validate() == nil {
			t.Fatal("unsafe config accepted")
		}
	}
	for _, change := range []func(*http.Request){func(r *http.Request) { r.Host = "localhost" }, func(r *http.Request) { r.Header.Del("Origin") }, func(r *http.Request) { r.Header.Set("Origin", "https://evil.example.test") }, func(r *http.Request) { r.Header.Set("Origin", "http://gateway.example.test") }, func(r *http.Request) { r.Header.Set("Sec-Fetch-Site", "cross-site") }} {
		r := req("POST", "/auth/login/begin", "{}")
		change(r)
		if a.CheckOrigin(r) {
			t.Fatal("origin bypass")
		}
	}
	r := req("POST", "/auth/login/begin", "{}")
	r.Host = "evil.example.test"
	r.Header.Set("X-Forwarded-Host", "gateway.example.test")
	if a.CheckOrigin(r) {
		t.Fatal("forwarded authority trusted")
	}
	file := filepath.Join(authDir(a.state), "credentials.json")
	if os.Remove(file) != nil {
		t.Fatal("remove")
	}
	if _, e := New(a.c, a.state); e == nil {
		t.Fatal("missing state reset")
	}
	if os.WriteFile(file, []byte(`{"schema":"privateweb.passkeys.v1"}`), 0600) != nil {
		t.Fatal("write")
	}
	if _, e := New(a.c, a.state); e == nil {
		t.Fatal("partial state accepted")
	}
}
func TestInvitationRequiredBoundedAndSingleUse(t *testing.T) {
	a := fixture(t)
	w := httptest.NewRecorder()
	a.serve(w, req("POST", "/auth/register/begin", `{}`))
	if w.Code != 403 {
		t.Fatal("open registration")
	}
	out := filepath.Join(a.state, "invitation.private.json")
	if IssueInvitation(a.c, a.state, out) != nil {
		t.Fatal("invite")
	}
	b, _ := os.ReadFile(out)
	var v struct {
		URL string `json:"url"`
	}
	if json.Unmarshal(b, &v) != nil {
		t.Fatal("invite read")
	}
	token := strings.Split(v.URL, "#enroll=")[1]
	w = httptest.NewRecorder()
	a.serve(w, req("POST", "/auth/register/begin", `{"token":"`+token+`"}`))
	if w.Code != 200 {
		t.Fatal("invitation refused")
	}
	var options map[string]any
	if json.Unmarshal(w.Body.Bytes(), &options) != nil {
		t.Fatal("options")
	}
	pk := options["publicKey"].(map[string]any)
	sel := pk["authenticatorSelection"].(map[string]any)
	if sel["userVerification"] != "required" || sel["residentKey"] != "required" || pk["attestation"] != "none" {
		t.Fatal("weakened authenticator policy")
	}
	cs := w.Result().Cookies()
	if len(cs) != 1 || !cs[0].Secure || !cs[0].HttpOnly || cs[0].SameSite != http.SameSiteStrictMode {
		t.Fatal("ceremony cookie")
	}
	finish := req("POST", "/auth/register/finish", `{}`)
	finish.AddCookie(cs[0])
	w = httptest.NewRecorder()
	a.serve(w, finish)
	if w.Code != 403 || len(a.pending) != 0 {
		t.Fatal("invalid finish/replay")
	}
	a.user.UsedInvitation = digest(token)
	if a.validInvitation(digest(token)) {
		t.Fatal("invite reused")
	}
	a.user.UsedInvitation = ""
	inv := invitation{a.c.Origin, digest(token), time.Now().Add(-time.Second)}
	if atomicJSON(filepath.Join(authDir(a.state), "invitation.json"), inv) != nil {
		t.Fatal("write")
	}
	if a.validInvitation(digest(token)) {
		t.Fatal("expired invite accepted")
	}
	for i := 0; i < 40; i++ {
		w = httptest.NewRecorder()
		a.serve(w, req("POST", "/auth/register/begin", `{}`))
	}
	if w.Code != 403 || len(a.pending) != 0 {
		t.Fatal("invalid registration consumed capacity")
	}
}
func TestSessionsExpiryRevocationFreshnessAndCredentialRemoval(t *testing.T) {
	a := fixture(t)
	a.user.Credentials = []webauthn.Credential{{ID: []byte("test-a"), PublicKey: []byte("synthetic")}, {ID: []byte("test-b"), PublicKey: []byte("synthetic")}}
	login := httptest.NewRecorder()
	a.issueSession(login, req("GET", "/", ""), []byte("test-a"))
	c := login.Result().Cookies()[0]
	call := func(method, path, body string) *httptest.ResponseRecorder {
		r := req(method, path, body)
		r.AddCookie(c)
		r.Header.Set("X-Access-Action", "logout")
		w := httptest.NewRecorder()
		a.Wrap(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = w.Write([]byte("protected")) })).ServeHTTP(w, r)
		return w
	}
	if call("GET", "/asset.bin", "").Code != 200 {
		t.Fatal("authorized denied")
	}
	s := a.sessions[digest(c.Value)]
	s.verified = time.Now().Add(-freshLifetime - time.Second)
	for _, path := range []string{"/api/items/clear", "/api/events/clear", "/api/resume", "/console/api/items/clear", "/console/api/events/clear", "/console/api/resume"} {
		if call("POST", path, "").Code != 403 {
			t.Fatal("old session destructive access")
		}
	}
	s.verified = time.Now()
	if call("POST", "/auth/credentials/remove", `{"handle":"`+digest("test-a")+`"}`).Code != 200 || s.active() {
		t.Fatal("credential revocation")
	}
	if call("GET", "/asset.bin", "").Code != 401 {
		t.Fatal("revoked session accepted")
	}
	if len(a.user.Credentials) != 1 || string(a.user.Credentials[0].ID) != "test-b" {
		t.Fatal("removed wrong credential")
	}
	a.issueSession(login, req("GET", "/", ""), []byte("test-b"))
	for _, s := range a.sessions {
		s.expires = time.Now().Add(-time.Second)
	}
	a.prune()
	if len(a.sessions) != 0 {
		t.Fatal("expired session retained")
	}
	a.Close()
	reloaded, e := New(a.c, a.state)
	if e != nil || len(reloaded.sessions) != 0 || len(reloaded.user.Credentials) != 1 {
		t.Fatal("durable keys/ephemeral sessions")
	}
	reloaded.Close()
}
func TestRevocationStopsStreamAndRejectsSubsequentWrites(t *testing.T) {
	a := fixture(t)
	login := httptest.NewRecorder()
	a.issueSession(login, req("GET", "/", ""), []byte("synthetic"))
	c := login.Result().Cookies()[0]
	started, done := make(chan struct{}), make(chan struct{})
	var wg sync.WaitGroup
	h := a.Wrap(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if _, e := w.Write([]byte("first")); e != nil {
			t.Error("first write")
		}
		close(started)
		<-r.Context().Done()
		if _, e := w.Write([]byte("late")); e == nil {
			t.Error("write after revoke")
		}
		close(done)
	}))
	r := req("GET", "/stream.mjpg", "")
	r.AddCookie(c)
	w := httptest.NewRecorder()
	wg.Go(func() { h.ServeHTTP(w, r) })
	<-started
	logout := req("POST", "/auth/logout", "")
	logout.Header.Set("X-Access-Action", "logout")
	logout.AddCookie(c)
	out := httptest.NewRecorder()
	a.serve(out, logout)
	if out.Code != 200 {
		t.Fatal("logout")
	}
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("stream not canceled")
	}
	wg.Wait()
	if w.Body.String() != "first" {
		t.Fatal("late bytes")
	}
	// Expiration also cancels without a second incoming request.
	s := newSession("synthetic")
	s.cancel()
	s.ctx, s.cancel = context.WithTimeout(context.Background(), 10*time.Millisecond)
	defer s.cancel()
	select {
	case <-s.ctx.Done():
	case <-time.After(time.Second):
		t.Fatal("expiry")
	}
	if s.active() {
		t.Fatal("expired active")
	}
}
func TestPersistenceFailurePoisonsAuthority(t *testing.T) {
	a := fixture(t)
	path := filepath.Join(authDir(a.state), "credentials.json")
	if os.Remove(path) != nil || os.Mkdir(path, 0700) != nil {
		t.Fatal("fault setup")
	}
	if a.persist(a.user) == nil || !a.broken {
		t.Fatal("durability not fail closed")
	}
	w := httptest.NewRecorder()
	a.serve(w, req("POST", "/auth/register/begin", `{}`))
	if w.Code != 503 {
		t.Fatal("authority survived uncertain persistence")
	}
}
