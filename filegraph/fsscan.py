"""Filesystem inspection helpers: mount kind detection and errno-shaped errors.

Two jobs, both about not lying to the caller:

* decide whether the scanned root lives on a network filesystem, so the scan
  budget can be adaptive (network roots are bounded, local roots are not);
* report filesystem failures with the path and the errno instead of letting
  ``pathlib`` flatten ``EACCES``/``ESTALE``/``ENOTCONN``/``EIO`` into a negative
  answer such as "not a directory".
"""

from __future__ import annotations

import os
import stat as stat_module
from pathlib import Path

#: Mount types that behave like a network filesystem: a syscall may block for
#: minutes or fail as a whole instead of per entry.
NETWORK_FS_TYPES: frozenset[str] = frozenset(
    {"cifs", "smb3", "nfs", "nfs4", "sshfs", "fuse.sshfs", "9p", "afs"}
)

#: Where the kernel lists the current mount namespace. Missing on non-Linux.
MOUNTINFO_PATH: Path = Path("/proc/self/mountinfo")

_OCTAL_ESCAPES = frozenset("01234567")


def _unescape(field: str) -> str:
    """Decode the octal escapes the kernel uses in mountinfo paths."""
    if "\\" not in field:
        return field
    out: list[str] = []
    i = 0
    while i < len(field):
        char = field[i]
        if char == "\\" and i + 3 < len(field) and set(field[i + 1 : i + 4]) <= _OCTAL_ESCAPES:
            out.append(chr(int(field[i + 1 : i + 4], 8)))
            i += 4
            continue
        out.append(char)
        i += 1
    return "".join(out)


def parse_mountinfo(text: str) -> list[tuple[str, str]]:
    """Return ``(mount_point, filesystem_type)`` pairs from mountinfo text.

    Unparsable lines are skipped: a partial mount table still tells us what it
    can, and the caller treats an unknown root as local.
    """
    mounts: list[tuple[str, str]] = []
    for line in text.splitlines():
        fields = line.split()
        try:
            separator = fields.index("-")
        except ValueError:
            continue
        if separator + 1 >= len(fields) or len(fields) < 5:
            continue
        mounts.append((_unescape(fields[4]), fields[separator + 1]))
    return mounts


def mount_fstype(path: Path | str) -> str | None:
    """Return the filesystem type of the mount holding ``path``.

    Uses a longest-prefix match over ``MOUNTINFO_PATH``. Returns ``None`` when
    the mount cannot be determined (no mountinfo, non-Linux, unreadable or
    unparsable table).
    """
    try:
        text = Path(MOUNTINFO_PATH).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    resolved = os.path.realpath(os.fspath(path))
    best: str | None = None
    best_length = -1
    for mount_point, fstype in parse_mountinfo(text):
        if not _is_within(resolved, mount_point):
            continue
        if len(mount_point) > best_length:
            best = fstype
            best_length = len(mount_point)
    return best


def _is_within(path: str, mount_point: str) -> bool:
    """Return True when ``path`` is ``mount_point`` or below it."""
    if mount_point == "/":
        return path.startswith("/")
    if path == mount_point:
        return True
    prefix = mount_point if mount_point.endswith("/") else f"{mount_point}/"
    return path.startswith(prefix)


def is_network_path(path: Path | str) -> bool:
    """Return True only when ``path`` is known to live on a network filesystem.

    Anything undetermined is treated as local, so an unknown platform never
    truncates a scan behind the caller's back.
    """
    return mount_fstype(path) in NETWORK_FS_TYPES


# ---------------------------------------------------------------------------
# errno-shaped reporting
# ---------------------------------------------------------------------------


def error_detail(exc: OSError) -> str:
    """Return ``"<strerror> (errno <n>)"`` for a filesystem exception."""
    strerror = exc.strerror or "Filesystem error"
    if exc.errno is None:
        return strerror
    return f"{strerror} (errno {exc.errno})"


def fs_error(path: Path | str, exc: OSError) -> str:
    """Return an error message naming both the path and the errno."""
    return f"{error_detail(exc)}: {path}"


def stat_path(path: Path | str) -> os.stat_result:
    """``os.stat`` behind a seam that tests can make fail like a dead mount."""
    return os.stat(path)


def lstat_path(path: Path | str) -> os.stat_result:
    """``os.lstat`` behind a seam that tests can make fail like a dead mount."""
    return os.lstat(path)


def path_kind(path: Path | str) -> str:
    """Return ``'directory'``, ``'file'`` or ``'other'`` for ``path``.

    Unlike ``Path.is_dir()``/``Path.exists()``, filesystem failures propagate
    with their real errno instead of being reported as a negative answer.
    """
    st = stat_path(path)
    if stat_module.S_ISDIR(st.st_mode):
        return "directory"
    if stat_module.S_ISREG(st.st_mode):
        return "file"
    return "other"
