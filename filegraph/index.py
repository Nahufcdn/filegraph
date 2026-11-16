"""Tree index: import a listing of a tree and answer queries from it.

The scan path is bounded and live; the index is the complete counterpart. A
listing produced where the data is cheap to walk (``find -printf
'%y\\t%s\\t%T@\\t%p\\n'`` on the machine that owns the share) is imported once
into a local SQLite database per root, and every later query for that root is
answered from the database alone: no ``stat``, no ``scandir``, no walk, so an
unmounted or unreachable share still answers.

Storage follows the network-root cache rule (S4): the database lives in
``$XDG_CACHE_HOME/filegraph/<sha256 of the resolved root>/index.db``, never
inside the indexed tree and never on the share. It is a separate file from the
tool-result cache so a multi-million-row index never bloats it. A re-import
builds a fresh database next to the old one and publishes it with a single
``os.replace``, so a reader sees either the previous complete tree or the new
complete tree, never a half-imported one.

The root key is the *lexical* resolution of the requested path
(``normpath(abspath(...))``) on purpose: resolving symlinks would ``lstat`` the
mount on every query, which is exactly what S8 forbids.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
from collections import defaultdict
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from filegraph import scanner, store
from filegraph.scanbudget import Budget

#: File name of the index inside its per-root cache directory.
INDEX_DB_NAME = "index.db"

#: Bumped when the on-disk layout changes; an older index is unreadable.
SCHEMA_VERSION = "1"

#: How many skipped line numbers the import report names.
MAX_REPORTED_SKIPPED_LINES = 5

#: Rows buffered before one ``executemany`` during import.
_INSERT_BATCH = 10_000

#: Accepted listing letters (S1). Anything else is stored as ``unknown``.
_LETTER_TYPES = {"f": "file", "d": "directory", "l": "symlink"}

#: Letters written by ``export --format tsv``; ``u`` re-imports as ``unknown``.
_TYPE_LETTERS = {"file": "f", "directory": "d", "symlink": "l", "unknown": "u"}

#: Same strategies ``scanner.find_patterns`` accepts.
_PATTERN_STRATEGIES = frozenset({"auto", "sequence", "prefix", "suffix", "date"})

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entries (
    id     INTEGER PRIMARY KEY,
    path   TEXT NOT NULL UNIQUE,
    parent TEXT NOT NULL,
    name   TEXT NOT NULL,
    depth  INTEGER NOT NULL,
    type   TEXT NOT NULL,
    size   INTEGER NOT NULL,
    suffix TEXT NOT NULL,
    mtime  TEXT NOT NULL
);

-- ``path`` is UNIQUE, so its index also serves the path-ordered reads.
CREATE INDEX IF NOT EXISTS idx_entries_depth ON entries(depth);
CREATE INDEX IF NOT EXISTS idx_entries_type_suffix ON entries(type, suffix);
"""

