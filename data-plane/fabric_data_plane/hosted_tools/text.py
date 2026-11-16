"""Text extraction and chunking for file search (docs/file-search.md).

Extraction is deliberately narrow: plain text, Markdown, JSON, CSV and HTML are decoded with
the standard library. PDF is accepted only when the optional ``pypdf`` dependency (pure
Python, pinned in the ``pdf`` extra) is installed; without it a PDF upload is refused with
``unsupported_file`` rather than indexed as garbage. Anything else is refused.

Chunking works on characters, not model tokens: the gateway has no tokenizer for whatever
model a deployment serves, and a character budget is predictable. Four characters per token
is the conversion used when a caller sends OpenAI's token-denominated ``chunking_strategy``.
"""

from __future__ import annotations

import csv
import html.parser
import io
import json
import re
from dataclasses import dataclass

#: Extensions accepted, mapped to the extractor kind.
TEXT_KINDS = {
    ".txt": "text",
    ".text": "text",
    ".md": "text",
    ".markdown": "text",
    ".rst": "text",
    ".log": "text",
    ".json": "json",
    ".csv": "csv",
    ".html": "html",
    ".htm": "html",
    ".pdf": "pdf",
}
CHARS_PER_TOKEN = 4


class ExtractionError(Exception):
    """The upload cannot be turned into text; ``code`` is the API error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def pdf_supported() -> bool:
    try:
        import pypdf  # noqa: F401
    except ImportError:
        return False
    return True


def file_kind(filename: str) -> str:
    lowered = filename.lower()
    dot = lowered.rfind(".")
    kind = TEXT_KINDS.get(lowered[dot:] if dot >= 0 else "")
    if kind is None:
        raise ExtractionError(
            "unsupported_file",
            "Supported file types: " + ", ".join(sorted(TEXT_KINDS)),
        )
    if kind == "pdf" and not pdf_supported():
        raise ExtractionError(
            "unsupported_file",
            "PDF extraction is not installed on this gateway (optional 'pdf' extra); "
            "upload the text as .txt or .md instead",
        )
    return kind


def _decode(data: bytes) -> str:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ExtractionError("invalid_file", "File is not valid UTF-8 text") from exc
    if "\x00" in text:
        raise ExtractionError("invalid_file", "File contains binary data")
    return text


class _HtmlText(html.parser.HTMLParser):
    _SKIP = frozenset({"script", "style", "noscript", "template", "head"})
    _BLOCK = frozenset(
        {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section",
         "article", "pre", "blockquote", "table", "ul", "ol", "header", "footer"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs) -> None:  # noqa: ANN001 - stdlib signature
        if tag in self._SKIP:
            self._skipping += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skipping:
            self._skipping -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def _html(text: str) -> str:
    parser = _HtmlText()
    parser.feed(text)
    parser.close()
    joined = "".join(parser.parts).replace("\xa0", " ")
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in joined.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _csv(text: str) -> str:
    rows = csv.reader(io.StringIO(text))
    try:
        return "\n".join(", ".join(cell.strip() for cell in row) for row in rows)
    except csv.Error as exc:
        raise ExtractionError("invalid_file", "File is not valid CSV") from exc


def _json(text: str) -> str:
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise ExtractionError("invalid_file", "File is not valid JSON") from exc
    # Pretty-printed so keys and values tokenize as words for BM25.
    return json.dumps(document, indent=1, ensure_ascii=False)


def _pdf(data: bytes) -> str:
    import pypdf

    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as exc:  # noqa: BLE001 - any parser failure is the upload's fault
        raise ExtractionError("invalid_file", "PDF could not be parsed") from exc
    return "\n\n".join(page.strip() for page in pages if page.strip())


def extract_text(filename: str, data: bytes) -> str:
    """Text content of an upload, or :class:`ExtractionError`."""
    kind = file_kind(filename)
    if kind == "pdf":
        text = _pdf(data)
    else:
        decoded = _decode(data)
        text = {"text": lambda t: t, "json": _json, "csv": _csv, "html": _html}[kind](decoded)
    text = text.replace("\r\n", "\n").strip()
    if not text:
        raise ExtractionError("invalid_file", "File contains no extractable text")
    return text


# --------------------------------------------------------------------------- chunking


@dataclass(frozen=True)
class Chunk:
    index: int
    text: str
    start: int  # character offset in the extracted text


def _break_point(text: str, start: int, end: int) -> int:
    """The best place at or before ``end`` to cut, preferring paragraph, line, sentence."""
    if end >= len(text):
        return len(text)
    floor = start + (end - start) // 2  # never cut a chunk to less than half its budget
    for separator in ("\n\n", "\n", ". ", "? ", "! ", "; ", ", ", " "):
        at = text.rfind(separator, floor, end)
        if at != -1:
            return at + len(separator)
    return end


def chunk_text(text: str, *, size: int, overlap: int) -> list[Chunk]:
    """Split ``text`` into chunks of at most ``size`` characters overlapping by ``overlap``.

    Cuts prefer paragraph, then line, sentence and word boundaries in the second half of the
    budget, so a chunk rarely ends mid-word. ``overlap`` is clamped below ``size / 2`` so the
    window always advances.
    """
    if size < 1:
        raise ValueError("chunk size must be positive")
    overlap = max(0, min(overlap, size // 2 - 1 if size > 2 else 0))
    chunks: list[Chunk] = []
    start = 0
    length = len(text)
    while start < length:
        end = _break_point(text, start, min(start + size, length))
        piece = text[start:end].strip()
        if piece:
            chunks.append(Chunk(index=len(chunks), text=piece, start=start))
        if end >= length:
            break
        next_start = max(end - overlap, start + 1)
        if overlap:
            # Begin the overlap at the earliest line or sentence start in the window that still
            # leaves a real overlap, else at a word boundary: a chunk should not open
            # mid-sentence, and should not lose its overlap to a boundary at the very end.
            limit = end - max(1, overlap // 4)
            starts = [
                at + len(separator)
                for separator in ("\n", ". ", "? ", "! ")
                if (at := text.find(separator, next_start, end)) != -1
                and at + len(separator) <= limit
            ]
            space = text.find(" ", next_start, end)
            if starts:
                next_start = min(starts)
            elif space != -1:
                next_start = space + 1
        start = next_start
    return chunks


_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """Lower-cased word tokens, the same rule for documents and queries."""
    return [token.lower() for token in _TOKEN.findall(text)]


def stem(token: str) -> str:
    """Light English suffix stripping, so "passwords" finds "password" and back.

    Deliberately conservative (plural, -ing, -ed): the same rule runs on documents and
    queries, in both the FTS5 and the pure-Python index, so the two always agree.
    """
    if len(token) > 4 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 4 and token.endswith("sses"):
        return token[:-2]
    if len(token) > 5 and token.endswith("ing"):
        return token[:-3]
    if len(token) > 4 and token.endswith("ed") and not token.endswith("eed"):
        return token[:-2]
    if len(token) > 3 and token.endswith("s") and not token.endswith(("ss", "us", "is")):
        return token[:-1]
    return token


def terms_of(text: str) -> list[str]:
    """Index terms: tokens, lower-cased and stemmed. Documents and queries share it."""
    return [stem(token) for token in tokenize(text)]
