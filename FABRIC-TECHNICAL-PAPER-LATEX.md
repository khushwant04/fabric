# Fabric Technical Paper — LaTeX Source

Copy the complete contents of the `latex` block into a file such as `fabric-paper.tex`, replace the author placeholders, and compile it with `pdflatex` twice. External material has been independently summarized and rephrased; original sources are cited in the bibliography for licensing compliance.

```latex
\documentclass[10pt,a4paper]{article}

\usepackage[margin=0.72in]{geometry}
\usepackage{amsmath,amssymb}
\usepackage{booktabs}
\usepackage{tabularx}
\usepackage{longtable}
\usepackage{array}
\usepackage{enumitem}
\usepackage{xcolor}
\usepackage{listings}
\usepackage{graphicx}
\usepackage{float}
\usepackage{caption}
\usepackage{microtype}
\usepackage{url}
\usepackage[hidelinks]{hyperref}
\usepackage[T1]{fontenc}
\usepackage{lmodern}

\definecolor{codegray}{RGB}{245,246,248}
\definecolor{codeblue}{RGB}{18,78,130}
\definecolor{codegreen}{RGB}{32,112,68}
\lstdefinestyle{fabric}{
  backgroundcolor=\color{codegray},
  basicstyle=\ttfamily\small,
  breaklines=true,
  frame=single,
  rulecolor=\color{black!20},
  keywordstyle=\color{codeblue}\bfseries,
  commentstyle=\color{codegreen},
  showstringspaces=false,
  columns=fullflexible
}
\lstset{style=fabric}

\newcommand{\system}{\textsc{Fabric}}
\newcommand{\code}[1]{\texttt{#1}}
\newcommand{\live}{\textbf{Live observation}}
\newcommand{\repo}{\textbf{Repository evidence}}
\newcommand{\design}{\textbf{Design claim}}
\newcommand{\todo}{\textbf{Future work}}
\newcommand{\yes}{\ensuremath{\checkmark}}
\newcommand{\no}{--}
\renewcommand{\arraystretch}{1.15}
\setlength{\parskip}{0.2em}
\setlength{\parindent}{1.1em}
\sloppy

\title{\textbf{Fabric: A Security- and Failure-Aware Control Plane for Multi-Tenant GPU Inference on Kubernetes}}
\author{Author Name\\
Hexel Studio\\
\texttt{author@example.com}}
\date{September 2026}

\begin{document}

\maketitle
\begin{abstract}
Production large-language-model (LLM) serving is not only an engine optimization problem. A useful platform must separate tenant identities, convert deployment intent into GPU workloads, keep accepted inference available when central services fail, meter streamed work durably, and change model releases without confusing desired state with serving state. This paper presents \system, a managed inference platform that separates a centrally operated control plane from independently serving Kubernetes ``stamps.'' The control plane manages accounts, identity exchange, deployment intent, placement, and usage aggregation. Each stamp contains a credentialed outbound agent, a Kubernetes-privileged but centrally uncredentialed operator, an OpenAI-compatible data plane, a write-only usage collector, and vLLM model hosts. Short-lived, audience-specific JSON Web Tokens are verified locally from cached JSON Web Key Sets. Tenant ownership is enforced both in application queries and through forced PostgreSQL row-level security. Desired state crosses the central/cluster boundary as generation-numbered custom resources; observed state returns separately. Usage is first committed to a bounded local SQLite spool, then leased to a telemetry-only collector and deduplicated centrally.

We describe the architecture, trust boundaries, state machines, routing and rollout semantics, deployment on Azure Kubernetes Service (AKS), and pilot evaluation. In the current audited topology, five NVIDIA T4 nodes serve five model varieties with one replica each. All five nodes and model hosts were Ready, all model custom resources reported Available, control and inference readiness returned HTTP 200, and an authenticated request to every model succeeded. Earlier controlled observations measured approximately 63 output tokens/s for one stream, 1.6 s p95 time-to-first-token, and correct refusal of excess concurrency. These figures characterize specific snapshots rather than universal performance. We also report negative and incomplete results: current production uses the stock engine kernel path; no end-to-end kernel speedup is claimed; metering is operational rather than billing-grade; a same-release replica decrease required a one-time direct Kubernetes correction; and a five-node/five-model topology leaves no spare GPU for non-disruptive candidate rollout. The central result is therefore architectural: explicit authority separation, local authorization, durable evidence, and conservative failure semantics can make a modest GPU fleet suitable for a controlled API pilot without presenting it as a complete hyperscale serving system.
\end{abstract}

\medskip
\noindent\textbf{Keywords:} LLM serving, Kubernetes operator, multi-tenancy, GPU inference, control plane, JWT, durable metering, vLLM, observability, failure isolation.
\bigskip
\fontsize{9.5pt}{11pt}\selectfont

\section{Introduction}

Autoregressive language-model inference combines an unusual execution profile with an unusually broad operational surface. A request first performs a compute-intensive prefill over its prompt and then repeatedly executes comparatively small decode iterations. Iteration-level scheduling and continuous batching improve device utilization, while paged key--value (KV) memory reduces fragmentation and permits larger effective batches \cite{orca,vllm}. These mechanisms explain why engines such as vLLM are central to modern serving. They do not, however, answer which tenant may invoke a model, who is allowed to place a workload, what happens when identity or database services are unavailable, how streamed token usage survives a process crash, or when a rollout is safe to finalize.

This paper examines those questions through \system, an implemented managed inference platform. The project began with a strict principle: normal inference must not traverse the central control plane. A central API can be unavailable while already accepted model deployments continue serving with cached public keys and local routes. The resulting system resembles cloud control/data-plane designs but adapts them to LLM-specific concerns: expensive cold starts, non-idempotent generation, continuous streams, mutable scheduler state, large local model caches, scarce GPU rollout capacity, and model-reported token counts.

\system's unit of deployment is a \emph{stamp}: a Kubernetes cluster or cluster partition that exposes an inference gateway and owns local model-host execution. The control plane stores account and deployment intent. A stamp agent polls outward, materializes intent as a custom resource, and reports observations. A separate operator reconciles that resource into model-host Deployments and Services. The gateway performs local JWT verification, tenant authorization, admission, backend selection, streaming proxying, and durable usage recording. A separate collector exports usage using a credential that cannot read desired state. This decomposition deliberately prevents a single process from simultaneously holding central credentials and broad Kubernetes mutation authority.

The paper makes four contributions.

\begin{enumerate}[leftmargin=*]
  \item It presents a concrete control/data-plane protocol for account-bound LLM deployment and inference in which central unavailability does not automatically remove accepted serving state.
  \item It describes defense in depth across short-lived audience-specific tokens, local model-ownership checks, forced PostgreSQL row-level security (RLS), distinct machine credentials, Kubernetes RBAC, and restricted listeners.
  \item It develops failure-aware reconciliation, routing, rollout, rate limiting, and at-least-once usage transport semantics that avoid unsafe replay of non-idempotent generation.
  \item It reports implementation and pilot evidence on AKS, while separating current live observations, historical measurements, implemented mechanisms, and proposed research. This separation prevents microbenchmark or topology results from being generalized beyond their evidence.
\end{enumerate}

\subsection{Claim taxonomy}

System papers often blur architecture, implementation, and production evidence. We use the taxonomy in Table~\ref{tab:claims} throughout.

\begin{table}[H]
\centering
\caption{Evidence classes used in this paper.}
\label{tab:claims}
\begin{tabularx}{\textwidth}{p{0.16\textwidth}X}
\toprule
Class & Meaning \\
\midrule
Design & Accepted architecture or policy; implementation is not implied. \\
Implemented & Present in source and normally covered by unit or integration tests. \\
Repository-recorded & A measurement recorded in project documents or immutable artifacts; not independently rerun for this paper. \\
Live observation & Direct Azure, Kubernetes, HTTP, or inference observation made during the September 2026 operational audit. \\
Future work & Proposed capability or experiment; no effectiveness claim is made. \\
\bottomrule
\end{tabularx}
\end{table}

The current source baseline is commit \code{6a5bfdf}. The current operational snapshot follows a scale-down from eight to five T4 nodes and from seven to five model-host replicas. Historical two-node and eight-node results are retained only when labeled. The source and internal technical records are available in the project repository \cite{fabricrepo}.

\section{Background and Motivation}

\subsection{LLM serving engines and the platform boundary}

Orca established iteration-level scheduling for generative models, allowing work to enter and leave a batch between decode iterations rather than waiting for an entire request to finish \cite{orca}. vLLM's PagedAttention organizes KV blocks using ideas analogous to virtual memory, reducing fragmentation and enabling flexible sharing \cite{vllm}. SGLang extends serving with a structured program model and prefix-oriented RadixAttention \cite{sglang}. DistServe separates prefill and decode onto different resources to reduce cross-phase interference and optimize SLO-qualified goodput \cite{distserve}. These systems substantially advance engine-level scheduling, memory management, and execution.

\system does not replace those engines. In the evaluated deployment, vLLM remains responsible for tokenization, prefill and decode scheduling, PagedAttention, model execution, KV management, CUDA-graph behavior, and sampling. \system wraps the engine with account identity, placement, cluster reconciliation, admission, endpoint routing, failure containment, telemetry, and audit. This boundary is intentional: duplicating a mature engine scheduler would add risk without strengthening the platform's authority and availability model.

A useful distinction is therefore:

\begin{equation}
  T_{\mathrm{customer}} = T_{\mathrm{gateway}} + T_{\mathrm{queue}} + T_{\mathrm{prefill}} + T_{\mathrm{decode}} + T_{\mathrm{network}},
\end{equation}

where engine research commonly optimizes the middle three terms, while the platform must also bound gateway work, identity failure, routing, refusal behavior, deployment availability, and evidence loss. Customer-visible success further depends on authorization and admission:

\begin{equation}
  G_{\mathrm{accepted}} = \lambda \,(1-p_{\mathrm{auth}}-p_{\mathrm{limit}}-p_{\mathrm{capacity}}-p_{\mathrm{backend}}),
\end{equation}

where $\lambda$ is offered request rate and each $p$ denotes a mutually classified refusal or failure probability. Engine throughput alone cannot explain requests rejected before reaching a GPU.

\subsection{Why central services should not be in the token path}

A central API and database are useful for account lifecycle, key exchange, deployment intent, and placement. Making them synchronous dependencies of every generated token creates an avoidable availability coupling. The cost is especially high for streaming responses: a request may remain active for tens of seconds or minutes, and replay after partial output is unsafe. \system instead issues short-lived inference tokens and distributes public verification keys plus local deployment ownership to the stamp. The gateway can then authorize a request without contacting the control plane.

The design follows a resource-centric interpretation of zero trust: network location alone does not grant authority, and each action is checked against an identity and resource \cite{nistzt}. It is not a claim of complete zero-trust compliance. Current egress is not default-deny, model-host mutual TLS is optional rather than mandatory, and several Azure endpoints are publicly reachable. The contribution is the explicit credential and authorization boundary, not a certification.

\subsection{Why desired state differs from serving state}

A declaration that a model should have one replica is not proof that the model can answer. Weight download, graph compilation, GPU scheduling, health checks, and endpoint publication occur asynchronously. Kubernetes controllers formalize this distinction by converging observed state toward desired state \cite{k8scontroller,k8soperator}. \system carries the same separation across a central-to-cluster boundary: the control plane owns intent; the agent publishes a custom resource; the operator owns workload realization and status; and the gateway owns the final route revision.

This separation also affects deletion and rollout. Removing intent cannot immediately erase a serving route if accepted streams remain. Conversely, observing an old Ready status must not acknowledge a newer generation. The protocol therefore uses monotonically increasing generations and keeps desired, observed, and routed revisions distinct.

\section{Goals, Non-Goals, and Threat Model}

\subsection{Design goals}

The implemented system targets the following goals:

\begin{itemize}[leftmargin=*]
  \item \textbf{Tenant isolation.} A verified account identity must only discover and invoke that account's deployments; control-plane persistence should reject accidental unscoped access.
  \item \textbf{Independent serving.} Accepted inference should continue with local routes and cached keys during a central outage, bounded by token validity and key availability.
  \item \textbf{Least combined authority.} Central credentials and broad Kubernetes workload mutation should not coexist in one process.
  \item \textbf{Conservative failure.} Unknown keys, unhealthy spools, unavailable limit coordination, stale observations, and ambiguous generation failures should fail closed or preserve the last accepted state rather than invent success.
  \item \textbf{Auditable convergence.} Generation and route revisions should reveal whether intent, workload, and gateway routing agree.
  \item \textbf{OpenAI-compatible access.} Existing clients should use model listing, chat completions, legacy completions, and streaming without a proprietary inference protocol.
  \item \textbf{Operational evidence.} Gateway, model-engine, and GPU telemetry should remain separate but correlatable.
\end{itemize}

\subsection{Non-goals}

The current implementation is not a global hyperscale scheduler, a billing ledger, a complete OpenAI API implementation, or a claim of universally faster model execution. It does not implement multi-region failover, multi-node tensor parallelism, disaggregated prefill/decode, scale-to-zero automation, a complete frontend, or global account limits across stamps. The public data plane supports \code{/v1/models}, chat and legacy completions, and two audio proxy routes; it does not implement the Responses, embeddings, image-generation, Assistants, or Realtime APIs.

\subsection{Adversaries and failures}

The threat model includes a customer attempting cross-account access, a leaked raw API key, a compromised stamp process, a malformed desired-state update, a backend that fails before or during generation, central control-plane unavailability, process or node restart, duplicate telemetry delivery, and overloaded admission. It assumes the underlying cloud, Kubernetes control plane, container isolation, cryptographic libraries, and database engine are not malicious. Cluster administrators and database superusers remain trusted; PostgreSQL RLS cannot constrain a superuser or a role with \code{BYPASSRLS}.

\section{System Architecture}

\subsection{Components}

Figure~\ref{fig:architecture} summarizes the architecture. The control plane is a FastAPI service backed by PostgreSQL. It owns accounts, memberships, API keys, per-account OIDC providers, token exchange, deployment intent, placement, stamp enrollment, observed status, audit, and usage aggregation. A stamp contains an agent, operator, data plane, collector, and model hosts.

\begin{figure}[H]
\centering
\fbox{\begin{minipage}{0.94\textwidth}
\small\ttfamily
\begin{tabular}{c}
CUSTOMER / OPENAI CLIENT\\
\quad | control operations \hspace{3.4cm} | inference\\
\quad v \hspace{5.4cm} v\\[2pt]
+----------------------------+ \hspace{0.4cm} +----------------------------+\\
| CENTRAL CONTROL PLANE      | \hspace{0.4cm} | STAMP DATA PLANE           |\\
| accounts, keys, OIDC, JWT  | \hspace{0.4cm} | local JWT + ownership      |\\
| deployment intent/status   | \hspace{0.4cm} | limits, routing, SSE       |\\
| placement, usage, audit    | \hspace{0.4cm} | durable usage spool        |\\
+-------------+--------------+ \hspace{0.4cm} +-------------+--------------+\\
\quad | PostgreSQL/RLS \hspace{4.7cm} |\\
\quad | \hspace{6.0cm} v\\
\quad | \hspace{4.4cm} +----------------------------+\\
\quad | \hspace{4.4cm} | vLLM MODEL HOSTS (GPU)     |\\
\quad | \hspace{4.4cm} +----------------------------+\\
\quad | desired state (outbound poll) \hspace{1.8cm} \^{} operator creates\\
\quad v \hspace{6.2cm} |\\
+-----------------------------------------------------------------------+\\
| STAMP: AGENT -> FabricModelDeployment CR -> OPERATOR                  |\\
|        COLLECTOR -> usage lease/ack -> central telemetry              |\\
+-----------------------------------------------------------------------+
\end{tabular}
\end{minipage}}
\caption{\system separates central product management from the local inference path. Arrows from the stamp to the control plane are outbound.}
\label{fig:architecture}
\end{figure}

The \textbf{agent} has a Fabric stamp credential and enough Kubernetes authority to publish \code{FabricModelDeployment} resources and, when enabled, read capacity. It does not own model-host Deployments. The \textbf{operator} has Kubernetes RBAC to realize custom resources but no Fabric control-plane credential. The \textbf{collector} has a telemetry-only credential and drains usage from a private loopback administrative endpoint. The \textbf{data plane} verifies inference tokens and routes requests but holds neither the agent credential nor telemetry export credential. The \textbf{model host} receives no Kubernetes service-account token because it need not call the Kubernetes API.

\subsection{The stamp contract}

A stamp enrolls using a short-lived, single-use token. The control plane derives ownership and allowed mode from the stored enrollment record rather than trusting the request body. Successful enrollment returns distinct agent and telemetry credentials. Identity state is persisted with owner-only permissions; restart reuses the identity rather than consuming another enrollment token.

Deployment state crossing into a stamp includes a deployment identifier, account identifier, public alias, internal model release, desired replicas, GPU count, routing strategy, token-verification fields, and runtime limits. The account on the assignment is the deployment owner, not necessarily the stamp owner; this distinction allows centrally owned managed capacity to host customer workloads without conflating identities.

\subsection{State progression}

Let $g_c$ be the control-plane desired generation, $g_a$ the generation durably acknowledged by the agent, $g_o$ the operator's observed generation, and $r_d$ the route revision reported by the data plane. A healthy steady state satisfies

\begin{equation}
   g_c = g_a = g_o, \qquad r_d = r_o,
\end{equation}

where $r_o$ is the operator-published route revision. The equalities are eventual, not transactional across systems. Safety relies on ordering:

\begin{enumerate}[leftmargin=*]
  \item The agent fetches assignments after $g_a$ and constructs the complete desired local set.
  \item It publishes or updates custom resources before persisting $g_a$.
  \item The operator materializes workloads and publishes concrete ready endpoints.
  \item Operator status carries the CR generation it observed; stale status cannot acknowledge newer intent.
  \item For rollout, the old workload is deleted only after the data plane acknowledges the new route revision and reports no in-flight work on the old backend.
\end{enumerate}

A crash between publication and acknowledgement causes replay, not omission. Reconciliation operations are designed to be idempotent under that replay.

\section{Identity, Authorization, and Tenant Isolation}

\subsection{Human and machine identities}

The control plane accepts three identity sources for token exchange: a human identity assertion from Auth0, an account-configured OIDC provider, or a Fabric API key. A raw API key is not accepted by the inference data plane. It is exchanged for a short-lived RS256-signed Fabric token with an explicit audience. Control tokens target \code{fabric-control}; inference tokens target \code{fabric-inference}. JWT structure follows RFC~7519, while explicit algorithm, issuer, audience, and claim validation follows the threat guidance of RFC~8725 \cite{jwt,jwtbcp}.

An inference token contains a subject, issuer, audience, issue/not-before/expiry times, token identifier, account identifier, principal type, and scopes. The gateway requires the inference audience and \code{inference:invoke}. A token minted for control operations is rejected even when its signature and issuer are valid. Public keys are served as JWKS. The data plane caches keys, refreshes on an unknown key identifier, and retains last-known-good keys when refresh fails. This preserves accepted identities during a transient central outage but intentionally refuses a newly rotated key it has never observed.

Token lifetime bounds revocation delay. It does not provide immediate revocation of an already issued JWT. API-key and stamp credential records support revocation, but complete automated overlap rotation through a KMS/HSM remains future work.

\subsection{Authorization at the gateway}

A model alias is not authority. After cryptographic verification, the gateway resolves an alias or deployment identifier against local route configuration and compares the route's account with the token account. Only then does admission and backend selection occur. Client-supplied authorization and internal ownership headers are removed before proxying so a caller cannot inject Fabric identity into the model host.

This local check is critical to availability: a central authorization lookup is not required for each request. It is also critical to isolation: cached routing state carries both endpoint and ownership, not merely a globally unique alias. Model listing returns only aliases belonging to the token account.

\subsection{Database defense in depth}

Every account-scoped control-plane row carries \code{account\_id}. Service-layer queries use the account derived from the verified credential. PostgreSQL RLS adds a second enforcement layer: policies constrain both row visibility and inserted/updated rows. PostgreSQL documents that row-security policies supplement ordinary privileges and can filter reads and writes \cite{postgresrls}. \system enables and forces RLS on account tables and runs the application under a role verified to be neither superuser nor \code{BYPASSRLS}.

Tenant context is transaction-local rather than session-global. This avoids a pooled connection retaining the previous request's account. Tests deliberately execute unfiltered SQL under different tenant contexts to verify that foreign rows do not appear. SQLite tests remain useful for service behavior but cannot establish RLS; a PostgreSQL test lane is therefore necessary.

Some machine operations legitimately cross account boundaries. A system-owned managed stamp may report status for customer-owned placements. Such operations use controlled elevation plus explicit ownership checks. This exception is a larger trust surface than ordinary tenant routes and is treated as such rather than hidden behind a blanket RLS claim.

\subsection{Cluster authority separation}

Table~\ref{tab:authority} shows the intended authority matrix. The design reduces the value of compromising one component.

\begin{table}[H]
\centering
\caption{Primary authority separation inside a stamp.}
\label{tab:authority}
\begin{tabularx}{\textwidth}{lXXXX}
\toprule
Component & Central desired state & Kubernetes workloads & Inference tokens & Usage export \\
\midrule
Agent & Read/write status & CR intent only & No & No \\
Operator & No Fabric credential & Model workloads/status & No & No \\
Data plane & Cached routes/JWKS & No workload mutation & Verify & Local spool only \\
Collector & No desired-state read & No workload mutation & No & Write-only \\
Model host & No & Own process only & No Fabric identity & Reports engine usage \\
\bottomrule
\end{tabularx}
\end{table}

This is least combined authority rather than zero privilege. Capacity measurement requires the agent to read selected node and pod information, and CR publication requires Kubernetes API access. The important boundary is that the centrally credentialed agent cannot directly mutate operator-generated model hosts, while the Kubernetes-privileged operator cannot authenticate to the central product API.

\section{Inference Data Path}

\subsection{Request processing}

For chat and legacy completions, the gateway executes the following ordered path:

\begin{enumerate}[leftmargin=*]
  \item Parse the bearer token and verify signature, issuer, time, audience, account, and scope.
  \item Resolve the requested model within that account's local deployments.
  \item Check that the durable usage spool and shared limit coordinator are healthy.
  \item Admit the request against account RPM/burst and in-flight limits.
  \item Select an eligible backend using the deployment's balancing policy.
  \item Replace the public alias with the internal engine release and strip internal headers.
  \item Proxy the non-streaming request or pin a stream to the selected backend.
  \item Record usage durably, release leases, and update gateway metrics.
\end{enumerate}

Authentication and admission occur before GPU work. A refused request therefore does not consume model-host compute. Current balancing choices include least-in-flight, round robin, session affinity using rendezvous hashing, and weighted random selection. The operator publishes concrete ready EndpointSlice addresses from a headless Service; this lets the gateway track health and in-flight work per model-host pod rather than relying on opaque ClusterIP balancing.

\subsection{Failure-aware backend selection}

Generation is non-idempotent. If a TCP connection cannot be established, the selected host could not have begun work, so the gateway may eject it temporarily and try another eligible host. After request bytes are written, response bytes are read, or an HTTP/protocol error occurs, replay is ambiguous and is not attempted. A 5xx response might still correspond to partially consumed GPU work. Once a stream yields any bytes, it remains pinned to that backend. Mid-stream failure becomes an SSE error event and cleanup; switching hosts would duplicate or contradict output.

This policy favors semantic safety over aggressive availability. It follows from the absence of an application-level idempotency key that can reproduce an identical stochastic generation. Control-plane deployment creation does support idempotency keys, but inference generation does not.

\subsection{OpenAI-compatible streaming}

The gateway exposes \code{/v1/chat/completions} and \code{/v1/completions}. With \code{stream=true}, it relays server-sent events (SSE), a common Chat Completions transport \cite{openaistream}. It requests an upstream terminal usage frame for accounting. If the caller did not request usage in its stream, the gateway consumes the terminal accounting-only frame rather than exposing a protocol difference. It preserves content frames as bytes to avoid lossy reserialization.

Stream metering is conservative. Missing or malformed terminal usage is classified as unmetered rather than estimated. The gateway records the reason for operational visibility. This avoids billing claims based on reconstructed chunk counts, since an SSE chunk need not correspond to exactly one tokenizer token.

\subsection{Multimodal and audio boundaries}

Chat JSON is intentionally passed through with minimal schema transformation beyond the model name. A compatible model host can therefore accept \code{image\_url} content parts, including a base64 data URI. A repository-recorded live test showed a Qwen3.5 model interpreting a synthetic image. This is evidence for that model and request shape, not a claim that all aliases are multimodal.

Audio transcription and translation routes accept multipart uploads, enforce a configured size bound, preserve relevant text fields, and route through the same identity, ownership, admission, and usage path. Audio streaming is explicitly rejected. At the audited deployment, no compatible audio model was placed, so route implementation is not equivalent to live audio capability.

\section{Reconciliation and Release Management}

\subsection{Custom-resource realization}

The agent publishes one \code{FabricModelDeployment} custom resource per assigned deployment. Its spec includes desired execution state; its status is written by the operator. The operator renders a Kubernetes Deployment with one pod per model replica, an \code{nvidia.com/gpu} limit, readiness at \code{/health}, a long startup window for weight loading and graph compilation, GPU labels/taints, and preferred anti-affinity between model hosts. The host does not mount a Kubernetes token.

Model weights and compilation artifacts use node-local storage by default. Environment variables place Hugging Face, vLLM, and TorchInductor caches under a shared node path. This makes pod replacement on the same node substantially cheaper but does not preserve cache when an ephemeral node is deleted or guarantee cache reuse across nodes.

\subsection{Acknowledged rollout}

A release change is represented by a candidate workload with a deterministic release-specific identity. The operator can run it beside the active workload, wait for all desired replicas and concrete endpoints, publish weighted routing that drains the old release, wait for the data plane to acknowledge the exact route revision and report zero old-backend in-flight work, and then delete the old workload. A failed candidate is removed while the old route remains active.

This is an acknowledged 0/100 cutover primitive, not a complete SLO-driven progressive delivery product. It requires enough spare GPU capacity to run active and candidate replicas concurrently. The current five-node topology uses all five GPUs for five models, so no GPU remains for a one-replica candidate. A pilot rollout must temporarily add a node, stop another model, or accept a recreate window.

\subsection{Observed replica convergence gap}

The September 2026 scale-down exposed an important operational defect. The control API successfully changed \code{qwen3.5-2b} from three replicas to one, and the desired count propagated to the custom resource. The deployed operator continued reporting a healthy same-release workload with three replicas rather than patching it down. After ten minutes of non-convergence, the Kubernetes Deployment was safely scaled to one because both the central desired state and CR specified one. The fleet then converged to five model pods before the AKS GPU pool was reduced from eight nodes to five.

This defect did not interrupt inference, but it demonstrates why desired, observed, and physical state must be measured independently. It should be fixed with a regression test for same-release replica changes. Until then, replica reductions require verification of both CR and Deployment counts.

\section{Admission, Usage, and Observability}

\subsection{Stamp-local admission}

Admission combines a continuously refilled per-account request bucket with a maximum in-flight count. A private stamp-local coordinator permits multiple gateway processes to share state without placing the central control plane on the request path. Request identifiers make admission idempotent, and renewable leases recover capacity after a dead gateway. If coordination is unavailable, new work receives 503 rather than silently exceeding limits. Capacity refusals return 429 with \code{Retry-After}.

The current scope is stamp-local. An account deployed to multiple stamps can exceed a nominal global limit. Token-per-minute, monthly budgets, reservations, and first-class per-key policies remain future work.

\subsection{Durable at-least-once usage}

When an engine reports prompt and completion token counts, the gateway writes a stable event to a bounded SQLite spool on a persistent volume. A private administrative endpoint leases records non-destructively to the collector. The collector sends them using a telemetry-only credential and acknowledges the lease only when each central result is accepted, identified as duplicate, or permanently rejected. A crash or lost acknowledgement replays the same stable identifiers; the control plane deduplicates per stamp.

This is at-least-once transport with deduplication, not exactly-once execution. The bounded spool may drop the oldest unleased records under sustained overflow, records outside accepted time bounds can be rejected, and a permanently disconnected stamp contributes no central usage. Consequently, usage is suitable for operations and pilot attribution but is not represented as a billing-grade ledger.

\subsection{Three-layer telemetry}

The observability design keeps three layers distinct:

\begin{enumerate}[leftmargin=*]
  \item \textbf{Gateway metrics} describe customer-visible requests, auth/ownership/limit outcomes, end-to-end latency, in-flight work, backend selection/ejection, verification posture, and metering state.
  \item \textbf{Engine metrics} describe vLLM queueing, running and waiting requests, cache behavior, token totals, and model execution.
  \item \textbf{GPU metrics} are exported by NVIDIA DCGM, which exposes device telemetry for Prometheus \cite{dcgm}. These include utilization, framebuffer memory, temperature, power, and clocks where available.
\end{enumerate}

Prometheus uses a dimensional label model, so account, deployment, outcome, and backend dimensions require bounded cardinality \cite{promdata}. Request identifiers are not metric labels. Latency distributions use histograms, from which aggregate quantiles can be calculated \cite{promhist}. Raw benchmark events are still needed for publication-grade recomputation; operational percentiles alone do not preserve independent samples or causal timing.

\section{Implementation and Azure Deployment}

\subsection{Software implementation}

The control plane and data plane are Python/FastAPI services. PostgreSQL access uses SQLAlchemy and Alembic. The agent, operator, and collector are Go binaries. Helm charts package the control plane and stamp. Model hosts use a vLLM OpenAI-compatible server image. CI tests Python services, Go components, Helm rendering, shared limits, and durable usage packaging.

The guarded Azure deployment workflow accepts a full Git commit SHA, verifies that it belongs to the main branch, builds control-plane, agent, and data-plane images for AMD64 in Azure Container Registry (ACR), resolves immutable digests, and deploys in a deliberate order: CRD schema, control plane and migrations, agent/operator, verification contract, and finally data plane. Helm upgrades are atomic and waited. Model-host image promotion is not yet included in that automated workflow and remains a provenance gap.

\subsection{Cloud topology}

Table~\ref{tab:azure} summarizes the audited deployment. Resource names are included for reproducibility but subscription and secret identifiers are omitted.

\begin{table}[H]
\centering
\caption{Audited Azure deployment in Central India.}
\label{tab:azure}
\begin{tabularx}{\textwidth}{p{0.22\textwidth}p{0.30\textwidth}X}
\toprule
Resource & Name/configuration & Purpose \\
\midrule
Resource group & \code{rg-inference} & Isolated Fabric resources \\
AKS & \code{aks-inference-cin-01}, Kubernetes 1.35.7 & CPU control workloads and GPU model hosts \\
System pool & 3 $\times$ \code{Standard\_D4s\_v5} & Control plane, stamp, Istio, monitoring \\
GPU pool, current & 5 $\times$ \code{Standard\_NC8as\_T4\_v3} & One 16-GiB T4 and one model replica per node \\
ACR & \code{acrfabricinference}, Standard & Four Fabric image repositories \\
PostgreSQL & v18, \code{Standard\_B1ms}, 64 GiB & Control state, RLS, audit, usage \\
Key Vault & RBAC, 90-day soft delete & Signing key and deployment secrets \\
Ingress & Istio external gateway & TLS control, inference, and Grafana hosts \\
Observability & Prometheus, Grafana, DCGM & Gateway, engine, cluster, and GPU metrics \\
\bottomrule
\end{tabularx}
\end{table}

AKS uses Azure CNI with Cilium, Entra/Azure RBAC, OIDC, and workload identity \cite{azurewi}. Cilium applies Kubernetes network policies across nodes \cite{cilium}. Istio terminates public HTTPS; Istio supports both secure ingress and service traffic controls, although current model-host mTLS is not mandatory \cite{istio}. cert-manager maintains three public certificates.

The current PostgreSQL instance is intentionally reported as observed: a Burstable B1ms server with seven-day backups, no high availability, and no geo-redundant backup. Azure offers synchronous primary/standby HA and geo-redundant recovery mechanisms \cite{azurepgha,azurepgbackup}, but these are not enabled in the pilot. A previous deployment document described a larger General Purpose target; the live query supersedes that plan.

\subsection{Current model fleet}

\begin{table}[H]
\centering
\caption{Post-scale pilot fleet. Each model had one Ready replica and returned HTTP 200 to an authenticated chat request.}
\label{tab:fleet}
\begin{tabularx}{\textwidth}{lXcc}
\toprule
Alias & Upstream release & Replicas & Kernel mode \\
\midrule
\code{qwen3.5-0.8b} & Qwen/Qwen3.5-0.8B & 1 & standard \\
\code{qwen3.5-2b} & Qwen/Qwen3.5-2B & 1 & standard \\
\code{qwen3.5-4b} & Qwen/Qwen3.5-4B & 1 & standard \\
\code{qwen2.5-coder-3b} & Qwen/Qwen2.5-Coder-3B-Instruct & 1 & standard \\
\code{phi4-mini} & microsoft/Phi-4-mini-instruct & 1 & standard \\
\bottomrule
\end{tabularx}
\end{table}

All five GPUs are allocated. This configuration maximizes model variety per active GPU but provides no redundancy for an individual model and no spare candidate lane. A node or pod failure temporarily removes that model until rescheduling and cold/warm startup complete.

\section{Evaluation}

\subsection{Questions and method}

The evaluation asks four questions:

\begin{description}[leftmargin=2.3cm,style=nextline]
  \item[Q1: Deployment] Does central intent converge into healthy GPU workloads and routes?
  \item[Q2: Isolation] Are wrong or absent credentials refused, and is tenant persistence constrained?
  \item[Q3: Failure] Do local serving, usage, and reconciliation have explicit behavior under dependency failure?
  \item[Q4: Pilot viability] Can a small commodity-GPU fleet expose multiple model varieties with observable health and predictable admission?
\end{description}

Evidence comes from source/tests, repository-recorded deployment measurements, and direct live audit commands. Live checks included Azure resource queries, AKS node-pool inspection, Kubernetes node/pod/CR status, public health probes, token exchange, model discovery, and one authenticated chat completion per model. Secrets and raw tokens were not recorded.

This is an engineering validation, not a controlled comparative performance study. No production trace was replayed, no confidence interval is reported for throughput, and the post-scale five-node topology was not load-tested to saturation.

\subsection{Current live results}

\begin{table}[H]
\centering
\caption{Current live observations after scaling to five GPU nodes.}
\label{tab:live}
\begin{tabularx}{\textwidth}{Xr}
\toprule
Observation & Result \\
\midrule
AKS GPU pool provisioning & Running / Succeeded \\
T4 GPU nodes & 5/5 Ready \\
Allocatable GPUs & 5 \\
Model-host pods & 5/5 Ready \\
Fabric model custom resources & 5 Available \\
Desired and ready model replicas & 5 / 5 \\
Control-plane readiness & HTTP 200 \\
Inference readiness & HTTP 200 \\
Authenticated model listing & All five aliases returned \\
Authenticated chat completion & HTTP 200 for every alias \\
Public TLS endpoints & Valid certificates at audit time \\
\bottomrule
\end{tabularx}
\end{table}

The scale operation proceeded in safety order: first change central desired replicas from seven to five while preserving all aliases; next verify five model pods; then reduce the AKS pool from eight to five; finally wait for node and pod readiness and invoke every model. Some pods moved to surviving nodes and required several minutes to become Ready. Public control and inference readiness remained 200.

\subsection{Historical operational measurements}

Table~\ref{tab:historical} records earlier project measurements. They belong to older topology and software snapshots and are not silently transferred to the current fleet.

\begin{table}[H]
\centering
\caption{Repository-recorded operational observations.}
\label{tab:historical}
\begin{tabularx}{\textwidth}{p{0.33\textwidth}X}
\toprule
Measurement & Recorded result and scope \\
\midrule
Node-local weight loading & 4.25 GiB in approximately 3.3 s on the measured T4 host \\
Cold start to healthy & Approximately 510 s; graph compilation dominated the path \\
Engine initialization example & Approximately 344 s under a documented eager configuration \\
Single-stream decode & Approximately 16 ms/token, or 63 tokens/s, in an earlier pilot \\
p95 TTFT & Approximately 1.6 s in that pilot workload \\
Concurrency admission & Of 30 concurrent requests, 5 served and 25 received 429 with \code{Retry-After} \\
Usage attribution & Model token counts attributed by deployment and stamp \\
Pre-scale monitoring audit & 71 targets up and zero down before the five-node reduction \\
\bottomrule
\end{tabularx}
\end{table}

These observations establish functionality and rough operating behavior, not a universal SLO. Prompt length, output length, model alias, warm state, and offered load affect every latency and throughput figure.

\subsection{Security and failure evidence}

Repository tests and deployment records cover missing credentials, invalid signatures, wrong audience, unknown model ownership, JWKS refresh behavior, RLS isolation, credential-class separation, spool replay/deduplication, backend connect failover, stream pinning, and stale-generation rejection. Live testing confirmed that control and inference tokens are not interchangeable and that model discovery is account-scoped. The deployed PostgreSQL application role was recorded as non-superuser and without \code{BYPASSRLS}.

The audit also identified periodic isolated Kubernetes 401 responses in agent and operator logs, approximately every 48--49 minutes. This cadence matches projected service-account token rotation: the custom client cached the token, refreshed only after receiving 401, and did not retry that first failed request. Reconciliation resumed on the next 15-second pass; no model pod restarted, no Kubernetes warning event appeared, all model replicas remained Available, and inference readiness stayed 200. This is a low-severity control-loop delay, not an inference outage, but the client should read the projected token per request or retry once after refresh. Kubernetes projected credentials are intentionally short-lived and proactively rotated \cite{k8stoken}; clients must not treat the mounted file as immutable.

\subsection{Kernel experiment: a deliberately bounded result}

The repository contains a Triton implementation of a packed gated-delta decode operation and an older immutable development artifact. Triton provides a tiled GPU programming and compilation model suitable for custom neural primitives \cite{triton}. For one sequence on an RTX 4070 Laptop GPU, that artifact measured vLLM 0.11.0's unpacked \code{fused\_recurrent\_gated\_delta\_rule} baseline at 19.041 $\mu$s and the corresponding Fabric implementation at 16.032 $\mu$s (a 1.188$\times$ baseline-to-Fabric ratio), followed by approximate parity at batches 16 and 32. This older operation is distinct from the newer packed decode operation available in the deployed model-host image; the audited fleet explicitly selects the stock implementation through \code{kernel\_mode=standard}. The artifact recorded close but not bit-identical long-run state behavior and explicitly marked itself unsuitable as production evidence.

A later T4 result in prose reported a larger isolated batch-one advantage, but no immutable T4 artifact or full-model A/B run is committed. More importantly, the current model fleet runs \code{kernel\_mode=standard}. The recurrence is only a small component of full token time, and memory traffic dominates at larger batches. We therefore make \emph{no claim that Fabric accelerates end-to-end generation}. This negative boundary is important: IO-aware optimization can be effective only when it reduces traffic on the relevant path, as highlighted by FlashAttention's analysis of memory hierarchy \cite{flashattention}. An isolated kernel speedup is insufficient evidence for customer throughput or tail-latency improvement.

\section{Failure Semantics}

Table~\ref{tab:failure} summarizes implemented behavior. The policy is neither ``always available'' nor ``fail closed everywhere.'' It preserves already accepted serving state when doing so does not expand authority, and refuses new work when safety or accounting cannot be established.

\begin{longtable}{p{0.25\textwidth}p{0.33\textwidth}p{0.33\textwidth}}
\caption{Failure semantics.}\label{tab:failure}\\
\toprule
Failure & Immediate behavior & Safety rationale \\
\midrule
\endfirsthead
\toprule
Failure & Immediate behavior & Safety rationale \\
\midrule
\endhead
Control plane/database unavailable & Existing routes and unexpired known-key tokens continue locally; new control operations may fail & Inference does not require synchronous central access \\
JWKS refresh failure & Keep last-known-good keys; refuse unknown key ID & Avoid both mass outage and trust expansion \\
Malformed route update & Retain last good local configuration & Empty/corrupt state must not erase accepted service \\
Backend connect failure & Eject temporarily and select another eligible host & Connection not established, so replay is safe \\
Failure after write/read & Do not replay & Generation may already have consumed compute or emitted output \\
Mid-stream failure & Emit stream error and clean up; never switch backend & Prevent duplicate or contradictory continuation \\
Usage spool unhealthy & Readiness fails and new GPU work is blocked & Do not perform work that cannot be durably attributed \\
Limit coordinator unavailable & Reject new work with 503 & Do not silently violate account limits \\
Agent transient failure & Retry; existing workloads remain & Desired-state lag is safer than destructive reset \\
Operator observation stale & Do not report newer generation Ready & Separate intent from evidence \\
Candidate fails readiness & Remove candidate; keep active route & Failed rollout must not destroy working release \\
GPU node removed & Kubernetes reschedules; model unavailable until Ready & Current one-replica fleet has no per-model redundancy \\
\bottomrule
\end{longtable}

Pod disruption budgets can constrain voluntary evictions but cannot prevent every disruption or a direct workload deletion \cite{k8spdb}. In the current topology, model hosts lack replica redundancy, so maintenance should be serialized and verified model by model.

\section{Cost and Capacity Trade-offs}

The original eight-T4 deployment was estimated in project documentation at approximately \$7.63/hour, using \$0.827/hour per T4 VM and roughly \$1.01/hour for the remaining platform. Applying the same assumptions to five GPU nodes yields

\begin{equation}
 C_{5} \approx 5(0.827) + 1.01 = \$5.145/\mathrm{hour},
\end{equation}

or approximately \$3,756 per 730-hour month. This is an arithmetic estimate, not an Azure invoice; discounts, taxes, network egress, storage growth, and plan changes are excluded. The reduction from eight to five nodes saves roughly \$1,811 per month under those assumptions.

The saving has an operational cost. At eight nodes with seven replicas, one node could stage a one-replica candidate. At five nodes with five model varieties, every GPU is occupied. The current pilot optimizes variety and standing cost, not redundancy or rollout speed. A practical operating policy is to add one temporary node before a release change, wait for it to become Ready, run the acknowledged candidate rollout, then remove it. Because GPU nodes use ephemeral local storage, a newly created node must pull the large model-host image and model weights and rebuild compilation caches. Earlier measurements suggest approximately 15--30 minutes for full fleet recovery after scaling from zero, with a conservative 45-minute window under network or registry variance.

Efficiency should eventually be measured as accepted output tokens per GPU-second, joules per accepted output token, and cost per million accepted output tokens under explicit TTFT/TPOT constraints. Hourly cost alone rewards turning off useful capacity and says nothing about delivered service.

\section{Related Work}

\paragraph{Engine scheduling and memory.}
Orca's iteration-level scheduling and selective batching established a core execution pattern for autoregressive models \cite{orca}. vLLM and PagedAttention address dynamic KV-cache allocation and sharing \cite{vllm}. SGLang adds prefix reuse and structured-program execution \cite{sglang}. Sarathi-Serve studies chunked prefill to improve the throughput--latency trade-off \cite{sarathi}. \system delegates engine scheduling to vLLM and concentrates on multi-tenant product and cluster control around it.

\paragraph{SLO-oriented serving.}
Clockwork builds predictable DNN serving from controlled execution and scheduling \cite{clockwork}. DistServe optimizes LLM goodput under TTFT/TPOT constraints through prefill/decode disaggregation \cite{distserve}. \system's present admission is simpler and stamp-local; its contribution is the identity, reconciliation, and failure boundary. SLO-aware cross-stamp placement and prefill/decode disaggregation remain future work.

\paragraph{Kubernetes model serving.}
KServe applies Kubernetes control/data-plane patterns and custom resources to model serving, including autoscaling integrations \cite{kserve}. \system is narrower and LLM-specific: account-bound token exchange, outbound stamp enrollment, local verification, usage durability, concrete model-host endpoint routing, and acknowledged stream drain. It does not yet match KServe's breadth or autoscaling maturity.

\paragraph{Cloud-native security and operations.}
The Kubernetes operator/controller pattern motivates declarative convergence \cite{k8soperator,k8scontroller}. PostgreSQL RLS provides database-level row filtering \cite{postgresrls}; JWT RFCs define the token format and deployment cautions \cite{jwt,jwtbcp}; NIST zero trust motivates per-resource checks independent of network location \cite{nistzt}; Prometheus and DCGM provide layered operational evidence \cite{promdata,dcgm}. \system combines these established mechanisms rather than proposing new cryptography or monitoring theory.

\section{Limitations and Threats to Validity}

\subsection{Pilot limitations}

The current five-node deployment has one replica per model, no GPU autoscaling, no scale-to-zero controller, and no spare rollout GPU. PostgreSQL is a Burstable single server without HA or geo-redundant backup. The AKS API has no IP allowlist, local cluster accounts remain enabled, and ACR, PostgreSQL, and Key Vault retain public network access. Key Vault purge protection and ACR image retention/soft-delete are disabled. These choices may be acceptable for a controlled pilot but should not be presented as an unrestricted production baseline.

The gateway/stamp package is singleton-oriented. Shared limits operate within a stamp, not globally. Cross-stamp failover, multi-zone gateway availability, disaster-recovery RPO/RTO tests, and automatic credential overlap rotation are absent. Egress is intentionally unrestricted because the agent and collector reach the control plane and model hosts may fetch weights. Mandatory east--west mTLS is not enforced.

\subsection{Measurement validity}

Historical and current results span different fleet sizes and software snapshots. The two-node performance observations, eight-node validation, and five-node post-scale checks are therefore not one experiment. Current post-scale testing established readiness and a successful small completion for each alias, not sustained throughput, fairness, or p99 latency. Prometheus's pre-scale target count should not be interpreted as the post-scale count.

Request-level observations within one host window are not independent fleet replicates. Future comparative experiments should use randomized node-paired crossover, fixed image/model digests, warm-state classification, repeated windows, raw request events, and confidence intervals. Model quality must be held constant before claiming cost or throughput improvement.

\subsection{Usage and API boundaries}

Usage relies on model-reported token counts and bounded at-least-once delivery. It lacks ledger integrity, indefinite retention, and complete export guarantees. OpenAI compatibility covers selected endpoints and common response forms rather than the entire API. Unknown forwarded fields depend on the model host. Audio routes exist without a live compatible model. Tool calling and structured output depend on the deployed model/template and are not gateway guarantees.

\subsection{Kernel boundary}

The current standard-kernel deployment is correct but does not validate Fabric's packed-kernel promotion safety. A paper-quality kernel result requires exact-original fallback, no partial recurrent-state mutation, full-generation logits/token agreement, immutable T4 artifacts, active decode-batch distributions, and full-model A/B measurement. Until then, kernel work is a research direction rather than a platform result.

\section{Future Work}

\subsection{Immediate correctness work}

Two small fixes have disproportionate value. First, the Kubernetes client should load its projected service-account token for each request or retry once after refreshing on 401; tests should rotate a temporary token file and cover body-bearing requests. Second, the operator should reconcile same-release replica changes. The regression test should update a CR from three replicas to one, observe the Kubernetes Deployment reach one, and verify route/status counts.

\subsection{Pilot hardening}

Next hardening steps are digest-pinned model-host promotion, automated backup/restore drills, optional PostgreSQL HA, private endpoints or firewall restriction, AKS local-account disablement, Key Vault purge protection, ACR retention, and tested certificate/credential rotation. These should be driven by pilot risk rather than a generic checklist.

\subsection{Architecture-aware capacity}

The model fleet mixes hybrid recurrent and dense attention architectures. Their memory envelopes differ: dense models grow KV state with context at every attention layer, while hybrid models combine context-dependent KV layers with fixed recurrent state on other layers. Future placement should therefore use a validated model/runtime/hardware profile rather than GPU count alone. Candidate profiles can vary context, maximum sequences, graph capture, cache policy, and kernel path offline, then promote only evidence-backed profiles. This direction is called the Architecture-Aware Memory Envelope Controller in the project research plan; it is not implemented or evaluated today.

\subsection{SLO-qualified autoscaling and rollout}

GPU scaling should use queueing, KV pressure, and SLO-qualified goodput rather than CPU utilization. KServe documents event-driven autoscaling from inference metrics as one established direction \cite{kserveauto}. For \system, cold-start state and rollout spare capacity must be first-class: removing a node deletes ephemeral cache, while adding capacity may take many minutes before a model is Ready. A controller should distinguish warm replicas, cold candidate nodes, and capacity reserved for safe rollout.

\subsection{Reproducible performance research}

Future experiments should export immutable artifacts containing source SHA, image digest, model/tokenizer revision, manifest hash, GPU UUID, driver/CUDA/vLLM/Triton versions, request trace, raw TTFT/TPOT events, scheduler batch distributions, DCGM power windows, and exact analysis queries. A conservative kernel selector should default to stock outside measured cells and open a circuit breaker after repeated alternative-kernel failure. Negative results should be retained to prevent unsafe rediscovery.

\section{Conclusion}

\system demonstrates that a small LLM serving pilot can be engineered as more than a model server behind an ingress. Its central control plane owns accounts and intent; stamps preserve accepted serving locally; separate agent, operator, gateway, and collector processes limit combined authority; short-lived audience-specific tokens and account-bound routes permit local authorization; forced PostgreSQL RLS adds persistence-layer isolation; generation and route revisions make convergence inspectable; non-idempotent streams are never replayed after output; and usage is durably spooled before asynchronous export.

The current five-T4 deployment serves five model varieties, one per node. Direct audit found all nodes and model hosts Ready, all model resources Available, public readiness healthy, and one successful authenticated request per model. This supports a controlled pilot. It does not establish hyperscale reliability, billing-grade accounting, global limits, autoscaling, or an end-to-end kernel speedup. The most useful result is therefore the explicit boundary: the system identifies which state and evidence are local, central, desired, observed, durable, or merely proposed. That discipline makes later optimization and hardening measurable rather than aspirational.

\appendix
\section{Public API Surface}

\begin{table}[H]
\centering
\caption{Selected customer-facing endpoints.}
\begin{tabularx}{\textwidth}{p{0.29\textwidth}p{0.12\textwidth}X}
\toprule
Endpoint & Auth & Purpose \\
\midrule
\code{POST /v1/token} & API key/OIDC assertion & Exchange long-lived identity for short-lived control or inference JWT \\
\code{GET /v1/models} & Inference JWT & List account-owned model aliases \\
\code{POST /v1/chat/completions} & Inference JWT & Chat JSON or SSE streaming \\
\code{POST /v1/completions} & Inference JWT & Legacy text completion JSON or SSE \\
\code{POST /v1/audio/transcriptions} & Inference JWT & Multipart audio proxy; non-streaming \\
\code{POST /v1/audio/translations} & Inference JWT & Multipart audio translation proxy; non-streaming \\
\code{GET /v1/accounts/.../deployments} & Control JWT & Account-scoped deployment intent \\
\code{GET /v1/accounts/\{account\_id\}/deployments/\{deployment\_id\}/usage} & Control JWT & Operational usage aggregate for one deployment \\
\code{GET /healthz} & None & Process liveness \\
\code{GET /readyz} & None & Dependency-aware readiness \\
\bottomrule
\end{tabularx}
\end{table}

A minimal token exchange and model-listing sequence is:

\begin{lstlisting}[language=bash,caption={Token exchange and account-scoped model discovery.}]
# The shared helper removes FABRIC_API_KEY from child environments, feeds the
# exchange body through stdin, and gives curl the bearer header through a file
# descriptor rather than a command-line argument.
source examples/curl/_common.sh
ITOK="$(fabric_token fabric-inference)"

fabric_curl "$ITOK" --fail-with-body --silent --show-error \
  "$FABRIC_INFERENCE_URL/v1/models" | jq '.data[].id'
\end{lstlisting}

The raw \code{FABRIC\_API\_KEY} must not be sent to the inference endpoint. Long-running clients must exchange a new token before expiry and rebuild or update the SDK client.

\section{Reproducibility Checklist}

A future experimental report should include at minimum:

\begin{enumerate}[leftmargin=*]
  \item Git commit, dirty-state flag, container image digest, Helm values hash, CRD version, and deployment generation.
  \item Exact model and tokenizer revisions; public alias must not substitute for immutable identity.
  \item GPU product, UUID, node, count, memory, compute capability, driver, CUDA, engine, Torch, and Triton versions.
  \item Prompt/output token distributions, concurrency or arrival process, streaming mode, random seeds, warm/cold classification, and run duration.
  \item Raw request timestamps for arrival, admission, upstream connect, first token, subsequent sampled tokens, completion/cancel, and status.
  \item Engine queue/running counts, active decode batch, KV occupancy, preemption, graph/eager path, and model-reported usage.
  \item DCGM power, memory, utilization, clocks, temperature, and throttling over synchronized windows.
  \item All refusals and failures, not only successful latency; report accepted-output goodput under a stated SLO.
  \item Independent run windows and confidence intervals; do not treat requests within one window as independent machine replicates.
  \item Artifact content hash and commands sufficient to recompute tables and figures.
\end{enumerate}

\section{Operational Verification Commands}

The following read-only checks reproduce the main health assertions after Azure authentication and AKS credential setup:

\begin{lstlisting}[language=bash,caption={Read-only cluster and model verification.}]
az aks nodepool show \
  --resource-group rg-inference \
  --cluster-name aks-inference-cin-01 \
  --name gpupool \
  --query '{count:count,power:powerState.code,state:provisioningState}'

kubectl get nodes -l agentpool=gpupool \
  -o custom-columns='NAME:.metadata.name,READY:.status.conditions[?(@.type=="Ready")].status,GPU:.status.allocatable.nvidia\.com/gpu'

kubectl get fabricmodeldeployments -n fabric-stamp -o json | jq '{
  models:(.items|length),
  desired:([.items[].spec.replicas]|add),
  ready:([.items[].status.readyReplicas]|add),
  aliases:[.items[].spec.modelAlias]
}'

kubectl get pods -n fabric-stamp \
  -l app.kubernetes.io/name=fabric-model-host -o wide

curl -fsS -o /dev/null -w '%{http_code}\n' \
  https://fabric-cp.hexelstudio.com/readyz
curl -fsS -o /dev/null -w '%{http_code}\n' \
  https://inference.hexelstudio.com/readyz
\end{lstlisting}

Do not print Key Vault secret values, API keys, or access tokens into experiment logs.

\begin{thebibliography}{99}

\bibitem{vllm}
W. Kwon et al., ``Efficient Memory Management for Large Language Model Serving with PagedAttention,'' in \emph{Proceedings of ACM SOSP}, 2023. DOI: \href{https://doi.org/10.1145/3600006.3613165}{10.1145/3600006.3613165}.

\bibitem{orca}
G.-I. Yu et al., ``Orca: A Distributed Serving System for Transformer-Based Generative Models,'' in \emph{Proceedings of USENIX OSDI}, 2022. \url{https://www.usenix.org/conference/osdi22/presentation/yu}.

\bibitem{clockwork}
A. Gujarati et al., ``Serving DNNs like Clockwork: Performance Predictability from the Bottom Up,'' in \emph{Proceedings of USENIX OSDI}, 2020. \url{https://www.usenix.org/conference/osdi20/presentation/gujarati}.

\bibitem{distserve}
Y. Zhong et al., ``DistServe: Disaggregating Prefill and Decoding for Goodput-optimized Large Language Model Serving,'' in \emph{Proceedings of USENIX OSDI}, 2024. \url{https://arxiv.org/abs/2401.09670}.

\bibitem{sglang}
L. Zheng et al., ``SGLang: Efficient Execution of Structured Language Model Programs,'' arXiv:2312.07104, 2023. \url{https://arxiv.org/abs/2312.07104}.

\bibitem{sarathi}
A. Agrawal et al., ``Taming Throughput-Latency Tradeoff in LLM Inference with Sarathi-Serve,'' arXiv:2403.02310, 2024. \url{https://arxiv.org/abs/2403.02310}.

\bibitem{triton}
P. Tillet, H.-T. Kung, and D. Cox, ``Triton: An Intermediate Language and Compiler for Tiled Neural Network Computations,'' in \emph{Proceedings of ACM MAPL}, 2019. DOI: \href{https://doi.org/10.1145/3315508.3329973}{10.1145/3315508.3329973}.

\bibitem{flashattention}
T. Dao et al., ``FlashAttention: Fast and Memory-Efficient Exact Attention with IO-Awareness,'' in \emph{Advances in Neural Information Processing Systems}, 2022. \url{https://arxiv.org/abs/2205.14135}.

\bibitem{k8scontroller}
Kubernetes Authors, ``Controllers,'' Kubernetes Documentation. \url{https://kubernetes.io/docs/concepts/architecture/controller/}. Accessed September 2026.

\bibitem{k8soperator}
Kubernetes Authors, ``Operator Pattern,'' Kubernetes Documentation. \url{https://kubernetes.io/docs/concepts/extend-kubernetes/operator/}. Accessed September 2026.

\bibitem{k8stoken}
Kubernetes Authors, ``Projected Volumes,'' Kubernetes Documentation. \url{https://kubernetes.io/docs/concepts/storage/projected-volumes/}. Accessed September 2026.

\bibitem{k8spdb}
Kubernetes Authors, ``Disruptions and Pod Disruption Budgets,'' Kubernetes Documentation. \url{https://kubernetes.io/docs/concepts/workloads/pods/disruptions/}. Accessed September 2026.

\bibitem{jwt}
M. Jones, J. Bradley, and N. Sakimura, ``JSON Web Token (JWT),'' RFC 7519, IETF, 2015. \url{https://www.rfc-editor.org/rfc/rfc7519.html}.

\bibitem{jwtbcp}
Y. Sheffer, D. Hardt, and M. Jones, ``JSON Web Token Best Current Practices,'' RFC 8725, IETF, 2020. \url{https://www.rfc-editor.org/rfc/rfc8725.html}.

\bibitem{postgresrls}
PostgreSQL Global Development Group, ``Row Security Policies,'' PostgreSQL Documentation. \url{https://www.postgresql.org/docs/current/ddl-rowsecurity.html}. Accessed September 2026.

\bibitem{nistzt}
S. Rose, O. Borchert, S. Mitchell, and S. Connelly, ``Zero Trust Architecture,'' NIST SP 800-207, 2020. DOI: \href{https://doi.org/10.6028/NIST.SP.800-207}{10.6028/NIST.SP.800-207}.

\bibitem{openaistream}
OpenAI, ``Chat Completions Streaming Events,'' API Documentation. \url{https://developers.openai.com/api/reference/resources/chat/subresources/completions/streaming-events/}. Accessed September 2026.

\bibitem{promdata}
Prometheus Authors, ``Data Model,'' Prometheus Documentation. \url{https://prometheus.io/docs/concepts/data_model/}. Accessed September 2026.

\bibitem{promhist}
Prometheus Authors, ``Histograms and Summaries,'' Prometheus Documentation. \url{https://prometheus.io/docs/practices/histograms/}. Accessed September 2026.

\bibitem{dcgm}
NVIDIA, ``DCGM Exporter,'' NVIDIA GPU Telemetry Documentation. \url{https://docs.nvidia.com/datacenter/cloud-native/gpu-telemetry/latest/dcgm-exporter.html}. Accessed September 2026.

\bibitem{istio}
Istio Authors, ``Secure Gateways,'' Istio Documentation. \url{https://istio.io/latest/docs/tasks/traffic-management/ingress/secure-ingress/}. Accessed September 2026.

\bibitem{cilium}
Cilium Authors, ``Network Policy,'' Cilium Documentation. \url{https://docs.cilium.io/en/stable/network/kubernetes/policy/}. Accessed September 2026.

\bibitem{kserve}
KServe Authors, ``System Architecture Overview,'' KServe Documentation. \url{https://kserve.github.io/website/docs/concepts/architecture}. Accessed September 2026.

\bibitem{kserveauto}
KServe Authors, ``Generative Inference Autoscaling,'' KServe Documentation. \url{https://kserve.github.io/website/docs/model-serving/generative-inference/autoscaling}. Accessed September 2026.

\bibitem{azurewi}
Microsoft, ``Microsoft Entra Workload ID on Azure Kubernetes Service,'' Azure Documentation. \url{https://learn.microsoft.com/azure/aks/workload-identity-overview}. Accessed September 2026.

\bibitem{azurepgha}
Microsoft, ``High Availability in Azure Database for PostgreSQL Flexible Server,'' Azure Documentation. \url{https://learn.microsoft.com/azure/postgresql/high-availability/concepts-high-availability}. Accessed September 2026.

\bibitem{azurepgbackup}
Microsoft, ``Backup and Restore in Azure Database for PostgreSQL Flexible Server,'' Azure Documentation. \url{https://learn.microsoft.com/azure/postgresql/flexible-server/concepts-backup-restore}. Accessed September 2026.

\bibitem{fabricrepo}
Hexel Studio, ``Fabric Source Repository and Technical Records,'' commit \code{6a5bfdf}, 2026. \url{https://github.com/khushwant04/fabric}.

\end{thebibliography}

\end{document}
```
