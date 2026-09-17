"""Reading token usage out of a streamed completion, without losing what the client asked for.

Streaming is the default for chat clients, so before this most of the platform's usage was
uncounted: the relay copied upstream bytes straight through and recorded a usage row of zero
tokens (M6, `docs/platform-direction.md`). The tokens were never missing from the protocol —
OpenAI-compatible servers will report them when the request asks with
``stream_options.include_usage`` — they were missing because nobody asked and nobody read.

Four rules shape this module, in priority order.

*A frame this module does not recognise is forwarded.* Suppression is decided by a positive
test against a known shape, never by "it did not look like content". An unmodelled shape must
degrade to passthrough, because the alternative is deleting a customer's answer.

*Forwarded bytes are the original bytes.* Nothing is re-serialised. A ``json.loads``/
``json.dumps`` round trip would reorder keys, change number formatting, and drop fields this
module does not model. Only frames that are deliberately dropped are treated as anything other
than opaque.

*What is withheld is bounded.* A frame is held only until its terminator arrives, which is what
a client would do anyway — but a host that never sends one must not be able to make the gateway
buffer its whole response. Past a ceiling the held bytes are released and framing resumes with
whatever follows. Releasing early costs nothing the client can see, because a client reassembles
the byte stream regardless of how it was split; it costs only the chance to read that one frame.
Two details keep that true: a terminator's worth of bytes is retained so a released span cannot
end mid-boundary and merge two events, and the frame that resumes framing is never suppressed,
because it opens with the tail of a frame the client has already been sent.

*A subtotal is not a total.* ``include_usage`` reports once, at the end, so a value read from a
usage-only frame is final. ``continuous_usage_stats`` reports a running count on every frame, so
a value read from a content frame is only the total if the stream actually reached its end with
framing intact. Otherwise this module reports no usage at all and says why: a partial count
recorded as a total under-bills silently, whereas an unmeasured stream that looks unmeasured is
a number somebody can act on.
"""

from __future__ import annotations

import dataclasses
import json
import math
from decimal import Decimal, DecimalException
from typing import Any

#: Server-sent events end with a blank line. Each line ending may independently be LF, CR,
#: or CRLF, so there are eight byte spellings (CR+LF is one CRLF ending, not two endings).
#: Original bytes are retained; these strings are only boundary detectors.
_TERMINATORS = (
    b"\n\n",
    b"\n\r",
    b"\n\r\n",
    b"\r\r",
    b"\r\r\n",
    b"\r\n\n",
    b"\r\n\r",
    b"\r\n\r\n",
)

#: Longest terminator, for the incremental scan that avoids rescanning the whole buffer.
_MAX_TERMINATOR = max(len(terminator) for terminator in _TERMINATORS)

#: The sentinel an OpenAI-compatible stream ends with. It is not JSON.
_DONE = b"[DONE]"

#: A frame carrying more than this is forwarded without being parsed. A model host is trusted
#: to be well-behaved; this only stops a pathological frame from being JSON-decoded. It bounds
#: parsing, not buffering: a frame this large can only be seen if it arrived complete in one
#: chunk, because anything held for a terminator is released at ``MAX_WITHHELD_BYTES``.
MAX_PARSED_FRAME_BYTES = 1 << 20

#: How many bytes may be held back waiting for a frame terminator. Past this the held bytes are
#: released to the client and framing restarts from what follows, so a host that never sends a
#: terminator streams in pieces of this size instead of blocking until it is done.
#:
#: This bounds *withheld* bytes, not frame size. A frame that arrives complete inside a single
#: chunk was never withheld and is parsed whatever its length; a frame that arrives in pieces is
#: released once the pieces reach the ceiling. That is the property to reason about: the client
#: is never waiting on more than this, and no stream can cost more than this per connection.
MAX_WITHHELD_BYTES = 64 << 10

#: Largest token count accepted from one report. This is a per-request count, not a lifetime
#: counter; signed 64-bit is already orders of magnitude above any context window and keeps every
#: conversion and downstream integer serialization bounded.
MAX_TOKEN_COUNT = (1 << 63) - 1

