package browserauth

import (
	"github.com/go-webauthn/webauthn/webauthn"
	"io"
	"net/http/httptest"
	"testing"
	"time"
)

func TestExclusiveStateOwner(t *testing.T) {
	a := fixture(t)
	if other, err := New(a.c, a.state); err == nil {
		other.Close()
		t.Fatal("second state owner accepted")
	}
	a.Close()
	b, err := New(a.c, a.state)
	if err != nil {
		t.Fatal("released lock unavailable")
	}
	defer b.Close()
	a.Close() // A second Close must not release the new instance's lock.
	if other, err := New(a.c, a.state); err == nil {
		other.Close()
		t.Fatal("lock lost after duplicate close")
	}
}
func TestSlowBodyDoesNotBlockAuthority(t *testing.T) {
	a := fixture(t)
	reader, writer := io.Pipe()
	defer reader.Close()
	defer writer.Close()
	r := req("POST", "/auth/login/begin", "{}")
	r.Body = reader
	slowDone := make(chan struct{})
	go func() { defer close(slowDone); a.serve(httptest.NewRecorder(), r) }()
	done := make(chan struct{})
	go func() { defer close(done); a.serve(httptest.NewRecorder(), req("GET", "/auth/status", "")) }()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("slow body blocked authority")
	}
	writer.Close()
	<-slowDone
}
func TestLoginCapacityPreservesCeremoniesAndEnrollmentReserve(t *testing.T) {
	a := fixture(t)
	a.user.Credentials = []webauthn.Credential{{ID: []byte("synthetic"), PublicKey: []byte("synthetic")}}
	for i := 0; i < maxPending-16; i++ {
		w := httptest.NewRecorder()
		a.serve(w, req("POST", "/auth/login/begin", `{}`))
		if w.Code != 200 {
			t.Fatal("capacity")
		}
	}
	before := make(map[string]bool)
	for k := range a.pending {
		before[k] = true
	}
	w := httptest.NewRecorder()
	a.serve(w, req("POST", "/auth/login/begin", `{}`))
	if w.Code != 429 {
		t.Fatal("missing load shedding")
	}
	for k := range before {
		if _, ok := a.pending[k]; !ok {
			t.Fatal("live ceremony evicted")
		}
	}
	login := httptest.NewRecorder()
	a.issueSession(login, req("GET", "/", ""), []byte("synthetic"))
	r := req("POST", "/auth/register/begin", `{}`)
	r.AddCookie(login.Result().Cookies()[0])
	w = httptest.NewRecorder()
	a.serve(w, r)
	if w.Code != 200 {
		t.Fatal("authorized enrollment starved")
	}
}
