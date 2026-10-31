"""Refresh rotation, logout scoping and concurrency on disposable PostgreSQL and Valkey."""

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import text
from test_auth import auth_settings as auth_settings
from test_auth import credentials, local_auth
from test_oidc_flow import ISSUER, SUBJECT, FakeProvider, Runtime, enabled, runtime, state_from
from tinypods_core.auth import AuthError, Login
from tinypods_core.auth.contracts import LogoutNotice, VerifiedIdentity
from tinypods_core.auth.providers import LocalPasswordProvider
from tinypods_core.auth.providers.zitadel.encryption import RefreshCipher
from tinypods_core.auth.providers.zitadel.sessions import ZitadelSessions
from tinypods_core.auth.tokens import digest
from tinypods_core.db.models import AuthSession, ProviderGrant, RefreshCredential
from tinypods_core.rate_limit import RateDecision, RatePolicy
from tinypods_core.settings import Settings


class LifecycleProvider(FakeProvider):
    """Gate provider I/O while exercising real durable rotation and revocation."""

    def __init__(self) -> None:
        """Retain only disposable test controls and provider secrets."""
        super().__init__()
        self.started, self.gate = asyncio.Event(), asyncio.Event()
        self.block = False
        self.failure: BaseException | None = None
        self.notice: LogoutNotice | None = None
        self.refresh_calls = 0
        self.session_id = "provider-session"
        self.auth_time = time.time() - 60
        self.identity_override: VerifiedIdentity | None = None

    async def exchange(self, code: str, nonce: str, verifier: str) -> VerifiedIdentity:
        """Model a verified login carrying server-only refresh material."""
        return VerifiedIdentity(
            ISSUER,
            SUBJECT,
            self.session_id,
            SecretStr("upstream-private-one"),
            nonce,
            self.auth_time,
            time.time(),
        )

    async def refresh(
        self,
        refresh_token: SecretStr,
        nonce: str,
        *,
        subject: str,
        session_id: str | None,
        auth_time: float | None,
    ) -> VerifiedIdentity:
        """Observe one claim, then model success, cancellation or ambiguous failure."""
        self.refresh_calls += 1
        self.started.set()
        if self.block:
            await self.gate.wait()
        if self.failure is not None:
            raise self.failure
        return self.identity_override or VerifiedIdentity(
            ISSUER,
            subject,
            session_id,
            SecretStr("upstream-private-rotated-" + str(self.refresh_calls)),
            nonce,
            auth_time,
            time.time(),
        )

    async def validate_logout_token(self, encoded: str) -> LogoutNotice:
        """Stand in only for the separately tested signed protocol validation boundary."""
        if encoded == "invalid":
            raise AuthError(400)
        assert self.notice is not None
        return self.notice


@dataclass
class LifecycleRuntime:
    """Collect owned disposable resources and the issued browser login."""

    active: Runtime
    provider: LifecycleProvider
    service: ZitadelSessions
    login: Login
    settings: Settings


@asynccontextmanager
async def lifecycle(settings: Settings) -> AsyncIterator[LifecycleRuntime]:
    """Bootstrap an isolated owner and provision a non-owner through normal browser login."""
    configured = enabled(
        settings,
        zitadel_refresh_enabled=True,
        zitadel_refresh_encryption_keys=(SecretStr(Fernet.generate_key().decode()),),
    )
    async with runtime(configured) as active:
        await LocalPasswordProvider(active.database).bootstrap(credentials())
        provider = LifecycleProvider()
        active.browser.provider = provider
        service = ZitadelSessions(
            active.database,
            configured,
            provider,
            active.browser.sessions,
            active.browser.configuration_digest,
            active.limiter,
        )
        active.browser.lifecycle = service
        start = await active.browser.start()
        login, _ = await active.browser.finish(
            state_from(start.authorization_url), start.browser_token, "accepted"
        )
        assert login.refresh_token is not None and login.refresh_expires_at is not None
        yield LifecycleRuntime(active, provider, service, login, configured)


