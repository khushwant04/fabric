"""File search: OpenAI-shaped ``/v1/files`` and ``/v1/vector_stores`` plus the hosted tool.

Every route authenticates with the inference token and scopes by its ``account_id``. A file
or store of another account answers exactly like a missing one (404, same code and message),
so ids cannot be probed across accounts.

Field names follow OpenAI's Files and Vector Stores APIs closely enough that the official SDK
works unchanged (``client.files.create``, ``client.vector_stores.create``,
``client.vector_stores.files.create_and_poll``, ``client.vector_stores.search``). Indexing is
synchronous, so a file attach returns ``status: "completed"`` (or ``failed``) immediately.

The ``file_search`` agent tool (docs/file-search.md) runs the same search inside
``POST /v1/agent/runs`` and returns the top chunks with citations. Its trace records counts and
sizes, never document or query text.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile

from fabric_data_plane.agent_tools import ToolError
from fabric_data_plane.errors import ApiError, BadRequest, NotFound, PayloadTooLarge
from fabric_data_plane.hosted_tools import HostedResult
from fabric_data_plane.hosted_tools.text import CHARS_PER_TOKEN, ExtractionError, extract_text
from fabric_data_plane.hosted_tools.vector_store import StoreError

FILES_PATH = "/v1/files"
VECTOR_STORES_PATH = "/v1/vector_stores"
EMBEDDINGS_PATH = "/v1/embeddings"
FILE_PURPOSES = frozenset({"assistants"})
DEFAULT_MAX_RESULTS = 5
_MAX_QUERY_CHARS = 4096
_MAX_NAME = 256
_MULTIPART_OVERHEAD = 64 * 1024

TOOL_DESCRIPTION = (
    "Search the user's uploaded documents. Returns the most relevant passages with a "
    "citation (file name and chunk id) for each; cite them in the answer."
)
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {"query": {"type": "string", "description": "What to search for"}},
    "required": ["query"],
}


def _api_error(exc: StoreError) -> ApiError:
    return ApiError(exc.status, exc.code, exc.message)


def internal_request(authorization: str, path: str = EMBEDDINGS_PATH) -> Request:
    """A request carrying only the caller's credential, for gateway-originated model calls.

    Embedding calls reuse the proxy path (authorization, admission, metering), but must not
    inherit an upload's multipart headers or an agent run's step headers.
    """
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"authorization", authorization.encode("latin-1")),
            (b"content-type", b"application/json"),
        ],
        "client": None,
        "server": None,
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


class FileSearchService:
    """Store access, embeddings and limits; the routes and the tool share one instance."""

    def __init__(self, services: Any) -> None:
        self.services = services
        self.settings = services.settings

    @property
    def store(self):
        return self.services.file_store()

    def require_enabled(self) -> None:
        if self.store is None:
            raise BadRequest(
                "file_search_disabled",
                "File search is disabled on this gateway (FABRIC_DP_FILE_SEARCH_STORE_PATH)",
            )

    async def _run(self, operation: str, function, *args, **kwargs):
        """Run a blocking store call in a worker thread, counting the outcome."""
        try:
            result = await asyncio.to_thread(function, *args, **kwargs)
        except StoreError as exc:
            self.services.metrics.store_operation(operation=operation, outcome=exc.code)
            raise _api_error(exc) from exc
        self.services.metrics.store_operation(operation=operation, outcome="ok")
        return result

    # ------------------------------------------------------------------ embeddings

    def embedding_model_for(self, plane: Any, account_id: Any, requested: Any) -> str | None:
        """The embeddings alias a new store uses, or None for BM25 only."""
        if requested is not None:
            if not isinstance(requested, str) or not requested.strip():
                raise BadRequest("invalid_request", "embedding_model must be a model alias")
            plane.registry.resolve(requested.strip(), account_id=account_id)  # 404 if not owned
            return requested.strip()
        alias = self.settings.file_search_embedding_model.strip()
        if not alias:
            return None
        try:
            plane.registry.resolve(alias, account_id=account_id)
        except ApiError:
            return None
        return alias

    async def embed(
        self, plane: Any, authorization: str, model: str, texts: list[str]
    ) -> list[list[float]]:
        """Embed ``texts`` through the proxy path (authorized and metered per call)."""
        proxy = self.services.proxy
        if proxy is None:
            raise ToolError("embeddings_unavailable", "no proxy is registered")
        vectors: list[list[float]] = []
        batch = self.settings.file_search_embedding_batch_size
        for start in range(0, len(texts), batch):
            part = texts[start : start + batch]
            response = await proxy(
                plane,
                internal_request(authorization),
                EMBEDDINGS_PATH,
                principal=plane.authenticate(authorization),
                payload={"model": model, "input": part},
            )
            try:
                body = json.loads(bytes(response.body))
            except (ValueError, UnicodeDecodeError, AttributeError) as exc:
                raise ToolError("embeddings_failed", "embeddings reply is not JSON") from exc
            data = body.get("data") if isinstance(body, dict) else None
            if response.status_code >= 400 or not isinstance(data, list) or len(data) != len(
                part
            ):
                raise ToolError("embeddings_failed", "embeddings call failed")
            ordered = sorted(data, key=lambda item: item.get("index", 0))
            for item in ordered:
                vector = item.get("embedding")
                if not isinstance(vector, list) or not vector:
                    raise ToolError("embeddings_failed", "embeddings reply has no vector")
                vectors.append([float(v) for v in vector])
        return vectors

    # ------------------------------------------------------------------ operations

    async def attach(
        self,
        plane: Any,
        authorization: str,
        account: str,
        store_id: str,
        file_id: str,
        chunking: Any = None,
    ) -> dict[str, Any]:
        size, overlap = self._chunking(chunking)
        try:
            chunks, model = await self._run(
                "attach", self.store.plan_chunks, account, store_id, file_id, size=size,
                overlap=overlap,
            )
        except ApiError as exc:
            if exc.code != "file_already_attached":
                raise
            # Idempotent, so a re-run demo or a retried client gets the existing object.
            return await self._run(
                "get", self.store.store_file_object, account, store_id, file_id
            )
        vectors = None
        if model and chunks:
            try:
                vectors = await self.embed(plane, authorization, model, [c.text for c in chunks])
            except (ToolError, ApiError):
                # Degrade to BM25 for this file rather than refuse the attach.
                self.services.metrics.store_operation(operation="embed", outcome="failed")
                vectors = None
        return await self._run(
            "attach", self.store.write_chunks, account, store_id, file_id, chunks, vectors
        )

    def _chunking(self, chunking: Any) -> tuple[int, int]:
        size = self.settings.file_search_chunk_chars
        overlap = self.settings.file_search_chunk_overlap_chars
        if chunking is None or (isinstance(chunking, dict) and chunking.get("type") == "auto"):
            return size, overlap
        if not isinstance(chunking, dict) or chunking.get("type") != "static":
            raise BadRequest("invalid_request", "chunking_strategy must be auto or static")
        static = chunking.get("static") or {}
        tokens = static.get("max_chunk_size_tokens")
        overlap_tokens = static.get("chunk_overlap_tokens", 0)
        if (
            not isinstance(tokens, int)
            or not isinstance(overlap_tokens, int)
            or not 100 <= tokens <= 4096
            or not 0 <= overlap_tokens <= tokens // 2
        ):
            raise BadRequest(
                "invalid_request",
                "static chunking needs max_chunk_size_tokens 100-4096 and an overlap of at "
                "most half of it",
            )
        return tokens * CHARS_PER_TOKEN, overlap_tokens * CHARS_PER_TOKEN

    async def search(
        self,
        plane: Any,
        authorization: str,
        account: str,
        store_ids: list[str],
        query: str,
        *,
        max_results: int,
        score_threshold: float = 0.0,
    ) -> tuple[list[Any], str]:
        """Ranked hits and how they were ranked (bm25, hybrid, or bm25_fallback)."""
        rows = await self._run("search", self.store.require_stores, account, store_ids)
        models = {row["embedding_model"] for row in rows if row["embedding_model"]}
        ranking = "bm25"
        query_vector = None
        if len(models) == 1:
            try:
                query_vector = (await self.embed(plane, authorization, models.pop(), [query]))[0]
                ranking = "hybrid"
            except (ToolError, ApiError):
                ranking = "bm25_fallback"
        hits = await self._run(
            "search",
            self.store.search,
            account,
            store_ids,
            query,
            max_results=max_results,
            query_vector=query_vector,
            hybrid_weight=self.settings.file_search_hybrid_weight,
            score_threshold=score_threshold,
        )
        await asyncio.to_thread(self.store.touch, store_ids)
        return hits, ranking


# --------------------------------------------------------------------------- routes


def _list_page(data: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "object": "list",
        "data": data,
        "first_id": data[0]["id"] if data else None,
        "last_id": data[-1]["id"] if data else None,
        "has_more": False,
    }


def _limit(request: Request, default: int = 100) -> int:
    try:
        return max(1, min(int(request.query_params.get("limit", default)), 1000))
    except ValueError as exc:
        raise BadRequest("invalid_limit", "limit must be an integer") from exc


async def _json_body(request: Request, read_json: Any) -> dict[str, Any]:
    if not await request.body():
        return {}
    return await read_json(request)


def build_router(plane: Any, services: Any, *, read_json: Any) -> APIRouter:
    router = APIRouter(tags=["file search"])
    service = services.file_search

    def principal_for(request: Request) -> tuple[Any, str, str]:
        authorization = request.headers.get("authorization")
        principal = plane.authenticate(authorization)
        service.require_enabled()
        return principal, str(principal.account_id), authorization or ""

    @router.post(FILES_PATH, summary="Upload a file for file search")
    async def create_file(request: Request) -> JSONResponse:
        _principal, account, _auth = principal_for(request)
        limit = service.settings.file_search_max_file_bytes
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit + _MULTIPART_OVERHEAD:
            raise PayloadTooLarge("file_too_large", f"File exceeds the {limit}-byte limit")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > limit + _MULTIPART_OVERHEAD:
                raise PayloadTooLarge("file_too_large", f"File exceeds the {limit}-byte limit")
        request._body = bytes(body)  # replayed to the multipart parser
        try:
            form = await request.form()
        except Exception as exc:  # noqa: BLE001 - assorted parser errors
            raise BadRequest(
                "invalid_multipart", "Request body is not valid multipart/form-data"
            ) from exc
        try:
            purpose = form.get("purpose")
            if purpose not in FILE_PURPOSES:
                raise BadRequest("invalid_purpose", "purpose must be 'assistants'")
            upload = form.get("file")
            if not isinstance(upload, UploadFile):
                raise BadRequest("file_required", "A file must be supplied as 'file'")
            data = await upload.read(limit + 1)
            filename = (upload.filename or "upload.txt").replace("\\", "/").rsplit("/", 1)[-1]
        finally:
            await form.close()
        filename = filename[:_MAX_NAME] or "upload.txt"
        if not data:
            raise BadRequest("file_empty", "The supplied file is empty")
        if len(data) > limit:
            raise PayloadTooLarge("file_too_large", f"File exceeds the {limit}-byte limit")
        try:
            text = await asyncio.to_thread(extract_text, filename, data)
        except ExtractionError as exc:
            services.metrics.store_operation(operation="upload", outcome=exc.code)
            raise BadRequest(exc.code, exc.message) from exc
        created = await service._run(
            "upload", service.store.create_file, account, filename=filename,
            purpose=str(purpose), data=data, text=text,
        )
        return JSONResponse(created)

    @router.get(FILES_PATH, summary="List this account's files")
    async def list_files(request: Request) -> JSONResponse:
        _p, account, _a = principal_for(request)
        rows = await service._run("list", service.store.list_files, account, limit=_limit(request))
        return JSONResponse(_list_page(rows))

    @router.get(FILES_PATH + "/{file_id}", summary="Retrieve a file")
    async def get_file(file_id: str, request: Request) -> JSONResponse:
        _p, account, _a = principal_for(request)
        return JSONResponse(await service._run("get", service.store.get_file, account, file_id))

    @router.delete(FILES_PATH + "/{file_id}", summary="Delete a file and its chunks")
    async def delete_file(file_id: str, request: Request) -> JSONResponse:
        _p, account, _a = principal_for(request)
        return JSONResponse(
            await service._run("delete", service.store.delete_file, account, file_id)
        )

    @router.post(VECTOR_STORES_PATH, summary="Create a vector store")
    async def create_store(request: Request) -> JSONResponse:
        principal, account, authorization = principal_for(request)
        payload = await _json_body(request, read_json)
        name = payload.get("name") or "vector store"
        if not isinstance(name, str) or len(name) > _MAX_NAME:
            raise BadRequest("invalid_request", f"name must be a string of at most {_MAX_NAME}")
        metadata = payload.get("metadata") or {}
        if not isinstance(metadata, dict) or len(metadata) > 16 or not all(
            isinstance(k, str) and isinstance(v, str) and len(k) <= 64 and len(v) <= 512
            for k, v in metadata.items()
        ):
            raise BadRequest("invalid_request", "metadata must be up to 16 short string pairs")
        file_ids = payload.get("file_ids") or []
        if not isinstance(file_ids, list) or not all(isinstance(f, str) for f in file_ids):
            raise BadRequest("invalid_request", "file_ids must be a list of file ids")
        model = service.embedding_model_for(
            plane, principal.account_id, payload.get("embedding_model")
        )
        for file_id in file_ids:  # 404 before anything is created
            await service._run("get", service.store.get_file, account, file_id)
        store_id = await service._run(
            "create_store", service.store.create_store, account, name=name,
            metadata=json.dumps(metadata), embedding_model=model,
        )
        for file_id in dict.fromkeys(file_ids):
            await service.attach(
                plane, authorization, account, store_id, file_id,
                payload.get("chunking_strategy"),
            )
        return JSONResponse(
            await service._run("get_store", service.store.store_object, account, store_id)
        )

    @router.get(VECTOR_STORES_PATH, summary="List this account's vector stores")
    async def list_stores(request: Request) -> JSONResponse:
        _p, account, _a = principal_for(request)
        rows = await service._run(
            "list", service.store.list_stores, account, limit=_limit(request)
        )
        return JSONResponse(_list_page(rows))

    @router.get(VECTOR_STORES_PATH + "/{store_id}", summary="Retrieve a vector store")
    async def get_store(store_id: str, request: Request) -> JSONResponse:
        _p, account, _a = principal_for(request)
        return JSONResponse(
            await service._run("get_store", service.store.store_object, account, store_id)
        )

    @router.delete(VECTOR_STORES_PATH + "/{store_id}", summary="Delete a vector store")
    async def delete_store(store_id: str, request: Request) -> JSONResponse:
        _p, account, _a = principal_for(request)
        return JSONResponse(
            await service._run("delete_store", service.store.delete_store, account, store_id)
        )

    @router.post(VECTOR_STORES_PATH + "/{store_id}/files", summary="Attach and index a file")
    async def attach_file(store_id: str, request: Request) -> JSONResponse:
        _p, account, authorization = principal_for(request)
        payload = await _json_body(request, read_json)
        file_id = payload.get("file_id")
        if not isinstance(file_id, str) or not file_id:
            raise BadRequest("invalid_request", "file_id is required")
        result = await service.attach(
            plane, authorization, account, store_id, file_id, payload.get("chunking_strategy")
        )
        return JSONResponse(result)

    @router.get(VECTOR_STORES_PATH + "/{store_id}/files", summary="List a store's files")
    async def list_store_files(store_id: str, request: Request) -> JSONResponse:
        _p, account, _a = principal_for(request)
        rows = await service._run("list", service.store.list_store_files, account, store_id)
        return JSONResponse(_list_page(rows))

    @router.get(
        VECTOR_STORES_PATH + "/{store_id}/files/{file_id}", summary="Retrieve a store file"
    )
    async def get_store_file(store_id: str, file_id: str, request: Request) -> JSONResponse:
        _p, account, _a = principal_for(request)
        return JSONResponse(
            await service._run(
                "get", service.store.store_file_object, account, store_id, file_id
            )
        )

    @router.delete(
        VECTOR_STORES_PATH + "/{store_id}/files/{file_id}", summary="Remove a file from a store"
    )
    async def detach_file(store_id: str, file_id: str, request: Request) -> JSONResponse:
        _p, account, _a = principal_for(request)
        return JSONResponse(
            await service._run("detach", service.store.detach_file, account, store_id, file_id)
        )

    @router.post(VECTOR_STORES_PATH + "/{store_id}/search", summary="Search a vector store")
    async def search_store(store_id: str, request: Request) -> JSONResponse:
        _p, account, authorization = principal_for(request)
        payload = await _json_body(request, read_json)
        query = payload.get("query")
        if isinstance(query, list) and all(isinstance(q, str) for q in query):
            query = " ".join(query)
        if not isinstance(query, str) or not query.strip() or len(query) > _MAX_QUERY_CHARS:
            raise BadRequest("invalid_request", "query must be a non-empty string")
        if payload.get("filters") is not None:
            raise BadRequest(
                "filters_unsupported", "Attribute filters are not supported by this gateway"
            )
        ceiling = service.settings.file_search_max_results
        wanted = payload.get("max_num_results", 10)
        if not isinstance(wanted, int) or isinstance(wanted, bool) or wanted < 1:
            raise BadRequest("invalid_request", "max_num_results must be a positive integer")
        options = payload.get("ranking_options") or {}
        threshold = options.get("score_threshold", 0.0) if isinstance(options, dict) else 0.0
        if not isinstance(threshold, int | float) or not 0 <= threshold <= 1:
            raise BadRequest("invalid_request", "score_threshold must be between 0 and 1")
        hits, ranking = await service.search(
            plane, authorization, account, [store_id], query.strip(),
            max_results=min(wanted, ceiling), score_threshold=float(threshold),
        )
        return JSONResponse(
            {
                "object": "vector_store.search_results.page",
                "search_query": query,
                "data": [
                    {
                        "file_id": hit.file_id,
                        "filename": hit.filename,
                        "score": hit.score,
                        "attributes": {},
                        "content": [{"type": "text", "text": hit.text}],
                        "chunk_id": hit.chunk_id,
                    }
                    for hit in hits
                ],
                "has_more": False,
                "next_page": None,
                "ranking": ranking,
            }
        )

    return router


# --------------------------------------------------------------------------- the tool


def parse_declaration(
    name: str, executor: dict[str, Any], settings: Any
) -> tuple[list[str], int]:
    """Validate a file_search declaration's store ids and result count (static checks)."""
    store_ids = executor.get("vector_store_ids")
    if (
        not isinstance(store_ids, list)
        or not store_ids
        or len(store_ids) > 10
        or not all(isinstance(s, str) and s for s in store_ids)
    ):
        raise BadRequest(
            "invalid_tool", f"tool {name!r}: vector_store_ids must list 1-10 vector store ids"
        )
    wanted = executor.get("max_results", executor.get("max_num_results", DEFAULT_MAX_RESULTS))
    if not isinstance(wanted, int) or isinstance(wanted, bool) or wanted < 1:
        raise BadRequest("invalid_tool", f"tool {name!r}: max_results must be a positive integer")
    return list(dict.fromkeys(store_ids)), min(wanted, settings.file_search_max_results)


