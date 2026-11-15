"""Contract of the MCP entry that the installer writes.

These drive ``scripts/setup-mcp.sh install`` as a black box against a scratch
config, because what users depend on is the entry that ends up on disk, not the
jq expression that produces it. The entry is a consumed contract: a stale module
name leaves the server unable to start while everything still looks installed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALLER = REPO_ROOT / "scripts" / "setup-mcp.sh"
PROJECT_ROOT = str(REPO_ROOT)
MODULE = "filegraph.main"

pytestmark = pytest.mark.skipif(
    shutil.which("jq") is None, reason="scripts/setup-mcp.sh requires jq"
)


def install(config: Path) -> subprocess.CompletedProcess[str]:
    """Run the installer as a user would, pointed at one scratch config."""
    env = dict(
        os.environ,
        OPENCODE_CONFIG=str(config),
        FILEGRAPH_PYTHON=sys.executable,
    )
    return subprocess.run(
        ["bash", str(INSTALLER), "install"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def entry_of(config: Path) -> dict:
    return json.loads(config.read_text(encoding="utf-8"))["mcp"]["filegraph"]


def test_install_registers_the_filegraph_module_and_keeps_other_keys(tmp_path):
    config = tmp_path / "opencode.json"
    config.write_text(
        json.dumps({"mcp": {"other": {"command": ["x"]}}, "theme": "dark"}),
        encoding="utf-8",
    )

    result = install(config)

    assert result.returncode == 0, result.stderr
    entry = entry_of(config)
    assert entry["command"] == [sys.executable, "-m", MODULE]
    assert entry["cwd"] == PROJECT_ROOT
    assert entry["type"] == "local"
    written = json.loads(config.read_text(encoding="utf-8"))
    assert written["theme"] == "dark"
    assert written["mcp"]["other"] == {"command": ["x"]}


def test_repair_updates_a_stale_src_module_and_cwd(tmp_path):
    config = tmp_path / "opencode.json"
    config.write_text(
        json.dumps(
            {
                "mcp": {
                    "filegraph": {
                        "command": ["/old/python", "-m", "src.main"],
                        "type": "local",
                        "cwd": "/old/checkout",
                    }
                },
                "theme": "dark",
            }
        ),
        encoding="utf-8",
    )

    result = install(config)

    assert result.returncode == 0, result.stderr
    entry = entry_of(config)
    assert entry["command"] == [sys.executable, "-m", MODULE]
    assert entry["cwd"] == PROJECT_ROOT
    assert json.loads(config.read_text(encoding="utf-8"))["theme"] == "dark"


def test_repair_keeps_keys_the_user_added_inside_the_entry(tmp_path):
    """The installer owns command, type and cwd. Everything else is the user's."""
    config = tmp_path / "opencode.json"
    config.write_text(
        json.dumps(
            {
                "mcp": {
                    "filegraph": {
                        "command": ["/old/python", "-m", "src.main"],
                        "type": "local",
                        "cwd": "/old/checkout",
                        "enabled": False,
                        "env": {"API_KEY": "secret"},
                        "description": "user note",
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    result = install(config)

    assert result.returncode == 0, result.stderr
    entry = entry_of(config)
    assert entry["command"] == [sys.executable, "-m", MODULE]
    assert entry["enabled"] is False
    assert entry["env"] == {"API_KEY": "secret"}
    assert entry["description"] == "user note"


def test_second_install_is_a_no_op_and_the_backup_stays_pristine(tmp_path):
    config = tmp_path / "opencode.json"
    original = json.dumps({"mcp": {}})
    config.write_text(original, encoding="utf-8")

    assert install(config).returncode == 0
    backup = config.with_name(config.name + ".filegraph.bak")
    assert backup.read_text(encoding="utf-8") == original
    after_first = config.read_bytes()

    result = install(config)

    assert result.returncode == 0, result.stderr
    assert "already installed" in result.stdout
    assert config.read_bytes() == after_first
    assert backup.read_text(encoding="utf-8") == original


def test_malformed_config_is_reported_and_left_untouched(tmp_path):
    config = tmp_path / "opencode.json"
    config.write_text('{"mcp": {,}', encoding="utf-8")
    original = config.read_bytes()

    result = install(config)

    assert result.returncode != 0
    assert config.read_bytes() == original
