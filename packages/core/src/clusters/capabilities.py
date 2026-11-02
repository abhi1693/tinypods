"""Typed capability snapshots and bounded native-async Kubernetes discovery."""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID

import aiohttp
import anyio
from kubernetes.aio.client import ApiClient
from kubernetes.aio.client.exceptions import ApiException
from pydantic import BaseModel, ConfigDict, Field, field_serializer
from tinypods_core.json_types import JsonValue
from tinypods_core.secrets.crypto import SecretError
from tinypods_core.secrets.kubernetes import Kubeconfig, build_sdk_client, reject_redirect


class CapabilityModel(BaseModel):
    """Validate public selected fields and reject unrecognized cache schema fields."""

    model_config = ConfigDict(extra="forbid", strict=True)


class TaintCapability(CapabilityModel):
    """Describe a scheduling taint without flattening key, value and effect."""

    key: str = Field(max_length=253)
    value: str = Field(default="", max_length=253)
    effect: Literal["NoSchedule", "PreferNoSchedule", "NoExecute"]
    time_added: datetime | None = None


class NodeCapability(CapabilityModel):
    """Describe named node metadata, runtime details and resource budgets."""

    name: str = Field(max_length=253)
    architecture: str = Field(max_length=128)
    operating_system: str = Field(max_length=128)
    os_image: str | None = Field(default=None, max_length=1024)
    kernel_version: str | None = Field(default=None, max_length=253)
    labels: dict[str, str] = Field(default_factory=dict, max_length=1024)
    annotations: dict[str, str] = Field(default_factory=dict, max_length=1024)
    taints: list[TaintCapability] = Field(default_factory=list, max_length=100)
    capacity: dict[str, str] = Field(default_factory=dict, max_length=100)
    allocatable: dict[str, str] = Field(default_factory=dict, max_length=100)
    ready: bool
    schedulable: bool


class StorageCapability(CapabilityModel):
    """Describe advertised storage behavior without claiming provisioning succeeds."""

    name: str = Field(max_length=253)
    provisioner: str = Field(max_length=253)
    default: bool
    allow_expansion: bool
    binding_mode: str | None = Field(default=None, max_length=128)


class IngressCapability(CapabilityModel):
    """Describe a configured ingress class, not verified external routing."""

    name: str = Field(max_length=253)
    controller: str = Field(max_length=253)
    default: bool


class RuntimeCapability(CapabilityModel):
    """Describe runtime selection and scheduling constraints, not verified isolation."""

    name: str = Field(max_length=253)
    handler: str = Field(max_length=253)
    node_selector: dict[str, str]
    tolerations: list[dict[str, JsonValue]]


class ApiCapability(CapabilityModel):
    """Describe an advertised API resource."""

    group_version: str = Field(max_length=253)
    name: str = Field(max_length=253)
    kind: str = Field(max_length=253)
    namespaced: bool


class CapabilitySection[T](CapabilityModel):
    """Carry a bounded partial result and explicit collection outcome."""

    state: Literal["available", "unsupported", "forbidden", "unavailable", "not-collected"]
    reason: Literal[
        "collected",
        "unsupported",
        "forbidden",
        "unauthorized",
        "timeout",
        "unreachable",
        "invalid-response",
        "limit",
    ] = "collected"
    complete: bool = True
    items: list[T] = Field(default_factory=list, max_length=2000)


class CapabilitySnapshot(CapabilityModel):
    """Store only rebuildable selected facts for a specific cluster credential."""

    schema_version: Literal[1] = 1
    cluster_id: UUID
    credential_version_id: UUID
    started_at: datetime
    collected_at: datetime
    kubernetes: CapabilitySection[str]
    apis: CapabilitySection[ApiCapability]
    nodes: CapabilitySection[NodeCapability]
    storage: CapabilitySection[StorageCapability]
    ingress: CapabilitySection[IngressCapability]
    runtimes: CapabilitySection[RuntimeCapability]

    @field_serializer("started_at", "collected_at")
    def serialize_time(self, value: datetime) -> str:
        """Use fixed-width UTC timestamps for atomic cross-process ordering."""
        return value.astimezone(UTC).isoformat(timespec="microseconds")


class CapabilityRead(CapabilityModel):
    """Describe result provenance independently of the collected snapshot."""

    source: Literal["cache", "live"]
    cached: bool
    snapshot: CapabilitySnapshot


class UpstreamModel(BaseModel):
    """Ignore unneeded upstream fields; project only explicit public fields."""

    model_config = ConfigDict(extra="ignore")


