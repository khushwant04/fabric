"""A local OpenID Connect provider that stands in for Auth0 in the console demo.

Demo tooling only. It signs in a fixed set of demo people without a password prompt, so it
must never be reachable from anywhere but this machine. It exists so the console's real
Auth0 SDK and the control plane's real token verifier can run end to end without an Auth0
tenant:

  console (@auth0/nextjs-auth0)  discovery, /authorize, /oauth/token (code + PKCE,
                                 refresh), /oidc/logout
  control plane                  /.well-known/jwks.json to verify the access token
  demo seeding                   /oauth/token with the password grant, to act as a person

It serves HTTPS with a certificate from a throwaway CA the caller generated, and generates
its own RS256 signing key in memory on every start, so nothing it trusts outlives the run.

Usage:
  python mock_oidc.py --port 8443 --cert server.pem --key server-key.pem \
      --client-id ID --client-secret SECRET --audience https://api.fabric.demo/control
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import json
import secrets
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

#: The people the demo knows. The first one is who the browser signs in as.
USERS = [
    {"sub": "auth0|demo-maya-chen", "email": "maya.chen@northstar-labs.example",
     "name": "Maya Chen", "nickname": "maya"},
    {"sub": "auth0|demo-priya-raman", "email": "priya.raman@northstar-labs.example",
     "name": "Priya Raman", "nickname": "priya"},
    {"sub": "auth0|demo-daniel-okafor", "email": "daniel.okafor@northstar-labs.example",
     "name": "Daniel Okafor", "nickname": "daniel"},
    {"sub": "auth0|demo-sofia-lindqvist", "email": "sofia.lindqvist@northstar-labs.example",
     "name": "Sofia Lindqvist", "nickname": "sofia"},
    {"sub": "auth0|demo-tom-becker", "email": "tom.becker@northstar-labs.example",
     "name": "Tom Becker", "nickname": "tom"},
]
DEMO_PASSWORD = "fabric-demo"
ACCESS_TOKEN_TTL = 24 * 3600


def _b64url_uint(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class Provider:
    def __init__(self, issuer: str, browser_base: str, client_id: str, client_secret: str,
                 audience: str) -> None:
        self.issuer = issuer
        self.browser_base = browser_base.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.audience = audience
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = secrets.token_hex(8)
        self.codes: dict[str, dict] = {}
        self.refresh_tokens: dict[str, dict] = {}
        self.lock = threading.Lock()

    def jwks(self) -> dict:
        numbers = self.key.public_key().public_numbers()
        return {"keys": [{
            "kty": "RSA", "use": "sig", "alg": "RS256", "kid": self.kid,
            "n": _b64url_uint(numbers.n), "e": _b64url_uint(numbers.e),
        }]}

    def discovery(self) -> dict:
        base = self.issuer.rstrip("/")
        return {
            "issuer": self.issuer,
            # Browser-facing endpoints live on a plain-HTTP localhost listener, so a demo
            # browser needs neither the throwaway CA nor a hosts-file entry.
            "authorization_endpoint": f"{self.browser_base}/authorize",
            "token_endpoint": f"{base}/oauth/token",
            "userinfo_endpoint": f"{base}/userinfo",
            "jwks_uri": f"{base}/.well-known/jwks.json",
            "end_session_endpoint": f"{self.browser_base}/oidc/logout",
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post"],
            "code_challenge_methods_supported": ["S256"],
            "grant_types_supported": ["authorization_code", "refresh_token", "password"],
            "scopes_supported": ["openid", "profile", "email", "offline_access"],
            "claims_supported": ["sub", "email", "email_verified", "name", "nickname"],
        }

    def _sign(self, claims: dict) -> str:
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": self.kid})

    def tokens(self, user: dict, *, scope: str, audience: str, nonce: str | None) -> dict:
        now = int(time.time())
        access = self._sign({
            "iss": self.issuer, "sub": user["sub"],
            "aud": [audience, f"{self.issuer.rstrip('/')}/userinfo"],
            "iat": now, "exp": now + ACCESS_TOKEN_TTL, "scope": scope,
            "azp": self.client_id, "email": user["email"], "name": user["name"],
        })
        id_claims = {
            "iss": self.issuer, "sub": user["sub"], "aud": self.client_id,
            "iat": now, "exp": now + ACCESS_TOKEN_TTL, "auth_time": now,
            "email": user["email"], "email_verified": True,
            "name": user["name"], "nickname": user["nickname"],
            "sid": secrets.token_hex(8),
        }
        if nonce:
            id_claims["nonce"] = nonce
        body = {
            "access_token": access, "id_token": self._sign(id_claims),
            "token_type": "Bearer", "expires_in": ACCESS_TOKEN_TTL, "scope": scope,
        }
        if "offline_access" in scope.split():
            refresh = secrets.token_urlsafe(32)
            with self.lock:
                self.refresh_tokens[refresh] = {"user": user, "scope": scope, "audience": audience}
            body["refresh_token"] = refresh
        return body


def make_handler(provider: Provider):
    class Handler(BaseHTTPRequestHandler):
        server_version = "fabric-demo-oidc"

        def log_message(self, fmt: str, *args) -> None:  # quiet by default
            pass

        def _json(self, status: int, body: dict) -> None:
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _redirect(self, location: str) -> None:
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def _error(self, status: int, code: str, description: str) -> None:
            self._json(status, {"error": code, "error_description": description})

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            if url.path == "/.well-known/openid-configuration":
                return self._json(200, provider.discovery())
            if url.path == "/.well-known/jwks.json":
                return self._json(200, provider.jwks())
            if url.path == "/authorize":
                return self._authorize(query)
            if url.path == "/userinfo":
                return self._userinfo()
            if url.path in ("/oidc/logout", "/v2/logout"):
                target = query.get("post_logout_redirect_uri") or query.get("returnTo")
                if target:
                    return self._redirect(target)
                raw = b"<p>Signed out of the Fabric demo identity provider.</p>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return None
            if url.path == "/healthz":
                return self._json(200, {"status": "ok"})
            return self._error(404, "not_found", "unknown path")

        def _authorize(self, query: dict) -> None:
            if query.get("client_id") != provider.client_id:
                return self._error(400, "invalid_client", "unknown client_id")
            if query.get("response_type") != "code":
                return self._error(400, "unsupported_response_type", "only code is supported")
            redirect_uri = query.get("redirect_uri")
            if not redirect_uri:
                return self._error(400, "invalid_request", "redirect_uri is required")
            # Auto-approve: the demo has one browser user and no password prompt.
            code = secrets.token_urlsafe(24)
            with provider.lock:
                provider.codes[code] = {
                    "user": USERS[0],
                    "redirect_uri": redirect_uri,
                    "nonce": query.get("nonce"),
                    "scope": query.get("scope", "openid"),
                    "audience": query.get("audience") or provider.audience,
                    "code_challenge": query.get("code_challenge"),
                    "expires": time.time() + 120,
                }
            params = {"code": code}
            if "state" in query:
                params["state"] = query["state"]
            separator = "&" if "?" in redirect_uri else "?"
            self._redirect(f"{redirect_uri}{separator}{urlencode(params)}")

        def _userinfo(self) -> None:
            header = self.headers.get("Authorization", "")
            token = header.removeprefix("Bearer ").strip()
            try:
                claims = jwt.decode(
                    token, provider.key.public_key(), algorithms=["RS256"],
                    audience=f"{provider.issuer.rstrip('/')}/userinfo", issuer=provider.issuer,
                )
            except jwt.PyJWTError:
                return self._error(401, "invalid_token", "access token is not valid")
            user = next((u for u in USERS if u["sub"] == claims["sub"]), None)
            if user is None:
                return self._error(401, "invalid_token", "unknown subject")
            self._json(200, {**user, "email_verified": True})

        def _client_ok(self, form: dict) -> bool:
            client_id = form.get("client_id")
            client_secret = form.get("client_secret")
            header = self.headers.get("Authorization", "")
            if header.startswith("Basic "):
                try:
                    decoded = base64.b64decode(header[6:]).decode()
                    client_id, _, client_secret = decoded.partition(":")
                except ValueError:
                    return False
                from urllib.parse import unquote
                client_id, client_secret = unquote(client_id), unquote(client_secret)
            return client_id == provider.client_id and client_secret == provider.client_secret

        def do_POST(self) -> None:
            url = urlsplit(self.path)
            if url.path != "/oauth/token":
                return self._error(404, "not_found", "unknown path")
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode()
            if self.headers.get("Content-Type", "").startswith("application/json"):
                form = json.loads(raw or "{}")
            else:
                form = {k: v[0] for k, v in parse_qs(raw).items()}
            if not self._client_ok(form):
                return self._error(401, "invalid_client", "client authentication failed")
            grant = form.get("grant_type")
            if grant == "authorization_code":
                with provider.lock:
                    entry = provider.codes.pop(form.get("code", ""), None)
                if entry is None or entry["expires"] < time.time():
                    return self._error(400, "invalid_grant", "code is unknown or expired")
                if form.get("redirect_uri") and form["redirect_uri"] != entry["redirect_uri"]:
                    return self._error(400, "invalid_grant", "redirect_uri mismatch")
                if entry["code_challenge"]:
                    verifier = form.get("code_verifier", "")
                    digest = hashlib.sha256(verifier.encode()).digest()
                    expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
                    if expected != entry["code_challenge"]:
                        return self._error(400, "invalid_grant", "PKCE verification failed")
                return self._json(200, provider.tokens(
                    entry["user"], scope=entry["scope"], audience=entry["audience"],
                    nonce=entry["nonce"],
                ))
            if grant == "refresh_token":
                with provider.lock:
                    entry = provider.refresh_tokens.get(form.get("refresh_token", ""))
                if entry is None:
                    return self._error(400, "invalid_grant", "refresh token is unknown")
                return self._json(200, provider.tokens(
                    entry["user"], scope=entry["scope"], audience=entry["audience"], nonce=None,
                ))
            if grant == "password":
                # Lets the seeding script act as each demo person through the real APIs.
                user = next((u for u in USERS if u["email"] == form.get("username")), None)
                if user is None or form.get("password") != DEMO_PASSWORD:
                    return self._error(403, "invalid_grant", "wrong email or password")
                return self._json(200, provider.tokens(
                    user, scope=form.get("scope", "openid profile email"),
                    audience=form.get("audience") or provider.audience, nonce=None,
                ))
            return self._error(400, "unsupported_grant_type", f"{grant!r} is not supported")

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--public-host", default="localhost",
                        help="issuer host name; the issuer is https://<public-host>/")
    parser.add_argument("--browser-port", type=int, required=True,
                        help="plain-HTTP localhost port for /authorize and logout")
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--client-secret", required=True)
    parser.add_argument("--audience", required=True)
    args = parser.parse_args()

    # The Auth0 SDK derives the issuer from a bare host name, always on port 443.
    issuer = f"https://{args.public_host}/"
    browser_base = f"http://localhost:{args.browser_port}"
    provider = Provider(issuer, browser_base, args.client_id, args.client_secret, args.audience)
    handler = make_handler(provider)
    browser = ThreadingHTTPServer((args.host, args.browser_port), handler)
    threading.Thread(target=browser.serve_forever, daemon=True).start()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(args.cert, args.key)

    class TLSServer(ThreadingHTTPServer):
        # Handshake in the per-connection thread, so one stalled client cannot block accept.
        def finish_request(self, request, client_address):
            try:
                request = context.wrap_socket(request, server_side=True)
            except (ssl.SSLError, OSError):
                return
            super().finish_request(request, client_address)

    server = TLSServer((args.host, args.port), handler)
    print(f"demo identity provider: {html.escape(issuer)} (browser: {browser_base})", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
