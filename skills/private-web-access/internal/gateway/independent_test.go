package gateway

import (
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
)

func TestIndependentFinalConfigurationBoundaries(t *testing.T) {
	fresh := func() Config {
		return Config{Schema: "private-web-access/v1", Origin: "https://gateway.example.test", Listen: "127.0.0.1:9000", StateDirectory: "/synthetic/state", Applications: []Application{{ID: "viewer", Title: "Viewer", Mode: "isolated-readonly", Content: "published", Origin: "https://gateway.example.test:8443", Listen: "127.0.0.1:9001", Upstream: "http://127.0.0.1:9100", CredentialFile: "/synthetic/upstream.json", Methods: []string{"GET", "HEAD"}}}}
	}
	if err := fresh().Validate(); err != nil {
		t.Fatal("valid config rejected")
	}
	cases := map[string]func(*Config){
		"public-listener":      func(c *Config) { c.Listen = "0.0.0.0:9000" },
		"upstream-dns":         func(c *Config) { c.Applications[0].Upstream = "http://localhost:9100" },
		"upstream-external":    func(c *Config) { c.Applications[0].Upstream = "http://192.0.2.1:9100" },
		"upstream-credentials": func(c *Config) { c.Applications[0].Upstream = "http://user:synthetic@127.0.0.1:9100" },
		"upstream-path":        func(c *Config) { c.Applications[0].Upstream = "http://127.0.0.1:9100/private" },
		"upstream-query":       func(c *Config) { c.Applications[0].Upstream = "http://127.0.0.1:9100?synthetic=1" },
		"upstream-self":        func(c *Config) { c.Applications[0].Upstream = "http://127.0.0.1:9000" },
		"plaintext-origin":     func(c *Config) { c.Origin = "http://gateway.example.test" },
		"different-hostname":   func(c *Config) { c.Applications[0].Origin = "https://other.example.test:8443" },
		"same-origin":          func(c *Config) { c.Applications[0].Origin = c.Origin },
		"duplicate-port":       func(c *Config) { c.Applications[0].Listen = c.Listen },
		"isolated-post":        func(c *Config) { c.Applications[0].Methods = append(c.Applications[0].Methods, "POST") },
		"published-trusted": func(c *Config) {
			c.Applications[0].Mode = "trusted-prefix"
			c.Applications[0].Origin = ""
			c.Applications[0].Listen = ""
		},
		"forward-cookie":          func(c *Config) { c.Applications[0].ForwardHeaders = []string{"Cookie"} },
		"forward-authorization":   func(c *Config) { c.Applications[0].ForwardHeaders = []string{"Authorization"} },
		"forward-host":            func(c *Config) { c.Applications[0].ForwardHeaders = []string{"X-Forwarded-Host"} },
		"forward-tailscale":       func(c *Config) { c.Applications[0].ForwardHeaders = []string{"X-Tailscale-User-Login"} },
		"forward-reserved-action": func(c *Config) { c.Applications[0].ForwardHeaders = []string{"X-Access-Action"} },
	}
	for name, change := range cases {
		t.Run(name, func(t *testing.T) {
			c := fresh()
			change(&c)
			if c.Validate() == nil {
				t.Fatal("unsafe config accepted")
			}
		})
	}
}

