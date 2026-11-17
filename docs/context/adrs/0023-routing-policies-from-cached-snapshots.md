# ADR 0023: Routing policies are enforced from cached snapshots, with a deterministic split

**Decision status:** Accepted  
**Implementation status:** Implemented (control-plane API and versions, agent and operator delivery, stamp gateway and edge enforcement, console editor)  
**Date:** 2026-11-17

## Context

A client names a model by a deployment's alias. Canarying a new deployment, sending tool
calls to a larger model, or falling back when a model host is down all meant changing the
client. Accounts want one public model name (`chat-default`) whose meaning they control:
ordered rules that match a request, a weighted split across deployments, and a fallback
chain.

Two existing constraints shape where this can live:

* Configuration reaches serving components only as cached, versioned snapshots. A request
  never calls the control plane, and the last good snapshot keeps serving when the control
  plane is down (ADR 0001, ADR 0021, the paper's two-clock design).
* Model-name resolution happens at the stamp gateway (`DeploymentRegistry.resolve`). The
  edge only chooses a stamp that holds the name, and callers may skip the edge and call a
  stamp directly. Prefix caches live on one stamp's hosts, so the split must not scatter a
  conversation (ADR 0019).

## Decision

1. **Model.** A routing policy belongs to one account and is keyed by an alias. It has a
   mutable draft and immutable published versions; `live_version` names the one in force.
   Publishing freezes the draft as version N+1; rollback republishes an old version as a
   new one. Create, publish, rollback and delete are audited. An alias may not equal one of
   the account's deployment aliases, `auto` or `cascade`.
2. **One pure module, two copies.** Matching, the weighted draw and the fallback order are
   in `routing_policy.py`, standard library only. The data plane and the control plane hold
   byte-identical copies; a test in each suite fails if they differ. The simulate API, the
   stamp gateway and the edge all call `decide()`, so a simulated request and a real one
   cannot get different answers.
3. **Delivery: the snapshots that already exist.**
   * Stamps: desired state carries `routing_policies {revision, policies}` with the live
     versions of every account placed on the stamp, sent whole on every pass like
     `verification`. The agent writes it into `deployments.json` (or, in operator mode,
     onto each `ModelDeployment` for that account, and the operator renders each policy
     once). The gateway's reloading registry parses it with the rest of the file. The
     agent keeps the last snapshot through passes without one and across restarts.
   * Edge: the routing table carries the same live documents; the ETag and the revision
     high-water mark include them, so the edge's conditional polling and its on-disk copy
     cover policies with no new mechanism.
   A policy that fails to parse is skipped (its alias behaves as if it had none); the rest
   of the snapshot still applies.
4. **Enforcement at the stamp, consulted at the edge.** The stamp gateway enforces: it
   chooses the deployment among those it serves, walks the fallback chain, and applies the
   rule's strategy and timeout. The edge evaluates the same policy to pick a stamp holding
   the chosen target, applies stamp preference (`prefer`/`pin`, which only the edge can
   honour), and forwards the caller's bytes unchanged. A model name that no policy names
   takes exactly the path it took before.
5. **Deterministic split: weighted rendezvous hashing.** Each target scores
   `-weight / ln(h(key, deployment))` and the highest wins. The key is the run id, else the
   session key (`X-Fabric-Session`, `X-Session-Id`, `X-Conversation-Id`, body `user`) — the
   keys prefix affinity already uses — so a conversation stays on one deployment. Without
   one, the stamp draws with a random key. The edge, when it has no key, draws with a
   random key and sends it as `X-Fabric-Route-Key`; the stamp uses it. Rendezvous hashing
   has the property this needs: removing a target that did not win never changes the
   winner, so a stamp holding only some targets picks the edge's target whenever it holds
   it, and its renormalised split is exact otherwise. Weight changes move only the keys
   that have to move.
6. **Fallback inside the existing retry boundary.** A rule lists the conditions that move
   down its chain: `server_error` (5xx, or a host failure after the request may have
   started — the same case the cascade escalates on), `overload` (429/503 from the host),
   `timeout`, `no_ready_host`. The account's own limits and bad requests never fall back.
   Once a streamed response has started, nothing is retried; the stamp still never replays
   a request across model hosts of one deployment after it may have been sent.

## Consequences

### Positive

* One public name per workload; canaries, tool routing and fallbacks need no client change.
* No new delivery path, no request-path dependency on the control plane, and policies keep
  working through a control-plane outage on stamps and the edge.
* Simulation is the enforcement code, not a model of it.
* Prefix affinity survives a split: a session's requests reach one deployment, and the
  deployment's own strategy (or the rule's) keeps them on one host.

### Negative

* A published change reaches stamps only as fast as agents poll (seconds) and the edge only
  as fast as it polls (10 s default); there is no instant cut-over.
* Stamp preference is advisory for callers that bypass the edge.
* A `timeout` or `server_error` fallback can run a request twice (the first attempt may
  still be executing). It is opt-in per rule and documented.
* The token estimate used by prompt-length conditions is four characters per token.
* Two copies of a module must be kept identical; the drift test enforces it.

## Alternatives considered

* **Resolve at the edge only and rewrite `model`.** Callers that reach stamps directly would
  bypass the policy, and the edge is optional.
* **Look policies up from the control plane per request.** Violates ADR 0001.
* **Weighted random per request.** Scatters conversations and destroys prefix-cache hits.
* **Modulo hashing on the session key.** Moves most sessions when a target is added, and a
  stamp holding a subset of targets would disagree with the edge.
* **A shared Python package for the module.** A new distribution and dependency for both
  services for one small module; the byte-equality test gives the same guarantee.

## Verification

* `data-plane/tests/test_routing_policy.py`: matching per condition, first match, the draw's
  determinism, accuracy and subset property, weight changes, fallback chains, cycle and
  parse errors, request facts for each endpoint shape, copy identity.
* `data-plane/tests/test_policy_routing.py`: unchanged behaviour without a policy, routing,
  rule matching (tools, headers, API key, Messages), sticky and random splits, the edge's
  route key, parity with `decide()`, every fallback condition, no fallback after a stream
  starts, strategy override, enforcement from the agent's file and hot reload.
* `data-plane/tests/test_edge_policy.py`: target choice, edge/stamp agreement, fallback
  stamps, `prefer`/`pin`, isolation, serving from the cached table with the control plane
  down.
* `control-plane/tests/test_routing_policies.py`: drafts, publish, immutable versions,
  rollback, validation messages, simulate (and parity with the data-plane copy), tenant
  isolation, scopes, audit trail, delivery in desired state and the edge table.
* `agent/internal/agent/routing_policies_test.go`, `agent/internal/operator/routing_policies_test.go`.
