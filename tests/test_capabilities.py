"""Read-through capability discovery, real TLS transport and disposable Valkey snapshots."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from kubernetes.aio.client import ApiClient
from pydantic import BaseModel, SecretStr
from secret_fixtures import TOKEN, key_settings, kubeconfig
from test_auth import auth_settings as auth_settings
from test_cluster_connection import Certificates, Reply, TLSFixture, config_for
from test_cluster_connection import certificates as certificates
from test_cluster_registry import client_for, create
from test_cluster_secrets import cluster_records, owner, service
from tinypods_core.audit import audit_context
from tinypods_core.cache import ResponseCache
from tinypods_core.cache.snapshots import SnapshotCache
from tinypods_core.clusters.capabilities import (
    CapabilityClient,
    CapabilitySection,
    CapabilitySnapshot,
    NodeCapability,
)
from tinypods_core.clusters.discovery import CapabilityService, DiscoveryCoordinator
from tinypods_core.db import Database
from tinypods_core.secrets.kubernetes import Kubeconfig
from tinypods_core.settings import Settings

BASE = "/api/v1"


def snapshot(cluster: UUID, version: UUID, started: datetime) -> CapabilitySnapshot:
    """Produce only selected typed facts, never a raw Kubernetes response."""
    return CapabilitySnapshot(
        cluster_id=cluster,
        credential_version_id=version,
        started_at=started,
        collected_at=datetime.now(UTC),
        kubernetes=CapabilitySection(state="available", items=["v1.36.5+k3s1"]),
        apis=CapabilitySection(state="available"),
        nodes=CapabilitySection(
            state="available",
            items=[
                NodeCapability(
                    name="pi-1",
                    architecture="arm64",
                    operating_system="linux",
                    ready=True,
                    schedulable=True,
                ),
            ],
        ),
        storage=CapabilitySection(state="available"),
        ingress=CapabilitySection(state="unsupported", reason="unsupported", complete=False),
        runtimes=CapabilitySection(state="forbidden", reason="forbidden", complete=False),
    )


def test_real_tls_discovery_selected_fields(certificates: Certificates) -> None:
    """Official SDK requests preserve proxy prefixes and return useful partial results."""

    async def scenario() -> None:
        replies = {
            "/apis": Reply(200, b'{"groups":[]}'),
            "/api/v1": Reply(
                200, b'{"resources":[{"name":"pods","kind":"Pod","namespaced":true}]}'
            ),
            "/api/v1/nodes": Reply(
                200,
                json.dumps(
                    {
                        "items": [
                            {
                                "status": {
                                    "nodeInfo": {
                                        "architecture": "arm64",
                                        "operatingSystem": "linux",
                                    },
                                    "conditions": [{"type": "Ready", "status": "True"}],
                                },
                                "metadata": {
                                    "name": "pi-1",
                                    "labels": {"kubernetes.io/arch": "arm64"},
                                    "annotations": {"example.test/rack": "rack-1"},
                                },
                            },
                            {
                                "status": {
                                    "nodeInfo": {
                                        "architecture": "amd64",
                                        "operatingSystem": "linux",
                                    },
                                    "conditions": [],
                                },
                                "metadata": {"name": "server-2"},
                                "spec": {"unschedulable": True},
                            },
                        ]
                    }
                ).encode(),
            ),
            "/apis/storage.k8s.io/v1/storageclasses": Reply(
                200,
                json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {
                                    "name": "local",
                                    "annotations": {
                                        "storageclass.kubernetes.io/is-default-class": "true"
                                    },
                                },
                                "provisioner": "rancher.io/local-path",
                                "allowVolumeExpansion": True,
                                "volumeBindingMode": "WaitForFirstConsumer",
                            },
                        ]
                    }
                ).encode(),
            ),
            "/apis/networking.k8s.io/v1/ingressclasses": Reply(404, b"private"),
            "/apis/node.k8s.io/v1/runtimeclasses": Reply(403, b"private"),
        }
        # The fixture records complete request paths but routes only by path.
        async with TLSFixture(
            certificates, override=replies, prefix="/k8s/clusters/local"
        ).running() as peer:
            result = await CapabilityClient().collect(
                config_for(peer, certificates), uuid4(), uuid4(), datetime.now(UTC)
            )
            assert result.kubernetes.items == ["v1.33.1"]
            assert {n.architecture for n in result.nodes.items} == {"arm64", "amd64"}
            assert not result.nodes.items[1].schedulable
            assert result.storage.items[0].default
            assert result.ingress.state == "unsupported" and result.runtimes.state == "forbidden"
            assert result.apis.items[0].name == "pods"
            assert all(method == "GET" for method, _ in peer.requests)
            assert all(path.startswith("/k8s/clusters/local/") for _, path in peer.requests)
            assert (
                TOKEN not in result.model_dump_json()
                and "must-not-return" not in result.model_dump_json()
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("cache_mode", ["outage", "disabled"])
def test_http_cache_hit_and_cache_outage(
    auth_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    cache_mode: str,
) -> None:
    """Owner reads fall back live without turning cache faults into source errors."""
    calls = []

    async def collect(
        self: CapabilityClient, config: Kubeconfig, cluster: UUID, version: UUID, started: datetime
    ) -> CapabilitySnapshot:
        calls.append(cluster)
        assert config.token is not None
        return snapshot(cluster, version, started)

    monkeypatch.setattr(CapabilityClient, "collect", collect)
    settings = key_settings(auth_settings).model_copy(
        update={
            "cache_enabled": True,
            "cache_namespace": "capability-test-" + uuid4().hex,
        }
    )
    with client_for(settings) as client:
        region = create(client, "regions", name="Local", slug="local")
        cluster = create(client, "clusters", name="Pi", slug="pi", region_id=region["id"])
        path = BASE + "/clusters/" + cluster["id"]
        assert (
            client.post(
                path + "/kubeconfig/versions", json={"kubeconfig": kubeconfig().decode()}
            ).status_code
            == 201
        )
        first = client.get(path + "/capabilities")
        assert first.status_code == 200, first.text
        assert first.json()["source"] == "live" and first.json()["cached"]
        assert client.get(path + "/capabilities").json()["source"] == "cache"
        assert len(calls) == 1
        assert TOKEN not in first.text
        assert client.post(path + "/capabilities/discovery").status_code == 404
        assert client.post(path + "/capabilities").status_code == 405

    outage = settings.model_copy(
        update=(
            {"cache_enabled": False}
            if cache_mode == "disabled"
            else {"cache_valkey_url": SecretStr("redis://127.0.0.1:1/0")}
        )
    )
    # Use the same existing owner/database, without bootstrapping a second owner.
    from fastapi.testclient import TestClient
    from tinypods_api.main import create_app

    with TestClient(create_app(outage), base_url="https://testserver") as client:
        from test_auth import PASSWORD

        assert (
            client.post(
                BASE + "/auth/login",
                json={"username": "owner", "password": PASSWORD},
                headers={"x-csrf-token": "login"},
            ).status_code
            == 200
        )
        response = client.get(path + "/capabilities")
        assert response.status_code == 200 and response.json()["source"] == "live"
        assert not response.json()["cached"]

        async def reader_session() -> str:
            from tinypods_core.auth import SessionService
            from tinypods_core.db.models import User

            database = Database(settings)
            try:
                async with database.transaction() as session:
                    reader = User(username="reader", is_owner=False)
                    session.add(reader)
                issued = await SessionService(database, settings=settings).issue(reader.id)
                return issued.token
            finally:
                await database.close()

        token = asyncio.run(reader_session())
        client.cookies.clear()
        assert client.get(path + "/capabilities").status_code == 401
        client.cookies.set("__Host-tinypods-session", token)
        assert client.get(path + "/capabilities").status_code == 403


def test_snapshot_ordering_expiry_and_invalid_entries(auth_settings: Settings) -> None:
    """Real Valkey atomically rejects stale publication and bounds corrupted cached values."""

    async def scenario() -> None:
        settings = auth_settings.model_copy(update={"cache_namespace": "snapshots-" + uuid4().hex})
        cache = ResponseCache(settings)
        store = SnapshotCache(cache, 1)
        identity = str(uuid4())
        try:
            start = datetime.now(UTC)
            old = snapshot(UUID(identity), uuid4(), start)
            new = old.model_copy(update={"started_at": start + timedelta(seconds=1)})

            async def publish(value: CapabilitySnapshot) -> bool:
                return await store.publish(
                    identity,
                    str(value.model_dump(mode="json")["started_at"]),
                    value.model_dump_json().encode(),
                )

            assert await store.read(identity) is None
            assert await publish(new)
            assert not await publish(old)
            assert (
                CapabilitySnapshot.model_validate_json(await store.read(identity)).started_at
                == new.started_at
            )
            await cache.redis.set(store.key(identity), b"invalid")
            assert await publish(old)
            await cache.redis.set(store.key(identity), b"x" * (cache.max_bytes + 1))
            assert await store.read(identity) is None
            assert not await store.publish(identity, "x", b"x" * (cache.max_bytes + 1))
            await cache.redis.delete(store.key(identity))
            assert await publish(old)
            await cache.redis.expire(store.key(identity), 0)
            assert await store.read(identity) is None
        finally:
            await cache.close()

    asyncio.run(scenario())


def test_service_live_fallback_without_cache_and_credential_changes(
    auth_settings: Settings,
) -> None:
    """Service rechecks credential scope after source I/O, rather than trusting cached identity."""

    class Client(CapabilityClient):
        async def collect(
            self, config: Kubeconfig, cluster: UUID, version: UUID, started: datetime
        ) -> CapabilitySnapshot:
            return snapshot(cluster, version, started)

    async def scenario() -> None:
        settings = key_settings(auth_settings)
        database = Database(settings)
        try:
            cluster, _ = await cluster_records(database)
            async with database.session() as session:
                secret_service = service(session, settings)
                await secret_service.save_cluster_version(owner(), cluster, kubeconfig())
                result = await CapabilityService(
                    secret_service, None, DiscoveryCoordinator(), Client()
                ).get(owner(), cluster)
                assert result.source == "live" and not result.cached
        finally:
            await database.close()

    with audit_context("system:test-capabilities"):
        asyncio.run(scenario())


@pytest.mark.parametrize(
    "reply,reason",
    [
        (Reply(401, b"private"), "unauthorized"),
        (Reply(500, b"private"), "invalid-response"),
        (Reply(200, b"not-json"), "invalid-response"),
        (Reply(200, b"x" * 1048577), "limit"),
        (Reply(200, b"{}", "gzip"), "invalid-response"),
    ],
)
def test_bounded_upstream_failure_categories(
    certificates: Certificates,
    reply: Reply,
    reason: str,
) -> None:
    """Untrusted response bodies never become public diagnostic text."""

    async def scenario() -> None:
        async with TLSFixture(certificates, override={"/version": reply}).running() as peer:
            result = await CapabilityClient().collect(
                config_for(peer, certificates), uuid4(), uuid4(), datetime.now(UTC)
            )
            assert result.kubernetes.state == "unavailable"
            assert result.kubernetes.reason == reason
            assert "private" not in result.model_dump_json()

    asyncio.run(scenario())


@pytest.mark.parametrize("section_timeout", [0.001, 2])
def test_live_discovery_deadlines_and_cancellation(
    certificates: Certificates,
    section_timeout: float,
) -> None:
    """Per-request/section deadlines and caller cancellation close real SDK sessions."""

    async def scenario() -> None:
        gate = asyncio.Event()
        async with TLSFixture(certificates, gate=gate).running() as peer:
            client = CapabilityClient(request_timeout=0.03, section_timeout=section_timeout)
            result = await client.collect(
                config_for(peer, certificates), uuid4(), uuid4(), datetime.now(UTC)
            )
            assert result.kubernetes.reason == "timeout"
            task = asyncio.create_task(
                CapabilityClient().collect(
                    config_for(peer, certificates), uuid4(), uuid4(), datetime.now(UTC)
                )
            )
            await peer.started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(scenario())


def test_listing_page_bound_and_resource_fanout() -> None:
    """Bound server-controlled continuation and resource-version expansion independently."""
    from tinypods_core.clusters.capabilities import (
        Groups,
        Node,
        ObjectList,
        Resources,
        Version,
    )

    class Client(CapabilityClient):
        async def read[T: BaseModel](
            self,
            client: ApiClient,
            path: str,
            model: type[T],
            continuation: str | None = None,
        ) -> T:
            if path.endswith("/nodes"):
                return model.model_validate({"items": [], "metadata": {"continue": "next"}})
            if model is Groups:
                return model.model_validate(
                    {
                        "groups": [
                            {
                                "versions": [
                                    {"groupVersion": "example.test/v" + str(n)} for n in range(20)
                                ]
                            }
                        ]
                    }
                )
            if model is Resources:
                return model.model_validate(
                    {
                        "resources": [
                            {"name": "resource-" + str(n), "kind": "Fixture", "namespaced": True}
                            for n in range(110)
                        ]
                    }
                )
            if model is Version:
                return model.model_validate({"gitVersion": "v1.36.5"})
            return model.model_validate({"items": []})

    async def scenario() -> None:
        import anyio
        from tinypods_core.secrets.kubernetes import build_sdk_client

        client = await anyio.to_thread.run_sync(
            build_sdk_client, Kubeconfig("https://example.test")
        )
        async with client:
            values, complete = await Client().listing(client, "/api/v1/nodes", ObjectList[Node])
            assert values == [] and not complete
        result = await Client().collect(
            Kubeconfig("https://example.test"), uuid4(), uuid4(), datetime.now(UTC)
        )
        assert len(result.apis.items) == 2000 and not result.apis.complete

    asyncio.run(scenario())


def test_node_provisioning_metadata_and_resource_quantities() -> None:
    """Keep named node metadata, taints and exact resource units for future planning."""
    from tinypods_core.clusters.capabilities import Node, TaintCapability

    node = Node.model_validate(
        {
            "metadata": {
                "name": "pi-worker-1",
                "labels": {"kubernetes.io/arch": "arm64"},
                "annotations": {"example.test/rack": "one"},
            },
            "status": {
                "nodeInfo": {
                    "architecture": "arm64",
                    "operatingSystem": "linux",
                    "osImage": "Debian 13",
                    "kernelVersion": "6.12",
                },
                "capacity": {"cpu": "4", "memory": "8192000Ki", "ephemeral-storage": "128Gi"},
                "allocatable": {"cpu": "3500m", "memory": "7Gi", "ephemeral-storage": "100Gi"},
            },
            "spec": {"taints": [{"key": "dedicated", "value": "apps", "effect": "NoSchedule"}]},
        }
    )
    assert node.metadata.name == "pi-worker-1"
    assert node.status.allocatable["cpu"] == "3500m"
    assert node.status.capacity["ephemeral-storage"] == "128Gi"
    assert node.status.nodeInfo.osImage == "Debian 13"
    assert node.spec.taints[0].effect == "NoSchedule"
    assert TaintCapability(key="dedicated", effect="NoExecute").value == ""


def test_direct_service_cache_validation_and_credential_lifecycle(auth_settings: Settings) -> None:
    """Invalid/foreign cache data falls through; changing or revoked credentials never pass."""
    from tinypods_api.clusters.capabilities import get_capabilities
    from tinypods_core.cache.storage import CacheUnavailable
    from tinypods_core.secrets.crypto import SecretError

    class Cache(SnapshotCache):
        def __init__(self) -> None:
            self.body = None
            self.fail_read = False
            self.fail_write = False

        async def read(self, identity: str) -> bytes | None:
            if self.fail_read:
                raise CacheUnavailable
            return self.body

        async def publish(self, identity: str, started_at: str, payload: bytes) -> bool:
            if self.fail_write:
                raise CacheUnavailable
            self.body = payload
            return True

    class Client(CapabilityClient):
        def __init__(self) -> None:
            self.change = False

        async def collect(
            self, config: Kubeconfig, cluster: UUID, version: UUID, started: datetime
        ) -> CapabilitySnapshot:
            if self.change:
                async with database.session() as other:
                    await service(other, settings).save_cluster_version(
                        owner(), cluster, kubeconfig()
                    )
            return snapshot(cluster, version, started)

    async def scenario() -> None:
        nonlocal database
        database = Database(settings)
        cache = Cache()
        client = Client()
        coordinator = DiscoveryCoordinator()
        try:
            first, second = await cluster_records(database)
            async with database.session() as session:
                credentials = service(session, settings)
                await credentials.create(owner(), first, "Pi", "", kubeconfig())
                staged = await credentials.save_cluster_version(
                    owner(), second, kubeconfig(), activate=False
                )
                staged_id = staged.id
                current = CapabilityService(credentials, cache, coordinator, client)
                with pytest.raises(SecretError, match="secret_not_found"):
                    await current.get(owner(), second)
                await session.rollback()
                result = await get_capabilities(first, current, owner())
                assert result.cached
                assert (await current.get(owner(), first)).source == "cache"
                for body in (
                    None,
                    b"invalid",
                    snapshot(uuid4(), uuid4(), datetime.now(UTC)).model_dump_json().encode(),
                    snapshot(first, uuid4(), datetime.now(UTC)).model_dump_json().encode(),
                ):
                    cache.body = body
                    assert (await current.get(owner(), first)).source == "live"
                cache.fail_read = True
                cache.fail_write = True
                assert not (await current.get(owner(), first)).cached
                cache.fail_read = False
                cache.body = None
                client.change = True
                with pytest.raises(SecretError, match="cluster_credential_changed"):
                    await current.get(owner(), first)
                await session.rollback()
                await credentials.revoke(owner(), staged_id)
                other_binding = await credentials.cluster_binding(owner(), second)
                assert other_binding.active_version_id is None
                # A staged revoked version must still be rejected by activation.
                with pytest.raises(SecretError, match="secret_revoked"):
                    await credentials.activate_cluster_version(owner(), second, staged_id)
                await session.rollback()
                active = await current.identity(owner(), first)
                await credentials.revoke(owner(), active)
                with pytest.raises(SecretError, match="secret_revoked"):
                    await current.identity(owner(), first)
                await session.rollback()
        finally:
            await database.close()

    settings = key_settings(auth_settings)
    database = None
    with audit_context("system:test-capabilities"):
        asyncio.run(scenario())


def test_runtime_and_ingress_projection_and_large_metadata(certificates: Certificates) -> None:
    """Owner-visible metadata is retained while whole-object trimming bounds responses."""

    async def scenario() -> None:
        replies = {
            "/apis": Reply(200, b'{"groups":[]}'),
            "/api/v1": Reply(200, b'{"resources":[]}'),
            "/api/v1/nodes": Reply(
                200,
                json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {
                                    "name": "pi-1",
                                    "labels": {"role": "worker"},
                                    "annotations": {"example.test/rack": "rack-1"},
                                },
                                "spec": {
                                    "taints": [
                                        {
                                            "key": "dedicated",
                                            "value": "apps",
                                            "effect": "NoSchedule",
                                        }
                                    ]
                                },
                                "status": {
                                    "nodeInfo": {
                                        "architecture": "arm64",
                                        "operatingSystem": "linux",
                                        "osImage": "Debian 13",
                                        "kernelVersion": "6.12",
                                    },
                                    "conditions": [{"type": "MemoryPressure", "status": "False"}],
                                    "capacity": {
                                        "cpu": "4",
                                        "memory": "8Gi",
                                        "ephemeral-storage": "128Gi",
                                    },
                                    "allocatable": {"cpu": "3500m", "memory": "7Gi"},
                                },
                            }
                        ]
                    }
                ).encode(),
            ),
            "/apis/storage.k8s.io/v1/storageclasses": Reply(200, b'{"items":[]}'),
            "/apis/networking.k8s.io/v1/ingressclasses": Reply(
                200,
                json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {
                                    "name": "traefik",
                                    "annotations": {
                                        "ingressclass.kubernetes.io/is-default-class": "true"
                                    },
                                },
                                "spec": {"controller": "traefik.io/ingress-controller"},
                            }
                        ]
                    }
                ).encode(),
            ),
            "/apis/node.k8s.io/v1/runtimeclasses": Reply(
                200,
                json.dumps(
                    {
                        "items": [
                            {
                                "metadata": {"name": "sandbox"},
                                "handler": "sandbox",
                                "scheduling": {
                                    "nodeSelector": {"runtime": "sandbox"},
                                    "tolerations": [
                                        {
                                            "key": "dedicated",
                                            "operator": "Exists",
                                            "effect": "NoSchedule",
                                        }
                                    ],
                                },
                            }
                        ]
                    }
                ).encode(),
            ),
        }
        async with TLSFixture(certificates, override=replies).running() as peer:
            value = await CapabilityClient().collect(
                config_for(peer, certificates), uuid4(), uuid4(), datetime.now(UTC)
            )
            node = value.nodes.items[0]
            assert node.name == "pi-1" and node.labels["role"] == "worker"
            assert node.annotations["example.test/rack"] == "rack-1"
            assert node.taints[0].effect == "NoSchedule"
            assert node.os_image == "Debian 13" and node.kernel_version == "6.12"
            assert node.capacity["ephemeral-storage"] == "128Gi"
            assert node.allocatable["cpu"] == "3500m"
            assert value.ingress.items[0].default and value.runtimes.items[0].handler == "sandbox"

    asyncio.run(scenario())


def test_unreachable_discovery_and_projection_validation(
    certificates: Certificates,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transport errors are fixed diagnostics; malformed selected fields fail per section."""

    async def broken(self: ApiClient, *args: object, **kwargs: object) -> None:
        raise OSError("fixture-private")

    monkeypatch.setattr(ApiClient, "call_api", broken)

    async def scenario() -> None:
        async with TLSFixture(certificates).running() as peer:
            value = await CapabilityClient().collect(
                config_for(peer, certificates), uuid4(), uuid4(), datetime.now(UTC)
            )
            assert value.nodes.reason == "unreachable"
            assert "fixture-private" not in value.model_dump_json()

    asyncio.run(scenario())


