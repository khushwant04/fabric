package servingmetrics

import (
	"context"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	// The labels the operator puts on model-host pods. They match internal/operator and
	// internal/hostreport, which this package does not import.
	managedByLabel    = "fabric-operator"
	deploymentIDLabel = "fabric.khushwant.dev/deployment-id"
	hostContainer     = "model-host"
	mebibyte          = 1 << 20
)

var uuidPattern = regexp.MustCompile(`^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$`)

// Source names reported in Sample.Sources.
const (
	SourceGateway   = "gateway"
	SourceModelHost = "model_host"
	SourceDCGM      = "dcgm"
)

// collectorState holds the previous pass's cumulative counters, from which deltas are taken.
type collectorState struct {
	at         time.Time
	counters   map[string]float64
	histograms map[string]cumulative
	// baselined lists the sources (the gateway, each host pod) seen on the previous pass. A
	// series that first appears on a baselined source started from zero during the interval;
	// a source seen for the first time only records its baseline.
	baselined map[string]bool

	nextCounters   map[string]float64
	nextHistograms map[string]cumulative
	nextBaselined  map[string]bool
}

func newCollectorState() *collectorState {
	return &collectorState{
		counters: map[string]float64{}, histograms: map[string]cumulative{}, baselined: map[string]bool{},
	}
}

func (s *collectorState) begin() {
	s.nextCounters = map[string]float64{}
	s.nextHistograms = map[string]cumulative{}
	s.nextBaselined = map[string]bool{}
}

func (s *collectorState) commit(at time.Time) {
	s.at = at
	s.counters, s.histograms, s.baselined = s.nextCounters, s.nextHistograms, s.nextBaselined
}

func (s *collectorState) counter(source, key string, value float64) float64 {
	key = source + "|" + key
	s.nextCounters[key] = value
	s.nextBaselined[source] = true
	previous, seen := s.counters[key]
	switch {
	case !seen && s.baselined[source]:
		return value
	case !seen:
		return 0
	case value < previous:
		// A counter that went down belongs to a restarted process.
		return value
	}
	return value - previous
}

func (s *collectorState) histogram(source, key string, value cumulative) Histogram {
	if value == nil {
		return nil
	}
	key = source + "|" + key
	s.nextHistograms[key] = value
	s.nextBaselined[source] = true
	previous, seen := s.histograms[key]
	if !seen && !s.baselined[source] {
		return nil
	}
	delta := histogramDelta(value, previous)
	if len(delta) > MaxBuckets {
		return nil
	}
	return delta
}

// Collect takes one sample. It returns false on the first call, which only records the
// counters later samples take deltas from.
func (r *Reporter) Collect(ctx context.Context) (Sample, bool) {
	if r.state == nil {
		r.state = newCollectorState()
	}
	state := r.state
	state.begin()
	now := r.now().UTC()

	points := map[string]*DeploymentPoint{}
	point := func(id string) *DeploymentPoint {
		if existing, ok := points[id]; ok {
			return existing
		}
		created := &DeploymentPoint{DeploymentID: strings.ToLower(id)}
		points[id] = created
		return created
	}
	sources := []string{}

	gatewayRead := r.collectGateway(ctx, state, point)
	if gatewayRead {
		sources = append(sources, SourceGateway)
	}

	pods := r.listHosts(ctx)
	if r.collectHosts(ctx, state, pods, point, gatewayRead) {
		sources = append(sources, SourceModelHost)
	}

	gpus, gpuRead := r.collectGPUs(ctx, pods)
	if gpuRead {
		sources = append(sources, SourceDCGM)
	}
	attributeGPUs(gpus, point)

	first := state.at.IsZero()
	interval := now.Sub(state.at).Seconds()
	state.commit(now)
	if first {
		return Sample{}, false
	}

	sample := Sample{
		ObservedAt: now, IntervalSeconds: interval, Sources: sources,
		Deployments: []DeploymentPoint{}, GPUs: gpus,
	}
	for _, item := range points {
		sample.Deployments = append(sample.Deployments, *item)
	}
	sort.Slice(sample.Deployments, func(i, j int) bool {
		return sample.Deployments[i].DeploymentID < sample.Deployments[j].DeploymentID
	})
	if len(sample.Deployments) > MaxDeployments {
		sample.Deployments = sample.Deployments[:MaxDeployments]
	}
	if len(sample.GPUs) > MaxGPUs {
		sample.GPUs = sample.GPUs[:MaxGPUs]
	}
	return sample, true
}

// --- gateway ----------------------------------------------------------------------------

