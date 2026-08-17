// Package controlplane talks to the Fabric control plane.
//
// The agent is the only cluster component holding a stamp credential. It calls
// four endpoints and nothing else: enrollment once, then heartbeat,
// desired-state reads, and status writes. It never invokes inference and never
// uses the telemetry credential, which belongs to the collector.
package controlplane

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"
)

// Client is a control-plane API client for one stamp.
type Client struct {
	BaseURL    string
	HTTP       *http.Client
	Credential string // agent credential; empty until enrollment completes
}

// New returns a client with a bounded timeout.
func New(baseURL string, timeout time.Duration) *Client {
	return &Client{
		BaseURL: strings.TrimRight(baseURL, "/"),
		HTTP:    &http.Client{Timeout: timeout},
	}
}

// APIError is a structured error envelope returned by the control plane.
type APIError struct {
	Status  int
	Code    string
	Message string
}

func (e *APIError) Error() string {
	return fmt.Sprintf("control plane %d %s: %s", e.Status, e.Code, e.Message)
}

// Retryable reports whether waiting and trying again could succeed.
//
// A revoked credential or a consumed enrollment token never becomes valid again,
// so the agent must stop rather than hammer the control plane.
func (e *APIError) Retryable() bool {
	switch e.Code {
	case "credential_revoked", "credential_expired", "stamp_revoked",
		"enrollment_token_used", "enrollment_token_revoked", "enrollment_token_expired",
		"invalid_credential", "invalid_enrollment_token", "stamp_mismatch":
		return false
	}
	return e.Status >= 500 || e.Status == http.StatusTooManyRequests
}

// GPU describes one accelerator in a capability report.
type GPU struct {
	Product           string `json:"product"`
	Count             int    `json:"count"`
	MemoryBytes       int64  `json:"memory_bytes"`
	ComputeCapability string `json:"compute_capability,omitempty"`
}

// Capabilities is the bounded capability report. The control plane rejects
// unknown fields, so this struct must not drift from its schema.
type Capabilities struct {
	Orchestrator        string `json:"orchestrator"`
	OrchestratorVersion string `json:"orchestrator_version,omitempty"`
	Region              string `json:"region,omitempty"`
	GPUs                []GPU  `json:"gpus"`
	AllocatableGPUs     int    `json:"allocatable_gpus"`
	RequestedGPUs       int    `json:"requested_gpus"`
	DriverVersion       string `json:"driver_version,omitempty"`
	AgentVersion        string `json:"agent_version,omitempty"`
	RuntimeVersion      string `json:"runtime_version,omitempty"`
}

// Stamp is the registered stamp record.
type Stamp struct {
	ID           string `json:"id"`
	AccountID    string `json:"account_id"`
	Name         string `json:"name"`
	Mode         string `json:"mode"`
	Orchestrator string `json:"orchestrator"`
	Region       string `json:"region"`
	Status       string `json:"status"`
}

// EnrollResponse carries the stamp and both one-time credentials.
type EnrollResponse struct {
	Stamp               Stamp  `json:"stamp"`
	AgentCredential     string `json:"agent_credential"`
	TelemetryCredential string `json:"telemetry_credential"`
}

// DesiredDeployment is one assignment to reconcile.
type DesiredDeployment struct {
	DeploymentID      string         `json:"deployment_id"`
	AccountID         string         `json:"account_id"`
	Name              string         `json:"name"`
	ModelAlias        string         `json:"model_alias"`
	DesiredGeneration int            `json:"desired_generation"`
	Spec              map[string]any `json:"spec"`
	Deleted           bool           `json:"deleted"`
}

// DesiredState is the response to a desired-state read.
type DesiredState struct {
	StampID       string              `json:"stamp_id"`
	MaxGeneration int                 `json:"max_generation"`
	Deployments   []DesiredDeployment `json:"deployments"`
}

// StatusReport is an observed-state write.
type StatusReport struct {
	DeploymentID        string           `json:"deployment_id"`
	ObservedGeneration  *int             `json:"observed_generation,omitempty"`
	Phase               string           `json:"phase"`
	ReadyReplicas       int              `json:"ready_replicas"`
	UnavailableReplicas int              `json:"unavailable_replicas"`
	Endpoint            string           `json:"endpoint,omitempty"`
	Conditions          []map[string]any `json:"conditions,omitempty"`
}

func (c *Client) do(ctx context.Context, method, path string, body, out any, auth bool) error {
	var reader io.Reader
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			return fmt.Errorf("encode request: %w", err)
		}
		reader = bytes.NewReader(encoded)
	}

	request, err := http.NewRequestWithContext(ctx, method, c.BaseURL+path, reader)
	if err != nil {
		return fmt.Errorf("build request: %w", err)
	}
	if body != nil {
		request.Header.Set("Content-Type", "application/json")
	}
	if auth {
		if c.Credential == "" {
			return errors.New("no agent credential: enrollment has not completed")
		}
		request.Header.Set("Authorization", "Bearer "+c.Credential)
	}

	response, err := c.HTTP.Do(request)
	if err != nil {
		return fmt.Errorf("%s %s: %w", method, path, err)
	}
	defer response.Body.Close()

	payload, err := io.ReadAll(io.LimitReader(response.Body, 8<<20))
	if err != nil {
		return fmt.Errorf("read response: %w", err)
	}

	if response.StatusCode >= 300 {
		apiErr := &APIError{Status: response.StatusCode, Code: "unknown", Message: string(payload)}
		var envelope struct {
			Error struct {
				Code    string `json:"code"`
				Message string `json:"message"`
			} `json:"error"`
		}
		if json.Unmarshal(payload, &envelope) == nil && envelope.Error.Code != "" {
			apiErr.Code = envelope.Error.Code
			apiErr.Message = envelope.Error.Message
		}
		return apiErr
	}

	if out != nil && len(payload) > 0 {
		if err := json.Unmarshal(payload, out); err != nil {
			return fmt.Errorf("decode response: %w", err)
		}
	}
	return nil
}

