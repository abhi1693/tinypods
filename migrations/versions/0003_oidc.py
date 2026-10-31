"""Add external identity mappings and single-use server-side OIDC transactions."""

import sqlalchemy as sa
from alembic import op

revision = "0003_oidc"
down_revision = "0002_local_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_column("auth_session", "external_subject")
    op.drop_column("auth_session", "external_issuer")
    op.drop_column("auth_session", "source_provider")
    op.drop_table("auth_login_transaction")
    op.drop_table("auth_external_identity")
