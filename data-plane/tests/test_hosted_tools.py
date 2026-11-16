"""Hosted tools in the agent loop: code_interpreter (sandbox, e2b), web_fetch, web_search."""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from fabric_data_plane.agent_tools import ToolRunner
from fabric_data_plane.hosted_tools import ToolContext
from fabric_data_plane.hosted_tools.code_interpreter import E2BBackend, session_id
from fabric_data_plane.hosted_tools.runner import HostedTools
from fabric_data_plane.hosted_tools.web import HostPolicy, check_url, html_to_text
from tests.conftest import ACCOUNT_A, make_settings
from tests.scripted_host import (
    ScriptedHost,
    build_plane,
    completion,
    gateway,
    parse_sse,
    tool_call,
)

RUNS = "/v1/agent/runs"
SECRET_CODE = "print('the secret code text 8f3a')"
SECRET_STDOUT = "stdout-content-must-not-be-traced-41c2"
PAGE_TEXT = "Quarterly latency fell to 182 ms"
PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 64).decode()
SANDBOX_TOKEN = "gateway-to-sandbox-secret-123456"
E2B_KEY = "e2b_live_key_must_never_leak_9d1e"

DNS = {
    "public.test": ["93.184.215.14"],
    "intranet.test": ["10.1.2.3"],
    "rebind.test": ["169.254.169.254"],
    "mixed.test": ["93.184.215.14", "127.0.0.1"],
}

HOSTED = {
    "code_interpreter_enabled": True,
    "code_interpreter_url": "http://sandbox.fabric.svc:8090",
    "code_interpreter_token": SANDBOX_TOKEN,
    "web_fetch_enabled": True,
    "web_fetch_allowed_hosts": "*,intranet.test",
    "web_search_enabled": True,
    "web_backend": "searxng",
    "web_search_url": "http://searxng.fabric.svc:8080",
}


def hosted_tool(builtin: str, name: str | None = None) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name or builtin},
        "x-fabric-executor": {"type": "builtin", "name": builtin},
    }


def run_body(*tools: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "model": "launch-model",
        "messages": [{"role": "user", "content": "analyse this"}],
        "tools": list(tools),
        **extra,
    }


class Recorder:
    """An httpx MockTransport handler that records requests and answers from a function."""

    def __init__(self, answer=None) -> None:
        self.requests: list[httpx.Request] = []
        self.answer = answer or (lambda request: httpx.Response(404))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.answer(request)

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self), follow_redirects=False)


def sandbox_reply(request: httpx.Request) -> httpx.Response:
    if request.method == "DELETE":
        return httpx.Response(200, json={"deleted": True})
    return httpx.Response(
        200,
        json={
            "stdout": SECRET_STDOUT + "\n",
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
            "exit_code": 0,
            "timed_out": False,
            "killed_reason": None,
            "duration_ms": 41.5,
            "files": [
                {"name": "plot.png", "bytes": 72, "mime_type": "image/png", "content_base64": PNG},
                {"name": "data.csv", "bytes": 10, "mime_type": None},
            ],
            "error": None,
        },
    )


async def resolver(host: str, _port: int) -> list[str]:
    if host not in DNS:
        raise OSError("no such host")
    return DNS[host]


def plane_with(
    control_plane: Any,
    host: ScriptedHost,
    *,
    sandbox: Recorder | None = None,
    web: Recorder | None = None,
    service: Recorder | None = None,
    e2b_factory: Any = None,
    resolve: Any = resolver,
    **settings: Any,
):
    values = {**HOSTED, **settings}
    plane = build_plane(control_plane, host, **values)
    config = plane.settings
    hosted = HostedTools(
        config,
        sandbox_client=(sandbox or Recorder(sandbox_reply)).client(),
        web_client=(web or Recorder()).client(),
        service_client=(service or Recorder()).client(),
        resolver=resolve,
        e2b_factory=e2b_factory,
    )
    plane.agent_tool_runner = ToolRunner(config, hosted=hosted)  # type: ignore[attr-defined]
    return plane


@pytest.fixture
def host() -> ScriptedHost:
    return ScriptedHost()


@pytest.fixture
def auth(signing_key) -> dict[str, str]:
    return {"Authorization": f"Bearer {signing_key.issue()}"}


def tool_steps(body: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in body["steps"] if s["type"] == "tool"]


def tool_messages(host: ScriptedHost, index: int) -> list[dict[str, Any]]:
    return [m for m in host.requests[index]["payload"]["messages"] if m["role"] == "tool"]


# --------------------------------------------------------------------------- validation


