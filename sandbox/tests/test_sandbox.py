"""Sandbox executor and API: isolation, limits, sessions, authentication."""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import os
import sys
import time

import httpx
import pytest

from fabric_sandbox.app import TOKEN_HEADER, create_app
from fabric_sandbox.config import Settings
from fabric_sandbox.executor import InvalidRequest, Sandbox, SandboxBusy, harden_process
from tests.conftest import IS_ROOT, TOKEN

HAS_MATPLOTLIB = importlib.util.find_spec("matplotlib") is not None
LINUX = sys.platform.startswith("linux")


async def run(box: Sandbox, code: str, session: str = "s1", **kw):
    return await box.execute(session, code, **kw)


# --------------------------------------------------------------------------- basics


async def test_runs_code_and_reports_output(make_settings):
    box = Sandbox(make_settings())
    result = await run(box, "print(6 * 7)\nimport sys; print('warn', file=sys.stderr)")
    assert result.stdout == "42\n"
    assert result.stderr == "warn\n"
    assert result.exit_code == 0
    assert result.timed_out is False
    assert result.duration_ms > 0


async def test_exceptions_report_a_traceback_without_runner_frames(make_settings):
    box = Sandbox(make_settings())
    result = await run(box, "def f():\n    raise ValueError('boom')\nf()")
    assert result.exit_code == 1
    assert "ValueError: boom" in result.stderr
    assert 'File "<code>"' in result.stderr
    assert "runner.py" not in result.stderr


async def test_sys_exit_status_is_reported(make_settings):
    box = Sandbox(make_settings())
    assert (await run(box, "import sys; sys.exit(3)")).exit_code == 3


async def test_stdin_is_closed(make_settings):
    box = Sandbox(make_settings())
    result = await run(box, "try:\n    input()\nexcept EOFError:\n    print('eof')")
    assert result.stdout == "eof\n"


async def test_invalid_requests(make_settings):
    box = Sandbox(make_settings(max_code_bytes=100))
    with pytest.raises(InvalidRequest):
        await run(box, "")
    with pytest.raises(InvalidRequest):
        await run(box, "x = 1\n" * 50)
    with pytest.raises(InvalidRequest):
        await run(box, "print(1)", session="../escape")


# --------------------------------------------------------------------------- limits


async def test_wall_clock_timeout_kills_the_process_group(make_settings):
    box = Sandbox(make_settings())
    code = (
        "import subprocess, sys, time\n"
        # A background child in the same group must die with the execution.
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "open('child.pid', 'w').write(str(p.pid))\n"
        "print('started', flush=True)\n"
        "time.sleep(60)\n"
    )
    started = time.monotonic()
    result = await run(box, code, timeout_seconds=1.5)
    assert time.monotonic() - started < 10
    assert result.timed_out is True
    assert result.killed_reason == "timeout"
    assert result.stdout == "started\n"
    workdir = next(iter(box._sessions.values())).workdir
    child = int((workdir / "child.pid").read_text())
    await asyncio.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(child, 0)
        # A zombie is reaped by init; if still present it must at least not be running.
        with open(f"/proc/{child}/stat") as stat:
            assert stat.read().split()[2] == "Z"
            raise ProcessLookupError


@pytest.mark.skipif(not LINUX, reason="RLIMIT_CPU semantics are Linux-specific")
async def test_cpu_limit_kills_a_busy_loop(make_settings):
    box = Sandbox(make_settings(cpu_seconds=1))
    result = await run(box, "while True:\n    pass", timeout_seconds=20)
    assert result.timed_out is False
    assert result.killed_reason == "cpu_limit"
    assert result.exit_code is not None and result.exit_code < 0


@pytest.mark.skipif(not LINUX, reason="RLIMIT_AS is enforced reliably on Linux")
async def test_memory_limit_raises_memory_error(make_settings):
    box = Sandbox(make_settings(memory_bytes=256 * 1024**2))
    result = await run(box, "x = bytearray(1024 * 1024 * 1024)\nprint('allocated')")
    assert "allocated" not in result.stdout
    assert "MemoryError" in result.stderr
    assert result.exit_code == 1


