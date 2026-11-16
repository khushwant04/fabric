"""File search: /v1/files, /v1/vector_stores and the hosted file_search agent tool."""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest

from fabric_data_plane.hosted_tools.text import (
    ExtractionError,
    chunk_text,
    extract_text,
    pdf_supported,
)
from fabric_data_plane.hosted_tools.vector_store import FileStore, Limits
from fabric_data_plane.registry import Deployment
from tests.conftest import ACCOUNT_A, ACCOUNT_B, UPSTREAM
from tests.scripted_host import (
    ScriptedHost,
    build_plane,
    completion,
    default_registry,
    gateway,
    tool_call,
)

RUNS = "/v1/agent/runs"

HANDBOOK = """# Employee handbook

## Vacation
Full-time staff accrue 25 vacation days per year. Vacation requests go to your manager at
least two weeks ahead. Unused vacation days carry over until March 31.

## Expenses
Submit expense reports within 30 days. Meals while travelling are reimbursed up to 60 EUR
per day. Keep every receipt.
"""

SECURITY = """# Security policy

Passwords must be at least 14 characters and rotated when compromised. Report a lost laptop
to the security desk within one hour. Never share credentials over chat.
"""


class Host(ScriptedHost):
    """Chat completions from the script, plus a deterministic embeddings endpoint."""

    GROUPS = (
        ("vacation", "holiday", "leave", "time", "off", "pto"),
        ("expense", "expenses", "reimbursed", "receipt", "meals", "money"),
        ("password", "passwords", "credentials", "security", "laptop", "secret"),
    )

    def __init__(self) -> None:
        super().__init__()
        self.embedding_calls: list[dict[str, Any]] = []
        self.embeddings_down = False

    @classmethod
    def vector(cls, text: str) -> list[float]:
        words = text.lower().replace(".", " ").replace(",", " ").split()
        return [sum(word in group for word in words) + 0.01 for group in cls.GROUPS]

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/embeddings"):
            payload = json.loads(request.content)
            self.embedding_calls.append(
                {"payload": payload, "headers": {k.lower(): v for k, v in request.headers.items()}}
            )
            if self.embeddings_down:
                return httpx.Response(500, json={"error": {"code": "boom"}})
            data = [
                {"object": "embedding", "index": i, "embedding": self.vector(text)}
                for i, text in enumerate(payload["input"])
            ]
            tokens = sum(len(t.split()) for t in payload["input"])
            return httpx.Response(
                200,
                json={
                    "object": "list",
                    "data": data,
                    "model": payload["model"],
                    "usage": {"prompt_tokens": tokens, "total_tokens": tokens},
                },
            )
        return super().handler(request)


EMBED_DEPLOYMENT = Deployment(
    deployment_id=uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
    account_id=ACCOUNT_A,
    model_alias="embed-model",
    upstream_url=UPSTREAM,
    upstream_model="internal-embedder",
)


@pytest.fixture
def host() -> Host:
    return Host()


@pytest.fixture
def auth(signing_key) -> dict[str, str]:
    return {"Authorization": f"Bearer {signing_key.issue()}"}


@pytest.fixture
def auth_b(signing_key) -> dict[str, str]:
    return {"Authorization": f"Bearer {signing_key.issue(account_id=ACCOUNT_B)}"}


def make_plane(control_plane, host, tmp_path, **settings):
    values = {"file_search_store_path": str(tmp_path / "files")}
    values.update(settings)
    return build_plane(
        control_plane, host, registry=default_registry([EMBED_DEPLOYMENT]), **values
    )


async def upload(client, headers, name="handbook.md", content=HANDBOOK, purpose="assistants"):
    return await client.post(
        "/v1/files",
        headers=headers,
        files={"file": (name, content.encode() if isinstance(content, str) else content)},
        data={"purpose": purpose},
    )


