package main

import (
	"context"
	"flag"
	"fmt"
	"github.com/justinnotime/skills/skills/private-web-access/internal/browserauth"
	"github.com/justinnotime/skills/skills/private-web-access/internal/gateway"
	"io"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

func run() int {
	if len(os.Args) < 2 {
		return 2
	}
	f := flag.NewFlagSet("private-web", flag.ContinueOnError)
	f.SetOutput(io.Discard)
	file := f.String("config", "", "external private configuration")
	output := f.String("output", "", "private invitation output")
	if f.Parse(os.Args[2:]) != nil || f.NArg() != 0 {
		return 2
	}
	c, e := gateway.Load(*file)
	if e != nil {
		return 1
	}
	switch os.Args[1] {
	case "check":
		fmt.Println("CONFIG_VALID")
		return 0
	case "init":
		if browserauth.Initialize(c.Authentication(), c.StateDirectory) != nil {
			return 1
		}
		fmt.Println("AUTH_INITIALIZED")
		return 0
	case "enroll":
		if browserauth.IssueInvitation(c.Authentication(), c.StateDirectory, *output) != nil {
			return 1
		}
		fmt.Println("INVITATION_WRITTEN")
		return 0
	case "serve":
	default:
		return 2
	}
	g, e := gateway.New(c)
	if e != nil {
		return 1
	}
	defer g.Auth.Close()
	listeners := []net.Listener{}
	for _, s := range g.Servers {
		l, e := net.Listen("tcp", s.Addr)
		if e != nil {
			for _, l := range listeners {
				l.Close()
			}
			return 1
		}
		listeners = append(listeners, l)
	}
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
	defer cancel()
	done := make(chan error, len(g.Servers))
	for i, s := range g.Servers {
		s.BaseContext = func(net.Listener) context.Context { return ctx }
		go func() { done <- s.Serve(listeners[i]) }()
	}
	fmt.Println("GATEWAY_READY")
	failed := false
	select {
	case <-ctx.Done():
	case e := <-done:
		failed = e != nil && e != http.ErrServerClosed
	}
	cancel()
	stopctx, stop := context.WithTimeout(context.Background(), 5*time.Second)
	defer stop()
	g.Auth.Close()
	for _, s := range g.Servers {
		if s.Shutdown(stopctx) != nil {
			s.Close()
		}
	}
	if failed {
		return 1
	}
	return 0
}
func main() {
	code := run()
	if code != 0 {
		fmt.Fprintln(os.Stderr, "GATEWAY_OPERATION_FAILED")
	}
	os.Exit(code)
}
