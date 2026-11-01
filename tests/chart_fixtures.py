"""Trusted upstream metadata fixtures for normalized release integration tests."""

from uuid import UUID, uuid4

from sqlmodel import Session
from tinypods_core.db import migration_engine
from tinypods_core.repositories import Chart, ChartRepository, ChartVersion
from tinypods_core.settings import Settings


def chart_version(
    session: Session,
    *,
    source_type: str = "helm_repository",
    source_location: str = "https://charts.example.test",
    chart_name: str = "example",
    version: str = "1.0",
    digest: str = "sha256:" + "a" * 64,
) -> UUID:
    """Insert fixture metadata, never fetching or hosting chart archives."""
    repository = ChartRepository(
        name="fixture-" + uuid4().hex, source_type=source_type, url=source_location
    )
    session.add(repository)
    session.flush()
    chart = Chart(repository_id=repository.id, name=chart_name, available=True)
    session.add(chart)
    session.flush()
    record = ChartVersion(chart_id=chart.id, version=version, digest=digest, available=True)
    session.add(record)
    session.flush()
    return record.id


def seed_source(settings: Settings, **values: str) -> str:
    """Commit only fixture metadata in the disposable database configured by tests."""
    engine = migration_engine(settings)
    try:
        with Session(engine) as session:
            identity = chart_version(session, **values)
            session.commit()
            return str(identity)
    finally:
        engine.dispose()
