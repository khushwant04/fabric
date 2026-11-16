package agent

import (
	"context"
	"errors"
	"fmt"
	"math"
	"net/http"
	"sort"
	"strings"
	"time"

	"github.com/khushwant04/fabric/agent/internal/activity"
	"github.com/khushwant04/fabric/agent/internal/controlplane"
	"github.com/khushwant04/fabric/agent/internal/hardware"
	"github.com/khushwant04/fabric/agent/internal/hostreport"
)

// The stamp's activity log (internal/activity): what the agent records, and when.
//
// The aim is that right after the bootstrap command an owner sees, on the stamp's page,
// "Enrolled → 1 × NVIDIA RTX PRO 6000 found (96 GiB) → GPU runtime OK → Ready for
// deployments", and afterwards every moment that explains the stamp's state: a credential
// rotation, a desired-state generation applied, a model-host image pulled, a failure with a
// hint. Repeated observations are recorded once and again only when they change.

// deploymentInfo is what the host reporter's goroutine may know about an assignment. The
// reconcile loop owns a.known; this copy is published atomically after each pass.
type deploymentInfo struct {
	Alias      string
	Repository string
}

const (
	gpuRuntimeUnknown = ""
	gpuRuntimeOK      = "ok"
	gpuRuntimeMissing = "missing"
)

// startActivityReporter uploads the activity log on its own goroutine and client, like the
// host reporter, so a slow upload never delays reconciliation.
func (a *Agent) startActivityReporter(ctx context.Context) {
	if !a.config.ReportActivity || a.credentials == nil {
		return
	}
	stampID := a.credentials.StampID
	client := controlplane.New(a.config.ControlPlaneURL, a.config.RequestTimeout)
	client.CredentialSource = a.currentAgentCredential
	go a.activity.Run(ctx, func(ctx context.Context, events []activity.Event) error {
		err := client.ReportEvents(ctx, stampID, events)
		var apiErr *controlplane.APIError
		if errors.As(err, &apiErr) &&
			(apiErr.Status == http.StatusNotFound || apiErr.Status == http.StatusMethodNotAllowed) {
			return fmt.Errorf("%w: %v", activity.ErrNotAccepted, err)
		}
		return err
	})
}

// Activity is the agent's activity log, for callers that record from outside the loop.
func (a *Agent) Activity() *activity.Recorder { return a.activity }

// RepositoryFor names the Hugging Face repository a deployment serves, "" when unknown. It
// is safe to call from any goroutine.
func (a *Agent) RepositoryFor(deploymentID string) string {
	return a.deploymentInfo(deploymentID).Repository
}

func (a *Agent) deploymentInfo(deploymentID string) deploymentInfo {
	infos, _ := a.infos.Load().(map[string]deploymentInfo)
	return infos[deploymentID]
}

// publishDeploymentInfo copies what other goroutines may read about the assignments.
func (a *Agent) publishDeploymentInfo() {
	infos := make(map[string]deploymentInfo, len(a.known))
	for id, entry := range a.known {
		infos[id] = deploymentInfo{Alias: entry.ModelAlias, Repository: entry.UpstreamModel}
	}
	a.infos.Store(infos)
}

func (a *Agent) recordEnrolled(stampID, name, mode string) {
	a.activity.Record(activity.Event{
		Kind:     activity.KindEnrolled,
		Severity: activity.Success,
		Message:  fmt.Sprintf("Enrolled with the control plane as %s", orUnnamed(name)),
		Details:  map[string]any{"stamp_id": stampID, "mode": mode},
	})
}

func (a *Agent) recordIdentityReused(stampID string) {
	a.activity.Record(activity.Event{
		Kind:     activity.KindIdentityReused,
		Severity: activity.Info,
		Message:  "Agent started and reused its enrolled identity; no new token was needed",
		Details:  map[string]any{"stamp_id": stampID},
	})
}

func orID(alias, id string) string {
	if alias != "" {
		return alias
	}
	return id
}

func orUnnamed(name string) string {
	if strings.TrimSpace(name) == "" {
		return "an unnamed stamp"
	}
	return name
}

