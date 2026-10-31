"""Core-owned persistence models; plugin metadata belongs to each plugin."""

from datetime import UTC, datetime
from typing import ClassVar
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Column, DateTime, MetaData, UniqueConstraint, func
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
    token_digest: str = Field(primary_key=True, max_length=64, repr=False)
    user_id: UUID = Field(foreign_key="auth_user.id", index=True)
    csrf_digest: str = Field(max_length=64, repr=False)
    source_provider: str = "local"
    external_issuer: str | None = Field(default=None, max_length=2048)
    external_subject: str | None = Field(default=None, max_length=255)
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
    expires_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