async def grant_for(context: LifecycleRuntime, login: Login) -> ProviderGrant:
    """Inspect persisted state through digest-only browser credential lookup."""
    async with context.active.database.session() as session:
        credential = await session.get(RefreshCredential, digest(login.refresh_token or ""))
        assert credential is not None
        grant = await session.get(ProviderGrant, credential.grant_id)
        assert grant is not None
        return grant


def test_refresh_rotation_storage_and_reuse_revoke_family(auth_settings: Settings) -> None:
    """Never persist plaintext tokens; rotate all browser credentials and reject reuse."""

    async def scenario() -> None:
        async with lifecycle(auth_settings) as context:
            initial = context.login
            grant = await grant_for(context, initial)
            assert grant.encrypted_state is not None
            assert "upstream-private-one" not in grant.encrypted_state
            assert initial.refresh_token not in grant.encrypted_state
            renewed = await context.service.refresh(initial.refresh_token or "", initial.csrf_token)
            assert renewed.refresh_token != initial.refresh_token
            assert renewed.token != initial.token and renewed.csrf_token != initial.csrf_token
            assert renewed.refresh_expires_at == initial.refresh_expires_at
            assert not renewed.principal.is_owner
            assert (
                await context.active.browser.sessions.authenticate(renewed.token)
                == renewed.principal
            )
            with pytest.raises(AuthError):
                await context.active.browser.sessions.authenticate(initial.token)
            persisted = await grant_for(context, renewed)
            assert persisted.generation == 1 and persisted.encrypted_state is not None
            state = RefreshCipher(context.settings).decrypt(persisted.encrypted_state, persisted.id)
            assert state.refresh_token.get_secret_value() == "upstream-private-rotated-1"
            with pytest.raises(AuthError) as error:
                await context.service.refresh(initial.refresh_token or "", initial.csrf_token)
            assert error.value.status == 401
            assert (await grant_for(context, renewed)).status == "revoked"
            assert (await grant_for(context, renewed)).encrypted_state is None
            with pytest.raises(AuthError):
                await context.active.browser.sessions.authenticate(renewed.token)
            assert context.provider.refresh_calls == 1

    asyncio.run(scenario())


def test_refresh_works_after_access_expiry_and_logout_clears_family(
    auth_settings: Settings,
) -> None:
    """Renewal can outlive an access session, within the absolute family expiry."""

    async def scenario() -> None:
        async with lifecycle(auth_settings) as context:
            async with context.active.database.transaction() as session:
                row = await session.get(AuthSession, digest(context.login.token))
                assert row is not None
                row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
                session.add(row)
            with pytest.raises(AuthError):
                await context.active.browser.sessions.authenticate(context.login.token)
            renewed = await context.service.refresh(
                context.login.refresh_token or "", context.login.csrf_token
            )
            assert (
                await context.active.browser.sessions.authenticate(renewed.token)
                == renewed.principal
            )
            await context.active.browser.sessions.logout_browser(
                "", renewed.csrf_token, renewed.refresh_token
            )
            with pytest.raises(AuthError):
                await context.service.refresh(renewed.refresh_token or "", renewed.csrf_token)
            assert (await grant_for(context, renewed)).status == "revoked"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure", [AuthError(401), AuthError(503), RuntimeError("opaque failure")]
)
def test_refresh_failure_revokes_consumed_family(
    auth_settings: Settings, failure: BaseException
) -> None:
    """Never retry a provider token whose exchange may already have rotated it upstream."""

    async def scenario() -> None:
        async with lifecycle(auth_settings) as context:
            context.provider.failure = failure
            with pytest.raises(type(failure)):
                await context.service.refresh(
                    context.login.refresh_token or "", context.login.csrf_token
                )
            assert (await grant_for(context, context.login)).status == "revoked"
            with pytest.raises(AuthError):
                await context.active.browser.sessions.authenticate(context.login.token)

    asyncio.run(scenario())


