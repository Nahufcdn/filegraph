"""Core directory scanning logic."""

from __future__ import annotations

import hashlib
import os
import stat as stat_module
from collections import defaultdict
from collections.abc import Iterator
from pathlib import Path

from filegraph import fsscan
from filegraph.scanbudget import Budget, resolve_budget, run_budgeted

DEFAULT_EXCLUDED_DIRS: frozenset[str] = frozenset(
    {".git", "node_modules", "__pycache__", ".venv"}
)


def _list_dir(path: Path) -> list[Path]:
    """List a directory sorted by name, letting filesystem errors propagate."""
    return sorted(path.iterdir(), key=lambda p: p.name)


def _classify(path: Path) -> str:
    """Return 'symlink', 'file', 'directory' or 'unknown' for ``path``.

    One ``lstat`` through ``fsscan`` instead of ``is_symlink``/``is_file``/
    ``is_dir``, so ``EACCES``/``ESTALE``/``ENOTCONN``/``EIO`` raise with their
    errno instead of answering "not a file, not a directory".
    """
    st = fsscan.lstat_path(path)
    if stat_module.S_ISLNK(st.st_mode):
        return "symlink"
    if stat_module.S_ISDIR(st.st_mode):
        return "directory"
    if stat_module.S_ISREG(st.st_mode):
        return "file"
    return "unknown"


def _link_target(path: Path, budget: Budget) -> str:
    """Return a symlink's resolved target, warning instead of aborting the scan."""
    try:
        return str(path.resolve())
    except OSError as exc:
        budget.warn(f"Error reading {path}: {fsscan.error_detail(exc)}")
    try:
        return os.readlink(path)
    except OSError:
        return ""


def _worker_budget(
    session,
    max_seconds: float | None,
    deadline: float | None,
    max_entries: int | None,
) -> Budget:
    """Create the budget used inside a scan, streaming when in a worker."""
    return Budget(
        max_seconds=max_seconds,
        deadline=deadline,
        max_entries=max_entries,
        on_partial=None if session is None else session.emit,
        on_warning=None if session is None else session.warn,
    )


def _resolve_exclusions(
    exclude_dirs: list[str] | None,
    include_excluded: bool,
) -> set[str] | None:
    """Return the effective exclusion set.

    Args:
        exclude_dirs: Custom list of directory names to exclude.
        include_excluded: If True, bypass all exclusions.

    Returns:
        Set of directory names to exclude, or None if no exclusions apply.
    """
    if include_excluded:
        return set(exclude_dirs) if exclude_dirs else set()
    return set(exclude_dirs) if exclude_dirs is not None else DEFAULT_EXCLUDED_DIRS


def walk_directory(
    root: Path,
    exclude_dirs: set[str] | None = None,
    max_depth: int | None = None,
    budget: Budget | None = None,
) -> Iterator[Path]:
    """Yield every file path under root, pruning excluded dirs. Skips symlinks.

    Directory read failures are handed to ``os.walk``'s ``onerror`` and recorded
    in ``budget.warnings`` instead of being silently skipped.

    Args:
        root: Root directory to walk.
        exclude_dirs: Set of directory names to skip entirely. None means use
            DEFAULT_EXCLUDED_DIRS; empty set means skip nothing.
        max_depth: Maximum directory depth to traverse. None = unlimited.
        budget: Budget whose entry cap and deadline bound the walk, and whose
            ``warnings`` collect errors. None means unbounded.

    Yields:
        Path objects for each file found.
    """
    if budget is None:
        budget = Budget()

    try:
        if fsscan.path_kind(root) != "directory":
            return
    except OSError as exc:
        budget.warn(f"Error reading {root}: {fsscan.error_detail(exc)}")
        return

    effective_exclude = exclude_dirs if exclude_dirs is not None else DEFAULT_EXCLUDED_DIRS

    current: dict = {"dir": root}

    def _on_error(exc: OSError) -> None:
        where = exc.filename or current["dir"]
        budget.warn(f"Error reading {where}: {fsscan.error_detail(exc)}")

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=_on_error):
        if budget.truncated is not None:
            return
        current["dir"] = Path(dirpath)

        # Compute current depth relative to root
        rel = Path(dirpath).relative_to(root)
        depth = len(rel.parts)
        if max_depth is not None and depth >= max_depth:
            dirnames.clear()
            continue

        # Prune excluded directories in-place
        kept: list[str] = []
        for dirname in dirnames:
            if dirname in effective_exclude:
                continue
            child_dir = Path(dirpath, dirname)
            try:
                if _classify(child_dir) == "symlink":
                    continue
            except OSError as exc:
                budget.warn(f"Error reading {child_dir}: {fsscan.error_detail(exc)}")
                continue
            kept.append(dirname)
        dirnames[:] = sorted(kept)

        for fname in sorted(filenames):
            fpath = Path(dirpath) / fname
            try:
                if _classify(fpath) != "file":
                    continue
            except OSError as exc:
                budget.warn(f"Error reading {fpath}: {fsscan.error_detail(exc)}")
                continue
            stop = not budget.count_entry()
            yield fpath
            if stop:
                return


