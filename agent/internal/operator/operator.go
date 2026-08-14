// Package operator turns declared model deployments into cluster state.
//
// The agent creates one FabricModelDeployment per assignment the control plane places
// on this stamp. The operator is what makes those declarations real inside the
// cluster, and it is deliberately narrow: it renders the data plane's configuration
// into a ConfigMap and reports what it observed on each resource's status.
//
// It does not create the model host. No vLLM host exists in this project yet, and an
// operator that shipped a Deployment for one would be asserting a component that has
// never run. When that host exists, this is where it belongs.
//
// The split matters for a reason beyond tidiness: the agent holds central credentials
// and no Kubernetes permissions, while the operator holds Kubernetes permissions and
// no central credentials. Neither component can both talk to Fabric and mutate the
// cluster, so a compromise of either is bounded.
package operator

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"sort"
	"time"

	"github.com/khushwant04/fabric/agent/internal/kube"
)

const (
	// Group and version of the custom resource. A single version, because there is
	// exactly one shape today and conversion webhooks are not worth their weight.
	Group   = "fabric.khushwant.dev"
	Version = "v1alpha1"
	// Plural is the resource path segment.
	Plural = "fabricmodeldeployments"
	// Kind as it appears in manifests.
	Kind = "FabricModelDeployment"

	// ConditionApplied reports whether the cluster reflects the declared intent.
	ConditionApplied = "Applied"
)

// Spec is the declared intent for one deployment on this stamp.
//
// It carries the owning account because a managed stamp serves several: the data plane
// authorizes per account, so the account has to survive the trip from the control
// plane through the agent to the cluster.
type Spec struct {
	DeploymentID  string `json:"deploymentId"`
	AccountID     string `json:"accountId"`
	ModelAlias    string `json:"modelAlias"`
	UpstreamURL   string `json:"upstreamUrl"`
	UpstreamModel string `json:"upstreamModel,omitempty"`
	Generation    int    `json:"generation"`
}

// Condition is a standard Kubernetes-style status condition.
type Condition struct {
	Type               string `json:"type"`
	Status             string `json:"status"`
	Reason             string `json:"reason,omitempty"`
	Message            string `json:"message,omitempty"`
	ObservedGeneration int64  `json:"observedGeneration,omitempty"`
	LastTransitionTime string `json:"lastTransitionTime,omitempty"`
}

// Status is what the operator observed, not what was asked for.
type Status struct {
	Phase              string      `json:"phase,omitempty"`
	ObservedGeneration int64       `json:"observedGeneration,omitempty"`
	Conditions         []Condition `json:"conditions,omitempty"`
}

// Metadata is the subset of object metadata the operator uses.
type Metadata struct {
	Name              string            `json:"name"`
	Namespace         string            `json:"namespace,omitempty"`
	ResourceVersion   string            `json:"resourceVersion,omitempty"`
	Generation        int64             `json:"generation,omitempty"`
	Labels            map[string]string `json:"labels,omitempty"`
	Annotations       map[string]string `json:"annotations,omitempty"`
	DeletionTimestamp string            `json:"deletionTimestamp,omitempty"`
}

// ModelDeployment is one custom resource.
type ModelDeployment struct {
	APIVersion string   `json:"apiVersion,omitempty"`
	Kind       string   `json:"kind,omitempty"`
	Metadata   Metadata `json:"metadata"`
	Spec       Spec     `json:"spec"`
	Status     *Status  `json:"status,omitempty"`
}

type modelDeploymentList struct {
	Items []ModelDeployment `json:"items"`
}

// dataPlaneEntry is one line of the file the data plane reads. The field names are the
// data plane's contract, not the CRD's, and the two are deliberately decoupled: the
// custom resource is the cluster's API and can grow, while this file is consumed by a
// running process and changes only when that consumer does.
type dataPlaneEntry struct {
	DeploymentID  string `json:"deployment_id"`
	AccountID     string `json:"account_id"`
	ModelAlias    string `json:"model_alias"`
	UpstreamURL   string `json:"upstream_url"`
	UpstreamModel string `json:"upstream_model,omitempty"`
}

