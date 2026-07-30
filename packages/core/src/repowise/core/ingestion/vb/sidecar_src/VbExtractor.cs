using System.Text;
using System.Text.RegularExpressions;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.Text;
using Microsoft.CodeAnalysis.VisualBasic;
using Microsoft.CodeAnalysis.VisualBasic.Syntax;

namespace Repowise.Vb;

// Syntax-tree-only extraction (D1): VisualBasicSyntaxTree.ParseText, no
// VisualBasicCompilation, no semantic model. Mirrors what the C# tree-sitter
// grammar gives every other language, just walked by hand over Roslyn's own
// parse tree instead of a .scm query.
//
// Symbol/import/call/heritage shapes here must line up 1:1 with what
// vb/parse.py expects to receive — see the SymbolKind mapping table in
// docs/architecture/vb-support.md §5.5.
public static class VbExtractor
{
    public static FileParseResult ParseFile(FileParseRequest req)
    {
        var result = new FileParseResult { Path = req.Path };
        try
        {
            SourceText text;
            // Legacy VB source is frequently not UTF-8 — UTF-16/UTF-8 BOMs
            // are auto-detected here; a BOM-less non-UTF-8 file (e.g. plain
            // Windows-1252) falls back to UTF-8 decoding, since .NET's
            // "Encoding.Default" is UTF-8 (not the machine ANSI code page)
            // without the System.Text.Encoding.CodePages provider. Accepted
            // for v1 — revisit if real repos hit mis-decoded VB source.
            using (var reader = new StreamReader(req.AbsPath, Encoding.UTF8, detectEncodingFromByteOrderMarks: true))
            {
                text = SourceText.From(reader.ReadToEnd(), reader.CurrentEncoding);
            }

            var tree = VisualBasicSyntaxTree.ParseText(text, path: req.Path);
            var root = (CompilationUnitSyntax)tree.GetRoot();

            foreach (var diag in tree.GetDiagnostics())
            {
                if (diag.Severity == DiagnosticSeverity.Error)
                {
                    result.ParseErrors.Add(diag.ToString());
                }
            }

            var walker = new SymbolWalker(tree, req.RootNamespace);
            walker.Visit(root);

            result.Symbols = walker.Symbols;
            result.Imports = walker.Imports;
            result.Heritage = walker.Heritage;
            result.Calls = walker.Calls;
            result.EventWiring = HandlesExtractor.Extract(tree, root);
            result.Complexity = Metrics.Compute(tree, root, text);
        }
        catch (Exception ex)
        {
            // A file Roslyn cannot even open/parse degrades to parseErrors,
            // not a crash of the whole batch (matches the tree-sitter ERROR
            // node contract for every other language).
            result.ParseErrors.Add(ex.Message);
        }
        return result;
    }

    /// <summary>One container frame: a namespace or a type nesting level.</summary>
    private sealed class Container
    {
        public required string Name;
        public required string Kind; // "namespace" | "class" | "module" | "struct" | "interface" | "enum"
    }

    private sealed class SymbolWalker : VisualBasicSyntaxWalker
    {
        private readonly SyntaxTree _tree;
        private readonly string _rootNamespace;
        private readonly List<Container> _stack = new();

        public List<SymbolDto> Symbols { get; } = new();
        public List<ImportDto> Imports { get; } = new();
        public List<HeritageDto> Heritage { get; } = new();
        public List<CallDto> Calls { get; } = new();

        public SymbolWalker(SyntaxTree tree, string rootNamespace)
        {
            _tree = tree;
            _rootNamespace = rootNamespace ?? "";
        }

        // -- helpers ----------------------------------------------------

        private int LineOf(int position) => _tree.GetLineSpan(new TextSpan(position, 0)).StartLinePosition.Line + 1;

        private string QualifiedName(string name)
        {
            var parts = new List<string>();
            if (!string.IsNullOrEmpty(_rootNamespace))
            {
                parts.Add(_rootNamespace);
            }
            foreach (var c in _stack)
            {
                parts.Add(c.Name);
            }
            parts.Add(name);
            return string.Join(".", parts);
        }

