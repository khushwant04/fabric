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
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
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

// ModelCapabilities is the bounded routing metadata carried from desired state to
// the data-plane configuration. Snake-case nested names match the public deployment
// specification and keep the direct and operator delivery paths identical.
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
	// KernelMode selects which decode kernel the model host uses: "fabric" for the
	// Fabric substitution, "standard" for the server's own, "auto" to let the platform
	// decide. It belongs on the deployment rather than the stamp because it is a
	// property of what is being served, and because two deployments on one stamp
	// differing only in this is how the two are compared.
	KernelMode string `json:"kernelMode,omitempty"`
	// Replicas is how many model-host replicas serve this deployment. A fleet is
	// expressed by asking for more than one (ADR 0010): each replica is one pod holding
	// one GPU, and the data plane balances across them. Absent or zero means one, so a
	// deployment that predates this field is unchanged.
	Replicas int `json:"replicas,omitempty"`
	// Strategy is how the data plane balances across this deployment's backends:
	// "least_in_flight" (the default), "round_robin", "session_affinity", or "weighted"
	// (M2, ADR 0011). It belongs on the deployment rather than the stamp because the
	// right choice depends on what is being served, not on where. Empty means the data
	// plane's own default, which is least-in-flight.
	Strategy string `json:"strategy,omitempty"`
	// Capabilities lets the data plane choose this deployment for model=auto without
	// consulting the control plane or inspecting any other account's assignments.
	Capabilities *ModelCapabilities `json:"capabilities,omitempty"`
	// GPUCount is how many devices one replica needs, which becomes the container's
	// nvidia.com/gpu limit. The control plane admits the placement by comparing
	// replicas x this against the stamp's reported capacity (ADR 0013), so the pod has to
	// ask for the same number it was admitted for. Absent or zero means the count the
	// operator was configured with, so a deployment declared before this field is
	// unchanged.
	GPUCount int `json:"gpuCount,omitempty"`
	// MaxModelLen, MaxNumSeqs, GPUMemoryUtilization, and Execution are the serving
	// settings this deployment asked for, each falling back to the operator's configured
	// value when absent. They belong on the deployment because they are properties of the
	// model: one stamp-wide context length has to suit the largest model on the stamp,
	// which leaves capacity unused on the smaller ones and can make a short-context model
	// unservable outright.
	//
	// dtype is deliberately absent. The operator derives it from the profiled hardware,
	// and a deployment that could override it could ask for a precision the device does
	// not implement, which fails at startup rather than degrading.
	MaxModelLen int `json:"maxModelLen,omitempty"`
	MaxNumSeqs  int `json:"maxNumSeqs,omitempty"`
	// A string so the fraction is passed through exactly as declared.
	GPUMemoryUtilization string `json:"gpuMemoryUtilization,omitempty"`
	// Execution is "eager" or "cuda_graph"; empty means the operator's configured
	// behaviour. A mode rather than a boolean so absent and false stay distinct.
	Execution string `json:"execution,omitempty"`
	// JWTIssuer and JWKSURL are how the data plane must verify tokens. They describe the
	// stamp rather than this deployment, and they are here because the operator renders the
	// data plane's configuration from these resources and has no other channel to read
	// stamp-wide state from. Every resource on a stamp carries the same values, and the
	// rendered document holds one copy at the top rather than one per entry.
	JWTIssuer  string `json:"jwtIssuer,omitempty"`
	JWKSURL    string `json:"jwksUrl,omitempty"`
	Generation int    `json:"generation"`
}

// DesiredReplicas is the replica count to run, defaulting to one when unset.
//
// A deployment declared before replicas existed carries zero here, which must mean the
// single replica it has always had rather than none.
func (s Spec) DesiredReplicas() int {
	if s.Replicas < 1 {
		return 1
	}
	return s.Replicas
}

// DesiredGPUs is the per-replica device count, falling back to the operator's configured
// value when the deployment does not ask for one.
func (s Spec) DesiredGPUs(configured int) int {
	if s.GPUCount < 1 {
		return configured
	}
	return s.GPUCount
}

// DesiredMaxModelLen is the context bound to serve with, falling back to the operator's
// configured value when the deployment does not ask for one.
func (s Spec) DesiredMaxModelLen(configured int) int {
	if s.MaxModelLen < 1 {
		return configured
	}
	return s.MaxModelLen
}

