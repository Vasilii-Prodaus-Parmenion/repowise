"""Widen refactoring_suggestions.target_symbol from varchar(255) to text.

``target_symbol`` is a human-readable label, not a bounded identifier: most
detectors write a class/method name (well under 255 chars), but Break Cycle
joins up to four ``basename->basename`` cut edges into one string
(``extract_class.py``/``move_method.py``/``break_cycle.py``) and on a dense
component with long file names that easily clears 255 characters. On
PostgreSQL this raised ``StringDataRightTruncationError`` and aborted the
whole batch insert; SQLite never enforced the length so the bug was
Postgres-only. Every sibling payload column on this table
(``file_path``, ``plan_json``, ``evidence_json``, ``blast_radius_json``) is
already unbounded ``Text`` — this brings ``target_symbol`` in line with
them. No index or unique constraint touches this column, so widening it is a
plain, lossless type change.

Revision ID: 0045
Revises: 0044
Create Date: 2026-08-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("refactoring_suggestions") as batch_op:
        batch_op.alter_column(
            "target_symbol",
            existing_type=sa.String(255),
            type_=sa.Text(),
            existing_nullable=False,
            existing_server_default="",
        )


def downgrade() -> None:
    with op.batch_alter_table("refactoring_suggestions") as batch_op:
        batch_op.alter_column(
            "target_symbol",
            existing_type=sa.Text(),
            type_=sa.String(255),
            existing_nullable=False,
            existing_server_default="",
        )
