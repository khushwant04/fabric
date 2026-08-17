package operator

import (
	"context"
	"fmt"
	"strconv"
	"strings"

	"github.com/khushwant04/fabric/agent/internal/kube"
)

// ModelHost describes how to run the inference server for a deployment.
//
// The operator owns this workload; the agent does not, and could not: it holds no
// Kubernetes permissions beyond declaring intent. Keeping the host here means a
// placement becomes a running server without anyone applying a manifest by hand,
// which was the last manual step in the path from a control-plane placement to
// served tokens.
type ModelHost struct {
	// Image serving the model. Fabric does not build it; it is a vLLM image with the
	// weights either baked in or mounted.
	Image string
	// ServedName is what callers ask for, which the data plane sends upstream. It is
	// the release name rather than the customer's alias.
	ServedName string
	// ModelRef is what the server loads: a repository id or a path inside the image.
	ModelRef string
	// GPUs requested per replica. One replica per GPU is the MVP shape.
	GPUs int
	// MaxModelLen bounds context. It is required because the default for this model
	// family is very large and would not fit alongside its own weights.
	MaxModelLen int
	MaxNumSeqs  int
	// GPUMemoryUtilization is the fraction of the device vLLM may use. Below 1 by
	// necessity: the fraction is of *total* memory, and anything else resident on the
	// device is not accounted for, so asking for all of it fails at startup.
	GPUMemoryUtilization string
	// EnforceEager disables CUDA graph capture, which costs latency but saves the
	// memory those graphs would hold.
	EnforceEager bool
	DType        string
	// ExtraArgs are appended verbatim, for flags this type does not model.
	ExtraArgs []string
	// Port the server listens on.
	Port int
	// RuntimeClassName and NodeSelector place the pod on a GPU node.
	RuntimeClassName string
	NodeSelector     map[string]string
	Tolerations      []map[string]any
	// CacheClaim mounts an existing PersistentVolumeClaim at the model cache path, so
	// weights survive a restart instead of being pulled again.
	CacheClaim string
}

// Enabled reports whether the operator should manage a host at all.
//
// Without an image there is nothing to run, and the operator falls back to
// configuring the data plane against an upstream someone else operates. That is the
// behaviour every existing deployment relies on, so it stays the default.
func (m ModelHost) Enabled() bool {
	return m.Image != ""
}

const (
	// ConditionHostReady reports the model host's own readiness, separately from
	// whether configuration was applied. A caller needs to distinguish "the stamp
	// accepted this deployment" from "the server can answer".
	ConditionHostReady = "ModelHostReady"

	hostManagedBy = "fabric-operator"
)

// hostName derives the workload name from the deployment, so one host exists per
// deployment and its identity is stable across reconciles.
func hostName(item ModelDeployment) string {
	return "fabric-host-" + strings.ToLower(item.Spec.DeploymentID)
}

type deployment struct {
	APIVersion string         `json:"apiVersion,omitempty"`
	Kind       string         `json:"kind,omitempty"`
	Metadata   Metadata       `json:"metadata"`
	Spec       map[string]any `json:"spec"`
	Status     *struct {
		ReadyReplicas int `json:"readyReplicas,omitempty"`
		Replicas      int `json:"replicas,omitempty"`
	} `json:"status,omitempty"`
}

type service struct {
	APIVersion string         `json:"apiVersion,omitempty"`
	Kind       string         `json:"kind,omitempty"`
	Metadata   Metadata       `json:"metadata"`
	Spec       map[string]any `json:"spec"`
}

func (r *Reconciler) deploymentPath(name string) string {
	return fmt.Sprintf("/apis/apps/v1/namespaces/%s/deployments/%s", r.options.Namespace, name)
}

func (r *Reconciler) servicePath(name string) string {
	return fmt.Sprintf("/api/v1/namespaces/%s/services/%s", r.options.Namespace, name)
}

// hostArgs builds the server's command line.
//
// Every value is explicit rather than relying on the server's defaults, because the
// defaults are chosen for large-memory datacentre parts: the context length alone
// would exhaust a small device before the weights are loaded.
func (m ModelHost) hostArgs() []string {
	args := []string{
		"--model=" + m.ModelRef,
		"--served-model-name=" + m.ServedName,
		"--port=" + strconv.Itoa(m.Port),
		"--host=0.0.0.0",
	}
	if m.DType != "" {
		args = append(args, "--dtype="+m.DType)
	}
	if m.MaxModelLen > 0 {
		args = append(args, "--max-model-len="+strconv.Itoa(m.MaxModelLen))
	}
	if m.MaxNumSeqs > 0 {
		args = append(args, "--max-num-seqs="+strconv.Itoa(m.MaxNumSeqs))
	}
	if m.GPUMemoryUtilization != "" {
		args = append(args, "--gpu-memory-utilization="+m.GPUMemoryUtilization)
	}
	if m.EnforceEager {
		args = append(args, "--enforce-eager")
	}
	return append(args, m.ExtraArgs...)
}