def scan_tree(
    path: Path,
    max_depth: int | None = None,
    exclude_dirs: set[str] | None = None,
    _current_depth: int = 0,
    max_seconds: float | None = None,
    max_entries: int | None = None,
) -> dict:
    """Build a JSON tree of the directory hierarchy.

    Returns a dict with keys: ``tree`` (nested dict), ``warnings`` (list of
    strings) and ``truncated`` (None for a complete scan, otherwise
    ``{"reason", "limit", "entries_seen"}``). Symlinks are recorded but not
    followed. Filesystem errors produce partial results with warnings.

    Args:
        path: Root directory to scan.
        max_depth: Maximum depth to traverse. None = unlimited.
        exclude_dirs: Set of directory names to exclude. None uses defaults.
        max_seconds: Wall-clock budget. None means the default for the filesystem
            kind: 45 s on a network mount, no deadline on a local one.
        max_entries: Entry cap. None means the default for the filesystem kind:
            200000 on a network mount, no cap on a local one.
    """
    root = Path(path)
    max_seconds, max_entries = resolve_budget(root, max_seconds, max_entries)
    budget = Budget(max_seconds=max_seconds, max_entries=max_entries)
    args = (
        str(root),
        max_depth,
        exclude_dirs,
        _current_depth,
        max_seconds,
        budget.deadline,
        max_entries,
    )
    return run_budgeted(
        _scan_tree_worker,
        lambda partials, warnings: _partial_tree(root, partials, warnings),
        budget,
        args=args,
    )


def _scan_tree_worker(
    session,
    root_path: str,
    max_depth: int | None,
    exclude_dirs: set[str] | None,
    current_depth: int,
    max_seconds: float | None,
    deadline: float | None,
    max_entries: int | None,
) -> dict:
    """Run one scan, inside the caller's process or a killable worker."""
    budget = _worker_budget(session, max_seconds, deadline, max_entries)
    tree = _scan_node(
        Path(root_path), (), current_depth, budget, max_depth, exclude_dirs
    )
    return {"tree": tree, "warnings": budget.warnings, "truncated": budget.truncated}


def _partial_tree(root: Path, partials: list[dict], warnings: list[str]) -> dict:
    """Rebuild the tree a killed scan had streamed so far."""
    nodes: dict[tuple[str, ...], dict] = {
        (): {"name": root.name or str(root), "type": "directory", "children": []}
    }
    for payload in partials:
        if payload.get("kind") != "entry":
            continue
        parts = tuple(payload.get("parts") or ())
        if not parts:
            continue
        kind = payload.get("type")
        node: dict = {"name": parts[-1]}
        if kind == "file":
            node["type"] = "file"
        elif kind == "symlink":
            node["type"] = "symlink"
            node["target"] = payload.get("target") or ""
        elif kind == "directory":
            node["type"] = "directory"
            node["children"] = []
        else:
            node["type"] = "unknown"
        nodes[parts] = node
        parent = nodes.get(parts[:-1])
        if parent is not None:
            children = parent.setdefault("children", [])
            if node not in children:
                children.append(node)
    return {"tree": nodes[()], "warnings": list(warnings), "truncated": None}