var gatewayNames = map[string]bool{
	"fabric_dp_requests_total":                  true,
	"fabric_dp_tokens_total":                    true,
	"fabric_dp_request_duration_seconds_bucket": true,
}

// StatusClass maps a gateway outcome label to the class the console charts. The gateway
// does not keep the HTTP status, so the classes describe who failed: a refusal before the
// host is the caller's ("4xx"), a host error or unreachable host is the server's ("5xx").
func StatusClass(outcome string) string {
	switch {
	case strings.HasPrefix(outcome, "ok"):
		return "2xx"
	case strings.HasPrefix(outcome, "cancelled"):
		return "cancelled"
	case strings.HasPrefix(outcome, "upstream_"), outcome == "transport_error",
		strings.HasSuffix(outcome, "_unavailable"):
		return "5xx"
	}
	return "4xx"
}

func (r *Reporter) collectGateway(
	ctx context.Context, state *collectorState, point func(string) *DeploymentPoint,
) bool {
	endpoint := r.GatewayURL
	if endpoint == "" {
		endpoint = DefaultGatewayURL
	}
	if endpoint == "-" {
		return false
	}
	samples, err := r.scrape(ctx, endpoint, func(name string) bool { return gatewayNames[name] })
	if err != nil {
		r.logger().Debug("gateway metrics unavailable", "error", err)
		return false
	}
	const source = SourceGateway
	byDeployment := map[string]bool{}
	for _, sample := range samples {
		id := sample.Labels["deployment_id"]
		if !uuidPattern.MatchString(id) {
			continue
		}
		byDeployment[id] = true
		switch sample.Name {
		case "fabric_dp_requests_total":
			outcome := sample.Labels["outcome"]
			key := "requests|" + id + "|" + sample.Labels["account_id"] + "|" + outcome
			delta := state.counter(source, key, sample.Value)
			item := point(id)
			if item.Requests == nil {
				item.Requests = map[string]float64{}
			}
			item.Requests[StatusClass(outcome)] += delta
		case "fabric_dp_tokens_total":
			delta := state.counter(source, "tokens|"+id+"|"+sample.Labels["direction"], sample.Value)
			if sample.Labels["direction"] == "input" {
				point(id).InputTokens += delta
			} else if sample.Labels["direction"] == "output" {
				point(id).OutputTokens += delta
			}
		}
	}
	for id := range byDeployment {
		latency := collectHistogram(samples, "fabric_dp_request_duration_seconds",
			func(labels map[string]string) bool { return labels["deployment_id"] == id })
		if delta := state.histogram(source, "latency|"+id, latency); delta != nil {
			point(id).Latency = point(id).Latency.Merge(delta)
		}
	}
	// A gateway that answered is a source even when idle, so the console can tell "no
	// traffic" from "not reporting".
	state.nextBaselined[source] = true
	return true
}

// --- model hosts ------------------------------------------------------------------------

type hostPod struct {
	Name         string
	DeploymentID string
	Node         string
	Address      string
}

type podList struct {
	Items []struct {
		Metadata struct {
			Name              string            `json:"name"`
			Labels            map[string]string `json:"labels"`
			DeletionTimestamp *time.Time        `json:"deletionTimestamp"`
		} `json:"metadata"`
		Spec struct {
			NodeName   string `json:"nodeName"`
			Containers []struct {
				Name  string `json:"name"`
				Ports []struct {
					Name          string `json:"name"`
					ContainerPort int    `json:"containerPort"`
				} `json:"ports"`
			} `json:"containers"`
		} `json:"spec"`
		Status struct {
			Phase string `json:"phase"`
			PodIP string `json:"podIP"`
		} `json:"status"`
	} `json:"items"`
}