// recordCapacity turns a measurement into "GPUs measured" and "GPU runtime" entries.
func (a *Agent) recordCapacity(capacity hardware.Capacity, err error) {
	if err != nil && !capacity.Measured {
		a.activity.RecordChange("capacity-error", err.Error(), activity.Event{
			Kind:     activity.KindError,
			Severity: activity.Error,
			Message:  "Could not read this cluster's GPU nodes: " + err.Error(),
			Hint:     "The agent needs read access to nodes and pods; check the chart's ClusterRole.",
		})
		return
	}
	a.activity.Forget("capacity-error")

	groups := append([]hardware.GPUGroup(nil), capacity.GPUs...)
	sort.Slice(groups, func(i, j int) bool { return groups[i].Product < groups[j].Product })
	parts := make([]string, 0, len(groups))
	details := map[string]any{"allocatable_gpus": capacity.AllocatableGPUs}
	for index, group := range groups {
		parts = append(parts, describeGPU(group))
		if index == 0 {
			details["product"] = group.Product
			details["count"] = group.Count
			if group.MemoryBytes > 0 {
				details["memory_gib"] = math.Round(float64(group.MemoryBytes)/(1<<30)*10) / 10
			}
			if group.ComputeCapability != "" {
				details["compute_capability"] = group.ComputeCapability
			}
		}
	}
	if len(groups) > 1 {
		details["gpu_classes"] = len(groups)
	}
	if len(parts) > 0 {
		a.activity.RecordChange("gpus", strings.Join(parts, "|"), activity.Event{
			Kind:     activity.KindGPUsMeasured,
			Severity: activity.Info,
			Message:  strings.Join(parts, ", "),
			Details:  details,
		})
	}

	state := gpuRuntimeMissing
	if capacity.AllocatableGPUs > 0 {
		state = gpuRuntimeOK
	}
	a.gpuRuntime = state
	if state == gpuRuntimeOK {
		a.activity.RecordChange("gpu-runtime", state, activity.Event{
			Kind:     activity.KindGPURuntime,
			Severity: activity.Success,
			Message: fmt.Sprintf("GPU runtime OK: %d nvidia.com/gpu allocatable",
				capacity.AllocatableGPUs),
			Details: map[string]any{"allocatable_gpus": capacity.AllocatableGPUs, "ok": true},
		})
		return
	}
	// Ready is recorded again once the runtime is fixed.
	a.activity.Forget("ready")
	message := "GPU runtime missing: no node advertises allocatable nvidia.com/gpu"
	hint := "No GPU node matched. Check the NVIDIA driver with nvidia-smi, the NVIDIA device " +
		"plugin, and gpu.nodeSelector in the stamp chart."
	if capacity.UnequippedGPUNodes > 0 {
		message = fmt.Sprintf("GPU runtime missing: %d GPU node(s) advertise no allocatable nvidia.com/gpu",
			capacity.UnequippedGPUNodes)
		hint = "The NVIDIA device plugin is not running or cannot reach the driver. On k3s, " +
			"re-run the bootstrap command; otherwise check nvidia-smi on the node and the " +
			"nvidia-device-plugin pods in kube-system. Models fail with \"Failed to infer device " +
			"type\" until this is fixed."
	}
	a.activity.RecordChange("gpu-runtime", state, activity.Event{
		Kind:     activity.KindGPURuntime,
		Severity: activity.Warning,
		Message:  message,
		Hint:     hint,
		Details: map[string]any{
			"allocatable_gpus": 0, "ok": false, "unequipped_nodes": capacity.UnequippedGPUNodes,
		},
	})
}

// describeGPU reads like "1 × NVIDIA RTX PRO 6000 Blackwell Server Edition found (96 GiB)".
func describeGPU(group hardware.GPUGroup) string {
	product := group.Product
	if product == "" || product == "unknown" {
		product = "unidentified GPU"
	}
	text := fmt.Sprintf("%d × %s found", group.Count, product)
	if group.MemoryBytes > 0 {
		text += fmt.Sprintf(" (%.0f GiB)", float64(group.MemoryBytes)/(1<<30))
	}
	return text
}

// recordReadiness says the stamp is ready once it heartbeats and, when it can measure, its
// GPU runtime works.
func (a *Agent) recordReadiness() {
	if a.config.Capacity != nil && a.gpuRuntime != gpuRuntimeOK {
		return
	}
	details := map[string]any{}
	if a.measured.Measured {
		details["allocatable_gpus"] = a.measured.AllocatableGPUs
	}
	a.activity.RecordChange("ready", "ready", activity.Event{
		Kind:     activity.KindReady,
		Severity: activity.Success,
		Message:  "Ready for deployments",
		Details:  details,
	})
}

func (a *Agent) recordRotation(reason string, version int, err error) {
	if err != nil {
		a.activity.RecordChange("rotation-error", err.Error(), activity.Event{
			Kind:     activity.KindError,
			Severity: activity.Error,
			Message:  "Credential rotation failed; the current credentials keep working: " + err.Error(),
			Hint:     "The agent retries on its next heartbeat. Revoke the stamp instead if a credential leaked.",
		})
		return
	}
	a.activity.Forget("rotation-error")
	a.activity.Record(activity.Event{
		Kind:     activity.KindCredentialsRotated,
		Severity: activity.Success,
		Message:  fmt.Sprintf("Rotated machine credentials (version %d, %s)", version, rotationLabel(reason)),
		Details:  map[string]any{"credential_version": version, "reason": reason},
	})
}

func rotationLabel(reason string) string {
	switch reason {
	case "requested":
		return "requested from the console"
	case "max_age":
		return "scheduled by age"
	}
	return reason
}