def _scan_node(
    path: Path,
    parts: tuple[str, ...],
    current_depth: int,
    budget: Budget,
    max_depth: int | None,
    exclude_dirs: set[str] | None = None,
) -> dict:
    """Recursively scan a single path node."""
    name = path.name or str(path)

    try:
        kind = _classify(path)
    except OSError as exc:
        budget.warn(f"Error reading {path}: {fsscan.error_detail(exc)}")
        return {"name": name, "type": "unknown"}

    # Symlink — record but do not follow
    if kind == "symlink":
        return {"name": name, "type": "symlink", "target": _link_target(path, budget)}

    if kind == "file":
        return {"name": name, "type": "file"}

    if kind != "directory":
        return {"name": name, "type": "unknown"}

    # Check depth limit, then the budget before touching the filesystem again
    if max_depth is not None and current_depth >= max_depth:
        return {"name": name, "type": "directory", "children": []}
    if budget.truncated is not None:
        return {"name": name, "type": "directory", "children": []}

    try:
        entries = _list_dir(path)
    except OSError as exc:
        budget.warn(f"Error reading {path}: {fsscan.error_detail(exc)}")
        return {"name": name, "type": "directory", "children": []}

    effective_exclude = exclude_dirs if exclude_dirs is not None else DEFAULT_EXCLUDED_DIRS
    children: list[dict] = []
    for entry in entries:
        try:
            child_kind = _classify(entry)
        except OSError as exc:
            budget.warn(f"Error reading {entry}: {fsscan.error_detail(exc)}")
            continue

        # Skip excluded directories
        if child_kind == "directory" and entry.name in effective_exclude:
            continue

        child_parts = parts + (entry.name,)
        streamed = {
            "kind": "entry",
            "parts": list(child_parts),
            "type": child_kind,
            "target": _link_target(entry, budget) if child_kind == "symlink" else None,
        }
        keep_going = budget.count_entry(streamed)
        children.append(
            _scan_node(entry, child_parts, current_depth + 1, budget, max_depth, exclude_dirs)
        )
        if not keep_going:
            # The entry that reached the cap is recorded, then the scan stops.
            break

    return {"name": name, "type": "directory", "children": children}


def search_files(
    path: Path,
    file_type: str,
    exclude_dirs: set[str] | None = None,
    budget: Budget | None = None,
) -> list[dict]:
    """Return a flat list of files matching the given extension under path.

    Args:
        path: Root directory to search.
        file_type: File extension to filter by (e.g. 'jpg', 'png').
        exclude_dirs: Set of directory names to exclude. None uses defaults.
        budget: Budget bounding the walk, whose ``warnings`` collect errors.

    Returns:
        List of dicts with keys: name, path, size.

    Raises:
        FileNotFoundError: If path does not exist.
    """
    if budget is None:
        budget = Budget()

    try:
        if fsscan.path_kind(path) != "directory":
            return []
    except FileNotFoundError as exc:
        raise FileNotFoundError(fsscan.fs_error(path, exc)) from exc

    ext = file_type.lower().lstrip(".")
    results: list[dict] = []

    for entry in walk_directory(path, exclude_dirs=exclude_dirs, budget=budget):
        if entry.suffix.lower().lstrip(".") == ext:
            try:
                stat = entry.stat()
            except OSError as exc:
                budget.warn(f"Error reading {entry}: {fsscan.error_detail(exc)}")
                continue
            item = {
                "name": entry.name,
                "path": str(entry),
                "size": stat.st_size,
            }
            results.append(item)
            budget.emit({"kind": "item", "item": item})

    return sorted(results, key=lambda f: f["name"])


