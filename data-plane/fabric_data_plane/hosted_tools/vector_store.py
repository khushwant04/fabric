"""Account-owned files and vector stores, stamp-local (docs/file-search.md).

Storage is one SQLite database plus one blob file per upload under
``FABRIC_DP_FILE_SEARCH_STORE_PATH``. Every query is scoped by ``account_id``: a store or file
of another account is indistinguishable from one that does not exist.

Retrieval:

* **BM25** always. SQLite FTS5 when the interpreter's SQLite has it (the common case), else
  the same Okapi BM25 formula in pure Python over the chunks of the searched stores.
* **Hybrid**, when a store was created with an embeddings model: the query is embedded, every
  chunk vector of the searched stores is cosine-ranked in Python, and the two normalised
  scores are blended (``FABRIC_DP_FILE_SEARCH_HYBRID_WEIGHT``). A brute-force scan: fine for
  thousands of chunks, not for millions (see the scale notes in the doc).

The store holds document text; nothing here logs it. Callers record counts and sizes only.
"""

from __future__ import annotations

import array
import json
import math
import os
import pathlib
import sqlite3
import threading
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from fabric_data_plane.hosted_tools.text import Chunk, chunk_text, terms_of

Embedder = Callable[[list[str]], Awaitable[list[list[float]]]]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    filename TEXT NOT NULL,
    purpose TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS files_account ON files(account_id);
CREATE TABLE IF NOT EXISTS stores (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    last_active_at INTEGER NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    embedding_model TEXT
);
CREATE INDEX IF NOT EXISTS stores_account ON stores(account_id);
CREATE TABLE IF NOT EXISTS store_files (
    store_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    status TEXT NOT NULL,
    usage_bytes INTEGER NOT NULL,
    chunk_count INTEGER NOT NULL,
    embedded INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    last_error TEXT,
    PRIMARY KEY (store_id, file_id)
);
CREATE TABLE IF NOT EXISTS chunks (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id TEXT NOT NULL,
    file_id TEXT NOT NULL,
    account_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    text TEXT NOT NULL,
    length INTEGER NOT NULL,
    terms TEXT NOT NULL,
    term_count INTEGER NOT NULL,
    vector BLOB
);
CREATE INDEX IF NOT EXISTS chunks_store ON chunks(store_id, account_id);
CREATE INDEX IF NOT EXISTS chunks_file ON chunks(file_id);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    terms, content='', tokenize='unicode61'
);
"""


class StoreError(Exception):
    """A refusal with an HTTP status and API error code."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def not_found(kind: str) -> StoreError:
    # One message for missing and foreign alike, so existence never leaks across accounts.
    noun = "vector store" if kind == "vector_store" else "file"
    return StoreError(404, f"{kind}_not_found", f"No such {noun} for this account")


@dataclass(frozen=True)
class Limits:
    max_file_bytes: int
    max_files_per_account: int
    max_bytes_per_account: int
    max_stores_per_account: int
    max_chunks_per_store: int
    chunk_chars: int
    chunk_overlap_chars: int


@dataclass
class SearchHit:
    store_id: str
    file_id: str
    filename: str
    chunk_index: int
    text: str
    score: float
    bm25: float = 0.0
    cosine: float = 0.0

    @property
    def chunk_id(self) -> str:
        return f"{self.file_id}#{self.chunk_index}"


def fts5_available() -> bool:
    try:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
        finally:
            connection.close()
    except sqlite3.Error:
        return False
    return True


def _pack(vector: Sequence[float]) -> bytes:
    return array.array("f", vector).tobytes()


def _unpack(blob: bytes) -> array.array:
    values = array.array("f")
    values.frombytes(blob)
    return values


def _norm(vector: Sequence[float]) -> float:
    return math.sqrt(sum(v * v for v in vector)) or 1.0


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    return sum(x * y for x, y in zip(a, b, strict=True)) / (_norm(a) * _norm(b))


def _phrase(term: str) -> str:
    """One FTS5 phrase: the term quoted, so no query syntax is ever interpreted."""
    return '"' + term.replace('"', '""') + '"'


def _now() -> int:
    return int(time.time())


