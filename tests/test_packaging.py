"""Packaging contract: only ``filegraph`` is ever published.

The package used to be a directory literally named ``src``, so the wheel shipped
a top-level ``src`` into site-packages. That name must not come back, and the
console script must keep pointing at the package that actually exists.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_only_filegraph_is_published():
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)

    packages = config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    scripts = config["project"]["scripts"]

    assert packages == ["filegraph"]
    assert scripts["filegraph"] == "filegraph.cli:main"


def test_entry_point_module_exists():
    """The console script target must be a real module in the published package."""
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        target = tomllib.load(handle)["project"]["scripts"]["filegraph"]

    module_path, _, attribute = target.partition(":")
    on_disk = REPO_ROOT.joinpath(*module_path.split(".")).with_suffix(".py")

    assert on_disk.is_file(), f"{target} does not resolve to {on_disk}"
    assert attribute


def test_legacy_src_package_is_gone():
    """The compatibility shim was retired; the old module path must not resolve."""
    assert not (REPO_ROOT / "src").exists()
