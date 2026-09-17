# ADR 0014: The gateway asks a streamed response to report its usage, and consumes the frame it caused

**Decision status:** Accepted  
**Implementation status:** Implemented  
**Date:** 2026-09-17

## Context

Usage was recorded in one place, `DataPlane.record_usage`, from two call sites. The
non-streaming path passed the upstream JSON body and got real numbers out of `usage`. The
streaming path passed `{}`:

```python
# M6 will parse the terminal usage event; M2 preserves the existing zero-token
# streamed record rather than inventing usage.
plane.record_usage(principal, deployment, {}, streamed=True)
```

So every streamed request produced a usage row of zero tokens. Streaming is the default for
chat clients, which means most of the platform's usage was uncounted — and uncounted in the
worst way, because a zero-token row is a *positive assertion* that the request cost nothing.
Downstream that row is indistinguishable from a real answer with no output. The same gap
existed in `fabric_dp_tokens_total`, which is fed only from `request_finished`'s token
arguments and the streaming path passed none.

Two further details made the old record wrong beyond its numbers. It was written *before* the
response iterator started, so `occurred_at` was the request-start time; and it was written
unconditionally, so a stream that failed outright still produced a usage row while a failed
non-streaming request produced none.

The tokens were never absent from the protocol. An OpenAI-compatible server emits a terminal
frame carrying a `usage` object when the request asks for one with
`stream_options.include_usage`. Nothing in this repository asked, and the relay
(`async for chunk in upstream.aiter_bytes(): yield chunk`) forwarded bytes without looking at
them, so even a client that asked for usage itself had its tokens pass through uncounted.

## Decision

### 1. Ask for usage on every streamed request

The streaming branch merges `stream_options: {"include_usage": true}` into the upstream body,
preserving any other `stream_options` keys the client set. Only the streaming branch does this:
vLLM rejects `stream_options` on a non-streaming request.

### 2. Consume the frame Fabric caused, forward the frame the client asked for

Asking for usage adds an event the caller did not request — a frame with an empty `choices`
array and a `usage` object — and a strict client may reject it. So the gateway tracks who asked:

- the client set `include_usage` itself: the frame is forwarded untouched;
- Fabric added it: the usage-only frame is consumed and never reaches the client;
- a frame that carries usage *and* content (what `continuous_usage_stats` produces) is always
  forwarded, because its content belongs to the caller regardless of who asked.

Suppression is a **positive test against a known shape**: `usage` present, `choices` present and
empty, and every other key drawn from a small set of known chunk metadata. A frame that omits
`choices`, or carries a key this gateway does not model, may be an answer in a shape it does not
recognise, so it is forwarded. Deciding by "this did not look like content" is what deletes a
customer's answer, and that direction of error is not recoverable.

Suppression also requires a plain SSE envelope. A frame carrying `event:`, `id:`, `retry:` or a
comment has client-visible semantics this gateway does not model, so it remains opaque and is
forwarded even when its `data:` JSON resembles the terminal usage shape. Multiple `data:` lines
remain valid and are joined with a newline as SSE specifies. Field processing removes exactly one
optional ASCII space after `data:` and no other whitespace; a colonless `data` line is the
standards-valid empty data field and therefore invalidates any earlier subtotal while passing
through byte-exact. Broad trimming could erase bytes SSE preserves and turn an opaque, non-JSON
value into suppressible JSON. The resulting value must then
be strict UTF-8, BOM-free, interoperable JSON: duplicate object members, UTF-16/32 auto-detection,
and `NaN`/`Infinity` are rejected before they can mutate usage or drive suppression. In particular,
duplicate `choices` cannot hide an earlier content-bearing member when Python builds a dictionary.

The forward decision is made from the frame's shape *before* its numbers are read, so a
usage-only frame with counts the gateway cannot use is still consumed rather than leaking to a
client that never asked for it.

