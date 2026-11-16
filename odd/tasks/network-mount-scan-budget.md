# Network-mount scan budget

Make `filegraph` scanning survive slow or unreliable network mounts: bounded, interruptible,
honest about what it did not read. Scope decision: option A (bounded and honest scanning) with an
adaptive default — the budget applies by default only on network filesystems, so local scans keep
today's contract.

## Specs

- **S1** — A scan whose root is on a network filesystem never runs unbounded. With no explicit
  budget from the caller, it applies a default wall-clock deadline and a default entry cap.
- **S2** — A scan whose root is on a local filesystem keeps today's behaviour exactly: `max_depth`
  defaults to `None`, no deadline, no entry cap, same result shape.
- **S3** — When a budget stops a scan, the result is partial and says so: a `truncated` marker with
  the reason (`time_budget` or `entry_budget`), the counts reached, and the budget that was hit.
  A partial tree is never returned as if it were complete.
- **S4** — A syscall that never returns must not hang the tool: the deadline holds even while a
  single `scandir`/`stat` is blocked in the kernel, and the tool returns by then.
- **S5** — Filesystem errors are never reported as absence. `EACCES`, `ESTALE`, `ENOTCONN`, `EIO`
  and friends produce an explicit error carrying the path and the errno, not
  `{"error": "Path not found: …"}` and not `{"error": "Expected a directory: …"}`. An error on one
  entry during recursion is recorded in `warnings` and the scan continues.
- **S6** — Directory read failures during the `os.walk`-based tools are recorded (`onerror`) and
  surfaced in `warnings` instead of being silently skipped.
- **S7** — New options are documented where the tool is described: tool docstrings/schema, the
  README setup and usage sections, and any help text naming the defaults. Naming the defaults is
  part of the behaviour, not an afterthought: "bounded" with an unstated number is not usable.
- **S8** — Verification example, quoted from the report: "la ruta exacta esta en
  /home/sistemas/Documentos/mtn/escaneos-pacientes" — a default scan of that mount root returns
  within the default deadline, reports `truncated`, and does not leave `.filegraph/` behind on the
  share. Scanning one patient folder there returns a complete (non-truncated) result.
- **S9** — On a network-filesystem root, the persistent cache is written to a local directory
  outside the scanned tree, keyed by the resolved scanned path. Nothing is written into the share:
  no `.filegraph/`, no SQLite database, no WAL files on SMB.
- **S10** — A local-filesystem root keeps the current cache location and behaviour exactly
  (`<scanned>/.filegraph/cache.db`, same schema, same keys, same freshness rules).
- **S11** — A truncated scan is never served later as if it were a complete cached result. It is
  not persisted (decision L11), and a caller that retries gets a new bounded scan.
- **S12** — On a network-filesystem root, cache freshness is bounded by the same rule as a scan
  (S1): if freshness cannot be established within the budget, the entry is treated as stale and a
  bounded live scan runs. A second call must not be able to hang where the first one returned.

Out of scope for this feature (explicitly): removing the full re-walk in `check_freshness` for
**local** roots, and reducing the per-entry `stat` count further. Those stay open as option C.

## Tasks

| ID | Specs | Route | Outcome |
| --- | --- | --- | --- |
| T1 | S1, S3 | writer | Budget plumbing: `max_seconds` / `max_entries` on the scanning tools, defaults resolved per filesystem kind, `truncated` marker in the result |
| T2 | S1, S2 | writer | Network-filesystem detection for the scanned root (mountinfo longest-prefix match; unknown ⇒ local), so only network roots get defaults |
| T3 | S4 | writer | Hard deadline that survives a blocked syscall; the tool returns at the deadline with whatever it had |
| T4 | S5, S6 | writer | Explicit errno errors instead of "not found"/"unknown"; `onerror` for the walk-based tools and per-entry warnings |
| T5 | S7 | parent | Docs: tool docstrings, README usage + limits section, defaults stated |
| T6 | S8 | parent | Verify on the real CIFS mount with defaults |
| T7 | S9, S10 | writer | Cache location: network roots store locally (`$XDG_CACHE_HOME/filegraph` fallback `~/.cache/filegraph`) keyed by the resolved path; local roots unchanged |
| T8 | S11 | writer | A truncated result is not persisted |
| T9 | S12 | writer | Freshness for a network root is bounded; on timeout the entry is stale and a bounded live scan runs |
| T10 | S3, S4 | writer | Announce an entry before classifying it, so one blocking `lstat` cannot swallow the whole partial result |
| T11 | S3, S4 | writer | Stream names during enumeration instead of after the listing returns, so a killed scan returns what it reached |

## Slices

The ~400-line review slice target was blown by this feature and is recorded here instead of being
silently ignored: the behaviour is one coupled change (budget + detection + cache + streaming), and
splitting it further would have produced red intermediate commits. Two slices were still separated
by layer where both states were verifiably green:

- `5a7cc14` — scanner layer (T1-T4). Verified standalone by reverting the cache layer to `HEAD`: 173 passed.
- `d4097be` — cache layer (T7-T9). 179 passed.
- `e4fd908` — the empty-partial fix (T10+T11), found by T6 and shipped as its own slice: 183 passed.
- docs (T5) — README plus this document.

Delivery follows the fork-plus-PR route already used for the installer fix: the human's fork
`Nahufcdn/filegraph` receives the branch, and the PR is opened against `nahuelhollmann109/filegraph`
for approval from the other account. Push and PR creation are the human's decision, not automatic.


