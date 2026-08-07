"""Widen health_findings.details_json from varchar(255) to text.

``details_json`` stores a JSON blob of biomarker-specific evidence (e.g. the
list of duplicated blocks, coupling chains, or offending call sites) and can
exceed 255 characters on real repos — the same shape of bug fixed for
``refactoring_suggestions.target_symbol`` in 0045. On PostgreSQL this raises
``StringDataRightTruncationError`` and aborts the batch insert; SQLite never
enforced the length so the bug is Postgres-only. Every sibling payload column
on this table (``file_path``, ``reason``) is already unbounded ``Text`` —
this brings ``details_json`` in line with them. No index or unique
constraint touches this column, so widening it is a plain, lossless type
change.

Revision ID: 0046
Revises: 0045
Create Date: 2026-08-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("health_findings") as batch_op:
        batch_op.alter_column(
            "details_json",
            existing_type=sa.String(255),
            type_=sa.Text(),
            existing_nullable=False,
            existing_server_default="{}",
        )


def downgrade() -> None:
    with op.batch_alter_table("health_findings") as batch_op:
        batch_op.alter_column(
            "details_json",
            existing_type=sa.Text(),
            type_=sa.String(255),
            existing_nullable=False,
            existing_server_default="{}",
        )
