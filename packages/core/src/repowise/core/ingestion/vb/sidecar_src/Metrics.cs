using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.Text;
using Microsoft.CodeAnalysis.VisualBasic;
using Microsoft.CodeAnalysis.VisualBasic.Syntax;

namespace Repowise.Vb;

// Code-health metrics (D5, phase 5): CCN / cognitive / nesting / NLOC per
// function, WMC + an LCOM4/TCC approximation per class, plus the
// error-handling and starter perf smell hits vb-support.md §8 specifies.
// No semantic model (D1) — everything here is a syntax-tree-only
// approximation, explicitly lower-confidence than the tree-sitter walker's
// equivalent for the class-cohesion metrics (see ClassComplexityDto).
public static class Metrics
{
    public static ComplexityDto Compute(SyntaxTree tree, CompilationUnitSyntax root, SourceText text)
    {
        var dto = new ComplexityDto
        {
            FileNloc = CountNloc(text, 0, text.Lines.Count - 1),
        };

        var walker = new FileWalker(tree);
        walker.Visit(root);
        dto.Functions = walker.Functions;
        dto.Classes = walker.Classes;
        dto.ErrorHandlingHits = walker.ErrorHandlingHits;
        dto.PerfHits = walker.PerfHits;
        return dto;
    }

    // -- NLOC -------------------------------------------------------------

    /// <summary>Non-blank source lines in ``[startLine, endLine]`` (0-indexed,
    /// inclusive). A line whose trimmed text is empty or a bare ``'`` comment
    /// doesn't count — matches the "non-blank lines inside the body" contract
    /// FileComplexity.file_nloc/FunctionComplexity.nloc already document.</summary>
    private static int CountNloc(SourceText text, int startLine, int endLine)
    {
        int count = 0;
        int last = Math.Min(endLine, text.Lines.Count - 1);
        for (int i = Math.Max(0, startLine); i <= last; i++)
        {
            var line = text.Lines[i].ToString().Trim();
            if (line.Length == 0 || line.StartsWith("'"))
            {
                continue;
            }
            count++;
        }
        return count;
    }

    private static int LineIndexOf(SyntaxTree tree, int position) =>
        tree.GetLineSpan(new TextSpan(position, 0)).StartLinePosition.Line;

    // -- Nesting containers -------------------------------------------------

    /// <summary>Node kinds that open a new nesting level. Sibling alternates
    /// within the same container (ElseIf/Else/Case/Catch/Finally) are
    /// deliberately excluded — they share their container's depth rather
    /// than adding another one; see the walkthrough in the module docstring.</summary>
    private static bool IsNestingContainer(SyntaxNode node) => node switch
    {
        MultiLineIfBlockSyntax => true,
        SingleLineIfStatementSyntax => true,
        WhileBlockSyntax => true,
        DoLoopBlockSyntax => true,
        ForBlockSyntax => true,
        ForEachBlockSyntax => true,
        SelectBlockSyntax => true,
        TryBlockSyntax => true,
        _ => false,
    };

    private static int LocalMaxNesting(SyntaxNode node, int depth)
    {
        int max = depth;
        foreach (var child in node.ChildNodes())
        {
            int childDepth = IsNestingContainer(child) ? depth + 1 : depth;
            int childMax = LocalMaxNesting(child, childDepth);
            if (childMax > max)
            {
                max = childMax;
            }
        }
        return max;
    }

    // -- CCN / cognitive ----------------------------------------------------

    private sealed class FunctionAccumulator
    {
        public int Ccn = 1;
        public int Cognitive = 0;
        public int MaxNesting = 0;
        public readonly List<ConditionComplexityDto> ComplexConditions = new();
    }

