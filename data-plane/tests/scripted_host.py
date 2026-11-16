"""A scripted chat-completions host and gateway builder for agent-loop and cascade tests."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx

from fabric_data_plane.agent_tools import ToolRunner
from fabric_data_plane.app import DataPlane, create_inference_app
from fabric_data_plane.keys import KeyCache
from fabric_data_plane.registry import Deployment, DeploymentRegistry
from tests.conftest import ACCOUNT_A, ACCOUNT_B, UPSTREAM, make_settings

Reply = dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]]


def completion(
    content: str | None = "done",
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str | None = None,
    usage: tuple[int, int] = (10, 5),
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason or ("tool_calls" if tool_calls else "stop"),
            }
        ],
        "usage": {
            "prompt_tokens": usage[0],
            "completion_tokens": usage[1],
            "total_tokens": sum(usage),
        },
    }


def tool_call(call_id: str, name: str, arguments: Any) -> dict[str, Any]:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": raw}}


class ScriptedHost:
    """Answers each chat completion from ``replies`` (in order) or ``by_model``."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.replies: list[Reply] = []
        #: Upstream model name -> reply, used when ``replies`` is empty.
        self.by_model: dict[str, Reply] = {}
        #: Upstream model names whose host refuses connections.
        self.down: set[str] = set()
        self.default: Reply = completion("done")

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content or b"{}")
        self.requests.append(
            {
                "payload": payload,
                "headers": {k.lower(): v for k, v in request.headers.items()},
            }
        )
        model = payload.get("model")
        if model in self.down:
            raise httpx.ConnectError("model host is down", request=request)
        if payload.get("stream"):
            frames = (
                b'data: {"choices":[{"delta":{"content":"hi"}}],"usage":null}\n\n'
                b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":1,'
                b'"total_tokens":4}}\n\n'
                b"data: [DONE]\n\n"
            )
            return httpx.Response(200, content=frames)
        if self.replies:
            reply = self.replies.pop(0)
        else:
            reply = self.by_model.get(model, self.default)
        body = reply(payload) if callable(reply) else reply
        return httpx.Response(200, json={**body, "model": model})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def default_registry(extra: list[Deployment] | None = None) -> DeploymentRegistry:
    return DeploymentRegistry(
        [
            Deployment(
                deployment_id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
                account_id=ACCOUNT_A,
                model_alias="launch-model",
                upstream_url=UPSTREAM,
                upstream_model="internal-release-1",
            ),
            Deployment(
                deployment_id=uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
                account_id=ACCOUNT_B,
                model_alias="other-account-model",
                upstream_url=UPSTREAM,
            ),
            *(extra or []),
        ]
    )


FAKE_DNS = {
    "tools.test": ["203.0.113.10"],
    "slow.test": ["203.0.113.11"],
    "rebind.test": ["169.254.169.254"],
}


async def fake_resolver(host: str, _port: int) -> list[str]:
    if host not in FAKE_DNS:
        raise OSError("no such host")
    return FAKE_DNS[host]


def build_plane(
    control_plane: Any,
    host: ScriptedHost,
    *,
    registry: DeploymentRegistry | None = None,
    tool_handler: Callable[[httpx.Request], Any] | None = None,
    **settings: Any,
) -> DataPlane:
    values = {"agent_tool_allowed_hosts": "tools.test,slow.test,rebind.test,169.254.169.254"}
    values.update(settings)
    config = make_settings(**values)
    plane = DataPlane(
        settings=config,
        keys=KeyCache(config, client=control_plane.client()),
        registry=registry or default_registry(),
        client=host.client(),
    )

    def no_tools(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected tool request to {request.url}")

    plane.agent_tool_runner = ToolRunner(  # type: ignore[attr-defined]
        config,
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(tool_handler or no_tools), follow_redirects=False
        ),
        resolver=fake_resolver,
    )
    return plane


def gateway(plane: DataPlane) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=create_inference_app(plane))
    return httpx.AsyncClient(transport=transport, base_url="http://ingress.test")


def parse_sse(raw: str) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for block in raw.strip().split("\n\n"):
        event, data = "", ""
        for line in block.splitlines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if event:
            events.append((event, json.loads(data)))
    return events