async def make_store(client, headers, files: dict[str, str], **extra) -> str:
    ids = []
    for name, content in files.items():
        response = await upload(client, headers, name, content)
        assert response.status_code == 200, response.text
        ids.append(response.json()["id"])
    response = await client.post(
        "/v1/vector_stores", headers=headers, json={"name": "kb", "file_ids": ids, **extra}
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


# --------------------------------------------------------------------------- CRUD + isolation


async def test_file_and_store_crud(control_plane, host, auth, tmp_path):
    async with gateway(make_plane(control_plane, host, tmp_path)) as client:
        created = await upload(client, auth)
        assert created.status_code == 200, created.text
        file = created.json()
        assert file["object"] == "file" and file["id"].startswith("file-")
        assert file["filename"] == "handbook.md" and file["bytes"] == len(HANDBOOK.encode())
        assert file["purpose"] == "assistants" and file["status"] == "processed"
        assert (await client.get(f"/v1/files/{file['id']}", headers=auth)).json() == file
        listed = (await client.get("/v1/files", headers=auth)).json()
        assert listed["object"] == "list" and [f["id"] for f in listed["data"]] == [file["id"]]

        store = (await client.post("/v1/vector_stores", headers=auth, json={"name": "kb"})).json()
        assert store["object"] == "vector_store" and store["id"].startswith("vs_")
        assert store["file_counts"]["total"] == 0 and store["status"] == "completed"
        attached = await client.post(
            f"/v1/vector_stores/{store['id']}/files", headers=auth, json={"file_id": file["id"]}
        )
        assert attached.status_code == 200, attached.text
        assert attached.json()["status"] == "completed"
        assert attached.json()["chunk_count"] >= 1
        # Attaching again is idempotent.
        again = await client.post(
            f"/v1/vector_stores/{store['id']}/files", headers=auth, json={"file_id": file["id"]}
        )
        assert again.status_code == 200 and again.json()["id"] == file["id"]
        fetched = (await client.get(f"/v1/vector_stores/{store['id']}", headers=auth)).json()
        assert fetched["file_counts"]["completed"] == 1 and fetched["usage_bytes"] > 0
        assert fetched["ranking"] == "bm25"
        files = (await client.get(f"/v1/vector_stores/{store['id']}/files", headers=auth)).json()
        assert [f["id"] for f in files["data"]] == [file["id"]]

        hits = (
            await client.post(
                f"/v1/vector_stores/{store['id']}/search",
                headers=auth,
                json={"query": "how many vacation days", "max_num_results": 2},
            )
        ).json()
        assert hits["object"] == "vector_store.search_results.page"
        top = hits["data"][0]
        assert top["file_id"] == file["id"] and top["filename"] == "handbook.md"
        assert "25 vacation days" in top["content"][0]["text"] and 0 < top["score"] <= 1

        detached = await client.delete(
            f"/v1/vector_stores/{store['id']}/files/{file['id']}", headers=auth
        )
        assert detached.json()["deleted"] is True
        empty = await client.post(
            f"/v1/vector_stores/{store['id']}/search", headers=auth, json={"query": "vacation"}
        )
        assert empty.json()["data"] == []
        deleted = await client.delete(f"/v1/vector_stores/{store['id']}", headers=auth)
        assert deleted.json() == {
            "id": store["id"], "object": "vector_store.deleted", "deleted": True
        }
        missing = await client.get(f"/v1/vector_stores/{store['id']}", headers=auth)
        assert missing.status_code == 404
        gone = await client.delete(f"/v1/files/{file['id']}", headers=auth)
        assert gone.json() == {"id": file["id"], "object": "file", "deleted": True}
        assert (await client.get(f"/v1/files/{file['id']}", headers=auth)).status_code == 404
    assert not list((tmp_path / "files" / "blobs" / str(ACCOUNT_A)).iterdir())


async def test_another_account_cannot_see_or_search_a_store(
    control_plane, host, auth, auth_b, tmp_path
):
    async with gateway(make_plane(control_plane, host, tmp_path)) as client:
        store_id = await make_store(client, auth, {"handbook.md": HANDBOOK})
        file_id = (await client.get("/v1/files", headers=auth)).json()["data"][0]["id"]

        # Foreign ids answer exactly like ids that never existed.
        def same(foreign: httpx.Response, missing: httpx.Response) -> None:
            assert foreign.status_code == missing.status_code == 404
            assert foreign.json() == missing.json()

        same(
            await client.get(f"/v1/vector_stores/{store_id}", headers=auth_b),
            await client.get("/v1/vector_stores/vs_doesnotexist", headers=auth_b),
        )
        same(
            await client.post(
                f"/v1/vector_stores/{store_id}/search", headers=auth_b, json={"query": "vacation"}
            ),
            await client.post(
                "/v1/vector_stores/vs_nope/search", headers=auth_b, json={"query": "vacation"}
            ),
        )
        same(
            await client.get(f"/v1/files/{file_id}", headers=auth_b),
            await client.get("/v1/files/file-nope", headers=auth_b),
        )
        same(
            await client.delete(f"/v1/files/{file_id}", headers=auth_b),
            await client.delete("/v1/files/file-nope", headers=auth_b),
        )
        same(
            await client.delete(f"/v1/vector_stores/{store_id}", headers=auth_b),
            await client.delete("/v1/vector_stores/vs_nope", headers=auth_b),
        )
        # B cannot attach A's file to its own store, nor its file to A's store.
        own = (await client.post("/v1/vector_stores", headers=auth_b, json={})).json()["id"]
        same(
            await client.post(
                f"/v1/vector_stores/{own}/files", headers=auth_b, json={"file_id": file_id}
            ),
            await client.post(
                f"/v1/vector_stores/{own}/files", headers=auth_b, json={"file_id": "file-nope"}
            ),
        )
        b_file = (await upload(client, auth_b, "b.md", "vacation vacation")).json()["id"]
        foreign_store = await client.post(
            f"/v1/vector_stores/{store_id}/files", headers=auth_b, json={"file_id": b_file}
        )
        assert foreign_store.status_code == 404
        assert foreign_store.json()["error"]["code"] == "vector_store_not_found"
        # Listings are per account.
        b_stores = (await client.get("/v1/vector_stores", headers=auth_b)).json()["data"]
        assert [s["id"] for s in b_stores] == [own]
        b_files = (await client.get("/v1/files", headers=auth_b)).json()["data"]
        assert [f["id"] for f in b_files] == [b_file]
        # A's store still answers A.
        assert (await client.get(f"/v1/vector_stores/{store_id}", headers=auth)).status_code == 200


async def test_routes_need_a_valid_inference_token(control_plane, host, tmp_path):
    async with gateway(make_plane(control_plane, host, tmp_path)) as client:
        for method, path in (("GET", "/v1/files"), ("GET", "/v1/vector_stores"),
                             ("POST", "/v1/vector_stores")):
            response = await client.request(method, path, headers={"Authorization": "Bearer x"})
            assert response.status_code == 401


async def test_disabled_without_a_store_path(control_plane, host, auth):
    plane = build_plane(control_plane, host)
    async with gateway(plane) as client:
        listed = await client.get("/v1/vector_stores", headers=auth)
        run = await client.post(
            RUNS,
            headers=auth,
            json={
                "model": "launch-model",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "file_search", "vector_store_ids": ["vs_1"]}],
            },
        )
    assert listed.status_code == 400 and listed.json()["error"]["code"] == "file_search_disabled"
    assert run.status_code == 400 and run.json()["error"]["code"] == "file_search_disabled"
    assert host.requests == []