This makes the relay frame-aware rather than byte-aware, which requires splitting the SSE
stream on its blank-line terminator. SSE permits CR, LF or CRLF for each line ending, including
mixed blank boundaries; all eight valid byte spellings are recognized while the original bytes
are retained. Every forwarded frame is the **original bytes**. Nothing is
re-serialised, because a `json.loads`/`json.dumps` round trip would reorder keys, change number
formatting, and silently drop fields this code does not model. Only frames that are deliberately
dropped are parsed as anything other than opaque bytes.

**What this does not claim.** Requesting `include_usage` makes a compliant host attach a `usage`
key to *every* chunk, null on all but the last. Those frames carry content, so they are
forwarded with that key intact and a client that did not ask does see it. Removing it would mean
re-serialising every frame, which the rule above rejects. The guarantee is narrower and exact:
**while the stream is being framed**, no frame reporting token counts reaches a client that did
not request one — including when the host omits the final blank line, because the trailing
remainder gets the same decision. It does not hold for bytes released unparsed at the withhold
ceiling, or for a frame whose `data:` payload exceeds the 1 MiB parse cap (§2a). Both are gateway
limits and latch `framing_lost`; because framing resumes immediately after a release, the
withhold lapse is one frame wide rather than the rest of the stream.

### 2a. What is withheld is bounded, and a retry starts clean

Two properties the frame-aware relay has to preserve that a byte relay got for free.

*Bounded memory, and bounded delay.* A host that never emits a frame terminator could otherwise
make the meter accumulate its entire response. One ceiling applies: **64 KiB of withheld bytes**.
Past it the held bytes are released to the client and framing restarts on whatever arrives next.

The ceiling bounds *withheld* bytes, not frame size, and that is the property to reason about
rather than a limit on how large a frame may be:

- a frame that arrives complete inside a single chunk was never withheld, so it is parsed
  whatever its length (capped only by a 1 MiB limit on what will be handed to a JSON decoder);
- a frame that arrives in pieces is released once the pieces reach the ceiling;
- consequently no client waits on more than 64 KiB, and no stream costs more than that.

Releasing is deliberately *not* fatal to the rest of the stream. A client reassembles the byte
stream regardless of how it was split, so a release is invisible to it; the only thing lost is
the chance to read that one frame. The release retains the last three bytes — one less than the
longest supported terminator — so it cannot land *inside* a boundary and merge two SSE events.
The first frame recovered after a release is always forwarded **without parsing or changing usage
state**, because it begins with the retained tail of bytes whose prefix the client has already been
sent. A suffix such as `dat` could otherwise join later bytes into a fabricated `data:` usage
report and create a false bill. Framing then continues normally, and a terminal usage frame that
arrives after the recovered boundary is still read and still kept off a client that did not ask
for one. What a release does forfeit is trust in a *subtotal*: if a frame went by unread, the last
count that was read may not be the last one sent, so a stream that has released bytes is billed
only from a later, separate usage-only frame (§4).

Parsing has its own **1 MiB payload cap**. A complete frame beyond it is forwarded byte for byte
without JSON decoding, and `framing_lost` is latched for the same reason as a release: the gateway,
not the host, declined to inspect that frame. This is a second exception to suppression — an
oversized usage-only frame reaches a client that never requested it — and a subtotal seen
elsewhere in that stream is not trusted as the total.

An earlier version had two ceilings — a small one until the first blank line, a larger one after
— and abandoned framing permanently on either. Both parts were wrong. The latch meant a single
keep-alive frame lifted the limit for the rest of the stream, restoring exactly the unbounded
buffering the small ceiling was added to remove; and permanence meant one large frame cost that
stream its metering and its suppression guarantee for good.

*Retry safety.* The retry gate keys on bytes **received**, not bytes forwarded. A byte that
arrived proves the host began work on a non-idempotent completion whether or not the gateway
passed it on, and ADR 0011 permits replay only when the request is known not to have reached a
host. Keying on forwarded bytes would let a buffered partial frame read as "nothing happened".
The meter is also reset at the start of every attempt, so a failed attempt's partial frame
cannot be spliced onto the retried stream and its usage cannot be billed against a stream the
client never received.