## Log

- **L1** (user, verbatim): "tengo esta ruta montada con cfis y con permisos de 1000
  pero el mcp no lo lee  este tipo de rutas montadas
  investguemos en internet y en context7 por mcp para ver si existe alguna referencia de esto"
- **L2** (user, verbatim): "la ruta exacta esta en   /home/sistemas/Documentos/mtn/escaneos-pacientes
  el sintoma es que queda analizando de manera infinita y nunca para
  pero esto no lo probe con la nueva version que detacta los errores
  creo que puede ser algo de los permisos del mcp"
- **L3** (evidence, 2026-11-16): permissions and mount namespace are NOT the cause. The mount is
  `cifs //10.0.0.114/escaneos_pacientes` with `uid=1000,forceuid,gid=1000,forcegid,file_mode=0755,
  dir_mode=0755,vers=3.1.1,soft,nounix,serverino,mapposix,actimeo=1`; the MCP runs as uid 1000 (the
  mount owner), `stat` on the mount root returns in ~0 ms, and the server process sees the mount in
  its own `/proc/<pid>/mountinfo`. `<mount>/.filegraph` does not exist: no scan has ever finished.
- **L4** (evidence, 2026-11-16): cost profile on the live share — mount root 200 entries in 0.64 s
  (198 directories), patient directories 2–15 entries in 0.5–0.7 s each, and at least one directory
  blocked for >50 s inside a single syscall (a capped probe with 10 s per-directory checks still
  died at a 60 s deadline, because a Python-side check cannot interrupt a blocking C call).
- **L5** (code, 2026-11-16): `scanner.py:55` `os.walk` without `onerror`; `scanner.py:122`
  `iterdir()` plus per-entry `is_symlink`/`is_file`/`is_dir`; `max_depth` defaults to `None`
  (`scanner.py:45`, `tools.py:30`); no entry/byte/time cap and no `timeout`/`signal`/`Thread`
  anywhere in the package; `_scan_node`'s root catch is `PermissionError`-only; `pathlib`
  `exists`/`is_dir` swallow EACCES/ESTALE/EIO as `False`, which is how a mount failure reaches the
  caller as "not found".
- **L6** (decision, user): scope = option A, "A — Escaneo acotado y honesto" — budgets, `onerror`,
  an explicit default for depth, progress and partial results, and errno-shaped errors.
- **L7** (decision, user): the budget is **adaptive** — defaults apply only on network
  filesystems detected for the scanned root; local scans do not change at all.
- **L8** (decision, parent): defaults are `max_seconds=45` and `max_entries=200_000` for network
  roots; `max_depth` stays `None` in both cases, because a bounded default there would change
  results for deep local trees and is a contract change nobody asked for. Detection uses
  `/proc/self/mountinfo` with a longest-prefix match; when it cannot be determined, the root is
  treated as local (no budget) so an unknown platform never silently truncates.
- **L9** (decision, parent): `truncated` is a sibling key of `tree`/`results`, shaped
  `{"reason": "time_budget"|"entry_budget", "limit": <number>, "entries_seen": <number>}`, and is
  `None` for a complete scan. Callers that ignore it keep working; callers that read it can tell a
  partial answer from a complete one.
- **L10** (decision, user): the cache moves out of the share (option B, "B — Cache local, fuera del
  share"): local cache directory keyed by the resolved scanned path, never WAL over SMB. This
  supersedes the earlier "out of scope" note for the cache and pulls it into this feature as T7-T9.
- **L11** (decision, parent): a truncated result is **not** persisted, and `check_freshness` on a
  network root is bounded by the same budget; a freshness check that cannot finish is treated as
  stale, never as fresh. Rationale: caching a partial tree is worse than re-scanning, and an
  unbounded freshness walk would reintroduce the original hang on the second call.
- **L12** (evidence, 2026-11-16, parent): after T1-T4 the full suite is 173 passed (baseline 150),
  `/home/sistemas/Documentos/mtn/escaneos-pacientes` resolves to `fstype=cifs`, `network=True`,
  `budget=(45.0, 200000)`, the checkout resolves to `ext4`/`(None, None)`, and the share still has
  no `.filegraph/`.
- **L13** (evidence, 2026-11-16, parent): the first T6 run FAILED. The budget held (45.5 s, then
  45.5 s again on the second call, nothing written into the share, patient folder complete in 9.3 s)
  but the truncated result was EMPTY: `entries_seen: 0`, `children: 0`. Same at `max_seconds=3` and
  `=10`. The check found a real defect, which is what it was for.
- **L14** (evidence, 2026-11-16, parent): the earlier cost figure in L4 was wrong because the probe
  capped at 200. A full count of the mount root: **>20,000 direct entries**, `os.scandir` yielding
  ~660 entries/s, still arriving at the 30 s mark. Listing that single directory takes minutes, with
  no `stat` involved. A capped probe's count is never the size.
- **L15** (evidence, 2026-11-16, parent): after T10+T11, T6 passes. Mount root with defaults:
  `elapsed=45.7 s`, `truncated={reason: time_budget, limit: 45.0, entries_seen: 30502}`,
  `children=30502` (all `unknown`, because their metadata was never read — the honest partial),
  `<share>/.filegraph` absent, and one patient folder complete in 9.7 s with `truncated: null`.
  Suite 183 passed.
