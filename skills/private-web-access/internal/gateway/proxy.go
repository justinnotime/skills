package gateway

import (
	"bytes"
	"encoding/base64"
	"errors"
	"github.com/justinnotime/skills/skills/private-web-access/internal/config"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"path"
	"strings"
	"time"
)

func security(h http.Header, published bool) {
	scripts := "'self'"
	if published {
		scripts += " 'unsafe-inline'"
	}
	h.Set("Content-Security-Policy", "default-src 'self'; script-src "+scripts+"; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; media-src 'self' blob:; font-src 'self' data:; worker-src 'none'; object-src 'none'; frame-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
	h.Set("Cache-Control", "no-store")
	h.Set("Origin-Agent-Cluster", "?1")
	h.Set("Cross-Origin-Resource-Policy", "same-origin")
	h.Set("Cross-Origin-Opener-Policy", "same-origin")
	h.Set("Referrer-Policy", "no-referrer")
	h.Set("X-Content-Type-Options", "nosniff")
	h.Set("X-Frame-Options", "DENY")
	h.Set("Permissions-Policy", "publickey-credentials-get=(), publickey-credentials-create=(), document-domain=(), camera=(), microphone=(), geolocation=()")
}
func canonical(r *http.Request) bool {
	p := r.URL.Path
	if strings.Contains(p, "\\") || strings.ContainsAny(p, "\x00\r\n") || strings.Contains(p, "//") {
		return false
	}
	clean := path.Clean(p)
	if strings.HasSuffix(p, "/") && p != "/" {
		clean += "/"
	}
	if clean != p {
		return false
	}
	if r.Method != "GET" && r.Method != "HEAD" && r.URL.RawPath != "" {
		return false
	}
	return true
}
func Proxy(a Application, primary string) (http.Handler, error) {
	var secret struct {
		Authorization string `json:"authorization"`
	}
	b, e := config.ReadFile(a.CredentialFile, 8192, true)
	if e != nil || config.Decode(b, &secret) != nil || strings.ContainsAny(secret.Authorization, "\r\n\x00") {
		return nil, ErrConfig
	}
	clear(b)
	scheme, value, ok := strings.Cut(secret.Authorization, " ")
	if !ok || value == "" {
		return nil, ErrConfig
	}
	if scheme == "Basic" {
		decoded, e := base64.StdEncoding.DecodeString(value)
		if e != nil || !bytes.ContainsRune(decoded, ':') {
			return nil, ErrConfig
		}
		clear(decoded)
	} else if scheme != "Bearer" || len(value) < 32 || len(value) > 4096 || strings.ContainsAny(value, " \t") {
		return nil, ErrConfig
	}
	up, _ := url.Parse(a.Upstream)
	origin := a.Origin
	prefix := ""
	if a.Mode == "trusted-prefix" {
		origin = primary
		prefix = "/" + a.ID
	}
	public, _ := url.Parse(origin)
	proxy := &httputil.ReverseProxy{
		Rewrite: func(p *httputil.ProxyRequest) {
			p.SetURL(up)
			p.Out.Host = up.Host
			p.Out.Header = make(http.Header)
			for _, k := range append([]string{"Accept", "Accept-Language", "Content-Type", "Range", "If-Range", "If-None-Match", "If-Modified-Since"}, a.ForwardHeaders...) {
				for _, v := range p.In.Header.Values(k) {
					p.Out.Header.Add(k, v)
				}
			}
			p.Out.Header.Set("Authorization", secret.Authorization)
			p.Out.Header.Set("User-Agent", "Private-Web-Access")
		},
		Transport: &http.Transport{Proxy: nil, DialContext: (&net.Dialer{Timeout: 5 * time.Second}).DialContext, ResponseHeaderTimeout: 15 * time.Second, IdleConnTimeout: 30 * time.Second, MaxIdleConnsPerHost: 8, DisableCompression: true},
		ErrorLog:  log.New(io.Discard, "", 0), ErrorHandler: func(w http.ResponseWriter, r *http.Request, e error) { http.Error(w, "APPLICATION_UNAVAILABLE", 502) },
		ModifyResponse: func(r *http.Response) error {
			h := make(http.Header)
			for _, k := range []string{"Content-Type", "Content-Length", "Content-Encoding", "Content-Disposition", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified"} {
				for _, v := range r.Header.Values(k) {
					h.Add(k, v)
				}
			}
			if raw := r.Header.Get("Location"); raw != "" {
				u, e := url.Parse(raw)
				if e != nil || u.User != nil || u.Opaque != "" {
					return errors.New("REDIRECT_REJECTED")
				}
				u = up.ResolveReference(u)
				if u.Scheme != up.Scheme || u.Host != up.Host {
					return errors.New("REDIRECT_REJECTED")
				}
				u.Scheme, u.Host = public.Scheme, public.Host
				u.Path = prefix + u.Path
				u.RawPath = ""
				h.Set("Location", u.String())
			}
			security(h, a.Content == "published")
			r.Header = h
			// HTTP trailers are not an alternate response-header authority channel.
			r.Trailer = nil
			r.Body = &withoutTrailers{ReadCloser: r.Body, response: r}
			return nil
		},
	}
	allowed := map[string]bool{}
	for _, m := range a.Methods {
		allowed[m] = true
	}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !allowed[r.Method] || r.Header.Get("Upgrade") != "" {
			w.Header().Set("Allow", strings.Join(a.Methods, ", "))
			http.Error(w, "METHOD_NOT_ALLOWED", 405)
			return
		}
		if !canonical(r) {
			http.Error(w, "PATH_REJECTED", 400)
			return
		}
		if r.Method == "GET" || r.Method == "HEAD" {
			if r.ContentLength != 0 || len(r.TransferEncoding) != 0 {
				http.Error(w, "BODY_REJECTED", 400)
				return
			}
		} else {
			body, e := io.ReadAll(http.MaxBytesReader(w, r.Body, 1<<20))
			if e != nil {
				http.Error(w, "BODY_REJECTED", 413)
				return
			}
			r = r.Clone(r.Context())
			r.Body = io.NopCloser(bytes.NewReader(body))
			r.ContentLength = int64(len(body))
			r.TransferEncoding = nil
		}
		if prefix != "" {
			r = r.Clone(r.Context())
			r.URL.Path = strings.TrimPrefix(r.URL.Path, prefix)
			r.URL.RawPath = ""
		}
		proxy.ServeHTTP(&responseBoundary{ResponseWriter: w, header: w.Header().Clone(), published: a.Content == "published", trusted: a.Mode == "trusted-prefix"}, r)
	}), nil
}
