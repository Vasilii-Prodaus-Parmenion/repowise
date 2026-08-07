# Memory leak: OOM during "Generating Pages" on `repowise init --index-only --yes`

**Status:** Root cause #1 fixed (stopped the crash). Follow-up report: run now completes
but peaks at ~30GB RSS — root causes #5 and #6 below fixed; #2 remains architectural
(deferred). #4a/#4b deferred (later phase, not yet hit).

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

## Follow-up report: no crash, but 30GB peak RSS, not freed

With #1 fixed, the same `securewebsite`/`back-office` scan now **completes
successfully** — no OOM — but peak RSS still reaches **~30GB**, and the reporter
observed the memory as "not freed" afterward.

Re-traced the whole run (ingestion → analysis → generation → persistence), not just
the generation phase this time, since nothing at the "Generating Pages" step alone
accounts for 30GB once #1 is fixed. Findings:

### 5. Un-batched bulk upserts for graph nodes/edges/symbols — new primary suspect

**Confidence: high · Impact: high · Fix risk: low · Status: fixed**

`_batch_upsert_keyed()` (`persistence/crud/_shared.py:40-89`) takes an optional
`batch_size`. When it's `None` (the default), it does `chunks = [materialized]` — the
*entire* input list becomes one chunk: every ORM object is constructed and
`session.add()`-ed, then **one `session.flush()`** sends the whole batch at once.
`git.py`'s four call sites correctly pass `batch_size=_BATCH_SIZE` (500). The graph and
symbol call sites did not, despite `batch_upsert_graph_nodes`'s own docstring claiming
"in batches of up to 500":

- `batch_upsert_graph_nodes` (`persistence/crud/graph.py`) — was flushing all
  **~140,240** `GraphNode` rows in one call
- `batch_upsert_graph_edges` (same file) — all **~254,332** `GraphEdge` rows
- `batch_upsert_graph_metrics` (same file) — all **~13,088** `GraphMetric` rows
- `batch_upsert_graph_node_membership` (same file) — up to **~130,000**
  `GraphNodeMembership` rows
- `batch_upsert_symbols` (`persistence/crud/external_systems.py`) — all **~114,716**
  `WikiSymbol` rows