@pytest.mark.parametrize("builtin", ["code_interpreter", "web_fetch", "web_search"])
async def test_disabled_hosted_tools_are_refused_at_validation(control_plane, host, auth, builtin):
    plane = build_plane(control_plane, host)  # nothing enabled: the default
    async with gateway(plane) as client:
        response = await client.post(RUNS, headers=auth, json=run_body(hosted_tool(builtin)))
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "hosted_tool_disabled"
    assert builtin in error["message"]
    assert host.requests == []


async def test_model_sees_default_schema_and_never_the_executor(control_plane, host, auth):
    host.replies = [completion("no tools needed")]
    plane = plane_with(control_plane, host)
    custom = hosted_tool("web_search", "search")
    custom["function"]["description"] = "my own description"
    async with gateway(plane) as client:
        body = (
            await client.post(
                RUNS, headers=auth, json=run_body(hosted_tool("code_interpreter"), custom)
            )
        ).json()
    assert body["status"] == "completed"
    tools = host.requests[0]["payload"]["tools"]
    assert all("x-fabric-executor" not in t for t in tools)
    code = tools[0]["function"]
    assert code["parameters"]["required"] == ["code"]
    assert "sandbox" in code["description"]
    assert tools[1]["function"]["description"] == "my own description"
    assert tools[1]["function"]["parameters"]["required"] == ["query"]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"code_interpreter_enabled": True}, "CODE_INTERPRETER_URL"),
        (
            {"code_interpreter_enabled": True, "code_interpreter_url": "http://s"},
            "CODE_INTERPRETER_TOKEN",
        ),
        ({"code_interpreter_enabled": True, "code_interpreter_backend": "e2b"}, "E2B_API_KEY"),
        ({"web_fetch_enabled": True}, "WEB_FETCH_ALLOWED_HOSTS"),
        ({"web_search_enabled": True, "web_backend": "searxng"}, "WEB_SEARCH_URL"),
        ({"web_search_enabled": True, "web_backend": "firecrawl"}, "FIRECRAWL_URL"),
    ],
)
def test_enabled_tools_must_be_configured(overrides, message):
    with pytest.raises(ValueError, match=message):
        make_settings(**overrides)


def test_service_urls_may_not_point_at_metadata():
    config = make_settings(
        web_search_enabled=True, web_backend="searxng", web_search_url="http://169.254.169.254/"
    )
    with pytest.raises(ValueError, match="forbidden"):
        HostedTools(config)


# --------------------------------------------------------------------------- code interpreter


