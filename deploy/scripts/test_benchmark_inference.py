"""Protocol checks for the endpoint benchmark; no cluster, GPU, or dependencies required."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock


SPEC = importlib.util.spec_from_file_location("benchmark_inference", Path(__file__).with_name("benchmark-inference.py"))
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


class Handler(BaseHTTPRequestHandler):
    exchanges = 0
    bodies = []

    def log_message(self, *args):
        pass

    def send_json(self, data, status=200):
        encoded = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        if self.path == "/v1/models":
            self.send_json({"data": [{"id": "test-model", "owned_by": "test"}]})
        else:
            self.send_json({}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/v1/token":
            Handler.exchanges += 1
            Handler.bodies.append(body)
            self.send_json({"access_token": f"token-{Handler.exchanges}", "expires_in": 300})
            return
        Handler.bodies.append(body)
        prompt = body["messages"][0]["content"]
        if prompt == "rate limit":
            self.send_response(429)
            self.send_header("Retry-After", "2")
            self.end_headers()
            self.wfile.write(b"secret error message that must not be saved")
            return
        if prompt == "expired" and self.headers.get("Authorization") == "Bearer token-1":
            self.send_json({"error": "credential details must not persist"}, 401)
            return
        if not body["stream"]:
            self.send_json({"choices": [{"message": {"content": "private response"}, "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12}})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        events = [{"choices": [{"delta": {"role": "assistant", "content": ""}}]},
                  {"choices": [{"delta": {"content": "private response"}}]},
                  {"choices": [{"delta": {"content": " part two"}}]},
                  {"choices": [{"delta": {}, "finish_reason": "stop"}],
                   "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12}}]
        try:
            self.wfile.write(b": keepalive\n\n")
            for event in events:
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
                self.wfile.flush()
                time.sleep(0.005)
            if prompt != "incomplete":
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


class ProtocolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        Handler.exchanges = 0
        Handler.bodies = []
        self.env = mock.patch.dict(os.environ, {
            "FABRIC_API_KEY": "fab_key_testing-secret", "FABRIC_INFERENCE_TOKEN": "",
            "FABRIC_CONTROL_URL": self.url, "FABRIC_INFERENCE_URL": self.url + "/v1",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.credentials = benchmark.Credentials(self.url, 2)

    def request(self, prompt="ordinary", stream=True, cancel=0):
        return benchmark.run_request(self.credentials, self.url, "test-model", prompt, 8,
                                     stream, {"temperature": 0}, 2, cancel)

    def test_stream_excludes_empty_role_and_preserves_usage(self):
        result = self.request()
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["stream_done"])
        self.assertEqual(result["content_events"], 2)
        self.assertEqual(result["client_inter_content_interval_s"]["count"], 1)
        self.assertGreater(result["client_time_to_first_content_s"], 0)
        self.assertEqual(result["usage"]["completion_tokens"], 3)
        self.assertEqual(Handler.bodies[0]["audience"], "fabric-inference")
        self.assertEqual(Handler.bodies[-1]["stream_options"], {"include_usage": True})
        self.assertNotIn("private response", json.dumps(result))

    def test_nonstream_has_no_fake_ttft_or_chunk_intervals(self):
        result = self.request(stream=False)
        self.assertEqual(result["status"], "completed")
        self.assertIsNone(result["client_time_to_first_content_s"])
        self.assertEqual(result["client_inter_content_interval_s"]["count"], 0)
        self.assertEqual(result["usage"]["total_tokens"], 12)

    def test_incomplete_and_intentional_cancel_are_distinct(self):
        self.assertEqual(self.request("incomplete")["status"], "incomplete_stream")
        cancelled = self.request(cancel=1)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIsNone(cancelled["usage"])
        self.assertEqual(cancelled["content_events"], 1)

    def test_429_retains_only_status_and_retry_interval(self):
        result = self.request("rate limit")
        self.assertEqual(result["http_status"], 429)
        self.assertEqual(result["retry_after_s"], 2)
        self.assertNotIn("secret error", json.dumps(result))
        summary = benchmark.summarize([result], 1)
        self.assertEqual(summary["http_429"], 1)

    def test_401_renews_once_and_concurrent_expiry_is_coordinated(self):
        result = self.request("expired")
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["auth_attempts"], 2)
        self.assertEqual(Handler.exchanges, 2)
        self.credentials._expires_at = 0
        with benchmark.concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            tokens = list(pool.map(lambda _: self.credentials.get(), range(4)))
        self.assertEqual(len(set(tokens)), 1)
        self.assertEqual(Handler.exchanges, 3)

    def test_matrix_artifacts_exclude_text_and_cannot_overwrite(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            code = benchmark.main(["benchmark", "--models", "test-model", "--allow-http",
                                   "--prompt-words", "2", "--output-tokens", "8", "--concurrency", "2",
                                   "--requests", "2", "--warmup", "0", "--output-dir", directory])
            self.assertEqual(code, 0)
            artifact_path = next(Path(directory).glob("*.json"))
            text = artifact_path.read_text()
            data = json.loads(text)
            self.assertEqual(len(data["cells"]), 2)
            self.assertNotIn("private response", text)
            self.assertNotIn("fab_key_testing-secret", text)
            self.assertNotIn("Continue this numbered list", text)
            self.assertEqual(artifact_path.stat().st_mode & 0o777, 0o444)
            second = benchmark.write_artifact(directory, {"test": True})
            self.assertNotEqual(second, artifact_path)
            self.assertEqual(artifact_path.read_text(), text)

    def test_claims_require_metadata_and_reject_credentials(self):
        with self.assertRaises(benchmark.BenchmarkError):
            benchmark.configuration_metadata(None, True, ["test-model"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metadata.json"
            path.write_text(json.dumps({"api_key": "private"}))
            with self.assertRaises(benchmark.BenchmarkError):
                benchmark.configuration_metadata(path, False, [])
            path.write_text(json.dumps({"prompts": ["private input"]}))
            with self.assertRaises(benchmark.BenchmarkError):
                benchmark.configuration_metadata(path, False, [])

    def test_complete_claim_metadata_accepts_only_fixed_configuration(self):
        metadata = {"target": "local-test", "hardware": {"gpu_model": "Tesla T4", "gpu_count": 1,
                    "driver_version": "observed", "source": "device-artifact"}, "profile": "profile-r1",
                    "models": {"test-model": {"model_revision": "fixed-revision", "image_digest": "sha256:" + "a" * 64,
                    "kernel_mode": "standard", "serving_settings": {"dtype": "float16", "tensor_parallel_size": 1,
                    "replicas": 1, "max_model_len": 1024, "max_num_seqs": 8,
                    "gpu_memory_utilization": 0.85, "execution": "cuda_graph"}}},
                    "evidence_refs": ["artifact-id"]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metadata.json"
            path.write_text(json.dumps(metadata))
            self.assertEqual(benchmark.configuration_metadata(path, True, ["test-model"]), metadata)
            metadata["models"]["test-model"]["kernel_mode"] = "auto"
            path.write_text(json.dumps(metadata))
            with self.assertRaises(benchmark.BenchmarkError):
                benchmark.configuration_metadata(path, True, ["test-model"])

    def test_missing_usage_is_reported_without_token_estimates(self):
        result = self.request(stream=False)
        result["usage"] = None
        summary = benchmark.summarize([result], 1)
        self.assertEqual(summary["completed_requests_without_usage"], 1)
        self.assertFalse(summary["usage_covers_all_completed_requests"])
        self.assertEqual(summary["completed_output_tokens_reported"], 0)

    def test_snapshot_excludes_env_and_secret_values(self):
        outputs = {
            "nodes": {"items": [{"metadata": {"name": "gpu-node", "labels": {"nvidia.com/gpu.product": "T4", "private": "secret"}},
                                   "status": {"allocatable": {"nvidia.com/gpu": "1"}}}]},
            "pods": {"items": [{"metadata": {"name": "host"}, "spec": {"containers": [
                {"name": "vllm", "image": "test-image", "env": [{"name": "API_KEY", "value": "private"}],
                 "args": ["--model=test", "--hf-token=private", "--max-model-len", "1024"]}]} }]},
            "fabricmodeldeployments": {"items": [{"metadata": {"name": "fabric-model"},
                "status": {"phase": "ready", "readyReplicas": 0, "unavailableReplicas": 1,
                           "conditions": [{"type": "Available", "status": "False",
                                           "reason": "NoReadyModelHost", "message": "private diagnostic"}]}}]},
        }
        def run(command, **kwargs):
            self.assertIn("explicit-context", command)
            return mock.Mock(returncode=0, stdout=json.dumps(outputs[command[6]]).encode())
        with mock.patch.object(benchmark.subprocess, "run", side_effect=run):
            snapshot = benchmark.cluster_snapshot("explicit-context", "fabric", "pool=gpu")
        text = json.dumps(snapshot)
        self.assertNotIn("private", text)
        self.assertNotIn("hf-token", text)
        self.assertIn("--max-model-len", text)
        model = snapshot["reads"]["fabricmodeldeployments"]["items"][0]
        self.assertEqual(model["ready_replicas"], 0)
        self.assertEqual(model["unavailable_replicas"], 1)
        self.assertEqual(model["conditions"][0]["status"], "False")
        self.assertEqual(model["conditions"][0]["reason"], "NoReadyModelHost")

    def test_sse_multiline_comments_and_size_bound(self):
        self.assertEqual(list(benchmark.sse_events(io.BytesIO(b": hi\n\ndata: a\ndata: b\n\n"))), ["a\nb"])
        with self.assertRaises(benchmark.BenchmarkError):
            list(benchmark.sse_events(io.BytesIO(b"data: " + b"x" * benchmark.MAX_EVENT_BYTES)))

    def test_credential_bearing_image_or_model_references_are_omitted(self):
        self.assertEqual(benchmark.safe_reference("https://example.test/model?token=private"),
                         "[credential-bearing reference omitted]")

    def test_endpoint_rejects_embedded_credentials_and_http_without_opt_in(self):
        for endpoint in ("https://user:password@example.test", "https://example.test?api_key=private", self.url):
            with self.assertRaises(benchmark.BenchmarkError):
                benchmark.base_url(endpoint, False)


if __name__ == "__main__":
    unittest.main()
