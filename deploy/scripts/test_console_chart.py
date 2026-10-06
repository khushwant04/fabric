#!/usr/bin/env python3
"""Render the console Helm workload and check its runtime configuration."""

import pathlib
import subprocess
import unittest

import yaml


CHART = pathlib.Path(__file__).resolve().parents[1] / "helm/fabric-control-plane"
ACCOUNT_ID = "4c0a0d13-70e5-4a98-9a61-0a278fdc831c"
BASE_VALUES = {
    "database.existingSecret": "database",
    "signingKey.existingSecret": "signing",
    "credentialPepperExistingSecret": "pepper",
    "jwt.issuer": "https://control.example.com",
    "auth0.issuer": "https://identity.example.com/",
    "auth0.audience": "fabric-control",
}
CONSOLE_VALUES = {
    "console.enabled": "true",
    "console.baseUrl": "https://console.example.com",
    "console.auth.existingSecret": "console-auth",
}


def render(values=None):
    merged = BASE_VALUES | (values or {})
    command = ["helm", "template", "fabric", str(CHART)]
    for key, value in merged.items():
        command.extend(["--set", f"{key}={value}"])
    return subprocess.run(command, capture_output=True, text=True, check=False)


def console_deployment(values=None):
    result = render(CONSOLE_VALUES | (values or {}))
    if result.returncode:
        raise AssertionError(result.stderr)
    resources = list(yaml.safe_load_all(result.stdout))
    return next(
        item for item in resources
        if item and item.get("kind") == "Deployment"
        and item["metadata"]["labels"].get("app.kubernetes.io/component") == "console"
    )


class ConsoleChartTest(unittest.TestCase):
    def test_console_is_optional(self):
        result = render()
        self.assertEqual(result.returncode, 0, result.stderr)
        resources = [item for item in yaml.safe_load_all(result.stdout) if item]
        self.assertFalse(any(
            item.get("metadata", {}).get("labels", {}).get("app.kubernetes.io/component") == "console"
            for item in resources
        ))

    def test_runtime_uses_secret_references_and_internal_control_plane(self):
        deployment = console_deployment()
        pod = deployment["spec"]["template"]["spec"]
        container = pod["containers"][0]
        env = {item["name"]: item for item in container["env"]}
        self.assertEqual(env["APP_BASE_URL"]["value"], "https://console.example.com")
        self.assertEqual(env["FABRIC_CONTROL_PLANE_URL"]["value"], "http://fabric-fabric-control-plane:80")
        for name in ["AUTH0_DOMAIN", "AUTH0_CLIENT_ID", "AUTH0_CLIENT_SECRET", "AUTH0_SECRET", "AUTH0_AUDIENCE"]:
            self.assertEqual(env[name]["valueFrom"]["secretKeyRef"]["name"], "console-auth")
            self.assertNotIn("value", env[name])
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertEqual(container["readinessProbe"]["httpGet"]["path"], "/healthz")

    def test_single_tenant_and_domain_allowlist_reach_the_console(self):
        deployment = console_deployment({
            "singleTenant.enabled": "true",
            "singleTenant.accountId": ACCOUNT_ID,
            "singleTenant.adminSubject": "auth0|administrator",
            "console.inference.allowedDomains": "{inference.example.com,models.example.com}",
        })
        env = {item["name"]: item for item in deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env["FABRIC_SINGLE_TENANT_ACCOUNT_ID"]["value"], ACCOUNT_ID)
        self.assertEqual(env["FABRIC_INFERENCE_ALLOWED_DOMAINS"]["value"], "inference.example.com,models.example.com")

    def test_gateway_and_secret_configuration_changes_trigger_rollouts(self):
        def checksum(values=None):
            return console_deployment(values)["spec"]["template"]["metadata"]["annotations"]["checksum/config"]

        baseline = checksum()
        for values in [
            {"console.inference.allowedDomains": "{inference.example.com}"},
            {"console.inferenceAllowedOrigins": "{https://inference.example.com}"},
            {"console.auth.existingSecret": "other-console-auth"},
            {"console.auth.keys.clientSecret": "new-client-key"},
        ]:
            self.assertNotEqual(checksum(values), baseline)

    def test_missing_authentication_secret_is_rejected(self):
        result = render(CONSOLE_VALUES | {"console.auth.existingSecret": ""})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("console.auth.existingSecret is required", result.stderr)

    def test_callback_origin_rejects_credentials_queries_and_bad_hosts(self):
        for origin in [
            "http://console.example.com", "https://console.example.com/",
            "https://user:password@console.example.com", "https://console.example.com?state=x",
            "https://console.example.com#fragment", "https://console..example.com",
            "https://-console.example.com", "https://console.example.com:0",
            "https://console.example.com:65536",
        ]:
            with self.subTest(origin=origin):
                result = render(CONSOLE_VALUES | {"console.baseUrl": origin})
                self.assertNotEqual(result.returncode, 0)

    def test_port_origin_is_valid_only_without_native_ingress(self):
        console_deployment({"console.baseUrl": "https://console.example.com:8443"})
        result = render(CONSOLE_VALUES | {
            "console.baseUrl": "https://console.example.com:8443",
            "console.ingress.enabled": "true",
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("without a port", result.stderr)


if __name__ == "__main__":
    unittest.main()
