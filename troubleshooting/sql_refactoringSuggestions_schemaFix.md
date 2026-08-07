# `refactoring_suggestions.target_symbol` too narrow for Postgres

**Status:** Fixed.

## Issue

`persist_pipeline_result()` batch-inserting `refactoring_suggestions` failed on
Postgres with:

```
sqlalchemy.exc.DBAPIError: (asyncpg.exceptions.StringDataRightTruncationError)
value too long for type character varying(255)
```

`target_symbol` was `String(255)` (`packages/core/src/repowise/core/persistence/models.py`,
migration `0034_refactoring_suggestions.py`). Most detectors write a short class/method
name there, but `BreakCycleDetector` (`analysis/health/refactoring/break_cycle.py:141-146`)
builds a `cycle[N]: base1->base2, base3->base4, ...` label from up to 4 cut edges
(`_MAX_CUT_EDGES`) over a cycle of up to 20 files (`_MAX_CYCLE_FILES`); with normal C#
file-name lengths this routinely exceeds 255 chars. `move_method.py`'s
`f"{parent}.{name}"` label is similarly unbounded. SQLite never enforced the column
length, so the bug only surfaced against Postgres.

## Fix

`target_symbol` is a human-readable label, not a bounded identifier — every sibling
payload column on the same table (`file_path`, `plan_json`, `evidence_json`,
`blast_radius_json`) is already unbounded `Text`. Widened `target_symbol` to `Text` to
match:

- `models.py`: `RefactoringSuggestion.target_symbol` → `Text`.
- New migration `0045_refactoring_target_symbol_text.py`: `alter_column` via
  `batch_alter_table` (Postgres `ALTER TYPE`, SQLite table-rebuild), both directions.

No index/unique constraint touches this column, so it's a lossless, reversible type
change. Verified: unit tests (`test_models.py`, `test_refactoring_suggestions_crud.py`)
pass, and `alembic upgrade head` / `downgrade -1` / `upgrade head` round-trip cleanly on
a scratch SQLite DB.

Not fixed (out of scope here, callable as follow-up): capping/truncating the generated
label itself in `break_cycle.py` so it stays short and readable even on a dense
component — the schema fix alone unblocks the insert.
