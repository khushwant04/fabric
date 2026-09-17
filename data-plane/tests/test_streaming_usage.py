"""Streamed requests are metered, and the client's stream is unchanged (M6).

Streaming is the default for chat clients, and it used to record a usage row of zero tokens,
so most of the platform's usage was uncounted while looking accounted-for. These tests pin the
two halves of the fix: the tokens are read from the stream, and reading them does not alter
what the caller receives.
"""

from __future__ import annotations

import json

import pytest

from fabric_data_plane.streaming import (
    MAX_PARSED_FRAME_BYTES,
    MAX_WITHHELD_BYTES,
    UNMETERED_REASONS,
    UsageMeter,
    streaming_upstream_payload,
)
from tests.conftest import ACCOUNT_A, DEPLOYMENT_A, SigningKey, UpstreamStub

CHAT = "/v1/chat/completions"


def _unmetered(deployment: str, account: str, reason: str) -> str:
    """The counter line for one loss reason, as it appears in the exposition text."""
    return (
        f'fabric_dp_unmetered_streams_total{{deployment_id="{deployment}",'
        f'account_id="{account}",reason="{reason}"}}'
    )


async def _stream(client, token: str, **body: object):
    """One streamed chat request, as a client would send it."""
    payload = {"model": "launch-model", "stream": True, **body}
    return await client.post(CHAT, json=payload, headers={"Authorization": f"Bearer {token}"})


# --- the meter, in isolation ----------------------------------------------


def test_usage_is_read_from_the_terminal_frame() -> None:
    meter = UsageMeter(forward_usage_frame=False)

    forwarded = meter.feed(
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        b'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":7}}\n\n'
        b"data: [DONE]\n\n"
    )

    assert meter.usage is not None
    assert (meter.usage.input_tokens, meter.usage.output_tokens) == (11, 7)
    # The usage frame is consumed, because this request asked for it on the client's behalf.
    assert b"usage" not in forwarded
    assert b'"content":"hi"' in forwarded
    assert forwarded.endswith(b"data: [DONE]\n\n")


def test_a_frame_split_across_chunk_boundaries_is_still_read() -> None:
    # aiter_bytes gives arbitrary byte boundaries, so a frame can arrive in pieces.
    meter = UsageMeter(forward_usage_frame=False)
    frame = b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":5}}\n\n'

    for index in range(len(frame)):
        meter.feed(frame[index : index + 1])

    assert meter.usage is not None
    assert (meter.usage.input_tokens, meter.usage.output_tokens) == (3, 5)


def test_content_frames_are_forwarded_byte_for_byte() -> None:
    # Never re-serialised: that would reorder keys, change number formatting, and drop
    # fields this code does not model.
    meter = UsageMeter(forward_usage_frame=False)
    exact = b'data: {"z":1,"a":2.50,"choices":[{"delta":{"content":"x"}}],"extra":null}\n\n'

    assert meter.feed(exact) == exact


def test_a_client_that_asked_for_usage_still_receives_it() -> None:
    meter = UsageMeter(forward_usage_frame=True)
    frame = b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":2}}\n\n'

    assert meter.feed(frame) == frame
    assert meter.usage is not None


def test_a_usage_frame_that_also_carries_content_is_always_forwarded() -> None:
    # continuous_usage_stats puts usage on ordinary frames. Those belong to the caller.
    meter = UsageMeter(forward_usage_frame=False)
    frame = (
        b'data: {"choices":[{"delta":{"content":"hi"}}],'
        b'"usage":{"prompt_tokens":4,"completion_tokens":1}}\n\n'
    )

    assert meter.feed(frame) == frame
    # A cumulative count, so it becomes this stream's total only once the stream ends.
    meter.finish(complete=True)
    assert meter.usage is not None


def test_malformed_and_non_data_frames_are_forwarded_untouched() -> None:
    meter = UsageMeter(forward_usage_frame=False)

    assert meter.feed(b"data: not-json\n\n") == b"data: not-json\n\n"
    assert meter.feed(b": keep-alive\n\n") == b": keep-alive\n\n"
    assert meter.feed(b"event: ping\ndata: 1\n\n") == b"event: ping\ndata: 1\n\n"
    assert meter.usage is None


def test_an_unknown_sse_envelope_is_never_suppressed() -> None:
    """Only a plain data event is the usage frame Fabric caused.

    ``event:``, ``id:``, ``retry:`` and comments have client-visible SSE semantics. A frame with
    any of them is opaque even when its JSON happens to resemble the terminal usage shape.
    """
    payload = b'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":3}}\n\n'
    for prefix in (b"event: answer\n", b"id: 7\n", b"retry: 1000\n", b": private\n"):
        meter = UsageMeter(forward_usage_frame=False)
        frame = prefix + payload

        assert meter.feed(frame) == frame
        assert meter.usage is None


def test_deeply_nested_json_is_forwarded_when_the_decoder_refuses_it() -> None:
    """A decoder resource limit is not permission to abort or delete an opaque event."""
    meter = UsageMeter(forward_usage_frame=False)
    frame = b"data: " + b"[" * 20_000 + b"0" + b"]" * 20_000 + b"\n\n"

    assert len(frame) < MAX_WITHHELD_BYTES
    assert meter.feed(frame) == frame
    assert meter.usage is None


def test_sse_data_values_remove_only_one_optional_ascii_space() -> None:
    """SSE-preserved whitespace cannot be stripped into a suppressible JSON document."""
    meter = UsageMeter(forward_usage_frame=False)
    document = b'{"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":3}}'

    for whitespace in (b"\x0b", b"\x0c"):
        frame = b"data:" + whitespace + document + whitespace + b"\n\n"
        assert meter.feed(frame) == frame
        assert meter.usage is None


def test_ambiguous_or_non_interoperable_json_is_never_suppressed() -> None:
    """Positive classification starts only after strict UTF-8, unambiguous JSON decoding."""
    duplicate_choices = (
        b'{"choices":[{"delta":{"content":"secret"}}],"choices":[],'
        b'"usage":{"prompt_tokens":9,"completion_tokens":4}}'
    )
    ordinary = b'{"choices":[],"usage":{"prompt_tokens":9,"completion_tokens":4}}'
    documents = [
        duplicate_choices,
        (
            b'{"choices":[],"usage":{"prompt_tokens":1,"prompt_tokens":9,'
            b'"completion_tokens":4}}'
        ),
        b"\xef\xbb\xbf" + ordinary,
        ordinary.decode().encode("utf-16"),
        ordinary.decode().encode("utf-16-le"),
        (
            b'{"choices":[],"created":NaN,'
            b'"usage":{"prompt_tokens":9,"completion_tokens":4}}'
        ),
        (
            b'{"choices":[],"usage":{"prompt_tokens":9,'
            b'"completion_tokens":4,"total_tokens":Infinity}}'
        ),
    ]

    for document in documents:
        meter = UsageMeter(forward_usage_frame=False)
        frame = b"data: " + document + b"\n\n"
        assert meter.feed(frame) == frame
        assert meter.usage is None