async def test_code_interpreter_runs_in_the_sandbox_with_the_shared_secret(
    control_plane, host, auth
):
    sandbox = Recorder(sandbox_reply)
    host.replies = [
        completion(None, tool_calls=[tool_call("c1", "code_interpreter", {"code": SECRET_CODE})]),
        completion(None, tool_calls=[tool_call("c2", "code_interpreter", {"code": "print(2)"})]),
        completion("done"),
    ]
    plane = plane_with(control_plane, host, sandbox=sandbox)
    async with gateway(plane) as client:
        response = await client.post(
            RUNS,
            headers={**auth, "X-Fabric-Run-Id": "run-42"},
            json=run_body(hosted_tool("code_interpreter")),
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"

    executes = [r for r in sandbox.requests if r.method == "POST"]
    assert len(executes) == 2
    first = json.loads(executes[0].content)
    assert first["code"] == SECRET_CODE
    assert executes[0].headers["x-fabric-sandbox-token"] == SANDBOX_TOKEN
    assert "authorization" not in {k.lower() for k in executes[0].headers}
    # One opaque session per (account, run): never the raw ids.
    expected = session_id(ToolContext(str(ACCOUNT_A), "run-42"))
    assert {json.loads(r.content)["session_id"] for r in executes} == {expected}
    assert "run-42" not in executes[0].content.decode()
    # The run ended, so its session is released.
    deletes = [r for r in sandbox.requests if r.method == "DELETE"]
    assert [r.url.path for r in deletes] == [f"/v1/sessions/{expected}"]

    # The model gets the output; the trace gets sizes only.
    result = json.loads(tool_messages(host, 1)[0]["content"])
    assert result["stdout"] == SECRET_STDOUT + "\n"
    assert result["files"] == ["plot.png", "data.csv"]
    assert "content_base64" not in tool_messages(host, 1)[0]["content"]
    step = tool_steps(body)[0]
    assert step["executor"] == "hosted"
    assert step["hosted_tool"] == "code_interpreter"
    assert step["ok"] is True
    assert step["exit_code"] == 0
    assert step["code_bytes"] == len(SECRET_CODE)
    assert step["files"] == 2
    traced = json.dumps(body["steps"])
    assert SECRET_CODE not in traced and SECRET_STDOUT not in traced and PNG not in traced

    # Produced images are returned with the run.
    plot, csv = body["files"][:2]
    assert plot == {
        "tool_call_id": "c1",
        "name": "plot.png",
        "mime_type": "image/png",
        "bytes": 72,
        "content_base64": PNG,
    }
    assert csv["name"] == "data.csv" and "content_base64" not in csv
    assert len(body["files"]) == 4  # both calls produced files


async def test_requires_action_keeps_the_session_and_include_files_false(control_plane, host, auth):
    sandbox = Recorder(sandbox_reply)
    host.replies = [
        completion(
            None,
            tool_calls=[
                tool_call("c1", "code_interpreter", {"code": "x=1"}),
                tool_call("c2", "lookup_order", {}),
            ],
        ),
    ]
    client_tool = {"type": "function", "function": {"name": "lookup_order"}}
    plane = plane_with(control_plane, host, sandbox=sandbox)
    async with gateway(plane) as client:
        body = (
            await client.post(
                RUNS,
                headers=auth,
                json=run_body(hosted_tool("code_interpreter"), client_tool, include_files=False),
            )
        ).json()
    assert body["status"] == "requires_action"
    assert [r.method for r in sandbox.requests] == ["POST"]  # no DELETE: resumable
    assert all("content_base64" not in f for f in body["files"])


async def test_sandbox_failures_are_tool_errors_and_the_run_continues(control_plane, host, auth):
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    host.replies = [
        completion(None, tool_calls=[tool_call("c1", "code_interpreter", {"code": "1"})]),
        completion("sandbox was down"),
    ]
    plane = plane_with(control_plane, host, sandbox=Recorder(down))
    async with gateway(plane) as client:
        body = (
            await client.post(RUNS, headers=auth, json=run_body(hosted_tool("code_interpreter")))
        ).json()
    assert body["status"] == "completed"
    step = tool_steps(body)[0]
    assert (step["ok"], step["error"]) == (False, "sandbox_unavailable")
    assert json.loads(tool_messages(host, 1)[0]["content"])["error"] == "sandbox_unavailable"


@pytest.mark.parametrize(
    ("status", "code"),
    [(429, "sandbox_busy"), (401, "sandbox_unavailable"), (500, "sandbox_error")],
)
async def test_sandbox_status_mapping(control_plane, host, auth, status, code):
    host.replies = [
        completion(None, tool_calls=[tool_call("c1", "code_interpreter", {"code": "1"})]),
        completion("x"),
    ]
    sandbox = Recorder(lambda r: httpx.Response(status, json={"error": {}}))
    plane = plane_with(control_plane, host, sandbox=sandbox)
    async with gateway(plane) as client:
        body = (
            await client.post(RUNS, headers=auth, json=run_body(hosted_tool("code_interpreter")))
        ).json()
    assert tool_steps(body)[0]["error"] == code


async def test_timeouts_and_code_errors_are_counted_in_metrics(control_plane, host, auth):
    replies = iter(
        [
            {
                "stdout": "partial",
                "stderr": "",
                "exit_code": None,
                "timed_out": True,
                "killed_reason": "timeout",
                "duration_ms": 30000,
                "files": [],
            },
            {
                "stdout": "",
                "stderr": "Traceback ...\nZeroDivisionError",
                "exit_code": 1,
                "timed_out": False,
                "duration_ms": 12,
                "files": [],
            },
        ]
    )
    sandbox = Recorder(
        lambda r: httpx.Response(200, json=next(replies))
        if r.method == "POST"
        else httpx.Response(200, json={})
    )
    host.replies = [
        completion(None, tool_calls=[tool_call("c1", "code_interpreter", {"code": "loop"})]),
        completion(None, tool_calls=[tool_call("c2", "code_interpreter", {"code": "1/0"})]),
        completion("ok"),
    ]
    plane = plane_with(control_plane, host, sandbox=sandbox)
    async with gateway(plane) as client:
        body = (
            await client.post(RUNS, headers=auth, json=run_body(hosted_tool("code_interpreter")))
        ).json()
        metrics = (await client.get("/metrics")).text
    timeout_step, error_step = tool_steps(body)
    assert (timeout_step["ok"], timeout_step["error"]) == (False, "execution_timeout")
    assert json.loads(tool_messages(host, 1)[0]["content"])["stdout"] == "partial"
    assert (error_step["ok"], error_step["exit_code"]) == (True, 1)
    assert (
        'fabric_dp_hosted_tool_executions_total{tool="code_interpreter",outcome="timeout"} 1'
        in metrics
    )
    assert (
        'fabric_dp_hosted_tool_executions_total{tool="code_interpreter",outcome="code_error"} 1'
        in metrics
    )
    assert 'fabric_dp_hosted_tool_bytes_total{tool="code_interpreter",kind="code"}' in metrics


async def test_streamed_tool_events_carry_sizes_not_content(control_plane, host, auth):
    host.replies = [
        completion(None, tool_calls=[tool_call("c1", "code_interpreter", {"code": SECRET_CODE})]),
        completion("done"),
    ]
    plane = plane_with(control_plane, host)
    async with gateway(plane) as client:
        response = await client.post(
            RUNS, headers=auth, json=run_body(hosted_tool("code_interpreter"), stream=True)
        )
    events = parse_sse(response.text)
    [completed] = [data for event, data in events if event == "tool.completed"]
    assert completed["hosted_tool"] == "code_interpreter"
    assert completed["stdout_bytes"] > 0
    streamed_steps = json.dumps([d for e, d in events if e != "run.completed"])
    assert SECRET_CODE not in streamed_steps and SECRET_STDOUT not in streamed_steps


async def test_files_over_the_run_cap_are_omitted(control_plane, host, auth):
    host.replies = [
        completion(None, tool_calls=[tool_call("c1", "code_interpreter", {"code": "1"})]),
        completion("ok"),
    ]
    plane = plane_with(control_plane, host, code_interpreter_max_run_file_bytes=10)
    async with gateway(plane) as client:
        body = (
            await client.post(RUNS, headers=auth, json=run_body(hosted_tool("code_interpreter")))
        ).json()
    assert body["files"][0]["omitted"] == "size_cap"
    assert "content_base64" not in body["files"][0]


# --------------------------------------------------------------------------- e2b


class FakeExecution:
    def __init__(self, stdout=None, stderr=None, results=None, error=None):
        self.logs = type("Logs", (), {"stdout": stdout or [], "stderr": stderr or []})()
        self.results = results or []
        self.error = error


class FakeSandbox:
    def __init__(self, kwargs: dict[str, Any]) -> None:
        self.kwargs = kwargs
        self.codes: list[str] = []
        self.killed = False
        self.next: Any = None

    async def run_code(self, code: str, timeout: float, request_timeout: float):
        self.codes.append(code)
        if isinstance(self.next, Exception):
            raise self.next
        return self.next or FakeExecution(stdout=["hello\n"])

    async def kill(self) -> bool:
        self.killed = True
        return True


class FakeFactory:
    def __init__(self) -> None:
        self.created: list[FakeSandbox] = []

    async def __call__(self, **kwargs: Any) -> FakeSandbox:
        sandbox = FakeSandbox(kwargs)
        self.created.append(sandbox)
        return sandbox


E2B = {
    "code_interpreter_backend": "e2b",
    "code_interpreter_url": "",
    "e2b_api_key": E2B_KEY,
    "e2b_template": "fabric-science",
    "e2b_domain": "e2b.internal.example",
}


async def test_e2b_backend_reuses_one_sandbox_per_run_and_kills_it(control_plane, host, auth):
    factory = FakeFactory()
    png_result = type("R", (), {"png": PNG, "jpeg": None, "is_main_result": False, "text": None})()
    host.replies = [
        completion(None, tool_calls=[tool_call("c1", "code_interpreter", {"code": "a = 1"})]),
        completion(None, tool_calls=[tool_call("c2", "code_interpreter", {"code": SECRET_CODE})]),
        completion("done"),
    ]
    plane = plane_with(control_plane, host, e2b_factory=factory, **E2B)
    async with gateway(plane) as client:
        first = factory  # noqa: F841 - readability
        body = (
            await client.post(
                RUNS,
                headers={**auth, "X-Fabric-Run-Id": "e2b-run"},
                json=run_body(hosted_tool("code_interpreter")),
            )
        ).json()
        [sandbox] = factory.created
        assert sandbox.codes == ["a = 1", SECRET_CODE]
        assert sandbox.killed is True

    kwargs = sandbox.kwargs
    assert kwargs["api_key"] == E2B_KEY
    assert kwargs["template"] == "fabric-science"
    assert kwargs["domain"] == "e2b.internal.example"
    assert kwargs["allow_internet_access"] is False
    assert "e2b-run" not in json.dumps(kwargs["metadata"])
    assert body["status"] == "completed"
    assert tool_steps(body)[0]["backend"] == "e2b"
    everything = json.dumps(body) + json.dumps(host.requests)
    assert E2B_KEY not in everything
    del png_result


async def test_e2b_results_errors_and_files_are_mapped(control_plane, host, auth):
    factory = FakeFactory()
    host.replies = [
        completion(None, tool_calls=[tool_call("c1", "code_interpreter", {"code": "plot"})]),
        completion(None, tool_calls=[tool_call("c2", "code_interpreter", {"code": "1/0"})]),
        completion("ok"),
    ]
    plane = plane_with(control_plane, host, e2b_factory=factory, **E2B)
    png = type("R", (), {"png": PNG, "jpeg": None, "is_main_result": False, "text": None})()
    main = type("R", (), {"png": None, "jpeg": None, "is_main_result": True, "text": "42"})()
    error = type(
        "E",
        (),
        {
            "name": "ZeroDivisionError",
            "value": "division by zero",
            "traceback": "Traceback\nZeroDivisionError: division by zero",
        },
    )()
    outcomes = iter(
        [FakeExecution(stdout=["out\n"], results=[png, main]), FakeExecution(error=error)]
    )

    original = FakeSandbox.run_code

    async def scripted(self, code, timeout, request_timeout):
        self.codes.append(code)
        return next(outcomes)

    FakeSandbox.run_code = scripted  # type: ignore[method-assign]
    try:
        async with gateway(plane) as client:
            body = (
                await client.post(
                    RUNS, headers=auth, json=run_body(hosted_tool("code_interpreter"))
                )
            ).json()
    finally:
        FakeSandbox.run_code = original  # type: ignore[method-assign]
    first = json.loads(tool_messages(host, 1)[0]["content"])
    assert first["stdout"] == "out\n" and first["result"] == "42"
    assert first["files"] == ["result_1.png"]
    second = json.loads(tool_messages(host, 2)[-1]["content"])
    assert second["exit_code"] == 1 and "ZeroDivisionError" in second["stderr"]
    assert body["files"][0]["content_base64"] == PNG


async def test_e2b_capacity_and_idle_ttl():
    config = make_settings(
        code_interpreter_enabled=True, e2b_max_sandboxes=1, e2b_idle_ttl_seconds=60, **E2B
    )
    now = [0.0]
    factory = FakeFactory()
    backend = E2BBackend(config, factory=factory, clock=lambda: now[0])
    one, two = ToolContext("a", "r1"), ToolContext("a", "r2")
    assert json.loads((await backend.execute(one, {"code": "1"})).content)["stdout"] == "hello\n"
    from fabric_data_plane.agent_tools import ToolError

    with pytest.raises(ToolError) as busy:
        await backend.execute(two, {"code": "1"})
    assert busy.value.code == "sandbox_busy"
    now[0] += 61
    await backend.execute(two, {"code": "1"})  # r1 swept, r2 admitted
    assert factory.created[0].killed is True
    assert backend.active() == 1


async def test_e2b_sdk_errors_never_echo_their_text(control_plane, host, auth):
    async def failing(**_kwargs):
        raise RuntimeError(f"bad key {E2B_KEY}")

    host.replies = [
        completion(None, tool_calls=[tool_call("c1", "code_interpreter", {"code": "1"})]),
        completion("x"),
    ]
    plane = plane_with(control_plane, host, e2b_factory=failing, **E2B)
    async with gateway(plane) as client:
        body = (
            await client.post(RUNS, headers=auth, json=run_body(hosted_tool("code_interpreter")))
        ).json()
    assert tool_steps(body)[0]["error"] == "sandbox_unavailable"
    assert E2B_KEY not in json.dumps(body) + json.dumps(host.requests)


# --------------------------------------------------------------------------- web_fetch

PAGE = f"""<html><head><title>Ops report</title><style>.x{{}}</style>
<script>var secret = 1;</script></head><body><nav>Home</nav>
<h1>Q3</h1><p>{PAGE_TEXT}.</p><table><tr><td>p50</td><td>120</td></tr></table></body></html>"""


def page(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"Content-Type": "text/html; charset=utf-8", "Set-Cookie": "sid=abc"},
        content=PAGE.encode(),
    )


