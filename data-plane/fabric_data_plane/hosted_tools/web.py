"""web_fetch and web_search: reaching the web from the gateway without becoming an SSRF proxy.

Target policy (every URL the model supplies, every redirect hop, and every URL handed to
Firecrawl):

* ``http``/``https`` only, no credentials in the URL, default or explicit port;
* the host must match ``FABRIC_DP_WEB_FETCH_ALLOWED_HOSTS``: a listed name, a ``*.suffix``,
  or ``*`` for any host;
* loopback, link-local, unspecified, multicast, reserved and cloud metadata addresses are
  refused always (literal or resolved). A host admitted only by ``*`` must also resolve to
  *globally routable* addresses: RFC 1918, CGNAT, ULA and similar private ranges are
  refused. An explicitly listed host may resolve to a private address (an intranet wiki);
* the name is resolved once, every address is checked, and the connection is pinned to the
  vetted address (Host header and TLS SNI keep the name), so a DNS answer that changes
  between check and connect (rebinding) cannot redirect the request;
* redirects are followed by hand, at most ``web_fetch_max_redirects`` hops, and each hop
  is checked again; no cookies are stored or sent, no caller header is forwarded, and the
  request identifies itself with a Fabric User-Agent.

Operator-configured services (SearXNG, a search API, Firecrawl) are trusted endpoints that
may live in-cluster or on loopback; their URLs are still refused if they point at
link-local or metadata addresses.
"""

from __future__ import annotations

import asyncio
import http.cookiejar
import ipaddress
import json
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote_plus, urljoin, urlsplit, urlunsplit

import httpx

from fabric_data_plane.agent_tools import (
    _METADATA_HOSTNAMES,
    Resolver,
    ToolError,
    blocked_address,
    default_resolver,
)
from fabric_data_plane.hosted_tools import HostedResult

USER_AGENT = "Fabric-WebFetch/1 (hosted agent tool)"
_TEXT_TYPES = (
    "text/html",
    "application/xhtml+xml",
    "text/plain",
    "text/markdown",
    "application/json",
    "text/csv",
    "application/xml",
    "text/xml",
)
_MAX_URL = 2048
_MAX_QUERY = 512


# --------------------------------------------------------------------------- policy


@dataclass(frozen=True)
class HostPolicy:
    allow_any: bool
    names: frozenset[str]
    suffixes: tuple[str, ...]

    @classmethod
    def parse(cls, setting: str) -> HostPolicy:
        entries = [h.strip().lower().rstrip(".") for h in setting.split(",") if h.strip()]
        return cls(
            allow_any="*" in entries,
            names=frozenset(e for e in entries if not e.startswith("*")),
            suffixes=tuple(e[1:] for e in entries if e.startswith("*.")),
        )

    def explicit(self, host: str) -> bool:
        return host in self.names or any(host.endswith(s) for s in self.suffixes)

    def admits(self, host: str) -> bool:
        return self.allow_any or self.explicit(host)


def _public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global


def _ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class Target:
    url: str
    scheme: str
    host: str
    port: int
    explicit: bool


def check_url(url: Any, policy: HostPolicy) -> Target:
    """Static checks on a model-supplied URL (no DNS yet)."""
    if not isinstance(url, str) or not url.strip():
        raise ToolError("invalid_arguments", "url must be a non-empty string")
    url = url.strip()
    if len(url) > _MAX_URL:
        raise ToolError("invalid_arguments", "url is too long")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise ToolError("invalid_url", "url is not valid") from exc
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme not in ("http", "https") or not host:
        raise ToolError("invalid_url", "url must be absolute http(s)")
    if parts.username or parts.password:
        raise ToolError("invalid_url", "url must not carry credentials")
    if host in _METADATA_HOSTNAMES or host == "localhost" or host.endswith(".localhost"):
        raise ToolError("host_not_allowed", "target address is never allowed")
    if _ip_literal(host) and blocked_address(host):
        raise ToolError("host_not_allowed", "target address is never allowed")
    if not policy.admits(host):
        raise ToolError("host_not_allowed", "host is not in the web_fetch allowlist")
    explicit = policy.explicit(host)
    if _ip_literal(host) and not explicit and not _public(host):
        raise ToolError("host_not_allowed", "private addresses need an explicit allowlist entry")
    return Target(
        url=url,
        scheme=parts.scheme,
        host=host,
        port=port or (443 if parts.scheme == "https" else 80),
        explicit=explicit,
    )


