#!/usr/bin/env python3
"""Opt-in, read-only Fabric endpoint inventory and client-observed inference benchmark.

Python 3.10+, standard library only. Credentials come from environment variables;
response text and prompts are excluded from artifacts unless --persist-prompts is set.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import http.client
import itertools
import json
import math
import os
from pathlib import Path
import re
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_EVENT_BYTES = 1024 * 1024
SERVING_FLAGS = {
    "--model", "--served-model-name", "--dtype", "--tensor-parallel-size",
    "--max-model-len", "--max-num-seqs", "--gpu-memory-utilization",
    "--enforce-eager", "--enable-prefix-caching", "--max-num-batched-tokens",
}
SETTING_KEYS = {
    "temperature", "top_p", "top_k", "min_p", "seed", "presence_penalty",
    "frequency_penalty", "repetition_penalty", "ignore_eos", "chat_template_kwargs",
}


class BenchmarkError(Exception):
    """An error whose message is safe to print without response bodies or credentials."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def open_http(request, timeout):
    # Redirects must never forward a bearer credential to a different destination.
    return urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout)


def read_json(response):
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise BenchmarkError("JSON response exceeds the configured size bound")
    return json.loads(body)


def base_url(value, allow_http):
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise BenchmarkError("An absolute HTTP(S) endpoint URL is required")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise BenchmarkError("Endpoint URLs must not contain credentials, queries, or fragments")
    if parsed.scheme == "http" and not allow_http:
        raise BenchmarkError("HTTP requires --allow-http for local or cluster endpoints")
    return value.rstrip("/")


def api_url(base, path):
    # Both https://host and OpenAI-style https://host/v1 base URLs are accepted.
    return base + (path.removeprefix("/v1") if base.endswith("/v1") else path)


