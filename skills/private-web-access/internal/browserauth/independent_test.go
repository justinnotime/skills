package browserauth

import (
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"sync"
	"testing"
	"time"
)

type reviewSignaledReader struct {
	io.ReadCloser
	once    sync.Once
	started chan struct{}
}

func (r *reviewSignaledReader) Read(p []byte) (int, error) {
	r.once.Do(func() { close(r.started) })
	return r.ReadCloser.Read(p)
}

func TestIndependentFinalSlowBodiesPreserveReadAndLogout(t *testing.T) {
	a := fixture(t)
	login := httptest.NewRecorder()
	a.issueSession(login, req("GET", "/", ""), []byte("synthetic"))
	session := login.Result().Cookies()[0]
	h := a.Wrap(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("synthetic protected")) }))
	var pipes []*io.PipeWriter
	var done []chan struct{}
	for range 8 {
		reader, writer := io.Pipe()
		pipes = append(pipes, writer)
		t.Cleanup(func() { reader.Close(); writer.Close() })
		signal := &reviewSignaledReader{ReadCloser: reader, started: make(chan struct{})}
		r := req("POST", "/auth/register/begin", "")
		r.Body = signal
		ch := make(chan struct{})
		done = append(done, ch)
		go func() { defer close(ch); h.ServeHTTP(httptest.NewRecorder(), r) }()
		<-signal.started
	}
	completion := make(chan struct{})
	go func() {
		defer close(completion)
		r := req("GET", "/synthetic", "")
		r.AddCookie(session)
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		if w.Code != 200 {
			t.Error("authenticated GET starved")
		}
		logout := req("POST", "/auth/logout", "")
		logout.Header.Set("X-Access-Action", "logout")
		logout.AddCookie(session)
		w = httptest.NewRecorder()
		h.ServeHTTP(w, logout)
		if w.Code != 200 {
			t.Error("logout starved")
		}
	}()
	select {
	case <-completion:
	case <-time.After(time.Second):
		t.Fatal("slow request bodies blocked authenticated read/logout")
	}
	extra := httptest.NewRecorder()
	h.ServeHTTP(extra, req("POST", "/auth/login/begin", "{}"))
	if extra.Code != 429 {
		t.Fatal("body concurrency not bounded")
	}
	for _, p := range pipes {
		p.Close()
	}
	for _, ch := range done {
		<-ch
	}
}

func TestIndependentFinalInterprocessLock(t *testing.T) {
	if os.Getenv("PRIVATEWEB_REVIEW_LOCK_CHILD") == "1" {
		a, e := New(Config{Mode: Mode, Origin: "https://gateway.example.test"}, os.Getenv("PRIVATEWEB_REVIEW_STATE"))
		if e == nil {
			a.Close()
			os.Exit(2)
		}
		os.Exit(0)
	}
	a := fixture(t)
	cmd := exec.Command(os.Args[0], "-test.run=^TestIndependentFinalInterprocessLock$")
	cmd.Env = append(os.Environ(), "PRIVATEWEB_REVIEW_LOCK_CHILD=1", "PRIVATEWEB_REVIEW_STATE="+a.state)
	if out, e := cmd.CombinedOutput(); e != nil {
		t.Fatalf("second process not blocked: %v %s", e, out)
	}
}
