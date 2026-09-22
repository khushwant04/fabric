// Package state persists what the agent must not lose across restarts, and
// renders the local configuration the data plane reads.
//
// Two files, with different audiences and permissions:
//
//	credentials.json  the stamp id and machine credentials. 0600, agent only.
//	telemetry-credential  the write-only telemetry credential. 0600, for the
//	                      collector's own Secret, so the collector never reads
//	                      the agent's file and cannot obtain the agent credential.
//	                  In Kubernetes this is a Secret mounted for the agent alone.
//	deployments.json  the deployments assigned here and who owns each one.
//	                  Readable by the data plane; contains no secret.
//
// Losing credentials.json would force a re-enrollment, and enrollment tokens are
// single use, so a stamp that loses it needs a new token from an operator.
package state

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
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

// ModelCapabilities is bounded metadata for stamp-local automatic model selection.
// It is comparable so deployment changes remain detectable with ordinary struct equality.
type ModelCapabilities struct {
	AutoEnabled   bool `json:"auto_enabled"`
	Chat          bool `json:"chat"`
	Completion    bool `json:"completion"`
	Vision        bool `json:"vision"`
	Transcription bool `json:"transcription"`
	Translation   bool `json:"translation"`
	Code          bool `json:"code"`
	Reasoning     bool `json:"reasoning"`
	Priority      int  `json:"priority"`
}

// Deployment is one entry of the data plane's local configuration.
type Deployment struct {
	DeploymentID  string `json:"deployment_id"`
	AccountID     string `json:"account_id"`
	ModelAlias    string `json:"model_alias"`
	UpstreamURL   string `json:"upstream_url"`
	UpstreamModel string `json:"upstream_model,omitempty"`
	// KernelMode is carried for the operator's benefit, not the data plane's: the data
	// plane reads the keys it knows and ignores the rest, while the operator needs to
	// know which decode kernel the deployment asked for.
	KernelMode string `json:"kernel_mode,omitempty"`
	// Replicas is how many model-host replicas the deployment asks for. Carried for the
	// operator, which turns it into the Deployment's replica count (ADR 0010); the data
	// plane ignores it. Zero means the field was absent, which the operator reads as one.
	Replicas int `json:"replicas,omitempty"`
	// Strategy is how the data plane balances across the deployment's backends (M2,
	// ADR 0011): least_in_flight (default), round_robin, session_affinity, or weighted.
	// Unlike the other fields this one the data plane does read, once the operator has
	// carried it through the CR into the config document. Empty means the data plane's
	// own default of least-in-flight.
	Strategy string `json:"strategy,omitempty"`
	// Capabilities drives model=auto selection in the data plane. Legacy deployments
	// default to text chat/completion when the agent reads an older desired spec.
	Capabilities ModelCapabilities `json:"capabilities"`
	// GPUCount is how many devices one replica needs. Carried for the operator, which
	// turns it into the container's nvidia.com/gpu limit; the data plane ignores it.
	//
	// The control plane admits a placement by comparing replicas x this against what the
	// stamp reports (ADR 0013), so it has to be the same number the pod actually asks
	// for. Before M4 it was validated centrally and then ignored, while the pod was sized
	// from a per-stamp Helm value. Zero means the field was absent, which the operator
	// reads as the chart's configured count.
	GPUCount int `json:"gpu_count,omitempty"`
	// MaxModelLen, MaxNumSeqs, GPUMemoryUtilization, and Execution are serving settings
	// the deployment asked for. Carried for the operator, which turns them into the model
	// host's own flags; the data plane ignores them.
	//
	// These were per-stamp Helm values, which forced one context length to suit every
	// model on the stamp. Zero or empty means the field was absent, which the operator
	// reads as the value it was configured with.
	MaxModelLen int `json:"max_model_len,omitempty"`
	MaxNumSeqs  int `json:"max_num_seqs,omitempty"`
	// Carried as a string so the fraction reaches the server exactly as written rather
	// than as the nearest float64 rendering of it.
	GPUMemoryUtilization string `json:"gpu_memory_utilization,omitempty"`
	// Execution is "eager" to skip CUDA graph capture or "cuda_graph" to capture. Empty
	// means the stamp's configured behaviour, which is what keeps "no opinion"
	// distinguishable from "explicitly do not capture".
	Execution string `json:"execution,omitempty"`
	// Verification is stamp-wide, carried here because this struct is what flows through
	// both delivery paths: the agent writes the file directly, or declares custom
	// resources the operator renders. It is deliberately not serialised per entry — both
	// writers lift it to the document's single top-level section instead, since repeating
	// one stamp's issuer on every deployment would invite the entries to disagree.
	Verification Verification `json:"-"`
}

// Verification is how the data plane must check the tokens it is sent.
//
// Stamp-wide rather than per-deployment: one control plane mints every token a stamp
// serves, so there is one issuer and one key source. It comes from the control plane's
// own signing configuration, which is what makes it authoritative — a cluster holding a
// different value is drift, and correcting that is the point of carrying it.
type Verification struct {
	JWTIssuer string `json:"jwt_issuer,omitempty"`
	JWKSURL   string `json:"jwks_url,omitempty"`
}

// DeploymentsFile is the document the data plane loads.
type DeploymentsFile struct {
	Deployments []Deployment `json:"deployments"`
	// Written once at the top rather than repeated on every entry, because it describes
	// the stamp rather than any one deployment. Omitted when the control plane did not
	// send one, which the data plane reads as "keep what you have" rather than "clear it".
	Verification *Verification `json:"verification,omitempty"`
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
	document := DeploymentsFile{Deployments: sorted}
	// Lifted out of the entries to the one place it belongs. Every entry carries the same
	// stamp-wide value, so the first one that has it decides; omitted entirely when the
	// control plane sent nothing, which the data plane reads as "keep what you have".
	for _, entry := range sorted {
		if entry.Verification.JWTIssuer != "" || entry.Verification.JWKSURL != "" {
			verification := entry.Verification
			document.Verification = &verification
			break
		}
	}
	payload, err := json.MarshalIndent(document, "", "  ")
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

// TelemetryCredentialFile is the file name the agent writes for the collector.
const TelemetryCredentialFile = "telemetry-credential"

// WriteTelemetryCredential hands the telemetry credential to the collector.
//
// It is written separately from credentials.json rather than shared, because that
// file also holds the agent credential: a collector able to read it could pull
// desired state and write status, which the credential separation exists to
// prevent. Only this file belongs in the collector's Secret.
func WriteTelemetryCredential(path, credential string) error {
	if credential == "" {
		return errors.New("refusing to write an empty telemetry credential")
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return fmt.Errorf("create state directory: %w", err)
	}
	return writeAtomic(path, []byte(credential+"\n"), 0o600)
}

// ReadTelemetryCredential reads the credential the collector was given.
func ReadTelemetryCredential(path string) (string, error) {
	raw, err := os.ReadFile(path)
	if err != nil {
		return "", fmt.Errorf("read telemetry credential: %w", err)
	}
	credential := strings.TrimSpace(string(raw))
	if credential == "" {
		return "", fmt.Errorf("telemetry credential at %s is empty", path)
	}
	return credential, nil
}
