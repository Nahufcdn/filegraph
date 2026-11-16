"""Tests for ``filegraph sources`` and ``filegraph pull``.

Covers S2-S7 of ``odd/tasks/ssh-ingest-pull.md``. No SSH server is involved: a
fake ``ssh`` executable is placed first on ``PATH``, so the tests drive the real
argument list, the real stream handling and the real import, and can make the
stream fail in each documented way.
"""

from __future__ import annotations

import inspect
import json
import os
import re
import shlex
import stat
import sys
import tracemalloc
from pathlib import Path

import pytest

from filegraph import index as tree_index
from filegraph import sources
from filegraph.cli import main

REMOTE = "/mnt/tank"
SECRET = "-----BEGIN OPENSSH PRIVATE KEY-----\nnot-a-real-key\n"

_FAKE_SSH = '''#!{python}
"""A stand-in ``ssh``: it records its arguments and streams canned rows."""

import json
import os
import signal
import sys


def _emit(text):
    sys.stdout.write(text)
    sys.stdout.flush()


def _listing():
    with open(
        os.environ["FAKE_SSH_LISTING"],
        encoding="utf-8",
        errors="surrogateescape",
        newline="",
    ) as handle:
        while True:
            chunk = handle.read(65536)
            if not chunk:
                return
            _emit(chunk)


def _generated():
    remote = os.environ["FAKE_SSH_REMOTE"]
    count = int(os.environ["FAKE_SSH_COUNT"])
    _emit("d\\t4096\\t1700000000.0\\t" + remote + "\\n")
    chunk = []
    for index in range(count):
        chunk.append(
            "f\\t%d\\t1700000000.0\\t%s/dir%03d/file%06d.txt\\n"
            % (index, remote, index % 100, index)
        )
        if len(chunk) == 5000:
            _emit("".join(chunk))
            chunk.clear()
    if chunk:
        _emit("".join(chunk))


def main():
    argv = sys.argv[1:]
    target = os.environ.get("FAKE_SSH_ARGV")
    if target:
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(argv, handle)
    mode = os.environ.get("FAKE_SSH_MODE", "listing")
    if mode == "listing":
        _listing()
        return 0
    if mode == "generated":
        _generated()
        return 0
    if mode == "empty":
        return 0
    if mode == "partial-fail":
        _emit(os.environ["FAKE_SSH_ROWS"])
        return int(os.environ.get("FAKE_SSH_STATUS", "3"))
    if mode == "signal":
        _emit(os.environ["FAKE_SSH_ROWS"])
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGPIPE)
        return 0
    raise SystemExit("unknown FAKE_SSH_MODE: " + mode)


if __name__ == "__main__":
    sys.exit(main())
'''


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep the config and every index this module writes inside tmp_path."""
    config = tmp_path / "xdg-config"
    cache = tmp_path / "xdg-cache"
    config.mkdir()
    cache.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    return tmp_path


@pytest.fixture
def fake_ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_xdg: Path) -> Path:
    """Put a fake ``ssh`` first on PATH and return the directory holding it."""
    bin_dir = isolated_xdg / "fake-bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "ssh"
    script.write_text(_FAKE_SSH.format(python=sys.executable), encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(bin_dir), os.environ.get("PATH", "/usr/bin:/bin")])
    )
    monkeypatch.setenv("FAKE_SSH_ARGV", str(isolated_xdg / "ssh-argv.json"))
    return bin_dir


@pytest.fixture
def mount(tmp_path: Path) -> Path:
    path = tmp_path / "mount"
    path.mkdir()
    return path


def _run(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, str, str]:
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _add(
    capsys: pytest.CaptureFixture[str],
    mount: Path,
    *extra: str,
    name: str = "tank",
    remote: str = REMOTE,
) -> tuple[int, str, str]:
    return _run(
        capsys,
        [
            "sources",
            "add",
            name,
            "--host",
            "truenas.local",
            "--user",
            "backup",
            "--remote-path",
            remote,
            "--local-path",
            str(mount),
            *extra,
        ],
    )


def _listing(tmp_path: Path, rows: list[str], name: str = "rows.tsv") -> Path:
    path = tmp_path / name
    path.write_text("".join(f"{row}\n" for row in rows), encoding="utf-8")
    return path


def _use_listing(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("FAKE_SSH_MODE", "listing")
    monkeypatch.setenv("FAKE_SSH_LISTING", str(path))


def _argv_recorded(base: Path) -> list[str]:
    return json.loads((base / "ssh-argv.json").read_text(encoding="utf-8"))


def _tree_rows(local: Path) -> list[str]:
    """The rows of ``local``'s tree: root dir, one dir, two files, one symlink."""
    return [
        f"d\t4096\t1700000000.0\t{REMOTE}",
        f"d\t4096\t1700000000.0\t{REMOTE}/photos",
        f"f\t5\t1700000000.0\t{REMOTE}/photos/a.txt",
        f"f\t3\t1700000000.0\t{REMOTE}/b.txt",
        f"l\t7\t1700000000.0\t{REMOTE}/link",
    ]