// DesiredMaxNumSeqs is the concurrent-sequence bound, falling back as above.
func (s Spec) DesiredMaxNumSeqs(configured int) int {
	if s.MaxNumSeqs < 1 {
		return configured
	}
	return s.MaxNumSeqs
}

// DesiredGPUMemoryUtilization is the memory fraction, falling back as above.
//
// Returned as declared. The caller still clamps it against the smallest device in the
// pool, because a fraction that leaves too little absolute headroom fails at startup
// whether a deployment or the chart asked for it.
func (s Spec) DesiredGPUMemoryUtilization(configured string) string {
	if s.GPUMemoryUtilization == "" {
		return configured
	}
	return s.GPUMemoryUtilization
}

// DesiredEnforceEager reports whether to skip CUDA graph capture.
//
// Absent means the operator's configured behaviour, so a deployment that expresses no
// opinion is unchanged while one that explicitly asks for graphs can still turn off an
// eager default.
func (s Spec) DesiredEnforceEager(configured bool) bool {
	switch s.Execution {
	case "eager":
		return true
	case "cuda_graph":
		return false
	default:
		return configured
	}
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
	Phase               string         `json:"phase,omitempty"`
	ObservedGeneration  int64          `json:"observedGeneration,omitempty"`
	ReadyReplicas       *int           `json:"readyReplicas,omitempty"`
	UnavailableReplicas *int           `json:"unavailableReplicas,omitempty"`
	Rollout             *RolloutStatus `json:"rollout,omitempty"`
	Conditions          []Condition    `json:"conditions,omitempty"`
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
//
// A deployment is served by a pool of backends (ADR 0010). The data plane accepts both
// a single upstream_url (a one-backend pool) and a backends list, so the operator can
// publish concrete pod endpoints as backends while a stamp with no operator keeps
// writing the single upstream_url. When the operator publishes backends it still writes
// upstream_url as the first backend's address, so an older data plane that only reads
// upstream_url keeps working.
type dataPlaneEntry struct {
	DeploymentID  string             `json:"deployment_id"`
	AccountID     string             `json:"account_id"`
	ModelAlias    string             `json:"model_alias"`
	UpstreamURL   string             `json:"upstream_url"`
	UpstreamModel string             `json:"upstream_model,omitempty"`
	Backends      []dataPlaneBackend `json:"backends,omitempty"`
	// Strategy is how the data plane balances across Backends. Omitted when the
	// deployment does not name one, so the data plane applies its own default.
	Strategy      string             `json:"strategy,omitempty"`
	Capabilities  *ModelCapabilities `json:"capabilities,omitempty"`
	MaxModelLen   int                `json:"max_model_len,omitempty"`
	RouteRevision string             `json:"route_revision,omitempty"`
}

// dataPlaneBackend is one model-host endpoint in a deployment's pool.
//
// Weight is consulted only by the weighted strategy (FEAT-004), which is how a rollout
// splits traffic between the old and new release: every backend of a release carries that
// release's share, so the data plane sends each release traffic in proportion. Omitted
// when it is the default so a non-rollout pool renders unchanged.
type dataPlaneBackend struct {
	URL      string   `json:"url"`
	ID       string   `json:"id,omitempty"`
	Weight   *float64 `json:"weight,omitempty"`
	Workload string   `json:"workload,omitempty"`
}

// Options configures a reconciler.
type Options struct {
	Namespace     string
	ConfigMapName string
	// ConfigKey is the file name the data plane mounts, so the ConfigMap projects to
	// the path the data plane already expects.
	ConfigKey string
	Log       *slog.Logger
	// RouterStatusURL is the private data-plane listener used to acknowledge that a
	// cutover revision is loaded and old backend requests have drained.
	RouterStatusURL string
	RouterHTTP      *http.Client
	// ModelHost, when it carries an image, makes the operator run the inference
	// server for each declared deployment rather than pointing the data plane at one
	// somebody else operates.
	ModelHost ModelHost
	// Rollout governs how a change of release is applied and when it is abandoned.
	Rollout Rollout
}

// Reconciler renders declared deployments into cluster state.
type Reconciler struct {
	client  *kube.Client
	options Options
	// profiled records that the hardware has been described. Capability does not change
	// while a node runs, so listing nodes every pass would spend a cluster read per
	// interval on an answer that does not move.
	profiled bool
	// smallestMemoryMiB is the smallest frame buffer profiled in the pool, retained so a
	// per-deployment memory fraction can be clamped against the same device the
	// stamp-wide value was. Zero means the pool could not be described for memory, in
	// which case nothing is clamped. A host may be scheduled onto any node in the pool,
	// so a fraction that only fits the largest device fails intermittently.
	smallestMemoryMiB int
	// clampWarned remembers which per-deployment clamps have already been logged, keyed by
	// deployment and the adjustment made. Reconcile runs on an interval, so warning on
	// every pass would bury the one time the value actually changed.
	clampWarned map[string]string
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
	if options.RouterHTTP == nil {
		options.RouterHTTP = &http.Client{Timeout: 5 * time.Second}
	}
	if options.Rollout.ReadyTimeout == 0 {
		options.Rollout = DefaultRollout()
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
	// HostsReady counts deployments whose model host can answer, which is not the
	// same as the number configured.
	HostsReady int
	HostsTotal int
	// RolledBack and Deferred make rollout behaviour observable in one pass.
	RolledBack int
	Deferred   int
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

	ready := map[string]modelHostReadiness{}
	decisions := map[string]RolloutDecision{}
	rolloutStatuses := map[string]*RolloutStatus{}
	persistedStatuses := map[string]*RolloutStatus{}
	for _, item := range serving {
		if item.Status != nil {
			persistedStatuses[item.Spec.DeploymentID] = item.Status.Rollout
		}
	}
	backends := map[string][]dataPlaneBackend{}
	type cleanupAction struct {
		DeploymentID string
		Name         string
	}
	cleanupAfterPublish := make([]cleanupAction, 0)
	if r.options.ModelHost.Enabled() {
		r.applyHardwareProfile(ctx)
		result.HostsTotal = len(serving)
		inFlight := 0
		for _, item := range serving {
			if item.Status != nil && item.Status.Rollout.inProgress() {
				inFlight++
			}
		}

		for index := range serving {
			item := serving[index]
			state := rolloutState{}
			if item.Status != nil {
				state.Status = item.Status.Rollout
			}

			if state.Status == nil {
				exists, release, observed, observeErr := r.observeReleaseWorkload(
					ctx, item, hostName(item),
				)
				if observeErr != nil {
					return result, observeErr
				}
				if release == "" {
					release = releaseOf(item)
				}
				state.ActiveExists = exists
				state.ActiveReadiness = observed
				if exists {
					state.Status = &RolloutStatus{
						Phase: rolloutPhaseServing, TargetGeneration: item.Metadata.Generation,
						ActiveRelease: release, ActiveWorkload: hostName(item),
					}
				}
			} else {
				var observeErr error
				state.ActiveExists, _, state.ActiveReadiness, observeErr =
					r.observeReleaseWorkload(ctx, item, state.Status.ActiveWorkload)
				if observeErr != nil {
					return result, observeErr
				}
				if state.Status.CandidateWorkload != "" {
					state.CandidateExists, _, state.CandidateReadiness, observeErr =
						r.observeReleaseWorkload(ctx, item, state.Status.CandidateWorkload)
					if observeErr != nil {
						return result, observeErr
					}
					_, state.CandidateConcrete = r.discoverConcreteBackendsNamed(
						ctx, item, state.Status.CandidateWorkload,
					)
				}
				if state.Status.Phase == rolloutPhaseDraining {
					drained, drainErr := r.routerDrained(
						ctx, item.Spec.DeploymentID, state.Status.CutoverRevision,
						state.Status.DrainingWorkload,
					)
					if drainErr != nil {
						r.options.Log.Warn("could not acknowledge rollout drain",
							"deployment", item.Spec.DeploymentID, "error", drainErr)
					}
					state.DrainAcknowledged = drained
				}
			}

			decision := decideRollout(r.options.Rollout, item, state, inFlight, time.Now())
			decisions[item.Spec.DeploymentID] = decision
			if decision.Phase == rolloutPhasePreparing || decision.Phase == rolloutPhaseDraining {
				wasInProgress := state.Status != nil && state.Status.inProgress()
				if !wasInProgress {
					inFlight++
				}
			}
			if decision.Deferred {
				result.Deferred++
			}
			if decision.RolledBack {
				result.RolledBack++
			}

			activeReadiness := state.ActiveReadiness
			activeExists := state.ActiveExists
			if state.Status != nil && decision.ActiveWorkload != "" &&
				decision.ActiveWorkload == state.Status.CandidateWorkload {
				activeReadiness = state.CandidateReadiness
				activeExists = state.CandidateExists
			}
			if decision.EnsureActive && decision.ActiveWorkload != "" && !activeExists {
				observed, hostErr := r.ensureNamedWorkload(
					ctx, item, decision.ActiveRelease, decision.ActiveWorkload,
				)
				if hostErr != nil {
					return result, hostErr
				}
				activeReadiness = observed
			}
			candidateReadiness := state.CandidateReadiness
			if decision.EnsureCandidate && decision.CandidateWorkload != "" {
				observed, hostErr := r.ensureNamedWorkload(
					ctx, item, decision.CandidateRelease, decision.CandidateWorkload,
				)
				if hostErr != nil {
					return result, hostErr
				}
				candidateReadiness = observed
			}

			if decision.DeleteCandidate && state.Status != nil &&
				state.Status.CandidateWorkload != "" {
				cleanupAfterPublish = append(cleanupAfterPublish, cleanupAction{
					DeploymentID: item.Spec.DeploymentID,
					Name:         state.Status.CandidateWorkload,
				})
			}
			if decision.DeleteActive && state.Status != nil &&
				state.Status.ActiveWorkload != "" {
				cleanupAfterPublish = append(cleanupAfterPublish, cleanupAction{
					DeploymentID: item.Spec.DeploymentID,
					Name:         state.Status.ActiveWorkload,
				})
			}

			pool, strategy := r.rolloutBackends(ctx, item, decision)
			if len(pool) == 0 {
				return result, fmt.Errorf("deployment %s has no routable backend", item.Spec.DeploymentID)
			}
			backends[item.Spec.DeploymentID] = pool
			if strategy != "" {
				serving[index].Spec.Strategy = strategy
			}
			serving[index].Spec.UpstreamURL = pool[0].URL

			if decision.Phase == rolloutPhasePreparing || decision.Phase == rolloutPhaseFailed ||
				decision.RollbackDrain {
				ready[item.Spec.DeploymentID] = activeReadiness
			} else {
				ready[item.Spec.DeploymentID] = candidateReadiness
				if decision.Phase == rolloutPhaseServing {
					ready[item.Spec.DeploymentID] = activeReadiness
				}
			}
			if ready[item.Spec.DeploymentID].Ready > 0 {
				result.HostsReady++
			}
		}
	}

	changed, revisions, err := r.applyConfigMap(ctx, serving, backends)
	if err != nil {
		return result, err
	}
	result.ConfigChanged = changed

	for _, item := range serving {
		decision := decisions[item.Spec.DeploymentID]
		drainingIDs := make([]string, 0)
		if decision.Phase == rolloutPhaseDraining && decision.CutoverRevision == "" {
			for _, backend := range backends[item.Spec.DeploymentID] {
				if backend.Weight != nil && *backend.Weight == 0 {
					drainingIDs = append(drainingIDs, backend.ID)
				}
			}
		}
		rolloutStatuses[item.Spec.DeploymentID] = rolloutStatusFromDecision(
			item, decision, revisions[item.Spec.DeploymentID], drainingIDs,
		)
	}

	statusWritten := map[string]bool{}
	for _, item := range serving {
		if !r.statusIsCurrent(item) || r.options.ModelHost.Enabled() {
			if err := r.reportApplied(
				ctx, item, ready[item.Spec.DeploymentID], decisions[item.Spec.DeploymentID],
				rolloutStatuses[item.Spec.DeploymentID],
			); err != nil {
				r.options.Log.Warn("could not write status",
					"resource", item.Metadata.Name, "error", err)
				continue
			}
			result.StatusWrites++
			statusWritten[item.Spec.DeploymentID] = true
			persistedStatuses[item.Spec.DeploymentID] = rolloutStatuses[item.Spec.DeploymentID]
		}
	}

	// Destructive cleanup requires both route publication and a durable checkpoint. If
	// status fails, the old workload remains so the next pass can safely reconstruct.
	for _, action := range cleanupAfterPublish {
		if !statusWritten[action.DeploymentID] {
			continue
		}
		if err := r.deleteWorkload(ctx, action.Name); err != nil {
			return result, err
		}
	}

	if r.options.ModelHost.Enabled() {
		if err := r.pruneRolloutHosts(
			ctx, serving, persistedStatuses, revisions[""],
		); err != nil {
			r.options.Log.Warn("could not prune model hosts", "error", err)
		}
	}

	return result, nil
}

// renderConfig produces the data plane's file content.
//
// Entries are sorted by deployment id so an unchanged set produces an identical
// document, which is what lets the reconciler avoid a write and the data plane avoid
// a reload.
//
// backends maps a deployment id to the concrete pool the operator discovered for it.
// When present the entry carries a backends list and upstream_url is set to the first
// backend so an older data plane that reads only upstream_url still works (ADR 0010).
// When absent the entry keeps the single upstream_url shape, which is what a stamp with
// no operator, or one whose endpoints are not yet resolvable, produces.
func renderConfig(items []ModelDeployment, backends map[string][]dataPlaneBackend) (string, error) {
	entries := make([]dataPlaneEntry, 0, len(items))
	for _, item := range items {
		entry := dataPlaneEntry{
			DeploymentID:  item.Spec.DeploymentID,
			AccountID:     item.Spec.AccountID,
			ModelAlias:    item.Spec.ModelAlias,
			UpstreamURL:   item.Spec.UpstreamURL,
			UpstreamModel: item.Spec.UpstreamModel,
			Strategy:      item.Spec.Strategy,
			Capabilities:  item.Spec.Capabilities,
			MaxModelLen:   item.Spec.MaxModelLen,
		}
		if pool := backends[item.Spec.DeploymentID]; len(pool) > 0 {
			entry.Backends = pool
			// Keep upstream_url populated with the first backend so a data plane that
			// predates the pool still reaches a live host rather than nothing.
			entry.UpstreamURL = pool[0].URL
		}
		canonicalRoute, err := json.Marshal(entry)
		if err != nil {
			return "", fmt.Errorf("render route revision: %w", err)
		}
		routeDigest := sha256.Sum256(canonicalRoute)
		entry.RouteRevision = hex.EncodeToString(routeDigest[:])
		entries = append(entries, entry)
	}
	sort.Slice(entries, func(i, j int) bool {
		return entries[i].DeploymentID < entries[j].DeploymentID
	})

	// Lifted to one top-level section. Each resource carries the stamp's issuer because
	// that is the only channel the operator has for stamp-wide state, but writing it once
	// keeps the document unambiguous: entries cannot disagree about a value there is only
	// one of. Omitted when no resource declares it, which the data plane reads as "keep
	// what you have" rather than "clear it".
	var verification *dataPlaneVerification
	for _, item := range items {
		if item.Spec.JWTIssuer != "" && item.Spec.JWKSURL != "" {
			verification = &dataPlaneVerification{
				JWTIssuer: item.Spec.JWTIssuer,
				JWKSURL:   item.Spec.JWKSURL,
			}
			break
		}
	}

	// The revision is a digest of everything the data plane acts on, verification
	// included, so a changed issuer produces a changed revision and is not mistaken for
	// an unchanged document.
	canonicalBody := map[string]any{"deployments": entries}
	body := map[string]any{"deployments": entries}
	if verification != nil {
		canonicalBody["verification"] = verification
		body["verification"] = verification
	}
	canonical, err := json.Marshal(canonicalBody)
	if err != nil {
		return "", fmt.Errorf("render canonical configuration: %w", err)
	}
	digest := sha256.Sum256(canonical)
	body["revision"] = hex.EncodeToString(digest[:])
	document, err := json.MarshalIndent(body, "", "  ")
	if err != nil {
		return "", fmt.Errorf("render configuration: %w", err)
	}
	return string(document) + "\n", nil
}

// dataPlaneVerification is the stamp-wide token-verification contract as the data plane
// reads it.
type dataPlaneVerification struct {
	JWTIssuer string `json:"jwt_issuer"`
	JWKSURL   string `json:"jwks_url"`
}

type configMap struct {
	APIVersion string            `json:"apiVersion,omitempty"`
	Kind       string            `json:"kind,omitempty"`
	Metadata   Metadata          `json:"metadata"`
	Data       map[string]string `json:"data,omitempty"`
}

func configRevision(document string) string {
	var payload struct {
		Revision string `json:"revision"`
	}
	if json.Unmarshal([]byte(document), &payload) != nil {
		return ""
	}
	return payload.Revision
}

func routeRevisions(document string) map[string]string {
	var payload struct {
		Deployments []dataPlaneEntry `json:"deployments"`
	}
	if json.Unmarshal([]byte(document), &payload) != nil {
		return nil
	}
	revisions := make(map[string]string, len(payload.Deployments)+1)
	revisions[""] = configRevision(document)
	for _, entry := range payload.Deployments {
		revisions[entry.DeploymentID] = entry.RouteRevision
	}
	return revisions
}

func (r *Reconciler) applyConfigMap(
	ctx context.Context, items []ModelDeployment, backends map[string][]dataPlaneBackend,
) (bool, map[string]string, error) {
	document, err := renderConfig(items, backends)
	if err != nil {
		return false, nil, err
	}
	revisions := routeRevisions(document)

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
			return false, nil, fmt.Errorf("create configmap: %w", err)
		}
		r.options.Log.Info("configuration created",
			"configmap", r.options.ConfigMapName, "deployments", len(items))
		return true, revisions, nil
	}
	if err != nil {
		return false, nil, fmt.Errorf("read configmap: %w", err)
	}

	if existing.Data[r.options.ConfigKey] == document {
		// Writing an identical document would bump the resourceVersion and make every
		// mounted copy churn for no reason.
		return false, revisions, nil
	}

	// Carry the resourceVersion so a concurrent writer is detected instead of being
	// silently overwritten.
	desired.Metadata.ResourceVersion = existing.Metadata.ResourceVersion
	if err := r.client.Update(
		ctx, r.configMapPath(r.options.ConfigMapName), desired, nil,
	); err != nil {
		return false, nil, fmt.Errorf("update configmap: %w", err)
	}
	r.options.Log.Info("configuration updated",
		"configmap", r.options.ConfigMapName, "deployments", len(items))
	return true, revisions, nil
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

func (r *Reconciler) reportApplied(
	ctx context.Context, item ModelDeployment, readiness modelHostReadiness,
	decision RolloutDecision, rollout *RolloutStatus,
) error {
	now := time.Now().UTC().Format(time.RFC3339)
	available := !r.options.ModelHost.Enabled() || readiness.Ready > 0
	applied := !r.options.ModelHost.Enabled() ||
		(rollout != nil && rollout.Phase == rolloutPhaseServing &&
			rollout.ActiveRelease == releaseOf(item))
	boolStatus := func(value bool) string {
		if value {
			return "True"
		}
		return "False"
	}
	appliedReason := "DesiredReleaseProgressing"
	if applied {
		appliedReason = r.appliedReason()
	} else if decision.RolledBack {
		appliedReason = "DesiredReleaseRolledBack"
	}
	conditions := []Condition{
		{
			Type: ConditionApplied, Status: boolStatus(applied), Reason: appliedReason,
			Message:            "Whether the declared release is the active routed release",
			ObservedGeneration: item.Metadata.Generation, LastTransitionTime: now,
		},
		{
			Type: ConditionAvailable, Status: boolStatus(available),
			Reason:             map[bool]string{true: "ActiveReleaseServing", false: "NoReadyModelHost"}[available],
			Message:            "Whether at least one positively routed model-host replica can answer",
			ObservedGeneration: item.Metadata.Generation, LastTransitionTime: now,
		},
	}
	progressing := rollout != nil && rollout.inProgress()
	conditions = append(conditions, Condition{
		Type: ConditionProgressing, Status: boolStatus(progressing), Reason: decision.Reason,
		Message:            "Release preparation, cutover, and acknowledged drain state",
		ObservedGeneration: item.Metadata.Generation, LastTransitionTime: now,
	})

	phase := "pending"
	if applied {
		phase = "ready"
	}
	if decision.RolledBack && available {
		phase = "degraded"
	}
	readyReplicas := readiness.Ready
	unavailableReplicas := max(0, readiness.Desired-readiness.Ready)
	status := Status{
		Phase: phase, ObservedGeneration: item.Metadata.Generation,
		ReadyReplicas: &readyReplicas, UnavailableReplicas: &unavailableReplicas,
		Rollout: rollout, Conditions: conditions,
	}
	path := fmt.Sprintf("%s/%s/status", r.resourcePath(), item.Metadata.Name)
	if err := r.client.MergePatch(ctx, path, map[string]any{"status": status}, nil); err != nil {
		return fmt.Errorf("patch status: %w", err)
	}
	return nil
}

// appliedReason names what the operator did, so status cannot imply a running server
// on a stamp where the operator only writes configuration.
func (r *Reconciler) appliedReason() string {
	if r.options.ModelHost.Enabled() {
		return "ModelHostAndConfigurationApplied"
	}
	return "DataPlaneConfigurationRendered"
}

// pruneRolloutHosts removes only workloads not named by the route/state produced this
// pass. It runs after ConfigMap publication, never before.
func (r *Reconciler) pruneRolloutHosts(
	ctx context.Context, serving []ModelDeployment, statuses map[string]*RolloutStatus,
	globalRevision string,
) error {
	wanted := make(map[string]struct{}, len(serving)*2)
	for _, item := range serving {
		status := statuses[item.Spec.DeploymentID]
		if status == nil {
			wanted[hostName(item)] = struct{}{}
			continue
		}
		if status.ActiveWorkload != "" {
			wanted[status.ActiveWorkload] = struct{}{}
		}
		if status.CandidateWorkload != "" {
			wanted[status.CandidateWorkload] = struct{}{}
		}
	}
	path := fmt.Sprintf(
		"/apis/apps/v1/namespaces/%s/deployments?labelSelector=%s",
		r.options.Namespace, "app.kubernetes.io/managed-by%3Dfabric-operator",
	)
	var list struct {
		Items []deployment `json:"items"`
	}
	if err := r.client.Get(ctx, path, &list); err != nil {
		return fmt.Errorf("list model hosts: %w", err)
	}
	for _, existing := range list.Items {
		if _, keep := wanted[existing.Metadata.Name]; keep {
			continue
		}
		deploymentID := existing.Metadata.Labels["fabric.khushwant.dev/deployment-id"]
		if deploymentID == "" {
			continue
		}
		drained, err := r.routerRemoved(ctx, globalRevision, deploymentID)
		if err != nil {
			r.options.Log.Warn("could not acknowledge withdrawn deployment drain",
				"deployment", deploymentID, "error", err)
			continue
		}
		if !drained {
			continue
		}
		if err := r.deleteWorkload(ctx, existing.Metadata.Name); err != nil {
			return err
		}
		r.options.Log.Info("model host removed", "deployment", existing.Metadata.Name)
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
				"config_changed", result.ConfigChanged, "status_writes", result.StatusWrites,
				"hosts_ready", result.HostsReady, "hosts_total", result.HostsTotal)
		}

		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
		}
	}
}

