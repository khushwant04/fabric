#!/usr/bin/env python3
"""Assert the Cloud TPU packaging: model-host flags, no NVIDIA pieces, and unchanged GPU stamps."""

import pathlib
import subprocess
import unittest

import yaml

CHART = pathlib.Path(__file__).resolve().parents[1] / "helm/fabric-stamp"
IMAGE = "registry.example.com/fabric/model-host:v1"

# What the bootstrap sets on an NVIDIA VM, which TPU support must render exactly as before.
NVIDIA = {
    "gpu.runtimeClassName": "nvidia",
    "gpu.nodeSelector.accelerator": "nvidia",
    "gpuMetrics.dcgmExporter.enabled": "true",
}
# What the bootstrap sets on a Cloud TPU VM.
TPU = {
    "accelerator.type": "tpu",
    "gpu.nodeSelector.accelerator": "tpu",
    "accelerator.tpu.devices": "{/dev/vfio}",
}


def render(values=None):
    merged = {
        "controlPlane.url": "https://control.example.com",
        "enrollment.existingSecret": "enrollment",
        "stamp.name": "stamp-a",
        "operator.enabled": "true",
        "operator.managedModelHost.image": IMAGE,
    } | (values or {})
    command = ["helm", "template", "fabric", str(CHART), "--namespace", "fabric-stamp"]
    for key, value in merged.items():
        command.extend(["--set", f"{key}={value}"])
    return subprocess.run(command, capture_output=True, text=True, check=False)


def resources(values=None):
    result = render(values)
    if result.returncode:
        raise AssertionError(result.stderr)
    return [item for item in yaml.safe_load_all(result.stdout) if item]


def find(items, kind, suffix):
    return next(
        (item for item in items if item["kind"] == kind and item["metadata"]["name"].endswith(suffix)),
        None,
    )


def operator_args(items):
    deployment = find(items, "Deployment", "-operator")
    return deployment["spec"]["template"]["spec"]["containers"][0]["args"]


def agent_args(items):
    pod = find(items, "StatefulSet", "fabric-stamp")["spec"]["template"]["spec"]
    return next(c for c in pod["containers"] if c["name"] == "agent")["args"]


class TPUChartTest(unittest.TestCase):
    def test_a_tpu_stamp_runs_the_tpu_image_privileged_with_the_devices(self):
        args = operator_args(resources(TPU))
        self.assertIn("--model-host-image=vllm/vllm-tpu:v0.26.0", args)
        self.assertNotIn(f"--model-host-image={IMAGE}", args)
        self.assertIn("--model-host-accelerator=tpu", args)
        self.assertIn("--model-host-privileged", args)
        self.assertIn("--model-host-command=vllm serve", args)
        self.assertIn("--model-host-device=/dev/vfio", args)
        self.assertIn("--model-host-dtype=bfloat16", args)
        self.assertIn("--model-host-node-selector=accelerator=tpu", args)

    def test_a_tpu_stamp_has_no_nvidia_runtime_or_dcgm_even_if_asked(self):
        items = resources(TPU | {
            "gpu.runtimeClassName": "nvidia",
            "gpuMetrics.dcgmExporter.enabled": "true",
            "gpuMetrics.external.enabled": "true",
            "operator.managedModelHost.dtype": "float16",
        })
        args = operator_args(items)
        self.assertFalse(any(arg.startswith("--model-host-runtime-class") for arg in args))
        self.assertIn("--model-host-dtype=bfloat16", args)
        self.assertIsNone(find(items, "DaemonSet", "-dcgm-exporter"))
        self.assertFalse(any(arg.startswith("--dcgm-service") for arg in agent_args(items)))
        self.assertNotIn("nvidia.com/gpu", yaml.safe_dump_all(
            [item for item in items if item["kind"] in ("Deployment", "DaemonSet", "StatefulSet")]))

    def test_the_tpu_image_can_be_overridden_and_is_what_prepull_pulls(self):
        items = resources(TPU | {
            "accelerator.tpu.image": "mirror.example.com/vllm-tpu:pinned",
            "operator.managedModelHost.prepull.enabled": "true",
            "operator.managedModelHost.prepull.models": "{Qwen/Qwen3.5-9B}",
        })
        self.assertIn("--model-host-image=mirror.example.com/vllm-tpu:pinned", operator_args(items))
        prepull = find(items, "DaemonSet", "-prepull")
        images = {c["image"] for c in prepull["spec"]["template"]["spec"]["initContainers"]}
        self.assertEqual(images, {"mirror.example.com/vllm-tpu:pinned"})

    def test_bad_values_are_refused(self):
        rejected = render({"accelerator.type": "amd"})
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("accelerator.type must be nvidia, tpu or neuron", rejected.stderr)
        rejected = render(TPU | {"accelerator.tpu.devices": "{/etc/passwd}"})
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("accelerator.tpu.devices must be paths under /dev", rejected.stderr)

    def test_nvidia_stamps_render_no_tpu_flags(self):
        for values in ({}, NVIDIA, NVIDIA | {"accelerator.type": "nvidia"}):
            items = resources(values)
            args = operator_args(items)
            self.assertIn(f"--model-host-image={IMAGE}", args)
            self.assertFalse(any("tpu" in arg or arg.startswith(("--model-host-privileged",
                                                                "--model-host-device",
                                                                "--model-host-command"))
                                 for arg in args), args)
        items = resources(NVIDIA)
        self.assertIn("--model-host-runtime-class=nvidia", operator_args(items))
        self.assertIsNotNone(find(items, "DaemonSet", "-dcgm-exporter"))

    def test_choosing_nvidia_explicitly_renders_byte_for_byte_the_default(self):
        def strip_random(text):
            # The chart generates a fresh shared token on each render.
            return "\n".join(line for line in text.splitlines() if not line.strip().startswith("token:"))

        default = render(NVIDIA)
        explicit = render(NVIDIA | {"accelerator.type": "nvidia"})
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertEqual(strip_random(default.stdout), strip_random(explicit.stdout))


if __name__ == "__main__":
    unittest.main()