def search_files_budgeted(
    path: Path,
    file_type: str,
    exclude_dirs: set[str] | None = None,
    max_seconds: float | None = None,
    max_entries: int | None = None,
) -> dict:
    """Search by extension under a budget.

    Returns a dict with ``results``, ``warnings`` and ``truncated``.
    """
    root = Path(path)
    max_seconds, max_entries = resolve_budget(root, max_seconds, max_entries)
    budget = Budget(max_seconds=max_seconds, max_entries=max_entries)
    args = (str(root), file_type, exclude_dirs, max_seconds, budget.deadline, max_entries)
    return run_budgeted(_search_worker, _assemble_search, budget, args=args)


def _search_worker(
    session,
    root_path: str,
    file_type: str,
    exclude_dirs: set[str] | None,
    max_seconds: float | None,
    deadline: float | None,
    max_entries: int | None,
) -> dict:
    budget = _worker_budget(session, max_seconds, deadline, max_entries)
    results = search_files(Path(root_path), file_type, exclude_dirs, budget=budget)
    return {"results": results, "warnings": budget.warnings, "truncated": budget.truncated}


def _assemble_search(partials: list[dict], warnings: list[str]) -> dict:
    items = [
        payload["item"]
        for payload in partials
        if payload.get("kind") == "item" and "item" in payload
    ]
    return {"results": items, "warnings": list(warnings), "truncated": None}


def find_patterns(
    path: Path,
    exclude_dirs: set[str] | None = None,
    strategy: str = "auto",
    min_group_size: int = 3,
    budget: Budget | None = None,
) -> list[dict]:
    """Detect naming patterns across files in a directory tree.

    Strategies:
        - sequence: numeric sequences like file_001.txt, file_002.txt
        - prefix: files sharing a common filename prefix (>= 3 chars)
        - suffix: files sharing a common filename suffix (before extension)
        - date: files containing date tokens like 2024-01-15 or 2024_01_15
        - auto: run all strategies and return combined results

    Args:
        path: Root directory to search.
        exclude_dirs: Set of directory names to exclude. None uses defaults.
        strategy: Detection strategy to use.
        min_group_size: Minimum number of files to form a pattern group.
        budget: Budget bounding the walk, whose ``warnings`` collect errors.

    Returns:
        List of dicts, each with keys: type, pattern, files (sorted).
    """
    if budget is None:
        budget = Budget()

    try:
        if fsscan.path_kind(path) != "directory":
            return []
    except FileNotFoundError:
        return []

    valid_strategies = {"auto", "sequence", "prefix", "suffix", "date"}
    if strategy not in valid_strategies:
        return []

    # Collect all filenames (stems and full paths)
    file_entries: list[tuple[str, str]] = []  # (stem, full_path)
    for fpath in walk_directory(path, exclude_dirs=exclude_dirs, budget=budget):
        try:
            _ = fpath.stat()
        except OSError as exc:
            budget.warn(f"Error reading {fpath}: {fsscan.error_detail(exc)}")
            continue
        file_entries.append((fpath.stem, str(fpath)))
        budget.emit({"kind": "file", "path": str(fpath)})

    return _detect_patterns(file_entries, strategy, min_group_size)


def _detect_patterns(
    file_entries: list[tuple[str, str]], strategy: str, min_group_size: int
) -> list[dict]:
    """Run the requested detection strategies over a file list."""
    if not file_entries:
        return []

    results: list[dict] = []

    if strategy in ("sequence", "auto"):
        results.extend(_detect_sequences(file_entries, min_group_size))

    if strategy in ("prefix", "auto"):
        results.extend(_detect_prefixes(file_entries, min_group_size))

    if strategy in ("suffix", "auto"):
        results.extend(_detect_suffixes(file_entries, min_group_size))

    if strategy in ("date", "auto"):
        results.extend(_detect_dates(file_entries, min_group_size))

    return results


