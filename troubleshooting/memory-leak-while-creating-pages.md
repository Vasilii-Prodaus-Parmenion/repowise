# Memory leak: OOM during "Generating Pages" on `repowise init --index-only --yes`

**Status:** Root cause #1 fixed. #2 is architectural (deferred). #4a/#4b deferred (later phase, not hit by this crash).

## Report

Scanning a very big repository with big classes that expose a lot of symbols and have a
lot of usages in consumers.

- **Command:** `repowise init --index-only --yes`
- **Symptom:** crashes during phase **"Generating Pages"** — all RAM is consumed.
- **DB:** Postgres via `REPOWISE_DB_URL`.

> **Suspicion (reporter):** something should be written to DB as soon as possible — but
> check all the root causes, not just this one.

## Environment / scale

Workspace `securewebsite`, repo `back-office`:

| Metric | Value |
|---|---|
| Files scanned / included | 16,071 / 13,088 |
| Symbols extracted | 114,716 |
| Graph | 140,240 nodes · 254,332 edges |
| Git history | 13,374 files indexed · 44 hotspots |
| KG skeleton | 35,329 nodes · 97,527 edges · 407 layers |
| Health findings | 10,788 (avg 8.95/10, worst 1.78/10) |
| Dead code | 4,211 unreachable files · 4,938 unused exports · ~322,948 deletable lines |

## Execution log

```text
Repository: Workspace: securewebsite

Detected 16 repositories in /mnt/wsl/drive/dev/securewebsite
✓ Created .repowise-workspace.yaml

[1/16] Indexing back-office (back-office)…
Scanned 16,071 files, 13,088 included
  Excluded: 1 by .gitignore, 1 by extension, 26 by filename pattern, 13 oversized,
            310 binary, 1,308 generated, 1,324 unknown type
Indexing file history… ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 15/13374  0:00:28
Graph build can take several minutes on a first run. Safe to Ctrl-C — re-run
'repowise init --resume' to continue where it stopped.
  ↳ PageRank ✓ (7.5s)
  ↳ symbol PageRank ✓ (11.6s)
  ↳ betweenness centrality ✓ (70.2s)
  ↳ symbol betweenness ✓ (275.1s)
  ↳ community detection ✓ (28.1s)
  ↳ symbol communities ✓ (47.6s)
→ External systems: 1,138 declared deps across manifests
→ 13,088 files parsed · 114,716 symbols extracted
→ Graph: 140,240 nodes · 254,332 edges
→ Git: 13,374 files indexed · 44 hotspots
→ 0 architectural decisions found
→ 4211 unreachable files · 4938 unused exports · ~322,948 deletable lines
→ 10788 health findings · avg 8.95/10 · worst 1.78/10
  ↳ KG skeleton: 35329 nodes, 97527 edges, 407 layers

  ✓ 13,088 files, 114,716 symbols

embed_text_truncated page_id=api_contract:Parmenion.Comms.Api.Client/OpenAPIs/CommsApi.json chars=117835 chars_dropped=87835 cap=30000
embed_text_truncated page_id=api_contract:Parmenion.ValuationsService.Api.Client/Properties/ValuationsServiceApi.json chars=44222 chars_dropped=14222 cap=30000
embed_text_truncated page_id=symbol_spotlight:Parmenion.BusinessLogicLayer/EntityViews/View_Edi_Archive.vb::View_Edi_Archive chars=215357 chars_dropped=185357 cap=30000
embed_text_truncated page_id=file_page:EntityGenerator/Parmenion.Entities/LegacyContext.Entities.cs chars=49399 chars_dropped=19399 cap=30000
embed_text_truncated page_id=file_page:Parmenion.BusinessLogicLayer/Entities/Portfolio.vb chars=33168 chars_dropped=3168 cap=30000
embed_text_truncated page_id=file_page:Parmenion.BusinessLogicLayer/Common/ParmenionContext.vb chars=75268 chars_dropped=45268 cap=30000
embed_text_truncated page_id=file_page:Parmenion.BusinessLogicLayer/Common/PulseSyncEvents.vb chars=33594 chars_dropped=3594 cap=30000
embed_text_truncated page_id=scc_page:scc-49226c6a3d72 chars=46347 chars_dropped=16347 cap=30000

Generating pages  6554/13000 ...
[OOM — process killed]
```

## Analysis

Traced the "Generating Pages" phase end-to-end: orchestrator → page generator →
embedding → DB persist. Findings below are ranked by confidence and estimated impact.

### 1. `file_page_contexts` never freed — primary root cause

**Confidence: high · Impact: high · Fix risk: low**

```python
# packages/core/src/repowise/core/generation/page_generator/orchestrate.py:156
self.file_page_contexts: dict[str, FilePageContext] = {}
```

