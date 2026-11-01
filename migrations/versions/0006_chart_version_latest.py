"""Track one latest available chart version without changing release pins."""

import re

import sqlalchemy as sa
from alembic import op
from semver import Version

revision = "0006_chart_version_latest"
down_revision = "0005_chart_repositories"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "chart_versions",
        sa.Column("is_latest", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    connection = op.get_bind()
    latest = {}
    # Freeze ordering here rather than importing the evolving application service.
    # Imported, unverified pins remain unavailable and have no latest pointer.
    for row in connection.execute(
        sa.text("SELECT id, chart_id, version, digest FROM chart_versions WHERE available")
    ):
        parts = re.split(r"(?=[-+])", row.version.removeprefix("v"), maxsplit=1)
        core = parts[0]
        value = Version.parse(core + ".0" * max(0, 3 - len(core.split("."))) + "".join(parts[1:]))
        rank = (value, row.version, row.digest)
        previous = latest.get(row.chart_id)
        if previous is None or rank > previous[0]:
            latest[row.chart_id] = (rank, row.id)
    for _, identity in latest.values():
        connection.execute(
            sa.text("UPDATE chart_versions SET is_latest = true WHERE id = :id"), {"id": identity}
        )
    op.create_check_constraint(
        op.f("ck_chart_versions_latest_available"), "chart_versions", "NOT is_latest OR available"
    )
    op.create_index(
        "uq_chart_versions_latest",
        "chart_versions",
        ["chart_id"],
        unique=True,
        postgresql_where=sa.text("is_latest"),
    )


def downgrade():
    op.drop_index("uq_chart_versions_latest", table_name="chart_versions")
    op.drop_constraint(op.f("ck_chart_versions_latest_available"), "chart_versions", type_="check")
    op.drop_column("chart_versions", "is_latest")