async def test_file_size_limit(make_settings):
    box = Sandbox(make_settings(file_size_bytes=64 * 1024))
    code = (
        "try:\n"
        "    open('big.bin', 'wb').write(b'x' * (1024 * 1024))\n"
        "except OSError as exc:\n"
        "    print('refused', type(exc).__name__)\n"
    )
    result = await run(box, code)
    # Python ignores SIGXFSZ, so the write fails with EFBIG instead of killing the child.
    assert "refused" in result.stdout or result.killed_reason == "file_size_limit"


async def test_open_files_limit(make_settings):
    box = Sandbox(make_settings(open_files=32))
    code = (
        "import resource\n"
        "print(resource.getrlimit(resource.RLIMIT_NOFILE))\n"
        "files = []\n"
        "try:\n"
        "    for i in range(100):\n"
        "        files.append(open('f.txt', 'w'))\n"
        "except OSError:\n"
        "    print('limited at', len(files))\n"
    )
    result = await run(box, code)
    assert "(32, 32)" in result.stdout
    assert "limited at" in result.stdout


async def test_limits_cannot_be_raised_by_user_code(make_settings):
    box = Sandbox(make_settings(cpu_seconds=5))
    code = (
        "import resource\n"
        "try:\n"
        "    resource.setrlimit(resource.RLIMIT_CPU, (100, 100))\n"
        "    print('raised')\n"
        "except (ValueError, OSError):\n"
        "    print('refused')\n"
        "print(resource.getrlimit(resource.RLIMIT_CPU))\n"
    )
    result = await run(box, code)
    if IS_ROOT:
        pytest.skip("root keeps CAP_SYS_RESOURCE; production never runs user code as root")
    assert "refused" in result.stdout
    assert "(5, 6)" in result.stdout


async def test_output_is_truncated_at_the_cap_and_drained(make_settings):
    box = Sandbox(make_settings(max_output_bytes=1000))
    result = await run(
        box, "import sys\nsys.stdout.write('x' * 500000)\nprint('done', file=sys.stderr)"
    )
    assert len(result.stdout) == 1000
    assert result.stdout_truncated is True
    assert result.stderr == "done\n"
    assert result.stderr_truncated is False
    assert result.exit_code == 0


# --------------------------------------------------------------------------- environment