    private static void WalkForCcnCognitive(SyntaxTree tree, SyntaxNode node, int depth, FunctionAccumulator acc)
    {
        if (depth > acc.MaxNesting)
        {
            acc.MaxNesting = depth;
        }

        switch (node)
        {
            case MultiLineIfBlockSyntax ifBlock:
                acc.Ccn += 1;
                acc.Cognitive += 1 + depth;
                RecordComplexCondition(tree, ifBlock.IfStatement.Condition, "if", acc);
                break;
            case SingleLineIfStatementSyntax singleIf:
                acc.Ccn += 1;
                acc.Cognitive += 1 + depth;
                RecordComplexCondition(tree, singleIf.Condition, "if", acc);
                break;
            case ElseIfBlockSyntax elseIfBlock:
                acc.Ccn += 1;
                acc.Cognitive += 1;
                RecordComplexCondition(tree, elseIfBlock.ElseIfStatement.Condition, "if", acc);
                break;
            case WhileBlockSyntax whileBlock:
                acc.Ccn += 1;
                acc.Cognitive += 1 + depth;
                RecordComplexCondition(tree, whileBlock.WhileStatement.Condition, "while", acc);
                break;
            case DoLoopBlockSyntax doLoop:
                acc.Ccn += 1;
                acc.Cognitive += 1 + depth;
                var doCond = doLoop.DoStatement.WhileOrUntilClause?.Condition
                    ?? doLoop.LoopStatement.WhileOrUntilClause?.Condition;
                if (doCond != null)
                {
                    RecordComplexCondition(tree, doCond, "while", acc);
                }
                break;
            case ForBlockSyntax:
            case ForEachBlockSyntax:
                acc.Ccn += 1;
                acc.Cognitive += 1 + depth;
                break;
            case CaseBlockSyntax caseBlock when caseBlock.CaseStatement.Kind() != SyntaxKind.CaseElseStatement:
                acc.Ccn += 1;
                acc.Cognitive += 1;
                break;
            case CatchBlockSyntax:
                acc.Ccn += 1;
                acc.Cognitive += 1;
                break;
            case TernaryConditionalExpressionSyntax ternary:
                acc.Ccn += 1;
                acc.Cognitive += 1 + depth;
                RecordComplexCondition(tree, ternary.Condition, "ternary", acc);
                break;
            case BinaryExpressionSyntax bin when bin.IsKind(SyntaxKind.AndAlsoExpression) || bin.IsKind(SyntaxKind.OrElseExpression):
                acc.Ccn += 1;
                acc.Cognitive += 1;
                break;
        }

        int childDepth = IsNestingContainer(node) ? depth + 1 : depth;
        foreach (var child in node.ChildNodes())
        {
            WalkForCcnCognitive(tree, child, childDepth, acc);
        }
    }

    private static void RecordComplexCondition(
        SyntaxTree tree, ExpressionSyntax condition, string construct, FunctionAccumulator acc)
    {
        int operatorCount = CountBooleanOperators(condition);
        if (operatorCount == 0)
        {
            return;
        }
        acc.ComplexConditions.Add(new ConditionComplexityDto
        {
            Line = LineIndexOf(tree, condition.SpanStart) + 1,
            OperatorCount = operatorCount,
            EnclosingConstruct = construct,
        });
    }

    private static int CountBooleanOperators(SyntaxNode node)
    {
        int count = 0;
        foreach (var descendant in node.DescendantNodesAndSelf())
        {
            if (descendant.IsKind(SyntaxKind.AndAlsoExpression) || descendant.IsKind(SyntaxKind.OrElseExpression))
            {
                count++;
            }
        }
        return count;
    }

    // -- Fields/params helpers ------------------------------------------------

    private static int CountBumps(SyntaxList<StatementSyntax> body)
    {
        int bumps = 0;
        foreach (var stmt in body)
        {
            // Same depth-0 convention as WalkForCcnCognitive: a top-level
            // statement is not itself nested — LocalMaxNesting only adds a
            // level once it descends into that statement's own children.
            int local = LocalMaxNesting(stmt, 0);
            if (local >= 2)
            {
                bumps++;
            }
        }
        return bumps;
    }

    // -- Whole-file walker ----------------------------------------------------

    private sealed class FileWalker : VisualBasicSyntaxWalker
    {
        private readonly SyntaxTree _tree;
        private readonly SourceText _text;

        public readonly List<FunctionComplexityDto> Functions = new();
        public readonly List<ClassComplexityDto> Classes = new();
        public readonly List<ErrorHandlingHitDto> ErrorHandlingHits = new();
        public readonly List<PerfHitDto> PerfHits = new();

        // Enclosing method name, for perf-hit attribution — updated on
        // entry/exit of each MethodBlockSyntax visited.
        private string? _currentFunctionName;

        public FileWalker(SyntaxTree tree)
        {
            _tree = tree;
            _text = tree.GetText();
        }

        private int LineOf(int position) => LineIndexOf(_tree, position) + 1;

        public override void VisitMethodBlock(MethodBlockSyntax node)
        {
            var previous = _currentFunctionName;
            _currentFunctionName = node.SubOrFunctionStatement.Identifier.ValueText;
            Functions.Add(BuildFunctionComplexity(node, node.SubOrFunctionStatement.ParameterList, node.Statements));
            CollectErrorHandling(node);
            CollectPerfHits(node, _currentFunctionName);
            base.VisitMethodBlock(node);
            _currentFunctionName = previous;
        }

