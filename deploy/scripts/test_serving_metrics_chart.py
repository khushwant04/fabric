#!/usr/bin/env python3
"""Assert the serving-metrics packaging: agent flags, namespaced RBAC and the DCGM exporter."""

import pathlib
import subprocess
import unittest

import yaml

CHART = pathlib.Path(__file__).resolve().parents[1] / "helm/fabric-stamp"
IMAGE = "registry.example.com/fabric/model-host:v1"


def resources(values=None, managed=True):
    merged = {
        "controlPlane.url": "https://control.example.com",
        "enrollment.existingSecret": "enrollment",
        "stamp.name": "stamp-a",
    }
    if managed:
        merged |= {"operator.enabled": "true", "operator.managedModelHost.image": IMAGE}
    else:
        merged |= {"modelHost.url": "http://model-host.example:8000", "stamp.measureCapacity": "false"}
    merged |= values or {}
    command = ["helm", "template", "fabric", str(CHART), "--namespace", "fabric-stamp"]
    for key, value in merged.items():
        command.extend(["--set", f"{key}={value}"])
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode:
        raise AssertionError(result.stderr)
    return [item for item in yaml.safe_load_all(result.stdout) if item]


def find(items, kind, suffix):
    return next(
        (item for item in items if item["kind"] == kind and item["metadata"]["name"].endswith(suffix)),
        None,
    )


def stamp_pod(items):
    return find(items, "StatefulSet", "fabric-stamp")["spec"]["template"]["spec"]


def agent_args(items):
    return next(c for c in stamp_pod(items)["containers"] if c["name"] == "agent")["args"]


class ServingMetricsChartTest(unittest.TestCase):
    def test_on_by_default_reading_the_gateway_and_listing_model_hosts(self):
        items = resources()
        args = agent_args(items)
        self.assertIn("--report-metrics", args)
        self.assertIn("--metrics-interval=30s", args)
        self.assertIn("--gateway-metrics-url=http://127.0.0.1:8080/metrics", args)
        self.assertFalse(any(arg.startswith("--dcgm-service") for arg in args))

        role = find(items, "Role", "-agent-metrics")
        self.assertEqual(role["rules"], [{"apiGroups": [""], "resources": ["pods"], "verbs": ["list"]}])
        binding = find(items, "RoleBinding", "-agent-metrics")
        self.assertEqual(binding["roleRef"]["name"], role["metadata"]["name"])
        self.assertEqual(binding["subjects"][0]["namespace"], "fabric-stamp")
        # Nothing cluster-wide, and no exporter unless asked for.
        cluster_roles = [i["metadata"]["name"] for i in items if i["kind"] == "ClusterRole"]
        self.assertFalse(any("metrics" in name or "dcgm" in name for name in cluster_roles))
        self.assertIsNone(find(items, "DaemonSet", "-dcgm-exporter"))
        self.assertIsNone(find(items, "Role", "-agent-dcgm"))

    def test_without_managed_hosts_only_the_gateway_is_read_and_no_kubernetes_access(self):
        items = resources(managed=False)
        self.assertIn("--report-metrics", agent_args(items))
        self.assertIsNone(find(items, "Role", "-agent-metrics"))
        self.assertFalse(stamp_pod(items)["automountServiceAccountToken"])

    def test_opt_out_renders_nothing(self):
        items = resources({"agent.reportMetrics": "false", "gpuMetrics.dcgmExporter.enabled": "true",
                           "gpuMetrics.external.enabled": "true"})
        self.assertNotIn("--report-metrics", agent_args(items))
        self.assertIsNone(find(items, "Role", "-agent-metrics"))
        self.assertIsNone(find(items, "Role", "-agent-dcgm"))
        self.assertIsNone(find(items, "DaemonSet", "-dcgm-exporter"))

    def test_an_existing_exporter_is_read_through_one_named_endpoints_object(self):
        items = resources({"gpuMetrics.external.enabled": "true"})
        self.assertIn("--dcgm-service=fabric-observability/dcgm-dcgm-exporter", agent_args(items))
        role = find(items, "Role", "-agent-dcgm")
        self.assertEqual(role["metadata"]["namespace"], "fabric-observability")
        self.assertEqual(role["rules"], [{
            "apiGroups": [""], "resources": ["endpoints"], "verbs": ["get"],
            "resourceNames": ["dcgm-dcgm-exporter"],
        }])
        binding = find(items, "RoleBinding", "-agent-dcgm")
        self.assertEqual(binding["metadata"]["namespace"], "fabric-observability")
        self.assertEqual(binding["subjects"], [{
            "kind": "ServiceAccount", "name": stamp_pod(items)["serviceAccountName"],
            "namespace": "fabric-stamp",
        }])

    def test_the_bundled_exporter_runs_on_gpu_nodes_without_claiming_a_gpu(self):
        items = resources({
            "gpuMetrics.dcgmExporter.enabled": "true",
            "gpu.runtimeClassName": "nvidia",
            "gpu.nodeSelector.accelerator": "nvidia",
            "gpu.tolerations[0].key": "sku",
            "gpu.tolerations[0].operator": "Exists",
        })
        daemonset = find(items, "DaemonSet", "-dcgm-exporter")
        pod = daemonset["spec"]["template"]["spec"]
        self.assertEqual(pod["runtimeClassName"], "nvidia")
        self.assertEqual(pod["nodeSelector"], {"accelerator": "nvidia"})
        self.assertEqual(pod["tolerations"][0]["key"], "sku")
        self.assertFalse(pod["automountServiceAccountToken"])
        container = pod["containers"][0]
        self.assertNotIn("nvidia.com/gpu", str(container["resources"]))
        self.assertFalse(container["securityContext"].get("privileged", False))
        self.assertNotIn("volumes", pod)  # pod attribution is opt-in

        service = find(items, "Service", "-dcgm-exporter")
        self.assertEqual(service["spec"]["clusterIP"], "None")
        name = service["metadata"]["name"]
        self.assertIn(f"--dcgm-service=fabric-stamp/{name}", agent_args(items))
        role = find(items, "Role", "-agent-metrics")
        self.assertIn({"apiGroups": [""], "resources": ["endpoints"], "verbs": ["get"],
                       "resourceNames": [name]}, role["rules"])

    def test_pod_attribution_mounts_the_pod_resources_socket_read_only(self):
        items = resources({"gpuMetrics.dcgmExporter.enabled": "true",
                           "gpuMetrics.dcgmExporter.podAttribution": "true"})
        pod = find(items, "DaemonSet", "-dcgm-exporter")["spec"]["template"]["spec"]
        self.assertEqual(pod["volumes"][0]["hostPath"]["path"], "/var/lib/kubelet/pod-resources")
        mount = pod["containers"][0]["volumeMounts"][0]
        self.assertTrue(mount["readOnly"])
        env = {item["name"]: item.get("value") for item in pod["containers"][0]["env"]}
        self.assertEqual(env["DCGM_EXPORTER_KUBERNETES"], "true")


if __name__ == "__main__":
    unittest.main()
