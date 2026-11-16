"""Scan budgets: network-aware defaults, a hard deadline, truncation markers.

Two budgets bound a scan: a wall-clock deadline and an entry cap. Both are
cooperative inside the scanning loop, which is enough for the entry cap but not
for the deadline: a Python-side check cannot interrupt a ``scandir``/``stat``
that is blocked in the kernel. So a scan that carries a deadline runs in a
separate, killable process that streams what it discovered back over a pipe;
when the deadline expires the parent kills it and returns those partial results
with a ``truncated`` marker.
"""

from __future__ import annotations

import multiprocessing
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from filegraph import fsscan

#: Default wall-clock deadline for a scan whose root is on a network mount.
NETWORK_DEFAULT_MAX_SECONDS: float = 45.0

#: Default entry cap for a scan whose root is on a network mount.
NETWORK_DEFAULT_MAX_ENTRIES: int = 200_000

TIME_BUDGET = "time_budget"
ENTRY_BUDGET = "entry_budget"

#: How often the child streams its progress, in seconds.
PROGRESS_INTERVAL = 0.25

#: How long to wait for a killed child to disappear. It can be stuck in an
#: uninterruptible syscall, and the tool must not wait for that.
_KILL_GRACE_SECONDS = 1.0


def make_truncated(reason: str, limit: float | int | None, entries_seen: int) -> dict:
    """Build the sibling ``truncated`` marker of a partial result."""
    return {"reason": reason, "limit": limit, "entries_seen": entries_seen}


def resolve_budget(
    root: Path | str,
    max_seconds: float | None = None,
    max_entries: int | None = None,
) -> tuple[float | None, int | None]:
    """Resolve the effective budgets for a scanned root.

    Caller values always win. Without caller input, a network root gets the
    documented defaults and a local root gets no budget at all, so local scans
    keep today's behaviour.
    """
    if fsscan.is_network_path(root):
        if max_seconds is None:
            max_seconds = NETWORK_DEFAULT_MAX_SECONDS
        if max_entries is None:
            max_entries = NETWORK_DEFAULT_MAX_ENTRIES
    return max_seconds, max_entries


@dataclass
class Budget:
    """Bookkeeping for one scan: entry cap, deadline, warnings, progress.

    Args:
        max_seconds: Wall-clock budget in seconds, or None for no deadline.
        max_entries: Entry cap, or None for no cap.
        deadline: Absolute ``time.monotonic()`` deadline. Derived from
            ``max_seconds`` when not given, and passed to a child process so it
            checks the same instant as its parent.
        on_partial: Callback receiving streamed partial payloads.
        on_warning: Callback receiving each warning as it is recorded.
    """

    max_seconds: float | None = None
    max_entries: int | None = None
    deadline: float | None = None
    started: float = field(default_factory=time.monotonic)
    entries_seen: int = 0
    truncated: dict | None = None
    warnings: list[str] = field(default_factory=list)
    on_partial: Callable[[dict], None] | None = None
    on_warning: Callable[[str], None] | None = None
    _last_tick: float = field(default=-1.0)

    def __post_init__(self) -> None:
        if self.deadline is None and self.max_seconds is not None:
            self.deadline = self.started + self.max_seconds

    # -- reporting ---------------------------------------------------------
    def warn(self, text: str) -> None:
        """Record a warning and stream it to the caller's process."""
        self.warnings.append(text)
        if self.on_warning is not None:
            self.on_warning(text)

    def emit(self, payload: dict) -> None:
        """Stream a partial payload to the caller's process."""
        if self.on_partial is not None:
            self.on_partial(payload)

    # -- budget checks -----------------------------------------------------
    def remaining(self) -> float | None:
        """Seconds left before the deadline, or None without a deadline."""
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - time.monotonic())

    def tripped(self) -> bool:
        """Return True once a budget is hit, recording why the first time."""
        if self.truncated is not None:
            return True
        if self.max_entries is not None and self.entries_seen >= self.max_entries:
            self.truncated = make_truncated(
                ENTRY_BUDGET, self.max_entries, self.entries_seen
            )
            return True
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self.truncated = make_truncated(
                TIME_BUDGET, self.max_seconds, self.entries_seen
            )
            return True
        return False

    def count_entry(self, payload: dict | None = None) -> bool:
        """Count one directory entry; return False when the scan must stop."""
        if self.tripped():
            return False
        self.entries_seen += 1
        self._tick()
        if payload is not None:
            self.emit(payload)
        return not self.tripped()

    def _tick(self) -> None:
        if self.on_partial is None:
            return
        now = time.monotonic()
        if self.entries_seen == 1 or now - self._last_tick >= PROGRESS_INTERVAL:
            self._last_tick = now
            self.emit({"kind": "progress", "entries_seen": self.entries_seen})


