from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from threading import Lock


_registry_guard = Lock()
_locks: dict[tuple[str, object], tuple[Lock, int]] = {}


@contextmanager
def resource_lock(namespace: str, key: object) -> Iterator[None]:
    """Serialize one resource inside a process without leaking lock entries.

    PostgreSQL callers still take a database row lock for cross-process safety.
    This lock supplies deterministic SQLite semantics and avoids duplicate local
    filesystem work.
    """

    resource_key = (namespace, key)
    with _registry_guard:
        lock, references = _locks.get(resource_key, (Lock(), 0))
        _locks[resource_key] = (lock, references + 1)

    try:
        with lock:
            yield
    finally:
        with _registry_guard:
            current_lock, references = _locks[resource_key]
            if references == 1:
                del _locks[resource_key]
            else:
                _locks[resource_key] = (current_lock, references - 1)
