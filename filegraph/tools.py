"""MCP tool definitions for directory scanning."""

from __future__ import annotations

from pathlib import Path

from filegraph import fsscan
from filegraph.cache import check_freshness, get_or_scan
from filegraph.scanbudget import resolve_budget
from filegraph.scanner import (
    DEFAULT_EXCLUDED_DIRS,
    _resolve_exclusions,
    file_info,
    find_duplicates_budgeted,
    find_patterns_budgeted,
    scan_tree,
    search_files_budgeted,
)
from filegraph.store import cache_stats, invalidate_path, list_entries_by_path


def _directory_target(path: str) -> tuple[Path | None, str | None]:
    """Resolve ``path`` and confirm it is a directory.

    Returns ``(target, None)`` when usable, otherwise ``(None, error)``. A
    filesystem failure is reported with its path and errno, never as a plain
    "not found": ``pathlib`` answers ``False`` for EACCES/ESTALE/ENOTCONN/EIO,
    which is how a broken mount used to look like an empty directory.
    """
    target = Path(path).resolve()
    try:
        kind = fsscan.path_kind(target)
    except OSError as exc:
        return None, fsscan.fs_error(target, exc)
    if kind != "directory":
        return None, f"Expected a directory: {path}"
    return target, None


def _budget_cache_key(
    max_seconds: float | None, max_entries: int | None
) -> dict:
    """Cache-key extras for the effective budgets.

    Empty for an unbudgeted (local) scan, so existing cache entries stay valid.
    """
    options: dict = {}
    if max_seconds is not None:
        options["max_seconds"] = max_seconds
    if max_entries is not None:
        options["max_entries"] = max_entries
    return options


def scan_directory(
    path: str,
    max_depth: int | None = None,
    exclude_dirs: list[str] | None = None,
    include_excluded: bool = False,
    max_seconds: float | None = None,
    max_entries: int | None = None,
) -> dict:
    """Scan a directory and return its hierarchy as a JSON tree.

    Args:
        path: Absolute or relative path to the directory to scan. REQUIRED.
        max_depth: Maximum depth to traverse (None = unlimited).
        exclude_dirs: Custom list of directory names to exclude.
        include_excluded: If True, bypass all exclusions.
        max_seconds: Wall-clock budget in seconds. None means the default for
            the filesystem kind: 45 s on a network mount (cifs, nfs, sshfs, 9p,
            afs), no deadline on a local one.
        max_entries: Entry cap. None means the default for the filesystem kind:
            200000 on a network mount, no cap on a local one.

    Returns:
        Dict with 'tree' (nested dict), 'warnings' (list of strings) and
        'truncated' (None when complete, otherwise {"reason":
        "time_budget"|"entry_budget", "limit", "entries_seen"}).
    """
    target, error = _directory_target(path)
    if error is not None:
        return {"error": error, "tree": None, "warnings": []}

    effective_exclude = _resolve_exclusions(exclude_dirs, include_excluded)
    max_seconds, max_entries = resolve_budget(target, max_seconds, max_entries)

    def _live_scan() -> dict:
        result = scan_tree(
            target,
            max_depth=max_depth,
            exclude_dirs=effective_exclude,
            max_seconds=max_seconds,
            max_entries=max_entries,
        )
        result["_root_path"] = str(target)
        return result

    result = get_or_scan(
        target,
        "scan_directory",
        str(target),
        _live_scan,
        **_budget_cache_key(max_seconds, max_entries),
    )
    result.pop("_root_path", None)
    return result


def search_by_type(
    path: str,
    file_type: str,
    exclude_dirs: list[str] | None = None,
    include_excluded: bool = False,
    max_seconds: float | None = None,
    max_entries: int | None = None,
) -> dict:
    """Search for files by extension under the given path.

    Args:
        path: Root directory to search. REQUIRED.
        file_type: File extension to filter by (e.g. 'jpg', 'png').
        exclude_dirs: Custom list of directory names to exclude.
        include_excluded: If True, bypass all exclusions.
        max_seconds: Wall-clock budget in seconds. None means 45 s on a network
            mount and no deadline on a local one.
        max_entries: Entry cap. None means 200000 on a network mount and no cap
            on a local one.

    Returns:
        Dict with 'results' (list of file dicts), 'warnings' (list of strings)
        and 'truncated' (None when complete, otherwise the budget marker).
    """
    target, error = _directory_target(path)
    if error is not None:
        return {"error": error, "results": []}

    effective_exclude = _resolve_exclusions(exclude_dirs, include_excluded)
    max_seconds, max_entries = resolve_budget(target, max_seconds, max_entries)

    def _live_scan() -> dict:
        return search_files_budgeted(
            target,
            file_type,
            exclude_dirs=effective_exclude,
            max_seconds=max_seconds,
            max_entries=max_entries,
        )

    return get_or_scan(
        target,
        "search_by_type",
        str(target),
        _live_scan,
        file_type=file_type,
        **_budget_cache_key(max_seconds, max_entries),
    )


def get_file_metadata(path: str) -> dict:
    """Get metadata for a specific file.

    Args:
        path: Path to the file. REQUIRED.

    Returns:
        Dict with file metadata or an error naming the path and the errno.
    """
    target = Path(path).resolve()

    try:
        return file_info(target)
    except FileNotFoundError as exc:
        return {"error": fsscan.fs_error(target, exc)}
    except IsADirectoryError:
        return {"error": f"Expected a file, got a directory: {path}"}
    except OSError as exc:
        return {"error": fsscan.fs_error(target, exc)}