# --------------------------------------------------------------------------- limits


async def test_limits(control_plane, host, auth, tmp_path):
    plane = make_plane(
        control_plane,
        host,
        tmp_path,
        file_search_max_file_bytes=2000,
        file_search_max_files_per_account=3,
        file_search_max_bytes_per_account=2500,
        file_search_max_chunks_per_store=3,
        file_search_chunk_chars=200,
        file_search_chunk_overlap_chars=0,
        file_search_max_stores_per_account=2,
    )
    async with gateway(plane) as client:
        too_big = await upload(client, auth, "big.txt", "x " * 1500)
        assert too_big.status_code == 413 and too_big.json()["error"]["code"] == "file_too_large"
        first = await upload(client, auth, "a.txt", "word " * 300)  # 1500 bytes, ~8 chunks
        assert first.status_code == 200
        over_total = await upload(client, auth, "b.txt", "word " * 250)
        assert over_total.status_code == 409
        assert over_total.json()["error"]["code"] == "storage_limit_reached"
        assert (await upload(client, auth, "c.txt", "small")).status_code == 200
        assert (await upload(client, auth, "d.txt", "small")).status_code == 200
        over_count = await upload(client, auth, "e.txt", "small")
        assert over_count.status_code == 409
        assert over_count.json()["error"]["code"] == "file_limit_reached"

        store = (await client.post("/v1/vector_stores", headers=auth, json={})).json()["id"]
        too_many_chunks = await client.post(
            f"/v1/vector_stores/{store}/files", headers=auth, json={"file_id": first.json()["id"]}
        )
        assert too_many_chunks.status_code == 409
        assert too_many_chunks.json()["error"]["code"] == "chunk_limit_reached"
        listed = (await client.get(f"/v1/vector_stores/{store}/files", headers=auth)).json()
        assert listed["data"][0]["status"] == "failed"
        assert listed["data"][0]["last_error"]["code"] == "invalid_file"

        assert (await client.post("/v1/vector_stores", headers=auth, json={})).status_code == 200
        third = await client.post("/v1/vector_stores", headers=auth, json={})
        assert third.status_code == 409
        assert third.json()["error"]["code"] == "vector_store_limit_reached"