        private FunctionComplexityDto BuildFunctionComplexity(
            SyntaxNode node, ParameterListSyntax? parameters, SyntaxList<StatementSyntax> body)
        {
            var acc = new FunctionAccumulator();
            // Every direct child of the function body starts at nesting
            // depth 0 — a top-level ``For``/``If`` is not itself nested in
            // anything; IsNestingContainer only adds depth for a
            // container's OWN children, applied uniformly inside
            // WalkForCcnCognitive's recursive step below.
            foreach (var child in node.ChildNodes())
            {
                WalkForCcnCognitive(_tree, child, 0, acc);
            }
            int startLine = LineOf(node.SpanStart);
            int endLine = LineOf(node.Span.End);
            return new FunctionComplexityDto
            {
                Name = _currentFunctionName ?? "",
                StartLine = startLine,
                EndLine = endLine,
                Ccn = acc.Ccn,
                Cognitive = acc.Cognitive,
                MaxNesting = acc.MaxNesting,
                Nloc = CountNloc(_text, startLine - 1, endLine - 1),
                Bumps = CountBumps(body),
                ParamCount = parameters?.Parameters.Count ?? 0,
                ComplexConditions = acc.ComplexConditions,
            };
        }

        // -- Classes: WMC + LCOM4/TCC approximation --------------------------

        public override void VisitClassBlock(ClassBlockSyntax node)
        {
            Classes.Add(BuildClassComplexity(node, node.ClassStatement.Identifier.ValueText, node.Members));
            base.VisitClassBlock(node);
        }

        public override void VisitStructureBlock(StructureBlockSyntax node)
        {
            Classes.Add(BuildClassComplexity(node, node.StructureStatement.Identifier.ValueText, node.Members));
            base.VisitStructureBlock(node);
        }

        private ClassComplexityDto BuildClassComplexity(SyntaxNode node, string name, SyntaxList<StatementSyntax> members)
        {
            var fieldNames = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            var methodBlocks = new List<MethodBlockSyntax>();

            foreach (var member in members)
            {
                if (member is FieldDeclarationSyntax field && !field.Modifiers.Any(SyntaxKind.ConstKeyword))
                {
                    foreach (var declarator in field.Declarators)
                    {
                        foreach (var id in declarator.Names)
                        {
                            fieldNames.Add(id.Identifier.ValueText);
                        }
                    }
                }
                else if (member is MethodBlockSyntax methodBlock)
                {
                    methodBlocks.Add(methodBlock);
                }
            }

            var methodNames = new HashSet<string>(
                methodBlocks.Select(m => m.SubOrFunctionStatement.Identifier.ValueText),
                StringComparer.OrdinalIgnoreCase);

            var methods = new List<FunctionComplexityDto>();
            // (fields touched, methods called) per method, in declaration order —
            // matches ``methods`` so the cohesion pass below can zip them.
            var accessSets = new List<(HashSet<string> Fields, HashSet<string> Calls)>();

            foreach (var methodBlock in methodBlocks)
            {
                var previous = _currentFunctionName;
                _currentFunctionName = methodBlock.SubOrFunctionStatement.Identifier.ValueText;
                methods.Add(BuildFunctionComplexity(
                    methodBlock, methodBlock.SubOrFunctionStatement.ParameterList, methodBlock.Statements));
                _currentFunctionName = previous;

                accessSets.Add(CollectAccess(methodBlock, fieldNames, methodNames));
            }

            int fieldCount = fieldNames.Count;
            int methodCount = methods.Count;
            int totalNloc = methods.Sum(m => m.Nloc);
            int maxMethodCcn = methods.Count > 0 ? methods.Max(m => m.Ccn) : 0;

            var namesByIndex = methodBlocks.Select(m => m.SubOrFunctionStatement.Identifier.ValueText).ToList();
            var (lcom4, tcc) = ComputeCohesion(accessSets, namesByIndex);

            return new ClassComplexityDto
            {
                Name = name,
                StartLine = LineOf(node.SpanStart),
                EndLine = LineOf(node.Span.End),
                MethodCount = methodCount,
                TotalNloc = totalNloc,
                Methods = methods,
                Lcom4 = lcom4,
                MaxMethodCcn = maxMethodCcn,
                FieldCount = fieldCount,
                Tcc = tcc,
            };
        }

