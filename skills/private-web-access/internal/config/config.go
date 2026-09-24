package config

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"reflect"
	"strings"
)

const MaxConfigBytes = 65536

var ErrConfig = errors.New("CONFIG_INVALID")

func Decode(data []byte, out any) error {
	if len(data) > MaxConfigBytes {
		return ErrConfig
	}
	return DecodeLimit(data, out, MaxConfigBytes)
}
func DecodeLimit(data []byte, out any, limit int) error {
	if len(data) > limit {
		return ErrConfig
	}
	d := json.NewDecoder(bytes.NewReader(data))
	d.UseNumber()
	var scan func(int, reflect.Type) error
	scan = func(depth int, typ reflect.Type) error {
		if depth > 16 {
			return ErrConfig
		}
		t, e := d.Token()
		if e != nil {
			return ErrConfig
		}
		if x, ok := t.(json.Delim); ok {
			switch x {
			case '{':
				if typ == nil || typ.Kind() != reflect.Struct {
					return ErrConfig
				}
				fields := map[string]reflect.Type{}
				for i := 0; i < typ.NumField(); i++ {
					f := typ.Field(i)
					tag := strings.Split(f.Tag.Get("json"), ",")[0]
					if tag != "" && tag != "-" {
						fields[tag] = f.Type
					}
				}
				seen := map[string]bool{}
				for d.More() {
					k, e := d.Token()
					if e != nil {
						return ErrConfig
					}
					s, ok := k.(string)
					fieldType, known := fields[s]
					if !ok || seen[s] || !known {
						return ErrConfig
					}
					seen[s] = true
					if scan(depth+1, fieldType) != nil {
						return ErrConfig
					}
				}
				t, e = d.Token()
				if e != nil || t != json.Delim('}') {
					return ErrConfig
				}
			case '[':
				if typ == nil || (typ.Kind() != reflect.Slice && typ.Kind() != reflect.Array) {
					return ErrConfig
				}
				for d.More() {
					if scan(depth+1, typ.Elem()) != nil {
						return ErrConfig
					}
				}
				t, e = d.Token()
				if e != nil || t != json.Delim(']') {
					return ErrConfig
				}
			default:
				return ErrConfig
			}
		}
		return nil
	}
	typ := reflect.TypeOf(out)
	if typ == nil || typ.Kind() != reflect.Pointer {
		return ErrConfig
	}
	if scan(0, typ.Elem()) != nil {
		return ErrConfig
	}
	if _, e := d.Token(); e != io.EOF {
		return ErrConfig
	}
	d = json.NewDecoder(bytes.NewReader(data))
	d.DisallowUnknownFields()
	if d.Decode(out) != nil {
		return ErrConfig
	}
	return nil
}
func ReadFile(path string, limit int, secret bool) ([]byte, error) {
	f, e := os.Open(path)
	if e != nil {
		return nil, ErrConfig
	}
	defer f.Close()
	info, e := f.Stat()
	link, le := os.Lstat(path)
	if e != nil || le != nil || !info.Mode().IsRegular() || !os.SameFile(info, link) || link.Mode()&os.ModeSymlink != 0 || info.Size() > int64(limit) || (secret && info.Mode().Perm()&0077 != 0) {
		return nil, ErrConfig
	}
	b, e := io.ReadAll(io.LimitReader(f, int64(limit)+1))
	if e != nil || len(b) > limit {
		return nil, ErrConfig
	}
	return b, nil
}
