package browserauth

import (
	"context"
	"net/http"
	"sync"
	"time"
)

const sessionCookie = "__Host-privateweb-session"
const ceremonyCookie = "__Host-privateweb-ceremony"
const sessionLifetime = time.Hour
const freshLifetime = 5 * time.Minute

type session struct {
	mu                sync.Mutex
	ctx               context.Context
	cancel            context.CancelFunc
	expires, verified time.Time
	credential        string
	revoked           bool
}

func newSession(credential string) *session {
	now := time.Now()
	ctx, cancel := context.WithDeadline(context.Background(), now.Add(sessionLifetime))
	return &session{ctx: ctx, cancel: cancel, expires: now.Add(sessionLifetime), verified: now, credential: credential}
}
func (s *session) valid() bool {
	return !s.revoked && time.Now().Before(s.expires) && s.ctx.Err() == nil
}
func (s *session) revoke()      { s.cancel(); s.mu.Lock(); s.revoked = true; s.mu.Unlock() }
func (s *session) active() bool { s.mu.Lock(); defer s.mu.Unlock(); return s.valid() }
func cookie(w http.ResponseWriter, name, value string, ttl time.Duration) {
	age := int(ttl.Seconds())
	if ttl < 0 {
		age = -1
	}
	http.SetCookie(w, &http.Cookie{Name: name, Value: value, Path: "/", Secure: true, HttpOnly: true, SameSite: http.SameSiteStrictMode, MaxAge: age})
}

// Each response write/flush is serialized with revocation; a slow in-flight
// write has a five-second deadline. No later response chunk is admitted after revocation.
type guardedWriter struct {
	http.ResponseWriter
	s *session
}

func (w *guardedWriter) Unwrap() http.ResponseWriter { return w.ResponseWriter }
func (w *guardedWriter) Write(b []byte) (int, error) {
	w.s.mu.Lock()
	defer w.s.mu.Unlock()
	if !w.s.valid() {
		return 0, ErrAuth
	}
	_ = http.NewResponseController(w.ResponseWriter).SetWriteDeadline(time.Now().Add(5 * time.Second))
	return w.ResponseWriter.Write(b)
}
func (w *guardedWriter) FlushError() error {
	w.s.mu.Lock()
	defer w.s.mu.Unlock()
	if !w.s.valid() {
		return ErrAuth
	}
	_ = http.NewResponseController(w.ResponseWriter).SetWriteDeadline(time.Now().Add(5 * time.Second))
	return http.NewResponseController(w.ResponseWriter).Flush()
}
