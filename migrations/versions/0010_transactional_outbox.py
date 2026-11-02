"""Persist reusable transactional events without a queue or operation dependency."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_transactional_outbox"
down_revision = "0009_application_installations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox_events")),
        sa.CheckConstraint(
            "event_type ~ '^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$'",
            name=op.f("ck_outbox_events_event_type"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'", name=op.f("ck_outbox_events_payload_object")
        ),
        sa.CheckConstraint(
            "octet_length(payload::text) <= 16384", name=op.f("ck_outbox_events_payload_size")
        ),
        sa.CheckConstraint(
            "published_at IS NULL OR published_at >= created",
            name=op.f("ck_outbox_events_publication_time"),
        ),
    )
    op.create_index(
        "ix_outbox_pending",
        "outbox_events",
        ["created", "id"],
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.execute("""
        CREATE FUNCTION public.guard_outbox_event() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.published_at IS NOT NULL THEN
                    RAISE EXCEPTION 'New outbox events must be pending' USING ERRCODE='23514';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                IF OLD.published_at IS NULL THEN
                    RAISE EXCEPTION 'Pending outbox events cannot be deleted' USING ERRCODE='23514';
                END IF;
                RETURN OLD;
            END IF;
            IF ROW(NEW.id, NEW.event_type, NEW.payload, NEW.created)
                IS DISTINCT FROM ROW(OLD.id, OLD.event_type, OLD.payload, OLD.created)
                OR (OLD.published_at IS NOT NULL
                    AND NEW.published_at IS DISTINCT FROM OLD.published_at)
            THEN
                RAISE EXCEPTION 'Outbox event content and acknowledgments are immutable'
                    USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER immutable_outbox BEFORE INSERT OR UPDATE OR DELETE ON public.outbox_events
            FOR EACH ROW EXECUTE FUNCTION public.guard_outbox_event();
    """)


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT EXISTS(SELECT FROM outbox_events)")):
        raise RuntimeError("Outbox history prevents downgrade")
    op.drop_table("outbox_events")
    op.execute("DROP FUNCTION public.guard_outbox_event()")
