"""Core-owned persistence models; plugin metadata belongs to each plugin."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Column, DateTime, Index, UniqueConstraint, func
from sqlmodel import Field
from tinypods_core.audit.mixin import AuditTrailMixin
from tinypods_core.persistence import CoreModel as CoreModel  # noqa: F401
from tinypods_core.persistence import core_metadata as core_metadata


class Installation(CoreModel, table=True):
    """One persistent identity shared by this installation's processes."""

    __tablename__ = "platform_installation"
    __table_args__ = (CheckConstraint("singleton = 1", name="singleton"),)

    singleton: int = Field(default=1, primary_key=True)
    owner_id: UUID | None = Field(default=None, foreign_key="auth_user.id", unique=True)
    installation_id: UUID = Field(default_factory=uuid4, nullable=False, unique=True)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()),
    )


class User(AuditTrailMixin, table=True):
    """Store provider-independent core identities; no plaintext passwords or default credentials."""

    __tablename__ = "auth_user"
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    username: str = Field(max_length=64, unique=True)
    is_owner: bool = False


class AuthSession(AuditTrailMixin, table=True):
    """Store only digests of random session/CSRF secrets and absolute expiry."""

    __tablename__ = "auth_session"
    id: UUID = Field(default_factory=uuid4, unique=True, nullable=False)
    __table_args__ = (
        Index("ix_auth_session_client_sid", "external_client_id", "external_session_id"),
        Index("ix_auth_session_client_subject", "external_client_id", "external_subject"),
    )
    token_digest: str = Field(primary_key=True, max_length=64, repr=False)
    user_id: UUID = Field(foreign_key="auth_user.id", index=True)
    csrf_digest: str = Field(max_length=64, repr=False)
    source_provider: str = "local"
    external_issuer: str | None = Field(default=None, max_length=2048)
    external_subject: str | None = Field(default=None, max_length=255)
    external_client_id: str | None = Field(default=None, max_length=255)
    external_session_id: str | None = Field(default=None, max_length=255)
    provider_authenticated_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    external_grant_id: UUID | None = Field(
        default=None, foreign_key="auth_provider_grant.id", index=True
    )
    expires_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    revoked_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


class LocalCredential(AuditTrailMixin, table=True):
    """Attach a local password to an identity; external identities need no such row."""

    __tablename__ = "auth_local_credential"
    user_id: UUID = Field(primary_key=True, foreign_key="auth_user.id")
    password_hash: str = Field(repr=False)


class ExternalIdentity(AuditTrailMixin, table=True):
    """Map only verified issuer/subject pairs; email and provider roles never link accounts."""

    __tablename__ = "auth_external_identity"
    __table_args__ = (UniqueConstraint("issuer", "subject"),)
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    issuer: str = Field(max_length=2048)
    subject: str = Field(max_length=255)
    user_id: UUID = Field(foreign_key="auth_user.id")


class LoginTransaction(AuditTrailMixin, table=True):
    """Keep browser-bound OIDC challenges server-side until atomic single-use consumption."""

    __tablename__ = "auth_login_transaction"
    id: UUID = Field(default_factory=uuid4, unique=True, nullable=False)
    state_digest: str = Field(primary_key=True, max_length=64, repr=False)
    browser_digest: str = Field(max_length=64, repr=False)
    nonce: str = Field(repr=False)
    verifier: str = Field(repr=False)
    configuration_digest: str = Field(max_length=64)
    return_to: str
    previous_session_digest: str | None = Field(default=None, repr=False)
    previous_refresh_digest: str | None = Field(default=None, repr=False)
    expires_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))


class ProviderGrant(AuditTrailMixin, table=True):
    """Own one browser's bounded, encrypted provider refresh-token family."""

    __tablename__ = "auth_provider_grant"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'refreshing', 'revoked')", name="status"),
        CheckConstraint("generation >= 0", name="generation"),
        Index("ix_auth_provider_grant_client_sid", "client_id", "session_id"),
        Index("ix_auth_provider_grant_client_subject", "client_id", "subject"),
    )
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    user_id: UUID = Field(foreign_key="auth_user.id", index=True)
    issuer: str = Field(max_length=2048)
    client_id: str = Field(max_length=255)
    subject: str = Field(max_length=255)
    session_id: str | None = Field(default=None, max_length=255)
    encrypted_state: str | None = Field(default=None, repr=False)
    configuration_digest: str = Field(max_length=64, repr=False)
    status: str = "active"
    generation: int = 0
    authenticated_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    expires_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))


class RefreshCredential(AuditTrailMixin, table=True):
    """Keep refresh/CSRF digests and consumed credentials for reuse detection."""

    __tablename__ = "auth_refresh_credential"
    id: UUID = Field(default_factory=uuid4, unique=True, nullable=False)
    token_digest: str = Field(primary_key=True, max_length=64, repr=False)
    csrf_digest: str = Field(max_length=64, repr=False)
    grant_id: UUID = Field(foreign_key="auth_provider_grant.id", index=True)
    consumed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


class ProviderLogoutEvent(AuditTrailMixin, table=True):
    """Fence in-flight login and deduplicate verified logout events without storing JWTs."""

    __tablename__ = "auth_provider_logout_event"
    id: UUID = Field(default_factory=uuid4, unique=True, nullable=False)
    __table_args__ = (
        Index("ix_auth_provider_logout_event_client_sid", "client_id", "session_id"),
        Index("ix_auth_provider_logout_event_client_subject", "client_id", "subject"),
        Index("ix_auth_provider_logout_event_expiry", "expires_at"),
    )
    event_digest: str = Field(primary_key=True, max_length=64, repr=False)
    issuer: str = Field(max_length=2048)
    client_id: str = Field(max_length=255)
    subject: str | None = Field(default=None, max_length=255)
    session_id: str | None = Field(default=None, max_length=255)
    issued_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    expires_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))


from tinypods_core.audit import capture as _capture  # noqa: E402, F401
from tinypods_core.audit.registry import ModelRegistration, register  # noqa: E402

CORE_AUDIT_MODELS = (
    (User, ("id", "username", "is_owner"), "id"),
    (
        AuthSession,
        ("id", "user_id", "source_provider", "expires_at", "revoked_at", "external_grant_id"),
        "id",
    ),
    (LocalCredential, ("user_id",), "user_id"),
    (ExternalIdentity, ("id", "user_id"), "id"),
    (LoginTransaction, ("id", "expires_at"), "id"),
    (
        ProviderGrant,
        ("id", "user_id", "status", "generation", "authenticated_at", "expires_at"),
        "id",
    ),
    (RefreshCredential, ("id", "grant_id", "consumed_at"), "id"),
    (ProviderLogoutEvent, ("id", "issued_at", "expires_at"), "id"),
)

for _model, _fields, _identity in CORE_AUDIT_MODELS:
    register(
        ModelRegistration(
            _model,
            "auth",
            _model.__name__,
            _fields,
            identity=_identity,
            related=("user_id", User)
            if _model in (AuthSession, LocalCredential, ExternalIdentity, ProviderGrant)
            else None,
        )
    )

from tinypods_core.applications import models as _applications  # noqa: E402, F401
from tinypods_core.repositories import models as _repositories  # noqa: E402, F401
