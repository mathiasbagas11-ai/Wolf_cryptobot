"""Atomic, thread-safe JSON state store.

The previous bot read and wrote ~30 JSON files directly from many call sites,
which risked race conditions (the scheduler runs jobs concurrently) and file
corruption if the process crashed mid-write. This module centralises all state
access behind one small abstraction:

* **Atomic writes** — data is written to a ``*.tmp`` file then ``os.replace``-d
  over the target, which is atomic on POSIX and Windows. A crash can never leave
  a half-written, unparseable file.
* **Per-file locking** — a re-entrant lock per logical key serialises concurrent
  readers/writers within the process so the scheduler's overlapping jobs cannot
  interleave a read-modify-write.
* **Corruption tolerance** — a malformed file is logged and treated as the
  default value rather than crashing the caller.

Callers never touch ``open()`` or ``json`` directly; they go through a single
:class:`StateStore` instance, making persistence behaviour easy to reason about
and to swap out (e.g. for SQLite or Supabase) later.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import defaultdict
from typing import Any

log = logging.getLogger("wolf.state")


class StateStore:
    """A directory of atomically-written JSON documents keyed by name."""

    def __init__(self, base_dir: str) -> None:
        self._base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)
        self._global_lock = threading.Lock()
        self._locks: dict[str, threading.RLock] = defaultdict(threading.RLock)

    def _path(self, key: str) -> str:
        if not key or "/" in key or "\\" in key or key.startswith("."):
            raise ValueError(f"Invalid state key: {key!r}")
        return os.path.join(self._base_dir, f"{key}.json")

    def _lock_for(self, key: str) -> threading.RLock:
        with self._global_lock:
            return self._locks[key]

    def read(self, key: str, default: Any = None) -> Any:
        """Read ``key``; return ``default`` if missing or corrupt."""
        path = self._path(key)
        with self._lock_for(key):
            if not os.path.exists(path):
                return default
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    return json.load(fh)
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("State '%s' unreadable (%s); using default", key, exc)
                return default

    def write(self, key: str, data: Any) -> None:
        """Atomically persist ``data`` under ``key``."""
        path = self._path(key)
        tmp = f"{path}.tmp"
        with self._lock_for(key):
            try:
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=2, default=str)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, path)  # atomic swap
            except OSError as exc:
                log.error("Failed to persist state '%s': %s", key, exc)
                # Best-effort cleanup of the temp file.
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                raise

    def update(self, key: str, mutator, default: Any = None) -> Any:
        """Atomic read-modify-write.

        ``mutator`` receives the current value (or ``default``) and returns the
        new value to persist. The whole operation holds the per-key lock so
        concurrent callers cannot clobber one another.
        """
        with self._lock_for(key):
            current = self.read(key, default)
            updated = mutator(current)
            self.write(key, updated)
            return updated

    def exists(self, key: str) -> bool:
        return os.path.exists(self._path(key))