// listHosts lists the running model-host pods the operator created in this namespace. A
// failed listing is not an error worth reporting every interval; the sample simply has no
// host metrics.
func (r *Reporter) listHosts(ctx context.Context) []hostPod {
	if r.Kube == nil || r.Namespace == "" {
		return nil
	}
	selector := "app.kubernetes.io/managed-by=" + managedByLabel + "," + deploymentIDLabel
	path := "/api/v1/namespaces/" + url.PathEscape(r.Namespace) +
		"/pods?labelSelector=" + url.QueryEscape(selector)
	var list podList
	if err := r.Kube.Get(ctx, path, &list); err != nil {
		r.logger().Debug("could not list model-host pods for metrics", "error", err)
		return nil
	}
	pods := []hostPod{}
	for _, item := range list.Items {
		id := item.Metadata.Labels[deploymentIDLabel]
		if !uuidPattern.MatchString(id) || item.Status.Phase != "Running" || item.Status.PodIP == "" ||
			item.Metadata.DeletionTimestamp != nil {
			continue
		}
		port := DefaultModelHostPort
		for _, container := range item.Spec.Containers {
			if container.Name != hostContainer && len(item.Spec.Containers) > 1 {
				continue
			}
			for _, candidate := range container.Ports {
				if candidate.Name == "http" && candidate.ContainerPort > 0 {
					port = candidate.ContainerPort
				}
			}
		}
		pods = append(pods, hostPod{
			Name: item.Metadata.Name, DeploymentID: strings.ToLower(id), Node: item.Spec.NodeName,
			Address: "http://" + net.JoinHostPort(item.Status.PodIP, strconv.Itoa(port)) + "/metrics",
		})
	}
	sort.Slice(pods, func(i, j int) bool { return pods[i].Name < pods[j].Name })
	if len(pods) > MaxHosts {
		pods = pods[:MaxHosts]
	}
	return pods
}

var hostNames = map[string]bool{
	"vllm:time_to_first_token_seconds_bucket":   true,
	"vllm:time_per_output_token_seconds_bucket": true,
	"vllm:inter_token_latency_seconds_bucket":   true,
	"vllm:e2e_request_latency_seconds_bucket":   true,
	"vllm:num_requests_running":                 true,
	"vllm:num_requests_waiting":                 true,
	"vllm:gpu_cache_usage_perc":                 true,
	"vllm:kv_cache_usage_perc":                  true,
	"vllm:generation_tokens_total":              true,
	"vllm:prompt_tokens_total":                  true,
	"vllm:request_success_total":                true,
}

type hostResult struct {
	pod     hostPod
	samples []promSample
	err     error
}

// collectHosts scrapes every model host in parallel and folds each into its deployment.
func (r *Reporter) collectHosts(
	ctx context.Context, state *collectorState, pods []hostPod,
	point func(string) *DeploymentPoint, gatewayRead bool,
) bool {
	if len(pods) == 0 {
		return false
	}
	results := make([]hostResult, len(pods))
	limit := make(chan struct{}, scrapeParallelism)
	var wait sync.WaitGroup
	for index, pod := range pods {
		wait.Add(1)
		go func(index int, pod hostPod) {
			defer wait.Done()
			limit <- struct{}{}
			defer func() { <-limit }()
			samples, err := r.scrape(ctx, pod.Address, func(name string) bool { return hostNames[name] })
			results[index] = hostResult{pod: pod, samples: samples, err: err}
		}(index, pod)
	}
	wait.Wait()

	type gauges struct {
		running, waiting, kvSum float64
		kvHosts                 int
	}
	byDeployment := map[string]*gauges{}
	answered := false
	for _, result := range results {
		if result.err != nil {
			// A host still loading its weights is not serving metrics yet.
			r.logger().Debug("model host metrics unavailable", "pod", result.pod.Name, "error", result.err)
			continue
		}
		answered = true
		pod, samples := result.pod, result.samples
		source := "host:" + pod.Name
		state.nextBaselined[source] = true
		item := point(pod.DeploymentID)
		item.Hosts++
		values := byDeployment[pod.DeploymentID]
		if values == nil {
			values = &gauges{}
			byDeployment[pod.DeploymentID] = values
		}
		if running, ok := sumSeries(samples, "vllm:num_requests_running", nil); ok {
			values.running += running
		}
		if waiting, ok := sumSeries(samples, "vllm:num_requests_waiting", nil); ok {
			values.waiting += waiting
		}
		kv, ok := sumSeries(samples, "vllm:kv_cache_usage_perc", nil)
		if !ok {
			kv, ok = sumSeries(samples, "vllm:gpu_cache_usage_perc", nil)
		}
		if ok {
			values.kvSum += kv * 100
			values.kvHosts++
		}

		item.TTFT = item.TTFT.Merge(state.histogram(source, "ttft",
			collectHistogram(samples, "vllm:time_to_first_token_seconds", nil)))
		itl := collectHistogram(samples, "vllm:inter_token_latency_seconds", nil)
		if itl == nil {
			itl = collectHistogram(samples, "vllm:time_per_output_token_seconds", nil)
		}
		item.ITL = item.ITL.Merge(state.histogram(source, "itl", itl))

		// The gateway is the authority on requests, latency and tokens as the caller saw
		// them. Without it, the engine's own numbers are the best available.
		generated, hasGenerated := sumSeries(samples, "vllm:generation_tokens_total", nil)
		generatedDelta := state.counter(source, "generation_tokens", generated)
		prompt, _ := sumSeries(samples, "vllm:prompt_tokens_total", nil)
		promptDelta := state.counter(source, "prompt_tokens", prompt)
		e2e := state.histogram(source, "e2e", collectHistogram(samples, "vllm:e2e_request_latency_seconds", nil))
		success, _ := sumSeries(samples, "vllm:request_success_total", nil)
		successDelta := state.counter(source, "success", success)
		if !gatewayRead {
			item.Latency = item.Latency.Merge(e2e)
			item.InputTokens += promptDelta
			if successDelta > 0 {
				if item.Requests == nil {
					item.Requests = map[string]float64{}
				}
				item.Requests["2xx"] += successDelta
			}
		}
		// Generated tokens are counted as they are produced, while the gateway counts them
		// when a request finishes, so the engine's count draws a truer throughput line.
		if hasGenerated {
			item.OutputTokens = item.OutputTokens - gatewayOutput(item, gatewayRead) + generatedDelta
		}
	}
	for id, values := range byDeployment {
		item := point(id)
		running, waiting := values.running, values.waiting
		item.Running, item.Waiting = &running, &waiting
		if values.kvHosts > 0 {
			kv := values.kvSum / float64(values.kvHosts)
			item.KVCachePercent = &kv
		}
	}
	return answered
}

