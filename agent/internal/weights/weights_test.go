package weights

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
)

// cache lays out a Hugging Face hub cache the way huggingface_hub writes one: completed
// files are links in snapshots/<rev>/ to blobs/<sha>, a file being written is
// blobs/<sha>.incomplete.
func cache(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	root := filepath.Join(dir, "models--Qwen--Qwen3.5-2B")
	blobs := filepath.Join(root, "blobs")
	snapshot := filepath.Join(root, "snapshots", "abc123")
	for _, path := range []string{blobs, snapshot} {
		if err := os.MkdirAll(path, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	write := func(path string, size int) {
		if err := os.WriteFile(path, make([]byte, size), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	write(filepath.Join(blobs, "sha-one"), 3000)
	write(filepath.Join(blobs, "sha-config"), 50)
	write(filepath.Join(blobs, "sha-two.incomplete"), 1200)
	if err := os.Symlink("../../blobs/sha-one", filepath.Join(snapshot, "model-00001-of-00002.safetensors")); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("../../blobs/sha-config", filepath.Join(snapshot, "config.json")); err != nil {
		t.Fatal(err)
	}
	return dir
}

func hub(t *testing.T, status int, calls *atomic.Int32) *httptest.Server {
	t.Helper()
	return httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		if r.URL.Path != "/api/models/Qwen/Qwen3.5-2B" || r.URL.Query().Get("blobs") != "true" {
			t.Errorf("unexpected request %s", r.URL)
		}
		if r.Header.Get("Authorization") != "" {
			t.Error("the agent must not send credentials to the Hub")
		}
		w.WriteHeader(status)
		_, _ = w.Write([]byte(`{"siblings":[
			{"rfilename":"model-00001-of-00002.safetensors","size":3000},
			{"rfilename":"model-00002-of-00002.safetensors","size":2000},
			{"rfilename":"original/consolidated.safetensors","size":99999},
			{"rfilename":"pytorch_model.bin","size":7777},
			{"rfilename":"config.json","size":50}]}`))
	}))
}

func TestProgressCountsCompletedAndPartialWeightsAgainstTheHub(t *testing.T) {
	var calls atomic.Int32
	server := hub(t, http.StatusOK, &calls)
	defer server.Close()
	measure := &Cache{
		Dir: cache(t), Node: "gpu-01", Endpoint: server.URL,
		Repository: func(string) string { return "Qwen/Qwen3.5-2B" },
	}
	progress := measure.Measure(context.Background(), "dep", "gpu-01")
	if progress == nil {
		t.Fatal("no measurement")
	}
	// 3000 complete + 1200 being written, of 5000 in top-level safetensors.
	if progress.DownloadedBytes != 4200 || progress.TotalBytes != 5000 || progress.InProgressFiles != 1 {
		t.Fatalf("progress = %+v", progress)
	}
	measure.Measure(context.Background(), "dep", "gpu-01")
	if calls.Load() != 1 {
		t.Errorf("the Hub was asked %d times; a known size is remembered", calls.Load())
	}
}

func TestAGatedRepositoryReportsBytesWithoutATotal(t *testing.T) {
	var calls atomic.Int32
	server := hub(t, http.StatusUnauthorized, &calls)
	defer server.Close()
	measure := &Cache{
		Dir: cache(t), Node: "gpu-01", Endpoint: server.URL,
		Repository: func(string) string { return "Qwen/Qwen3.5-2B" },
	}
	progress := measure.Measure(context.Background(), "dep", "gpu-01")
	if progress == nil || progress.TotalBytes != 0 || progress.DownloadedBytes != 4200 {
		t.Fatalf("progress = %+v", progress)
	}
	measure.Measure(context.Background(), "dep", "gpu-01")
	if calls.Load() != 1 {
		t.Errorf("a refusal is retried only after a while, not every report")
	}
}

func TestNoMeasurementWhenTheCacheIsNotVisible(t *testing.T) {
	dir := cache(t)
	repo := func(string) string { return "Qwen/Qwen3.5-2B" }
	for name, measure := range map[string]*Cache{
		"another node":     {Dir: dir, Node: "gpu-01", Repository: repo},
		"no cache mounted": {Dir: filepath.Join(dir, "absent"), Node: "gpu-02", Repository: repo},
		"a local path":     {Dir: dir, Node: "gpu-02", Repository: func(string) string { return "/models/llama" }},
		"unknown model":    {Dir: dir, Node: "gpu-02", Repository: func(string) string { return "" }},
	} {
		if progress := measure.Measure(context.Background(), "dep", "gpu-02"); progress != nil {
			t.Errorf("%s: progress = %+v", name, progress)
		}
	}
	shared := &Cache{Dir: dir, Shared: true, Repository: repo, Endpoint: "http://127.0.0.1:1"}
	if shared.Measure(context.Background(), "dep", "elsewhere") == nil {
		t.Error("a shared cache is measured for every node")
	}
}
