// Package hostreport tells the control plane how this stamp's model-host pods are doing.
//
// An operator watching a new deployment wants to see the image pull, the container start,
// the weights load and the readiness probe pass, and the tail of the server's log when one
// of those steps fails. That information lives in the stamp's API server, and the control
// plane never connects to a stamp (ADR 0001, ADR 0007), so the agent pushes it: a bounded
// snapshot per pod of phase, container state, recent events and the last lines of the log.
//
// Each report is the complete set of model-host pods in the namespace. The control plane
// keeps only the latest snapshot per pod and forgets pods a report no longer names, so a
// report must never be sent from a failed pod listing: an empty list would read as "every
// host is gone".
//
// Reports are frequent while any host is starting, because that is when someone is
// watching, and slow once every host is ready.
package hostreport

import (
	"context"
	"errors"
	"fmt"
	"log/slog"
	"net/url"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

const (
	// ManagedByLabel and DeploymentIDLabel select the pods the operator created for a
	// deployment. They match the labels in internal/operator, which this package does not
	// import so the agent binary carries no operator code.
	ManagedByLabel    = "fabric-operator"
	DeploymentIDLabel = "fabric.khushwant.dev/deployment-id"
	// HostContainer is the model server's container name in the operator's pod template.
	HostContainer = "model-host"

	// DefaultLogLines is how much of the log is asked for per pod.
	DefaultLogLines = 200
	// MaxLogBytes bounds the log tail sent per pod. Together with the events and status it
	// keeps one pod's snapshot under about 64 KiB, which the control plane also enforces.
	MaxLogBytes = 48 << 10
	// MaxLineBytes bounds one log line; a progress bar redrawn without newlines can
	// otherwise be one enormous "line".
	MaxLineBytes = 2000
	// MaxEvents is how many of a pod's most recent events are reported.
	MaxEvents = 20
	// MaxPods bounds one report. A stamp running more model-host pods than this reports
	// the ones that need attention first (not ready, then most recently started).
	MaxPods = 64
	// ReportLogBudget bounds the log bytes in one whole report, so a stamp with many hosts
	// sends shorter tails rather than a request a proxy in front of the control plane
	// would refuse.
	ReportLogBudget = 640 << 10

	// readLimit is how much of the API server's log response is read before trimming. The
	// tail is selected by line count server-side, and lines are capped below, so this only
	// matters for a pathological log.
	readLimit = 1 << 20

	DefaultActiveInterval = 3 * time.Second
	DefaultSteadyInterval = 30 * time.Second
	// NotAcceptedBackoff is the wait after a control plane said it has no host report
	// endpoint.
	NotAcceptedBackoff = 5 * time.Minute
)

// Reader is the Kubernetes access this package needs, narrow so a test can fake it.
type Reader interface {
	Get(ctx context.Context, path string, out any) error
	GetText(ctx context.Context, path string, limit int64) ([]byte, error)
}

// Event is one Kubernetes event about a pod.
type Event struct {
	Type    string    `json:"type"`
	Reason  string    `json:"reason"`
	Message string    `json:"message"`
	Count   int       `json:"count"`
	At      time.Time `json:"at"`
}

// Condition is one pod condition (PodScheduled, Initialized, ContainersReady, Ready).
type Condition struct {
	Type               string     `json:"type"`
	Status             string     `json:"status"`
	Reason             string     `json:"reason,omitempty"`
	Message            string     `json:"message,omitempty"`
	LastTransitionTime *time.Time `json:"last_transition_time,omitempty"`
}

// Pod is the snapshot of one model-host pod. Node names are deliberately absent: the
// control plane decides placement, not scheduling, and has no use for them.
type Pod struct {
	Name             string      `json:"name"`
	Phase            string      `json:"phase"`
	Ready            bool        `json:"ready"`
	Terminating      bool        `json:"terminating"`
	RestartCount     int         `json:"restart_count"`
	ContainerState   string      `json:"container_state"`
	ContainerReason  string      `json:"container_reason,omitempty"`
	ContainerMessage string      `json:"container_message,omitempty"`
	StartedAt        *time.Time  `json:"started_at,omitempty"`
	Conditions       []Condition `json:"conditions"`
	Events           []Event     `json:"events"`
	Log              string      `json:"log"`
	LogTruncated     bool        `json:"log_truncated"`
	// LogPrevious marks a log read from the previous container instance, which is what
	// explains a crash loop: the current instance has not started yet.
	LogPrevious bool `json:"log_previous"`
	// LogError says why no log is present, for example a container still pulling.
	LogError string `json:"log_error,omitempty"`
	// Weights is how much of the model's weights the node's cache holds, while the host is
	// starting and the agent can see that cache. Absent when unknown.
	Weights *WeightProgress `json:"weights,omitempty"`

	// node is where the pod runs. Used to decide whether this agent can see that node's
	// model cache; never reported, because the control plane has no use for node names.
	node string
}

// WeightProgress is a measurement of a model's weights in the node's Hugging Face cache.
// TotalBytes is zero when the expected size is unknown.
type WeightProgress struct {
	Repository      string    `json:"repository"`
	DownloadedBytes int64     `json:"downloaded_bytes"`
	TotalBytes      int64     `json:"total_bytes"`
	InProgressFiles int       `json:"in_progress_files"`
	MeasuredAt      time.Time `json:"measured_at"`
}

// WeightSource measures weight download progress for a starting host. It returns nil when
// it cannot tell, for example because the pod runs on a node whose cache this agent does
// not mount.
type WeightSource interface {
	Measure(ctx context.Context, deploymentID, node string) *WeightProgress
}

// Deployment groups the pods of one deployment.
type Deployment struct {
	DeploymentID string `json:"deployment_id"`
	Pods         []Pod  `json:"pods"`
}

// Report is one complete snapshot of this stamp's model hosts.
type Report struct {
	ObservedAt  time.Time    `json:"observed_at"`
	Deployments []Deployment `json:"deployments"`
}

// Settled reports whether every host is ready and none is starting or stopping, which
// is when frequent reports stop being worth their cost.
func (r Report) Settled() bool {
	for _, deployment := range r.Deployments {
		for _, pod := range deployment.Pods {
			if !pod.Ready || pod.Terminating {
				return false
			}
		}
	}
	return true
}

// Sender delivers a report to the control plane.
type Sender func(ctx context.Context, report Report) error

// Reporter collects and sends reports on an adaptive interval.
type Reporter struct {
	Kube      Reader
	Namespace string
	Log       *slog.Logger
	// ActiveInterval applies while any host is not ready; SteadyInterval otherwise.
	ActiveInterval time.Duration
	SteadyInterval time.Duration
	// LogLines is how many trailing log lines are asked for per pod.
	LogLines int
	// Now is the clock, replaceable in tests.
	Now func() time.Time
	// Weights, when set, measures weight download progress for hosts that are not ready.
	Weights WeightSource
}

func (r *Reporter) now() time.Time {
	if r.Now != nil {
		return r.Now()
	}
	return time.Now()
}

func (r *Reporter) logger() *slog.Logger {
	if r.Log != nil {
		return r.Log
	}
	return slog.Default()
}

// Run collects and sends until the context ends. Failures are logged and retried on the
// steady interval; they never stop the agent, because host status is a convenience for
// a human and serving does not depend on it.
func (r *Reporter) Run(ctx context.Context, send Sender) {
	active := r.ActiveInterval
	if active <= 0 {
		active = DefaultActiveInterval
	}
	steady := r.SteadyInterval
	if steady <= 0 {
		steady = DefaultSteadyInterval
	}
	var lastError string
	for {
		wait := steady
		report, err := r.Collect(ctx)
		if err == nil {
			err = send(ctx, report)
			if err == nil && !report.Settled() {
				wait = active
			}
		}
		if errors.Is(err, ErrNotAccepted) {
			// An older control plane: asking again every few seconds cannot help, but it
			// may be upgraded under a running agent.
			wait = NotAcceptedBackoff
		}
		if err != nil && ctx.Err() == nil {
			// Logged when it changes rather than on every attempt: a stamp without the
			// RBAC grant would otherwise log the same refusal every thirty seconds.
			if message := err.Error(); message != lastError {
				r.logger().Warn("host status report failed", "error", err)
				lastError = message
			}
		} else if err == nil && lastError != "" {
			r.logger().Info("host status reports recovered")
			lastError = ""
		}

		timer := time.NewTimer(wait)
		select {
		case <-ctx.Done():
			timer.Stop()
			return
		case <-timer.C:
		}
	}
}

// Collect reads every model-host pod in the namespace with its events and log tail.
//
// Only the pod listing can fail the whole report. A pod whose events or log cannot be
// read is still reported, with the reason, because its status is the part that matters.
func (r *Reporter) Collect(ctx context.Context) (Report, error) {
	selector := "app.kubernetes.io/managed-by=" + ManagedByLabel + "," + DeploymentIDLabel
	path := "/api/v1/namespaces/" + url.PathEscape(r.Namespace) +
		"/pods?labelSelector=" + url.QueryEscape(selector)
	var pods podList
	if err := r.Kube.Get(ctx, path, &pods); err != nil {
		return Report{}, fmt.Errorf("list model-host pods: %w", err)
	}

	items := pods.Items
	// Hosts that need attention first, so the cap drops settled pods rather than the
	// one somebody is waiting on.
	sort.SliceStable(items, func(i, j int) bool {
		ri, rj := items[i].ready(), items[j].ready()
		if ri != rj {
			return !ri
		}
		return items[i].Metadata.CreationTimestamp.After(items[j].Metadata.CreationTimestamp)
	})
	if len(items) > MaxPods {
		items = items[:MaxPods]
	}

	logBudget := MaxLogBytes
	if len(items) > 0 && ReportLogBudget/len(items) < logBudget {
		logBudget = ReportLogBudget / len(items)
	}

	byDeployment := map[string][]Pod{}
	for _, item := range items {
		deploymentID := item.Metadata.Labels[DeploymentIDLabel]
		// The control plane rejects a report naming a deployment id that is not a UUID, so
		// a hand-labelled pod is skipped rather than allowed to fail every report.
		if !uuidPattern.MatchString(deploymentID) {
			continue
		}
		snapshot := r.snapshot(ctx, item, logBudget)
		if r.Weights != nil && !snapshot.Ready && !snapshot.Terminating {
			snapshot.Weights = r.Weights.Measure(ctx, deploymentID, snapshot.node)
		}
		byDeployment[deploymentID] = append(byDeployment[deploymentID], snapshot)
	}

	report := Report{ObservedAt: r.now().UTC(), Deployments: []Deployment{}}
	for deploymentID, snapshots := range byDeployment {
		sort.Slice(snapshots, func(i, j int) bool { return snapshots[i].Name < snapshots[j].Name })
		report.Deployments = append(report.Deployments,
			Deployment{DeploymentID: deploymentID, Pods: snapshots})
	}
	sort.Slice(report.Deployments, func(i, j int) bool {
		return report.Deployments[i].DeploymentID < report.Deployments[j].DeploymentID
	})
	return report, nil
}

func (r *Reporter) snapshot(ctx context.Context, item pod, logBudget int) Pod {
	snapshot := Pod{
		Name:        item.Metadata.Name,
		Phase:       orUnknown(item.Status.Phase),
		Ready:       item.ready(),
		Terminating: item.Metadata.DeletionTimestamp != nil,
		StartedAt:   item.Status.StartTime,
		Conditions:  []Condition{},
		Events:      []Event{},
		node:        item.Spec.NodeName,
	}
	for _, condition := range item.Status.Conditions {
		snapshot.Conditions = append(snapshot.Conditions, Condition{
			Type:               condition.Type,
			Status:             condition.Status,
			Reason:             clip(condition.Reason, 128),
			Message:            clip(condition.Message, 1000),
			LastTransitionTime: condition.LastTransitionTime,
		})
	}

	container, found := item.hostContainer()
	snapshot.ContainerState = "unknown"
	if found {
		snapshot.RestartCount = container.RestartCount
		switch {
		case container.State.Waiting != nil:
			snapshot.ContainerState = "waiting"
			snapshot.ContainerReason = container.State.Waiting.Reason
			snapshot.ContainerMessage = container.State.Waiting.Message
		case container.State.Running != nil:
			snapshot.ContainerState = "running"
		case container.State.Terminated != nil:
			snapshot.ContainerState = "terminated"
			snapshot.ContainerReason = container.State.Terminated.Reason
			snapshot.ContainerMessage = container.State.Terminated.Message
		}
		snapshot.ContainerReason = clip(snapshot.ContainerReason, 128)
		snapshot.ContainerMessage = clip(redactLine(snapshot.ContainerMessage), 1000)
	}

	events, err := r.events(ctx, item.Metadata.Name)
	if err != nil {
		r.logger().Debug("could not read pod events", "pod", item.Metadata.Name, "error", err)
	} else {
		snapshot.Events = events
	}

	r.attachLog(ctx, &snapshot, container, found, logBudget)
	return snapshot
}

// attachLog reads the log tail. A container that has never started has no log, and a
// crash-looping one is between instances, so the previous instance's log is read instead:
// that is the one with the traceback.
func (r *Reporter) attachLog(
	ctx context.Context, snapshot *Pod, container containerStatus, found bool, budget int,
) {
	if !found {
		snapshot.LogError = "container has not been created yet"
		return
	}
	previous := false
	switch {
	case container.State.Running != nil, container.State.Terminated != nil:
	case container.State.Waiting != nil && container.RestartCount > 0:
		previous = true
	default:
		snapshot.LogError = "container has not started yet"
		return
	}

	lines := r.LogLines
	if lines <= 0 {
		lines = DefaultLogLines
	}
	query := url.Values{}
	query.Set("container", container.Name)
	query.Set("tailLines", strconv.Itoa(lines))
	if previous {
		query.Set("previous", "true")
	}
	path := "/api/v1/namespaces/" + url.PathEscape(r.Namespace) + "/pods/" +
		url.PathEscape(snapshot.Name) + "/log?" + query.Encode()
	raw, err := r.Kube.GetText(ctx, path, readLimit)
	if err != nil {
		snapshot.LogError = "log unavailable"
		r.logger().Debug("could not read pod log", "pod", snapshot.Name, "error", err)
		return
	}
	snapshot.Log, snapshot.LogTruncated = Sanitize(string(raw), budget)
	snapshot.LogPrevious = previous
}

func (r *Reporter) events(ctx context.Context, podName string) ([]Event, error) {
	selector := "involvedObject.kind=Pod,involvedObject.name=" + podName
	path := "/api/v1/namespaces/" + url.PathEscape(r.Namespace) +
		"/events?fieldSelector=" + url.QueryEscape(selector)
	var list eventList
	if err := r.Kube.Get(ctx, path, &list); err != nil {
		return nil, err
	}
	events := make([]Event, 0, len(list.Items))
	for _, item := range list.Items {
		count := item.Count
		if count == 0 && item.Series != nil {
			count = item.Series.Count
		}
		if count == 0 {
			count = 1
		}
		events = append(events, Event{
			Type:    item.Type,
			Reason:  clip(item.Reason, 128),
			Message: clip(redactLine(item.Message), 1000),
			Count:   count,
			At:      item.when(),
		})
	}
	sort.SliceStable(events, func(i, j int) bool { return events[i].At.Before(events[j].At) })
	if len(events) > MaxEvents {
		events = events[len(events)-MaxEvents:]
	}
	return events, nil
}

// --- sanitizing ------------------------------------------------------------------------

var (
	// Credentials have no business in a model server's log, but a misconfigured
	// environment dump or a library logging a request header would put one there, and
	// this log is shown in a browser. A matching line is dropped whole: masking only the
	// match leaves whatever the pattern did not anticipate next to it.
	secretPatterns = []*regexp.Regexp{
		regexp.MustCompile(`hf_[A-Za-z0-9]{20,}`),
		regexp.MustCompile(`(?i)\bbearer\s+\S`),
		regexp.MustCompile(`(?i)authorization\s*[:=]`),
		regexp.MustCompile(`\bsk-[A-Za-z0-9_-]{20,}`),
		regexp.MustCompile(`\bAKIA[0-9A-Z]{16}\b`),
		regexp.MustCompile(`(?i)(api[_-]?key|access[_-]?token|secret|password|passwd)["']?\s*[:=]\s*\S`),
		regexp.MustCompile(`fabric_(agt|tel|key)_[A-Za-z0-9_-]{8,}`),
		// The prefixes Fabric's credentials actually carry (control-plane credentials.py).
		regexp.MustCompile(`\bfab_(key|enroll|agent|telem)_[A-Za-z0-9_-]{8,}`),
		regexp.MustCompile(`-----BEGIN [A-Z ]*PRIVATE KEY-----`),
	}
	uuidPattern = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)
	ansiEscape  = regexp.MustCompile(`\x1b\[[0-9;?]*[ -/]*[@-~]`)
)