        /// <summary>Identifiers touched by *methodBlock*, split into "matches a
        /// declared field name" and "matches a declared method name, invoked".
        /// Approximated by name matching (case-insensitively, per D8) rather
        /// than a semantic model (D1) — the same tradeoff vb-support.md §8
        /// documents for LCOM4 generally.</summary>
        private static (HashSet<string> Fields, HashSet<string> Calls) CollectAccess(
            MethodBlockSyntax methodBlock, HashSet<string> fieldNames, HashSet<string> methodNames)
        {
            var fields = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            var calls = new HashSet<string>(StringComparer.OrdinalIgnoreCase);

            foreach (var id in methodBlock.DescendantNodes().OfType<IdentifierNameSyntax>())
            {
                var name = id.Identifier.ValueText;
                if (fieldNames.Contains(name))
                {
                    fields.Add(name);
                }
                bool isInvocationTarget = id.Parent is InvocationExpressionSyntax invocation && invocation.Expression == id
                    || id.Parent is MemberAccessExpressionSyntax memberAccess && memberAccess.Expression is MeExpressionSyntax
                        && memberAccess.Name == id;
                if (isInvocationTarget && methodNames.Contains(name))
                {
                    calls.Add(name);
                }
            }
            return (fields, calls);
        }

        /// <summary>Connected-components LCOM4 + Tight Class Cohesion, both
        /// over the same "shares a field, or calls / is called by" method
        /// graph — TCC narrows to the field-sharing edges only (Bieman-Kang).
        /// Safety valve: fewer than two methods, or zero total field/call
        /// signal across all of them, is "no signal" (lcom4=1, tcc=1.0)
        /// rather than a manufactured 1-component/0-pair result.</summary>
        private static (int Lcom4, double Tcc) ComputeCohesion(
            List<(HashSet<string> Fields, HashSet<string> Calls)> accessSets, List<string> namesByIndex)
        {
            int n = accessSets.Count;
            if (n < 2)
            {
                return (1, 1.0);
            }
            bool anySignal = accessSets.Any(a => a.Fields.Count > 0 || a.Calls.Count > 0);
            if (!anySignal)
            {
                return (1, 1.0);
            }

            var parent = Enumerable.Range(0, n).ToArray();
            int Find(int x) => parent[x] == x ? x : (parent[x] = Find(parent[x]));
            void Union(int a, int b)
            {
                int ra = Find(a), rb = Find(b);
                if (ra != rb)
                {
                    parent[ra] = rb;
                }
            }

            int sharedFieldPairs = 0;
            int totalPairs = 0;
            for (int i = 0; i < n; i++)
            {
                for (int j = i + 1; j < n; j++)
                {
                    totalPairs++;
                    bool sharesField = accessSets[i].Fields.Overlaps(accessSets[j].Fields);
                    if (sharesField)
                    {
                        sharedFieldPairs++;
                        Union(i, j);
                    }
                }
            }
            // Method-call edges also merge components (LCOM4 counts both
            // shared-field and calls-between-methods as cohesion evidence),
            // but do NOT count toward TCC (field-sharing only, Bieman-Kang).
            for (int i = 0; i < n; i++)
            {
                foreach (var calleeName in accessSets[i].Calls)
                {
                    for (int j = 0; j < n; j++)
                    {
                        // Overload sets collapse to one component — the
                        // conservative (fewer false "split") direction to
                        // err in without a semantic model to pick the exact
                        // overload (D1).
                        if (j != i && string.Equals(namesByIndex[j], calleeName, StringComparison.OrdinalIgnoreCase))
                        {
                            Union(i, j);
                        }
                    }
                }
            }

            var components = new HashSet<int>();
            for (int i = 0; i < n; i++)
            {
                components.Add(Find(i));
            }

            double tcc = totalPairs > 0 ? (double)sharedFieldPairs / totalPairs : 1.0;
            return (components.Count, tcc);
        }

        // -- Error handling ---------------------------------------------------