#: Decimal exponents beyond this are rejected while parsing even in metadata. A few input bytes
#: can otherwise describe an integer with billions of digits; candidate classification must be
#: bounded by input size, not by the number a compact exponent denotes.
_MAX_JSON_DECIMAL_EXPONENT = 1_000

#: Top-level keys an OpenAI-compatible chunk carries that are *not* content. A frame whose keys
#: all fall in here, with an explicitly empty ``choices`` and a ``usage`` object, is the frame
#: this module asked for. Anything else — including a shape that omits ``choices`` entirely —
#: may be carrying an answer and is forwarded.
_METADATA_KEYS = frozenset(
    {
        "id",
        "object",
        "created",
        "model",
        "system_fingerprint",
        "service_tier",
        "choices",
        "usage",
    }
)

#: Sentinel that distinguishes an omitted count from an explicit JSON ``null``. Omission means
#: the host is reporting zero for that side; a present null is not a token count and voids the
#: report.
_MISSING = object()

#: Why a streamed request produced no usable token count. Exported so the metric that counts
#: these can publish every reason as a zero-valued series before any of them happens.
UNMETERED_REASONS = ("no_report", "incomplete_stream", "framing_lost")


@dataclasses.dataclass(frozen=True)
class StreamUsage:
    """Tokens a streamed response reported for itself."""

    input_tokens: int
    output_tokens: int

    def as_payload(self) -> dict[str, Any]:
        """The shape ``DataPlane.record_usage`` reads, so one recorder serves both paths."""
        return {
            "usage": {
                "prompt_tokens": self.input_tokens,
                "completion_tokens": self.output_tokens,
            }
        }


