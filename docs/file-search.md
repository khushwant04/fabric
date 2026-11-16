# File search

Upload documents to a stamp, index them into a vector store, and let an agent search them with
the hosted `file_search` tool in [`POST /v1/agent/runs`](agent-tool-loop.md). Results come back
with citations (file name and chunk id) that the model can quote.

The API follows OpenAI's Files and Vector Stores closely enough that the official `openai`
Python SDK works unchanged against the gateway (`client.files.create`,
`client.vector_stores.create`, `client.vector_stores.files.create_and_poll`,
`client.vector_stores.search`, list, retrieve and delete). This is tested
(`data-plane/tests/test_file_search.py::test_openai_sdk_files_and_vector_stores`).

It is opt-in: nothing works until an operator sets `FABRIC_DP_FILE_SEARCH_STORE_PATH` (the
Helm chart sets it by default; see [Deployment](#deployment)).

Code: `data-plane/fabric_data_plane/hosted_tools/` (`services.py` per-request binding,
`file_search.py` routes and tool,
`vector_store.py` storage and ranking, `text.py` extraction and chunking). Edge routing:
`data-plane/fabric_data_plane/edge/files.py`.

## API

Every route takes the same inference token as a chat completion (`Authorization: Bearer ...`)
and acts only on the token's account.

| Route | What it does |
|---|---|
| `POST /v1/files` | Multipart upload: `file` and `purpose=assistants`. Text is extracted at upload; an unsupported or unreadable file is refused here (`400 unsupported_file` / `invalid_file`) |
| `GET /v1/files`, `GET /v1/files/{id}` | List or retrieve file objects |
| `DELETE /v1/files/{id}` | Delete the file, its blob, and its chunks in every store |
| `POST /v1/vector_stores` | Create a store: `name`, optional `file_ids` (attached and indexed now), `metadata`, `chunking_strategy`, and the Fabric extension `embedding_model` |
| `GET /v1/vector_stores`, `GET /v1/vector_stores/{id}` | List or retrieve stores (`file_counts`, `usage_bytes`, and the extension `ranking`: `bm25` or `hybrid`) |
| `DELETE /v1/vector_stores/{id}` | Delete the store and its chunks (files remain) |
| `POST /v1/vector_stores/{id}/files` | Attach a file: `file_id`, optional `chunking_strategy`. Chunks and indexes synchronously, so the answer is already `completed` (or `failed` with `last_error`). Attaching the same file again returns the existing object |
| `GET /v1/vector_stores/{id}/files[/{file_id}]` | List or retrieve attached files (extensions: `chunk_count`, `embedded`) |
| `DELETE /v1/vector_stores/{id}/files/{file_id}` | Detach and drop the file's chunks from this store |
| `POST /v1/vector_stores/{id}/search` | `query` (string or list), `max_num_results` (default 10, capped by `FABRIC_DP_FILE_SEARCH_MAX_RESULTS`), `ranking_options.score_threshold`. Returns `data: [{file_id, filename, score, content: [{type: "text", text}], chunk_id}]` and the extension `ranking` |

Not supported, and refused rather than ignored where a caller could be misled: attribute
`filters` on search (`400 filters_unsupported`), purposes other than `assistants`. Not
implemented: file content download, file batches, store updates, expiry policies (accepted
fields such as `expires_after` are ignored, and stores never expire).

### Isolation

Every query in the store is scoped by `account_id`. A file or store id belonging to another
account gets exactly the same `404` (same code, same message) as an id that never existed, on
every route, including search, attach and delete; listings never include another account's
objects. Ranking statistics are computed only over the stores being searched, so another
account's documents cannot shift, and thereby reveal anything through, your scores. Tests:
`test_another_account_cannot_see_or_search_a_store`,
`test_scores_never_depend_on_another_accounts_documents`.

### Text extraction

`.txt`, `.md`/`.markdown`, `.rst`, `.log` (UTF-8), `.json` (re-serialised so keys and values
are searchable), `.csv` (rows as comma-separated lines), `.html`/`.htm` (visible text; `script`,
`style` and `head` dropped). Binary content (a NUL byte) or invalid UTF-8 is `invalid_file`.

**PDF** is supported only when the optional, pinned, pure-Python `pypdf` dependency is
installed (`pip install 'fabric-data-plane[pdf]'`). It is *not* in the default image, because it
parses untrusted documents inside the gateway process; without it a PDF upload is refused with
`400 unsupported_file` and a message saying so. Scanned PDFs without a text layer have no
extractable text (`invalid_file`).

### Chunking

Character-based, because the gateway has no tokenizer for whatever model a deployment serves:
`FABRIC_DP_FILE_SEARCH_CHUNK_CHARS` (default 2000) with `FABRIC_DP_FILE_SEARCH_CHUNK_OVERLAP_CHARS`
(default 300). A cut prefers a paragraph, then line, sentence and word boundary in the second
half of the budget; the next chunk starts at a line or sentence start inside the overlap window.
OpenAI's `chunking_strategy: {"type": "static", "static": {"max_chunk_size_tokens": N,
"chunk_overlap_tokens": M}}` is accepted and converted at four characters per token
(N in 100-4096, M at most N/2).

## Ranking

* **BM25, always.** Okapi BM25 (k1 1.2, b 0.75) over lower-cased, lightly stemmed word terms
  (plurals, `-ing`, `-ed`). When the interpreter's SQLite has FTS5 (the usual case) it finds
  the matching chunks; otherwise every chunk of the searched stores is scanned. Both paths score
  with the same function and return identical results (tested). Query text is never
  interpreted as FTS syntax. Pure BM25 scores are reported as `s / (s + 1)`, in (0, 1).
* **Hybrid, optionally.** When `FABRIC_DP_FILE_SEARCH_EMBEDDING_MODEL` names a deployment alias
  and the store's account owns a deployment with that alias (or the store was created with
  `"embedding_model": "<alias>"`), each chunk is embedded at attach time and the query at search
  time, by calling `/v1/embeddings` **through the gateway's ordinary proxy path**: authorized
  against the caller's account, admitted against its limits, retried across backends and
  metered as usage on the embeddings deployment. The caller's credential is not forwarded to
  the host. Vectors are stored in SQLite (float32) and ranked by cosine in Python. The blend is
  `(1 - w) * bm25 / max_bm25 + w * max(0, cosine)`, `w` = `FABRIC_DP_FILE_SEARCH_HYBRID_WEIGHT`
  (default 0.5).
* **Degradation.** If embedding fails at attach time the file is indexed for BM25 only
  (`embedded: false`); if it fails at search time the search answers with BM25 and
  `ranking: "bm25_fallback"` rather than failing.

## The `file_search` agent tool

Declare it in `tools` of `POST /v1/agent/runs` in any of these equivalent forms:

```json
{"type": "file_search", "vector_store_ids": ["vs_..."], "max_results": 5}
{"type": "builtin", "name": "file_search", "vector_store_ids": ["vs_..."], "max_results": 5}
{"type": "function",
 "function": {"name": "handbook", "description": "Search the employee handbook"},
 "x-fabric-executor": {"type": "builtin", "name": "file_search",
                       "vector_store_ids": ["vs_..."], "max_results": 3}}
