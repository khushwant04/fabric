package state

import (
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"
)

func TestTheCollectorFollowsARotatedCredentialFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), TelemetryCredentialFile)
	if err := WriteTelemetryCredential(path, "fab_telem_old"); err != nil {
		t.Fatalf("write: %v", err)
	}
	source := NewTelemetryCredentialSource(path, "fab_telem_old")
	if got := source.Get(); got != "fab_telem_old" {
		t.Fatalf("initial = %q", got)
	}

	if err := WriteTelemetryCredential(path, "fab_telem_new"); err != nil {
		t.Fatalf("rewrite: %v", err)
	}
	// Coarse filesystem timestamps could otherwise hide a rewrite within one tick.
	later := time.Now().Add(time.Second)
	if err := os.Chtimes(path, later, later); err != nil {
		t.Fatalf("chtimes: %v", err)
	}
	if got := source.Get(); got != "fab_telem_new" {
		t.Fatalf("after rotation = %q", got)
	}
}

func TestAMissingOrEmptyFileKeepsTheLastCredential(t *testing.T) {
	path := filepath.Join(t.TempDir(), TelemetryCredentialFile)
	if err := WriteTelemetryCredential(path, "fab_telem_kept"); err != nil {
		t.Fatalf("write: %v", err)
	}
	source := NewTelemetryCredentialSource(path, "fab_telem_kept")

	if err := os.WriteFile(path, []byte("\n"), 0o600); err != nil {
		t.Fatalf("empty: %v", err)
	}
	later := time.Now().Add(time.Second)
	_ = os.Chtimes(path, later, later)
	if got := source.Get(); got != "fab_telem_kept" {
		t.Fatalf("empty file replaced the credential: %q", got)
	}
	if err := os.Remove(path); err != nil {
		t.Fatalf("remove: %v", err)
	}
	if got := source.Get(); got != "fab_telem_kept" {
		t.Fatalf("missing file replaced the credential: %q", got)
	}
}

func TestTheCredentialSourceIsSafeForConcurrentUse(t *testing.T) {
	path := filepath.Join(t.TempDir(), TelemetryCredentialFile)
	if err := WriteTelemetryCredential(path, "fab_telem_a"); err != nil {
		t.Fatalf("write: %v", err)
	}
	source := NewTelemetryCredentialSource(path, "fab_telem_a")
	var group sync.WaitGroup
	for i := 0; i < 8; i++ {
		group.Add(1)
		go func() {
			defer group.Done()
			for j := 0; j < 50; j++ {
				if got := source.Get(); got == "" {
					t.Error("empty credential")
				}
			}
		}()
	}
	_ = WriteTelemetryCredential(path, "fab_telem_b")
	group.Wait()
}
