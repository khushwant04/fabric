# Fabric Technical Paper — Original LaTeX Manuscript

Copy the single `latex` block into `fabric-paper.tex`. Keep the `paper/figures/*.pdf` paths beside the repository layout, or adjust the paths when moving the TeX file. Regenerate every chart from the checked-in evidence register with `uv run --project paper python paper/generate_figures.py`; the Jupyter notebook under `paper/notebooks/` invokes the same generator.

```latex
\documentclass[10pt,a4paper]{article}

\usepackage[margin=0.72in]{geometry}
\usepackage[T1]{fontenc}
\usepackage{lmodern}
\usepackage{microtype}
\usepackage{amsmath,amssymb}
\usepackage{booktabs}
\usepackage{tabularx}
\usepackage{longtable}
\usepackage{array}
\usepackage{enumitem}
\usepackage{xcolor}
\usepackage{graphicx}
\usepackage{float}
\usepackage{caption}
\usepackage{url}
\usepackage[hidelinks]{hyperref}

\newcommand{\system}{\textsc{Fabric}}
\newcommand{\code}[1]{\texttt{#1}}
\newcommand{\evidence}[1]{\textbf{Evidence scope:} #1}
\renewcommand{\arraystretch}{1.12}
\setlength{\parindent}{1.05em}
\setlength{\parskip}{0.15em}
\sloppy

\title{\textbf{Between Intent and Tokens:\\Two-Clock Governance for Managed Language-Model Serving}}
\author{Author Name\\Affiliation\\\texttt{author@example.com}}
\date{September 2026}

\begin{document}
\maketitle

\begin{abstract}
A managed language-model service lives at two rates. Configuration changes move slowly: an account update becomes placement intent, crosses a network boundary, becomes a Kubernetes object, produces a GPU workload, and finally enters a routing table. Generation moves quickly: a request is authenticated, admitted, attached to a model process, and extended token by token. Coupling the second clock to the first makes every stream depend on a distant API and database; pretending that the clocks are identical makes stale status look like proof of service. This paper uses \system, an implemented Kubernetes-based serving platform, to develop a two-clock account of system behavior. Three invariants follow. First, authority should not accumulate in one process or credential domain: central synchronization, Kubernetes mutation, request serving, and telemetry export are distinct application roles, although the audited stamp still injects the agent service-account token into all containers of their shared pod and therefore only partially enforces this invariant. Second, authorization must remain at the serving edge: audience-specific JSON Web Tokens, cached JSON Web Key Sets, and account-owned routes permit a stamp to decide locally. Third, an acknowledgement or destructive transition must follow durable or directly observed evidence: desired state is persisted before a delivery watermark advances, a route is acknowledged before an old workload is removed, and usage is spooled before asynchronous export is acknowledged.

The analysis covers the complete deployed path from Cloudflare DNS and proxy choices through Azure ingress, Istio, Kubernetes, PostgreSQL row-level security, identity exchange, outbound stamp reconciliation, vLLM model hosts, usage collection, and monitoring. A 20 September audit found five Ready T4 nodes, five distinct one-replica model deployments, five Available custom resources, and an HTTP 200 completion from every model. These checks establish point-in-time reachability, not post-scale throughput. Historical two-T4 measurements and an RTX 4070 development microbenchmark are reported as separate snapshots. The implementation also exposes useful counterexamples: its generation counters are not one shared sequence, same-release replica reduction failed to converge automatically, periodic projected-token 401s delayed one reconciliation pass, the current fleet has no rollout spare, and usage records are operational rather than billing-grade. The result is a precise way to reason about serving continuity without overstating fleet maturity or engine acceleration.
\end{abstract}

\noindent\textbf{Keywords:} LLM serving, control plane, Kubernetes reconciliation, local authorization, multi-tenancy, durable telemetry, vLLM, GPU operations.

\section{Introduction}

Language-model serving combines an administrative system with a real-time conversation system. The administrative side changes account membership, keys, releases, replica counts, and placement. Its natural unit of progress is a reconciled configuration. The serving side consumes a prompt and emits a sequence whose useful unit is a request or token. A deployment update may take minutes because an image and weights must be pulled, a GPU scheduled, a process initialized, and readiness observed. A token step is measured in milliseconds and a stream cannot pause while a central database recovers. Treating both activities as ordinary synchronous API calls hides the main engineering problem.

This paper calls these activities the \emph{configuration clock} and the \emph{generation clock}. They are independent rather than merely fast and slow stages of one transaction. The configuration clock can stop while an already accepted release continues to answer. The generation clock can be busy while a new release is still preparing. Conversely, a successful control API response says that intent was accepted; it does not say that an endpoint is Ready or that the gateway has installed it. The distinction is particularly important for autoregressive inference, where expensive cold starts and long-lived server-sent-event (SSE) streams amplify an otherwise brief control-plane disturbance.

\system is a useful case study because its central service and its Kubernetes ``stamps'' already embody this split. The central service owns commercial and administrative state. Each stamp owns the immediate decision to admit and route inference. An outbound agent transfers desired state into the cluster. An operator materializes GPU workloads. A data-plane gateway performs local identity verification and proxies selected OpenAI-compatible operations. A collector exports durably recorded usage. The architecture is not presented as a universal serving design or a completed production baseline. It is examined as an implemented protocol with dated operational evidence and visible gaps.

The two-clock frame yields three invariants.

\begin{enumerate}[leftmargin=*]
  \item \textbf{Authority should not aggregate in one credential domain.} A component able to read central assignments should not also possess unconstrained workload mutation, public request-serving, and usage-ingestion authority. Fabric separates the application roles, but its present shared-pod service-account mount leaves a documented gap between this invariant and deployment enforcement.
  \item \textbf{Authorization remains at the serving edge.} A valid identity is insufficient until the local gateway proves audience, scope, and ownership of the selected route. The central plane is not queried for each request or token.
  \item \textbf{Evidence precedes acknowledgement and destruction.} A component advances a durable watermark only after applying the corresponding state. It removes an old backend only after the router confirms the exact route revision and the backend has no tracked in-flight work. It deletes leased usage only after central acceptance, duplicate recognition, or permanent rejection.
\end{enumerate}

The rest of the paper formalizes the clocks, traces the deployed architecture and request path, examines identity and reconciliation, and evaluates dated evidence. External systems are cited for the mechanisms on which \system builds: iteration scheduling and paged KV memory in Orca and vLLM \cite{orca,vllm}; predictable and disaggregated serving in Clockwork and DistServe \cite{clockwork,distserve}; Kubernetes controllers \cite{k8scontroller,k8soperator}; JWT validation \cite{jwt,jwtbcp}; and row-level database policy \cite{postgresrls}. No direct quotations are required.

\section{Problem Statement and Two-Clock Formalization}

\subsection{Two timelines}

Let $C(t)$ denote the latest central intent at administrative time $t$. That intent is not directly executable. It passes through an agent-applied local set $A(t)$, an operator observation $O(t)$, and a gateway route configuration $R(t)$. Each mapping is asynchronous:

\begin{equation}
 C \longrightarrow A \longrightarrow O \longrightarrow R.
\end{equation}

The arrows mean eventual transfer under repeated reconciliation, not an atomic commit. Each layer may retain the last known safe state while a successor is incomplete. A customer request arriving at generation time $\tau$ instead uses a local tuple

\begin{equation}
 Q(\tau)=\langle K_{\tau},R_{\tau},L_{\tau},B_{\tau},S_{\tau}\rangle,
\end{equation}

where $K$ is the locally cached verification-key set, $R$ is the account-scoped route table, $L$ is admission state, $B$ is backend health and in-flight state, and $S$ is the durable usage spool. The decision to begin work is a function of $Q(\tau)$ and the request, not of a synchronous read of $C(t)$. Central unavailability can prevent configuration progress or token issuance while leaving requests with already issued, unexpired, known-key credentials serviceable.

This independence is bounded. An unknown key identifier cannot be trusted when JWKS refresh fails. An unhealthy spool or unavailable stamp-local limiter blocks new work because required evidence cannot be retained. A missing route cannot be reconstructed from central intent during the request. Independence therefore means that the generation clock consumes accepted local state; it does not mean that the edge invents authority during partition.

\subsection{Counters are typed, not interchangeable}

The implementation contains several monotone values and digests. They describe different facts:

\begin{table}[H]
\centering
\caption{State markers and the evidence each one actually carries.}
\label{tab:markers}
\begin{tabularx}{\textwidth}{p{0.24\textwidth}p{0.22\textwidth}X}
\toprule
Marker & Owner & Meaning \\
\midrule
Deployment generation & control plane & Revision of one account deployment specification. \\
Stamp delivery watermark & control plane & Cross-account sequence used to return assignments incrementally to one stamp. \\
Agent acknowledged watermark & agent & Highest fetched stamp sequence durably recorded after constructing and publishing the remembered local set. \\
CR metadata generation & Kubernetes & Revision of a \code{FabricModelDeployment} spec as seen by Kubernetes. \\
CR observed generation & operator & Metadata generation used by the operator for the status it produced. \\
Rendered-config revision & operator/data plane & Digest of the concrete route document loaded by the gateway. \\
Route revision and drain state & operator/data plane & Digest and per-backend in-flight evidence used during cutover or removal. \\
\bottomrule
\end{tabularx}
\end{table}

There is no persisted \code{routed\_generation} field. ``Routed'' is a conceptual state backed by a loaded configuration digest and router state. Likewise, CR metadata generation and the central stamp sequence are not mathematically the same counter. The CR schema has a place for a control-plane generation, but the current publisher does not populate it; central readiness logic can compare a returned observed generation with a stamp delivery watermark even though the sequences have different origins. A publication-quality interpretation must therefore avoid an equation that declares all generations equal. The defensible condition is typed:

\begin{equation}
 \mathrm{Applied}(w) \land \mathrm{Observed}(m) \land
 \mathrm{Loaded}(d) \land \mathrm{SafeToDrain}(d,b),
\end{equation}

with explicit relations connecting watermark $w$, CR revision $m$, route digest $d$, and backend $b$. Establishing and testing those relations is future correctness work.

\subsection{Deriving the three invariants}

The clocks create incompatible blocking requirements. Configuration must tolerate delay and replay; generation must make a bounded local decision. If one process combines central credentials, cluster mutation, and public request parsing, then a generation-path exploit can rewrite administrative state. This motivates non-aggregation of authority. If every request asks the central plane to resolve ownership, generation inherits the failure rate and latency of the configuration path. This motivates edge authorization. Finally, asynchronous progress makes response codes and desired rows weak evidence: a watermark, cutover, deletion, or lease acknowledgement must be justified by a durable write or direct observation. This motivates evidence-before-transition.

The invariants resemble resource-centric security guidance in NIST SP 800-207 \cite{nistzt}, but this paper does not label the deployment generically as ``zero trust.'' Egress is not default-deny, some Azure services remain public, and model-host mutual TLS is not guaranteed. The narrower claims are testable against process credentials, database policies, route checks, and state-machine order.

\section{Architecture}

\subsection{Public edge and Azure entry}

Figure~\ref{fig:cloud} records the deployed end-to-end topology. Clients use separate control and inference hostnames. In the audited Cloudflare zone, the control-plane and Grafana records are proxied, while the inference record is DNS-only. Cloudflare documents how proxied and DNS-only records differ \cite{cloudflareproxy}; the paper does not infer or assert a Cloudflare Full or Full (strict) encryption mode. The DNS-only inference choice prevents an outer HTTP proxy timeout from becoming a hidden bound on long generations.

All three names resolve toward the Azure entry used by the installation. Traffic reaches an Azure public IP and the managed load-balancing path for the AKS Istio external ingress. At the cluster boundary, TLS terminates at Istio, which selects a host-specific Gateway and VirtualService route \cite{istioingress,azureistio}. In-cluster application links are HTTP unless explicitly identified otherwise. Thus public HTTPS does not imply mandatory model-host mTLS. The control VirtualService forwards to the service in \code{fabric-control}; inference forwards to the data-plane service in \code{fabric-stamp}; Grafana resides in \code{fabric-observability}. TLS credential secrets for the managed external gateway reside with the Istio ingress workload rather than in the application namespaces.

\begin{figure}[H]
\centering
\includegraphics[width=\textwidth]{paper/figures/cloud_architecture.pdf}
\caption{Audited cloud and cluster path. Cloudflare status, Azure resources, and five-host inventory are deployment observations; namespace, service, process, and HTTP-link relationships are supported by charts and source. Control and Grafana are Cloudflare-proxied, inference is DNS-only, and the figure makes no claim about Cloudflare Full/Strict mode. TLS terminates at Istio at the cluster boundary; unmarked internal application links are HTTP.}
\label{fig:cloud}
\end{figure}

\subsection{Central plane}

The Python/FastAPI control plane stores accounts, memberships, API keys, per-account OIDC configuration, deployments, placements, stamp enrollment, audit records, observed status, idempotency records, and aggregated usage in PostgreSQL. Auth0 supplies one source of human identity assertion. An account may instead configure an OIDC issuer and audience. Fabric exchanges an accepted long-lived credential or assertion for a short-lived token intended for either control operations or inference.

The audited administrative secret path uses Azure Key Vault. Signing material, the credential pepper, database credentials, and bootstrap material are retained there, then copied into deployment-time Kubernetes configuration by an administrator. The application and Helm workflow do not implement a direct Key Vault client, CSI mount, or automatic secret synchronization. Calling this an administrative source is therefore more accurate than describing Key Vault as an in-request dependency. Workload Identity is enabled on AKS and is an available Azure mechanism for pod-to-cloud identity \cite{azurewi}, but it is not evidence that every current secret flow uses it.

PostgreSQL is both system of record and a tenant-isolation backstop. The current audit found PostgreSQL 18 on Burstable \code{Standard\_B1ms}, 64 GiB, without high availability. Azure documents HA and backup options \cite{azurepgha,azurepgbackup}; the deployment has seven-day backups, no HA, and no geo-redundant backup. Those are candid pilot properties, not recommendations.

\subsection{Stamp and authority partition}

A stamp is the failure and serving domain. Its agent enrolls with a single-use token, receives agent and telemetry credentials, stores identity, emits heartbeats, polls assignments, and posts status. The protocol is outbound-only: the central plane does not initiate a connection into the cluster. The agent also calls the Kubernetes API to publish CR intent and, where configured, read limited capacity information. Outbound-only does not mean default-deny egress; current policy leaves required egress open.

The operator has Kubernetes RBAC for custom-resource status, Deployments, Services, EndpointSlices, and rendered configuration but no Fabric control credential. At the application layer, the data-plane binary does not invoke workload mutation and the collector reads leased records only through a loopback administrative endpoint. Model hosts receive no Kubernetes service-account token. However, agent, data plane, and collector are separate containers in one StatefulSet pod, and service-account automount is configured at pod scope. Kubernetes therefore injects the agent identity into all three containers. That identity can mutate \code{FabricModelDeployment} specs. A compromised gateway or collector could use credentials outside its intended code path, so the current packaging does not enforce process-level Kubernetes least privilege. The agent also receives and persists the telemetry secret in order to hand it to the collector; that boundary is likewise normal-code and mount/API separation, not cryptographic impossibility.

The role split is still meaningful but incomplete. A compromised operator can mutate model-host resources but cannot authenticate as a customer or central stamp. A compromised gateway or collector does not inherit the operator service account, yet it can reuse the shared agent pod token to modify CR intent. The collector's telemetry credential remains separate from central desired-state authentication, but the pod-level Kubernetes credential expands its effective blast radius. Enforcing the invariant requires separating the agent into its own pod or explicitly projecting its token only into the agent container. This gap is treated as a result and a priority correction, not as an implementation detail to omit.

\subsection{Supply and observation paths}

Azure Container Registry (ACR) supplies AMD64 control-plane, agent, data-plane, and model-host images to kubelets through AKS registry attachment. The guarded repository workflow builds and digest-resolves the first three Fabric services; model-host image promotion is not in that automated sequence and remains a provenance gap. Model weights are obtained by the model-host runtime and cached on node-local storage where configured. Replacing an ephemeral node loses that cache.

Prometheus scrapes gateway and vLLM metrics; one DCGM exporter per GPU node supplies device observations; Grafana displays the resulting series. Prometheus's dimensional model and histogram guidance shape label and latency design \cite{promdata,promhist}, while DCGM defines the GPU telemetry source \cite{dcgm}. Gateway outcomes, engine scheduling, and physical GPU state are kept as separate evidence layers. Cilium enforces configured Kubernetes policy \cite{cilium,azurecilium}, but the present network posture should not be confused with complete private connectivity.

\section{The Fast Clock: Request and Token Path}

\subsection{Local decision sequence}

An inference call reaches Istio, then the stamp data-plane service. No normal request calls the central API. The gateway executes the following local sequence:

\begin{enumerate}[leftmargin=*]
  \item parse a bearer token and select a locally cached public key;
  \item verify signature algorithm, issuer, exact inference audience, time claims, account identifier, and \code{inference:invoke} scope;
  \item resolve the public model alias only among routes owned by that account;
  \item require a healthy usage spool and stamp-local limit authority;
  \item acquire admission, choose a concrete eligible backend, and retain an in-flight lease;
  \item replace the public alias with the internal release and remove client-supplied authorization or internal identity headers;
  \item proxy a complete response or one pinned SSE stream;
  \item durably classify reported usage, release router/admission state once, and update bounded-cardinality metrics.
\end{enumerate}

The route is an authorization object, not just a load-balancer entry. A cryptographically valid token for account $a$ cannot invoke an alias owned by account $b$. Model discovery applies the same account filter. This remains true while the central database is unreachable because ownership arrived earlier with accepted local configuration.

vLLM remains responsible for tokenization, PagedAttention, continuous batching, KV-cache management, model execution, and sampling \cite{vllm}. \system surrounds rather than replaces the engine scheduler. SGLang, Sarathi-Serve, and DistServe explore richer prefix, chunked-prefill, and disaggregated execution policies \cite{sglang,sarathi,distserve}; none should be inferred from Fabric's current routing layer.

\subsection{Non-idempotent failure policy}

A generation request is not generally replayable. Sampling can diverge, and an upstream may have consumed GPU time even when the gateway has not received an HTTP response. Fabric retries only a pre-connection transport failure, specifically connection error or connection timeout, against another eligible backend. An HTTP 5xx penalizes or ejects the backend for future selection but is returned rather than replayed. A write, protocol failure, response timeout, or any ambiguous condition is not retried.

SSE makes the boundary observable. Before the first response byte, another backend may be selected only for the same safe connection-failure class. After any byte, the stream remains pinned. On a mid-stream failure, the gateway emits an SSE error object when possible, terminates, and cleans up; it does not ask a second model process to invent a continuation. OpenAI documents SSE streaming for chat completions \cite{openaistream}. Fabric preserves content-frame bytes and asks the engine for a terminal usage frame. If the caller did not request that accounting frame, the gateway may consume it locally rather than alter the visible stream. A missing or malformed terminal usage report is marked unmetered, not estimated from chunk count.

This policy links the fast clock to the evidence invariant. The router retains a withdrawn pool while accepted attempts remain. Stream cleanup is idempotent and releases route and admission leases once. A release controller therefore cannot infer drain merely from deleting a Service or changing a weight; it waits for the data plane's exact revision and zero tracked work.

\section{Identity and Tenancy}

\subsection{Identity exchange and audience separation}

Auth0 validation checks issuer, configured audience, signature, required claims, and time against cached JWKS. Per-account OIDC registration is a separate path: it requires HTTPS discovery, an exact issuer and audience, asymmetric algorithms from an allow-list, and a unique account association. External subjects are namespaced by account so two issuers cannot accidentally create the same Fabric principal. The identity provider establishes who a subject is; Fabric account membership determines what that subject may administer.

Fabric then signs short-lived RS256 JWTs following the JWT format in RFC 7519 and deployment cautions in RFC 8725 \cite{jwt,jwtbcp}. Control tokens use the \code{fabric-control} audience; inference tokens use \code{fabric-inference}. A correctly signed control token presented at the gateway fails because its audience is wrong. A raw API key is accepted only by exchange endpoints, never as inference authority. Cached current and retiring keys permit overlap. If refresh fails, last-known-good keys remain usable, but a token naming an unknown key is refused. This is continuity without trust expansion.

Short lifetime bounds, but does not eliminate, revocation delay. Complete automated overlapping key and machine-credential rotation is not present. The paper consequently claims audience isolation and local verification, not instantaneous revocation.

\subsection{Route ownership}

Authorization at the edge uses three predicates:

\begin{equation}
 \mathrm{Allow}(q)=\mathrm{ValidInferenceJWT}(q)\land
 \mathrm{OwnsRoute}(q.account,q.model)\land
 \mathrm{Admitted}(q.account).
\end{equation}

The order matters. Alias syntax or a globally unique deployment ID is not permission. Ownership comes from accepted local route state and the verified account claim. The gateway strips caller-provided internal account and authorization headers before forwarding, preventing the untrusted request from becoming an identity channel to vLLM. The model host need not understand Fabric tenancy.

\subsection{Forced row-level policy}

At the slow clock, service methods derive account context from verified credentials and filter account-owned rows. PostgreSQL row-level security (RLS) provides an independent persistence-layer constraint. Policies use both \code{USING} and \code{WITH CHECK}; RLS is enabled and forced on tenant tables. PostgreSQL documents the policy semantics and privileged-role exceptions \cite{postgresrls}. The application role must be neither superuser nor \code{BYPASSRLS}; otherwise catalog-visible policies would not constrain it.

The request account is installed with transaction-local settings, and system elevation is likewise scoped to a transaction. This is essential with a connection pool: session-global state could leak the previous tenant onto the next request. Tests that execute deliberately unfiltered SQL under two account contexts establish that foreign rows remain hidden. SQLite unit tests cannot prove PostgreSQL policy behavior.

Some machine and managed-capacity operations legitimately cross account boundaries. They use narrow system elevation and explicit service checks. Forced RLS is therefore defense in depth for ordinary tenant access, not a claim that every internal operation is single-tenant or that a database administrator is untrusted.

\section{The Slow Clock: Reconciliation}

\subsection{Enrollment and outbound synchronization}

A stamp begins with a short-lived, single-use, account- and mode-bound enrollment token. The control plane atomically consumes it and issues separate credentials for synchronization and telemetry. The agent saves identity with owner-only permissions before normal polling. After restart it reuses the identity rather than attempting enrollment again.

For desired state, the agent sends its durable stamp watermark and receives changes after that point. It reconstructs the complete remembered assignment set, publishes the corresponding CR set, and only then stores the new watermark. A crash before the store causes replay. If rendered local state is missing after restart, the agent can return to watermark zero and request a full rebuild. This is evidence-before-acknowledgement: replay is preferable to claiming that unpublished intent was applied.

A \code{FabricModelDeployment} CR carries account, alias, release, desired replicas, GPU and runtime settings, strategy, and verification information. Kubernetes separates spec and status through the status subresource. The agent owns the central synchronization and CR specification. The operator watches that specification and owns status. This follows the controller/operator pattern in Kubernetes \cite{k8scontroller,k8soperator} while adding an explicit central-to-cluster boundary.

\subsection{Workload and route realization}

For each deployment, the operator renders a model-host Deployment and headless Service. A pod requests \code{nvidia.com/gpu}, receives runtime and node-placement settings, exposes health probes, and disables service-account-token automount. EndpointSlice observation turns Ready pod addresses into concrete gateway backends. When endpoint discovery cannot produce concrete addresses, Service DNS is a fallback, but concrete endpoints are preferred because they preserve per-host health and in-flight accounting.

The operator also renders data-plane configuration. Publication success, status persistence, and route loading are separate events. Malformed configuration does not intentionally replace the last good route set. Deletion first withdraws a route; router pools with accepted attempts remain alive until those attempts finish. Orphan workload pruning requires evidence that the relevant global route revision has loaded and no attempt for the deployment remains.

\subsection{Projected-token 401 observation}

During the live audit, isolated Kubernetes API HTTP 401 responses appeared in agent and operator logs at roughly 48--49 minute intervals. The custom client had read a projected service-account token and refreshed it after a 401, but did not replay the failed operation. Kubernetes documents rotation of projected service-account credentials \cite{k8stoken}. The cadence and client behavior make token rotation the leading explanation, not a proof for every event.

The consequence was one missed pass. Reconciliation resumed on the next approximately 15-second loop; model pods did not restart, custom resources remained Available, and inference readiness stayed HTTP 200. The observation is therefore classified as a low-severity one-pass control delay, not an inference outage. A read-per-request token strategy or a single safe retry after refresh, with tests for body-bearing operations, would remove the delay.

\section{Rollout and Destructive Transitions}

A changed release receives a deterministic candidate workload distinct from the active workload. While the candidate pulls, initializes, and becomes healthy, routing remains entirely on the active release. The candidate must reach its desired replica count and expose concrete Ready endpoints before the operator publishes a 0/100 cutover. This is not percentage canary analysis; it is an acknowledged replacement primitive.

After publication, the operator waits for the data plane to report the exact route revision and zero in-flight work for the old backend. Only then may it checkpoint progress and delete the old workload. If the candidate fails readiness or a newer generation supersedes it, the controller can restore old routing and must similarly observe reverse drain before removing the candidate. Destructive cleanup also depends on successful configuration publication and CR-status persistence. These checks instantiate the evidence invariant at the most expensive boundary: a desired release string is never treated as proof that a stream moved.

This mechanism needs simultaneous active and candidate capacity. The current five-node fleet has no spare GPU, so even a one-replica replacement cannot be staged without temporarily adding a node, stopping another model, or accepting a recreate interval. Kubernetes Pod Disruption Budgets can constrain voluntary disruption but do not manufacture replicas or capacity and cannot prevent every failure \cite{k8spdb}.

The September scale-down revealed a separate same-release defect. Central intent and the CR changed \code{qwen3.5-2b} from three replicas to one. The existing same-release Kubernetes Deployment stayed at three for ten minutes because the reconcile path did not patch replicas when the active release name was unchanged. Operators directly scaled it only after confirming central intent and the CR both requested one. Inference remained available. The incident demonstrates why desired, CR-observed, physical replica, and routed state must be checked independently. Until the reconcile path and regression test are fixed, same-release reductions require that explicit check.

\section{Durable Usage Evidence}

A successful model response is not itself a durable accounting record. For non-streaming calls, the gateway reads engine-reported prompt and completion token counts. For streams, it uses a trustworthy terminal usage frame. It creates a stable event identifier and commits the record to a bounded SQLite spool on persistent storage. Missing trustworthy counts produce an explicit unmetered classification rather than an estimate.

The collector accesses a private loopback administrative endpoint. It leases a stable batch without deleting it, removes untrusted local account identity, and sends the batch with its telemetry credential. Central ingestion validates the credential and event bounds and deduplicates by stable identity. The collector acknowledges a lease only when every item is accepted, recognized as a duplicate, or permanently rejected. A lost network response leaves the local records available for replay. Thus transport is at least once and central materialization is deduplicated; execution is not exactly once.

The spool is intentionally bounded. Sustained disconnection or overload can exhaust it, and policy may remove old unleased records. A disconnected stamp cannot make events visible centrally. There is no cryptographically chained ledger, invoice reconciliation, indefinite retention, or proof that model-reported counts equal an external tokenizer recomputation. The mechanism is suitable for operational attribution, capacity analysis, and pilot reporting. It is explicitly not billing-grade.

This path illustrates both authority and evidence constraints. The gateway can append locally but cannot submit as the collector. The collector can submit but cannot read central desired state or mutate model hosts. The agent hands off the telemetry secret during bootstrap but its normal synchronization client does not ingest usage. Most importantly, deletion of a local lease follows central item-level evidence rather than an optimistic POST attempt.

\section{Overload and Routing}

Admission combines a continuously refilled account request bucket with a maximum in-flight count. A private stamp-local coordinator shares those decisions among gateway processes. Request identifiers and renewable leases make repeated admission messages idempotent and recover capacity after a dead gateway. When the coordinator is unavailable, the gateway responds with 503 instead of exceeding configured limits. Capacity refusal uses HTTP 429 and \code{Retry-After}. Authentication and admission precede GPU dispatch.

Limits are local to a stamp. If the same account is placed in several stamps, the present system does not enforce one global request or token budget. There are no monthly quotas, global reservations, or first-class model autoscaling policies. ``Overload control'' here means explicit local refusal, not global fairness.

The gateway can choose least-in-flight, round-robin, weighted selection, or explicit-session rendezvous affinity. Anonymous traffic is not silently converted into sticky sessions. Health and in-flight leases survive route reloads in memoized backend pools, which lets a withdrawal drain instead of destroying knowledge about active work. A connect-failed endpoint may be temporarily ejected. The routing layer deliberately avoids queue migration after output begins.

Figure~\ref{fig:admission} shows one historical test. Thirty requests were offered concurrently to an earlier two-T4 pilot under a configured local cap. Five were served and 25 received 429; usage attribution existed for all 30 outcomes. This is evidence that refusal was explicit in that setup. It is not a five-node saturation curve, a fairness result, or a sustained-throughput measurement.

\begin{figure}[H]
\centering
\includegraphics[width=0.82\textwidth]{paper/figures/admission_outcome.pdf}
\caption{Repository-recorded admission outcome from an older two-T4 pilot: 30 offered, five served, and 25 explicitly rate-limited. The experiment describes one configured stamp-local cap and must not be transferred to the current five-node fleet.}
\label{fig:admission}
\end{figure}

\section{Implementation}

The control plane and gateway are Python services built with FastAPI; SQLAlchemy and Alembic manage persistence. The agent, operator, and collector are Go programs. Helm packages central and stamp resources. The custom resource, status subresource, Services, EndpointSlices, RBAC, persistent spool storage, Istio resources, ServiceMonitors, and dashboard configuration are repository-managed. Kubernetes provides the reconciliation substrate; it does not provide Fabric account semantics.

The audited AKS cluster in Central India uses Kubernetes 1.35.7, three \code{Standard\_D4s\_v5} system nodes, Azure CNI with Cilium, Entra/Azure RBAC, OIDC and workload identity, and an Istio external ingress. Application namespaces are \code{fabric-control}, \code{fabric-stamp}, and \code{fabric-observability}; the managed ingress is in \code{aks-istio-ingress}. Five \code{Standard\_NC8as\_T4\_v3} nodes each expose one 16-GiB NVIDIA T4. Five vLLM hosts run one per node behind headless Services. Current shared serving configuration uses the standard kernel path; this paper makes no current-fleet acceleration claim.

ACR is attached to AKS for kubelet pulls. A guarded CI/CD workflow accepts a full source commit, checks its provenance, builds three service images in ACR, resolves digests, and upgrades in an order that protects schema and contract dependencies. The model-host image exists in the deployed supply path, but its promotion is not covered by that same automated workflow. This distinction matters because an engine image controls CUDA, vLLM, model templates, and serving behavior.

\section{Evaluation}

\subsection{Evidence protocol}

Evidence is divided into four classes: source-supported mechanism, dated repository record, direct live audit, and derived scenario. The five-node audit checked Azure pool state, Kubernetes nodes, pods, CR status, public readiness, account-scoped model discovery, and one small authenticated chat request per alias. It did not run a post-scale load test. Historical latency and admission measurements remain attached to their original pilot. Derived costs are estimates, not invoices.

All generated plots read \code{paper/data/evidence.json}. Figure~\ref{fig:fleet} emphasizes that the three bars are discrete audits, not a continuous telemetry series. The 18 September early validation had eight T4 nodes, six serving replicas, two unallocated GPUs, and four aliases. A later pre-scale audit had eight nodes, seven replicas, one spare, and five aliases. The 20 September post-scale audit had five nodes, five replicas, no spare, and five aliases.

\begin{figure}[H]
\centering
\includegraphics[width=0.83\textwidth]{paper/figures/fleet_evolution.pdf}
\caption{Three separately recorded fleet snapshots. Bars combine serving replicas and unallocated T4 nodes; the line counts aliases. They are not samples of a continuous experiment, and the post-scale configuration exchanges rollout spare for lower standing capacity.}
\label{fig:fleet}
\end{figure}

\subsection{20 September live audit}

The current audit found five of five T4 nodes Ready and five allocatable GPUs. Five of five model-host pods were Ready. All five \code{FabricModelDeployment} resources reported Available, with five desired and five Ready replicas in total. Control-plane and inference readiness each returned HTTP 200. Account-scoped discovery returned all five aliases, and one authenticated chat request per alias returned HTTP 200. Public certificates were valid at the time of observation.

\begin{table}[H]
\centering
\caption{Point-in-time current model inventory on 20 September. Weight sizes are repository metadata, not measured VRAM use.}
\label{tab:models}
\begin{tabularx}{\textwidth}{lrrrX}
\toprule
Alias & Weight GB & Desired & Ready & Probe \\
\midrule
\code{qwen3.5-0.8b} & 1.7 & 1 & 1 & HTTP 200 \\
\code{qwen3.5-2b} & 4.5 & 1 & 1 & HTTP 200 \\
\code{qwen3.5-4b} & 9.3 & 1 & 1 & HTTP 200 \\
\code{qwen2.5-coder-3b} & 6.2 & 1 & 1 & HTTP 200 \\
\code{phi4-mini} & 7.7 & 1 & 1 & HTTP 200 \\
\bottomrule
\end{tabularx}
\end{table}

\begin{figure}[H]
\centering
\includegraphics[width=0.9\textwidth]{paper/figures/current_model_inventory.pdf}
\caption{Current five-model inventory and one point-in-time authenticated result per alias. The left panel uses nominal checkpoint metadata; the right panel is a reachability check, not a reliability, latency, or throughput sample. Every model has one T4 replica.}
\label{fig:inventory}
\end{figure}

The fleet ran the standard kernel configuration. With all five GPUs occupied and one replica per alias, the audit proves neither per-model redundancy nor safe spare-lane rollout. Loss of a node removes its model until Kubernetes reschedules and the host becomes Ready. The current PostgreSQL observation was Burstable B1ms with no HA. These facts supersede earlier target plans where they differ.

\subsection{18 September and older T4 records}

The 18 September early deployment used eight T4 nodes and four aliases. Six GPUs served models and two were left free. \code{qwen3.5-2b} used three replicas; three other aliases used one each. Shared settings included FP16, context 4096, maximum eight sequences, 0.85 GPU-memory utilization, eager execution, least-in-flight routing, and \code{kernel\_mode=standard}. The observability record counted 15 scrape targets at that snapshot: eight DCGM exporters, six model hosts, and one data plane. That count is not reported as current.

The same dated validation recorded text calls, one synthetic-image interpretation by a compatible Qwen model, refusal of a forged token, refusal of a control-audience token at inference, and a usage-export batch with five accepted and five acknowledged records. The retail-rate scenario estimated the eight-node system at \$7.63 per hour, of which \$6.616 was GPU compute and approximately \$1.01 was other standing cost. It was not an invoice.

A still older two-T4 pilot recorded approximately 4.25 GiB of node-local load in 3.3 seconds, a roughly 510-second cold path dominated by graph compilation, a separate approximately 344-second engine-initialization example under eager configuration, single-stream decode near 16 ms/token (about 63 tokens/s), and p95 time to first token near 1.6 seconds for that workload. Prompt and output distributions, repeated windows, and confidence intervals are unavailable. These values are operational anecdotes tied to the old topology, not current service objectives.

\subsection{Development-only kernel crossover}

Figure~\ref{fig:kernel} visualizes the paper evidence register's immutable, commit-pinned development record for one unpacked recurrence operation on an RTX 4070 Laptop GPU using vLLM 0.11.0 as the comparison. After 100 warmups and 500 repetitions, baseline/Fabric ratios were 1.188 at one sequence, 0.985 at 16, and 0.995 at 32. The underlying times were 19.041 versus 16.032 microseconds, 157.564 versus 159.932, and 305.720 versus 307.124. The advantage at one sequence disappears toward parity and slight baseline advantage at larger active-sequence counts.

\begin{figure}[H]
\centering
\includegraphics[width=0.82\textwidth]{paper/figures/kernel_crossover.pdf}
\caption{Commit-pinned RTX 4070 Laptop development microbenchmark for one vLLM 0.11.0 unpacked recurrence operation. It is not a T4 result, a full-model benchmark, or evidence about the current standard-kernel fleet; the crossover argues against extrapolating the batch-one point.}
\label{fig:kernel}
\end{figure}

Triton offers a tiled GPU programming model for such experiments \cite{triton}; FlashAttention demonstrates why memory hierarchy and IO behavior matter to kernel efficiency \cite{flashattention}. Neither fact closes the system-level evidence gap. The operation is a fraction of generation, current hosts select stock kernels, no immutable T4 full-model A/B series is reported, and no end-to-end Fabric-kernel acceleration claim is made.

\subsection{Cost and capacity}

Figure~\ref{fig:cost} applies the 18 September retail estimate to node-count scenarios. The five-node point is approximately \$5.145 per hour under the earlier \$1.01 fixed-cost assumption. It is a derived scenario despite the later PostgreSQL SKU change. There are no utilization measurements behind the curve and no PostgreSQL utilization chart.

\begin{figure}[H]
\centering
\includegraphics[width=0.82\textwidth]{paper/figures/cost_capacity.pdf}
\caption{Derived standing-cost scenarios from 18 September retail prices, not invoices. The five-node point reuses an earlier non-GPU fixed-cost assumption although PostgreSQL was later observed on another SKU. The plot contains no throughput or database-utilization inference.}
\label{fig:cost}
\end{figure}

Reducing standing GPUs also removed candidate capacity. Cost per hour alone cannot rank designs that provide different redundancy or rollout behavior. A future comparison should report accepted output tokens per GPU-second and per unit cost under stated TTFT and inter-token constraints, while preserving model quality and workload distribution.

\section{Related Work}

Orca introduced iteration-level scheduling and selective batching for generative models \cite{orca}. vLLM's PagedAttention organizes KV memory to reduce fragmentation and support flexible sharing \cite{vllm}. SGLang uses structured execution and prefix-oriented reuse \cite{sglang}; Sarathi-Serve studies chunked prefill \cite{sarathi}. Fabric delegates these engine concerns to vLLM and focuses on identity, placement, local authorization, routing evidence, and operations around the engine.

Clockwork seeks predictable DNN serving through controlled scheduling and execution \cite{clockwork}. DistServe separates prefill and decode resources to optimize SLO-qualified goodput \cite{distserve}. The present Fabric admission controller is much simpler: a stamp-local request bucket and in-flight bound. It does not claim SLO-optimal scheduling or phase disaggregation.

KServe applies Kubernetes architecture and autoscaling patterns to model serving \cite{kserve,kserveauto}. Fabric's narrower implementation emphasizes outbound stamp enrollment, account-bound token exchange, a custom desired/observed contract, concrete backend drain evidence, and durable local usage. It does not match KServe's breadth, autoscaling maturity, or ecosystem integrations.

Kubernetes controllers and operators supply the reconciliation pattern \cite{k8scontroller,k8soperator}; projected tokens and PDBs define important credential and disruption behavior \cite{k8stoken,k8spdb}. PostgreSQL RLS, JWT standards, Istio ingress, Cilium policy, Prometheus, and DCGM are established building blocks rather than Fabric inventions. The contribution of the analysis is their arrangement around independent clocks and explicit evidence boundaries.

\section{Limitations and Threats to Validity}

The present topology has one replica for each model. It has no global account limits, model autoscaling, scale-to-zero controller, cross-stamp failover, or spare rollout node. A node fault removes one alias until recovery. The gateway and coordinator are stamp-local, and no multi-zone gateway result or disaster-recovery RPO/RTO test is reported.

The network and secret posture remains a pilot posture. ACR, PostgreSQL, Key Vault, and AKS service access include public Azure endpoints. Egress is not default-deny. Model-host mTLS is not guaranteed. Local cluster-account and other hardening choices should be reviewed against the live configuration. PostgreSQL is a single Burstable B1ms server with seven-day backups, no HA, and no geo-redundant backup. Key and credential overlap rotation is not completely automated.

Usage is a bounded operational record, not a billing-grade ledger. The complete OpenAI API is not implemented. Model listing, chat completions, legacy completions, and selected audio proxy routes exist, but no compatible audio model was live at audit time. Responses, embeddings, images, Assistants, and realtime behavior are outside the demonstrated surface; tool use and structured output depend on the model and template.

The 20 September checks establish availability at one instant and one successful small request per model. They provide no post-scale throughput, tail-latency, fairness, saturation, or reliability estimate. The older two-T4 pilot, 18 September deployment, pre-scale audit, and post-scale audit differ in topology and possibly software state. The RTX result concerns one development micro-operation on different hardware. No end-to-end kernel effect can be inferred.

Reconciliation itself has known correctness gaps: same-release replica reduction did not converge, the projected-token client can lose one pass after a 401, and generation-like counters lack one end-to-end typed relation. A healthy CR status can still be stale with respect to central intent if counters are conflated. The paper reports these gaps because hiding them would defeat the evidence invariant.

\section{Future Work}

Immediate correctness work should first type the configuration protocol. The central deployment revision, stamp watermark, CR source revision, Kubernetes metadata generation, rendered digest, and route acknowledgement should have explicit fields and transition tests. Central readiness should compare related values rather than numerically ordered but independent counters. A same-release replica test should change three replicas to one and verify the Deployment, endpoints, route, and returned status. The Kubernetes client should reload a projected token per request or retry one safe operation after 401. The stamp should also isolate the agent's Kubernetes identity: either run the agent in a separate pod or replace pod-wide automount with an agent-only projected token volume, then test from the gateway and collector containers that CR mutation is denied.

Operational hardening should add digest-governed model-host promotion, automated credential overlap, restore drills, optional PostgreSQL HA, narrower public access, private endpoints where justified, retention protection, and tested certificate rotation. Model-host east--west encryption and egress policy should become explicit deployment choices with verification, not assumptions derived from public TLS.

Capacity management should treat cold state and rollout spare as resources. A model autoscaler must distinguish a schedulable GPU from a Ready model, and a recently created node from a node with image and weight cache. Queue length, KV pressure, TTFT, token progress, and accepted-work goodput are better inputs than CPU utilization alone. KServe's generative autoscaling work provides a relevant comparison point \cite{kserveauto}, but Fabric must also reserve or temporarily acquire candidate capacity.

Performance research should pin source, image, model, tokenizer, driver, CUDA, vLLM, and analysis versions; retain raw request events and scheduler batch distributions; randomize comparable windows; and publish uncertainty. Alternative kernels should default off outside validated cells, preserve an exact fallback, and be promoted only after full-generation token/logit and state checks on the actual T4 target. Negative crossover results should remain in the record.

Finally, the usage path can be strengthened with retention alarms, export-lag objectives, reconciliation reports, and optional tamper-evident sequencing. Those additions may support invoicing work later, but billing-grade language should wait for an auditable ledger, contractual rounding rules, correction workflows, and independent reconciliation.

\section{Conclusion}

Managed language-model serving cannot be understood as one control loop. It runs on a slow clock that moves intent through asynchronous cluster state and a fast clock that authorizes, admits, and advances requests without waiting for the center. Fabric makes that split concrete. Its central service manages identity and intent; an outbound agent publishes local desired state; an operator without a Fabric central credential realizes GPU workloads; an edge gateway verifies inference authority and owns streams; and a collector exports durable operational usage. These are distinct application roles, but the current shared stamp pod still exposes the agent Kubernetes token to the gateway and collector containers, so Kubernetes authority is not yet isolated at the process boundary.

Three invariants organize the design and expose where implementation still falls short. Authority should be divided so no public generation or telemetry process can exercise synchronization or broad Kubernetes mutation credentials; the application roles follow this rule, but the shared pod token currently violates strict enforcement. Authorization is evaluated beside the serving route with exact audience, scope, and account ownership. Evidence is required before a watermark advances, a leased record disappears, or an old release is destroyed. These invariants explain both continuity and refusal: known unexpired tokens and accepted routes can survive a central outage, while unknown keys, unsafe replay, unavailable admission authority, and unhealthy evidence storage fail conservatively.

The 20 September audit found five Ready T4 nodes, five Ready model hosts, five Available one-replica model resources, and one HTTP 200 completion from every alias. That is enough to establish a functioning five-model pilot at one point in time. It is not evidence of sustained throughput, global limits, model redundancy, billing accuracy, autoscaling, private-cloud hardening, or end-to-end kernel acceleration. The same-release scale-down gap, one-pass token-rotation delay, and absent rollout spare are therefore part of the result. The two-clock view is useful precisely because it requires every claim to name which clock advanced and what evidence proves it.

\appendix
\section{Reproducibility and Evidence Map}

The source baseline for the paper and evidence register is commit
\code{f50fdc6d25d0bb644fbcdb6703d82f09923a6bea} in the Fabric repository \cite{fabricrepo}. Reproduction has three levels.

\paragraph{Figures.}
Every plotted value is read from \code{paper/data/evidence.json}. From the repository root, regenerate the vector PDFs and review PNGs with:

\begin{verbatim}
uv run --project paper python paper/generate_figures.py
\end{verbatim}

The notebook \code{paper/notebooks/generate\_paper\_figures.ipynb} invokes the same generator rather than implementing a second analysis. Expected PDFs are:

\begin{verbatim}
paper/figures/cloud_architecture.pdf
paper/figures/fleet_evolution.pdf
paper/figures/current_model_inventory.pdf
paper/figures/admission_outcome.pdf
paper/figures/kernel_crossover.pdf
paper/figures/cost_capacity.pdf
\end{verbatim}

Do not impute absent post-scale latency, throughput, PostgreSQL utilization, or model-host mTLS observations into the evidence file.

\paragraph{Read-only live checks.}
A repeat audit should capture timestamps and source/image revisions; query the AKS GPU pool; count Ready GPU nodes and allocatable devices; inspect model-host pods; compare CR desired and Ready replicas; check the physical Kubernetes Deployment replica count; probe public control and inference readiness; exchange a test credential without logging it; list account-owned aliases; and issue one bounded request per alias. Because same-release scale-down has failed before, central intent, CR spec/status, Deployment replicas, endpoints, and loaded route revision should be recorded separately.

\paragraph{Security checks.}
Use a non-superuser, non-\code{BYPASSRLS} PostgreSQL application role and execute tenant-isolation tests against PostgreSQL, not only SQLite. Verify that an inference token cannot call control operations, a control token cannot call inference, a forged signature is rejected, and one account cannot discover or invoke another account's route. Rotate a temporary projected Kubernetes token during a test and confirm that one authorized operation is not lost.

\paragraph{Stream and usage checks.}
Exercise connection refusal before upstream establishment, an HTTP 5xx, a disconnect after SSE bytes, and a missing terminal usage frame. Confirm that only the pre-connection case is eligible for another backend, the stream remains pinned, leases release once, and untrusted usage is not fabricated. Interrupt collector delivery before and after central acceptance to verify stable replay IDs, duplicate recognition, and delayed local acknowledgement.

\paragraph{Claim discipline.}
Keep the 18 September, pre-scale, and 20 September fleet rows distinct. Report the RTX series only as a development microbenchmark for the named operation and hardware. Treat cost points as derived retail scenarios. Redact tokens, Key Vault values, account secrets, subscription identifiers, and raw credentials from artifacts.

\begin{thebibliography}{99}

\bibitem{fabricrepo}
Hexel Studio, ``Fabric source repository and technical records,'' commit \code{f50fdc6d25d0bb644fbcdb6703d82f09923a6bea}, 2026. \url{https://github.com/khushwant04/fabric}.

\bibitem{vllm}
W. Kwon et al., ``Efficient Memory Management for Large Language Model Serving with PagedAttention,'' \emph{Proceedings of ACM SOSP}, 2023. DOI: \href{https://doi.org/10.1145/3600006.3613165}{10.1145/3600006.3613165}.

\bibitem{orca}
G.-I. Yu et al., ``Orca: A Distributed Serving System for Transformer-Based Generative Models,'' \emph{Proceedings of USENIX OSDI}, 2022. \url{https://www.usenix.org/conference/osdi22/presentation/yu}.

\bibitem{clockwork}
A. Gujarati et al., ``Serving DNNs like Clockwork: Performance Predictability from the Bottom Up,'' \emph{Proceedings of USENIX OSDI}, 2020. \url{https://www.usenix.org/conference/osdi20/presentation/gujarati}.

\bibitem{distserve}
Y. Zhong et al., ``DistServe: Disaggregating Prefill and Decoding for Goodput-optimized Large Language Model Serving,'' \emph{Proceedings of USENIX OSDI}, 2024. \url{https://www.usenix.org/conference/osdi24/presentation/zhong-yinmin}.

\bibitem{sglang}
L. Zheng et al., ``SGLang: Efficient Execution of Structured Language Model Programs,'' arXiv:2312.07104, 2023. \url{https://arxiv.org/abs/2312.07104}.

\bibitem{sarathi}
A. Agrawal et al., ``Taming Throughput-Latency Tradeoff in LLM Inference with Sarathi-Serve,'' arXiv:2403.02310, 2024. \url{https://arxiv.org/abs/2403.02310}.

\bibitem{triton}
P. Tillet, H.-T. Kung, and D. Cox, ``Triton: An Intermediate Language and Compiler for Tiled Neural Network Computations,'' \emph{Proceedings of ACM MAPL}, 2019. DOI: \href{https://doi.org/10.1145/3315508.3329973}{10.1145/3315508.3329973}.

\bibitem{flashattention}
T. Dao et al., ``FlashAttention: Fast and Memory-Efficient Exact Attention with IO-Awareness,'' \emph{Advances in Neural Information Processing Systems}, 2022. \url{https://arxiv.org/abs/2205.14135}.

\bibitem{k8scontroller}
The Kubernetes Authors, ``Controllers,'' \url{https://kubernetes.io/docs/concepts/architecture/controller/}. Accessed September 2026.

\bibitem{k8soperator}
The Kubernetes Authors, ``Operator pattern,'' \url{https://kubernetes.io/docs/concepts/extend-kubernetes/operator/}. Accessed September 2026.

\bibitem{k8stoken}
The Kubernetes Authors, ``Projected volumes: service account token,'' \url{https://kubernetes.io/docs/concepts/storage/projected-volumes/}. Accessed September 2026.

\bibitem{k8spdb}
The Kubernetes Authors, ``Disruptions and Pod Disruption Budgets,'' \url{https://kubernetes.io/docs/concepts/workloads/pods/disruptions/}. Accessed September 2026.

\bibitem{jwt}
M. Jones, J. Bradley, and N. Sakimura, ``JSON Web Token (JWT),'' RFC 7519, IETF, 2015. \url{https://www.rfc-editor.org/rfc/rfc7519.html}.

\bibitem{jwtbcp}
Y. Sheffer, D. Hardt, and M. Jones, ``JSON Web Token Best Current Practices,'' RFC 8725, IETF, 2020. \url{https://www.rfc-editor.org/rfc/rfc8725.html}.

\bibitem{postgresrls}
PostgreSQL Global Development Group, ``Row Security Policies,'' \url{https://www.postgresql.org/docs/current/ddl-rowsecurity.html}. Accessed September 2026.

\bibitem{nistzt}
S. Rose, O. Borchert, S. Mitchell, and S. Connelly, ``Zero Trust Architecture,'' NIST SP 800-207, 2020. DOI: \href{https://doi.org/10.6028/NIST.SP.800-207}{10.6028/NIST.SP.800-207}.

\bibitem{openaistream}
OpenAI, ``Streaming API responses,'' API documentation. \url{https://platform.openai.com/docs/api-reference/chat/create#chat-create-stream}. Accessed September 2026.

\bibitem{promdata}
Prometheus Authors, ``Data model,'' \url{https://prometheus.io/docs/concepts/data_model/}. Accessed September 2026.

\bibitem{promhist}
Prometheus Authors, ``Histograms and summaries,'' \url{https://prometheus.io/docs/practices/histograms/}. Accessed September 2026.

\bibitem{dcgm}
NVIDIA, ``DCGM Exporter,'' \url{https://docs.nvidia.com/datacenter/cloud-native/gpu-telemetry/latest/dcgm-exporter.html}. Accessed September 2026.

\bibitem{istioingress}
Istio Authors, ``Secure Gateways,'' \url{https://istio.io/latest/docs/tasks/traffic-management/ingress/secure-ingress/}. Accessed September 2026.

\bibitem{cilium}
Cilium Authors, ``Kubernetes network policy,'' \url{https://docs.cilium.io/en/stable/network/kubernetes/policy/}. Accessed September 2026.

\bibitem{kserve}
KServe Authors, ``System architecture overview,'' \url{https://kserve.github.io/website/docs/concepts/architecture}. Accessed September 2026.

\bibitem{kserveauto}
KServe Authors, ``Generative inference autoscaling,'' \url{https://kserve.github.io/website/docs/model-serving/generative-inference/autoscaling}. Accessed September 2026.

\bibitem{azurewi}
Microsoft, ``Microsoft Entra Workload ID on Azure Kubernetes Service,'' \url{https://learn.microsoft.com/azure/aks/workload-identity-overview}. Accessed September 2026.

\bibitem{azurepgha}
Microsoft, ``High availability in Azure Database for PostgreSQL Flexible Server,'' \url{https://learn.microsoft.com/azure/postgresql/flexible-server/concepts-high-availability}. Accessed September 2026.

\bibitem{azurepgbackup}
Microsoft, ``Backup and restore in Azure Database for PostgreSQL Flexible Server,'' \url{https://learn.microsoft.com/azure/postgresql/flexible-server/concepts-backup-restore}. Accessed September 2026.

\bibitem{azureistio}
Microsoft, ``Deploy external or internal ingresses for Istio service mesh add-on for Azure Kubernetes Service,'' \url{https://learn.microsoft.com/azure/aks/istio-deploy-ingress}. Accessed September 2026.

\bibitem{azurecilium}
Microsoft, ``Azure CNI Powered by Cilium,'' \url{https://learn.microsoft.com/azure/aks/azure-cni-powered-by-cilium}. Accessed September 2026.

\bibitem{cloudflareproxy}
Cloudflare, ``Proxy status,'' DNS documentation. \url{https://developers.cloudflare.com/dns/proxy-status/}. Accessed September 2026.

\end{thebibliography}

\end{document}
```

All external source content was synthesized and rephrased for compliance with licensing restrictions.