Populated once per code file in `build_level2_coros` (`page_generator/levels.py:178`) —
**for every file in the repo, not just the ones that get a page** (the docstring at
`levels.py:139-147` says so explicitly: *"Context is assembled for ALL code files"*).
Each `FilePageContext` (`generation/context/contexts.py:16-71`) carries:

- `file_source_snippet` — raw source trimmed to a token budget (default 48,000 tokens
  ≈ up to ~190KB of text) — `context/assembler.py:290,331-340`
- `rag_context: list[str]`, `dependency_summaries: dict[str, str]`, symbol / co-change
  data

For this run: 13,088 files × up to ~190KB of retained source alone, before counting the
other fields. "Big classes with a lot of usages/consumers" means large files with dense
dependency summaries — exactly what inflates this structure the most.

Grepped every reference to `file_page_contexts` in the `generation/` tree — it is only
*read* at two sites: `scc_page` (`levels.py:223`) and `module_page` (`levels.py:261`),
i.e. levels 3 and 4. After level 4 finishes (`orchestrate.py:714`), nothing ever reads
it again — levels 6-8 (repo_overview/infra/onboarding) don't touch it, and the "layer"
page type the docstring also mentions as a consumer was retired
(`orchestrate.py:716-718`). **It is simply never cleared.** It sits fully populated in
memory for the rest of the run: levels 6-8, then the finalize pass (mermaid repair,
interlinking, page-tree assignment).

→ Isolated, low-risk fix: clear it right after level 4 completes.

### 2. `all_pages` held for cross-page finalize passes — architectural

**Confidence: certain · Impact: high · Fix risk: high (out of scope)**

`orchestrate.py:692`, `all_pages: list[GeneratedPage] = []`, extended after every level,
never trimmed — holds every page's full rendered markdown simultaneously because
`_finalize()` (`orchestrate.py:747`) does cross-page work over the entire set at once
(mermaid repair, wiki-link interlinking, related-pages attachment, page-tree
placement). This is load-bearing for the current design; avoiding it would mean
reworking those passes to read from the DB instead of an in-memory list — out of scope
for this round.

### 3. The "write to DB ASAP" suspicion — already implemented, doesn't fix this crash

`repowise init` (including `--index-only`) already writes each page to Postgres
incrementally, the instant it's generated —
`packages/cli/src/repowise/cli/commands/init_cmd/_generation_persist.py`. A background
consumer drains a queue and commits one page at a time via its own session, correctly
using `REPOWISE_DB_URL` (`repowise.core.persistence.database.resolve_db_url`).

This doesn't free memory because the full page list still has to survive for the
finalize pass (#2) and for a second, *necessary* end-of-run write
(`persistence/crud/pages.py:353-431`, `upsert_pages_from_generated`) that captures the
finalize-pass edits — interlinking/mermaid fixes mutate content *after* the incremental
sink already wrote the pre-edit version. So it's not redundant, just not what's crashing
this run: the crash happens *during* generation, before that end-of-run write phase is
ever reached.

### 4. Two secondary issues — later phases, not hit by this crash

- **`upsert_pages_from_generated`** (`persistence/crud/pages.py:378-430`) resolves all
  ~13k existing DB rows into `existing_by_id` up front, builds all Page ORM objects, and
  does one `session.flush()` at the very end — a 2-3x peak of full page content in the
  session identity map, on top of the rest of ingestion/git/analysis data already in
  that same open transaction (`init_cmd/persistence.py:137`, one session for the whole
  persist). Happens in a phase *after* generation, so not reached by this log, but
  likely the next OOM point once #1 is fixed.
- **Unbounded `asyncio.Queue()`** in `_generation_persist.py:94` (no `maxsize`) — a
  secondary accumulation risk if Postgres writes ever lag behind page generation. Lower
  priority: generation concurrency is already throttled by a semaphore.

### Ranking

| # | Issue | Confidence it's the cause | Fix risk |
|---|---|---|---|
| 1 | `file_page_contexts` never cleared | High | Low |
| 2 | `all_pages` held for finalize | Certain, but architectural | High (out of scope) |
| 4a | `upsert_pages_from_generated` peak | Medium (future phase) | Low-medium |
| 4b | Unbounded queue | Low | Medium |

## Resolution

Scope for this pass: fix **#1 only**.

`_GenerationRun.execute()` (`orchestrate.py`) clears `self.file_page_contexts`
immediately after level 4 (`module_page`) completes and before levels 6-8 /
`_finalize()` run, since nothing downstream reads it. This releases the per-file source
snippets + assembled context for all ~13k files at the earliest safe point instead of
holding them for the rest of the run.

Issues #2, #4a and #4b are documented above but deliberately left unfixed in this
commit — #2 is architectural (would need the finalize passes reworked to not require
the whole page list in memory), and #4a/#4b address a later phase this specific crash
never reached.
