"""VB.NET support via a Roslyn sidecar (see docs/architecture/vb-support.md).

VB is the one analysed language whose AST comes from outside the Python
process. This package owns the sidecar build, its process lifecycle, and the
preflight check that gates a run before it half-indexes a VB repo.
"""

from __future__ import annotations