def _detect_sequences(
    file_entries: list[tuple[str, str]], min_group_size: int
) -> list[dict]:
    """Detect numeric sequence patterns like file_001, file_002."""
    import re

    pattern = re.compile(r"^(.*?)(\d+)(\D*)$")
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)

    for stem, full_path in file_entries:
        m = pattern.match(stem)
        if m:
            prefix, _num, suffix = m.groups()
            key = (prefix, suffix)
            groups[key].append(full_path)

    results = []
    for (prefix, suffix), files in groups.items():
        if len(files) < min_group_size:
            continue
        # Verify numeric parts form a plausible sequence (sorted numeric check)
        nums = []
        for stem, fp in file_entries:
            m = pattern.match(stem)
            if m and m.group(1) == prefix and m.group(3) == suffix:
                nums.append(int(m.group(2)))
        nums_sorted = sorted(nums)
        # Accept if max - min + 1 == count (contiguous) or at least monotonically increasing
        if len(nums_sorted) >= min_group_size:
            pattern_str = f"{prefix}{{{nums_sorted[0]}}}{suffix}"
            results.append({
                "type": "sequence",
                "pattern": pattern_str,
                "files": sorted(files),
            })

    return results


def _detect_prefixes(
    file_entries: list[tuple[str, str]], min_group_size: int
) -> list[dict]:
    """Detect common filename prefixes (>= 3 chars, shared by >= min_group_size files)."""
    if len(file_entries) < min_group_size:
        return []

    stems = [stem for stem, _ in file_entries]
    # Find the longest common prefix across all stems
    prefix = os.path.commonprefix(stems)

    # Trim to last word boundary (underscore or hyphen)
    for i in range(len(prefix), 0, -1):
        if prefix[i - 1] in ("_", "-"):
            prefix = prefix[:i]
            break
    else:
        # No word boundary found — keep as-is if long enough
        pass

    if len(prefix) < 3:
        return []

    # Collect files that start with this prefix
    matching = [fp for stem, fp in file_entries if stem.startswith(prefix)]
    if len(matching) < min_group_size:
        return []

    return [{
        "type": "prefix",
        "pattern": prefix,
        "files": sorted(matching),
    }]


def _detect_suffixes(
    file_entries: list[tuple[str, str]], min_group_size: int
) -> list[dict]:
    """Detect common filename suffixes before extension.

    Finds the longest common suffix shared by >= min_group_size files,
    trimmed to the last word boundary.
    """
    if len(file_entries) < min_group_size:
        return []

    stems = [stem for stem, _ in file_entries]

    # Find the longest common suffix across all stems
    if not stems:
        return []

    # Reverse-stripping: find common suffix
    suffix = ""
    min_len = min(len(s) for s in stems)
    for i in range(1, min_len + 1):
        chars = {s[-i] for s in stems}
        if len(chars) == 1:
            suffix = stems[0][-i:]
        else:
            break

    # Trim to first word boundary (underscore or hyphen)
    for i, ch in enumerate(suffix):
        if ch in ("_", "-"):
            suffix = suffix[i + 1 :]
            break

    if len(suffix) < 2:
        return []

    # Collect files whose stem ends with this suffix
    matching = [fp for stem, fp in file_entries if stem.endswith(suffix)]
    if len(matching) < min_group_size:
        return []

    return [{
        "type": "suffix",
        "pattern": suffix,
        "files": sorted(matching),
    }]