def test_large_metadata_is_bounded_and_invalid_projection_is_isolated() -> None:
    """Bound complete selected snapshots and isolate invalid public resource fields."""
    from tinypods_core.clusters.capabilities import Groups, Resources, Version

    class Client(CapabilityClient):
        def __init__(self, *, invalid: bool = False) -> None:
            super().__init__()
            self.invalid = invalid

        async def read[T: BaseModel](
            self,
            client: ApiClient,
            path: str,
            model: type[T],
            continuation: str | None = None,
        ) -> T:
            if path.endswith("/nodes"):
                return model.model_validate(
                    {
                        "metadata": {"continue": "next"},
                        "items": [
                            {
                                "metadata": {
                                    "name": "pi-" + str(n),
                                    "annotations": {"large": "x" * 80000},
                                },
                                "status": {
                                    "nodeInfo": {
                                        "architecture": "x" * 500 if self.invalid else "arm64",
                                        "operatingSystem": "linux",
                                    }
                                },
                            }
                            for n in range(10)
                        ],
                    }
                )
            if model is Groups:
                return model.model_validate({"groups": []})
            if model is Resources:
                return model.model_validate({"resources": []})
            if model is Version:
                return model.model_validate({"gitVersion": "v1.36.5"})
            return model.model_validate({"items": []})

    async def scenario() -> None:
        result = await Client().collect(
            Kubeconfig("https://example.test"), uuid4(), uuid4(), datetime.now(UTC)
        )
        assert len(result.model_dump_json().encode()) <= 1048576
        assert result.nodes.items and not result.nodes.complete
        invalid = await Client(invalid=True).collect(
            Kubeconfig("https://example.test"), uuid4(), uuid4(), datetime.now(UTC)
        )
        assert invalid.nodes.reason == "invalid-response"

    asyncio.run(scenario())


def test_discovery_slot_saturation_is_bounded(auth_settings: Settings) -> None:
    """Unavailable cache locks are irrelevant, but process-local overload is bounded."""
    from tinypods_core.secrets.crypto import SecretError

    async def scenario() -> None:
        database = Database(key_settings(auth_settings))
        coordinator = DiscoveryCoordinator()
        try:
            cluster, _ = await cluster_records(database)
            async with database.session() as session:
                credentials = service(session, key_settings(auth_settings))
                await credentials.save_cluster_version(owner(), cluster, kubeconfig())
                for _ in range(4):
                    await coordinator.slots.acquire()
                try:
                    with pytest.raises(SecretError, match="cluster_discovery_busy"):
                        await CapabilityService(credentials, None, coordinator).get(
                            owner(), cluster
                        )
                finally:
                    for _ in range(4):
                        coordinator.slots.release()
        finally:
            await database.close()

    with audit_context("system:test-capabilities"):
        asyncio.run(scenario())
