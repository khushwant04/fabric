"""Audio transcription and translation ingress.

The audio endpoints are multipart rather than JSON, which is the only structural
difference from completions. Everything a completion guarantees still has to hold:
the caller's token decides ownership, the alias never leaks the internal release,
credentials are stripped before proxying, and completed work is metered.
"""

from __future__ import annotations

from tests.conftest import SigningKey, UpstreamStub

WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 24


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def upload(model: str = "launch-model", audio: bytes = WAV, **fields):
    data = {"model": model, **fields}
    return {"files": {"file": ("clip.wav", audio, "audio/wav")}, "data": data}


async def test_transcription_is_proxied_to_the_deployment(
    client, signing_key: SigningKey, upstream: UpstreamStub
) -> None:
    response = await client.post(
        "/v1/audio/transcriptions", **upload(), headers=auth(signing_key.issue())
    )
    assert response.status_code == 200, response.text

    body = response.json()
    assert body["text"] == "a spoken sentence"
    # The customer sees their alias, not the internal release name.
    assert body["model"] == "launch-model"

    assert len(upstream.requests) == 1
    forwarded = upstream.requests[0]
    assert forwarded["url"] == "http://model-host.test/v1/audio/transcriptions"
    # The upstream is addressed by its own model name.
    assert forwarded["model"] == "internal-release-1"
    # The audio itself arrives intact.
    assert WAV in forwarded["multipart"]
    assert "authorization" not in forwarded["headers"]


async def test_translations_share_the_same_path(
    client, signing_key: SigningKey, upstream: UpstreamStub
) -> None:
    response = await client.post(
        "/v1/audio/translations", **upload(), headers=auth(signing_key.issue())
    )
    assert response.status_code == 200, response.text
    assert upstream.requests[0]["url"] == "http://model-host.test/v1/audio/translations"


async def test_extra_fields_are_forwarded(
    client, signing_key: SigningKey, upstream: UpstreamStub
) -> None:
    response = await client.post(
        "/v1/audio/transcriptions",
        **upload(language="en"),
        headers=auth(signing_key.issue()),
    )
    assert response.status_code == 200, response.text
    assert upstream.requests[0]["language"] == "en"


async def test_missing_credentials_are_rejected(client, upstream: UpstreamStub) -> None:
    response = await client.post("/v1/audio/transcriptions", **upload())
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_credentials"
    # Nothing reached the GPU.
    assert upstream.requests == []


async def test_another_accounts_model_is_refused(
    client, signing_key: SigningKey, upstream: UpstreamStub
) -> None:
    """Ownership comes from the token, not from the model named in the form."""
    response = await client.post(
        "/v1/audio/transcriptions",
        **upload("other-account-model"),
        headers=auth(signing_key.issue()),
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "model_not_available"
    # Nothing reached the model host.
    assert upstream.requests == []


async def test_control_audience_token_is_refused(
    client, signing_key: SigningKey, upstream: UpstreamStub
) -> None:
    response = await client.post(
        "/v1/audio/transcriptions",
        **upload(),
        headers=auth(signing_key.issue(audience="fabric-control")),
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "wrong_audience"
    assert upstream.requests == []


async def test_model_is_required(client, signing_key: SigningKey) -> None:
    response = await client.post(
        "/v1/audio/transcriptions",
        files={"file": ("clip.wav", WAV, "audio/wav")},
        headers=auth(signing_key.issue()),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "model_required"


async def test_file_is_required(client, signing_key: SigningKey) -> None:
    response = await client.post(
        "/v1/audio/transcriptions",
        data={"model": "launch-model"},
        files={},
        headers=auth(signing_key.issue()),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "file_required"


async def test_empty_file_is_refused(client, signing_key: SigningKey) -> None:
    response = await client.post(
        "/v1/audio/transcriptions", **upload(audio=b""), headers=auth(signing_key.issue())
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "file_empty"


async def test_streaming_is_refused_rather_than_downgraded(
    client, signing_key: SigningKey, upstream: UpstreamStub
) -> None:
    """A caller who asked for a stream must not silently receive one whole object."""
    response = await client.post(
        "/v1/audio/transcriptions",
        **upload(stream="true"),
        headers=auth(signing_key.issue()),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "stream_unsupported"
    assert upstream.requests == []


async def test_text_response_format_is_passed_through(
    client, signing_key: SigningKey, upstream: UpstreamStub
) -> None:
    """A non-JSON reply is returned as-is rather than being forced into an object."""
    upstream.transcription_text_response = True
    response = await client.post(
        "/v1/audio/transcriptions",
        **upload(response_format="text"),
        headers=auth(signing_key.issue()),
    )
    assert response.status_code == 200, response.text
    assert response.text == "a spoken sentence"
    assert response.headers["content-type"].startswith("text/plain")


async def test_usage_is_metered(
    client, signing_key: SigningKey, plane, upstream: UpstreamStub
) -> None:
    await client.post(
        "/v1/audio/transcriptions", **upload(), headers=auth(signing_key.issue())
    )
    lease = plane.usage.lease(limit=10)
    assert [(r.input_tokens, r.output_tokens) for r in lease.records] == [(4, 5)]


async def test_upstream_failure_is_reported(
    client, signing_key: SigningKey, upstream: UpstreamStub
) -> None:
    upstream.fail = True
    response = await client.post(
        "/v1/audio/transcriptions", **upload(), headers=auth(signing_key.issue())
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "upstream_unavailable"



async def test_audio_at_the_configured_size_limit_is_accepted(
    client, plane, signing_key: SigningKey, upstream: UpstreamStub
) -> None:
    plane.settings.max_audio_upload_bytes = len(WAV)
    response = await client.post(
        "/v1/audio/transcriptions", **upload(), headers=auth(signing_key.issue())
    )
    assert response.status_code == 200, response.text
    assert len(upstream.requests) == 1


async def test_audio_over_the_configured_size_limit_is_refused_before_upstream(
    client, plane, signing_key: SigningKey, upstream: UpstreamStub
) -> None:
    plane.settings.max_audio_upload_bytes = len(WAV) - 1
    response = await client.post(
        "/v1/audio/transcriptions", **upload(), headers=auth(signing_key.issue())
    )
    assert response.status_code == 413
    assert response.json()["error"] == {
        "code": "audio_too_large",
        "message": "The supplied audio file exceeds the configured size limit",
        "details": {"max_bytes": len(WAV) - 1},
    }
    assert upstream.requests == []
