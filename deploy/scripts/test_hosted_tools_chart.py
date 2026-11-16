#!/usr/bin/env python3
"""Render the stamp chart's hosted tools (docs/hosted-tools.md) and check their shape."""

import pathlib
import subprocess
import unittest

import yaml

CHART = pathlib.Path(__file__).resolve().parents[1] / "helm/fabric-stamp"
BASE_VALUES = {
    "controlPlane.url": "https://control.example.com",
    "controlPlane.jwtIssuer": "https://control.example.com",
    "enrollment.existingSecret": "fabric-enrollment",
    "modelHost.url": "http://model-host:8000",
}
SANDBOX = {"hostedTools.codeInterpreter.enabled": "true"}


def render(values=None):
    command = ["helm", "template", "fab", str(CHART)]
    for key, value in (BASE_VALUES | (values or {})).items():
        command.extend(["--set", f"{key}={value}"])
    return subprocess.run(command, capture_output=True, text=True, check=False)


def resources(values=None):
    result = render(values)
    if result.returncode:
        raise AssertionError(result.stderr)
    return [item for item in yaml.safe_load_all(result.stdout) if item]


def sandbox_items(values=None):
    return [
        item
        for item in resources(values)
        if item["metadata"].get("labels", {}).get("app.kubernetes.io/component") == "sandbox"
    ]


def one(items, kind):
    [item] = [item for item in items if item["kind"] == kind]
    return item


def gateway_env(values=None):
    sts = one(resources(values), "StatefulSet")
    [container] = [
        c for c in sts["spec"]["template"]["spec"]["containers"] if c["name"] == "data-plane"
    ]
    return {item["name"]: item for item in container["env"]}