def _export(capsys: pytest.CaptureFixture[str], root: Path) -> list[str]:
    code, out, err = _run(capsys, ["export", "--root", str(root), "--format", "tsv"])
    assert code == 0, err
    return out.splitlines()


def _progress_rows(err: str) -> list[int]:
    return [int(match) for match in re.findall(r"^progress: (\d+) rows, ", err, re.MULTILINE)]


# ---------------------------------------------------------------------------
# T2: ``sources add`` / ``list`` / ``remove`` (S2)
# ---------------------------------------------------------------------------


def test_sources_add_writes_a_private_config(
    capsys: pytest.CaptureFixture[str], mount: Path, isolated_xdg: Path
) -> None:
    code, out, err = _add(capsys, mount)

    assert (code, out, err) == (0, "Added source: tank\n", "")
    config_dir = isolated_xdg / "xdg-config" / "filegraph"
    config_file = config_dir / "sources.toml"
    assert stat.S_IMODE(config_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(config_file.stat().st_mode) == 0o600


def test_sources_add_existing_name_needs_force(
    capsys: pytest.CaptureFixture[str], mount: Path, isolated_xdg: Path
) -> None:
    assert _add(capsys, mount)[0] == 0
    config_file = isolated_xdg / "xdg-config" / "filegraph" / "sources.toml"
    before = config_file.read_bytes()

    code, out, err = _add(capsys, mount, "--host", "other.local")

    assert (code, out) == (1, "")
    assert err == "Error: source already exists: tank (use --force to replace)\n"
    assert config_file.read_bytes() == before


def test_sources_add_force_replaces(
    capsys: pytest.CaptureFixture[str], mount: Path
) -> None:
    assert _add(capsys, mount)[0] == 0

    code, out, err = _add(capsys, mount, "--force")

    assert (code, out, err) == (0, "Replaced source: tank\n", "")


@pytest.mark.parametrize(
    "flag, value, message",
    [
        ("--remote-path", "mnt/tank", "remote path must be absolute: mnt/tank"),
        ("--local-path", "mnt/tank", "local path must be absolute: mnt/tank"),
    ],
)
def test_sources_add_rejects_relative_paths(
    capsys: pytest.CaptureFixture[str],
    mount: Path,
    isolated_xdg: Path,
    flag: str,
    value: str,
    message: str,
) -> None:
    argv = [
        "sources",
        "add",
        "tank",
        "--host",
        "truenas.local",
        "--user",
        "backup",
        "--remote-path",
        REMOTE,
        "--local-path",
        str(mount),
    ]
    argv[argv.index(flag) + 1] = value

    code, out, err = _run(capsys, argv)

    assert (code, out) == (1, "")
    assert err == f"Error: {message}\n"
    assert not (isolated_xdg / "xdg-config" / "filegraph" / "sources.toml").exists()


def test_sources_add_rejects_a_local_path_without_a_parent(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, isolated_xdg: Path
) -> None:
    code, out, err = _add(capsys, tmp_path / "absent" / "mount")

    assert (code, out) == (1, "")
    assert err == f"Error: local path parent does not exist: {tmp_path / 'absent'}\n"
    assert not (isolated_xdg / "xdg-config" / "filegraph" / "sources.toml").exists()


def test_sources_list_prints_fields_and_no_key_material(
    capsys: pytest.CaptureFixture[str], mount: Path, tmp_path: Path
) -> None:
    key = tmp_path / "id_tank"
    key.write_text(SECRET, encoding="utf-8")
    assert _add(capsys, mount, "--port", "2222", "--key", str(key))[0] == 0

    code, out, err = _run(capsys, ["sources", "list"])

    assert (code, err) == (0, "")
    assert out.splitlines() == [
        "tank",
        "  host: truenas.local",
        "  user: backup",
        "  port: 2222",
        f"  key: {key}",
        f"  remote: {REMOTE}",
        f"  local: {mount}",
    ]
    assert "PRIVATE KEY" not in out
    assert "not-a-real-key" not in out


def test_sources_list_without_sources(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run(capsys, ["sources", "list"])

    assert (code, out, err) == (0, "No sources configured.\n", "")


def test_sources_remove_deletes_one_entry(
    capsys: pytest.CaptureFixture[str], mount: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    other.mkdir()
    assert _add(capsys, mount)[0] == 0
    assert _add(capsys, other, name="media", remote="/mnt/media")[0] == 0

    code, out, err = _run(capsys, ["sources", "remove", "tank"])

    assert (code, out, err) == (0, "Removed source: tank\n", "")
    assert _run(capsys, ["sources", "list"])[1].splitlines()[0] == "media"


def test_sources_remove_unknown_name(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run(capsys, ["sources", "remove", "tank"])

    assert (code, out) == (1, "")
    assert err == "Error: no such source: tank\n"


# ---------------------------------------------------------------------------
# T1: ``pull`` streams the listing and builds the ssh argument list (S1, S4)
# ---------------------------------------------------------------------------


def test_pull_rewrites_the_prefix_and_imports(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_ssh: Path,
    mount: Path,
    tmp_path: Path,
    isolated_xdg: Path,
) -> None:
    assert _add(capsys, mount)[0] == 0
    _use_listing(monkeypatch, _listing(tmp_path, _tree_rows(mount)))

    code, out, err = _run(capsys, ["pull", "--source", "tank"])

    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert re.fullmatch(
        rf"Pulled 5 entries from tank for {mount} in \d+\.\d{{3}}s", lines[0]
    ), lines[0]
    assert lines[1:] == [
        "entries: 5",
        "directories: 2",
        "files: 2",
        "symlinks: 1",
        "unknown: 0",
        "total bytes: 8",
        "skipped: 0 malformed row(s)",
        f"index: {tree_index.index_path(mount)}",
    ]
    assert _export(capsys, mount) == [
        f"d\t4096\t1700000000.0\t{mount}",
        f"f\t3\t1700000000.0\t{mount}/b.txt",
        f"l\t7\t1700000000.0\t{mount}/link",
        f"d\t4096\t1700000000.0\t{mount}/photos",
        f"f\t5\t1700000000.0\t{mount}/photos/a.txt",
    ]
    assert _argv_recorded(isolated_xdg) == [
        "backup@truenas.local",
        "find /mnt/tank -printf '%y\t%s\t%T@\t%p\n'",
    ]


def test_pull_verbose_prints_the_invoked_command_with_port_and_key(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_ssh: Path,
    mount: Path,
    tmp_path: Path,
    isolated_xdg: Path,
) -> None:
    assert _add(capsys, mount, "--port", "2222", "--key", "/home/backup/.ssh/id_tank")[0] == 0
    _use_listing(monkeypatch, _listing(tmp_path, _tree_rows(mount)))
    expected = [
        "ssh",
        "-p",
        "2222",
        "-i",
        "/home/backup/.ssh/id_tank",
        "backup@truenas.local",
        "find /mnt/tank -printf '%y\t%s\t%T@\t%p\n'",
    ]

    code, out, err = _run(capsys, ["pull", "--source", "tank", "--verbose"])

    assert code == 0
    assert err.splitlines()[0] == (
        "ssh command: " + shlex.join(expected).replace("\n", "\\n")
    )
    assert _argv_recorded(isolated_xdg) == expected[1:]
    assert "PRIVATE KEY" not in out + err


def test_pull_verbose_prints_the_command_without_port_or_key(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_ssh: Path,
    mount: Path,
    tmp_path: Path,
) -> None:
    assert _add(capsys, mount)[0] == 0
    _use_listing(monkeypatch, _listing(tmp_path, _tree_rows(mount)))
    expected = [
        "ssh",
        "backup@truenas.local",
        "find /mnt/tank -printf '%y\t%s\t%T@\t%p\n'",
    ]

    code, _out, err = _run(capsys, ["pull", "--source", "tank", "--verbose"])

    assert code == 0
    assert err.splitlines()[0] == (
        "ssh command: " + shlex.join(expected).replace("\n", "\\n")
    )


def test_pull_without_verbose_prints_no_command(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_ssh: Path,
    mount: Path,
    tmp_path: Path,
) -> None:
    assert _add(capsys, mount)[0] == 0
    _use_listing(monkeypatch, _listing(tmp_path, _tree_rows(mount)))

    code, out, err = _run(capsys, ["pull", "--source", "tank"])

    assert code == 0
    assert "ssh command:" not in err
    assert str(fake_ssh) not in out


def test_pull_unknown_source(capsys: pytest.CaptureFixture[str], fake_ssh: Path) -> None:
    code, out, err = _run(capsys, ["pull", "--source", "tank"])

    assert (code, out) == (1, "")
    assert err == "Error: no such source: tank\n"


# ---------------------------------------------------------------------------
# T3: strict prefix mapping and failed stream semantics (S3, S5)
# ---------------------------------------------------------------------------


def test_pull_rejects_the_first_row_outside_the_remote_path(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_ssh: Path,
    mount: Path,
    tmp_path: Path,
) -> None:
    assert _add(capsys, mount)[0] == 0
    _use_listing(monkeypatch, _listing(tmp_path, _tree_rows(mount)))
    assert _run(capsys, ["pull", "--source", "tank"])[0] == 0
    index_file = tree_index.index_path(mount)
    before = index_file.read_bytes()

    _use_listing(
        monkeypatch,
        _listing(
            tmp_path,
            [
                f"d\t4096\t1700000000.0\t{REMOTE}/photos",
                f"f\t5\t1700000000.0\t{REMOTE}/photos/a.txt",
                "f\t9\t1700000000.0\t/mnt/other/escaped.txt",
                "f\t9\t1700000000.0\t/etc/passwd",
            ],
            name="bad.tsv",
        ),
    )
    code, out, err = _run(capsys, ["pull", "--source", "tank"])

    assert (code, out) == (1, "")
    assert err == "Error: path outside remote path /mnt/tank: /mnt/other/escaped.txt\n"
    assert index_file.read_bytes() == before
    assert tree_index.index_status(mount)["entries"] == 5


def test_pull_sibling_prefix_is_not_inside_the_remote_path(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_ssh: Path,
    mount: Path,
    tmp_path: Path,
) -> None:
    assert _add(capsys, mount)[0] == 0
    _use_listing(
        monkeypatch, _listing(tmp_path, [f"f\t1\t1700000000.0\t/mnt/tank2/a.txt"])
    )

    code, out, err = _run(capsys, ["pull", "--source", "tank"])

    assert (code, out) == (1, "")
    assert err == "Error: path outside remote path /mnt/tank: /mnt/tank2/a.txt\n"
    assert not tree_index.index_path(mount).exists()


def test_pull_ssh_failure_keeps_the_previous_index(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_ssh: Path,
    mount: Path,
    tmp_path: Path,
) -> None:
    assert _add(capsys, mount)[0] == 0
    _use_listing(monkeypatch, _listing(tmp_path, _tree_rows(mount)))
    assert _run(capsys, ["pull", "--source", "tank"])[0] == 0
    index_file = tree_index.index_path(mount)
    before = index_file.read_bytes()

    monkeypatch.setenv("FAKE_SSH_MODE", "partial-fail")
    monkeypatch.setenv(
        "FAKE_SSH_ROWS",
        "".join(row + "\n" for row in (_tree_rows(mount)[:3])),
    )
    code, out, err = _run(capsys, ["pull", "--source", "tank"])

    assert (code, out) == (1, "")
    assert err == "Error: ssh exited with status 3 after 3 row(s)\n"
    assert index_file.read_bytes() == before
    assert tree_index.index_status(mount)["entries"] == 5


def test_pull_truncated_stream_keeps_the_previous_index(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_ssh: Path,
    mount: Path,
    tmp_path: Path,
) -> None:
    assert _add(capsys, mount)[0] == 0
    _use_listing(monkeypatch, _listing(tmp_path, _tree_rows(mount)))
    assert _run(capsys, ["pull", "--source", "tank"])[0] == 0
    index_file = tree_index.index_path(mount)
    before = index_file.read_bytes()

    monkeypatch.setenv("FAKE_SSH_MODE", "signal")
    monkeypatch.setenv(
        "FAKE_SSH_ROWS",
        "".join(row + "\n" for row in _tree_rows(mount)[:2]),
    )
    code, out, err = _run(capsys, ["pull", "--source", "tank"])

    assert (code, out) == (1, "")
    assert err == "Error: ssh terminated by signal 13 after 2 row(s)\n"
    assert index_file.read_bytes() == before


def test_pull_zero_rows_fails_and_writes_no_index(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_ssh: Path,
    mount: Path,
) -> None:
    assert _add(capsys, mount)[0] == 0
    monkeypatch.setenv("FAKE_SSH_MODE", "empty")

    code, out, err = _run(capsys, ["pull", "--source", "tank"])

    assert (code, out) == (1, "")
    assert err == "Error: ssh produced no rows (0 row(s) arrived)\n"
    assert not tree_index.index_path(mount).exists()


class _FakeStdout:
    """A stdout that hands out rows and then fails like a broken pipe."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.closed = False

    def __iter__(self) -> "_FakeStdout":
        return self

    def __next__(self) -> str:
        if self._lines:
            return self._lines.pop(0)
        raise OSError(32, "Broken pipe")

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    """The part of an ``ssh`` process the stream reader touches."""

    def __init__(self, lines: list[str]) -> None:
        self.stdout = _FakeStdout(lines)

    def poll(self) -> int:
        return 0

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def terminate(self) -> None:  # pragma: no cover - reaped already
        pass

    def kill(self) -> None:  # pragma: no cover - reaped already
        pass


def test_pull_reports_a_broken_pipe(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    mount: Path,
) -> None:
    assert _add(capsys, mount)[0] == 0
    rows = [row + "\n" for row in _tree_rows(mount)[:2]]
    monkeypatch.setattr(
        sources.subprocess, "Popen", lambda *args, **kwargs: _FakeProcess(rows)
    )

    code, out, err = _run(capsys, ["pull", "--source", "tank"])

    assert (code, out) == (1, "")
    assert err == "Error: ssh stream failed after 2 row(s): Broken pipe\n"
    assert not tree_index.index_path(mount).exists()


def test_pull_reports_a_missing_ssh_binary(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    mount: Path,
    tmp_path: Path,
) -> None:
    assert _add(capsys, mount)[0] == 0
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    code, out, err = _run(capsys, ["pull", "--source", "tank"])

    assert (code, out) == (1, "")
    assert err.startswith("Error: cannot run ssh: "), err


# ---------------------------------------------------------------------------
# T4: progress reporting and the final report (S6)
# ---------------------------------------------------------------------------


def test_pull_reports_progress_every_n_rows(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_ssh: Path,
    mount: Path,
    tmp_path: Path,
) -> None:
    assert _add(capsys, mount)[0] == 0
    _use_listing(monkeypatch, _listing(tmp_path, _tree_rows(mount)))

    code, _out, err = _run(
        capsys, ["pull", "--source", "tank", "--progress-every", "2"]
    )

    assert code == 0
    progress = err.splitlines()
    assert len(progress) == 2, progress
    assert re.fullmatch(
        r"progress: 2 rows, \d+ entries/s, \d+\.\d{3}s elapsed", progress[0]
    ), progress[0]
    assert progress[1].startswith("progress: 4 rows, ")


def test_pull_reports_no_progress_without_the_option(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_ssh: Path,
    mount: Path,
    tmp_path: Path,
) -> None:
    assert _add(capsys, mount)[0] == 0
    _use_listing(monkeypatch, _listing(tmp_path, _tree_rows(mount)))

    code, _out, err = _run(capsys, ["pull", "--source", "tank"])

    assert code == 0
    assert err == ""


def test_pull_streams_a_very_large_listing(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_ssh: Path,
    mount: Path,
) -> None:
    assert _add(capsys, mount)[0] == 0
    monkeypatch.setenv("FAKE_SSH_MODE", "generated")
    monkeypatch.setenv("FAKE_SSH_REMOTE", REMOTE)
    monkeypatch.setenv("FAKE_SSH_COUNT", "200000")

    tracemalloc.start()
    try:
        code, out, err = _run(
            capsys, ["pull", "--source", "tank", "--progress-every", "50000"]
        )
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert code == 0
    assert _progress_rows(err) == [50000, 100000, 150000, 200000]
    assert "entries: 200001" in out.splitlines()
    assert "files: 200000" in out.splitlines()
    status = tree_index.index_status(mount)
    assert status["entries"] == 200001
    assert status["directories"] == 1
    # A streamed import keeps a bounded amount of Python state: the 200k rows
    # (over 10 MB of text) are never materialized as one list.
    assert peak < 48 * 1024 * 1024


# ---------------------------------------------------------------------------
# T5: CLI only, and the ``--root`` override (S7)
# ---------------------------------------------------------------------------


def test_pull_root_overrides_the_configured_local_path(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    fake_ssh: Path,
    mount: Path,
    tmp_path: Path,
) -> None:
    other = tmp_path / "other-mount"
    other.mkdir()
    assert _add(capsys, mount)[0] == 0
    _use_listing(monkeypatch, _listing(tmp_path, _tree_rows(mount)))

    code, out, err = _run(capsys, ["pull", "--source", "tank", "--root", str(other)])

    assert (code, err) == (0, "")
    assert f"for {other} in " in out.splitlines()[0]
    assert tree_index.index_status(other)["entries"] == 5
    assert not tree_index.index_path(mount).exists()


def test_pull_root_must_be_absolute(
    capsys: pytest.CaptureFixture[str],
    fake_ssh: Path,
    mount: Path,
) -> None:
    assert _add(capsys, mount)[0] == 0

    code, out, err = _run(capsys, ["pull", "--source", "tank", "--root", "relative"])

    assert (code, out) == (1, "")
    assert err == "Error: --root must be absolute: relative\n"


def test_pull_is_absent_from_the_mcp_surface() -> None:
    from filegraph import main as mcp_main
    from filegraph import tools

    assert [name for name in dir(tools) if "pull" in name.lower()] == []
    server = inspect.getsource(mcp_main)
    assert "ssh" not in server.lower()
    assert "pull" not in server.lower()


# ---------------------------------------------------------------------------
# PRESERVE: the report ``import`` prints shared the printer this change touched
# ---------------------------------------------------------------------------


def test_import_report_output_is_unchanged(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    root = tmp_path / "indexed"
    root.mkdir()
    listing = _listing(
        tmp_path,
        [
            f"d\t4096\t1700000000.0\t{root}",
            f"f\t5\t1700000000.0\t{root}/a.txt",
        ],
    )

    code, out, err = _run(capsys, ["import", "--root", str(root), str(listing)])

    assert (code, err) == (0, "")
    lines = out.splitlines()
    assert re.fullmatch(
        rf"Imported 2 entries for {root} in \d+\.\d{{3}}s", lines[0]
    ), lines[0]
    assert lines[1:] == [
        "entries: 2",
        "directories: 1",
        "files: 1",
        "symlinks: 0",
        "unknown: 0",
        "total bytes: 5",
        "skipped: 0 malformed row(s)",
        f"index: {tree_index.index_path(root)}",
    ]
