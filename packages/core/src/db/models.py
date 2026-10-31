"""Core-owned persistence models; plugin metadata belongs to each plugin."""

from datetime import UTC, datetime
from typing import ClassVar
from uuid import UUID, uuid4

from sqlalchemy import CheckConstraint, Column, DateTime, MetaData, func
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
    installation_id: UUID = Field(default_factory=uuid4, nullable=False, unique=True)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True), nullable=False, server_default=func.now()),
    )