def find_duplicates(
    path: str,
    hash_algo: str = "sha256",
    min_size: int = 0,
    max_seconds: float | None = None,
    max_entries: int | None = None,
) -> dict:
    """Find duplicate files by content hash under the given path.

    Args:
        path: Root directory to search. REQUIRED.
        hash_algo: Hash algorithm name (default: sha256).
        min_size: Minimum file size in bytes to consider.
        max_seconds: Wall-clock budget in seconds. None means 45 s on a network
            mount and no deadline on a local one.
        max_entries: Entry cap. None means 200000 on a network mount and no cap
            on a local one.

    Returns:
        Dict with 'duplicates' (list of duplicate group dicts), 'warnings'
        (list of strings) and 'truncated' (None when complete).
    """
    target, error = _directory_target(path)
    if error is not None:
        return {"error": error, "duplicates": []}

    max_seconds, max_entries = resolve_budget(target, max_seconds, max_entries)

    def _live_scan() -> dict:
        effective_exclude = _resolve_exclusions(None, False)
        return find_duplicates_budgeted(
            target,
            exclude_dirs=effective_exclude,
            hash_algo=hash_algo,
            min_size=min_size,
            max_seconds=max_seconds,
            max_entries=max_entries,
        )

    return get_or_scan(
        target,
        "find_duplicates",
        str(target),
        _live_scan,
        **_budget_cache_key(max_seconds, max_entries),
    )


def find_patterns(
    path: str,
    strategy: str = "auto",
    min_group_size: int = 3,
    max_seconds: float | None = None,
    max_entries: int | None = None,
) -> dict:
    """Detect naming patterns across files under the given path.

    Args:
        path: Root directory to search. REQUIRED.
        strategy: Detection strategy (auto, sequence, prefix, suffix, date).
        min_group_size: Minimum files to form a pattern group.
        max_seconds: Wall-clock budget in seconds. None means 45 s on a network
            mount and no deadline on a local one.
        max_entries: Entry cap. None means 200000 on a network mount and no cap
            on a local one.

    Returns:
        Dict with 'patterns' (list of pattern group dicts), 'warnings' (list of
        strings) and 'truncated' (None when complete).
    """
    target, error = _directory_target(path)
    if error is not None:
        return {"error": error, "patterns": []}

    max_seconds, max_entries = resolve_budget(target, max_seconds, max_entries)

    def _live_scan() -> dict:
        effective_exclude = _resolve_exclusions(None, False)
        return find_patterns_budgeted(
            target,
            exclude_dirs=effective_exclude,
            strategy=strategy,
            min_group_size=min_group_size,
            max_seconds=max_seconds,
            max_entries=max_entries,
        )

    return get_or_scan(
        target,
        "find_patterns",
        str(target),
        _live_scan,
        **_budget_cache_key(max_seconds, max_entries),
    )


# Cache management tools


def index_directory(
    path: str,
    max_seconds: float | None = None,
    max_entries: int | None = None,
) -> dict:
    """Index a directory — scan and store results in cache.

    Args:
        path: Directory to index. REQUIRED.
        max_seconds: Wall-clock budget in seconds. None means 45 s on a network
            mount and no deadline on a local one.
        max_entries: Entry cap. None means 200000 on a network mount and no cap
            on a local one.

    Returns:
        Dict with status, the path, and the 'truncated' marker when the scan was
        stopped by a budget.
    """
    target, error = _directory_target(path)
    if error is not None:
        return {"status": "error", "error": error}

    max_seconds, max_entries = resolve_budget(target, max_seconds, max_entries)

    def _live_scan() -> dict:
        effective_exclude = _resolve_exclusions(None, False)
        result = scan_tree(
            target,
            exclude_dirs=effective_exclude,
            max_seconds=max_seconds,
            max_entries=max_entries,
        )
        result["_root_path"] = str(target)
        return result

    result = get_or_scan(
        target,
        "scan_directory",
        str(target),
        _live_scan,
        **_budget_cache_key(max_seconds, max_entries),
    )
    tree = result.get("tree")

    if tree is None:
        return {"status": "error", "error": result.get("error", "Scan failed")}

    status: dict = {"status": "indexed", "path": str(target)}
    if result.get("truncated") is not None:
        status["truncated"] = result["truncated"]
    return status


def sync_cache(path: str) -> dict:
    """Sync cache — re-scan stale entries for the given path.

    Args:
        path: Directory to sync. REQUIRED.

    Returns:
        Dict with refreshed/fresh counts and optional error message.
    """
    target, error = _directory_target(path)
    if error is not None:
        return {"status": "error", "error": error}

    entries = list_entries_by_path(target, str(target))
    if not entries:
        return {"status": "error", "error": f"No cached entries for {path}. Run index first."}

    refreshed = 0
    fresh = 0
    for entry in entries:
        if check_freshness(target, entry["cache_key"]):
            fresh += 1
        else:
            refreshed += 1

    return {"status": "synced", "refreshed": refreshed, "fresh": fresh}


def cache_status(path: str | None = None) -> dict:
    """Show cache statistics.

    Args:
        path: Optional directory path. If not provided, searches from cwd upward.

    Returns:
        Dict with entries, total_files, and size_bytes.
    """
    if path:
        target = Path(path).resolve()
    else:
        # Walk up from cwd to find nearest .filegraph/cache.db
        current = Path.cwd()
        target = None
        while True:
            db_path = current / ".filegraph" / "cache.db"
            if db_path.exists():
                target = current
                break
            parent = current.parent
            if parent == current:
                break
            current = parent

        if target is None:
            return {"entries": 0, "total_files": 0, "size_bytes": 0}

    stats = cache_stats(target)
    return stats


def unindex_directory(path: str) -> dict:
    """Remove cache entries for the given path.

    Args:
        path: Directory to unindex. REQUIRED.

    Returns:
        Dict with removed count and optional error message.
    """
    target = Path(path).resolve()
    removed = invalidate_path(target, str(target))
    return {"status": "unindexed", "removed": removed}