func TestIndependentFinalProxyCredentialAndResponseBoundary(t *testing.T) {
	var called atomic.Int32
	up := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		called.Add(1)
		if r.Header.Get("Authorization") != "Bearer synthetic-final-review-upstream-secret" {
			t.Error("credential not replaced")
		}
		for _, name := range []string{"Cookie", "Origin", "Referer", "Forwarded", "X-Forwarded-Host", "X-Forwarded-For", "X-Tailscale-User-Login", "X-Unconfigured"} {
			if r.Header.Get(name) != "" {
				t.Errorf("browser header leaked: %s", name)
			}
		}
		if r.Header.Get("X-Application-Action") != "synthetic" {
			t.Error("explicit app header missing")
		}
		if r.URL.Path != "/document" {
			t.Errorf("unexpected backend path %q", r.URL.Path)
		}
		if r.URL.Query().Get("mode") == "external" {
			w.Header().Set("Location", "https://external.example.test/synthetic")
			w.WriteHeader(302)
			return
		}
		if r.URL.Query().Get("mode") == "internal" {
			w.Header().Set("Location", "/next")
			w.WriteHeader(302)
			return
		}
		for name, value := range map[string]string{"Set-Cookie": "synthetic=unsafe", "Access-Control-Allow-Origin": "*", "Refresh": "0;url=https://external.example.test", "Link": "<https://external.example.test>;rel=preload", "Content-Security-Policy": "default-src *", "X-Synthetic-Secret": "must-not-escape"} {
			w.Header().Set(name, value)
		}
		w.Header().Set("Content-Type", "text/plain")
		w.Write([]byte("synthetic body"))
	}))
	defer up.Close()
	secret := filepath.Join(t.TempDir(), "secret.json")
	if err := os.WriteFile(secret, []byte(`{"authorization":"Bearer synthetic-final-review-upstream-secret"}`), 0600); err != nil {
		t.Fatal(err)
	}
	app := Application{ID: "console", Mode: "trusted-prefix", Content: "application", Upstream: up.URL, CredentialFile: secret, Methods: []string{"GET", "HEAD", "POST"}, ForwardHeaders: []string{"X-Application-Action"}}
	proxy, err := Proxy(app, "https://gateway.example.test")
	if err != nil {
		t.Fatal(err)
	}
	request := func(path string) *http.Request {
		r := httptest.NewRequest("GET", "https://gateway.example.test"+path, nil)
		for _, name := range []string{"Cookie", "Origin", "Referer", "Forwarded", "X-Forwarded-Host", "X-Forwarded-For", "X-Tailscale-User-Login", "X-Unconfigured", "Authorization", "X-Application-Action"} {
			r.Header.Set(name, "synthetic")
		}
		return r
	}
	for _, mode := range []string{"", "?mode=external", "?mode=internal"} {
		w := httptest.NewRecorder()
		proxy.ServeHTTP(w, request("/console/document"+mode))
		if mode == "?mode=external" {
			if w.Code != 502 {
				t.Fatal("external redirect accepted")
			}
			continue
		}
		if mode == "?mode=internal" {
			if w.Header().Get("Location") != "https://gateway.example.test/console/next" {
				t.Fatal("internal redirect not constrained")
			}
			continue
		}
		if w.Code != 200 || w.Body.String() != "synthetic body" {
			t.Fatal("proxy failed")
		}
		for _, name := range []string{"Set-Cookie", "Access-Control-Allow-Origin", "Refresh", "Link", "X-Synthetic-Secret"} {
			if w.Header().Get(name) != "" {
				t.Errorf("upstream header escaped: %s", name)
			}
		}
		if !strings.Contains(w.Header().Get("Content-Security-Policy"), "worker-src 'none'") || !strings.Contains(w.Header().Get("Permissions-Policy"), "publickey-credentials-get=(self)") {
			t.Fatal("gateway browser policy not restored")
		}
	}
	for _, change := range []func(*http.Request){func(r *http.Request) { r.Method = "DELETE" }, func(r *http.Request) { r.Header.Set("Upgrade", "websocket") }, func(r *http.Request) { r.Body = io.NopCloser(strings.NewReader("x")); r.ContentLength = 1 }, func(r *http.Request) { r.URL.Path = "/console/../document" }, func(r *http.Request) { r.URL.Path = "/console//document" }, func(r *http.Request) { r.URL.Path = "/console/\\document" }} {
		before := called.Load()
		r := request("/console/document")
		change(r)
		w := httptest.NewRecorder()
		proxy.ServeHTTP(w, r)
		if w.Code < 400 || called.Load() != before {
			t.Fatal("rejected request reached backend")
		}
	}
}