async def fetch_run(control_plane, host, auth, url: str, web: Recorder, **settings: Any):
    host.replies = [
        completion(None, tool_calls=[tool_call("f1", "web_fetch", {"url": url})]),
        completion("ok"),
    ]
    plane = plane_with(control_plane, host, web=web, **settings)
    async with gateway(plane) as client:
        body = (
            await client.post(
                RUNS,
                headers={**auth, "Cookie": "caller=1"},
                json=run_body(hosted_tool("web_fetch")),
            )
        ).json()
    step = tool_steps(body)[0]
    content = json.loads(tool_messages(host, 1)[0]["content"]) if len(host.requests) > 1 else None
    return body, step, content


async def test_web_fetch_returns_readable_text_pinned_and_without_credentials(
    control_plane, host, auth
):
    web = Recorder(page)
    body, step, content = await fetch_run(
        control_plane, host, auth, "https://public.test/report?q=1", web
    )
    assert step["ok"] is True and step["http_status"] == 200
    assert content["title"] == "Ops report"
    assert PAGE_TEXT in content["text"]
    assert "var secret" not in content["text"] and ".x{}" not in content["text"]
    [request] = web.requests
    # Connected to the vetted address; the name travels in Host and TLS SNI.
    assert request.url.host == "93.184.215.14"
    assert request.headers["host"] == "public.test"
    assert request.extensions["sni_hostname"] == "public.test"
    assert request.url.query == b"q=1"
    assert request.headers["user-agent"].startswith("Fabric-WebFetch/")
    headers = {k.lower() for k in request.headers}
    assert "authorization" not in headers and "cookie" not in headers
    assert PAGE_TEXT not in json.dumps(body["steps"])
    assert "public.test" not in json.dumps(body["steps"])


