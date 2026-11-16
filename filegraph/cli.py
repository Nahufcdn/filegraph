"""CLI entry point for directory scanning tools."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from collections.abc import Iterable
from pathlib import Path

from filegraph import fsscan
from filegraph import index as tree_index
from filegraph import sources
from filegraph.cache import check_freshness, get_or_scan, make_cache_key
from filegraph.scanner import scan_tree, _resolve_exclusions
from filegraph.store import cache_stats, invalidate_path, list_entries_by_path
from filegraph.tools import (
    find_duplicates as _find_duplicates,
    find_patterns as _find_patterns,
    scan_directory,
    search_by_type,
)


def _format_tree(node: dict, prefix: str = "", is_last: bool = True) -> list[str]:
    """Convert a tree dict to indented lines with box-drawing characters."""
    lines: list[str] = []
    connector = "└── " if is_last else "├── "
    lines.append(f"{prefix}{connector}{node['name']}")

    if node.get("type") == "directory" and "children" in node:
        children = node["children"]
        child_prefix = prefix + ("    " if is_last else "│   ")
        for i, child in enumerate(children):
            lines.extend(_format_tree(child, child_prefix, i == len(children) - 1))

    return lines


def _print_json(data: dict) -> None:
    """Print data as formatted JSON."""
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _print_tree(data: dict) -> None:
    """Print tree data with box-drawing characters."""
    if "tree" in data:
        lines = _format_tree(data["tree"])
        print("\n".join(lines))
        if data.get("warnings"):
            print("\nWarnings:")
            for w in data["warnings"]:
                print(f"  ⚠ {w}")
    else:
        _print_json(data)


def cmd_scan(args: argparse.Namespace) -> int:
    """Execute the scan subcommand."""
    result = scan_directory(
        path=args.path,
        max_depth=args.depth,
        include_excluded=args.no_exclude,
    )
    if "error" in result:
        print(f"Error: {result['error']}", file=sys.stderr)
        return 1
    if args.format == "json":
        _print_json(result)
    else:
        _print_tree(result)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """Execute the search subcommand."""
    result = search_by_type(
        path=args.path,
        file_type=args.type,
        include_excluded=args.no_exclude,
    )
    if "error" in result:
        print(f"Error: {result['error']}", file=sys.stderr)
        return 1
    if args.format == "json":
        _print_json(result)
    else:
        results = result.get("results", [])
        if not results:
            print("No files found.")
        else:
            for r in results:
                print(f"{r['name']}  ({r['size']} bytes)")
    return 0


def cmd_find_duplicates(args: argparse.Namespace) -> int:
    """Execute the find-duplicates subcommand."""
    result = _find_duplicates(
        path=args.path,
        hash_algo=args.algo,
        min_size=args.min_size,
    )
    if "error" in result:
        print(f"Error: {result['error']}", file=sys.stderr)
        return 1
    groups = result.get("duplicates", [])
    if args.format == "json":
        _print_json(result)
    else:
        if not groups:
            print("No duplicates found.")
        else:
            for group in groups:
                print(f"\nHash: {group['hash'][:12]}... ({group['size']} bytes)")
                for f in group["files"]:
                    print(f"  {f}")
    return 0


def cmd_find_patterns(args: argparse.Namespace) -> int:
    """Execute the find-patterns subcommand."""
    result = _find_patterns(
        path=args.path,
        strategy=args.strategy,
        min_group_size=args.min_group,
    )
    if "error" in result:
        print(f"Error: {result['error']}", file=sys.stderr)
        return 1
    groups = result.get("patterns", [])
    if args.format == "json":
        _print_json(result)
    else:
        if not groups:
            print("No patterns found.")
        else:
            for group in groups:
                print(f"\n{group['type'].upper()}: {group['pattern']}")
                for f in group["files"]:
                    print(f"  {f}")
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    """Index a directory — scan and store results in cache."""
    target = Path(args.path).resolve()
    if not target.exists():
        print(f"Error: Path not found: {args.path}", file=sys.stderr)
        return 1
    if not target.is_dir():
        print(f"Error: Expected a directory: {args.path}", file=sys.stderr)
        return 1

    def _live_scan() -> dict:
        effective_exclude = _resolve_exclusions(None, False)
        result = scan_tree(target, exclude_dirs=effective_exclude)
        result["_root_path"] = str(target)
        return result

    result = get_or_scan(target, "scan_directory", str(target), _live_scan)
    tree = result.get("tree")
    if tree is None:
        print(f"Error: {result.get('error', 'Scan failed')}", file=sys.stderr)
        return 1
    print(f"Indexed {args.path}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    """Sync cache — re-scan stale entries for the given path."""
    target = Path(args.path).resolve()
    if not target.exists():
        print(f"Error: Path not found: {args.path}", file=sys.stderr)
        return 1
    if not target.is_dir():
        print(f"Error: Expected a directory: {args.path}", file=sys.stderr)
        return 1

    entries = list_entries_by_path(target, str(target))
    if not entries:
        print(f"No cached entries for {args.path}. Run 'filegraph index' first.")
        return 1

    refreshed = 0
    fresh = 0
    for entry in entries:
        if check_freshness(target, entry["cache_key"]):
            fresh += 1
        else:
            refreshed += 1

    print(f"Synced: {refreshed} refreshed, {fresh} fresh")
    return 0


def _source_from_args(args: argparse.Namespace) -> sources.Source:
    """Build the source ``sources add`` describes (validating it)."""
    return sources.Source(
        name=args.name,
        host=args.host,
        user=args.user,
        remote_path=args.remote_path,
        local_path=args.local_path,
        port=args.port,
        key=args.key,
        remote_command=args.remote_command,
    )


def cmd_sources_add(args: argparse.Namespace) -> int:
    """Add (or with --force replace) one source definition."""
    try:
        source = _source_from_args(args)
        replaced = args.name in sources.load_sources()
        sources.add_source(source, force=args.force)
    except sources.SourceError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"{'Replaced' if replaced else 'Added'} source: {args.name}")
    return 0


def cmd_sources_list(args: argparse.Namespace) -> int:
    """Print every source definition, never any key material."""
    try:
        entries = sources.load_sources()
    except sources.SourceError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if not entries:
        print("No sources configured.")
        return 0
    for name in sorted(entries):
        source = entries[name]
        print(name)
        print(f"  host: {source.host}")
        print(f"  user: {source.user}")
        if source.port is not None:
            print(f"  port: {source.port}")
        if source.key is not None:
            print(f"  key: {source.key}")
        print(f"  remote: {source.remote_path}")
        print(f"  local: {source.local_path}")
    return 0


def cmd_sources_remove(args: argparse.Namespace) -> int:
    """Delete one source definition."""
    try:
        sources.remove_source(args.name)
    except sources.SourceError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Removed source: {args.name}")
    return 0


def _print_import_counts(report: dict) -> None:
    """Print the per-type counts of an import report."""
    print(f"entries: {report['entries']}")
    print(f"directories: {report['directories']}")
    print(f"files: {report['files']}")
    print(f"symlinks: {report['symlinks']}")
    print(f"unknown: {report['unknown']}")
    print(f"total bytes: {report['total_bytes']}")
    skipped = f"skipped: {report['skipped']} malformed row(s)"
    if report["skipped_lines"]:
        lines = ", ".join(str(number) for number in report["skipped_lines"])
        if report["skipped"] > len(report["skipped_lines"]):
            lines += ", ..."
        skipped += f" at lines {lines}"
    print(skipped)
    print(f"index: {report['index_path']}")


def _command_line_text(argv: list[str]) -> str:
    """Render an argument list as one printable line.

    The arguments are shell-quoted so the reader can copy them, and newlines in
    the remote command are escaped so the line stays one line in a log.
    """
    return shlex.join(argv).replace("\n", "\\n")


def cmd_pull(args: argparse.Namespace) -> int:
    """Stream one source's listing over ssh and import it for its local mount."""
    try:
        source = sources.get_source(args.source)
    except sources.SourceError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    local_root = source.local_path if args.root is None else args.root
    if not os.path.isabs(local_root):
        print(f"Error: --root must be absolute: {local_root}", file=sys.stderr)
        return 1
    local_root = os.path.normpath(local_root)

    if args.verbose:
        print(
            f"ssh command: {_command_line_text(sources.command_line(source))}",
            file=sys.stderr,
        )

    lines = sources.iter_listing(
        source,
        local_root,
        progress=_print_progress if args.progress_every > 0 else None,
        progress_every=args.progress_every,
    )
    try:
        report = tree_index.import_listing(local_root, lines, source_name=source.name)
    except sources.PullError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except tree_index.ListingRejected as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"Error: cannot write index: {fsscan.error_detail(exc)}", file=sys.stderr)
        return 1
    finally:
        lines.close()

    print(
        f"Pulled {report['entries']} entries from {source.name} for {local_root} "
        f"in {report['elapsed']:.3f}s"
    )
    _print_import_counts(report)
    return 0


