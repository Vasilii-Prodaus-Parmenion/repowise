using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.Text;
using Microsoft.CodeAnalysis.VisualBasic;
using Microsoft.CodeAnalysis.VisualBasic.Syntax;

namespace Repowise.Vb;

// Event-wiring extraction (D8, phase 4): `Handles` clauses on method
// declarations, and `AddHandler`/`RemoveHandler ... AddressOf ...`
// statements. Kept as its own walker (rather than folded into
// VbExtractor.cs's SymbolWalker) since it needs no container-stack state —
// every fact it reports is resolved against the already-extracted symbol
// table on the Python side (vb/handles.py), matching the "sidecar reports
// facts, Python owns policy" split in docs/architecture/vb-support.md §11.
public static class HandlesExtractor
{
    public static List<EventWiringDto> Extract(SyntaxTree tree, CompilationUnitSyntax root)
    {
        var walker = new Walker(tree);
        walker.Visit(root);
        return walker.Results;
    }

    private sealed class Walker : VisualBasicSyntaxWalker
    {
        private readonly SyntaxTree _tree;

        public List<EventWiringDto> Results { get; } = new();

        public Walker(SyntaxTree tree)
        {
            _tree = tree;
        }

        private int LineOf(int position) => _tree.GetLineSpan(new TextSpan(position, 0)).StartLinePosition.Line + 1;

        public override void VisitMethodStatement(MethodStatementSyntax node)
        {
            var handles = node.HandlesClause;
            if (handles != null)
            {
                var line = LineOf(node.SpanStart);
                foreach (var item in handles.Events)
                {
                    // ``item`` is a HandlesClauseItemSyntax: the event name
                    // (``EventMember``) lives on the item itself; the
                    // container (what's to the left of the dot) is one of
                    // three EventContainerSyntax shapes.
                    var eventName = item.EventMember.Identifier.ValueText;
                    switch (item.EventContainer)
                    {
                        case WithEventsEventContainerSyntax withEvents:
                            Results.Add(new EventWiringDto
                            {
                                Kind = "handles",
                                Line = line,
                                WithEventsName = withEvents.Identifier.ValueText,
                                EventName = eventName,
                            });
                            break;

                        case KeywordEventContainerSyntax keyword:
                            // `Handles Me.Load` / `Handles MyBase.Load` — no
                            // WithEvents field to cross-reference; the
                            // Python side falls back to the enclosing type
                            // itself as the wiring source (§6.1).
                            Results.Add(new EventWiringDto
                            {
                                Kind = "handles",
                                Line = line,
                                WithEventsName = keyword.Keyword.ValueText,
                                EventName = eventName,
                            });
                            break;

                        // WithEventsPropertyEventContainerSyntax (nested
                        // `Handles Foo.Bar.Baz`) is late-bound-shaped without
                        // a semantic model — skipped, consistent with D1/§6.3.
                    }
                }
            }
            base.VisitMethodStatement(node);
        }

        public override void VisitAddRemoveHandlerStatement(AddRemoveHandlerStatementSyntax node)
        {
            var isAdd = node.IsKind(SyntaxKind.AddHandlerStatement);
            var targetName = TargetMethodName(node.DelegateExpression);
            if (targetName != null)
            {
                Results.Add(new EventWiringDto
                {
                    Kind = isAdd ? "add_handler" : "remove_handler",
                    Line = LineOf(node.SpanStart),
                    TargetName = targetName,
                });
            }
            base.VisitAddRemoveHandlerStatement(node);
        }

        private static string? TargetMethodName(ExpressionSyntax delegateExpr)
        {
            if (delegateExpr is UnaryExpressionSyntax addressOf &&
                addressOf.IsKind(SyntaxKind.AddressOfExpression))
            {
                return LastIdentifier(addressOf.Operand);
            }
            // Lambda / `New EventHandler(...)` delegate forms have no fixed
            // method name to resolve — late-bound-shaped, skipped (D1/§6.3).
            return null;
        }

        private static string? LastIdentifier(ExpressionSyntax expr) => expr switch
        {
            IdentifierNameSyntax id => id.Identifier.ValueText,
            MemberAccessExpressionSyntax member => member.Name.Identifier.ValueText,
            _ => null,
        };
    }
}
