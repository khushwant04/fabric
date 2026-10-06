"""Exercise release upgrades with real chart rendering and recorded cluster commands."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]

COMMAND_RECORDER = r'''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys

args = sys.argv[1:]
tool = pathlib.Path(sys.argv[0]).name
entry = {"tool": tool, "args": args}
record = pathlib.Path(os.environ["UPGRADE_TEST_RECORD"])
def save():
    with record.open("a") as target:
        target.write(json.dumps(entry) + "\n")
if tool == "helm" and args[:2] == ["get", "values"]:
    save()
    if os.environ.get("UPGRADE_TEST_MISSING_CP") and args[2] == "cp":
        sys.stderr.write("release not found\n")
        sys.exit(1)
    print(pathlib.Path(os.environ["UPGRADE_TEST_CP" if args[2] == "cp" else "UPGRADE_TEST_STAMP"]).read_text())
elif tool == "helm" and args[0] == "template":
    save()
    sys.exit(subprocess.run([os.environ["UPGRADE_TEST_HELM"], *args]).returncode)
elif tool == "helm" and args[0] == "upgrade":
    assert "--reset-values" in args, "upgrade must use current chart defaults"
    assert "--reuse-values" not in args, "old chart defaults must not be reused"
    values = pathlib.Path(args[args.index("-f") + 1])
    entry["values_mode"] = values.stat().st_mode & 0o777
    assert entry["values_mode"] == 0o600
    rendered_args = ["template", args[1], args[2]]
    index = 3
    while index < len(args):
        item = args[index]
        if item in ["-n", "-f", "--set", "--set-string"]:
            rendered_args.extend(args[index:index + 2])
            index += 2
        elif item == "--timeout":
            index += 2
        elif item in ["--reset-values", "--atomic", "--wait"]:
            index += 1
        else:
            raise AssertionError("unexpected Helm upgrade option: " + item)
    rendered = subprocess.run([os.environ["UPGRADE_TEST_HELM"], *rendered_args], text=True, capture_output=True)
    if rendered.returncode:
        sys.stderr.write(rendered.stderr)
        sys.exit(rendered.returncode)
    output = record.parent / ("rendered-" + str(len(record.read_text().splitlines())) + ".yaml")
    output.write_text(rendered.stdout)
    entry["rendered"] = str(output)
    save()
elif tool == "kubectl":
    if args[0] == "apply":
        entry["received_manifest"] = bool(sys.stdin.read())
    save()
    if args[0] == "get":
        model = {"metadata": {"name": "qwen"}, "spec": {"jwtIssuer": "https://control.company.test", "jwksUrl": "https://control.company.test/.well-known/jwks.json"}}
        count = os.environ.get("UPGRADE_TEST_MODELS", "one")
        if count == "invalid":
            model["spec"].pop("jwksUrl")
        print(json.dumps({"items": [] if count == "empty" else [model]}))
    elif args[0] == "exec":
        print(json.dumps({"source": "synced", "rejected_updates": 0}))
else:
    raise AssertionError("unexpected command " + tool + " " + repr(args))
'''


class UpgradeAutomationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="fabric-upgrade-test-")
        self.addCleanup(self.temp.cleanup)
        self.work = pathlib.Path(self.temp.name)
        commands = self.work / "commands"
        commands.mkdir()
        for tool in ["helm", "kubectl"]:
            executable = commands / tool
            executable.write_text(COMMAND_RECORDER)
            executable.chmod(0o700)
        self.cp = {
            "image": {"repository": "private.registry/control-plane", "digest": "sha256:" + "a" * 64},
            "database": {"existingSecret": "database-credentials"},
            "signingKey": {"existingSecret": "signing-key"},
            "credentialPepper": "private-upgrade-test-credential",
            "jwt": {"issuer": "https://control.company.test"},
            "auth0": {"issuer": "https://identity.company.test", "audience": "fabric"},
            "replicas": 3,
        }
        self.stamp = {
            "image": {
                "agent": {"repository": "private.registry/agent", "digest": "sha256:" + "a" * 64},
                "dataPlane": {"repository": "private.registry/data-plane", "digest": "sha256:" + "b" * 64},
            },
            "controlPlane": {"url": "https://control.company.test", "jwtIssuer": "https://control.company.test"},
            "enrollment": {"token": "private-upgrade-test-enrollment"},
            "operator": {"enabled": True, "managedModelHost": {"image": "private.registry/vllm@sha256:" + "a" * 64}},
            "gpu": {"nodeSelector": {"agentpool": "gpupool"}},
        }
        self.env = {
            **os.environ,
            "PATH": str(commands) + os.pathsep + os.environ["PATH"],
            "UPGRADE_TEST_HELM": shutil.which("helm") or "helm",
            "UPGRADE_TEST_RECORD": str(self.work / "record.jsonl"),
            "UPGRADE_TEST_CP": str(self.work / "control.yaml"),
            "UPGRADE_TEST_STAMP": str(self.work / "stamp.yaml"),
            "CONTROL_PLANE_DIGEST": "sha256:" + "1" * 64,
            "AGENT_DIGEST": "sha256:" + "2" * 64,
            "DATA_PLANE_DIGEST": "sha256:" + "3" * 64,
            "CONSOLE_DIGEST": "sha256:" + "4" * 64,
            "CP_RELEASE": "cp", "CP_NAMESPACE": "fabric-control",
            "STAMP_RELEASE": "st", "STAMP_NAMESPACE": "fabric-stamp",
        }

    def run_upgrade(self):
        pathlib.Path(self.env["UPGRADE_TEST_CP"]).write_text(yaml.safe_dump(self.cp))
        pathlib.Path(self.env["UPGRADE_TEST_STAMP"]).write_text(yaml.safe_dump(self.stamp))
        result = subprocess.run(["bash", str(ROOT / "deploy/scripts/deploy-azure.sh")], env=self.env, text=True, capture_output=True)
        for private_value in ["private-upgrade-test-credential", "private-upgrade-test-enrollment"]:
            self.assertNotIn(private_value, result.stdout + result.stderr)
        record = pathlib.Path(self.env["UPGRADE_TEST_RECORD"])
        entries = [json.loads(line) for line in record.read_text().splitlines()] if record.exists() else []
        for entry in entries:
            if entry["tool"] == "helm" and entry["args"][0] == "upgrade":
                self.assertFalse(pathlib.Path(entry["args"][entry["args"].index("-f") + 1]).exists(), "temporary credential values must be removed")
        return result, entries

    def upgrades(self, entries):
        return [entry for entry in entries if entry["tool"] == "helm" and entry["args"][0] == "upgrade"]

    def resources(self, upgrade):
        return [item for item in yaml.safe_load_all(pathlib.Path(upgrade["rendered"]).read_text()) if item]

    def test_legacy_values_get_new_defaults_without_configuration_loss(self):
        result, entries = self.run_upgrade()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([entry["args"][:3] for entry in entries[:2]], [["get", "values", "cp"], ["get", "values", "st"]])
        upgrades = self.upgrades(entries)
        self.assertEqual([entry["args"][1] for entry in upgrades], ["cp", "st", "st"])
        cp_resources = self.resources(upgrades[0])
        cp = next(item for item in cp_resources if item["kind"] == "Deployment")
        self.assertEqual(cp["spec"]["replicas"], 3)
        self.assertEqual(cp["spec"]["template"]["spec"]["containers"][0]["image"], "private.registry/control-plane@" + self.env["CONTROL_PLANE_DIGEST"])
        self.assertFalse(any(item["metadata"]["name"].endswith("-console") for item in cp_resources))
        for index, upgrade in enumerate(upgrades[1:]):
            resources = self.resources(upgrade)
            pod = next(item for item in resources if item["kind"] == "StatefulSet")["spec"]["template"]["spec"]
            dp = next(item for item in pod["containers"] if item["name"] == "data-plane")
            expected_digest = "sha256:" + "b" * 64 if index == 0 else self.env["DATA_PLANE_DIGEST"]
            self.assertEqual(dp["image"], "private.registry/data-plane@" + expected_digest)
            agent = next(item for item in pod["containers"] if item["name"] == "agent")
            enrollment = next(item for item in agent["env"] if item["name"] == "FABRIC_AGENT_ENROLLMENT_TOKEN")
            self.assertEqual(enrollment["valueFrom"]["secretKeyRef"]["name"], "st-fabric-stamp-enrollment")
            self.assertFalse(any(item["kind"] == "Secret" and item["metadata"]["name"].endswith("-enrollment") for item in resources))
            self.assertTrue(any(item["kind"] == "ServiceAccount" and item["metadata"]["name"].endswith("-model-host") for item in resources))

    def test_empty_enrolled_stamp_can_upgrade(self):
        self.env["UPGRADE_TEST_MODELS"] = "empty"
        result, entries = self.run_upgrade()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.upgrades(entries)), 3)

    def test_existing_optional_settings_and_secret_references_survive(self):
        self.cp["singleTenant"] = {"enabled": True, "accountId": "12345678-1234-5678-9234-567812345678", "adminSubject": "auth0|owner"}
        self.cp["console"] = {"enabled": True, "baseUrl": "https://console.company.test", "auth": {"existingSecret": "console-auth"}, "image": {"repository": "private.registry/console"}}
        self.cp["cache"] = {"existingSecret": "cache-url"}
        self.stamp["enrollment"] = {"existingSecret": "retained-enrollment"}
        self.stamp["exposure"] = {"enabled": True, "host": "gpu.company.test", "tls": {"secretName": "inference-tls"}}
        result, entries = self.run_upgrade()
        self.assertEqual(result.returncode, 0, result.stderr)
        resources = self.resources(self.upgrades(entries)[0])
        console = next(item for item in resources if item["kind"] == "Deployment" and item["metadata"]["name"].endswith("-console"))
        self.assertEqual(console["spec"]["template"]["spec"]["containers"][0]["image"], "private.registry/console@" + self.env["CONSOLE_DIGEST"])
        self.assertTrue(any(item["kind"] == "Ingress" for item in self.resources(self.upgrades(entries)[2])))

    def test_missing_release_fails_before_cluster_changes(self):
        self.env["UPGRADE_TEST_MISSING_CP"] = "yes"
        result, entries = self.run_upgrade()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(entries), 1)
        self.assertFalse(self.upgrades(entries))

    def test_missing_verification_fields_prevent_data_plane_upgrade(self):
        self.env["UPGRADE_TEST_MODELS"] = "invalid"
        result, entries = self.run_upgrade()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.upgrades(entries)), 2)
        self.assertIn("missing HTTPS verification contract", result.stderr)


if __name__ == "__main__":
    unittest.main()
