"""Seed a realistic Fabric account through the real APIs, then keep its stamps alive.

Demo tooling only, run by scripts/demo-console/run.sh. Every record is created the way a
customer would create it: people sign in through the (mock) identity provider and call the
control plane with their own tokens; stamps are enrolled by the real Go agent with a
single-use token; the agent pulls desired state and writes the data plane's configuration;
inference goes through the real data plane; the real collector forwards usage. Nothing is
written to the database directly.

Two things stand in for hardware this machine does not have, and both use the same
stamp-authenticated APIs a production stamp uses:

* GPU inventory. The agent measures GPUs from Kubernetes nodes. With no cluster here it
  is started with --gpus N, which reports a count but no device model, so this script
  sends the heartbeat a measuring agent would (product, memory, compute capability) with
  the stamp's own agent credential.
* Workload readiness. In a cluster the operator observes model-host pods and the agent
  forwards that verdict. With no operator the agent can only say it wrote a file, so this
  script reports the operator-style observation (Applied/Available/Progressing) with the
  stamp's agent credential.
* Activity and host logs. The agent runs here with --once, so its activity uploader and
  host reporter never start; this script pushes what they would (POST /v1/stamps/{id}/events
  and /hosts) with each stamp's own agent credential.

After seeding it writes state.json (read by run.sh and the screenshot script) and keeps
running: heartbeats every 30 s so stamps stay "active", and a collector pass every 30 s
so playground requests reach the Usage page.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import random
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

GIB = 1 << 30
MIB = 1 << 20
PASSWORD = "fabric-demo"

PEOPLE = {
    "maya": ("maya.chen@northstar-labs.example", "auth0|demo-maya-chen"),
    "priya": ("priya.raman@northstar-labs.example", "auth0|demo-priya-raman"),
    "daniel": ("daniel.okafor@northstar-labs.example", "auth0|demo-daniel-okafor"),
    "sofia": ("sofia.lindqvist@northstar-labs.example", "auth0|demo-sofia-lindqvist"),
    "tom": ("tom.becker@northstar-labs.example", "auth0|demo-tom-becker"),
}

T4 = {"product": "Tesla T4", "memory_bytes": 15360 * MIB, "compute_capability": "7.5"}
A10 = {"product": "NVIDIA A10", "memory_bytes": 23028 * MIB, "compute_capability": "8.6"}
RTX_PRO_6000 = {"product": "NVIDIA RTX PRO 6000 Blackwell Server Edition",
                "memory_bytes": 97887 * MIB, "compute_capability": "12.0"}

STAMPS = [
    {
        "name": "aks-gpu", "orchestrator": "aks", "orchestrator_version": "1.31.5",
        "region": "eastus2", "per_node": 2, "driver": "550.144.03",
        "gpus": [(T4, 2), (A10, 2)],  # one T4 node pool, one A10 node pool
    },
    {
        "name": "gcp-rtx6000", "orchestrator": "gke", "orchestrator_version": "1.32.2-gke.1182003",
        "region": "us-central1", "per_node": 2, "driver": "575.57.08",
        "gpus": [(RTX_PRO_6000, 2)],
    },
    {
        "name": "vm-gpu", "orchestrator": "k3s", "orchestrator_version": "v1.32.3+k3s1",
        "region": "westeurope", "per_node": 1, "driver": "570.133.20",
        "gpus": [(A10, 1), (T4, 1)],  # two single-GPU VMs
    },
    {
        # A VM bootstrapped with scripts/bootstrap-stamp.sh a few minutes ago: its first
        # deployment is pulling the model runtime image right now.
        "name": "rtx-pro-6000", "orchestrator": "k3s", "orchestrator_version": "v1.36.5+k3s1",
        "region": "asia-southeast1", "per_node": 1, "driver": "580.82.07",
        "gpus": [(RTX_PRO_6000, 1)],
    },
]

# Enrolled, then removed from the console: hidden behind "Show removed (1)".
REMOVED_STAMP = {
    "name": "a10-lab-old", "orchestrator": "k3s", "orchestrator_version": "v1.31.4+k3s1",
    "region": "westeurope", "per_node": 1, "driver": "550.120", "gpus": [(A10, 1)],
}

# name, alias, release, stamp (None = not placed), outcome, extra spec, requests to send.
# Small Qwen3.5 models served by vLLM. cascade_tier orders model="cascade": chat-fast first,
# then support-assistant, then the "-large" alias as the upper tier.
DEPLOYMENTS = [
    ("support-assistant", "qwen3.5-2b", "Qwen/Qwen3.5-2B", "aks-gpu", "ready",
     {"replicas": 2, "resources": {"gpu_class": "t4"},
      "capabilities": {"cascade_tier": 1},
      "runtime": {"max_model_len": 16384, "strategy": "prefix_aware"}}, 140),
    ("chat-fast", "qwen3.5-0.8b", "Qwen/Qwen3.5-0.8B", "aks-gpu", "ready",
     {"resources": {"gpu_class": "t4"}, "capabilities": {"priority": -10, "cascade_tier": 0},
      "runtime": {"max_model_len": 8192}}, 95),
    ("assistant-large", "qwen3.5-2b-large", "Qwen/Qwen3.5-2B", "gcp-rtx6000", "ready",
     {"resources": {"gpu_class": "a10"},
      "capabilities": {"priority": 10, "reasoning": True, "cascade_tier": 2},
      "runtime": {"max_model_len": 65536, "max_num_seqs": 256}}, 60),
    ("code-review-bot", "qwen3.5-2b-code", "Qwen/Qwen3.5-2B", "gcp-rtx6000", "deploying",
     {"resources": {"gpu_class": "a10"}, "capabilities": {"code": True}}, 0),
    ("batch-summarizer", "qwen3.5-0.8b-batch", "Qwen/Qwen3.5-0.8B", "vm-gpu", "updating",
     {"resources": {"gpu_class": "t4"}, "runtime": {"strategy": "round_robin"}}, 45),
    ("long-context-eval", "qwen3.5-2b-longctx", "Qwen/Qwen3.5-2B", "vm-gpu", "failed",
     {"resources": {"gpu_class": "t4"}, "runtime": {"max_model_len": 262144}}, 0),
    ("research-pool", "qwen3.5-2b-research", "Qwen/Qwen3.5-2B", None, "waiting",
     {"replicas": 4, "resources": {"gpu_class": "a10"}}, 0),
    # The first deployment on the freshly bootstrapped machine: no pre-pull, so the Logs tab
    # shows the model runtime image downloading.
    ("docs-qa", "qwen3.5-4b-docs", "Qwen/Qwen3.5-4B", "rtx-pro-6000", "pulling",
     {"resources": {"gpu_class": "rtx-pro-6000"}, "runtime": {"max_model_len": 32768}}, 0),
]

PROMPTS = [
    "Summarize this support ticket in two sentences: customer cannot reset MFA after phone change.",
    "Classify the sentiment of: 'The new release fixed our latency issue, thanks team!'",
    "Write a Python function that validates an IBAN.",
    "Explain the difference between p50 and p99 latency to a product manager.",
    "Draft a polite reply asking the customer for their order number.",
    "Which GPU class does a 2B parameter model need for a 32k context?",
    "Translate to German: 'Your deployment is ready and serving traffic.'",
    "List three risks of running inference outside the customer's network.",
    "Generate a SQL query returning the top 10 accounts by monthly token usage.",
    "Rewrite this paragraph to be more concise: the platform enables teams to deploy...",
]

log_prefix = "   "


def rng_prompt() -> str:
    return random.choice(PROMPTS)


def log(message: str) -> None:
    print(f"{log_prefix}{message}", flush=True)


class Demo:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.work = Path(args.work)
        self.cp = args.control_url.rstrip("/")
        self.idp = args.idp_url.rstrip("/")
        self.http = httpx.Client(timeout=30.0, verify=args.ca)
        self.children: list[subprocess.Popen] = []
        self.stamps: dict[str, dict] = {}
        self.deployments: dict[str, dict] = {}
        self.key_ids: dict[str, str] = {}
        self.routing_policies: dict[str, str] = {}
        self.account_id = ""
        self.token = ""
        self.keys: dict[str, str] = {}
        # Fixed reference times, so a re-sent host report keeps its timeline moving forward.
        self.started = time.time()
        self.removed_deployment_id = ""

    # --- helpers -------------------------------------------------------------------

    def person_token(self, who: str) -> str:
        email, _ = PEOPLE[who]
        response = self.http.post(f"{self.idp}/oauth/token", data={
            "grant_type": "password", "username": email, "password": PASSWORD,
            "client_id": self.args.client_id, "client_secret": self.args.client_secret,
            "audience": self.args.audience, "scope": "openid profile email",
        })
        response.raise_for_status()
        return response.json()["access_token"]

    def call(self, method: str, path: str, *, token: str | None = None, body=None,
             ok=(200, 201, 204)) -> dict | list | None:
        headers = {"Authorization": f"Bearer {token or self.token}"}
        response = self.http.request(method, f"{self.cp}{path}", headers=headers, json=body)
        if response.status_code not in ok:
            raise SystemExit(f"{method} {path} -> {response.status_code}: {response.text}")
        return response.json() if response.content else None

    def exchange(self, assertion: str, account_id: str, audience: str = "fabric-control") -> str:
        response = self.http.post(f"{self.cp}/v1/token", json={
            "grant_type": "auth0_token", "audience": audience,
            "assertion": assertion, "account_id": account_id,
        })
        response.raise_for_status()
        return response.json()["access_token"]

    def api_key_token(self, secret: str, audience: str) -> str:
        response = self.http.post(f"{self.cp}/v1/token", json={
            "grant_type": "api_key", "api_key": secret, "audience": audience,
        })
        response.raise_for_status()
        return response.json()["access_token"]

    def acct(self, suffix: str) -> str:
        return f"/v1/accounts/{self.account_id}{suffix}"

    # --- people, account, access ----------------------------------------------------

    def seed_people_and_account(self) -> None:
        tokens = {who: self.person_token(who) for who in PEOPLE}
        for who, token in tokens.items():
            self.call("GET", "/v1/me", token=token)  # first sign-in records the user
        account = self.call("POST", "/v1/accounts", token=tokens["maya"],
                            body={"slug": "northstar-labs", "name": "Northstar Labs"})
        self.account_id = account["id"]
        # A second workspace so the account switcher has something to switch to.
        research = self.call("POST", "/v1/accounts", token=tokens["priya"],
                             body={"slug": "northstar-research", "name": "Northstar Research"})
        self.maya_assertion = tokens["maya"]
        self.token = self.exchange(tokens["maya"], self.account_id)
        log(f"account Northstar Labs: {self.account_id}")

        for who, role in (("priya", "admin"), ("daniel", "developer"),
                          ("sofia", "developer"), ("tom", "viewer")):
            email, subject = PEOPLE[who]
            self.call("POST", self.acct("/members"),
                      body={"auth0_subject": subject, "email": email, "role": role})
        research_token = self.exchange(tokens["priya"], research["id"])
        email, subject = PEOPLE["maya"]
        self.call("POST", f"/v1/accounts/{research['id']}/members", token=research_token,
                  body={"auth0_subject": subject, "email": email, "role": "developer"})
        log("members: 5 in Northstar Labs, Maya also a developer in Northstar Research")

        principals = {}
        for name in ("ci-deployer", "batch-inference", "eval-harness", "legacy-etl"):
            principals[name] = self.call("POST", self.acct("/service-principals"),
                                         body={"name": name})["id"]
        keys = [
            ("github-actions-deploy", ["deployments:read", "deployments:write", "stamps:read"],
             None, "ci-deployer"),
            ("batch-inference-prod", ["inference:invoke", "deployments:read"], None,
             "batch-inference"),
            ("eval-nightly", ["inference:invoke"], 90, "eval-harness"),
            ("maya-laptop", ["inference:invoke", "deployments:read"], 30, None),
            ("grafana-readonly", ["deployments:read", "stamps:read"], 180, None),
            ("legacy-notebook", ["inference:invoke"], 7, None),
        ]
        created = {}
        for name, scopes, days, principal in keys:
            body = {"name": name, "scopes": scopes, "expires_in_days": days}
            if principal:
                body["service_principal_id"] = principals[principal]
            response = self.call("POST", self.acct("/api-keys"), body=body)
            created[name] = response
            self.keys[name] = response["secret"]
            self.key_ids[name] = response["api_key"]["id"]
        self.call("DELETE", self.acct(f"/api-keys/{created['legacy-notebook']['api_key']['id']}"))
        self.call("DELETE", self.acct(f"/service-principals/{principals['legacy-etl']}"))
        log("service principals: 4 (1 disabled); API keys: 6 (1 revoked)")

        self.call("PUT", self.acct("/oidc-provider"), body={
            "issuer": "https://login.northstar-labs.example/",
            "audience": "fabric",
            "jwks_uri": "https://login.northstar-labs.example/.well-known/jwks.json",
            "auto_provision_role": "developer",
        })
        log("identity provider: login.northstar-labs.example")

    # --- stamps --------------------------------------------------------------------

    def run_agent(self, stamp: dict, *, enroll_token: str = "") -> None:
        state = self.work / "stamps" / stamp["name"]
        env = {**os.environ, "FABRIC_AGENT_ENROLLMENT_TOKEN": enroll_token}
        command = [
            str(self.args.agent_bin), "--control-plane", self.cp,
            "--state-dir", str(state / "agent"),
            "--telemetry-credential-file", str(state / "collector" / "telemetry-credential"),
            "--stamp-name", stamp["name"], "--region", stamp["region"],
            "--orchestrator", stamp["orchestrator"],
            "--upstream", self.args.model_host_url,
            "--inference-url", stamp["inference_url"],
            "--measure-capacity=false", "--gpus", str(self.total_gpus(stamp)),
            "--once",
        ]
        result = subprocess.run(command, env=env, capture_output=True, text=True)
        (state / "agent.log").open("a").write(result.stdout + result.stderr)
        if result.returncode != 0:
            raise SystemExit(f"agent for {stamp['name']} failed:\n{result.stderr}")

    @staticmethod
    def total_gpus(stamp: dict) -> int:
        return sum(count for _, count in stamp["gpus"])

    def capabilities(self, stamp: dict) -> dict:
        total = self.total_gpus(stamp)
        claims = [
            {"deployment_id": d["id"], "gpus": d["gpus"]}
            for d in self.deployments.values() if d["stamp"] == stamp["name"]
        ]
        used = sum(claim["gpus"] for claim in claims)
        return {
            "orchestrator": stamp["orchestrator"],
            "orchestrator_version": stamp["orchestrator_version"],
            "region": stamp["region"],
            "inference_url": stamp["inference_url"],
            "gpus": [{**gpu, "count": count, "source": "node-labels"}
                     for gpu, count in stamp["gpus"]],
            "allocatable_gpus": total,
            "requested_gpus": used,
            "fabric_requested_gpus": used,
            "fabric_gpu_claims": claims,
            "max_gpus_per_node": stamp["per_node"],
            "driver_version": stamp["driver"],
            "container_runtime_version": "containerd://1.7.27",
            "agent_version": "0.9.0",
            "operator_version": "0.9.0",
            "collector_version": "0.9.0",
            "runtime_version": "vllm 0.26.0",
        }

    def heartbeat(self, stamp: dict) -> None:
        self.call("POST", f"/v1/stamps/{stamp['id']}/heartbeat", token=stamp["agent_credential"],
                  body={"capabilities": self.capabilities(stamp)})

    def load_credentials(self, stamp: dict) -> None:
        state = self.work / "stamps" / stamp["name"] / "agent" / "credentials.json"
        credentials = json.loads(state.read_text())
        stamp["id"] = credentials["stamp_id"]
        stamp["agent_credential"] = credentials["agent_credential"]

    def enroll_stamps(self) -> None:
        for spec in STAMPS:
            stamp = dict(spec)
            stamp["inference_url"] = f"https://{stamp['name']}.gw.northstar-labs.example"
            token = self.call("POST", self.acct("/stamp-enrollment-tokens"),
                              body={"allowed_mode": "byoi", "expires_in_minutes": 30})
            self.run_agent(stamp, enroll_token=token["enrollment_token"])
            self.load_credentials(stamp)
            self.heartbeat(stamp)
            self.stamps[stamp["name"]] = stamp
            log(f"stamp {stamp['name']}: enrolled {stamp['id']} "
                "(" + ", ".join(f"{n}x {g['product']}" for g, n in stamp["gpus"]) + ")")
        # An outstanding token, as an operator would leave while setting up the next cluster.
        self.call("POST", self.acct("/stamp-enrollment-tokens"),
                  body={"allowed_mode": "byoi", "expires_in_minutes": 1440})

    # --- deployments ---------------------------------------------------------------

    def create_deployments(self) -> None:
        for name, alias, release, stamp, outcome, extra, requests in DEPLOYMENTS:
            spec = {
                "runtime": {"release": release, **extra.get("runtime", {})},
                "replicas": extra.get("replicas", 1),
                "resources": extra.get("resources", {}),
                "capabilities": extra.get("capabilities", {}),
            }
            created = self.call("POST", self.acct("/deployments"),
                                body={"name": name, "model_alias": alias, "spec": spec})
            gpus = spec["replicas"] * spec["resources"].get("gpu_count", 1)
            self.deployments[name] = {
                "id": created["id"], "alias": alias, "stamp": stamp, "outcome": outcome,
                "gpus": gpus, "requests": requests, "replicas": spec["replicas"],
                "release": release, "max_model_len": spec["runtime"].get("max_model_len"),
            }
            if stamp:
                self.call("POST", self.acct(f"/deployments/{created['id']}/placements"),
                          body={"stamp_id": self.stamps[stamp]["id"]})
            log(f"deployment {name} ({alias}) -> {stamp or 'not placed'}")

        for stamp in self.stamps.values():
            self.run_agent(stamp)  # pull desired state, write deployments.json, report
            self.heartbeat(stamp)
        self.report_observations()
        self.report_hosts()
        self.report_activity()

    # --- routing policies ----------------------------------------------------------

    def seed_routing_policies(self) -> None:
        """Routing policies (docs/routing-policies.md): one published twice with a draft on
        top, so the editor shows a live version, history and a pending diff; one draft only."""
        dep = {name: d["id"] for name, d in self.deployments.items()}

        def target(name: str, weight: int) -> dict:
            return {"deployment_id": dep[name], "weight": weight}

        rules = [
            {"id": "tool-calls", "name": "Tool calls",
             "match": {"has_tools": True, "endpoints": ["chat_completions", "responses"]},
             "targets": [target("assistant-large", 100)], "fallbacks": [dep["support-assistant"]],
             "fallback_on": ["server_error", "timeout", "no_ready_host"],
             "strategy": "prefix_aware", "timeout_seconds": 60},
            {"id": "batch-key", "name": "Batch jobs",
             "match": {"api_key_ids": [self.key_ids["batch-inference-prod"]]},
             "targets": [target("batch-summarizer", 100)], "fallbacks": [dep["chat-fast"]],
             "fallback_on": ["overload", "no_ready_host"]},
            {"id": "long-prompts", "name": "Long prompts",
             "match": {"prompt_tokens": {"min": 8000}},
             "targets": [target("assistant-large", 100)],
             "stamp_preference": {"mode": "prefer", "stamp_ids": [self.stamps["gcp-rtx6000"]["id"]]}},
            {"id": "gold-tier", "name": "Gold tier",
             "match": {"headers": [{"name": "x-tier", "op": "equals", "value": "gold"}]},
             "targets": [target("assistant-large", 70), target("support-assistant", 30)]},
        ]

        def default(*targets: dict) -> dict:
            return {"id": "default", "name": "Default", "targets": list(targets),
                    "fallbacks": [dep["assistant-large"]],
                    "fallback_on": ["server_error", "no_ready_host"]}

        created = self.call("POST", self.acct("/routing-policies"), body={
            "alias": "chat-default",
            "description": "The model name every Northstar app calls for chat.",
            "draft": {"rules": rules[:2], "default": default(target("support-assistant", 100))},
        })
        policy = self.acct(f"/routing-policies/{created['id']}")
        self.call("POST", policy + "/publish", body={"note": "Initial rules"})
        self.call("PUT", policy + "/draft", body={"draft": {
            "rules": rules, "default": default(target("support-assistant", 90), target("chat-fast", 10)),
        }})
        self.call("POST", policy + "/publish", body={"note": "Canary chat-fast at 10%"})
        # An edit in progress: the canary raised to 20%, not yet published.
        self.call("PUT", policy + "/draft", body={"draft": {
            "rules": rules, "default": default(target("support-assistant", 80), target("chat-fast", 20)),
        }})
        self.routing_policies["chat-default"] = created["id"]

        review = self.call("POST", self.acct("/routing-policies"), body={
            "alias": "code-review",
            "description": "Pull-request review bot; waiting for its deployment to become ready.",
            "draft": {"rules": [], "default": {
                "id": "default", "name": "Default", "targets": [target("code-review-bot", 100)],
                "fallbacks": [dep["assistant-large"]], "fallback_on": ["no_ready_host"]}},
        })
        self.routing_policies["code-review"] = review["id"]
        for stamp in self.stamps.values():
            self.run_agent(stamp)  # deliver the published policy with desired state
        # The agent pass reports its own view of each placement; restore what the operator
        # would observe, as create_deployments does after its own pass.
        self.report_observations()
        log("routing policies: chat-default (v2 live, draft pending), code-review (draft)")

    def placement_generation(self, deployment: dict) -> int:
        placements = self.call("GET", self.acct(f"/deployments/{deployment['id']}/placements"))
        return max(p["desired_generation"] for p in placements)

    def report_observations(self) -> None:
        """What the in-cluster operator would observe about each model host."""
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for name, deployment in self.deployments.items():
            if not deployment["stamp"]:
                continue
            stamp = self.stamps[deployment["stamp"]]
            replicas = deployment["replicas"]
            generation = self.placement_generation(deployment)
            outcome = deployment["outcome"]

            def condition(kind, status, reason, message):
                return {"type": kind, "status": status, "reason": reason, "message": message,
                        "last_transition_time": now}

            if outcome in ("ready", "updating"):
                phase, ready, unavailable = "ready", replicas, 0
                conditions = [
                    condition("Applied", "True", "ModelHostAndConfigurationApplied",
                              f"Placement generation {generation} is serving on this stamp"),
                    condition("Available", "True", "ActiveReleaseServing",
                              f"{replicas} of {replicas} model-host replicas answering"),
                ]
            elif outcome == "deploying":
                phase, ready, unavailable = "progressing", 0, replicas
                conditions = [
                    condition("Applied", "False", "DesiredReleaseProgressing",
                              "Model host started; downloading weights to the node cache"),
                    condition("Available", "False", "NoReadyModelHost",
                              "No model-host replica is ready yet"),
                    condition("Progressing", "True", "DownloadingWeights",
                              "Model host started; downloading weights to the node cache"),
                ]
            elif outcome == "pulling":
                phase, ready, unavailable = "progressing", 0, replicas
                conditions = [
                    condition("Applied", "False", "DesiredReleaseProgressing",
                              "Pulling the model runtime image on a new node"),
                    condition("Available", "False", "NoReadyModelHost",
                              "No model-host replica is ready yet"),
                    condition("Progressing", "True", "PullingImage",
                              "Pulling the model runtime image on a new node"),
                ]
            else:  # failed
                phase, ready, unavailable = "failed", 0, replicas
                conditions = [
                    condition("Available", "False", "InsufficientGPUMemory",
                              "KV cache for max_model_len 262144 needs 21.4 GiB; 10.1 GiB free "
                              "on Tesla T4. Lower max_model_len or request a larger GPU class."),
                    condition("Applied", "False", "ModelHostCrashLoop",
                              "Model host exited 3 times in 10 minutes"),
                ]
            self.call("POST", f"/v1/stamps/{stamp['id']}/status",
                      token=stamp["agent_credential"], body={
                          "deployment_id": deployment["id"],
                          "observed_generation": generation,
                          "phase": phase, "ready_replicas": ready,
                          "unavailable_replicas": unavailable,
                          "endpoint": f"{stamp['inference_url']}/v1" if phase == "ready" else None,
                          "conditions": conditions,
                      })
        log("model-host observations reported for every placement")

    def report_hosts(self) -> None:
        """What the agent's host reporter would push: each pod's status, events, log tail.

        One complete report per stamp, as the agent sends it (docs/model-host-logs.md). It is
        re-sent from keepalive so the snapshots stay fresh instead of going stale.
        """
        now = time.time()

        def at(seconds_ago: float) -> str:
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - seconds_ago))

        def event(kind: str, reason: str, message: str, seconds_ago: float, count: int = 1):
            return {"type": kind, "reason": reason, "message": message, "count": count,
                    "at": at(seconds_ago)}

        def stamp_line(seconds_ago: float, level: str, source: str, text: str) -> str:
            clock = time.strftime("%m-%d %H:%M:%S", time.gmtime(now - seconds_ago))
            return f"{level} {clock} [{source}] {text}"

        image = "public.ecr.aws/hexelstudio/fabric/model-host:git-4f1c2a9"
        # How long ago the seed started, so in-progress work advances between reports.
        running = now - self.started
        by_stamp: dict[str, list[dict]] = {}
        for name, deployment in self.deployments.items():
            if not deployment["stamp"]:
                continue
            release = deployment["release"]
            outcome = deployment["outcome"]
            pods = []
            for replica in range(deployment["replicas"]):
                pod_name = f"fabric-host-{deployment['id']}-7c9d5f8b6-{'xk2pqr'[replica:]}{replica}"
                scheduled = [
                    event("Normal", "Scheduled",
                          f"Successfully assigned {pod_name} to gpu-node-{replica}", 900),
                    event("Normal", "Pulling", f'Pulling image "{image}"', 898),
                    event("Normal", "Pulled",
                          f'Successfully pulled image "{image}" in 1m42.3s (1m42.3s including '
                          "waiting). Image size: 9876543210 bytes.", 796),
                    event("Normal", "Created", "Created container model-host", 795),
                    event("Normal", "Started", "Started container model-host", 795),
                ]
                startup = [
                    stamp_line(794, "INFO", "api_server.py:1351", "vLLM API server version 0.11.0"),
                    stamp_line(794, "INFO", "utils.py:253",
                               f"non-default args: {{'model': '{release}', 'port': 8000}}"),
                    stamp_line(780, "INFO", "model.py:657", "Resolved architecture from config"),
                    stamp_line(770, "INFO", "gpu_model_runner.py:1958",
                               f"Starting to load model {release}..."),
                    stamp_line(768, "INFO", "cuda.py:328", "Using Flash Attention backend."),
                ]
                conditions_ready = [
                    {"type": "PodScheduled", "status": "True", "last_transition_time": at(900)},
                    {"type": "Initialized", "status": "True", "last_transition_time": at(899)},
                ]
                if outcome in ("ready", "updating"):
                    log_lines = startup + [
                        stamp_line(760, "INFO", "default_loader.py:262",
                                   "Loading weights took 6.84 seconds"),
                        stamp_line(758, "INFO", "gpu_model_runner.py:2007",
                                   "Model loading took 4.21 GiB and 7.62 seconds"),
                        stamp_line(740, "INFO", "gpu_worker.py:276",
                                   "Available KV cache memory: 6.12 GiB"),
                        stamp_line(738, "INFO", "kv_cache_utils.py:849",
                                   "GPU KV cache size: 57,344 tokens"),
                        stamp_line(722, "INFO", "core.py:210",
                                   "init engine (profile, create kv cache, warmup model) took 18.4 s"),
                        stamp_line(720, "INFO", "launcher.py:28", "Available routes are: /v1/models, "
                                   "/v1/chat/completions, /v1/completions, /health, /metrics"),
                        stamp_line(719, "INFO", "server.py:83", "Started server process [1]"),
                        stamp_line(719, "INFO", "on.py:62", "Application startup complete."),
                    ] + [
                        stamp_line(600 - 30 * i, "INFO", "loggers.py:123",
                                   f"Engine 000: Avg prompt throughput: {180 + 7 * i:.1f} tokens/s, "
                                   f"Avg generation throughput: {42 + i:.1f} tokens/s, Running: "
                                   f"{1 + i % 3} reqs, Waiting: 0 reqs, GPU KV cache usage: "
                                   f"{2.1 + i * 0.4:.1f}%, Prefix cache hit rate: {38 + i}.0%")
                        for i in range(12)
                    ]
                    pods.append({
                        "name": pod_name, "phase": "Running", "ready": True,
                        "restart_count": 0, "container_state": "running", "started_at": at(900),
                        "conditions": conditions_ready + [
                            {"type": "ContainersReady", "status": "True",
                             "last_transition_time": at(718)},
                            {"type": "Ready", "status": "True", "last_transition_time": at(718)},
                        ],
                        "events": scheduled,
                        "log": "\n".join(log_lines),
                    })
                elif outcome == "pulling":
                    # A fresh machine: the runtime image is still coming down.
                    pull_started = 75 + running
                    pods.append({
                        "name": pod_name, "phase": "Pending", "ready": False,
                        "restart_count": 0, "container_state": "waiting",
                        "container_reason": "ContainerCreating",
                        "started_at": at(pull_started + 2),
                        "conditions": [
                            {"type": "PodScheduled", "status": "True",
                             "last_transition_time": at(pull_started + 2)},
                            {"type": "Initialized", "status": "True",
                             "last_transition_time": at(pull_started + 1)},
                            {"type": "ContainersReady", "status": "False",
                             "reason": "ContainersNotReady", "last_transition_time": at(pull_started)},
                            {"type": "Ready", "status": "False", "reason": "ContainersNotReady",
                             "last_transition_time": at(pull_started)},
                        ],
                        "events": [
                            event("Normal", "Scheduled",
                                  f"Successfully assigned fabric-stamp/{pod_name} to rtx-pro-6000",
                                  pull_started + 2),
                            event("Normal", "Pulling", f'Pulling image "{image}"', pull_started),
                        ],
                        "log": "",
                        "log_error": "container has not started yet",
                    })
                elif outcome == "deploying":
                    # Weights are downloading; the agent measures the node cache against the
                    # 4.4 GB the Hugging Face API lists, and the bar advances between reports.
                    total = 4_400_000_000
                    downloaded = min(int(total * 0.97), int(total * 0.58 + 9_000_000 * running))
                    log_lines = startup + [
                        stamp_line(760, "INFO", "weight_utils.py:449",
                                   "Using model weights format ['*.safetensors']"),
                        stamp_line(759, "WARNING", "__init__.py:1182",
                                   "HF_HUB_ENABLE_HF_TRANSFER is unset; downloading with the "
                                   "default backend"),
                    ]
                    pods.append({
                        "weights": {
                            "repository": release, "downloaded_bytes": downloaded,
                            "total_bytes": total, "in_progress_files": 1,
                            "measured_at": at(0),
                        },
                        "name": pod_name, "phase": "Running", "ready": False,
                        "restart_count": 0, "container_state": "running", "started_at": at(900),
                        "conditions": conditions_ready + [
                            {"type": "ContainersReady", "status": "False",
                             "reason": "ContainersNotReady",
                             "message": "containers with unready status: [model-host]",
                             "last_transition_time": at(795)},
                            {"type": "Ready", "status": "False", "reason": "ContainersNotReady",
                             "last_transition_time": at(795)},
                        ],
                        "events": scheduled + [
                            event("Warning", "Unhealthy",
                                  "Startup probe failed: Get \"http://10.42.1.17:8000/health\": "
                                  "dial tcp 10.42.1.17:8000: connect: connection refused",
                                  15, count=9),
                        ],
                        "log": "\n".join(log_lines),
                    })
                else:  # failed: the KV cache for the configured context does not fit
                    length = deployment["max_model_len"] or 262144
                    log_lines = startup + [
                        stamp_line(330, "INFO", "default_loader.py:262",
                                   "Loading weights took 7.02 seconds"),
                        stamp_line(328, "INFO", "gpu_model_runner.py:2007",
                                   "Model loading took 4.21 GiB and 7.80 seconds"),
                        stamp_line(310, "INFO", "gpu_worker.py:276",
                                   "Available KV cache memory: 10.10 GiB"),
                        stamp_line(309, "ERROR", "core.py:708", "EngineCore failed to start."),
                        "Traceback (most recent call last):",
                        '  File "/usr/local/lib/python3.12/dist-packages/vllm/v1/engine/core.py", '
                        "line 699, in run_engine_core",
                        "    engine_core = EngineCoreProc(*args, **kwargs)",
                        '  File "/usr/local/lib/python3.12/dist-packages/vllm/v1/core/kv_cache_utils.py", '
                        "line 702, in check_enough_kv_cache_memory",
                        "    raise ValueError(",
                        f"ValueError: To serve at least one request with the models's max seq len "
                        f"({length}), (21.40 GiB KV cache is needed, which is larger than the "
                        "available KV cache memory (10.10 GiB). Based on the available memory, "
                        "the estimated maximum model length is 123648. Try increasing "
                        "`gpu_memory_utilization` or decreasing `max_model_len` when initializing "
                        "the engine.",
                    ]
                    pods.append({
                        "name": pod_name, "phase": "Running", "ready": False,
                        "restart_count": 3, "container_state": "waiting",
                        "container_reason": "CrashLoopBackOff",
                        "container_message": "back-off 2m40s restarting failed container=model-host",
                        "started_at": at(900),
                        "conditions": conditions_ready + [
                            {"type": "ContainersReady", "status": "False",
                             "reason": "ContainersNotReady", "last_transition_time": at(300)},
                            {"type": "Ready", "status": "False", "reason": "ContainersNotReady",
                             "last_transition_time": at(300)},
                        ],
                        "events": scheduled + [
                            event("Warning", "BackOff",
                                  "Back-off restarting failed container model-host in pod "
                                  f"{pod_name}", 40, count=6),
                        ],
                        "log": "\n".join(log_lines),
                        "log_previous": True,
                    })
            by_stamp.setdefault(deployment["stamp"], []).append(
                {"deployment_id": deployment["id"], "pods": pods})

        for stamp_name, deployments in by_stamp.items():
            stamp = self.stamps[stamp_name]
            self.call("POST", f"/v1/stamps/{stamp['id']}/hosts", token=stamp["agent_credential"],
                      body={"observed_at": at(0), "deployments": deployments})

    # --- activity logs ---------------------------------------------------------------

    def report_activity(self) -> None:
        """What each stamp's agent would have recorded since it was bootstrapped.

        Event ids are fixed, so a re-run is stored once (the control plane deduplicates).
        """
        now = time.time()
        image = "public.ecr.aws/hexelstudio/fabric/model-host:git-4f1c2a9"

        def at(seconds_ago: float) -> str:
            return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - seconds_ago))

        def gpus_line(stamp: dict) -> str:
            return ", ".join(f"{count} × {gpu['product']} found ({gpu['memory_bytes'] / GIB:.0f} GiB)"
                             for gpu, count in stamp["gpus"])

        def story(stamp: dict, enrolled_ago: float, extra: list[tuple]) -> list[dict]:
            total = self.total_gpus(stamp)
            first, count = stamp["gpus"][0]
            base = [
                (enrolled_ago, "enrolled", "success", f"Enrolled with the control plane as {stamp['name']}",
                 None, {"mode": "byoi"}),
                (enrolled_ago - 1, "gpus_measured", "info", gpus_line(stamp), None,
                 {"product": first["product"], "count": count,
                  "memory_gib": round(first["memory_bytes"] / GIB, 1), "allocatable_gpus": total}),
                (enrolled_ago - 2, "gpu_runtime", "success",
                 f"GPU runtime OK: {total} nvidia.com/gpu allocatable", None,
                 {"allocatable_gpus": total, "ok": True}),
                (enrolled_ago - 3, "ready", "success", "Ready for deployments", None,
                 {"allocatable_gpus": total}),
            ]
            events = []
            for index, (ago, kind, severity, message, hint, details) in enumerate(base + extra):
                events.append({"id": f"demo-{stamp['name']}-{index:03d}", "kind": kind,
                               "severity": severity, "message": message, "hint": hint,
                               "details": details or {}, "at": at(ago)})
            return events

        pull_hint = ("The first deployment on this machine downloads the model runtime once; "
                     "later deployments start from the local copy.")
        stories = {
            "aks-gpu": story(self.stamps["aks-gpu"], 5400, [
                (1000, "desired_state_applied", "info",
                 "Applied desired-state generation 2: configured qwen3.5-2b, configured qwen3.5-0.8b",
                 None, {"generation": 2, "changes": 2}),
                (898, "image_pull_started", "info", "Pulling the model runtime image for qwen3.5-2b",
                 pull_hint, {"image": image}),
                (796, "image_pull_finished", "success", "Model runtime image pulled in 1m42s (9.9 GB)",
                 None, {"image": image, "seconds": 102, "size_bytes": 9876543210}),
                (420, "credentials_rotated", "success",
                 "Rotated machine credentials (version 2, requested from the console)", None,
                 {"credential_version": 2, "reason": "requested"}),
            ]),
            "gcp-rtx6000": story(self.stamps["gcp-rtx6000"], 4200, [
                (950, "desired_state_applied", "info",
                 "Applied desired-state generation 2: configured qwen3.5-2b-large, configured qwen3.5-2b-code",
                 None, {"generation": 2, "changes": 2}),
                (898, "image_pull_started", "info",
                 "Pulling the model runtime image for qwen3.5-2b-large", pull_hint, {"image": image}),
                (796, "image_pull_finished", "success", "Model runtime image pulled in 1m42s (9.9 GB)",
                 None, {"image": image, "seconds": 102, "size_bytes": 9876543210}),
            ]),
            "vm-gpu": story(self.stamps["vm-gpu"], 7200, [
                (6900, "gpu_runtime", "warning",
                 "GPU runtime missing: 1 GPU node(s) advertise no allocatable nvidia.com/gpu",
                 "The NVIDIA device plugin is not running or cannot reach the driver. On k3s, "
                 "re-run the bootstrap command; otherwise check nvidia-smi on the node and the "
                 "nvidia-device-plugin pods in kube-system.", {"ok": False, "unequipped_nodes": 1}),
                (6600, "gpu_runtime", "success", "GPU runtime OK: 2 nvidia.com/gpu allocatable", None,
                 {"allocatable_gpus": 2, "ok": True}),
                (960, "desired_state_applied", "info",
                 "Applied desired-state generation 2: configured qwen3.5-0.8b-batch, configured qwen3.5-2b-longctx",
                 None, {"generation": 2, "changes": 2}),
                (796, "image_pull_finished", "success", "Model runtime image pulled in 1m42s (9.9 GB)",
                 None, {"image": image, "seconds": 102, "size_bytes": 9876543210}),
                (300, "model_host_failed", "error",
                 "Model host for qwen3.5-2b-longctx failed: The model does not fit in GPU memory with these settings.",
                 "Lower max_model_len or max_num_seqs, raise gpu_memory_utilization slightly, or "
                 "deploy on a GPU with more memory.", {"reason": "GPUOutOfMemory", "restarts": 3}),
            ]),
            "rtx-pro-6000": story(self.stamps["rtx-pro-6000"], 480, [
                (90, "desired_state_applied", "info",
                 "Applied desired-state generation 1: configured qwen3.5-4b-docs", None,
                 {"generation": 1, "changes": 1}),
                (75, "image_pull_started", "info", "Pulling the model runtime image for qwen3.5-4b-docs",
                 pull_hint, {"image": image}),
            ]),
        }
        # The vm-gpu story has two GPU runtime checks before "ready"; keep its order honest.
        stories["vm-gpu"][3]["at"] = at(6590)
        for name, events in stories.items():
            stamp = self.stamps[name]
            self.call("POST", f"/v1/stamps/{stamp['id']}/events", token=stamp["agent_credential"],
                      body={"events": events})
        log("activity logs: " + ", ".join(f"{name} {len(events)}" for name, events in stories.items()))

    def enroll_removed_stamp(self) -> None:
        """A stamp that was enrolled and later removed, kept behind "Show removed"."""
        stamp = dict(REMOVED_STAMP)
        stamp["inference_url"] = f"https://{stamp['name']}.gw.northstar-labs.example"
        token = self.call("POST", self.acct("/stamp-enrollment-tokens"),
                          body={"allowed_mode": "byoi", "expires_in_minutes": 30})
        self.run_agent(stamp, enroll_token=token["enrollment_token"])
        self.load_credentials(stamp)
        self.heartbeat(stamp)
        # A deployment that was running there, which removal leaves marked "Stamp removed".
        created = self.call("POST", self.acct("/deployments"), body={
            "name": "legacy-chat", "model_alias": "qwen3.5-0.8b-legacy",
            "spec": {"runtime": {"release": "Qwen/Qwen3.5-0.8B"}, "replicas": 1,
                     "resources": {"gpu_class": "a10"}}})
        self.call("POST", self.acct(f"/deployments/{created['id']}/placements"),
                  body={"stamp_id": stamp["id"]})
        self.removed_deployment_id = created["id"]
        now = time.time()
        self.call("POST", f"/v1/stamps/{stamp['id']}/events", token=stamp["agent_credential"], body={
            "events": [{"id": f"demo-{stamp['name']}-000", "kind": "enrolled", "severity": "success",
                        "message": f"Enrolled with the control plane as {stamp['name']}",
                        "details": {}, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                           time.gmtime(now - 86400 * 3))}]})
        self.call("DELETE", self.acct(f"/stamps/{stamp['id']}"))
        log(f"stamp {stamp['name']}: enrolled and removed (hidden behind Show removed)")
    # --- serving metrics -------------------------------------------------------------

    # Bucket bounds as the real sources publish them: the gateway's latency histogram and
    # vLLM's time-to-first-token and inter-token-latency histograms.
    LATENCY_BOUNDS = (0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0)
    TTFT_BOUNDS = (0.001, 0.005, 0.01, 0.02, 0.04, 0.06, 0.08, 0.1, 0.25, 0.5, 0.75, 1.0, 2.5,
                   5.0, 7.5, 10.0)
    ITL_BOUNDS = (0.01, 0.025, 0.05, 0.075, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.75, 1.0, 2.5)
    # name -> (peak requests/min, median latency s, median TTFT s, median ITL s,
    #          output tokens per request, KV-cache % at peak)
    TRAFFIC = {
        "support-assistant": (90.0, 1.4, 0.085, 0.022, 180, 46.0),
        "chat-fast": (140.0, 0.55, 0.035, 0.012, 70, 28.0),
        "assistant-large": (36.0, 3.2, 0.21, 0.030, 420, 63.0),
        "batch-summarizer": (24.0, 2.1, 0.12, 0.018, 260, 38.0),
    }

    @staticmethod
    def histogram(count: float, median: float, bounds: tuple, sigma: float = 0.55) -> dict:
        """Per-bucket counts of a log-normal latency distribution, as the agent sends them."""

        def cdf(bound: float) -> float:
            return 0.5 * (1 + math.erf((math.log(bound) - math.log(median)) / (sigma * math.sqrt(2))))

        buckets, below = {}, 0.0
        for bound in bounds:
            share = cdf(bound)
            value = round(count * (share - below), 3)
            if value > 0:
                buckets[f"{bound:g}"] = value
            below = share
        if count * (1 - below) > 0.001:
            buckets["+Inf"] = round(count * (1 - below), 3)
        return buckets

    def metric_samples(self, stamp: dict, moments: list[float], interval: float) -> list[dict]:
        """What a stamp's agent would push at each moment: its deployments and its GPUs."""
        served = [(name, d) for name, d in self.deployments.items()
                  if d["stamp"] == stamp["name"] and name in self.TRAFFIC]
        devices = []
        for product, count in stamp["gpus"]:
            for _ in range(count):
                node = len(devices) // stamp["per_node"]
                devices.append({"product": product, "node": f"{stamp['name']}-gpu-{node}",
                                "index": str(len(devices) % stamp["per_node"])})
        owners: list[str | None] = []
        for name, d in served:
            owners.extend([name] * d["gpus"])
        owners = (owners + [None] * len(devices))[:len(devices)]

        samples = []
        for moment in moments:
            hour = (moment / 3600.0) % 24
            # A working-day curve with a little noise; each minute is reproducible.
            daily = 0.35 + 0.65 * max(0.0, math.sin(math.pi * (hour - 6) / 14)) if 6 <= hour <= 20 else 0.3
            rng = random.Random(int(moment // 60) * 7919 + len(stamp["name"]))
            points, load_by_deployment = [], {}
            for name, d in served:
                peak, latency, ttft, itl, tokens, kv_peak = self.TRAFFIC[name]
                load = daily * rng.uniform(0.85, 1.15)
                load_by_deployment[name] = load
                requests = peak * load * interval / 60
                # A short burst of host errors two hours ago, so the error charts have a story.
                burst = name == "support-assistant" and 7200 <= time.time() - moment <= 7800
                server = requests * (0.08 if burst else rng.uniform(0, 0.004))
                client = requests * rng.uniform(0.002, 0.012)
                ok = max(0.0, requests - server - client)
                # Latency rises with load, wobbles minute to minute, and doubles in the burst.
                stretch = ((1 + 0.6 * max(0.0, load - 0.8)) * rng.uniform(0.9, 1.12)
                           + (0.8 if burst else 0))
                running = peak * load * latency / 60
                points.append({
                    "deployment_id": d["id"],
                    "requests": {"2xx": round(ok, 3), "4xx": round(client, 3), "5xx": round(server, 3)},
                    "input_tokens": round(requests * tokens * 2.4, 1),
                    "output_tokens": round(requests * tokens, 1),
                    "latency_seconds": self.histogram(requests, latency * stretch, self.LATENCY_BOUNDS),
                    "ttft_seconds": self.histogram(requests, ttft * stretch, self.TTFT_BOUNDS, 0.6),
                    "itl_seconds": self.histogram(requests * tokens, itl * (1 + 0.3 * load),
                                                  self.ITL_BOUNDS, 0.35),
                    "hosts": d["replicas"],
                    "running": round(running, 2),
                    "waiting": round(max(0.0, running - 6 * d["replicas"]) + (3 if burst else 0), 2),
                    "kv_cache_percent": round(min(97.0, kv_peak * load * rng.uniform(0.9, 1.1)), 2),
                })
            gpus = []
            for device, owner in zip(devices, owners):
                load = load_by_deployment.get(owner, 0.0) if owner else 0.0
                total = device["product"]["memory_bytes"]
                util = min(100.0, 12 + 80 * load * rng.uniform(0.9, 1.08)) if owner else rng.uniform(0, 2)
                gpu = {
                    "node": device["node"], "index": device["index"],
                    "product": device["product"]["product"],
                    "utilization_percent": round(util, 1),
                    # vLLM reserves most of the device for weights and KV cache at start-up.
                    "memory_used_bytes": round(total * (0.9 if owner else 0.01)),
                    "memory_total_bytes": total,
                }
                if owner:
                    gpu["deployment_id"] = self.deployments[owner]["id"]
                gpus.append(gpu)
            for point in points:
                mine = [g for g in gpus if g.get("deployment_id") == point["deployment_id"]]
                if mine:
                    point["gpus"] = len(mine)
                    point["gpu_utilization_percent"] = round(
                        sum(g["utilization_percent"] for g in mine) / len(mine), 2)
                    point["gpu_memory_used_bytes"] = sum(g["memory_used_bytes"] for g in mine)
                    point["gpu_memory_total_bytes"] = sum(g["memory_total_bytes"] for g in mine)
            samples.append({
                "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment)),
                "interval_seconds": interval,
                "sources": ["gateway", "model_host", "dcgm"],
                "deployments": points,
                "gpus": gpus,
            })
        return samples

    def push_metrics(self, stamp: dict, samples: list[dict]) -> None:
        for start in range(0, len(samples), 20):  # the endpoint's per-report bound
            self.call("POST", f"/v1/stamps/{stamp['id']}/metrics", token=stamp["agent_credential"],
                      body={"samples": samples[start:start + 20]})

    def backfill_metrics(self, hours: int = 24) -> None:
        """A day of per-minute samples, as if the agents had been reporting all along."""
        now = time.time()
        moments = [now - 60 * minute for minute in range(hours * 60, 0, -1)]
        for stamp in self.stamps.values():
            self.push_metrics(stamp, self.metric_samples(stamp, moments, 60.0))
        log(f"serving metrics: backfilled {hours}h of per-minute samples for "
            f"{len(self.stamps)} stamps")

    def report_metrics(self) -> None:
        """The 30-second sample each agent pushes, sent from keepalive."""
        for stamp in self.stamps.values():
            self.push_metrics(stamp, self.metric_samples(stamp, [time.time()], 30.0))

    # --- data planes, traffic, usage -----------------------------------------------

    def free_port(self) -> int:
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def start_data_planes(self) -> None:
        jwks = self.work / "jwks.json"
        jwks.write_text(self.http.get(f"{self.cp}/.well-known/jwks.json").text)
        for index, stamp in enumerate(self.stamps.values()):
            port = self.args.inference_port if index == 0 else self.free_port()
            admin = self.free_port()
            state = self.work / "stamps" / stamp["name"]
            env = {
                **os.environ,
                "DEMO_DEPLOYMENTS": str(state / "agent" / "deployments.json"),
                "DEMO_JWKS": str(jwks), "DEMO_JWT_ISSUER": self.args.jwt_issuer,
                "DEMO_DP_PORT": str(port), "DEMO_DP_ADMIN_PORT": str(admin),
                "DEMO_DP_CERT": str(self.work / "server.pem"),
                "DEMO_DP_KEY": str(self.work / "server-key.pem"),
            }
            process = subprocess.Popen(
                [self.args.data_plane_python, str(Path(__file__).with_name("data_plane.py"))],
                env=env, cwd=self.args.data_plane_dir,
                stdout=(state / "data-plane.log").open("w"), stderr=subprocess.STDOUT,
            )
            self.children.append(process)
            stamp["dp_url"] = f"https://localhost:{port}"
            stamp["dp_admin_url"] = f"http://127.0.0.1:{admin}"
        for stamp in self.stamps.values():
            for _ in range(100):
                try:
                    if httpx.get(f"{stamp['dp_admin_url']}/healthz", timeout=1).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.2)
            else:
                raise SystemExit(f"data plane for {stamp['name']} did not start")
            log(f"data plane for {stamp['name']}: {stamp['dp_url']}")

    def generate_traffic(self) -> None:
        tokens = [
            self.api_key_token(self.keys["batch-inference-prod"], "fabric-inference"),
            self.api_key_token(self.keys["eval-nightly"], "fabric-inference"),
            self.api_key_token(self.keys["maya-laptop"], "fabric-inference"),
        ]
        rng = random.Random(7)
        jobs = []
        for deployment in self.deployments.values():
            for _ in range(deployment["requests"]):
                prompt = rng.choice(PROMPTS)
                if rng.random() < 0.4:
                    prompt = prompt + "\n\nContext:\n" + " ".join(rng.choice(PROMPTS)
                                                               for _ in range(rng.randint(2, 12)))
                jobs.append((deployment, rng.choice(tokens), prompt, rng.random() < 0.3,
                             rng.choice([128, 256, 512, 1024])))

        def send(job) -> int:
            deployment, token, prompt, stream, max_tokens = job
            stamp = self.stamps[deployment["stamp"]]
            with httpx.Client(timeout=60, verify=self.args.ca) as client:
                response = client.post(
                    f"{stamp['dp_url']}/v1/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"model": deployment["alias"], "stream": stream,
                          "max_tokens": max_tokens,
                          "messages": [{"role": "user", "content": prompt}],
                          **({"stream_options": {"include_usage": True}} if stream else {})},
                )
                return response.status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            codes = list(pool.map(send, jobs))
        failures = [code for code in codes if code != 200]
        log(f"inference: {len(codes)} requests through the data planes, "
            f"{len(failures)} not 200 {sorted(set(failures)) if failures else ''}")

    def seed_agent_runs(self) -> None:
        """A few agent runs on aks-gpu (the console's gateway) so Agents > Runs has data.

        The stub model host plays a tool-using agent when a request declares tools; its first
        calculator call is malformed, so the tool-call guard repairs it. Optional demo data:
        a failure is logged and never fails the seed.
        """
        try:
            self._agent_runs()
            log("agent runs: 3 runs on aks-gpu (Agents > Runs)")
        except Exception as exc:  # noqa: BLE001
            log(f"agent runs: skipped ({exc.__class__.__name__})")

    def _agent_runs(self) -> None:
        stamp = self.stamps["aks-gpu"]
        alias = self.deployments["support-assistant"]["alias"]
        token = self.api_key_token(self.keys["maya-laptop"], "fabric-inference")
        tools = [
            {"type": "function", "function": {"name": "calculator", "parameters": {
                "type": "object", "properties": {"expression": {"type": "string"}},
                "required": ["expression"]}},
             "x-fabric-executor": {"type": "builtin", "name": "calculator"}},
            {"type": "function", "function": {"name": "current_time", "parameters": {
                "type": "object", "properties": {"timezone": {"type": "string"}}}},
             "x-fabric-executor": {"type": "builtin", "name": "current_time"}},
        ]
        task = [{"role": "user", "content": "Split 86.40 EUR three ways with a 17.5% tip."}]
        with httpx.Client(timeout=60, verify=self.args.ca) as client:
            def post(path: str, run_id: str, body: dict, **budget: int) -> int:
                headers = {"Authorization": f"Bearer {token}", "X-Fabric-Run-Id": run_id}
                for name, value in budget.items():
                    headers[f"X-Fabric-Run-Budget-{name.capitalize()}"] = str(value)
                return client.post(f"{stamp['dp_url']}{path}", headers=headers,
                                   json=body).status_code

            # Completed within budget, with one tool-guard repair.
            post("/v1/agent/runs", "trip-planner-7f2c",
                 {"model": alias, "messages": task, "tools": tools}, tokens=20000, steps=8)
            # A chat client that keeps going: the step budget refuses its fourth call.
            for _ in range(4):
                post("/v1/chat/completions", "nightly-eval-0412",
                     {"model": alias, "max_tokens": 128,
                      "messages": [{"role": "user", "content": rng_prompt()}]}, steps=3)
            # A token budget the task overshoots: the run ends budget_exhausted.
            post("/v1/agent/runs", "support-ticket-881",
                 {"model": alias, "messages": task, "tools": tools}, tokens=300)

    def collect(self, quiet: bool = False) -> None:
        for stamp in self.stamps.values():
            state = self.work / "stamps" / stamp["name"]
            result = subprocess.run([
                str(self.args.collector_bin), "--control-plane", self.cp,
                "--data-plane-admin", stamp["dp_admin_url"],
                "--credential-file", str(state / "collector" / "telemetry-credential"),
                "--once",
            ], capture_output=True, text=True)
            (state / "collector.log").open("a").write(result.stdout + result.stderr)
            if result.returncode != 0 and not quiet:
                log(f"collector for {stamp['name']} failed: {result.stderr.strip()[-300:]}")
        if not quiet:
            usage = self.call("GET", self.acct("/deployments/usage"))
            log(f"usage forwarded: {usage['events']} events, {usage['input_tokens']} input / "
                f"{usage['output_tokens']} output tokens")

    def finish_rollouts(self) -> None:
        # A spec change the stamp has not observed yet, so one deployment shows "updating".
        name, deployment = next((n, d) for n, d in self.deployments.items() if d["outcome"] == "updating")
        current = self.call("GET", self.acct(f"/deployments/{deployment['id']}"))
        spec = current["desired_spec"]
        spec["runtime"]["max_model_len"] = 8192
        spec["runtime"]["max_num_seqs"] = 128
        self.call("PATCH", self.acct(f"/deployments/{deployment['id']}"), body={"spec": spec})
        log(f"{name}: serving settings changed (generation 2 waiting for the stamp)")

    # --- lifecycle -----------------------------------------------------------------

    def write_state(self) -> None:
        playground = self.deployments["support-assistant"]
        state = {
            "account_id": self.account_id,
            "inference_url": self.stamps["aks-gpu"]["dp_url"],
            "deployment_ids": {name: d["id"] for name, d in self.deployments.items()},
            "playground_deployment_id": playground["id"],
            "stamps": {name: s["id"] for name, s in self.stamps.items()},
            "first_deployment_id": self.deployments["docs-qa"]["id"],
            "downloading_deployment_id": self.deployments["code-review-bot"]["id"],
            "removed_stamp_deployment_id": self.removed_deployment_id,
            "routing_policies": self.routing_policies,
            # Where each stamp's reported gateway really listens, for the demo edge gateway's
            # address overrides: {"aks-gpu.gw.northstar-labs.example:443": "127.0.0.1:8090"}.
            "gateway_addresses": {
                f"{urlsplit(s['inference_url']).hostname}:443":
                    f"127.0.0.1:{urlsplit(s['dp_url']).port}"
                for s in self.stamps.values()
            },
        }
        Path(self.args.state_file).write_text(json.dumps(state, indent=2))

    def keepalive(self) -> None:
        while True:
            time.sleep(30)
            for stamp in self.stamps.values():
                try:
                    self.heartbeat(stamp)
                except SystemExit as exc:
                    log(f"heartbeat for {stamp['name']} failed: {exc}")
            try:
                self.collect(quiet=True)
            except Exception as exc:  # keep the demo up through a transient failure
                log(f"collector pass failed: {exc}")
            try:
                self.report_hosts()
            except SystemExit as exc:
                log(f"host report failed: {exc}")
            try:
                self.report_metrics()
            except SystemExit as exc:
                log(f"metrics report failed: {exc}")

    def stop(self, *_args) -> None:
        for child in self.children:
            child.terminate()
        for child in self.children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
        sys.exit(0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--work", required=True)
    parser.add_argument("--control-url", required=True)
    parser.add_argument("--idp-url", required=True)
    parser.add_argument("--ca", required=True)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    parser.add_argument("--audience", required=True)
    parser.add_argument("--jwt-issuer", required=True)
    parser.add_argument("--agent-bin", required=True)
    parser.add_argument("--collector-bin", required=True)
    parser.add_argument("--model-host-url", required=True)
    parser.add_argument("--data-plane-python", required=True)
    parser.add_argument("--data-plane-dir", required=True)
    parser.add_argument("--inference-port", type=int, required=True)
    parser.add_argument("--state-file", required=True)
    args = parser.parse_args()

    demo = Demo(args)
    signal.signal(signal.SIGTERM, demo.stop)
    signal.signal(signal.SIGINT, demo.stop)
    try:
        demo.seed_people_and_account()
        demo.enroll_stamps()
        demo.create_deployments()
        demo.seed_routing_policies()
        demo.enroll_removed_stamp()
        demo.backfill_metrics()
        demo.start_data_planes()
        demo.generate_traffic()
        demo.seed_agent_runs()
        demo.collect()
        demo.finish_rollouts()
        demo.write_state()
        log("seeding complete")
        demo.keepalive()
    except BaseException:
        for child in demo.children:
            child.kill()
        raise


if __name__ == "__main__":
    main()
