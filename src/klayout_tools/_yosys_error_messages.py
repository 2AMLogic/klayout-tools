"""Error-message builders for failed ``yosys`` runs, shared by the verbs.

``equiv.py``'s ``_yosys_error_message`` and ``synthesize.py``'s
``_synthesis_error_message`` lived in their respective verb modules, each
duplicating the other's structure; issue #1755's WASI-sandbox hint had
diverged between them precisely because the detection lived twice. They are
co-located here (with issue #5804's wasm-trap hint) so the hint detection
cannot silently diverge again. Kept out of ``equiv.py``/``synthesize.py``/
``_provenance.py`` because those files are under the per-file size ratchet
(the extraction pattern of e34fedfb1/c07980ca6).
"""

from __future__ import annotations

import re
import subprocess

from . import macros as macro_spec
from ._provenance import wasi_sandbox_hint_if_applicable

# Issue #5804: a WASI yosys (e.g. ``yowasp-yosys``) runs inside a wasm32
# guest whose linear memory is capped at ~4 GiB. A heavy ``sat -seq <depth>``
# query over a large flattened netlist allocates roughly with design size x
# unroll depth and can exceed that ceiling; the guest's allocation failure
# unwinds out of the guest as a bare wasmtime trap -- no yosys ``ERROR:``
# line anywhere -- so the traceback shape below is the only signal. The
# boundary is design-dependent (the same script passes at one design size and
# traps two steps up), so no numeric ceiling is claimed anywhere.
_WASM_TRAP_RE = re.compile(r"wasmtime\._trap\.Trap|thrown Wasm exception")

_WASM_TRAP_HINT = (
    "; yosys died with a WebAssembly trap and no yosys ERROR of its own -- "
    "the 'yosys' on $PATH is a WASI/wasm build (e.g. yowasp-yosys), and the "
    "most likely cause is that the query exceeded the wasm32 guest's ~4 GiB "
    "linear-memory ceiling (e.g. a sat -seq <depth> unroll over a large "
    "flattened netlist). Point request.yosys_binary (or $KLT_YOSYS_BINARY) "
    "at a native yosys build, which has no such ceiling."
)


def wasm_trap_hint_if_applicable(output: str) -> str:
    """The wasm-trap hint if ``output`` carries the wasmtime trap signature
    (``wasmtime._trap.Trap`` or ``thrown Wasm exception``) -- otherwise
    ``""``.

    Shared by ``synthesize.py`` and ``equiv.py`` (issue #5804) so the
    wasm-memory-ceiling detection cannot silently diverge between verbs,
    the same failure mode issue #1755 fixed for
    ``wasi_sandbox_hint_if_applicable``. Callers consult this only in
    their no-``ERROR:`` tail branch: a run that produced a yosys ``ERROR:``
    line keeps that line's message and never sees the hint.

    A WASI yosys runs inside a wasm32 guest whose linear memory is capped
    at ~4 GiB; a query that allocates past the ceiling dies as a bare
    ``thrown Wasm exception`` trap with no yosys-side message, which the
    generic tail-of-output branch would otherwise echo verbatim. The hint
    names the ceiling and the ``request.yosys_binary``/
    ``$KLT_YOSYS_BINARY`` native-build remedy.
    """
    if not _WASM_TRAP_RE.search(output):
        return ""
    return _WASM_TRAP_HINT


def _yosys_error_message(stdout: str, stderr: str, returncode: int | None) -> str:
    """Build an actionable error message from a failed ``yosys -s`` run --
    mirrors ``_synthesis_error_message`` below (both live here so their
    hint detection cannot diverge -- the issue #1755 lesson): prefers the
    last ``ERROR:`` line Yosys itself printed, falling back to a short tail
    of captured output.

    When the error line is the ``Can't open script file `<path>' for
    reading: No such file or directory`` shape *and* ``<path>`` verifiably
    exists on the host filesystem, appends a hint that the ``yosys`` on
    ``$PATH`` is likely a WASI-sandboxed build (e.g. ``yowasp-yosys``) whose
    sandbox does not preopen that path -- see issue #1368/#1755; for a path under
    the host temp dir the hint instead names the sandbox's private ``/tmp``
    mount and leads with relocating the request (issue #2453). A script
    path that genuinely does not exist is a different failure and is left
    unchanged.

    In the no-``ERROR:`` tail branch, a run whose captured output carries
    the wasmtime trap signature (``wasmtime._trap.Trap`` /
    ``thrown Wasm exception`` -- a WASI yosys build, e.g. yowasp-yosys,
    exhausting the wasm32 guest's ~4 GiB memory ceiling) appends the
    shared ``wasm_trap_hint_if_applicable`` hint naming the ceiling and
    the ``request.yosys_binary``/``$KLT_YOSYS_BINARY`` native-build remedy
    (issue #5804). Output carrying neither an ``ERROR:`` line nor a trap
    signature keeps today's tail-of-output message verbatim.
    """
    for stream in (stderr, stdout):
        error_lines = [line.strip() for line in stream.splitlines() if "ERROR:" in line]
        if error_lines:
            message = f"yosys equivalence check failed: {error_lines[-1]}"
            message += wasi_sandbox_hint_if_applicable(error_lines[-1])
            return message

    tail_source = (stderr or stdout).strip().splitlines()
    snippet = " ".join(tail_source[-3:]) if tail_source else "no output captured"
    message = f"yosys exited with code {returncode}: {snippet}"
    return message + wasm_trap_hint_if_applicable(f"{stderr}\n{stdout}")


