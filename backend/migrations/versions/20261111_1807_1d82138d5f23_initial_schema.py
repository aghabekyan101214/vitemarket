"""initial schema

Revision ID: 1d82138d5f23
Revises:
Create Date: 2026-11-11 18:07:01.096533+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "1d82138d5f23"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENUMS = {
    "admin_role": ("admin", "catalog_manager", "site_editor"),
    "product_status": ("active", "archived"),
    "website_status": ("draft", "active", "disabled"),
    "job_status": ("pending", "running", "succeeded", "failed"),
    "page_type": ("home", "products", "product"),
    "domain_status": ("pending", "active", "disabled"),
}


def upgrade() -> None:
    for name, values in ENUMS.items():
        postgresql.ENUM(*values, name=name).create(op.get_bind(), checkfirst=False)

    op.create_table(
        "admin_users",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM(name="admin_role", create_type=False),
            nullable=False,
        ),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("failed_logins", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("position('@' in email) > 1", name=op.f("ck_admin_users_email_format")),
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_admin_users_email_lowercase")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_admin_users")),
        sa.UniqueConstraint("email", name=op.f("uq_admin_users_email")),
    )
    op.create_table(
        "products",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("sku", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="product_status", create_type=False),
            server_default="active",
            nullable=False,
        ),
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_products")),
        sa.UniqueConstraint("sku", name=op.f("uq_products_sku")),
    )
    op.create_table(
        "websites",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("key", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("country_code", sa.String(length=2), nullable=False),
        sa.Column("default_locale", sa.String(length=10), nullable=False),
        sa.Column("locales", postgresql.ARRAY(sa.String(length=10)), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="website_status", create_type=False),
            server_default="draft",
            nullable=False,
        ),
        sa.Column("auto_publish", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("content_version", sa.BigInteger(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "published_version", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "default_locale ~ '^[a-z]{2,3}(-[A-Z]{2})?$'",
            name=op.f("ck_websites_default_locale_format"),
        ),
        sa.CheckConstraint(
            "key ~ '^[a-z0-9](?:[a-z0-9-]{0,48}[a-z0-9])?$'", name=op.f("ck_websites_key_format")
        ),
        sa.CheckConstraint("cardinality(locales) > 0", name=op.f("ck_websites_locales_not_empty")),
        sa.CheckConstraint(
            "char_length(country_code) = 2 AND country_code = upper(country_code)",
            name=op.f("ck_websites_country_code_format"),
        ),
        sa.CheckConstraint(
            "default_locale = ANY(locales)", name=op.f("ck_websites_default_locale_in_locales")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_websites")),
        sa.UniqueConstraint("key", name=op.f("uq_websites_key")),
    )
    op.create_table(
        "admin_user_websites",
        sa.Column("admin_user_id", sa.Uuid(), nullable=False),
        sa.Column("website_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["admin_user_id"],
            ["admin_users.id"],
            name=op.f("fk_admin_user_websites_admin_user_id_admin_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["website_id"],
            ["websites.id"],
            name=op.f("fk_admin_user_websites_website_id_websites"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("admin_user_id", "website_id", name=op.f("pk_admin_user_websites")),
    )
    op.create_index(
        op.f("ix_admin_user_websites_website_id"),
        "admin_user_websites",
        ["website_id"],
        unique=False,
    )
    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("admin_user_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=50), nullable=False),
        sa.Column("entity", sa.String(length=50), nullable=False),
        sa.Column("entity_id", sa.String(length=100), nullable=False),
        sa.Column("website_id", sa.Uuid(), nullable=True),
        sa.Column("diff", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["admin_user_id"],
            ["admin_users.id"],
            name=op.f("fk_audit_log_admin_user_id_admin_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index("ix_audit_log_entity", "audit_log", ["entity", "entity_id"], unique=False)
    op.create_index(
        "ix_audit_log_website_created", "audit_log", ["website_id", "created_at"], unique=False
    )
    op.create_table(
        "product_images",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("content_type", sa.String(length=50), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "alt",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "width > 0 AND height > 0 AND size_bytes > 0",
            name=op.f("ck_product_images_dimensions_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_product_images_product_id_products"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_product_images")),
        sa.UniqueConstraint("storage_key", name=op.f("uq_product_images_storage_key")),
    )
    op.create_index(
        "ix_product_images_product_position",
        "product_images",
        ["product_id", "position"],
        unique=False,
    )
    op.create_table(
        "product_translations",
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("locale", sa.String(length=10), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("slug", sa.String(length=200), nullable=False),
        sa.Column("short_description", sa.String(length=500), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "locale ~ '^[a-z]{2,3}(-[A-Z]{2})?$'",
            name=op.f("ck_product_translations_locale_format"),
        ),
        sa.CheckConstraint(
            "slug ~ '^[a-z0-9]+(-[a-z0-9]+)*$'", name=op.f("ck_product_translations_slug_format")
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_product_translations_product_id_products"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("product_id", "locale", name=op.f("pk_product_translations")),
        sa.UniqueConstraint("locale", "slug", name=op.f("uq_product_translations_locale_slug")),
    )
    op.create_table(
        "publish_jobs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("website_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="job_status", create_type=False),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "tags", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column(
            "paths", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("full_site", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("content_version", sa.BigInteger(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["website_id"],
            ["websites.id"],
            name=op.f("fk_publish_jobs_website_id_websites"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publish_jobs")),
    )
    op.create_index(
        "ix_publish_jobs_due",
        "publish_jobs",
        ["run_after"],
        unique=False,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_publish_jobs_website_created",
        "publish_jobs",
        ["website_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "uq_publish_jobs_pending_per_site",
        "publish_jobs",
        ["website_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_table(
        "seo_metadata",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("website_id", sa.Uuid(), nullable=False),
        sa.Column("locale", sa.String(length=10), nullable=False),
        sa.Column("page", postgresql.ENUM(name="page_type", create_type=False), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=300), nullable=True),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column("canonical_url", sa.Text(), nullable=True),
        sa.Column("og_title", sa.String(length=300), nullable=True),
        sa.Column("og_description", sa.String(length=500), nullable=True),
        sa.Column("og_image_key", sa.Text(), nullable=True),
        sa.Column("robots", sa.String(length=100), server_default="index,follow", nullable=False),
        sa.Column("structured_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(page = 'product') = (product_id IS NOT NULL)",
            name=op.f("ck_seo_metadata_product_only_for_product_page"),
        ),
        sa.CheckConstraint(
            "locale ~ '^[a-z]{2,3}(-[A-Z]{2})?$'", name=op.f("ck_seo_metadata_locale_format")
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_seo_metadata_product_id_products"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["website_id"],
            ["websites.id"],
            name=op.f("fk_seo_metadata_website_id_websites"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_seo_metadata")),
        sa.UniqueConstraint(
            "website_id",
            "locale",
            "page",
            "product_id",
            name=op.f("uq_seo_metadata_website_id_locale_page_product_id"),
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        op.f("ix_seo_metadata_product_id"), "seo_metadata", ["product_id"], unique=False
    )
    op.create_table(
        "website_content",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("website_id", sa.Uuid(), nullable=False),
        sa.Column("page", postgresql.ENUM(name="page_type", create_type=False), nullable=False),
        sa.Column("locale", sa.String(length=10), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("subtitle", sa.Text(), nullable=True),
        sa.Column(
            "blocks",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "jsonb_typeof(blocks) = 'array'", name=op.f("ck_website_content_blocks_is_array")
        ),
        sa.CheckConstraint(
            "locale ~ '^[a-z]{2,3}(-[A-Z]{2})?$'", name=op.f("ck_website_content_locale_format")
        ),
        sa.CheckConstraint(
            "page IN ('home', 'products')", name=op.f("ck_website_content_page_allowed")
        ),
        sa.ForeignKeyConstraint(
            ["website_id"],
            ["websites.id"],
            name=op.f("fk_website_content_website_id_websites"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_website_content")),
        sa.UniqueConstraint(
            "website_id", "page", "locale", name=op.f("uq_website_content_website_id_page_locale")
        ),
    )
    op.create_table(
        "website_domains",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("website_id", sa.Uuid(), nullable=False),
        sa.Column("hostname", sa.String(length=253), nullable=False),
        sa.Column("is_primary", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(name="domain_status", create_type=False),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "redirect_to_primary", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("cloudflare_zone_id", sa.String(length=64), nullable=True),
        sa.Column(
            "verification_token",
            sa.String(length=64),
            server_default=sa.text("replace((gen_random_uuid())::text, '-'::text, ''::text)"),
            nullable=False,
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "hostname !~ '[:/\\s]' AND right(hostname, 1) <> '.'",
            name=op.f("ck_website_domains_hostname_bare"),
        ),
        sa.CheckConstraint(
            "hostname = lower(hostname)", name=op.f("ck_website_domains_hostname_lowercase")
        ),
        sa.ForeignKeyConstraint(
            ["website_id"],
            ["websites.id"],
            name=op.f("fk_website_domains_website_id_websites"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_website_domains")),
        sa.UniqueConstraint("hostname", name=op.f("uq_website_domains_hostname")),
    )
    op.create_index(
        op.f("ix_website_domains_website_id"), "website_domains", ["website_id"], unique=False
    )
    op.create_index(
        "uq_website_domains_primary",
        "website_domains",
        ["website_id"],
        unique=True,
        postgresql_where=sa.text("is_primary"),
    )
    op.create_table(
        "website_products",
        sa.Column("website_id", sa.Uuid(), nullable=False),
        sa.Column("product_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("is_visible", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("is_featured", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name=op.f("fk_website_products_product_id_products"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["website_id"],
            ["websites.id"],
            name=op.f("fk_website_products_website_id_websites"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("website_id", "product_id", name=op.f("pk_website_products")),
    )
    op.create_index(
        "ix_website_products_listing",
        "website_products",
        ["website_id", "position", "product_id"],
        unique=False,
        postgresql_where=sa.text("is_visible"),
    )
    op.create_index(
        op.f("ix_website_products_product_id"), "website_products", ["product_id"], unique=False
    )
    op.create_table(
        "website_themes",
        sa.Column("website_id", sa.Uuid(), nullable=False),
        sa.Column(
            "tokens",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("logo_key", sa.Text(), nullable=True),
        sa.Column("logo_dark_key", sa.Text(), nullable=True),
        sa.Column("favicon_key", sa.Text(), nullable=True),
        sa.Column("og_default_image_key", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["website_id"],
            ["websites.id"],
            name=op.f("fk_website_themes_website_id_websites"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("website_id", name=op.f("pk_website_themes")),
    )


def downgrade() -> None:
    op.drop_table("website_themes")
    op.drop_index(op.f("ix_website_products_product_id"), table_name="website_products")
    op.drop_index(
        "ix_website_products_listing",
        table_name="website_products",
        postgresql_where=sa.text("is_visible"),
    )
    op.drop_table("website_products")
    op.drop_index(
        "uq_website_domains_primary",
        table_name="website_domains",
        postgresql_where=sa.text("is_primary"),
    )
    op.drop_index(op.f("ix_website_domains_website_id"), table_name="website_domains")
    op.drop_table("website_domains")
    op.drop_table("website_content")
    op.drop_index(op.f("ix_seo_metadata_product_id"), table_name="seo_metadata")
    op.drop_table("seo_metadata")
    op.drop_index(
        "uq_publish_jobs_pending_per_site",
        table_name="publish_jobs",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_index("ix_publish_jobs_website_created", table_name="publish_jobs")
    op.drop_index(
        "ix_publish_jobs_due",
        table_name="publish_jobs",
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.drop_table("publish_jobs")
    op.drop_table("product_translations")
    op.drop_index("ix_product_images_product_position", table_name="product_images")
    op.drop_table("product_images")
    op.drop_index("ix_audit_log_website_created", table_name="audit_log")
    op.drop_index("ix_audit_log_entity", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index(op.f("ix_admin_user_websites_website_id"), table_name="admin_user_websites")
    op.drop_table("admin_user_websites")
    op.drop_table("websites")
    op.drop_table("products")
    op.drop_table("admin_users")

    for name in ENUMS:
        postgresql.ENUM(name=name).drop(op.get_bind(), checkfirst=False)
