#!/usr/bin/env python3
"""Assert shared cache configuration, isolation, and control-plane rollouts."""

import pathlib
import subprocess
import unittest

import yaml

CHART = pathlib.Path(__file__).resolve().parents[1] / "helm/fabric-control-plane"


def render(values=None):
    merged = {
        "database.existingSecret": "database",
        "signingKey.existingSecret": "signing",
        "credentialPepperExistingSecret": "pepper",
        "jwt.issuer": "https://control.example.com",
        "auth0.issuer": "https://identity.example.com/",
        "auth0.audience": "fabric-control",
    } | (values or {})
    command = ["helm", "template", "fabric", str(CHART)]
    for key, value in merged.items():
        command.extend(["--set", f"{key}={value}"])
    return subprocess.run(command, capture_output=True, text=True, check=False)


def resources(values=None):
    result = render(values)
    if result.returncode:
        raise AssertionError(result.stderr)
    return [item for item in yaml.safe_load_all(result.stdout) if item]


def cp_deployment(values=None):
    return next(item for item in resources(values) if item["kind"] == "Deployment"
                and item["metadata"]["name"] == "fabric-fabric-control-plane")


class CacheChartTest(unittest.TestCase):
    def test_cache_optional_and_external_cache_has_no_internal_workload(self):
        for values in [{}, {"cache.existingSecret": "external-cache"}]:
            rendered = resources(values)
            self.assertFalse(any(item["metadata"]["name"].endswith("-cache")
                                 for item in rendered))

    def test_cache_requires_secret_and_only_control_plane_pods_can_reach_it(self):
        rejected = render({"cache.internal.enabled": "true"})
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("cache.existingSecret is required", rejected.stderr)
        rendered = resources({"cache.internal.enabled": "true", "cache.existingSecret": "cache"})
        service = next(item for item in rendered if item["kind"] == "Service"
                       and item["metadata"]["name"].endswith("-cache"))
        self.assertEqual(service["spec"]["type"], "ClusterIP")
        deployment = next(item for item in rendered if item["kind"] == "Deployment"
                          and item["metadata"]["name"].endswith("-cache"))
        self.assertEqual(deployment["spec"]["replicas"], 1)
        pod = deployment["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertTrue(pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"])
        self.assertIn("--appendonly no", pod["containers"][0]["args"][0])
        self.assertIn("--maxmemory 192mb", pod["containers"][0]["args"][0])
        policy = next(item for item in rendered if item["kind"] == "NetworkPolicy"
                      and item["metadata"]["name"].endswith("-cache"))
        self.assertEqual(policy["spec"]["ingress"][0]["from"], [{"podSelector": {
            "matchLabels": {"app.kubernetes.io/name": "fabric-control-plane",
                            "app.kubernetes.io/instance": "fabric"}}}])

    def test_limits_are_runtime_configuration_and_changes_roll_the_control_plane(self):
        values = {"cache.existingSecret": "cache"}
        deployment = cp_deployment(values)
        env = {item["name"]: item for item in
               deployment["spec"]["template"]["spec"]["containers"][0]["env"]}
        self.assertEqual(env["FABRIC_REDIS_URL"]["valueFrom"]["secretKeyRef"],
                         {"name": "cache", "key": "redis-url"})
        for name, expected in {
            "FABRIC_API_CACHE_TTL_SECONDS": "60", "FABRIC_API_CACHE_LIVE_TTL_SECONDS": "3",
            "FABRIC_API_CACHE_TIMEOUT_SECONDS": "0.2", "FABRIC_API_CACHE_MAX_CONNECTIONS": "32",
            "FABRIC_API_CACHE_MAX_VALUE_BYTES": "1048576",
        }.items():
            self.assertEqual(env[name]["value"], expected)
        baseline = deployment["spec"]["template"]["metadata"]["annotations"]["checksum/config"]
        changed = cp_deployment(values | {"cache.liveTtlSeconds": "2"})
        self.assertNotEqual(baseline,
                            changed["spec"]["template"]["metadata"]["annotations"]["checksum/config"])


if __name__ == "__main__":
    unittest.main()
