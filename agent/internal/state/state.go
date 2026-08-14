// Package state persists what the agent must not lose across restarts, and
// renders the local configuration the data plane reads.
//
// Two files, with different audiences and permissions:
//
//	credentials.json  the stamp id and machine credentials. 0600, agent only.
//	                  In Kubernetes this is a Secret mounted for the agent alone.
//	deployments.json  the deployments assigned here and who owns each one.
//	                  Readable by the data plane; contains no secret.
//
// Losing credentials.json would force a re-enrollment, and enrollment tokens are
// single use, so a stamp that loses it needs a new token from an operator.
package state

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"sort"
)

// Credentials is the agent's durable identity.
type Credentials struct {
	StampID string `json:"stamp_id"`
	// AccountID owns the stamp itself. For a managed stamp this is the Fabric
	// system account, which is not the account that owns the deployments served.
	AccountID           string `json:"account_id"`
	Mode                string `json:"mode"`
	AgentCredential     string `json:"agent_credential"`
	TelemetryCredential string `json:"telemetry_credential"`
	// AckedGeneration is the highest desired generation already applied, so a
	// restart resumes instead of replaying every assignment.
	AckedGeneration int `json:"acked_generation"`
}

// Deployment is one entry of the data plane's local configuration.
type Deployment struct {
	DeploymentID  string `json:"deployment_id"`
	AccountID     string `json:"account_id"`
	ModelAlias    string `json:"model_alias"`
	UpstreamURL   string `json:"upstream_url"`
	UpstreamModel string `json:"upstream_model,omitempty"`
}

// DeploymentsFile is the document the data plane loads.
type DeploymentsFile struct {
	Deployments []Deployment `json:"deployments"`
}

// writeAtomic replaces a file in one step so a reader never sees a partial
// document. The data plane may load deployments.json at any moment.
func writeAtomic(path string, data []byte, perm os.FileMode) error {
	directory := filepath.Dir(path)
	if err := os.MkdirAll(directory, 0o755); err != nil {
		return fmt.Errorf("create %s: %w", directory, err)
	}
	temporary, err := os.CreateTemp(directory, ".tmp-*")
	if err != nil {
		return fmt.Errorf("create temp file: %w", err)
	}
	name := temporary.Name()
	defer os.Remove(name)

	if _, err := temporary.Write(data); err != nil {
		temporary.Close()
		return fmt.Errorf("write temp file: %w", err)
	}
	if err := temporary.Chmod(perm); err != nil {
		temporary.Close()
		return fmt.Errorf("chmod temp file: %w", err)
	}
	if err := temporary.Close(); err != nil {
		return fmt.Errorf("close temp file: %w", err)
	}
	if err := os.Rename(name, path); err != nil {
		return fmt.Errorf("replace %s: %w", path, err)
	}
	return nil
}

// LoadCredentials reads persisted credentials. A missing file is not an error:
// it means this stamp has not enrolled yet.
func LoadCredentials(path string) (*Credentials, error) {
	payload, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	credentials := &Credentials{}
	if err := json.Unmarshal(payload, credentials); err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}
	if credentials.StampID == "" || credentials.AgentCredential == "" {
		return nil, fmt.Errorf("%s is missing a stamp id or agent credential", path)
	}
	return credentials, nil
}

// SaveCredentials persists credentials with owner-only permissions.
func SaveCredentials(path string, credentials *Credentials) error {
	payload, err := json.MarshalIndent(credentials, "", "  ")
	if err != nil {
		return fmt.Errorf("encode credentials: %w", err)
	}
	// 0600: the agent's Secret is not readable by the collector or the operator.
	return writeAtomic(path, append(payload, '\n'), 0o600)
}

// WriteDeployments renders the data plane's configuration.
//
// Entries are sorted so an unchanged assignment set produces an identical file
// and does not look like a change to anything watching it.
func WriteDeployments(path string, deployments []Deployment) error {
	sorted := make([]Deployment, len(deployments))
	copy(sorted, deployments)
	sort.Slice(sorted, func(i, j int) bool {
		return sorted[i].ModelAlias < sorted[j].ModelAlias
	})
	if sorted == nil {
		sorted = []Deployment{}
	}
	payload, err := json.MarshalIndent(DeploymentsFile{Deployments: sorted}, "", "  ")
	if err != nil {
		return fmt.Errorf("encode deployments: %w", err)
	}
	// 0644: the data plane reads this, and it holds no secret.
	return writeAtomic(path, append(payload, '\n'), 0o644)
}

// ReadDeployments loads a rendered configuration, for tests and diagnostics.
func ReadDeployments(path string) (*DeploymentsFile, error) {
	payload, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", path, err)
	}
	file := &DeploymentsFile{}
	if err := json.Unmarshal(payload, file); err != nil {
		return nil, fmt.Errorf("parse %s: %w", path, err)
	}
	return file, nil
}