func (a *Agent) recordApplied(generation int, changes []string) {
	message := fmt.Sprintf("Applied desired-state generation %d", generation)
	if len(changes) > 0 {
		shown := changes
		if len(shown) > 3 {
			shown = append(append([]string(nil), shown[:3]...), fmt.Sprintf("%d more", len(changes)-3))
		}
		message += ": " + strings.Join(shown, ", ")
	}
	a.activity.Record(activity.Event{
		Kind:     activity.KindDesiredStateApplied,
		Severity: activity.Info,
		Message:  message,
		Details:  map[string]any{"generation": generation, "changes": len(changes)},
	})
}

func (a *Agent) recordReconcileError(err error) {
	a.activity.RecordChange("reconcile-error", err.Error(), activity.Event{
		Kind:     activity.KindError,
		Severity: activity.Error,
		Message:  "Reconcile failed, retrying: " + err.Error(),
	})
}

// observeHosts records image pulls and failing hosts from a host report. Pod events can be
// re-read for an hour, so each is keyed by pod, phase and time and recorded once.
func (a *Agent) observeHosts(report hostreport.Report) {
	for _, deployment := range report.Deployments {
		info := a.deploymentInfo(deployment.DeploymentID)
		label := info.Alias
		if label == "" {
			label = "a deployment"
		}
		for _, pod := range deployment.Pods {
			for _, pull := range hostreport.ImagePulls(pod) {
				a.recordPull(deployment.DeploymentID, label, pod.Name, pull)
			}
			if diagnosis, failing := hostreport.Diagnose(pod); failing {
				a.activity.RecordChange("fail/"+pod.Name+"/"+diagnosis.Reason, "1", activity.Event{
					Kind:     activity.KindModelHostFailed,
					Severity: activity.Error,
					Message:  fmt.Sprintf("Model host for %s failed: %s", label, diagnosis.Message),
					Hint:     diagnosis.Hint,
					Details: map[string]any{
						"deployment_id": deployment.DeploymentID, "pod": pod.Name,
						"reason": diagnosis.Reason, "restarts": pod.RestartCount,
					},
				})
			}
		}
	}
}

func (a *Agent) recordPull(deploymentID, label, podName string, pull hostreport.ImagePull) {
	details := map[string]any{"deployment_id": deploymentID, "pod": podName}
	if pull.Image != "" {
		details["image"] = pull.Image
	}
	key := "pull/" + podName + "/" + pull.Phase
	state := pull.At.UTC().Format(time.RFC3339)
	event := activity.Event{Details: details, At: pull.At.UTC()}
	switch pull.Phase {
	case "started":
		event.Kind = activity.KindImagePullStarted
		event.Severity = activity.Info
		event.Message = fmt.Sprintf("Pulling the model runtime image for %s", label)
		event.Hint = "The first deployment on this machine downloads the model runtime once; later deployments start from the local copy."
	case "finished":
		event.Kind = activity.KindImagePullFinished
		event.Severity = activity.Success
		event.Message = "Model runtime image pulled"
		if pull.Duration > 0 {
			event.Message += " in " + pull.Duration.Round(time.Second).String()
			details["seconds"] = math.Round(pull.Duration.Seconds())
		}
		if pull.SizeBytes > 0 {
			event.Message += " (" + formatBytes(pull.SizeBytes) + ")"
			details["size_bytes"] = pull.SizeBytes
		}
	case "failed":
		event.Kind = activity.KindModelHostFailed
		event.Severity = activity.Error
		event.Message = fmt.Sprintf("Could not pull the model runtime image for %s", label)
		event.Hint = "Check that this machine can reach the image registry and that the image tag exists."
		details["reason"] = "ImagePullFailed"
	default:
		// Already present: unremarkable, and it would repeat for every new pod.
		return
	}
	a.activity.RecordChange(key, state, event)
}

// formatBytes uses decimal units, as registries and the kubelet report image sizes.
func formatBytes(size int64) string {
	switch {
	case size >= 1e9:
		return fmt.Sprintf("%.1f GB", float64(size)/1e9)
	case size >= 1e6:
		return fmt.Sprintf("%.0f MB", float64(size)/1e6)
	}
	return fmt.Sprintf("%d bytes", size)
}

// withoutWeights copies a report with weight progress removed, for a control plane that
// predates the field and rejects unknown fields.
func withoutWeights(report hostreport.Report) (hostreport.Report, bool) {
	stripped := false
	copied := hostreport.Report{ObservedAt: report.ObservedAt, Deployments: make([]hostreport.Deployment, 0, len(report.Deployments))}
	for _, deployment := range report.Deployments {
		pods := make([]hostreport.Pod, len(deployment.Pods))
		for index, pod := range deployment.Pods {
			if pod.Weights != nil {
				pod.Weights = nil
				stripped = true
			}
			pods[index] = pod
		}
		copied.Deployments = append(copied.Deployments, hostreport.Deployment{DeploymentID: deployment.DeploymentID, Pods: pods})
	}
	return copied, stripped
}