async def test_upload_validation(control_plane, host, auth, tmp_path):
    async with gateway(make_plane(control_plane, host, tmp_path)) as client:
        exe = await upload(client, auth, "tool.exe", b"MZ\x00\x00")
        assert exe.status_code == 400 and exe.json()["error"]["code"] == "unsupported_file"
        binary = await upload(client, auth, "notes.txt", b"abc\x00def")
        assert binary.status_code == 400 and binary.json()["error"]["code"] == "invalid_file"
        purpose = await upload(client, auth, purpose="fine-tune")
        assert purpose.status_code == 400 and purpose.json()["error"]["code"] == "invalid_purpose"
        empty = await upload(client, auth, "e.md", b"")
        assert empty.status_code == 400
        bad_json = await upload(client, auth, "data.json", "{not json")
        assert bad_json.status_code == 400 and bad_json.json()["error"]["code"] == "invalid_file"
        filters = await client.post(
            "/v1/vector_stores/vs_x/search", headers=auth, json={"query": "q", "filters": {}}
        )
        assert filters.status_code == 400


# --------------------------------------------------------------------------- text + chunking


def test_extraction_by_type():
    page = (
        "<html><head><title>t</title><style>p{}</style></head><body><h1>Refunds</h1>"
        "<script>steal()</script><p>Within&nbsp;30 days.</p></body></html>"
    )
    text = extract_text("policy.html", page.encode())
    assert "Refunds" in text and "Within 30 days." in text
    assert "steal" not in text and "p{}" not in text
    assert '"plan": "pro"' in extract_text("a.json", b'{"plan":"pro","seats":3}')
    assert extract_text("a.csv", b"name,days\nalice,25\n") == "name, days\nalice, 25"
    assert extract_text("a.md", "﻿# Title\r\nbody".encode()) == "# Title\nbody"
    with pytest.raises(ExtractionError):
        extract_text("a.docx", b"PK")


def test_pdf_is_extracted_or_refused_clearly():
    if pdf_supported():
        pytest.importorskip("pypdf")
        import io

        from pypdf import PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        buffer = io.BytesIO()
        writer.write(buffer)
        with pytest.raises(ExtractionError) as info:  # a blank page has no text
            extract_text("blank.pdf", buffer.getvalue())
        assert info.value.code == "invalid_file"
    else:
        with pytest.raises(ExtractionError) as info:
            extract_text("doc.pdf", b"%PDF-1.4")
        assert info.value.code == "unsupported_file" and "pdf" in info.value.message.lower()


