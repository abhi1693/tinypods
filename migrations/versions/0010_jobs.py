"""Persist minimal application-wide jobs."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010_jobs"
down_revision = "0009_application_installations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("task", sa.String(128), nullable=False),
        sa.Column("data", JSONB(), nullable=False),
        sa.Column("target_type", sa.String(128), nullable=True),
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column(
            "requested_by_id",
            sa.Uuid(),
            sa.ForeignKey("auth_user.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_jobs"),
        sa.CheckConstraint("task ~ '^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$'", name="task"),
        sa.CheckConstraint(
            "jsonb_typeof(data) = 'object' AND octet_length(data::text) <= 16384", name="data"
        ),
        sa.CheckConstraint("(target_type IS NULL) = (target_id IS NULL)", name="target_pair"),
        sa.CheckConstraint(
            "status IN ('pending','queued','running','succeeded','failed','cancelled')",
            name="status",
        ),
        sa.CheckConstraint("started IS NULL OR started >= created", name="started"),
        sa.CheckConstraint("completed IS NULL OR completed >= created", name="completed"),
    )
    op.create_index("ix_jobs_pending", "jobs", ["status", "id"])
    op.create_index("ix_jobs_target_history", "jobs", ["target_type", "target_id", "created", "id"])


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT FROM jobs)")):
        raise RuntimeError("Job history prevents downgrade")
    op.drop_table("jobs")
