"""The pre-rename `src` path must keep working, without re-entering the wheel.

An MCP entry registered by an older installer starts ``python -m src.main`` with
``cwd`` set to the checkout, so the shim only has to exist in the working tree.
These tests pin both halves of that decision: the legacy path resolves to the
real server, and ``src`` is never published as an installed package again.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def run_python(code: str) -> subprocess.CompletedProcess[str]:
    """Run a snippet the way an MCP entry runs: cwd is the checkout."""
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_legacy_module_path_resolves_to_the_shim_file():
    result = run_python(
        "import importlib.util as u; print(u.find_spec('src.main').origin)"
    )

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == REPO_ROOT / "src" / "main.py"


def test_legacy_entry_point_exposes_the_real_server():
    """The object the legacy entry starts is filegraph's, not a second copy."""
    result = run_python(
        "import src.main as legacy, filegraph.main as real;"
        "print(legacy.mcp is real.mcp, legacy.main is real.main)"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["True", "True"]


def test_legacy_submodules_are_aliases_not_copies():
    result = run_python(
        "import filegraph.cache as real, src.cache as legacy; print(legacy is real)"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


def test_shim_is_not_published_in_the_wheel():
    """Only `filegraph` ships: a top-level `src` must never reach site-packages."""
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)

    packages = config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]

    assert packages == ["filegraph"]
    assert config["project"]["scripts"]["filegraph"] == "filegraph.cli:main"