def _print_progress(rows: int, elapsed: float) -> None:
    """Report progress on stderr so stdout stays the pull's report."""
    rate = rows / elapsed if elapsed > 0 else 0.0
    print(
        f"progress: {rows} rows, {rate:.0f} entries/s, {elapsed:.3f}s elapsed",
        file=sys.stderr,
    )


def cmd_import(args: argparse.Namespace) -> int:
    """Import a tree listing as the index for --root."""
    if args.listing == "-":
        report = _run_import(args.root, sys.stdin, "-")
    else:
        try:
            handle = open(
                args.listing,
                "r",
                encoding="utf-8",
                errors="surrogateescape",
                newline="",
            )
        except OSError as exc:
            print(
                f"Error: cannot read listing: {fsscan.fs_error(args.listing, exc)}",
                file=sys.stderr,
            )
            return 1
        with handle:
            report = _run_import(args.root, handle, args.listing)

    if "error" in report:
        print(f"Error: {report['error']}", file=sys.stderr)
        return 1

    print(
        f"Imported {report['entries']} entries for {report['root']} "
        f"in {report['elapsed']:.3f}s"
    )
    _print_import_counts(report)
    return 0


def _run_import(root: str, lines: Iterable[str], source_name: str) -> dict:
    """Import ``lines``, turning an expected rejection into an error payload."""
    try:
        return tree_index.import_listing(root, lines, source_name=source_name)
    except tree_index.ListingRejected as exc:
        return {"error": str(exc)}
    except OSError as exc:
        return {"error": f"cannot write index: {fsscan.error_detail(exc)}"}


