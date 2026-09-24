package gateway

import (
	"github.com/justinnotime/skills/skills/private-web-access/internal/browserauth"
	"github.com/justinnotime/skills/skills/private-web-access/internal/portal"
	"io"
	"log"
	"net/http"
	"time"
)

type Gateway struct {
	Auth    *browserauth.Auth
	Servers []*http.Server
}

func server(listen string, h http.Handler) *http.Server {
	return &http.Server{Addr: listen, Handler: h, ReadHeaderTimeout: 5 * time.Second, ReadTimeout: 15 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 16 << 10, ErrorLog: log.New(io.Discard, "", 0)}
}
func New(c Config) (*Gateway, error) {
	if c.Validate() != nil {
		return nil, ErrConfig
	}
	auth, e := browserauth.New(c.Authentication(), c.StateDirectory)
	if e != nil {
		return nil, e
	}
	g := &Gateway{Auth: auth}
	ok := false
	defer func() {
		if !ok {
			auth.Close()
		}
	}()
	mux := http.NewServeMux()
	entries := []portal.Entry{}
	for _, a := range c.Applications {
		proxy, e := Proxy(a, c.Origin)
		if e != nil {
			return nil, e
		}
		target := a.Origin + "/"
		if a.Mode == "trusted-prefix" {
			target = "/" + a.ID + "/"
			mux.Handle(target, proxy)
		} else {
			h := auth.ProtectApplication(a.Origin, a.ID, proxy)
			g.Servers = append(g.Servers, server(a.Listen, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				security(w.Header(), a.Content == "published")
				h.ServeHTTP(w, r)
			})))
		}
		mux.HandleFunc("GET /apps/"+a.ID+"/{$}", func(w http.ResponseWriter, r *http.Request) { http.Redirect(w, r, target, http.StatusSeeOther) })
		entries = append(entries, portal.Entry{ID: a.ID, Title: a.Title, Description: a.Description})
	}
	mux.Handle("/", portal.Handler(entries))
	protected := auth.Wrap(mux)
	primary := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		security(w.Header(), false)
		w.Header().Set("Permissions-Policy", "publickey-credentials-get=(self), publickey-credentials-create=(self), document-domain=()")
		protected.ServeHTTP(w, r)
	})
	g.Servers = append([]*http.Server{server(c.Listen, primary)}, g.Servers...)
	ok = true
	return g, nil
}