### 3. Record once, at the end, from what the stream reported

The eager zero-token record is gone. Usage is now recorded in `cleanup_stream`, which already
exists to release the account and backend leases exactly once however the stream ended —
completed, failed at the transport, or abandoned by the client. That is the only place that runs
exactly once, so it is the only place that can record exactly one row per request with a real
completion time.

Cleanup is phased rather than guarded by one eager "done" bit. Usage/request accounting completes
synchronously once; placement admission is released synchronously next; concurrency release is the
only await and runs in one shielded task. If disconnect cancellation arrives while that task waits
on the limiter's lock, the response wrapper awaits the same task in its outer `finally` instead of
skipping release or duplicating accounting. This prevents cancellation from stranding either
account concurrency or placement lifecycle state.

A stream holds placement-metric membership from **admission**, before Starlette enters its body
iterator, through this cleanup. Backend in-flight state begins later, inside the iterator, so it
cannot protect the interval itself: a registry withdrawal between response construction and
iterator entry would otherwise tombstone the loss labels before the captured request reaches its
host. Retirement therefore waits for both admitted streams and backend attempts to drain. Every
retired pool **generation** is retained, not one pool per stable deployment id: the same id can be
withdrawn, reintroduced and withdrawn again before its oldest attempt ends, and replacing the
older retired pool would make that live lease invisible. Router state aggregates equal backend ids
across those generations because its public contract has no generation label. Counter values are
process-lifetime history and remain exposed after retirement; membership only gates whether an
unretained late cleanup can add a new event.

### 4. A total is billed; a subtotal is counted as lost

Usage is recorded when a host **accepted** the request — the same rule the non-streaming path
applies with `response.is_success` — and reported a count this gateway can vouch for as the
*total* for the stream. Two things are read differently:

- a **usage-only frame** is the report `include_usage` asks for. It arrives once, at the end, so
  it is a total on sight. A row is written from it even if the client later disconnected: the work
  was done and measured.
- a count on a **content frame** is what `continuous_usage_stats` produces: a running cumulative
  subtotal. It is this stream's total only if the stream was seen to reach its end with framing
  intact. A stream cut short by a disconnect or a mid-body transport failure is billed nothing.

That distinction is the difference between a bill and a guess. A client that sets
`continuous_usage_stats` and hangs up after two of a thousand output tokens leaves the meter
holding `2`; recording it would under-bill by three orders of magnitude *and* look exactly like a
complete measurement while doing it. §2a refuses that trade for released framing, and the same
reasoning has to apply here.

A count that is present but is not a token count — including explicit JSON `null`, a negative,
a fractional number, a string or a bool — voids the whole report rather than being repaired.
Reading the half of a report that happens to parse and zeroing the rest puts a confidently wrong
number on an invoice, which is the more likely way a broken counter presents. A *missing* count
reads as zero, because a host reporting some counts is reporting zero for the others.

Validity is monotonic until a valid total arrives: **any later data event without a valid report**
clears an earlier non-final cumulative count, including an unusable usage object, `usage: null`, a
strict-decoding rejection, a non-object JSON value, or data inside an otherwise opaque SSE
envelope. Otherwise a broken terminal report could pass through byte-exact while leaving the last
subtotal eligible to be promoted when the body ends, recreating exactly the silent under-bill this
rule exists to prevent. A later independently valid usage-only frame restores trust.

Counts are bounded to signed 64-bit before integer materialisation. That ceiling is orders of
magnitude above a per-request context window and prevents a compact JSON exponent from asking the
gateway to construct or serialize an integer with billions of digits. Decimal exponents beyond
1,000 are rejected during strict document parsing; decimal parsing exceptions degrade to opaque
passthrough like every other unclassifiable candidate.

An all-zero `usage` object is treated as *no report* rather than as a report of zero, so a
`continuous_usage_stats` stream whose first frames legitimately carry zeros is metered from the
last frame that actually counted something.

`fabric_dp_unmetered_streams_total` carries the residual loss, labelled by `deployment_id`,
`account_id` and `reason`:

