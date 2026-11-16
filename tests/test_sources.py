"""Tests for the per-source definition: config file, validation, ssh argv.

Covers S1, S2 and S4 of ``odd/tasks/ssh-ingest-pull.md``: the source model with
a remote path and a local mount path, the ``sources.toml`` location and its
``0700``/``0600`` permissions, the validation rules, and the ``ssh`` argument
list built from the configured address and the per-source command template.
"""

from __future__ import annotations

import stat
import tomllib
from pathlib import Path

import pytest

from filegraph import sources


@pytest.fixture(autouse=True)
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep every config this module writes inside the test's tmp_path."""
    home = tmp_path / "xdg-config"
    home.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    return home


@pytest.fixture
def mount(tmp_path: Path) -> Path:
    """An existing local mount directory to use as ``local_path``."""
    path = tmp_path / "mount"
    path.mkdir()
    return path


def _make(mount: Path, **overrides: object) -> sources.Source:
    fields: dict[str, object] = {
        "name": "tank",
        "host": "truenas.local",
        "user": "backup",
        "remote_path": "/mnt/tank",
        "local_path": str(mount),
    }
    fields.update(overrides)
    return sources.Source(**fields)


# ---------------------------------------------------------------------------
# T1: where the config lives (S1, S2)
# ---------------------------------------------------------------------------


def test_config_path_honours_xdg_config_home(config_home: Path) -> None:
    assert sources.config_path() == config_home / "filegraph" / "sources.toml"


def test_config_path_defaults_to_home_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    assert sources.config_path() == tmp_path / "home" / ".config" / "filegraph" / "sources.toml"


def test_load_sources_without_a_config_file_is_empty() -> None:
    assert sources.load_sources() == {}


# ---------------------------------------------------------------------------
# T1: the source model and its default remote command (S1, S4)
# ---------------------------------------------------------------------------


def test_default_remote_command_is_find_printf() -> None:
    assert sources.DEFAULT_REMOTE_COMMAND == "find {path} -printf '%y\t%s\t%T@\t%p\n'"


def test_source_without_remote_command_uses_the_default(mount: Path) -> None:
    assert _make(mount).remote_command == sources.DEFAULT_REMOTE_COMMAND


def test_remote_command_substitutes_only_the_path_placeholder(mount: Path) -> None:
    source = _make(
        mount, remote_command="wrapper --root {path} --other {root} --again {path}"
    )

    assert (
        sources.remote_command_for(source)
        == "wrapper --root /mnt/tank --other {root} --again /mnt/tank"
    )


def test_remote_command_default_gets_the_remote_path(mount: Path) -> None:
    assert (
        sources.remote_command_for(_make(mount))
        == "find /mnt/tank -printf '%y\t%s\t%T@\t%p\n'"
    )


# ---------------------------------------------------------------------------
# T1: the ssh argument list (S4)
# ---------------------------------------------------------------------------


def test_command_line_without_port_or_key(mount: Path) -> None:
    assert sources.command_line(_make(mount)) == [
        "ssh",
        "backup@truenas.local",
        "find /mnt/tank -printf '%y\t%s\t%T@\t%p\n'",
    ]


def test_command_line_with_port_and_key(mount: Path) -> None:
    source = _make(mount, port=2222, key="/home/backup/.ssh/id_tank")

    assert sources.command_line(source) == [
        "ssh",
        "-p",
        "2222",
        "-i",
        "/home/backup/.ssh/id_tank",
        "backup@truenas.local",
        "find /mnt/tank -printf '%y\t%s\t%T@\t%p\n'",
    ]


def test_command_line_uses_a_configured_template(mount: Path) -> None:
    source = _make(mount, remote_command="tank-listing {path}")

    assert sources.command_line(source)[-1] == "tank-listing /mnt/tank"


# ---------------------------------------------------------------------------
# T2: validation of the source fields (S2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("field", ["remote_path", "local_path"])
def test_source_requires_absolute_paths(mount: Path, field: str) -> None:
    with pytest.raises(sources.SourceError) as excinfo:
        _make(mount, **{field: "mnt/tank"})

    assert str(excinfo.value) == f"{field.replace('_', ' ')} must be absolute: mnt/tank"


