"""Tests for the tree index: import a listing, export it, serve queries from it.

Covers S1-S9 of ``odd/tasks/tree-index-import-export.md``: the accepted TSV
shape, the import report, idempotent replacement and path validation, the
on-disk location, tsv/json export, the four tools served from the index with
the same contracts as a live scan, the "never touch the mount" guarantee, and
the index status report.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat as stat_module
from pathlib import Path

import pytest

from filegraph import index, scanner
from filegraph.cli import main
from filegraph.tools import (
    cache_status,
    find_duplicates,
    find_patterns,
    scan_directory,
    search_by_type,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def cache_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep every index this module creates inside the test's tmp_path."""
    home = tmp_path / "xdg-cache"
    home.mkdir()
    monkeypatch.setenv("XDG_CACHE_HOME", str(home))
    return home


def _build_tree(root: Path) -> None:
    """A small tree with files, nested dirs and one symlink."""
    (root / "a.txt").write_text("hello")  # 5 bytes
    (root / "zed.txt").write_text("x")  # 1 byte
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_text("hi")  # 2 bytes
    (root / "sub" / "deep").mkdir()
    (root / "sub" / "deep" / "c.txt").write_text("abc")  # 3 bytes
    os.symlink("a.txt", root / "link")


def _letter(path: Path) -> str:
    st = os.lstat(path)
    if stat_module.S_ISLNK(st.st_mode):
        return "l"
    if stat_module.S_ISDIR(st.st_mode):
        return "d"
    if stat_module.S_ISREG(st.st_mode):
        return "f"
    return "b"