def test_chunking_overlaps_and_prefers_boundaries():
    paragraphs = [f"Paragraph {i}. " + "word " * 40 for i in range(10)]
    text = "\n\n".join(paragraphs)
    chunks = chunk_text(text, size=300, overlap=60)
    assert len(chunks) > 5
    assert all(len(c.text) <= 300 for c in chunks)
    assert [c.index for c in chunks] == list(range(len(chunks)))
    # Consecutive chunks overlap.
    for left, right in zip(chunks, chunks[1:], strict=False):
        assert right.start < left.start + len(left.text)
    # Every word of the source lands in some chunk.
    covered = " ".join(c.text for c in chunks)
    assert all(f"Paragraph {i}." in covered for i in range(10))
    # No chunk starts mid-word.
    assert all(text[c.start - 1] in " \n" for c in chunks if c.start)


def test_chunking_edge_cases():
    assert chunk_text("", size=100, overlap=10) == []
    assert [c.text for c in chunk_text("short", size=100, overlap=10)] == ["short"]
    unbroken = "x" * 1000
    pieces = chunk_text(unbroken, size=100, overlap=500)  # overlap clamped below size/2
    assert all(len(p.text) <= 100 for p in pieces) and len(pieces) < 25


# --------------------------------------------------------------------------- ranking


def _store(tmp_path, use_fts: bool) -> FileStore:
    limits = Limits(
        max_file_bytes=10**6,
        max_files_per_account=100,
        max_bytes_per_account=10**8,
        max_stores_per_account=10,
        max_chunks_per_store=1000,
        chunk_chars=400,
        chunk_overlap_chars=0,
    )
    return FileStore(str(tmp_path / ("fts" if use_fts else "py")), limits, use_fts=use_fts)


def _index(store: FileStore, account: str, docs: dict[str, str]) -> str:
    store_id = store.create_store(account, name="kb", metadata="{}", embedding_model=None)
    for name, text in docs.items():
        file_id = store.create_file(
            account, filename=name, purpose="assistants", data=text.encode(), text=text
        )["id"]
        chunks, _ = store.plan_chunks(account, store_id, file_id, size=400, overlap=0)
        store.write_chunks(account, store_id, file_id, chunks, None)
    return store_id


DOCS = {
    "cats.md": "Cats are small carnivorous mammals. A cat sleeps most of the day.",
    "dogs.md": "Dogs are loyal. A dog needs daily walks and enjoys fetching sticks.",
    "mixed.md": "Pets: some people keep a cat, others a dog, a few keep fish.",
    "rockets.md": "Rockets reach orbit by burning propellant at very high exhaust speed.",
}


@pytest.mark.parametrize("use_fts", [True, False], ids=["fts5", "python"])
def test_bm25_ranks_the_most_relevant_chunk_first(tmp_path, use_fts):
    store = _store(tmp_path, use_fts)
    store_id = _index(store, "acct", DOCS)
    hits = store.search("acct", [store_id], "how far does a rocket fly to orbit", max_results=3)
    assert hits[0].filename == "rockets.md"
    hits = store.search("acct", [store_id], "dog walks", max_results=4)
    assert [h.filename for h in hits[:2]] == ["dogs.md", "mixed.md"]
    assert all(0 < h.score < 1 for h in hits)
    assert hits == sorted(hits, key=lambda h: -h.score)
    # Query syntax is never interpreted: FTS operators and quotes are plain words.
    assert store.search("acct", [store_id], 'cat" OR NEAR(dog', max_results=2)
    assert store.search("acct", [store_id], "zebra", max_results=2) == []
    assert store.search("acct", [store_id], "!!!", max_results=2) == []
    # Another account searching the same store id sees nothing.
    with pytest.raises(Exception, match="No such vector store"):
        store.search("other", [store_id], "cat", max_results=2)


@pytest.mark.parametrize("use_fts", [True, False], ids=["fts5", "python"])
def test_scores_never_depend_on_another_accounts_documents(tmp_path, use_fts):
    store = _store(tmp_path, use_fts)
    mine = _index(store, "acct", DOCS)
    before = [(h.chunk_id, h.score) for h in store.search("acct", [mine], "cat", max_results=4)]
    assert before and before[0][1] > 0.3
    # Another tenant indexes many documents about cats: corpus statistics must not move.
    _index(store, "noisy", {f"cat{i}.md": "cat cat cat kitten" for i in range(30)})
    after = [(h.chunk_id, h.score) for h in store.search("acct", [mine], "cat", max_results=4)]
    assert after == before


