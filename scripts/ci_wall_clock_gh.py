#!/usr/bin/env python3
"""The `gh api` client the history fetcher reads Actions through.

Extracted from `fetch_ci_wall_clock_history.py` when it grew a retry
(#6214): that script sits at its `file-size-baseline.json` ceiling, so
the growth landed here instead -- the same move `ci_wall_clock_steps.py`
(#5964) made for the step-measurement conventions both scripts share.

#6214's failure: one transient HTTP 502 during the history fetch (one
`gh api` attempt per path, no retry) silently swapped the wall-clock
gate from its rolling model to committed fixed ceilings ~2x below the
workload the repo actually runs, reddening an unrelated PR until someone
reran CI. `GhClient.get` therefore retries **transient** failures -- an
HTTP 5xx, a timeout, a dropped connection -- up to `RETRY_ATTEMPTS`
times with short geometric backoff. Permanent failures (4xx, rate
limits, non-JSON responses, a missing `gh`) raise on the first attempt,
exactly as before #6214.

Every attempt counts against the `max_calls` budget, so the step stays
bounded however often the API hiccups; a failure that survives its
retries is still an ordinary `FetchError`, which the fetcher turns into
an empty history and exit 0 -- never a red build.

Standard library only, like the scripts it serves.
"""

from __future__ import annotations

import json
import re
import subprocess
import time

RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 2.5

# Which failed-`gh` error texts are transient: a server 5xx, gh's own
# timeout phrasing, or a dropped connection. 4xx -- rate limits included
# -- never matches, and is never retried.
_TRANSIENT_ERROR = re.compile(r"HTTP 5\d\d|timed out|deadline exceeded|connection")


class FetchError(Exception):
    """Any reason the history could not be collected (never fatal)."""


class TransientError(FetchError):
    """A 5xx/timeout/connection failure worth one more attempt (#6214)."""


class GhClient:
    """A counted, timeout-bounded `gh api` caller with bounded retries."""

    def __init__(
        self, gh: str, timeout: int, max_calls: int, retry_sleep: float = 2.0
    ) -> None:
        self.gh = gh
        self.timeout = timeout
        self.max_calls = max_calls
        self.retry_sleep = retry_sleep
        self.calls = 0

    def get(self, path: str) -> object:
        """One logical read: up to `RETRY_ATTEMPTS` attempts when the
        failure was transient (#6214), backing off geometrically by
        `retry_sleep` seconds (2 s then 5 s by default; no sleeping when
        it is 0). Every attempt is charged to the API-call budget, so
        exhausting that budget mid-retry raises exactly like any other
        over-budget call."""
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            if self.calls >= self.max_calls:
                raise FetchError(f"API call budget of {self.max_calls} exhausted")
            self.calls += 1
            try:
                return self._attempt(path)
            except TransientError:
                if attempt == RETRY_ATTEMPTS:
                    raise
                delay = self.retry_sleep * RETRY_BACKOFF ** (attempt - 1)
                if delay > 0:
                    time.sleep(delay)
        raise FetchError("retry loop finished without a verdict")

    def _attempt(self, path: str) -> object:
        try:
            proc = subprocess.run(
                [self.gh, "api", "-H", "Accept: application/vnd.github+json", path],
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise TransientError(f"`{self.gh} api {path}` failed: {exc}") from exc
        except OSError as exc:
            raise FetchError(f"`{self.gh} api {path}` failed: {exc}") from exc
        if proc.returncode != 0:
            detail = proc.stderr.strip()[:200]
            error = TransientError if _TRANSIENT_ERROR.search(detail) else FetchError
            raise error(f"`{self.gh} api {path}` exited {proc.returncode}: {detail}")
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise FetchError(
                f"`{self.gh} api {path}` returned non-JSON: {exc}"
            ) from exc