def test_parallel_refresh_and_backchannel_cannot_resurrect_session(auth_settings: Settings) -> None:
    """Fence one upstream exchange and let logout win while that exchange is in flight."""

    async def scenario() -> None:
        async with lifecycle(auth_settings) as context:
            context.provider.block = True
            first = asyncio.create_task(
                context.service.refresh(context.login.refresh_token or "", context.login.csrf_token)
            )
            await context.provider.started.wait()
            with pytest.raises(AuthError) as error:
                await context.service.refresh(
                    context.login.refresh_token or "", context.login.csrf_token
                )
            assert error.value.status == 409 and context.provider.refresh_calls == 1
            now = datetime.now(UTC)
            context.provider.notice = LogoutNotice(
                ISSUER,
                "test-client",
                str(uuid4()),
                now,
                now + timedelta(minutes=2),
                SUBJECT,
                context.provider.session_id,
            )
            await context.service.backchannel_logout("verified")
            context.provider.gate.set()
            with pytest.raises(AuthError) as error:
                await first
            assert error.value.status == 401
            assert (await grant_for(context, context.login)).status == "revoked"
            with pytest.raises(AuthError):
                await context.active.browser.sessions.authenticate(context.login.token)

    asyncio.run(scenario())


def test_refresh_cancellation_and_logout_during_refresh(auth_settings: Settings) -> None:
    """Durably revoke on cancellation and independently revoke a family during provider I/O."""

    async def scenario() -> None:
        async with lifecycle(auth_settings) as context:
            context.provider.block = True
            request = asyncio.create_task(
                context.service.refresh(context.login.refresh_token or "", context.login.csrf_token)
            )
            await context.provider.started.wait()
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            assert (await grant_for(context, context.login)).status == "revoked"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mutation", ["expired", "configuration", "ciphertext", "missing-state", "disabled"]
)
def test_refresh_policy_and_cipher_failures_revoke_family(
    auth_settings: Settings, mutation: str
) -> None:
    """Reject expired, changed or unauthenticated provider state without issuing credentials."""

    async def scenario() -> None:
        async with lifecycle(auth_settings) as context:
            grant = await grant_for(context, context.login)
            async with context.active.database.transaction() as session:
                row = await session.get(ProviderGrant, grant.id)
                assert row is not None
                if mutation == "expired":
                    row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
                elif mutation == "ciphertext":
                    row.encrypted_state = "tampered"
                elif mutation == "missing-state":
                    row.encrypted_state = None
                session.add(row)
            if mutation == "configuration":
                context.settings.zitadel_client_id = "changed-client"
            if mutation == "disabled":
                context.settings.zitadel_refresh_enabled = False
            with pytest.raises(AuthError) as error:
                await context.service.refresh(
                    context.login.refresh_token or "", context.login.csrf_token
                )
            assert error.value.status == 401
            assert (await grant_for(context, context.login)).status == "revoked"
            assert context.provider.refresh_calls == 0

    asyncio.run(scenario())


def test_refresh_csrf_rejection_does_not_consume_credentials(auth_settings: Settings) -> None:
    """Reject bad CSRF, absent and unknown handles before mutation or upstream I/O."""

    async def scenario() -> None:
        async with lifecycle(auth_settings) as context:
            for token, csrf, status in (
                ("", "", 401),
                ("x" * 129, "", 401),
                ("unknown", "", 401),
                (context.login.refresh_token or "", "wrong", 403),
            ):
                with pytest.raises(AuthError) as error:
                    await context.service.refresh(token, csrf)
                assert error.value.status == status
            assert (await grant_for(context, context.login)).status == "active"
            assert context.provider.refresh_calls == 0
            renewed = await context.service.refresh(
                context.login.refresh_token or "", context.login.csrf_token
            )
            assert renewed.refresh_token is not None

    asyncio.run(scenario())