def streaming_upstream_payload(payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Return the upstream body that will report usage, and whether the client asked for it.

    Merged rather than replaced: a client that set other ``stream_options`` keys keeps them.
    The returned flag decides whether the usage frame is forwarded or consumed, so a caller
    that asked for usage still gets exactly the stream it requested.
    """
    options = payload.get("stream_options")
    options = dict(options) if isinstance(options, dict) else {}
    client_asked = bool(options.get("include_usage"))
    return {**payload, "stream_options": {**options, "include_usage": True}}, client_asked


class UsageMeter:
    """Splits an SSE byte stream into frames, keeping the usage and forwarding the rest."""

    def __init__(self, *, forward_usage_frame: bool) -> None:
        self._forward_usage_frame = forward_usage_frame
        self._buffer = bytearray()
        self._scanned = 0
        self._framing_lost = False
        self._resyncing = False
        self._stream_ended = False
        self._usage: StreamUsage | None = None
        self._usage_is_final = False

    @property
    def usage(self) -> StreamUsage | None:
        """The tokens this stream reported, or ``None`` if it reported no usable total.

        A usage-only frame is the report ``include_usage`` asks for and arrives once, at the
        end, so it is a total on sight. A count on a content frame is a ``continuous_usage_stats``
        running subtotal, and is only this stream's total if the stream reached its end and no
        frame went by unread on the way.
        """
        if self._usage is None:
            return None
        if self._usage_is_final:
            return self._usage
        if self._stream_ended and not self._framing_lost:
            return self._usage
        return None

    @property
    def framing_lost(self) -> bool:
        """Whether a frame ever went by unread because this module refused to hold or parse it."""
        return self._framing_lost

    @property
    def unmetered_reason(self) -> str | None:
        """Why this stream cannot be billed, or ``None`` if it can.

        Reported rather than swallowed so the loss is attributable: a host that ignores
        ``include_usage`` is somebody else's configuration, a stream that was cut short is the
        client's or the network's, and a frame this module declined to hold or parse is the
        gateway's own limit.
        """
        if self.usage is not None:
            return None
        if self._framing_lost:
            return "framing_lost"
        if not self._stream_ended:
            return "incomplete_stream"
        return "no_report"

    def reset(self) -> None:
        """Forget everything about the current attempt.

        A retried request starts a new upstream stream. Carrying a dead attempt's partial frame
        into it would splice two streams into one unparseable frame, and carrying its usage
        would bill the client for a stream they never received.
        """
        self._buffer = bytearray()
        self._scanned = 0
        self._framing_lost = False
        self._resyncing = False
        self._stream_ended = False
        self._usage = None
        self._usage_is_final = False

    def feed(self, chunk: bytes) -> bytes:
        """Consume upstream bytes and return the bytes to forward to the client."""
        self._buffer.extend(chunk)
        forward = bytearray()
        while True:
            frame = self._take_frame(final=False)
            if frame is None:
                break
            if self._keep(frame):
                forward.extend(frame)

        # Whatever is left has no terminator yet. Held past the ceiling it stops being a frame
        # in transit and becomes a stall, so it is released and framing restarts on what follows.
        # The client cannot tell the difference; only this module loses that frame's contents.
        if len(self._buffer) > MAX_WITHHELD_BYTES:
            # All but a terminator's worth. The released bytes might end *inside* the terminator
            # — one "\n" of a "\n\n" — and releasing that partial boundary would erase it: the
            # tail could no longer be recognised as the end of an event, so it would be cut
            # together with the frame that follows and the client would receive two events merged
            # into one. Keeping the last few bytes leaves the boundary intact.
            keep_back = _MAX_TERMINATOR - 1
            self._framing_lost = True
            self._resyncing = True
            forward.extend(self._buffer[:-keep_back])
            self._buffer = bytearray(self._buffer[-keep_back:])
            self._scanned = 0
        return bytes(forward)

    def finish(self, *, complete: bool) -> bytes:
        """Return trailing bytes that never got a terminator, and record how the stream ended.

        ``complete`` says the upstream response was read to its end. It is what separates a
        final count from a running subtotal, so it must be false whenever the stream stopped
        early — a transport failure mid-body, or a client that hung up.

        A stream that ends without a final blank line still has a complete event in it if the
        remainder parses, so the forward/consume decision is applied one last time: otherwise a
        host that omits the trailing terminator would hand the client the usage frame it never
        asked for. A remainder that does not parse is forwarded, exactly as the byte relay this
        replaced would have.
        """
        # A latch, not an assignment. The relay calls this again on the failure branch when the
        # error surfaces from closing the response rather than from reading it, and a body that
        # was already read to its end does not stop having been read to its end.
        self._stream_ended = self._stream_ended or complete
        forward = bytearray()
        while True:
            frame = self._take_frame(final=True)
            if frame is None:
                break
            if self._keep(frame):
                forward.extend(frame)
        remainder = bytes(self._buffer)
        self._buffer = bytearray()
        self._scanned = 0
        if not remainder:
            return bytes(forward)
        if self._keep(remainder):
            forward.extend(remainder)
        return bytes(forward)

    def _take_frame(self, *, final: bool) -> bytes | None:
        """Split the first complete frame off the buffer, terminator included.

        Scanning resumes from where the last search gave up, minus the longest terminator, so a
        large frame arriving in small chunks is linear rather than quadratic. A match ending in
        bare CR at the current chunk boundary is deferred: the next byte may be LF, making that
        CR part of a CRLF ending. ``final`` resolves the ambiguity at end-of-stream.
        """
        start = max(0, self._scanned - (_MAX_TERMINATOR - 1))
        boundary = -1
        end = -1
        for terminator in _TERMINATORS:
            index = self._buffer.find(terminator, start)
            if index == -1:
                continue
            candidate = index + len(terminator)
            if not final and candidate == len(self._buffer) and terminator.endswith(b"\r"):
                continue
            # Earliest boundary wins. At the same byte, longest wins so CRLF remains atomic:
            # ``\n\r\n`` must consume all three bytes rather than matching ``\n\r`` first.
            if boundary == -1 or index < boundary or (index == boundary and candidate > end):
                boundary = index
                end = candidate
        if end == -1:
            self._scanned = len(self._buffer)
            return None
        frame = bytes(self._buffer[:end])
        self._buffer = bytearray(self._buffer[end:])
        self._scanned = 0
        return frame

    def _keep(self, frame: bytes) -> bool:
        """Record any usage in this frame and decide whether the client should see it."""
        # The first frame after a release begins with the tail of a frame that was already sent
        # on, so its prefix is unavailable. It is not merely unsuppressible; it is untrustworthy
        # for classification. A retained suffix such as ``dat`` could otherwise join later bytes
        # into a fabricated ``data:`` usage report and create a false bill. Forward it without
        # parsing or changing usage state, then resume at the following boundary.
        if self._resyncing:
            self._resyncing = False
            return True

        payload = _data_payload(frame)
        if payload is None:
            if _has_data_field(frame):
                self._invalidate_subtotal()
            return True
        if payload == _DONE:
            return True
        if len(payload) > MAX_PARSED_FRAME_BYTES:
            # Unread for the same reason as a released frame — a limit this module imposes — so
            # it counts against trusting a subtotal, and it is forwarded whatever its shape.
            self._framing_lost = True
            return True
        try:
            document = _strict_json_document(payload)
        except (UnicodeDecodeError, ValueError, RecursionError):
            # Syntax/decoding errors, ambiguous/non-standard JSON and valid JSON nested beyond
            # Python's recursion limit are all unclassifiable here. None is Fabric's known usage
            # frame, so the original bytes pass through rather than being deleted or aborting a
            # successful relay. It still invalidates a running subtotal: this unread data may be
            # the later report that proves the earlier count was not final.
            self._invalidate_subtotal()
            return True
        if not isinstance(document, dict):
            self._invalidate_subtotal()
            return True

        usage_only = _is_usage_only(document)
        reported_usage = document.get("usage")
        usage = _read_usage(reported_usage)
        if usage is not None:
            self._usage = usage
            # A usage-only frame is the terminal report; a count riding on a content frame is a
            # subtotal that only becomes a total when the stream is seen to end.
            self._usage_is_final = usage_only
        else:
            # Any later data event without a valid report makes an earlier cumulative count
            # stale. A separately valid terminal report can restore trust afterwards.
            self._invalidate_subtotal()

        if self._forward_usage_frame:
            return True
        # Decided from the frame's *shape*, not from whether its numbers were readable: a
        # usage-only frame this module caused is consumed even when its counts are unusable,
        # because the client never asked to receive it.
        return not usage_only

    def _invalidate_subtotal(self) -> None:
        """Prevent an earlier cumulative count from becoming a total after unread later data."""
        if not self._usage_is_final:
            self._usage = None
            self._usage_is_final = False


def _strict_json_document(payload: bytes) -> Any:
    """Decode only interoperable UTF-8 JSON with unambiguous object members.

    ``json.loads`` on bytes auto-detects UTF-16/32 and its defaults accept duplicate members and
    JavaScript constants such as ``NaN``. None is the known UTF-8 JSON event Fabric requested,
    and duplicate resolution can erase an earlier content-bearing ``choices`` before the positive
    suppression test sees it. Decode explicitly, reject a field-value BOM, reject constants, and
    reject a duplicate at any object depth.
    """
    text = payload.decode("utf-8", errors="strict")
    if text.startswith("\ufeff"):
        raise ValueError("a BOM inside an SSE data value is content, not JSON framing")
    return json.loads(
        text,
        object_pairs_hook=_unique_object,
        parse_constant=_reject_json_constant,
        parse_float=_parse_json_decimal,
    )


def _parse_json_decimal(value: str) -> Decimal:
    """Parse a JSON decimal without letting a compact exponent imply unbounded work."""
    try:
        number = Decimal(value)
    except DecimalException as exc:
        raise ValueError("decimal is outside the supported exponent range") from exc
    if abs(number.as_tuple().exponent) > _MAX_JSON_DECIMAL_EXPONENT:
        raise ValueError("decimal exponent is too large")
    return number


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build one JSON object while refusing ambiguous duplicate member names."""
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"duplicate JSON member: {key}")
        document[key] = value
    return document