async def test_web_fetch_never_stores_cookies(control_plane, host, auth):
    web = Recorder(page)
    host.replies = [
        completion(None, tool_calls=[tool_call("f1", "web_fetch", {"url": "http://public.test/"})]),
        completion(None, tool_calls=[tool_call("f2", "web_fetch", {"url": "http://public.test/"})]),
        completion("ok"),
    ]
    plane = plane_with(control_plane, host, web=web)
    async with gateway(plane) as client:
        await client.post(RUNS, headers=auth, json=run_body(hosted_tool("web_fetch")))
    assert len(web.requests) == 2
    assert "cookie" not in {k.lower() for k in web.requests[1].headers}


@pytest.mark.parametrize(
    ("url", "error"),
    [
        ("http://10.0.0.5/", "host_not_allowed"),  # private literal, admitted only by *
        ("http://169.254.169.254/latest/meta-data/", "host_not_allowed"),
        ("http://[fd00:ec2::254]/", "host_not_allowed"),
        ("http://metadata.google.internal/", "host_not_allowed"),
        ("http://localhost:8081/usage", "host_not_allowed"),
        ("http://127.0.0.1/", "host_not_allowed"),
        ("http://rebind.test/", "host_not_allowed"),  # name -> link-local
        ("http://mixed.test/", "host_not_allowed"),  # one bad address is enough
        ("file:///etc/passwd", "invalid_url"),
        ("https://user:pw@public.test/", "invalid_url"),
        ("http://unknown.test/", "host_unreachable"),
    ],
)
async def test_web_fetch_ssrf_targets_are_refused(control_plane, host, auth, url, error):
    web = Recorder(page)
    _body, step, _content = await fetch_run(control_plane, host, auth, url, web)
    assert (step["ok"], step["error"]) == (False, error)
    assert web.requests == []