# ---------------------------------------------------------------------------
# killable worker process
# ---------------------------------------------------------------------------


class WorkerSession:
    """The child-process side: streams progress to the waiting parent."""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def emit(self, payload: dict) -> None:
        self._send(("partial", payload))

    def warn(self, text: str) -> None:
        self._send(("warn", text))

    def done(self, result: dict) -> None:
        self._send(("done", result))

    def failed(self, exc: BaseException) -> None:
        self._send(("error", f"{type(exc).__name__}: {exc}"))

    def _send(self, message: tuple[str, object]) -> None:
        try:
            self._connection.send(message)
        except (BrokenPipeError, OSError, ValueError):
            # The parent stopped listening (deadline) — nothing left to report.
            pass


@dataclass
class WorkerOutcome:
    """What the parent kept from a worker run."""

    result: dict | None = None
    partials: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    entries_seen: int = 0
    error: str | None = None
    timed_out: bool = False


class _Collector:
    """Drains worker messages on a background thread while the parent waits."""

    def __init__(self) -> None:
        self.result: dict | None = None
        self.partials: list[dict] = []
        self.warnings: list[str] = []
        self.error: str | None = None
        self.progress_entries = 0
        self.entry_payloads = 0

    def run(self, connection: Any) -> None:
        while True:
            try:
                message = connection.recv()
            except Exception:
                # EOF after a kill, or a truncated frame: keep what arrived.
                return
            if not isinstance(message, tuple) or len(message) != 2:
                continue
            kind, payload = message
            if kind == "partial" and isinstance(payload, dict):
                if payload.get("kind") == "progress":
                    self.progress_entries = max(
                        self.progress_entries, int(payload.get("entries_seen") or 0)
                    )
                else:
                    self.partials.append(payload)
                    if payload.get("kind") == "entry":
                        self.entry_payloads += 1
            elif kind == "warn":
                self.warnings.append(str(payload))
            elif kind == "done" and isinstance(payload, dict):
                self.result = payload
            elif kind == "error":
                self.error = str(payload)


def _worker_entry(connection: Any, work: Callable[..., dict], args: Sequence[Any]) -> None:
    """Child process body: run the scan and report back, then exit."""
    session = WorkerSession(connection)
    try:
        session.done(work(session, *args))
    except BaseException as exc:  # never leave the parent waiting on a crash
        session.failed(exc)
    finally:
        try:
            connection.close()
        except (OSError, ValueError):
            pass


def _worker_context():
    """Fork where available: the child inherits the caller's module state."""
    methods = multiprocessing.get_all_start_methods()
    return multiprocessing.get_context("fork" if "fork" in methods else None)


def run_worker(
    work: Callable[..., dict],
    args: Sequence[Any] = (),
    *,
    timeout: float,
) -> WorkerOutcome:
    """Run ``work(session, *args)`` in a child process killable at ``timeout``."""
    context = _worker_context()
    recv_connection, send_connection = context.Pipe(duplex=False)
    try:
        process = context.Process(
            target=_worker_entry, args=(send_connection, work, tuple(args)), daemon=True
        )
        process.start()
    finally:
        send_connection.close()

    collector = _Collector()
    reader = threading.Thread(target=collector.run, args=(recv_connection,), daemon=True)
    reader.start()

    process.join(max(0.0, timeout))
    timed_out = process.is_alive()
    if timed_out:
        process.kill()
        process.join(_KILL_GRACE_SECONDS)
    reader.join(_KILL_GRACE_SECONDS)
    try:
        recv_connection.close()
    except (OSError, ValueError):
        pass

    return WorkerOutcome(
        result=collector.result,
        partials=collector.partials,
        warnings=collector.warnings,
        entries_seen=max(collector.progress_entries, collector.entry_payloads),
        error=collector.error,
        timed_out=timed_out,
    )


def run_budgeted(
    work: Callable[..., dict],
    assemble: Callable[[list[dict], list[str]], dict],
    budget: Budget,
    *,
    args: Sequence[Any] = (),
) -> dict:
    """Run a scan under ``budget``, hard-stopping it at the deadline.

    Without a deadline the scan runs in-process: nothing changes for local
    roots. With a deadline it runs in a killable child, and a stopped scan
    returns the partial results plus a ``truncated`` marker.
    """
    if budget.deadline is None:
        return work(None, *args)

    timeout = budget.remaining()
    outcome = run_worker(work, args, timeout=0.0 if timeout is None else timeout)

    if outcome.result is not None:
        return outcome.result

    body = assemble(outcome.partials, outcome.warnings)
    if outcome.timed_out:
        body["truncated"] = make_truncated(
            TIME_BUDGET, budget.max_seconds, outcome.entries_seen
        )
    else:
        body.setdefault("truncated", None)
        body["error"] = outcome.error or "Scan worker exited without a result"
    return body