def _reject_json_constant(value: str) -> Any:
    """Reject Python's non-standard ``NaN``/``Infinity`` JSON extensions."""
    raise ValueError(f"non-standard JSON constant: {value}")


def _is_usage_only(document: dict[str, Any]) -> bool:
    """Whether this frame exists only to report usage.

    A positive test. ``choices`` must be present and empty, ``usage`` must be there, and every
    other key must be known metadata. A frame that omits ``choices``, or carries a key this
    module does not model, may be an answer in a shape it does not recognise and is forwarded.
    """
    # A usable usage object, not merely the key: ``include_usage`` puts ``usage: null`` on
    # ordinary chunks, and a frame carrying nothing at all is forwarded rather than swallowed.
    if not isinstance(document.get("usage"), dict):
        return False
    choices = document.get("choices")
    if not isinstance(choices, list) or choices:
        return False
    return set(document) <= _METADATA_KEYS


def _has_data_field(frame: bytes) -> bool:
    """Whether an opaque SSE envelope still carries data that may supersede a subtotal."""
    normalized = frame.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return any(
        line == b"data" or line.startswith(b"data:")
        for line in normalized.split(b"\n")
    )


def _data_payload(frame: bytes) -> bytes | None:
    """Concatenate the ``data:`` lines of one frame, or ``None`` if it carries no data.

    Joined with a newline, as server-sent events specifies, so a frame batching two documents
    does not silently concatenate into something unparseable.

    Comments and other SSE fields (``event:``, ``id:``, ``retry:``) are not data and leave the
    frame opaque, which means it is forwarded untouched.
    """
    parts: list[bytes] = []
    for line in frame.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n"):
        if not line:
            continue
        if line == b"data":
            parts.append(b"")
            continue
        if not line.startswith(b"data:"):
            # Unknown SSE envelope semantics are not Fabric's usage frame. In particular,
            # ``event:``, ``id:``, ``retry:`` and comments can change how a client interprets
            # the data; deleting such a frame because its JSON resembles usage would violate
            # the positive-classification rule.
            return None
        value = line[len(b"data:") :]
        # SSE removes exactly one optional ASCII space after the colon. Python ``strip`` is far
        # broader: it would erase vertical-tab/form-feed bytes that SSE preserves and JSON
        # rejects, turning an opaque event into a suppressible usage document.
        if value.startswith(b" "):
            value = value[1:]
        parts.append(value)
    if not parts:
        return None
    return b"\n".join(parts)


