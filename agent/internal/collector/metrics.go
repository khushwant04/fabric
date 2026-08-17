package collector

import (
	"bufio"
	"context"
	"fmt"
	"net/http"
	"os/exec"
	"strconv"
	"strings"
	"time"
)

// GPUSample is one reading of one device.
//
// Deliberately a small fixed set rather than everything nvidia-smi can produce. These are
// the fields that answer the questions a stamp operator actually has: is the device busy,
// is it out of memory, and is it throttling. Collecting more would look thorough while
// adding fields nobody reads and cannot interpret.
type GPUSample struct {
	Index             int     `json:"index"`
	Product           string  `json:"product"`
	UtilizationGPU    float64 `json:"utilization_gpu_percent"`
	UtilizationMemory float64 `json:"utilization_memory_percent"`
	MemoryUsedMiB     float64 `json:"memory_used_mib"`
	MemoryTotalMiB    float64 `json:"memory_total_mib"`
	TemperatureC      float64 `json:"temperature_c"`
	PowerDrawW        float64 `json:"power_draw_w"`
	SMClockMHz        float64 `json:"sm_clock_mhz"`
	SMClockMaxMHz     float64 `json:"sm_clock_max_mhz"`
}

// ClockFraction reports how close the device is running to its maximum clock.
//
// Recorded as a ratio rather than a boolean "throttling", because the reason a clock is
// low cannot be read from the clock alone: it may be idle, power limited, or thermally
// limited, and asserting one would be a guess.
func (s GPUSample) ClockFraction() float64 {
	if s.SMClockMaxMHz == 0 {
		return 0
	}
	return s.SMClockMHz / s.SMClockMaxMHz
}

//nolint:lll // the query is one line by nvidia-smi's design
const gpuQuery = "index,name,utilization.gpu,utilization.memory,memory.used,memory.total," +
	"temperature.gpu,power.draw,clocks.sm,clocks.max.sm"

// SampleGPUs reads the devices visible to this pod.
//
// Uses nvidia-smi rather than a library binding: the binary is present wherever the
// driver is, needs no CGO, and keeps this module dependency-free. A stamp without a GPU
// returns no samples and no error, because that is a normal configuration rather than a
// failure.
func SampleGPUs(ctx context.Context) ([]GPUSample, error) {
	command := exec.CommandContext(
		ctx, "nvidia-smi",
		"--query-gpu="+gpuQuery,
		"--format=csv,noheader,nounits",
	)
	output, err := command.Output()
	if err != nil {
		// Absent driver or binary is not an error worth failing a pass for; the caller
		// records that no samples were available.
		return nil, nil
	}

	samples := make([]GPUSample, 0, 1)
	scanner := bufio.NewScanner(strings.NewReader(string(output)))
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		fields := strings.Split(line, ",")
		if len(fields) != 10 {
			return nil, fmt.Errorf("unexpected nvidia-smi output: %q", line)
		}
		for i := range fields {
			fields[i] = strings.TrimSpace(fields[i])
		}

		number := func(raw string) float64 {
			// "[N/A]" appears for values a device does not report, and treating it as
			// zero is better than dropping the whole sample.
			value, convErr := strconv.ParseFloat(raw, 64)
			if convErr != nil {
				return 0
			}
			return value
		}

		index, _ := strconv.Atoi(fields[0])
		samples = append(samples, GPUSample{
			Index:             index,
			Product:           fields[1],
			UtilizationGPU:    number(fields[2]),
			UtilizationMemory: number(fields[3]),
			MemoryUsedMiB:     number(fields[4]),
			MemoryTotalMiB:    number(fields[5]),
			TemperatureC:      number(fields[6]),
			PowerDrawW:        number(fields[7]),
			SMClockMHz:        number(fields[8]),
			SMClockMaxMHz:     number(fields[9]),
		})
	}
	return samples, scanner.Err()
}

// RuntimeSample is what the model host reports about itself.
//
// Scraped from the host's own metrics endpoint rather than inferred from proxied
// requests, because queue depth and cache utilisation are only visible inside the server.
type RuntimeSample struct {
	Source           string             `json:"source"`
	Reachable        bool               `json:"reachable"`
	Values           map[string]float64 `json:"values,omitempty"`
	UnreachableCause string             `json:"unreachable_cause,omitempty"`
}

