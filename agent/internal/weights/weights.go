// Package weights measures how far a model host's weight download has got.
//
// A first deployment spends minutes downloading weights, and vLLM logs nothing about it:
// the Hugging Face progress bars are carriage-return redraws that a container log keeps only
// in fragments. So instead of parsing the log, the agent looks at the result. The model host
// downloads into the node's Hugging Face cache (managedModelHost.cache, HF_HUB_CACHE), and
// the chart mounts that directory read-only into the agent. Completed files appear under
// snapshots/<revision>/ as links into blobs/, and a file being written is
// blobs/<sha>.incomplete. Summing those against the weight sizes the Hugging Face API lists
// for the repository gives a byte-accurate progress figure that does not depend on any log
// format, vLLM version or downloader backend.
//
// It degrades rather than guesses. A pod on a node whose cache this agent cannot see (a
// multi-node stamp with a hostPath cache), a repository that is not a Hugging Face id, or an
// API that refuses to list a gated repository yields "unknown total" or no measurement at
// all, and the console says so instead of showing a made-up percentage.
package weights

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/khushwant04/fabric/agent/internal/hostreport"
)

// DefaultEndpoint is the public Hugging Face Hub.
const DefaultEndpoint = "https://huggingface.co"

// negativeTTL is how long a failed size lookup is remembered before it is tried again.
const negativeTTL = 10 * time.Minute

var repoPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$`)

// Cache measures downloads in a Hugging Face hub cache directory.
type Cache struct {
	// Dir is the hub cache as mounted in the agent (the model host's HF_HUB_CACHE).
	Dir string
	// Node is the node this agent runs on. A pod elsewhere is measured only when Shared.
	Node string
	// Shared means every node writes to the same cache (a ReadWriteMany claim).
	Shared bool
	// Repository maps a deployment to the model it serves; "" when unknown.
	Repository func(deploymentID string) string
	// Endpoint is the Hugging Face API base; HTTP is the client used for it.
	Endpoint string
	HTTP     *http.Client
	Now      func() time.Time

	mu     sync.Mutex
	totals map[string]total
}

type total struct {
	bytes     int64
	checkedAt time.Time
	ok        bool
}

func (c *Cache) now() time.Time {
	if c.Now != nil {
		return c.Now()
	}
	return time.Now()
}

// Measure implements hostreport.WeightSource.
func (c *Cache) Measure(ctx context.Context, deploymentID, node string) *hostreport.WeightProgress {
	if c.Dir == "" || c.Repository == nil {
		return nil
	}
	if !c.Shared && (node == "" || c.Node == "" || node != c.Node) {
		return nil
	}
	repository := c.Repository(deploymentID)
	if !repoPattern.MatchString(repository) {
		return nil
	}
	if info, err := os.Stat(c.Dir); err != nil || !info.IsDir() {
		return nil
	}
	downloaded, inProgress := c.downloaded(repository)
	progress := &hostreport.WeightProgress{
		Repository:      repository,
		DownloadedBytes: downloaded,
		TotalBytes:      c.expected(ctx, repository),
		InProgressFiles: inProgress,
		MeasuredAt:      c.now().UTC(),
	}
	if progress.TotalBytes > 0 && progress.DownloadedBytes > progress.TotalBytes {
		// The cache can hold more than the weights the server loads (an older revision,
		// extra files); never report more than all of it.
		progress.DownloadedBytes = progress.TotalBytes
	}
	return progress
}

// downloaded sums completed weight files in every snapshot plus files being written.
func (c *Cache) downloaded(repository string) (int64, int) {
	root := filepath.Join(c.Dir, "models--"+strings.ReplaceAll(repository, "/", "--"))
	var bytes int64
	counted := map[string]bool{}
	snapshots, _ := os.ReadDir(filepath.Join(root, "snapshots"))
	for _, snapshot := range snapshots {
		dir := filepath.Join(root, "snapshots", snapshot.Name())
		entries, _ := os.ReadDir(dir)
		for _, entry := range entries {
			if !isWeightFile(entry.Name()) {
				continue
			}
			target := filepath.Join(dir, entry.Name())
			// Links point into blobs/; resolving them keeps one blob shared by two
			// revisions from being counted twice.
			if resolved, err := filepath.EvalSymlinks(target); err == nil {
				target = resolved
			}
			if counted[target] {
				continue
			}
			if info, err := os.Stat(target); err == nil && info.Mode().IsRegular() {
				counted[target] = true
				bytes += info.Size()
			}
		}
	}
	inProgress := 0
	_ = filepath.WalkDir(filepath.Join(root, "blobs"), func(path string, entry fs.DirEntry, err error) error {
		if err != nil || entry.IsDir() || !strings.HasSuffix(entry.Name(), ".incomplete") {
			return nil
		}
		if info, statErr := entry.Info(); statErr == nil {
			inProgress++
			bytes += info.Size()
		}
		return nil
	})
	return bytes, inProgress
}

func isWeightFile(name string) bool {
	return strings.HasSuffix(name, ".safetensors") || strings.HasSuffix(name, ".bin") ||
		strings.HasSuffix(name, ".pt")
}

// expected returns the size of the weights the server will load, 0 when unknown.
func (c *Cache) expected(ctx context.Context, repository string) int64 {
	c.mu.Lock()
	if c.totals == nil {
		c.totals = map[string]total{}
	}
	known, seen := c.totals[repository]
	c.mu.Unlock()
	if seen && (known.ok || c.now().Sub(known.checkedAt) < negativeTTL) {
		return known.bytes
	}
	bytes, err := c.lookup(ctx, repository)
	c.mu.Lock()
	c.totals[repository] = total{bytes: bytes, checkedAt: c.now(), ok: err == nil && bytes > 0}
	c.mu.Unlock()
	return bytes
}

type modelInfo struct {
	Siblings []struct {
		Name string `json:"rfilename"`
		Size int64  `json:"size"`
	} `json:"siblings"`
}

// lookup asks the Hub for the repository's file sizes. vLLM loads the top-level
// safetensors when there are any and the PyTorch .bin files otherwise, so that is the set
// summed. Files in subdirectories (original checkpoints, GGUF variants) are not loaded.
func (c *Cache) lookup(ctx context.Context, repository string) (int64, error) {
	endpoint := strings.TrimRight(c.Endpoint, "/")
	if endpoint == "" {
		endpoint = DefaultEndpoint
	}
	client := c.HTTP
	if client == nil {
		client = &http.Client{Timeout: 10 * time.Second}
	}
	parts := strings.SplitN(repository, "/", 2)
	address := endpoint + "/api/models/" + url.PathEscape(parts[0]) + "/" +
		url.PathEscape(parts[1]) + "?blobs=true"
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, address, nil)
	if err != nil {
		return 0, err
	}
	request.Header.Set("Accept", "application/json")
	response, err := client.Do(request)
	if err != nil {
		return 0, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		// Gated and private repositories answer 401 without a token, which the agent
		// deliberately does not hold.
		return 0, fmt.Errorf("hugging face api %d", response.StatusCode)
	}
	var info modelInfo
	if err := json.NewDecoder(io.LimitReader(response.Body, 4<<20)).Decode(&info); err != nil {
		return 0, err
	}
	var safetensors, bins int64
	for _, sibling := range info.Siblings {
		if strings.Contains(sibling.Name, "/") {
			continue
		}
		switch {
		case strings.HasSuffix(sibling.Name, ".safetensors"):
			safetensors += sibling.Size
		case strings.HasSuffix(sibling.Name, ".bin") && !strings.HasPrefix(sibling.Name, "training_args"):
			bins += sibling.Size
		}
	}
	if safetensors > 0 {
		return safetensors, nil
	}
	return bins, nil
}
