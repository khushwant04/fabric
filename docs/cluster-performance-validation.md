# Cluster connection and inference performance validation

Fabric can connect an existing Kubernetes GPU cluster and serve deployments through an
OpenAI-compatible endpoint. The endpoint benchmark below inventories reachable model aliases
and measures client-observed behavior without changing deployment specs, nodes, or rollouts.
It needs Python 3.10 or newer and uses only the standard library.

## What connects automatically

Install [`fabric-stamp`](../deploy/helm/fabric-stamp/) once per cluster or **disjoint GPU pool**,
with an account-bound single-use enrollment token and the control-plane URL. The agent enrolls
on startup, persists its stamp identity on a PVC, and synchronizes outbound. Creating a model
deployment centrally records intent; a separate placement makes it appear in that stamp's
desired state. Registering a model or applying a local CR does not enroll a cluster or import
the model into the central inventory. Use the [README bootstrap workflow](../README.md#bootstrap-a-kubernetes-stamp)
for the complete installation commands.

| Operation | Current behavior |
|---|---|
| Connect customer-owned Kubernetes, k3s, EKS, or GKE | BYOI enrollment and ownership-checked placement; generic Kubernetes distributions are not rejected by an orchestrator allowlist. |
| Connect shared Fabric-owned managed capacity | Enroll under the protected system account; managed placement currently accepts AKS or k3s and requires the customer's managed-capacity entitlement. |
| Add a GPU worker to an enrolled pool | Join it through the cluster's own node workflow and provide the driver/device plugin. The existing agent discovers selected nodes advertising `nvidia.com/gpu` at its next capacity refresh, normally one minute. No per-node Fabric enrollment. |
| Add GPUs to a pool | Capacity increases; declared replicas stay fixed until explicitly updated. There is no GPU autoscaler or automatic node provisioning. |
| Use an existing OpenAI-compatible host | Configure `modelHost.url`; Fabric does not discover/import arbitrary existing Pods, Services, or model endpoints. The fallback path uses one configured upstream URL for the stamp. |
| Let Fabric run model hosts | Enable the operator and supply its model-host image/configuration. It creates workloads for placed deployments. |
| Revoke a stamp | Stops synchronization and machine credentials. Running BYOI workloads remain under cluster control; revocation is not an evacuation workflow. |
| Move between clusters | Multiple placements are representable, but there is no migration/drain API with transparent cross-stamp endpoint failover. |

The stamp's agent/gateway/collector pod can run on a CPU node. Kubernetes schedules model-host
replicas onto the selected GPU nodes; the control plane sees aggregate pool capacity rather
than individual node identities. Match `gpu.nodeSelector` to the intended pool and use disjoint
selectors for separate stamps sharing one cluster. Overlapping stamps would advertise the
same hardware as independent capacity. Configure tolerations/runtime class for the host as
needed, and keep the agent's identity volume across restarts.

Capacity is refreshed dynamically. The updated operator also refreshes hardware profiles
once per minute and retries empty/failed reads at a bounded 15-second interval. A joining
weaker GPU can change dtype and memory headroom, so keep a homogeneous pool or isolate
different hardware with selectors. Verify resulting settings before placing models on changed
hardware. Earlier operator images profiled once per process, including freezing empty/failed
first reads; updating the image is necessary to receive the refresh fix.
The current node reader excludes nodes without `Ready=True` and cordoned nodes before
counting allocatable GPUs. Match host tolerations and runtime configuration to the pool;
these checks do not prove a requested Pod can tolerate every taint or fit CPU/RAM.

Code references: [`Ensure`](../agent/internal/agent/agent.go),
[`placement authorization`](../control-plane/app/services/deployments.py),
[`managed orchestrator allowlist`](../control-plane/app/core/platform.py),
[`capacity measurement`](../agent/internal/hardware/capacity.go),
[`startup hardware profile`](../agent/internal/operator/operator.go).

## Inventory before generating load

Set credentials through your shell or secret manager; do not put credentials in command
arguments, metadata files, or URLs. With `FABRIC_API_KEY`, the script exchanges for audience
`fabric-inference` and renews before expiry. The raw key goes only to the control plane.
Alternatively set `FABRIC_INFERENCE_TOKEN` instead of the API key; that token cannot be
renewed by this script. Endpoint redirects are refused to avoid forwarding credentials.

```bash
export FABRIC_CONTROL_URL=https://control.example.com
export FABRIC_INFERENCE_URL=https://inference.example.com
# Supply FABRIC_API_KEY securely, or FABRIC_INFERENCE_TOKEN instead.
python3 deploy/scripts/benchmark-inference.py inventory \
  --output-dir /tmp/fabric-inference-inventory
```

Both `https://inference.example.com` and `https://inference.example.com/v1` are valid inference
base URLs. Plain HTTP requires `--allow-http`, intended for loopback or in-cluster endpoints.
TLS verification remains enabled for HTTPS. The inventory is the authenticated endpoint's
model list, not a claim that every alias is healthy or that it uses a particular GPU/kernel.

For a read-only cluster snapshot, provide **both an explicit context and namespace**. The
script never selects the current context implicitly and never executes cluster writes.

```bash
python3 deploy/scripts/benchmark-inference.py inventory \
  --context my-k3s-context --namespace fabric-stamp \
  --snapshot-gpu-selector accelerator=nvidia \
  --output-dir /tmp/fabric-inference-inventory
```

The snapshot records allowlisted node hardware/Ready information, Pod image IDs and safe
serving flags, and CR model/settings/status, including replica availability and conditions.
Check Pod readiness and the CR `Available` condition. Earlier operator images could report
`phase: ready` with zero ready replicas during recovery; the updated operator reports an
applied but unavailable release as `degraded`. See [startup diagnosis](model-startup-diagnosis.md)
for the observed stages and explicit Qwen text-only policy.
The snapshot omits environment variables, Secrets,
annotations, raw manifests, and arbitrary server arguments. Namespace Pods and CRs may be
readable even if cluster-wide nodes are not; unavailable reads are marked explicitly. This
snapshot supports configuration review; it does not automatically associate every endpoint
alias with a Pod or verify an evidence claim.

## Run an explicit workload matrix

Start with one model, low concurrency, and bounded output. This is live inference traffic
and consumes GPU time, tokens, and account quota. Run it on an endpoint you are authorized
to exercise. Requests are closed-loop: each worker sends its next request after the previous
one finishes; this is not a fixed requests-per-second load generator.

```bash
python3 deploy/scripts/benchmark-inference.py benchmark \
  --models qwen3.5-0.8b \
  --prompt-words 32,256 --output-tokens 32,128 \
  --concurrency 1,4 --requests 8 --warmup 1 --modes both \
  --output-dir /tmp/fabric-inference-benchmarks
```

This example runs 16 matrix cells, each with one serial warmup and eight measured requests.
`--models all` explicitly selects all listed aliases except `auto`; that can multiply load
substantially. Keep concrete model aliases for reproducible comparisons. The default prompts
are synthetic numbered-list continuations, and `--prompt-words` counts words rather than
tokenizer tokens. `max_tokens` is an upper bound; a model can finish early, so compare actual
reported token counts. Warmup is excluded from measurements and does not prove a compile or
prefix cache was warm.

Use `--prompt-file /path/prompts.json` for a JSON array of application-representative prompt
strings. Only their SHA-256, word count, and UTF-8 length are stored by default; responses are
never saved. `--persist-prompts` is explicit permission to retain input text. Hashes of known
or guessable text are not anonymization. Use public/synthetic prompts when sharing artifacts.

`--settings-file` accepts a JSON object with `temperature`, `top_p`, `top_k`, `min_p`, `seed`,
`presence_penalty`, `frequency_penalty`, `repetition_penalty`, `ignore_eos`, and
`chat_template_kwargs` containing only `enable_thinking`. The default is `temperature: 0`.
For a Qwen release supporting the thinking switch, for example:

```json
{"temperature": 0, "chat_template_kwargs": {"enable_thinking": false}}
```

The script requests `stream_options.include_usage` for streams. An upstream that does not
provide terminal usage is recorded as missing usage; no token counts are invented. There is
one credential-renewal retry for a rejected 401. Accepted inference work, 429s, and transport
failures are not automatically retried, preserving the observed admission/error behavior.

To exercise intentional client disconnects separately:

```bash
python3 deploy/scripts/benchmark-inference.py benchmark \
  --models qwen3.5-0.8b --prompt-words 32 --output-tokens 128 \
  --concurrency 2 --requests 6 --warmup 0 --modes stream \
  --cancel-every 3 --cancel-after-content 3 \
  --output-dir /tmp/fabric-inference-cancellations
```

Cancellation closes the client connection after the specified count of nonempty content
events. It does not prove the upstream stopped generating immediately. Such requests commonly
have no terminal usage and are kept distinct from successful completions and incomplete
streams. Do not use a cancellation run to claim steady-state generation throughput.

## Interpret the artifact accurately

Each invocation writes a unique UTC-timestamped JSON file with exclusive creation and mode
`0444` after completion. Existing artifacts are never overwritten. File permissions are not
WORM/integrity protection; use immutable object storage or a signed manifest when publishing
evidence. Artifacts include the benchmark script hash, exact workload settings, endpoint,
authentication mode without credential values, per-request outcomes, warmup outcomes,
summaries, and optional before/after cluster snapshots.

| Field | Meaning and boundary |
|---|---|
| `client_duration_s` | Client request start through parsed response/end of stream, including connection setup, gateway, network, and any token renewal. |
| `client_time_to_first_content_s` | Time to the first nonempty SSE `delta.content`. Empty role events and reasoning-only deltas do not count. This is client-observed first content, not instrumented engine TTFT. |
| `client_inter_content_interval_s` | Intervals between nonempty content events. Events can bundle multiple tokens or be buffered; these values are **not true TPOT**. |
| `reported_completed_output_tokens_per_wall_s` | Sum of model-reported completion tokens for completed requests divided by cell wall time. Missing usage makes this a partial observed count. Failed/cancelled work may still have consumed GPU time. |
| `http_429` / request `http_status` | Admission refusals and HTTP errors, including a numeric `Retry-After` when present. |
| `incomplete_stream` | Stream reached EOF without `[DONE]`; kept separate even if usage or a finish reason arrived. |
| `cancelled` | The benchmark deliberately closed the stream. |
| `configuration_status` | `unverified` by default; caller-supplied complete metadata is still not independently verified by the script. |

Summaries use nearest-rank p50/p95 and include only successful requests for latency. A handful
of requests is a smoke test, not a reliable tail-latency estimate. Record refusals alongside
successful latency; do not hide admission limits by reporting only accepted requests. The
client creates a fresh HTTP/1.1 connection per request, so TLS overhead and client capacity
affect results. Engine TTFT/TPOT, GPU saturation, cache/preemption, and power should come from
the relevant vLLM, gateway, and DCGM Prometheus series with matching time ranges and labels.

The script exits 0 after an inventory or successful run (including requested cancellations),
1 when measured requests contain errors/incomplete streams, and 2 for setup/protocol failures.
A warmup failure is retained separately and must be reviewed. A Ctrl-C can end a run before
its final artifact is written; keep matrix sizes bounded.

## Attach evidence before making performance claims

Unverified smoke measurements are useful and clearly labelled. For a claim-ready collection,
provide `--metadata-file` and `--performance-claims`; the latter requires target, hardware,
profile, model revision, image digest, kernel selection, exact serving configuration, and
evidence references. Example structure, whose values must be replaced with observed facts:

```json
{
  "target": "my-k3s-gpu-pool",
  "hardware": {
    "gpu_model": "Tesla T4",
    "gpu_count": 1,
    "driver_version": "REPLACE_WITH_OBSERVED_VERSION",
    "source": "REPLACE_WITH_NODE_OR_DEVICE_EVIDENCE"
  },
  "profile": "REPLACE_WITH_PROFILE_AND_REVISION",
  "models": {
    "qwen3.5-0.8b": {
      "model_revision": "REPLACE_WITH_IMMUTABLE_MODEL_REVISION",
      "image_digest": "REPLACE_WITH_ACTUAL_IMAGE_AT_SHA256_DIGEST",
      "kernel_mode": "standard",
      "serving_settings": {
        "dtype": "float16",
        "tensor_parallel_size": 1,
        "replicas": 1,
        "max_model_len": 4096,
        "max_num_seqs": 16,
        "gpu_memory_utilization": 0.85,
        "execution": "cuda_graph"
      }
    }
  },
  "evidence_refs": ["REPLACE_WITH_CONFIG_AND_DEVICE_ARTIFACT_IDS"]
}
```

The image field must contain a real `sha256:` digest of 64 hexadecimal characters, optionally
after an image name and `@`. `main`/`latest` model revisions and an ambiguous `auto` kernel
selection are refused in claim mode. Metadata is operator-authored evidence, not a trust
oracle: complete labels do not prove hardware, configuration, or performance. The operator's
declared `kernel_mode=fabric` is not proof the kernel ran; check runtime selection/fallback
evidence. Metadata must contain no credentials or private prompts/response content. Common
credential keys/values are rejected, but review arbitrary free-form metadata before sharing.

```bash
python3 deploy/scripts/benchmark-inference.py benchmark \
  --models qwen3.5-0.8b --prompt-words 32,256 --output-tokens 32,128 \
  --concurrency 1,4 --requests 40 --warmup 3 --modes both \
  --metadata-file /path/observed-performance-metadata.json --performance-claims \
  --context my-k3s-context --namespace fabric-stamp \
  --output-dir /tmp/fabric-inference-evidence
```

For stock-versus-Fabric comparison, use separately deployed copies of the **same exact model,
revision, image, hardware class, dtype, context/concurrency/memory settings, execution mode,
replica count, and routing policy**, differing only in explicit kernel selection. Keep enough
spare GPU capacity for both; do not mutate a live model into a comparison candidate. Run the
same prompt/settings matrix against concrete aliases, alternate run order, repeat independent
runs, and retain both positive and negative results. Account for shared-cluster interference,
cold starts and cache state. Kernel microbenchmark gains alone do not establish better
end-to-end inference performance.

## Optional Kubernetes benchmark Job

Use the same script inside a CPU-only Job if you want the client inside the cluster. Choose
an existing approved Python 3.10+ image and replace `YOUR_APPROVED_PYTHON_IMAGE@sha256:...`
with its actual digest; this guide does not prescribe a hypothetical benchmark image.
Create a ConfigMap from the script and supply credentials through an existing Secret.
These setup commands write only the benchmark resources, not model workloads:

```bash
kubectl --context my-k3s-context --namespace fabric-benchmark create configmap inference-benchmark-script \
  --from-file=benchmark-inference.py=deploy/scripts/benchmark-inference.py
# Create fabric-benchmark-auth through your secret manager; its api-key field holds FABRIC_API_KEY.
```

Adapt this template before applying it. Its deliberately small matrix is an endpoint smoke
measurement with unverified metadata. Supply an existing writable artifact PVC named
`fabric-benchmark-artifacts` in the benchmark namespace; inspect it through a separate reader
Pod after the Job exits, or use an approved artifact uploader. A terminated Job container
cannot be used with `kubectl cp`, so an `emptyDir` alone would strand its results. The Job has
no GPU request and no Kubernetes API access.

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: fabric-inference-benchmark
  namespace: fabric-benchmark
spec:
  backoffLimit: 0
  template:
    metadata:
      labels: {app: fabric-inference-benchmark}
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      securityContext: {runAsNonRoot: true, runAsUser: 65532, runAsGroup: 65532, fsGroup: 65532}
      containers:
        - name: benchmark
          image: YOUR_APPROVED_PYTHON_IMAGE@sha256:REPLACE_WITH_REAL_DIGEST
          command: [python3, /script/benchmark-inference.py]
          args:
            - benchmark
            - --models
            - qwen3.5-0.8b
            - --prompt-words
            - '32'
            - --output-tokens
            - '32'
            - --concurrency
            - '1,2'
            - --requests
            - '4'
            - --warmup
            - '1'
            - --output-dir
            - /artifacts
          env:
            - {name: FABRIC_CONTROL_URL, value: 'https://control.example.com'}
            - {name: FABRIC_INFERENCE_URL, value: 'https://inference.example.com'}
            - name: FABRIC_API_KEY
              valueFrom:
                secretKeyRef: {name: fabric-benchmark-auth, key: api-key}
          resources:
            requests: {cpu: '250m', memory: 128Mi}
            limits: {cpu: '2', memory: 512Mi}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: {drop: [ALL]}
          volumeMounts:
            - {name: script, mountPath: /script, readOnly: true}
            - {name: artifacts, mountPath: /artifacts}
      volumes:
        - name: script
          configMap: {name: inference-benchmark-script}
        - name: artifacts
          persistentVolumeClaim: {claimName: fabric-benchmark-artifacts}
```

Respect the stamp's existing NetworkPolicy: allow the benchmark namespace to reach the
inference listener, and permit benchmark egress to DNS and the control-plane/inference
destinations. Do not expose the admin/usage/router-state listeners to the Job. Policies for
external HTTPS hosts need the actual IP/CIDR or your cluster's supported DNS policy mechanism;
an invented universal policy would not reliably select those destinations. Do not disable
namespace isolation for the benchmark. The Job does not run optional kubectl snapshots;
collect them separately with explicitly authorized read permissions.

## Local verification

The benchmark's local fake HTTP/SSE server tests cover terminal usage, empty role events,
nonstream measurement boundaries, cancellation versus incomplete streams, 429s, coordinated
credential renewal, artifact privacy/exclusive creation, metadata gating, bounded SSE parsing,
and sanitized cluster snapshots:

```bash
python3 -m unittest discover -s deploy/scripts -p test_benchmark_inference.py -v
```

These protocol checks require loopback socket permission, but no cluster, cloud credentials,
GPU, real model, package installation, or rollout. Actual cluster/model performance remains
unmeasured until the workflow is run against an explicitly selected endpoint and target.
