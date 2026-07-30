# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

repowise indexes a codebase once and builds five intelligence layers — dependency
**graph**, **git** history signals, generated **docs**, architectural **decisions**, and
**code health** — then serves them over MCP, a CLI, a REST API and a local dashboard.

It is a dual-language monorepo: Python does all the analysis and generation, TypeScript
does the dashboard and the VS Code extension. There is **one** shipped PyPI
distribution (`repowise`), assembled by `pyproject.toml` from three separate `src/`
roots via `[tool.setuptools.package-dir]`. Version lives in the root `pyproject.toml`.

## Commands

Python (uv workspace — `packages/core`, `packages/cli`, `packages/server`):

```bash
uv sync --all-packages              # install everything
uv sync --all-packages --all-extras # + dev deps

uv run pytest tests/unit/           # the usual inner loop
uv run pytest tests/providers/      # provider tests, no API keys needed
uv run pytest tests/integration/    # uses the sample_repo fixture
uv run pytest tests/e2e/            # full init + update flows

uv run pytest tests/unit/test_ids.py                     # one file
uv run pytest tests/unit/test_ids.py::test_name          # one test
uv run pytest tests/unit/health/ -k marker -x            # filter + fail fast
uv run pytest tests/integration/test_health_perf_benchmark.py -m slow

uv run ruff check packages/ tests/
uv run ruff format packages/ tests/
uv run mypy packages/core/src packages/cli/src packages/server/src
```

Node (npm workspaces — `types`, `ui`, `api-client`, `web`, `vscode`):

```bash
npm ci
npm run type-check --workspace packages/web
npm run lint --workspace packages/web
npm run lint:shared                                  # see "framework-free" below
npm run test --workspace @repowise-dev/api-client    # also @repowise-dev/{types,ui}
npm run dev --workspace packages/web                 # dashboard on :3000
npm run test --workspace packages/vscode             # needs a display; CI uses xvfb-run
```

`make <target>` wraps most of the above, but the `Makefile` assumes a POSIX shell
(`find`, `/tmp`) — on Windows call `uv run` / `npm run` directly.

CI (`.github/workflows/ci.yml`) gates: pytest `providers/` + `unit/` on Python
3.11/3.12/3.13, `integration/` on push to main only, web lint + type-check,
`lint:shared`, the three shared-package test suites, and a VS Code build + smoke test.
`pre-commit` runs ruff and mypy; `pytest tests/unit/ tests/providers/` runs on pre-push.

## Architecture

### Dependency direction

`core ← server ← cli`. `core` imports neither of the other two — keep it that way.
`server` depends on `core`; `cli` depends on both (it embeds the MCP server for
`repowise mcp`). Everything is importable as `repowise.core.*`, `repowise.server.*`,
`repowise.cli.*` (namespace packages across three src roots).

### The three stores

Each answers a question the others cannot answer efficiently. All three are
rebuildable from source, which is why none of them is a server.

| Store | Answers | Backend |
|---|---|---|
| SQL | what exists, what changed, when | SQLAlchemy async + aiosqlite (SQLite) or asyncpg (Postgres); Alembic migrations |
| Vector | what is semantically similar | LanceDB embedded (`.repowise/lancedb/`) when SQLite; pgvector column when Postgres |
| Graph | how things connect, what is central | NetworkX in-process → `.repowise/graph.json`; `graph_nodes`/`graph_edges` SQL tables above ~30K nodes |

The `VectorStore` abstraction picks its backend from `DATABASE_URL` at startup.

### The pipeline is the spine

`core/pipeline/orchestrator.py::run_pipeline()` is the single entry point for indexing,
analysis and generation — `repowise init`, `repowise update`, the server's
`job_executor.py` and the webhook path all call it. `core/pipeline/persist.py::
persist_pipeline_result()` is the shared write path for CLI and server. If you are
adding an analysis stage, it goes through the orchestrator, not into a command.

Ingestion order matters: `FileTraverser` → `ASTParser` → `GraphBuilder` →
`CallResolver` → `GitIndexer` (which adds `co_changes` edges) → analysis
(health, dead code, decisions, risk) → optional LLM generation.

### Two operational modes

- **Init** — first full index. Resumable via the job state machine.
- **Maintenance** — incremental, driven by git diff and change propagation through the
  graph. Only pages the diff actually reaches get regenerated, bounded by a cascade
  budget (default 30 highest-PageRank pages); the rest defers to a nightly job.

### Layer map