class Credentials:
    def __init__(self, control_url, timeout):
        self.control_url = control_url
        self.timeout = timeout
        self.api_key = os.environ.get("FABRIC_API_KEY", "")
        self.static_token = os.environ.get("FABRIC_INFERENCE_TOKEN", "")
        if bool(self.api_key) == bool(self.static_token):
            raise BenchmarkError("Set exactly one of FABRIC_API_KEY or FABRIC_INFERENCE_TOKEN")
        if self.api_key and not control_url:
            raise BenchmarkError("FABRIC_CONTROL_URL is required when using FABRIC_API_KEY")
        self._lock = threading.Lock()
        self._token = ""
        self._expires_at = 0.0
        self.exchanges = 0

    def get(self):
        if self.static_token:
            return self.static_token
        with self._lock:
            if self._token and time.monotonic() < self._expires_at:
                return self._token
            started = time.monotonic()
            request = urllib.request.Request(
                api_url(self.control_url, "/v1/token"),
                data=json.dumps({"grant_type": "api_key", "api_key": self.api_key,
                                 "audience": "fabric-inference"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            try:
                with open_http(request, self.timeout) as response:
                    payload = read_json(response)
                token = payload["access_token"]
                ttl = float(payload["expires_in"])
                if not isinstance(token, str) or not token or not math.isfinite(ttl) or ttl <= 0:
                    raise ValueError
            except urllib.error.HTTPError as exc:
                exc.close()
                raise BenchmarkError(f"Inference token exchange returned HTTP {exc.code}") from None
            except (OSError, ValueError, KeyError, TypeError, BenchmarkError):
                raise BenchmarkError("Inference token exchange failed; no response body was retained") from None
            self._token = token
            self._expires_at = started + max(0.0, ttl - min(30.0, ttl / 10.0))
            self.exchanges += 1
            return token

    def invalidate(self, token):
        with self._lock:
            if token == self._token:
                self._expires_at = 0.0


def authenticated_open(credentials, url, timeout, payload=None):
    """Retry one rejected 401 with renewed credentials; never retry accepted work or 429s."""
    for attempt in range(2):
        token = credentials.get()
        request = urllib.request.Request(
            url, data=None if payload is None else json.dumps(payload).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                     "Accept": "application/json" if payload is None or not payload.get("stream")
                     else "text/event-stream"},
        )
        try:
            return open_http(request, timeout), attempt + 1
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and attempt == 0 and credentials.api_key:
                exc.close()
                credentials.invalidate(token)
                continue
            raise
    raise BenchmarkError("Authentication failed")


def inventory(credentials, inference_url, timeout):
    response, _ = authenticated_open(credentials, api_url(inference_url, "/v1/models"), timeout)
    with response:
        payload = read_json(response)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise BenchmarkError("Model inventory is not an OpenAI models response")
    models = []
    for model in payload["data"]:
        if isinstance(model, dict) and isinstance(model.get("id"), str):
            models.append({"id": model["id"], "owned_by": model.get("owned_by")})
    return sorted(models, key=lambda model: model["id"])


def sse_events(response):
    """Yield bounded SSE data payloads, ignoring comments and joining multiline data."""
    lines = []
    size = 0
    while True:
        line = response.readline(MAX_EVENT_BYTES + 1)
        if not line:
            if lines:
                yield "\n".join(lines)
            return
        size += len(line)
        if size > MAX_EVENT_BYTES:
            raise BenchmarkError("SSE event exceeds the configured size bound")
        line = line.decode("utf-8").rstrip("\r\n")
        if not line:
            if lines:
                yield "\n".join(lines)
            lines, size = [], 0
        elif line.startswith("data:"):
            lines.append(line[5:].removeprefix(" "))


def token_usage(payload):
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    counts = {}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = usage.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            counts[name] = value
    return counts or None


def distribution(values):
    values = sorted(values)
    if not values:
        return {"count": 0, "mean": None, "p50": None, "p95": None, "max": None}
    def percentile(p):
        return values[max(0, math.ceil(len(values) * p) - 1)]
    return {"count": len(values), "mean": statistics.fmean(values),
            "p50": percentile(0.5), "p95": percentile(0.95), "max": values[-1]}


def run_request(credentials, inference_url, model, prompt, output_tokens, stream,
                settings, timeout, cancel_after=0):
    result = {"status": "transport_error", "http_status": None, "auth_attempts": None,
              "usage": None, "stream_done": False, "finish_reason": None,
              "content_events": 0, "client_time_to_first_content_s": None,
              "client_inter_content_interval_s": distribution([]),
              "requested_cancellation": bool(cancel_after)}
    payload = dict(settings, model=model, messages=[{"role": "user", "content": prompt}],
                   max_tokens=output_tokens, stream=stream)
    if stream:
        payload["stream_options"] = {"include_usage": True}
    started = time.perf_counter()
    content_times = []
    try:
        response, attempts = authenticated_open(
            credentials, api_url(inference_url, "/v1/chat/completions"), timeout, payload,
        )
        result["auth_attempts"] = attempts
        with response:
            result["http_status"] = response.status
            if not stream:
                data = read_json(response)
                if (not isinstance(data, dict) or not isinstance(data.get("choices"), list)
                        or not data["choices"] or data.get("error") is not None):
                    raise ValueError
                result["usage"] = token_usage(data)
                result["finish_reason"] = data["choices"][0].get("finish_reason") if data["choices"] else None
                result["status"] = "completed"
            else:
                if "text/event-stream" not in response.headers.get("Content-Type", ""):
                    raise BenchmarkError("Streaming response did not use SSE")
                for event in sse_events(response):
                    if event.strip() == "[DONE]":
                        result["stream_done"] = True
                        result["status"] = "completed"
                        break
                    data = json.loads(event)
                    if not isinstance(data, dict):
                        raise ValueError
                    if data.get("error") is not None:
                        result["status"] = "upstream_stream_error"
                        break
                    if usage := token_usage(data):
                        result["usage"] = usage
                    for choice in data.get("choices", []):
                        if choice.get("finish_reason") is not None:
                            result["finish_reason"] = choice["finish_reason"]
                        content = choice.get("delta", {}).get("content")
                        if isinstance(content, str) and content:
                            content_times.append(time.perf_counter() - started)
                    if cancel_after and len(content_times) >= cancel_after:
                        result["status"] = "cancelled"
                        break
                else:
                    result["status"] = "incomplete_stream"
    except urllib.error.HTTPError as exc:
        result["status"] = "http_error"
        result["http_status"] = exc.code
        retry = exc.headers.get("Retry-After", "")
        result["retry_after_s"] = int(retry) if re.fullmatch(r"[0-9]{1,6}", retry) else None
        exc.close()
    except BenchmarkError:
        result["status"] = "protocol_or_auth_error"
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError):
        result["status"] = "parse_error"
    except (OSError, urllib.error.URLError):
        result["status"] = "transport_error"
    except http.client.HTTPException:
        result["status"] = "transport_error"
    result["client_duration_s"] = time.perf_counter() - started
    result["content_events"] = len(content_times)
    if content_times:
        result["client_time_to_first_content_s"] = content_times[0]
        result["client_inter_content_interval_s"] = distribution(
            [b - a for a, b in zip(content_times, content_times[1:])],
        )
    return result


def summarize(results, elapsed):
    complete = [result for result in results if result["status"] == "completed"]
    metered = [result for result in complete if result["usage"] is not None]
    token_complete = [result for result in complete
                      if result["usage"] is not None and "completion_tokens" in result["usage"]]
    statuses = {status: sum(result["status"] == status for result in results)
                for status in sorted({result["status"] for result in results})}
    output = sum(result["usage"]["completion_tokens"] for result in token_complete)
    return {"requests": len(results), "statuses": statuses,
            "http_429": sum(result["http_status"] == 429 for result in results),
            "completed_requests_with_usage": len(metered),
            "completed_requests_without_usage": len(complete) - len(metered),
            "completed_requests_with_completion_tokens": len(token_complete),
            "partial_or_cancelled_usage_is_in_request_records": True,
            "completed_output_tokens_reported": output,
            "usage_covers_all_completed_requests": len(token_complete) == len(complete),
            "reported_completed_output_tokens_per_wall_s": output / elapsed if elapsed else None,
            "completed_requests_per_wall_s": len(complete) / elapsed if elapsed else None,
            "client_duration_s": distribution([r["client_duration_s"] for r in complete]),
            "client_time_to_first_content_s": distribution(
                [r["client_time_to_first_content_s"] for r in complete
                 if r["client_time_to_first_content_s"] is not None]),
            "wall_s": elapsed}


def safe_reference(value):
    """Avoid preserving credentials accidentally embedded in model/image URLs."""
    if not isinstance(value, str):
        return value
    if "://" in value:
        parsed = urllib.parse.urlsplit(value)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            return "[credential-bearing reference omitted]"
    if re.search(r"fab_(key|agent|telemetry|enroll)_|Bearer\s+", value):
        return "[credential-bearing reference omitted]"
    return value


def serving_args(args):
    kept = {}
    for index, arg in enumerate(args):
        if not isinstance(arg, str):
            continue
        key, separator, value = arg.partition("=")
        if key not in SERVING_FLAGS:
            continue
        if not separator:
            value = args[index + 1] if index + 1 < len(args) and not args[index + 1].startswith("--") else True
        kept[key] = safe_reference(value)
    return kept


def cluster_snapshot(context, namespace, selector):
    """Read only allowlisted fields; never save env, Secrets, annotations, or raw manifests."""
    snapshot = {"context": context, "namespace": namespace, "gpu_selector": selector,
                "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(), "reads": {}}
    for resource in ("nodes", "pods", "fabricmodeldeployments"):
        command = ["kubectl", "--context", context, "--namespace", namespace,
                   "get", resource, "-o", "json"]
        if resource == "nodes" and selector:
            command.extend(["--selector", selector])
        try:
            process = subprocess.run(command, capture_output=True, timeout=30, check=False)
            if process.returncode:
                snapshot["reads"][resource] = {"status": "unavailable", "exit_code": process.returncode}
                continue
            payload = json.loads(process.stdout)
            selected = []
            for item in payload["items"]:
                spec, status = item.get("spec", {}), item.get("status", {})
                row = {"name": item["metadata"]["name"]}
                if resource == "nodes":
                    labels = item["metadata"].get("labels", {})
                    row.update(gpu_allocatable=status.get("allocatable", {}).get("nvidia.com/gpu", "0"),
                               unschedulable=spec.get("unschedulable", False),
                               hardware_labels={key: value for key, value in labels.items()
                                                if key.startswith("nvidia.com/gpu.") or key in {
                                                    "node.kubernetes.io/instance-type",
                                                    "cloud.google.com/gke-accelerator"}},
                               ready=next((c.get("status") for c in status.get("conditions", [])
                                           if c.get("type") == "Ready"), "Unknown"))
                elif resource == "pods":
                    states = {c["name"]: c for c in status.get("containerStatuses", [])}
                    row.update(node=spec.get("nodeName"), phase=status.get("phase"), containers=[
                        {"name": c["name"], "image": safe_reference(c.get("image")),
                         "image_id": safe_reference(states.get(c["name"], {}).get("imageID")),
                         "ready": states.get(c["name"], {}).get("ready"),
                         "restarts": states.get(c["name"], {}).get("restartCount"),
                         "gpu_limit": c.get("resources", {}).get("limits", {}).get("nvidia.com/gpu"),
                         "serving_flags": serving_args(c.get("args", []))}
                        for c in spec.get("containers", [])])
                else:
                    row.update(spec={key: safe_reference(spec[key]) for key in (
                        "deploymentId", "modelAlias", "upstreamModel", "kernelMode", "replicas",
                        "gpuCount", "maxModelLen", "maxNumSeqs", "gpuMemoryUtilization", "execution",
                    ) if key in spec}, phase=status.get("phase"),
                               observed_generation=status.get("observedGeneration"),
                               ready_replicas=status.get("readyReplicas"),
                               unavailable_replicas=status.get("unavailableReplicas"),
                               conditions=[{
                                   key: condition[key] for key in (
                                       "type", "status", "reason", "observedGeneration",
                                   ) if key in condition
                               } for condition in status.get("conditions", [])
                                   if condition.get("type") in {"Applied", "Available", "Progressing"}])
                selected.append(row)
            snapshot["reads"][resource] = {"status": "read", "items": selected}
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
            snapshot["reads"][resource] = {"status": "unavailable"}
    return snapshot


def write_artifact(directory, artifact):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    path = directory / f"{stamp}-{uuid.uuid4().hex}.json"
    # Never overwrite an earlier run; read-only after completion is not WORM storage.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(artifact, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
    path.chmod(0o444)
    return path


def numbers(value):
    try:
        values = list(dict.fromkeys(int(part) for part in value.split(",")))
    except ValueError:
        raise argparse.ArgumentTypeError("Use comma-separated positive integers") from None
    if not values or min(values) < 1:
        raise argparse.ArgumentTypeError("Use comma-separated positive integers")
    return values


def configuration_metadata(path, require_claims, models):
    metadata = json.loads(Path(path).read_text()) if path else {}
    if not isinstance(metadata, dict):
        raise BenchmarkError("Metadata must be a JSON object without credentials or private content")
    def reject_credentials(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if re.search(r"(^|_)(secret|password|api_key|access_token|authorization|credential|prompt|prompts|response|responses|messages)($|_)", key, re.I):
                    raise BenchmarkError("Credential and request-content fields must not be included in metadata")
                reject_credentials(child)
        elif isinstance(value, list):
            for child in value:
                reject_credentials(child)
        elif isinstance(value, str) and re.search(r"fab_(key|agent|telemetry|enroll)_|Bearer\s+", value):
            raise BenchmarkError("Credential values must not be included in metadata")
    reject_credentials(metadata)
    json.dumps(metadata, allow_nan=False)
    if require_claims:
        required = {"target", "hardware", "profile", "models", "evidence_refs"}
        if not required.issubset(metadata) or not all(metadata[key] for key in required):
            raise BenchmarkError("Performance claims need target, hardware, profile, models, and evidence_refs metadata")
        hardware = metadata["hardware"]
        if not isinstance(hardware, dict) or not all(hardware.get(key) for key in (
            "gpu_model", "gpu_count", "driver_version", "source")):
            raise BenchmarkError("Performance claims need explicit GPU model/count/driver/source")
        if not isinstance(hardware["gpu_count"], int) or isinstance(hardware["gpu_count"], bool) or hardware["gpu_count"] < 1:
            raise BenchmarkError("Hardware gpu_count must be a positive integer")
        if not isinstance(metadata["evidence_refs"], list) or any(not isinstance(ref, str) or not ref for ref in metadata["evidence_refs"]):
            raise BenchmarkError("Evidence references must be a nonempty list of artifact identifiers")
        for model in models:
            entry = metadata["models"].get(model, {}) if isinstance(metadata["models"], dict) else {}
            if not isinstance(entry, dict) or not all(entry.get(key) for key in (
                "model_revision", "image_digest", "kernel_mode", "serving_settings")):
                raise BenchmarkError("Each measured model needs revision, image digest, kernel mode, and serving settings")
            if not re.search(r"(?:^|@)sha256:[0-9a-f]{64}$", str(entry["image_digest"])):
                raise BenchmarkError("Performance claims require an actual sha256 image digest")
            if entry["kernel_mode"] not in {"fabric", "standard"}:
                raise BenchmarkError("Performance claims must identify the selected kernel as fabric or standard")
            settings = entry["serving_settings"]
            if not isinstance(settings, dict) or not all(key in settings for key in (
                "dtype", "tensor_parallel_size", "replicas", "max_model_len", "max_num_seqs",
                "gpu_memory_utilization", "execution")):
                raise BenchmarkError("Performance claims require all documented serving settings")
            if str(entry["model_revision"]).lower() in {"unknown", "unverified", "main", "latest"}:
                raise BenchmarkError("Model revision must identify fixed model contents")
        if any(str(metadata[key]).lower() in {"unknown", "unverified"} for key in ("target", "profile")):
            raise BenchmarkError("Performance claims cannot use unknown target/profile metadata")
    return metadata


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("inventory", "benchmark"))
    parser.add_argument("--allow-http", action="store_true")
    parser.add_argument("--output-dir", default="artifacts/inference-benchmarks")
    parser.add_argument("--timeout", type=float, default=180, help="Network read timeout in seconds")
    parser.add_argument("--models", help="Comma-separated inventory model IDs, or all; required for benchmark")
    parser.add_argument("--prompt-words", type=numbers, default=[32, 256])
    parser.add_argument("--prompt-file", help="JSON array of custom prompt strings; contents excluded by default")
    parser.add_argument("--persist-prompts", action="store_true", help="Explicitly permit saving input prompt text")
    parser.add_argument("--output-tokens", type=numbers, default=[32, 128])
    parser.add_argument("--concurrency", type=numbers, default=[1, 4])
    parser.add_argument("--requests", type=int, default=8, help="Measured requests per matrix cell")
    parser.add_argument("--warmup", type=int, default=1, help="Serial unmeasured requests per cell")
    parser.add_argument("--modes", choices=("both", "stream", "nonstream"), default="both")
    parser.add_argument("--settings-file", help="JSON generation options from the documented allowlist")
    parser.add_argument("--cancel-every", type=int, default=0, help="Cancel each Nth measured streaming request")
    parser.add_argument("--cancel-after-content", type=int, default=3)
    parser.add_argument("--metadata-file", help="Nonsecret exact model/hardware/profile configuration and evidence")
    parser.add_argument("--performance-claims", action="store_true", help="Require complete claim metadata; never verifies it automatically")
    parser.add_argument("--context", help="Explicit kubectl context for an optional sanitized read-only snapshot")
    parser.add_argument("--namespace", help="Explicit snapshot namespace; required with --context")
    parser.add_argument("--snapshot-gpu-selector", default="")
    args = parser.parse_args(argv)
    if args.timeout <= 0 or not math.isfinite(args.timeout) or args.requests < 1 or args.warmup < 0:
        parser.error("timeout and requests must be positive; warmup must be nonnegative")
    if args.cancel_every < 0 or args.cancel_after_content < 1:
        parser.error("Cancellation interval must be nonnegative and content count positive")
    if bool(args.context) != bool(args.namespace):
        parser.error("--context and --namespace must be supplied together")
    if args.context and (args.context.startswith("-") or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", args.namespace)):
        parser.error("Use an explicit context name and a valid Kubernetes namespace")
    if args.command == "benchmark" and not args.models:
        parser.error("benchmark requires an explicit --models selection")
    inference_url = base_url(os.environ.get("FABRIC_INFERENCE_URL", ""), args.allow_http)
    control = os.environ.get("FABRIC_CONTROL_URL", "")
    control_url = base_url(control, args.allow_http) if control else ""
    credentials = Credentials(control_url, args.timeout)
    artifact = {"schema_version": 1, "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "command": args.command, "inference_url": inference_url,
                "control_url": control_url or None,
                "authentication": "api_key_exchange" if credentials.api_key else "static_inference_token",
                "measurement_boundary": "client HTTP/SSE; includes gateway, network, and auth renewal; not engine TPOT",
                "configuration_status": "unverified", "prompts_persisted": args.persist_prompts,
                "client": {"python": sys.version.split()[0], "transport": "urllib HTTP/1.1, connection per request",
                           "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
                "cells": []}
    available = inventory(credentials, inference_url, args.timeout)
    artifact["model_inventory"] = available
    if args.command == "benchmark":
        models = [m["id"] for m in available if m["id"] != "auto"] if args.models == "all" else list(dict.fromkeys(args.models.split(",")))
        if not models or any(model not in {m["id"] for m in available} for model in models):
            raise BenchmarkError("Selected models must be present in the authenticated endpoint inventory")
        artifact["metadata"] = configuration_metadata(args.metadata_file, args.performance_claims, models)
        if args.performance_claims:
            artifact["configuration_status"] = "documented_by_caller_not_independently_verified"
        settings = json.loads(Path(args.settings_file).read_text()) if args.settings_file else {"temperature": 0}
        if not isinstance(settings, dict) or not set(settings).issubset(SETTING_KEYS):
            raise BenchmarkError("Generation settings contain unsupported keys")
        for key, value in settings.items():
            if key == "chat_template_kwargs":
                continue
            if key == "ignore_eos":
                if not isinstance(value, bool):
                    raise BenchmarkError("ignore_eos must be a boolean")
            elif not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
                raise BenchmarkError("Numeric generation options must contain finite numbers")
        if "chat_template_kwargs" in settings and (not isinstance(settings["chat_template_kwargs"], dict)
                or not set(settings["chat_template_kwargs"]).issubset({"enable_thinking"})
                or any(not isinstance(value, bool) for value in settings["chat_template_kwargs"].values())):
            raise BenchmarkError("Only enable_thinking is accepted inside chat_template_kwargs")
        # Reject NaN/infinity before any request or artifact serialization.
        json.dumps(settings, allow_nan=False)
        if args.prompt_file:
            prompts = json.loads(Path(args.prompt_file).read_text())
            if not isinstance(prompts, list) or not prompts or any(not isinstance(p, str) or not p.strip() for p in prompts):
                raise BenchmarkError("Prompt file must contain a nonempty JSON array of nonempty strings")
        else:
            prompts = ["Continue this numbered list until the output limit: " +
                       " ".join(f"item{i}" for i in range(words)) for words in args.prompt_words]
        artifact["workload"] = {"settings": settings, "output_tokens": args.output_tokens,
                                "concurrency": args.concurrency, "requests_per_cell": args.requests,
                                "warmup_per_cell": args.warmup, "modes": args.modes,
                                "timeout_s": args.timeout, "stream_include_usage": True,
                                "cancel_every": args.cancel_every,
                                "cancel_after_content": args.cancel_after_content,
                                "prompt_source": "custom" if args.prompt_file else "synthetic_numbered_list",
                                "prompt_length_unit": "words, not tokenizer tokens"}
        modes = [True, False] if args.modes == "both" else [args.modes == "stream"]
        if args.context:
            artifact["cluster_before"] = cluster_snapshot(args.context, args.namespace, args.snapshot_gpu_selector)
        for model, prompt, output, concurrency, stream in itertools.product(
                models, prompts, args.output_tokens, args.concurrency, modes):
            prompt_info = {"sha256": hashlib.sha256(prompt.encode()).hexdigest(), "words": len(prompt.split()),
                           "utf8_bytes": len(prompt.encode())}
            if args.persist_prompts:
                prompt_info["text"] = prompt
            def call(cancel=0):
                return run_request(credentials, inference_url, model, prompt, output,
                                   stream, settings, args.timeout, cancel)
            warmup = [call() for _ in range(args.warmup)]
            started = time.perf_counter()
            with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = [pool.submit(call, args.cancel_after_content if stream and args.cancel_every
                                       and (index + 1) % args.cancel_every == 0 else 0)
                           for index in range(args.requests)]
                results = [future.result() for future in futures]
            elapsed = time.perf_counter() - started
            cell = {"model": model, "prompt": prompt_info, "max_tokens": output,
                    "concurrency": concurrency, "stream": stream,
                    "warmup_statuses": [r["status"] for r in warmup],
                    "requests": results, "summary": summarize(results, elapsed)}
            artifact["cells"].append(cell)
            print(f"{model}: stream={stream} words={prompt_info['words']} max_tokens={output} "
                  f"concurrency={concurrency} statuses={cell['summary']['statuses']}", flush=True)
        if args.context:
            artifact["cluster_after"] = cluster_snapshot(args.context, args.namespace, args.snapshot_gpu_selector)
    elif args.context:
        artifact["cluster_snapshot"] = cluster_snapshot(args.context, args.namespace, args.snapshot_gpu_selector)
    artifact["token_exchanges"] = credentials.exchanges
    artifact["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    path = write_artifact(args.output_dir, artifact)
    print(f"Artifact: {path}")
    if args.command == "inventory":
        print(json.dumps(available, indent=2))
    failed = any(r["status"] not in {"completed", "cancelled"}
                 for cell in artifact["cells"] for r in cell["requests"])
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except urllib.error.HTTPError as error:
        error.close()
        print(f"Endpoint returned HTTP {error.code}; response body omitted", file=sys.stderr)
        sys.exit(2)
    except (BenchmarkError, OSError, ValueError, TypeError, KeyError) as error:
        # Only our own bounded messages are printed; arbitrary exception strings may hold URLs/content.
        print(str(error) if isinstance(error, BenchmarkError) else type(error).__name__, file=sys.stderr)
        sys.exit(2)
