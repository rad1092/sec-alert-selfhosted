"""phase6 openai rewrite fields

Revision ID: 20260326_01
Revises:
Create Date: 2026-03-26 19:20:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260326_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "filings" not in tables:
        return

    existing_columns = {column["name"] for column in inspector.get_columns("filings")}
    if "openai_headline" not in existing_columns:
        op.add_column("filings", sa.Column("openai_headline", sa.Text(), nullable=True))
    if "openai_context" not in existing_columns:
        op.add_column("filings", sa.Column("openai_context", sa.Text(), nullable=True))
    if "openai_category" not in existing_columns:
        op.add_column("filings", sa.Column("openai_category", sa.String(length=32), nullable=True))
    if "openai_model" not in existing_columns:
        op.add_column("filings", sa.Column("openai_model", sa.String(length=128), nullable=True))
    if "openai_generated_at" not in existing_columns:
        op.add_column(
            "filings",
            sa.Column("openai_generated_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "filings" not in tables:
        return

    existing_columns = {column["name"] for column in inspector.get_columns("filings")}
    for column_name in (
        "openai_generated_at",
        "openai_model",
        "openai_category",
        "openai_context",
        "openai_headline",
    ):
        if column_name in existing_columns:
            op.drop_column("filings", column_name)