def test_a_colonless_data_field_invalidates_an_earlier_subtotal() -> None:
    """SSE permits ``data`` without a colon; it is a data event with an empty value."""
    meter = UsageMeter(forward_usage_frame=False)
    meter.feed(
        b'data: {"choices":[{"delta":{"content":"partial"}}],'
        b'"usage":{"prompt_tokens":10,"completion_tokens":1}}\n\n'
    )

    assert meter.feed(b"data\n\n") == b"data\n\n"
    meter.finish(complete=True)

    assert meter.usage is None
    assert meter.unmetered_reason == "no_report"


def test_carriage_return_framing_is_understood() -> None:
    meter = UsageMeter(forward_usage_frame=False)
    body = b'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":2}}\r\n\r\n'

    assert meter.feed(body) == b""
    assert meter.usage is not None


@pytest.mark.parametrize("terminator", [b"\r\r", b"\n\r\n"])
def test_all_valid_sse_line_endings_are_understood_across_chunks(
    terminator: bytes,
) -> None:
    """CR, LF and CRLF may independently end either line in the blank boundary."""
    meter = UsageMeter(forward_usage_frame=False)
    content = b'data: {"choices":[{"delta":{"content":"hi"}}]}' + terminator
    usage = (
        b'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":3}}'
        + terminator
    )
    done = b"data: [DONE]" + terminator

    forwarded = bytearray()
    for byte in content + usage + done:
        forwarded.extend(meter.feed(bytes([byte])))
    forwarded.extend(meter.finish(complete=True))

    assert bytes(forwarded) == content + done
    assert meter.usage is not None
    assert (meter.usage.input_tokens, meter.usage.output_tokens) == (2, 3)


def test_a_truncated_final_frame_is_still_forwarded() -> None:
    # A stream cut off mid-frame must look to the client exactly like the byte relay did.
    meter = UsageMeter(forward_usage_frame=False)

    assert meter.feed(b'data: {"choices":[{"delta"') == b""
    assert meter.finish(complete=False) == b'data: {"choices":[{"delta"'
    assert meter.usage is None


def test_an_all_zero_usage_object_is_not_treated_as_a_report() -> None:
    # An early continuous-usage frame can legitimately be zero; latching onto it would
    # under-report the whole stream.
    meter = UsageMeter(forward_usage_frame=False)

    meter.feed(b'data: {"choices":[],"usage":{"prompt_tokens":0,"completion_tokens":0}}\n\n')
    assert meter.usage is None

    meter.feed(b'data: {"choices":[],"usage":{"prompt_tokens":9,"completion_tokens":4}}\n\n')
    assert meter.usage is not None
    assert (meter.usage.input_tokens, meter.usage.output_tokens) == (9, 4)


