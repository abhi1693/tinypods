"""Application definitions and constraints against disposable PostgreSQL."""

from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from conftest import PostgresEndpoints
from pydantic import SecretStr, ValidationError
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlmodel import Session, select
from tinypods_core.applications import (
    Application,
    ApplicationCategory,
    ApplicationStatus,
    Category,
    ConfigurationField,
    Release,
    SourceType,
)
from tinypods_core.audit import AuditRecord
from tinypods_core.audit.registry import registry
from tinypods_core.db.migrations import MigrationManager
from tinypods_core.settings import Settings

ROOT = Path(__file__).resolve().parents[1]
DIGEST = "sha256:" + "a" * 64


def test_configuration_definition_contract() -> None:
    """Keep supply ownership independent of timing and reject malformed definitions."""
    assert {status.value for status in ApplicationStatus} == {"experimental", "stable", "premium"}
    field = ConfigurationField(label="Replicas", type="number", path="server.replicas", value=2)
    assert field.set_by == "chart_default"
    assert not field.install_only
    for supplier in ("chart_default", "user", "administrator", "auto"):
        for install_only in (False, True):
            definition = ConfigurationField(
                label="Password",
                type="password",
                path="database.password",
                set_by=supplier,
                install_only=install_only,
                sensitive=True,
                required=True,
            )
            assert definition.value is None
            assert definition.set_by == supplier
            assert definition.install_only == install_only
    assert ConfigurationField(
        label="Mode", type="choice", path="mode", choices=["a", "b"]
    ).choices == ["a", "b"]
    for invalid in (
        {"type": "script"},
        {"set_by": "generate"},
        {"path": "a..b"},
        {"path": ["a", "b"]},
        {"label": " "},
        {"targets": ["x"]},
        {"sensitive": True, "value": "secret"},
        {"type": "password", "value": "secret"},
        {"choices": ["one"]},
        {"value": float("inf")},
    ):
        with pytest.raises(ValidationError):
            ConfigurationField.model_validate(
                {"label": "Setting", "type": "text", "path": "a.b", **invalid}
            )


