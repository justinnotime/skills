package gateway

import (
	"io"
	"net/http"
)

var responseFields = []string{"Content-Type", "Content-Length", "Content-Encoding", "Content-Disposition", "Content-Range", "Accept-Ranges", "ETag", "Last-Modified", "Location", "Allow"}

// Keep upstream interim and late headers in a separate map. Only the final
// response allowlist ever touches the actual network writer.
type responseBoundary struct {
	http.ResponseWriter
	header             http.Header
	wrote              bool
	published, trusted bool
}

func (w *responseBoundary) Header() http.Header         { return w.header }
func (w *responseBoundary) Unwrap() http.ResponseWriter { return w.ResponseWriter }
func (w *responseBoundary) WriteHeader(code int) {
	if code < 200 || w.wrote {
		return
	}
	w.wrote = true
	dst := w.ResponseWriter.Header()
	clear(dst)
	for _, k := range responseFields {
		for _, v := range w.header.Values(k) {
			dst.Add(k, v)
		}
	}
	security(dst, w.published)
	for _, policy := range w.header.Values("Content-Security-Policy") {
		dst.Add("Content-Security-Policy", policy)
	}
	if w.trusted {
		dst.Set("Permissions-Policy", "publickey-credentials-get=(self), publickey-credentials-create=(self), document-domain=()")
	}
	w.ResponseWriter.WriteHeader(code)
}
func (w *responseBoundary) Write(b []byte) (int, error) {
	if !w.wrote {
		w.WriteHeader(200)
	}
	return w.ResponseWriter.Write(b)
}
func (w *responseBoundary) FlushError() error {
	if !w.wrote {
		w.WriteHeader(200)
	}
	return http.NewResponseController(w.ResponseWriter).Flush()
}

type withoutTrailers struct {
	io.ReadCloser
	response *http.Response
}

func (b *withoutTrailers) Read(p []byte) (int, error) {
	n, e := b.ReadCloser.Read(p)
	b.response.Trailer = nil
	return n, e
}
func (b *withoutTrailers) Close() error {
	e := b.ReadCloser.Close()
	b.response.Trailer = nil
	return e
}