def _detect_dates(
    file_entries: list[tuple[str, str]], min_group_size: int
) -> list[dict]:
    """Detect date tokens like 2024-01-15 or 2024_01_15 in filenames.

    Groups files by date-token pattern structure (e.g. YYYY-MM-DD),
    not by the exact date value.
    """
    import re

    date_pattern = re.compile(r"(\d{4})[-_](\d{2})[-_](\d{2})")
    # Group key: (year_position_pattern, month_position_pattern, day_position_pattern,
    #             separator_between_year_month, separator_between_month_day)
    # Simplified: use a canonical pattern like "YYYY-MM-DD" based on separators
    groups: dict[str, list[str]] = defaultdict(list)

    for stem, full_path in file_entries:
        matches = date_pattern.findall(stem)
        if matches:
            for year, month, day in matches:
                # Determine separator from original match
                date_token = f"{year}-{month}-{day}"
                # Find the actual separator used in the stem
                sep_match = re.search(r"\d{4}([-_])\d{2}[-_]\d{2}", stem)
                sep = sep_match.group(1) if sep_match else "-"
                pattern_key = f"YYYY{sep}MM{sep}DD"
                groups[pattern_key].append(full_path)

    results = []
    for pattern_key, files in groups.items():
        if len(files) >= min_group_size:
            results.append({
                "type": "date",
                "pattern": pattern_key,
                "files": sorted(set(files)),
            })

    return results


def find_duplicates(
    path: Path,
    exclude_dirs: set[str] | None = None,
    hash_algo: str = "sha256",
    min_size: int = 0,
    budget: Budget | None = None,
) -> list[dict]:
    """Find duplicate files by content hash.

    Uses a two-pass approach: group by size first, then hash files in
    groups with more than one candidate to avoid unnecessary I/O.

    Args:
        path: Root directory to search.
        exclude_dirs: Set of directory names to exclude. None uses defaults.
        hash_algo: Hash algorithm name (must be valid for hashlib).
        min_size: Minimum file size in bytes to consider.
        budget: Budget bounding the walk, whose ``warnings`` collect errors.

    Returns:
        List of dicts, each with keys: hash, size, files (sorted list of
        file path strings). Only groups with 2+ files are returned.
    """
    if budget is None:
        budget = Budget()

    try:
        if fsscan.path_kind(path) != "directory":
            return []
    except FileNotFoundError:
        return []

    try:
        valid_algos = set(hashlib.algorithms_guaranteed)
        if hash_algo not in valid_algos:
            return []
    except Exception:
        return []

    # Pass 1: group files by size
    size_groups: dict[int, list[Path]] = defaultdict(list)
    for fpath in walk_directory(path, exclude_dirs=exclude_dirs, budget=budget):
        try:
            size = fpath.stat().st_size
        except OSError as exc:
            budget.warn(f"Error reading {fpath}: {fsscan.error_detail(exc)}")
            continue
        if size < min_size:
            continue
        if size == 0:
            continue
        size_groups[size].append(fpath)

    # Pass 2: hash files in multi-entry groups
    hash_groups: dict[tuple[int, str], list[str]] = defaultdict(list)
    for size, candidates in size_groups.items():
        if len(candidates) < 2:
            continue
        for fpath in candidates:
            try:
                file_hash = _hash_file(fpath, hash_algo)
                key = (size, file_hash)
                hash_groups[key].append(str(fpath))
            except OSError as exc:
                budget.warn(f"Error reading {fpath}: {fsscan.error_detail(exc)}")
                continue

    # Build result: only groups with 2+ files
    results = []
    for (size, file_hash), files in sorted(hash_groups.items()):
        if len(files) >= 2:
            group = {
                "hash": file_hash,
                "size": size,
                "files": sorted(files),
            }
            results.append(group)
            budget.emit({"kind": "group", "group": group})

    return results


def find_duplicates_budgeted(
    path: Path,
    exclude_dirs: set[str] | None = None,
    hash_algo: str = "sha256",
    min_size: int = 0,
    max_seconds: float | None = None,
    max_entries: int | None = None,
) -> dict:
    """Find duplicates under a budget.

    Returns a dict with ``duplicates``, ``warnings`` and ``truncated``.
    """
    root = Path(path)
    max_seconds, max_entries = resolve_budget(root, max_seconds, max_entries)
    budget = Budget(max_seconds=max_seconds, max_entries=max_entries)
    args = (
        str(root),
        exclude_dirs,
        hash_algo,
        min_size,
        max_seconds,
        budget.deadline,
        max_entries,
    )
    return run_budgeted(_duplicates_worker, _assemble_duplicates, budget, args=args)


