"""Native-crash stage diagnostics for ``klt lvs`` (issue #5630).

``klt lvs`` can die inside KLayout's native code with no Python exception
on the path: issue #5630 reports a deterministic SIGSEGV (exit 139, empty
stdout, empty stderr) on a routed ~1070-instance ASAP7 block under the
documented ``finfet_bulk`` + ``flatten_layout`` + ``flatten_reference``
recipe, where each half of the recipe completes to a verdict. A crash like
that is uncatchable in-process -- no ``except`` clause runs, and no JSON
envelope can be emitted after it -- so this module makes the *process
itself* tell the caller two things it otherwise could not:

* **that it was a tool crash, not a verdict** -- :func:`enable` turns on
  :mod:`faulthandler`, whose C-level handler dumps the Python traceback of
  every thread to stderr even mid-native-code death (the default SIGSEGV
  disposition would print nothing);
* **which pipeline stage was running** -- :func:`stage` wraps a stage
  function in a frame whose compiled name is
  ``klt_lvs_stage_<name>``, so that traceback names the stage, and
  :func:`current_stage` exposes it to live callers.

The exit code after a native crash stays 139 (the OS signal status); the
diagnostic is stderr-only, exactly like ``lvs_timeout``'s heartbeat. The
JSON stdout contract is untouched. Subprocess isolation of the whole
compare was considered and rejected for this defect (see #5630's
implementation guidance) -- this is the in-process alternative.

Stage vocabulary (kept deliberately small; the layout-resolve stage's
sub-steps are the suspect sequence from #5630's bisection -- bulk join,
then flatten + the post-flatten finger re-merge #4954 added in v0.9.1):

* ``layout_resolve`` -- extraction + supply findings + per-circuit combine
  + FinFET bulk policy + optional flatten (:func:`lvs_layout_stage`).
* ``finfet_bulk_join`` -- the per-circuit ``join_nets`` the bulk policy
  performs inside layout resolve (#4514).
* ``flatten_layout_combine`` -- ``Netlist.flatten()`` plus the FinFET
  post-flatten ``combine_devices()`` (#4954).
* ``reference_read`` -- reference netlist read and its own transforms
  (:func:`lvs_reference_stage`).
"""

from __future__ import annotations

import functools
import sys
import threading
from collections.abc import Callable
from typing import Any, TypeVar, cast

_F = TypeVar("_F", bound=Callable[..., Any])

_stage_state = threading.local()


def current_stage() -> str | None:
    """The innermost :func:`stage` name this thread is inside, if any."""
    return getattr(_stage_state, "name", None)


def enable(file: Any = None) -> bool:
    """Turn on :mod:`faulthandler` for fatal signals (SIGSEGV, SIGABRT,
    SIGBUS, ...), dumping every thread's Python traceback to ``file``
    (default: stderr). Idempotent. Returns whether it could be enabled --
    a process with no usable stderr stream stays silent rather than
    failing the compare it was diagnosing."""
    import faulthandler

    stream = sys.stderr if file is None else file
    if stream is None:
        return False
    try:
        faulthandler.enable(file=stream, all_threads=True)
    except (ValueError, OSError, RuntimeError):
        # e.g. a stream without a usable fileno()
        return False
    return True


def _enter(name: str) -> str | None:
    previous = current_stage()
    _stage_state.name = name
    return previous


def _restore(previous: str | None) -> None:
    _stage_state.name = previous


def _wrapper_name(name: str) -> str:
    safe = "".join(c if c.isalnum() else "_" for c in name).strip("_")
    return f"klt_lvs_stage_{safe or 'unnamed'}"


def stage(name: str) -> Callable[[_F], _F]:
    """Decorator naming a pipeline stage in every crash traceback.

    The returned wrapper's *compiled* function name is
    ``klt_lvs_stage_<name>`` (not just its ``__name__`` attribute), so the
    C-level :mod:`faulthandler` dump -- which prints code-object names, not
    attributes -- reports the stage that was running when a native crash
    hit, with zero per-call cost beyond one frame. The stage is also
    recorded thread-locally for :func:`current_stage`.
    """

    def decorate(fn: _F) -> _F:
        scope: dict[str, Any] = {"_fn": fn, "_enter": _enter, "_restore": _restore}
        source = (
            f"def {_wrapper_name(name)}(*args, **kwargs):\n"
            f"    _previous = _enter({name!r})\n"
            f"    try:\n"
            f"        return _fn(*args, **kwargs)\n"
            f"    finally:\n"
            f"        _restore(_previous)\n"
        )
        exec(source, scope)  # noqa: S102 - name is a fixed identifier shape
        wrapper = scope[_wrapper_name(name)]
        functools.update_wrapper(wrapper, fn)
        wrapper.__lvs_stage__ = name
        # `cast`: the wrapper forwards *args/**kwargs verbatim, so it has `fn`'s
        # signature; without this every decorated stage would erase to
        # `(*Any, **Any) -> Any` for callers (issue #5630 review).
        return cast("_F", wrapper)

    return decorate
