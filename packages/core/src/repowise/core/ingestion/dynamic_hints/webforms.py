"""Dynamic-hint extractor for ASP.NET Web Forms code-behind pairing.

Why this exists
================
A Web Forms page/control/master is declared as markup (``.aspx`` / ``.ascx``
/ ``.master``) plus a paired code-behind class, wired together by the
``CodeBehind`` (compiled-and-deployed model) or ``CodeFile`` (in-place
compilation model) attribute on the page directive::

    <%@ Page Language="VB" CodeBehind="Login.aspx.vb" Inherits="MyApp.Login" %>
    <%@ Control Language="VB" CodeFile="UserGreeting.ascx.vb" Inherits="UserGreeting" %>

The ``.aspx``/``.ascx`` file is the entry point — IIS/ASP.NET instantiates
the code-behind class per HTTP request, so nothing in the repo ever writes a
static reference to it (see vb-support.md §6.2). Without this edge, every
Web Forms page's code-behind class reads as an unreachable file to
``DeadCodeAnalyzer._detect_unreachable_files``, and its members as unused
exports.

Design
======
Pure regex pass, mirroring ``dynamic_hints/xaml.py``'s shape: no XML parser,
works on partial/malformed markup mid-edit. The code-behind path is resolved
relative to the markup file's own directory — the universal convention for
both the VB and C# Web Forms templates (Visual Studio has never generated
any other layout). Language-agnostic: the code-behind half may be
``.aspx.vb`` or ``.aspx.cs``, and this extractor doesn't care which.
"""

from __future__ import annotations

import re
from pathlib import Path

from .base import DynamicEdge, DynamicHintExtractor

_SKIP_DIRS = {"bin", "obj", ".vs", "node_modules", ".git", "My Project"}
_MARKUP_EXTS = (".aspx", ".ascx", ".master")

# <%@ Page ... CodeBehind="Login.aspx.vb" ... %> / <%@ Control ... CodeFile="X.ascx.vb" ... %>
# Attribute order in the directive is not fixed, so match the attribute
# itself rather than anchoring to "Page"/"Control"/"Master".
_CODE_BEHIND_RE = re.compile(
    r"""(?:CodeBehind|CodeFile)\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)


class WebFormsDynamicHints(DynamicHintExtractor):
    """Emit ``dynamic_uses`` edges from Web Forms markup to its code-behind."""

    name = "webforms"

    def extract(self, repo_root: Path) -> list[DynamicEdge]:
        repo_root_resolved = repo_root.resolve()
        edges: list[DynamicEdge] = []

        for ext in _MARKUP_EXTS:
            for markup_path in self._rglob(repo_root, f"*{ext}"):
                try:
                    rel = markup_path.resolve().relative_to(repo_root_resolved)
                except ValueError:
                    continue
                if any(part in _SKIP_DIRS for part in rel.parts):
                    continue
                rel_posix = rel.as_posix()
                try:
                    text = markup_path.read_text(encoding="utf-8-sig", errors="ignore")
                except OSError:
                    continue

                match = _CODE_BEHIND_RE.search(text)
                if not match:
                    continue
                raw = match.group(1).strip()
                if not raw:
                    continue
                candidate = (markup_path.parent / raw).resolve()
                try:
                    target_rel = candidate.relative_to(repo_root_resolved).as_posix()
                except ValueError:
                    continue
                if target_rel == rel_posix:
                    continue
                edges.append(
                    DynamicEdge(
                        source=rel_posix,
                        target=target_rel,
                        edge_type="dynamic_uses",
                        hint_source=f"{self.name}:codebehind",
                    )
                )

        return edges
