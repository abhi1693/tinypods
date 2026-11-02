"""Bounded DNS-pinned public HTTPS metadata transport."""

import asyncio
import base64
import ipaddress
import socket
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from urllib.parse import urljoin, urlsplit

import anyio
import httpx2 as httpx
from tinypods_core.repositories.sources import SourceError
from tinypods_core.settings import Settings

Resolver = Callable[[str], Awaitable[list[str]]]


async def resolve_public_host(host: str) -> list[str]:
    """Resolve asynchronously; the caller validates every answer and pins the connection."""
    answers = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    # Prefer IPv4 when both families are advertised; many operator networks lack
    # an IPv6 route even though their DNS resolver returns AAAA records.
    addresses = {str(answer[4][0]) for answer in answers}
    return sorted(addresses, key=lambda address: (ipaddress.ip_address(address).version, address))


@dataclass(frozen=True)
class Document:
    """Bounded upstream response, kept only for this sync operation."""

    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str


@dataclass
class SharedRead:
    """One live read with counted callers; completed bytes are never cached."""

    task: asyncio.Task[Document]
    callers: int = 0


class MetadataHTTP:
    """Pin public DNS answers and preserve the original TLS/Host identity per request.

    Each request owns its client, so pools and cookies cannot cross original
    hostnames sharing an IP. Proxies, insecure TLS and automatic redirects are off.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        resolver: Resolver = resolve_public_host,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Retain operator bounds; transport/resolver injection is for trusted tests."""
        self.settings, self.resolver, self.transport = settings, resolver, transport
        self._reads: dict[
            tuple[str, tuple[tuple[str, str], ...], bool, int | None], SharedRead
        ] = {}
        self.maximum_reads = 32

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        authenticate: bool = True,
        maximum_bytes: int | None = None,
    ) -> Document:
        """Share identical in-flight reads within this settings/credential owner.

        All request headers and limits participate in identity. Every leader still
        enforces DNS, redirects and credentials. Cancelling one caller leaves other
        callers running; the last caller cancels and joins unfinished work. No
        task or completed document survives its last caller, including failures.
        """
        outbound = dict(headers or {})
        key = (url, tuple(sorted(outbound.items())), authenticate, maximum_bytes)
        shared = self._reads.get(key)
        if shared is None:
            if len(self._reads) >= self.maximum_reads:
                raise SourceError(503, "source_busy")
            shared = SharedRead(
                asyncio.create_task(
                    self._get(
                        url,
                        headers=outbound,
                        authenticate=authenticate,
                        maximum_bytes=maximum_bytes,
                    )
                )
            )
            self._reads[key] = shared
        shared.callers += 1
        try:
            return await asyncio.shield(shared.task)
        finally:
            shared.callers -= 1
            if shared.callers == 0:
                del self._reads[key]
                if not shared.task.done():
                    shared.task.cancel()
                    with suppress(asyncio.CancelledError):
                        await shared.task

    async def _get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        authenticate: bool = True,
        maximum_bytes: int | None = None,
    ) -> Document:
        """Read bounded metadata, checking every redirect before making its connection."""
        maximum = self.settings.repository_max_bytes if maximum_bytes is None else maximum_bytes
        current = url
        outbound = dict(headers or {})
        for _ in range(4):
            try:
                target = httpx.URL(current)
                parsed = urlsplit(current)
                if (
                    target.scheme != "https"
                    or not target.host
                    or parsed.port not in (None, 443)
                    or parsed.username
                    or parsed.password
                    or parsed.fragment
                    or "\\" in current
                ):
                    raise SourceError(403, "source_not_allowed")
                allowed = {host.casefold() for host in self.settings.repository_allowed_hosts}
                if allowed and target.host.casefold() not in allowed:
                    raise SourceError(403, "source_not_allowed")
                addresses = await self.resolver(target.host)
                if not addresses or any(
                    not ipaddress.ip_address(address).is_global for address in addresses
                ):
                    raise SourceError(403, "source_not_public")
                # Pin the checked IP while SNI still verifies the source hostname.
                # A second DNS lookup at connect time would allow rebinding to a private address.
                pinned = target.copy_with(host=addresses[0])
                request_headers = {
                    **outbound,
                    "Host": target.netloc.decode("ascii"),
                    "Accept-Encoding": "identity",
                }
                credentials = self.settings.repository_credentials.get(target.host)
                if (
                    authenticate
                    and credentials is not None
                    and not any(key.casefold() == "authorization" for key in request_headers)
                ):
                    raw = (
                        credentials.username.get_secret_value()
                        + ":"
                        + credentials.password.get_secret_value()
                    )
                    request_headers["Authorization"] = "Basic " + base64.b64encode(
                        raw.encode()
                    ).decode("ascii")
                async with (
                    # Client construction loads TLS trust files synchronously.
                    await anyio.to_thread.run_sync(
                        partial(
                            httpx.AsyncClient,
                            transport=self.transport,
                            verify=True,
                            trust_env=False,
                            timeout=5.0,
                            follow_redirects=False,
                        )
                    ) as client,
                    client.stream(
                        "GET",
                        pinned,
                        headers=request_headers,
                        extensions={"sni_hostname": target.host},
                    ) as response,
                ):
                    length = response.headers.get("content-length")
                    if length is not None and int(length) > maximum:
                        raise SourceError(422, "source_metadata_too_large")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > maximum:
                            raise SourceError(422, "source_metadata_too_large")
                    document = Document(
                        response.status_code, dict(response.headers), bytes(body), current
                    )
                # Report upstream throttling without an account switch or automatic retry.
                if document.status == 429:
                    raise SourceError(429, "source_rate_limited")
                if document.status not in (301, 302, 303, 307, 308):
                    return document
                location = document.headers.get("location")
                if not location:
                    raise SourceError(502, "invalid_source_redirect")
                following = urljoin(current, location)
                # Redirected endpoints must never inherit the original site's account/token.
                # Only credentials explicitly configured for the new host may be attached there.
                if urlsplit(following).netloc != parsed.netloc:
                    outbound = {
                        key: value
                        for key, value in outbound.items()
                        if key.casefold() != "authorization"
                    }
                current = following
            except (httpx.HTTPError, httpx.InvalidURL, OSError, ValueError):
                raise SourceError(502, "source_unavailable") from None
        raise SourceError(502, "source_redirect_limit")
