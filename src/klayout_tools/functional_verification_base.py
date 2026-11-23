"""Shared exception and SDF settings for functional verification."""

from __future__ import annotations

import re
from typing import Any

#: Only Icarus has an SDF back-annotation path through this flow: Verilator
#: has no ``$sdf_annotate`` at all (spike §4.1). ``options.sdf`` on any other
#: engine is a request error, mirroring ``options.coverage`` + ``icarus``.
SDF_ENGINES = ("icarus",)

#: The extra elaboration root that carries the ``$sdf_annotate`` call. A
#: cocotb regression's ``hdl_toplevel`` *is* the DUT (driven from Python),
#: so there is no Verilog testbench module to host an ``initial`` block --
#: elaborating a second, otherwise-empty root alongside the DUT supplies one
#: without touching the netlist, the testbench, or the DUT's own hierarchy.
#: cocotb's own Icarus runner already uses this exact shape for its
#: ``cocotb_iverilog_dump`` waveform module.
SDF_ANNOTATE_MODULE = "klt_sdf_annotate"

#: The generated transparent pass-through wrapper module (issue #1056's
#: fix -- see :func:`_write_sdf_dut_wrapper`), and the name it instantiates
#: the real DUT under. Both are elaborated in place of ``hdl_toplevel``
#: *only* for an ``options.sdf`` build; every other build path (baseline
#: gate-level, RTL, coverage) still passes the real ``hdl_toplevel`` straight
#: through, unaffected.
SDF_WRAPPER_MODULE = "klt_sdf_dut_wrapper"

SDF_WRAPPER_DUT_INSTANCE = "klt_sdf_dut"

#: ``options.sdf.corner`` values, mapping 1:1 onto ``iverilog -T``. Corner
#: selection is a *compile-time* flag for Icarus, not an ``$sdf_annotate``
#: argument: Icarus ignores every argument past the second ("$sdf_annotate
#: currently only uses the first two argument", its own wording), so a
#: request that named a corner there would be silently ignored (spike §3.5).
SDF_CORNERS = ("min", "typ", "max")

#: Icarus's own default when ``-T`` is not passed; named explicitly so the
#: response's echo is never ambiguous about which corner was simulated.
DEFAULT_SDF_CORNER = "typ"

#: ``options.sdf.entries`` values (issue #2364). ``"all"`` (the default, and
#: the only behavior before this field existed) annotates every delay entry
#: the SDF carries -- ``IOPATH`` module-path delays and ``INTERCONNECT`` net
#: delays alike. ``"interconnect"`` drops every non-``INTERCONNECT`` delay
#: entry from the SDF text before annotation, counted as its own
#: :data:`SDF_IOPATH_DROPPED_CLASS` ``environment.sdf.dropped`` class: the
#: net-delay back-annotation configuration #2364's isolation table validated
#: on a timing-clean sky130A design (positive setup/hold slack, TNS 0), where
#: ``INTERCONNECT``-only annotation simulates correctly while any SDF
#: carrying ``IOPATH`` entries kills the design even at a 10x-relaxed clock
#: period -- an annotation-mechanism kill, not a timing outcome.
SDF_ENTRIES_MODES = ("all", "interconnect")

#: See :data:`SDF_ENTRIES_MODES`; the explicit default keeps the response's
#: echo unambiguous about which entry population was annotated.
DEFAULT_SDF_ENTRIES = "all"

#: Every SDF problem Icarus hits -- an unopenable file, an instance it cannot
#: find, an ``IOPATH`` the cell's ``specify`` block does not declare, an
#: ``INTERCONNECT`` without ``-ginterconnect`` -- is reported with one of
#: these prefixes and then **survived**: the annotation is dropped, `vvp`
#: exits ``0``, and cocotb duly reports the zero-delay verdict as a pass.
#: Scanning for them is the only thing standing between "re-verified with
#: real post-route delays" and "re-ran at zero delay and nobody noticed"
#: (spike §3.3, where a 50-flop netlist annotated 0 flops and still reported
#: the expected verdicts).
SDF_DIAGNOSTIC_MARKERS = ("SDF WARNING", "SDF ERROR")

#: ...with two deliberate exemptions, both of which fire on *correct* input:
#:
#: - ``TIMINGCHECK not supported`` -- Icarus implements SDF delays but not
#:   SDF timing checks (spike §3.4), and a real ``write_sdf`` emits
#:   ``TIMINGCHECK`` sections alongside ``IOPATH``/``INTERCONNECT``. Failing
#:   on this would reject every real post-route SDF; the delays it *does*
#:   apply are unaffected. The consequence worth knowing (no setup/hold
#:   violation reporting on this path -- that is OpenSTA's job) is documented
#:   in ``docs/cli/functional-verification.md``.
#: - ``NEGATIVE_CONSTRAINT``/``PATHPULSE`` &c. are **not** exempted: only the
#:   timing-check family is, because only it is both benign and unavoidable.
SDF_BENIGN_DIAGNOSTIC_SUBSTRINGS = ("TIMINGCHECK",)