def test_the_last_report_wins() -> None:
    meter = UsageMeter(forward_usage_frame=False)

    meter.feed(b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":1}}\n\n')
    meter.feed(b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":8}}\n\n')

    assert meter.usage is not None
    assert meter.usage.output_tokens == 8


# --- the request the gateway sends upstream -------------------------------


def test_include_usage_is_requested_and_client_options_survive() -> None:
    upstream, client_asked = streaming_upstream_payload(
        {"model": "m", "stream": True, "stream_options": {"continuous_usage_stats": True}}
    )

    assert upstream["stream_options"] == {
        "continuous_usage_stats": True,
        "include_usage": True,
    }
    assert client_asked is False


def test_a_client_asking_for_usage_is_recorded_as_having_asked() -> None:
    _upstream, client_asked = streaming_upstream_payload(
        {"model": "m", "stream": True, "stream_options": {"include_usage": True}}
    )

    assert client_asked is True


@pytest.mark.parametrize("options", [None, "nonsense", 7, []])
def test_a_missing_or_invalid_stream_options_is_replaced(options: object) -> None:
    upstream, client_asked = streaming_upstream_payload(
        {"model": "m", "stream": True, "stream_options": options}
    )

    assert upstream["stream_options"] == {"include_usage": True}
    assert client_asked is False


# --- end to end -----------------------------------------------------------


async def test_a_streamed_request_records_the_tokens_it_used(
    client, plane, signing_key: SigningKey
) -> None:
    response = await _stream(client, signing_key.issue())
    assert response.status_code == 200, response.text

    records = plane.usage.drain()
    # Exactly one record for one request: no eager zero-token row beside the real one.
    assert len(records) == 1
    assert records[0].deployment_id == DEPLOYMENT_A
    assert (records[0].input_tokens, records[0].output_tokens) == (11, 7)
    assert records[0].streamed is True


async def test_the_gateway_asks_the_host_to_report_usage(
    client, signing_key: SigningKey, upstream: UpstreamStub
) -> None:
    await _stream(client, signing_key.issue())

    assert upstream.requests[0]["payload"]["stream_options"] == {"include_usage": True}


async def test_a_client_that_did_not_ask_does_not_receive_the_usage_frame(
    client, signing_key: SigningKey
) -> None:
    """The frame Fabric caused is consumed; the frames the client asked for are untouched.

    Note what is *not* claimed: asking for usage makes a compliant host attach a ``usage`` key
    to every ordinary chunk too, and that key does reach the client. Removing it would mean
    re-serialising every frame, which would silently rewrite fields this gateway does not
    model. Only the whole usage-only frame is removed.
    """
    response = await _stream(client, signing_key.issue())

    frames = [frame for frame in response.content.split(b"\n\n") if frame]
    assert frames == [
        b'data: {"choices":[{"delta":{"content":"hi"}}],"usage":null}',
        b"data: [DONE]",
    ]
    # No frame reports token counts, which is the event the client never requested.
    assert b"prompt_tokens" not in response.content


async def test_a_client_that_asked_for_usage_receives_the_frame_and_is_metered(
    client, plane, signing_key: SigningKey
) -> None:
    response = await _stream(
        client, signing_key.issue(), stream_options={"include_usage": True}
    )

    assert b'"usage"' in response.content
    frames = [
        frame for frame in response.content.split(b"\n\n") if frame.startswith(b"data: {")
    ]
    reported = json.loads(frames[-1][len(b"data: ") :])
    assert reported["usage"]["completion_tokens"] == 7

    records = plane.usage.drain()
    assert len(records) == 1
    assert (records[0].input_tokens, records[0].output_tokens) == (11, 7)


async def test_streamed_tokens_reach_the_metrics(
    client, plane, signing_key: SigningKey
) -> None:
    await _stream(client, signing_key.issue())

    rendered = plane.metrics.render()
    assert (
        f'fabric_dp_tokens_total{{deployment_id="{DEPLOYMENT_A}",direction="input"}} 11'
        in rendered
    )
    assert (
        f'fabric_dp_tokens_total{{deployment_id="{DEPLOYMENT_A}",direction="output"}} 7'
        in rendered
    )


async def test_a_host_that_reports_nothing_is_counted_rather_than_billed_at_zero(
    client, plane, signing_key: SigningKey, upstream: UpstreamStub
) -> None:
    """A zero-token row is indistinguishable from a real answer that cost nothing."""
    upstream.report_stream_usage = False

    # The deployment/account identity is published at zero during DataPlane construction, before
    # its first request can be the loss that changes the series.
    assert f"{_unmetered(DEPLOYMENT_A, ACCOUNT_A, 'no_report')} 0" in plane.metrics.render()

    response = await _stream(client, signing_key.issue())
    assert response.status_code == 200

    assert plane.usage.drain() == []
    assert f"{_unmetered(DEPLOYMENT_A, ACCOUNT_A, 'no_report')} 1" in plane.metrics.render()


async def test_an_unavailable_host_records_no_usage(
    client, plane, signing_key: SigningKey, upstream: UpstreamStub
) -> None:
    upstream.fail = True

    await _stream(client, signing_key.issue())

    assert plane.usage.drain() == []


async def test_completions_streams_are_metered_too(
    client, plane, signing_key: SigningKey
) -> None:
    response = await client.post(
        "/v1/completions",
        json={"model": "launch-model", "stream": True, "prompt": "hi"},
        headers={"Authorization": f"Bearer {signing_key.issue()}"},
    )
    assert response.status_code == 200, response.text

    records = plane.usage.drain()
    assert len(records) == 1
    assert (records[0].input_tokens, records[0].output_tokens) == (11, 7)



# --- regressions from the first behavioural review -------------------------


def test_a_content_frame_without_a_choices_key_is_never_deleted() -> None:
    """Suppression is a positive test, so an unmodelled shape degrades to passthrough.

    The first version asked "is `choices` falsy", which is true both for an empty array and for
    a frame that has no `choices` key at all. Any host whose content lives elsewhere had its
    answer deleted while its tokens were still billed.
    """
    meter = UsageMeter(forward_usage_frame=False)
    frame = (
        b'data: {"delta":{"content":"important"},'
        b'"usage":{"prompt_tokens":11,"completion_tokens":7}}\n\n'
    )

    assert meter.feed(frame) == frame
    meter.finish(complete=True)
    assert meter.usage is not None


def test_an_unrecognised_metadata_key_keeps_the_frame() -> None:
    # A key this module does not model may be carrying an answer.
    meter = UsageMeter(forward_usage_frame=False)
    frame = (
        b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1},'
        b'"output":[{"text":"hi"}]}\n\n'
    )

    assert meter.feed(frame) == frame


def test_known_metadata_around_a_usage_frame_is_still_suppressed() -> None:
    meter = UsageMeter(forward_usage_frame=False)
    frame = (
        b'data: {"id":"c1","object":"chat.completion.chunk","created":1,"model":"m",'
        b'"system_fingerprint":"fp","choices":[],'
        b'"usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n'
    )

    assert meter.feed(frame) == b""
    assert meter.usage is not None


def test_a_usage_only_frame_is_suppressed_even_when_its_counts_are_unusable() -> None:
    """Forwarding is decided by shape, so an unreadable frame the client never asked for
    is still consumed rather than leaking."""
    meter = UsageMeter(forward_usage_frame=False)

    assert meter.feed(b'data: {"choices":[],"usage":{"total_tokens":18}}\n\n') == b""
    assert meter.usage is None


def test_a_null_usage_key_on_a_content_frame_is_forwarded() -> None:
    # include_usage makes a compliant host put usage: null on every ordinary chunk.
    meter = UsageMeter(forward_usage_frame=False)
    frame = b'data: {"choices":[{"delta":{"content":"hi"}}],"usage":null}\n\n'

    assert meter.feed(frame) == frame
    assert meter.usage is None


def test_batched_data_lines_are_joined_as_the_protocol_specifies() -> None:
    # Two data: lines in one frame are one payload separated by a newline, not concatenated.
    meter = UsageMeter(forward_usage_frame=False)
    frame = b'data: {"choices":[],\ndata: "usage":{"prompt_tokens":2,"completion_tokens":3}}\n\n'

    meter.feed(frame)

    assert meter.usage is not None
    assert (meter.usage.input_tokens, meter.usage.output_tokens) == (2, 3)


def test_held_bytes_are_released_rather_than_buffered_without_limit() -> None:
    """A host that never sends a frame boundary must not be buffered indefinitely."""
    meter = UsageMeter(forward_usage_frame=False)
    blob = b"x" * (MAX_WITHHELD_BYTES + 1)

    forwarded = meter.feed(blob)

    assert meter.framing_lost is True
    # All of it reaches the client: what is not released now is released at the end.
    assert forwarded + meter.finish(complete=True) == blob


def test_framing_resumes_after_a_release() -> None:
    """Releasing held bytes loses one frame, not the rest of the stream.

    The client cannot tell a release from any other chunk boundary, so there is no reason to
    stop framing — and continuing means a terminal usage frame after a release is still read
    and still kept off a client that never asked for it.
    """
    meter = UsageMeter(forward_usage_frame=False)
    released = meter.feed(b"x" * (MAX_WITHHELD_BYTES + 1))
    assert meter.framing_lost is True

    forwarded = meter.feed(
        b'\n\ndata: {"choices":[],"usage":{"prompt_tokens":6,"completion_tokens":3}}\n\n'
    )

    # The resynchronising frame is the released frame's tail, so it is forwarded, and the usage
    # frame after it is suppressed again.
    assert released + forwarded == b"x" * (MAX_WITHHELD_BYTES + 1) + b"\n\n"
    assert meter.usage is not None
    assert (meter.usage.input_tokens, meter.usage.output_tokens) == (6, 3)


@pytest.mark.parametrize("terminator", [b"\n\n", b"\r\n\r\n"])
@pytest.mark.parametrize("overshoot", [1, 2, 3, 4, 5])
def test_a_release_never_merges_two_events(terminator: bytes, overshoot: int) -> None:
    """A released span must not end inside a frame terminator.

    Releasing a partial boundary erases it: the leftover bytes can no longer be recognised as
    the end of an event, so they are cut together with the frame that follows and the client
    receives two events merged into one — which a strict SSE client parses as a single
    malformed document, losing both. The window is only a few bytes wide, so it is swept.
    """
    meter = UsageMeter(forward_usage_frame=False)
    first = b"data: " + b"a" * (MAX_WITHHELD_BYTES + overshoot) + terminator
    second = b'data: {"choices":[{"delta":{"content":"second"}}]}' + terminator

    forwarded = bytearray()
    # Body and terminator in separate writes, which is what a host that flushes per event does.
    for piece in (first[: -len(terminator)], first[-len(terminator) :], second):
        forwarded.extend(meter.feed(piece))
    forwarded.extend(meter.finish(complete=True))

    assert bytes(forwarded) == first + second
    events = [event for event in bytes(forwarded).split(terminator) if event]
    assert len(events) == 2, "two events were delivered to the client as one"


def test_resynchronising_tail_is_forwarded_before_suppression_resumes() -> None:
    """Nothing may be dropped while resynchronising: it starts with bytes already sent on."""
    meter = UsageMeter(forward_usage_frame=False)
    released = meter.feed(b"data: " + b"a" * MAX_WITHHELD_BYTES)

    forwarded = meter.feed(
        b'\n\ndata: {"choices":[],"usage":{"prompt_tokens":6,"completion_tokens":3}}\n\n'
    )

    # The released frame's retained tail is forwarded with its boundary. The usage frame after
    # that is a separate frame, so ordinary suppression resumes without deleting client bytes.
    assert released + forwarded == b"data: " + b"a" * MAX_WITHHELD_BYTES + b"\n\n"
    assert b"prompt_tokens" not in forwarded
    assert meter.usage is not None


def test_resynchronising_suffix_cannot_fabricate_billable_usage() -> None:
    """A recovered frame has no trustworthy prefix and must not mutate usage state."""
    meter = UsageMeter(forward_usage_frame=False)
    first = b"x" * (MAX_WITHHELD_BYTES - 2) + b"dat"
    rest = (
        b'a: {"choices":[],"usage":{"prompt_tokens":900,"completion_tokens":700}}\n\n'
    )

    forwarded = meter.feed(first) + meter.feed(rest) + meter.finish(complete=True)

    assert forwarded == first + rest
    assert meter.framing_lost is True
    assert meter.usage is None
    assert meter.unmetered_reason == "framing_lost"


def test_a_newline_framed_host_streams_in_bounded_pieces() -> None:
    # Not blank-line framed, so it can never be metered. It must still stream rather than
    # being held until the response ends.
    meter = UsageMeter(forward_usage_frame=False)
    line = b'data: {"choices":[{"delta":{"content":"a"}}]}\n'

    forwarded = bytearray()
    for _ in range((MAX_WITHHELD_BYTES // len(line)) + 2):
        forwarded.extend(meter.feed(line))

    assert meter.framing_lost is True
    assert forwarded, "a newline-framed host received nothing until the end of its response"
    assert len(forwarded) <= MAX_WITHHELD_BYTES + 3 * len(line)


def test_a_small_newline_framed_response_arrives_when_the_stream_ends() -> None:
    """The documented cost of framing: below the ceiling, a non-SSE host does not stream.

    Every OpenAI-compatible host separates events with a blank line, and a host that does not
    cannot be framed incrementally at all — the terminator is the only thing that says where one
    event stops. So a small response from such a host is delivered in one piece at the end. It is
    delivered byte for byte, which is the property that matters; ADR 0014 records the rest.
    """
    meter = UsageMeter(forward_usage_frame=False)
    body = b'data: {"choices":[{"delta":{"content":"a"}}]}\n' * 90

    assert meter.feed(body) == b""
    assert meter.finish(complete=True) == body
    assert meter.framing_lost is False
    # Several documents joined by newlines are not one document, so nothing is metered.
    assert meter.usage is None
    assert meter.unmetered_reason == "no_report"


def test_a_frame_that_arrives_whole_is_parsed_however_large_it_is() -> None:
    """The ceiling bounds what is withheld, not how big a frame may be.

    A frame delivered complete inside one chunk was never held back, so there is nothing to
    release and no reason to refuse to read it.
    """
    meter = UsageMeter(forward_usage_frame=False)
    padding = "a" * (MAX_WITHHELD_BYTES * 2)
    frame = (
        b'data: {"choices":[{"delta":{"content":"' + padding.encode() + b'"}}],'
        b'"usage":{"prompt_tokens":8,"completion_tokens":9}}\n\n'
    )

    assert meter.feed(frame) == frame
    assert meter.framing_lost is False
    assert meter.usage is None  # a subtotal, until the stream is seen to end
    assert meter.finish(complete=True) == b""
    assert meter.usage is not None


def test_a_frame_too_large_to_parse_is_forwarded_whole() -> None:
    """Parsing is bounded too, so one pathological frame cannot be a JSON-decode bomb."""
    meter = UsageMeter(forward_usage_frame=False)
    padding = b"a" * (MAX_PARSED_FRAME_BYTES + 1)
    frame = b'data: {"choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1},"p":"'
    frame += padding + b'"}\n\n'

    assert meter.feed(frame) == frame
    assert meter.usage is None


def test_a_subtotal_is_not_a_total_until_the_stream_ends() -> None:
    """continuous_usage_stats reports a running count, and a cut stream's is not the total."""
    subtotal = (
        b'data: {"choices":[{"delta":{"content":"a"}}],'
        b'"usage":{"prompt_tokens":5,"completion_tokens":5}}\n\n'
    )

    cut = UsageMeter(forward_usage_frame=False)
    cut.feed(subtotal)
    assert cut.usage is None
    assert cut.unmetered_reason == "incomplete_stream"

    whole = UsageMeter(forward_usage_frame=False)
    whole.feed(subtotal)
    whole.finish(complete=True)
    assert whole.usage is not None
    assert (whole.usage.input_tokens, whole.usage.output_tokens) == (5, 5)
    assert whole.unmetered_reason is None


def test_a_subtotal_read_before_a_release_is_never_a_total() -> None:
    """If a frame went by unread, the last count that was read may not be the last one sent."""
    meter = UsageMeter(forward_usage_frame=False)
    meter.feed(
        b'data: {"choices":[{"delta":{"content":"a"}}],'
        b'"usage":{"prompt_tokens":5,"completion_tokens":5}}\n\n'
    )
    meter.feed(b"z" * (MAX_WITHHELD_BYTES + 1))
    meter.finish(complete=True)

    assert meter.usage is None
    assert meter.unmetered_reason == "framing_lost"


def test_a_frame_too_large_to_parse_also_forfeits_a_subtotal() -> None:
    """An unread frame is an unread frame, whichever limit refused it."""
    meter = UsageMeter(forward_usage_frame=False)
    meter.feed(
        b'data: {"choices":[{"delta":{"content":"a"}}],'
        b'"usage":{"prompt_tokens":5,"completion_tokens":5}}\n\n'
    )
    meter.feed(b'data: {"p":"' + b"a" * (MAX_PARSED_FRAME_BYTES + 1) + b'"}\n\n')
    meter.finish(complete=True)

    assert meter.framing_lost is True
    assert meter.usage is None
    assert meter.unmetered_reason == "framing_lost"


def test_a_completed_body_stays_completed_when_closing_it_fails() -> None:
    """``finish`` latches: the relay calls it again if the error comes from closing the response.

    A body that was read to its end does not stop having been read to its end, and treating it
    as cut short would refuse a total this stream genuinely reported.
    """
    meter = UsageMeter(forward_usage_frame=False)
    meter.feed(
        b'data: {"choices":[{"delta":{"content":"a"}}],'
        b'"usage":{"prompt_tokens":5,"completion_tokens":5}}\n\n'
    )

    meter.finish(complete=True)
    meter.finish(complete=False)

    assert meter.usage is not None
    assert meter.unmetered_reason is None


def test_a_terminal_report_is_a_total_even_if_the_client_hangs_up() -> None:
    """The usage-only frame is the end of the stream, so it needs no further confirmation."""
    meter = UsageMeter(forward_usage_frame=False)

    meter.feed(b'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":7}}\n\n')

    # finish() is never reached when a client disconnects, and this count still stands.
    assert meter.usage is not None
    assert meter.unmetered_reason is None


def test_a_stream_ending_without_a_terminator_still_hides_the_usage_frame() -> None:
    # A host that omits the final blank line must not hand over the frame Fabric caused.
    meter = UsageMeter(forward_usage_frame=False)

    tail = b'data: {"choices":[],"usage":{"prompt_tokens":4,"completion_tokens":2}}'
    assert meter.feed(tail) == b""
    assert meter.finish(complete=True) == b""
    assert meter.usage is not None


def test_an_unparseable_tail_is_still_forwarded() -> None:
    meter = UsageMeter(forward_usage_frame=False)

    assert meter.feed(b'data: {"choices":[{"delta"') == b""
    assert meter.finish(complete=False) == b'data: {"choices":[{"delta"'


@pytest.mark.parametrize(
    "completion",
    [-3, 0.4, "7", True, [7], None],
    ids=["negative", "fractional", "string", "bool", "list", "null"],
)
def test_one_unusable_count_voids_the_whole_report(completion: object) -> None:
    """A confidently wrong invoice is worse than a visibly unmetered stream.

    Zeroing the count that could not be read would bill the half of the report that happened
    to parse, which is exactly how a broken token counter would present.
    """
    meter = UsageMeter(forward_usage_frame=False)
    frame = (
        b'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":'
        + json.dumps(completion).encode()
        + b"}}\n\n"
    )

    # Still consumed — the shape is the frame Fabric asked for, whatever its numbers say.
    assert meter.feed(frame) == b""
    assert meter.usage is None
    assert meter.finish(complete=True) == b""
    assert meter.unmetered_reason == "no_report"


def test_an_invalid_terminal_report_voids_an_earlier_subtotal() -> None:
    """A stale cumulative count must not become the bill after a broken final report."""
    meter = UsageMeter(forward_usage_frame=False)
    meter.feed(
        b'data: {"choices":[{"delta":{"content":"partial"}}],'
        b'"usage":{"prompt_tokens":100,"completion_tokens":2}}\n\n'
    )
    meter.feed(
        b'data: {"choices":[],"usage":{"prompt_tokens":100,"completion_tokens":-3}}\n\n'
    )

    meter.finish(complete=True)

    assert meter.usage is None
    assert meter.unmetered_reason == "no_report"


def test_a_strictly_rejected_later_report_invalidates_an_earlier_subtotal() -> None:
    """Unread terminal data proves the prior cumulative count was not necessarily final."""
    subtotal = (
        b'data: {"choices":[{"delta":{"content":"partial"}}],'
        b'"usage":{"prompt_tokens":10,"completion_tokens":1}}\n\n'
    )
    ambiguous = [
        (
            b'{"choices":[],"usage":{"prompt_tokens":10,'
            b'"completion_tokens":1,"completion_tokens":9}}'
        ),
        b"\xef\xbb\xbf"
        b'{"choices":[],"usage":{"prompt_tokens":10,"completion_tokens":9}}',
        b'{"choices":[],"created":NaN,'
        b'"usage":{"prompt_tokens":10,"completion_tokens":9}}',
        b"[" * 20_000 + b"0" + b"]" * 20_000,
    ]

    for document in ambiguous:
        meter = UsageMeter(forward_usage_frame=False)
        meter.feed(subtotal)
        frame = b"data: " + document + b"\n\n"
        assert meter.feed(frame) == frame
        meter.finish(complete=True)
        assert meter.usage is None
        assert meter.unmetered_reason == "no_report"


@pytest.mark.parametrize(
    "count",
    [b"1e5000", b"1e999999999", b"1e999999999999999999999"],
)
def test_compact_exponents_cannot_trigger_unbounded_token_conversion(count: bytes) -> None:
    """A tiny candidate document cannot make integer work scale with its represented number."""
    meter = UsageMeter(forward_usage_frame=False)
    frame = (
        b'data: {"choices":[],"usage":{"prompt_tokens":'
        + count
        + b',"completion_tokens":4}}\n\n'
    )

    assert meter.feed(frame) == frame
    assert meter.usage is None
    meter.finish(complete=True)
    assert meter.unmetered_reason == "no_report"


def test_a_missing_count_reads_as_zero_rather_than_voiding_the_report() -> None:
    meter = UsageMeter(forward_usage_frame=False)

    meter.feed(b'data: {"choices":[],"usage":{"prompt_tokens":11}}\n\n')

    assert meter.usage is not None
    assert (meter.usage.input_tokens, meter.usage.output_tokens) == (11, 0)


def test_a_metadata_frame_with_null_usage_is_forwarded() -> None:
    # Suppression needs a usable usage object, not merely the key.
    meter = UsageMeter(forward_usage_frame=False)
    frame = b'data: {"choices":[],"usage":null}\n\n'

    assert meter.feed(frame) == frame


def test_reset_forgets_a_failed_attempts_frame_and_usage() -> None:
    """A retry is a different upstream stream; nothing may bleed across."""
    meter = UsageMeter(forward_usage_frame=False)
    meter.feed(b'data: {"choices":[],"usage":{"prompt_tokens":9,"completion_tokens":9}}\n\n')
    meter.feed(b'data: {"choices":[{"delta":{"content":"par')

    meter.reset()

    assert meter.usage is None
    assert meter.finish(complete=False) == b""
    frame = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
    assert meter.feed(frame) == frame


def _plane_with(control_plane, handler, *backends: tuple[str, str]):
    """A data plane whose pool is exactly these backends, served by ``handler``."""
    import httpx

    from fabric_data_plane.app import DataPlane
    from fabric_data_plane.keys import KeyCache
    from fabric_data_plane.pool import Backend
    from fabric_data_plane.registry import Deployment, DeploymentRegistry
    from tests.conftest import make_settings

    registry = DeploymentRegistry(
        [
            Deployment(
                deployment_id=DEPLOYMENT_A,
                account_id=ACCOUNT_A,
                model_alias="launch-model",
                upstream_url=backends[0][1],
                upstream_model="internal-release-1",
                backends=tuple(
                    Backend(backend_id=name, url=url) for name, url in backends
                ),
            )
        ]
    )
    settings = make_settings()
    return DataPlane(
        settings=settings,
        keys=KeyCache(settings, client=control_plane.client()),
        registry=registry,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def test_a_retried_stream_is_metered_from_the_attempt_that_served_it(
    control_plane, signing_key: SigningKey
) -> None:
    """A retry must neither splice a dead attempt's bytes nor bill its tokens."""
    import httpx

    from fabric_data_plane.app import create_inference_app

    dead, live = "http://dead.test", "http://live.test"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(dead):
            raise httpx.ConnectError("refused", request=request)
        body = (
            b'data: {"choices":[{"delta":{"content":"hi"}}],"usage":null}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":21,"completion_tokens":13}}\n\n'
            b"data: [DONE]\n\n"
        )
        return httpx.Response(200, content=body)

    plane = _plane_with(control_plane, handler, ("dead", dead), ("live", live))
    transport = httpx.ASGITransport(app=create_inference_app(plane))
    async with httpx.AsyncClient(transport=transport, base_url="http://dp.test") as client:
        response = await _stream(client, signing_key.issue())

    # Every forwarded frame parses, so no dead attempt's bytes were spliced in.
    for frame in response.content.split(b"\n\n"):
        if frame.startswith(b"data: {"):
            json.loads(frame[len(b"data: ") :])

    records = plane.usage.drain()
    assert len(records) == 1
    assert (records[0].input_tokens, records[0].output_tokens) == (21, 13)


async def test_a_stream_cut_short_by_the_client_is_counted_rather_than_part_billed(
    control_plane, signing_key: SigningKey
) -> None:
    """A running subtotal from a hung-up stream is not that stream's bill.

    With ``continuous_usage_stats`` every frame carries a cumulative count, so the last count
    seen before a disconnect is whatever the model had produced by then — here 2 of 999 output
    tokens. Recording it would under-bill by three orders of magnitude and look like a complete
    measurement while doing it.
    """
    import httpx

    from fabric_data_plane.app import create_inference_app

    async def body():
        for completion in range(1, 6):
            yield (
                b'data: {"choices":[{"delta":{"content":"a"}}],'
                b'"usage":{"prompt_tokens":100,"completion_tokens":'
                + str(completion).encode()
                + b"}}\n\n"
            )
        yield b'data: {"choices":[],"usage":{"prompt_tokens":100,"completion_tokens":999}}\n\n'
        yield b"data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body())

    plane = _plane_with(control_plane, handler, ("only", "http://live.test"))
    app = create_inference_app(plane)
    request_body = json.dumps(
        {
            "model": "launch-model",
            "stream": True,
            "stream_options": {"continuous_usage_stats": True},
        }
    ).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": CHAT,
        "raw_path": CHAT.encode(),
        "query_string": b"",
        "headers": [
            (b"authorization", f"Bearer {signing_key.issue()}".encode()),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(request_body)).encode()),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("ingress.test", 80),
    }
    sent = False

    async def receive():
        nonlocal sent
        if not sent:
            sent = True
            return {"type": "http.request", "body": request_body, "more_body": False}
        return {"type": "http.disconnect"}

    delivered = 0

    async def send(message):
        nonlocal delivered
        if message["type"] != "http.response.body":
            return
        delivered += 1
        if delivered == 2:
            raise RuntimeError("client hung up mid-stream")

    with pytest.raises(BaseExceptionGroup):
        await app(scope, receive, send)
    assert delivered == 2, "the client hung up before any frame was forwarded"

    assert plane.usage.drain() == [], "a subtotal was billed as though it were the total"
    rendered = plane.metrics.render()
    assert f"{_unmetered(DEPLOYMENT_A, ACCOUNT_A, 'incomplete_stream')} 1" in rendered
    # And the leases are still released, as they were before metering existed.
    assert plane.concurrency.snapshot()["in_flight"] == 0


async def test_a_completed_continuous_usage_stream_is_billed_from_its_last_count(
    control_plane, signing_key: SigningKey
) -> None:
    """The other half of the rule: reaching the end turns the last subtotal into the total.

    This host reports cumulatively and never sends a usage-only frame, so the only count
    available is the one on the final content frame.
    """
    import httpx

    from fabric_data_plane.app import create_inference_app

    def handler(request: httpx.Request) -> httpx.Response:
        frames = [
            b'data: {"choices":[{"delta":{"content":"a"}}],'
            b'"usage":{"prompt_tokens":100,"completion_tokens":' + str(n).encode() + b"}}\n\n"
            for n in (1, 2, 3)
        ]
        return httpx.Response(200, content=b"".join(frames) + b"data: [DONE]\n\n")

    plane = _plane_with(control_plane, handler, ("only", "http://live.test"))
    transport = httpx.ASGITransport(app=create_inference_app(plane))
    async with httpx.AsyncClient(transport=transport, base_url="http://dp.test") as client:
        response = await _stream(
            client, signing_key.issue(), stream_options={"continuous_usage_stats": True}
        )
    assert response.status_code == 200

    records = plane.usage.drain()
    assert len(records) == 1
    assert (records[0].input_tokens, records[0].output_tokens) == (100, 3)
    # Every frame carried usage the client asked for, and every one was forwarded.
    assert response.content.count(b'"usage"') == 3


async def test_release_and_resume_is_byte_exact_through_the_gateway(
    control_plane, signing_key: SigningKey
) -> None:
    """A release inside a separately-written terminator must not merge two SSE events."""
    import httpx

    from fabric_data_plane.app import create_inference_app

    terminator = b"\r\n\r\n"
    first = b"data: " + b"a" * (MAX_WITHHELD_BYTES + 2) + terminator
    second = b'data: {"choices":[{"delta":{"content":"after"}}]}' + terminator
    usage = (
        b'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":7}}'
        + terminator
    )
    done = b"data: [DONE]" + terminator

    async def body():
        # Deliberately split the boundary from its frame, as a flushing host may do.
        yield first[: -len(terminator)]
        yield first[-len(terminator) :]
        yield second
        yield usage
        yield done

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body())

    plane = _plane_with(control_plane, handler, ("only", "http://live.test"))
    transport = httpx.ASGITransport(app=create_inference_app(plane))
    async with httpx.AsyncClient(transport=transport, base_url="http://dp.test") as client:
        response = await _stream(client, signing_key.issue())

    # Only Fabric's usage frame is absent. In particular, neither boundary around the release
    # was swallowed, so a strict SSE client still sees the two content events separately.
    assert response.content == first + second + done
    assert len([event for event in response.content.split(terminator) if event]) == 3
    records = plane.usage.drain()
    assert len(records) == 1
    assert (records[0].input_tokens, records[0].output_tokens) == (11, 7)


async def test_a_host_drop_mid_body_is_counted_rather_than_part_billed(
    control_plane, signing_key: SigningKey
) -> None:
    """A host-side transport failure leaves a subtotal, not the stream's final bill."""
    import httpx

    from fabric_data_plane.app import create_inference_app

    async def body():
        yield (
            b'data: {"choices":[{"delta":{"content":"partial"}}],'
            b'"usage":{"prompt_tokens":100,"completion_tokens":2}}\n\n'
        )
        raise httpx.ReadError("model host dropped the body")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body())

    plane = _plane_with(control_plane, handler, ("only", "http://live.test"))
    transport = httpx.ASGITransport(app=create_inference_app(plane))
    async with httpx.AsyncClient(transport=transport, base_url="http://dp.test") as client:
        response = await _stream(client, signing_key.issue())

    assert b'"content":"partial"' in response.content
    assert b'"code":"upstream_unavailable"' in response.content
    assert plane.usage.drain() == []
    assert (
        f"{_unmetered(DEPLOYMENT_A, ACCOUNT_A, 'incomplete_stream')} 1"
        in plane.metrics.render()
    )