def test_fts5_and_python_bm25_agree(tmp_path):
    fts, python = _store(tmp_path, True), _store(tmp_path, False)
    a, b = _index(fts, "acct", DOCS), _index(python, "acct", DOCS)
    for query in ("cat sleeps", "rockets propellant", "loyal dog", "fish", "a cat or a dog"):
        left = [(h.filename, h.score) for h in fts.search("acct", [a], query, max_results=4)]
        right = [(h.filename, h.score) for h in python.search("acct", [b], query, max_results=4)]
        assert left == right


async def test_hybrid_ranking_uses_metered_embeddings(control_plane, host, auth, tmp_path):
    plane = make_plane(control_plane, host, tmp_path, file_search_embedding_model="embed-model")
    async with gateway(plane) as client:
        store_id = await make_store(
            client, auth, {"handbook.md": HANDBOOK, "security.md": SECURITY},
            chunking_strategy={
                "type": "static",
                "static": {"max_chunk_size_tokens": 100, "chunk_overlap_tokens": 0},
            },
        )
        store = (await client.get(f"/v1/vector_stores/{store_id}", headers=auth)).json()
        assert store["ranking"] == "hybrid"
        files = (await client.get(f"/v1/vector_stores/{store_id}/files", headers=auth)).json()
        assert all(f["embedded"] for f in files["data"])
        # "pto" appears in no document, so BM25 alone finds nothing; the embedding does.
        response = await client.post(
            f"/v1/vector_stores/{store_id}/search", headers=auth, json={"query": "pto"}
        )
        body = response.json()
        assert body["ranking"] == "hybrid"
        assert "vacation" in body["data"][0]["content"][0]["text"].lower()
        # Lexical and semantic agree on an exact phrase.
        response = await client.post(
            f"/v1/vector_stores/{store_id}/search", headers=auth,
            json={"query": "lost laptop security desk"},
        )
        assert response.json()["data"][0]["filename"] == "security.md"

        # Embeddings went through the proxy: upstream model name, no caller credential,
        # and every call metered as usage for the embeddings deployment.
        assert host.embedding_calls
        assert all(c["payload"]["model"] == "internal-embedder" for c in host.embedding_calls)
        assert all("authorization" not in c["headers"] for c in host.embedding_calls)
        records = plane.usage.drain()
        assert len(records) == len(host.embedding_calls)
        assert {str(r.deployment_id) for r in records} == {str(EMBED_DEPLOYMENT.deployment_id)}

        # Embeddings failing at query time degrades to BM25 instead of failing the search.
        host.embeddings_down = True
        degraded = await client.post(
            f"/v1/vector_stores/{store_id}/search", headers=auth, json={"query": "vacation"}
        )
        assert degraded.status_code == 200 and degraded.json()["ranking"] == "bm25_fallback"
        assert degraded.json()["data"]


async def test_an_account_without_the_embeddings_deployment_gets_bm25(
    control_plane, host, auth_b, tmp_path
):
    plane = make_plane(control_plane, host, tmp_path, file_search_embedding_model="embed-model")
    async with gateway(plane) as client:
        store = (await client.post("/v1/vector_stores", headers=auth_b, json={})).json()
        named = await client.post(
            "/v1/vector_stores", headers=auth_b, json={"embedding_model": "embed-model"}
        )
    assert store["ranking"] == "bm25"
    assert named.status_code in (403, 404)  # account B does not own that deployment
    assert host.embedding_calls == []


# --------------------------------------------------------------------------- OpenAI SDK