def test_backchannel_session_scope_subject_scope_and_idempotency(auth_settings: Settings) -> None:
    """Keep local and other-provider sessions live and make repeated notices harmless."""

    async def scenario() -> None:
        async with lifecycle(auth_settings) as context:
            user_id = context.login.principal.user_id
            identity = VerifiedIdentity(
                ISSUER,
                SUBJECT,
                "other-session",
                SecretStr("other-secret"),
                "n" * 43,
                context.provider.auth_time,
                time.time(),
            )
            other = await context.service.issue(
                identity, user_id, previous_digest=None, previous_refresh_digest=None
            )
            local = await context.active.browser.sessions.issue(user_id)
            now = datetime.now(UTC)
            notice = LogoutNotice(
                ISSUER,
                "test-client",
                str(uuid4()),
                now,
                now + timedelta(minutes=2),
                SUBJECT,
                context.provider.session_id,
            )
            await context.service.apply_logout(notice)
            with pytest.raises(AuthError):
                await context.active.browser.sessions.authenticate(context.login.token)
            assert (
                await context.active.browser.sessions.authenticate(other.token) == other.principal
            )
            assert (
                await context.active.browser.sessions.authenticate(local.token) == local.principal
            )
            await context.service.apply_logout(notice)
            subject_notice = replace(notice, event_id=str(uuid4()), session_id=None)
            await context.service.apply_logout(subject_notice)
            with pytest.raises(AuthError):
                await context.active.browser.sessions.authenticate(other.token)
            fresh = replace(identity, auth_time=now.timestamp() + 1, issued_at=now.timestamp() + 1)
            after = await context.service.issue(
                fresh, user_id, previous_digest=None, previous_refresh_digest=None
            )
            await context.service.apply_logout(subject_notice)
            assert (
                await context.active.browser.sessions.authenticate(after.token) == after.principal
            )
            assert (
                await context.active.browser.sessions.authenticate(local.token) == local.principal
            )

    asyncio.run(scenario())


def test_logout_fences_late_login_and_rejects_invalid_notification(auth_settings: Settings) -> None:
    """A verified logout prevents a matching old authentication from issuing new cookies."""

    async def scenario() -> None:
        async with lifecycle(auth_settings) as context:
            now = datetime.now(UTC)
            notice = LogoutNotice(
                ISSUER,
                "test-client",
                str(uuid4()),
                now,
                now + timedelta(minutes=2),
                SUBJECT,
                context.provider.session_id,
            )
            await context.service.apply_logout(notice)
            identity = VerifiedIdentity(
                ISSUER,
                SUBJECT,
                context.provider.session_id,
                None,
                "n" * 43,
                context.provider.auth_time,
                time.time(),
            )
            with pytest.raises(AuthError) as error:
                await context.service.issue(
                    identity,
                    context.login.principal.user_id,
                    previous_digest=None,
                    previous_refresh_digest=None,
                )
            assert error.value.status == 401
            with pytest.raises(AuthError) as error:
                await context.service.backchannel_logout("invalid")
            assert error.value.status == 400

    asyncio.run(scenario())


def test_abandoned_refresh_claim_requires_sign_in_instead_of_retrying_provider_token(
    auth_settings: Settings,
) -> None:
    """A crashed refresh stays fail closed and a stale claim cannot reuse its upstream token."""

    async def scenario() -> None:
        async with lifecycle(auth_settings) as context:
            await context.service._claim(
                context.login.refresh_token or "", context.login.csrf_token
            )
            with pytest.raises(AuthError):
                await context.active.browser.sessions.authenticate(context.login.token)
            async with context.active.database.transaction() as session:
                credential = await session.get(
                    RefreshCredential, digest(context.login.refresh_token or "")
                )
                assert credential is not None
                credential.consumed_at = datetime.now(UTC) - timedelta(seconds=31)
                session.add(credential)
            with pytest.raises(AuthError) as error:
                await context.service.refresh(
                    context.login.refresh_token or "", context.login.csrf_token
                )
            assert error.value.status == 401 and context.provider.refresh_calls == 0
            assert (await grant_for(context, context.login)).status == "revoked"

    asyncio.run(scenario())


