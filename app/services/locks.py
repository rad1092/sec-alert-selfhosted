from __future__ import annotations

from pathlib import Path

import portalocker


class SingletonProcessLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            portalocker.lock(handle, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except portalocker.LockException as exc:
            handle.close()
            raise RuntimeError(
                f"Another SEC Alert process is already holding {self.path.name}.",
            ) from exc
        self.handle = handle

    def release(self) -> None:
        if self.handle is None:
            return
        portalocker.unlock(self.handle)
        self.handle.close()
        self.handle = None
