"""Forward native stdout diagnostics during a KLayout reader call."""

from __future__ import annotations

import errno
import os
import shutil
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress

# File descriptors are process-wide. Concurrent readers must restore them in
# order, including when one reader raises or a read is nested.
_redirect_lock = threading.RLock()


@contextmanager
def native_stdout_to_stderr() -> Iterator[None]:
    """Send lines written directly to fd 1 to the caller's stderr stream."""
    with _redirect_lock:
        if sys.stdout is not None:
            # Embedded callers can close the Python stream while fd 1 stays open.
            with suppress(ValueError):
                sys.stdout.flush()
        try:
            saved = os.dup(1)
        except OSError as exc:
            if exc.errno != errno.EBADF:
                raise
            # An embedded caller may have closed fd 1 entirely.
            yield
            return
        try:
            inheritable = os.get_inheritable(1)
            with tempfile.TemporaryFile(
                mode="w+", encoding="utf-8", errors="replace"
            ) as capture:
                os.dup2(capture.fileno(), 1, inheritable=inheritable)
                try:
                    yield
                finally:
                    os.dup2(saved, 1, inheritable=inheritable)
                    capture.seek(0)
                    shutil.copyfileobj(capture, sys.stderr)
        finally:
            os.close(saved)