def test_local_logout_wins_over_in_flight_refresh(auth_settings: Settings) -> None:
    """Sign-out can revoke a consumed refresh credential while its upstream call is pending."""

    async def scenario() -> None:
        async with lifecycle(auth_settings) as context:
            context.provider.block = True
            task = asyncio.create_task(
                context.service.refresh(context.login.refresh_token or "", context.login.csrf_token)
            )
            await context.provider.started.wait()
            await context.active.browser.sessions.logout_browser(
                context.login.token, context.login.csrf_token, context.login.refresh_token
            )
            context.provider.gate.set()
            with pytest.raises(AuthError):
                await task
            assert (await grant_for(context, context.login)).status == "revoked"

    asyncio.run(scenario())


def test_optional_refresh_and_missing_nonce_fail_closed(auth_settings: Settings) -> None:
    """Login can omit renewal material, but renewal material cannot omit its original nonce."""

    async def scenario() -> None:
        async with lifecycle(auth_settings) as context:
            identity = VerifiedIdentity(ISSUER, SUBJECT)
            no_refresh = await context.service.issue(
                identity,
                context.login.principal.user_id,
                previous_digest=None,
                previous_refresh_digest=None,
            )
            assert no_refresh.refresh_token is None
            broken = replace(identity, refresh_token=SecretStr("provider-token"))
            with pytest.raises(AuthError) as error:
                await context.service.issue(
                    broken,
                    context.login.principal.user_id,
                    previous_digest=None,
                    previous_refresh_digest=None,
                )
            assert error.value.status == 401
            now = datetime.now(UTC)
            notice = LogoutNotice(
                ISSUER, "test-client", str(uuid4()), now, now + timedelta(minutes=2), SUBJECT
            )
            await context.service.apply_logout(notice)
            with pytest.raises(AuthError):
                await context.service.issue(
                    replace(identity, auth_time=now.timestamp() - 1),
                    context.login.principal.user_id,
                    previous_digest=None,
                    previous_refresh_digest=None,
                )

    asyncio.run(scenario())


def test_lifecycle_requires_installation_and_matching_logout_context(
    auth_settings: Settings,
) -> None:
    """Reject unknown logout contexts and missing installation state before applying changes."""

    async def scenario() -> None:
        async with lifecycle(auth_settings) as context:
            now = datetime.now(UTC)
            valid = LogoutNotice(
                ISSUER, "test-client", str(uuid4()), now, now + timedelta(minutes=2), SUBJECT
            )
            for notice in (
                replace(valid, issuer="https://other.example"),
                replace(valid, client_id="other-client"),
                replace(valid, subject=None),
            ):
                with pytest.raises(AuthError) as error:
                    await context.service.apply_logout(notice)
                assert error.value.status == 400
            await context.service.apply_logout(
                replace(valid, subject=None, session_id="unrelated-session")
            )
            async with context.active.database.transaction() as session:
                await session.exec(text("DELETE FROM platform_installation"))
            for operation in (
                context.service.apply_logout(valid),
                context.service.issue(
                    VerifiedIdentity(ISSUER, SUBJECT),
                    context.login.principal.user_id,
                    previous_digest=None,
                    previous_refresh_digest=None,
                ),
            ):
                with pytest.raises(AuthError) as error:
                    await operation
                assert error.value.status == 503

    asyncio.run(scenario())