class ObjectMeta(UpstreamModel):
    """Read only bounded Kubernetes names and annotations used for default classes."""

    name: str = Field(max_length=253)
    annotations: dict[str, str] = Field(default_factory=dict, max_length=100)


class ListMeta(UpstreamModel):
    """Retain the opaque server pagination token for the same list only."""

    continuation: str = Field(default="", alias="continue", max_length=4096)


class ObjectList[T](UpstreamModel):
    """Bound a page even if a server ignores the requested limit."""

    metadata: ListMeta = Field(default_factory=ListMeta)
    items: list[T] = Field(max_length=20)


class Condition(UpstreamModel):
    """Select only node readiness conditions."""

    type: str
    status: str


class NodeInfo(UpstreamModel):
    """Select architecture and operating system reported by a node."""

    architecture: str
    operatingSystem: str
    osImage: str | None = None
    kernelVersion: str | None = None


class NodeStatus(UpstreamModel):
    """Select scheduling-relevant reported facts."""

    nodeInfo: NodeInfo
    capacity: dict[str, str] = Field(default_factory=dict, max_length=100)
    allocatable: dict[str, str] = Field(default_factory=dict, max_length=100)
    conditions: list[Condition] = Field(default_factory=list, max_length=100)


class Taint(UpstreamModel):
    """Read upstream scheduling taints including optional eviction start time."""

    key: str
    value: str = ""
    effect: Literal["NoSchedule", "PreferNoSchedule", "NoExecute"]
    timeAdded: datetime | None = None


class NodeMeta(UpstreamModel):
    """Read explicitly requested owner-visible scheduling metadata."""

    name: str = Field(max_length=253)
    labels: dict[str, str] = Field(default_factory=dict, max_length=1024)
    annotations: dict[str, str] = Field(default_factory=dict, max_length=1024)


class NodeSpec(UpstreamModel):
    """Read administrative scheduling and taints separately from readiness."""

    unschedulable: bool = False
    taints: list[Taint] = Field(default_factory=list, max_length=100)


class Node(UpstreamModel):
    """Select the node sections used by discovery."""

    metadata: NodeMeta
    status: NodeStatus
    spec: NodeSpec = Field(default_factory=NodeSpec)


class Storage(UpstreamModel):
    """Select storage-class metadata and provisioning hints."""

    metadata: ObjectMeta
    provisioner: str
    allowVolumeExpansion: bool = False
    volumeBindingMode: str | None = None


class IngressSpec(UpstreamModel):
    """Select the ingress controller identifier."""

    controller: str


class Ingress(UpstreamModel):
    """Select an ingress class."""

    metadata: ObjectMeta
    spec: IngressSpec


class Scheduling(UpstreamModel):
    """Preserve only explicit runtime scheduling constraints."""

    nodeSelector: dict[str, str] = Field(default_factory=dict, max_length=100)
    tolerations: list[dict[str, JsonValue]] = Field(default_factory=list, max_length=100)


class Runtime(UpstreamModel):
    """Select a runtime class and its scheduling constraints."""

    metadata: ObjectMeta
    handler: str
    scheduling: Scheduling = Field(default_factory=Scheduling)


class Version(UpstreamModel):
    """Read the bounded Kubernetes version without storing an arbitrary upstream body."""

    gitVersion: str = Field(
        pattern=r"^v?[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$", max_length=64
    )


class GroupVersion(UpstreamModel):
    """Read one safe advertised group/version identifier."""

    groupVersion: str = Field(pattern=r"^[a-z0-9.-]+/v[0-9][a-z0-9]*$", max_length=253)


class Group(UpstreamModel):
    """Bound advertised versions for one API group."""

    versions: list[GroupVersion] = Field(max_length=20)


class Groups(UpstreamModel):
    """Bound group discovery before expanding resource requests."""

    groups: list[Group] = Field(max_length=100)


class Resource(UpstreamModel):
    """Read one advertised resource shape."""

    name: str = Field(max_length=253)
    kind: str = Field(max_length=253)
    namespaced: bool


class Resources(UpstreamModel):
    """Bound each API resource discovery response."""

    resources: list[Resource] = Field(max_length=500)


class DiscoveryFailure(Exception):
    """Carry only fixed safe diagnostics, never upstream confidential bodies."""

    def __init__(self, reason: str) -> None:
        """Retain only a fixed public diagnostic category."""
        self.reason = reason
        super().__init__(reason)


