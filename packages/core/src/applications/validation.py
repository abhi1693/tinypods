"""Bounded validation of existing release fields and contained chart definitions."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from uuid import UUID

import anyio
from anyio import to_process
from pydantic import ValidationError
from semver import Version
from tinypods_core.applications.bundles import inspect_bundle
from tinypods_core.applications.configuration import configuration_overrides, validate_values_schema
from tinypods_core.applications.contracts import (
    ReleaseOptions,
    ReleaseValidationIssue,
)
from tinypods_core.applications.helm_values import (
    flatten_charts,
    resolve_values,
)
from tinypods_core.json_types import JsonRecord
from tinypods_core.repositories.sources import SourceError
from tinypods_core.repositories.verification import ChartPin


@dataclass(frozen=True)
class ReleaseDraft:
    """Detached deployment fields read before external verification or CPU work."""

    release_id: UUID
    application_id: UUID
    chart_version_id: UUID
    revision: int
    configuration_fields: list[JsonRecord] = field(repr=False)
    options: JsonRecord
    core_version: str
    chart: ChartPin


@dataclass(frozen=True)
class ValidatedDefinition:
    """Safe validation issues without duplicating deployment data."""

    issues: tuple[ReleaseValidationIssue, ...]


def compatibility_issues(
    options: ReleaseOptions, current_version: str
) -> list[ReleaseValidationIssue]:
    """Check declared architectures and the version bounds supported by this core."""
    issues = []
    if not options.architectures or len(set(options.architectures)) != len(options.architectures):
        issues.append(ReleaseValidationIssue(code="invalid_architectures", path=["architectures"]))
    try:
        current = Version.parse(current_version)
        minimum = Version.parse(options.core_version_min) if options.core_version_min else None
        maximum = Version.parse(options.core_version_max) if options.core_version_max else None
        if (
            (minimum and maximum and minimum > maximum)
            or (minimum and current < minimum)
            or (maximum and current > maximum)
        ):
            issues.append(
                ReleaseValidationIssue(
                    code="incompatible_core_version", path=["core_version_min", "core_version_max"]
                )
            )
    except ValueError:
        issues.append(
            ReleaseValidationIssue(
                code="invalid_core_version", path=["core_version_min", "core_version_max"]
            )
        )
    return issues


def validate_definition(body: bytes, draft: ReleaseDraft) -> ValidatedDefinition:
    """Inspect contained charts, schemas and field defaults; return no raw error values.

    This trusted CPU-only entry point runs in a cancellable worker process. Public
    charts can contain pathological regular expressions or recursive schemas, so
    a worker thread alone cannot enforce the validation deadline.
    """
    try:
        options = ReleaseOptions.model_validate(draft.options)
    except ValidationError:
        return ValidatedDefinition((ReleaseValidationIssue(code="invalid_release_options"),))
    issues = compatibility_issues(options, draft.core_version)
    if issues:
        return ValidatedDefinition(tuple(issues))
    try:
        bundle = inspect_bundle(body, draft.chart)
        overrides, fields, issues = configuration_overrides(
            draft.configuration_fields, bundle.facts.defaults
        )
        if issues:
            return ValidatedDefinition(tuple(issues))
        resolved = resolve_values(bundle, overrides)
        for node in flatten_charts(resolved):
            if node.enabled:
                issues.extend(
                    validate_values_schema(node.chart.facts.values_schema, node.values, node.path)
                )
        return ValidatedDefinition(tuple(issues[:20]))
    except SourceError as error:
        return ValidatedDefinition((ReleaseValidationIssue(code=error.code),))


class PublicationValidator:
    """Own one bounded CPU-validation slot and terminate work that exceeds its deadline."""

    def __init__(self, timeout_seconds: float = 10.0) -> None:
        """Retain the complete validation deadline; the limiter binds lazily to its event loop."""
        self.timeout_seconds = timeout_seconds
        self._limiter: anyio.CapacityLimiter | None = None

    @asynccontextmanager
    async def admission(self) -> AsyncIterator[None]:
        """Reserve capacity before downloading; reject overload without retaining archives.

        There is no archive-bearing queue. Callers may retry a busy operation;
        capacity exhaustion does not mean that a release definition is invalid.
        Cancellation or any source/worker failure releases the reservation.
        """
        if self._limiter is None:
            self._limiter = anyio.CapacityLimiter(1)
        try:
            self._limiter.acquire_nowait()
        except anyio.WouldBlock:
            raise SourceError(503, "validation_busy") from None
        try:
            yield
        finally:
            self._limiter.release()

    async def validate(self, body: bytes, draft: ReleaseDraft) -> ValidatedDefinition:
        """Admit a direct CPU validation without queueing its archive."""
        async with self.admission():
            return await self.validate_admitted(body, draft)

    async def validate_admitted(self, body: bytes, draft: ReleaseDraft) -> ValidatedDefinition:
        """Run CPU work within an admission reservation, with its own execution deadline."""
        try:
            with anyio.fail_after(self.timeout_seconds):
                return await to_process.run_sync(
                    validate_definition,
                    body,
                    draft,
                    cancellable=True,
                    limiter=anyio.CapacityLimiter(1),
                )
        except TimeoutError:
            return ValidatedDefinition((ReleaseValidationIssue(code="validation_timeout"),))
