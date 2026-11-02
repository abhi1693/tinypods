"""Bounded, conditional JSON snapshots sharing the existing async cache pool."""

from tinypods_core.cache.storage import ResponseCache

READ = """
local size = redis.call('STRLEN', KEYS[1])
if size > tonumber(ARGV[1]) then return false end
return redis.call('GET', KEYS[1])
"""

PUBLISH = """
local old = false
if redis.call('STRLEN', KEYS[1]) <= tonumber(ARGV[4]) then
    old = redis.call('GET', KEYS[1])
end
if old then
    local ok, previous = pcall(cjson.decode, old)
    if ok and type(previous) == 'table' and type(previous.started_at) == 'string'
       and previous.started_at >= ARGV[1] then return 0 end
end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
return 1
"""


class SnapshotCache:
    """Use a separate key family without public-response generations or queue changes."""

    def __init__(self, cache: ResponseCache, ttl: int) -> None:
        """Borrow an application-owned pool; the application retains cleanup ownership."""
        self.cache = cache
        self.ttl = ttl

    def key(self, identity: str) -> str:
        """Separate typed snapshot schema keys from public HTTP response entries."""
        return f"{self.cache.namespace}:cluster-capabilities:v1:{identity}"

    async def read(self, identity: str) -> bytes | None:
        """Bound response transfer atomically, including entries replaced concurrently."""
        value = await self.cache._run(
            lambda: self.cache.redis.eval(READ, 1, self.key(identity), self.cache.max_bytes)
        )
        return value if isinstance(value, bytes) else None

    async def publish(self, identity: str, started_at: str, payload: bytes) -> bool:
        """Prevent an older refresh from replacing a newer snapshot across processes."""
        if len(payload) > self.cache.max_bytes:
            return False
        return bool(
            await self.cache._run(
                lambda: self.cache.redis.eval(
                    PUBLISH,
                    1,
                    self.key(identity),
                    started_at,
                    payload,
                    self.ttl,
                    self.cache.max_bytes,
                )
            )
        )
