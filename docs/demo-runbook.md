# Demo-night runbook: new features onto real stamps

Gets this branch's features (Messages API, agent runs, tool loop, cascade, hosted tools,
edge gateway, console Agents section and drawers) onto a control plane on AKS and GPU
stamps on AKS node pools, GCP VMs and other VMs. Work top to bottom; each step says how to
check it before moving on. Replace `REGISTRY`, hostnames and names with yours.

Fallback for the stage: `make demo-console` runs the whole console locally with a seeded
account, and every `make demo-*` target runs without a GPU. Start them once beforehand —
the first run downloads packages and builds the console.

## 0. Pick a tag

```bash
export REGISTRY=myregistry.azurecr.io/fabric
export TAG=demo-$(git rev-parse --short HEAD)
```

## 1. Build and push images (from the repository root, with Docker)

```bash
for c in control-plane data-plane agent console sandbox; do
  docker build -f deploy/images/$c.Dockerfile -t $REGISTRY/$c:$TAG . && docker push $REGISTRY/$c:$TAG
done
```

- `data-plane` serves both the stamp gateway and the edge gateway. It includes the E2B
  client by default; an air-gapped build passes `--build-arg EXTRAS=`.
- The model host is unchanged (`model-host.Dockerfile`, or your existing vLLM image). The
  RTX PRO 6000 (Blackwell) needs a vLLM image built for CUDA 12.8 or newer.

Check: every image is visible in the registry, and AKS and each VM can pull it
(`imagePullSecrets` where the registry is private).

## 2. Control plane, console and edge on AKS

Upgrade the control plane first; it runs migrations and adds the edge routing API.

```bash
helm upgrade cp deploy/helm/fabric-control-plane -n fabric-control --reuse-values \
  --set image.repository=$REGISTRY/control-plane --set image.tag=$TAG \
  --set console.image.repository=$REGISTRY/console --set console.image.tag=$TAG
kubectl -n fabric-control rollout status deploy/cp-fabric-control-plane
```

Check the value names against your installed release (`helm get values cp -n fabric-control`)
before running; the chart's `values.yaml` is the reference.

Mint the edge credential once and store it in a Secret. The key is printed once.

```bash
kubectl exec -n fabric-control deploy/cp-fabric-control-plane -- \
  python -m app.cli create-edge-credential --name fabric-edge
kubectl -n fabric-control create secret generic fabric-edge-credential \
  --from-literal=api-key='<printed key>'
```

Enable the edge with its own HTTPS ingress (streaming needs buffering off and long timeouts):

```bash
helm upgrade cp deploy/helm/fabric-control-plane -n fabric-control --reuse-values \
  --set edge.enabled=true \
  --set edge.image.repository=$REGISTRY/data-plane --set edge.image.tag=$TAG \
  --set edge.credential.existingSecret=fabric-edge-credential \
  --set edge.ingress.enabled=true --set edge.ingress.className=nginx \
  --set edge.ingress.host=inference.example.com \
  --set edge.ingress.tls.secretName=edge-tls
```

`edge-tls` is the certificate Secret for that host (cert-manager can create it); the chart
refuses to render without it while edge TLS is on.

Add the ingress annotations from the `edge:` section of `values.yaml` (buffering off, read
timeout ≥ 600 s) and TLS. With `console.inferenceUrl` unset, the console uses the edge URL.

Check: `curl https://inference.example.com/healthz` → ok;
`kubectl -n fabric-control logs deploy/cp-fabric-control-plane-edge` shows a routing-table
revision. Stamps appear in the table only once step 3 gives each an HTTPS URL.

## 3. Each stamp

Do this per stamp: `aks-gpu` (AKS node pools), `gcp-rtx6000` (GCP VM, k3s), `vm-gpu`.

### 3a. Already enrolled — upgrade in place

```bash
helm upgrade stamp deploy/helm/fabric-stamp -n fabric-stamp --reuse-values \
  --set image.agent.repository=$REGISTRY/agent --set image.agent.tag=$TAG \
  --set image.dataPlane.repository=$REGISTRY/data-plane --set image.dataPlane.tag=$TAG \
  --set exposure.enabled=true --set exposure.host=<stamp>.inference.example.com
kubectl -n fabric-stamp rollout status statefulset/stamp-fabric-stamp
```

The agent keeps its identity on its volume; no new enrollment token is needed.

### 3b. New VM — bootstrap and enroll

