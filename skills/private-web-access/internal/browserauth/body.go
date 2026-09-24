package browserauth

import (
	"io"
	"net/http"
	"strings"
)

func readBody(w http.ResponseWriter, r *http.Request, limit int64) ([]byte, error) {
	if strings.Split(r.Header.Get("Content-Type"), ";")[0] != "application/json" {
		return nil, ErrAuth
	}
	b, e := io.ReadAll(http.MaxBytesReader(w, r.Body, limit))
	if e != nil {
		return nil, ErrAuth
	}
	return b, nil
}
