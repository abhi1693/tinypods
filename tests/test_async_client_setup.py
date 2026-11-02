"""TLS-loading client construction stays off event loops; network work remains async."""

import asyncio
import threading

import httpx2 as httpx
import pytest
from test_oidc_protocol import Issuer
from tinypods_core.repositories import transport
from tinypods_core.settings import Settings


def test_http_and_oauth_client_setup_runs_in_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise all construction boundaries with thread checks rather than timing assertions."""
    loop_thread = threading.get_ident()
    original_init = httpx.AsyncClient.__init__
    setup = []
    network = []

    def initialize(client: httpx.AsyncClient, *args: object, **kwargs: object) -> None:
        assert threading.get_ident() != loop_thread
        setup.append("http")
        original_init(client, *args, **kwargs)

    async def handler(request: httpx.Request) -> httpx.Response:
        network.append(threading.get_ident())
        return httpx.Response(200, json={"ok": True})

    async def resolver(host: str) -> list[str]:
        return ["8.8.8.8"]

    monkeypatch.setattr(httpx.AsyncClient, "__init__", initialize)

    async def scenario() -> None:
        reader = transport.MetadataHTTP(
            Settings(), resolver=resolver, transport=httpx.MockTransport(handler)
        )
        assert (await reader.get("https://example.test")).status == 200
        adapter = Issuer().adapter()
        adapter.transport = httpx.MockTransport(handler)
        assert await adapter._json("https://issuer.test") == {"ok": True}
        async with await adapter._client() as client:
            assert client is not None
        assert setup == ["http", "http", "http"]
        assert network == [loop_thread, loop_thread]

    asyncio.run(scenario())
