"""Single Instance Lock — Prevents multiple concurrent instances of the application or voice server.
"""

import sys
import os
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

class SingleInstanceLock:
    def __init__(self, lock_name: str = "app.lock"):
        self.lock_file = Path(os.getenv("TEMP", ".")) / lock_name
        self.handle = None

    def acquire(self) -> bool:
        """Acquire process lock. Returns True if acquired, False if another instance is running."""
        try:
            self.lock_file.parent.mkdir(parents=True, exist_ok=True)
            self.handle = open(self.lock_file, "w")
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.handle.write(str(os.getpid()))
            self.handle.flush()
            return True
        except (IOError, OSError) as e:
            logger.warning("Another instance is already running (lock file: %s, error: %s)", self.lock_file, e)
            if self.handle:
                try:
                    self.handle.close()
                except Exception:
                    pass
                self.handle = None
            return False

    def release(self) -> None:
        """Release process lock."""
        if self.handle:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
                self.handle.close()
            except Exception as e:
                logger.warning("Error releasing lock: %s", e)
            finally:
                self.handle = None
                try:
                    if self.lock_file.exists():
                        self.lock_file.unlink()
                except Exception:
                    pass
