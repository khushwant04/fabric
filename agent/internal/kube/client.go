// Package kube is a small Kubernetes API client for the operator.
//
// It covers exactly what the operator does: read one custom resource kind, write one
// ConfigMap, and patch status. That is a few hundred lines against the REST API,
// where controller-runtime or client-go would add a large transitive dependency tree
// to a module that currently has none, and an image that is 21 MB because it ships a
// static binary on a distroless base.
//
// The tradeoff is deliberate and bounded: no informers, no caches, no leader
// election. The operator reconciles a handful of resources on an interval, which does
// not need them. If it ever manages many objects or needs watch-driven latency, the
// right move is client-go rather than growing this file.
package kube

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"time"
)

const (
	tokenPath     = "/var/run/secrets/kubernetes.io/serviceaccount/token"
	caPath        = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
	namespacePath = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
)

// Client talks to the Kubernetes API server.
type Client struct {
	BaseURL   string
	Namespace string
	HTTP      *http.Client

	token     string
	tokenPath string
}

// APIError is a non-success response from the API server.
type APIError struct {
	Status  int
	Reason  string
	Message string
}

func (e *APIError) Error() string {
	return fmt.Sprintf("kubernetes %d %s: %s", e.Status, e.Reason, e.Message)
}

// IsNotFound reports whether the resource does not exist, which is an ordinary
// outcome for a controller rather than a failure.
func IsNotFound(err error) bool {
	var apiErr *APIError
	return errors.As(err, &apiErr) && apiErr.Status == http.StatusNotFound
}

// IsConflict reports a lost optimistic-concurrency race, which is retried.
func IsConflict(err error) bool {
	var apiErr *APIError
	return errors.As(err, &apiErr) && apiErr.Status == http.StatusConflict
}

// InCluster builds a client from the pod's service-account credentials.
func InCluster() (*Client, error) {
	host := os.Getenv("KUBERNETES_SERVICE_HOST")
	port := os.Getenv("KUBERNETES_SERVICE_PORT")
	if host == "" || port == "" {
		return nil, errors.New("not running in a cluster: KUBERNETES_SERVICE_HOST is unset")
	}

	token, err := os.ReadFile(tokenPath)
	if err != nil {
		return nil, fmt.Errorf("read service account token: %w", err)
	}
	namespace, err := os.ReadFile(namespacePath)
	if err != nil {
		return nil, fmt.Errorf("read namespace: %w", err)
	}
	authority, err := os.ReadFile(caPath)
	if err != nil {
		return nil, fmt.Errorf("read cluster CA: %w", err)
	}

	pool := x509.NewCertPool()
	if !pool.AppendCertsFromPEM(authority) {
		return nil, errors.New("cluster CA bundle contains no usable certificate")
	}

	return &Client{
		BaseURL:   fmt.Sprintf("https://%s:%s", host, port),
		Namespace: strings.TrimSpace(string(namespace)),
		HTTP: &http.Client{
			Timeout: 30 * time.Second,
			Transport: &http.Transport{
				// The API server is verified against the cluster CA. Skipping
				// verification would expose the service-account token to anything
				// that can intercept the connection.
				TLSClientConfig: &tls.Config{RootCAs: pool, MinVersion: tls.VersionTLS12},
			},
		},
		token:     strings.TrimSpace(string(token)),
		tokenPath: tokenPath,
	}, nil
}

// refreshToken re-reads a projected service-account token.
//
// Projected tokens are short-lived and rotated in place, so a long-running controller
// that read the file once would eventually authenticate with an expired token.
func (c *Client) refreshToken() {
	if c.tokenPath == "" {
		return
	}
	if raw, err := os.ReadFile(c.tokenPath); err == nil {
		if token := strings.TrimSpace(string(raw)); token != "" {
			c.token = token
		}
	}
}

func (c *Client) do(
	ctx context.Context, method, path string, body any, out any, contentType string,
) error {
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
		if contentType == "" {
			contentType = "application/json"
		}
		request.Header.Set("Content-Type", contentType)
	}
	request.Header.Set("Accept", "application/json")
	if c.token != "" {
		request.Header.Set("Authorization", "Bearer "+c.token)
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
		apiErr := &APIError{Status: response.StatusCode, Message: string(payload)}
		var status struct {
			Reason  string `json:"reason"`
			Message string `json:"message"`
		}
		if json.Unmarshal(payload, &status) == nil && status.Reason != "" {
			apiErr.Reason = status.Reason
			apiErr.Message = status.Message
		}
		if response.StatusCode == http.StatusUnauthorized {
			// Most likely a rotated projected token; the next attempt uses the new one.
			c.refreshToken()
		}
		return apiErr
	}

	if out == nil {
		return nil
	}
	if err := json.Unmarshal(payload, out); err != nil {
		return fmt.Errorf("decode response: %w", err)
	}
	return nil
}

// Get reads a resource into out.
func (c *Client) Get(ctx context.Context, path string, out any) error {
	return c.do(ctx, http.MethodGet, path, nil, out, "")
}

// Create posts a new resource.
func (c *Client) Create(ctx context.Context, path string, body any, out any) error {
	return c.do(ctx, http.MethodPost, path, body, out, "")
}

// Update replaces a resource, carrying the resourceVersion it was read at so a
// concurrent writer is detected rather than silently overwritten.
func (c *Client) Update(ctx context.Context, path string, body any, out any) error {
	return c.do(ctx, http.MethodPut, path, body, out, "")
}

// Delete removes a resource.
func (c *Client) Delete(ctx context.Context, path string) error {
	return c.do(ctx, http.MethodDelete, path, nil, nil, "")
}

// MergePatch applies a JSON merge patch.
func (c *Client) MergePatch(ctx context.Context, path string, patch any, out any) error {
	return c.do(ctx, http.MethodPatch, path, patch, out, "application/merge-patch+json")
}