- `no_report` — the stream ran to its end and the host reported nothing usable. Somebody else's
  configuration: the host is ignoring `stream_options.include_usage`, or its counter is broken.
- `incomplete_stream` — the stream stopped before its end and only a subtotal was available. The
  client's or the network's.
- `framing_lost` — the gateway released withheld bytes at the ceiling or declined to parse an
  oversized frame, and no terminal report followed. This gateway's own limit.

Three properties of that series are deliberate. It is labelled by **account**, because lost usage
is lost revenue that belongs to somebody and `deployment_id` does not answer whose. It is labelled
by **reason**, because the three causes have different owners and different fixes and a single
series cannot tell an operator which is rising. And every reason is **seeded to zero when the
registry makes a deployment/account placement known**, before that identity can finish its first
request. `increase()` and `rate()` need an earlier sample to compare against: seeding and
incrementing in the same first-loss update would still make the series spring into existence at
1, hiding that event. Placement withdrawal retires active membership only after admitted streams
and backend attempts drain; process-lifetime counter values stay exposed, while an unretained late
cleanup cannot add a new event.

Requests **no host accepted** are excluded entirely: they are already visible as request outcomes,
and no completion was produced, so nothing was lost. Recording zeros is what made the original
defect invisible; a counter makes the residual gap a number somebody can act on.

## Consequences

### Positive

- Streamed usage is counted, which is most usage.
- `fabric_dp_tokens_total` covers streamed traffic for the first time.
- One usage row per streamed request, with a completion timestamp, and none for a request that
  never produced an answer — matching how the non-streaming path already behaved.
- A client that asked for usage keeps receiving it and is now also metered.
- The remaining loss is visible as a counter instead of arriving as false zeros.

### Negative

- The relay buffers each SSE frame until its terminator instead of forwarding arbitrary byte
  spans. A frame is one token's worth of JSON and a client could not act on a partial frame
  anyway, but this is no longer a pure passthrough.
- A host that separates events with something other than a blank line cannot be framed
  incrementally at all — the terminator is the only thing that says where one event stops — so a
  response from such a host below 64 KiB is delivered in one piece when the stream ends rather
  than as it arrives, and is never metered. It is delivered byte for byte, which is the property
  that matters, and every OpenAI-compatible server this platform targets is blank-line framed.
  Past the ceiling such a host streams in 64 KiB pieces.
- A frame whose bytes are released at the ceiling is unread: its usage is not seen, and if it was
  a usage frame the client receives it despite not asking. Framing resumes immediately, so this
  costs one frame rather than the stream, and it shows up as `framing_lost` on the counter. The
  same applies to a complete frame with more than 1 MiB of `data:` payload, which is forwarded
  without parsing.
- Releasing keeps a terminator's worth of tail bytes and never suppresses the resynchronising
  frame. Without both rules a release inside a boundary could merge two SSE events, then consume
  their combined bytes as though they were the usage frame.
- A client that did not ask for usage still receives the `usage` key that `include_usage` adds
  to ordinary chunks. Only the usage-only frame is removed. Stripping the key would require
  re-serialising every frame.
- A stream cut short before its terminal report is billed nothing, so work genuinely performed
  is not charged for. This trigger is client-controlled: a caller can deliberately disconnect
  before the terminal frame and repeat. The accepted bound is that disconnecting also cancels
  the upstream response — the caller cannot receive the completed answer for free — while the
  existing per-account rate/concurrency limits bound attempts and the per-account
  `incomplete_stream` series makes repeated loss alertable. That is chosen over billing a
  `continuous_usage_stats` subtotal as a total. Fully closing the gap needs a partial-marked
  usage record or a final count available at cancellation; neither contract exists yet.
- A host that does not honour `stream_options.include_usage` is silently unmetered apart from
  the counter. There is no way to derive its token counts from a byte relay.
- `stream_options` is now always present in the upstream body for streamed requests, so a host
  that rejects the field outright would fail. Every OpenAI-compatible server this platform
  targets accepts it.
