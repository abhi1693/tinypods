"""Explicit resource actions with consistent HTTP contracts and policy composition."""

from collections.abc import Callable
from enum import StrEnum
from typing import Any, TypeVar

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from tinypods_api.auth.dependencies import SESSION_CSRF_METADATA, get_principal
from tinypods_api.cache import CachePolicy
from tinypods_api.http.errors import ERROR_RESPONSES
from tinypods_api.http.operations import build_operation_id
from tinypods_api.rate_limit import EndpointRatePolicy, RateLimitedRoute
from tinypods_core.json_types import JsonRecord

Endpoint = TypeVar("Endpoint", bound=Callable[..., Any])


class ResourceAction(StrEnum):
    """Standard single-resource operations; services retain business ownership."""

    LIST = "list"
    RETRIEVE = "retrieve"
    CREATE = "create"
    REPLACE = "replace"
    UPDATE = "update"
    DELETE = "delete"


ACTION_HTTP = {
    ResourceAction.LIST: ("", "GET", 200),
    ResourceAction.RETRIEVE: ("/{id}", "GET", 200),
    ResourceAction.CREATE: ("", "POST", 201),
    ResourceAction.REPLACE: ("/{id}", "PUT", 200),
    ResourceAction.UPDATE: ("/{id}", "PATCH", 200),
    ResourceAction.DELETE: ("/{id}", "DELETE", 204),
}


class ResourceRouter(APIRouter):
    """Register typed handlers using one resource's common HTTP and security rules.

    Handlers keep normal FastAPI signatures. This router does not infer writable
    fields from database models, authorize objects, execute queries or commit.
    Services must enforce resource access before reading or mutating data.
    """

    def __init__(
        self, *, domain: str, resource: str, prefix: str, public_reads: bool = False
    ) -> None:
        """Retain stable contract names and default to authenticated resource access.

        Args:
            domain: Owning business area used in generated operation identifiers.
            resource: Singular stable resource name used in operation identifiers.
            prefix: Relative plural collection path, mounted under the API version.
            public_reads: Explicitly allow identity-independent collection/detail reads.
        """
        build_operation_id(domain, resource, "list")
        self.domain = domain
        self.resource = resource
        self.public_reads = public_reads
        self._actions: set[ResourceAction] = set()
        super().__init__(
            prefix=prefix, tags=[domain], route_class=RateLimitedRoute, responses=ERROR_RESPONSES
        )

    def action(
        self,
        action: ResourceAction,
        *,
        response_model: type[BaseModel] | None = None,
        cache: CachePolicy | None = None,
        rate: EndpointRatePolicy | None = None,
    ) -> Callable[[Endpoint], Endpoint]:
        """Declare one action with explicit DTOs and safe shared defaults.

        Args:
            action: Standard HTTP operation to register once for this resource.
            response_model: Public response DTO; required except for empty deletes.
            cache: Opt-in shared cache policy for explicitly public reads only.
            rate: Optional endpoint budget replacing the read/write default.

        Returns:
            A signature-preserving decorator registering the typed handler.

        Raises:
            ValueError: Action duplication, missing/unexpected DTO or unsafe caching.
        """
        if action in self._actions:
            raise ValueError("Resource action is already registered")
        path, method, status = ACTION_HTTP[action]
        read = method == "GET"
        protected = not (read and self.public_reads)
        if (status == 204) != (response_model is None):
            raise ValueError("Responses require a DTO except for an empty delete")
        if cache is not None and (protected or not read):
            raise ValueError("Shared caching requires explicitly public reads")
        operation = build_operation_id(self.domain, self.resource, action.value)
        policy = rate or EndpointRatePolicy(name=operation, limit=120 if read else 30)
        extra: JsonRecord = policy.openapi_extra()
        if cache is not None:
            extra.update(cache.openapi_extra())
        if not read:
            extra.update(SESSION_CSRF_METADATA)

        def register(endpoint: Endpoint) -> Endpoint:
            """Keep the original callable signature visible to dependency/schema discovery."""
            self.add_api_route(
                path,
                endpoint,
                methods=[method],
                status_code=status,
                operation_id=operation,
                response_model=response_model,
                dependencies=[Depends(get_principal)] if protected else [],
                openapi_extra=extra,
            )
            self._actions.add(action)
            return endpoint

        return register
