package portal

import (
	"bytes"
	"embed"
	"html/template"
	"net/http"
	"strconv"
)

//go:embed web/*
var assets embed.FS

type Entry struct{ ID, Title, Description string }

func Handler(entries []Entry) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "GET" && r.Method != "HEAD" {
			http.Error(w, "METHOD_NOT_ALLOWED", 405)
			return
		}
		file, mime := "", ""
		switch r.URL.Path {
		case "/":
			file, mime = "index.html", "text/html; charset=utf-8"
		case "/login":
			file, mime = "login.html", "text/html; charset=utf-8"
		case "/auth.js":
			file, mime = "auth.js", "text/javascript; charset=utf-8"
		case "/portal.js":
			file, mime = "app.js", "text/javascript; charset=utf-8"
		case "/style.css", "/portal.css":
			file, mime = "style.css", "text/css; charset=utf-8"
		default:
			http.NotFound(w, r)
			return
		}
		b, e := assets.ReadFile("web/" + file)
		if e != nil {
			http.Error(w, "ASSET_UNAVAILABLE", 500)
			return
		}
		if file == "index.html" {
			var out bytes.Buffer
			t, e := template.New("portal").Parse(string(b))
			if e != nil || t.Execute(&out, entries) != nil {
				http.Error(w, "ASSET_UNAVAILABLE", 500)
				return
			}
			b = out.Bytes()
		}
		w.Header().Set("Content-Type", mime)
		w.Header().Set("Content-Length", strconv.Itoa(len(b)))
		if r.Method != "HEAD" {
			w.Write(b)
		}
	})
}
