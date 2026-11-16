// Package activity is a stamp's registration and activity log, as the agent sees it.
//
// Right after running the bootstrap command, an owner wants to watch the console say
// "Enrolled → 1 × RTX PRO 6000 found (96 GiB) → GPU runtime OK → Ready for deployments", and
// later see when credentials rotated, a desired-state generation was applied, a model-host
// image was pulled, or something failed. Those moments happen inside the agent, and the
// control plane never connects to a stamp (ADR 0001, ADR 0007), so the agent records them
// here and pushes them to POST /v1/stamps/{id}/events.
//
// Recording never blocks and never fails: events wait in a bounded buffer until an upload
// succeeds, and the oldest are dropped if the control plane stays unreachable for long. Each
// event carries an id the control plane deduplicates on, so an upload whose reply was lost is
// simply sent again. Credential-shaped text is masked before an event is buffered.
package activity

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"log/slog"
	"regexp"
	"strconv"
	"sync"
	"time"
)

// Severity colours the console's timeline.
type Severity string

const (
	Info    Severity = "info"
	Success Severity = "success"
	Warning Severity = "warning"
	Error   Severity = "error"
)

// Kinds the control plane accepts. Its schema rejects anything else.
const (
	KindEnrolled            = "enrolled"
	KindIdentityReused      = "identity_reused"
	KindGPUsMeasured        = "gpus_measured"
	KindGPURuntime          = "gpu_runtime"
	KindCredentialsRotated  = "credentials_rotated"
	KindDesiredStateApplied = "desired_state_applied"
	KindImagePullStarted    = "image_pull_started"
	KindImagePullFinished   = "image_pull_finished"
	KindModelHostFailed     = "model_host_failed"
	KindReady               = "ready"
	KindError               = "error"
)

const (
	// MaxPending bounds the buffer while the control plane is unreachable.
	MaxPending = 200
	// MaxBatch matches the control plane's per-request limit.
	MaxBatch = 50
	// MaxDetails matches the control plane's limit on detail keys.
	MaxDetails = 16

	maxMessage = 500
	maxDetail  = 300
	// maxKeys bounds the de-duplication memory; it is cleared when full, which at worst
	// records one repeated state again.
	maxKeys = 2048

	flushDelay         = 500 * time.Millisecond
	idleFlush          = 30 * time.Second
	retryMin           = 2 * time.Second
	retryMax           = time.Minute
	NotAcceptedBackoff = 5 * time.Minute
)

// Event is one entry, in the shape the control plane's schema expects.
type Event struct {
	ID       string         `json:"id"`
	Kind     string         `json:"kind"`
	Severity Severity       `json:"severity"`
	Message  string         `json:"message"`
	Hint     string         `json:"hint,omitempty"`
	Details  map[string]any `json:"details"`
	At       time.Time      `json:"at"`
}

// Sender uploads a batch. It returns ErrNotAccepted when the control plane has no
// activity endpoint, so the recorder backs off rather than retrying every few seconds.
type Sender func(ctx context.Context, events []Event) error

// ErrNotAccepted marks a control plane that predates the activity log.
var ErrNotAccepted = errors.New("control plane does not accept stamp activity")

// Recorder buffers events and uploads them. The zero value is not usable; use New.
type Recorder struct {
	log    *slog.Logger
	now    func() time.Time
	mu     sync.Mutex
	queue  []Event
	states map[string]string
	notify chan struct{}
}

// New returns an empty recorder. A nil logger uses slog's default.
func New(log *slog.Logger) *Recorder {
	if log == nil {
		log = slog.Default()
	}
	return &Recorder{
		log:    log,
		now:    time.Now,
		states: map[string]string{},
		notify: make(chan struct{}, 1),
	}
}

// SetClock replaces the clock, for tests.
func (r *Recorder) SetClock(now func() time.Time) { r.now = now }

// Record buffers an event. Missing ids and times are filled in, text is masked and
// clipped, and the oldest pending event is dropped when the buffer is full.
func (r *Recorder) Record(event Event) {
	if r == nil {
		return
	}
	if event.ID == "" {
		event.ID = newID()
	}
	if event.At.IsZero() {
		event.At = r.now().UTC()
	}
	if event.Severity == "" {
		event.Severity = Info
	}
	event.Message = clip(Redact(event.Message), maxMessage)
	event.Hint = clip(Redact(event.Hint), maxMessage)
	details := make(map[string]any, len(event.Details))
	for key, value := range event.Details {
		if len(details) == MaxDetails {
			break
		}
		if text, ok := value.(string); ok {
			value = clip(Redact(text), maxDetail)
		}
		details[key] = value
	}
	event.Details = details

	r.mu.Lock()
	r.queue = append(r.queue, event)
	if len(r.queue) > MaxPending {
		r.queue = r.queue[len(r.queue)-MaxPending:]
	}
	r.mu.Unlock()
	select {
	case r.notify <- struct{}{}:
	default:
	}
}

