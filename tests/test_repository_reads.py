"""Concurrent upstream reads retain independent cancellation, policy and resource limits."""

import asyncio

import httpx2 as httpx
import pytest
from tinypods_core.repositories.sources import SourceError
from tinypods_core.repositories.transport import MetadataHTTP
from tinypods_core.settings import Settings


def test_inflight_reads_share_only_identical_requests() -> None:
    """Count wire reads, isolate request policies, and never retain completed responses."""

    async def scenario() -> None:
        started = asyncio.Event()
        finish = asyncio.Event()
        calls = 0

        async def resolver(host: str) -> list[str]:
            return ["8.8.8.8"]

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            started.set()
            await finish.wait()
            return httpx.Response(200, content=b"bytes")

        client = MetadataHTTP(Settings(), resolver=resolver, transport=httpx.MockTransport(handler))
        first = asyncio.create_task(client.get("https://example.test/data"))
        await started.wait()
        second = asyncio.create_task(client.get("https://example.test/data"))
        await asyncio.sleep(0)
        assert calls == 1
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not second.done()
        finish.set()
        assert (await second).body == b"bytes"
        assert not client._reads
        await client.get("https://example.test/data")
        assert calls == 2
        finish.clear()
        started.clear()
        pending = [
            asyncio.create_task(client.get("https://example.test/data", **options))
            for options in (
                {},
                {"headers": {"Authorization": "Bearer other"}},
                {"authenticate": False},
                {"maximum_bytes": 100},
            )
        ]
        while calls < 6:
            await asyncio.sleep(0)
        finish.set()
        await asyncio.gather(*pending)
        assert calls == 6 and not client._reads

    asyncio.run(scenario())


def test_last_caller_cancellation_and_failure_release_reads() -> None:
    """Join cancelled work, bound distinct active reads and allow retries after failures."""

    async def scenario() -> None:
        started = asyncio.Event()
        stopped = asyncio.Event()
        fail = False

        async def resolver(host: str) -> list[str]:
            return ["8.8.8.8"]

        async def handler(request: httpx.Request) -> httpx.Response:
            if fail:
                raise httpx.ConnectError("private detail")
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
            return httpx.Response(200)

        client = MetadataHTTP(Settings(), resolver=resolver, transport=httpx.MockTransport(handler))
        client.maximum_reads = 1
        pending = asyncio.create_task(client.get("https://example.test/data"))
        await started.wait()
        with pytest.raises(SourceError, match="source_busy"):
            await client.get("https://example.test/other")
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert stopped.is_set() and not client._reads
        fail = True
        with pytest.raises(SourceError, match="source_unavailable"):
            await client.get("https://example.test/data")
        assert not client._reads

    asyncio.run(scenario())
