package browserauth

import (
	"os"
	"path/filepath"
	"syscall"
)

// One process owns the credential state for its whole lifetime. This lock must
// precede reading credentials, otherwise a second writer could cache old grants.
func lockState(state string) (*os.File, error) {
	if privateDir(state) != nil || privateDir(authDir(state)) != nil {
		return nil, ErrAuth
	}
	f, err := os.OpenFile(filepath.Join(authDir(state), "server.lock"), os.O_CREATE|os.O_RDWR|syscall.O_NOFOLLOW, 0600)
	if err != nil {
		return nil, ErrAuth
	}
	info, err := f.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Mode().Perm() != 0600 || syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB) != nil {
		f.Close()
		return nil, ErrAuth
	}
	return f, nil
}
func releaseState(f *os.File) { _ = syscall.Flock(int(f.Fd()), syscall.LOCK_UN); _ = f.Close() }
