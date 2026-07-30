"""LanguageSpec for aspx (extracted from the registry data table).

ASP.NET Web Forms markup — ``.aspx`` (pages), ``.ascx`` (user controls),
``.master`` (master pages). No AST grammar; the ``CodeBehind``/``CodeFile``
attribute pairing a markup file to its ``.aspx.vb``/``.aspx.cs`` code-behind
class is handled by ``dynamic_hints/webforms.py`` (mirrors XAML's
``dynamic_uses`` binding edges — see ``specs/xaml.py``). Registered here so
the traverser surfaces a file node those edges can attach to.
"""

from ..spec import LanguageSpec

SPEC = LanguageSpec(
    tag="aspx",
    display_name="ASP.NET Web Forms",
    extensions=frozenset({".aspx", ".ascx", ".master"}),
    is_code=False,
    is_passthrough=True,
    color_hex="#6A2C70",
)