```

The first two expose a function named `file_search` with a single `query` parameter; the third
keeps your function name and description. `vector_store_ids` (1-10) must all belong to the
caller's account, checked before the first model call (`404 vector_store_not_found` otherwise);
`max_results` defaults to 5 and is capped by `FABRIC_DP_FILE_SEARCH_MAX_RESULTS`. The store ids
are never sent to the model host.

The model calls it with `{"query": "..."}` and receives a `role: "tool"` message:

```json
{"query": "vacation days per year",
 "results": [{"citation": "handbook.md#0", "file_id": "file-...", "filename": "handbook.md",
              "chunk_id": "file-...#0", "score": 0.83, "text": "Full-time staff accrue 25 ..."}]}
```

Tell the model how to cite (for example "cite every policy claim as [file#chunk] using the
citation field"); the gateway adds no instructions of its own.

**Trace.** The tool step in the run summary and SSE `tool.completed` event
(`executor: "hosted"`, `hosted_tool: "file_search"`) carries counts and sizes only: `results`, `vector_stores`, `query_chars`, `ranking`, `duration_ms`,
`result_bytes`, `ok`/`error`. Never the query text, file names or document text (tested).

## Limits

All are settings (`FABRIC_DP_` prefix) and Helm values under `dataPlane.fileSearch`:

| Setting | Default | Refusal |
|---|---|---|
| `FILE_SEARCH_MAX_FILE_BYTES` | 10 MiB | `413 file_too_large` (checked on the multipart body before parsing) |
| `FILE_SEARCH_MAX_FILES_PER_ACCOUNT` | 100 | `409 file_limit_reached` |
| `FILE_SEARCH_MAX_BYTES_PER_ACCOUNT` | 200 MiB | `409 storage_limit_reached` |
| `FILE_SEARCH_MAX_STORES_PER_ACCOUNT` | 20 | `409 vector_store_limit_reached` |
| `FILE_SEARCH_MAX_CHUNKS_PER_STORE` | 10 000 | `409 chunk_limit_reached` (the store file is recorded as `failed`) |
| `FILE_SEARCH_MAX_RESULTS` | 20 | caps `max_num_results` / `max_results` |

### Scale

This is built for demo and departmental scale, and says so:

* Hybrid ranking scans every chunk vector of the searched stores in Python per query. At the
  default 10 000 chunks per store and 1 024-dimensional vectors that is about 10 million
  multiply-adds in pure Python, on the order of a second per query on one core; a few hundred
  chunks (a demo) is a few milliseconds. It is not an ANN index; for large corpora use a vector
  database.
* One SQLite database per stamp gateway, one writer at a time, calls run in a worker thread.
  Indexing is synchronous: a large file blocks its own request (not others) until indexed.
* BM25 candidate retrieval through FTS5 rescoring is capped at the 2 000 best FTS5 matches.

## Deployment

* **Storage.** `FABRIC_DP_FILE_SEARCH_STORE_PATH` holds `file-search.sqlite3` (WAL mode) and
  `blobs/<account>/<file-id>`. In the stamp chart, `dataPlane.fileSearch.enabled` (default
  true) mounts a volume at `/var/lib/fabric-files`. `dataPlane.fileSearch.persistence.enabled`
  is **false by default**: the store is an `emptyDir` and is lost when the pod is replaced, and
  `NOTES.txt` warns. Enable it (a retained PVC, `helm.sh/resource-policy: keep`, or
  `existingClaim`) before customers rely on file search. The stamp gateway runs as a single
  replica, which is what a `ReadWriteOnce` claim needs.
* **Stores live on one stamp.** The edge gateway sends every `/v1/files*` and
  `/v1/vector_stores*` call of an account to one deterministic stamp: the highest rendezvous
  weight for `<account>:files` among the stamps holding any of the account's deployments.
  Affinity is strict (a down file stamp is a `503`, never a silent failover to a stamp that
  would answer `404` for every id).
* **Agent runs that use `file_search` must run on that same stamp.** The edge recognises every
  declaration form and narrows the run's candidate stamps to the file stamp. If the file stamp
  does not serve the requested model the run is refused with
  `400 file_search_stamp_mismatch`; choose a model deployed there. Calling a stamp directly
  (no edge) works as long as you always use the same stamp.
* **The file stamp can move.** It changes only when the set of stamps holding the account's
  deployments changes. Stores then remain on the old stamp, unreachable through the edge, until
  the set changes back. There is no replication or migration.
* **Disabled** (`FABRIC_DP_FILE_SEARCH_STORE_PATH` unset): every route and the tool answer
  `400 file_search_disabled`.

## Metrics

`fabric_dp_hosted_tool_executions_total{tool="file_search",outcome}`,
`fabric_dp_hosted_tool_duration_seconds_total`, `fabric_dp_hosted_tool_bytes_total{kind="result"}`
(shared with the other [hosted tools](hosted-tools.md)) and
`fabric_dp_file_store_operations_total{operation,outcome}` on the stamp's `/metrics`. Labels
are fixed sets chosen by the gateway; no file names or ids.

## On-stage walkthrough (no GPU)

```bash
make demo-file-search-mcp
```

This starts the local platform (control plane, Go agent, data plane) with a deterministic stub
model host, plus the demo MCP server ([MCP connectors](mcp-connectors.md)), then runs
`examples/python/scripts/file_search_mcp_demo.py`:

1. **Upload with the OpenAI SDK.** Two Markdown files from `examples/python/data/support-kb/`
   go up with `client.files.create`, and `client.vector_stores.create(file_ids=...,
   chunking_strategy=...)` indexes them. Point out: the stock SDK, no Fabric client code.
2. **Search directly.** `client.vector_stores.search("refund for an item damaged in delivery")`
   ranks the refund-policy passage first.
3. **Agent run.** One `POST /v1/agent/runs` with `file_search` plus two MCP views of an order
   desk. The stream shows `file_search ok 3 passages, bm25`, then the MCP lookup, then a pause
   for approval of the refund ticket, the resume, and a final answer quoting the policy sentence
   with `[refund-policy.md#0]`.
4. **Metrics and the MCP server log**, showing the gateway counted every call and that the MCP
   server never received a Fabric credential.

With a real model: `FABRIC_E2E_UPSTREAM=http://host:8000 FABRIC_E2E_RELEASE=<served name> make
demo-file-search-mcp`. A real model decides its own tool calls, so the answer will differ; the
system prompt in the script asks it to cite.

Against a deployed stamp, run the script directly with `FABRIC_CONTROL_URL`,
`FABRIC_INFERENCE_URL`, `FABRIC_API_KEY`, `FABRIC_MODEL`, and for the MCP part
`FABRIC_DEMO_MCP_URL`/`FABRIC_DEMO_MCP_KEY` (an HTTPS MCP server whose host is in
`dataPlane.mcp.allowedHosts`).
