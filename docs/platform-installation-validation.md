# Single-tenant installation validation

Validated on 2026-10-06 against an isolated single-node k3s cluster running
`v1.35.5+k3s1`. The node had no allocatable NVIDIA GPUs. This establishes the
installation, enrollment, authorization, and ingress behavior below; GPU model
startup and inference require a GPU cluster.

The [release manifest](../deploy/releases/singletenant-20261006/release.json) records
the registry-confirmed image digests and source-file hashes. Image-only Helm overlays
are available for the [control plane and console](../deploy/releases/singletenant-20261006/control-plane-images.yaml)
and [stamp](../deploy/releases/singletenant-20261006/stamp-images.yaml). The local
installation used locally imported builds. The published control-plane and console
images were also pulled and run in isolated containers. Target-cluster access to
the private registry requires its own pull credentials.

| Check | Observed result |
|---|---|
| Fresh Helm control-plane installation | Migration/bootstrap completed; API and console Deployments became ready. |
| Repeated bootstrap/upgrade | The configured fixed account and administrator membership were preserved. |
| Stamp installation before model selection | Agent, data plane, collector, and operator became ready without a chart-wide model repository. |
| Infrastructure enrollment | The agent registered the stamp and reported zero measured GPU capacity. |
| Qwen3.5-4B placement without GPUs | Placement returned HTTP 409; the model deployment remained recorded without a serving claim. |
| Native Kubernetes ingress | Traefik served `/v1/models` over HTTPS with a locally supplied certificate; anonymous access returned HTTP 401. |
| Administrative listener isolation | The public ingress returned HTTP 404 for the administrative path. |
| Stamp identity persistence | After removing the one-time enrollment Secret and restarting the stamp, its identity persisted on the existing PVC. |
| Durable storage | Stamp identity and usage-spool PVCs were bound through the cluster's `local-path` StorageClass. |
| Console container | Standalone server ran as UID/GID 65532 with a read-only root filesystem and writable temporary/cache mounts. |
| Console authentication boundary | Health/home returned HTTP 200; protected routes redirected to Auth0. Callback configuration and PKCE were checked. |

The native HTTPS check used an isolated hostname and certificate trusted by the test
client. It did not exercise public DNS propagation or a public certificate issuer.
Real browser sign-in with the installation owner's Auth0 account remains untested;
the authorization suites verify account membership and token isolation independently.

The recorded model remained `Qwen/Qwen3.5-4B`; unavailable hardware produced a capacity
error. The operator tests separately reconcile an empty stamp, then place
`Qwen/Qwen3.5-4B` and check that the exact repository becomes both the loaded model
and upstream served name.

## Automated checks

| Suite | Result and coverage |
|---|---|
| Control-plane PostgreSQL | 222 tests passed using a non-superuser `fabric_app` role without BYPASSRLS; two tests were skipped. |
| Data plane | 371 tests passed, one skipped; Ruff passed. Routing, verification, metering, streaming, and upstream isolation were checked. |
| Go agent/operator | All packages passed `go test ./...`, including model-free stamp startup, readiness reporting, capacity claims, standard/legacy GPU discovery labels, and endpoint validation. |
| Console application | 70 test cases passed, including fixed-account membership, canonical UUID matching, placement retry after a lost response, confirmed deletion, observed state, and gateway authorization; lint, TypeScript, and production build passed. |
| Console Helm packaging | Seven tests passed, covering optional installation, fixed-account configuration, runtime Secret references, gateway allowlists, callback origin validation, and private container settings. |
| Legacy upgrade automation | Five tests passed, including upgrades from stored release values, empty enrolled stamps, verification preservation, and failure before unsafe changes. |
| Stamp exposure/production rendering | Native Ingress, existing certificate, generated hostname, model-free operator, Istio compatibility, immutable image selection, and private administrative listeners passed. |
| Installation guide examples | Both values examples rendered successfully; all shell blocks parsed and local links resolved. |

Final deployment validation still requires a real Auth0 login, registry pulls from
the target cluster, and a GPU workload.

Reproduce the packaging checks from the repository root:

```bash
bash deploy/scripts/test-stamp-exposure-chart.sh
bash deploy/scripts/test-production-chart.sh
python3 deploy/scripts/test_console_chart.py
python3 deploy/scripts/test_upgrade_automation.py
```

The Python chart checks require Helm and PyYAML. Use the
[installation guide](single-tenant-installation.md) for the control-plane and stamp
commands, explicit cluster contexts, and required Secrets. For performance evidence
and its limits, see [runtime validation](runtime-performance-validation.md) and
[startup diagnosis](model-startup-diagnosis.md).
