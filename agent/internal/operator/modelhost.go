package operator

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"net"
	"sort"
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
	// CacheMode decides where downloaded weights live: "hostPath" for the node's own
	// disk, "pvc" for a PersistentVolumeClaim, or "none" to refetch on every start.
	//
	// A node-local path is the default because it matches how weights behave. They are
	// large, immutable, and only useful to a pod already scheduled on that node, and a
	// GPU node's ephemeral disk is both fast and already paid for. A single shared claim
	// was the previous behaviour and was wrong: ReadWriteOnce cannot attach to two nodes,
	// so the second host on a second node would never start.
	// KernelBlockV and KernelNumWarps override the Fabric kernel's tiling when it is used.
	KernelBlockV   int
	KernelNumWarps int
	CacheMode      string
	// CacheHostPath is the directory used when CacheMode is "hostPath". On AKS the
	// ephemeral disk is mounted at /mnt, which is why the default lives under it.
	CacheHostPath string
	// CacheClaim is the claim used when CacheMode is "pvc". It must be ReadWriteMany if
	// more than one host will mount it.
	CacheClaim string
	// SpreadAcrossNodes keeps hosts on different nodes, so two deployments on one stamp
	// do not contend for a single device while another node sits idle.
	SpreadAcrossNodes bool
}

// ParseNodeSelector reads repeated key=value flags into a selector.
func ParseNodeSelector(entries []string) (map[string]string, error) {
	selector := map[string]string{}
	for _, entry := range entries {
		key, value, found := strings.Cut(entry, "=")
		if !found || key == "" {
			return nil, fmt.Errorf("node selector %q must be key=value", entry)
		}
		selector[key] = value
	}
	return selector, nil
}

// ParseTolerations reads repeated key[=value]:effect flags.
//
// A GPU node is usually tainted so only workloads that ask for a device land on it,
// which means the host cannot schedule at all without a matching toleration. Parsing is
// strict because a silently ignored toleration looks like a scheduler problem later.
func ParseTolerations(entries []string) ([]map[string]any, error) {
	tolerations := make([]map[string]any, 0, len(entries))
	for _, entry := range entries {
		spec, effect, found := strings.Cut(entry, ":")
		if !found || spec == "" {
			return nil, fmt.Errorf("toleration %q must be key[=value]:effect", entry)
		}
		key, value, hasValue := strings.Cut(spec, "=")
		toleration := map[string]any{"key": key, "effect": effect}
		if hasValue && value != "" {
			toleration["operator"] = "Equal"
			toleration["value"] = value
		} else {
			// Exists rather than Equal: a taint with no value is the common shape for
			// "this node has a GPU", and Equal with an empty value would not match it.
			toleration["operator"] = "Exists"
		}
		tolerations = append(tolerations, toleration)
	}
	return tolerations, nil
}

// Enabled reports whether the operator should manage a host at all.
//
// Without an image there is nothing to run, and the operator falls back to
// configuring the data plane against an upstream someone else operates. That is the
// behaviour every existing deployment relies on, so it stays the default.
func (m ModelHost) Enabled() bool {
	return m.Image != ""
}

// startupFailureThreshold allows 20 minutes at a 10s period. A cold model on a small
// GPU spends most of that compiling, and the cost of being generous is only a slower
// report of a genuinely broken server, while the cost of being strict is killing a
// working one.
const startupFailureThreshold = 120

// kernelEnvironment turns a deployment's requested kernel into the host's environment.
//
// Only an explicit request for the Fabric kernel switches it on. "auto" is deliberately
// conservative and serves the model server's own kernel, because choosing between them
// automatically would need a policy grounded in measurements this platform does not yet
// hold for every shape it might serve. A deployment that wants the substitution says so.
func (m ModelHost) kernelEnvironment(mode string) []map[string]any {
	if mode != "fabric" {
		return nil
	}
	env := []map[string]any{{"name": "FABRIC_KERNEL", "value": "1"}}
	// Passed through so the kernel's tiling can be measured against a running host
	// without building an image for every shape. The best shape depends on how the host
	// runs the kernel, not only on the GPU: captured into a CUDA graph there is no launch
	// to overlap, and a shape tuned with launches in view loses in service.
	if m.KernelBlockV > 0 {
		env = append(env, map[string]any{"name": "FABRIC_KERNEL_BLOCK_V", "value": strconv.Itoa(m.KernelBlockV)})
	}
	if m.KernelNumWarps > 0 {
		env = append(env, map[string]any{"name": "FABRIC_KERNEL_NUM_WARPS", "value": strconv.Itoa(m.KernelNumWarps)})
	}
	return env
}

