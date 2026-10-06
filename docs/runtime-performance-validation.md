# Packed decode validation on AKS T4

The Qwen3.5 packed gated-delta decode kernel was corrected and compared with
the installed stock vLLM 0.26.0 operation on a spare Tesla T4 on 2026-10-06.
Serving hosts continue to use the stock operation. This validation measured a
single synthetic kernel, not complete model inference or startup.

## Correctness and timing

The correction preserves the stock rounding order for sigmoid beta before the
FP32 recurrent update. Validation compares output **and every recurrent-state
element**, using three independent cold seeds and sixteen recurrent steps.
Default tolerance is zero. The comparator refuses to time a candidate that
fails correctness and retains failures in the exported evidence.

The default one-warp kernel passed exact comparisons for batches 1, 4, 8 and 16
for both tested shapes. Some multi-warp candidates differed in FP32 state and
failed the exact gate. Both complete sweep artifacts therefore have
`status: fail`; they must not be presented as a fully passing sweep. The table
below reports only candidates that passed their individual gate.

| Shape from model config | Batch | Stock microseconds | Default Fabric microseconds | Fastest passing swept ratio |
|---|---:|---:|---:|---:|
| Qwen3.5-0.8B: H16, HV16, K128, V128 | 1 | 20.75 | 20.74 | 1.804x |
| Same | 4 | 20.62 | 20.54 | 1.165x |
| Same | 8 | 68.92 | 68.65 | 1.120x |
| Same | 16 | 125.60 | 126.32 | 1.032x |
| Qwen3.5-4B: H16, HV32, K128, V128 | 1 | 25.12 | 25.12 | 1.606x |
| Same | 4 | 68.98 | 62.25 | 1.120x |
| Same | 8 | 125.76 | 120.46 | 1.042x |
| Same | 16 | 244.98 | 239.34 | 1.024x |

The sweep selected block V16 / one warp except for 4B batch16, which selected
V32 / one warp. These selections are experimental and have not been activated
in serving. Graph replay removes Python launch overhead for the comparison;
the live hosts currently run eager execution. The measured ratios cannot be
used as end-to-end token throughput gains.

## Evidence and reproduction

The GPU was a Tesla T4, compute capability 7.5, driver 580.159.04. The Job used
the existing model-host image at immutable digest
`sha256:fe3246f43f6525a734acbdbdfc07a2b13f5b48bcc8b7db832624d5c3d735af55`,
with Torch 2.11.0+cu130 and Triton 3.6.0. Model config revisions and hashes,
full candidate comparisons, timings, stock source/helper hashes, and device
observations are retained in the artifacts:

- [0.8B shape artifact](../runtime/artifacts/t4-production/20261006-20261006T132704.010336Z-nogit.json)
- [4B shape artifact](../runtime/artifacts/t4-production/20261006-20261006T132727.099344Z-nogit.json)
- [Source manifest](../runtime/artifacts/t4-production/source-manifest.json)

The ConfigMap-mounted source lacked Git metadata, so the source manifest records
the hashes of the exact files executed. The `t4-production` artifact target
identifies the hardware where validation ran; it does not mean the candidate
kernel was enabled in production.

The [stock extractor](../deploy/scripts/extract-stock-packed.py) reads the
installed packed wrapper/kernel and verifies its supported dependencies and
`FLA_USE_FAST_OPS=0` helper before importing Torch/Triton. It avoids initializing
a vLLM engine and records the extraction hashes. Run it inside the pinned image
on an explicitly selected spare GPU, with the checked-out runtime/serving
directories mounted read-only and a writable work/artifact directory:

```bash
python3 deploy/scripts/extract-stock-packed.py \
  --output /work/stock-packed.py \
  --runtime-root "$PWD/runtime" --serving-root "$PWD/serving" -- \
  --model-config /work/qwen-4b-config.json \
  --model-ref Qwen/Qwen3.5-4B \
  --model-revision 851bf6e806efd8d0a36b00ddf55e13ccb7b8cd0a \
  --batch 1 4 8 16 --dtype float16 --cuda-graph --sweep \
  --drift-steps 16 --warmup 30 --reps 200 \
  --target t4-production --artifact-root /work/evidence --json
```

Supply the model config from that exact revision. A failed candidate makes the
command exit nonzero after writing evidence; review candidate status alongside
timing. Artifact creation is exclusive, so choose a fresh output path.

Before enabling a custom serving kernel, build an image containing this exact
revision and validate complete-model outputs, streaming, concurrent requests,
and recurrent generation against a separately deployed stock host. Use the
[endpoint benchmark](cluster-performance-validation.md) with matching model,
hardware and serving settings. Kernel microbenchmarks alone do not meet that
gate. [Startup measurements](model-startup-diagnosis.md) separately identify
scheduling, image, engine warmup and first-request delays.