async def test_no_environment_leaks_into_user_code(make_settings, monkeypatch):
    monkeypatch.setenv("FABRIC_SANDBOX_TOKEN", "service-token-must-not-leak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "cloud-secret")
    monkeypatch.setenv("PYTHONPATH", "/attacker")
    monkeypatch.setenv("PYTHONSTARTUP", "/attacker/startup.py")
    box = Sandbox(make_settings())
    result = await run(
        box,
        "import os, sys\nprint(sorted(os.environ))\nprint(sys.flags.isolated, sys.flags.no_site)",
    )
    assert "service-token-must-not-leak" not in result.stdout
    assert "AWS_SECRET_ACCESS_KEY" not in result.stdout
    assert "FABRIC_SANDBOX_TOKEN" not in result.stdout
    assert "PYTHONPATH" not in result.stdout
    assert result.stdout.strip().endswith("1 1")


@pytest.mark.skipif(not LINUX, reason="/proc and prctl are Linux-specific")
async def test_parent_environment_is_not_readable_through_proc(make_settings):
    harden_process()  # what __main__ does before serving
    box = Sandbox(make_settings())
    code = (
        "import os\n"
        "try:\n"
        "    data = open(f'/proc/{os.getppid()}/environ', 'rb').read()\n"
        "    print('readable', len(data))\n"
        "except OSError as exc:\n"
        "    print('denied', type(exc).__name__)\n"
    )
    result = await run(box, code)
    if IS_ROOT:
        pytest.skip("root bypasses dumpable checks; covered by the uid-switch test")
    assert result.stdout.startswith("denied")


# A system interpreter every uid can execute (a uv-managed one may live under /root).
SYSTEM_PYTHON = next(
    (
        p
        for p in ("/usr/bin/python3.12", "/usr/local/bin/python3.12", "/usr/bin/python3")
        if os.access(p, os.X_OK)
    ),
    None,
)


@pytest.mark.skipif(not (IS_ROOT and LINUX and SYSTEM_PYTHON), reason="needs root")
async def test_runs_as_configured_unprivileged_uid(make_settings):
    box = Sandbox(
        make_settings(run_as_uid=65534, run_as_gid=65534, allow_root=False, python=SYSTEM_PYTHON)
    )
    code = (
        "import os\n"
        "print(os.getuid(), os.getgid(), os.getgroups())\n"
        "try:\n"
        "    open(f'/proc/{os.getppid()}/environ', 'rb').read()\n"
        "    print('readable')\n"
        "except OSError:\n"
        "    print('denied')\n"
        "try:\n"
        "    os.listdir(os.path.dirname(os.getcwd()))\n"
        "    print('listed')\n"
        "except OSError:\n"
        "    print('unlistable')\n"
    )
    result = await run(box, code)
    assert result.stdout.splitlines() == ["65534 65534 []", "denied", "unlistable"]


def test_refuses_to_run_user_code_as_root(make_settings):
    if not IS_ROOT:
        pytest.skip("only meaningful as root")
    with pytest.raises(RuntimeError, match="refusing to run user code as root"):
        Sandbox(make_settings(allow_root=False))


def test_settings_require_a_token(root_dir):
    with pytest.raises(ValueError, match="FABRIC_SANDBOX_TOKEN"):
        Settings(token="", root_dir=root_dir)
    with pytest.raises(ValueError):
        Settings(token="short", root_dir=root_dir)


# --------------------------------------------------------------------------- sessions


async def test_files_persist_within_a_session_and_not_across(make_settings):
    box = Sandbox(make_settings())
    first = await run(box, "open('data.csv', 'w').write('a,b\\n1,2\\n')", session="run-a")
    assert [f["name"] for f in first.files] == ["data.csv"]
    second = await run(box, "print(open('data.csv').read().splitlines()[1])", session="run-a")
    assert second.stdout == "1,2\n"
    assert second.files == []  # unchanged files are not reported again
    other = await run(box, "import os; print(os.listdir('.'))", session="run-b")
    assert other.stdout == "[]\n"


async def test_session_directories_are_unguessable(make_settings):
    box = Sandbox(make_settings())
    await run(box, "print(1)", session="predictable-run-id")
    names = [p.name for p in box.sessions_dir.iterdir()]
    assert all("predictable" not in name for name in names)
    assert len(next(n for n in names if not n.endswith(".cache"))) == 64


async def test_sessions_expire_after_idle_ttl(make_settings):
    now = [1000.0]
    box = Sandbox(make_settings(session_ttl_seconds=60), clock=lambda: now[0])
    await run(box, "open('keep.txt', 'w').write('x')", session="ttl")
    workdir = box._sessions["ttl"].workdir
    now[0] += 30
    assert box.sweep() == 0
    assert workdir.exists()
    now[0] += 61
    assert box.sweep() == 1
    assert not workdir.exists()
    result = await run(box, "import os; print(os.listdir('.'))", session="ttl")
    assert result.stdout == "[]\n"


async def test_session_storage_cap_removes_new_files(make_settings):
    box = Sandbox(make_settings(session_max_bytes=10_000))
    await run(box, "open('small.txt', 'w').write('x' * 100)", session="cap")
    result = await run(box, "open('big.txt', 'w').write('x' * 50_000)", session="cap")
    assert result.error == "session_storage_exceeded"
    assert result.files == []
    listing = await run(box, "import os; print(sorted(os.listdir('.')))", session="cap")
    assert listing.stdout == "['small.txt']\n"


async def test_session_table_evicts_least_recently_used(make_settings):
    box = Sandbox(make_settings(max_sessions=2))
    for name in ("one", "two", "three"):
        await run(box, "print(1)", session=name)
    assert set(box._sessions) == {"two", "three"}


async def test_symlinked_outputs_are_not_read(make_settings, tmp_path):
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"not for the model")
    box = Sandbox(make_settings())
    result = await run(box, f"import os; os.symlink({str(secret)!r}, 'leak.png')")
    assert all(f.get("content_base64") is None for f in result.files)


