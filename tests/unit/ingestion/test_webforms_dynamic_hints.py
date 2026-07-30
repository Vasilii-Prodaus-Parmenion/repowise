"""Unit tests for the ASP.NET Web Forms code-behind dynamic-hint extractor.

vb-support.md §6.2: the ``.aspx``/``.ascx`` markup is the entry point; its
``CodeBehind``/``CodeFile`` code-behind class is reached only through it.
Real files on disk, no mocking — the extractor's own design is a plain
filesystem scan.
"""

from __future__ import annotations

from pathlib import Path

from repowise.core.ingestion.dynamic_hints.webforms import WebFormsDynamicHints


def test_codebehind_attribute_resolves_relative_to_markup_file(tmp_path: Path) -> None:
    (tmp_path / "Login.aspx").write_text(
        '<%@ Page Language="VB" CodeBehind="Login.aspx.vb" Inherits="MyApp.Login" %>'
    )
    (tmp_path / "Login.aspx.vb").write_text("Public Class Login\nEnd Class\n")

    edges = WebFormsDynamicHints().extract(tmp_path)

    assert len(edges) == 1
    edge = edges[0]
    assert edge.source == "Login.aspx"
    assert edge.target == "Login.aspx.vb"
    assert edge.edge_type == "dynamic_uses"
    assert edge.hint_source == "webforms:codebehind"


def test_ascx_user_control_codefile_attribute(tmp_path: Path) -> None:
    (tmp_path / "Greeting.ascx").write_text(
        '<%@ Control Language="VB" CodeFile="Greeting.ascx.vb" Inherits="Greeting" %>'
    )
    (tmp_path / "Greeting.ascx.vb").write_text("Public Class Greeting\nEnd Class\n")

    edges = WebFormsDynamicHints().extract(tmp_path)

    assert len(edges) == 1
    assert edges[0].source == "Greeting.ascx"
    assert edges[0].target == "Greeting.ascx.vb"


def test_codebehind_in_subdirectory_resolves_relative_to_markup_dir(tmp_path: Path) -> None:
    pages = tmp_path / "Pages"
    pages.mkdir()
    (pages / "Login.aspx").write_text('<%@ Page CodeBehind="Login.aspx.cs" Inherits="X" %>')
    (pages / "Login.aspx.cs").write_text("public class Login {}\n")

    edges = WebFormsDynamicHints().extract(tmp_path)

    assert len(edges) == 1
    assert edges[0].source == "Pages/Login.aspx"
    assert edges[0].target == "Pages/Login.aspx.cs"


def test_no_directive_yields_no_edges(tmp_path: Path) -> None:
    (tmp_path / "Static.aspx").write_text("<html><body>no directive here</body></html>")

    assert WebFormsDynamicHints().extract(tmp_path) == []


def test_my_project_directory_is_skipped(tmp_path: Path) -> None:
    my_project = tmp_path / "My Project"
    my_project.mkdir()
    (my_project / "Ignored.aspx").write_text('<%@ Page CodeBehind="Ignored.aspx.vb" %>')
    (my_project / "Ignored.aspx.vb").write_text("Public Class Ignored\nEnd Class\n")

    assert WebFormsDynamicHints().extract(tmp_path) == []
