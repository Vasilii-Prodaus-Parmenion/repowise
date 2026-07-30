"""LanguageSpec for vbnet (extracted from the registry data table).

VB.NET has no tree-sitter grammar; its AST comes from a Roslyn sidecar
process instead (see docs/architecture/vb-support.md). ``grammar_package``
and ``scm_file`` stay ``None`` on purpose — there is no ``.scm`` query file
to write, and ``language_configs.py`` documents the same absence on the
parser side.
"""

from ..spec import LanguageSpec

SPEC = LanguageSpec(
    tag="vbnet",
    display_name="VB.NET",
    import_support="full",
    test_dir_suffixes=(".Tests",),
    extensions=frozenset({".vb"}),
    entry_point_patterns=("Program.vb", "Main.vb"),
    generated_suffixes=(".Designer.vb",),
    blocked_dirs=("bin", "obj", "My Project"),
    color_hex="#945db7",
)