class FileStore:
    """SQLite-backed files, stores and chunks. Thread-safe; call from a worker thread."""

    def __init__(self, root: str, limits: Limits, *, use_fts: bool | None = None) -> None:
        self.root = pathlib.Path(root)
        self.blobs = self.root / "blobs"
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.limits = limits
        self._lock = threading.RLock()
        self.db = sqlite3.connect(
            str(self.root / "file-search.sqlite3"), check_same_thread=False, isolation_level=None
        )
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(_SCHEMA)
        self.use_fts = fts5_available() if use_fts is None else use_fts
        if self.use_fts:
            self.db.executescript(_FTS_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self.db.close()

    # ------------------------------------------------------------------ helpers

    def _blob_path(self, account_id: str, file_id: str) -> pathlib.Path:
        return self.blobs / account_id / file_id

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}{uuid.uuid4().hex}"

    def _file_row(self, account_id: str, file_id: str) -> sqlite3.Row:
        row = self.db.execute(
            "SELECT id, filename, purpose, bytes, created_at FROM files "
            "WHERE id = ? AND account_id = ?",
            (file_id, account_id),
        ).fetchone()
        if row is None:
            raise not_found("file")
        return row

    def _store_row(self, account_id: str, store_id: str) -> sqlite3.Row:
        row = self.db.execute(
            "SELECT * FROM stores WHERE id = ? AND account_id = ?", (store_id, account_id)
        ).fetchone()
        if row is None:
            raise not_found("vector_store")
        return row

    def _delete_chunks(self, where: str, params: tuple[Any, ...]) -> None:
        if self.use_fts:
            rows = self.db.execute(f"SELECT rowid, terms FROM chunks WHERE {where}", params)
            for row in rows.fetchall():
                self.db.execute(
                    "INSERT INTO chunks_fts(chunks_fts, rowid, terms) VALUES('delete', ?, ?)",
                    (row["rowid"], row["terms"]),
                )
        self.db.execute(f"DELETE FROM chunks WHERE {where}", params)

    # ------------------------------------------------------------------ files

    def create_file(
        self, account_id: str, *, filename: str, purpose: str, data: bytes, text: str
    ) -> dict[str, Any]:
        limits = self.limits
        if len(data) > limits.max_file_bytes:
            raise StoreError(
                413, "file_too_large", f"File exceeds the {limits.max_file_bytes}-byte limit"
            )
        with self._lock:
            count, total = self.db.execute(
                "SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM files WHERE account_id = ?",
                (account_id,),
            ).fetchone()
            if count >= limits.max_files_per_account:
                raise StoreError(
                    409, "file_limit_reached",
                    f"This account already holds {limits.max_files_per_account} files",
                )
            if total + len(data) > limits.max_bytes_per_account:
                raise StoreError(
                    409, "storage_limit_reached",
                    f"This account's files would exceed {limits.max_bytes_per_account} bytes",
                )
            file_id = self._new_id("file-")
            path = self._blob_path(account_id, file_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".partial")
            temporary.write_bytes(data)
            os.replace(temporary, path)
            created = _now()
            self.db.execute(
                "INSERT INTO files (id, account_id, filename, purpose, bytes, created_at, text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (file_id, account_id, filename, purpose, len(data), created, text),
            )
            return self._file_object(self._file_row(account_id, file_id))

    @staticmethod
    def _file_object(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "object": "file",
            "bytes": row["bytes"],
            "created_at": row["created_at"],
            "filename": row["filename"],
            "purpose": row["purpose"],
            "status": "processed",
            "status_details": None,
            "expires_at": None,
        }

    def get_file(self, account_id: str, file_id: str) -> dict[str, Any]:
        with self._lock:
            return self._file_object(self._file_row(account_id, file_id))

    def list_files(self, account_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.db.execute(
                "SELECT id, filename, purpose, bytes, created_at FROM files "
                "WHERE account_id = ? ORDER BY created_at DESC, id LIMIT ?",
                (account_id, limit),
            ).fetchall()
            return [self._file_object(row) for row in rows]

    def delete_file(self, account_id: str, file_id: str) -> dict[str, Any]:
        with self._lock:
            self._file_row(account_id, file_id)
            self.db.execute("BEGIN")
            try:
                self._delete_chunks("file_id = ? AND account_id = ?", (file_id, account_id))
                self.db.execute(
                    "DELETE FROM store_files WHERE file_id = ? AND account_id = ?",
                    (file_id, account_id),
                )
                self.db.execute(
                    "DELETE FROM files WHERE id = ? AND account_id = ?", (file_id, account_id)
                )
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self._blob_path(account_id, file_id).unlink(missing_ok=True)
            return {"id": file_id, "object": "file", "deleted": True}

    def file_text(self, account_id: str, file_id: str) -> tuple[str, str]:
        with self._lock:
            row = self.db.execute(
                "SELECT filename, text FROM files WHERE id = ? AND account_id = ?",
                (file_id, account_id),
            ).fetchone()
            if row is None:
                raise not_found("file")
            return row["filename"], row["text"]

    # ------------------------------------------------------------------ stores

    def create_store(
        self,
        account_id: str,
        *,
        name: str,
        metadata: str,
        embedding_model: str | None,
    ) -> str:
        with self._lock:
            (count,) = self.db.execute(
                "SELECT COUNT(*) FROM stores WHERE account_id = ?", (account_id,)
            ).fetchone()
            if count >= self.limits.max_stores_per_account:
                raise StoreError(
                    409, "vector_store_limit_reached",
                    f"This account already holds {self.limits.max_stores_per_account} stores",
                )
            store_id = self._new_id("vs_")
            now = _now()
            self.db.execute(
                "INSERT INTO stores (id, account_id, name, created_at, last_active_at, metadata,"
                " embedding_model) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (store_id, account_id, name, now, now, metadata, embedding_model),
            )
            return store_id

    def store_object(self, account_id: str, store_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._store_row(account_id, store_id)
            counts = dict(
                self.db.execute(
                    "SELECT status, COUNT(*) FROM store_files WHERE store_id = ? "
                    "GROUP BY status",
                    (store_id,),
                ).fetchall()
            )
            (usage,) = self.db.execute(
                "SELECT COALESCE(SUM(length), 0) FROM chunks WHERE store_id = ?", (store_id,)
            ).fetchone()
        completed = counts.get("completed", 0)
        failed = counts.get("failed", 0)
        return {
            "id": row["id"],
            "object": "vector_store",
            "name": row["name"],
            "created_at": row["created_at"],
            "last_active_at": row["last_active_at"],
            "status": "completed",
            "usage_bytes": int(usage),
            "file_counts": {
                "in_progress": 0,
                "completed": completed,
                "failed": failed,
                "cancelled": 0,
                "total": completed + failed,
            },
            "metadata": json.loads(row["metadata"] or "{}"),
            "expires_after": None,
            "expires_at": None,
            # Fabric extension: how this store ranks results.
            "ranking": "hybrid" if row["embedding_model"] else "bm25",
        }

    def list_stores(self, account_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            ids = [
                row["id"]
                for row in self.db.execute(
                    "SELECT id FROM stores WHERE account_id = ? "
                    "ORDER BY created_at DESC, id LIMIT ?",
                    (account_id, limit),
                ).fetchall()
            ]
            return [self.store_object(account_id, store_id) for store_id in ids]

    def require_stores(self, account_id: str, store_ids: Sequence[str]) -> list[sqlite3.Row]:
        with self._lock:
            return [self._store_row(account_id, store_id) for store_id in store_ids]

    def delete_store(self, account_id: str, store_id: str) -> dict[str, Any]:
        with self._lock:
            self._store_row(account_id, store_id)
            self.db.execute("BEGIN")
            try:
                self._delete_chunks("store_id = ? AND account_id = ?", (store_id, account_id))
                self.db.execute("DELETE FROM store_files WHERE store_id = ?", (store_id,))
                self.db.execute(
                    "DELETE FROM stores WHERE id = ? AND account_id = ?", (store_id, account_id)
                )
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        return {"id": store_id, "object": "vector_store.deleted", "deleted": True}

    def touch(self, store_ids: Sequence[str]) -> None:
        with self._lock:
            now = _now()
            for store_id in store_ids:
                self.db.execute(
                    "UPDATE stores SET last_active_at = ? WHERE id = ?", (now, store_id)
                )

    # ------------------------------------------------------------------ store files

    def store_file_object(self, account_id: str, store_id: str, file_id: str) -> dict[str, Any]:
        with self._lock:
            self._store_row(account_id, store_id)
            row = self.db.execute(
                "SELECT * FROM store_files WHERE store_id = ? AND file_id = ? AND account_id = ?",
                (store_id, file_id, account_id),
            ).fetchone()
            if row is None:
                raise not_found("file")
        return self._store_file(row)

    @staticmethod
    def _store_file(row: sqlite3.Row) -> dict[str, Any]:
        error = None
        if row["last_error"]:
            code, _, message = str(row["last_error"]).partition(":")
            error = {"code": code, "message": message}
        return {
            "id": row["file_id"],
            "object": "vector_store.file",
            "vector_store_id": row["store_id"],
            "created_at": row["created_at"],
            "status": row["status"],
            "usage_bytes": row["usage_bytes"],
            "last_error": error,
            "attributes": None,
            "chunking_strategy": None,
            # Fabric extensions.
            "chunk_count": row["chunk_count"],
            "embedded": bool(row["embedded"]),
        }

    def list_store_files(self, account_id: str, store_id: str) -> list[dict[str, Any]]:
        with self._lock:
            self._store_row(account_id, store_id)
            rows = self.db.execute(
                "SELECT * FROM store_files WHERE store_id = ? AND account_id = ? "
                "ORDER BY created_at DESC, file_id",
                (store_id, account_id),
            ).fetchall()
        return [self._store_file(row) for row in rows]

    def detach_file(self, account_id: str, store_id: str, file_id: str) -> dict[str, Any]:
        with self._lock:
            self.store_file_object(account_id, store_id, file_id)
            self.db.execute("BEGIN")
            try:
                self._delete_chunks(
                    "store_id = ? AND file_id = ? AND account_id = ?",
                    (store_id, file_id, account_id),
                )
                self.db.execute(
                    "DELETE FROM store_files WHERE store_id = ? AND file_id = ?",
                    (store_id, file_id),
                )
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        return {"id": file_id, "object": "vector_store.file.deleted", "deleted": True}

    def plan_chunks(
        self, account_id: str, store_id: str, file_id: str, *, size: int, overlap: int
    ) -> tuple[list[Chunk], str | None]:
        """Chunks for ``file_id`` and the store's embeddings model; nothing is written."""
        with self._lock:
            store = self._store_row(account_id, store_id)
            _filename, text = self.file_text(account_id, file_id)
            existing = self.db.execute(
                "SELECT 1 FROM store_files WHERE store_id = ? AND file_id = ?",
                (store_id, file_id),
            ).fetchone()
            if existing is not None:
                raise StoreError(
                    409, "file_already_attached", "This file is already in this vector store"
                )
        return chunk_text(text, size=size, overlap=overlap), store["embedding_model"]

    def write_chunks(
        self,
        account_id: str,
        store_id: str,
        file_id: str,
        chunks: list[Chunk],
        vectors: list[list[float]] | None,
    ) -> dict[str, Any]:
        """Index ``chunks`` (and ``vectors``) for a file; refuse past the per-store cap."""
        with self._lock:
            self._store_row(account_id, store_id)
            self._file_row(account_id, file_id)
            (held,) = self.db.execute(
                "SELECT COUNT(*) FROM chunks WHERE store_id = ?", (store_id,)
            ).fetchone()
            now = _now()
            if held + len(chunks) > self.limits.max_chunks_per_store:
                self.db.execute(
                    "INSERT OR REPLACE INTO store_files (store_id, file_id, account_id, status,"
                    " usage_bytes, chunk_count, embedded, created_at, last_error)"
                    " VALUES (?, ?, ?, 'failed', 0, 0, 0, ?, ?)",
                    (
                        store_id, file_id, account_id, now,
                        "invalid_file:the vector store would exceed "
                        f"{self.limits.max_chunks_per_store} chunks",
                    ),
                )
                raise StoreError(
                    409, "chunk_limit_reached",
                    f"Attaching this file would exceed {self.limits.max_chunks_per_store} "
                    "chunks in the vector store",
                )
            self.db.execute("BEGIN")
            try:
                usage = 0
                for position, chunk in enumerate(chunks):
                    vector = _pack(vectors[position]) if vectors else None
                    terms = terms_of(chunk.text)
                    joined = " ".join(terms)
                    cursor = self.db.execute(
                        "INSERT INTO chunks (store_id, file_id, account_id, chunk_index, text,"
                        " length, terms, term_count, vector) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (store_id, file_id, account_id, chunk.index, chunk.text,
                         len(chunk.text.encode()), joined, len(terms), vector),
                    )
                    usage += len(chunk.text.encode())
                    if self.use_fts:
                        self.db.execute(
                            "INSERT INTO chunks_fts(rowid, terms) VALUES (?, ?)",
                            (cursor.lastrowid, joined),
                        )
                self.db.execute(
                    "INSERT OR REPLACE INTO store_files (store_id, file_id, account_id, status,"
                    " usage_bytes, chunk_count, embedded, created_at, last_error)"
                    " VALUES (?, ?, ?, 'completed', ?, ?, ?, ?, NULL)",
                    (store_id, file_id, account_id, usage, len(chunks), int(bool(vectors)), now),
                )
                self.db.execute(
                    "UPDATE stores SET last_active_at = ? WHERE id = ?", (now, store_id)
                )
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
        return self.store_file_object(account_id, store_id, file_id)

    # ------------------------------------------------------------------ search

    #: Most chunks one query rescores. FTS5 pre-ranks matches; only these are scored exactly.
    _FTS_CANDIDATES = 2000

    def _bm25(
        self, account_id: str, store_ids: Sequence[str], terms: list[str], limit: int
    ) -> list[tuple[int, float]]:
        """Okapi BM25 (k1=1.2, b=0.75) with statistics from the searched stores only.

        Corpus statistics (chunk count, average length, document frequency) come from the
        stores being searched, never from the whole database: FTS5's built-in ``bm25()``
        uses table-wide statistics, which would let one account's documents shift, and so
        reveal something about, another account's scores. FTS5 is used only to find the
        matching chunks quickly; the pure-Python path scans the stores instead. Both score
        with this one function, so they rank identically.
        """
        marks = ",".join("?" * len(store_ids))
        scope = f"chunks.account_id = ? AND chunks.store_id IN ({marks})"
        params = (account_id, *store_ids)
        total, average = self.db.execute(
            f"SELECT COUNT(*), AVG(term_count) FROM chunks WHERE {scope}", params
        ).fetchone()
        if not total:
            return []
        average = float(average or 1.0) or 1.0
        unique = list(dict.fromkeys(terms))
        if self.use_fts:
            frequency = {}
            for term in unique:
                (count,) = self.db.execute(
                    "SELECT COUNT(*) FROM chunks_fts JOIN chunks ON chunks.rowid = chunks_fts.rowid"
                    f" WHERE chunks_fts MATCH ? AND {scope}",
                    (_phrase(term), *params),
                ).fetchone()
                frequency[term] = count
            rows = self.db.execute(
                "SELECT chunks.rowid AS rowid, chunks.terms AS terms, chunks.term_count AS n"
                " FROM chunks_fts JOIN chunks ON chunks.rowid = chunks_fts.rowid"
                f" WHERE chunks_fts MATCH ? AND {scope} ORDER BY bm25(chunks_fts) LIMIT ?",
                (" OR ".join(_phrase(t) for t in unique), *params, self._FTS_CANDIDATES),
            ).fetchall()
            documents = [(row["rowid"], Counter(row["terms"].split()), row["n"]) for row in rows]
        else:
            rows = self.db.execute(
                "SELECT chunks.rowid AS rowid, chunks.terms AS terms, chunks.term_count AS n"
                f" FROM chunks WHERE {scope}",
                params,
            ).fetchall()
            documents = [(row["rowid"], Counter(row["terms"].split()), row["n"]) for row in rows]
            frequency = Counter()
            for _rowid, counts, _n in documents:
                frequency.update(term for term in unique if term in counts)
        k1, b = 1.2, 0.75
        idf = {
            term: math.log((total - frequency.get(term, 0) + 0.5)
                           / (frequency.get(term, 0) + 0.5) + 1.0)
            for term in unique
        }
        scored = []
        for rowid, counts, length in documents:
            score = 0.0
            for term in unique:
                tf = counts.get(term, 0)
                if tf:
                    score += idf[term] * tf * (k1 + 1) / (
                        tf + k1 * (1 - b + b * length / average)
                    )
            if score > 0:
                scored.append((rowid, score))
        scored.sort(key=lambda item: (-item[1], item[0]))
        return scored[:limit]

    def search(
        self,
        account_id: str,
        store_ids: Sequence[str],
        query: str,
        *,
        max_results: int,
        query_vector: list[float] | None = None,
        hybrid_weight: float = 0.5,
        score_threshold: float = 0.0,
    ) -> list[SearchHit]:
        """Top chunks for ``query`` across ``store_ids`` (all of this account)."""
        terms = list(dict.fromkeys(terms_of(query)))[:64]
        candidates = max(50, max_results * 5)
        with self._lock:
            self.require_stores(account_id, store_ids)
            if not store_ids:
                return []
            lexical: list[tuple[int, float]] = []
            if terms:
                lexical = self._bm25(account_id, store_ids, terms, candidates)
            semantic: dict[int, float] = {}
            if query_vector:
                marks = ",".join("?" * len(store_ids))
                rows = self.db.execute(
                    "SELECT rowid, vector FROM chunks WHERE account_id = ? AND vector IS NOT NULL"
                    f" AND store_id IN ({marks})",
                    (account_id, *store_ids),
                ).fetchall()
                query_norm = _norm(query_vector)
                for row in rows:
                    vector = _unpack(row["vector"])
                    if len(vector) != len(query_vector):
                        continue
                    dot = sum(x * y for x, y in zip(vector, query_vector, strict=True))
                    semantic[row["rowid"]] = dot / (_norm(vector) * query_norm)
                top_semantic = sorted(semantic.items(), key=lambda item: item[1], reverse=True)
                semantic = dict(top_semantic[:candidates])

            bm25 = dict(lexical)
            top_bm25 = max(bm25.values(), default=0.0)
            scores: dict[int, tuple[float, float, float]] = {}
            for rowid in set(bm25) | set(semantic):
                lexical_score = bm25.get(rowid, 0.0)
                cosine_score = max(0.0, semantic.get(rowid, 0.0))
                if query_vector:
                    lexical_norm = lexical_score / top_bm25 if top_bm25 > 0 else 0.0
                    score = (1 - hybrid_weight) * lexical_norm + hybrid_weight * cosine_score
                else:
                    # Absolute rather than relative, so a weak best match still scores low.
                    score = lexical_score / (lexical_score + 1.0)
                scores[rowid] = (score, lexical_score, cosine_score)
            ranked = sorted(scores.items(), key=lambda item: (-item[1][0], item[0]))
            ranked = [item for item in ranked if item[1][0] > score_threshold][:max_results]
            if not ranked:
                return []
            marks = ",".join("?" * len(ranked))
            rows = {
                row["rowid"]: row
                for row in self.db.execute(
                    "SELECT chunks.rowid AS rowid, chunks.store_id, chunks.file_id,"
                    " chunks.chunk_index, chunks.text, files.filename FROM chunks"
                    " JOIN files ON files.id = chunks.file_id"
                    f" WHERE chunks.rowid IN ({marks}) AND chunks.account_id = ?",
                    (*[rowid for rowid, _ in ranked], account_id),
                ).fetchall()
            }
        hits = []
        for rowid, (score, lexical_score, cosine_score) in ranked:
            row = rows.get(rowid)
            if row is None:
                continue
            hits.append(
                SearchHit(
                    store_id=row["store_id"],
                    file_id=row["file_id"],
                    filename=row["filename"],
                    chunk_index=row["chunk_index"],
                    text=row["text"],
                    score=round(score, 6),
                    bm25=round(lexical_score, 6),
                    cosine=round(cosine_score, 6),
                )
            )
        return hits