// modelCacheMountPath is where weights are visible inside the container.
const modelCacheMountPath = "/model-cache"

// DefaultCacheHostPath is on the AKS ephemeral disk, which is local NVMe on GPU SKUs.
const DefaultCacheHostPath = "/mnt/fabric/model-cache"

// cacheVolume builds the weight cache according to the configured mode.
func (m ModelHost) cacheVolume() map[string]any {
	switch m.CacheMode {
	case "pvc":
		if m.CacheClaim != "" {
			return map[string]any{
				"name":                  "cache",
				"persistentVolumeClaim": map[string]any{"claimName": m.CacheClaim},
			}
		}
	case "none":
		// Deliberate: refetch on every start. Predictable, and correct where no local
		// disk exists, at the cost of the download.
		return map[string]any{"name": "cache", "emptyDir": map[string]any{}}
	case "hostPath", "":
		path := m.CacheHostPath
		if path == "" {
			path = DefaultCacheHostPath
		}
		return map[string]any{
			"name": "cache",
			"hostPath": map[string]any{
				"path": path,
				// Created on first use, since a fresh node has no such directory and
				// requiring one would make the host fail to start on a new node.
				"type": "DirectoryOrCreate",
			},
		}
	}
	return map[string]any{"name": "cache", "emptyDir": map[string]any{}}
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
//
// This is the *primary* (old) workload during a rollout and the only workload the rest
// of the time. Its name does not carry the release, so the release the deployment settles
// on keeps the stable name and a coexisting new workload (see hostNameForRelease) is the
// one that is torn down.
func hostName(item ModelDeployment) string {
	return "fabric-host-" + strings.ToLower(item.Spec.DeploymentID)
}

// releaseSuffix is a short, stable, DNS-safe token derived from a release name, so two
// releases of one deployment get distinct workload and Service names without exceeding
// Kubernetes' 63-character limit or depending on the release string being label-safe.
func releaseSuffix(release string) string {
	sum := sha256.Sum256([]byte(release))
	return hex.EncodeToString(sum[:])[:8]
}

// hostNameForRelease names the *coexisting* new workload during a weighted rollout. It is
// distinct from hostName so the old and new releases run as separate Deployments and
// Services on their own GPUs, and it is derived from the release so the same release
// always resolves to the same workload across reconciles (idempotent create/patch).
func hostNameForRelease(item ModelDeployment, release string) string {
	return hostName(item) + "-" + releaseSuffix(release)
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

// desiredHost builds the primary workload for a deployment, named by hostName.
func (r *Reconciler) desiredHost(item ModelDeployment) deployment {
	return r.desiredHostNamed(item, hostName(item))
}

// desiredHostNamed builds a model-host Deployment under an explicit workload name.
//
// During a weighted rollout the new release runs as a *separate* workload beside the old
// (ADR 0011), so the name is a parameter rather than always hostName. A per-workload
// label (fabric.khushwant.dev/host) is added to the pod template and the selector so each
// workload's headless Service selects only its own pods, and the two releases do not end
// up in one another's backend pool.
func (r *Reconciler) desiredHostNamed(item ModelDeployment, name string) deployment {
	host := r.options.ModelHost
	// The deployment's own device count, which is what the control plane admitted the
	// placement against (ADR 0013). Falls back to the stamp's configured count for a
	// declaration that predates the field.
	host.GPUs = item.Spec.DesiredGPUs(r.options.ModelHost.GPUs)
	release := releaseOf(item)
	if release != "" {
		// Runtime release is the immutable artifact/address the host loads and the name
		// it serves. Preserve the configured local model path only for its matching
		// initial served name; every changed release must alter process content, not just
		// workload labels.
		host.ServedName = release
		if release != r.options.ModelHost.ServedName || host.ModelRef == "" {
			host.ModelRef = release
		}
	}
	appName := "fabric-model-host"
	selector := map[string]string{
		"fabric.khushwant.dev/deployment-id": item.Spec.DeploymentID,
		"app.kubernetes.io/name":             appName,
	}
	if name != hostName(item) {
		// The legacy stable workload's selector is immutable and selects the deployment
		// id plus app name. Candidates use a distinct app name and exact host label, so
		// they never collide with an installed pre-M3 Service or ReplicaSet.
		appName = "fabric-model-host-release"
		selector = map[string]string{
			"fabric.khushwant.dev/host": name,
			"app.kubernetes.io/name":    appName,
		}
	}
	labels := map[string]string{
		"app.kubernetes.io/name":             appName,
		"app.kubernetes.io/managed-by":       hostManagedBy,
		"app.kubernetes.io/part-of":          "fabric",
		"fabric.khushwant.dev/deployment-id": item.Spec.DeploymentID,
		"fabric.khushwant.dev/account-id":    item.Spec.AccountID,
		"fabric.khushwant.dev/host":          name,
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
		// A startup probe rather than a long liveness delay. Guessing how long a model
		// server needs is how a healthy server gets killed: a fixed delay plus a failure
		// threshold is a deadline, and a T4 compiling graphs for a cold model overran it,
		// so the container was restarted mid-startup and lost the compilation it had
		// done. A startup probe gives it a generous window to answer once, and liveness
		// only begins after that succeeds.
		"startupProbe": map[string]any{
			"httpGet":          map[string]any{"path": "/health", "port": "http"},
			"periodSeconds":    10,
			"failureThreshold": startupFailureThreshold,
		},
		"livenessProbe": map[string]any{
			"httpGet":          map[string]any{"path": "/health", "port": "http"},
			"periodSeconds":    30,
			"failureThreshold": 6,
		},
		"volumeMounts": []map[string]any{
			// A model server needs writable scratch and shared memory; the image is
			// otherwise treated as immutable.
			{"name": "cache", "mountPath": modelCacheMountPath},
			{"name": "shm", "mountPath": "/dev/shm"},
		},
		// Pointed at the mount explicitly rather than relying on the image's default
		// cache location, which differs between images and would silently put a
		// multi-gigabyte download on the container filesystem.
		"env": append(host.kernelEnvironment(item.Spec.KernelMode), []map[string]any{
			{"name": "HF_HOME", "value": modelCacheMountPath},
			{"name": "HF_HUB_CACHE", "value": modelCacheMountPath + "/hub"},
			// Compiled graphs belong on the node's disk for the same reason weights do:
			// they are expensive to produce and identical on every start. Left on the
			// container filesystem, a restart recompiles from scratch, which is what
			// turned one overrunning startup into a loop of them.
			{"name": "VLLM_CACHE_ROOT", "value": modelCacheMountPath + "/vllm"},
			{"name": "TORCHINDUCTOR_CACHE_DIR", "value": modelCacheMountPath + "/inductor"},
		}...),
	}

	volumes := []map[string]any{
		{"name": "shm", "emptyDir": map[string]any{"medium": "Memory"}},
		host.cacheVolume(),
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
	if host.SpreadAcrossNodes {
		// Anti-affinity between model hosts, not a hard constraint: on a single-node
		// stamp a required rule would leave the second deployment permanently Pending,
		// which is worse than sharing a node.
		podSpec["affinity"] = map[string]any{
			"podAntiAffinity": map[string]any{
				"preferredDuringSchedulingIgnoredDuringExecution": []map[string]any{
					{
						"weight": 100,
						"podAffinityTerm": map[string]any{
							"topologyKey": "kubernetes.io/hostname",
							"labelSelector": map[string]any{
								"matchLabels": map[string]string{
									"app.kubernetes.io/name": "fabric-model-host",
								},
							},
						},
					},
				},
			},
		}
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
			// One pod per replica, each holding one GPU: a fleet is expressed by asking
			// for more than one replica (ADR 0010), and the data plane balances across
			// them. Defaults to one when the deployment does not ask, so a deployment
			// that predates the field is unchanged.
			"replicas": item.Spec.DesiredReplicas(),
			"selector": map[string]any{
				"matchLabels": selector,
			},
			"strategy": map[string]any{
				// Recreate within a single workload: its own replicas each want a GPU, so
				// a rolling update would deadlock waiting for one to free. A release
				// *change* no longer recreates this workload in place when the stamp has
				// spare capacity; the new release comes up as a separate workload beside
				// it and traffic shifts by weight (ADR 0011). Recreate is still correct
				// per workload, and is the whole story on a stamp that cannot fit both.
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
	return r.desiredHostServiceNamed(item, hostName(item))
}

// desiredHostServiceNamed builds the headless Service for one workload, selecting only
// that workload's pods (fabric.khushwant.dev/host) so a coexisting rollout's two Services
// expose their own release's endpoints rather than the union.
func (r *Reconciler) desiredHostServiceNamed(item ModelDeployment, name string) service {
	appName := "fabric-model-host"
	selector := map[string]string{
		"fabric.khushwant.dev/deployment-id": item.Spec.DeploymentID,
		"app.kubernetes.io/name":             appName,
	}
	if name != hostName(item) {
		appName = "fabric-model-host-release"
		selector = map[string]string{
			"fabric.khushwant.dev/host": name,
			"app.kubernetes.io/name":    appName,
		}
	}
	return service{
		APIVersion: "v1",
		Kind:       "Service",
		Metadata: Metadata{
			Name:      name,
			Namespace: r.options.Namespace,
			Labels: map[string]string{
				"app.kubernetes.io/name":             appName,
				"app.kubernetes.io/managed-by":       hostManagedBy,
				"fabric.khushwant.dev/deployment-id": item.Spec.DeploymentID,
				"fabric.khushwant.dev/host":          name,
			},
		},
		Spec: map[string]any{
			// Headless (clusterIP: None) so the pod endpoints are individually
			// discoverable rather than hidden behind one virtual IP (ADR 0010). The data
			// plane balances across the concrete backends the operator publishes; it does
			// not rely on kube-proxy spreading a ClusterIP, which would be opaque to it
			// and could not be ejected when one replica fails.
			"clusterIP": "None",
			"selector":  selector,
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
	return r.hostUpstreamNamed(hostName(item))
}

// hostUpstreamNamed is the Service DNS address for one named workload.
func (r *Reconciler) hostUpstreamNamed(name string) string {
	return fmt.Sprintf(
		"http://%s.%s.svc:%d", name, r.options.Namespace, r.options.ModelHost.Port,
	)
}

// endpointSlice is the subset of a discovery.k8s.io EndpointSlice the operator reads.
type endpointSlice struct {
	AddressType string                  `json:"addressType,omitempty"`
	Endpoints   []endpointSliceEndpoint `json:"endpoints"`
}

type endpointSliceEndpoint struct {
	Addresses  []string `json:"addresses"`
	Conditions struct {
		// A nil Ready is treated as ready, matching Kubernetes: an unset condition on
		// a slice means the endpoint's readiness is not being reported, not that it
		// is unready.
		Ready *bool `json:"ready,omitempty"`
	} `json:"conditions"`
	// TargetRef identifies the pod independently of its current address. The UID is
	// used as the backend id when Kubernetes supplies it, so metrics and health retain
	// the same identity while the operator refreshes a changed dial address.
	TargetRef *struct {
		Kind      string `json:"kind,omitempty"`
		Namespace string `json:"namespace,omitempty"`
		Name      string `json:"name,omitempty"`
		UID       string `json:"uid,omitempty"`
	} `json:"targetRef,omitempty"`
}

type endpointSliceList struct {
	Items []endpointSlice `json:"items"`
}

type discoveredBackend struct {
	backend  dataPlaneBackend
	priority int
}

func endpointAddressPriority(addressType string) int {
	switch addressType {
	case "IPv4":
		return 0
	case "IPv6":
		return 1
	case "FQDN":
		return 2
	default:
		return 3
	}
}

func endpointBackendID(endpoint endpointSliceEndpoint, address string) string {
	if endpoint.TargetRef == nil {
		return address
	}
	if endpoint.TargetRef.UID != "" {
		return endpoint.TargetRef.UID
	}
	if endpoint.TargetRef.Name != "" {
		if endpoint.TargetRef.Namespace != "" {
			return endpoint.TargetRef.Namespace + "/" + endpoint.TargetRef.Name
		}
		return endpoint.TargetRef.Name
	}
	return address
}

// discoverBackends resolves the concrete pool for a deployment (ADR 0010).
//
// It lists the EndpointSlices the headless Service selects and turns each ready pod
// address into a backend. When no ready endpoint is resolvable yet, for example while
// the pods are still starting, it falls back to the Service's own DNS name as a single
// backend so the data plane is never handed an empty pool and can queue behind
// readiness rather than fail. Discovery is on the reconcile path, not the request path,
// so a slow or failed lookup degrades to the Service address instead of stalling a
// request.
func (r *Reconciler) discoverBackends(ctx context.Context, item ModelDeployment) []dataPlaneBackend {
	return r.discoverBackendsNamed(ctx, item, hostName(item))
}

// discoverBackendsNamed resolves the pool for one named workload from the EndpointSlices
// its headless Service selects. During a weighted rollout it is called once per release's
// workload so each release's concrete endpoints can be published with that release's
// weight.
func (r *Reconciler) discoverBackendsNamed(
	ctx context.Context, item ModelDeployment, name string,
) []dataPlaneBackend {
	fallbackURL := r.hostUpstreamNamed(name)
	fallback := []dataPlaneBackend{{URL: fallbackURL, ID: fallbackURL}}

	selector := fmt.Sprintf("kubernetes.io/service-name%%3D%s", name)
	path := fmt.Sprintf(
		"/apis/discovery.k8s.io/v1/namespaces/%s/endpointslices?labelSelector=%s",
		r.options.Namespace, selector,
	)
	var list endpointSliceList
	if err := r.client.Get(ctx, path, &list); err != nil {
		// Not fatal: the Service DNS name still reaches the ready pods through the
		// cluster's own resolution, so the deployment is served while discovery recovers.
		r.options.Log.Warn("could not read endpoints; publishing the service address",
			"deployment", item.Spec.DeploymentID, "error", err)
		return fallback
	}

	// EndpointSlices can contain the same pod in more than one address family. Keep one
	// deterministic dial address per pod identity, preferring IPv4 then IPv6 then FQDN.
	// Without a targetRef the address itself is the only truthful identity available.
	byID := map[string]discoveredBackend{}
	for _, slice := range list.Items {
		priority := endpointAddressPriority(slice.AddressType)
		for _, endpoint := range slice.Endpoints {
			if endpoint.Conditions.Ready != nil && !*endpoint.Conditions.Ready {
				continue
			}
			for _, address := range endpoint.Addresses {
				if address == "" {
					continue
				}
				id := endpointBackendID(endpoint, address)
				candidate := discoveredBackend{
					backend: dataPlaneBackend{
						URL: "http://" + net.JoinHostPort(
							address, strconv.Itoa(r.options.ModelHost.Port),
						),
						ID: id,
					},
					priority: priority,
				}
				existing, exists := byID[id]
				if !exists || candidate.priority < existing.priority ||
					(candidate.priority == existing.priority &&
						candidate.backend.URL < existing.backend.URL) {
					byID[id] = candidate
				}
			}
		}
	}

	if len(byID) == 0 {
		return fallback
	}
	backends := make([]dataPlaneBackend, 0, len(byID))
	for _, candidate := range byID {
		backends = append(backends, candidate.backend)
	}
	// Sorted so an unchanged endpoint set renders an identical document and the data
	// plane does not reload for a reordering.
	sort.Slice(backends, func(i, j int) bool { return backends[i].ID < backends[j].ID })
	return backends
}

func (r *Reconciler) discoverConcreteBackendsNamed(
	ctx context.Context, item ModelDeployment, name string,
) ([]dataPlaneBackend, bool) {
	backends := r.discoverBackendsNamed(ctx, item, name)
	if len(backends) == 1 && backends[0].URL == r.hostUpstreamNamed(name) {
		return backends, false
	}
	return backends, len(backends) > 0
}

type modelHostReadiness struct {
	Ready   int
	Desired int
}

func (s modelHostReadiness) fullyReady() bool {
	return s.Desired > 0 && s.Ready >= s.Desired
}

// rolloutBackends resolves the concrete pool the data plane should serve for a
// deployment this pass, and the strategy it must use.
func (r *Reconciler) rolloutBackends(
	ctx context.Context, item ModelDeployment, decision RolloutDecision,
) ([]dataPlaneBackend, string) {
	if len(decision.Weighted) == 0 {
		return nil, ""
	}
	pool := make([]dataPlaneBackend, 0)
	for _, release := range decision.Weighted {
		endpoints := r.discoverBackendsNamed(ctx, item, release.Workload)
		if release.Workload == decision.CandidateWorkload && release.Weight > 0 {
			var concrete bool
			endpoints, concrete = r.discoverConcreteBackendsNamed(ctx, item, release.Workload)
			if !concrete {
				// The exact pool being serialized lost readiness after policy observation.
				// Abort publication so the previously loaded active-only route remains.
				return nil, ""
			}
		}
		if len(endpoints) == 0 {
			continue
		}
		per := release.Weight / float64(len(endpoints))
		for _, backend := range endpoints {
			backend.Workload = release.Workload
			if decision.ForceWeighted || len(decision.Weighted) > 1 {
				weight := per
				backend.Weight = &weight
			}
			pool = append(pool, backend)
		}
	}
	sort.Slice(pool, func(i, j int) bool { return pool[i].ID < pool[j].ID })
	if decision.ForceWeighted || len(decision.Weighted) > 1 {
		return pool, "weighted"
	}
	return pool, ""
}

// applyHost creates or updates the workload for one deployment and reports how many
// replicas are ready against how many were requested.
func (r *Reconciler) applyHost(
	ctx context.Context, item ModelDeployment,
) (modelHostReadiness, error) {
	return r.applyHostWithRollout(ctx, item, RolloutDecision{
		ActiveRelease: releaseOf(item), ActiveWorkload: hostName(item), EnsureActive: true,
	}, rolloutState{})
}

// itemForRelease returns the deployment with its upstream release overridden, so a
// workload can be built for a release other than the one declared (the sidecar keeps the
// previous release, the primary during a rollback returns to the last-good one).
func itemForRelease(item ModelDeployment, release string) ModelDeployment {
	effective := item
	if release != "" && release != releaseOf(item) {
		effective.Spec.UpstreamModel = release
	}
	return effective
}

// ensureNamedWorkload creates or updates one exact release workload and its Service,
// then returns observed replica readiness. It never mutates another release.
func (r *Reconciler) ensureNamedWorkload(
	ctx context.Context, item ModelDeployment, release, name string,
) (modelHostReadiness, error) {
	readiness := modelHostReadiness{Desired: item.Spec.DesiredReplicas()}
	desired := r.desiredHostNamed(itemForRelease(item, release), name)
	desired.Metadata.Annotations = map[string]string{annotationRelease: release}
	if err := r.applyWorkload(ctx, name, desired); err != nil {
		return readiness, err
	}
	if err := r.applyHostServiceNamed(ctx, item, name); err != nil {
		return readiness, err
	}
	return r.observeNamedWorkload(ctx, item, name)
}

func (r *Reconciler) observeReleaseWorkload(
	ctx context.Context, item ModelDeployment, name string,
) (bool, string, modelHostReadiness, error) {
	readiness := modelHostReadiness{Desired: item.Spec.DesiredReplicas()}
	var current deployment
	if err := r.client.Get(ctx, r.deploymentPath(name), &current); err != nil {
		if kube.IsNotFound(err) {
			return false, "", readiness, nil
		}
		return false, "", readiness, fmt.Errorf("read model host status %s: %w", name, err)
	}
	if current.Status != nil {
		readiness.Ready = current.Status.ReadyReplicas
	}
	return true, current.Metadata.Annotations[annotationRelease], readiness, nil
}

func (r *Reconciler) observeNamedWorkload(
	ctx context.Context, item ModelDeployment, name string,
) (modelHostReadiness, error) {
	readiness := modelHostReadiness{Desired: item.Spec.DesiredReplicas()}
	var current deployment
	if err := r.client.Get(ctx, r.deploymentPath(name), &current); err != nil {
		if kube.IsNotFound(err) {
			return readiness, nil
		}
		return readiness, fmt.Errorf("read model host status %s: %w", name, err)
	}
	if current.Status != nil {
		readiness.Ready = current.Status.ReadyReplicas
	}
	return readiness, nil
}

// applyHostWithRollout remains the single-host compatibility path used by focused tests;
// ReconcileOnce uses the durable active/candidate state machine directly.
func (r *Reconciler) applyHostWithRollout(
	ctx context.Context, item ModelDeployment, decision RolloutDecision, _ rolloutState,
) (modelHostReadiness, error) {
	release := decision.ActiveRelease
	workload := decision.ActiveWorkload
	if release == "" {
		release = releaseOf(item)
	}
	if workload == "" {
		workload = hostName(item)
	}
	return r.ensureNamedWorkload(ctx, item, release, workload)
}

// applyWorkload creates or patches one model-host Deployment.
func (r *Reconciler) applyWorkload(ctx context.Context, name string, desired deployment) error {
	var existing deployment
	err := r.client.Get(ctx, r.deploymentPath(name), &existing)
	switch {
	case kube.IsNotFound(err):
		path := fmt.Sprintf("/apis/apps/v1/namespaces/%s/deployments", r.options.Namespace)
		if err := r.client.Create(ctx, path, desired, nil); err != nil {
			return fmt.Errorf("create model host %s: %w", name, err)
		}
		r.options.Log.Info("model host created", "deployment", name)
		return nil
	case err != nil:
		return fmt.Errorf("read model host %s: %w", name, err)
	default:
		// Patched rather than replaced, so fields defaulted by the API server and
		// anything a cluster admission controller added are left alone.
		patch := map[string]any{
			"spec": desired.Spec,
			"metadata": map[string]any{
				"labels":      desired.Metadata.Labels,
				"annotations": desired.Metadata.Annotations,
			},
		}
		if err := r.client.MergePatch(ctx, r.deploymentPath(name), patch, nil); err != nil {
			return fmt.Errorf("update model host %s: %w", name, err)
		}
		return nil
	}
}

func (r *Reconciler) applyHostService(ctx context.Context, item ModelDeployment) error {
	return r.applyHostServiceNamed(ctx, item, hostName(item))
}

// applyHostServiceNamed creates or patches the headless Service for one named workload.
func (r *Reconciler) applyHostServiceNamed(ctx context.Context, item ModelDeployment, name string) error {
	desired := r.desiredHostServiceNamed(item, name)

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

	// Services created before ADR 0010 have an allocated ClusterIP. clusterIP is
	// immutable, so patching selector/ports cannot make them headless; recreate the
	// operator-owned Service in place. Pods are untouched, and the data plane keeps its
	// previously published concrete endpoint addresses during this short migration.
	if clusterIP, _ := existing.Spec["clusterIP"].(string); clusterIP != "" && clusterIP != "None" {
		if err := r.client.Delete(ctx, r.servicePath(name)); err != nil && !kube.IsNotFound(err) {
			return fmt.Errorf("delete legacy model host service %s: %w", name, err)
		}
		path := fmt.Sprintf("/api/v1/namespaces/%s/services", r.options.Namespace)
		if err := r.client.Create(ctx, path, desired, nil); err != nil {
			return fmt.Errorf("recreate model host service %s as headless: %w", name, err)
		}
		r.options.Log.Info("model host service migrated to headless", "service", name)
		return nil
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

// deleteWorkload removes one workload and its Service by name, tolerating a missing one.
func (r *Reconciler) deleteWorkload(ctx context.Context, name string) error {
	for _, path := range []string{r.deploymentPath(name), r.servicePath(name)} {
		if err := r.client.Delete(ctx, path); err != nil && !kube.IsNotFound(err) {
			return fmt.Errorf("delete %s: %w", path, err)
		}
	}
	return nil
}
