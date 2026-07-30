using System.Text.Json.Serialization;

namespace Repowise.Vb;

/// <summary>
/// Wire protocol constants. A protocol mismatch between client and sidecar is
/// a hard error, not a negotiation: the sidecar is built from source shipped
/// in the same wheel as the client, so a mismatch means a stale cache dir.
/// </summary>
public static class Protocol
{
    public const int Version = 1;
    public const string SidecarVersion = "0.1.0";
}

public sealed class HelloResponse
{
    [JsonPropertyName("id")] public long Id { get; set; }
    [JsonPropertyName("ok")] public bool Ok { get; set; }
    [JsonPropertyName("protocol")] public int Protocol { get; set; }
    [JsonPropertyName("sidecar")] public string Sidecar { get; set; } = "";
    [JsonPropertyName("roslyn")] public string Roslyn { get; set; } = "";
    [JsonPropertyName("runtime")] public string Runtime { get; set; } = "";
}

public sealed class ShutdownResponse
{
    [JsonPropertyName("id")] public long Id { get; set; }
    [JsonPropertyName("ok")] public bool Ok { get; set; }
}

public sealed class ErrorResponse
{
    [JsonPropertyName("id")] public long Id { get; set; }
    [JsonPropertyName("ok")] public bool Ok { get; set; }
    [JsonPropertyName("error")] public string Error { get; set; } = "";
}

// ---------------------------------------------------------------------
// "parse" op — symbols/imports/calls/heritage extraction (phase 2). The
// "language" field on the request keeps this language-neutral in shape
// (D4): only "vbnet" is sent today. "eventWiring" (D8, phase 4) and
// "complexity" (D5, phase 5) are not on the wire yet — they slot in as
// additive fields on FileParseResult without a protocol version bump,
// same as this "parse" op itself did not bump Version past what phase 1
// shipped.
// ---------------------------------------------------------------------

public sealed class FileParseRequest
{
    [JsonPropertyName("path")] public string Path { get; set; } = "";
    [JsonPropertyName("absPath")] public string AbsPath { get; set; } = "";

    /// <summary>
    /// From the owning .vbproj's &lt;RootNamespace&gt; (D7). Empty until
    /// phase 3 wires project awareness — an empty root namespace is simply
    /// not prepended.
    /// </summary>
    [JsonPropertyName("rootNamespace")] public string RootNamespace { get; set; } = "";
}

public sealed class ParseRequest
{
    [JsonPropertyName("id")] public long Id { get; set; }
    [JsonPropertyName("op")] public string Op { get; set; } = "parse";
    [JsonPropertyName("language")] public string Language { get; set; } = "vbnet";
    [JsonPropertyName("files")] public List<FileParseRequest> Files { get; set; } = new();
}

public sealed class SymbolDto
{
    [JsonPropertyName("name")] public string Name { get; set; } = "";
    [JsonPropertyName("qualifiedName")] public string QualifiedName { get; set; } = "";
    [JsonPropertyName("kind")] public string Kind { get; set; } = "";
    [JsonPropertyName("signature")] public string Signature { get; set; } = "";
    [JsonPropertyName("startLine")] public int StartLine { get; set; }
    [JsonPropertyName("endLine")] public int EndLine { get; set; }
    [JsonPropertyName("docstring")] public string? Docstring { get; set; }
    [JsonPropertyName("visibility")] public string Visibility { get; set; } = "public";
    [JsonPropertyName("isAsync")] public bool IsAsync { get; set; }
    [JsonPropertyName("parentName")] public string? ParentName { get; set; }
}

public sealed class NamedBindingDto
{
    [JsonPropertyName("localName")] public string LocalName { get; set; } = "";
    [JsonPropertyName("exportedName")] public string? ExportedName { get; set; }
    [JsonPropertyName("isModuleAlias")] public bool IsModuleAlias { get; set; }
}

public sealed class ImportDto
{
    [JsonPropertyName("rawStatement")] public string RawStatement { get; set; } = "";
    [JsonPropertyName("modulePath")] public string ModulePath { get; set; } = "";
    [JsonPropertyName("importedNames")] public List<string> ImportedNames { get; set; } = new();
    [JsonPropertyName("bindings")] public List<NamedBindingDto> Bindings { get; set; } = new();
}

public sealed class CallDto
{
    [JsonPropertyName("targetName")] public string TargetName { get; set; } = "";
    [JsonPropertyName("receiverName")] public string? ReceiverName { get; set; }
    [JsonPropertyName("line")] public int Line { get; set; }
    [JsonPropertyName("argumentCount")] public int? ArgumentCount { get; set; }
}

public sealed class HeritageDto
{
    [JsonPropertyName("childName")] public string ChildName { get; set; } = "";
    [JsonPropertyName("parentName")] public string ParentName { get; set; } = "";
    [JsonPropertyName("kind")] public string Kind { get; set; } = "";
    [JsonPropertyName("line")] public int Line { get; set; }
}

// ---------------------------------------------------------------------
// Event wiring (D8, phase 4) — Handles clauses, WithEvents-implied
// container names, and AddHandler/RemoveHandler + AddressOf statements.
// Additive field on FileParseResult, no protocol version bump (see the
// comment on ParseRequest above).
// ---------------------------------------------------------------------