async def vet_address(target: Target, resolver: Resolver) -> str:
    """Resolve once, check every address, return the one to connect to."""
    if _ip_literal(target.host):
        return target.host
    try:
        addresses = await resolver(target.host, target.port)
    except OSError as exc:
        raise ToolError("host_unreachable", "host did not resolve") from exc
    if not addresses:
        raise ToolError("host_unreachable", "host did not resolve")
    for address in addresses:
        if blocked_address(address):
            raise ToolError("host_not_allowed", "host resolves to a forbidden address")
        if not target.explicit and not _public(address):
            raise ToolError("host_not_allowed", "host resolves to a private address")
    return addresses[0]


def check_service_url(url: str, what: str) -> None:
    """Operator-configured endpoint: may be in-cluster, never link-local or metadata."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme not in ("http", "https") or not host:
        raise ValueError(f"{what} must be an absolute http(s) URL")
    if parts.username or parts.password:
        raise ValueError(f"{what} must not carry credentials; use the API key setting")
    if host in _METADATA_HOSTNAMES:
        raise ValueError(f"{what} points at a metadata endpoint")
    if _ip_literal(host):
        ip = ipaddress.ip_address(host)
        if ip.is_link_local or ip.is_multicast or ip.is_unspecified:
            raise ValueError(f"{what} points at a forbidden address")


# --------------------------------------------------------------------------- extraction


class _Readable(HTMLParser):
    """HTML to readable text with the standard library: drop scripts/styles, keep blocks."""

    _SKIP = {"script", "style", "noscript", "template", "svg", "head", "iframe", "object"}
    _BLOCK = {
        "p",
        "div",
        "br",
        "li",
        "tr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "section",
        "article",
        "header",
        "footer",
        "table",
        "ul",
        "ol",
        "pre",
        "blockquote",
        "hr",
        "dd",
        "dt",
        "figcaption",
        "main",
        "nav",
        "aside",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        elif tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")
        elif tag in ("td", "th"):
            self.parts.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        elif tag in self._SKIP and self._skip:
            self._skip -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> tuple[str, str]:
    """Return (title, text) of an HTML document."""
    parser = _Readable()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - malformed markup: keep what was parsed
        pass
    text = "".join(parser.parts)
    lines = [re.sub(r"[ \t\r\f\v]+", " ", line).strip() for line in text.split("\n")]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return re.sub(r"\s+", " ", parser.title).strip(), text


def _cap_text(text: str, limit: int) -> tuple[str, bool]:
    return (text, False) if len(text) <= limit else (text[:limit], True)


# --------------------------------------------------------------------------- clients


def web_client() -> httpx.AsyncClient:
    """A dedicated client: no proxies from the environment, no redirects, no cookie jar,
    and no connection reuse (a pinned connection must never serve another host name)."""
    jar = http.cookiejar.CookieJar(policy=http.cookiejar.DefaultCookiePolicy(allowed_domains=[]))
    return httpx.AsyncClient(
        follow_redirects=False,
        trust_env=False,
        cookies=jar,
        limits=httpx.Limits(max_keepalive_connections=0),
    )


async def _read_capped(response: httpx.Response, cap: int) -> tuple[bytes, bool]:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        room = cap - len(body)
        if len(chunk) > room:
            body.extend(chunk[:room])
            return bytes(body), True
        body.extend(chunk)
    return bytes(body), False


def _pinned(target: Target, address: str) -> tuple[str, dict[str, str], dict[str, Any]]:
    parts = urlsplit(target.url)
    literal = f"[{address}]" if ":" in address else address
    netloc = f"{literal}:{target.port}"
    url = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
    default_port = 443 if target.scheme == "https" else 80
    host_header = target.host if target.port == default_port else f"{target.host}:{target.port}"
    extensions: dict[str, Any] = {}
    if target.scheme == "https":
        extensions["sni_hostname"] = target.host
    return url, {"Host": host_header}, extensions


class WebTools:
    def __init__(
        self,
        settings: Any,
        *,
        client: httpx.AsyncClient | None = None,
        service_client: httpx.AsyncClient | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self.settings = settings
        self.policy = HostPolicy.parse(settings.web_fetch_allowed_hosts)
        self.client = client or web_client()
        # Configured services are called by name (in-cluster DNS) with a normal client.
        self.service_client = service_client or httpx.AsyncClient(
            follow_redirects=False, trust_env=False
        )
        self.resolver = resolver or default_resolver
        for name, url in (
            ("FABRIC_DP_WEB_SEARCH_URL", settings.web_search_url.split("{", 1)[0]),
            ("FABRIC_DP_FIRECRAWL_URL", settings.firecrawl_url),
        ):
            if url:
                check_service_url(url, name)

    # ------------------------------------------------------------------ fetch

    async def fetch(self, arguments: dict[str, Any]) -> HostedResult:
        target = check_url(arguments.get("url"), self.policy)
        deadline = self.settings.web_fetch_timeout_seconds
        try:
            if self.settings.web_backend == "firecrawl":
                return await asyncio.wait_for(self._firecrawl_scrape(target), deadline + 5)
            return await asyncio.wait_for(self._direct_fetch(target), deadline)
        except TimeoutError as exc:
            raise ToolError("fetch_timeout", "the page did not load in time") from exc

    async def _direct_fetch(self, target: Target) -> HostedResult:
        settings = self.settings
        redirects = 0
        fetched = 0
        while True:
            address = await vet_address(target, self.resolver)
            url, headers, extensions = _pinned(target, address)
            request = httpx.Request(
                "GET",
                url,
                headers={
                    **headers,
                    "User-Agent": USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
                    "Accept-Encoding": "gzip, deflate",
                },
                extensions={
                    **extensions,
                    "timeout": httpx.Timeout(settings.web_fetch_timeout_seconds).as_dict(),
                },
            )
            try:
                response = await self.client.send(request, stream=True, follow_redirects=False)
            except httpx.TimeoutException as exc:
                raise ToolError("fetch_timeout", "the page did not load in time") from exc
            except httpx.HTTPError as exc:
                raise ToolError("host_unreachable", "the host could not be reached") from exc
            try:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ToolError("fetch_http_error", "redirect without a location")
                    if redirects >= settings.web_fetch_max_redirects:
                        raise ToolError("too_many_redirects", "the page redirected too often")
                    redirects += 1
                    target = check_url(urljoin(target.url, location), self.policy)
                    continue
                body, truncated = await _read_capped(response, settings.web_fetch_max_bytes)
                fetched += len(body)
                status = response.status_code
                content_type = response.headers.get("content-type", "").split(";")[0].strip()
                encoding = response.encoding or "utf-8"
            except httpx.TimeoutException as exc:
                raise ToolError("fetch_timeout", "the page did not load in time") from exc
            except httpx.HTTPError as exc:
                raise ToolError("host_unreachable", "the reply failed") from exc
            finally:
                await response.aclose()
            break

        if content_type and not content_type.startswith(_TEXT_TYPES):
            raise ToolError("unsupported_content_type", f"content type {content_type} is not text")
        raw = body.decode(encoding, errors="replace")
        if content_type in ("text/html", "application/xhtml+xml") or (
            not content_type and "<html" in raw[:1000].lower()
        ):
            title, text = html_to_text(raw)
        else:
            title, text = "", raw
        text, cut = _cap_text(text, settings.web_fetch_max_chars)
        ok = 200 <= status < 300
        payload = {
            "url": target.url,
            "status": status,
            "content_type": content_type or None,
            "title": title or None,
            "text": text,
            "truncated": truncated or cut,
        }
        return HostedResult(
            content=json.dumps(payload),
            ok=ok,
            error=None if ok else "fetch_http_error",
            truncated=truncated or cut,
            details={
                "backend": "direct",
                "http_status": status,
                "fetched_bytes": fetched,
                "text_chars": len(text),
                "redirects": redirects,
            },
        )

    def _firecrawl_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
        key = self.settings.firecrawl_api_key.get_secret_value()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    async def _firecrawl(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = self.settings.firecrawl_url.rstrip("/") + path
        try:
            response = await self.service_client.post(
                url,
                json=body,
                headers=self._firecrawl_headers(),
                timeout=self.settings.firecrawl_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise ToolError("backend_timeout", "the web backend did not answer in time") from exc
        except httpx.HTTPError as exc:
            raise ToolError("backend_unavailable", "the web backend could not be reached") from exc
        if not response.is_success:
            raise ToolError(
                "backend_error", f"the web backend answered HTTP {response.status_code}"
            )
        if len(response.content) > self.settings.web_fetch_max_bytes * 4:
            raise ToolError("backend_error", "the web backend reply is too large")
        try:
            data = response.json()
        except ValueError as exc:
            raise ToolError("backend_error", "the web backend reply is not JSON") from exc
        if not isinstance(data, dict) or data.get("success") is False:
            raise ToolError("backend_error", "the web backend refused the request")
        return data

    async def _firecrawl_scrape(self, target: Target) -> HostedResult:
        # Firecrawl resolves and connects itself, so the gateway can only pre-check the
        # target; Firecrawl's own egress policy must also block private ranges.
        await vet_address(target, self.resolver)
        started = time.perf_counter()
        data = await self._firecrawl(
            self.settings.firecrawl_scrape_path,
            {
                "url": target.url,
                "formats": ["markdown"],
                "onlyMainContent": True,
                "timeout": int(self.settings.web_fetch_timeout_seconds * 1000),
            },
        )
        doc = data.get("data") if isinstance(data.get("data"), dict) else {}
        metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
        markdown = str(doc.get("markdown") or "")
        text, cut = _cap_text(markdown, self.settings.web_fetch_max_chars)
        status = metadata.get("statusCode")
        payload = {
            "url": target.url,
            "status": status,
            "content_type": "text/markdown",
            "title": metadata.get("title"),
            "text": text,
            "truncated": cut,
        }
        ok = not isinstance(status, int) or 200 <= status < 300
        return HostedResult(
            content=json.dumps(payload),
            ok=ok,
            error=None if ok else "fetch_http_error",
            truncated=cut,
            details={
                "backend": "firecrawl",
                "http_status": status if isinstance(status, int) else None,
                "fetched_bytes": len(markdown.encode()),
                "text_chars": len(text),
                "backend_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )

    # ------------------------------------------------------------------ search

    async def search(self, arguments: dict[str, Any]) -> HostedResult:
        query = arguments.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolError("invalid_arguments", "query must be a non-empty string")
        if len(query) > _MAX_QUERY:
            raise ToolError("invalid_arguments", "query is too long")
        limit = self.settings.web_search_max_results
        count = arguments.get("count", limit)
        if not isinstance(count, int) or isinstance(count, bool) or count < 1:
            count = limit
        count = min(count, limit)
        backend = self.settings.web_backend
        if backend == "firecrawl":
            data = await self._firecrawl(
                self.settings.firecrawl_search_path, {"query": query, "limit": count}
            )
            raw = data.get("data")
            if isinstance(raw, dict):  # v2 groups results by source
                raw = raw.get("web") or []
            results = self._results(raw, ("description", "snippet", "markdown"))
        else:
            results = await self._search_http(query, count, searxng=backend == "searxng")
        results = results[:count]
        payload = {"query": query, "results": results}
        return HostedResult(
            content=json.dumps(payload),
            details={"backend": backend, "results": len(results), "query_chars": len(query)},
        )

    async def _search_http(self, query: str, count: int, *, searxng: bool) -> list[dict]:
        settings = self.settings
        if searxng:
            url = settings.web_search_url.rstrip("/") + "/search"
            params: dict[str, Any] | None = {"q": query, "format": "json", "safesearch": 1}
        else:
            url = settings.web_search_url.replace("{query}", quote_plus(query)).replace(
                "{count}", str(count)
            )
            params = None
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        key = settings.web_search_api_key.get_secret_value()
        if key:
            headers[settings.web_search_api_key_header] = key
        try:
            response = await self.service_client.get(
                url, params=params, headers=headers, timeout=settings.web_search_timeout_seconds
            )
        except httpx.TimeoutException as exc:
            raise ToolError("backend_timeout", "the search backend did not answer in time") from exc
        except httpx.HTTPError as exc:
            raise ToolError(
                "backend_unavailable", "the search backend could not be reached"
            ) from exc
        if not response.is_success:
            raise ToolError(
                "backend_error", f"the search backend answered HTTP {response.status_code}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ToolError("backend_error", "the search backend reply is not JSON") from exc
        raw = data.get("results") if isinstance(data, dict) else data
        return self._results(raw, ("content", "snippet", "description"))

    @staticmethod
    def _results(raw: Any, snippet_keys: tuple[str, ...]) -> list[dict[str, Any]]:
        results = []
        for item in raw if isinstance(raw, list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                continue
            snippet = next((item[k] for k in snippet_keys if isinstance(item.get(k), str)), "")
            results.append(
                {
                    "title": str(item.get("title") or "")[:300],
                    "url": item["url"][:_MAX_URL],
                    "snippet": snippet[:500],
                }
            )
        return results
