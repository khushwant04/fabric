# Kernel launch tuning for T4 and A10

The gated-delta decode kernels take their launch shape (`block_v`, `num_warps`,
`num_stages`) from a per-architecture table in
[`runtime/kernels/launch_config.py`](../runtime/kernels/launch_config.py). The table is
keyed by compute capability and by batch bucket (1, 2–4, 5–16, 17+). A sweep run on the
target GPU can replace it through a JSON file, without a code change or a new image.

**Most T4 and A10 entries are not measured.** The only measured entries for a production
GPU are the T4 packed-kernel entries for batch 1–16. They come from the 2026-10-06 sweep
described in [packed decode validation](runtime-performance-validation.md). Every other T4
entry, and every A10 entry, was chosen by reasoning. Each is marked `measured: False` and
carries the reasoning in its `note`. Run the sweep below on each GPU before calling any of
them tuned.

## How a shape is chosen

For each launch, field by field, the first of these that is set wins:

1. Explicit `block_v` / `num_warps` / `num_stages` arguments.
2. `FABRIC_KERNEL_BLOCK_V`, `FABRIC_KERNEL_NUM_WARPS`, `FABRIC_KERNEL_NUM_STAGES`. These
   apply to the packed kernel (the one substituted into vLLM) only. They were already the
   serving override knobs; the stages variable is new.
3. The entry for this compute capability and batch bucket in the file named by
   `FABRIC_KERNEL_TUNING_FILE`.
4. The built-in table entry.
5. The kernel's previous default for a GPU the table does not know: vLLM's own tile
   (32 wide, 1 warp, 3 stages) for the packed kernel, and 32/8 for the recurrence kernel.

A tile and its warp count go together. If `block_v` comes from an argument or the
environment but `num_warps` does not, the warp count is derived from `block_v` by the
kernel's old rule, not taken from a table entry tuned for a different tile.

The device's compute capability is read once per device and cached, and each resolved
shape is cached per batch size. Nothing on the decode path touches device memory, so the
lookup is safe inside CUDA-graph capture.

The tuning file is read and validated when the vLLM substitution is installed. If the
file is invalid, `sitecustomize` prints `[fabric] KERNEL SUBSTITUTION FAILED: ...` and the
host serves vLLM's own op. The first request does not fail. The armed message shows what
is in effect:

```
[fabric] kernel substitution armed pid=... tile={'environment_overrides': {}, 'tuning_file': '/path/file.json', 'tuning_entries': 4} ...
```

## Shipped entries

Packed kernel (the kernel used for vLLM substitution), as `block_v / num_warps / num_stages`:

| GPU | Batch 1 | 2–4 | 5–16 | 17+ |
|---|---|---|---|---|
| T4, SM75 | 16/1/3 measured | 16/1/3 measured | 16/1/3 measured | 32/1/1 reasoned |
| A10, SM86 | 16/1/3 reasoned | 32/1/3 reasoned | 32/1/3 reasoned | 32/1/3 reasoned |
| RTX 4070, SM89 | 32/1/3 reasoned | 32/1/3 reasoned | 32/1/3 reasoned | 32/1/3 reasoned |
| Any other | 32/1/3 default | 32/1/3 default | 32/1/3 default | 32/1/3 default |

Recurrence kernel (`gated_delta_decode`, used by `integration/dispatch.py`):

| GPU | Batch 1 | 2–4 | 5–16 | 17+ |
|---|---|---|---|---|
| T4, SM75 | 16/4/1 reasoned | 16/4/1 reasoned | 32/8/1 reasoned | 32/8/1 reasoned |
| A10, SM86 | 16/4/3 reasoned | 32/8/3 reasoned | 32/8/3 reasoned | 32/8/3 reasoned |
| RTX 4070, SM89 | 32/8/3 measured | 32/8/3 measured | 32/8/3 measured (batch 8 only) | 32/8/3 reasoned |
| Any other | 32/8/3 default | 32/8/3 default | 32/8/3 default | 32/8/3 default |

Where the entries come from:

- **T4 packed, batch 1–16 (measured).** These are the fastest candidates that passed the
  exact gate (atol=0) against stock vLLM. They were timed in CUDA-graph replay, in FP16, at
  K=V=128 with 16 and 32 value heads. The result was 16/1 at batches 1, 4 and 8 for both
  shapes, and at batch 16 for 16 value heads. At batch 16 with 32 value heads, 32/1 won.
  Both artifacts have `status: fail` overall, because multi-warp candidates missed the
  exact gate. The selected one-warp candidates passed their own gate. A test checks the
  table against the committed artifacts. The sweep recorded `num_stages=3`. This kernel
  has no loop for Triton to pipeline, and SM75 has no `cp.async`, so stages are not
  expected to matter, but no T4 run has measured stages 1 or 2.
- **T4, other entries (reasoned).** The T4 has 40 SMs. At batch 1 with 16 value heads, a
  16-wide tile launches 128 programs (3.2 waves) instead of 64 (1.6 waves). Above batch 16
  the step is bound by memory bandwidth: the grid is many waves deep, and a wider tile
  halves the q/k re-reads. Entries not taken from the artifact use `num_stages=1`, since
  SM75 has no asynchronous copies for Triton's pipeliner to use.
- **A10 (reasoned, nothing measured).** The A10 has 72 SMs. At batch 1, a 32-wide tile
  launches 64 programs, which leaves SMs idle, so the table uses a 16-wide tile. From batch
  2 the table keeps vLLM's tile. Wider tiles (64, 128) at high batch would cut q/k re-reads
  further, but they need more than one warp to hold the FP32 state without spilling. On
  T4, every multi-warp candidate differed from stock vLLM in the last bits of the FP32
  state. So wider tiles stay out of the defaults until an A10 sweep shows they are both
  faster and acceptable.
- **RTX 4070 recurrence entries (measured).** These come from the committed
  `rtx4070-dev` value-tile sweeps, run with eager launches.

The table is keyed by batch, but the number of programs also scales with the number of
value heads. For a model whose head count differs from 16, run the sweep with its shape.

## Dtype policy: T4 serves FP16

SM75 has no native BF16. On compute capability below 8.0, the Fabric kernels refuse BF16
inputs:

- `integration/dispatch.py`: `unsupported_reason` returns a reason that names the
  capability and `--dtype half`. `auto` mode falls back to the eager path and counts the
  fallback. `fabric` mode raises `KernelUnavailableError`.
- `fused_packed_decode` and `gated_delta_decode` raise `ValueError`.
- The vLLM substitution (`serving/fabric_serving/register.py`) hands BF16 calls on SM75
  back to vLLM's own op, counts them in `register.delegations`, and prints one line to
  stderr.

**T4 deployments must run FP16 (`--dtype half`).** The operator already corrects part of
this. When a model host asks for `bfloat16` and the weakest GPU in the pool is below
compute capability 8.0, `applyProfile` in
[`agent/internal/operator/hardware.go`](../agent/internal/operator/hardware.go) rewrites
the dtype to `float16` and records the adjustment. The capability table is in
`agent/internal/hardware`. The operator does not change an empty dtype: with no
`--model-host-dtype`, vLLM resolves `auto` from the model config on its own. Set
`--model-host-dtype=float16` explicitly for T4 pools instead of relying on that. A10 can
serve FP16 or BF16.

## Running the sweep

`runtime/harness/tune.py` runs on one GPU. For every batch it checks each candidate
`block_v × num_warps × num_stages` against the eager reference: output and every
recurrent-state element, three cold seeds, and a 16-step recurrent drift run. It then
times the candidates that passed, interleaved, replayed from a CUDA graph (which is how
vLLM runs decode). Within each batch bucket it picks the candidate with the lowest mean
latency relative to the fastest candidate at each batch. The sweep checks `--target`
against the GPU present before it runs anything, and refuses on a mismatch. Entries are
keyed by the compute capability it detects.

Run it on a spare GPU with nothing else running, with the head counts and head
dimensions from the served model's config (`linear_num_key_heads`,
`linear_num_value_heads`, `linear_key_head_dim`, `linear_value_head_dim`). In the
model-host image the runtime is at `/opt/fabric/runtime`.

T4:

```bash
cd runtime
python3 -m harness.tune --target t4-production --dtype float16 \
  --heads 16 --value-heads 16 --key-dim 128 --value-dim 128 \
  --batch 1 2 4 8 16 32 64 --artifact-root /work/evidence
```

On SM75 the default stages swept are 1, 2 and 3. Stage 3 is included so that the
recorded entries can be checked again.

A10:

```bash
cd runtime
python3 -m harness.tune --target a10-research --dtype float16 \
  --heads 16 --value-heads 16 --key-dim 128 --value-dim 128 \
  --batch 1 2 4 8 16 32 64 --artifact-root /work/evidence
```

Sweep in the dtype the host serves. The tuning file is keyed by GPU and batch, not by
dtype, so a BF16 A10 deployment needs a BF16 sweep (`--dtype bfloat16`). The sweep refuses
BF16 on SM75.

Useful options:

- `--require-bitwise-default` keeps only candidates whose output and state match vLLM's
  tile (32/1/3) bit for bit. On T4, that tile matched stock vLLM exactly. Use this option
  where exact agreement with the stock op is required. For a direct exact comparison with
  the stock op, use `serving/fabric_serving/compare_packed.py --sweep` (see
  [packed decode validation](runtime-performance-validation.md)).
- `--num-warps 1` restricts the sweep to one-warp tiles.
- `--no-cuda-graph` times eager launches. This answers a different question from how
  vLLM serves; see the note in `register.py`.
- `--atol` and `--rtol` (default 2e-3) are the tolerances against the eager reference.

Exit status: `0` means every candidate passed and every bucket was selected. `1` means
some candidate failed; the evidence and tuning file are still written, and only passing
candidates are selected. `2` means nothing ran (target mismatch, no GPU, BF16 on SM75, or
bad arguments).

## Where the output goes

- Versioned artifact: `<artifact-root>/<target>/<UTC stamp>-<commit>.json`. Its suite is
  `packed-decode-launch-tuning`. It holds every candidate's correctness record and median
  timing, the selections, the GPU name and compute capability, and the full environment,
  with a content hash. Writes are exclusive and never overwrite evidence.
- Tuning file: `<artifact-root>/<target>/tuning/<same stem>.tuning.json`, or
  `--tuning-output`. Schema `fabric-kernel-tuning/v1`:

```json
{
  "schema": "fabric-kernel-tuning/v1",
  "target": "t4-production",
  "gpu": {"name": "Tesla T4", "compute_capability": "7.5"},
  "artifact": {"path": "...", "content_hash": "..."},
  "entries": [
    {"kernel": "packed", "compute_capability": "7.5", "batch_bucket": "1",
     "block_v": 16, "num_warps": 1, "num_stages": 1, "measured": true, "note": "..."}
  ]
}
```

A file may cover some buckets and capabilities and not others. Anything it does not cover
falls through to the table. The whole file is rejected, with an error naming the file and
the entry, if any entry is malformed or duplicated.

## Shipping it to a model host

1. Commit the artifact and the tuning file under `runtime/artifacts/<target>/`, with the
   artifact cited next to it, as for other evidence.
2. Make the file visible to the host. Either:
   - build the model-host image, which copies `runtime/` to `/opt/fabric/runtime/`, so the
     file is at `/opt/fabric/runtime/artifacts/<target>/tuning/<file>`; or
   - mount it from a ConfigMap, which needs no rebuild.
3. Set `FABRIC_KERNEL_TUNING_FILE=<path>` on the host alongside `FABRIC_KERNEL=1`. The
   operator does not manage this variable yet. It sets `FABRIC_KERNEL`,
   `FABRIC_KERNEL_BLOCK_V` and `FABRIC_KERNEL_NUM_WARPS` only, so add it to the pod spec
   yourself.
4. Restart the host. Check the `kernel substitution armed` line for the file and the
   number of entries.

To return to the exact like-for-like tile on any GPU, set `FABRIC_KERNEL_BLOCK_V=32`,
`FABRIC_KERNEL_NUM_WARPS=1` and `FABRIC_KERNEL_NUM_STAGES=3`. Explicit environment values
beat both the file and the table.

A kernel sweep alone does not meet the gate for enabling the substitution. The
complete-model and endpoint validation in
[packed decode validation](runtime-performance-validation.md) still applies.
