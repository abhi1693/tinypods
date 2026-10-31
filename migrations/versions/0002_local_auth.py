"""Add local accounts and revocable sessions."""

import sqlalchemy as sa
from alembic import op

revision = "0002_local_auth"
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


def downgrade() -> None:
    op.drop_table("auth_session")
    op.drop_constraint(
        "fk_platform_installation_owner_id_auth_user", "platform_installation", type_="foreignkey"
    )
    op.drop_constraint("uq_platform_installation_owner_id", "platform_installation", type_="unique")
    op.drop_column("platform_installation", "owner_id")
    op.drop_table("auth_local_credential")
    op.drop_table("auth_user")
