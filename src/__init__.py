"""Compatibility shim for the layout where the package was called ``src``.

MCP entries registered by an older installer start ``python -m src.main``, which
resolves from the entry's ``cwd`` — the checkout — so this package keeps those
entries working without the published wheel ever shipping a top-level ``src``
again. ``pyproject.toml`` lists only ``filegraph`` under the wheel packages, and
that is deliberate: importing ``src`` from an installed environment is not
supported and never will be.

Scheduled for removal in 0.2.0, once entries have had a release to be repaired.
"""

from __future__ import annotations

import sys
import warnings

from filegraph import cache, cli, scanner, store, tools

warnings.warn(
    "the `src` package is a compatibility shim for the pre-rename layout and "
    "will be removed in 0.2.0; import `filegraph` instead",
    DeprecationWarning,
    stacklevel=2,
)

# Alias the submodules in sys.modules so `from src.cache import ...` keeps
# working as an alias of the real module rather than a second copy of it.
# `src.main` is deliberately absent: it stays a real file so that
# `python -m src.main` executes it normally instead of re-executing
# `filegraph.main` through runpy.
for _module in (cache, cli, scanner, store, tools):
    sys.modules[f"{__name__}.{_module.__name__.rsplit('.', 1)[-1]}"] = _module

del _module
