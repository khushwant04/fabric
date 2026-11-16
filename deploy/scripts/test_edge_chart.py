#!/usr/bin/env python3
"""Render the edge gateway (ADR 0021) from the control-plane chart and check its shape."""

import pathlib
import subprocess
import unittest

import yaml

CHART = pathlib.Path(__file__).resolve().parents[1] / "helm/fabric-control-plane"
BASE_VALUES = {
    "database.existingSecret": "database",
    "signingKey.existingSecret": "signing",
    "credentialPepperExistingSecret": "pepper",
    "jwt.issuer": "https://control.example.com",
    "auth0.issuer": "https://identity.example.com/",
    "auth0.audience": "fabric-control",
}
EDGE_VALUES = {
    "edge.enabled": "true",
    "edge.credential.existingSecret": "edge-credential",
}


def render(values=None):
    command = ["helm", "template", "fabric", str(CHART)]
    for key, value in (BASE_VALUES | (values or {})).items():
        command.extend(["--set", f"{key}={value}"])
    return subprocess.run(command, capture_output=True, text=True, check=False)


def resources(values=None):
    result = render(values)
    if result.returncode:
        raise AssertionError(result.stderr)
    return [item for item in yaml.safe_load_all(result.stdout) if item]


def edge_items(values=None):
    return [
        item
        for item in resources(EDGE_VALUES | (values or {}))
        if item["metadata"].get("labels", {}).get("app.kubernetes.io/component") == "edge"
    ]


def by_kind(items, kind):
    return [item for item in items if item["kind"] == kind]


class EdgeChartTest(unittest.TestCase):
    def test_disabled_by_default(self):
        items = resources()
        self.assertFalse(any(
            item["metadata"].get("labels", {}).get("app.kubernetes.io/component") == "edge"
            for item in items
        ))

    def test_requires_a_credential_secret(self):
        result = render({"edge.enabled": "true"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("create-edge-credential", result.stderr)

    def test_workload_reads_the_credential_from_a_secret_file(self):
        [deployment] = by_kind(edge_items(), "Deployment")
        pod = deployment["spec"]["template"]["spec"]
        [container] = pod["containers"]
        self.assertEqual(container["command"], ["python", "-m", "fabric_data_plane.edge"])
        env = {item["name"]: item for item in container["env"]}
        self.assertEqual(
            env["FABRIC_EDGE_API_KEY_FILE"]["value"], "/etc/fabric/edge/api-key"
        )
        self.assertNotIn("FABRIC_EDGE_API_KEY", env)
        self.assertEqual(env["FABRIC_EDGE_JWT_ISSUER"]["value"], "https://control.example.com")
        self.assertEqual(
            env["FABRIC_EDGE_CONTROL_PLANE_URL"]["value"],
            "http://fabric-fabric-control-plane:80",
        )
        volumes = {volume["name"]: volume for volume in pod["volumes"]}
        self.assertEqual(volumes["credential"]["secret"]["secretName"], "edge-credential")
        # No persistence: the cached table lives in an emptyDir.
        self.assertIn("emptyDir", volumes["table-cache"])
        self.assertEqual(container["readinessProbe"]["httpGet"]["path"], "/readyz")
        self.assertFalse(pod["automountServiceAccountToken"])

    def test_service_publishes_inference_only_and_does_not_select_the_control_plane(self):
        items = resources(EDGE_VALUES)
        services = by_kind(items, "Service")
        edge = next(s for s in services if s["metadata"]["name"].endswith("-edge"))
        self.assertEqual([port["targetPort"] for port in edge["spec"]["ports"]], ["http"])
        self.assertEqual(edge["spec"]["selector"]["app.kubernetes.io/name"], "fabric-edge")
        control = next(s for s in services if s["metadata"]["name"] == "fabric-fabric-control-plane")
        self.assertNotEqual(control["spec"]["selector"]["app.kubernetes.io/name"], "fabric-edge")

    def test_ingress_with_tls_and_console_default(self):
        values = {
            "edge.ingress.enabled": "true",
            "edge.ingress.host": "inference.example.com",
            "edge.ingress.tls.secretName": "edge-tls",
            "console.enabled": "true",
            "console.baseUrl": "https://console.example.com",
            "console.auth.existingSecret": "console-auth",
        }
        items = resources(EDGE_VALUES | values)
        ingress = next(
            item for item in by_kind(items, "Ingress") if item["metadata"]["name"].endswith("-edge")
        )
        self.assertEqual(ingress["spec"]["tls"][0]["secretName"], "edge-tls")
        console = next(
            item for item in by_kind(items, "Deployment")
            if item["metadata"]["labels"].get("app.kubernetes.io/component") == "console"
        )
        env = {item["name"]: item.get("value") for item in
               console["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env["FABRIC_INFERENCE_URL"], "https://inference.example.com")

    def test_persistence_and_its_replica_guard(self):
        items = edge_items({"edge.persistence.enabled": "true", "edge.replicas": "1"})
        [claim] = by_kind(items, "PersistentVolumeClaim")
        self.assertEqual(claim["spec"]["accessModes"], ["ReadWriteOnce"])
        refused = render(EDGE_VALUES | {"edge.persistence.enabled": "true"})
        self.assertNotEqual(refused.returncode, 0)

    def test_stamp_ca_bundle_and_overrides(self):
        [deployment] = by_kind(
            edge_items({
                "edge.stampCaBundle.configMap": "stamp-ca",
                "edge.stampAddressOverrides.stamp\\.example\\.com:443": "10.0.0.5:443",
            }),
            "Deployment",
        )
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        env = {item["name"]: item.get("value") for item in container["env"]}
        self.assertEqual(env["FABRIC_EDGE_STAMP_CA_BUNDLE"], "/etc/fabric/stamp-ca/ca.crt")
        self.assertEqual(
            env["FABRIC_EDGE_STAMP_ADDRESS_OVERRIDES"], '{"stamp.example.com:443":"10.0.0.5:443"}'
        )


if __name__ == "__main__":
    unittest.main()