def _find_listing(root: Path) -> str:
    """A ``find -printf '%y\\t%s\\t%T@\\t%p\\n'`` shaped listing of ``root``."""
    lines: list[str] = []

    def _row(path: Path) -> None:
        st = os.lstat(path)
        lines.append(f"{_letter(path)}\t{st.st_size}\t{st.st_mtime}\t{path}")

    _row(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in dirnames:
            _row(Path(dirpath) / name)
        for name in sorted(filenames):
            _row(Path(dirpath) / name)
    return "\n".join(lines) + "\n"


def _write_listing(tmp_path: Path, text: str, name: str = "listing.tsv") -> Path:
    listing = tmp_path / name
    listing.write_text(text, encoding="utf-8")
    return listing


def _import(root: Path, listing: Path) -> int:
    return main(["import", "--root", str(root), str(listing)])


def _export_tsv(root: Path, capsys: pytest.CaptureFixture[str], *extra: str) -> str:
    rc = main(["export", "--root", str(root), "--format", "tsv", *extra])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    assert captured.err == ""
    return captured.out


def _export_json(
    root: Path, capsys: pytest.CaptureFixture[str], *extra: str
) -> dict:
    rc = main(["export", "--root", str(root), "--format", "json", *extra])
    captured = capsys.readouterr()
    assert rc == 0, captured.err
    return json.loads(captured.out)


def _rows(tsv: str) -> dict[str, list[str]]:
    """Map path -> [letter, size, mtime] from exported tsv text."""
    rows: dict[str, list[str]] = {}
    for line in tsv.splitlines():
        fields = line.split("\t")
        assert len(fields) == 4, f"not a tsv row: {line!r}"
        rows[fields[3]] = fields[:3]
    return rows


def _strip_targets(node: dict) -> dict:
    """Drop symlink targets: the listing format has no target column."""
    stripped = {"name": node["name"], "type": node["type"]}
    if "children" in node:
        stripped["children"] = [_strip_targets(child) for child in node["children"]]
    return stripped


def _snapshot(root: Path) -> list[str]:
    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(dirnames):
            found.append(os.path.relpath(os.path.join(dirpath, name), root))
        for name in sorted(filenames):
            found.append(os.path.relpath(os.path.join(dirpath, name), root))
    return found


def _forbid_fs_access(
    monkeypatch: pytest.MonkeyPatch, root: Path
) -> list[str]:
    """Make any stat/lstat/scandir/walk on ``root`` raise and be recorded."""
    root_key = os.path.normpath(os.path.abspath(os.fspath(root)))
    touched: list[str] = []

    def _guard(real, name):
        def wrapper(path, *args, **kwargs):
            try:
                raw = os.fspath(path)
            except TypeError:
                raw = None
            if isinstance(raw, (str, bytes)):
                key = os.path.normpath(os.path.abspath(os.fsdecode(raw)))
                if key == root_key or key.startswith(root_key + os.sep):
                    touched.append(f"{name}: {os.fsdecode(raw)}")
                    raise AssertionError(
                        f"{name} touched the indexed root: {os.fsdecode(raw)}"
                    )
            return real(path, *args, **kwargs)

        return wrapper

    monkeypatch.setattr(os, "stat", _guard(os.stat, "stat"))
    monkeypatch.setattr(os, "lstat", _guard(os.lstat, "lstat"))
    monkeypatch.setattr(os, "scandir", _guard(os.scandir, "scandir"))
    monkeypatch.setattr(os, "walk", _guard(os.walk, "walk"))
    return touched


# ---------------------------------------------------------------------------
# S1 — import accepts the find(1) TSV shape
# ---------------------------------------------------------------------------


class TestImportStoresListing:
    def test_import_stores_tsv_rows_for_root(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        _build_tree(root)
        text = _find_listing(root) + f"b\t0\t1700000000.0\t{root}/dev\n"
        listing = _write_listing(tmp_path, text)

        rc = _import(root, listing)
        capsys.readouterr()

        assert rc == 0
        rows = _rows(_export_tsv(root, capsys))
        assert rows[str(root)][0] == "d"
        assert rows[str(root / "a.txt")] == ["f", "5", str(
            os.lstat(root / "a.txt").st_mtime
        )]
        assert rows[str(root / "sub")][0] == "d"
        assert rows[str(root / "link")][0] == "l"
        # Any other type letter is stored as `unknown` and re-exported as `u`.
        assert rows[str(root / "dev")][0] == "u"

    def test_import_reads_the_listing_from_stdin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        text = (
            f"d\t4096\t1700000000.0\t{root}\n"
            f"f\t5\t1700000000.5\t{root}/a.txt\n"
            f"l\t0\t1700000000.25\t{root}/link\n"
        )
        monkeypatch.setattr("sys.stdin", io.StringIO(text))

        rc = main(["import", "--root", str(root), "-"])

        capsys.readouterr()
        assert rc == 0
        assert _export_tsv(root, capsys) == text


# ---------------------------------------------------------------------------
# S2 — the import reports what it did; malformed rows never abort
# ---------------------------------------------------------------------------


class TestImportReport:
    def test_reports_counts_bytes_and_elapsed(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        _build_tree(root)
        listing = _write_listing(tmp_path, _find_listing(root))

        rc = _import(root, listing)
        out = capsys.readouterr().out

        assert rc == 0
        assert f"Imported 8 entries for {root} in " in out
        assert re.search(r"in \d+\.\d{3}s$", out.splitlines()[0])
        assert "entries: 8" in out
        assert "directories: 3" in out  # root, sub, sub/deep
        assert "files: 4" in out
        assert "symlinks: 1" in out
        assert "unknown: 0" in out
        assert "total bytes: 11" in out  # files only: 5 + 1 + 2 + 3
        assert "skipped: 0 malformed row(s)" in out
        assert f"index: {index.index_path(root)}" in out

    def test_malformed_rows_are_skipped_with_their_line_numbers(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        listing = _write_listing(
            tmp_path,
            "\n".join(
                [
                    f"f\t5\t1700000000.0\t{root}/ok1.txt",
                    f"f\tnot-a-size\t1700000000.0\t{root}/bad-size.txt",
                    f"f\t5\tnot-a-time\t{root}/bad-mtime.txt",
                    f"f\t5\t1700000000.0",
                    "f\t5\t1700000000.0\t",
                    "f\t5\t1700000000.0\trelative/path.txt",
                    f"f\t2\t1700000000.0\t{root}/ok2.txt",
                ]
            )
            + "\n",
        )

        rc = _import(root, listing)
        out = capsys.readouterr().out

        assert rc == 0
        assert "skipped: 5 malformed row(s) at lines 2, 3, 4, 5, 6" in out
        assert "Imported 2 entries" in out
        rows = _rows(_export_tsv(root, capsys))
        assert sorted(rows) == [f"{root}/ok1.txt", f"{root}/ok2.txt"]

    def test_reports_only_the_first_few_skipped_lines(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        bad = [f"f\tX\t1700000000.0\t{root}/bad{i}" for i in range(10)]
        listing = _write_listing(
            tmp_path,
            "\n".join(
                [f"f\t5\t1700000000.0\t{root}/ok.txt"] + bad
            )
            + "\n",
        )

        rc = _import(root, listing)
        out = capsys.readouterr().out

        assert rc == 0
        assert "skipped: 10 malformed row(s) at lines 2, 3, 4, 5, 6, ..." in out
        assert sorted(_rows(_export_tsv(root, capsys))) == [f"{root}/ok.txt"]

    def test_zero_usable_rows_is_an_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        listing = _write_listing(
            tmp_path, f"f\tX\tT\t{root}/a\nf\tX\tT\t{root}/b\nf\tX\tT\t{root}/c\n"
        )

        rc = _import(root, listing)
        captured = capsys.readouterr()

        assert rc == 1
        assert captured.out == ""
        assert captured.err.startswith("Error: no usable entries")
        assert "skipped 3" in captured.err
        assert not index.index_path(root).exists()


# ---------------------------------------------------------------------------
# S3 — idempotent replacement, root validation, atomic visibility
# ---------------------------------------------------------------------------


class TestImportReplacesAndValidates:
    def test_reimport_replaces_the_stored_index(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        first = _write_listing(
            tmp_path,
            f"d\t4096\t1700000000.0\t{root}\nf\t5\t1700000000.0\t{root}/a.txt\n",
            name="first.tsv",
        )
        second = _write_listing(
            tmp_path,
            f"d\t4096\t1700000001.0\t{root}\nf\t7\t1700000001.0\t{root}/b.txt\n",
            name="second.tsv",
        )

        assert _import(root, first) == 0
        capsys.readouterr()
        assert _import(root, second) == 0
        out = capsys.readouterr().out

        assert "Imported 2 entries" in out
        rows = _rows(_export_tsv(root, capsys))
        assert sorted(rows) == [str(root), f"{root}/b.txt"]
        assert rows[f"{root}/b.txt"][1] == "7"

    def test_paths_outside_root_reject_the_whole_import(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        good = _write_listing(
            tmp_path,
            f"d\t4096\t1700000000.0\t{root}\nf\t5\t1700000000.0\t{root}/a.txt\n",
            name="good.tsv",
        )
        bad = _write_listing(
            tmp_path,
            f"d\t4096\t1700000000.0\t{root}\n"
            f"f\t5\t1700000000.0\t{root}/a.txt\n"
            f"f\t5\t1700000000.0\t/elsewhere/outside.txt\n",
            name="bad.tsv",
        )
        assert _import(root, good) == 0
        capsys.readouterr()

        rc = _import(root, bad)
        captured = capsys.readouterr()

        assert rc == 1
        assert "/elsewhere/outside.txt" in captured.err
        assert captured.err.startswith("Error: ")
        # The rejected import left the previous index intact.
        assert sorted(_rows(_export_tsv(root, capsys))) == [str(root), f"{root}/a.txt"]

    def test_import_does_not_leave_a_partial_index_behind(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        listing = _write_listing(
            tmp_path,
            f"d\t4096\t1700000000.0\t{root}\nf\t5\t1700000000.0\t{root}/a.txt\n",
        )

        assert _import(root, listing) == 0
        capsys.readouterr()

        assert sorted(path.name for path in index.index_path(root).parent.iterdir()) == [
            "index.db"
        ]

    def test_a_reader_never_sees_a_half_imported_tree(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        old = _write_listing(
            tmp_path,
            f"d\t4096\t1700000000.0\t{root}\n"
            f"f\t5\t1700000000.0\t{root}/a.txt\n"
            f"f\t6\t1700000000.0\t{root}/b.txt\n",
            name="old.tsv",
        )
        new = _write_listing(
            tmp_path,
            f"d\t4096\t1700000001.0\t{root}\nf\t7\t1700000001.0\t{root}/only.txt\n",
            name="new.tsv",
        )
        assert _import(root, old) == 0
        capsys.readouterr()
        before = _export_tsv(root, capsys)

        seen: list[list[str]] = []
        real_replace = os.replace

        def _watch(src, dst, *args, **kwargs):
            visible = index.export_tsv_lines(root)
            seen.append([] if visible is None else list(visible))
            result = real_replace(src, dst, *args, **kwargs)
            after = index.export_tsv_lines(root)
            seen.append([] if after is None else list(after))
            return result

        monkeypatch.setattr(os, "replace", _watch)
        assert _import(root, new) == 0
        monkeypatch.setattr(os, "replace", real_replace)
        capsys.readouterr()

        assert "".join(seen[0]) == before, "reader saw a half-imported tree"
        assert sorted(_rows("".join(seen[1]))) == [str(root), f"{root}/only.txt"]


# ---------------------------------------------------------------------------
# S4 — the index lives in the local cache, never inside the tree
# ---------------------------------------------------------------------------


class TestIndexLocation:
    def test_index_lives_in_the_local_cache_keyed_by_the_root(
        self, tmp_path: Path, cache_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "share"
        root.mkdir()
        _build_tree(root)
        listing = _write_listing(tmp_path, _find_listing(root))

        assert _import(root, listing) == 0
        capsys.readouterr()

        resolved = os.path.normpath(os.path.abspath(os.fspath(root)))
        digest = hashlib.sha256(resolved.encode("utf-8", "surrogateescape")).hexdigest()
        expected = cache_home / "filegraph" / digest / "index.db"

        assert index.index_path(root) == expected
        assert expected.is_file()
        assert str(expected).startswith(str(cache_home))

    def test_import_writes_nothing_inside_the_indexed_root(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "share"
        root.mkdir()
        _build_tree(root)
        listing = _write_listing(tmp_path, _find_listing(root))
        before = _snapshot(root)

        assert _import(root, listing) == 0
        capsys.readouterr()

        assert _snapshot(root) == before
        assert not (root / ".filegraph").exists()


# ---------------------------------------------------------------------------
# S5 — export round-trips tsv and returns the scan_directory tree as json
# ---------------------------------------------------------------------------


class TestExport:
    def test_export_tsv_round_trips_unchanged(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        _build_tree(root)
        original = _find_listing(root) + f"b\t0\t1700000000.0\t{root}/dev\n"
        first = _write_listing(tmp_path, original, name="original.tsv")
        assert _import(root, first) == 0
        capsys.readouterr()

        exported = _export_tsv(root, capsys)
        second = _write_listing(tmp_path, exported, name="exported.tsv")
        assert _import(root, second) == 0
        capsys.readouterr()

        assert _export_tsv(root, capsys) == exported
        rows = _rows(exported)
        assert rows[f"{root}/a.txt"][0] == "f"
        assert rows[f"{root}/sub"][0] == "d"
        assert rows[f"{root}/link"][0] == "l"
        assert rows[f"{root}/dev"][0] == "u"

    def test_export_json_is_the_same_tree_scan_directory_returns(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        _build_tree(root)
        listing = _write_listing(tmp_path, _find_listing(root))
        live = scanner.scan_tree(root)["tree"]
        assert _import(root, listing) == 0
        capsys.readouterr()

        tree = _export_json(root, capsys)

        assert tree == _strip_targets(live)

    def test_export_without_an_index_is_an_error(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()

        for fmt in ("json", "tsv"):
            rc = main(["export", "--root", str(root), "--format", fmt])
            captured = capsys.readouterr()
            assert rc == 1
            assert captured.out == ""
            assert captured.err.startswith("Error: no index for root:")
            assert str(root) in captured.err

    def test_export_honours_path_and_max_depth(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        _build_tree(root)
        listing = _write_listing(tmp_path, _find_listing(root))
        assert _import(root, listing) == 0
        capsys.readouterr()

        tree = _export_json(root, capsys, "--path", "sub", "--max-depth", "1")
        assert tree["name"] == "sub"
        assert tree["type"] == "directory"
        assert [child["name"] for child in tree["children"]] == ["b.txt", "deep"]
        assert tree["children"][1]["children"] == []

        flat = _export_json(root, capsys, "--path", str(root / "sub"), "--max-depth", "0")
        assert flat == {"name": "sub", "type": "directory", "children": []}

        rows = _rows(_export_tsv(root, capsys, "--path", "sub", "--max-depth", "1"))
        assert sorted(rows) == [f"{root}/sub", f"{root}/sub/b.txt", f"{root}/sub/deep"]

    def test_export_names_a_missing_subpath(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        listing = _write_listing(tmp_path, _find_listing(root))
        assert _import(root, listing) == 0
        capsys.readouterr()

        rc = main(
            ["export", "--root", str(root), "--format", "json", "--path", "nope"]
        )
        captured = capsys.readouterr()

        assert rc == 1
        assert captured.err.startswith("Error: path not in index: ")
        assert "nope" in captured.err


# ---------------------------------------------------------------------------
# S6/S7 — the tools serve an indexed root with the live scan's contracts
# ---------------------------------------------------------------------------


class TestServingFromIndex:
    def test_scan_directory_serves_an_indexed_root(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        _build_tree(root)
        live = scan_directory(str(root))
        assert live["source"] == "scan"
        assert live["truncated"] is None
        listing = _write_listing(tmp_path, _find_listing(root))
        assert _import(root, listing) == 0
        capsys.readouterr()

        served = scan_directory(str(root))

        assert served["source"] == "index"
        assert served["warnings"] == []
        assert served["truncated"] is None
        assert served["tree"] == _strip_targets(live["tree"])
        symlink = next(
            child for child in served["tree"]["children"] if child["name"] == "link"
        )
        assert symlink["type"] == "symlink"

    def test_index_honours_max_depth_and_max_entries_like_a_live_scan(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        _build_tree(root)
        listing = _write_listing(tmp_path, _find_listing(root))
        assert _import(root, listing) == 0
        capsys.readouterr()

        for max_depth, max_entries in ((1, None), (None, 2), (1, 2), (2, 4)):
            live = scanner.scan_tree(root, max_depth=max_depth, max_entries=max_entries)
            served = scan_directory(
                str(root), max_depth=max_depth, max_entries=max_entries
            )
            assert served["tree"] == _strip_targets(live["tree"])
            assert served["truncated"] == live["truncated"]

        served = scan_directory(str(root), max_entries=2)
        assert served["truncated"] == {
            "reason": "entry_budget",
            "limit": 2,
            "entries_seen": 2,
        }

    def test_index_applies_the_same_directory_exclusions_as_a_live_scan(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        (root / "keep.txt").write_text("k")
        (root / ".git").mkdir()
        (root / ".git" / "config").write_text("git")
        listing = _write_listing(tmp_path, _find_listing(root))
        live = scanner.scan_tree(root)["tree"]
        assert [child["name"] for child in live["children"]] == ["keep.txt"]
        assert _import(root, listing) == 0
        capsys.readouterr()

        served = scan_directory(str(root))
        assert served["tree"] == _strip_targets(live)

        included = scan_directory(str(root), include_excluded=True)
        assert [child["name"] for child in included["tree"]["children"]] == [
            ".git",
            "keep.txt",
        ]

    def test_search_by_type_serves_an_indexed_root(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        _build_tree(root)
        (root / ".git").mkdir()
        (root / ".git" / "hook.txt").write_text("hook")
        live = search_by_type(str(root), "txt")
        assert live["source"] == "scan"
        listing = _write_listing(tmp_path, _find_listing(root))
        assert _import(root, listing) == 0
        capsys.readouterr()

        served = search_by_type(str(root), "txt")

        assert served["source"] == "index"
        assert served["results"] == live["results"]
        assert served["truncated"] is None
        assert [item["name"] for item in served["results"]] == [
            "a.txt",
            "b.txt",
            "c.txt",
            "zed.txt",
        ]
        # The symlink is a link, not a file, and .git stays excluded.
        assert f"{root}/link" not in [item["path"] for item in served["results"]]

    def test_find_patterns_serves_an_indexed_root(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        for i in range(4):
            (root / f"file_{i:03d}.txt").write_text(f"data {i}")
        live = find_patterns(str(root))
        assert live["source"] == "scan"
        assert live["patterns"]
        listing = _write_listing(tmp_path, _find_listing(root))
        assert _import(root, listing) == 0
        capsys.readouterr()

        served = find_patterns(str(root))

        assert served["source"] == "index"
        assert served["patterns"] == live["patterns"]
        assert served["truncated"] is None

    def test_find_duplicates_serves_size_groups_from_an_indexed_root(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        (root / "a.bin").write_bytes(b"abcd")
        (root / "b.bin").write_bytes(b"abcd")
        (root / "c.bin").write_bytes(b"wxyz")  # same size, different content
        (root / "unique.bin").write_bytes(b"zzzzzz")
        live = find_duplicates(str(root))
        assert live["source"] == "scan"
        assert len(live["duplicates"]) == 1
        assert len(live["duplicates"][0]["files"]) == 2  # only the real hash match
        listing = _write_listing(tmp_path, _find_listing(root))
        assert _import(root, listing) == 0
        capsys.readouterr()

        served = find_duplicates(str(root))

        assert served["source"] == "index"
        assert served["content_hashed"] is False
        assert served["duplicates"] == [
            {
                "hash": None,
                "size": 4,
                "files": [f"{root}/a.bin", f"{root}/b.bin", f"{root}/c.bin"],
            }
        ]
        assert any("not hashed" in warning for warning in served["warnings"])

    def test_an_index_is_a_snapshot_not_a_view_of_the_filesystem(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        (root / "live.txt").write_text("live")
        assert scan_directory(str(root))["source"] == "scan"
        listing = _write_listing(
            tmp_path,
            f"d\t4096\t1700000000.0\t{root}\nf\t5\t1700000000.0\t{root}/ghost.txt\n",
        )
        assert _import(root, listing) == 0
        capsys.readouterr()

        served = scan_directory(str(root))

        assert served["source"] == "index"
        assert [child["name"] for child in served["tree"]["children"]] == ["ghost.txt"]


# ---------------------------------------------------------------------------
# S8 — an indexed root is never stat-ed or walked
# ---------------------------------------------------------------------------


class TestIndexedRootNeverTouchesTheFilesystem:
    def test_indexed_queries_never_stat_or_list_the_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        _build_tree(root)
        listing = _write_listing(tmp_path, _find_listing(root))
        before = _snapshot(root)
        assert _import(root, listing) == 0
        capsys.readouterr()

        touched = _forbid_fs_access(monkeypatch, root)

        scanned = scan_directory(str(root))
        searched = search_by_type(str(root), "txt")
        patterns = find_patterns(str(root))
        duplicates = find_duplicates(str(root))
        tree = _export_json(root, capsys)
        rows = _rows(_export_tsv(root, capsys))
        rc = main(["status", "--root", str(root)])
        status_out = capsys.readouterr().out
        status = cache_status(str(root))

        assert touched == []
        assert scanned["source"] == "index"
        assert scanned["tree"]["name"] == "root"
        assert [item["name"] for item in searched["results"]] == [
            "a.txt",
            "b.txt",
            "c.txt",
            "zed.txt",
        ]
        assert patterns["source"] == "index"
        assert duplicates["source"] == "index"
        assert tree["name"] == "root"
        assert sorted(rows) == [
            str(root),
            f"{root}/a.txt",
            f"{root}/link",
            f"{root}/sub",
            f"{root}/sub/b.txt",
            f"{root}/sub/deep",
            f"{root}/sub/deep/c.txt",
            f"{root}/zed.txt",
        ]
        assert rc == 0
        assert "Indexed: yes" in status_out
        assert status["indexed"] is True

        # Serving never wrote anything into the indexed tree either.
        monkeypatch.undo()
        assert _snapshot(root) == before

    def test_a_root_that_does_not_exist_is_served_from_its_index(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        ghost = tmp_path / "ghost"
        text = (
            f"d\t4096\t1700000000.0\t{ghost}\n"
            f"f\t5\t1700000000.0\t{ghost}/a.txt\n"
            f"d\t4096\t1700000000.0\t{ghost}/sub\n"
            f"f\t2\t1700000000.0\t{ghost}/sub/b.txt\n"
        )
        listing = _write_listing(tmp_path, text)
        assert _import(ghost, listing) == 0
        capsys.readouterr()
        assert not ghost.exists()

        touched = _forbid_fs_access(monkeypatch, ghost)
        result = scan_directory(str(ghost))
        searched = search_by_type(str(ghost), "txt")

        assert touched == []
        assert "error" not in result
        assert result["source"] == "index"
        assert result["tree"] == {
            "name": "ghost",
            "type": "directory",
            "children": [
                {"name": "a.txt", "type": "file"},
                {
                    "name": "sub",
                    "type": "directory",
                    "children": [{"name": "b.txt", "type": "file"}],
                },
            ],
        }
        assert [item["path"] for item in searched["results"]] == [
            f"{ghost}/a.txt",
            f"{ghost}/sub/b.txt",
        ]

    def test_a_corrupt_index_is_reported_instead_of_walking_the_mount(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        (root / "a.txt").write_text("hello")
        index.index_path(root).parent.mkdir(parents=True, exist_ok=True)
        index.index_path(root).write_bytes(b"this is not a database")

        touched = _forbid_fs_access(monkeypatch, root)
        result = scan_directory(str(root))

        assert touched == []
        assert "error" in result
        assert str(index.index_path(root)) in result["error"]


# ---------------------------------------------------------------------------
# S9 — index status reporting
# ---------------------------------------------------------------------------


class TestIndexStatus:
    def test_status_reports_the_index_details(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        _build_tree(root)
        listing = _write_listing(tmp_path, _find_listing(root))
        assert _import(root, listing) == 0
        capsys.readouterr()

        rc = main(["status", "--root", str(root)])
        out = capsys.readouterr().out

        assert rc == 0
        assert "Indexed: yes" in out
        assert f"root: {root}" in out
        assert re.search(r"imported: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", out)
        assert "entries: 8" in out
        assert "directories: 3" in out
        assert "files: 4" in out
        assert "symlinks: 1" in out
        assert "unknown: 0" in out
        assert "total bytes: 11" in out
        assert f"index: {index.index_path(root)}" in out

    def test_status_reports_a_root_without_an_index(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()

        rc = main(["status", "--root", str(root)])
        out = capsys.readouterr().out

        assert rc == 0
        assert "Indexed: no" in out
        assert f"root: {root}" in out
        assert f"index: {index.index_path(root)}" in out
        assert "imported:" not in out

    def test_cache_status_tool_reports_the_index(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = tmp_path / "root"
        root.mkdir()
        _build_tree(root)
        listing = _write_listing(tmp_path, _find_listing(root))
        assert _import(root, listing) == 0
        capsys.readouterr()

        status = cache_status(str(root))

        assert status["indexed"] is True
        assert status["root"] == str(root)
        assert status["entries"] == 8
        assert status["directories"] == 3
        assert status["files"] == 4
        assert status["symlinks"] == 1
        assert status["unknown"] == 0
        assert status["total_bytes"] == 11
        assert status["index_path"] == str(index.index_path(root))
        assert status["imported_at"].startswith("20")

        other = tmp_path / "other"
        other.mkdir()
        assert cache_status(str(other))["indexed"] is False
