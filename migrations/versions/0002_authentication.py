"""Add supported local and external authentication, sessions and login transactions."""

import sqlalchemy as sa
from alembic import op

revision = "0002_authentication"
down_revision = "0001_installation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_user",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("username", sa.String(64), nullable=False),
        sa.Column("is_owner", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username"),
    )
    op.create_table(
        "auth_local_credential",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("user_id"),
        sa.ForeignKeyConstraint(["user_id"], ["auth_user.id"]),
    )
    op.add_column("platform_installation", sa.Column("owner_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_platform_installation_owner_id_auth_user",
        "platform_installation",
        "auth_user",
        ["owner_id"],
        ["id"],
    )
    op.create_unique_constraint(
        "uq_platform_installation_owner_id", "platform_installation", ["owner_id"]
    )
    op.create_table(
        "auth_session",
        sa.Column("token_digest", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("csrf_digest", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("token_digest"),
        sa.ForeignKeyConstraint(["user_id"], ["auth_user.id"]),
    )
    op.create_index("ix_auth_session_user_id", "auth_session", ["user_id"])
    op.add_column(
        "auth_session",
        sa.Column("source_provider", sa.String(), nullable=False, server_default="local"),
    )
    op.alter_column("auth_session", "source_provider", server_default=None)
    op.add_column("auth_session", sa.Column("external_issuer", sa.String(2048), nullable=True))
    op.add_column("auth_session", sa.Column("external_subject", sa.String(255), nullable=True))
    op.create_table(
        "auth_external_identity",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("issuer", sa.String(2048), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("issuer", "subject"),
        sa.ForeignKeyConstraint(["user_id"], ["auth_user.id"]),
    )
    op.create_table(
        "auth_login_transaction",
        sa.Column("state_digest", sa.String(64), nullable=False),
        sa.Column("browser_digest", sa.String(64), nullable=False),
        sa.Column("nonce", sa.String(), nullable=False),
        sa.Column("verifier", sa.String(), nullable=False),
        sa.Column("configuration_digest", sa.String(64), nullable=False),
        sa.Column("return_to", sa.String(), nullable=False),
        sa.Column("previous_session_digest", sa.String(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("state_digest"),
    )

    op.create_table(
        "auth_provider_grant",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("issuer", sa.String(2048), nullable=False),
        sa.Column("client_id", sa.String(255), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("session_id", sa.String(255), nullable=True),
        sa.Column("encrypted_state", sa.String(), nullable=True),
        sa.Column("configuration_digest", sa.String(64), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("authenticated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["auth_user.id"]),
        sa.CheckConstraint(
            "status IN ('active', 'refreshing', 'revoked')",
            name=op.f("ck_auth_provider_grant_status"),
        ),
        sa.CheckConstraint("generation >= 0", name=op.f("ck_auth_provider_grant_generation")),
    )
    op.create_index("ix_auth_provider_grant_user_id", "auth_provider_grant", ["user_id"])
    op.create_table(
        "auth_refresh_credential",
        sa.Column("token_digest", sa.String(64), nullable=False),
        sa.Column("csrf_digest", sa.String(64), nullable=False),
        sa.Column("grant_id", sa.Uuid(), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("token_digest"),
        sa.ForeignKeyConstraint(["grant_id"], ["auth_provider_grant.id"]),
    )
    op.create_index("ix_auth_refresh_credential_grant_id", "auth_refresh_credential", ["grant_id"])
    op.create_table(
        "auth_provider_logout_event",
        sa.Column("event_digest", sa.String(64), nullable=False),
        sa.Column("issuer", sa.String(2048), nullable=False),
        sa.Column("client_id", sa.String(255), nullable=False),
        sa.Column("subject", sa.String(255), nullable=True),
        sa.Column("session_id", sa.String(255), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_digest"),
    )
    op.add_column("auth_session", sa.Column("external_client_id", sa.String(255), nullable=True))
    op.add_column("auth_session", sa.Column("external_session_id", sa.String(255), nullable=True))
    op.add_column(
        "auth_session",
        sa.Column("provider_authenticated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("auth_session", sa.Column("external_grant_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_auth_session_external_grant_id_auth_provider_grant",
        "auth_session",
        "auth_provider_grant",
        ["external_grant_id"],
        ["id"],
    )
    op.create_index("ix_auth_session_external_grant_id", "auth_session", ["external_grant_id"])
    op.add_column(
        "auth_login_transaction", sa.Column("previous_refresh_digest", sa.String(), nullable=True)
    )

    op.create_index(
        "ix_auth_session_client_sid", "auth_session", ["external_client_id", "external_session_id"]
    )
    op.create_index(
        "ix_auth_session_client_subject", "auth_session", ["external_client_id", "external_subject"]
    )
    op.create_index(
        "ix_auth_provider_grant_client_sid", "auth_provider_grant", ["client_id", "session_id"]
    )
    op.create_index(
        "ix_auth_provider_grant_client_subject", "auth_provider_grant", ["client_id", "subject"]
    )
    op.create_index(
        "ix_auth_provider_logout_event_client_sid",
        "auth_provider_logout_event",
        ["client_id", "session_id"],
    )
    op.create_index(
        "ix_auth_provider_logout_event_client_subject",
        "auth_provider_logout_event",
        ["client_id", "subject"],
    )
    op.create_index(
        "ix_auth_provider_logout_event_expiry", "auth_provider_logout_event", ["expires_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_auth_provider_logout_event_expiry", table_name="auth_provider_logout_event")
    op.drop_index(
        "ix_auth_provider_logout_event_client_subject", table_name="auth_provider_logout_event"
    )
    op.drop_index(
        "ix_auth_provider_logout_event_client_sid", table_name="auth_provider_logout_event"
    )
    op.drop_index("ix_auth_provider_grant_client_subject", table_name="auth_provider_grant")
    op.drop_index("ix_auth_provider_grant_client_sid", table_name="auth_provider_grant")
    op.drop_index("ix_auth_session_client_subject", table_name="auth_session")
    op.drop_index("ix_auth_session_client_sid", table_name="auth_session")
    op.drop_column("auth_login_transaction", "previous_refresh_digest")
    op.drop_index("ix_auth_session_external_grant_id", table_name="auth_session")
    op.drop_constraint(
        "fk_auth_session_external_grant_id_auth_provider_grant", "auth_session", type_="foreignkey"
    )
    op.drop_column("auth_session", "external_grant_id")
    op.drop_column("auth_session", "provider_authenticated_at")
    op.drop_column("auth_session", "external_session_id")
    op.drop_column("auth_session", "external_client_id")
    op.drop_table("auth_provider_logout_event")
    op.drop_table("auth_refresh_credential")
    op.drop_table("auth_provider_grant")
    op.drop_column("auth_session", "external_subject")
    op.drop_column("auth_session", "external_issuer")
    op.drop_column("auth_session", "source_provider")
    op.drop_table("auth_login_transaction")
    op.drop_table("auth_external_identity")
    op.drop_table("auth_session")
    op.drop_constraint(
        "fk_platform_installation_owner_id_auth_user", "platform_installation", type_="foreignkey"
    )
    op.drop_constraint("uq_platform_installation_owner_id", "platform_installation", type_="unique")
    op.drop_column("platform_installation", "owner_id")
    op.drop_table("auth_local_credential")
    op.drop_table("auth_user")