| Concern | Where |
|---|---|
| Traversal, AST, graph, call resolution, git mining | `packages/core/src/repowise/core/ingestion/` |
| Health, dead code, decisions, change risk, coupling, security, KG | `packages/core/src/repowise/core/analysis/` |
| Page generation, context assembly, Jinja2 prompts, editor files | `packages/core/src/repowise/core/generation/` |
| LLM providers + rate limiter | `packages/core/src/repowise/core/providers/` |
| CLI commands (click + rich) | `packages/cli/src/repowise/cli/commands/` |
| REST routers, MCP tools, scheduler, job executor | `packages/server/src/repowise/server/` |
| Dashboard (Next.js 15 App Router) | `packages/web/src/` |

The MCP surface is **ten task-shaped tools** (`tool_*` modules under
`server/mcp_server/`), deliberately capped — a small surface is easier for an agent to
choose from. Tools are batch-oriented (`get_context(targets)`), not entity-oriented.

## Conventions that are easy to violate

- **One `ASTParser`, no per-language subclasses.** Language differences live in
  `packages/core/queries/<lang>.scm` and `LANGUAGE_CONFIGS`. The parser must never grow
  an `if lang == "python"` branch. Adding a language = one `.scm` file + one config
  entry; recipe in `docs/layers/LANGUAGE_SUPPORT.md`.
- **No provider SDK imports outside `core/providers/`.** Every LLM call goes through
  `BaseProvider`. Adding a provider touches a fixed list of registration sites
  (`registry.py`, `rate_limiter.py`, `provider_config.py`, `provider_selection.py`,
  `helpers.py`, plus two web components) — the checklist is in
  `.github/CONTRIBUTING.md`.
- **Prompts are Jinja2 templates**, not inline strings, so users can override them from
  `.repowise/prompts/`. Same for editor files (`templates/claude_md.j2`).
- **Async-first.** All DB access is async SQLAlchemy; do not block the event loop.
- **`packages/ui` and `packages/api-client` must stay framework-free** — no `next` /
  `next/*` imports. `npm run lint:shared` enforces this and it is a CI gate.
- **No raw hex in UI code.** Colors come from the semantic design tokens; see
  `docs/design/theme-tokens.md` and `packages/ui/scripts/ui-gates.sh` (no-raw-hex +
  WCAG contrast gates).
- **Exclusion is three layers** (root `.repowiseIgnore`, per-directory
  `.repowiseIgnore`, runtime `extra_exclude_patterns` from `-x` / config / repo
  settings), all `pathspec` gitwildmatch. Don't add a fourth mechanism.
- **`save_config()` round-trips YAML** — merge into the existing file, never overwrite,
  or Web-UI-set keys get dropped on the next `init`.
- Analysis layers are **deterministic and LLM-free** (health, dead code, risk, seven of
  the eight decision sources). Keep LLM calls out of the indexing path; they belong in
  generation or in explicit on-request commands.
- `docs/CHANGELOG.md` is mirrored into
  `packages/core/src/repowise/core/upgrade/_data/CHANGELOG.md` and a drift-guard test in
  `tests/unit/upgrade/` fails if they diverge — update both.

## `CLAUDE.md` files here

repowise generates `.claude/CLAUDE.md` itself (`repowise generate-claude-md`, and
automatically after `init` / `update`), writing only between
`<!-- REPOWISE:START ... -->` and `<!-- REPOWISE:END -->` markers; anything outside
them is never touched. `.claude/` is gitignored, so that file is local-only — **this**
root `CLAUDE.md` is the tracked, hand-written one and repowise will not rewrite it.
Editor-file generation internals: `docs/architecture/editor-files.md`.

## Tests

`asyncio_mode = "auto"`, so async tests need no marker. `tests/unit/` avoids the
filesystem and never calls an LLM; `tests/integration/` uses the `sample_repo` fixture
(`tests/conftest.py`); `tests/e2e/` runs real init/update flows; `tests/providers/`
runs without API keys. Fixture repos under `tests/fixtures/` include multi-language and
.NET solution/workspace trees — their `obj/` build output is not tracked.

## Commits and branches

Conventional Commits with an optional scope (`feat(cli): add --resume to init`,
`fix(health): bound duplication detection`), imperative subject under ~72 chars.
Branch prefixes: `feat/`, `fix/`, `chore/`, `refactor/`. PRs target `main` and need a
code-owner approval.

## Where to read more

- `docs/architecture/ARCHITECTURE.md` — the end-to-end system reference (stores,
  pipelines, MCP/REST/UI, and the *why* behind each design decision)
- `docs/layers/INTELLIGENCE_LAYERS.md` — what each of the five layers computes
- `docs/reference/CLI_REFERENCE.md` · `docs/reference/CONFIG.md` — every command, flag
  and config key
- `docs/agent/MCP_TOOLS.md` — the ten tools and worked multi-tool examples
- `docs/README.md` — the full documentation index
