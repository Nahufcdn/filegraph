"""Compatibility entry point for MCP entries that start ``python -m src.main``.

The server itself lives in :mod:`filegraph.main`; this module only forwards to
it, so an entry written before the package rename keeps starting the real
server. Scheduled for removal in 0.2.0.
"""

from __future__ import annotations

from filegraph.main import main, mcp

__all__ = ["main", "mcp"]

if __name__ == "__main__":
    main()