func (r *Reconciler) desiredHost(item ModelDeployment) deployment {
	host := r.options.ModelHost
	name := hostName(item)
	labels := map[string]string{
		"app.kubernetes.io/name":             "fabric-model-host",
		"app.kubernetes.io/managed-by":       hostManagedBy,
		"app.kubernetes.io/part-of":          "fabric",
		"fabric.khushwant.dev/deployment-id": item.Spec.DeploymentID,
		"fabric.khushwant.dev/account-id":    item.Spec.AccountID,
	}

	container := map[string]any{
		"name":            "model-host",
		"image":           host.Image,
		"imagePullPolicy": "IfNotPresent",
		"args":            host.hostArgs(),
		"ports": []map[string]any{
			{"name": "http", "containerPort": host.Port, "protocol": "TCP"},
		},
		"resources": map[string]any{
			"limits": map[string]any{
				// Requests are omitted deliberately: for an extended resource
				// Kubernetes requires request and limit to be equal, and setting only
				// the limit makes that explicit rather than relying on defaulting.
				"nvidia.com/gpu": host.GPUs,
			},
		},
		// Readiness gates traffic on the server being able to answer, which for a
		// model server is minutes after the container starts.
		"readinessProbe": map[string]any{
			"httpGet":             map[string]any{"path": "/health", "port": "http"},
			"initialDelaySeconds": 15,
			"periodSeconds":       10,
			// Loading weights is slow and a restart loop would never finish, so this
			// is generous rather than tight.
			"failureThreshold": 60,
		},
		"livenessProbe": map[string]any{
			"httpGet":             map[string]any{"path": "/health", "port": "http"},
			"initialDelaySeconds": 300,
			"periodSeconds":       30,
			"failureThreshold":    6,
		},
		"volumeMounts": []map[string]any{
			// A model server needs writable scratch and shared memory; the image is
			// otherwise treated as immutable.
			{"name": "cache", "mountPath": "/root/.cache/huggingface"},
			{"name": "shm", "mountPath": "/dev/shm"},
		},
	}

	volumes := []map[string]any{
		{"name": "shm", "emptyDir": map[string]any{"medium": "Memory"}},
	}
	if host.CacheClaim != "" {
		volumes = append(volumes, map[string]any{
			"name":                  "cache",
			"persistentVolumeClaim": map[string]any{"claimName": host.CacheClaim},
		})
	} else {
		volumes = append(volumes, map[string]any{"name": "cache", "emptyDir": map[string]any{}})
	}

	podSpec := map[string]any{
		"containers": []map[string]any{container},
		"volumes":    volumes,
		// The model host calls no Kubernetes API.
		"automountServiceAccountToken": false,
	}
	if host.RuntimeClassName != "" {
		podSpec["runtimeClassName"] = host.RuntimeClassName
	}
	if len(host.NodeSelector) > 0 {
		podSpec["nodeSelector"] = host.NodeSelector
	}
	if len(host.Tolerations) > 0 {
		podSpec["tolerations"] = host.Tolerations
	}

	return deployment{
		APIVersion: "apps/v1",
		Kind:       "Deployment",
		Metadata: Metadata{
			Name:      name,
			Namespace: r.options.Namespace,
			Labels:    labels,
		},
		Spec: map[string]any{
			// One replica per deployment: a GPU is not shared between replicas, and
			// scaling is a placement decision the control plane makes by placing on
			// more stamps.
			"replicas": 1,
			"selector": map[string]any{
				"matchLabels": map[string]string{
					"fabric.khushwant.dev/deployment-id": item.Spec.DeploymentID,
					"app.kubernetes.io/name":             "fabric-model-host",
				},
			},
			"strategy": map[string]any{
				// Recreate, not RollingUpdate: two replicas would both want the GPU
				// and the new one would never schedule while the old one holds it.
				"type": "Recreate",
			},
			"template": map[string]any{
				"metadata": map[string]any{"labels": labels},
				"spec":     podSpec,
			},
		},
	}
}