class CapabilityClient:
    """Collect bounded read-only sections through the official native async SDK."""

    def __init__(self, *, request_timeout: float = 5, section_timeout: float = 30) -> None:
        """Retain bounded operation deadlines; callers own trusted client configuration."""
        self.request_timeout = request_timeout
        self.section_timeout = section_timeout

    async def read[T: BaseModel](
        self, client: ApiClient, path: str, model: type[T], continuation: str | None = None
    ) -> T:
        """Serialize authenticated SDK requests and close bounded response streams."""
        query = [] if continuation is None else [("limit", 20), ("continue", continuation)]
        args = await client.param_serialize(
            method="GET",
            resource_path=path,
            query_params=query,
            header_params={"Accept": "application/json"},
            auth_settings=["BearerToken"],
        )
        try:
            with anyio.fail_after(self.request_timeout):
                wrapped = await client.call_api(
                    method=args[0],
                    url=args[1],
                    header_params=args[2],
                    body=args[3],
                    post_params=args[4],
                    _request_timeout=float(self.request_timeout),
                    _preload_content=False,
                )
                response = cast(aiohttp.ClientResponse, wrapped.response)
                try:
                    if response.status != 200:
                        reasons = {401: "unauthorized", 403: "forbidden", 404: "unsupported"}
                        raise DiscoveryFailure(reasons.get(response.status, "invalid-response"))
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise DiscoveryFailure("invalid-response")
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(16384):
                        body.extend(chunk)
                        if len(body) > 1048576:
                            raise DiscoveryFailure("limit")
                    return model.model_validate_json(body)
                finally:
                    response.close()
        except TimeoutError:
            raise DiscoveryFailure("timeout") from None
        except (ValueError, RecursionError, SecretError):
            raise DiscoveryFailure("invalid-response") from None
        except (aiohttp.ClientError, ApiException, OSError):
            raise DiscoveryFailure("unreachable") from None

    async def listing[T: BaseModel](
        self, client: ApiClient, path: str, model: type[ObjectList[T]]
    ) -> tuple[list[T], bool]:
        """Follow at most five bounded pages, without silently claiming completeness."""
        items: list[T] = []
        continuation = ""
        for _ in range(5):
            page = await self.read(client, path, model, continuation)
            items.extend(page.items)
            continuation = page.metadata.continuation
            if not continuation:
                return items, True
        return items, False

    async def collect(
        self, config: Kubeconfig, cluster_id: UUID, credential_id: UUID, started_at: datetime
    ) -> CapabilitySnapshot:
        """Collect independent sections concurrently within one owned SDK session."""
        client = await anyio.to_thread.run_sync(build_sdk_client, config)
        trace = aiohttp.TraceConfig()
        trace.on_request_redirect.append(reject_redirect)
        client.configuration.trace_configs = [trace]
        semaphore = asyncio.Semaphore(3)

        async def section[T](
            loader: Callable[[], Awaitable[tuple[list[T], bool]]],
        ) -> CapabilitySection[T]:
            """Isolate section failures while preserving task cancellation and total deadlines."""
            try:
                with anyio.fail_after(self.section_timeout):
                    async with semaphore:
                        items, complete = await loader()
                return CapabilitySection(
                    state="available",
                    items=items,
                    complete=complete,
                    reason="collected" if complete else "limit",
                )
            except DiscoveryFailure as exc:
                state = {"unsupported": "unsupported", "forbidden": "forbidden"}.get(
                    exc.reason, "unavailable"
                )
                return CapabilitySection.model_validate(
                    {"state": state, "reason": exc.reason, "complete": False}
                )
            except TimeoutError:
                return CapabilitySection(state="unavailable", reason="timeout", complete=False)
            except ValueError:
                return CapabilitySection(
                    state="unavailable", reason="invalid-response", complete=False
                )

        async def version() -> tuple[list[str], bool]:
            """Read only the constrained upstream version."""
            value = await self.read(client, "/version/", Version)
            return [value.gitVersion], True

        async def nodes() -> tuple[list[NodeCapability], bool]:
            """Do not equate administrative schedulability with available resources."""
            values, complete = await self.listing(client, "/api/v1/nodes", ObjectList[Node])
            return [
                NodeCapability(
                    name=node.metadata.name,
                    architecture=node.status.nodeInfo.architecture,
                    operating_system=node.status.nodeInfo.operatingSystem,
                    os_image=node.status.nodeInfo.osImage,
                    kernel_version=node.status.nodeInfo.kernelVersion,
                    labels=node.metadata.labels,
                    annotations=node.metadata.annotations,
                    taints=[
                        TaintCapability(
                            key=t.key, value=t.value, effect=t.effect, time_added=t.timeAdded
                        )
                        for t in node.spec.taints
                    ],
                    capacity=node.status.capacity,
                    allocatable=node.status.allocatable,
                    ready=any(
                        c.type == "Ready" and c.status == "True" for c in node.status.conditions
                    ),
                    schedulable=not node.spec.unschedulable,
                )
                for node in values
            ], complete

        async def storage() -> tuple[list[StorageCapability], bool]:
            """Select default annotations deliberately instead of copying annotations."""
            values, complete = await self.listing(
                client, "/apis/storage.k8s.io/v1/storageclasses", ObjectList[Storage]
            )
            return [
                StorageCapability(
                    name=value.metadata.name,
                    provisioner=value.provisioner,
                    default=value.metadata.annotations.get(
                        "storageclass.kubernetes.io/is-default-class"
                    )
                    == "true",
                    allow_expansion=value.allowVolumeExpansion,
                    binding_mode=value.volumeBindingMode,
                )
                for value in values
            ], complete

        async def ingress() -> tuple[list[IngressCapability], bool]:
            """Read class configuration without testing routes or provisioning ingress."""
            values, complete = await self.listing(
                client, "/apis/networking.k8s.io/v1/ingressclasses", ObjectList[Ingress]
            )
            return [
                IngressCapability(
                    name=value.metadata.name,
                    controller=value.spec.controller,
                    default=value.metadata.annotations.get(
                        "ingressclass.kubernetes.io/is-default-class"
                    )
                    == "true",
                )
                for value in values
            ], complete

        async def runtimes() -> tuple[list[RuntimeCapability], bool]:
            """Keep runtime scheduling requirements separate from node readiness."""
            values, complete = await self.listing(
                client, "/apis/node.k8s.io/v1/runtimeclasses", ObjectList[Runtime]
            )
            return [
                RuntimeCapability(
                    name=value.metadata.name,
                    handler=value.handler,
                    node_selector=value.scheduling.nodeSelector,
                    tolerations=value.scheduling.tolerations,
                )
                for value in values
            ], complete

        async def apis() -> tuple[list[ApiCapability], bool]:
            """Bound API fan-out and never interpolate an unvalidated discovery path."""
            groups = await self.read(client, "/apis", Groups)
            versions = sorted({v.groupVersion for g in groups.groups for v in g.versions})
            complete = len(versions) <= 31
            values: list[ApiCapability] = []
            for group_version in ["v1", *versions[:31]]:
                path = "/api/v1" if group_version == "v1" else "/apis/" + group_version
                resources = await self.read(client, path, Resources)
                values.extend(
                    ApiCapability(
                        group_version=group_version,
                        name=r.name,
                        kind=r.kind,
                        namespaced=r.namespaced,
                    )
                    for r in resources.resources
                )
                if len(values) > 2000:
                    return values[:2000], False
            return values, complete

        async with client:
            (
                kubernetes,
                api_values,
                node_values,
                storage_values,
                ingress_values,
                runtime_values,
            ) = await asyncio.gather(
                section(version),
                section(apis),
                section(nodes),
                section(storage),
                section(ingress),
                section(runtimes),
            )
        snapshot = CapabilitySnapshot(
            cluster_id=cluster_id,
            credential_version_id=credential_id,
            started_at=started_at,
            collected_at=datetime.now(UTC),
            kubernetes=kubernetes,
            apis=api_values,
            nodes=node_values,
            storage=storage_values,
            ingress=ingress_values,
            runtimes=runtime_values,
        )

        # Owner-requested node metadata can be substantial. Trim only whole objects
        # and report incompleteness rather than silently truncating individual values.
        while len(snapshot.model_dump_json().encode()) > 1048576:
            sections: list[
                CapabilitySection[ApiCapability]
                | CapabilitySection[NodeCapability]
                | CapabilitySection[StorageCapability]
                | CapabilitySection[IngressCapability]
                | CapabilitySection[RuntimeCapability]
            ] = [
                snapshot.apis,
                snapshot.nodes,
                snapshot.storage,
                snapshot.ingress,
                snapshot.runtimes,
            ]
            largest = max(sections, key=lambda item: len(item.model_dump_json()))
            del largest.items[len(largest.items) // 2 :]
            largest.complete = False
            largest.reason = "limit"
        return snapshot
