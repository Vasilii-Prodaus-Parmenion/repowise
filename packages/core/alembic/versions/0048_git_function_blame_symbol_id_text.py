"""Widen git_function_blame.symbol_id from varchar(512) to text.

``symbol_id`` is ``"{path}::{name}"``, and ``name`` for an anonymous
function/arrow-callback falls back to a synthetic name derived from the
function body (see ``function_blame_rollup.py``), which can push the whole
value past 512 characters on real repos. On PostgreSQL this raises
``StringDataRightTruncationError`` and aborts the bulk upsert; SQLite never
enforced the length so the bug is Postgres-only. Same shape of bug fixed for
``health_findings.function_name`` in 0047, ``details_json`` in 0046, and
``refactoring_suggestions.target_symbol`` in 0045. The unique constraint
``uq_git_function_blame`` (repository_id, symbol_id) still applies to text
columns, so widening is a plain, lossless type change.

Revision ID: 0048
Revises: 0047
Create Date: 2026-08-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("git_function_blame") as batch_op:
        batch_op.alter_column(
            "symbol_id",
            existing_type=sa.String(512),
            type_=sa.Text(),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("git_function_blame") as batch_op:
        batch_op.alter_column(
            "symbol_id",
            existing_type=sa.Text(),
            type_=sa.String(512),
            existing_nullable=False,
        )
