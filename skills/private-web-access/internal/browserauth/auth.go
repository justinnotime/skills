package browserauth

import (
	"bytes"
	"context"
	"crypto/subtle"
	"encoding/json"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/go-webauthn/webauthn/protocol"
	"github.com/go-webauthn/webauthn/webauthn"
	"github.com/justinnotime/skills/skills/private-web-access/internal/config"
)

const maxPending = 64
const maxSessions = 32
const challengeLifetime = 3 * time.Minute

type pending struct {
	data                     webauthn.SessionData
	expires                  time.Time
	kind, invitation, parent string
}
type Auth struct {
	mu        sync.Mutex
	c         Config
	state     string
	user      owner
	wa        *webauthn.WebAuthn
	pending   map[string]pending
	sessions  map[string]*session
	bodySlots chan struct{}
	broken    bool
	stateLock *os.File
}

func New(c Config, state string) (*Auth, error) {
	if c.Mode == "" {
		if c.Validate() != nil {
			return nil, ErrAuth
		}
		return nil, nil
	}
	if c.Validate() != nil {
		return nil, ErrAuth
	}
	lock, e := lockState(state)
	if e != nil {
		return nil, ErrAuth
	}
	success := false
	defer func() {
		if !success {
			releaseState(lock)
		}
	}()
	u, e := readOwner(c, state)
	if e != nil {
		return nil, ErrAuth
	}
	wa, e := newWebAuthn(c)
	if e != nil {
		return nil, ErrAuth
	}
	success = true
	return &Auth{stateLock: lock, c: c, state: state, user: u, wa: wa, pending: map[string]pending{}, sessions: map[string]*session{}, bodySlots: make(chan struct{}, 8)}, nil
}
func (a *Auth) cancelAll() {
	for _, s := range a.sessions {
		s.cancel()
	}
}
func (a *Auth) Close() {
	if a == nil {
		return
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	a.broken = true
	a.cancelAll()
	if a.stateLock != nil {
		releaseState(a.stateLock)
		a.stateLock = nil
	}
	for _, s := range a.sessions {
		s.revoke()
	}
}

// The loopback listener is behind a TLS terminator that preserves Host. Forwarded
// identity/host headers are never used as authentication or origin authority.
func shellPath(path string) bool {
	if path == "/" || path == "/login" {
		return true
	}
	return regexp.MustCompile(`^/(?:apps/)?[a-z][a-z0-9-]{0,31}/$`).MatchString(path)
}
func loginTarget(path string) string {
	if path == "/" || path == "/login" {
		return "/login"
	}
	id := strings.Trim(strings.TrimPrefix(path, "/apps/"), "/")
	return "/login?next=" + url.QueryEscape(id)
}

func (a *Auth) CheckOrigin(r *http.Request) bool {
	u, _ := url.Parse(a.c.Origin)
	if site := r.Header.Get("Sec-Fetch-Site"); site == "cross-site" || site == "same-site" {
		navigation := r.Method == "GET" && shellPath(r.URL.Path) && r.Header.Get("Sec-Fetch-Mode") == "navigate" && r.Header.Get("Sec-Fetch-Dest") == "document"
		if !navigation {
			return false
		}
	}
	if r.Host != u.Host {
		return false
	}
	if r.Method != "GET" && r.Method != "HEAD" {
		return r.Header.Get("Origin") == a.c.Origin && r.Header.Get("Sec-Fetch-Site") != "cross-site"
	}
	return r.Header.Get("Origin") == "" || r.Header.Get("Origin") == a.c.Origin
}
func reply(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(v)
}
func reject(w http.ResponseWriter, code int, msg string) { http.Error(w, msg, code) }
func cookieKey(r *http.Request, name string) string {
	// Reject duplicate cookies rather than relying on proxy/browser ordering.
	cs := r.CookiesNamed(name)
	if len(cs) != 1 || len(cs[0].Value) != 43 {
		return ""
	}
	return digest(cs[0].Value)
}
func (a *Auth) getSession(r *http.Request) *session {
	if a.broken {
		return nil
	}
	s := a.sessions[cookieKey(r, sessionCookie)]
	if s != nil && s.active() {
		return s
	}
	return nil
}
func (a *Auth) prune() {
	now := time.Now()
	for k, p := range a.pending {
		if now.After(p.expires) {
			delete(a.pending, k)
		}
	}
	for k, s := range a.sessions {
		if !s.active() {
			s.revoke()
			delete(a.sessions, k)
		}
	}
}
func (a *Auth) persist(u owner) error {
	if atomicJSON(filepath.Join(authDir(a.state), "credentials.json"), u) != nil {
		a.broken = true
		a.cancelAll()
		// Durability uncertainty must not allow further login/session authority.
		for _, s := range a.sessions {
			s.revoke()
		}
		return ErrAuth
	}
	a.user = u
	return nil
}
func (a *Auth) validInvitation(hash string) bool {
	if hash == "" || hash == a.user.UsedInvitation {
		return false
	}
	b, e := config.ReadFile(filepath.Join(authDir(a.state), "invitation.json"), 4096, true)
	var v invitation
	return e == nil && config.Decode(b, &v) == nil && v.Origin == a.c.Origin && time.Now().Before(v.Expires) && time.Until(v.Expires) <= 10*time.Minute && subtle.ConstantTimeCompare([]byte(v.Digest), []byte(hash)) == 1
}
func (a *Auth) issueSession(w http.ResponseWriter, r *http.Request, credential []byte) {
	if old := a.getSession(r); old != nil {
		old.revoke()
		delete(a.sessions, cookieKey(r, sessionCookie))
	}
	if len(a.sessions) >= maxSessions {
		for k, s := range a.sessions {
			s.revoke()
			delete(a.sessions, k)
			break
		}
	}
	t := token()
	a.sessions[digest(t)] = newSession(string(credential))
	cookie(w, sessionCookie, t, sessionLifetime)
}
func (a *Auth) begin(w http.ResponseWriter, r *http.Request, kind string) {
	var in struct {
		Token string `json:"token"`
	}
	b := make([]byte, 0)
	var e error
	b, e = readBody(w, r, 1024)
	if e != nil || config.Decode(b, &in) != nil {
		reject(w, 400, "AUTH_REQUEST_INVALID")
		return
	}
	p := pending{kind: kind, expires: time.Now().Add(challengeLifetime)}
	var options any
	var data *webauthn.SessionData
	if kind == "register" {
		if len(a.user.Credentials) >= maxCredentials {
			reject(w, 409, "CREDENTIAL_LIMIT")
			return
		}
		if len(in.Token) == 43 && a.validInvitation(digest(in.Token)) {
			p.invitation = digest(in.Token)
		} else if s := a.getSession(r); s != nil && time.Since(s.verified) < freshLifetime {
			p.parent = cookieKey(r, sessionCookie)
		} else {
			reject(w, 403, "ENROLLMENT_REJECTED")
			return
		}
		excluded := make([]protocol.CredentialDescriptor, 0, len(a.user.Credentials))
		for _, c := range a.user.Credentials {
			excluded = append(excluded, c.Descriptor())
		}
		options, data, e = a.wa.BeginRegistration(a.user, webauthn.WithExclusions(excluded))
	} else {
		if len(a.user.Credentials) == 0 {
			reject(w, 403, "AUTH_REJECTED")
			return
		}
		options, data, e = a.wa.BeginDiscoverableLogin(webauthn.WithUserVerification(protocol.VerificationRequired))
	}
	if e != nil {
		reject(w, 400, "AUTH_REJECTED")
		return
	}
	if old := cookieKey(r, ceremonyCookie); old != "" {
		delete(a.pending, old)
	}
	t := token()
	p.data = *data
	// Reserve capacity for explicitly authorized enrollment. Never evict another
	// client's in-progress ceremony: shed new load when capacity is exhausted.
	limit := maxPending
	if kind == "login" {
		limit -= 16
	}
	if len(a.pending) >= limit {
		reject(w, 429, "AUTH_BUSY")
		return
	}

	a.pending[digest(t)] = p
	cookie(w, ceremonyCookie, t, challengeLifetime)
	reply(w, options)
}
func (a *Auth) finish(w http.ResponseWriter, r *http.Request, kind string) {
	k := cookieKey(r, ceremonyCookie)
	p, ok := a.pending[k]
	delete(a.pending, k)
	cookie(w, ceremonyCookie, "", -1)
	if !ok || p.kind != kind || time.Now().After(p.expires) {
		reject(w, 403, "AUTH_REJECTED")
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, 64<<10)
	var c *webauthn.Credential
	var e error
	u := a.user
	u.Credentials = append([]webauthn.Credential(nil), a.user.Credentials...)
	if kind == "register" {
		authorized := a.validInvitation(p.invitation)
		if p.parent != "" {
			s := a.sessions[p.parent]
			authorized = s != nil && s.active() && time.Since(s.verified) < freshLifetime && cookieKey(r, sessionCookie) == p.parent
		}
		if !authorized || len(u.Credentials) >= maxCredentials {
			reject(w, 403, "ENROLLMENT_REJECTED")
			return
		}
		c, e = a.wa.FinishRegistration(a.user, p.data, r)
		if e == nil {
			for _, old := range u.Credentials {
				if bytes.Equal(old.ID, c.ID) {
					e = ErrAuth
				}
			}
		}
		if e == nil {
			u.Credentials = append(u.Credentials, *c)
			if p.invitation != "" {
				u.UsedInvitation = p.invitation
			}
		}
	} else {
		c, e = a.wa.FinishDiscoverableLogin(func(rawID, userHandle []byte) (webauthn.User, error) {
			if !bytes.Equal(userHandle, a.user.ID) {
				return nil, ErrAuth
			}
			for _, known := range a.user.Credentials {
				if bytes.Equal(rawID, known.ID) {
					return a.user, nil
				}
			}
			return nil, ErrAuth
		}, p.data, r)
		if e == nil && c.Authenticator.CloneWarning {
			e = ErrAuth
		}
		if e == nil {
			for i := range u.Credentials {
				if bytes.Equal(u.Credentials[i].ID, c.ID) {
					u.Credentials[i] = *c
				}
			}
		}
	}
	if e != nil {
		reject(w, 403, "AUTH_REJECTED")
		return
	}
	if a.persist(u) != nil {
		reject(w, 503, "AUTH_STATE_UNAVAILABLE")
		return
	}
	a.issueSession(w, r, c.ID)
	reply(w, struct {
		OK bool `json:"ok"`
	}{true})
}
func (a *Auth) serve(w http.ResponseWriter, r *http.Request) {
	// Read attacker-controlled streams before taking the shared authority lock.
	limit := int64(0)
	switch r.Method + " " + r.URL.Path {
	case "POST /auth/login/begin", "POST /auth/register/begin":
		limit = 1024
	case "POST /auth/login/finish", "POST /auth/register/finish":
		limit = 64 << 10
	case "POST /auth/credentials/remove":
		limit = 256
	}
	if limit > 0 {
		select {
		case a.bodySlots <- struct{}{}:
		default:
			reject(w, 429, "AUTH_BUSY")
			return
		}
		body, e := readBody(w, r, limit)
		<-a.bodySlots
		if e != nil {
			reject(w, 400, "AUTH_REQUEST_INVALID")
			return
		}
		r = r.Clone(r.Context())
		r.Body = io.NopCloser(bytes.NewReader(body))
		r.ContentLength = int64(len(body))
	}
	buffered := &authResponse{header: make(http.Header)}
	a.mu.Lock()
	a.serveLocked(buffered, r)
	a.mu.Unlock()
	for k, v := range buffered.header {
		w.Header()[k] = v
	}
	w.WriteHeader(buffered.statusCode())
	if r.Method != "HEAD" {
		_, _ = w.Write(buffered.body.Bytes())
	}
}

type authResponse struct {
	header http.Header
	code   int
	body   bytes.Buffer
}

func (w *authResponse) Header() http.Header { return w.header }
func (w *authResponse) WriteHeader(code int) {
	if w.code == 0 {
		w.code = code
	}
}
func (w *authResponse) Write(b []byte) (int, error) {
	if w.code == 0 {
		w.code = 200
	}
	return w.body.Write(b)
}
func (w *authResponse) statusCode() int {
	if w.code == 0 {
		return 200
	}
	return w.code
}
func (a *Auth) serveLocked(w http.ResponseWriter, r *http.Request) {
	a.prune()
	if a.broken {
		reject(w, 503, "AUTH_STATE_UNAVAILABLE")
		return
	}
	switch r.Method + " " + r.URL.Path {
	case "GET /auth/status":
		s := a.getSession(r)
		count := 0
		if s != nil {
			count = len(a.user.Credentials)
		}
		reply(w, struct {
			Enabled       bool `json:"enabled"`
			Authenticated bool `json:"authenticated"`
			Credentials   int  `json:"credentials"`
		}{true, s != nil, count})
	case "POST /auth/login/begin":
		a.begin(w, r, "login")
	case "POST /auth/register/begin":
		a.begin(w, r, "register")
	case "POST /auth/login/finish":
		a.finish(w, r, "login")
	case "POST /auth/register/finish":
		a.finish(w, r, "register")
	case "POST /auth/logout", "POST /auth/logout-all":
		if r.Header.Get("X-Access-Action") != "logout" || r.ContentLength != 0 {
			reject(w, 403, "AUTH_REJECTED")
			return
		}
		if a.getSession(r) == nil {
			reject(w, 401, "LOGIN_REQUIRED")
			return
		}
		if r.URL.Path == "/auth/logout-all" {
			a.cancelAll()
			for k, s := range a.sessions {
				s.revoke()
				delete(a.sessions, k)
			}
		} else {
			k := cookieKey(r, sessionCookie)
			a.sessions[k].revoke()
			delete(a.sessions, k)
		}
		cookie(w, sessionCookie, "", -1)
		reply(w, struct {
			OK bool `json:"ok"`
		}{true})
	case "GET /auth/credentials":
		if a.getSession(r) == nil {
			reject(w, 401, "LOGIN_REQUIRED")
			return
		}
		type item struct {
			Slot   int    `json:"slot"`
			Handle string `json:"handle"`
			Synced bool   `json:"synced"`
		}
		out := []item{}
		for i, c := range a.user.Credentials {
			out = append(out, item{i + 1, digest(string(c.ID)), c.Flags.BackupEligible})
		}
		reply(w, out)
	case "POST /auth/credentials/remove":
		s := a.getSession(r)
		if s == nil {
			reject(w, 401, "LOGIN_REQUIRED")
			return
		}
		if time.Since(s.verified) >= freshLifetime {
			reject(w, 403, "REAUTH_REQUIRED")
			return
		}
		var in struct {
			Handle string `json:"handle"`
		}
		b, e := readBody(w, r, 256)
		decodeErr := config.Decode(b, &in)
		slot := -1
		for i, c := range a.user.Credentials {
			if digest(string(c.ID)) == in.Handle {
				slot = i
			}
		}
		if e != nil || decodeErr != nil || slot < 0 || len(a.user.Credentials) <= 1 {
			reject(w, 400, "CREDENTIAL_REMOVE_REJECTED")
			return
		}
		removed := string(a.user.Credentials[slot].ID)
		u := a.user
		u.Credentials = append([]webauthn.Credential(nil), a.user.Credentials...)
		u.Credentials = append(u.Credentials[:slot], u.Credentials[slot+1:]...)
		// Revoke first even when durable deletion subsequently fails.
		for k, s := range a.sessions {
			if s.credential == removed {
				s.revoke()
				delete(a.sessions, k)
			}
		}
		if a.persist(u) != nil {
			reject(w, 503, "AUTH_STATE_UNAVAILABLE")
			return
		}
		reply(w, struct {
			OK bool `json:"ok"`
		}{true})
	default:
		reject(w, 404, "NOT_FOUND")
	}
}
func (a *Auth) Wrap(next http.Handler) http.Handler {
	if a == nil {
		return next
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !a.CheckOrigin(r) {
			reject(w, 403, "ORIGIN_REJECTED")
			return
		}
		if strings.HasPrefix(r.URL.Path, "/auth/") {
			a.serve(w, r)
			return
		}
		if r.Method == "GET" && (r.URL.Path == "/login" || r.URL.Path == "/auth.js" || r.URL.Path == "/style.css") {
			next.ServeHTTP(w, r)
			return
		}
		a.mu.Lock()
		a.prune()
		s := a.getSession(r)
		a.mu.Unlock()
		if s == nil {
			if r.Method == "GET" && shellPath(r.URL.Path) {
				http.Redirect(w, r, loginTarget(r.URL.Path), http.StatusSeeOther)
			} else {
				reject(w, 401, "LOGIN_REQUIRED")
			}
			return
		}
		if r.Method != "GET" && r.Method != "HEAD" && time.Since(s.verified) >= freshLifetime {
			reject(w, 403, "REAUTH_REQUIRED")
			return
		}
		ctx, cancel := context.WithCancel(r.Context())
		defer cancel()
		stop := context.AfterFunc(s.ctx, cancel)
		defer stop()
		next.ServeHTTP(&guardedWriter{w, s}, r.WithContext(ctx))
	})
}