// gatewayOutput returns the gateway's output-token count for a deployment the first time a
// host of that deployment replaces it with the engine's count, and zero afterwards.
func gatewayOutput(item *DeploymentPoint, gatewayRead bool) float64 {
	if !gatewayRead || item.outputFromEngine {
		return 0
	}
	item.outputFromEngine = true
	return item.OutputTokens
}

// --- GPUs -------------------------------------------------------------------------------

type endpoints struct {
	Subsets []struct {
		Addresses []struct {
			IP       string `json:"ip"`
			NodeName string `json:"nodeName"`
		} `json:"addresses"`
		Ports []struct {
			Name string `json:"name"`
			Port int    `json:"port"`
		} `json:"ports"`
	} `json:"subsets"`
}

var dcgmNames = map[string]bool{
	"DCGM_FI_DEV_GPU_UTIL":    true,
	"DCGM_FI_DEV_FB_USED":     true,
	"DCGM_FI_DEV_FB_FREE":     true,
	"DCGM_FI_DEV_FB_RESERVED": true,
}

type gpuReading struct {
	GPUPoint
	pod, podNamespace string
}

// collectGPUs reads every DCGM exporter behind the configured Services: one exporter pod
// per GPU node, each reporting the devices on its node.
func (r *Reporter) collectGPUs(ctx context.Context, pods []hostPod) ([]GPUPoint, bool) {
	if r.Kube == nil || len(r.DCGM) == 0 {
		return nil, false
	}
	readings := []gpuReading{}
	answered := false
	for _, service := range r.DCGM {
		path := "/api/v1/namespaces/" + url.PathEscape(service.Namespace) + "/endpoints/" +
			url.PathEscape(service.Name)
		var found endpoints
		if err := r.Kube.Get(ctx, path, &found); err != nil {
			r.logger().Debug("could not read DCGM exporter endpoints", "service",
				service.Namespace+"/"+service.Name, "error", err)
			continue
		}
		for _, subset := range found.Subsets {
			port := 0
			for _, candidate := range subset.Ports {
				if port == 0 || candidate.Name == "metrics" {
					port = candidate.Port
				}
			}
			if port == 0 {
				continue
			}
			for _, address := range subset.Addresses {
				endpoint := "http://" + net.JoinHostPort(address.IP, strconv.Itoa(port)) + "/metrics"
				samples, err := r.scrape(ctx, endpoint, func(name string) bool { return dcgmNames[name] })
				if err != nil {
					r.logger().Debug("DCGM exporter unavailable", "node", address.NodeName, "error", err)
					continue
				}
				answered = true
				readings = append(readings, parseDCGM(samples, address.NodeName)...)
			}
		}
	}

	podDeployment := map[string]string{}
	nodeDeployments := map[string]map[string]bool{}
	for _, pod := range pods {
		podDeployment[pod.Name] = pod.DeploymentID
		if nodeDeployments[pod.Node] == nil {
			nodeDeployments[pod.Node] = map[string]bool{}
		}
		nodeDeployments[pod.Node][pod.DeploymentID] = true
	}
	gpus := make([]GPUPoint, 0, len(readings))
	for _, reading := range readings {
		gpu := reading.GPUPoint
		switch {
		case reading.pod != "":
			// DCGM's Kubernetes mapping names the pod holding the device; only one of ours
			// counts, never a workload in another namespace.
			if reading.podNamespace == "" || reading.podNamespace == r.Namespace {
				gpu.DeploymentID = podDeployment[reading.pod]
			}
		case len(nodeDeployments[gpu.Node]) == 1:
			for id := range nodeDeployments[gpu.Node] {
				gpu.DeploymentID = id
			}
		}
		gpus = append(gpus, gpu)
	}
	sort.Slice(gpus, func(i, j int) bool {
		if gpus[i].Node != gpus[j].Node {
			return gpus[i].Node < gpus[j].Node
		}
		left, _ := strconv.Atoi(gpus[i].Index)
		right, _ := strconv.Atoi(gpus[j].Index)
		return left < right
	})
	return gpus, answered
}