def _read_usage(usage: Any) -> StreamUsage | None:
    """Read a usage object, ignoring one that reports nothing usable.

    ``include_usage`` makes a compliant host attach ``usage: null`` to every ordinary chunk, so
    the common case here is a non-dict that means "not this frame".

    A count that is present but not a token count — negative, fractional, a string, a bool —
    voids the whole report rather than being repaired. Reading the counts that happen to look
    sane and zeroing the rest would put a confidently wrong number on an invoice; refusing the
    report leaves the stream visibly unmetered instead.

    A server sending ``continuous_usage_stats`` reports usage on every frame, and an early frame
    can legitimately carry zeros. Treating all-zero as absent keeps the meter on the last frame
    that actually counted something rather than latching onto the first.
    """
    if not isinstance(usage, dict):
        return None
    prompt = _read_count(usage.get("prompt_tokens", _MISSING))
    completion = _read_count(usage.get("completion_tokens", _MISSING))
    if prompt is None or completion is None:
        return None
    if prompt == 0 and completion == 0:
        return None
    return StreamUsage(input_tokens=prompt, output_tokens=completion)


def _read_count(value: Any) -> int | None:
    """Read one token count: an ``int``, or ``None`` if the value is unusable.

    A missing key reads as zero, because a host reporting only some of the counts is reporting
    zero for the others. A key that is *present* and not a whole non-negative number is not a
    count at all, and says the reporter cannot be trusted for this stream.

    Bools are excluded because ``True`` is an ``int`` in Python and a host sending one is not
    reporting one token.
    """
    if value is _MISSING:
        return 0
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        return None
    if isinstance(value, Decimal):
        if not value.is_finite() or value < 0 or value > MAX_TOKEN_COUNT:
            return None
        if value != value.to_integral_value():
            return None
    elif isinstance(value, float):
        if not math.isfinite(value) or value < 0 or value > MAX_TOKEN_COUNT:
            return None
        if not value.is_integer():
            return None
    elif value < 0 or value > MAX_TOKEN_COUNT:
        return None
    count = int(value)
    return count
