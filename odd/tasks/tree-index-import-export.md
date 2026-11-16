# Tree index: import and export

Collect a directory tree where it is cheap (a `find` on the server that owns the data, handed over as
a text listing) and serve it from `filegraph` without ever re-walking the mount. This is the
counterpart of the bounded scan: the scan is for live lookups, the index is for the complete graph.

Sizing that forces this design: the NAS share holds 139.993 directories, each with 3-6 subdirectories
and ~20 or more files — on the order of 3.5 million entries. Measured per-entry cost over the CIFS
mount (190 ms to list a name, ~2.4 s for a cold `stat`) makes a full client-side crawl a multi-week
job, while the same walk on the server is local IO.

## Specs

- **S1** — `filegraph import --root <path> [--format tsv] <listing | ->` reads a text listing of a
  tree and stores it as an index for `<root>`. The accepted TSV shape is exactly what
  `find -printf '%y\t%s\t%T@\t%p\n'` produces: type, size, mtime, absolute path. `f` is a file,
  `d` a directory, `l` a symlink; any other type letter is stored as `unknown`.
- **S2** — The import reports what it did: entries read, directories, files, total bytes, entries
  skipped as malformed (with the first few line numbers), and the elapsed time. Malformed lines never
  abort the whole import; a listing with zero usable rows is an error with a non-zero exit code.
- **S3** — Import is idempotent per root: re-importing the same listing replaces the stored index for
  that root instead of duplicating it, and importing a listing whose paths are not under `--root` is
  rejected with an explicit error naming the first offending path.
- **S4** — The index is stored in the local cache location already used for network roots
  (`$XDG_CACHE_HOME/filegraph/<sha256 of the resolved root>/`), never inside the indexed tree, and
  never in the share.
- **S5** — `filegraph export --root <path> [--format json|tsv] [--path <subpath>] [--max-depth N]`
  writes the stored tree back out: `tsv` in the same shape S1 reads (round-trip safe), `json` as the
  same nested `{"name", "type", "children"}` tree `scan_directory` returns. Exporting a root with no
  index is an explicit error, not an empty tree.
- **S6** — `scan_directory`, `search_by_type`, `find_patterns` and `find_duplicates` serve an indexed
  root from the index instead of walking the filesystem, and say so in their result
  (`"source": "index"` versus `"source": "scan"`). For a root with no index, behaviour is exactly
  today's.
- **S7** — Serving from the index honours the existing contracts: `max_depth`, `max_entries`,
  `truncated` with `{"reason", "limit", "entries_seen"}`, `warnings`, and the same sorted order a
  live scan produces for the same tree.
- **S8** — An indexed root is never re-validated against the filesystem: the index is a snapshot, and
  querying it must not touch the mount (no `stat`, no walk). Freshness only applies to live scans.
- **S9** — `filegraph status --root <path>` (or `cache_status` when an index exists) reports whether
  the root is indexed, when the import ran, and the counts per type with total bytes, so the user can
  tell "indexed" from "scanned".

## Tasks

| ID | Specs | Route | Notes |
| --- | --- | --- | --- |
| T1 | S1, S2, S3, S4 | writer | `filegraph/index.py` + storage + `import` CLI command, test-first |
| T2 | S5 | writer | `export` CLI command, tsv round-trip and json tree |
| T3 | S6, S7 | writer | `scan_directory` served from the index, same contracts |
| T4 | S6, S7 | writer | `search_by_type`, `find_patterns`, `find_duplicates` served from the index |
| T5 | S8, S9 | writer | Never touch the mount for an indexed root; index status reporting |
| T6 | S1-S9 | parent | Docs: README section, CLI help, and a real import of the NAS listing once available |

## Log

- **L1** (user, verbatim): "vamos a ahcer una cosa implementemos por ahora el import y export del
  filegraph para poder hacer lo del ssh en el caso de que me pasen las credenciales"
- **L2** (user, verbatim): "la carpeta que quiero analizar del nas tiene 139.993 carpetas que dentro
  puede contener de a 3 o 6 subs carpetas y aproximado 20 archivos o mas"
- **L3** (user, verbatim): "y luego vamos a ver lo del Crawl por CLI"
- **L4** (decision, user): no SSH access is granted by the human to the agent; the human runs the
  enumeration themselves and hands over the listing. `filegraph` therefore needs an import path and
  must not require credentials for the share.
- **L5** (evidence, 2026-11-16): measured on the live mount — listing a name costs ~190 ms and a cold
  `stat` ~2.4 s per file, while directory metadata is cheap. With ~3.5 M entries implied by L2, a
  client-side crawl is ~185 h of listing alone, which is why the index is built from a server-side
  `find` and served locally.
- **L6** (decision, parent): the index is stored per root as its own SQLite database in the local
  cache directory (S4), separate from the tool-result cache, so a 3.5 M-row index never bloats the
  per-tool cache and can be replaced atomically on re-import.