async def test_a_gateway_framing_loss_is_attributed_to_the_gateway(
    control_plane, signing_key: SigningKey
) -> None:
    """A frame released at the bound is not mislabeled as a host reporting nothing."""
    import httpx

    from fabric_data_plane.app import create_inference_app

    body_bytes = b"data: " + b"a" * (MAX_WITHHELD_BYTES + 2) + b"\n\ndata: [DONE]\n\n"

    async def body():
        yield body_bytes[: MAX_WITHHELD_BYTES + 8]
        yield body_bytes[MAX_WITHHELD_BYTES + 8 :]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body())

    plane = _plane_with(control_plane, handler, ("only", "http://live.test"))
    transport = httpx.ASGITransport(app=create_inference_app(plane))
    async with httpx.AsyncClient(transport=transport, base_url="http://dp.test") as client:
        response = await _stream(client, signing_key.issue())

    assert response.content == body_bytes
    assert plane.usage.drain() == []
    rendered = plane.metrics.render()
    assert f"{_unmetered(DEPLOYMENT_A, ACCOUNT_A, 'framing_lost')} 1" in rendered
    assert f"{_unmetered(DEPLOYMENT_A, ACCOUNT_A, 'no_report')} 0" in rendered


async def test_bare_cr_sse_is_metered_and_suppressed_end_to_end(
    control_plane, signing_key: SigningKey
) -> None:
    """Bare CR is a standards-valid SSE line ending, including across host chunks."""
    import httpx

    from fabric_data_plane.app import create_inference_app

    content = b'data: {"choices":[{"delta":{"content":"hi"}}]}\r\r'
    usage = b'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":7}}\r\r'
    done = b"data: [DONE]\r\r"

    async def body():
        for byte in content + usage + done:
            yield bytes([byte])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body())

    plane = _plane_with(control_plane, handler, ("only", "http://live.test"))
    transport = httpx.ASGITransport(app=create_inference_app(plane))
    async with httpx.AsyncClient(transport=transport, base_url="http://dp.test") as client:
        response = await _stream(client, signing_key.issue())

    assert response.status_code == 200
    assert response.content == content + done
    records = plane.usage.drain()
    assert len(records) == 1
    assert (records[0].input_tokens, records[0].output_tokens) == (11, 7)