def test_refresh_rate_limits_are_mandatory_and_do_not_consume_on_denial(
    auth_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No provider refresh is attempted without a successful shared reservation."""

    async def denied(policy: RatePolicy, subject: str) -> RateDecision:
        return RateDecision(False, 17)

    async def scenario() -> None:
        async with lifecycle(auth_settings) as context:
            context.service.limiter = None
            with pytest.raises(AuthError) as error:
                await context.service.refresh(
                    context.login.refresh_token or "", context.login.csrf_token
                )
            assert error.value.status == 503
            context.service.limiter = context.active.limiter
            monkeypatch.setattr(context.active.limiter, "reserve", denied)
            with pytest.raises(AuthError) as error:
                await context.service.refresh(
                    context.login.refresh_token or "", context.login.csrf_token
                )
            assert error.value.status == 429 and error.value.retry_after == 17
            assert (await grant_for(context, context.login)).status == "active"
            assert context.provider.refresh_calls == 0

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field,value",
    [("issuer", "https://other.example"), ("subject", "other-user"), ("refresh_token", None)],
)
def test_refresh_completion_rejects_changed_identity_or_missing_credentials(
    auth_settings: Settings, field: str, value: object
) -> None:
    """The commit boundary independently verifies the provider result before issuing cookies."""

    async def scenario() -> None:
        async with lifecycle(auth_settings) as context:
            identity = VerifiedIdentity(
                ISSUER,
                SUBJECT,
                context.provider.session_id,
                SecretStr("new-token"),
                "n" * 43,
                context.provider.auth_time,
                time.time(),
            )
            context.provider.identity_override = replace(identity, **{field: value})
            with pytest.raises(AuthError):
                await context.service.refresh(
                    context.login.refresh_token or "", context.login.csrf_token
                )
            assert (await grant_for(context, context.login)).status == "revoked"

    asyncio.run(scenario())


def test_new_login_revokes_old_refresh_family_even_without_access_cookie(
    auth_settings: Settings,
) -> None:
    """Switching identity cannot leave an expired browser's renewal credential active."""

    async def scenario() -> None:
        async with lifecycle(auth_settings) as context:
            auth = local_auth(context.active.database, context.active.limiter)
            logged_in = await auth.login(
                credentials(), previous_refresh_digest=digest(context.login.refresh_token or "")
            )
            assert logged_in.principal.is_owner and logged_in.refresh_token is None
            assert (await grant_for(context, context.login)).status == "revoked"
            again = await auth.login(
                credentials(), previous_refresh_digest=digest("unknown-old-cookie")
            )
            assert again.principal.is_owner
            async with context.active.database.transaction() as session:
                await context.active.browser.sessions.revoke_grant(
                    session, uuid4(), datetime.now(UTC)
                )

    asyncio.run(scenario())


def test_reauthentication_revokes_the_old_provider_family(auth_settings: Settings) -> None:
    """A fresh verified provider login replaces the entire previous browser grant."""

    async def scenario() -> None:
        async with lifecycle(auth_settings) as context:
            identity = VerifiedIdentity(
                ISSUER,
                SUBJECT,
                "new-session",
                SecretStr("new-provider-token"),
                "n" * 43,
                time.time(),
                time.time(),
            )
            new = await context.service.issue(
                identity,
                context.login.principal.user_id,
                previous_digest=digest(context.login.token),
                previous_refresh_digest=digest(context.login.refresh_token or ""),
            )
            assert new.refresh_token is not None
            assert (await grant_for(context, context.login)).status == "revoked"
            assert (await grant_for(context, new)).status == "active"

    asyncio.run(scenario())


def test_refresh_context_missing_grant_fails_closed() -> None:
    """A malformed storage result must never turn a refresh handle into an identity."""
    from tinypods_core.auth.sessions import SessionService

    class MissingResult:
        def one_or_none(self) -> None:
            """Simulate a damaged or filtered-out grant row."""
            return None

    class MissingGrantSession:
        async def get(self, model: object, key: str) -> RefreshCredential:
            """Return a credential whose parent cannot be read."""
            return RefreshCredential(
                token_digest=digest("handle"), csrf_digest=digest("csrf"), grant_id=uuid4()
            )

        async def exec(self, statement: object) -> MissingResult:
            """Model the failed parent lookup without weakening a real foreign key."""
            return MissingResult()

    with pytest.raises(AuthError) as error:
        asyncio.run(SessionService.refresh_context(MissingGrantSession(), "handle", "csrf"))
    assert error.value.status == 401