async def test_concurrency_limit_refuses_when_busy(make_settings):
    box = Sandbox(make_settings(max_concurrent=1, queue_timeout_seconds=0.2))
    slow = asyncio.create_task(run(box, "import time; time.sleep(2)", session="slow"))
    await asyncio.sleep(0.3)
    with pytest.raises(SandboxBusy):
        await run(box, "print(1)", session="fast")
    assert (await slow).exit_code == 0
    assert box.counters["busy"] == 1


# --------------------------------------------------------------------------- plots


@pytest.mark.skipif(not HAS_MATPLOTLIB, reason="matplotlib not installed")
async def test_matplotlib_figures_are_returned_as_png(make_settings):
    box = Sandbox(make_settings(memory_bytes=4 * 1024**3))
    code = (
        "import matplotlib.pyplot as plt\n"
        "plt.plot([1, 2, 3], [2, 4, 1])\n"
        "plt.title('demo')\n"
        "plt.show()\n"
    )
    result = await run(box, code, timeout_seconds=60)
    assert result.exit_code == 0, result.stderr
    [figure] = result.files
    assert figure["name"] == "figure_1.png"
    assert figure["mime_type"] == "image/png"
    assert base64.b64decode(figure["content_base64"]).startswith(b"\x89PNG")


@pytest.mark.skipif(not HAS_MATPLOTLIB, reason="matplotlib not installed")
async def test_explicit_savefig_is_not_duplicated_and_size_capped(make_settings):
    box = Sandbox(make_settings(memory_bytes=4 * 1024**3, max_returned_file_bytes=100))
    code = "import matplotlib.pyplot as plt\n" "plt.plot([1, 2])\n" "plt.savefig('chart.png')\n"
    result = await run(box, code, timeout_seconds=60)
    [chart] = result.files
    assert chart["name"] == "chart.png"
    assert chart["omitted"] == "size_cap"
    assert "content_base64" not in chart


# --------------------------------------------------------------------------- HTTP API


@pytest.fixture
def api(make_settings):
    settings = make_settings()
    app = create_app(settings, Sandbox(settings))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://sandbox")


async def test_api_requires_the_shared_secret(api):
    async with api as client:
        body = {"session_id": "s", "code": "print(1)"}
        assert (await client.post("/v1/execute", json=body)).status_code == 401
        wrong = await client.post("/v1/execute", json=body, headers={TOKEN_HEADER: "nope"})
        assert wrong.status_code == 401
        ok = await client.post("/v1/execute", json=body, headers={TOKEN_HEADER: TOKEN})
        assert ok.status_code == 200
        assert ok.json()["stdout"] == "1\n"
        assert (await client.delete("/v1/sessions/s")).status_code == 401
        assert (await client.get("/healthz")).status_code == 200


async def test_api_validates_requests(api):
    async with api as client:
        headers = {TOKEN_HEADER: TOKEN}
        bad = await client.post("/v1/execute", json={"session_id": "s"}, headers=headers)
        assert bad.status_code == 400
        bad = await client.post(
            "/v1/execute",
            json={"session_id": "s", "code": "1", "timeout_seconds": -1},
            headers=headers,
        )
        assert bad.status_code == 400
        await client.post("/v1/execute", json={"session_id": "s", "code": "1"}, headers=headers)
        deleted = await client.delete("/v1/sessions/s", headers=headers)
        assert deleted.json() == {"deleted": True}
        metrics = (await client.get("/metrics")).text
        assert "fabric_sandbox_executions_total 1" in metrics