// RedactedLine replaces a log line that looked like it carried a credential.
const RedactedLine = "[line removed: matched a credential pattern]"

func redactLine(line string) string {
	for _, pattern := range secretPatterns {
		if pattern.MatchString(line) {
			return RedactedLine
		}
	}
	return line
}

// Sanitize removes terminal escapes and credential-looking lines, caps each line, and
// keeps at most maxBytes from the end, cut on a line boundary. It reports whether
// anything was dropped for size.
func Sanitize(raw string, maxBytes int) (string, bool) {
	raw = strings.ToValidUTF8(raw, "�")
	raw = strings.ReplaceAll(raw, "\r\n", "\n")
	lines := strings.Split(strings.TrimRight(raw, "\n"), "\n")
	if len(lines) == 1 && lines[0] == "" {
		return "", false
	}
	for index, line := range lines {
		// A carriage return redraws a progress bar in place; only the final state is
		// worth keeping.
		if cut := strings.LastIndex(line, "\r"); cut >= 0 {
			line = line[cut+1:]
		}
		line = ansiEscape.ReplaceAllString(line, "")
		line = redactLine(line)
		lines[index] = clip(line, MaxLineBytes)
	}

	truncated := false
	size := 0
	start := len(lines)
	for start > 0 {
		next := size + len(lines[start-1]) + 1
		if next > maxBytes {
			truncated = true
			break
		}
		size = next
		start--
	}
	return strings.Join(lines[start:], "\n"), truncated
}