// Options configures a reconciler.
type Options struct {
	Namespace     string
	ConfigMapName string
	// ConfigKey is the file name the data plane mounts, so the ConfigMap projects to
	// the path the data plane already expects.
	ConfigKey string
	Log       *slog.Logger
}

// Reconciler renders declared deployments into cluster state.
type Reconciler struct {
	client  *kube.Client
	options Options
}

// New builds a reconciler.
func New(client *kube.Client, options Options) *Reconciler {
	if options.ConfigMapName == "" {
		options.ConfigMapName = "fabric-deployments"
	}
	if options.ConfigKey == "" {
		options.ConfigKey = "deployments.json"
	}
	if options.Log == nil {
		options.Log = slog.Default()
	}
	return &Reconciler{client: client, options: options}
}

func (r *Reconciler) resourcePath() string {
	return fmt.Sprintf(
		"/apis/%s/%s/namespaces/%s/%s", Group, Version, r.options.Namespace, Plural,
	)
}

func (r *Reconciler) configMapPath(name string) string {
	return fmt.Sprintf("/api/v1/namespaces/%s/configmaps/%s", r.options.Namespace, name)
}

// List returns the declared deployments in this namespace.
func (r *Reconciler) List(ctx context.Context) ([]ModelDeployment, error) {
	var list modelDeploymentList
	if err := r.client.Get(ctx, r.resourcePath(), &list); err != nil {
		return nil, fmt.Errorf("list %s: %w", Plural, err)
	}
	return list.Items, nil
}

// Result describes one reconcile pass.
type Result struct {
	Declared      int
	Serving       int
	ConfigChanged bool
	StatusWrites  int
}

// ReconcileOnce brings the ConfigMap in line with the declared resources and records
// what it observed on each one.
//
// The ConfigMap is written before any status is reported, for the same reason the
// agent writes configuration before acknowledging a generation: a status saying a
// deployment is applied must not be readable before the thing it describes exists.
func (r *Reconciler) ReconcileOnce(ctx context.Context) (Result, error) {
	declared, err := r.List(ctx)
	if err != nil {
		return Result{}, err
	}

	result := Result{Declared: len(declared)}

	serving := make([]ModelDeployment, 0, len(declared))
	for _, item := range declared {
		// A resource being deleted is withdrawn from configuration immediately. The
		// data plane must stop serving it before the object disappears, not after.
		if item.Metadata.DeletionTimestamp != "" {
			continue
		}
		serving = append(serving, item)
	}
	result.Serving = len(serving)

	changed, err := r.applyConfigMap(ctx, serving)
	if err != nil {
		return result, err
	}
	result.ConfigChanged = changed

	for _, item := range serving {
		if !r.statusIsCurrent(item) {
			if err := r.reportApplied(ctx, item); err != nil {
				// One resource failing to accept status does not invalidate the
				// configuration already written, nor the other resources.
				r.options.Log.Warn("could not write status",
					"resource", item.Metadata.Name, "error", err)
				continue
			}
			result.StatusWrites++
		}
	}

	return result, nil
}

// renderConfig produces the data plane's file content.
//
// Entries are sorted by deployment id so an unchanged set produces an identical
// document, which is what lets the reconciler avoid a write and the data plane avoid
// a reload.
func renderConfig(items []ModelDeployment) (string, error) {
	entries := make([]dataPlaneEntry, 0, len(items))
	for _, item := range items {
		entries = append(entries, dataPlaneEntry{
			DeploymentID:  item.Spec.DeploymentID,
			AccountID:     item.Spec.AccountID,
			ModelAlias:    item.Spec.ModelAlias,
			UpstreamURL:   item.Spec.UpstreamURL,
			UpstreamModel: item.Spec.UpstreamModel,
		})
	}
	sort.Slice(entries, func(i, j int) bool {
		return entries[i].DeploymentID < entries[j].DeploymentID
	})

	document, err := json.MarshalIndent(map[string]any{"deployments": entries}, "", "  ")
	if err != nil {
		return "", fmt.Errorf("render configuration: %w", err)
	}
	return string(document) + "\n", nil
}

type configMap struct {
	APIVersion string            `json:"apiVersion,omitempty"`
	Kind       string            `json:"kind,omitempty"`
	Metadata   Metadata          `json:"metadata"`
	Data       map[string]string `json:"data,omitempty"`
}