def cmd_export(args: argparse.Namespace) -> int:
    """Export the stored tree for --root as tsv or json."""
    try:
        if args.format == "tsv":
            lines = tree_index.export_tsv_lines(
                args.root, subpath=args.path, max_depth=args.max_depth
            )
            if lines is None:
                print(f"Error: no index for root: {args.root}", file=sys.stderr)
                return 1
            for line in lines:
                sys.stdout.write(line)
            return 0

        tree = tree_index.export_tree(
            args.root, subpath=args.path, max_depth=args.max_depth
        )
        if tree is None:
            print(f"Error: no index for root: {args.root}", file=sys.stderr)
            return 1
        _print_json(tree)
        return 0
    except tree_index.IndexPathNotFound as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except tree_index.IndexUnreadable as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def cmd_status(args: argparse.Namespace) -> int:
    """Show cache statistics, or the index status for --root."""
    if args.root is not None:
        return _print_index_status(args.root)

    # Walk up from cwd to find nearest .filegraph/cache.db
    current = Path.cwd()
    while True:
        db_path = current / ".filegraph" / "cache.db"
        if db_path.exists():
            break
        parent = current.parent
        if parent == current:
            # Reached filesystem root without finding cache
            print("Cache entries: 0")
            print("Total files:   0")
            print("DB size:       0 bytes")
            return 0
        current = parent

    stats = cache_stats(current)
    print(f"Cache entries: {stats['entries']}")
    print(f"Total files:   {stats['total_files']}")
    print(f"DB size:       {stats['size_bytes']} bytes")
    return 0