        private void CollectErrorHandling(SyntaxNode node)
        {
            foreach (var descendant in node.DescendantNodes(n => n == node || !(n is MethodBlockSyntax)))
            {
                if (descendant is CatchBlockSyntax catchBlock)
                {
                    int line = LineOf(catchBlock.CatchStatement.SpanStart);
                    if (catchBlock.Statements.Count == 0)
                    {
                        ErrorHandlingHits.Add(new ErrorHandlingHitDto { Kind = "swallowed_catch", Line = line });
                        continue;
                    }
                    var asClause = catchBlock.CatchStatement.AsClause;
                    if (asClause != null && catchBlock.CatchStatement.WhenClause == null
                        && IsBroadExceptionType(asClause.Type))
                    {
                        ErrorHandlingHits.Add(new ErrorHandlingHitDto { Kind = "broad_except", Line = line });
                    }
                }
                else if (descendant.IsKind(SyntaxKind.OnErrorResumeNextStatement))
                {
                    ErrorHandlingHits.Add(new ErrorHandlingHitDto
                    {
                        Kind = "on_error_resume_next",
                        Line = LineOf(descendant.SpanStart),
                    });
                }
            }
        }

        private static bool IsBroadExceptionType(TypeSyntax type)
        {
            var name = type.ToString();
            var bare = name.Contains('.') ? name[(name.LastIndexOf('.') + 1)..] : name;
            return string.Equals(bare, "Exception", StringComparison.OrdinalIgnoreCase);
        }

        // -- Perf hits (starter set) -------------------------------------------

        private void CollectPerfHits(MethodBlockSyntax methodBlock, string functionName)
        {
            bool isAsync = methodBlock.SubOrFunctionStatement.Modifiers.Any(SyntaxKind.AsyncKeyword);

            foreach (var loop in methodBlock.DescendantNodes().Where(IsLoopNode))
            {
                foreach (var assign in loop.DescendantNodes().OfType<AssignmentStatementSyntax>())
                {
                    if (!assign.IsKind(SyntaxKind.AddAssignmentStatement))
                    {
                        continue;
                    }
                    // Only when the RHS is provably string-shaped (a literal or
                    // concatenation of literals) — an opaque `s += x` could be
                    // numeric. Mirrors the tree-sitter walker's precision bar.
                    if (LooksLikeStringExpression(assign.Right))
                    {
                        PerfHits.Add(new PerfHitDto
                        {
                            Kind = "string_concat_in_loop",
                            Line = LineOf(assign.SpanStart),
                            Function = functionName,
                        });
                    }
                }

                foreach (var objCreate in loop.DescendantNodes().OfType<ObjectCreationExpressionSyntax>())
                {
                    var typeName = objCreate.Type.ToString();
                    var bare = typeName.Contains('.') ? typeName[(typeName.LastIndexOf('.') + 1)..] : typeName;
                    if (string.Equals(bare, "Regex", StringComparison.OrdinalIgnoreCase))
                    {
                        PerfHits.Add(new PerfHitDto
                        {
                            Kind = "resource_construction_in_loop",
                            Line = LineOf(objCreate.SpanStart),
                            Function = functionName,
                            Detail = "New Regex",
                        });
                    }
                }
            }

            if (isAsync)
            {
                foreach (var memberAccess in methodBlock.DescendantNodes().OfType<MemberAccessExpressionSyntax>())
                {
                    var member = memberAccess.Name.Identifier.ValueText;
                    if (member is "Result")
                    {
                        PerfHits.Add(new PerfHitDto
                        {
                            Kind = "blocking_sync_in_async",
                            Line = LineOf(memberAccess.SpanStart),
                            Function = functionName,
                            Detail = ".Result",
                        });
                    }
                    else if (member is "Wait" && memberAccess.Parent is InvocationExpressionSyntax)
                    {
                        PerfHits.Add(new PerfHitDto
                        {
                            Kind = "blocking_sync_in_async",
                            Line = LineOf(memberAccess.SpanStart),
                            Function = functionName,
                            Detail = ".Wait()",
                        });
                    }
                }
            }
        }

        private static bool IsLoopNode(SyntaxNode node) =>
            node is ForBlockSyntax or ForEachBlockSyntax or WhileBlockSyntax or DoLoopBlockSyntax;

        private static bool LooksLikeStringExpression(ExpressionSyntax expr) => expr switch
        {
            LiteralExpressionSyntax lit => lit.IsKind(SyntaxKind.StringLiteralExpression)
                || lit.IsKind(SyntaxKind.InterpolatedStringExpression),
            InterpolatedStringExpressionSyntax => true,
            BinaryExpressionSyntax bin when bin.IsKind(SyntaxKind.ConcatenateExpression) =>
                LooksLikeStringExpression(bin.Left) || LooksLikeStringExpression(bin.Right),
            _ => false,
        };
    }
}