async def test_private_names_need_an_explicit_entry(control_plane, host, auth):
    web = Recorder(page)
    _b, step, _c = await fetch_run(control_plane, host, auth, "http://intranet.test/", web)
    assert step["ok"] is True  # listed explicitly: an intranet page is allowed
    assert web.requests[0].url.host == "10.1.2.3"
    web = Recorder(page)
    _b, step, _c = await fetch_run(
        control_plane,
        ScriptedHost(),
        auth,
        "http://intranet.test/",
        web,
        web_fetch_allowed_hosts="*",
    )
    assert (step["ok"], step["error"]) == (False, "host_not_allowed")
    assert web.requests == []


async def test_web_fetch_allowlist_without_wildcard(control_plane, host, auth):
    web = Recorder(page)
    _b, step, _c = await fetch_run(
        control_plane,
        host,
        auth,
        "http://public.test/",
        web,
        web_fetch_allowed_hosts="docs.example.com",
    )
    assert step["error"] == "host_not_allowed"


async def test_redirects_are_rechecked_and_bounded(control_plane, host, auth):
    to_metadata = Recorder(
        lambda r: httpx.Response(
            302, headers={"Location": "http://169.254.169.254/latest/meta-data"}
        )
    )
    _b, step, _c = await fetch_run(control_plane, host, auth, "http://public.test/", to_metadata)
    assert step["error"] == "host_not_allowed"
    assert len(to_metadata.requests) == 1

    def hop(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/final":
            return page(request)
        return httpx.Response(301, headers={"Location": "/final"})

    follows = Recorder(hop)
    _b, step, content = await fetch_run(
        control_plane, ScriptedHost(), auth, "http://public.test/start", follows
    )
    assert step["ok"] is True and step["redirects"] == 1
    assert content["url"] == "http://public.test/final"

    loop = Recorder(lambda r: httpx.Response(302, headers={"Location": "/again"}))
    _b, step, _c = await fetch_run(
        control_plane,
        ScriptedHost(),
        auth,
        "http://public.test/",
        loop,
        web_fetch_max_redirects=2,
    )
    assert step["error"] == "too_many_redirects"
    assert len(loop.requests) == 3


async def test_dns_rebinding_cannot_move_the_connection(control_plane, host, auth):
    answers = iter([["93.184.215.14"], ["127.0.0.1"]])

    async def rebinding(host_: str, _port: int) -> list[str]:
        return next(answers)

    web = Recorder(page)
    host.replies = [
        completion(None, tool_calls=[tool_call("f1", "web_fetch", {"url": "http://flip.test/"})]),
        completion("ok"),
    ]
    plane = plane_with(control_plane, host, web=web, resolve=rebinding)
    async with gateway(plane) as client:
        await client.post(RUNS, headers=auth, json=run_body(hosted_tool("web_fetch")))
    # Resolved once, connected to exactly the vetted address.
    assert [r.url.host for r in web.requests] == ["93.184.215.14"]


async def test_web_fetch_caps_bytes_and_text(control_plane, host, auth):
    big = Recorder(
        lambda r: httpx.Response(200, headers={"Content-Type": "text/plain"}, content=b"y" * 50_000)
    )
    _b, step, content = await fetch_run(
        control_plane,
        host,
        auth,
        "http://public.test/",
        big,
        web_fetch_max_bytes=4096,
        web_fetch_max_chars=1000,
    )
    assert step["truncated"] is True and step["fetched_bytes"] == 4096
    assert len(content["text"]) == 1000 and content["truncated"] is True


async def test_binary_content_is_refused(control_plane, host, auth):
    binary = Recorder(
        lambda r: httpx.Response(
            200, headers={"Content-Type": "application/octet-stream"}, content=b"\x00\x01"
        )
    )
    _b, step, _c = await fetch_run(control_plane, host, auth, "http://public.test/", binary)
    assert step["error"] == "unsupported_content_type"


def test_html_extraction_and_policy_parsing():
    title, text = html_to_text("<title> A  page </title><p>one</p><script>x()</script><p>two</p>")
    assert title == "A page"
    assert text == "one\n\ntwo"
    policy = HostPolicy.parse("*.example.com, Docs.Test.")
    assert policy.explicit("wiki.example.com") and policy.explicit("docs.test")
    assert not policy.admits("example.org")


def test_check_url_static_rules():
    policy = HostPolicy.parse("*")
    assert check_url("https://public.test:8443/x", policy).port == 8443
    from fabric_data_plane.agent_tools import ToolError

    for bad in ("http://0.0.0.0/", "http://[::1]/", "http://192.168.1.1/", "ftp://x.test/"):
        with pytest.raises(ToolError):
            check_url(bad, policy)


# --------------------------------------------------------------------------- web_search

SEARX = {
    "results": [
        {"title": f"Result {i}", "url": f"https://public.test/{i}", "content": f"snippet {i}"}
        for i in range(10)
    ]
}


async def search_run(control_plane, host, auth, service: Recorder, args=None, **settings):
    host.replies = [
        completion(
            None,
            tool_calls=[
                tool_call("s1", "web_search", args or {"query": "fabric latency", "count": 3})
            ],
        ),
        completion("ok"),
    ]
    plane = plane_with(control_plane, host, service=service, **settings)
    async with gateway(plane) as client:
        body = (
            await client.post(RUNS, headers=auth, json=run_body(hosted_tool("web_search")))
        ).json()

    content = json.loads(tool_messages(host, 1)[0]["content"])
    return body, tool_steps(body)[0], content


async def test_web_search_via_searxng(control_plane, host, auth):
    service = Recorder(lambda r: httpx.Response(200, json=SEARX))
    body, step, content = await search_run(control_plane, host, auth, service)
    [request] = service.requests
    assert str(request.url).startswith("http://searxng.fabric.svc:8080/search?")
    assert request.url.params["q"] == "fabric latency"
    assert request.url.params["format"] == "json"
    assert [r["title"] for r in content["results"]] == ["Result 0", "Result 1", "Result 2"]
    assert content["results"][0] == {
        "title": "Result 0",
        "url": "https://public.test/0",
        "snippet": "snippet 0",
    }
    assert step["results"] == 3 and step["backend"] == "searxng"
    assert "fabric latency" not in json.dumps(body["steps"])
    assert "snippet 0" not in json.dumps(body["steps"])


async def test_web_search_count_is_capped_by_the_operator(control_plane, host, auth):
    service = Recorder(lambda r: httpx.Response(200, json=SEARX))
    _b, _s, content = await search_run(
        control_plane, host, auth, service, {"query": "q", "count": 15}, web_search_max_results=4
    )
    assert len(content["results"]) == 4


async def test_web_search_generic_http_template_with_key(control_plane, host, auth):
    service = Recorder(
        lambda r: httpx.Response(
            200, json={"results": [{"title": "T", "url": "https://public.test/t", "snippet": "S"}]}
        )
    )
    _b, step, content = await search_run(
        control_plane,
        host,
        auth,
        service,
        web_backend="direct",
        web_search_url="https://search.internal/api?q={query}&n={count}",
        web_search_api_key="search-key",
        web_search_api_key_header="X-Api-Key",
    )
    [request] = service.requests
    assert request.url.params["q"] == "fabric latency" and request.url.params["n"] == "3"
    assert request.headers["x-api-key"] == "search-key"
    assert content["results"][0]["snippet"] == "S"


async def test_web_search_backend_errors(control_plane, host, auth):
    service = Recorder(lambda r: httpx.Response(503))
    _b, step, content = await search_run(control_plane, host, auth, service)
    assert (step["ok"], step["error"]) == (False, "backend_error")


# --------------------------------------------------------------------------- firecrawl

FIRECRAWL = {
    "web_backend": "firecrawl",
    "web_search_url": "",
    "firecrawl_url": "http://firecrawl.fabric.svc:3002",
    "firecrawl_api_key": "fc-secret",
}


async def test_firecrawl_search_v1_and_v2_shapes(control_plane, host, auth):
    v1 = Recorder(
        lambda r: httpx.Response(
            200,
            json={
                "success": True,
                "data": [{"title": "A", "url": "https://public.test/a", "description": "desc"}],
            },
        )
    )
    _b, step, content = await search_run(control_plane, host, auth, v1, **FIRECRAWL)
    [request] = v1.requests
    assert request.url.path == "/v1/search"
    assert json.loads(request.content) == {"query": "fabric latency", "limit": 3}
    assert request.headers["authorization"] == "Bearer fc-secret"
    assert content["results"] == [{"title": "A", "url": "https://public.test/a", "snippet": "desc"}]

    v2 = Recorder(
        lambda r: httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "web": [{"title": "B", "url": "https://public.test/b", "description": "d2"}]
                },
            },
        )
    )
    _b, _s, content = await search_run(
        control_plane, ScriptedHost(), auth, v2, firecrawl_search_path="/v2/search", **FIRECRAWL
    )
    assert v2.requests[0].url.path == "/v2/search"
    assert content["results"][0]["title"] == "B"