async def test_openai_sdk_files_and_vector_stores(control_plane, host, signing_key, tmp_path):
    openai = pytest.importorskip("openai")
    plane = make_plane(control_plane, host, tmp_path)
    transport = httpx.ASGITransport(app=__import__(
        "fabric_data_plane.app", fromlist=["create_inference_app"]
    ).create_inference_app(plane))
    client = openai.AsyncOpenAI(
        api_key=signing_key.issue(),
        base_url="http://ingress.test/v1",
        http_client=httpx.AsyncClient(transport=transport, base_url="http://ingress.test"),
        max_retries=0,
    )
    async with client:
        uploaded = await client.files.create(
            file=("handbook.md", HANDBOOK.encode(), "text/markdown"), purpose="assistants"
        )
        assert uploaded.id.startswith("file-") and uploaded.status == "processed"
        security = await client.files.create(
            file=("security.md", SECURITY.encode()), purpose="assistants"
        )
        store = await client.vector_stores.create(name="handbook", file_ids=[uploaded.id])
        assert store.file_counts.completed == 1 and store.status == "completed"
        attached = await client.vector_stores.files.create_and_poll(
            vector_store_id=store.id, file_id=security.id
        )
        assert attached.status == "completed"
        page = await client.vector_stores.search(
            vector_store_id=store.id, query="password length", max_num_results=3
        )
        results = page.data
        assert results[0].filename == "security.md" and results[0].file_id == security.id
        assert "14 characters" in results[0].content[0].text
        assert 0 < results[0].score <= 1

        stores = [s.id async for s in client.vector_stores.list()]
        assert stores == [store.id]
        files = [f.id async for f in client.vector_stores.files.list(vector_store_id=store.id)]
        assert set(files) == {uploaded.id, security.id}
        assert (await client.vector_stores.retrieve(store.id)).file_counts.total == 2
        assert (await client.files.retrieve(uploaded.id)).filename == "handbook.md"
        assert {f.id async for f in client.files.list()} == {uploaded.id, security.id}
        assert (await client.vector_stores.delete(store.id)).deleted is True
        assert (await client.files.delete(uploaded.id)).deleted is True
        with pytest.raises(openai.NotFoundError):
            await client.vector_stores.retrieve(store.id)


# --------------------------------------------------------------------------- the agent tool


def _results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    tool = [m for m in payload["messages"] if m["role"] == "tool"][-1]
    return json.loads(tool["content"])["results"]