// clip shortens s to at most n bytes, marker included, without splitting a UTF-8
// sequence. Bytes bound characters, so the control plane's character limits hold too.
func clip(s string, n int) string {
	if len(s) <= n {
		return s
	}
	const marker = "…"
	cut := n - len(marker)
	if cut < 0 {
		cut = 0
	}
	for cut > 0 && (s[cut]&0xC0) == 0x80 {
		cut--
	}
	return s[:cut] + marker
}

func orUnknown(value string) string {
	if value == "" {
		return "Unknown"
	}
	return value
}

// --- Kubernetes shapes -----------------------------------------------------------------

type podList struct {
	Items []pod `json:"items"`
}

type pod struct {
	Metadata struct {
		Name              string            `json:"name"`
		Labels            map[string]string `json:"labels"`
		CreationTimestamp time.Time         `json:"creationTimestamp"`
		DeletionTimestamp *time.Time        `json:"deletionTimestamp"`
	} `json:"metadata"`
	Spec struct {
		NodeName string `json:"nodeName"`
	} `json:"spec"`
	Status struct {
		Phase      string     `json:"phase"`
		StartTime  *time.Time `json:"startTime"`
		Conditions []struct {
			Type               string     `json:"type"`
			Status             string     `json:"status"`
			Reason             string     `json:"reason"`
			Message            string     `json:"message"`
			LastTransitionTime *time.Time `json:"lastTransitionTime"`
		} `json:"conditions"`
		ContainerStatuses []containerStatus `json:"containerStatuses"`
	} `json:"status"`
}

