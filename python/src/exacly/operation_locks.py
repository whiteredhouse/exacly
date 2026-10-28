"""Ordered directory locks for local operations touching both a ledger and state.

All multi-directory operations use device/inode order and deduplicate aliases. Individual ledger and
observer operations already lock these same directory inodes. Callers keep the locks through their
durable commits and must not nest another public operation that acquires the same directory lock.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from exacly import execution_store as store
from exacly import state_writer

try:
    import fcntl
except ImportError:  # pragma: no cover - local coordination requires POSIX locks.
    fcntl = None  # type: ignore[assignment]


@contextmanager
def directories(
    paths: Iterable[Path], *, write: bool = True, deadline: store.Deadline | None = None
) -> Iterator[dict[Path, int]]:
    """Lock existing real directories once, in one global order, sharing a wait budget."""
    store.require(os.name == "posix" and fcntl is not None, "local operations require POSIX directory locks")
    budget = deadline if deadline is not None else store.Deadline()
    selected: dict[tuple[int, int], int] = {}
    identities: dict[Path, tuple[int, int]] = {}
    with ExitStack() as stack:
        try:
            for given in paths:
                path = given.absolute()
                if path in identities:
                    continue
                descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                stack.callback(os.close, descriptor)
                info = os.fstat(descriptor)
                identity = (info.st_dev, info.st_ino)
                identities[path] = identity
                selected.setdefault(identity, descriptor)
            for identity in sorted(selected):
                descriptor = selected[identity]
                while True:
                    try:
                        fcntl.flock(descriptor, (fcntl.LOCK_EX if write else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                        break
                    except (BlockingIOError, InterruptedError):
                        budget.check()
                        time.sleep(min(store.LOCK_RETRY_INTERVAL, budget.remaining()))
            for path, identity in identities.items():
                current = path.stat(follow_symlinks=False)
                store.require((current.st_dev, current.st_ino) == identity, "locked operation directory changed")
        except OSError as error:
            raise store.StoreError("cannot lock local operation directories") from error
        yield {path: selected[identity] for path, identity in identities.items()}


@contextmanager
def locked(
    ledger: Path, snapshot: state_writer.Snapshot, *, deadline: store.Deadline | None = None
) -> Iterator[tuple[Path, int]]:
    """Hold ledger and current-state directory locks together without duplicate acquisition."""
    absolute = store.location(ledger)
    store.require(snapshot.content is not None, "completion requires an existing state file")
    protected = {str(absolute) + suffix for suffix in ("", "-journal", "-wal", "-shm")}
    store.require(str(snapshot.path) not in protected, "state destination overlaps the execution ledger")
    with directories((absolute.parent, snapshot.path.parent), deadline=deadline) as pinned:
        store.require(store.location(ledger) == absolute, "execution store path changed")
        parent_fd = pinned[snapshot.path.parent]
        state_writer.check_parent(snapshot, parent_fd)
        yield absolute, parent_fd