On the VM (Ubuntu 22.04/24.04, NVIDIA driver installed, `nvidia-smi` works):

```bash
sudo bash scripts/bootstrap-k3s.sh --nvidia --node-name gcp-rtx6000
```

Then create a single-use enrollment token (console → Inference stamps → Enroll stamp, or the
API in the README), put it in a Secret, and install the chart with the images above,
`operator.enabled=true`, `operator.managedModelHost.image`, `exposure.*`, and
`stamp.name=<stamp>`. The README's "Bootstrap a Kubernetes stamp" section has the full
commands.

### 3c. Every stamp

- **HTTPS URL**: `exposure` (Ingress + cert-manager) must give the gateway a public HTTPS
  URL, or the edge cannot route to it (`docs/stamp-native-endpoint.md`).
- **T4**: keep `operator.managedModelHost.dtype=float16` (the chart default). T4 has no
  BF16.
- **Tool calling** (agents, Agents SDK, tool loop): the operator passes
  `--enable-auto-tool-choice` and the model's parser. Check
  `operator.managedModelHost.toolCalling` for your exact Qwen3.5 release.

Check per stamp:
`kubectl -n fabric-stamp logs statefulset/stamp-fabric-stamp -c agent | tail` shows
heartbeats; the console's Inference stamps page shows it Active with its GPUs; within one
poll the edge's routing table includes it.

## 4. Hosted tools (optional, per stamp)

```bash
# Code interpreter on E2B (key in a Secret, never in values)
kubectl -n fabric-stamp create secret generic e2b --from-literal=api-key='<E2B key>'
helm upgrade stamp deploy/helm/fabric-stamp -n fabric-stamp --reuse-values \
  --set hostedTools.codeInterpreter.enabled=true \
  --set hostedTools.codeInterpreter.backend=e2b \
  --set hostedTools.codeInterpreter.e2b.existingSecret=e2b

# Web search and fetch through your self-hosted Firecrawl
helm upgrade stamp deploy/helm/fabric-stamp -n fabric-stamp --reuse-values \
  --set hostedTools.webSearch.enabled=true --set hostedTools.webFetch.enabled=true \
  --set hostedTools.web.backend=firecrawl \
  --set hostedTools.web.firecrawl.url=https://firecrawl.internal.example.com
```

- E2B cloud: code and data leave the cluster. Firecrawl needs its own egress policy that
  blocks private address ranges.
- File search: enable persistence for the store (see `docs/file-search.md`); it is
  emptyDir by default.
- MCP: only HTTPS servers listed in `dataPlane.mcp.allowedHosts`.

## 5. Smoke test through the edge (before the audience arrives)

```bash
export FABRIC_INFERENCE_URL=https://inference.example.com
# token: exchange an API key as in examples/README.md
curl -s $FABRIC_INFERENCE_URL/v1/models -H "Authorization: Bearer $TOKEN"
curl -s $FABRIC_INFERENCE_URL/v1/messages -H "x-api-key: $TOKEN" -H 'content-type: application/json' \
  -d '{"model":"qwen3.5-2b","max_tokens":64,"messages":[{"role":"user","content":"hi"}]}'
```

Then, pointing the examples at the edge:

- `uv run --project examples/python python examples/python/scripts/agent_demo.py` — runs,
  budgets, trace, tool guard
- `.../openai_agents_demo.py` — OpenAI Agents SDK
- console → Agents → Agent playground with calculator + code interpreter

Check: each response carries `X-Fabric-Edge-Stamp`; the console's Runs page lists the runs.

## If something breaks on stage

| Symptom | Likely cause | Fix |
|---|---|---|
| `503 no_stamp_reachable` from the edge | stamp has no HTTPS URL, stale heartbeat (>5 min), or cert not trusted | check step 3c; `edge.stampCaBundle` for private CAs |
| Agent never calls tools | vLLM started without tool calling | `toolCalling` parser for the model |
| T4 model host won't start | BF16 requested | `dtype=float16` |
| Model host fails "KV cache … needs N GiB" | context too long for the GPU | lower `maxModelLen` |
| `400 hosted_tool_disabled` | tool not enabled on that stamp | step 4 |
| Run budget refused unexpectedly | budget headers reused across runs | new `X-Fabric-Run-Id` per run |
| Playground model fails on another stamp | console pointed at one stamp's gateway | leave `console.inferenceUrl` unset so it uses the edge |
