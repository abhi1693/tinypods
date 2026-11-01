"""Dedicated metadata registry for core-owned persistence."""

from typing import ClassVar

from sqlalchemy import MetaData
from sqlmodel import SQLModel

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
