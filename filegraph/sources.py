"""Per-source definitions for ``filegraph pull``: config file and ssh argv.

A source pairs a **remote path** (what the server-side ``find`` walks) with a
**local mount path** (the root this machine queries, i.e. the index root). The
definitions live in ``$XDG_CONFIG_HOME/filegraph/sources.toml`` (``0700``
directory, ``0600`` file), because they name a remote account and a key path.

``filegraph`` never reads, prints or stores key contents: only the key *path* is
kept, and it is handed to the system ``ssh`` binary as an argument. The transport
is always a local ``ssh`` process with an argument list (never a shell string), so
the user's own agent, key and ``authorized_keys`` setup applies and the remote
command is the only string the server sees.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import tomllib
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

#: Directory under ``$XDG_CONFIG_HOME`` holding the source definitions.
CONFIG_DIR_NAME = "filegraph"

#: File name of the source definitions inside that directory.
CONFIG_FILE_NAME = "sources.toml"

#: The listing command a source falls back to. ``{path}`` is the only placeholder
#: ``filegraph`` substitutes, so a restricted ``authorized_keys`` entry can pin
#: exactly this string.
DEFAULT_REMOTE_COMMAND = "find {path} -printf '%y\t%s\t%T@\t%p\n'"

#: Placeholder replaced by the source's remote path; nothing else is substituted.
PATH_PLACEHOLDER = "{path}"

#: First line of a written config file.
_HEADER = (
    "# filegraph sources: managed by `filegraph sources add` and"
    " `filegraph sources remove`.\n"
)

#: Highest accepted TCP port.
MAX_PORT = 65535

#: Rows between progress reports when the caller does not ask for a cadence.
DEFAULT_PROGRESS_EVERY = 100_000


class SourceError(Exception):
    """A source is invalid, missing, or the config file cannot be used."""


class PullError(Exception):
    """A pull cannot be completed; ``rows_read`` says how far the stream got."""

    def __init__(self, message: str, rows_read: int = 0) -> None:
        super().__init__(message)
        self.rows_read = rows_read


@dataclass(frozen=True)
class Source:
    """One named remote tree and the local mount that serves it.

    The fields are validated on construction, but only lexically: loading a
    config never stats the mount, so a definition stays usable while the share
    is unmounted and the index keeps answering.
    """

    name: str
    host: str
    user: str
    remote_path: str
    local_path: str
    port: int | None = None
    key: str | None = None
    remote_command: str = DEFAULT_REMOTE_COMMAND

    def __post_init__(self) -> None:
        if not self.name:
            raise SourceError("source name must not be empty")
        if not self.host:
            raise SourceError("host must not be empty")
        if not self.user:
            raise SourceError("user must not be empty")
        for label, value in (
            ("remote path", self.remote_path),
            ("local path", self.local_path),
        ):
            if not value:
                raise SourceError(f"{label} must not be empty")
            if not os.path.isabs(value):
                raise SourceError(f"{label} must be absolute: {value}")
        if self.port is not None and not 0 < self.port <= MAX_PORT:
            raise SourceError(f"port out of range: {self.port}")
        if not self.remote_command:
            raise SourceError("remote command must not be empty")
        # Normalize the paths once, so prefix mapping and index roots agree.
        object.__setattr__(self, "remote_path", os.path.normpath(self.remote_path))
        object.__setattr__(self, "local_path", os.path.normpath(self.local_path))


# ---------------------------------------------------------------------------
# the config file (S2)
# ---------------------------------------------------------------------------


def config_dir() -> Path:
    """Return the directory holding ``sources.toml``."""
    configured = os.environ.get("XDG_CONFIG_HOME")
    base = Path(configured) if configured else Path.home() / ".config"
    return base / CONFIG_DIR_NAME


def config_path() -> Path:
    """Return the path of ``sources.toml``."""
    return config_dir() / CONFIG_FILE_NAME


def _detail(exc: OSError) -> str:
    return exc.strerror or str(exc)


def _string(value: str) -> str:
    """Quote a TOML basic string (JSON escapes are a subset of TOML's)."""
    return json.dumps(value, ensure_ascii=False)


def _port_value(value: object) -> int | None:
    """Read the optional ``port`` field, rejecting anything not an integer."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise SourceError(f"port must be an integer: {value}")
    return value


def _key_value(value: object) -> str | None:
    """Read the optional ``key`` field: a path, or nothing."""
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise SourceError(f"key must be a path: {value}")
    return value


def load_sources() -> dict[str, Source]:
    """Return every configured source, keyed by name, sorted by name.

    A missing file is an empty set of sources; an unreadable or malformed file is
    an error, because silently dropping a source would make ``pull`` report a
    name that exists as unknown.
    """
    path = config_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise SourceError(f"cannot read {path}: {_detail(exc)}") from exc
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise SourceError(f"invalid config {path}: {exc}") from exc
    table = document.get("sources", {})
    if not isinstance(table, dict):
        raise SourceError(f"invalid config {path}: [sources] must be a table")
    found: dict[str, Source] = {}
    for name in sorted(table):
        entry = table[name]
        if not isinstance(entry, dict):
            raise SourceError(f"invalid config {path}: [sources.{name}] must be a table")
        try:
            found[name] = Source(
                name=name,
                host=str(entry.get("host", "")),
                user=str(entry.get("user", "")),
                remote_path=str(entry.get("remote_path", "")),
                local_path=str(entry.get("local_path", "")),
                port=_port_value(entry.get("port")),
                key=_key_value(entry.get("key")),
                remote_command=str(entry.get("remote_command", DEFAULT_REMOTE_COMMAND)),
            )
        except SourceError as exc:
            raise SourceError(f"invalid source {name}: {exc}") from exc
    return found


def save_sources(entries: Mapping[str, Source]) -> None:
    """Write ``entries`` to the config file with ``0700``/``0600`` permissions.

    The file is published with ``os.replace`` so a reader never sees a partial
    config, and its mode is forced to ``0600``: the file names a remote account.
    """
    directory = config_dir()
    path = config_path()
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(directory, 0o700)
    except OSError as exc:
        raise SourceError(f"cannot create {directory}: {_detail(exc)}") from exc

    text = [_HEADER]
    for name in sorted(entries):
        source = entries[name]
        text.append(f"\n[sources.{_string(name)}]\n")
        text.append(f"host = {_string(source.host)}\n")
        text.append(f"user = {_string(source.user)}\n")
        if source.port is not None:
            text.append(f"port = {source.port}\n")
        if source.key is not None:
            text.append(f"key = {_string(source.key)}\n")
        text.append(f"remote_path = {_string(source.remote_path)}\n")
        text.append(f"local_path = {_string(source.local_path)}\n")
        text.append(f"remote_command = {_string(source.remote_command)}\n")

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".sources-", suffix=".tmp", dir=directory
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write("".join(text))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise SourceError(f"cannot write {path}: {_detail(exc)}") from exc


# ---------------------------------------------------------------------------
# reading and editing the definitions (S2)
# ---------------------------------------------------------------------------


def get_source(name: str) -> Source:
    """Return the source called ``name``."""
    entries = load_sources()
    try:
        return entries[name]
    except KeyError:
        raise SourceError(f"no such source: {name}") from None


def validate_local_path(path: str) -> None:
    """Check ``local_path`` is a directory, or one that is not mounted yet.

    A share that is unmounted right now is legitimate — that is exactly when the
    index answers — so a path that does not exist is accepted when its parent is
    an existing directory.
    """
    if os.path.isdir(path):
        return
    if os.path.exists(path):
        raise SourceError(f"local path is not a directory: {path}")
    parent = os.path.dirname(os.path.normpath(path))
    if not os.path.isdir(parent):
        raise SourceError(f"local path parent does not exist: {parent}")


def add_source(source: Source, *, force: bool = False) -> None:
    """Add ``source``, refusing to overwrite an existing name without ``force``."""
    validate_local_path(source.local_path)
    entries = load_sources()
    if source.name in entries and not force:
        raise SourceError(
            f"source already exists: {source.name} (use --force to replace)"
        )
    entries[source.name] = source
    save_sources(entries)


def remove_source(name: str) -> None:
    """Delete one source definition, or fail when it does not exist."""
    entries = load_sources()
    if name not in entries:
        raise SourceError(f"no such source: {name}")
    del entries[name]
    save_sources(entries)


# ---------------------------------------------------------------------------
# the ssh invocation (S4)
# ---------------------------------------------------------------------------


def remote_command_for(source: Source) -> str:
    """Return the remote command: the template with ``{path}`` substituted.

    Nothing else is substituted, so any other brace in the template reaches the
    server verbatim.
    """
    return source.remote_command.replace(PATH_PLACEHOLDER, source.remote_path)


def command_line(source: Source) -> list[str]:
    """Return the ``ssh`` argument list for ``source``.

    The list is executed directly, never through a shell: the destination and the
    key path are single arguments, and the remote command is one argument too.
    """
    argv = ["ssh"]
    if source.port is not None:
        argv.extend(["-p", str(source.port)])
    if source.key:
        argv.extend(["-i", source.key])
    argv.append(f"{source.user}@{source.host}")
    argv.append(remote_command_for(source))
    return argv


# ---------------------------------------------------------------------------
# the listing stream (S3, S5, S6)
# ---------------------------------------------------------------------------


def _is_under(path: str, root: str) -> bool:
    """Return True when ``path`` is ``root`` or below it (the index's rule)."""
    if path == root:
        return True
    if root == os.sep:
        return path.startswith(os.sep)
    return path.startswith(root + os.sep)


def _to_local(path: str, remote_root: str, local_root: str) -> str:
    """Rewrite an absolute ``path`` below ``remote_root`` onto ``local_root``."""
    if path == remote_root:
        return local_root
    if remote_root == os.sep:
        rest = path.lstrip(os.sep)
    else:
        rest = path[len(remote_root) + 1 :]
    return os.path.join(local_root, rest) if rest else local_root


def _mapped_line(source: Source, local_root: str, line: str) -> str:
    """Return one listing row with its path moved onto the local mount.

    A malformed row is passed through untouched: the importer counts it as a
    skipped row, exactly as it does for ``filegraph import``. A well-formed row
    outside the remote path aborts the pull, naming that path.
    """
    text = line.rstrip("\n")
    if text.endswith("\r"):
        text = text[:-1]
    fields = text.split("\t", 3)
    if len(fields) != 4:
        return text + "\n"
    letter, size, mtime, raw_path = fields
    path = os.path.normpath(raw_path)
    if not _is_under(path, source.remote_path):
        raise PullError(f"path outside remote path {source.remote_path}: {raw_path}")
    local = _to_local(path, source.remote_path, local_root)
    return f"{letter}\t{size}\t{mtime}\t{local}\n"


def _exit_message(status: int, rows: int) -> str:
    """Describe a failed ``ssh`` exit without losing the row count."""
    if status < 0:
        return f"ssh terminated by signal {-status} after {rows} row(s)"
    return f"ssh exited with status {status} after {rows} row(s)"


def _reap(process: subprocess.Popen) -> None:
    """Close our end of the pipe and make sure no ``ssh`` process is left behind."""
    stream = process.stdout
    if stream is not None and not stream.closed:
        try:
            stream.close()
        except OSError:
            pass
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def iter_listing(
    source: Source,
    local_root: str,
    *,
    progress: Callable[[int, float], None] | None = None,
    progress_every: int = 0,
) -> Iterator[str]:
    """Stream ``source``'s listing as rows the importer can read for ``local_root``.

    Rows arrive from a local ``ssh`` process and are rewritten one at a time, so
    the listing is never held in memory. The stream is only complete when
    ``ssh`` itself succeeded: a non-zero exit, a killed ``ssh`` or an empty
    listing raises :class:`PullError` (carrying the rows that did arrive) instead
    of ending the iteration, so the importer never publishes a partial or empty
    index over the previous one.
    """
    argv = command_line(source)
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
        )
    except OSError as exc:
        raise PullError(f"cannot run ssh: {_detail(exc)}") from exc

    rows = 0
    try:
        stream = process.stdout
        if stream is None:  # pragma: no cover - stdout=PIPE always provides one
            raise PullError("ssh provided no stream")
        for line in stream:
            rows += 1
            mapped = _mapped_line(source, local_root, line)
            if progress is not None and progress_every > 0 and rows % progress_every == 0:
                progress(rows, time.monotonic() - started)
            yield mapped
        status = process.wait()
        if status != 0:
            raise PullError(_exit_message(status, rows), rows)
        if rows == 0:
            raise PullError("ssh produced no rows (0 row(s) arrived)", 0)
    except OSError as exc:
        raise PullError(
            f"ssh stream failed after {rows} row(s): {_detail(exc)}", rows
        ) from exc
    finally:
        _reap(process)
