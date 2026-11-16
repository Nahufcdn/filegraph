"""Tests for network-aware scan budgets (S1-S4) and honest filesystem errors (S5-S6)."""

from __future__ import annotations

import errno
import os
import time
from pathlib import Path

import pytest

from filegraph import fsscan, scanner, scanbudget
from filegraph.scanbudget import ENTRY_BUDGET, TIME_BUDGET, resolve_budget


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_mountinfo(path: Path, mount_point: Path, fstype: str) -> Path:
    """Write a mountinfo file that maps mount_point to fstype."""
    path.write_text(
        f"36 35 0:31 / {Path(mount_point).resolve()} rw,relatime shared:1 - "
        f"{fstype} //server/share rw\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def network_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A directory the detector reports as a CIFS mount."""
    root = tmp_path / "share"
    root.mkdir()
    info = _write_mountinfo(tmp_path / "net-mountinfo", root, "cifs")
    monkeypatch.setattr(fsscan, "MOUNTINFO_PATH", info)
    return root


@pytest.fixture
def local_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A directory the detector reports as a local ext4 mount."""
    root = tmp_path / "local"
    root.mkdir()
    info = _write_mountinfo(tmp_path / "local-mountinfo", root, "ext4")
    monkeypatch.setattr(fsscan, "MOUNTINFO_PATH", info)
    return root


def _block_listing(monkeypatch: pytest.MonkeyPatch, blocked: Path, seconds: float = 30.0) -> None:
    """Make one directory read block, like a syscall stuck in the kernel.

    A cooperative Python-side check cannot interrupt this; only a killable
    process can return from it before ``seconds`` elapse.
    """
    real = scanner._list_dir

    def _slow_list(path: Path) -> list[Path]:
        if Path(path) == blocked:
            time.sleep(seconds)
        return real(path)

    monkeypatch.setattr(scanner, "_list_dir", _slow_list)


def _child_names(node: dict) -> set[str]:
    return {child["name"] for child in node.get("children", [])}


def _scan_with(root: Path, **options) -> dict:
    """Scan with explicit budgets supplied by the caller."""
    from filegraph.tools import scan_directory

    return scan_directory(str(root), **options)


def _scan_tree_with(root: Path, **options) -> dict:
    """Scan the core tree function, bypassing the cache."""
    from filegraph.scanner import scan_tree

    return scan_tree(root, **options)


def _tool(tool: str, root: Path, **options):
    """Call one scanning tool with defaults resolved by the tool itself."""
    from filegraph.tools import find_duplicates, find_patterns, scan_directory, search_by_type

    if tool == "search":
        return search_by_type(str(root), "txt", **options)
    if tool == "duplicates":
        return find_duplicates(str(root), **options)
    if tool == "patterns":
        return find_patterns(str(root), **options)
    return scan_directory(str(root), **options)


# ---------------------------------------------------------------------------
# S1 — network roots never run unbounded
# ---------------------------------------------------------------------------


class TestNetworkDefaults:
    def test_network_defaults_are_applied_without_caller_input(self, network_root: Path) -> None:
        assert scanbudget.NETWORK_DEFAULT_MAX_SECONDS == 45.0
        assert scanbudget.NETWORK_DEFAULT_MAX_ENTRIES == 200_000
        assert resolve_budget(network_root) == (45.0, 200_000)

    def test_default_entry_cap_stops_the_scan(
        self, network_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for i in range(5):
            (network_root / f"f{i}.txt").write_text("x")
        complete = _scan_with(network_root, max_entries=100)
        assert complete["truncated"] is None

        monkeypatch.setattr(scanbudget, "NETWORK_DEFAULT_MAX_ENTRIES", 2)

        result = _tool("scan", network_root)

        assert result["truncated"] == {
            "reason": ENTRY_BUDGET,
            "limit": 2,
            "entries_seen": 2,
        }
        # Partial, not a complete tree returned as if it were complete
        assert 0 < len(result["tree"]["children"]) < len(complete["tree"]["children"])

    def test_default_time_budget_stops_the_scan(
        self, network_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(scanbudget, "NETWORK_DEFAULT_MAX_SECONDS", 0.3)
        (network_root / "a.txt").write_text("x")
        blocked = network_root / "zz_blocked"
        blocked.mkdir()
        _block_listing(monkeypatch, blocked)

        started = time.monotonic()
        result = _tool("scan", network_root)
        elapsed = time.monotonic() - started

        assert elapsed < 5.0
        truncated = result["truncated"]
        assert truncated["reason"] == TIME_BUDGET
        assert truncated["limit"] == 0.3
        assert truncated["entries_seen"] >= 1
        assert "a.txt" in _child_names(result["tree"])


# ---------------------------------------------------------------------------
# S2 — local roots keep today's behaviour
# ---------------------------------------------------------------------------


class TestLocalRootUnchanged:
    def test_local_root_gets_no_default_budget(self, local_root: Path) -> None:
        assert resolve_budget(local_root) == (None, None)

    def test_local_scan_is_complete_and_unbudgeted(self, local_root: Path) -> None:
        deep = local_root / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "deep.txt").write_text("x")
        for i in range(20):
            (local_root / f"f{i:02d}.txt").write_text("y")

        result = _tool("scan", local_root)

        assert result["truncated"] is None
        assert result["warnings"] == []

        # max_depth still defaults to None: the deepest file is reached
        node = result["tree"]
        for name in ("a", "b", "c"):
            node = next(child for child in node["children"] if child["name"] == name)
        assert [child["name"] for child in node["children"]] == ["deep.txt"]


# ---------------------------------------------------------------------------
# S3 — a stopped scan returns partial results and says so
# ---------------------------------------------------------------------------


class TestTruncationMarker:
    def test_entry_budget_marker(self, local_root: Path) -> None:
        from filegraph.scanner import scan_tree

        for i in range(4):
            (local_root / f"f{i}.txt").write_text("x")

        complete = scan_tree(local_root)
        assert complete["truncated"] is None
        assert len(complete["tree"]["children"]) == 4

        result = _scan_tree_with(local_root, max_entries=2)

        assert result["truncated"] == {
            "reason": ENTRY_BUDGET,
            "limit": 2,
            "entries_seen": 2,
        }
        assert [child["name"] for child in result["tree"]["children"]] == ["f0.txt", "f1.txt"]

    def test_time_budget_marker(self, local_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        (local_root / "a.txt").write_text("x")
        blocked = local_root / "zz_blocked"
        blocked.mkdir()
        _block_listing(monkeypatch, blocked, seconds=10)

        result = _scan_with(local_root, max_seconds=0.4)

        truncated = result["truncated"]
        assert set(truncated) == {"reason", "limit", "entries_seen"}
        assert truncated["reason"] == TIME_BUDGET
        assert truncated["limit"] == 0.4
        assert isinstance(truncated["entries_seen"], int)
        assert "a.txt" in _child_names(result["tree"])


# ---------------------------------------------------------------------------
# S4 — the deadline survives a syscall blocked in the kernel
# ---------------------------------------------------------------------------


class TestHardDeadline:
    def test_tool_returns_while_a_directory_read_blocks(
        self, local_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (local_root / "readable.txt").write_text("x")
        blocked = local_root / "zz_blocked"
        blocked.mkdir()
        # 30 s inside one call: far past the 0.5 s deadline below.
        _block_listing(monkeypatch, blocked, seconds=30)

        started = time.monotonic()
        result = _scan_with(local_root, max_seconds=0.5)
        elapsed = time.monotonic() - started

        assert result["truncated"]["reason"] == TIME_BUDGET
        assert elapsed < 5.0, f"tool blocked for {elapsed:.1f}s past its 0.5s deadline"
        assert "readable.txt" in _child_names(result["tree"])

    @pytest.mark.parametrize(
        ("tool", "key"),
        [("search", "results"), ("duplicates", "duplicates"), ("patterns", "patterns")],
    )
    def test_walk_tools_return_at_the_deadline(
        self, local_root: Path, monkeypatch: pytest.MonkeyPatch, tool: str, key: str
    ) -> None:
        """The os.walk-based tools are bounded by the same deadline."""
        (local_root / "docs").mkdir()
        (local_root / "docs" / "a.txt").write_text("x")
        blocked = local_root / "zz_blocked"
        blocked.mkdir()
        real_scandir = os.scandir

        def _slow(path="."):
            if Path(path) == blocked:
                time.sleep(30)
            return real_scandir(path)

        monkeypatch.setattr(os, "scandir", _slow)

        started = time.monotonic()
        result = _tool(tool, local_root, max_seconds=0.4)
        elapsed = time.monotonic() - started

        assert result["truncated"]["reason"] == TIME_BUDGET
        assert result["truncated"]["limit"] == 0.4
        assert elapsed < 5.0, f"{tool} blocked for {elapsed:.1f}s past its 0.4s deadline"
        assert key in result


# ---------------------------------------------------------------------------
# S5 — filesystem errors are never reported as absence
# ---------------------------------------------------------------------------


class TestFilesystemErrors:
    @pytest.mark.parametrize(
        "code", [errno.EACCES, errno.ESTALE, errno.ENOTCONN, errno.EIO]
    )
    def test_directory_error_carries_path_and_errno(
        self, local_root: Path, monkeypatch: pytest.MonkeyPatch, code: int
    ) -> None:
        def _fail(path, *args, **kwargs):
            raise OSError(code, os.strerror(code))

        monkeypatch.setattr(fsscan, "stat_path", _fail)

        result = _tool("scan", local_root)

        assert result["error"] == f"{os.strerror(code)} (errno {code}): {local_root}"
        assert result["tree"] is None
        assert result["warnings"] == []

    def test_file_metadata_error_carries_path_and_errno(
        self, local_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from filegraph.tools import get_file_metadata

        target = local_root / "a.txt"
        target.write_text("x")

        def _fail(path, *args, **kwargs):
            raise OSError(errno.ESTALE, os.strerror(errno.ESTALE))

        monkeypatch.setattr(fsscan, "stat_path", _fail)

        result = get_file_metadata(str(target))

        assert result["error"] == (
            f"{os.strerror(errno.ESTALE)} (errno {errno.ESTALE}): {target}"
        )


# ---------------------------------------------------------------------------
# S6 — os.walk-based tools record skipped directories
# ---------------------------------------------------------------------------


class TestWalkToolErrors:
    @pytest.mark.parametrize(
        ("tool", "key"),
        [("search", "results"), ("duplicates", "duplicates"), ("patterns", "patterns")],
    )
    def test_skipped_directory_is_reported_in_warnings(
        self, local_root: Path, monkeypatch: pytest.MonkeyPatch, tool: str, key: str
    ) -> None:
        (local_root / "docs").mkdir()
        (local_root / "docs" / "a.txt").write_text("x")
        bad = local_root / "unreadable"
        bad.mkdir()

        real_scandir = os.scandir

        def _scandir(path="."):
            if Path(path) == bad:
                # os.scandir carries the failing path in filename, like the kernel seam
                raise OSError(errno.EIO, os.strerror(errno.EIO), str(bad))
            return real_scandir(path)

        monkeypatch.setattr(os, "scandir", _scandir)

        result = _tool(tool, local_root)

        assert result["warnings"] == [
            f"Error reading {bad}: {os.strerror(errno.EIO)} (errno {errno.EIO})"
        ]
        assert key in result
        assert result["truncated"] is None


# ---------------------------------------------------------------------------
# S12 — freshness on a network root is bounded; a stuck walk means stale
# ---------------------------------------------------------------------------


def _block_walk(monkeypatch: pytest.MonkeyPatch, seconds: float = 10.0) -> None:
    """Make the freshness walk block, like a stat stuck on a dead share."""
    real_walk = os.walk

    def _slow_walk(*args, **kwargs):
        time.sleep(seconds)
        return real_walk(*args, **kwargs)

    monkeypatch.setattr(os, "walk", _slow_walk)


class TestWorkerParity:
    def test_budgeted_scan_matches_the_in_process_scan(self, local_root: Path) -> None:
        from filegraph.scanner import scan_tree

        (local_root / "alpha" / "beta").mkdir(parents=True)
        (local_root / "alpha" / "beta" / "deep.txt").write_text("d")
        (local_root / "alpha" / "one.txt").write_text("1")
        (local_root / "top.txt").write_text("t")
        (local_root / ".git").mkdir()
        (local_root / ".git" / "config").write_text("c")
        (local_root / "link.txt").symlink_to(local_root / "top.txt")

        in_process = scan_tree(local_root)
        budgeted = scan_tree(local_root, max_seconds=60)

        assert budgeted["truncated"] is None
        assert budgeted["tree"] == in_process["tree"]
        assert budgeted["warnings"] == in_process["warnings"]
