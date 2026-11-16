"""Unit tests for SQLite cache store."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from filegraph import fsscan, store
from filegraph.cache import make_cache_key
from filegraph.store import (
    cache_stats,
    get_cache,
    get_file_set,
    init_db,
    invalidate_path,
    search_fts,
    set_cache,
)
from filegraph.tools import scan_directory


# ---------------------------------------------------------------------------
# Schema recovery tests
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# init_db tests
# ---------------------------------------------------------------------------


class TestInitDb:
    def test_creates_db_file(self, tmp_path: Path) -> None:
        """init_db creates .filegraph/cache.db with correct tables."""
        init_db(tmp_path)
        db_path = tmp_path / ".filegraph" / "cache.db"
        assert db_path.exists()

    def test_enables_wal_mode(self, tmp_path: Path) -> None:
        """init_db enables WAL journal mode."""
        init_db(tmp_path)
        db_path = tmp_path / ".filegraph" / "cache.db"
        conn = sqlite3.connect(str(db_path))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal"

    def test_creates_cache_table(self, tmp_path: Path) -> None:
        """init_db creates the cache table with expected columns."""
        init_db(tmp_path)
        db_path = tmp_path / ".filegraph" / "cache.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        row = conn.execute("PRAGMA table_info(cache)").fetchone()
        conn.close()
        assert row is not None

    def test_creates_cache_files_table(self, tmp_path: Path) -> None:
        """init_db creates the cache_files table."""
        init_db(tmp_path)
        db_path = tmp_path / ".filegraph" / "cache.db"
        conn = sqlite3.connect(str(db_path))
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        conn.close()
        assert "cache" in tables
        assert "cache_files" in tables

    def test_idempotent(self, tmp_path: Path) -> None:
        """Calling init_db twice does not raise or duplicate tables."""
        init_db(tmp_path)
        init_db(tmp_path)  # Should not raise


# ---------------------------------------------------------------------------
# set_cache / get_cache round-trip tests
# ---------------------------------------------------------------------------


class TestCacheRoundTrip:
    def test_set_then_get(self, tmp_path: Path) -> None:
        """set_cache stores data that get_cache retrieves correctly."""
        init_db(tmp_path)
        key = "abc123"
        result = json.dumps({"tree": {"name": "root", "type": "directory"}})
        file_set = [("/a/file.txt", 100.0), ("/a/other.txt", 200.0)]

        set_cache(tmp_path, key, "scan_directory", "/a", result, file_set)
        entry = get_cache(tmp_path, key)

        assert entry is not None
        assert entry["tool_name"] == "scan_directory"
        assert entry["path"] == "/a"
        assert json.loads(entry["result_json"]) == {"tree": {"name": "root", "type": "directory"}}
        assert entry["max_mtime"] == 200.0

    def test_get_nonexistent_returns_none(self, tmp_path: Path) -> None:
        """get_cache returns None for missing key."""
        init_db(tmp_path)
        assert get_cache(tmp_path, "nope") is None

    def test_upsert_overwrites(self, tmp_path: Path) -> None:
        """set_cache with existing key overwrites the old value."""
        init_db(tmp_path)
        key = "k1"
        file_set = [("/a.txt", 1.0)]

        set_cache(tmp_path, key, "tool1", "/a", json.dumps({"v": 1}), file_set)
        set_cache(tmp_path, key, "tool2", "/b", json.dumps({"v": 2}), file_set)

        entry = get_cache(tmp_path, key)
        assert entry["tool_name"] == "tool2"
        assert entry["path"] == "/b"
        assert json.loads(entry["result_json"]) == {"v": 2}

    def test_multiple_keys(self, tmp_path: Path) -> None:
        """Multiple cache keys coexist independently."""
        init_db(tmp_path)
        file_set = [("/x.txt", 1.0)]

        set_cache(tmp_path, "k1", "t1", "/a", json.dumps({"a": 1}), file_set)
        set_cache(tmp_path, "k2", "t2", "/b", json.dumps({"b": 2}), file_set)

        assert get_cache(tmp_path, "k1")["tool_name"] == "t1"
        assert get_cache(tmp_path, "k2")["tool_name"] == "t2"


# ---------------------------------------------------------------------------
# get_file_set tests
# ---------------------------------------------------------------------------


class TestGetFileSet:
    def test_returns_recorded_files(self, tmp_path: Path) -> None:
        """get_file_set returns the file manifest for a cache entry."""
        init_db(tmp_path)
        key = "k1"
        file_set = [("/a.txt", 100.0), ("/b.txt", 200.0)]
        set_cache(tmp_path, key, "t", "/a", json.dumps({}), file_set)

        files = get_file_set(tmp_path, key)
        assert len(files) == 2
        paths = {f["file_path"] for f in files}
        assert paths == {"/a.txt", "/b.txt"}

    def test_empty_for_nonexistent_key(self, tmp_path: Path) -> None:
        """get_file_set returns empty list for missing key."""
        init_db(tmp_path)
        assert get_file_set(tmp_path, "nope") == []


# ---------------------------------------------------------------------------
# invalidate_path tests
# ---------------------------------------------------------------------------


class TestInvalidatePath:
    def test_removes_matching_path(self, tmp_path: Path) -> None:
        """invalidate_path deletes entries for the given path."""
        init_db(tmp_path)
        file_set = [("/a.txt", 1.0)]

        set_cache(tmp_path, "k1", "t", "/a", json.dumps({}), file_set)
        set_cache(tmp_path, "k2", "t", "/b", json.dumps({}), file_set)

        deleted = invalidate_path(tmp_path, "/a")
        assert deleted == 1
        assert get_cache(tmp_path, "k1") is None
        assert get_cache(tmp_path, "k2") is not None

    def test_removes_prefix_matches(self, tmp_path: Path) -> None:
        """invalidate_path removes entries with path prefix."""
        init_db(tmp_path)
        file_set = [("/a.txt", 1.0)]

        set_cache(tmp_path, "k1", "t", "/project/src", json.dumps({}), file_set)
        set_cache(tmp_path, "k2", "t", "/project/tests", json.dumps({}), file_set)
        set_cache(tmp_path, "k3", "t", "/other", json.dumps({}), file_set)

        deleted = invalidate_path(tmp_path, "/project")
        assert deleted == 2
        assert get_cache(tmp_path, "k1") is None
        assert get_cache(tmp_path, "k2") is None
        assert get_cache(tmp_path, "k3") is not None

    def test_returns_zero_when_no_match(self, tmp_path: Path) -> None:
        """invalidate_path returns 0 when no entries match."""
        init_db(tmp_path)
        assert invalidate_path(tmp_path, "/nothing") == 0

    def test_cascade_deletes_cache_files(self, tmp_path: Path) -> None:
        """invalidate_path also removes associated cache_files rows."""
        init_db(tmp_path)
        key = "k1"
        file_set = [("/a.txt", 1.0), ("/b.txt", 2.0)]
        set_cache(tmp_path, key, "t", "/a", json.dumps({}), file_set)

        assert len(get_file_set(tmp_path, key)) == 2
        invalidate_path(tmp_path, "/a")
        assert len(get_file_set(tmp_path, key)) == 0


# ---------------------------------------------------------------------------
# search_fts tests
# ---------------------------------------------------------------------------


class TestSearchFts:
    def test_returns_matching_filenames(self, tmp_path: Path) -> None:
        """search_fts returns file paths matching the query substring."""
        init_db(tmp_path)
        file_set = [
            ("/photos/landscape.jpg", 1.0),
            ("/photos/portrait.jpg", 2.0),
            ("/docs/readme.txt", 3.0),
        ]
        set_cache(tmp_path, "k1", "t", "/photos", json.dumps({}), file_set)

        # FTS5 tokenizes by words; "landscape" is a complete token
        results = search_fts(tmp_path, "landscape")
        assert len(results) == 1
        assert results[0]["file_path"] == "/photos/landscape.jpg"

    def test_no_matches_returns_empty(self, tmp_path: Path) -> None:
        """search_fts returns empty list when no files match."""
        init_db(tmp_path)
        file_set = [("/a.txt", 1.0)]
        set_cache(tmp_path, "k1", "t", "/a", json.dumps({}), file_set)

        results = search_fts(tmp_path, "nonexistent")
        assert results == []


# ---------------------------------------------------------------------------
# cache_stats tests
# ---------------------------------------------------------------------------


class TestCacheStats:
    def test_empty_cache(self, tmp_path: Path) -> None:
        """cache_stats returns zeros for empty cache."""
        init_db(tmp_path)
        stats = cache_stats(tmp_path)
        assert stats["entries"] == 0
        assert stats["total_files"] == 0
        assert stats["size_bytes"] > 0  # DB file exists

    def test_after_inserts(self, tmp_path: Path) -> None:
        """cache_stats reflects correct counts after inserts."""
        init_db(tmp_path)
        set_cache(tmp_path, "k1", "t", "/a", json.dumps({}), [("/a.txt", 1.0)])
        set_cache(tmp_path, "k2", "t", "/b", json.dumps({}), [("/b.txt", 1.0), ("/c.txt", 2.0)])

        stats = cache_stats(tmp_path)
        assert stats["entries"] == 2
        assert stats["total_files"] == 3


# ---------------------------------------------------------------------------
# Cache location: outside the share for network roots (S9), unchanged locally (S10)
# ---------------------------------------------------------------------------


def _fake_mountinfo(base: Path, mount_point: Path, fstype: str) -> Path:
    """Write a mountinfo file that maps ``mount_point`` to ``fstype``."""
    info = base / "mountinfo"
    info.write_text(
        f"36 35 0:31 / {Path(mount_point).resolve()} rw,relatime shared:1 - "
        f"{fstype} //server/share rw\n",
        encoding="utf-8",
    )
    return info


def _expected_local_db(cache_root: Path, scanned: Path) -> Path:
    """The database path a network root must resolve to."""
    resolved = os.path.realpath(os.fspath(scanned))
    digest = hashlib.sha256(resolved.encode()).hexdigest()
    return cache_root / "filegraph" / digest / "cache.db"


def _share_files(root: Path) -> list[str]:
    """Every path under ``root``, so "nothing was written" is checkable."""
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


class TestNetworkRootCacheLocation:
    """S9: a network root is cached locally, never inside the scanned tree."""

    def test_network_root_database_is_local_and_nothing_lands_in_the_share(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        share = tmp_path / "share"
        share.mkdir()
        monkeypatch.setattr(
            fsscan, "MOUNTINFO_PATH", _fake_mountinfo(tmp_path, share, "cifs")
        )
        xdg = tmp_path / "xdg-cache"
        monkeypatch.setenv("XDG_CACHE_HOME", str(xdg))
        (share / "a.txt").write_text("hello")
        before = _share_files(share)

        result = scan_directory(str(share))

        assert result["tree"]["name"] == "share"
        expected_db = _expected_local_db(xdg, share)
        assert store._get_db_path(share) == expected_db
        assert expected_db.exists()
        assert cache_stats(share)["entries"] == 1
        # Nothing at all was added to the share: no .filegraph/, no db, no WAL.
        assert _share_files(share) == before
        assert not (share / ".filegraph").exists()
        assert [p for p in share.rglob("*") if p.suffix in {".db", ".db-wal", ".db-shm"}] == []

    def test_falls_back_to_home_cache_when_xdg_cache_home_is_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        share = tmp_path / "share"
        share.mkdir()
        monkeypatch.setattr(
            fsscan, "MOUNTINFO_PATH", _fake_mountinfo(tmp_path, share, "nfs")
        )
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.setenv("HOME", str(home))

        init_db(share)
        set_cache(share, "k1", "t", str(share), json.dumps({}), [])

        expected_db = _expected_local_db(home / ".cache", share)
        assert store._get_db_path(share) == expected_db
        assert expected_db.exists()
        assert get_cache(share, "k1") is not None
        assert _share_files(share) == []


class TestLocalRootCacheLocation:
    """S10: a local root keeps today's location, schema, keys and freshness."""

    def test_local_root_keeps_the_in_tree_database_and_keys(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        xdg = tmp_path / "xdg-cache"
        monkeypatch.setenv("XDG_CACHE_HOME", str(xdg))
        root = tmp_path / "project"
        root.mkdir()
        (root / "a.txt").write_text("hello")

        assert store._get_db_path(root) == root / ".filegraph" / "cache.db"

        result = scan_directory(str(root))

        assert result["tree"]["name"] == "project"
        assert (root / ".filegraph" / "cache.db").exists()
        assert not xdg.exists()
        # The key is unchanged: an unbudgeted local scan adds no key options.
        entry = get_cache(root, make_cache_key("scan_directory", str(root)))
        assert entry is not None
        assert entry["path"] == str(root)


class TestSchemaRecovery:
    """A cache.db that exists without its schema must be repaired, not fatal."""

    def test_cache_stats_recovers_from_empty_db(self, tmp_path: Path) -> None:
        """An empty cache.db is repaired instead of raising no such table."""
        (tmp_path / ".filegraph").mkdir()
        (tmp_path / ".filegraph" / "cache.db").write_bytes(b"")

        stats = cache_stats(tmp_path)

        assert stats["entries"] == 0
        assert stats["total_files"] == 0

    def test_recovers_from_db_without_cache_table(self, tmp_path: Path) -> None:
        """A cache.db holding an unrelated table is repaired too."""
        (tmp_path / ".filegraph").mkdir()
        conn = sqlite3.connect(str(tmp_path / ".filegraph" / "cache.db"))
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
        conn.commit()
        conn.close()

        set_cache(tmp_path, "k1", "t", "/a", json.dumps({}), [("/a.txt", 1.0)])
        stats = cache_stats(tmp_path)

        assert stats["entries"] == 1
        assert stats["total_files"] == 1