_SV_SOURCE_ERROR_RE = re.compile(r"(?:^|[\s/\\])\S*\.svh?:\d+:\s*ERROR:")


def _sv_frontend_hint_if_applicable(error_line: str) -> str:
    """Hint appended when Yosys's native ``-sv`` front end rejects a
    SystemVerilog construct (issue #4181).

    Yosys prefixes a parse error with ``<file>:<line>:``, so the source
    extension is read straight off the error line -- a ``.v`` source with
    the same syntax error gets no hint. Conservative table: only the
    unpacked-array-port class (``syntax error, unexpected '['`` while a
    port list is expected) is recognised; anything else returns ``""``.
    """
    if not _SV_SOURCE_ERROR_RE.search(error_line):
        return ""
    if "syntax error, unexpected '['" not in error_line:
        return ""
    return (
        " -- hint: this looks like an unpacked-array port (e.g. "
        "`input logic [31:0] a_i[2]`), a SystemVerilog construct Yosys's "
        "native `read_verilog -sv` front end does not support; rewrite it "
        "as a packed/flattened port or pre-convert the source"
    )


def _synthesis_error_message(completed: subprocess.CompletedProcess) -> str:
    """Build an actionable error message from a failed ``yosys -s`` run.

    Prefers the last ``ERROR:`` line Yosys itself printed (its own error
    lines go to stderr -- verified live, Yosys survey worked example), on
    either stream; falls back to a generic exit-code message with a short
    tail of captured output when no ``ERROR:`` line is found.

    When the error line is the ``Can't open script file `<path>' for
    reading: No such file or directory`` shape *and* ``<path>`` verifiably
    exists on the host filesystem, appends a hint that the ``yosys`` on
    ``$PATH`` is likely a WASI-sandboxed build (e.g. ``yowasp-yosys``) whose
    sandbox does not preopen that path -- see issue #1368; for a path under
    the host temp dir the hint instead names the sandbox's private ``/tmp``
    mount and leads with relocating the request (issue #2453). A script path
    that genuinely does not exist is a different failure and is left
    unchanged.

    In the no-``ERROR:`` tail branch, a run whose captured output carries
    the wasmtime trap signature (``wasmtime._trap.Trap`` /
    ``thrown Wasm exception`` -- a WASI yosys build, e.g. yowasp-yosys,
    exhausting the wasm32 guest's ~4 GiB memory ceiling) appends the
    shared ``wasm_trap_hint_if_applicable`` hint naming the ceiling and
    the ``request.yosys_binary``/``$KLT_YOSYS_BINARY`` native-build remedy
    (issue #5804). Output carrying neither an ``ERROR:`` line nor a trap
    signature keeps today's tail-of-output message verbatim.
    """
    for stream in (completed.stderr or "", completed.stdout or ""):
        error_lines = [line.strip() for line in stream.splitlines() if "ERROR:" in line]
        if error_lines:
            message = f"yosys synthesis failed: {error_lines[-1]}"
            message += wasi_sandbox_hint_if_applicable(error_lines[-1])
            # Issue #2635: `Module `\<cell>' ... is not part of the design`
            # is what a hard macro with no `request.macros` entry looks
            # like, and the raw line says nothing about which request field
            # fixes it. Appended as a further `--` clause, the same
            # recognized-hint structure the WASI hint above already uses.
            macro_hint = macro_spec.undeclared_module_hint(error_lines[-1])
            if macro_hint is not None:
                message = f"{message} -- {macro_hint}"
            message += _sv_frontend_hint_if_applicable(error_lines[-1])
            return message

    tail_source = (completed.stderr or completed.stdout or "").strip().splitlines()
    snippet = " ".join(tail_source[-3:]) if tail_source else "no output captured"
    message = f"yosys exited with code {completed.returncode}: {snippet}"
    return message + wasm_trap_hint_if_applicable(
        f"{completed.stderr or ''}\n{completed.stdout or ''}"
    )
