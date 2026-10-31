"""Core-owned persistence models; plugin metadata belongs to each plugin."""

from datetime import UTC, datetime
from typing import ClassVar
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Column, DateTime, Index, MetaData, UniqueConstraint, func
from sqlmodel import Field, SQLModel

core_metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_name)s",
        "uq": "uq_%(table_name)s_%(column_0_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)


class CoreModel(SQLModel):
    """Place core persistence tables in their dedicated SQLModel metadata registry."""

    metadata: ClassVar[MetaData] = core_metadata


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


class User(CoreModel, table=True):
    """Store provider-independent core identities; no plaintext passwords or default credentials."""

    __tablename__ = "auth_user"
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    username: str = Field(max_length=64, unique=True)
    is_owner: bool = False


class AuthSession(CoreModel, table=True):
    """Store only digests of random session/CSRF secrets and absolute expiry."""

    __tablename__ = "auth_session"
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


class LocalCredential(CoreModel, table=True):
    """Attach a local password to an identity; external identities need no such row."""

    __tablename__ = "auth_local_credential"
    user_id: UUID = Field(primary_key=True, foreign_key="auth_user.id")
    password_hash: str = Field(repr=False)


class ExternalIdentity(CoreModel, table=True):
    """Map only verified issuer/subject pairs; email and provider roles never link accounts."""

    __tablename__ = "auth_external_identity"
    __table_args__ = (UniqueConstraint("issuer", "subject"),)
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    issuer: str = Field(max_length=2048)
    subject: str = Field(max_length=255)
    user_id: UUID = Field(foreign_key="auth_user.id")


class LoginTransaction(CoreModel, table=True):
    """Keep browser-bound OIDC challenges server-side until atomic single-use consumption."""

    __tablename__ = "auth_login_transaction"
    state_digest: str = Field(primary_key=True, max_length=64, repr=False)
    browser_digest: str = Field(max_length=64, repr=False)
    nonce: str = Field(repr=False)
    verifier: str = Field(repr=False)
    configuration_digest: str = Field(max_length=64)
    return_to: str
    previous_session_digest: str | None = Field(default=None, repr=False)
    previous_refresh_digest: str | None = Field(default=None, repr=False)
    expires_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))


class ProviderGrant(CoreModel, table=True):
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


class RefreshCredential(CoreModel, table=True):
    """Keep refresh/CSRF digests and consumed credentials for reuse detection."""

    __tablename__ = "auth_refresh_credential"
    token_digest: str = Field(primary_key=True, max_length=64, repr=False)
    csrf_digest: str = Field(max_length=64, repr=False)
    grant_id: UUID = Field(foreign_key="auth_provider_grant.id", index=True)
    consumed_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )


class ProviderLogoutEvent(CoreModel, table=True):
    """Fence in-flight login and deduplicate verified logout events without storing JWTs."""

    __tablename__ = "auth_provider_logout_event"
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
