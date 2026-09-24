package gateway

import (
	"encoding/json"
	"github.com/justinnotime/skills/skills/private-web-access/internal/browserauth"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// Explicit opt-in fixture: generated state and synthetic content only.
func TestBrowserFixture(t *testing.T) {
	out := os.Getenv("PRIVATE_WEB_BROWSER_FIXTURE")
	if out == "" {
		t.Skip("opt-in browser integration fixture")
	}
	state := t.TempDir()
	os.Chmod(state, 0700)
	cred := filepath.Join(state, "upstream.json")
	if os.WriteFile(cred, []byte(`{"authorization":"Bearer synthetic-credential-with-at-least-32-characters"}`), 0600) != nil {
		t.Fatal("credential")
	}
	primary := httptest.NewUnstartedServer(nil)
	defer primary.Close()
	isolated := httptest.NewUnstartedServer(nil)
	defer isolated.Close()
	origin := func(s *httptest.Server) string {
		_, port, _ := strings.Cut(s.Listener.Addr().String(), ":")
		return "https://gateway.example.test:" + port
	}
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Authorization") != "Bearer synthetic-credential-with-at-least-32-characters" || r.Header.Get("Cookie") != "" {
			http.Error(w, "BOUNDARY_FAILED", 401)
			return
		}
		w.Header().Set("Content-Type", "text/html")
		if r.Method == "POST" {
			io.WriteString(w, "ACTION_OK")
			return
		}
		io.WriteString(w, `<!doctype html><h1 id="content">Synthetic application</h1><script src="/auth.js"></script>`)
	}))
	defer upstream.Close()
	c := Config{Schema: "private-web-access/v1", Origin: origin(primary), Listen: primary.Listener.Addr().String(), StateDirectory: state, Applications: []Application{
		{ID: "console", Title: "Console", Mode: "trusted-prefix", Content: "application", Upstream: upstream.URL, CredentialFile: cred, Methods: []string{"GET", "HEAD", "POST"}},
		{ID: "library", Title: "Library", Mode: "isolated-readonly", Content: "published", Origin: origin(isolated), Listen: isolated.Listener.Addr().String(), Upstream: upstream.URL, CredentialFile: cred, Methods: []string{"GET", "HEAD"}},
	}}
	if browserauth.Initialize(c.Authentication(), state) != nil {
		t.Fatal("init")
	}
	invite := filepath.Join(state, "invite.json")
	if browserauth.IssueInvitation(c.Authentication(), state, invite) != nil {
		t.Fatal("invite")
	}
	g, e := New(c)
	if e != nil {
		t.Fatal("gateway")
	}
	defer g.Auth.Close()
	primary.Config.Handler = g.Servers[0].Handler
	isolated.Config.Handler = g.Servers[1].Handler
	primary.StartTLS()
	isolated.StartTLS()
	b, _ := os.ReadFile(invite)
	var v struct {
		URL string `json:"url"`
	}
	json.Unmarshal(b, &v)
	u, _ := url.Parse(v.URL)
	b, _ = json.Marshal(map[string]string{"origin": c.Origin, "isolated": c.Applications[1].Origin, "token": strings.TrimPrefix(u.Fragment, "enroll=")})
	if os.WriteFile(out, b, 0600) != nil {
		t.Fatal("fixture output")
	}
	io.Copy(io.Discard, os.Stdin)
}
