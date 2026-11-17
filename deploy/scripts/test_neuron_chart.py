#!/usr/bin/env python3
"""Assert the AWS Neuron packaging: model-host flags, no NVIDIA pieces, unchanged GPU/TPU stamps."""

import pathlib
import subprocess
import unittest

import yaml

CHART = pathlib.Path(__file__).resolve().parents[1] / "helm/fabric-stamp"
IMAGE = "registry.example.com/fabric/model-host:v1"
NEURON_IMAGE = (
    "public.ecr.aws/neuron/pytorch-inference-vllm-neuronx:0.13.0-neuronx-py312-sdk2.28.0-ubuntu24.04"
)

# What the bootstrap sets on an NVIDIA VM and on a Cloud TPU VM, which Neuron support must
# render exactly as before.
NVIDIA = {
    "gpu.runtimeClassName": "nvidia",
    "gpu.nodeSelector.accelerator": "nvidia",
    "gpuMetrics.dcgmExporter.enabled": "true",
}
TPU = {
    "accelerator.type": "tpu",
    "gpu.nodeSelector.accelerator": "tpu",
    "accelerator.tpu.devices": "{/dev/vfio}",
}
# What the bootstrap sets on an inf2/trn1/trn2 instance where the Neuron device plugin runs.
NEURON = {
    "accelerator.type": "neuron",
    "gpu.nodeSelector.accelerator": "neuron",
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


class NeuronChartTest(unittest.TestCase):
    def test_a_neuron_stamp_runs_the_neuron_image_through_the_device_plugin(self):
        args = operator_args(resources(NEURON))
        self.assertIn(f"--model-host-image={NEURON_IMAGE}", args)
        self.assertNotIn(f"--model-host-image={IMAGE}", args)
        self.assertIn("--model-host-accelerator=neuron", args)
        self.assertIn("--model-host-neuron-cores-per-device=2", args)
        self.assertIn("--model-host-command=vllm serve", args)
        self.assertIn("--model-host-dtype=bfloat16", args)
        self.assertIn("--model-host-node-selector=accelerator=neuron", args)
        # The device plugin injects the devices: no privilege and no hostPath devices.
        self.assertNotIn("--model-host-privileged", args)
        self.assertFalse(any(arg.startswith("--model-host-device") for arg in args), args)

    def test_trainium2_cores_per_device_reaches_the_operator(self):
        args = operator_args(resources(NEURON | {"accelerator.neuron.coresPerDevice": "4"}))
        self.assertIn("--model-host-neuron-cores-per-device=4", args)

    def test_without_the_device_plugin_the_host_is_privileged_with_the_devices(self):
        args = operator_args(resources(NEURON | {
            "accelerator.neuron.privileged": "true",
            "accelerator.neuron.devices": "{/dev/neuron0,/dev/neuron1}",
        }))
        self.assertIn("--model-host-privileged", args)
        self.assertIn("--model-host-device=/dev/neuron0", args)
        self.assertIn("--model-host-device=/dev/neuron1", args)

    def test_a_neuron_stamp_has_no_nvidia_runtime_dcgm_or_tpu_flags_even_if_asked(self):
        items = resources(NEURON | {
            "gpu.runtimeClassName": "nvidia",
            "gpuMetrics.dcgmExporter.enabled": "true",
            "gpuMetrics.external.enabled": "true",
            "operator.managedModelHost.dtype": "float16",
            "accelerator.tpu.devices": "{/dev/vfio}",
        })
        args = operator_args(items)
        self.assertFalse(any(arg.startswith("--model-host-runtime-class") for arg in args))
        self.assertFalse(any("tpu" in arg for arg in args), args)
        self.assertFalse(any(arg.startswith("--model-host-device") for arg in args), args)
        self.assertIn("--model-host-dtype=bfloat16", args)
        self.assertIsNone(find(items, "DaemonSet", "-dcgm-exporter"))
        self.assertFalse(any(arg.startswith("--dcgm-service") for arg in agent_args(items)))
        self.assertNotIn("nvidia.com/gpu", yaml.safe_dump_all(
            [item for item in items if item["kind"] in ("Deployment", "DaemonSet", "StatefulSet")]))

    def test_the_neuron_image_can_be_overridden_and_is_what_prepull_pulls(self):
        items = resources(NEURON | {
            "accelerator.neuron.image": "mirror.example.com/vllm-neuron:pinned",
            "operator.managedModelHost.prepull.enabled": "true",
            "operator.managedModelHost.prepull.models": "{Qwen/Qwen3.5-4B}",
        })
        self.assertIn("--model-host-image=mirror.example.com/vllm-neuron:pinned", operator_args(items))
        prepull = find(items, "DaemonSet", "-prepull")
        images = {c["image"] for c in prepull["spec"]["template"]["spec"]["initContainers"]}
        self.assertEqual(images, {"mirror.example.com/vllm-neuron:pinned"})

    def test_bad_values_are_refused(self):
        rejected = render(NEURON | {"accelerator.neuron.devices": "{/dev/vfio}"})
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("accelerator.neuron.devices must be Neuron device nodes", rejected.stderr)
        rejected = render(NEURON | {"accelerator.neuron.coresPerDevice": "3"})
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("accelerator.neuron.coresPerDevice must be 1, 2, 4 or 8", rejected.stderr)
        rejected = render({"accelerator.type": "inferentia"})
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("accelerator.type must be nvidia, tpu or neuron", rejected.stderr)

    def test_nvidia_and_tpu_stamps_render_no_neuron_flags(self):
        for values in ({}, NVIDIA, TPU):
            args = operator_args(resources(values | {"accelerator.neuron.privileged": "true",
                                                     "accelerator.neuron.devices": "{/dev/neuron0}"}))
            self.assertFalse(any("neuron" in arg for arg in args), args)
        self.assertIn("--model-host-runtime-class=nvidia", operator_args(resources(NVIDIA)))
        self.assertIn("--model-host-accelerator=tpu", operator_args(resources(TPU)))

    def test_neuron_values_do_not_change_nvidia_or_tpu_renders(self):
        def strip_random(text):
            # The chart generates a fresh shared token on each render.
            return "\n".join(line for line in text.splitlines() if not line.strip().startswith("token:"))

        for values in (NVIDIA, TPU):
            plain = render(values)
            touched = render(values | {"accelerator.neuron.coresPerDevice": "8",
                                       "accelerator.neuron.image": "x/y:z"})
            self.assertEqual(plain.returncode, 0, plain.stderr)
            self.assertEqual(strip_random(plain.stdout), strip_random(touched.stdout))


if __name__ == "__main__":
    unittest.main()
