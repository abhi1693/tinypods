"""Add intended installations, immutable history and application-scoped secrets."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009_application_installations"
down_revision = "0008_release_publication"
branch_labels = None
depends_on = None


def audit_columns():
    return [
        sa.Column("created", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_updated", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), primary_key=True),
    ]


def upgrade():
    op.create_table(
        "application_installations",
        *audit_columns(),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column(
            "owner_id",
            sa.Uuid(),
            sa.ForeignKey("auth_user.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "cluster_id",
            sa.Uuid(),
            sa.ForeignKey("clusters.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("namespace", sa.String(63), nullable=False),
        sa.Column("revision_id", sa.Uuid()),
        sa.CheckConstraint("length(trim(name)) > 0", name="name"),
        sa.CheckConstraint(
            "length(namespace) BETWEEN 1 AND 63 AND namespace ~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?$'",
            name="namespace",
        ),
    )
    op.create_index(
        "ix_installation_owner_history", "application_installations", ["owner_id", "created", "id"]
    )
    op.create_index(
        "ix_application_installations_cluster_id", "application_installations", ["cluster_id"]
    )
    op.create_table(
        "installation_revisions",
        *audit_columns(),
        sa.Column(
            "installation_id",
            sa.Uuid(),
            sa.ForeignKey("application_installations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "release_id",
            sa.Uuid(),
            sa.ForeignKey("releases.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("overrides", JSONB(), nullable=False),
        sa.Column("values", JSONB(), nullable=False),
        sa.UniqueConstraint("id", "installation_id", name="uq_revision_installation"),
        sa.CheckConstraint("jsonb_typeof(overrides) = 'object'", name="overrides"),
        sa.CheckConstraint("jsonb_typeof(values) = 'object'", name="values"),
    )
    op.create_index(
        "ix_installation_revisions_release_id", "installation_revisions", ["release_id"]
    )
    op.create_index(
        "ix_revision_history", "installation_revisions", ["installation_id", "created", "id"]
    )
    op.create_foreign_key(
        "fk_installation_current_revision",
        "application_installations",
        "installation_revisions",
        ["revision_id", "id"],
        ["id", "installation_id"],
        ondelete="RESTRICT",
    )
    op.create_table(
        "application_secrets",
        *audit_columns(),
        sa.Column(
            "secret_id",
            sa.Uuid(),
            sa.ForeignKey("secrets.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "installation_id",
            sa.Uuid(),
            sa.ForeignKey("application_installations.id", ondelete="RESTRICT"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_application_secret_history", "application_secrets", ["installation_id", "created", "id"]
    )
    op.create_table(
        "revision_secrets",
        *audit_columns(),
        sa.Column(
            "revision_id",
            sa.Uuid(),
            sa.ForeignKey("installation_revisions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "version_id",
            sa.Uuid(),
            sa.ForeignKey("secret_versions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("target", sa.String(512), nullable=False),
        sa.Column("use", sa.String(64), nullable=False),
        sa.UniqueConstraint("revision_id", "target", name="uq_revision_secret_target"),
    )
    op.create_index("ix_revision_secrets_version_id", "revision_secrets", ["version_id"])
    op.create_table(
        "revision_kubernetes_secrets",
        *audit_columns(),
        sa.Column(
            "revision_id",
            sa.Uuid(),
            sa.ForeignKey("installation_revisions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("target", sa.String(512), nullable=False),
        sa.Column("use", sa.String(64), nullable=False),
        sa.Column("name", sa.String(253), nullable=False),
        sa.Column("key", sa.String(253), nullable=False),
        sa.UniqueConstraint("revision_id", "target", name="uq_revision_kubernetes_secret_target"),
    )
    op.drop_constraint(op.f("ck_secrets_kind"), "secrets", type_="check")
    op.create_check_constraint("kind", "secrets", "kind IN ('cluster', 'application')")
    op.execute("""
        CREATE OR REPLACE FUNCTION public.check_cluster_secret_scope() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        DECLARE target uuid; kind text;
        BEGIN
            IF TG_TABLE_NAME = 'secrets' THEN
                target := CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END;
            ELSE
                target := CASE WHEN TG_OP = 'DELETE' THEN OLD.secret_id ELSE NEW.secret_id END;
            END IF;
            SELECT s.kind INTO kind FROM public.secrets s WHERE s.id = target;
            IF kind IS NOT NULL AND (
                (kind = 'cluster' AND (NOT EXISTS (SELECT 1 FROM public.cluster_secrets WHERE
                secret_id = target)
                    OR EXISTS (SELECT 1 FROM public.application_secrets WHERE secret_id = target)))
                OR (kind = 'application' AND (NOT EXISTS (SELECT 1 FROM
                public.application_secrets WHERE secret_id = target)
                    OR EXISTS (SELECT 1 FROM public.cluster_secrets WHERE secret_id = target)))
            ) THEN
                RAISE EXCEPTION 'Secret requires exactly its matching real scope' USING
                ERRCODE='23514';
            END IF;
            RETURN NULL;
        END $$;
        CREATE CONSTRAINT TRIGGER application_secrets_scope_check
        AFTER INSERT OR UPDATE OR DELETE ON public.application_secrets
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION
                public.check_cluster_secret_scope();
        CREATE FUNCTION public.guard_installation_history() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            RAISE EXCEPTION 'Installation history is immutable' USING ERRCODE='23514';
        END $$;
        CREATE FUNCTION public.guard_application_installation() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF (NEW.id, NEW.owner_id, NEW.cluster_id, NEW.namespace, NEW.created)
                IS DISTINCT FROM (OLD.id, OLD.owner_id, OLD.cluster_id, OLD.namespace,
                OLD.created) THEN
                RAISE EXCEPTION 'Installation target is immutable' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER application_installation_target BEFORE UPDATE ON
                public.application_installations
            FOR EACH ROW EXECUTE FUNCTION public.guard_application_installation();
        CREATE TRIGGER application_secret_scope BEFORE UPDATE ON public.application_secrets
            FOR EACH ROW EXECUTE FUNCTION public.guard_installation_history();
        CREATE FUNCTION public.guard_revision_configuration() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        DECLARE item jsonb; target text[];
        BEGIN
            FOR item IN SELECT jsonb_array_elements(configuration_fields)
                FROM public.releases WHERE id = NEW.release_id
            LOOP
                IF (item->>'sensitive')::boolean OR item->>'type' = 'password' THEN
                    target := string_to_array(item->>'path', '.');
                    IF NEW.overrides #> target IS NOT NULL OR NEW.values #> target IS NOT NULL THEN
                        RAISE EXCEPTION 'Confidential configuration requires scoped storage'
                            USING ERRCODE='23514';
                    END IF;
                END IF;
            END LOOP;
            RETURN NEW;
        END $$;
        CREATE TRIGGER revision_configuration BEFORE INSERT ON public.installation_revisions
            FOR EACH ROW EXECUTE FUNCTION public.guard_revision_configuration();
        CREATE FUNCTION public.guard_revision_binding_insert() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM public.installation_revisions r
                WHERE r.id = NEW.revision_id AND r.xmin::text =
                    (pg_current_xact_id()::text::bigint % 4294967296)::text
            ) THEN
                RAISE EXCEPTION 'Bindings must be saved atomically with their revision'
                    USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;
        CREATE FUNCTION public.check_revision_secret_scope() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM public.installation_revisions r
                JOIN public.application_secrets s ON s.installation_id = r.installation_id
                JOIN public.secret_versions v ON v.secret_id = s.secret_id
                WHERE r.id = NEW.revision_id AND v.id = NEW.version_id AND v.revoked_at IS NULL
            ) THEN
                RAISE EXCEPTION 'Revision secret scope mismatch or revoked' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER revision_secret_scope BEFORE INSERT ON public.revision_secrets
            FOR EACH ROW EXECUTE FUNCTION public.check_revision_secret_scope();
    """)
    for table in ("revision_secrets", "revision_kubernetes_secrets"):
        op.execute(
            f"CREATE TRIGGER atomic_binding BEFORE INSERT ON public.{table} "
            "FOR EACH ROW EXECUTE FUNCTION public.guard_revision_binding_insert()"
        )
    for table in ("installation_revisions", "revision_secrets", "revision_kubernetes_secrets"):
        op.execute(
            f"CREATE TRIGGER immutable_history BEFORE UPDATE OR DELETE ON public.{table} "
            "FOR EACH ROW EXECUTE FUNCTION public.guard_installation_history()"
        )


def downgrade():
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS(SELECT FROM application_installations) "
            "OR EXISTS(SELECT FROM application_secrets)"
        )
    ):
        raise RuntimeError("Installation history prevents downgrade")
    for table in ("revision_secrets", "revision_kubernetes_secrets"):
        op.execute(f"DROP TRIGGER atomic_binding ON {table}")
    op.execute("DROP FUNCTION public.guard_revision_binding_insert()")
    op.execute("DROP TRIGGER revision_configuration ON installation_revisions")
    op.execute("DROP FUNCTION public.guard_revision_configuration()")
    op.execute("DROP TRIGGER revision_secret_scope ON revision_secrets")
    op.execute("DROP FUNCTION public.check_revision_secret_scope()")
    op.execute("DROP TRIGGER application_installation_target ON application_installations")
    op.execute("DROP FUNCTION public.guard_application_installation()")
    op.execute("DROP TRIGGER application_secret_scope ON application_secrets")
    for table in ("revision_kubernetes_secrets", "revision_secrets", "installation_revisions"):
        op.execute(f"DROP TRIGGER immutable_history ON {table}")
    op.execute("DROP FUNCTION public.guard_installation_history()")
    op.execute("DROP TRIGGER application_secrets_scope_check ON application_secrets")
    op.drop_table("revision_kubernetes_secrets")
    op.drop_table("revision_secrets")
    op.drop_table("application_secrets")
    op.drop_constraint(
        "fk_installation_current_revision", "application_installations", type_="foreignkey"
    )
    op.drop_table("installation_revisions")
    op.drop_table("application_installations")
    op.drop_constraint(op.f("ck_secrets_kind"), "secrets", type_="check")
    op.create_check_constraint("kind", "secrets", "kind = 'cluster'")
    op.execute("""
        CREATE OR REPLACE FUNCTION public.check_cluster_secret_scope() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, public AS $$
        DECLARE target uuid;
        BEGIN
            IF TG_TABLE_NAME = 'secrets' THEN
                target := CASE WHEN TG_OP = 'DELETE' THEN OLD.id ELSE NEW.id END;
            ELSE
                target := CASE WHEN TG_OP = 'DELETE' THEN OLD.secret_id ELSE NEW.secret_id END;
            END IF;
            IF EXISTS (SELECT 1 FROM public.secrets WHERE id = target)
               AND NOT EXISTS (SELECT 1 FROM public.cluster_secrets WHERE secret_id = target) THEN
                RAISE EXCEPTION 'Secret requires its real cluster association'
                    USING ERRCODE = '23514', CONSTRAINT = 'ck_secrets_cluster_scope';
            END IF;
            RETURN NULL;
        END $$;
    """)
