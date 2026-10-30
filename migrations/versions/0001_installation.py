"""Create the installation identity and core migration baseline."""

from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision = "0001_installation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    table = op.create_table(
        "platform_installation",
        sa.Column("singleton", sa.Integer(), nullable=False),
        sa.Column("installation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("singleton = 1", name="ck_platform_installation_singleton"),
        sa.PrimaryKeyConstraint("singleton", name="pk_platform_installation"),
        sa.UniqueConstraint("installation_id", name="uq_platform_installation_installation_id"),
    )
    op.bulk_insert(table, [{"singleton": 1, "installation_id": uuid4()}])


def downgrade() -> None:
    op.drop_table("platform_installation")