// Enroll registers this stamp with a one-time token.
//
// The token is presented once and then discarded by the caller; the control
// plane derives account ownership from the token record and ignores any
// ownership field a caller might send.
func (c *Client) Enroll(
	ctx context.Context, token, name string, capabilities Capabilities,
) (*EnrollResponse, error) {
	body := map[string]any{
		"enrollment_token": token,
		"name":             name,
		"capabilities":     capabilities,
	}
	out := &EnrollResponse{}
	if err := c.do(ctx, http.MethodPost, "/v1/stamps/enroll", body, out, false); err != nil {
		return nil, err
	}
	return out, nil
}

// Heartbeat reports liveness and optionally refreshes capabilities.
func (c *Client) Heartbeat(ctx context.Context, stampID string, capabilities *Capabilities) error {
	body := map[string]any{}
	if capabilities != nil {
		body["capabilities"] = capabilities
	}
	path := "/v1/stamps/" + url.PathEscape(stampID) + "/heartbeat"
	return c.do(ctx, http.MethodPost, path, body, nil, true)
}

// DesiredState reads assignments newer than the acknowledged generation.
func (c *Client) DesiredState(
	ctx context.Context, stampID string, afterGeneration int,
) (*DesiredState, error) {
	path := "/v1/stamps/" + url.PathEscape(stampID) +
		"/desired-state?after_generation=" + strconv.Itoa(afterGeneration)
	out := &DesiredState{}
	if err := c.do(ctx, http.MethodGet, path, nil, out, true); err != nil {
		return nil, err
	}
	return out, nil
}

// ReportStatus writes observed state for one deployment.
func (c *Client) ReportStatus(ctx context.Context, stampID string, report StatusReport) error {
	path := "/v1/stamps/" + url.PathEscape(stampID) + "/status"
	return c.do(ctx, http.MethodPost, path, report, nil, true)
}

// MaxUsageBatch matches the server's per-request limit, so a larger backlog is
// chunked rather than rejected wholesale.
const MaxUsageBatch = 500

// UsageRecord is one record as the ingestion contract expects it.
//
// It deliberately carries no account and no stamp. The server derives both from
// the telemetry credential and the placement, and rejects a record that tries to
// carry them, so ownership cannot originate at the edge.
type UsageRecord struct {
	DeploymentID     string `json:"deployment_id"`
	InputTokens      int    `json:"input_tokens"`
	OutputTokens     int    `json:"output_tokens"`
	OccurredAt       string `json:"occurred_at"`
	DeduplicationKey string `json:"deduplication_key"`
}

// UsageRejection identifies a record the server refused, by position in the batch.
type UsageRejection struct {
	Index        int    `json:"index"`
	Code         string `json:"code"`
	DeploymentID string `json:"deployment_id,omitempty"`
}

// UsageIngestResult is the server's per-batch accounting.
type UsageIngestResult struct {
	Accepted   int              `json:"accepted"`
	Duplicates int              `json:"duplicates"`
	Rejected   int              `json:"rejected"`
	Rejections []UsageRejection `json:"rejections"`
}

// ReportUsage submits a batch of usage records.
//
// Authentication uses whatever credential the client holds, which for a collector
// is the write-only telemetry credential. A collector must never be constructed
// with the agent credential: the server refuses it, but the separation is the
// caller's to maintain.
func (c *Client) ReportUsage(ctx context.Context, records []UsageRecord) (*UsageIngestResult, error) {
	if len(records) == 0 {
		return nil, errors.New("no records to report")
	}
	if len(records) > MaxUsageBatch {
		return nil, fmt.Errorf("batch of %d exceeds the limit of %d", len(records), MaxUsageBatch)
	}
	body := struct {
		Records []UsageRecord `json:"records"`
	}{Records: records}

	var result UsageIngestResult
	if err := c.do(ctx, http.MethodPost, "/v1/telemetry/usage", body, &result, true); err != nil {
		return nil, err
	}
	return &result, nil
}

// ReportMetrics submits one metrics sample for this stamp.
//
// Separate from usage on purpose: usage is billing-relevant and must not be lost, so it
// is retried and deduplicated, while a metrics sample is superseded by the next one and
// retrying a stale reading would report the past as the present.
func (c *Client) ReportMetrics(ctx context.Context, report any) error {
	return c.do(ctx, http.MethodPost, "/v1/telemetry/metrics", report, nil, true)
}
