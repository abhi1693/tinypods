"""First-user and explicit operator ownership on isolated real PostgreSQL stores."""

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlmodel import select
from test_auth import auth_settings as auth_settings
from test_auth import credentials
from test_oidc_flow import ISSUER, enabled
from tinypods_cli.main import app as cli
from tinypods_core.auth import AuthError, Scope, SessionService
from tinypods_core.auth.contracts import VerifiedIdentity
from tinypods_core.auth.federation import IdentityMappings
from tinypods_core.auth.ownership import OwnerAccess
from tinypods_core.auth.providers.local.provider import LocalPasswordProvider
from tinypods_core.db import Database
from tinypods_core.db.models import Installation, User
from tinypods_core.settings import Settings
from typer.testing import CliRunner


@pytest.mark.parametrize("mixed", [False, True])
def test_first_user_bootstrap_serializes_providers(auth_settings: Settings, mixed: bool) -> None:
    """Concurrent first sign-ins select exactly one owner across both providers."""

    async def scenario() -> None:
        settings = enabled(auth_settings)
        database = Database(settings)
        try:
            mappings = IdentityMappings(database, settings)
            external = mappings.resolve(VerifiedIdentity(ISSUER, "first"))
            other = (
                LocalPasswordProvider(database).bootstrap(credentials())
                if mixed
                else mappings.resolve(VerifiedIdentity(ISSUER, "second"))
            )
            results = await asyncio.gather(external, other, return_exceptions=True)
            assert isinstance(results[0], UUID)
            if isinstance(results[1], AuthError):
                assert mixed and results[1].status == 409
            async with database.session() as session:
                users = (await session.exec(select(User))).all()
                owners = [user for user in users if user.is_owner]
                assert len(owners) == 1
                instance = await session.get(Installation, 1)
                assert instance is not None and instance.owner_id == owners[0].id
            later = await mappings.resolve(VerifiedIdentity(ISSUER, "later"))
            assert await mappings.resolve(VerifiedIdentity(ISSUER, "later")) == later
            async with database.session() as session:
                user = await session.get(User, later)
                assert user is not None and not user.is_owner
            with pytest.raises(AuthError) as error:
                await LocalPasswordProvider(database).bootstrap(credentials("another"))
            assert error.value.status == 409
        finally:
            await database.close()

    asyncio.run(scenario())


def test_operator_owner_changes_revalidate_existing_sessions(auth_settings: Settings) -> None:
    """Grant/revoke is audited, preserves a final owner and applies to existing sessions."""

    async def scenario() -> None:
        database = Database(auth_settings)
        try:
            first = await LocalPasswordProvider(database).bootstrap(credentials())
            async with database.transaction() as session:
                user = User(username="second")
                session.add(user)
                await session.flush()
                second = user.id
            access = OwnerAccess(database)
            sessions = SessionService(database)
            login = await sessions.issue(second)
            with pytest.raises(AuthError) as error:
                await sessions.require_access(login.principal, Scope.CATALOG_ADMIN)
            assert error.value.status == 403
            await access.set_owner(second, enabled=True)
            with pytest.raises(AuthError) as error:
                await sessions.require_access(login.principal, Scope.CATALOG_ADMIN)
            assert error.value.status == 403
            current = await sessions.authenticate(login.token)
            assert (await sessions.require_access(current, Scope.CATALOG_ADMIN)).is_owner
            await access.set_owner(second, enabled=True)
            await access.set_owner(second, enabled=False)
            await access.set_owner(second, enabled=False)
            with pytest.raises(AuthError) as error:
                await sessions.require_access(login.principal, Scope.CATALOG_ADMIN)
            assert error.value.status == 403
            with pytest.raises(AuthError) as error:
                await access.set_owner(first, enabled=False)
            assert error.value.status == 409
            with pytest.raises(AuthError) as error:
                await access.set_owner(uuid4(), enabled=True)
            assert error.value.status == 404
            await access.set_owner(second, enabled=True)
            results = await asyncio.gather(
                access.set_owner(first, enabled=False),
                access.set_owner(second, enabled=False),
                return_exceptions=True,
            )
            errors = [result for result in results if isinstance(result, AuthError)]
            assert len(errors) == 1 and errors[0].status == 409
            async with database.session() as session:
                owners = (await session.exec(select(User).where(User.is_owner))).all()
                assert len(owners) == 1
                instance = await session.get(Installation, 1)
                assert instance is not None and instance.owner_id == first
                events = (
                    await session.exec(
                        text(
                            "SELECT count(*) FROM audit_records WHERE action = 'update' "
                            "AND postchange_data ? 'is_owner'"
                        )
                    )
                ).scalar_one()
                assert events > 0
            async with database.transaction() as session:
                await session.exec(text("DELETE FROM platform_installation"))
            with pytest.raises(AuthError) as error:
                await access.set_owner(second, enabled=True)
            assert error.value.status == 503
        finally:
            await database.close()

    asyncio.run(scenario())


def test_operator_owner_cli(auth_settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI performs explicit audited grants/revocations and reports unknown identities."""
    monkeypatch.setenv("TINYPODS_DATABASE_URL", auth_settings.database_url.get_secret_value())

    async def seed() -> UUID:
        database = Database(auth_settings)
        try:
            await LocalPasswordProvider(database).bootstrap(credentials())
            async with database.transaction() as session:
                user = User(username="second")
                session.add(user)
                await session.flush()
                return user.id
        finally:
            await database.close()

    target = str(asyncio.run(seed()))
    runner = CliRunner()
    granted = runner.invoke(cli, ["auth", "owner", target])
    assert granted.exit_code == 0 and "access granted" in granted.output
    revoked = runner.invoke(cli, ["auth", "owner", target, "--revoke"])
    assert revoked.exit_code == 0 and "access revoked" in revoked.output
    missing = runner.invoke(cli, ["auth", "owner", str(uuid4())])
    assert missing.exit_code != 0 and "404" in missing.output