def _duplicates_worker(
    session,
    root_path: str,
    exclude_dirs: set[str] | None,
    hash_algo: str,
    min_size: int,
    max_seconds: float | None,
    deadline: float | None,
    max_entries: int | None,
) -> dict:
    budget = _worker_budget(session, max_seconds, deadline, max_entries)
    duplicates = find_duplicates(
        Path(root_path),
        exclude_dirs=exclude_dirs,
        hash_algo=hash_algo,
        min_size=min_size,
        budget=budget,
    )
    return {
        "duplicates": duplicates,
        "warnings": budget.warnings,
        "truncated": budget.truncated,
    }


def _assemble_duplicates(partials: list[dict], warnings: list[str]) -> dict:
    groups = [
        payload["group"]
        for payload in partials
        if payload.get("kind") == "group" and "group" in payload
    ]
    return {"duplicates": groups, "warnings": list(warnings), "truncated": None}


def find_patterns_budgeted(
    path: Path,
    exclude_dirs: set[str] | None = None,
    strategy: str = "auto",
    min_group_size: int = 3,
    max_seconds: float | None = None,
    max_entries: int | None = None,
) -> dict:
    """Detect naming patterns under a budget.

    Returns a dict with ``patterns``, ``warnings`` and ``truncated``.
    """
    root = Path(path)
    max_seconds, max_entries = resolve_budget(root, max_seconds, max_entries)
    budget = Budget(max_seconds=max_seconds, max_entries=max_entries)
    args = (
        str(root),
        exclude_dirs,
        strategy,
        min_group_size,
        max_seconds,
        budget.deadline,
        max_entries,
    )
    def _assemble(partials: list[dict], warnings: list[str]) -> dict:
        return _assemble_patterns(partials, warnings, strategy, min_group_size)

    return run_budgeted(_patterns_worker, _assemble, budget, args=args)


def _patterns_worker(
    session,
    root_path: str,
    exclude_dirs: set[str] | None,
    strategy: str,
    min_group_size: int,
    max_seconds: float | None,
    deadline: float | None,
    max_entries: int | None,
) -> dict:
    budget = _worker_budget(session, max_seconds, deadline, max_entries)
    patterns = find_patterns(
        Path(root_path),
        exclude_dirs=exclude_dirs,
        strategy=strategy,
        min_group_size=min_group_size,
        budget=budget,
    )
    return {"patterns": patterns, "warnings": budget.warnings, "truncated": budget.truncated}


def _assemble_patterns(
    partials: list[dict],
    warnings: list[str],
    strategy: str,
    min_group_size: int,
) -> dict:
    paths = [
        payload["path"]
        for payload in partials
        if payload.get("kind") == "file" and payload.get("path")
    ]
    entries = [(Path(path).stem, path) for path in paths]
    return {
        "patterns": _detect_patterns(entries, strategy, min_group_size),
        "warnings": list(warnings),
        "truncated": None,
    }


def _hash_file(path: Path, algo: str) -> str:
    """Compute hex digest of a file's contents."""
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def file_info(path: Path) -> dict:
    """Return metadata for a single file.

    Args:
        path: Path to a file.

    Returns:
        Dict with keys: name, size, modified, created, permissions, is_symlink.

    Raises:
        FileNotFoundError: If path does not exist.
        IsADirectoryError: If path is a directory.
        OSError: For any other filesystem failure, with its real errno.
    """
    try:
        kind = fsscan.path_kind(path)
    except FileNotFoundError as exc:
        raise FileNotFoundError(fsscan.fs_error(path, exc)) from exc

    if kind == "directory":
        raise IsADirectoryError(f"Expected a file, got a directory: {path}")

    stat = path.stat()
    return {
        "name": path.name,
        "size": stat.st_size,
        "modified": stat.st_mtime,
        "created": stat.st_ctime,
        "permissions": oct(stat.st_mode),
        "is_symlink": path.is_symlink(),
    }
