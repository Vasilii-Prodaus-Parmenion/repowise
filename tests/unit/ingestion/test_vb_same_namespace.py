"""Unit tests for VB.NET same-namespace + project-Import implicit reference edges."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import networkx as nx

from repowise.core.ingestion.languages.vb_same_namespace import (
    resolve_vb_same_namespace_refs,
)


def _graph_for(texts: dict[str, str]) -> nx.DiGraph:
    g = nx.DiGraph()
    for p in texts:
        g.add_node(p, node_type="file")
    return g


class TestVbSameNamespace:
    def test_sibling_type_reference_produces_edge(self) -> None:
        texts = {
            "src/Core/Order.vb": "Namespace Acme.Core\nPublic Class Order\nEnd Class\nEnd Namespace\n",
            "src/Core/OrderService.vb": (
                "Namespace Acme.Core\nPublic Class OrderService\n"
                "    Private _pending As Order\n"
                "End Class\nEnd Namespace\n"
            ),
        }
        g = _graph_for(texts)
        added = resolve_vb_same_namespace_refs(g, None, texts, None)
        assert added == 1
        edge = g["src/Core/OrderService.vb"]["src/Core/Order.vb"]
        assert edge["edge_type"] == "imports"
        assert edge["hint_source"] == "same_namespace"
        assert edge["imported_names"] == ["Order"]

    def test_no_namespace_and_no_index_indexes_nothing(self) -> None:
        # With no DotNetProjectIndex there is no RootNamespace to fall back
        # on, and neither file declares a Namespace block — no effective
        # namespace exists for either file, so no edge is possible.
        texts = {
            "src/Order.vb": "Public Class Order\nEnd Class\n",
            "src/Svc.vb": "Public Class Svc\n    Private _o As Order\nEnd Class\n",
        }
        g = _graph_for(texts)
        added = resolve_vb_same_namespace_refs(g, None, texts, None)
        assert added == 0

    def test_ambiguous_type_produces_no_edge(self) -> None:
        texts = {
            "src/A/Thing.vb": "Namespace Acme\nPartial Public Class Thing\nEnd Class\nEnd Namespace\n",
            "src/B/Thing.vb": "Namespace Acme\nPartial Public Class Thing\nEnd Class\nEnd Namespace\n",
            "src/User.vb": (
                "Namespace Acme\nPublic Class User\n    Private t As Thing\nEnd Class\nEnd Namespace\n"
            ),
        }
        g = _graph_for(texts)
        added = resolve_vb_same_namespace_refs(g, None, texts, None)
        assert added == 0

    def test_bcl_name_produces_no_edge(self) -> None:
        texts = {
            "src/Task.vb": "Namespace Acme\nPublic Class Task\nEnd Class\nEnd Namespace\n",
            "src/Runner.vb": (
                "Namespace Acme\nPublic Class Runner\n    Private t As Task\nEnd Class\nEnd Namespace\n"
            ),
        }
        g = _graph_for(texts)
        added = resolve_vb_same_namespace_refs(g, None, texts, None)
        assert added == 0

    def test_alias_imports_shadows(self) -> None:
        texts = {
            "src/Helper.vb": "Namespace Acme\nPublic Class Helper\nEnd Class\nEnd Namespace\n",
            "src/Consumer.vb": (
                "Imports Helper = Other.Place.Helper\n"
                "Namespace Acme\nPublic Class Consumer\n    Private h As Helper\nEnd Class\nEnd Namespace\n"
            ),
        }
        g = _graph_for(texts)
        added = resolve_vb_same_namespace_refs(g, None, texts, None)
        assert added == 0

    def test_cross_namespace_name_produces_no_edge(self) -> None:
        texts = {
            "src/Core/Widget.vb": "Namespace Acme.Core\nPublic Class Widget\nEnd Class\nEnd Namespace\n",
            "src/Web/Page.vb": (
                "Namespace Acme.Web\nPublic Class Page\n    Private w As Widget\nEnd Class\nEnd Namespace\n"
            ),
        }
        g = _graph_for(texts)
        added = resolve_vb_same_namespace_refs(g, None, texts, None)
        assert added == 0

    def test_existing_edge_wins(self) -> None:
        texts = {
            "src/Order.vb": "Namespace Acme\nPublic Class Order\nEnd Class\nEnd Namespace\n",
            "src/Svc.vb": (
                "Namespace Acme\nPublic Class Svc\n    Private o As Order\nEnd Class\nEnd Namespace\n"
            ),
        }
        g = _graph_for(texts)
        g.add_edge("src/Svc.vb", "src/Order.vb", edge_type="imports", confidence=1.0)
        added = resolve_vb_same_namespace_refs(g, None, texts, None)
        assert added == 0
        assert "hint_source" not in g["src/Svc.vb"]["src/Order.vb"]

    def test_case_insensitive_reference_still_matches(self) -> None:
        # VB matches identifiers case-insensitively (D8) — a reference whose
        # casing drifts from the declaration must still resolve.
        texts = {
            "src/Order.vb": "Namespace Acme\nPublic Class Order\nEnd Class\nEnd Namespace\n",
            "src/Svc.vb": (
                "Namespace Acme\nPublic Class Svc\n    Private o As ORDER\nEnd Class\nEnd Namespace\n"
            ),
        }
        g = _graph_for(texts)
        added = resolve_vb_same_namespace_refs(g, None, texts, None)
        assert added == 1
        assert g.has_edge("src/Svc.vb", "src/Order.vb")


class TestVbRootNamespaceTier:
    def test_root_namespace_alone_ties_siblings_together(self) -> None:
        # No Namespace block in either file — RootNamespace itself is the
        # effective namespace shared by both (vb-support.md §5.5).
        texts = {
            "src/Order.vb": "Public Class Order\nEnd Class\n",
            "src/Svc.vb": "Public Class Svc\n    Private o As Order\nEnd Class\n",
        }
        g = _graph_for(texts)

        class _FakeIndex:
            file_to_project: ClassVar[dict] = {}

            def project_for_file(self, path):
                class _P:
                    root_namespace = "Acme.Legacy"

                return _P()

            def globals_for_project(self, csproj):
                return set()

        added = resolve_vb_same_namespace_refs(g, _FakeIndex(), texts, Path("/repo"))
        assert added == 1
        assert g.has_edge("src/Svc.vb", "src/Order.vb")


class TestVbProjectImportTier:
    def test_project_level_import_links_zero_imports_file(self) -> None:
        texts = {
            "test/Specs/Helpers/Doer.vb": (
                "Namespace Acme.Specs.Helpers\nPublic Class Doer\nEnd Class\nEnd Namespace\n"
            ),
            "test/Specs/RetrySpecs.vb": (
                "Namespace Acme.Specs\nPublic Class RetrySpecs\n"
                "    Public Sub Runs()\n        Dim d As New Doer()\n    End Sub\n"
                "End Class\nEnd Namespace\n"
            ),
        }
        g = _graph_for(texts)

        class _FakeIndex:
            file_to_project: ClassVar[dict] = {
                Path("/repo/test/Specs/RetrySpecs.vb").resolve(): Path("Specs.vbproj")
            }

            def project_for_file(self, path):
                return None

            def globals_for_project(self, csproj):
                return {"Acme.Specs.Helpers"}

        added = resolve_vb_same_namespace_refs(g, _FakeIndex(), texts, Path("/repo"))
        assert added == 1
        edge = g["test/Specs/RetrySpecs.vb"]["test/Specs/Helpers/Doer.vb"]
        assert edge["hint_source"] == "global_using"

    def test_explicit_imports_namespace_shadows_project_tier(self) -> None:
        texts = {
            "src/Helpers/Doer.vb": (
                "Namespace Acme.Helpers\nPublic Class Doer\nEnd Class\nEnd Namespace\n"
            ),
            "src/Consumer.vb": (
                "Imports Acme.Helpers\n"
                "Namespace Acme\nPublic Class Consumer\n"
                "    Private d As Doer\nEnd Class\nEnd Namespace\n"
            ),
        }
        g = _graph_for(texts)

        class _FakeIndex:
            file_to_project: ClassVar[dict] = {
                Path("/repo/src/Consumer.vb").resolve(): Path("proj.vbproj")
            }

            def project_for_file(self, path):
                return None

            def globals_for_project(self, csproj):
                return {"Acme.Helpers"}

        added = resolve_vb_same_namespace_refs(g, _FakeIndex(), texts, Path("/repo"))
        assert added == 0