        /// <summary>Nearest enclosing *type* container (skips namespaces), or null.</summary>
        private Container? CurrentTypeContainer()
        {
            for (int i = _stack.Count - 1; i >= 0; i--)
            {
                if (_stack[i].Kind != "namespace")
                {
                    return _stack[i];
                }
            }
            return null;
        }

        private static string VisibilityOf(SyntaxTokenList modifiers)
        {
            if (modifiers.Any(SyntaxKind.PrivateKeyword)) return "private";
            // "Protected Friend" has no combined value in the closed
            // visibility set — "protected" is the closer approximation.
            if (modifiers.Any(SyntaxKind.ProtectedKeyword)) return "protected";
            if (modifiers.Any(SyntaxKind.FriendKeyword)) return "internal";
            return "public"; // VB's own default, unlike C#'s "private"
        }

        private static bool IsAsyncOf(SyntaxTokenList modifiers) => modifiers.Any(SyntaxKind.AsyncKeyword);

        private static readonly Regex DocLineRe = new(@"^\s*'''\s?", RegexOptions.Compiled);
        private static readonly Regex SummaryRe =
            new(@"<summary>(.*?)</summary>", RegexOptions.Compiled | RegexOptions.Singleline);

        /// <summary>
        /// VB doc comments are ``'''`` (XML-doc) trivia immediately preceding
        /// the declaration. Extracted with a light regex rather than the
        /// full structured-trivia XML API — good enough for a summary line,
        /// consistent with the "documented, not semantic" scope of D1.
        /// </summary>
        private static string? ExtractDocSummary(SyntaxNode node)
        {
            var docLines = new List<string>();
            foreach (var trivia in node.GetLeadingTrivia())
            {
                if (trivia.IsKind(SyntaxKind.DocumentationCommentTrivia))
                {
                    foreach (var line in trivia.ToFullString().Split('\n'))
                    {
                        docLines.Add(DocLineRe.Replace(line.TrimEnd('\r'), ""));
                    }
                }
            }
            if (docLines.Count == 0)
            {
                return null;
            }
            var joined = string.Join("\n", docLines).Trim();
            var m = SummaryRe.Match(joined);
            return (m.Success ? m.Groups[1].Value : joined).Trim();
        }

        // -- containers ---------------------------------------------------

        public override void VisitNamespaceBlock(NamespaceBlockSyntax node)
        {
            _stack.Add(new Container { Name = node.NamespaceStatement.Name.ToString(), Kind = "namespace" });
            base.VisitNamespaceBlock(node);
            _stack.RemoveAt(_stack.Count - 1);
        }

        private void VisitTypeBlock(SyntaxNode block, SyntaxToken identifier, SyntaxTokenList modifiers,
            string kind, string symbolKind, InheritsStatementSyntax? inherits, ImplementsStatementSyntax? implements)
        {
            var name = identifier.ValueText;
            var symbol = new SymbolDto
            {
                Name = name,
                QualifiedName = QualifiedName(name),
                Kind = symbolKind,
                Signature = FirstLineOf(block),
                StartLine = LineOf(block.SpanStart),
                EndLine = LineOf(block.Span.End),
                Docstring = ExtractDocSummary(block),
                Visibility = VisibilityOf(modifiers),
                ParentName = CurrentTypeContainer()?.Name,
            };
            Symbols.Add(symbol);

            if (inherits != null)
            {
                foreach (var t in inherits.Types)
                {
                    Heritage.Add(new HeritageDto
                    {
                        ChildName = name,
                        ParentName = t.ToString(),
                        Kind = "extends",
                        Line = LineOf(inherits.SpanStart),
                    });
                }
            }
            if (implements != null)
            {
                foreach (var t in implements.Types)
                {
                    Heritage.Add(new HeritageDto
                    {
                        ChildName = name,
                        ParentName = t.ToString(),
                        Kind = "implements",
                        Line = LineOf(implements.SpanStart),
                    });
                }
            }

            _stack.Add(new Container { Name = name, Kind = kind });
        }

        private static string FirstLineOf(SyntaxNode node)
        {
            var text = node.ToString();
            var idx = text.IndexOfAny(new[] { '\r', '\n' });
            return (idx >= 0 ? text[..idx] : text).Trim();
        }