async def test_file_search_in_the_agent_loop_returns_citations(
    control_plane, host, auth, tmp_path
):
    plane = make_plane(control_plane, host, tmp_path)
    async with gateway(plane) as client:
        store_id = await make_store(client, auth, {"handbook.md": HANDBOOK, "sec.md": SECURITY})

        def answer(payload: dict[str, Any]) -> dict[str, Any]:
            top = _results(payload)[0]
            return completion(f"You get 25 vacation days a year [{top['citation']}].")

        host.replies = [
            completion(None, tool_calls=[
                tool_call("c1", "file_search", {"query": "vacation days per year"})
            ]),
            answer,
        ]
        response = await client.post(
            RUNS,
            headers=auth,
            json={
                "model": "launch-model",
                "messages": [{"role": "user", "content": "How much vacation do I get?"}],
                "tools": [{"type": "file_search", "vector_store_ids": [store_id],
                           "max_results": 2}],
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "completed"
    assert body["message"]["content"].endswith("[handbook.md#0].")
    # The model saw a plain function tool; the store ids never reached the host.
    first = host.requests[0]["payload"]
    assert first["tools"][0]["function"]["name"] == "file_search"
    assert first["tools"][0]["function"]["parameters"]["required"] == ["query"]
    assert store_id not in json.dumps(first)
    results = _results(host.requests[1]["payload"])
    assert len(results) <= 2
    assert results[0]["filename"] == "handbook.md"
    assert results[0]["chunk_id"] == f"{results[0]['file_id']}#0"
    assert "25 vacation days" in results[0]["text"]
    # The trace records counts and sizes, never document or query text.
    [step] = [s for s in body["steps"] if s["type"] == "tool"]
    assert step["executor"] == "hosted" and step["hosted_tool"] == "file_search"
    assert step["ok"] is True
    assert step["results"] == len(results) and step["vector_stores"] == 1
    assert step["ranking"] == "bm25" and step["query_chars"] == len("vacation days per year")
    serialized = json.dumps(body["steps"])
    assert "vacation" not in serialized and "handbook" not in serialized

    async with gateway(plane) as client:
        metrics = (await client.get("/metrics")).text
    assert 'fabric_dp_hosted_tool_executions_total{tool="file_search",outcome="ok"} 1' in metrics
    assert 'fabric_dp_file_store_operations_total{operation="upload",outcome="ok"} 2' in metrics


async def test_file_search_as_a_function_executor(control_plane, host, auth, tmp_path):
    plane = make_plane(control_plane, host, tmp_path)
    async with gateway(plane) as client:
        store_id = await make_store(client, auth, {"handbook.md": HANDBOOK})
        host.replies = [
            completion(None, tool_calls=[tool_call("c1", "handbook", {"query": "receipt"})]),
            completion("Keep every receipt."),
        ]
        response = await client.post(
            RUNS,
            headers=auth,
            json={
                "model": "launch-model",
                "messages": [{"role": "user", "content": "expenses?"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "handbook", "description": "Company handbook"},
                        "x-fabric-executor": {
                            "type": "builtin", "name": "file_search",
                            "vector_store_ids": [store_id], "max_results": 1,
                        },
                    }
                ],
            },
        )
    assert response.json()["status"] == "completed", response.text
    assert host.requests[0]["payload"]["tools"][0]["function"]["description"] == "Company handbook"
    assert all("x-fabric-executor" not in t for t in host.requests[0]["payload"]["tools"])
    results = _results(host.requests[1]["payload"])
    assert len(results) == 1 and "receipt" in results[0]["text"]


async def test_file_search_on_a_foreign_store_is_refused_before_any_model_call(
    control_plane, host, auth, auth_b, tmp_path
):
    plane = make_plane(control_plane, host, tmp_path)
    async with gateway(plane) as client:
        store_id = await make_store(client, auth, {"handbook.md": HANDBOOK})
        response = await client.post(
            RUNS,
            headers=auth_b,
            json={
                "model": "other-account-model",
                "messages": [{"role": "user", "content": "x"}],
                "tools": [{"type": "file_search", "vector_store_ids": [store_id]}],
            },
        )
        malformed = await client.post(
            RUNS,
            headers=auth,
            json={
                "model": "launch-model",
                "messages": [{"role": "user", "content": "x"}],
                "tools": [{"type": "file_search", "vector_store_ids": []}],
            },
        )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "vector_store_not_found"
    assert malformed.status_code == 400
    assert host.requests == []


async def test_file_search_tool_errors_reach_the_model(control_plane, host, auth, tmp_path):
    plane = make_plane(control_plane, host, tmp_path)
    async with gateway(plane) as client:
        store_id = await make_store(client, auth, {"handbook.md": HANDBOOK})
        host.replies = [
            completion(None, tool_calls=[tool_call("c1", "file_search", {"q": "missing"})]),
            completion("sorry"),
        ]
        body = (
            await client.post(
                RUNS,
                # With the tool guard off the invalid call reaches the executor unrepaired.
                headers={**auth, "X-Fabric-Tool-Guard": "off"},
                json={
                    "model": "launch-model",
                    "messages": [{"role": "user", "content": "x"}],
                    "tools": [{"type": "file_search", "vector_store_ids": [store_id]}],
                },
            )
        ).json()
    [step] = [s for s in body["steps"] if s["type"] == "tool"]
    assert step["ok"] is False and step["error"] == "invalid_arguments"


async def test_store_survives_a_gateway_restart(control_plane, host, auth, tmp_path):
    async with gateway(make_plane(control_plane, host, tmp_path)) as client:
        store_id = await make_store(client, auth, {"handbook.md": HANDBOOK})
    async with gateway(make_plane(control_plane, host, tmp_path)) as client:
        response = await client.post(
            f"/v1/vector_stores/{store_id}/search", headers=auth, json={"query": "receipt"}
        )
    assert response.status_code == 200 and response.json()["data"]