def test_application_migration_constraints_and_audit(postgres_endpoints: PostgresEndpoints) -> None:
    """Migrate existing data, enforce relationships, and retain transactional audit history.

    Args:
        postgres_endpoints: Isolated infrastructure; never operator databases.
    """
    with psycopg.connect(postgres_endpoints.direct, autocommit=True) as connection:
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")
    manager = MigrationManager(
        Settings(database_url=SecretStr(postgres_endpoints.direct)), ROOT / "alembic.ini"
    )
    command.upgrade(manager.config(), "0003_audit_records")
    engine = create_engine(
        postgres_endpoints.direct.replace("postgresql://", "postgresql+psycopg://")
    )
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE operator_data (value text)"))
            connection.execute(text("INSERT INTO operator_data VALUES ('preserved')"))
        manager.upgrade()
        manager.check()
        with Session(engine) as session:
            app = Application(name="Actual", slug="actual")
            other = Application(name="Umami", slug="umami", status=ApplicationStatus.STABLE)
            category = Category(name="Finance", slug="finance", icon="finance.svg")
            session.add_all([app, other, category])
            session.commit()
            app_id, other_id, category_id = app.id, other.id, category.id
            session.add(ApplicationCategory(application_id=app_id, category_id=category_id))
            release = Release(
                application_id=app_id,
                chart_version="1.0.0",
                source_location="https://charts.example.test",
                chart_name="actual",
                digest=DIGEST,
                configuration_fields=[
                    {"label": "Replicas", "type": "number", "path": "server.replicas", "value": 2},
                    {
                        "label": "Password",
                        "type": "password",
                        "path": "database.password",
                        "set_by": "auto",
                        "install_only": True,
                        "sensitive": True,
                    },
                ],
            )
            second = Release(
                application_id=other_id,
                chart_version="1.0.0",
                source_type=SourceType.OCI_REGISTRY,
                source_location="oci://registry.example.test/charts",
                chart_name="umami",
                digest=DIGEST,
            )
            session.add_all([release, second])
            session.commit()
            session.refresh(release)
            release_id = release.id
            before = release.model_dump()
            assert release.configuration_fields[0]["set_by"] == "chart_default"
            assert release.configuration_fields[1]["install_only"] is True
            app.status = ApplicationStatus.PREMIUM
            app.description = "Changed description"
            session.commit()
            session.refresh(release)
            assert release.model_dump() == before
            assert session.get(Application, app_id).status == "premium"
            assert session.get(Category, category_id).icon == "finance.svg"
            event = session.exec(
                select(AuditRecord).where(AuditRecord.changed_object_id == str(release_id))
            ).one()
            assert event.related_object_id == str(app_id)
            assert "configuration_fields" not in event.postchange_data
            # Replacing definitions persists validation and produces an audit event
            # without copying arbitrary configuration literals into history.
            release.configuration_fields = [
                {"label": "Mode", "type": "choice", "path": "mode", "choices": ["a", "b"]}
            ]
            session.commit()
            events = session.exec(
                select(AuditRecord).where(AuditRecord.changed_object_id == str(release_id))
            ).all()
            assert len(events) == 2
            assert all("configuration_fields" not in event.postchange_data for event in events)
            release.chart_name = "rolled-back"
            session.flush()
            session.rollback()
            assert session.get(Release, release_id).chart_name == "actual"
            assert (
                len(
                    session.exec(
                        select(AuditRecord).where(AuditRecord.changed_object_id == str(release_id))
                    ).all()
                )
                == 2
            )
            for malformed in (
                None,
                {},
                [{"label": "Bad", "type": "script", "path": "x"}],
                [{"label": "Password", "type": "password", "path": "x", "value": "secret"}],
            ):
                release.configuration_fields = malformed
                with pytest.raises(StatementError):
                    session.commit()
                session.rollback()
        invalid_sql = [
            ("UPDATE applications SET slug='actual' WHERE id=:other", {"other": other_id}),
            ("UPDATE categories SET slug='Bad Slug'", {}),
            ("UPDATE applications SET status='unknown'", {}),
            ("UPDATE applications SET status='paid_users_only'", {}),
            ("UPDATE applications SET status='beta_users_only'", {}),
            ("UPDATE applications SET status='production_ready'", {}),
            (
                "INSERT INTO application_categories VALUES (:app, :category)",
                {"app": app_id, "category": category_id},
            ),
            (
                "INSERT INTO application_categories VALUES (:app, :category)",
                {"app": uuid4(), "category": category_id},
            ),
            (
                "INSERT INTO application_categories VALUES (:app, :category)",
                {"app": app_id, "category": uuid4()},
            ),
            (
                "UPDATE releases SET application_id=:app WHERE application_id=:other",
                {"app": app_id, "other": other_id},
            ),
            ("UPDATE releases SET application_id=:missing", {"missing": uuid4()}),
            ("UPDATE releases SET source_type='git'", {}),
            ("UPDATE releases SET digest='latest'", {}),
            ("UPDATE releases SET chart_version=' '", {}),
            ("UPDATE releases SET configuration_fields='{}'::jsonb", {}),
            ("DELETE FROM applications WHERE id=:id", {"id": app_id}),
            ("DELETE FROM categories WHERE id=:id", {"id": category_id}),
        ]
        for statement, params in invalid_sql:
            with engine.begin() as connection, pytest.raises(IntegrityError):
                connection.execute(text(statement), params)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT value FROM operator_data")) == "preserved"
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM object_types WHERE app_label='applications'")
                )
                == 3
            )
        command.downgrade(manager.config(), "0003_audit_records")
        manager.upgrade()
        manager.check()
        assert registry[Application].app_label == "applications"
    finally:
        engine.dispose()