        public override void VisitClassBlock(ClassBlockSyntax node)
        {
            VisitTypeBlock(node, node.ClassStatement.Identifier, node.ClassStatement.Modifiers,
                "class", "class", node.Inherits.FirstOrDefault(), node.Implements.FirstOrDefault());
            base.VisitClassBlock(node);
            _stack.RemoveAt(_stack.Count - 1);
        }

        public override void VisitModuleBlock(ModuleBlockSyntax node)
        {
            // A Module is a static class — its Subs/Functions are "function"
            // kind (callable unqualified), not "method".
            VisitTypeBlock(node, node.ModuleStatement.Identifier, node.ModuleStatement.Modifiers,
                "module", "module", null, null);
            base.VisitModuleBlock(node);
            _stack.RemoveAt(_stack.Count - 1);
        }

        public override void VisitStructureBlock(StructureBlockSyntax node)
        {
            VisitTypeBlock(node, node.StructureStatement.Identifier, node.StructureStatement.Modifiers,
                "struct", "struct", node.Inherits.FirstOrDefault(), node.Implements.FirstOrDefault());
            base.VisitStructureBlock(node);
            _stack.RemoveAt(_stack.Count - 1);
        }

        public override void VisitInterfaceBlock(InterfaceBlockSyntax node)
        {
            VisitTypeBlock(node, node.InterfaceStatement.Identifier, node.InterfaceStatement.Modifiers,
                "interface", "interface", node.Inherits.FirstOrDefault(), null);
            base.VisitInterfaceBlock(node);
            _stack.RemoveAt(_stack.Count - 1);
        }

        public override void VisitEnumBlock(EnumBlockSyntax node)
        {
            VisitTypeBlock(node, node.EnumStatement.Identifier, node.EnumStatement.Modifiers,
                "enum", "enum", null, null);
            base.VisitEnumBlock(node);
            _stack.RemoveAt(_stack.Count - 1);
        }

        // -- members ------------------------------------------------------

        public override void VisitMethodBlock(MethodBlockSyntax node)
        {
            var stmt = node.SubOrFunctionStatement;
            var container = CurrentTypeContainer();
            // Sub/Function in a Module is callable unqualified ("function");
            // in a Class/Structure/Interface it is a "method" (matches C#).
            var kind = container is { Kind: "module" } ? "function" : "method";
            Symbols.Add(new SymbolDto
            {
                Name = stmt.Identifier.ValueText,
                QualifiedName = QualifiedName(stmt.Identifier.ValueText),
                Kind = kind,
                Signature = FirstLineOf(stmt),
                StartLine = LineOf(node.SpanStart),
                EndLine = LineOf(node.Span.End),
                Docstring = ExtractDocSummary(node),
                Visibility = VisibilityOf(stmt.Modifiers),
                IsAsync = IsAsyncOf(stmt.Modifiers),
                ParentName = container?.Name,
            });
            base.VisitMethodBlock(node);
        }

        public override void VisitMethodStatement(MethodStatementSyntax node)
        {
            // Roslyn visits a MethodBlockSyntax's SubOrFunctionStatement as a
            // MethodStatementSyntax child too — skip it here since
            // VisitMethodBlock already emitted the symbol (with the full
            // block's end line, not just the header's).
            if (node.Parent is MethodBlockSyntax)
            {
                return;
            }
            var container = CurrentTypeContainer();
            var kind = container is { Kind: "module" } ? "function" : "method";
            Symbols.Add(new SymbolDto
            {
                Name = node.Identifier.ValueText,
                QualifiedName = QualifiedName(node.Identifier.ValueText),
                Kind = kind,
                Signature = FirstLineOf(node),
                StartLine = LineOf(node.SpanStart),
                EndLine = LineOf(node.Span.End),
                Docstring = ExtractDocSummary(node),
                Visibility = VisibilityOf(node.Modifiers),
                IsAsync = IsAsyncOf(node.Modifiers),
                ParentName = container?.Name,
            });
            base.VisitMethodStatement(node);
        }