async def test_deep_json_remains_a_successful_byte_exact_relay(
    control_plane, signing_key: SigningKey
) -> None:
    """Decoder recursion in one opaque event must not abort the accepted upstream stream."""
    import httpx

    from fabric_data_plane.app import create_inference_app

    deep = b"data: " + b"[" * 20_000 + b"0" + b"]" * 20_000 + b"\n\n"
    usage = b'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":7}}\n\n'
    done = b"data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=deep + usage + done)

    plane = _plane_with(control_plane, handler, ("only", "http://live.test"))
    transport = httpx.ASGITransport(app=create_inference_app(plane))
    async with httpx.AsyncClient(transport=transport, base_url="http://dp.test") as client:
        response = await _stream(client, signing_key.issue())

    assert response.status_code == 200
    assert response.content == deep + done
    records = plane.usage.drain()
    assert len(records) == 1
    assert (records[0].input_tokens, records[0].output_tokens) == (11, 7)


async def test_a_non_streaming_request_carries_no_stream_options(
    client, signing_key: SigningKey, upstream: UpstreamStub
) -> None:
    """vLLM rejects ``stream_options`` on a non-streaming request, so it must not be added."""
    await client.post(
        CHAT,
        json={"model": "launch-model", "messages": [{"role": "user", "content": "hi"}]},
        headers={"Authorization": f"Bearer {signing_key.issue()}"},
    )

    assert "stream_options" not in upstream.requests[0]["payload"]


