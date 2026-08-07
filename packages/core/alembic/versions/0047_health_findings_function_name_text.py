"""Widen health_findings.function_name from varchar(255) to text.

``function_name`` stores the symbol name a finding is anchored to, which on
real repos can be a long generic/overload C# signature or a synthetic name
and exceed 255 characters — the same shape of bug fixed for
``details_json`` in 0046 and ``refactoring_suggestions.target_symbol`` in
0045. On PostgreSQL this raises ``StringDataRightTruncationError`` and
aborts the batch insert; SQLite never enforced the length so the bug is
Postgres-only. ``git_function_blame.function_name`` already stores the same
kind of value as unbounded ``Text`` — this brings ``health_findings`` in
line with it. No index or unique constraint touches this column, so
widening it is a plain, lossless type change.

Revision ID: 0047
Revises: 0046
Create Date: 2026-08-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "0047"
down_revision: str | None = "0046"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("health_findings") as batch_op:
        batch_op.alter_column(
            "function_name",
            existing_type=sa.String(255),
            type_=sa.Text(),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("health_findings") as batch_op:
        batch_op.alter_column(
            "function_name",
            existing_type=sa.Text(),
            type_=sa.String(255),
            existing_nullable=True,
        )