func (r *Reconciler) applyConfigMap(ctx context.Context, items []ModelDeployment) (bool, error) {
	document, err := renderConfig(items)
	if err != nil {
		return false, err
	}

	desired := configMap{
		APIVersion: "v1",
		Kind:       "ConfigMap",
		Metadata: Metadata{
			Name:      r.options.ConfigMapName,
			Namespace: r.options.Namespace,
			Labels: map[string]string{
				"app.kubernetes.io/managed-by": "fabric-operator",
				"app.kubernetes.io/part-of":    "fabric",
			},
		},
		Data: map[string]string{r.options.ConfigKey: document},
	}

	var existing configMap
	err = r.client.Get(ctx, r.configMapPath(r.options.ConfigMapName), &existing)
	if kube.IsNotFound(err) {
		path := fmt.Sprintf("/api/v1/namespaces/%s/configmaps", r.options.Namespace)
		if err := r.client.Create(ctx, path, desired, nil); err != nil {
			return false, fmt.Errorf("create configmap: %w", err)
		}
		r.options.Log.Info("configuration created",
			"configmap", r.options.ConfigMapName, "deployments", len(items))
		return true, nil
	}
	if err != nil {
		return false, fmt.Errorf("read configmap: %w", err)
	}

	if existing.Data[r.options.ConfigKey] == document {
		// Writing an identical document would bump the resourceVersion and make every
		// mounted copy churn for no reason.
		return false, nil
	}

	// Carry the resourceVersion so a concurrent writer is detected instead of being
	// silently overwritten.
	desired.Metadata.ResourceVersion = existing.Metadata.ResourceVersion
	if err := r.client.Update(
		ctx, r.configMapPath(r.options.ConfigMapName), desired, nil,
	); err != nil {
		return false, fmt.Errorf("update configmap: %w", err)
	}
	r.options.Log.Info("configuration updated",
		"configmap", r.options.ConfigMapName, "deployments", len(items))
	return true, nil
}

// statusIsCurrent reports whether the resource already carries the operator's verdict
// for its current generation, so an unchanged resource is not rewritten every pass.
func (r *Reconciler) statusIsCurrent(item ModelDeployment) bool {
	if item.Status == nil || item.Status.ObservedGeneration != item.Metadata.Generation {
		return false
	}
	for _, condition := range item.Status.Conditions {
		if condition.Type == ConditionApplied && condition.Status == "True" {
			return true
		}
	}
	return false
}

func (r *Reconciler) reportApplied(ctx context.Context, item ModelDeployment) error {
	patch := map[string]any{
		"status": Status{
			Phase:              "ready",
			ObservedGeneration: item.Metadata.Generation,
			Conditions: []Condition{
				{
					Type:   ConditionApplied,
					Status: "True",
					// Names what was actually done. The operator renders the data
					// plane's configuration; it does not start a model host, so a
					// reason implying a running workload would be false.
					Reason:             "DataPlaneConfigurationRendered",
					Message:            "The stamp's data plane is configured to serve this deployment",
					ObservedGeneration: item.Metadata.Generation,
					LastTransitionTime: time.Now().UTC().Format(time.RFC3339),
				},
			},
		},
	}

	path := fmt.Sprintf("%s/%s/status", r.resourcePath(), item.Metadata.Name)
	if err := r.client.MergePatch(ctx, path, patch, nil); err != nil {
		return fmt.Errorf("patch status: %w", err)
	}
	return nil
}

// Run reconciles on an interval until the context ends.
func (r *Reconciler) Run(ctx context.Context, interval time.Duration) error {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		result, err := r.ReconcileOnce(ctx)
		if err != nil {
			// A controller keeps trying: the API server may be briefly unavailable,
			// and giving up would leave the cluster stale with no recovery path.
			r.options.Log.Warn("reconcile failed, will retry", "error", err)
		} else if result.ConfigChanged || result.StatusWrites > 0 {
			r.options.Log.Info("reconciled",
				"declared", result.Declared, "serving", result.Serving,
				"config_changed", result.ConfigChanged, "status_writes", result.StatusWrites)
		}

		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
		}
	}
}