func (r *Reconciler) desiredHostService(item ModelDeployment) service {
	name := hostName(item)
	return service{
		APIVersion: "v1",
		Kind:       "Service",
		Metadata: Metadata{
			Name:      name,
			Namespace: r.options.Namespace,
			Labels: map[string]string{
				"app.kubernetes.io/name":             "fabric-model-host",
				"app.kubernetes.io/managed-by":       hostManagedBy,
				"fabric.khushwant.dev/deployment-id": item.Spec.DeploymentID,
			},
		},
		Spec: map[string]any{
			"selector": map[string]string{
				"fabric.khushwant.dev/deployment-id": item.Spec.DeploymentID,
				"app.kubernetes.io/name":             "fabric-model-host",
			},
			"ports": []map[string]any{
				{"name": "http", "port": r.options.ModelHost.Port, "targetPort": "http"},
			},
		},
	}
}

// hostUpstream is the address the data plane should proxy to for this deployment.
//
// The operator overrides whatever the agent rendered, because when the operator owns
// the host it is the only component that knows where it ended up.
func (r *Reconciler) hostUpstream(item ModelDeployment) string {
	return fmt.Sprintf(
		"http://%s.%s.svc:%d", hostName(item), r.options.Namespace, r.options.ModelHost.Port,
	)
}

// applyHost creates or updates the workload for one deployment and reports whether
// its server is ready.
func (r *Reconciler) applyHost(ctx context.Context, item ModelDeployment) (bool, error) {
	name := hostName(item)
	desired := r.desiredHost(item)

	var existing deployment
	err := r.client.Get(ctx, r.deploymentPath(name), &existing)
	switch {
	case kube.IsNotFound(err):
		path := fmt.Sprintf("/apis/apps/v1/namespaces/%s/deployments", r.options.Namespace)
		if err := r.client.Create(ctx, path, desired, nil); err != nil {
			return false, fmt.Errorf("create model host %s: %w", name, err)
		}
		r.options.Log.Info("model host created", "deployment", name)
	case err != nil:
		return false, fmt.Errorf("read model host %s: %w", name, err)
	default:
		// Patched rather than replaced, so fields defaulted by the API server and
		// anything a cluster admission controller added are left alone.
		patch := map[string]any{"spec": desired.Spec, "metadata": map[string]any{"labels": desired.Metadata.Labels}}
		if err := r.client.MergePatch(ctx, r.deploymentPath(name), patch, nil); err != nil {
			return false, fmt.Errorf("update model host %s: %w", name, err)
		}
	}

	if err := r.applyHostService(ctx, item); err != nil {
		return false, err
	}

	// Readiness comes from the workload's own status rather than being assumed from a
	// successful write: creating a Deployment says nothing about whether the server
	// inside it has loaded a model.
	var current deployment
	if err := r.client.Get(ctx, r.deploymentPath(name), &current); err != nil {
		return false, fmt.Errorf("read model host status %s: %w", name, err)
	}
	if current.Status == nil {
		return false, nil
	}
	return current.Status.ReadyReplicas > 0, nil
}

func (r *Reconciler) applyHostService(ctx context.Context, item ModelDeployment) error {
	name := hostName(item)
	desired := r.desiredHostService(item)

	var existing service
	err := r.client.Get(ctx, r.servicePath(name), &existing)
	if kube.IsNotFound(err) {
		path := fmt.Sprintf("/api/v1/namespaces/%s/services", r.options.Namespace)
		if err := r.client.Create(ctx, path, desired, nil); err != nil {
			return fmt.Errorf("create model host service %s: %w", name, err)
		}
		return nil
	}
	if err != nil {
		return fmt.Errorf("read model host service %s: %w", name, err)
	}

	// A Service's clusterIP is immutable, so only the parts that may change are sent.
	patch := map[string]any{"spec": map[string]any{
		"selector": desired.Spec["selector"],
		"ports":    desired.Spec["ports"],
	}}
	if err := r.client.MergePatch(ctx, r.servicePath(name), patch, nil); err != nil {
		return fmt.Errorf("update model host service %s: %w", name, err)
	}
	return nil
}

// deleteHost removes the workload for a deployment that is no longer declared.
func (r *Reconciler) deleteHost(ctx context.Context, deploymentID string) error {
	name := "fabric-host-" + strings.ToLower(deploymentID)
	for _, path := range []string{r.deploymentPath(name), r.servicePath(name)} {
		if err := r.client.Delete(ctx, path); err != nil && !kube.IsNotFound(err) {
			return fmt.Errorf("delete %s: %w", path, err)
		}
	}
	return nil
}