class HostedToolsChartTest(unittest.TestCase):
    def test_disabled_by_default(self):
        self.assertEqual(sandbox_items(), [])
        env = gateway_env()
        self.assertFalse(any(name.startswith("FABRIC_DP_CODE_INTERPRETER") for name in env))
        self.assertNotIn("FABRIC_DP_WEB_FETCH_ENABLED", env)

    def test_sandbox_workload_is_locked_down(self):
        items = sandbox_items(SANDBOX | {"sandbox.runtimeClassName": "gvisor"})
        deployment = one(items, "Deployment")
        pod = deployment["spec"]["template"]["spec"]
        self.assertEqual(pod["runtimeClassName"], "gvisor")
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertEqual(pod["securityContext"]["seccompProfile"]["type"], "RuntimeDefault")
        [container] = pod["containers"]
        security = container["securityContext"]
        self.assertTrue(security["readOnlyRootFilesystem"])
        self.assertFalse(security["allowPrivilegeEscalation"])
        self.assertEqual(security["capabilities"]["drop"], ["ALL"])
        self.assertIn("limits", container["resources"])
        [volume] = pod["volumes"]
        self.assertEqual(volume["emptyDir"]["sizeLimit"], "2Gi")
        env = {item["name"]: item for item in container["env"]}
        self.assertEqual(
            env["FABRIC_SANDBOX_TOKEN"]["valueFrom"]["secretKeyRef"]["name"],
            "fab-fabric-stamp-sandbox",
        )
        # Sandbox pods never match the stamp's selectors.
        labels = deployment["spec"]["template"]["metadata"]["labels"]
        self.assertEqual(labels["app.kubernetes.io/name"], "fabric-stamp-sandbox")

    def test_network_policy_denies_egress_and_admits_only_the_gateway(self):
        policy = one(sandbox_items(SANDBOX), "NetworkPolicy")
        spec = policy["spec"]
        self.assertEqual(sorted(spec["policyTypes"]), ["Egress", "Ingress"])
        self.assertEqual(spec["egress"], [])
        [rule] = spec["ingress"]
        [source] = rule["from"]
        self.assertEqual(
            source["podSelector"]["matchLabels"]["app.kubernetes.io/component"], "stamp"
        )
        self.assertEqual(rule["ports"], [{"port": "sandbox", "protocol": "TCP"}])

    def test_gateway_is_wired_to_the_sandbox_secret(self):
        env = gateway_env(SANDBOX)
        self.assertEqual(env["FABRIC_DP_CODE_INTERPRETER_ENABLED"]["value"], "true")
        self.assertEqual(env["FABRIC_DP_CODE_INTERPRETER_BACKEND"]["value"], "sandbox")
        self.assertEqual(
            env["FABRIC_DP_CODE_INTERPRETER_URL"]["value"], "http://fab-fabric-stamp-sandbox:8090"
        )
        ref = env["FABRIC_DP_CODE_INTERPRETER_TOKEN"]["valueFrom"]["secretKeyRef"]
        self.assertEqual(ref, {"name": "fab-fabric-stamp-sandbox", "key": "token"})
        secret = one(sandbox_items(SANDBOX), "Secret")
        self.assertGreaterEqual(len(secret["stringData"]["token"]), 32)

    def test_e2b_backend_reads_the_key_from_a_secret_and_deploys_no_sandbox(self):
        values = SANDBOX | {
            "hostedTools.codeInterpreter.backend": "e2b",
            "hostedTools.codeInterpreter.e2b.existingSecret": "e2b",
            "hostedTools.codeInterpreter.e2b.domain": "e2b.internal.example",
        }
        self.assertEqual(sandbox_items(values), [])
        env = gateway_env(values)
        self.assertEqual(
            env["FABRIC_DP_E2B_API_KEY"]["valueFrom"]["secretKeyRef"],
            {"name": "e2b", "key": "api-key"},
        )
        self.assertNotIn("value", env["FABRIC_DP_E2B_API_KEY"])
        self.assertEqual(env["FABRIC_DP_E2B_DOMAIN"]["value"], "e2b.internal.example")
        self.assertEqual(env["FABRIC_DP_E2B_ALLOW_INTERNET"]["value"], "false")
        self.assertNotIn("FABRIC_DP_CODE_INTERPRETER_URL", env)

    def test_e2b_without_a_secret_is_refused(self):
        result = render(SANDBOX | {"hostedTools.codeInterpreter.backend": "e2b"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("existingSecret", result.stderr)

    def test_web_tools_with_firecrawl(self):
        values = {
            "hostedTools.webFetch.enabled": "true",
            "hostedTools.webFetch.allowedHosts": "*",
            "hostedTools.webSearch.enabled": "true",
            "hostedTools.web.backend": "firecrawl",
            "hostedTools.web.firecrawl.url": "http://firecrawl.tools.svc:3002",
            "hostedTools.web.firecrawl.existingSecret": "firecrawl",
        }
        env = gateway_env(values)
        self.assertEqual(env["FABRIC_DP_WEB_BACKEND"]["value"], "firecrawl")
        self.assertEqual(env["FABRIC_DP_WEB_FETCH_ALLOWED_HOSTS"]["value"], "*")
        self.assertEqual(
            env["FABRIC_DP_FIRECRAWL_URL"]["value"], "http://firecrawl.tools.svc:3002"
        )
        self.assertEqual(
            env["FABRIC_DP_FIRECRAWL_API_KEY"]["valueFrom"]["secretKeyRef"]["name"], "firecrawl"
        )

    def test_web_misconfiguration_fails_rendering(self):
        cases = [
            ({"hostedTools.webFetch.enabled": "true"}, "allowedHosts"),
            ({"hostedTools.webSearch.enabled": "true"}, "searchUrl"),
            (
                {
                    "hostedTools.webSearch.enabled": "true",
                    "hostedTools.web.backend": "firecrawl",
                },
                "firecrawl.url",
            ),
            ({"hostedTools.web.backend": "bing"}, "web.backend"),
        ]
        for values, message in cases:
            with self.subTest(values=values):
                result = render(values)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_searxng(self):
        env = gateway_env(
            {
                "hostedTools.webSearch.enabled": "true",
                "hostedTools.web.backend": "searxng",
                "hostedTools.web.searchUrl": "http://searxng.tools.svc:8080",
            }
        )
        self.assertEqual(env["FABRIC_DP_WEB_SEARCH_URL"]["value"], "http://searxng.tools.svc:8080")
        self.assertEqual(env["FABRIC_DP_WEB_BACKEND"]["value"], "searxng")


if __name__ == "__main__":
    unittest.main()