def test_every_loss_reason_is_published_at_zero_before_traffic(plane) -> None:
    """Placement activation puts zero on the board before the first request can be a loss.

    ``increase()`` and ``rate()`` need an earlier sample to compare against, and an absent
    series is indistinguishable from a gateway that was never wired up.
    """
    rendered = plane.metrics.render()
    for reason in UNMETERED_REASONS:
        assert f"{_unmetered(DEPLOYMENT_A, ACCOUNT_A, reason)} 0" in rendered


def test_a_retired_placement_cannot_gain_an_event_from_unretained_cleanup(plane) -> None:
    """A cleanup not retained before withdrawal cannot change process-lifetime history."""
    deployment = str(DEPLOYMENT_A)
    account = str(ACCOUNT_A)
    plane.metrics.retire_deployment(deployment)

    plane.metrics.stream_finished(
        deployment=deployment,
        account=account,
        unmetered_reason="no_report",
    )

    rendered = plane.metrics.render()
    for reason in UNMETERED_REASONS:
        assert f"{_unmetered(deployment, account, reason)} 0" in rendered


def test_stream_admission_and_backend_drain_both_gate_retirement(plane) -> None:
    """Finishing the last stream cannot erase a concurrent ordinary attempt's metrics.

    The same stable id is then reintroduced before the old attempt drains, proving old-pool
    cleanup cannot retire the new placement's membership either.
    """
    from fabric_data_plane.app import _acquire_backend, _release_backend
    from fabric_data_plane.registry import DeploymentRegistry

    deployment = plane.registry.resolve("launch-model", account_id=ACCOUNT_A)
    deployment_label = str(deployment.deployment_id)
    pool = plane.pool_for(deployment)
    backend = pool.select()
    assert backend is not None

    plane.retain_stream(deployment)
    _acquire_backend(plane, pool, backend, deployment_label)

    plane.registry = DeploymentRegistry([])
    plane.reconcile_pools()
    plane.metrics.stream_finished(
        deployment=deployment_label,
        account=str(ACCOUNT_A),
        unmetered_reason="no_report",
    )
    plane.release_stream(deployment)

    # The ordinary attempt still owns a backend lease, so stream cleanup cannot retire it.
    rendered = plane.metrics.render()
    assert (
        f'fabric_dp_backend_in_flight{{deployment_id="{deployment_label}",'
        f'backend_id="{backend.backend_id}"}} 1' in rendered
    )

    # Reintroducing the stable id before old-pool drain must protect the new membership from the
    # retired pool's eventual cleanup.
    plane.registry = DeploymentRegistry([deployment])
    plane.reconcile_pools()
    plane.pool_for(deployment)
    _release_backend(plane, pool, backend, deployment_label, "ok")
    plane.router_state()

    rendered = plane.metrics.render()
    assert (
        f'fabric_dp_backend_requests_total{{deployment_id="{deployment_label}",'
        f'backend_id="{backend.backend_id}",outcome="ok"}} 1' in rendered
    )
    assert f"{_unmetered(deployment_label, ACCOUNT_A, 'no_report')} 1" in rendered