public sealed class EventWiringDto
{
    /// <summary>"handles" | "add_handler" | "remove_handler".</summary>
    [JsonPropertyName("kind")] public string Kind { get; set; } = "";
    [JsonPropertyName("line")] public int Line { get; set; }
    /// <summary>Left-hand side of ``Handles X.Y`` — a WithEvents field name,
    /// or the "Me"/"MyBase"/"MyClass" keyword text. Null for add/remove
    /// handler.</summary>
    [JsonPropertyName("withEventsName")] public string? WithEventsName { get; set; }
    /// <summary>Right-hand side of ``Handles X.Y`` (the event name). Null for
    /// add/remove handler.</summary>
    [JsonPropertyName("eventName")] public string? EventName { get; set; }
    /// <summary>``AddressOf`` target method name for add/remove handler. Null
    /// for "handles".</summary>
    [JsonPropertyName("targetName")] public string? TargetName { get; set; }
}

public sealed class FileParseResult
{
    [JsonPropertyName("path")] public string Path { get; set; } = "";
    [JsonPropertyName("symbols")] public List<SymbolDto> Symbols { get; set; } = new();
    [JsonPropertyName("imports")] public List<ImportDto> Imports { get; set; } = new();
    [JsonPropertyName("calls")] public List<CallDto> Calls { get; set; } = new();
    [JsonPropertyName("heritage")] public List<HeritageDto> Heritage { get; set; } = new();
    [JsonPropertyName("eventWiring")] public List<EventWiringDto> EventWiring { get; set; } = new();
    [JsonPropertyName("docstring")] public string? Docstring { get; set; }
    [JsonPropertyName("parseErrors")] public List<string> ParseErrors { get; set; } = new();
    [JsonPropertyName("complexity")] public ComplexityDto Complexity { get; set; } = new();
}

// ---------------------------------------------------------------------
// Code health (D5, phase 5) — CCN/cognitive/nesting/NLOC + class metrics
// + error-handling/perf smell hits, computed by Metrics.cs from the same
// syntax tree VbExtractor.cs already walked. Additive field on
// FileParseResult, no protocol version bump (see the comment on
// ParseRequest above — same rule Phase 4's eventWiring already followed).
// Mirrors packages/core/src/repowise/core/analysis/health/complexity/models.py
// field-for-field so vb/complexity.py's JSON -> FileComplexity mapping is
// a straight walk, not a translation.
// ---------------------------------------------------------------------

public sealed class ConditionComplexityDto
{
    [JsonPropertyName("line")] public int Line { get; set; }
    [JsonPropertyName("operatorCount")] public int OperatorCount { get; set; }
    [JsonPropertyName("enclosingConstruct")] public string EnclosingConstruct { get; set; } = "";
}

public sealed class FunctionComplexityDto
{
    [JsonPropertyName("name")] public string Name { get; set; } = "";
    [JsonPropertyName("startLine")] public int StartLine { get; set; }
    [JsonPropertyName("endLine")] public int EndLine { get; set; }
    [JsonPropertyName("ccn")] public int Ccn { get; set; } = 1;
    [JsonPropertyName("maxNesting")] public int MaxNesting { get; set; }
    [JsonPropertyName("cognitive")] public int Cognitive { get; set; }
    [JsonPropertyName("nloc")] public int Nloc { get; set; }
    [JsonPropertyName("bumps")] public int Bumps { get; set; }
    [JsonPropertyName("paramCount")] public int ParamCount { get; set; }
    [JsonPropertyName("complexConditions")] public List<ConditionComplexityDto> ComplexConditions { get; set; } = new();
}

public sealed class ClassComplexityDto
{
    [JsonPropertyName("name")] public string Name { get; set; } = "";
    [JsonPropertyName("startLine")] public int StartLine { get; set; }
    [JsonPropertyName("endLine")] public int EndLine { get; set; }
    [JsonPropertyName("methodCount")] public int MethodCount { get; set; }
    [JsonPropertyName("totalNloc")] public int TotalNloc { get; set; }
    [JsonPropertyName("methods")] public List<FunctionComplexityDto> Methods { get; set; } = new();
    [JsonPropertyName("lcom4")] public int Lcom4 { get; set; } = 1;
    [JsonPropertyName("maxMethodCcn")] public int MaxMethodCcn { get; set; }
    [JsonPropertyName("fieldCount")] public int FieldCount { get; set; }
    [JsonPropertyName("tcc")] public double Tcc { get; set; } = 1.0;
}

public sealed class ErrorHandlingHitDto
{
    /// <summary>"swallowed_catch" | "broad_except" | "on_error_resume_next".</summary>
    [JsonPropertyName("kind")] public string Kind { get; set; } = "";
    [JsonPropertyName("line")] public int Line { get; set; }
}

public sealed class PerfHitDto
{
    /// <summary>"string_concat_in_loop" | "blocking_sync_in_async" | "resource_construction_in_loop".</summary>
    [JsonPropertyName("kind")] public string Kind { get; set; } = "";
    [JsonPropertyName("line")] public int Line { get; set; }
    [JsonPropertyName("function")] public string? Function { get; set; }
    [JsonPropertyName("detail")] public string Detail { get; set; } = "";
}

public sealed class ComplexityDto
{
    [JsonPropertyName("functions")] public List<FunctionComplexityDto> Functions { get; set; } = new();
    [JsonPropertyName("classes")] public List<ClassComplexityDto> Classes { get; set; } = new();
    [JsonPropertyName("fileNloc")] public int FileNloc { get; set; }
    [JsonPropertyName("errorHandlingHits")] public List<ErrorHandlingHitDto> ErrorHandlingHits { get; set; } = new();
    [JsonPropertyName("perfHits")] public List<PerfHitDto> PerfHits { get; set; } = new();
}

public sealed class ParseResponse
{
    [JsonPropertyName("id")] public long Id { get; set; }
    [JsonPropertyName("ok")] public bool Ok { get; set; }
    [JsonPropertyName("results")] public List<FileParseResult> Results { get; set; } = new();
}
