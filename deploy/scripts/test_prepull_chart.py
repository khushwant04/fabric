#!/usr/bin/env python3
"""Assert the model-host pre-pull DaemonSet (opt-in), and the agent's weight-progress mount."""

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


def prepull(values=None, enabled=True):
    merged = ({"operator.managedModelHost.prepull.enabled": "true"} if enabled else {}) | (values or {})
    found = [item for item in resources(merged) if item["kind"] == "DaemonSet"
             and item["metadata"]["name"].endswith("-prepull")]
    return found[0] if found else None


def agent(values=None):
    stamp = next(item for item in resources(values) if item["kind"] == "StatefulSet")
    pod = stamp["spec"]["template"]["spec"]
    container = next(item for item in pod["containers"] if item["name"] == "agent")
    return pod, container


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

    def test_off_by_default_so_the_customer_watches_the_first_pull(self):
        self.assertIsNone(prepull(enabled=False))

    def test_disabled_or_unmanaged_hosts_render_nothing(self):
        self.assertIsNone(prepull({"operator.managedModelHost.prepull.enabled": "false"}))
        self.assertIsNone(prepull({"operator.enabled": "false", "modelHost.url": "http://host:8000"}))


class WeightProgressChartTest(unittest.TestCase):
    def test_the_agent_reads_the_node_cache_read_only(self):
        pod, container = agent()
        self.assertIn("--model-cache-dir=/model-cache/hub", container["args"])
        mount = next(m for m in container["volumeMounts"] if m["name"] == "model-cache")
        self.assertTrue(mount["readOnly"])
        volume = next(v for v in pod["volumes"] if v["name"] == "model-cache")
        self.assertEqual(volume["hostPath"]["path"], "/mnt/fabric/model-cache")
        node = next(e for e in container["env"] if e["name"] == "FABRIC_AGENT_NODE_NAME")
        self.assertEqual(node["valueFrom"]["fieldRef"]["fieldPath"], "spec.nodeName")
        # The same path the operator gives model hosts, so the two can never disagree.
        operator = next(item for item in resources() if item["kind"] == "Deployment"
                        and item["metadata"]["name"].endswith("-operator"))
        args = operator["spec"]["template"]["spec"]["containers"][0]["args"]
        self.assertIn("--model-host-cache-host-path=/mnt/fabric/model-cache", args)

    def test_no_mount_without_a_hostpath_cache_or_when_turned_off(self):
        for values in ({"operator.managedModelHost.cache.mode": "none"},
                       {"agent.weightProgress": "false"},
                       {"agent.reportHosts": "false"}):
            pod, container = agent(values)
            self.assertFalse(any(v["name"] == "model-cache" for v in pod["volumes"]), values)
            self.assertFalse(any(a.startswith("--model-cache-dir") for a in container["args"]))

    def test_activity_reporting_can_be_turned_off(self):
        _pod, container = agent({"agent.reportActivity": "false"})
        self.assertIn("--report-activity=false", container["args"])
        _pod, container = agent()
        self.assertNotIn("--report-activity=false", container["args"])


if __name__ == "__main__":
    unittest.main()