def _print_index_status(root: str) -> int:
    """Report whether ``root`` is indexed, and what the index holds."""
    resolved = tree_index.resolve_root(root)
    path = tree_index.index_path(resolved)
    try:
        status = tree_index.index_status(resolved)
    except tree_index.IndexUnreadable as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Indexed: {'yes' if status else 'no'}")
    print(f"root: {resolved}")
    if status:
        print(f"imported: {status['imported_at']}")
        print(f"entries: {status['entries']}")
        print(f"directories: {status['directories']}")
        print(f"files: {status['files']}")
        print(f"symlinks: {status['symlinks']}")
        print(f"unknown: {status['unknown']}")
        print(f"total bytes: {status['total_bytes']}")
    print(f"index: {path}")
    return 0


def cmd_unindex(args: argparse.Namespace) -> int:
    """Remove cache entries for the given path."""
    target = Path(args.path).resolve()
    removed = invalidate_path(target, str(target))
    print(f"Removed {removed} cached entries")
    return 0


def _help_action(parser: argparse.ArgumentParser):
    """Return a command that prints ``parser``'s help and reports a failure."""

    def _show(_args: argparse.Namespace) -> int:
        parser.print_help()
        return 1

    return _show


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with subcommands."""
    parser = argparse.ArgumentParser(
        prog="filegraph",
        description="Directory scanning and file system inspection tools",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # scan
    scan_parser = subparsers.add_parser("scan", help="Scan directory hierarchy")
    scan_parser.add_argument("path", help="Directory to scan")
    scan_parser.add_argument("--format", choices=["json", "tree"], default="tree")
    scan_parser.add_argument("--depth", type=int, default=None, help="Max depth")
    scan_parser.add_argument("--no-exclude", action="store_true", help="Include excluded dirs")
    scan_parser.set_defaults(func=cmd_scan)

    # find-duplicates
    dup_parser = subparsers.add_parser("find-duplicates", help="Find duplicate files")
    dup_parser.add_argument("path", help="Directory to scan")
    dup_parser.add_argument("--format", choices=["json", "tree"], default="tree")
    dup_parser.add_argument("--algo", default="sha256", help="Hash algorithm")
    dup_parser.add_argument("--min-size", type=int, default=0, help="Min file size")
    dup_parser.set_defaults(func=cmd_find_duplicates)

    # search
    search_parser = subparsers.add_parser("search", help="Search files by type")
    search_parser.add_argument("path", help="Directory to search")
    search_parser.add_argument("type", help="File extension (e.g. jpg, png)")
    search_parser.add_argument("--format", choices=["json", "tree"], default="tree")
    search_parser.add_argument("--no-exclude", action="store_true", help="Include excluded dirs")
    search_parser.set_defaults(func=cmd_search)

    # find-patterns
    pattern_parser = subparsers.add_parser("find-patterns", help="Find naming patterns")
    pattern_parser.add_argument("path", help="Directory to scan")
    pattern_parser.add_argument("--format", choices=["json", "tree"], default="tree")
    pattern_parser.add_argument("--strategy", default="auto", help="Detection strategy")
    pattern_parser.add_argument("--min-group", type=int, default=3, help="Min group size")
    pattern_parser.set_defaults(func=cmd_find_patterns)

    # index
    index_parser = subparsers.add_parser("index", help="Create/update cache for directory")
    index_parser.add_argument("path", help="Directory to index")
    index_parser.set_defaults(func=cmd_index)

    # sync
    sync_parser = subparsers.add_parser("sync", help="Incremental cache update")
    sync_parser.add_argument("path", help="Directory to sync")
    sync_parser.set_defaults(func=cmd_sync)

    # status
    status_parser = subparsers.add_parser("status", help="Show cache statistics")
    status_parser.add_argument(
        "--root",
        default=None,
        help="Report the index status of this root instead of cache statistics",
    )
    status_parser.set_defaults(func=cmd_status)

    # sources
    sources_parser = subparsers.add_parser(
        "sources", help="Manage the sources `pull` can fetch from"
    )
    sources_sub = sources_parser.add_subparsers(dest="sources_command")

    sources_add_parser = sources_sub.add_parser(
        "add", help="Define a remote tree and the local mount that serves it"
    )
    sources_add_parser.add_argument("name", help="Source name, e.g. tank")
    sources_add_parser.add_argument("--host", required=True, help="Remote host")
    sources_add_parser.add_argument("--user", required=True, help="Remote user")
    sources_add_parser.add_argument(
        "--remote-path", required=True, help="Absolute path the remote find walks"
    )
    sources_add_parser.add_argument(
        "--local-path",
        required=True,
        help="Absolute local mount path that becomes the index root",
    )
    sources_add_parser.add_argument("--port", type=int, default=None, help="SSH port")
    sources_add_parser.add_argument(
        "--key", default=None, help="Path to the ssh private key (never its contents)"
    )
    sources_add_parser.add_argument(
        "--remote-command",
        default=sources.DEFAULT_REMOTE_COMMAND,
        help=(
            "Remote command template; {path} is replaced by the remote path "
            "(default: %(default)r)"
        ),
    )
    sources_add_parser.add_argument(
        "--force", action="store_true", help="Replace an existing definition"
    )
    sources_add_parser.set_defaults(func=cmd_sources_add)

    sources_list_parser = sources_sub.add_parser(
        "list", help="Print the source definitions"
    )
    sources_list_parser.set_defaults(func=cmd_sources_list)

    sources_remove_parser = sources_sub.add_parser("remove", help="Delete a source")
    sources_remove_parser.add_argument("name", help="Source name")
    sources_remove_parser.set_defaults(func=cmd_sources_remove)

    sources_parser.set_defaults(func=_help_action(sources_parser))

    # pull
    pull_parser = subparsers.add_parser(
        "pull", help="Fetch a source's listing over ssh and import it"
    )
    pull_parser.add_argument(
        "--source", required=True, help="Name of a configured source"
    )
    pull_parser.add_argument(
        "--root",
        default=None,
        help="Absolute local mount path to index instead of the configured one",
    )
    pull_parser.add_argument(
        "--verbose", action="store_true", help="Print the invoked ssh command line"
    )
    pull_parser.add_argument(
        "--progress-every",
        type=int,
        default=sources.DEFAULT_PROGRESS_EVERY,
        metavar="N",
        help="Report progress every N rows, 0 to disable (default: %(default)s)",
    )
    pull_parser.set_defaults(func=cmd_pull)

    # import
    import_parser = subparsers.add_parser(
        "import", help="Import a tree listing as the index for a root"
    )
    import_parser.add_argument(
        "--root", required=True, help="Root path the listing describes"
    )
    import_parser.add_argument(
        "--format", choices=["tsv"], default="tsv", help="Listing format"
    )
    import_parser.add_argument(
        "listing", nargs="?", default="-", help="Listing file, or - for stdin"
    )
    import_parser.set_defaults(func=cmd_import)

    # export
    export_parser = subparsers.add_parser(
        "export", help="Export the stored tree for a root"
    )
    export_parser.add_argument("--root", required=True, help="Root to export")
    export_parser.add_argument(
        "--format", choices=["json", "tsv"], default="json", help="Output format"
    )
    export_parser.add_argument(
        "--path", default=None, help="Export only this path below the root"
    )
    export_parser.add_argument(
        "--max-depth", type=int, default=None, help="Max depth from the export root"
    )
    export_parser.set_defaults(func=cmd_export)

    # unindex
    unindex_parser = subparsers.add_parser("unindex", help="Remove cache for path")
    unindex_parser.add_argument("path", help="Directory to unindex")
    unindex_parser.set_defaults(func=cmd_unindex)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 1

    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