- A usage object with one unusable count is discarded whole, so a host with a partly broken
  counter is unmetered rather than partly billed. The nonsense value itself is not reported
  anywhere, only counted as `no_report`.

### Neutral

- The dedup key stays `record_id`, a fresh UUID per record. There is exactly one record per
  streamed request now, so the collector's at-least-once forwarding is unchanged.
- Suppressing a frame changes where chunk boundaries fall in the forwarded byte stream. The byte
  sequence is preserved; SSE is framed by its terminators, not by transport chunking.
- Streamed frames still carry the upstream model name rather than the customer's alias. Rewriting
  it would mean re-serialising every frame, which §2 rejects; the non-streaming path continues to
  rewrite because it already parses the whole body.

## Alternatives considered

- **Keep the zero-token record and add a second record with the real tokens.** Rejected: two
  records for one request, with different dedup keys, double-count the request and inflate any
  per-request accounting built on the buffer.
- **Mutate the buffered record when the terminal frame arrives.** Rejected: `UsageRecord` is
  frozen, and a collector may already have drained it — the drain is destructive with no ack, so
  the update would silently vanish.
- **Record zeros when nothing was reported.** Rejected: that is the original defect. A zero-token
  row asserts something false; a counter admits ignorance.
- **Strip `usage` from every frame so the client never sees it.** Rejected: it requires
  re-serialising every frame on the hot path, and it would corrupt fields the gateway does not
  model. Only the usage-only frame Fabric caused is removed, and it is removed whole.
- **Always request `continuous_usage_stats` so a disconnect is still metered.** Deferred: it puts
  a usage object on every frame, enlarging every payload on the streaming hot path, to recover
  tokens only for abandoned streams. The counter measures how often that happens first.
- **Keep consuming the host stream after the client disconnects to obtain its terminal usage.**
  Rejected: it turns cancellation into a request to keep spending GPU until generation completes,
  which is a denial-of-service primitive and violates the response relay's cancellation semantics.
  The upstream is closed promptly; the unbilled partial work is accepted and alerted as
  `incomplete_stream` until the usage contract can represent a partial measurement.
- **Meter streams by counting frames or tokenising content locally.** Rejected: the gateway would
  be inventing numbers that disagree with the engine's own tokeniser, which is worse than a
  counter that says "unknown".
- **Decide suppression by "this frame has no content".** Rejected: it cannot tell an empty
  `choices` array from a shape that has no `choices` key, so it deletes answers from any host
  this gateway does not model. The error is unrecoverable in that direction — the client loses
  the response and is billed for it — so the test is positive and unrecognised frames pass.
- **Buffer a frame however long it takes.** Rejected: a host that never sends a terminator would
  hold its whole response in memory, once per concurrent stream, on a path whose only admission
  control is per-account concurrency.
- **Gate the retry on forwarded bytes.** Rejected: a partial frame in the meter's buffer would
  read as "nothing reached a host", which is exactly the condition ADR 0011 forbids replaying on.
- **Bill the last count seen when a stream is cut short.** Rejected: under
  `continuous_usage_stats` that is a running subtotal, so it under-bills silently while producing
  a row indistinguishable from a complete measurement. §4.
- **Floor an unusable token count to zero and bill the rest of the report.** Rejected: it puts a
  confidently wrong number on an invoice. A discarded report leaves the stream visibly unmetered.
- **Stop framing permanently once the buffer ceiling is hit.** Rejected: it costs a stream its
  metering and its suppression guarantee for the rest of its life over one large frame, when a
  release is invisible to the client and framing can simply resume. §2a.
- **Count lost streams on one unlabelled series.** Rejected: without `account_id` nobody can tell
  whose usage was lost, without `reason` nobody can tell who owns the fix, and without a seeded
  zero the first loss on a deployment is not a change any alert can fire on.

## Verification

