package gateway

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"net/http/httptrace"
	"net/textproto"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func reviewProxy(t *testing.T, handler http.HandlerFunc) http.Handler {
	t.Helper()
	upstream := httptest.NewServer(handler)
	t.Cleanup(upstream.Close)
	secretFile := filepath.Join(t.TempDir(), "synthetic.json")
	if err := os.WriteFile(secretFile, []byte(`{"authorization":"Bearer synthetic-testing-value-not-a-secret"}`), 0600); err != nil {
		t.Fatal(err)
	}
	proxy, err := Proxy(Application{ID: "results", Mode: "isolated-readonly", Content: "published", Origin: "https://gateway.example.test:8443", Upstream: upstream.URL, CredentialFile: secretFile, Methods: []string{"GET", "HEAD", "POST"}}, "https://gateway.example.test")
	if err != nil {
		t.Fatal(err)
	}
	return proxy
}

func TestInformationalHeadersStayInsideBoundary(t *testing.T) {
	proxy := reviewProxy(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Set-Cookie", "__Host-privateweb-session=synthetic-injection; Secure; HttpOnly; Path=/")
		w.Header().Set("Link", "<https://external.example.test/synthetic>; rel=preload; as=script")
		w.Header().Set("Content-Security-Policy", "default-src * 'unsafe-inline'")
		w.WriteHeader(http.StatusEarlyHints)
		w.Header().Del("Set-Cookie")
		w.Header().Del("Link")
		w.Header().Del("Content-Security-Policy")
		w.WriteHeader(200)
		w.Write([]byte("synthetic"))
	})
	server := httptest.NewServer(proxy)
	defer server.Close()
	var hints http.Header
	trace := &httptrace.ClientTrace{Got1xxResponse: func(code int, h textproto.MIMEHeader) error {
		if code == 103 {
			hints = http.Header(h).Clone()
		}
		return nil
	}}
	req, _ := http.NewRequestWithContext(httptrace.WithClientTrace(context.Background(), trace), "GET", server.URL+"/", nil)
	resp, err := server.Client().Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, resp.Body)
	if len(hints) != 0 {
		t.Fatal("informational headers escaped")
	}
	if resp.Header.Get("Set-Cookie") != "" {
		t.Fatal("final headers escaped")
	}

}

func TestRequestTrailersAreNotForwarded(t *testing.T) {
	upstreamHeaders := make(chan http.Header, 1)
	proxy := reviewProxy(t, func(w http.ResponseWriter, r *http.Request) {
		io.Copy(io.Discard, r.Body)
		upstreamHeaders <- r.Trailer.Clone()
		w.Write([]byte("synthetic"))
	})
	req := httptest.NewRequest("POST", "http://gateway.example.test/action", strings.NewReader("synthetic"))
	req.Trailer = http.Header{"Cookie": {"synthetic=trailer"}, "Authorization": {"Bearer synthetic-attacker"}}
	w := httptest.NewRecorder()
	proxy.ServeHTTP(w, req)
	got := <-upstreamHeaders
	if len(got) > 0 {
		t.Fatalf("trailer forwarding: %#v", got)
	}
}

func TestResponseTrailersAreNotForwarded(t *testing.T) {
	proxy := reviewProxy(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Add("Trailer", "Set-Cookie")
		w.Header().Add("Trailer", "X-Synthetic-Credential")
		w.Write([]byte("synthetic"))
		w.Header().Set("Set-Cookie", "synthetic=trailer")
		w.Header().Set("X-Synthetic-Credential", "synthetic-trailer-value")
	})
	server := httptest.NewServer(proxy)
	defer server.Close()
	resp, err := server.Client().Get(server.URL + "/")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	io.Copy(io.Discard, resp.Body)
	if len(resp.Trailer) > 0 {
		t.Fatalf("response trailers forwarded: %#v", resp.Trailer)
	}
}
