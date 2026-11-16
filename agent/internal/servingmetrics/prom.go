package servingmetrics

import (
	"bufio"
	"io"
	"math"
	"sort"
	"strconv"
	"strings"
)

// promSample is one line of a Prometheus text exposition.
type promSample struct {
	Name   string
	Labels map[string]string
	Value  float64
}

// maxScrapeBytes bounds how much of one metrics endpoint is read. A vLLM host exposes a few
// hundred kilobytes at most; anything larger is not a metrics page this package understands.
const maxScrapeBytes = 4 << 20

// parseExposition reads the samples whose name keep accepts. Labels are kept, unlike the
// collector's allowlist parser, because the deployment, outcome, bucket bound and GPU index
// all live in labels. A deliberately small parser: the module has no dependencies and the
// format is line-oriented.
func parseExposition(reader io.Reader, keep func(name string) bool) []promSample {
	samples := []promSample{}
	scanner := bufio.NewScanner(io.LimitReader(reader, maxScrapeBytes))
	scanner.Buffer(make([]byte, 0, 64<<10), 1<<20)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || line[0] == '#' {
			continue
		}
		nameEnd := strings.IndexAny(line, "{ \t")
		if nameEnd <= 0 {
			continue
		}
		name := line[:nameEnd]
		if !keep(name) {
			continue
		}
		rest := line[nameEnd:]
		labels := map[string]string{}
		if rest[0] == '{' {
			parsed, remainder, ok := parseLabels(rest[1:])
			if !ok {
				continue
			}
			labels, rest = parsed, remainder
		}
		fields := strings.Fields(rest)
		if len(fields) == 0 {
			continue
		}
		// An optional timestamp may follow the value; the value is always the first field.
		value, err := strconv.ParseFloat(fields[0], 64)
		if err != nil || math.IsNaN(value) {
			continue
		}
		samples = append(samples, promSample{Name: name, Labels: labels, Value: value})
	}
	return samples
}

// parseLabels reads `key="value",...}` and returns what follows the closing brace.
func parseLabels(text string) (map[string]string, string, bool) {
	labels := map[string]string{}
	index := 0
	for {
		for index < len(text) && (text[index] == ' ' || text[index] == ',') {
			index++
		}
		if index >= len(text) {
			return nil, "", false
		}
		if text[index] == '}' {
			return labels, text[index+1:], true
		}
		equals := strings.IndexByte(text[index:], '=')
		if equals < 0 {
			return nil, "", false
		}
		key := strings.TrimSpace(text[index : index+equals])
		index += equals + 1
		if index >= len(text) || text[index] != '"' {
			return nil, "", false
		}
		index++
		var value strings.Builder
		closed := false
		for index < len(text) {
			char := text[index]
			if char == '\\' && index+1 < len(text) {
				switch text[index+1] {
				case 'n':
					value.WriteByte('\n')
				default:
					value.WriteByte(text[index+1])
				}
				index += 2
				continue
			}
			if char == '"' {
				closed = true
				index++
				break
			}
			value.WriteByte(char)
			index++
		}
		if !closed {
			return nil, "", false
		}
		labels[key] = value.String()
	}
}

// Histogram is a latency distribution as per-bucket counts, keyed by the bucket's upper
// bound ("0.25", "+Inf"). Not cumulative: each count is the observations above the previous
// bound and at or below this one, so histograms from two hosts or two intervals merge by
// adding counts, which is what the control plane does.
type Histogram map[string]float64

// cumulative is a Prometheus histogram as scraped: cumulative counts by bound.
type cumulative map[string]float64

func normalizeBound(raw string) (string, float64, bool) {
	if raw == "+Inf" || raw == "Inf" || raw == "inf" {
		return "+Inf", math.Inf(1), true
	}
	value, err := strconv.ParseFloat(raw, 64)
	if err != nil || math.IsNaN(value) {
		return "", 0, false
	}
	if math.IsInf(value, 1) {
		return "+Inf", value, true
	}
	return strconv.FormatFloat(value, 'g', -1, 64), value, true
}

func boundValue(key string) float64 {
	_, value, _ := normalizeBound(key)
	return value
}

func sortedBounds[V any](values map[string]V) []string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	sort.Slice(keys, func(i, j int) bool { return boundValue(keys[i]) < boundValue(keys[j]) })
	return keys
}

// histogramDelta turns two cumulative scrapes into the per-bucket counts observed between
// them. A count that went down means the process restarted, so the current scrape is the
// whole interval.
func histogramDelta(current, previous cumulative) Histogram {
	reset := false
	for bound, value := range current {
		if value < previous[bound] {
			reset = true
			break
		}
	}
	diff := cumulative{}
	for bound, value := range current {
		if reset {
			diff[bound] = value
		} else {
			diff[bound] = value - previous[bound]
		}
	}
	out := Histogram{}
	below := 0.0
	for _, bound := range sortedBounds(diff) {
		count := diff[bound] - below
		if count > 0 {
			out[bound] = count
		}
		if diff[bound] > below {
			below = diff[bound]
		}
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// Merge adds other's counts into h.
func (h Histogram) Merge(other Histogram) Histogram {
	if len(other) == 0 {
		return h
	}
	if h == nil {
		h = Histogram{}
	}
	for bound, count := range other {
		h[bound] += count
	}
	return h
}

// Quantile estimates the q-quantile by linear interpolation within the bucket that holds it,
// as Prometheus's histogram_quantile does. An observation in the +Inf bucket reports the
// largest finite bound.
func (h Histogram) Quantile(q float64) (float64, bool) {
	total := 0.0
	for _, count := range h {
		total += count
	}
	if total <= 0 {
		return 0, false
	}
	rank := q * total
	seen := 0.0
	lower := 0.0
	largestFinite := 0.0
	for _, bound := range sortedBounds(h) {
		upper := boundValue(bound)
		count := h[bound]
		if !math.IsInf(upper, 1) {
			largestFinite = upper
		}
		if seen+count >= rank && count > 0 {
			if math.IsInf(upper, 1) {
				return largestFinite, true
			}
			return lower + (upper-lower)*((rank-seen)/count), true
		}
		seen += count
		if !math.IsInf(upper, 1) {
			lower = upper
		}
	}
	return largestFinite, true
}

// collectHistogram sums every series of a histogram matching match into one cumulative set.
// Series that differ only in labels the caller does not care about (model name, engine)
// share bucket bounds, so summing the cumulative counts is exact.
func collectHistogram(samples []promSample, name string, match func(map[string]string) bool) cumulative {
	out := cumulative{}
	for _, sample := range samples {
		if sample.Name != name+"_bucket" || (match != nil && !match(sample.Labels)) {
			continue
		}
		bound, _, ok := normalizeBound(sample.Labels["le"])
		if !ok {
			continue
		}
		out[bound] += sample.Value
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// sumSeries adds the values of every series of name matching match, and says whether any
// series was present at all.
func sumSeries(samples []promSample, name string, match func(map[string]string) bool) (float64, bool) {
	total, found := 0.0, false
	for _, sample := range samples {
		if sample.Name == name && (match == nil || match(sample.Labels)) {
			total += sample.Value
			found = true
		}
	}
	return total, found
}