#: The shape Icarus gives *every* SDF diagnostic it emits: the marker, then
#: a ``<file>:<line-number>:`` locator, then the message -- true of all four
#: failure modes the spike captured live (§3.3) and of the ``$sdf_annotate``
#: argument warning (§3.6). A marker-bearing transcript line that does *not*
#: have this shape is not a diagnostic Icarus wrote; it is a corrupted one
#: (issue #1136 -- see :func:`_scan_sdf_diagnostics`), so the classification
#: below is applied only to lines that match.
SDF_DIAGNOSTIC_LINE_RE = re.compile(r"^SDF (?:WARNING|ERROR): .+?:\d+: .+$")

#: cocotb's log-line header (``<sim-time><unit> <LEVEL> <logger>``), e.g.
#: ``590.00ns INFO     cocotb.klt_sdf_dut_wrapper``. A marker-bearing line that
#: *contains* one is a stdout splice even when it still matches
#: :data:`SDF_DIAGNOSTIC_LINE_RE` (issue #2996, extending #1136's rule past
#: the locator): the cut landed inside the message, or inside the path with
#: the spliced-in line's own ``<file>:<line>:`` satisfying the locator. Keying
#: on the interleaved writer's signature rather than on a second locator
#: makes the result independent of path length and of which log line lands.
SDF_SPLICED_LOG_HEADER_RE = re.compile(
    r"\s\d+(?:\.\d+)?\s?(?:fs|ps|ns|us|ms|s)\s+"
    r"(?:DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+\S+"
)

#: The ``environment.sdf.dropped`` class counting the zero-delay
#: ``INTERCONNECT`` entries removed before annotation because their
#: destination is a top-level port bit the netlist drives through an
#: ``assign`` alias -- issue #2285's counted exemption, the second member of
#: this shape after #1102's ``timingcheck``. Unlike ``timingcheck`` this
#: class is *not* derived from a diagnostic substring: the entries are
#: dropped from the SDF text up front
#: (:func:`_drop_sdf_zero_delay_alias_port_interconnects`), so Icarus never
#: emits a diagnostic for them at all.
SDF_ALIAS_PORT_DROPPED_CLASS = "zero_delay_alias_port_interconnect"

#: Not an ``SDF``-prefixed diagnostic at all, but the single most dangerous
#: line on this path (spike §3.1): with ``-gspecify`` missing, Icarus drops
#: the annotation call as an ordinary compile-time ``warning:`` among
#: hundreds of others and the run is otherwise indistinguishable from a
#: successful annotation. This module always passes ``-gspecify``, so it must
#: never appear -- and if it ever does (a cocotb backend that reorders or
#: drops build args, say), that is a silent zero-delay run, caught here.
SDF_OMITTED_ANNOTATION_MARKER = "Omitting $sdf_annotate"

#: Minimum Icarus *major* version that has ``-ginterconnect`` at all (issue
#: #1004's live finding on Icarus 12.0, folded in here rather than deferred).
#: The spike's GO verdict was captured on Icarus 13.0 -- this repo's own
#: pinned build -- and on 12.0 (Ubuntu noble's distro package) ``iverilog``
#: rejects the flag outright: ``Unknown/Unsupported Language generation
#: interconnect``, exit 255. Since ``-ginterconnect`` is *mandatory* for a
#: post-route SDF (every ``INTERCONNECT`` entry fails without it), an older
#: Icarus cannot serve this request at all; probing the resolved version and
#: saying so is strictly better than letting a raw compiler error surface
#: from four layers down.
SDF_MIN_ICARUS_MAJOR = 13


class FunctionalVerificationError(Exception):
    """Raised when a regression cannot be *trusted to have run*: a missing/
    malformed request, an unresolvable RTL source or testbench module, an
    unsupported engine, ``options.coverage`` requested on an engine that has
    no coverage path, a missing cocotb/simulator install, a build or
    elaboration error, a simulator crash, or a run that produced no
    ``results.xml``.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback. A run that *did* complete and reported failing tests is not
    an error -- it is ``status: "fail"`` + exit code 3 (see this module's
    docstring and ``docs/cli/functional-verification.md``).

    ``code`` is an optional stable machine-readable classification the CLI
    passes through as ``error.code`` (``None`` for every error without one).
    """

    code: str | None = None
    details: dict[str, Any] | None = None

    def __init__(self, message: str = "", *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