@pytest.mark.parametrize(
    "field, message",
    [
        ("name", "source name must not be empty"),
        ("host", "host must not be empty"),
        ("user", "user must not be empty"),
    ],
)
def test_source_requires_identity_fields(mount: Path, field: str, message: str) -> None:
    with pytest.raises(sources.SourceError) as excinfo:
        _make(mount, **{field: ""})

    assert str(excinfo.value) == message


@pytest.mark.parametrize("port", [0, -1, 65536, 100000])
def test_source_rejects_out_of_range_port(mount: Path, port: int) -> None:
    with pytest.raises(sources.SourceError) as excinfo:
        _make(mount, port=port)

    assert str(excinfo.value) == f"port out of range: {port}"


# ---------------------------------------------------------------------------
# T2: save/load and permissions (S2)
# ---------------------------------------------------------------------------


def test_save_then_load_roundtrips_every_field(mount: Path) -> None:
    source = _make(
        mount,
        port=2222,
        key="/home/backup/.ssh/id_tank",
        remote_command="tank-listing {path}",
    )

    sources.add_source(source)

    assert sources.load_sources() == {"tank": source}


def test_saved_config_is_private(config_home: Path, mount: Path) -> None:
    sources.add_source(_make(mount))

    config_dir = config_home / "filegraph"
    config_file = config_dir / "sources.toml"
    assert stat.S_IMODE(config_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(config_file.stat().st_mode) == 0o600
    assert tomllib.loads(config_file.read_text(encoding="utf-8"))["sources"]["tank"] == {
        "host": "truenas.local",
        "user": "backup",
        "remote_path": "/mnt/tank",
        "local_path": str(mount),
        "remote_command": sources.DEFAULT_REMOTE_COMMAND,
    }


def test_second_save_keeps_mode_0600(config_home: Path, mount: Path) -> None:
    sources.add_source(_make(mount))
    sources.add_source(_make(mount, host="other.local"), force=True)

    config_file = config_home / "filegraph" / "sources.toml"
    assert stat.S_IMODE(config_file.stat().st_mode) == 0o600
    assert sources.load_sources()["tank"].host == "other.local"


def test_saving_two_sources_keeps_both(mount: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()

    sources.add_source(_make(mount))
    sources.add_source(_make(other, name="media", remote_path="/mnt/media"))

    assert sorted(sources.load_sources()) == ["media", "tank"]


def test_add_requires_force_to_replace(mount: Path) -> None:
    sources.add_source(_make(mount))

    with pytest.raises(sources.SourceError) as excinfo:
        sources.add_source(_make(mount, host="other.local"))

    assert str(excinfo.value) == "source already exists: tank (use --force to replace)"
    assert sources.load_sources()["tank"].host == "truenas.local"


def test_remove_source_deletes_only_that_entry(mount: Path, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    sources.add_source(_make(mount))
    sources.add_source(_make(other, name="media", remote_path="/mnt/media"))

    sources.remove_source("tank")

    assert sorted(sources.load_sources()) == ["media"]


def test_remove_unknown_source_fails() -> None:
    with pytest.raises(sources.SourceError) as excinfo:
        sources.remove_source("tank")

    assert str(excinfo.value) == "no such source: tank"


# ---------------------------------------------------------------------------
# T2: local path validation (S2)
# ---------------------------------------------------------------------------


def test_add_accepts_an_existing_directory(mount: Path) -> None:
    sources.add_source(_make(mount))

    assert sources.load_sources()["tank"].local_path == str(mount)


def test_add_accepts_a_mount_that_does_not_exist_yet(tmp_path: Path) -> None:
    pending = tmp_path / "not-mounted-yet"

    sources.add_source(_make(pending))

    assert sources.load_sources()["tank"].local_path == str(pending)


def test_add_rejects_a_local_path_whose_parent_is_missing(tmp_path: Path) -> None:
    missing = tmp_path / "absent" / "mount"

    with pytest.raises(sources.SourceError) as excinfo:
        sources.add_source(_make(missing))

    assert str(excinfo.value) == f"local path parent does not exist: {missing.parent}"
    assert sources.load_sources() == {}


def test_add_rejects_a_local_path_that_is_a_file(tmp_path: Path) -> None:
    existing_file = tmp_path / "not-a-dir"
    existing_file.write_text("x")

    with pytest.raises(sources.SourceError) as excinfo:
        sources.add_source(_make(existing_file))

    assert str(excinfo.value) == f"local path is not a directory: {existing_file}"