Tests must prove: a streamed request records exactly one usage row carrying the tokens the host
reported; the gateway sends `include_usage` upstream, preserves other `stream_options` keys, and
does **not** send the field on a non-streaming request; a client that did not ask receives its
content frames unchanged and no frame reporting token counts; a client that did ask receives the
frame and is metered; a usage frame carrying content is always forwarded, and so is one that omits
`choices` or carries an unmodelled key — the case that would otherwise delete an answer while
billing for it; a usage-shaped JSON payload under `event:`, `id:`, `retry:` or a comment remains
opaque and is forwarded; a colonless `data` field passes byte-exact and invalidates a prior
subtotal; vertical-tab/form-feed bytes around a `data:` value are preserved rather than broadly stripped into valid JSON; duplicate members, alternate encodings/BOMs and
`NaN`/`Infinity` remain byte-exact and unmetered rather than crossing the positive-classification
boundary; a usage-only frame is suppressed even when its counts are unreadable; a `usage: null`
key on a content frame is forwarded and reads as no report; batched `data:` lines are
joined as the protocol specifies; usage split across chunk boundaries is still read, including one
byte at a time; bare-CR and mixed CR/LF/CRLF blank boundaries are understood and pinned both at
the meter and end to end; a truncated final frame is still forwarded, including on the failure
path; malformed JSON and valid JSON beyond the decoder's recursion limit pass through byte for
byte, with the latter also pinned end to end so it cannot abort an accepted stream; non-`data:`
frames pass through unparsed; an all-zero usage object is not treated as a report and the last real
report wins; a report with
one unusable count — including explicit `null` — is discarded rather than half-billed, while a
*missing* count reads as zero; a broken or strictly rejected later data event invalidates an
earlier non-final subtotal rather than promoting it when the stream ends; and compact exponent
forms cannot escape parsing or trigger unbounded integer conversion.

On §2a: withheld bytes are released rather than buffered without limit; the last three bytes are
retained so a release cannot land inside a terminator and merge two SSE events; the
resynchronising frame is forwarded without parsing or mutating usage — including when its retained
suffix could fabricate a `data:` prefix — before suppression resumes; framing then resumes
normally, so a terminal usage frame that follows one is still read and suppressed; this byte-exact behaviour
is pinned both at the meter and end to end through the gateway with a host that writes an event
body and terminator separately; a newline-framed host streams in bounded pieces past the ceiling
and is delivered whole, byte for byte, below it; a frame that arrives complete in one chunk is
parsed however large it is; a frame too large to parse is forwarded whole and attributed as
`framing_lost`; a reset forgets a failed attempt's partial frame and usage, and a retried stream
is neither spliced onto a dead attempt nor billed its tokens — exactly one row, carrying the
serving attempt's counts.

On §4: a subtotal on a content frame is not a total until the stream is seen to end, and that
completion latch stays set if response closing later fails; a stream the client cuts off mid-body
records no row and increments `incomplete_stream`, proven through the raw ASGI interface rather
than the test client, because that is the only way to hang up after a frame has been forwarded;
a host that drops mid-body does the same; a subtotal read before a release is never a total; a
terminal usage-only frame is a total with no further confirmation needed; a completed
cumulative-only stream is billed from its last count; streamed tokens reach
`fabric_dp_tokens_total`; a host that answers and reports nothing writes no row and increments
`no_report`; an unreachable host and a rejected request record no usage and increment nothing;
every reason is published at zero during placement activation, before any request can create its
first loss; cancellation while the concurrency limiter lock is contended releases placement
synchronously, completes the one shielded limiter release, and never duplicates accounting;
withdrawal between response construction and iterator entry keeps an admitted stream's
loss attributable; the last stream cleanup cannot retire backend metrics while an ordinary attempt
still drains; same-id reintroduction before that drain protects the new membership from old-pool
cleanup; repeated withdraw/reintroduce cycles retain every draining pool generation and preserve
visibility and backend outcomes whichever generation drains first; withdrawing an idle newer
generation still respects an older generation's live lease; process-lifetime history remains
after retirement while unretained cleanup cannot change it; and `/v1/completions` streams are
metered the same as chat.
