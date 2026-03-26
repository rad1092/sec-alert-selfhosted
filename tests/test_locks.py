from __future__ import annotations

from pathlib import Path

import pytest

from app.services.locks import SingletonProcessLock


def test_singleton_process_lock_rejects_duplicate_acquire(tmp_path: Path):
    path = tmp_path / "app.lock"
    first = SingletonProcessLock(path)
    second = SingletonProcessLock(path)

    first.acquire()
    try:
        with pytest.raises(RuntimeError):
            second.acquire()
    finally:
        first.release()