_INSERT_SQL = (
    "INSERT OR REPLACE INTO entries "
    "(path, parent, name, depth, type, size, suffix, mtime) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)


class ListingRejected(Exception):
    """The listing cannot be imported: no usable rows, or a path outside root."""


class IndexUnreadable(Exception):
    """An index file exists but cannot be served."""


class IndexPathNotFound(Exception):
    """An export was asked for a path the index does not contain."""


# ---------------------------------------------------------------------------
# location: one database per root, in the local cache directory
# ---------------------------------------------------------------------------


def resolve_root(root: str | os.PathLike[str]) -> str:
    """Return the index key for ``root`` without touching the filesystem.

    Lexical only: ``abspath`` plus ``normpath``. ``realpath`` would ``lstat``
    every component, which on an unreachable mount is the very cost the index
    exists to avoid (S8).
    """
    return os.path.normpath(os.path.abspath(os.fspath(root)))


def _index_dir_for(resolved_root: str) -> Path:
    digest = hashlib.sha256(
        resolved_root.encode("utf-8", "surrogateescape")
    ).hexdigest()
    return store.local_cache_dir() / digest


def index_dir(root: str | os.PathLike[str]) -> Path:
    """Return the cache directory holding ``root``'s index."""
    return _index_dir_for(resolve_root(root))


def index_path(root: str | os.PathLike[str]) -> Path:
    """Return the SQLite file holding ``root``'s index."""
    return index_dir(root) / INDEX_DB_NAME


def has_index(root: str | os.PathLike[str]) -> bool:
    """Return True when ``root`` has an index (a local stat, never the root's)."""
    return os.path.isfile(index_path(root))


# ---------------------------------------------------------------------------
# path helpers shared by import and the queries
# ---------------------------------------------------------------------------


def _is_under(path: str, root: str) -> bool:
    """Return True when ``path`` is ``root`` or below it."""
    if path == root:
        return True
    if root == os.sep:
        return path.startswith(os.sep)
    return path.startswith(root + os.sep)


def _relative(path: str, root: str) -> str:
    """Return ``path`` relative to ``root`` ("" for the root itself)."""
    if path == root:
        return ""
    if root == os.sep:
        return path.lstrip(os.sep)
    return path[len(root) + 1 :]


def _depth(path: str, root: str) -> int:
    """Return the depth of ``path`` below ``root`` (root itself is 0)."""
    relative = _relative(path, root)
    return 0 if not relative else relative.count(os.sep) + 1


def _suffix(name: str) -> str:
    """Return the extension ``scanner`` compares, normalized the same way."""
    return os.path.splitext(name)[1].lower().lstrip(".")


def _excluded(relative_path: str, exclude: set[str]) -> bool:
    """Return True when a directory above ``relative_path`` is excluded."""
    if not exclude:
        return False
    parts = relative_path.split(os.sep)[:-1]
    return any(part in exclude for part in parts)


# ---------------------------------------------------------------------------
# import (S1, S2, S3, S4)
# ---------------------------------------------------------------------------


def _parse_line(line: str) -> tuple[str, int, str, str] | None:
    """Parse one listing row, or return None when it is malformed.

    The accepted shape is exactly ``find -printf '%y\\t%s\\t%T@\\t%p\\n'``:
    type letter, size, mtime, absolute path. A path containing tabs survives,
    because only the first three separators split the row.
    """
    text = line.rstrip("\n")
    if text.endswith("\r"):
        text = text[:-1]
    fields = text.split("\t", 3)
    if len(fields) != 4:
        return None
    letter, size_text, mtime_text, path = fields
    if len(letter) != 1 or not path:
        return None
    try:
        size = int(size_text)
    except ValueError:
        return None
    if size < 0:
        return None
    mtime = mtime_text.strip()
    if not mtime:
        return None
    try:
        float(mtime)
    except ValueError:
        return None
    return letter, size, mtime, path


def _entry_row(resolved_root: str, path: str, letter: str, size: int, mtime: str) -> tuple:
    """Build one stored row; the type letter maps to the stored type."""
    name = os.path.basename(path.rstrip(os.sep)) or path
    return (
        path,
        os.path.dirname(path),
        name,
        _depth(path, resolved_root),
        _LETTER_TYPES.get(letter, "unknown"),
        size,
        _suffix(name),
        mtime,
    )


def _apply_schema(conn: sqlite3.Connection) -> None:
    """Create the index schema on a fresh build connection."""
    conn.execute("PRAGMA journal_mode=OFF")
    conn.execute("PRAGMA synchronous=OFF")
    conn.executescript(_SCHEMA_SQL)


def _write_meta(conn: sqlite3.Connection, values: dict[str, object]) -> None:
    conn.executemany(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        [(key, str(value)) for key, value in values.items()],
    )


def _counts(conn: sqlite3.Connection) -> tuple[dict[str, int], int]:
    """Return per-type row counts and the total size of stored files."""
    counts = {"directory": 0, "file": 0, "symlink": 0, "unknown": 0}
    total_bytes = 0
    for row in conn.execute(
        "SELECT type, COUNT(*) AS rows, COALESCE(SUM(size), 0) AS total "
        "FROM entries GROUP BY type"
    ):
        counts[row["type"]] = int(row["rows"])
        if row["type"] == "file":
            total_bytes = int(row["total"])
    return counts, total_bytes


def _fsync(path: Path) -> None:
    """Flush a finished build to disk before it is published."""
    try:
        with open(path, "rb") as handle:
            os.fsync(handle.fileno())
    except OSError:
        pass


def import_listing(
    root: str | os.PathLike[str],
    lines: Iterable[str],
    *,
    source_name: str = "-",
) -> dict:
    """Import a text listing of ``root``'s tree as its index.

    Args:
        root: Root the listing describes. It is never stat-ed: the listing may
            be imported for a share that is not mounted here.
        lines: Iterable of listing lines (``type\\tsize\\tmtime\\tpath\\n``).
        source_name: Name of the listing, for the report.

    Returns:
        Dict with ``status``, ``root``, ``index_path``, per-type counts,
        ``total_bytes``, ``skipped``, ``skipped_lines`` and ``elapsed``.

    Raises:
        ListingRejected: A path is outside ``root``, or no row is usable.
    """
    started = time.monotonic()
    resolved_root = resolve_root(root)
    directory = _index_dir_for(resolved_root)
    final = directory / INDEX_DB_NAME
    replaced = os.path.isfile(final)
    directory.mkdir(parents=True, exist_ok=True)

    handle, temporary_name = tempfile.mkstemp(
        prefix=".index-", suffix=".tmp", dir=directory
    )
    os.close(handle)
    temporary = Path(temporary_name)

    skipped = 0
    skipped_lines: list[int] = []
    accepted = 0

    def _skip(lineno: int) -> None:
        nonlocal skipped
        skipped += 1
        if len(skipped_lines) < MAX_REPORTED_SKIPPED_LINES:
            skipped_lines.append(lineno)

    try:
        conn = sqlite3.connect(str(temporary))
        conn.row_factory = sqlite3.Row
        try:
            _apply_schema(conn)
            batch: list[tuple] = []
            for lineno, line in enumerate(lines, start=1):
                parsed = _parse_line(line)
                if parsed is None:
                    _skip(lineno)
                    continue
                letter, size, mtime, raw_path = parsed
                path = os.path.normpath(raw_path)
                if not os.path.isabs(path):
                    _skip(lineno)
                    continue
                if not _is_under(path, resolved_root):
                    raise ListingRejected(f"path outside --root: {path}")
                batch.append(_entry_row(resolved_root, path, letter, size, mtime))
                accepted += 1
                if len(batch) >= _INSERT_BATCH:
                    conn.executemany(_INSERT_SQL, batch)
                    batch.clear()
            if batch:
                conn.executemany(_INSERT_SQL, batch)
            if accepted == 0:
                raise ListingRejected(
                    "no usable entries in listing: "
                    f"skipped {skipped} malformed row(s)"
                )
            conn.commit()
            counts, total_bytes = _counts(conn)
            _write_meta(
                conn,
                {
                    "root": resolved_root,
                    "schema_version": SCHEMA_VERSION,
                    "source_format": "tsv",
                    "source_name": source_name,
                    "imported_at": datetime.now(timezone.utc).isoformat(
                        timespec="seconds"
                    ),
                    "skipped": skipped,
                    "skipped_lines": json.dumps(skipped_lines),
                },
            )
            conn.commit()
        finally:
            conn.close()
        _fsync(temporary)
        os.replace(temporary, final)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise

    return {
        "status": "imported",
        "root": resolved_root,
        "index_path": str(final),
        "entries": sum(counts.values()),
        "rows_read": accepted,
        "directories": counts["directory"],
        "files": counts["file"],
        "symlinks": counts["symlink"],
        "unknown": counts["unknown"],
        "total_bytes": total_bytes,
        "skipped": skipped,
        "skipped_lines": skipped_lines,
        "elapsed": time.monotonic() - started,
        "replaced": replaced,
    }


# ---------------------------------------------------------------------------
# read side: open an index without ever touching the indexed root
# ---------------------------------------------------------------------------


def _read_meta(conn: sqlite3.Connection) -> dict[str, str]:
    return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM meta")}


def _open(resolved_root: str) -> tuple[sqlite3.Connection, dict[str, str]] | None:
    """Open the index for ``resolved_root`` read-only, or None without an index.

    Raises:
        IndexUnreadable: The file exists but is not a usable index for this
            root. Serving a live scan instead would touch the mount, so the
            failure is reported.
    """
    path = _index_dir_for(resolved_root) / INDEX_DB_NAME
    if not os.path.isfile(path):
        return None
    try:
        conn = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        meta = _read_meta(conn)
    except (sqlite3.DatabaseError, sqlite3.OperationalError) as exc:
        raise IndexUnreadable(f"index unreadable: {path}: {exc}") from exc
    if meta.get("schema_version") != SCHEMA_VERSION or meta.get("root") != resolved_root:
        conn.close()
        raise IndexUnreadable(f"index unreadable: {path}: unexpected index layout")
    return conn, meta


def _files_in_order(conn: sqlite3.Connection, resolved_root: str) -> Iterator[sqlite3.Row]:
    """Yield stored files in the order a live walk yields them (sorted paths)."""
    yield from conn.execute(
        "SELECT path, name, size, suffix FROM entries "
        "WHERE type='file' AND path >= ? ORDER BY path",
        (resolved_root,),
    )


# ---------------------------------------------------------------------------
# queries served from the index (S6, S7)
# ---------------------------------------------------------------------------


def _node(row: sqlite3.Row) -> dict:
    """Build a tree node for one stored row."""
    kind = row["type"]
    if kind == "directory":
        return {"name": row["name"], "type": "directory", "children": []}
    if kind == "file":
        return {"name": row["name"], "type": "file"}
    if kind == "symlink":
        # The listing has no target column, so a symlink node names its type
        # and stops there.
        return {"name": row["name"], "type": "symlink"}
    return {"name": row["name"], "type": "unknown"}


class _RowStream:
    """One-row lookahead over a path-ordered cursor."""

    def __init__(self, rows: Iterable[sqlite3.Row]) -> None:
        self._rows = iter(rows)
        self._row: sqlite3.Row | None = None
        self._filled = False

    def peek(self) -> sqlite3.Row | None:
        if not self._filled:
            self._row = next(self._rows, None)
            self._filled = True
        return self._row

    def take(self) -> sqlite3.Row | None:
        row = self.peek()
        self._filled = False
        return row

    def skip_subtree(self, path: str) -> None:
        """Drop the rows below ``path``; a subtree is contiguous in path order."""
        prefix = path if path.endswith(os.sep) else path + os.sep
        while True:
            row = self.peek()
            if row is None or not row["path"].startswith(prefix):
                return
            self.take()


def _build_tree(
    conn: sqlite3.Connection,
    resolved_root: str,
    start: str,
    *,
    max_depth: int | None,
    max_entries: int | None,
    exclude: set[str],
) -> tuple[dict, dict | None]:
    """Build the nested tree below ``start``, honouring depth and entry caps.

    Rows are read in path order, which is exactly the live scan's depth-first,
    name-sorted visit order, so the tree, the order of every child list, the
    entry cap's stopping point and the ``truncated`` marker's ``entries_seen``
    all match a live scan of the same tree.
    """
    start_depth = _depth(start, resolved_root)
    tree = {
        "name": os.path.basename(start.rstrip(os.sep)) or start,
        "type": "directory",
        "children": [],
    }
    budget = Budget(max_entries=max_entries)

    sql = "SELECT path, name, depth, type FROM entries WHERE path > ?"
    params: list[object] = [start]
    if max_depth is not None:
        sql += " AND depth <= ?"
        params.append(start_depth + max_depth)
    sql += " ORDER BY path"
    stream = _RowStream(conn.execute(sql, params))

    def _scan(current: str, node: dict) -> None:
        """Fill ``node`` with the rows below ``current``, like ``_scan_node``."""
        while True:
            row = stream.peek()
            if row is None or not _is_under(row["path"], current):
                return
            stream.take()
            path = row["path"]
            if row["type"] == "directory" and row["name"] in exclude:
                # An excluded directory is hidden with everything below it.
                stream.skip_subtree(path)
                continue
            keep_going = budget.count_entry()
            child = _node(row)
            node["children"].append(child)
            if row["type"] == "directory":
                if budget.truncated is not None:
                    # The live scan never listed it, so its children are absent.
                    stream.skip_subtree(path)
                elif max_depth is None or row["depth"] - start_depth < max_depth:
                    _scan(path, child)
            if not keep_going:
                # The live scan records the entry that reached the cap, stops this
                # listing, and its caller's listing continues with one more entry
                # before stopping too. Dropping what is left of this listing and
                # returning reproduces that unwinding exactly.
                stream.skip_subtree(current)
                return

    _scan(start, tree)
    return tree, budget.truncated


def scan_tree(
    root: str | os.PathLike[str],
    *,
    max_depth: int | None = None,
    exclude_dirs: set[str] | None = None,
    max_entries: int | None = None,
) -> dict | None:
    """Answer ``scanner.scan_tree`` for an indexed root, or None without an index."""
    resolved_root = resolve_root(root)
    opened = _open(resolved_root)
    if opened is None:
        return None
    conn, _meta = opened
    try:
        tree, truncated = _build_tree(
            conn,
            resolved_root,
            resolved_root,
            max_depth=max_depth,
            max_entries=max_entries,
            exclude=set(exclude_dirs or ()),
        )
    finally:
        conn.close()
    return {
        "tree": tree,
        "warnings": [],
        "truncated": truncated,
        "source": "index",
    }


def search_by_type(
    root: str | os.PathLike[str],
    file_type: str,
    *,
    exclude_dirs: set[str] | None = None,
    max_entries: int | None = None,
) -> dict | None:
    """Answer ``scanner.search_files_budgeted`` for an indexed root."""
    resolved_root = resolve_root(root)
    opened = _open(resolved_root)
    if opened is None:
        return None
    conn, _meta = opened
    extension = file_type.lower().lstrip(".")
    exclude = set(exclude_dirs or ())
    budget = Budget(max_entries=max_entries)
    results: list[dict] = []
    try:
        # With an entry cap the cap counts every walked file, not only the
        # matches, so the candidate filter has to happen after counting.
        sql = "SELECT path, name, size, suffix FROM entries WHERE type='file'"
        params: list[object] = []
        if max_entries is None:
            sql += " AND suffix = ?"
            params.append(extension)
        sql += " AND path >= ? ORDER BY path"
        params.append(resolved_root)
        for row in conn.execute(sql, params):
            path = row["path"]
            if not _is_under(path, resolved_root):
                break
            if _excluded(_relative(path, resolved_root), exclude):
                continue
            stop = not budget.count_entry()
            if row["suffix"] == extension:
                results.append(
                    {"name": row["name"], "path": path, "size": int(row["size"])}
                )
            if stop:
                break
    finally:
        conn.close()
    return {
        "results": sorted(results, key=lambda item: item["name"]),
        "warnings": [],
        "truncated": budget.truncated,
        "source": "index",
    }


def find_patterns(
    root: str | os.PathLike[str],
    *,
    exclude_dirs: set[str] | None = None,
    strategy: str = "auto",
    min_group_size: int = 3,
    max_entries: int | None = None,
) -> dict | None:
    """Answer ``scanner.find_patterns_budgeted`` for an indexed root."""
    resolved_root = resolve_root(root)
    opened = _open(resolved_root)
    if opened is None:
        return None
    conn, _meta = opened
    if strategy not in _PATTERN_STRATEGIES:
        conn.close()
        return {"patterns": [], "warnings": [], "truncated": None, "source": "index"}
    exclude = set(exclude_dirs or ())
    budget = Budget(max_entries=max_entries)
    paths: list[str] = []
    try:
        for row in _files_in_order(conn, resolved_root):
            path = row["path"]
            if _excluded(_relative(path, resolved_root), exclude):
                continue
            stop = not budget.count_entry()
            paths.append(path)
            if stop:
                break
    finally:
        conn.close()
    entries = [(Path(path).stem, path) for path in paths]
    return {
        "patterns": scanner._detect_patterns(entries, strategy, min_group_size),
        "warnings": [],
        "truncated": budget.truncated,
        "source": "index",
    }


def find_duplicates(
    root: str | os.PathLike[str],
    *,
    exclude_dirs: set[str] | None = None,
    hash_algo: str = "sha256",
    min_size: int = 0,
    max_entries: int | None = None,
) -> dict | None:
    """Answer ``scanner.find_duplicates_budgeted`` for an indexed root.

    The listing records sizes but not contents, and reading file contents would
    touch the mount, so groups are size-only candidates: every group with two
    or more files of the same size is reported with ``hash`` None. Use a live
    scan to prove a group by content.
    """
    resolved_root = resolve_root(root)
    opened = _open(resolved_root)
    if opened is None:
        return None
    conn, _meta = opened
    try:
        valid_algo = hash_algo in hashlib.algorithms_guaranteed
    except Exception:  # pragma: no cover - defensive, mirrors scanner.find_duplicates
        valid_algo = False
    if not valid_algo:
        conn.close()
        return {
            "duplicates": [],
            "warnings": [],
            "truncated": None,
            "source": "index",
            "content_hashed": False,
        }

    exclude = set(exclude_dirs or ())
    budget = Budget(max_entries=max_entries)
    by_size: dict[int, list[str]] = defaultdict(list)
    try:
        for row in _files_in_order(conn, resolved_root):
            path = row["path"]
            if _excluded(_relative(path, resolved_root), exclude):
                continue
            stop = not budget.count_entry()
            size = int(row["size"])
            if size >= min_size and size != 0:
                by_size[size].append(path)
            if stop:
                break
    finally:
        conn.close()

    duplicates = [
        {"hash": None, "size": size, "files": sorted(files)}
        for size, files in sorted(by_size.items())
        if len(files) >= 2
    ]
    warnings = []
    if duplicates:
        warnings.append(
            "find_duplicates served from the index: file contents were not "
            "hashed, groups are size-only candidates"
        )
    return {
        "duplicates": duplicates,
        "warnings": warnings,
        "truncated": budget.truncated,
        "source": "index",
        "content_hashed": False,
    }


# ---------------------------------------------------------------------------
# export (S5)
# ---------------------------------------------------------------------------


def _subpath_start(resolved_root: str, subpath: str | os.PathLike[str] | None) -> str:
    """Resolve ``--path`` to an absolute path under ``root`` (lexically)."""
    if subpath is None:
        return resolved_root
    text = os.fspath(subpath)
    if os.path.isabs(text):
        start = os.path.normpath(text)
    else:
        start = os.path.normpath(os.path.join(resolved_root, text))
    if not _is_under(start, resolved_root):
        raise IndexPathNotFound(f"path not in index: {text}")
    return start


def _contains_subtree(conn: sqlite3.Connection, start: str) -> bool:
    """Return True when ``start`` or something below it is stored."""
    row = conn.execute(
        "SELECT path FROM entries WHERE path >= ? ORDER BY path LIMIT 1", (start,)
    ).fetchone()
    return row is not None and _is_under(row["path"], start)


def export_tree(
    root: str | os.PathLike[str],
    *,
    subpath: str | os.PathLike[str] | None = None,
    max_depth: int | None = None,
) -> dict | None:
    """Export the stored tree as ``scan_directory``'s nested tree shape."""
    resolved_root = resolve_root(root)
    opened = _open(resolved_root)
    if opened is None:
        return None
    conn, _meta = opened
    try:
        start = _subpath_start(resolved_root, subpath)
        if subpath is not None and not _contains_subtree(conn, start):
            raise IndexPathNotFound(f"path not in index: {subpath}")
        tree, _truncated = _build_tree(
            conn,
            resolved_root,
            start,
            max_depth=max_depth,
            max_entries=None,
            exclude=set(),
        )
    finally:
        conn.close()
    return tree


def export_tsv_lines(
    root: str | os.PathLike[str],
    *,
    subpath: str | os.PathLike[str] | None = None,
    max_depth: int | None = None,
) -> Iterator[str] | None:
    """Yield the stored rows as listing lines, in the shape ``import`` reads."""
    resolved_root = resolve_root(root)
    opened = _open(resolved_root)
    if opened is None:
        return None
    conn, _meta = opened
    try:
        start = _subpath_start(resolved_root, subpath)
        if subpath is not None and not _contains_subtree(conn, start):
            raise IndexPathNotFound(f"path not in index: {subpath}")
        start_depth = _depth(start, resolved_root)
    except BaseException:
        conn.close()
        raise

    def _lines() -> Iterator[str]:
        try:
            sql = "SELECT path, type, size, mtime FROM entries WHERE path >= ?"
            params: list[object] = [start]
            if max_depth is not None:
                sql += " AND depth <= ?"
                params.append(start_depth + max_depth)
            sql += " ORDER BY path"
            for row in conn.execute(sql, params):
                if not _is_under(row["path"], start):
                    break
                letter = _TYPE_LETTERS.get(row["type"], "u")
                yield f"{letter}\t{row['size']}\t{row['mtime']}\t{row['path']}\n"
        finally:
            conn.close()

    return _lines()


# ---------------------------------------------------------------------------
# status (S9)
# ---------------------------------------------------------------------------


def index_status(root: str | os.PathLike[str]) -> dict | None:
    """Return what the index for ``root`` holds, or None when it has none."""
    resolved_root = resolve_root(root)
    path = _index_dir_for(resolved_root) / INDEX_DB_NAME
    opened = _open(resolved_root)
    if opened is None:
        return None
    conn, meta = opened
    try:
        counts, total_bytes = _counts(conn)
    finally:
        conn.close()
    try:
        size_bytes = os.stat(path).st_size
    except OSError:
        size_bytes = 0
    return {
        "indexed": True,
        "root": resolved_root,
        "imported_at": meta.get("imported_at", ""),
        "source_format": meta.get("source_format", ""),
        "entries": sum(counts.values()),
        "directories": counts["directory"],
        "files": counts["file"],
        "symlinks": counts["symlink"],
        "unknown": counts["unknown"],
        "total_bytes": total_bytes,
        "skipped": int(meta.get("skipped", 0) or 0),
        "index_path": str(path),
        "index_size_bytes": size_bytes,
    }
