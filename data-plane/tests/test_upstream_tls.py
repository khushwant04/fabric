"""Mutual TLS to the model host.

A single-pod stamp reaches its host over localhost, where TLS adds nothing. Once the host
is a separate workload, possibly on another node, the connection crosses the cluster
network and both ends need to know who they are talking to.

These use real key material rather than mocks, because the failure modes worth catching
are configuration ones: a certificate without its key, an unpinned authority, a client
that presents nothing.
"""

from __future__ import annotations

import datetime as dt
import ipaddress

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from fabric_data_plane.app import create_admin_app, upstream_tls
from tests.conftest import make_settings


def _issue(common_name: str, *, issuer=None, issuer_key=None, ca: bool = False):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = dt.datetime.now(tz=dt.UTC)

    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer or subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
    )
    if not ca:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            ]),
            critical=False,
        )
    certificate = builder.sign(issuer_key or key, hashes.SHA256())
    return certificate, key


def _write(tmp_path, name: str, certificate, key):
    cert_path = tmp_path / f"{name}.crt"
    key_path = tmp_path / f"{name}.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    return str(cert_path), str(key_path)


def test_no_material_means_plain_http() -> None:
    """The default must not be half-configured TLS."""
    assert upstream_tls(make_settings()) == {}


def test_a_certificate_without_its_key_is_refused(tmp_path) -> None:
    """Discovering this as a connection error at request time would be worse."""
    certificate, key = _issue("data-plane")
    cert_path, _key_path = _write(tmp_path, "client", certificate, key)

    with pytest.raises(ValueError, match="without upstream_client_key"):
        upstream_tls(make_settings(upstream_client_cert=cert_path))


def test_a_key_without_its_certificate_is_refused(tmp_path) -> None:
    certificate, key = _issue("data-plane")
    _cert_path, key_path = _write(tmp_path, "client", certificate, key)

    with pytest.raises(ValueError, match="without upstream_client_cert"):
        upstream_tls(make_settings(upstream_client_key=key_path))


def test_client_material_and_authority_are_both_passed(tmp_path) -> None:
    ca_cert, ca_key = _issue("fabric-test-ca", ca=True)
    client_cert, client_key = _issue(
        "data-plane", issuer=ca_cert.subject, issuer_key=ca_key
    )
    cert_path, key_path = _write(tmp_path, "client", client_cert, client_key)
    ca_path, _ = _write(tmp_path, "ca", ca_cert, ca_key)

    options = upstream_tls(
        make_settings(
            upstream_client_cert=cert_path,
            upstream_client_key=key_path,
            upstream_ca_bundle=ca_path,
        )
    )

    assert options["cert"] == (cert_path, key_path)
    # Pinned to the cluster's own authority, which the system store does not know.
    assert options["verify"] == ca_path


async def test_a_client_with_this_material_reaches_a_tls_host(tmp_path) -> None:
    """The material has to work against a real TLS server, not just parse.

    Verifying against the issuing authority is the assertion: a client that trusted
    anything would pass this too, so an unrelated authority is checked as well.
    """
    ca_cert, ca_key = _issue("fabric-test-ca", ca=True)
    server_cert, server_key = _issue("localhost", issuer=ca_cert.subject, issuer_key=ca_key)
    ca_path, _ = _write(tmp_path, "ca", ca_cert, ca_key)
    _write(tmp_path, "server", server_cert, server_key)

    other_ca, other_key = _issue("unrelated-ca", ca=True)
    other_path, _ = _write(tmp_path, "other-ca", other_ca, other_key)

    import ssl
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, *_args) -> None:
            return

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(tmp_path / "server.crt"), str(tmp_path / "server.key"))

    server = HTTPServer(("127.0.0.1", 0), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    try:
        async with httpx.AsyncClient(**upstream_tls(
            make_settings(upstream_ca_bundle=ca_path)
        )) as trusting:
            response = await trusting.post(f"https://localhost:{port}/v1/chat/completions")
            assert response.status_code == 200

        async with httpx.AsyncClient(**upstream_tls(
            make_settings(upstream_ca_bundle=other_path)
        )) as distrusting:
            with pytest.raises(httpx.ConnectError):
                await distrusting.post(f"https://localhost:{port}/v1/chat/completions")
    finally:
        server.shutdown()


async def test_the_posture_is_reported_not_inferred(tmp_path, control_plane, upstream) -> None:
    """A stamp that believes it uses mTLS and does not must be distinguishable."""
    from fabric_data_plane.app import DataPlane
    from fabric_data_plane.keys import KeyCache
    from tests.conftest import make_registry

    ca_cert, ca_key = _issue("fabric-test-ca", ca=True)
    client_cert, client_key = _issue("data-plane", issuer=ca_cert.subject, issuer_key=ca_key)
    cert_path, key_path = _write(tmp_path, "client", client_cert, client_key)
    ca_path, _ = _write(tmp_path, "ca", ca_cert, ca_key)

    settings = make_settings(
        upstream_client_cert=cert_path,
        upstream_client_key=key_path,
        upstream_ca_bundle=ca_path,
    )
    plane = DataPlane(
        settings=settings,
        keys=KeyCache(settings, client=control_plane.client()),
        registry=make_registry(),
        client=upstream.client(),
    )

    transport = httpx.ASGITransport(app=create_admin_app(plane))
    async with httpx.AsyncClient(transport=transport, base_url="http://admin") as client:
        state = (await client.get("/admin/upstream")).json()

    assert state == {
        "client_certificate_configured": True,
        "authority_pinned": True,
        "mutual_tls": True,
    }
