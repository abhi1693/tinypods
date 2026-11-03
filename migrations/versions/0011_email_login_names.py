"""Allow validated email addresses as account login names."""

import sqlalchemy as sa
from alembic import op

revision = "0011_email_login_names"
down_revision = "0010_jobs"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column(
        "auth_user",
        "username",
        existing_type=sa.String(64),
        type_=sa.String(254),
        existing_nullable=False,
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS(SELECT FROM auth_user WHERE length(username) > 64)")
    ):
        raise RuntimeError("Long account login names prevent downgrade")
    op.alter_column(
        "auth_user",
        "username",
        existing_type=sa.String(254),
        type_=sa.String(64),
        existing_nullable=False,
    )