// RecordChange records the event only when state differs from what was last recorded under
// key, so a measurement repeated every minute appears once and again only when it changes.
// It reports whether the event was recorded.
func (r *Recorder) RecordChange(key, state string, event Event) bool {
	if r == nil {
		return false
	}
	r.mu.Lock()
	if previous, seen := r.states[key]; seen && previous == state {
		r.mu.Unlock()
		return false
	}
	if len(r.states) >= maxKeys {
		r.states = map[string]string{}
	}
	r.states[key] = state
	r.mu.Unlock()
	r.Record(event)
	return true
}

// Forget clears the state remembered under key, so the next RecordChange records again.
// Used when a failure clears: a later recurrence is news.
func (r *Recorder) Forget(key string) {
	if r == nil {
		return
	}
	r.mu.Lock()
	delete(r.states, key)
	r.mu.Unlock()
}

// Pending returns a copy of the buffered events, oldest first.
func (r *Recorder) Pending() []Event {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]Event(nil), r.queue...)
}

// Run uploads buffered events until the context ends: shortly after something is
// recorded, and periodically otherwise. A failed upload keeps the events and retries with
// backoff; it never stops the agent, because the log is for a human and serving does not
// depend on it.
func (r *Recorder) Run(ctx context.Context, send Sender) {
	retry := time.Duration(0)
	var lastError string
	for {
		wait := idleFlush
		if retry > 0 {
			wait = retry
		}
		timer := time.NewTimer(wait)
		select {
		case <-ctx.Done():
			timer.Stop()
			return
		case <-r.notify:
			timer.Stop()
			if retry > 0 {
				// Keep backing off; a new event does not make the control plane reachable.
				continue
			}
			// Let a burst (enrolled, measured, runtime checked) go up as one request.
			select {
			case <-ctx.Done():
				return
			case <-time.After(flushDelay):
			}
		case <-timer.C:
		}

		err := r.flush(ctx, send)
		switch {
		case err == nil:
			retry = 0
			if lastError != "" {
				r.log.Info("stamp activity uploads recovered")
				lastError = ""
			}
		case errors.Is(err, ErrNotAccepted):
			retry = NotAcceptedBackoff
		default:
			if retry == 0 {
				retry = retryMin
			} else if retry *= 2; retry > retryMax {
				retry = retryMax
			}
		}
		if err != nil && ctx.Err() == nil && err.Error() != lastError {
			r.log.Warn("stamp activity upload failed", "error", err)
			lastError = err.Error()
		}
	}
}

// flush sends everything pending in batches, removing what the control plane accepted.
func (r *Recorder) flush(ctx context.Context, send Sender) error {
	for {
		r.mu.Lock()
		batch := append([]Event(nil), r.queue[:min(len(r.queue), MaxBatch)]...)
		r.mu.Unlock()
		if len(batch) == 0 {
			return nil
		}
		if err := send(ctx, batch); err != nil {
			return err
		}
		r.mu.Lock()
		// Events may have been dropped from the front while the upload ran; remove the sent
		// ones by id rather than by position.
		sent := make(map[string]bool, len(batch))
		for _, event := range batch {
			sent[event.ID] = true
		}
		kept := r.queue[:0]
		for _, event := range r.queue {
			if !sent[event.ID] {
				kept = append(kept, event)
			}
		}
		r.queue = kept
		r.mu.Unlock()
	}
}

// --- masking -------------------------------------------------------------------------

var secretPatterns = []*regexp.Regexp{
	regexp.MustCompile(`hf_[A-Za-z0-9]{20,}`),
	regexp.MustCompile(`(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+`),
	regexp.MustCompile(`(?i)(authorization\s*[:=]\s*)\S+`),
	regexp.MustCompile(`\bsk-[A-Za-z0-9_-]{20,}`),
	regexp.MustCompile(`\bAKIA[0-9A-Z]{16}\b`),
	regexp.MustCompile(`\bfab_(?:key|enroll|agent|telem)_[A-Za-z0-9_-]+`),
	regexp.MustCompile(`\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+`),
	regexp.MustCompile(`(?i)((?:api[_-]?key|access[_-]?token|secret|password|passwd|token)["']?\s*[:=]\s*)\S+`),
	regexp.MustCompile(`-----BEGIN [A-Z ]*PRIVATE KEY-----`),
}

// Redacted replaces a masked credential.
const Redacted = "[redacted]"

// Redact masks credential-shaped substrings, keeping a "key=" prefix where there is one.
func Redact(text string) string {
	for _, pattern := range secretPatterns {
		if pattern.NumSubexp() > 0 {
			text = pattern.ReplaceAllString(text, "${1}"+Redacted)
		} else {
			text = pattern.ReplaceAllString(text, Redacted)
		}
	}
	return text
}

func clip(s string, n int) string {
	if len(s) <= n {
		return s
	}
	const marker = "…"
	cut := n - len(marker)
	for cut > 0 && (s[cut]&0xC0) == 0x80 {
		cut--
	}
	return s[:cut] + marker
}

func newID() string {
	var raw [12]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return "evt-" + strconv.FormatInt(time.Now().UnixNano(), 10)
	}
	return "evt-" + hex.EncodeToString(raw[:])
}