// applyHardwareProfile adjusts host settings to the GPUs this stamp actually has.
//
// Profiled once and remembered, because nodes do not change capability while running and
// listing them on every pass would add a cluster read per interval for an answer that does
// not move. A pool that is replaced changes the node names, which is what invalidates it.
func (r *Reconciler) applyHardwareProfile(ctx context.Context) {
	if r.profiled {
		return
	}

	profiles, err := ProfileGPUNodes(ctx, r.client, r.options.ModelHost.NodeSelector)
	if err != nil {
		// Not fatal. Refusing to serve because the hardware could not be described would
		// turn a missing permission into an outage, and the configured values may well be
		// correct. It is logged loudly because an unprofiled stamp is one where a wrong
		// setting will surface as a crash loop instead.
		r.options.Log.Warn("could not profile gpu nodes; using configured settings unchanged",
			"error", err)
		r.profiled = true
		return
	}

	if len(profiles) == 0 {
		r.options.Log.Warn("no gpu nodes advertise an allocatable device",
			"selector", r.options.ModelHost.NodeSelector)
		r.profiled = true
		return
	}

	for _, profile := range profiles {
		r.options.Log.Info("gpu profiled",
			"node", profile.Node, "model", profile.Model,
			"compute_capability", profile.Capability.String(),
			"memory_mib", profile.MemoryMiB, "gpus", profile.Count,
			"source", profile.Source)
	}

	r.smallestMemoryMiB = smallestMemory(profiles)

	adjusted, changes := applyProfile(r.options.ModelHost, profiles)
	for _, change := range changes {
		// Recorded at warning level: the platform is overriding what someone asked for,
		// and doing that silently is its own kind of failure.
		r.options.Log.Warn("host setting adjusted for this hardware",
			"setting", change.Setting, "requested", change.From,
			"applied", change.To, "reason", change.Reason)
	}
	r.options.ModelHost = adjusted
	r.profiled = true
}
