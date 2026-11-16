#!/usr/bin/env python3
"""Assert the model-host pre-pull DaemonSet: placement, image, weight warm-up and opt-out."""

import pathlib
import subprocess
import unittest

import yaml

CHART = pathlib.Path(__file__).resolve().parents[1] / "helm/fabric-stamp"
IMAGE = "registry.example.com/fabric/model-host:v1"


def resources(values=None):
    merged = {
        "controlPlane.url": "https://control.example.com",
        "enrollment.existingSecret": "enrollment",
        "stamp.name": "stamp-a",
        "operator.enabled": "true",
        "operator.managedModelHost.image": IMAGE,
    } | (values or {})
    command = ["helm", "template", "fabric", str(CHART)]
    for key, value in merged.items():
        command.extend(["--set", f"{key}={value}"])
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise AssertionError(result.stderr)
    return [item for item in yaml.safe_load_all(result.stdout) if item]


def prepull(values=None):
    found = [item for item in resources(values) if item["kind"] == "DaemonSet"
             and item["metadata"]["name"].endswith("-prepull")]
    return found[0] if found else None


class PrepullChartTest(unittest.TestCase):
    def test_pulls_the_model_host_image_on_gpu_nodes_without_requesting_a_gpu(self):
        daemonset = prepull({
            "gpu.nodeSelector.pool": "gpu",
            "gpu.tolerations[0].key": "sku",
            "gpu.tolerations[0].value": "gpu",
            "gpu.tolerations[0].effect": "NoSchedule",
        })
        pod = daemonset["spec"]["template"]["spec"]
        self.assertEqual(pod["nodeSelector"], {"pool": "gpu"})
        self.assertEqual(pod["tolerations"][0]["key"], "sku")
        self.assertFalse(pod["automountServiceAccountToken"])
        containers = pod["initContainers"] + pod["containers"]
        self.assertTrue(all(container["image"] == IMAGE for container in containers))
        for container in containers:
            self.assertNotIn("nvidia.com/gpu", str(container.get("resources", {})))
        # Without models, nothing touches the node's disk.
        self.assertEqual([c["name"] for c in pod["initContainers"]], ["pull-model-host"])
        self.assertNotIn("volumes", pod)

    def test_warms_listed_weights_into_the_hostpath_cache_the_operator_uses(self):
        daemonset = prepull({"operator.managedModelHost.prepull.models": "{Qwen/Qwen3.5-2B}"})
        pod = daemonset["spec"]["template"]["spec"]
        warm = next(c for c in pod["initContainers"] if c["name"] == "warm-weights")
        self.assertIn("Qwen/Qwen3.5-2B", warm["command"])
        env = {item["name"]: item.get("value") for item in warm["env"]}
        self.assertEqual(env["HF_HOME"], "/model-cache")
        self.assertEqual(env["HF_HUB_CACHE"], "/model-cache/hub")
        self.assertEqual(pod["volumes"][0]["hostPath"]["path"], "/mnt/fabric/model-cache")
        # Large weight files are buffered in memory while downloading.
        self.assertEqual(warm["resources"]["limits"]["memory"], "10Gi")

    def test_no_weight_warmup_without_a_node_cache(self):
        daemonset = prepull({
            "operator.managedModelHost.prepull.models": "{Qwen/Qwen3.5-2B}",
            "operator.managedModelHost.cache.mode": "none",
        })
        pod = daemonset["spec"]["template"]["spec"]
        self.assertEqual([c["name"] for c in pod["initContainers"]], ["pull-model-host"])

    def test_disabled_or_unmanaged_hosts_render_nothing(self):
        self.assertIsNone(prepull({"operator.managedModelHost.prepull.enabled": "false"}))
        self.assertIsNone(prepull({"operator.enabled": "false", "modelHost.url": "http://host:8000"}))


if __name__ == "__main__":
    unittest.main()
