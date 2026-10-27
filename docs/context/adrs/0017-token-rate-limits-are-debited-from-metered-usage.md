# ADR 0017: Token-rate limits are debited from metered usage

**Decision status:** Accepted  
**Implementation status:** Implemented for the in-process and shared coordinator backends; per-account policies and calendar budgets remain open  
**Date:** 2026-10-27

## Context

Two account controls existed before this: a request rate and an in-flight cap (ADR 0016). Neither
measures cost. One request can ask for a few tokens or for tens of thousands, so an account inside
its request rate can still take most of a GPU with a handful of long generations, while another
account sending many short requests is refused first. Hosted inference APIs limit tokens per
minute for exactly this reason, and clients already handle a `429` with `Retry-After` for it.

A token limit cannot be enforced exactly at admission. Prompt length can be estimated, but output
length is decided by the model and is unknown until the request ends. Reserving `max_tokens` up
front would refuse requests that will never use their ceiling, and most callers do not set one.

The usage the gateway already meters is the only trustworthy count: the model host's own report,
recorded once per completed request, streamed or not (ADR 0014).

## Decision

### 1. A debt bucket, debited after the request

Each account has a token bucket refilled continuously at `tokens_per_minute`, capped at a burst
that defaults to one minute's allowance. Admission requires the balance to be non-negative.
When a request finishes, its metered input plus output tokens are subtracted, which may leave the
balance negative. An account in debt is refused with `429 token_rate_limited` and a `Retry-After`
of the time needed to refill to zero.

Over any interval long enough to contain several requests the account's spend is held to the
configured rate exactly. A single request may overshoot by its own size. Concurrent requests can
each overshoot once, so the in-flight cap is what bounds the worst case: at most
`max_in_flight_per_account` requests can be admitted against the same non-negative balance.

### 2. Checked first, without side effects

The token check runs before the request rate and the concurrency cap, and changes nothing. An
account that is in token debt therefore does not also spend a request-rate token on an attempt
that was never going to be admitted.

### 3. Debited once, through the coordinator's admission record

With the shared coordinator, the gateway debits by `request_id`, the same identifier the
coordinator already records for the admission. The admission row gains a `consumed` flag, so a
gateway may retry a debit whose reply it lost and the account is charged once. An admission row
that has already expired is still charged, and a consumed tombstone is written so the retry of
that debit is recognised as well. Allowed admissions are kept for a full lease interval when token
limits are on, so the record outlives the request it describes.

A debit that still fails after one retry is logged, not raised. Admission continues to fail closed
when the coordinator is unavailable. A missed debit under-charges one request that has already
completed, and failing that request after its answer was produced would help no one.

### 4. Restart and policy change follow the request rate's rules

Token balances persist in the coordinator's SQLite store. Downtime refills nothing: an account in
debt when the coordinator stops is in the same debt when it starts. A change to the token policy
resets every account to the new burst rather than applying the new rate retroactively to debt run
up under the old one. A store written before token limits existed is upgraded in place, with the
added columns defaulting to "no token limit" and "not yet debited".

## Consequences

- Operators set `dataPlane.limits.tokensPerMinute` and optionally `tokenBurst`. Zero disables it.
- A stream the gateway could not meter (ADR 0014's `framing_lost` and `incomplete_stream` cases)
  is not charged, which is the same residual gap usage metering already reports.
- The limit is per stamp, like the request rate. A global quota needs allocated per-stamp budgets
  or an explicit choice of consistency over partition availability.
- The policy is one value for every account on a stamp. Per-account policies need the control
  plane's `limits_policy_ref` to name a real resource delivered with desired state.

## Not decided here

Daily or monthly token budgets. They need a calendar window, a time zone, and a notion of who may
raise them, which belongs with a policy resource rather than a stamp-wide setting.
