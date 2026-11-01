"""Task-local, explicit actor and operation correlation for durable history."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from uuid import UUID, uuid4


@dataclass(frozen=True)
class AuditContext:
    """Trusted actor attribution shared by writes in one request or job."""

    request_id: UUID
    user_name: str
    user_id: UUID | None = None


current_context: ContextVar[AuditContext | None] = ContextVar("audit_context", default=None)


@contextmanager
def audit_context(
    user_name: str, *, user_id: UUID | None = None, request_id: UUID | None = None
) -> Iterator[AuditContext]:
    """Bind a trusted actor for an API request, CLI invocation or worker job.

    Args:
        user_name: Safe username or explicit system attribution, never raw input.
        user_id: Verified core user identity, absent for system work.
        request_id: Server-owned correlation UUID; generated when omitted.

    Yields:
        Immutable context restored on exit, including cancellation and errors.
    """
    context = AuditContext(request_id or uuid4(), user_name[:128], user_id)
    token = current_context.set(context)
    try:
        yield context
    finally:
        current_context.reset(token)


def bind_user(user_id: UUID, user_name: str) -> None:
    """Attribute subsequent request writes to an already verified principal.

    Args:
        user_id: Verified core identity.
        user_name: Safe username copied from that identity.
    """
    context = current_context.get()
    if context is not None:
        current_context.set(replace(context, user_id=user_id, user_name=user_name[:128]))