@pytest.mark.parametrize("first_to_drain", ["older", "newer"])
def test_every_retired_generation_is_tracked_until_it_drains(
    plane, first_to_drain: str
) -> None:
    """Repeated stable-id reuse cannot overwrite an older draining pool generation."""
    from fabric_data_plane.app import _acquire_backend, _release_backend
    from fabric_data_plane.registry import DeploymentRegistry

    deployment = plane.registry.resolve("launch-model", account_id=ACCOUNT_A)
    label = str(deployment.deployment_id)

    older_pool = plane.pool_for(deployment)
    older_backend = older_pool.select()
    assert older_backend is not None
    _acquire_backend(plane, older_pool, older_backend, label)
    plane.registry = DeploymentRegistry([])
    plane.reconcile_pools()

    plane.registry = DeploymentRegistry([deployment])
    plane.reconcile_pools()
    newer_pool = plane.pool_for(deployment)
    assert newer_pool is not older_pool
    newer_backend = newer_pool.select()
    assert newer_backend is not None
    _acquire_backend(plane, newer_pool, newer_backend, label)
    plane.registry = DeploymentRegistry([])
    plane.reconcile_pools()

    first = (
        (older_pool, older_backend)
        if first_to_drain == "older"
        else (newer_pool, newer_backend)
    )
    second = (
        (newer_pool, newer_backend)
        if first_to_drain == "older"
        else (older_pool, older_backend)
    )
    _release_backend(plane, first[0], first[1], label, "ok")

    # One generation remains visible after the other drains, regardless of age/order.
    state = plane.router_state()
    retired = [entry for entry in state["backends"] if entry.get("retired")]
    assert len(retired) == 1
    assert retired[0]["in_flight"] == 1
    rendered = plane.metrics.render()
    assert (
        f'fabric_dp_backend_in_flight{{deployment_id="{label}",'
        f'backend_id="{first[1].backend_id}"}} 1' in rendered
    )
    assert (
        f'fabric_dp_backend_requests_total{{deployment_id="{label}",'
        f'backend_id="{first[1].backend_id}",outcome="ok"}} 1' in rendered
    )

    _release_backend(plane, second[0], second[1], label, "ok")
    # The second outcome is recorded before final retirement removes current-backend series.
    assert (
        f'fabric_dp_backend_requests_total{{deployment_id="{label}",'
        f'backend_id="{second[1].backend_id}",outcome="ok"}} 2'
        in plane.metrics.render()
    )
    assert plane.router_state()["backends"] == []
    assert DEPLOYMENT_A not in plane._retired_pools