//nolint:gochecknoglobals // a fixed allowlist, not configuration
var runtimeMetricNames = []string{
	// vLLM's own names. Only these are kept: a full scrape would carry hundreds of
	// series that nothing consumes and that change between versions.
	"vllm:num_requests_running",
	"vllm:num_requests_waiting",
	"vllm:gpu_cache_usage_perc",
	"vllm:num_preemptions_total",
	"vllm:generation_tokens_total",
	"vllm:prompt_tokens_total",
}

// ScrapeRuntime reads the model host's metrics endpoint.
func newRequest(ctx context.Context, endpoint string) (*http.Request, error) {
	return http.NewRequestWithContext(ctx, http.MethodGet, endpoint, nil)
}

func (c *Collector) ScrapeRuntime(ctx context.Context, endpoint string) RuntimeSample {
	sample := RuntimeSample{Source: endpoint}
	if endpoint == "" {
		return sample
	}

	request, err := newRequest(ctx, endpoint)
	if err != nil {
		sample.UnreachableCause = err.Error()
		return sample
	}

	response, err := c.Source.HTTP.Do(request)
	if err != nil {
		// A host that is still loading weights is not yet serving metrics, which is
		// expected rather than broken.
		sample.UnreachableCause = err.Error()
		return sample
	}
	defer response.Body.Close()

	if response.StatusCode >= 300 {
		sample.UnreachableCause = fmt.Sprintf("status %d", response.StatusCode)
		return sample
	}

	sample.Reachable = true
	sample.Values = parsePrometheus(response.Body, runtimeMetricNames)
	return sample
}

// parsePrometheus extracts the named metrics from a text exposition.
//
// A deliberately small parser: the format is line-oriented, only the allowlisted names
// are wanted, and pulling in a Prometheus client library for this would add a dependency
// tree to a module that has none.
func parsePrometheus(reader interface{ Read([]byte) (int, error) }, names []string) map[string]float64 {
	wanted := make(map[string]struct{}, len(names))
	for _, name := range names {
		wanted[name] = struct{}{}
	}

	values := map[string]float64{}
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 0, 64*1024), 1<<20)
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}

		// name{labels} value  — labels are dropped, since a stamp serves one model and
		// per-label series would need a dimensional store this does not have.
		name := line
		if brace := strings.IndexByte(line, '{'); brace >= 0 {
			name = line[:brace]
		} else if space := strings.IndexByte(line, ' '); space >= 0 {
			name = line[:space]
		}
		if _, keep := wanted[name]; !keep {
			continue
		}

		parts := strings.Fields(line)
		if len(parts) < 2 {
			continue
		}
		value, err := strconv.ParseFloat(parts[len(parts)-1], 64)
		if err != nil {
			continue
		}
		// Summed across label sets, which is the meaningful aggregate for counters and
		// close enough for the gauges here given one model per stamp.
		values[name] += value
	}
	return values
}

// MetricsReport is one sampling pass, as the control plane receives it.
type MetricsReport struct {
	CollectedAt string        `json:"collected_at"`
	GPUs        []GPUSample   `json:"gpus"`
	Runtime     RuntimeSample `json:"runtime"`
}

// CollectMetrics takes one sample of the device and the runtime.
func (c *Collector) CollectMetrics(ctx context.Context, runtimeEndpoint string) MetricsReport {
	report := MetricsReport{CollectedAt: time.Now().UTC().Format(time.RFC3339)}

	gpus, err := SampleGPUs(ctx)
	if err != nil {
		c.logf("could not sample GPUs: %v", err)
	}
	report.GPUs = gpus
	report.Runtime = c.ScrapeRuntime(ctx, runtimeEndpoint)
	return report
}

// ForwardMetrics samples and reports once.
//
// A failure is logged and dropped rather than queued. The next sample replaces this one,
// so holding it would deliver a reading that is no longer true, and the queue exists for
// usage where every record matters.
func (c *Collector) ForwardMetrics(ctx context.Context, runtimeEndpoint string) error {
	report := c.CollectMetrics(ctx, runtimeEndpoint)
	if err := c.Control.ReportMetrics(ctx, report); err != nil {
		return fmt.Errorf("report metrics: %w", err)
	}
	return nil
}
