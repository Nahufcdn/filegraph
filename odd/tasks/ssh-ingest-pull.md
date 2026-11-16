# SSH ingest (`filegraph pull`)

Fetch a tree listing from a machine that owns the data, over SSH, and publish it as the index for the
**local mount path** the MCP will query. The index and its import already exist; this adds the
transport and the per-source definition that ties a remote path to a local mount path.

The mount is not trusted for the transfer: the CIFS client on this PC cannot even read 64 KB without
stalling in D state, so the listing travels over the SSH channel (streamed to the PC, nothing written
on the server) and never through the mounted filesystem.

## Specs

- **S1** — `filegraph pull --source <name>` streams a listing from the remote host and imports it as
  the index of that source's **local mount path**. A source is identified by name and holds: `host`,
  `user`, optional `port` and `key`, the **remote path** the server-side `find` walks, and the
  **local mount path** that the MCP will query (which is also the index root).
- **S2** — `filegraph sources add <name> --host --user --remote-path --local-path [--port] [--key]`
  validates that both paths are absolute and that local-path is a directory-or-not-yet-existing but
  under an existing parent, then writes `~/.config/filegraph/sources.toml` with mode `0600`
  (creating the directory with `0700`). `sources list` prints name, host, user, remote path and local
  path (never key material), and `sources remove <name>` deletes one entry. Adding an existing name
  requires `--force` instead of silently overwriting.
- **S3** — Path mapping is explicit and strict: every listing row whose path starts with the remote
  path is rewritten to the local equivalent before import. A single row outside the remote path
  aborts the pull with a non-zero exit naming the first offending path, and the previous index stays
  intact.
- **S4** — The remote command is a per-source template with a `{path}` placeholder, defaulting to
  `find {path} -printf '%y\t%s\t%T@\t%p\n'`, so a restricted `authorized_keys` entry can pin exactly
  that command. `filegraph` never reads, prints or stores key contents: it passes the configured key
  path to `ssh` and prints the invoked command line only under `--verbose`, with the path to the key
  shown verbatim (it is a path, not a secret) and never any credential.
- **S5** — A failed or truncated stream (ssh exit status non-zero, broken pipe, zero rows) leaves the
  previous index untouched and exits non-zero, reporting how many rows arrived before the failure.
- **S6** — A large listing is processed as a stream: memory stays bounded (the import already streams)
  and progress is reported for every N rows with rows read, entries/second and elapsed time, plus a
  final report of counts per type and total bytes.
- **S7** — CLI only: no MCP tool is added for pulling, and no MCP tool ever opens an SSH connection.
  `pull --root <local>` overrides the configured local mount path for one run.
- **S8** — Docs: README section that states the security model (`restrict,command=`, read-only user,
  no shell) and ships the exact wrapper script and the `authorized_keys` line, plus the pull/refresh
  workflow.

## Tasks

| ID | Specs | Route |
| --- | --- | --- |
| T1 | S1, S4 | writer — source model, config load/save, `pull` building the ssh invocation |
| T2 | S2 | writer — `sources add/list/remove`, permissions, validation |
| T3 | S3, S5 | writer — prefix mapping, abort semantics, previous index preserved |
| T4 | S6 | writer — streaming progress reporting and final report |
| T5 | S7 | writer — no MCP surface, `--root` override |
| T6 | S8 | parent — README security section and the wrapper script |

## Log

- **L1** (user, verbatim): "y ver la opcion de meter un ssh para carpetas motadas dentro del mcp?"
- **L2** (user, verbatim): "seria mejor la opcion B
  pero con varias opciones mas
  primero si vamos a alizar por ssh y la ruta esta montada en local para despues consumirla hay que
  pedir la ruta del mount local
  y esta consulta se puede hacer por cli
  la seguridad esta perfecto como la planteas"
- **L3** (decision, user): option B (SSH ingest) with the local mount path as a mandatory part of each
  source; the pull is a CLI command, not an MCP tool; the security model of the previous message is
  approved as written (`restrict,command=`, read-only user, no shell, key never touched by filegraph).
- **L4** (evidence, 2026-11-16): the CIFS client on this PC is unusable for data transfer — a 64 KB
  read did not complete inside a 30 s cap and stalled in D state — so the listing must travel over
  SSH and the mount is only for querying after import. The remote host is a TrueNAS SCALE box (API
  v2.0 answers 401, SMB negotiates 3.1.1, GNU `find` available), so `-printf` is available
  server-side.
- **L5** (decision, parent): the remote command stays a per-source template instead of being
  hardcoded, because the restricted `authorized_keys` entry pins one exact command string and the
  user may name their wrapper differently.