func parseDCGM(samples []promSample, node string) []gpuReading {
	byDevice := map[string]*gpuReading{}
	order := []string{}
	reserved := map[string]float64{}
	free := map[string]float64{}
	for _, sample := range samples {
		index := sample.Labels["gpu"]
		if index == "" {
			continue
		}
		reading := byDevice[index]
		if reading == nil {
			nodeName := node
			if nodeName == "" {
				nodeName = sample.Labels["Hostname"]
			}
			reading = &gpuReading{GPUPoint: GPUPoint{
				Node: clip(nodeName, 253), Index: clip(index, 16), Product: clip(sample.Labels["modelName"], 128),
			}}
			byDevice[index] = reading
			order = append(order, index)
		}
		if pod := sample.Labels["pod"]; pod != "" {
			reading.pod, reading.podNamespace = pod, sample.Labels["namespace"]
		}
		switch sample.Name {
		case "DCGM_FI_DEV_GPU_UTIL":
			reading.UtilizationPercent = clamp(sample.Value, 0, 100)
		case "DCGM_FI_DEV_FB_USED":
			reading.MemoryUsedBytes = sample.Value * mebibyte
		case "DCGM_FI_DEV_FB_FREE":
			free[index] = sample.Value * mebibyte
		case "DCGM_FI_DEV_FB_RESERVED":
			reserved[index] = sample.Value * mebibyte
		}
	}
	out := make([]gpuReading, 0, len(order))
	for _, index := range order {
		reading := byDevice[index]
		reading.MemoryTotalBytes = reading.MemoryUsedBytes + free[index] + reserved[index]
		out = append(out, *reading)
	}
	return out
}

// attributeGPUs adds each deployment's devices to its point: mean utilisation, summed memory.
func attributeGPUs(gpus []GPUPoint, point func(string) *DeploymentPoint) {
	type totals struct {
		count             int
		util, used, total float64
	}
	byDeployment := map[string]*totals{}
	for _, gpu := range gpus {
		if gpu.DeploymentID == "" {
			continue
		}
		entry := byDeployment[gpu.DeploymentID]
		if entry == nil {
			entry = &totals{}
			byDeployment[gpu.DeploymentID] = entry
		}
		entry.count++
		entry.util += gpu.UtilizationPercent
		entry.used += gpu.MemoryUsedBytes
		entry.total += gpu.MemoryTotalBytes
	}
	for id, entry := range byDeployment {
		item := point(id)
		util := entry.util / float64(entry.count)
		used, total := entry.used, entry.total
		item.GPUs = entry.count
		item.GPUUtilizationPercent, item.GPUMemoryUsedBytes, item.GPUMemoryTotalBytes = &util, &used, &total
	}
}

// --- helpers ----------------------------------------------------------------------------

func (r *Reporter) scrape(ctx context.Context, endpoint string, keep func(string) bool) ([]promSample, error) {
	client := r.HTTP
	if client == nil {
		client = &http.Client{Timeout: scrapeTimeout}
	}
	ctx, cancel := context.WithTimeout(ctx, scrapeTimeout)
	defer cancel()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
	if err != nil {
		return nil, err
	}
	request.Header.Set("Accept", "text/plain;version=0.0.4")
	response, err := client.Do(request)
	if err != nil {
		return nil, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("status %d", response.StatusCode)
	}
	return parseExposition(response.Body, keep), nil
}

func clamp(value, low, high float64) float64 {
	if value < low {
		return low
	}
	if value > high {
		return high
	}
	return value
}

func clip(value string, limit int) string {
	if len(value) <= limit {
		return value
	}
	cut := limit
	for cut > 0 && (value[cut]&0xC0) == 0x80 {
		cut--
	}
	return value[:cut]
}
