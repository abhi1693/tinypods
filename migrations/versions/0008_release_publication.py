"""Add independent application/release lifecycle states without additional tables."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_release_publication"
down_revision = "0007_cluster_credentials"
branch_labels = None
depends_on = None

PROTECT_RELEASE = """
CREATE OR REPLACE FUNCTION protect_published_release() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.published_at IS NOT NULL THEN
      RAISE EXCEPTION 'Published release history is immutable' USING ERRCODE='23514';
    END IF;
    RETURN OLD;
  END IF;
  IF OLD.published_at IS NOT NULL AND ROW(
      NEW.application_id, NEW.chart_version_id, NEW.revision, NEW.configuration_fields,
      NEW.architectures, NEW.renderer, NEW.renderer_contract,
      NEW.core_version_min, NEW.core_version_max, NEW.published_at
    ) IS DISTINCT FROM ROW(
      OLD.application_id, OLD.chart_version_id, OLD.revision, OLD.configuration_fields,
      OLD.architectures, OLD.renderer, OLD.renderer_contract,
      OLD.core_version_min, OLD.core_version_max, OLD.published_at
    ) THEN
    RAISE EXCEPTION 'Published deployment fields are immutable' USING ERRCODE='23514';
  END IF;
  IF OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at THEN
    RAISE EXCEPTION 'Release revocation is permanent' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END $$;
"""


def upgrade():
    op.add_column(
        "applications",
        sa.Column("state", sa.String(length=16), nullable=False, server_default="draft"),
    )
    op.create_check_constraint(
        op.f("ck_applications_state"),
        "applications",
        "state IN ('draft', 'in_review', 'published')",
    )
    for column in (
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="draft"),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "architectures",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("renderer", sa.String(), nullable=False, server_default="helm"),
        sa.Column("renderer_contract", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("core_version_min", sa.String(), nullable=True),
        sa.Column("core_version_max", sa.String(), nullable=True),
        sa.Column("deprecated", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lifecycle_reason", sa.Text(), nullable=False, server_default=""),
    ):
        op.add_column("releases", column)
    op.drop_constraint("uq_releases_application_id", "releases", type_="unique")
    op.create_unique_constraint(
        "uq_releases_chart_revision", "releases", ["application_id", "chart_version_id", "revision"]
    )
    for name, condition in (
        ("revision", "revision > 0"),
        ("state", "state IN ('draft', 'in_review', 'published')"),
        ("published_at", "state != 'published' OR published_at IS NOT NULL"),
        ("architectures", "jsonb_typeof(architectures) = 'array'"),
    ):
        op.create_check_constraint(op.f("ck_releases_" + name), "releases", condition)
    op.execute(PROTECT_RELEASE)
    op.execute(
        "CREATE TRIGGER protect_published_release BEFORE UPDATE OR DELETE ON releases "
        "FOR EACH ROW EXECUTE FUNCTION protect_published_release()"
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS(SELECT FROM releases WHERE published_at IS NOT NULL)")
    ):
        raise RuntimeError("Published release history prevents downgrade")
    op.execute("DROP TRIGGER protect_published_release ON releases")
    op.execute("DROP FUNCTION protect_published_release()")
    for name in ("architectures", "state", "revision", "published_at"):
        op.drop_constraint(op.f("ck_releases_" + name), "releases", type_="check")
    op.drop_constraint("uq_releases_chart_revision", "releases", type_="unique")
    # Existing revisions are never discarded to satisfy the old unique key.
    op.create_unique_constraint(
        "uq_releases_application_id", "releases", ["application_id", "chart_version_id"]
    )
    for name in (
        "lifecycle_reason",
        "revoked_at",
        "deprecated",
        "core_version_max",
        "core_version_min",
        "renderer_contract",
        "renderer",
        "architectures",
        "notes",
        "published_at",
        "state",
        "revision",
    ):
        op.drop_column("releases", name)
    op.drop_constraint(op.f("ck_applications_state"), "applications", type_="check")
    op.drop_column("applications", "state")
