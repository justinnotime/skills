package gateway

import (
	"errors"
	"github.com/justinnotime/skills/skills/private-web-access/internal/browserauth"
	"github.com/justinnotime/skills/skills/private-web-access/internal/config"
	"net"
	"net/http"
	"net/url"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
)

var ErrConfig = errors.New("GATEWAY_CONFIG_INVALID")
var identifier = regexp.MustCompile(`^[a-z][a-z0-9-]{0,31}$`)

type Application struct {
	ID             string   `json:"id"`
	Title          string   `json:"title"`
	Description    string   `json:"description"`
	Mode           string   `json:"mode"`
	Content        string   `json:"content"`
	Origin         string   `json:"origin,omitempty"`
	Listen         string   `json:"listen,omitempty"`
	Upstream       string   `json:"upstream"`
	CredentialFile string   `json:"credential_file"`
	Methods        []string `json:"methods"`
	ForwardHeaders []string `json:"forward_headers,omitempty"`
}
type Config struct {
	Schema         string        `json:"schema"`
	Origin         string        `json:"origin"`
	Listen         string        `json:"listen"`
	StateDirectory string        `json:"state_directory"`
	Applications   []Application `json:"applications"`
}

func Loopback(address string) bool {
	h, p, e := net.SplitHostPort(address)
	n, err := strconv.Atoi(p)
	ip := net.ParseIP(h)
	return e == nil && err == nil && strconv.Itoa(n) == p && n > 0 && n <= 65535 && ip != nil && ip.IsLoopback()
}
func (c Config) Authentication() browserauth.Config {
	return browserauth.Config{Mode: browserauth.Mode, Origin: c.Origin}
}
func (c Config) Validate() error {
	if c.Schema != "private-web-access/v1" || c.Authentication().Validate() != nil || !Loopback(c.Listen) || !filepath.IsAbs(c.StateDirectory) || filepath.Clean(c.StateDirectory) != c.StateDirectory || len(c.Applications) > 32 {
		return ErrConfig
	}
	primary, _ := url.Parse(c.Origin)
	ids := map[string]bool{"auth": true, "login": true, "apps": true}
	origins := map[string]bool{primary.Host: true}
	listens := map[string]bool{c.Listen: true}
	for _, a := range c.Applications {
		up, e := url.Parse(a.Upstream)
		if !identifier.MatchString(a.ID) || ids[a.ID] || len(a.Title) < 1 || len(a.Title) > 100 || len(a.Description) > 500 || strings.ContainsAny(a.Title+a.Description, "\r\n\x00") || e != nil || up.Scheme != "http" || !Loopback(up.Host) || up.User != nil || up.Path != "" || up.RawQuery != "" || up.ForceQuery || up.Fragment != "" || up.Opaque != "" || !filepath.IsAbs(a.CredentialFile) || filepath.Clean(a.CredentialFile) != a.CredentialFile {
			return ErrConfig
		}
		ids[a.ID] = true
		if a.Mode == "isolated-readonly" {
			u, e := url.Parse(a.Origin)
			if e != nil || (browserauth.Config{Mode: browserauth.Mode, Origin: a.Origin}).Validate() != nil || u.Hostname() != primary.Hostname() || u.Port() == "" || u.Port() == "443" || origins[u.Host] || !Loopback(a.Listen) || listens[a.Listen] {
				return ErrConfig
			}
			port, e := strconv.Atoi(u.Port())
			if e != nil || port < 1 || port > 65535 || strconv.Itoa(port) != u.Port() {
				return ErrConfig
			}
			origins[u.Host], listens[a.Listen] = true, true
		} else if a.Mode != "trusted-prefix" || a.Origin != "" || a.Listen != "" || a.Content != "application" {
			return ErrConfig
		}
		if a.Content != "application" && a.Content != "published" {
			return ErrConfig
		}
		methods := map[string]bool{}
		for _, m := range a.Methods {
			if methods[m] || m != "GET" && m != "HEAD" && (a.Mode != "trusted-prefix" || m != "POST" && m != "PUT" && m != "PATCH" && m != "DELETE") {
				return ErrConfig
			}
			methods[m] = true
		}
		if !methods["GET"] || !methods["HEAD"] {
			return ErrConfig
		}
		for _, h := range a.ForwardHeaders {
			h = http.CanonicalHeaderKey(h)
			if !regexp.MustCompile(`^X-[A-Za-z0-9-]{1,64}$`).MatchString(h) || strings.HasPrefix(h, "X-Forwarded-") || strings.HasPrefix(h, "X-Access-") || strings.HasPrefix(h, "X-Tailscale-") || h == "X-Real-Ip" {
				return ErrConfig
			}
		}
	}
	for _, a := range c.Applications {
		up, _ := url.Parse(a.Upstream)
		if listens[up.Host] {
			return ErrConfig
		}
	}
	return nil
}
func Load(file string) (Config, error) {
	var c Config
	b, e := config.ReadFile(file, config.MaxConfigBytes, true)
	if e != nil || config.Decode(b, &c) != nil || c.Validate() != nil {
		return Config{}, ErrConfig
	}
	return c, nil
}
