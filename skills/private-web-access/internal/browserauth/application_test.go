package browserauth

import (
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestApplicationSessionAndOriginBoundary(t *testing.T) {
	a := fixture(t)
	s := newSession("synthetic")
	tok := token()
	a.sessions[digest(tok)] = s
	const origin = "https://gateway.example.test:8443"
	called := 0
	h := a.ProtectApplication(origin, "results", http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { called++; w.Write([]byte("SYNTHETIC")) }))
	request := func(method, path string) *http.Request {
		r := httptest.NewRequest(method, origin+path, nil)
		r.AddCookie(&http.Cookie{Name: sessionCookie, Value: tok})
		return r
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, request("GET", "/"))
	if w.Code != 200 || called != 1 {
		t.Fatal("shared session refused")
	}
	for _, change := range []func(*http.Request){
		func(r *http.Request) { r.Method = "POST" },
		func(r *http.Request) { r.Host = "gateway.example.test" },
		func(r *http.Request) { r.Header.Set("Origin", a.c.Origin) },
		func(r *http.Request) { r.Header.Set("Sec-Fetch-Site", "same-site") },
		func(r *http.Request) { r.Header.Set("Sec-Fetch-Site", "cross-site") },
		func(r *http.Request) { r.Header.Set("Upgrade", "websocket") },
		func(r *http.Request) { r.ContentLength = 1 },
		func(r *http.Request) { r.AddCookie(&http.Cookie{Name: sessionCookie, Value: tok}) },
		func(r *http.Request) { r.URL.Path = "/auth/status" },
	} {
		r := request("GET", "/api/data")
		change(r)
		w = httptest.NewRecorder()
		h.ServeHTTP(w, r)
		if w.Code < 400 || called != 1 {
			t.Fatal("application boundary bypass")
		}
	}
	// Top-level navigation from the portal is allowed; ambient resource requests
	// from sibling applications are not, even though cookies share the hostname.
	r := request("GET", "/")
	r.Header.Set("Sec-Fetch-Site", "same-site")
	r.Header.Set("Sec-Fetch-Mode", "navigate")
	r.Header.Set("Sec-Fetch-Dest", "document")
	w = httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != 200 {
		t.Fatal("portal navigation refused")
	}
	s.expires = time.Now().Add(-time.Second)
	w = httptest.NewRecorder()
	h.ServeHTTP(w, request("GET", "/asset"))
	if w.Code != 401 {
		t.Fatal("expired session accepted")
	}
	w = httptest.NewRecorder()
	h.ServeHTTP(w, request("GET", "/"))
	if w.Code != 303 || w.Header().Get("Location") != a.c.Origin+"/login?next=results" {
		t.Fatal("login return lost")
	}
}

func TestSiblingOriginCannotUseCentralAuthority(t *testing.T) {
	a := fixture(t)
	for _, path := range []string{"/auth/status", "/auth/login/begin", "/api/status", "/console/frame.jpg"} {
		r := req("GET", path, "")
		r.Header.Del("Origin")
		r.Header.Set("Sec-Fetch-Site", "same-site")
		if a.CheckOrigin(r) {
			t.Fatal("sibling origin admitted to central API")
		}
	}
}