def test_idle_reintroduction_cannot_retire_an_older_active_generation(plane) -> None:
    """Retirement is stable-id wide, even when the newly withdrawn pool served nothing."""
    from fabric_data_plane.app import _acquire_backend, _release_backend
    from fabric_data_plane.registry import DeploymentRegistry

    deployment = plane.registry.resolve("launch-model", account_id=ACCOUNT_A)
    label = str(deployment.deployment_id)
    older_pool = plane.pool_for(deployment)
    backend = older_pool.select()
    assert backend is not None
    _acquire_backend(plane, older_pool, backend, label)

    plane.registry = DeploymentRegistry([])
    plane.reconcile_pools()
    plane.registry = DeploymentRegistry([deployment])
    plane.reconcile_pools()
    newer_pool = plane.pool_for(deployment)
    assert newer_pool is not older_pool

    # Generation B is idle when withdrawn; generation A still protects stable-id metrics.
    plane.registry = DeploymentRegistry([])
    plane.reconcile_pools()
    rendered = plane.metrics.render()
    assert (
        f'fabric_dp_backend_in_flight{{deployment_id="{label}",'
        f'backend_id="{backend.backend_id}"}} 1' in rendered
    )

    _release_backend(plane, older_pool, backend, label, "ok")
    assert (
        f'fabric_dp_backend_requests_total{{deployment_id="{label}",'
        f'backend_id="{backend.backend_id}",outcome="ok"}} 1'
        in plane.metrics.render()
    )
    assert plane.router_state()["backends"] == []


async def test_withdrawal_before_iterator_entry_keeps_an_admitted_loss_attributable(
    plane, signing_key: SigningKey, upstream: UpstreamStub
) -> None:
    """Admission, not backend iteration, holds the metric identity through cleanup."""
    from starlette.requests import Request

    from fabric_data_plane.app import _proxy
    from fabric_data_plane.registry import DeploymentRegistry

    upstream.report_stream_usage = False
    body = json.dumps({"model": "launch-model", "stream": True}).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": CHAT,
        "raw_path": CHAT.encode(),
        "query_string": b"",
        "headers": [
            (b"authorization", f"Bearer {signing_key.issue()}".encode()),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("ingress.test", 80),
    }
    received = False

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    # Construct the response but do not enter its body iterator. No backend lease exists yet.
    response = await _proxy(plane, Request(scope, receive), CHAT)

    # Withdraw the placement in exactly that interval. The admitted stream must keep its metric
    # membership even though pool/backend in-flight counts are still zero.
    plane.registry = DeploymentRegistry([])
    plane.reconcile_pools()
    assert f"{_unmetered(DEPLOYMENT_A, ACCOUNT_A, 'no_report')} 0" in plane.metrics.render()

    content = b"".join([chunk async for chunk in response.body_iterator])

    assert b'"content":"hi"' in content
    assert plane.usage.drain() == []
    assert f"{_unmetered(DEPLOYMENT_A, ACCOUNT_A, 'no_report')} 1" in plane.metrics.render()
    assert plane.concurrency.snapshot()["in_flight"] == 0


async def test_cleanup_cancellation_while_limiter_lock_is_contended_releases_once(
    plane, signing_key: SigningKey
) -> None:
    """Disconnect cancellation cannot strand concurrency or placement admission."""
    import asyncio

    from starlette.requests import Request

    from fabric_data_plane.app import _proxy
    from fabric_data_plane.limits import ConcurrencyLimiter

    plane.concurrency = ConcurrencyLimiter(1)
    body = json.dumps({"model": "launch-model", "stream": True}).encode()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": CHAT,
        "raw_path": CHAT.encode(),
        "query_string": b"",
        "headers": [
            (b"authorization", f"Bearer {signing_key.issue()}".encode()),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("ingress.test", 80),
    }
    received = False

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": body, "more_body": False}
        return {"type": "http.disconnect"}

    response = await _proxy(plane, Request(scope, receive), CHAT)
    await plane.concurrency._lock.acquire()

    async def consume() -> bytes:
        return b"".join([chunk async for chunk in response.body_iterator])

    consumer = asyncio.create_task(consume())
    # Cleanup accounts and releases placement synchronously, then blocks on the held limiter lock.
    for _ in range(100):
        if not plane._admitted_streams:
            break
        await asyncio.sleep(0)
    assert not plane._admitted_streams
    assert not consumer.done()

    consumer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await consumer
    assert plane.concurrency.snapshot()["in_flight"] == 1

    plane.concurrency._lock.release()
    # The response wrapper's outer-finally call awaits the same shielded release task.
    await response._cleanup()
    await response._cleanup()

    assert plane.concurrency.snapshot()["in_flight"] == 0
    assert not plane._admitted_streams
    records = plane.usage.drain()
    assert len(records) == 1, "cleanup retry duplicated or lost the usage record"


async def test_an_unreachable_host_is_not_counted_as_lost_usage(
    client, plane, signing_key: SigningKey, upstream: UpstreamStub
) -> None:
    """The counter is for a host that answered and lost the count, not for a failure.

    Failures are already visible as request outcomes, and no host began a completion here, so
    nothing was lost. Mixing them in makes the series impossible to alert on.
    """
    upstream.fail = True

    await _stream(client, signing_key.issue())

    rendered = plane.metrics.render()
    for reason in UNMETERED_REASONS:
        assert f"{_unmetered(DEPLOYMENT_A, ACCOUNT_A, reason)} 0" in rendered
    assert plane.usage.drain() == []


async def test_a_rejected_request_records_no_usage(
    client, plane, signing_key: SigningKey, upstream: UpstreamStub
) -> None:
    """Usage is trusted only from a stream the host accepted, as on the non-streaming path."""
    upstream.status = 400

    await _stream(client, signing_key.issue())

    assert plane.usage.drain() == []



async def test_a_rejected_stream_contributes_no_tokens_to_the_metrics(
    client, plane, signing_key: SigningKey, upstream: UpstreamStub
) -> None:
    """The usage gate applies to the metrics as well as to the billing row."""
    upstream.status = 400

    await _stream(client, signing_key.issue())

    rendered = plane.metrics.render()
    assert f'fabric_dp_tokens_total{{deployment_id="{DEPLOYMENT_A}"' not in rendered
    assert plane.usage.drain() == []
