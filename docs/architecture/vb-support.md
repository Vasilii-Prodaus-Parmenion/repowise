# VB.NET Support via Roslyn

Plan of record for adding Visual Basic .NET (`.vb`) as an analysed language.
VB is the first language whose AST comes from **outside the Python process** — a
long-lived Roslyn sidecar rather than a tree-sitter grammar. This document
covers the decisions taken, the seams each one lands on, and the phased work
breakdown.

Status: **implemented — all 5 phases** (see §9 for the phase breakdown and
what's deliberately deferred beyond v1).

Related reading: [language-support.md](language-support.md) (the general
add-a-language recipe, which VB deliberately departs from),
[ARCHITECTURE.md](ARCHITECTURE.md).

---

## 1. Why not tree-sitter

The normal recipe is one `.scm` file plus one `LanguageConfig` entry. VB cannot
follow it:

- There is no maintained, packaged tree-sitter VB.NET grammar on PyPI. The
  candidates that exist are incomplete and unversioned.
- VB's grammar is genuinely hostile to the tree-sitter style: statement-
  terminated blocks (`End Sub`, `End If`), significant line continuations,
  case-insensitive keywords *and* identifiers, `Handles` clauses, `WithEvents`
  fields, XML literals, and `#If` conditional compilation. Hand-writing and
  maintaining a grammar for it is not a reasonable ongoing cost.
- Roslyn's `Microsoft.CodeAnalysis.VisualBasic` is the reference parser, ships
  from Microsoft, and is exactly as correct as the compiler.

The cost is a .NET runtime dependency and a process boundary. The user-facing
consequence — a hard prerequisite — is accepted deliberately (D3, §5.4).

---

## 2. Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | **Syntax trees only.** `VisualBasicSyntaxTree.ParseText` per file. No `VisualBasicCompilation`, no `MetadataReference`s, no semantic model, no MSBuild evaluation, no NuGet restore of the *target* repo. | Parity with what tree-sitter gives every other language, at a fraction of the complexity. Works on repos that do not restore or build. Keeps the sidecar stateless, which is what makes batching and restart-on-crash cheap. |
| D2 | **Sidecar C# source ships in the wheel; built on first use** into `.repowise/roslyn-sidecar/<key>/`, cached by key. | Keeps the single PyPI distribution pure-Python and small for the ~all users with no VB. Costs one ~20–40 s `dotnet build` per repo per repowise version. |
| D3 | **Preflight abort.** If the traversal census counted ≥1 `.vb` file and the dotnet SDK is missing or below minimum, `run_pipeline()` raises *before* the parse phase. Escape hatch: exclude `.vb` via `-x`/config. | Nothing gets half-indexed. The failure names the requirement and the escape hatch. |
| D4 | **VB only; C# stays on tree-sitter** — but the sidecar protocol carries a `language` field and is written language-neutral so C# can move later without a protocol break. | Smallest blast radius. C#'s working `.scm`, bindings, heritage, member-reads, MVVM synthetic symbols and perf dialect are untouched. |
| D5 | **Sidecar returns code-health metrics**; the health walker gains a non-tree-sitter branch, exactly as `sql` already has. | Otherwise VB scores nothing in code health — which is the layer legacy-VB owners most want. |
| D6 | **Run-scoped lifetime**, one sidecar process per project per `run_pipeline()`, torn down in a `finally`. | No PID files, no orphan reaping, no version skew. `repowise watch` holds one open across its update cycles because it is itself one long-lived Python process. |
| D7 | **Extend the existing dotnet resolver to `.vbproj`**, including `RootNamespace` and project-level `<Import Include="…">`. | A VB file's effective namespace is frequently declared nowhere in the file. Without this, qualified names are wrong and import edges do not resolve. |
| D8 | **`Handles`/`WithEvents`/`AddHandler` edges and designer-partial pairing ship in v1.** | Without them the dead-code analyser flags every event handler and every `.Designer.vb` half. That is not a cosmetic false-positive rate; it is the whole UI layer of a typical WinForms/WebForms app. |

### Language tag

Tag `vbnet`, display name `VB.NET`, extension `.vb`. (`csharp`/`fsharp` spell
the language out, which would argue for `visualbasic`; `vbnet` is chosen
because it distinguishes VB.NET from VB6/VBA, which repowise does not support.
One-line change if we prefer otherwise — it is referenced from the spec, the
`LanguageTag` literal, and the dispatch sets.)

---

## 3. Architecture

```
FileTraverser
      |  .vb → language "vbnet" (LanguageSpec)
      v
Preflight (D3): .vb count > 0  ->  require dotnet SDK >= 8.0  else RAISE
      v
Parse phase (pipeline/phases/ingestion.py)
      |
      +-- non-VB files ----> ProcessPoolExecutor (spawn) -> ASTParser  [unchanged]
      |
      +-- VB files --------> VbSidecarClient.parse_batch()  (parent process)
                                    |
                                    | NDJSON over stdin/stdout
                                    v
                         dotnet RepowiseVb.dll  (one per project, run-scoped)
                                    |
                                    | Parallel.ForEach over the batch
                                    v
                         VisualBasicSyntaxTree.ParseText
                                    |
                                    +-> symbols / imports / calls / heritage
                                    +-> Handles + AddHandler wiring
                                    +-> complexity metrics (D5)
                                    v
                         one JSON response per file
      v
merged back into traversal order -> GraphBuilder.add_file()  [unchanged]
      v
Import resolution: resolvers/dotnet (now .vbproj-aware, D7)
      v
Health phase: engine._walk() -> walk_vb_file() reads cached sidecar metrics
```

The two parallel branches matter: VB files are **excluded from the process
pool** rather than merely optimised out of it. A `spawn`ed worker calling
`ASTParser.parse_file` on a `.vb` file would start its own sidecar — N workers,
N `dotnet` processes, N first-use builds racing each other. Parallelism for VB
is recovered *inside* the sidecar via `Parallel.ForEach` over each batch.

---

## 4. The sidecar

### 4.1 Layout

```
packages/core/src/repowise/core/ingestion/vb/
  __init__.py
  sidecar.py            # VbSidecarClient: spawn, handshake, batch, shutdown
  build.py              # first-use dotnet build + cache dir + build lock
  preflight.py          # dotnet SDK discovery / version gate (D3)
  parse.py              # sidecar JSON -> ParsedFile (Symbol/Import/CallSite/…)
  complexity.py         # sidecar JSON -> FileComplexity + walk_vb_file()
  handles.py            # Handles/WithEvents/AddHandler -> DynamicEdge
  sidecar_src/
    RepowiseVb.csproj   # net8.0, Microsoft.CodeAnalysis.VisualBasic
    Program.cs          # NDJSON read loop, dispatch, Parallel.ForEach
    Protocol.cs         # request/response DTOs + System.Text.Json context
    VbExtractor.cs      # syntax walk -> symbols/imports/calls/heritage
    HandlesExtractor.cs # Handles clauses, WithEvents fields, AddHandler
    Metrics.cs          # CCN / cognitive / nesting / NLOC / class metrics
```

`sidecar_src/**` must be added to wheel package data. The root
`pyproject.toml` assembles three `src/` roots via
`[tool.setuptools.package-dir]`; non-`.py` files under a package need an
explicit `[tool.setuptools.package-data]` entry (the `queries/*.scm` files are
the precedent to copy).

### 4.2 Protocol

Newline-delimited JSON, one message per line, request/response correlated by
`id`. `stdout` is the transport and carries nothing else; the sidecar writes
diagnostics to `stderr`, which the client drains onto `structlog` at `debug`.

**Handshake** — sent immediately after spawn, before any parse:

```json
→ {"id":1,"op":"hello","protocol":1}
← {"id":1,"ok":true,"protocol":1,"sidecar":"0.1.0","roslyn":"4.11.0","runtime":"8.0.10"}
```

A `protocol` mismatch is a hard error, not a negotiation: the sidecar is built
from source shipped in the same wheel, so a mismatch means a stale cache dir
and the fix is to rebuild.

**Parse** — one request per chunk of files:

```json
→ {"id":7,"op":"parse","language":"vbnet","files":[
     {"path":"src/Forms/MainForm.vb","absPath":"C:\\repo\\src\\Forms\\MainForm.vb",
      "rootNamespace":"Acme.App"}
   ]}
← {"id":7,"ok":true,"results":[
     {"path":"src/Forms/MainForm.vb",
      "symbols":[…],"imports":[…],"calls":[…],"heritage":[…],
      "eventWiring":[…],"docstring":"…","parseErrors":[],
      "complexity":{"functions":[…],"classes":[…],"fileNloc":412,
                    "errorHandlingHits":[…],"perfHits":[…]}}
   ]}
```

The request carries `absPath`, not file text: the sidecar reads the bytes
itself. This is deliberate. Legacy VB source is frequently **not** UTF-8 —
Windows-1252 and UTF-16-with-BOM are both common — and .NET's
`SourceText.From(stream, Encoding.Default, detectEncodingFromByteOrderMarks:
true)` handles that better than guessing on the Python side. The cost is that
`.vb` files are read twice per run (once by `_read_sources` for the
content-hash, once by the sidecar). That is acceptable; `.vb` is a small
fraction of any repo we expect, and the alternative is escaping whole files
through a JSON pipe.

`rootNamespace` comes from the owning `.vbproj` (D7) and is per-file because a
solution can contain several VB projects with different root namespaces.

**Shutdown** — `{"id":N,"op":"shutdown"}`, then wait briefly, then kill.

### 4.3 Batching, failure, timeouts

- Chunk size 64 files per request. Bounds peak memory on both sides and lets
  the parse progress bar tick per chunk rather than once at the end.
- Per-request timeout scaled to chunk size (e.g. `10 s + 0.5 s/file`).
- On crash or timeout: restart the sidecar once and retry the chunk. A second
  failure raises — consistent with D3's "either analysed properly or not at
  all" contract, rather than silently emitting an empty `ParsedFile` and
  reporting a VB codebase as having no symbols.
- A single *file* that Roslyn cannot parse is not a crash: it comes back with
  populated `parseErrors` and whatever partial symbols the recovery produced,
  exactly like a tree-sitter `ERROR` node.

### 4.4 First-use build (D2)

Cache key: `sha256(repowise_version, protocol_version, hash(sidecar_src/**))`,
directory `.repowise/roslyn-sidecar/<key>/`.

1. If `<key>/RepowiseVb.dll` exists → use it.
2. Otherwise acquire an exclusive lock (`<...>/.build.lock`, same
   liveness-probe pattern as `core/update_lock.py` + `procutils.py`, so a
   crashed build does not wedge the directory forever), build into a temp dir
   with `dotnet build -c Release --nologo`, then atomically rename into place.
   Concurrent `repowise` processes on the same repo therefore build once.
3. Build failure → raise with the captured `dotnet` output. The overwhelmingly
   likely cause is no network on first use (NuGet must fetch
   `Microsoft.CodeAnalysis.VisualBasic` once), so the message must say so.

Escape hatch: `REPOWISE_ROSLYN_SIDECAR=/path/to/dir` skips the build and uses a
prebuilt sidecar. This covers air-gapped installs, CI images that pre-seed it,
and local sidecar development.

---

## 5. Python-side integration

### 5.1 Language registration

Follows the standard recipe (§"Adding a new language" in
[language-support.md](language-support.md)) with two deliberate omissions:

- `languages/specs/vbnet.py` exporting `SPEC`, registered in `ALL_SPECS`.
  `grammar_package=None`, `scm_file=None`, `is_passthrough=False`,
  `import_support="full"`, `extensions={".vb"}`,
  `entry_point_patterns=("Program.vb", "Main.vb")`,
  `manifest_files=("*.vbproj",)`, `blocked_dirs=("bin", "obj", "My Project")`,
  plus the .NET `test_dir_suffixes=(".Tests",)` convention C# already declares.
- `"vbnet"` added to the `LanguageTag` literal (`ingestion/models.py:100`).
- **No `queries/vbnet.scm`** and **no `LANGUAGE_CONFIGS` entry** — there is no
  tree-sitter grammar to query. This is the one place VB breaks the recipe, and
  it needs a comment in `language_configs.py` saying so, or the next person
  will read the absence as an oversight.

### 5.2 Dispatch out of `ASTParser`

`ASTParser.parse_file` already routes non-tree-sitter languages before the
grammar lookup (`parser.py:241`, `SPECIAL_HANDLER_LANGUAGES` at
`special_handlers.py:27`). VB gets the same treatment: a `vbnet` branch that
delegates to the run-scoped sidecar client for a single file.

This single-file path is **not** how the bulk parse phase works — it exists for
the two other `parse_file` callers, both of which run in the parent process and
handle one file at a time:

- `pipeline/incremental.py:127`
- `pipeline/reparse.py:53`

The client is a per-repo singleton (`get_sidecar(repo_path)`), so these paths
join whatever sidecar the run already started, and start one if not.

A guard in `_parse_one` (`pipeline/phases/ingestion.py:135`) should raise on a
`vbnet` `FileInfo` rather than silently starting a sidecar inside a spawned
worker. That is a programming error, and it should look like one.

### 5.3 Parse-phase partition

In `pipeline/phases/ingestion.py`, after `_split_cached` (~line 337) splits
`to_parse` into cache hits and misses, partition the misses by language:

```
to_parse  ->  vb_misses  +  other_misses
```

`other_misses` feeds the existing `ProcessPoolExecutor` unchanged.
`vb_misses` feeds `VbSidecarClient.parse_batch()` as an awaitable that runs
**concurrently with the pool** (both are awaited together), so a mixed
C#/VB solution does not serialise the two halves. Results merge into the same
`merged: dict[int, Any]` keyed by traversal position, so everything downstream
— `_cache_parsed`, `graph_builder.add_file`, ordering — is untouched.

The parse cache (`ingestion/parse_cache.py`) needs no structural change: it
pickles `ParsedFile` graphs keyed by content hash. But its cache-version hash
mixes in the `.scm` sources, and VB has none — so the **sidecar source hash and
protocol version must be mixed into `_cache_version()`**, or a sidecar change
will serve stale VB parses. `PARSER_SCHEMA_VERSION`
(`core/upgrade/version.py:45`) should also be bumped when VB lands, since
`ParsedFile` gains no new fields but its VB contents change meaning.

### 5.4 Preflight (D3)

New `vb/preflight.py`, called from the orchestrator between traversal and the
parse phase — the traverser's `stats.lang_counts` already carries the per-
language file count needed to decide.

- Discovery: `dotnet --version`, then `dotnet --list-sdks` to check the
  floor. Minimum **SDK 8.0** (the sidecar targets `net8.0`).
- Respect `DOTNET_ROOT` and `PATH`; on Windows also probe the default
  `%ProgramFiles%\dotnet\dotnet.exe` before giving up, because a machine can
  have the SDK installed and not on `PATH`.
- Raise a dedicated `DotnetSdkMissingError` carrying: what was looked for,
  what was found, the `.vb` file count, the install URL, and the
  `-x '**/*.vb'` escape hatch.
- Add the same check as a **non-fatal advisory** in `repowise doctor`
  (`packages/cli/src/repowise/cli/commands/doctor_cmd/`), so users can discover
  the requirement before an `init` aborts on it. The advisory is only
  interesting when the repo actually contains `.vb`.

### 5.5 Symbol mapping

`SymbolKind` (`models.py:100`) is a closed literal and should not grow for VB.
Mapping:

| VB declaration | `SymbolKind` | Notes |
|---|---|---|
| `Class` | `class` | |
| `Module` | `module` | VB `Module` = static class; members are implicitly shared |
| `Structure` | `struct` | |
| `Interface` | `interface` | |
| `Enum` | `enum` | |
| `Sub` / `Function` (in a type) | `method` | `parent_name` = containing type |
| `Sub` / `Function` (in a `Module`) | `function` | Callable unqualified, so `function` is the honest kind |
| `Property` | `method` | Matches how C# properties are already emitted |
| `Event` | `method` | Referenced by `Handles`/`AddHandler`, so it must be a node |
| `Operator` | `method` | |
| `Delegate` | `type_alias` | |
| `Const` | `constant` | |
| Module-level `Dim`/`Public` field | `variable` | |

Case-insensitivity is the trap. VB matches identifiers case-insensitively, so
`FooBar`, `foobar` and `FOOBAR` are the same symbol, and real codebases are
inconsistent about it. **Symbol IDs and the import/call/heritage name index
must be matched case-insensitively for VB**, while `Symbol.name` keeps the
declaration's original casing for display. This affects `graph/_resolvers.py`
and the dotnet namespace map, which are currently case-sensitive. Getting this
wrong produces a graph that silently loses roughly half its call edges, so it
belongs in the first phase, not a follow-up.

Other extraction notes:

- **Docstrings**: VB uses `'''` XML doc comments. The same XML-doc shape C#
  already handles — reuse the summary-extraction logic rather than writing a
  second XML-doc reader.
- **Visibility**: `Public`/`Private`/`Protected`/`Friend`/`Protected Friend`.
  `Friend` maps to `internal`. VB's default for a class member with no modifier
  is `Public`, unlike C#.
- **`Async`** → `Symbol.is_async`.
- **`Partial`**: emit each part, and let the designer-pairing pass (§6.2) link
  them. Do not merge them into one symbol; line ranges must stay real.
- **`#If` blocks**: parse with no preprocessor symbols defined, so inactive
  branches become disabled-text trivia. Symbols inside them are not emitted.
  Documented limitation, matching what tree-sitter does for C `#ifdef`.
- **`Imports` aliases** (`Imports IO = System.IO`) → `NamedBinding`, so
  alias-qualified calls resolve.

---

## 6. VB idioms (D8)

### 6.1 Event wiring

```vb
Private WithEvents Button1 As Button
Private Sub Button1_Click(sender As Object, e As EventArgs) Handles Button1.Click
```

Nothing in the file *calls* `Button1_Click`. Without event-wiring edges, every
handler in the codebase is unreferenced and the dead-code analyser reports the
UI layer as dead. Three sources to cover:

1. `Handles X.Y` clauses on a method declaration.
2. `WithEvents` field declarations (which is what makes `Handles` legal).
3. `AddHandler expr, AddressOf Method` / `RemoveHandler`.

The sidecar returns these as an `eventWiring` list per file; `vb/handles.py`
turns them into `DynamicEdge`s (`dynamic_hints/base.py`) with
`edge_type="dynamic"` and `hint_source="vb_handles"`, registered in the
`HintRegistry`. Handler methods then carry an inbound edge and survive dead-code
analysis on evidence rather than on a suppression list.

Cross-file case: `Handles` can reference a control declared in the
`.Designer.vb` partial. Resolution is per *type*, not per file — the wiring
edge targets the handler symbol, and the `WithEvents` field may live in the
sibling partial. Resolve against the merged partial-type view (§6.2), not the
single file.

### 6.2 Designer partials and code-behind

- `Foo.Designer.vb` ↔ `Foo.vb`: generated `Partial Class` half. Pair them by
  stem so neither is reported dead because "nothing references it", and so
  `Handles` resolution can see both halves.
- `Foo.aspx` / `Foo.ascx` ↔ `Foo.aspx.vb` / `Foo.ascx.vb`: WebForms
  code-behind. The `.aspx` is the entry point; the `.vb` is reached only
  through it.
- `My Project/` (`Application.Designer.vb`, `Resources.Designer.vb`,
  `Settings.Designer.vb`): fully generated. Blocked via the spec's
  `blocked_dirs`, so they never enter the index at all.

The pairing belongs in the same place the C# / XAML equivalents live —
`dynamic_hints/` for the edges, plus `generated_suffixes=(".Designer.vb",)` on
the spec so the existing generated-file machinery applies.

### 6.3 Late binding

`Option Strict Off` plus `Object`-typed variables means some calls are
genuinely unresolvable without a semantic model, and with D1 we do not have
one. These are simply not emitted. Consistent with every other language's
dynamic-dispatch gap; worth one line in the user-facing support matrix rather
than a heuristic that invents edges.

---

## 7. Import resolution (D7)

VB reuses the existing `resolvers/dotnet/` stack, which needs three changes:

1. `resolvers/dotnet/msbuild.py` — `find_csproj_files` (line 116) and
   `parse_csproj` (line 76) are `.csproj`-only by name and by glob. Generalise
   to a project-extension set `{.csproj, .vbproj}`. Rename the public helpers
   (`find_project_files` / `parse_project`) with the old names kept as
   aliases, since `MSBuildProject` is already extension-agnostic in shape.
2. `resolvers/dotnet/solution.py:56` hard-filters solution entries to paths
   ending `.csproj`. Accept `.vbproj` too, so VB projects join the project
   graph and `ProjectReference` edges cross the C#/VB boundary — which is the
   common shape in a migrating codebase.
3. **New**: read `<RootNamespace>` and project-level
   `<Import Include="System.Linq" />` items from the `.vbproj`. VB is unusual
   here: `RootNamespace` is prepended to every declared namespace (and *is* the
   namespace for files that declare none), and project-level imports are in
   scope in every file without appearing in it. Without both, qualified names
   are wrong and a large share of import edges do not resolve. C# has an
   analogue in `resolvers/dotnet/global_usings.py`, which is the right module to
   extend rather than duplicate.

A new `resolvers/vbnet.py` registered in `resolvers/__init__.py`'s `_RESOLVERS`
handles `Imports` statements against the shared dotnet namespace index. It is
thin: the namespace map, project index and global-usings machinery are already
built and are language-neutral once (1)–(3) land.

Also worth having, mirroring `languages/csharp_same_namespace.py`: a
same-namespace pass for VB, since VB code leans on implicit same-namespace
visibility even more than C# does.

---

## 8. Code health (D5)

`HealthAnalyzer._walk` (`analysis/health/engine.py:715`) already branches for a
language with no tree-sitter grammar:

```python
if language == "sql":
    from .sql_complexity import walk_sql_file
    return walk_sql_file(pf.file_info, source)
return walk_file(path, language, source)
```

VB slots in identically — `walk_vb_file(pf.file_info, source)` in
`ingestion/vb/complexity.py`. `sql_complexity.py` is the precedent to follow in
shape.

The difference: `walk_sql_file` computes metrics itself, whereas VB's come from
the sidecar during the parse phase — and by the time health runs, that phase is
over. So the parse phase stashes each file's `complexity` payload in a
run-scoped map keyed by absolute path, and `walk_vb_file` is a lookup that
converts JSON → `FileComplexity`. Misses (a file health sees but parse did not,
or a run where the map was not populated) return an empty `FileComplexity`,
which is the documented degrade-to-silence contract.

The map has to be reachable from both phases. It rides on the sidecar client
singleton (already per-repo and run-scoped), which keeps it out of the pipeline
result object and gives it the right lifetime for free.

**What ships**, computed in `Metrics.cs` from the syntax tree:

- `FunctionComplexity`: `ccn`, `cognitive`, `max_nesting`, `nloc`,
  `param_count`, `bumps`, `complex_conditions`. CCN counts `If`/`ElseIf`, each
  `Case`, `While`, `Do`, `For`, `For Each`, `Catch`, `AndAlso`/`OrElse`, and
  the `If()` ternary.
- `ClassComplexity`: member counts, WMC, and LCOM4. LCOM4 needs a
  field/method access graph, which without a semantic model is approximated by
  matching identifiers (case-insensitively) against the declared field and
  method names of the type. **This is an approximation**, and the god-class /
  Extract-Class detectors consuming it should be treated as lower-confidence
  for VB until measured against a real codebase.
- `file_nloc`.
- `error_handling_hits`: `Try`/`Catch` shapes — empty catch, catch-and-swallow,
  `Catch ex As Exception` blanket catch, `On Error Resume Next`. The last is
  VB-specific and worth flagging loudly; it is the strongest "this code
  predates structured error handling" signal there is.
- `perf_hits`: a starter set only — string concatenation in a loop
  (idiomatic and expensive in VB), `.Result`/`.Wait()` inside an `Async`
  method, and per-iteration `New Regex`.

**What does not ship**, stated plainly so it is not a surprise: the
`perf/dialects/` and `dataflow/dialects/` plugin protocols both walk
`tree_sitter.Node` objects, so a VB dialect cannot be registered without
either a second cross-process protocol or an in-Python VB AST. VB therefore
gets no cross-function N+1 detection and no Extract Method refactoring
suggestions in v1. Both are additive later — the sidecar already owns the tree
and could emit `PerfFnFacts` — and both degrade to silence, not to wrong
findings.

---

## 9. Work breakdown

Five phases, each independently reviewable. Phases 1–3 are the minimum for a
useful VB index; 4–5 are what make it not embarrassing on a real WinForms app.

**Phase 1 — sidecar skeleton and the process boundary — IMPLEMENTED**
- `sidecar_src/` project, `hello` + `shutdown` ops, NDJSON loop.
- `vb/build.py` (cache key, build lock, atomic rename, `REPOWISE_ROSLYN_SIDECAR`).
- `vb/sidecar.py` client: spawn, handshake, stderr drain, restart-once, teardown.
- `vb/preflight.py` + `DotnetSdkMissingError` + `doctor` advisory.
- Wheel `package-data` for `sidecar_src/**`.

  Verified end-to-end on a machine with the .NET SDK installed: cold-cache
  build, handshake, and shutdown all round-trip through a real `dotnet`
  process. Covered by `tests/unit/ingestion/vb/` (preflight discovery, build
  cache/lock semantics, sidecar client singleton) — no SDK or subprocess
  required for that suite. Not yet wired into `run_pipeline()` or the parse
  phase: VB is not yet a registered language (`LanguageTag`, `specs/vbnet.py`),
  so there is nothing for the preflight abort to gate until Phase 2 lands.

**Phase 2 — symbols, imports, calls, heritage — IMPLEMENTED**
- `specs/vbnet.py`, `LanguageTag`, the `language_configs.py` "no grammar here,
  on purpose" comment.
- `VbExtractor.cs`; `vb/parse.py` JSON → `ParsedFile`.
- Case-insensitive identity for VB in `graph/_resolvers.py` and the dotnet
  namespace map.
- `parse_file` `vbnet` branch; `_parse_one` guard.
- Parse-phase partition; sidecar hash into the parse-cache version;
  `PARSER_SCHEMA_VERSION` bump.

  The sidecar now answers `"parse"` requests: `VbExtractor.cs` walks the
  Roslyn syntax tree (no semantic model, per D1) and emits symbols, `Imports`
  bindings, invocation call sites, and `Inherits`/`Implements` heritage,
  matching the §5.5 mapping table (`Module` members → `function`, type
  members → `method`, properties/events/operators → `method`, `Delegate` →
  `type_alias`). Verified with a hand-built sample file round-tripped
  through the real, freshly-`dotnet build`-compiled sidecar (hello → parse →
  shutdown), not just unit fixtures.

  Bulk ingestion (`pipeline/phases/ingestion.py`) now partitions `to_parse`
  into `vb_misses`/`other_misses` right after the parse-cache split: VB files
  run through the run-scoped sidecar (`get_sidecar` + `parse_batch`, chunked
  at 64 files per request) concurrently with `ProcessPoolExecutor` parsing
  everything else, and `reparse_for_resume` gets the identical treatment.
  `_parse_one` raises defensively if a `vbnet` `FileInfo` ever reaches a
  spawned worker.

  The two single-file synchronous callers (`incremental.py`'s
  `build_repo_graph`, `reparse.py`'s `reparse_repo`) route through a new
  `ASTParser(repo_path=...)` constructor parameter and a `vbnet` branch in
  `parse_file` — but **not** through the shared `get_sidecar` singleton.
  Both callers may run on a thread that already has an asyncio loop active
  (`rebuild_graph_and_git` calls `build_repo_graph` synchronously on its own
  loop), and `asyncio.run()` cannot nest onto — or safely share a subprocess
  transport across — a second loop. `vb/sidecar.py`'s new `VbSyncBridge`
  sidesteps this by always driving a dedicated background thread with its
  own loop and its own standalone sidecar client, scoped to one call to
  `build_repo_graph`/`reparse_repo` (one process per call, not per file) and
  torn down via `ASTParser.close()`. This is a deliberate departure from
  D6's "join whatever sidecar the run already started" for these two paths
  specifically — accepted under D6's own risk note that a fresh sidecar
  startup is a low-severity, once-per-update cost.

  Case-insensitive identity lives in `call_resolver.py` and
  `heritage_resolver.py` (where symbol/import/call/heritage name indices are
  actually built and matched), not in `resolvers/dotnet/` — that stack only
  walks `.cs` files today (Phase 3 territory) and never sees VB source. A
  `_norm_key()` helper casefolds index keys and lookup keys for `vbnet`
  symbols only, using each file's own `file_info.language`; every other
  language's keys are untouched. `Symbol.name` itself is never casefolded,
  so display casing is unaffected. Covered by
  `tests/unit/ingestion/test_vb_case_insensitive_resolution.py`, including a
  same-language negative case pinning that C# stays case-sensitive.

  The parse cache's `parser_fingerprint()` now folds in the sidecar's wire
  protocol version and a hash of `sidecar_src/**` (via the newly-public
  `vb/build.py::sidecar_src_fingerprint()`), deliberately without
  `repowise_version` — same "an unrelated release must not churn the whole
  cache" principle `PARSER_SCHEMA_VERSION` already followed.
  `PARSER_SCHEMA_VERSION` is bumped to 2 so a store indexed under phase 1
  (VB registered nowhere, or empty `ParsedFile`s) re-parses under the new
  logic. Preflight (`ensure_vb_prerequisites`) is now actually called, from
  both `_run_ingestion` and `reparse_for_resume`, right after traversal
  stats are known and before the parse phase starts.

  Not yet wired: `.vbproj` `RootNamespace` threading (the sidecar protocol's
  `rootNamespace` field exists and is honored by `VbExtractor.cs`, but every
  Python-side caller sends `""` until Phase 3 lands the `.vbproj` reader).
  `eventWiring`/`complexity` are not yet on the wire (Phase 4/5 additions to
  the same `"parse"` op, no protocol bump needed). Covered by
  `tests/unit/ingestion/vb/test_parse.py` (JSON → `ParsedFile` mapping,
  `Module`-vs-`Class` kind, caller-symbol-id resolution by enclosing line
  range, language registration) — no SDK or subprocess required for that
  suite. Full pipeline (`init`/`update`) verification against a real VB
  fixture repo is deferred to the Phase 2/tests/integration item in §10;
  this sandbox's numpy/scipy install cannot run any test that reaches
  PageRank at all (a pre-existing, VB-unrelated environment issue), which
  blocked exercising `_run_ingestion` end-to-end here.

**Phase 3 — project awareness — IMPLEMENTED**
- `.vbproj` in `msbuild.py` and `solution.py`.
- `RootNamespace` + project-level `Imports` (extend `global_usings.py`).
- `resolvers/vbnet.py`; VB same-namespace pass.

  `msbuild.py`'s `find_csproj_files`/`parse_csproj` are now
  `find_project_files`/`parse_project` (old names kept as aliases —
  `index.py` and existing tests still import them), globbing and parsing
  both `.csproj` and `.vbproj` via a new `PROJECT_EXTENSIONS` set.
  `parse_project` gained an `Import`-tag branch: VB's ItemGroup-level
  `<Import Include="System.Linq" />` (there is no VB `global using`
  equivalent) lands in the same `project_usings` field C#'s
  `<Using Include=...>` already populates, so `global_usings.py` needed no
  changes at all — `collect_project_global_usings` was already
  extension-agnostic once its input included VB projects.
  `solution.py`'s `parse_sln` accepts either extension in a `.sln` entry,
  so a mixed C#/VB solution's `ProjectReference` graph is complete.

  `index.py`'s master file walk (`_walk_repo_ext_files`, parameterised on
  extension) now covers `.vb` alongside `.cs`, so `file_to_project`
  bucketing — and therefore RootNamespace and ProjectReference lookups —
  works identically for both languages. `build_namespace_map` itself stays
  C#-only (its regexes assume brace/semicolon syntax); VB namespaces are
  scanned separately with two new `namespace_map.py` functions
  (`declared_namespaces_vb`, `scan_type_declarations_vb`, VB syntax,
  case-insensitive keywords) and merged into the *same* `namespace_map`,
  RootNamespace-prefixed and stored under both original-case and
  casefolded keys — the latter is what makes VB's case-insensitive
  namespace matching (D8) and a mismatched-case cross-language import both
  resolve without maintaining two separate maps.

  `resolvers/vbnet.py` mirrors `resolvers/csharp.py` almost exactly
  (same project/ProjectReference/anywhere ranking, same NuGet-external and
  stem-match fallbacks), reusing `csharp.py`'s private repo-root/importer
  memoisation helpers directly rather than duplicating the cache — a
  mixed C#/VB repo shares one cache instead of paying the resolve cost
  twice. The one real difference: it tries an exact-case namespace lookup
  first, then a casefolded one, so `Imports` casing drift resolves.
  Registered in `resolvers/__init__.py`'s `_RESOLVERS["vbnet"]`.

  `languages/vb_same_namespace.py` mirrors `csharp_same_namespace.py`'s
  self-contained regex-driven scan (own-namespace tier + project-wide-Import
  tier, ambiguity/BCL/alias guards, "existing edge wins"), adapted for two
  VB-specific realities: every namespace compared is first made
  "effective" (RootNamespace-prepended, since VB's RootNamespace *is* the
  namespace for a file that declares none) and then casefolded throughout,
  since C#'s pass compares raw case-sensitive strings and VB cannot. Wired
  into `graph/_resolvers.py` (`ResolveMixin._resolve_vb_same_namespace`)
  and called from `builder.py` right after the C# pass.

  RootNamespace threading into the sidecar (flagged as a known Phase-2 gap
  — every Python-side caller sent `""`) is closed: a new
  `vb/rootns.py` (`build_vb_project_namespaces` + `root_namespace_for_file`)
  does a lightweight `.vbproj`-only scan — deliberately *not* the full
  `DotNetProjectIndex`, which doesn't exist yet during the parse phase and
  would otherwise be built twice per run. `vb/sidecar.py`'s `parse_batch`
  gained a `root_namespaces: dict[path, ns]` parameter alongside the
  existing single-value `root_namespace` kwarg, since §4.2 already
  documented that a solution can hold several VB projects with different
  root namespaces — a single string per batch was always going to be
  wrong for that case. `_parse_vb_batch` (bulk ingestion) now builds one
  project-namespace map per run and a per-chunk `root_namespaces` dict from
  it; `VbSyncBridge` (the single-file synchronous path used by
  `incremental.py`/`reparse.py` via `ASTParser`) lazily builds and caches
  the same map once per bridge instance rather than per file.

  Not yet wired: `type_map`/`partial_types` stay C#-only — VB's
  same-namespace pass builds its own throwaway type index per call instead
  of sharing `DotNetProjectIndex.type_map`, so partial-class co-fragment
  linking (§6.2's Designer-partial pairing) is Phase 4 territory, not
  this one. VB type declarations also aren't nesting-qualified the way
  C#'s are (no brace-depth tracking — VB has no braces); every VB type's
  `qualified` name equals its bare name, an accepted approximation given
  VB nests types far less than C#'s partial-class style does. Full
  `init`/`update` verification against a real mixed C#/VB fixture repo is
  still deferred to the Phase 2/tests/integration item in §10 for the same
  pre-existing, VB-unrelated sandbox reason noted there (this sandbox's
  numpy/scipy install cannot run anything that reaches PageRank, which
  blocks exercising `_run_ingestion` end-to-end here) — Phase 3's Python-side
  logic (project parsing, namespace resolution, same-namespace edges,
  RootNamespace lookup) is covered by `tests/unit/ingestion/test_vbnet_resolver.py`,
  `tests/unit/ingestion/test_vb_same_namespace.py`, and additions to
  `tests/unit/ingestion/vb/test_sidecar.py` / `tests/unit/pipeline/`, all
  against on-disk fixtures or recorded fakes — no SDK or subprocess
  required for any of it.

**Phase 4 — VB idioms — IMPLEMENTED**
- `HandlesExtractor.cs`; `vb/handles.py` → `DynamicEdge`s; registry entry.
- Designer/code-behind pairing; `generated_suffixes`; `My Project/` blocking.

  `HandlesExtractor.cs` (already present from an earlier pass on this
  branch) walks `Handles`/`AddHandler`/`RemoveHandler` and reports them as
  the `eventWiring` field on each "parse" response entry — wired into
  `VbExtractor.ParseFile` and the `Protocol.cs` DTOs. The Python side was
  the missing half: `vb/handles.py`'s `VbHandlesDynamicHints` reads that
  field back off a new `VbSidecarClient.file_results` map (raw per-file
  "parse" result DTOs, keyed by absolute path, populated by `parse_batch`
  — the same run-scoped-singleton pattern this document's health-map plan
  in §8 already called for, generalised to cover eventWiring too) and turns
  it into `DynamicEdge`s with `edge_type="dynamic"`, `hint_source="vb_handles"`,
  registered in `HintRegistry`.

  A `"handles"` entry's line is the handler method's *own* declaration line
  (`HandlesExtractor.cs` reports `MethodStatementSyntax.SpanStart`), so the
  target symbol is always resolvable in the *same* file — no cross-file
  `WithEvents`-field lookup needed to mark the handler reached; that
  resolution only matters for explaining *why* it fires, which is out of
  scope here. `"add_handler"`/`"remove_handler"` entries carry only the
  `AddressOf` target's bare name (no semantic model, D1), resolved
  case-insensitively against every VB method/function symbol parsed this
  run, preferring a same-file match and fanning out to every candidate on
  an ambiguous cross-file name (over-emission over under-emission, mirroring
  `dynamic_hints/dotnet.py`'s existing ambiguous-type-name handling).

  The edges target the handler's *symbol* id, not just its file — a
  file-level edge would only rescue `DeadCodeAnalyzer._detect_unused_exports`
  (its `file_dynamically_loaded` check), not the private handlers
  `_detect_unused_internals` flags on a narrower per-symbol "calls" check.
  That detector, and the per-symbol rescue list in `_detect_unused_exports`,
  both gained `"dynamic"` as a recognised evidence edge type alongside
  `"calls"` — a small, generically-useful analyzer fix (any dynamic-hint
  extractor's symbol-targeted edge now counts, not just VB's), not a
  VB-specific branch.

  Designer/code-behind pairing turned out to need no VB-specific work for
  the WinForms half: `.Designer.vb` already matches the spec's
  `generated_suffixes`, and the traverser's existing generated-file
  detection (`_is_generated`, shared machinery every `generated_suffixes`
  language already uses) drops such files from the index *entirely* before
  they can be flagged dead — the same mechanism C#'s `.Designer.cs` already
  relies on. `blocked_dirs=("bin", "obj", "My Project")` was already set on
  the spec and needed no further wiring; it's covered by the same
  traversal-level mechanism.

  The WebForms half (`.aspx`/`.ascx` ↔ its code-behind) *did* need new
  code, since the code-behind `.vb`/`.cs` file is a real, fully-indexed
  file with no static referrer anywhere in the repo — the ASP.NET runtime
  resolves it via the markup's `CodeBehind`/`CodeFile` attribute at request
  time. A new passthrough `aspx` language (`.aspx`/`.ascx`/`.master`,
  `languages/specs/aspx.py`, mirroring `specs/xaml.py`'s shape exactly — a
  markup file node with no AST, existing purely so dynamic-hint edges have
  a source to attach to) plus a new `dynamic_hints/webforms.py` extractor
  (a plain regex scan for the `CodeBehind`/`CodeFile` attribute, resolved
  relative to the markup file's own directory) closes that gap. Both C#
  and VB code-behind benefit — the extractor doesn't care which language
  the paired class is written in. Covered by
  `tests/unit/ingestion/test_webforms_dynamic_hints.py` and
  `tests/unit/ingestion/vb/test_handles.py` (recorded-JSON fixtures, no SDK
  or subprocess required for either).

**Phase 5 — code health — IMPLEMENTED**
- `Metrics.cs`; `vb/complexity.py`; `engine._walk` branch.
- Complexity map stashed on the client; empty-`FileComplexity` fallback.

  `Metrics.cs` computes everything §8 specified from the same syntax tree
  `VbExtractor.cs` already walks, no semantic model: per-function CCN
  (`If`/`ElseIf`, each non-`Case Else` `Case`, `While`, `Do`, `For`, `For
  Each`, `Catch`, each `AndAlso`/`OrElse`, the `If()` ternary), a SonarSource-
  style cognitive-complexity approximation (nesting-weighted for the
  primary construct, flat +1 for `ElseIf`/`Case`/`Catch` siblings — they
  share their container's depth rather than adding another one), max
  nesting, NLOC, param count, and "bumps" (top-level statements whose own
  internal nesting reaches ≥2). Class-level: WMC (`max_method_ccn` plus the
  per-method list callers can sum), field count, and an LCOM4 + Tight Class
  Cohesion approximation — a union-find over methods that merges on shared
  field access *or* a call between them (case-insensitive name matching
  against the class's own declared fields/methods, since D1 rules out a
  semantic model), with TCC narrowed to the field-sharing edges only
  (Bieman-Kang; call edges count for LCOM4 but not TCC). `error_handling_hits`
  covers `swallowed_catch` (empty `Catch`), `broad_except` (`Catch ex As
  Exception` with no `When` filter, reusing the *existing* biomarker kind
  strings so no new Python-side messaging was needed) and a VB-specific
  `on_error_resume_next`. `perf_hits` covers a starter set reusing three
  more *existing* generic kind strings — `string_concat_in_loop`,
  `blocking_sync_in_async` (`.Result`/`.Wait()` in an `Async` method), and
  `resource_construction_in_loop` (`New Regex` per loop iteration) — so
  every one of these fires through the health pipeline's current biomarkers
  with zero additional Python-side plumbing.

  The wire shape (`ComplexityDto` and friends in `Protocol.cs`) mirrors
  `analysis/health/complexity/models.py` field-for-field, so `vb/complexity.py`'s
  `walk_vb_file` is a pure JSON→dataclass conversion, no computation of its
  own — exactly the `sql_complexity.py` precedent this section originally
  named. The lookup side generalises the `VbSidecarClient.file_results` map
  Phase 4 introduced: `vb/sidecar.py::lookup_file_result(abs_path)` searches
  every registered client (abs paths are globally unique, so this needs no
  repo_path — useful since `HealthAnalyzer._walk` only has the `FileInfo`,
  not the repo root) and returns `None` on a miss, which `walk_vb_file`
  turns into the same empty `FileComplexity` every unmapped language gets.
  `HealthAnalyzer._walk` gained a `language == "vbnet"` branch immediately
  next to the existing `"sql"` one.

  One correctness fix that surfaced only by running the sidecar for real
  (this sandbox has the .NET SDK, so Phase 4/5 were verified end-to-end
  against the live, freshly-built sidecar rather than JSON fixtures alone):
  the initial nesting-depth seeding at the top of a function body double-
  counted a top-level control structure's own depth before recursing into
  its children, inflating cognitive complexity and max nesting on any
  function whose *first* statement was itself a loop/if/etc. Fixed by
  seeding every top-level statement at depth 0 uniformly — a top-level
  `For` is not nested in anything; only its own children are one level in.

  A second gap found and fixed in the same pass: **the run-scoped VB
  sidecar was never actually torn down.** `shutdown_sidecar()` existed since
  Phase 1 but nothing called it — `run_pipeline()` had no `finally` at all.
  Fixed by splitting `run_pipeline` into a thin public wrapper and a
  `_run_pipeline_body` — the wrapper's `finally` calls `shutdown_sidecar()`
  unconditionally (a no-op dict lookup for every non-VB repo), so the
  `dotnet` process no longer leaks past the run that started it, on success,
  on a raised exception, or on a resume early-return.

  Covered by `tests/unit/ingestion/vb/test_complexity.py` (JSON→
  `FileComplexity` mapping for every field, including the
  `HealthAnalyzer._walk` dispatch itself) — recorded fixtures, no SDK or
  subprocess required. Verified separately against the real, freshly-built
  sidecar (parse → complexity lookup → dynamic-edge extraction, chained
  through the actual `VbSidecarClient`/`parse_batch`/`HintRegistry` code
  paths) on a hand-written sample covering `Handles`, `On Error Resume
  Next`, a blanket `Catch`, nested `For`/`If`/`ElseIf` with an `AndAlso`
  condition, and a same-class method call — CCN/cognitive/nesting/LCOM4/TCC
  all hand-verified against the source by construction.

**Docs, with Phase 5 — DONE**
- `docs/layers/LANGUAGE_SUPPORT.md` — VB row, prerequisite, known gaps (late
  binding, `#If`, no perf/dataflow).
- `docs/architecture/language-support.md` — a short "languages without a
  grammar" section, since VB makes the "one `.scm` + one config" claim no
  longer universally true.
- `docs/reference/CONFIG.md` — the sidecar env var.
- `docs/CHANGELOG.md` **and** its mirror at
  `packages/core/src/repowise/core/upgrade/_data/CHANGELOG.md` — **not
  updated in this pass.** This repo's changelog is batched at release time
  (recent commits on `main` already sit well past the 0.36.0 entry with no
  per-PR changelog additions), and this work has landed no version bump —
  inventing a version number or entry here would conflict with however the
  actual release note gets written. The two files are still byte-identical
  (the drift-guard's only real requirement), so the guard test passes as-is.

---

## 10. Tests

The sidecar is a build-dependent external process, so the suite has to be
layered or CI turns red on every machine without the SDK.

- **`tests/unit/`** — no SDK, no subprocess. Feed recorded sidecar JSON
  fixtures through `vb/parse.py` and `vb/complexity.py` and assert on
  `ParsedFile` / `FileComplexity`. Covers the mapping table, case-insensitive
  identity, visibility defaults, `Module`-vs-`Class` kinds, `Handles` →
  `DynamicEdge`. This is where most of the coverage should live.
- **`tests/unit/`** — preflight with a faked `dotnet` probe: missing, too old,
  present-but-not-on-`PATH`, and `.vb`-count-zero (must not raise).
- **`tests/integration/`** — a real sidecar, marked and skipped when the SDK
  is absent. New fixture `tests/fixtures/vbnet_solution/`: a `.sln` with one
  `.vbproj` and one `.csproj`, a `Module`, a WinForms form with
  `Foo.Designer.vb` + `Handles`, a `RootNamespace` that differs from the
  folder name, and one deliberately mixed-case call site. Add its `obj/` to
  the untracked list alongside the existing .NET fixtures.
- **`tests/e2e/`** — `init` on that fixture; assert non-zero VB symbols, that
  the `ProjectReference` C#→VB edge exists, and that no event handler appears
  in the dead-code report.
- **CI** — no workflow in `.github/workflows/` installs the .NET SDK today
  (`grep -n dotnet` matches nothing), so `ci.yml` needs `actions/setup-dotnet`
  added to the `integration/` job. Unit and provider jobs stay SDK-free, which
  keeps the per-Python-version matrix (3.11/3.12/3.13) unchanged. Note that
  `integration/` only runs on push to `main`, so the sidecar's real-process
  path is not gated on PRs — an argument for keeping the Phase-2 mapping
  coverage in `tests/unit/` against recorded JSON, where it runs on every PR.
- **Protocol drift guard** — a test that the C# DTOs in `Protocol.cs` and the
  Python readers agree on field names, cheapest as a golden JSON sample the
  sidecar emits and Python parses.

---

## 11. Risks

| Risk | Severity | Mitigation |
|---|---|---|
| **Case-insensitive identity** leaks into a case-sensitive graph, silently dropping edges. | High — degrades quality invisibly, and "the graph looks thin" is hard to trace back. | Phase 2, not a follow-up. Fixture with deliberately mixed-case call sites. Assert edge counts, not just non-zero. |
| First-use `dotnet build` needs NuGet; fails on air-gapped machines. | Medium | `REPOWISE_ROSLYN_SIDECAR` escape hatch; error message names the cause explicitly. |
| ~20–40 s first-run build surprises users mid-`init`. | Low–Medium | Emit it as its own named progress phase, not silence. Cache is per repowise version, so it is once per upgrade, not once per run. |
| Sidecar startup (~1–2 s) on every small `repowise update`. | Low | Accepted under D6. `watch` reuses one. Revisit as a daemon (D6 option 2) only if measurement justifies it. |
| No semantic model ⇒ late-bound and `Object`-typed calls missing. | Medium, inherent to D1 | Documented in the support matrix. Do not paper over with name-matching heuristics that invent edges. |
| LCOM4 approximation misfires on VB, producing bogus god-class findings. | Medium | Validate against a real VB codebase before enabling the Extract-Class detector for VB; ship it off if it cannot be validated. |
| Sidecar becomes a second place language logic lives, drifting from the Python side. | Medium, long-term | Protocol version + golden-sample drift test. Keep all *policy* (kind mapping decisions, thresholds) in Python; the sidecar reports facts. |
| **Antivirus/EDR kills or blocks the freshly-built, unsigned `RepowiseVb.exe`**, surfacing as `VbSidecarError: VB sidecar closed stdout unexpectedly` with no stderr and no .NET crash. Confirmed in the wild: Windows Defender's Attack-Surface-Reduction rule "block executable files that don't meet a prevalence/age/trusted-list criterion" (GUID `01443614-cd74-433a-b99e-2ecdc07bfc25`) audits/kills exactly this binary — it is new, unsigned, and locally built, which is precisely what that rule targets. | High on a managed/corporate Windows machine — silent, looks like a random crash. | Fixed the compounding bug that made this worse: `sidecar_src_fingerprint()` (`vb/build.py`) was hashing `sidecar_src/bin/` and `sidecar_src/obj/` alongside the real `.cs` sources. Those are MSBuild's own intermediate/output directories sitting *inside* the folder being fingerprinted, and they embed the *previous* build's own output path (e.g. `obj/**/*.FileListAbsolute.txt`) — so every build changed the fingerprint, which changed the cache key (D2), which pointed at a *new* output directory next time. The cache never converged: every single `repowise` run rebuilt from scratch and executed a brand-new, never-before-seen binary, which is close to worst-case for the ASR rule above. Now excluded from the hash, so the same binary is built once per repowise version and reused — subsequent runs execute a file Defender has already seen, and an IT admin can add one stable path exclusion (`.repowise/roslyn-sidecar/`) instead of chasing a new path every run. `VbSidecarError`'s message on this path now also names the exit code, recent stderr, and the AV/EDR possibility explicitly, plus the `REPOWISE_ROSLYN_SIDECAR` escape hatch, instead of the bare symptom string. |

---

## 12. Non-goals

- VB6 / VBA / `.bas` / `.frm` / `.cls`. Different language, different parser.
- Routing C# through Roslyn (D4). The protocol allows it; this work does not do it.
- Semantic-model features: overload resolution, inferred types, `Option Strict
  Off` late-bound resolution, cross-assembly inheritance chains.
- MSBuild evaluation of the target repo (`Compile` item lists, conditional
  compilation constants, linked files). Excluded by D1; §5.5 documents the `#If`
  consequence.
- A persistent cross-invocation sidecar daemon (D6).