Each call site also pre-materializes a plain-dict/tuple precursor list before building
the ORM objects (`persist.py`'s `persist_graph_nodes`, the `edges`/`all_symbols` loops
in `persist_ingestion`), so the peak briefly held: the precursor list + every freshly
built ORM instance + (on an update) every pre-existing row loaded for matching — all in
one `AsyncSession`, all while the full `PipelineResult` from ingestion/analysis/
generation (graph, parsed_files, generated_pages/`all_pages`) is *still* resident,
since persistence runs on the same in-memory result (see #6). Chunking the flush is
exactly what `git.py` already does for its four call sites; the graph/symbol call sites
were simply missed.

**Fix:** pass `batch_size=_BATCH_SIZE` at all five call sites, matching `git.py`'s
existing pattern (`persistence/crud/graph.py`, `persistence/crud/external_systems.py` —
the latter needed the `_BATCH_SIZE` import added too).

### 6. `GraphBuilder`'s cached file/symbol subgraph copies — secondary fix

**Confidence: high · Impact: medium · Fix risk: low · Status: fixed**

`GraphBuilder` holds one combined `nx.DiGraph()` with both file and symbol nodes
(`ingestion/graph/builder.py:52`), not two separate graphs. But `file_subgraph()` and
`symbol_subgraph()` (`ingestion/graph/_metrics.py:80-135`) each do a
`g.subgraph(nodes).copy()` the first time a metric needs one (SCCs, pagerank,
betweenness, community detection), and cache the result on
`_file_subgraph_cache`/`_symbol_subgraph_cache` for the builder's lifetime. So at
steady state there are **three live NetworkX graphs**: the full graph plus a full copy
of its file-only and symbol-only subsets. `_GenerationRun` (`orchestrate.py`) holds the
`graph_builder` (and therefore both cached copies) for the entire generation phase, but
nothing in generation calls `file_subgraph()`/`symbol_subgraph()` directly — only
`graph()`/`pagerank()`/`betweenness_centrality()`/`community_detection()`/
`strongly_connected_components()`, whose results are snapshotted into plain dicts
(`self.pagerank`, `self.betweenness`, ...) in `_GenerationRun.__init__`. The two cached
subgraph copies are dead weight from that point on.

This is the (worse, whole-graph) sibling of `release_graph()` (`builder.py:157-172`),
which already exists but is explicitly scoped to pipelines that need *no* further graph
traversal (e.g. fast/no-docs mode) — `_GenerationRun` still needs the full graph for
`extract_call_graph`/`extract_heritage` per file, so `release_graph()` itself isn't
safe to call here.

**Fix:** added `GraphBuilder.release_subgraph_caches()` — a thin public wrapper around
the existing (private) `_invalidate_subgraph_caches()` — and call it from
`_GenerationRun.__init__` right after the five metric dicts are snapshotted. If
anything calls a metric method again later, the subgraphs are simply rebuilt from the
still-live full graph.

### 7. Ruled out this round

- **Dead code / health findings** (`analysis/dead_code/models.py`,
  `analysis/health/models.py`) store line numbers/ranges and small metadata dicts, not
  source text per finding — not a meaningful contributor at 10,788 / 4,211 / 4,938 /
  ~322,948-line counts.
- **Double-copying `PipelineResult` data between phases** — `parsed_files`,
  `source_map`, `graph_builder`, `dead_code_report`, etc. are passed by reference from
  ingestion through generation and into persistence (`pipeline/orchestrator.py`,
  `pipeline/phases/generation.py`, `pipeline/persist.py`). One live copy, not two — the
  actual risk is that this one copy stays alive through persistence too (see below),
  not that it's duplicated.
- **Process lifecycle** — for `repowise init` run from the CLI, the command runs to
  completion in a single short-lived process with engines/connections explicitly
  disposed before it returns (`init_cmd/command.py`, `_generation_persist.py`,
  `init_cmd/persistence.py`); the process then exits and the OS reclaims everything, so
  the "not freed" observation is the transient peak during the run, not a still-running
  process. (Caveat: if the same `run_pipeline()`/`persist_pipeline_result()` path is
  ever triggered through `packages/server`'s long-lived job executor instead of the
  CLI, that peak would sit in a still-running interpreter instead — a materially
  different risk profile worth keeping in mind if this workload moves there.)

### Updated ranking

| # | Issue | Confidence | Fix risk | Status |
|---|---|---|---|---|
| 1 | `file_page_contexts` never cleared | High | Low | Fixed |
| 5 | Un-batched graph/symbol ORM upserts (single flush of 140k+/254k+/115k+ rows) | High | Low | Fixed |
| 6 | `GraphBuilder` file/symbol subgraph copies retained through generation | High | Low | Fixed |
| 2 | `all_pages` held for finalize + persistence | Certain, but architectural | High (out of scope) | Deferred |
| 4a | `upsert_pages_from_generated` peak | Medium (future phase) | Low-medium | Deferred |
| 4b | Unbounded queue | Low | Medium | Deferred |

## Resolution (follow-up)

- `persistence/crud/graph.py`: pass `batch_size=_BATCH_SIZE` in
  `batch_upsert_graph_nodes`, `batch_upsert_graph_edges`, `batch_upsert_graph_metrics`,
  `batch_upsert_graph_node_membership`.
- `persistence/crud/external_systems.py`: import `_BATCH_SIZE` and pass it in
  `batch_upsert_symbols`.
- `ingestion/graph/builder.py`: added `GraphBuilder.release_subgraph_caches()`.
- `generation/page_generator/orchestrate.py`: call `graph_builder.release_subgraph_caches()`
  in `_GenerationRun.__init__` right after the metric dicts are snapshotted.

Issue #2 remains out of scope for the same reason as before (architectural rework of
the finalize passes), and now additionally spans into persistence, since the same
`all_pages`/`PipelineResult` data stays live through both. #4a/#4b are unchanged from
the original pass.
