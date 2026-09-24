package browserauth

import (
	"context"
	"net/http"
	"net/url"
	"strings"
)

// ProtectApplication authorizes a read-only, separate-origin listener. Every
// port on the hostname must terminate at this trusted gate: cookies do not have
// port scope. The downstream handler must strip all browser credentials.
// No WebAuthn, session issuance, or account management is served on this origin.
func (a *Auth) ProtectApplication(origin, name string, next http.Handler) http.Handler {
	u, err := url.Parse(origin)
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if a == nil || err != nil || u == nil || r.Host != u.Host {
			reject(w, 403, "ORIGIN_REJECTED")
			return
		}
		if r.Method != "GET" && r.Method != "HEAD" || r.ContentLength != 0 || len(r.TransferEncoding) != 0 || r.Header.Get("Upgrade") != "" {
			w.Header().Set("Allow", "GET, HEAD")
			reject(w, 405, "READ_ONLY_APPLICATION")
			return
		}
		navigation := r.Method == "GET" && r.Header.Get("Sec-Fetch-Mode") == "navigate" && r.Header.Get("Sec-Fetch-Dest") == "document"
		site := r.Header.Get("Sec-Fetch-Site")
		if o := r.Header.Get("Origin"); o != "" && o != origin || (site == "cross-site" || site == "same-site") && !navigation {
			reject(w, 403, "ORIGIN_REJECTED")
			return
		}
		if strings.HasPrefix(r.URL.Path, "/auth/") || r.URL.Path == "/login" {
			reject(w, 404, "NOT_FOUND")
			return
		}
		a.mu.Lock()
		a.prune()
		s := a.getSession(r)
		a.mu.Unlock()
		if s == nil {
			if navigation || r.Method == "GET" && r.URL.Path == "/" {
				http.Redirect(w, r, a.c.Origin+"/login?next="+url.QueryEscape(name), http.StatusSeeOther)
			} else {
				reject(w, 401, "LOGIN_REQUIRED")
			}
			return
		}
		ctx, cancel := context.WithCancel(r.Context())
		defer cancel()
		stop := context.AfterFunc(s.ctx, cancel)
		defer stop()
		next.ServeHTTP(&guardedWriter{w, s}, r.WithContext(ctx))
	})
}