        public override void VisitPropertyBlock(PropertyBlockSyntax node)
        {
            var stmt = node.PropertyStatement;
            var container = CurrentTypeContainer();
            Symbols.Add(new SymbolDto
            {
                Name = stmt.Identifier.ValueText,
                QualifiedName = QualifiedName(stmt.Identifier.ValueText),
                Kind = "method", // matches how C# properties are already emitted
                Signature = FirstLineOf(stmt),
                StartLine = LineOf(node.SpanStart),
                EndLine = LineOf(node.Span.End),
                Docstring = ExtractDocSummary(node),
                Visibility = VisibilityOf(stmt.Modifiers),
                ParentName = container?.Name,
            });
            base.VisitPropertyBlock(node);
        }

        public override void VisitPropertyStatement(PropertyStatementSyntax node)
        {
            // Auto-implemented property (no block) — skip if this statement
            // is actually the header of a PropertyBlockSyntax (already
            // handled above); Roslyn only invokes this directly otherwise.
            if (node.Parent is PropertyBlockSyntax)
            {
                return;
            }
            var container = CurrentTypeContainer();
            Symbols.Add(new SymbolDto
            {
                Name = node.Identifier.ValueText,
                QualifiedName = QualifiedName(node.Identifier.ValueText),
                Kind = "method",
                Signature = FirstLineOf(node),
                StartLine = LineOf(node.SpanStart),
                EndLine = LineOf(node.Span.End),
                Docstring = ExtractDocSummary(node),
                Visibility = VisibilityOf(node.Modifiers),
                ParentName = container?.Name,
            });
            base.VisitPropertyStatement(node);
        }

        public override void VisitEventStatement(EventStatementSyntax node)
        {
            if (node.Parent is EventBlockSyntax)
            {
                return;
            }
            var container = CurrentTypeContainer();
            Symbols.Add(new SymbolDto
            {
                Name = node.Identifier.ValueText,
                QualifiedName = QualifiedName(node.Identifier.ValueText),
                // Referenced by Handles/AddHandler, so it must be a node
                // (D8) — emitted as "method" like other callable members.
                Kind = "method",
                Signature = FirstLineOf(node),
                StartLine = LineOf(node.SpanStart),
                EndLine = LineOf(node.Span.End),
                Docstring = ExtractDocSummary(node),
                Visibility = VisibilityOf(node.Modifiers),
                ParentName = container?.Name,
            });
            base.VisitEventStatement(node);
        }

        public override void VisitEventBlock(EventBlockSyntax node)
        {
            var stmt = node.EventStatement;
            var container = CurrentTypeContainer();
            Symbols.Add(new SymbolDto
            {
                Name = stmt.Identifier.ValueText,
                QualifiedName = QualifiedName(stmt.Identifier.ValueText),
                Kind = "method",
                Signature = FirstLineOf(stmt),
                StartLine = LineOf(node.SpanStart),
                EndLine = LineOf(node.Span.End),
                Docstring = ExtractDocSummary(node),
                Visibility = VisibilityOf(stmt.Modifiers),
                ParentName = container?.Name,
            });
            base.VisitEventBlock(node);
        }

        public override void VisitOperatorBlock(OperatorBlockSyntax node)
        {
            var stmt = node.OperatorStatement;
            var container = CurrentTypeContainer();
            Symbols.Add(new SymbolDto
            {
                Name = stmt.OperatorToken.ValueText,
                QualifiedName = QualifiedName(stmt.OperatorToken.ValueText),
                Kind = "method",
                Signature = FirstLineOf(stmt),
                StartLine = LineOf(node.SpanStart),
                EndLine = LineOf(node.Span.End),
                Docstring = ExtractDocSummary(node),
                Visibility = VisibilityOf(stmt.Modifiers),
                ParentName = container?.Name,
            });
            base.VisitOperatorBlock(node);
        }