class FileSearchTool:
    """The hosted executor bound to one run: account, credential and stores are fixed."""

    requires_approval = False

    def __init__(
        self,
        service: FileSearchService,
        plane: Any,
        *,
        account: str,
        authorization: str,
        store_ids: list[str],
        max_results: int,
    ) -> None:
        self.service = service
        self.plane = plane
        self.account = account
        self.authorization = authorization
        self.store_ids = store_ids
        self.max_results = max_results

    async def execute(self, arguments: dict[str, Any]) -> HostedResult:
        query = arguments.get("query")
        if isinstance(query, list) and all(isinstance(q, str) for q in query):
            query = " ".join(query)
        if not isinstance(query, str) or not query.strip():
            raise ToolError("invalid_arguments", "query must be a non-empty string")
        query = query.strip()[:_MAX_QUERY_CHARS]
        try:
            hits, ranking = await self.service.search(
                self.plane, self.authorization, self.account, self.store_ids, query,
                max_results=self.max_results,
            )
        except NotFound as exc:
            raise ToolError("vector_store_not_found", exc.message) from exc
        except ApiError as exc:
            raise ToolError(exc.code, exc.message) from exc
        results = [
            {
                "citation": f"{hit.filename}#{hit.chunk_index}",
                "file_id": hit.file_id,
                "filename": hit.filename,
                "chunk_id": hit.chunk_id,
                "score": hit.score,
                "text": hit.text,
            }
            for hit in hits
        ]
        content = json.dumps({"query": query, "results": results}, ensure_ascii=False)
        # Trace fields: counts and sizes only, never the query or document text.
        details = {
            "results": len(results),
            "vector_stores": len(self.store_ids),
            "query_chars": len(query),
            "ranking": ranking,
        }
        return HostedResult(content=content, details=details)