async def test_firecrawl_scrape_prechecks_targets(control_plane, host, auth):
    scrape = Recorder(
        lambda r: httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "markdown": "# Ops\n" + PAGE_TEXT,
                    "metadata": {"title": "Ops", "statusCode": 200},
                },
            },
        )
    )
    host.replies = [
        completion(
            None,
            tool_calls=[
                tool_call("f1", "web_fetch", {"url": "https://public.test/r"}),
                tool_call("f2", "web_fetch", {"url": "http://10.9.9.9/admin"}),
                tool_call("f3", "web_fetch", {"url": "http://rebind.test/"}),
            ],
        ),
        completion("ok"),
    ]
    plane = plane_with(control_plane, host, service=scrape, **FIRECRAWL)
    async with gateway(plane) as client:
        body = (
            await client.post(RUNS, headers=auth, json=run_body(hosted_tool("web_fetch")))
        ).json()
    ok, private, rebound = tool_steps(body)
    assert ok["ok"] is True and ok["backend"] == "firecrawl"
    assert (private["error"], rebound["error"]) == ("host_not_allowed", "host_not_allowed")
    # Only the public target was ever handed to Firecrawl.
    [request] = scrape.requests
    assert request.url.path == "/v1/scrape"
    assert json.loads(request.content)["url"] == "https://public.test/r"
    content = json.loads(tool_messages(host, 1)[0]["content"])
    assert PAGE_TEXT in content["text"] and content["title"] == "Ops"


def test_e2b_sdk_accepts_the_arguments_the_backend_passes():
    """Contract with the pinned SDK (skipped where the optional extra is not installed)."""
    sdk = pytest.importorskip("e2b_code_interpreter")
    import inspect

    create = inspect.signature(sdk.AsyncSandbox.create)
    for name in ("template", "timeout", "metadata", "allow_internet_access"):
        assert name in create.parameters
    run_code = inspect.signature(sdk.AsyncSandbox.run_code)
    assert {"timeout", "request_timeout"} <= set(run_code.parameters)
    from e2b.connection_config import ApiParams

    assert {"api_key", "domain"} <= set(ApiParams.__annotations__)
    config = make_settings(code_interpreter_enabled=True, **E2B)
    kwargs = E2BBackend(config)._create_kwargs("k" * 64)
    assert set(kwargs) <= set(create.parameters) | set(ApiParams.__annotations__)