        public override void VisitDelegateStatement(DelegateStatementSyntax node)
        {
            var container = CurrentTypeContainer();
            Symbols.Add(new SymbolDto
            {
                Name = node.Identifier.ValueText,
                QualifiedName = QualifiedName(node.Identifier.ValueText),
                Kind = "type_alias",
                Signature = FirstLineOf(node),
                StartLine = LineOf(node.SpanStart),
                EndLine = LineOf(node.Span.End),
                Docstring = ExtractDocSummary(node),
                Visibility = VisibilityOf(node.Modifiers),
                ParentName = container?.Name,
            });
            base.VisitDelegateStatement(node);
        }

        public override void VisitFieldDeclaration(FieldDeclarationSyntax node)
        {
            var container = CurrentTypeContainer();
            var isConst = node.Modifiers.Any(SyntaxKind.ConstKeyword);
            var kind = isConst ? "constant" : "variable";
            var visibility = VisibilityOf(node.Modifiers);
            var docstring = ExtractDocSummary(node);
            foreach (var declarator in node.Declarators)
            {
                foreach (var modifiedId in declarator.Names)
                {
                    var name = modifiedId.Identifier.ValueText;
                    Symbols.Add(new SymbolDto
                    {
                        Name = name,
                        QualifiedName = QualifiedName(name),
                        Kind = kind,
                        Signature = FirstLineOf(node),
                        StartLine = LineOf(node.SpanStart),
                        EndLine = LineOf(node.Span.End),
                        Docstring = docstring,
                        Visibility = visibility,
                        ParentName = container?.Name,
                    });
                }
            }
            base.VisitFieldDeclaration(node);
        }

        // -- imports --------------------------------------------------------

        public override void VisitImportsStatement(ImportsStatementSyntax node)
        {
            foreach (var clause in node.ImportsClauses)
            {
                if (clause is SimpleImportsClauseSyntax simple)
                {
                    var modulePath = simple.Name.ToString();
                    string localName;
                    string? exportedName = null;
                    if (simple.Alias != null)
                    {
                        localName = simple.Alias.Identifier.ValueText;
                    }
                    else
                    {
                        localName = modulePath;
                    }
                    Imports.Add(new ImportDto
                    {
                        RawStatement = "Imports " + clause.ToString().Trim(),
                        ModulePath = modulePath,
                        ImportedNames = new List<string> { "*" },
                        Bindings = new List<NamedBindingDto>
                        {
                            new()
                            {
                                LocalName = localName,
                                ExportedName = exportedName,
                                IsModuleAlias = true,
                            },
                        },
                    });
                }
                // XmlNamespaceImportsClauseSyntax (Imports <xmlns:...>) is not
                // a code dependency — deliberately not emitted.
            }
            base.VisitImportsStatement(node);
        }

        // -- calls ------------------------------------------------------

        public override void VisitInvocationExpression(InvocationExpressionSyntax node)
        {
            var argCount = node.ArgumentList?.Arguments.Count;
            var line = LineOf(node.SpanStart);

            switch (node.Expression)
            {
                case IdentifierNameSyntax id:
                    Calls.Add(new CallDto
                    {
                        TargetName = id.Identifier.ValueText,
                        ReceiverName = null,
                        Line = line,
                        ArgumentCount = argCount,
                    });
                    break;

                case MemberAccessExpressionSyntax member:
                    var receiver = ReceiverNameOf(member.Expression);
                    if (receiver != null)
                    {
                        Calls.Add(new CallDto
                        {
                            TargetName = member.Name.Identifier.ValueText,
                            ReceiverName = receiver,
                            Line = line,
                            ArgumentCount = argCount,
                        });
                    }
                    break;
            }
            base.VisitInvocationExpression(node);
        }

        /// <summary>
        /// Bare identifier / Me / MyClass / MyBase receivers only — deeper
        /// chains (``a.b.C()``) are late-bound-shaped enough that D1's
        /// no-semantic-model contract already excludes them (§6.3).
        /// ``Me`` is normalised to "self" so it lines up with the existing
        /// cross-language self/this resolution tier in CallResolver.
        /// </summary>
        private static string? ReceiverNameOf(ExpressionSyntax expr) => expr switch
        {
            MeExpressionSyntax => "self",
            MyClassExpressionSyntax => "self",
            MyBaseExpressionSyntax => "self",
            IdentifierNameSyntax id => id.Identifier.ValueText,
            _ => null,
        };
    }
}