type containerStatus struct {
	Name         string `json:"name"`
	Ready        bool   `json:"ready"`
	RestartCount int    `json:"restartCount"`
	State        struct {
		Waiting *struct {
			Reason  string `json:"reason"`
			Message string `json:"message"`
		} `json:"waiting"`
		Running *struct {
			StartedAt *time.Time `json:"startedAt"`
		} `json:"running"`
		Terminated *struct {
			Reason   string `json:"reason"`
			Message  string `json:"message"`
			ExitCode int    `json:"exitCode"`
		} `json:"terminated"`
	} `json:"state"`
}

func (p pod) ready() bool {
	for _, condition := range p.Status.Conditions {
		if condition.Type == "Ready" {
			return condition.Status == "True"
		}
	}
	return false
}

func (p pod) hostContainer() (containerStatus, bool) {
	for _, status := range p.Status.ContainerStatuses {
		if status.Name == HostContainer {
			return status, true
		}
	}
	if len(p.Status.ContainerStatuses) > 0 {
		return p.Status.ContainerStatuses[0], true
	}
	return containerStatus{}, false
}

type eventList struct {
	Items []kubeEvent `json:"items"`
}

type kubeEvent struct {
	Type           string     `json:"type"`
	Reason         string     `json:"reason"`
	Message        string     `json:"message"`
	Count          int        `json:"count"`
	FirstTimestamp *time.Time `json:"firstTimestamp"`
	LastTimestamp  *time.Time `json:"lastTimestamp"`
	EventTime      *time.Time `json:"eventTime"`
	Series         *struct {
		Count            int        `json:"count"`
		LastObservedTime *time.Time `json:"lastObservedTime"`
	} `json:"series"`
	Metadata struct {
		CreationTimestamp time.Time `json:"creationTimestamp"`
	} `json:"metadata"`
}

// when picks the most specific time an event carries. core/v1 events set lastTimestamp;
// events written through events.k8s.io set eventTime and series instead.
func (e kubeEvent) when() time.Time {
	switch {
	case e.Series != nil && e.Series.LastObservedTime != nil:
		return e.Series.LastObservedTime.UTC()
	case e.LastTimestamp != nil:
		return e.LastTimestamp.UTC()
	case e.EventTime != nil:
		return e.EventTime.UTC()
	case e.FirstTimestamp != nil:
		return e.FirstTimestamp.UTC()
	}
	return e.Metadata.CreationTimestamp.UTC()
}

// ErrNotAccepted is returned by a sender when the control plane does not know the host
// report endpoint, so the caller can back off instead of retrying every few seconds.
var ErrNotAccepted = errors.New("control plane does not accept host reports")
