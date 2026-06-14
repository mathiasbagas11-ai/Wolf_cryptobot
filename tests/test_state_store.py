"""Tests for the atomic state store."""

from __future__ import annotations

import threading

import pytest


def test_write_read_roundtrip(store):
    store.write("foo", {"a": 1, "b": [1, 2, 3]})
    assert store.read("foo") == {"a": 1, "b": [1, 2, 3]}


def test_read_missing_returns_default(store):
    assert store.read("nope", default=[]) == []
    assert store.read("nope") is None


def test_corrupt_file_returns_default(store, tmp_path):
    # Write garbage directly to the backing file.
    path = store._path("bad")  # noqa: SLF001 - testing internals deliberately
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{not json")
    assert store.read("bad", default={"ok": True}) == {"ok": True}


def test_invalid_key_rejected(store):
    for bad in ("", "../escape", "a/b", ".hidden"):
        with pytest.raises(ValueError):
            store.write(bad, {})


def test_update_is_atomic_read_modify_write(store):
    store.write("counter", {"n": 0})
    store.update("counter", lambda cur: {"n": cur["n"] + 1})
    assert store.read("counter") == {"n": 1}


def test_concurrent_updates_do_not_lose_writes(store):
    store.write("counter", {"n": 0})

    def bump():
        for _ in range(50):
            store.update("counter", lambda cur: {"n": cur["n"] + 1})

    threads = [threading.Thread(target=bump) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert store.read("counter") == {"n": 200}
