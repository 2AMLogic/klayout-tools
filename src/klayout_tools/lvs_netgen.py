"""netgen engine subsystem for ``klt lvs`` (issue #343): subprocess
invocation + report parsing.

Split out of ``lvs.py`` (issue #1803) as a self-contained subsystem --
``options.netgen_setup`` resolution (:func:`_resolve_netgen_setup`),
netgen-binary resolution (:func:`_resolve_netgen_binary`, issue #2373),
subprocess invocation (:func:`_run_netgen_lvs`, :func:`_cleanup_netgen_work_dir`),
and report parsing (:func:`_locate_netgen_verdict`, :func:`_parse_netgen_report`,
:func:`_declares_netgen_property_errors`, :func:`_netgen_property_error_context`,
:func:`_describe_netgen_property_delta`, :func:`_parse_netgen_property_errors`,
:func:`_parse_netgen_section_blocks`), plus this subsystem's own
``_NETGEN_*`` regex/marker constants. This mirrors the shape of the earlier
``lvs_mismatch.py`` (#1721), ``gen_compose.py``/``gen_compose_routing.py``
(#1717), ``_paths.py`` (#1715), and ``gen.py``/``gen_pcells`` (#1713) splits:
a cohesive, low-coupling subsystem relocated verbatim out of a file that had
grown past the point one module should hold request/response orchestration
*and* a second engine's own subprocess/report-parsing plumbing.

``_engine_version`` (generic klayout-version detection, also used by the
``klayout`` engine branch) is deliberately **not** part of this split -- it
stays in ``lvs.py``.

Dependency surface mirrors ``lvs_mismatch.py``'s discipline: this module
calls out to ``lvs.py``'s ``LvsError`` and ``CATEGORY_*`` constants, imported
*inside* the handful of functions that use them (never at module scope), so
this module never has a load-time dependency back on ``lvs.py`` -- only
``lvs.py`` depends on this module at import time. ``lvs.py`` in turn imports
this module's entry points (:func:`_resolve_netgen_setup`,
:func:`_resolve_netgen_binary`, :func:`_run_netgen_lvs`, plus
:data:`_NETGEN_DEFAULT_TIMEOUT_S`) back at
module scope, preserving ``klayout_tools.lvs.<name>`` as a working import
path for every name the test suite/callers used before this split.
:func:`_mismatch` is imported from ``lvs_mismatch.py`` at module scope
instead, since that module carries no load-time dependency on either this
module or ``lvs.py``.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from typing import TYPE_CHECKING, Any

from ._paths import _resolve_relative
from .lvs_mismatch import _mismatch

if TYPE_CHECKING:
    import klayout.db as kdb

#: Default ``netgen -batch lvs`` wall-clock budget -- the same idiom as
#: ``sim.py``'s ``DEFAULT_TIMEOUT_S``/``options.timeout_s``, but a separate,
#: larger default: an LVS graph-match on a real block can run longer than a
#: single SPICE corner. Overridable per request via ``options.netgen_timeout_s``.
_NETGEN_DEFAULT_TIMEOUT_S = 300.0

#: ANSI escape sequences, stripped from every piece of netgen text before it
#: is folded into a ``klt lvs`` report field (:func:`_strip_ansi`, issue
#: #1969). Covers the two shapes a terminal-colouring program emits -- CSI
#: (``ESC [ ... <final byte>``, the ``\033[1;31m`` SGR form) and OSC
#: (``ESC ] ... BEL``/``ESC \``) -- plus the two-character ``ESC <char>``
#: form. A lone ``ESC`` that matches none of these is dropped separately, so
#: the invariant :func:`_strip_ansi` guarantees is the simple, checkable one:
#: **no ``ESC`` byte survives into a report field.**
_ANSI_ESCAPE_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI (SGR colour, cursor moves, ...)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC (title/hyperlink)
    r"|\x1b[@-_]"  # two-character escapes
)


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from netgen-produced ``text``.

    Issue #1969, Finding 2. ``klt lvs`` emits no colour of its own -- the
    only ANSI in the whole CLI lives in ``cli/signoff_cmd.py``, which does it
    by explicit design. But the netgen engine folds netgen's *own* report
    text into ``klt lvs``'s report fields, and one of those channels is
    verbatim: :func:`_describe_netgen_property_delta` documents that it
    "passes any other wording through verbatim rather than dropping the
    line", landing netgen's text directly in a ``mismatches[].description``
    that ``cli/lvs_cmd.py``'s ``--format text`` renderer prints. With no
    sanitization, a colourising netgen build would put raw escape bytes on a
    non-TTY stdout (and into the ``--format json`` payload) -- reproduced in
    ``tests/test_lvs.py``'s
    ``test_netgen_engine_text_format_emits_no_ansi_to_non_tty_stdout``.

    Sanitizing here, at the fold-in boundary, is deliberately *not* an
    ``isatty()`` gate in ``lvs_cmd.py``. ``signoff_cmd.py`` does gate its
    *own* colour on a TTY (issue #2227, via ``cli/color.py``), but that only
    governs escapes ``klt`` itself emits: a TTY gate here would still leave
    netgen's escapes in the JSON contract's own string fields, where no
    terminal check applies at all. The text is foreign input; it is cleaned
    on the way in, once, for every consumer and every ``--format``.

    No known netgen build colourises ``comp.out`` today (netgen 1.5.323 and
    1.5.133 both write plain text), so this is a defence against foreign
    output rather than a fix for an observed netgen release -- exactly the
    posture the same text deserves for being outside this project's control.
    """
    return _ANSI_ESCAPE_RE.sub("", text).replace("\x1b", "")


#: netgen's own startup banner (``tclnetgen.c``'s ``netgen_AppInit``, verified
#: against a from-source build of netgen 1.5.323 for this issue): ``"Netgen
#: 1.5.323 compiled on <date>"``, printed to stdout on every invocation --
#: mirrors ``sim.py``'s ``_ENGINE_VERSION_RE`` (``sim.py:1081``) for ngspice's
#: analogous ``"ngspice-<version>"`` banner.
_NETGEN_ENGINE_VERSION_RE = re.compile(r"Netgen\s+([\w.]+)")

#: A matched device pair's parameter-difference block, as netgen's
#: ``PrintPropertyResults`` (``base/netcmp.c``) writes it to the log file,
#: e.g.::
#:
#:     pmos:1 vs. pmos:1:
#:      W circuit1: 1e-06   circuit2: 2e-06   (delta=66.7%, cutoff=1%)
#:
#: Verified against a from-source netgen 1.5.323 build for this issue (see
#: the dated addendum in docs/design/lvs-extraction-spike.md).
#:
#: The index after the colon is numeric (``1``, ``2``, ...) only for
#: primitive *devices*. For a *subcircuit instance*, netgen uses the
#: instance name instead, e.g.::
#:
#:     sub:i1 vs. sub:i1:
#:      w circuit1: 1e-06   circuit2: 2e-06   (delta=66.7%, cutoff=0%)
#:
#: so the index group must accept any non-whitespace token, not just
#: digits (issue #363).
_NETGEN_PROPERTY_BLOCK_RE = re.compile(
    r"^(\S+):(\S+) vs\. (\S+):(\S+):\n((?: .+\n)+)", re.MULTILINE
)

#: One parameter-difference line inside a :data:`_NETGEN_PROPERTY_BLOCK_RE`
#: body. netgen emits (at least) two trailing-qualifier shapes, both verified
#: against a from-source netgen 1.5.323 build::
#:
#:      W circuit1: 1e-06   circuit2: 2e-06   (delta=66.7%, cutoff=1%)
#:      model circuit1: "fast"   circuit2: "slow"   (exact match req'd)
#:
#: -- the numeric form for a tolerance-compared property, and the "exact
#: match req'd" form for a string-valued one (``PropertyErrorCheck``'s
#: non-numeric branch). The qualifier is therefore captured as free text and
#: interpreted afterwards by :func:`_describe_netgen_property_delta`, rather
#: than hard-requiring the ``delta=…, cutoff=…`` shape: a line whose
#: qualifier wording changes across netgen versions must still parse, because
#: a property line silently failing to parse is exactly how a real property
#: error turned into a false ``"match"`` verdict (issue #343 review).
_NETGEN_PROPERTY_LINE_RE = re.compile(
    r"^\s*(\S+)\s+circuit1:\s*(.+?)\s+circuit2:\s*(.+?)"
    r"(?:\s*\(([^)]*)\))?\s*$"
)

#: The ``delta=…, cutoff=…`` qualifier shape, when netgen used it.
_NETGEN_PROPERTY_DELTA_RE = re.compile(
    r"^delta=([^,]+),\s*cutoff=(.+)$",
)

#: netgen's own declarations that a matched netlist nonetheless carries
#: parameter (property) errors -- the summary line printed with the
#: per-circuit verdict, and the trailing marker printed after ``Final
#: result:``. These are the *authoritative* signal that property errors
#: exist: :func:`_parse_netgen_report` keys the match -> mismatch downgrade
#: on them directly, never on whether :data:`_NETGEN_PROPERTY_LINE_RE`
#: happened to parse the supporting evidence (issue #343 review -- a
#: string-valued property difference parsed to nothing and the report was
#: reported as a clean ``"match"``).
_NETGEN_PROPERTY_ERROR_MARKERS: tuple[str, ...] = (
    "Property errors were found.",
    "match uniquely with property errors",
    "had property errors",
)

#: Side-by-side report section headers netgen prints ahead of a topology
#: mismatch, and the ``mismatches[]`` category/label each buckets into when
#: this module does not attempt to parse the column-aligned table itself
#: (see ``_parse_netgen_section_blocks``'s docstring for why: a fixed-width,
#: pipe-delimited table with filler cells like ``"(no matching net)"`` is
#: brittle to parse precisely across netgen versions, so the raw block is
#: preserved in ``details.raw`` instead of guessing at a per-net/per-device
#: split that could be wrong in an unbounded way). Built inside
#: :func:`_parse_netgen_section_blocks` itself (rather than as a module-level
#: constant) since its category values are ``lvs.py`` constants, imported
#: with the same deferred discipline as everything else this module needs
#: from ``lvs.py``.
_NETGEN_SECTION_HEADER_TEXT: tuple[tuple[str, str], ...] = (
    ("NET mismatches:", "net mismatch(es)"),
    ("DEVICE mismatches:", "device mismatch(es)"),
)

#: Boundary markers used to find the end of a
#: :data:`_NETGEN_SECTION_HEADER_TEXT` block: the next section (of either
#: kind), the "Subcircuit pins:" report that always follows the mismatch
#: tables, or the terminal verdict line (either wording -- see
#: :data:`_NETGEN_VERDICT_MARKERS`) -- whichever appears first.
_NETGEN_SECTION_BOUNDARIES: tuple[str, ...] = (
    "NET mismatches:",
    "DEVICE mismatches:",
    "Subcircuit pins:",
    "Final result:",
    "Result:",
)

#: The terminal verdict line's leading text, across observed netgen builds.
#: ``"Final result:"`` was verified against a from-source netgen 1.5.323
#: build (issue #343); ``"Result:"`` is the wording used by at least one
#: still-current packaged build (Debian/Ubuntu's ``netgen-lvs`` 1.5.133,
#: built 2022-12-01 -- issue #1192). Order matters: ``"Final result:"`` is
#: checked first and preferred when a report happens to contain both, since
#: it is the more specific, unambiguous marker.
_NETGEN_VERDICT_MARKERS: tuple[str, ...] = ("Final result:", "Result:")


#: Binary names :func:`_resolve_netgen_binary` looks for on ``PATH``, in
#: order, when the caller named none explicitly (issue #2373).
#:
#: ``netgen`` first -- it is upstream's own name, what a from-source build
#: installs, and what every host that worked before this resolution order
#: existed already has. ``netgen-lvs`` second: that is the binary name
#: Debian/Ubuntu's ``netgen-lvs`` package installs (``/usr/bin/netgen-lvs``),
#: because on those distributions the name ``netgen`` is taken by an
#: unrelated FEM mesh generator. Without this fallback a host with netgen
#: correctly installed from its distribution got a "binary not found" error
#: whose own remediation ("install netgen") was already satisfied.
_NETGEN_BINARY_NAMES: tuple[str, ...] = ("netgen", "netgen-lvs")

#: Environment-variable override for the netgen binary, checked after
#: ``options.netgen_binary`` and before the :data:`_NETGEN_BINARY_NAMES`
#: ``PATH`` search -- the same "explicit option > env var > default"
#: convention ``KLT_TIERS_DOC`` (``design_evidence_tiers.py``) and
#: ``KLT_SIM_MAX_WORKERS`` (``cli/parser.py``) already follow.
_NETGEN_BINARY_ENV_VAR = "KLT_NETGEN_BINARY"


def _resolve_netgen_binary(options: dict[str, Any], request_dir: str) -> str:
    """Resolve the netgen binary to invoke, returning an absolute path
    (issue #2373).

    Resolution order, first runnable candidate wins:

    1. ``options.netgen_binary`` -- an explicit binary name or path, for a
       from-source build in an unusual location.
    2. ``$KLT_NETGEN_BINARY`` -- the same override without editing the
       request document.
    3. ``netgen`` on ``PATH``.
    4. ``netgen-lvs`` on ``PATH`` -- Debian/Ubuntu's package installs the
       binary under this name (see :data:`_NETGEN_BINARY_NAMES`).

    An explicitly-named binary that is not runnable is an application error
    naming *which* source named it, never a silent fallback to the ``PATH``
    search: an override the caller believes is in force but is not would be
    worse than not supporting one at all (the same reasoning
    ``options.parameter_tolerance`` is rejected rather than ignored for this
    engine). A value containing a path separator is resolved against
    ``request_dir`` first, exactly like :func:`_resolve_netgen_setup`; a bare
    name is looked up on ``PATH``.

    When nothing resolves, the error names *both* built-in candidates -- the
    pre-#2373 message named only ``netgen``, so the one host class that most
    needed the message (Debian/Ubuntu, with ``netgen-lvs`` already installed)
    was told to install what it already had.
    """
    from .lvs import LvsError

    for value, source in (
        (options.get("netgen_binary"), "options.netgen_binary"),
        (os.environ.get(_NETGEN_BINARY_ENV_VAR), f"${_NETGEN_BINARY_ENV_VAR}"),
    ):
        if value is None or value == "":
            continue
        candidate = value
        if os.sep in value or (os.altsep and os.altsep in value):
            candidate = _resolve_relative(value, request_dir)
        resolved = shutil.which(candidate)
        if resolved is None:
            raise LvsError(
                f"{source} does not name a runnable netgen binary: "
                f"'{candidate}' is not an executable file and was not found "
                "on PATH"
            )
        return resolved

    for name in _NETGEN_BINARY_NAMES:
        resolved = shutil.which(name)
        if resolved is not None:
            return resolved

    tried = ", ".join(f"'{name}'" for name in _NETGEN_BINARY_NAMES)
    raise LvsError(
        f"could not launch netgen: no netgen binary found on PATH (tried "
        f"{tried} -- 'netgen-lvs' is the name Debian/Ubuntu's netgen-lvs "
        "package installs). Install netgen "
        "(https://github.com/RTimothyEdwards/netgen), name the binary "
        f"explicitly with options.netgen_binary or ${_NETGEN_BINARY_ENV_VAR}, "
        "or use engine 'klayout' instead."
    )


def _resolve_netgen_setup(options: dict[str, Any], request_dir: str) -> str | None:
    """Resolve ``options.netgen_setup`` (an explicit path to a netgen LVS
    setup ``.tcl`` file) against ``request_dir``, or ``None`` when omitted.

    ``klt lvs`` deliberately resolves no PDK on its own (this module's
    docstring, and ``provenance.pdk`` is always ``null``) -- so, unlike
    ``pdk.netgen_setup_file`` (issue #343's PDK-side lookup), this function
    does not itself call ``find_pdk``. A caller wanting the PDK-native setup
    resolved automatically composes the two: pass
    ``pdk.netgen_setup_file(variant=...)``'s result as this field. Omitting
    it runs netgen with no setup file (its own documented "trivial default
    setup" -- device/net comparison still works, but PDK-specific device-class
    merging/property tolerances from the setup script do not apply).
    """
    from .lvs import LvsError

    value = options.get("netgen_setup")
    if value is None:
        return None
    resolved = _resolve_relative(value, request_dir)
    if not os.path.isfile(resolved):
        raise LvsError(f"options.netgen_setup not found: {resolved}")
    return resolved


def _run_netgen_lvs(
    *,
    layout_netlist: kdb.Netlist,
    layout_circuit: kdb.Circuit,
    reference_netlist: kdb.Netlist,
    reference_circuit: kdb.Circuit,
    setup_file: str | None,
    timeout_s: float,
    binary: str,
) -> tuple[str, list[dict[str, Any]], str | None]:
    """Invoke ``netgen -batch lvs`` headlessly in netlist-vs-netlist mode and
    return ``(status, mismatches, engine_version)``.

    Writes ``layout_netlist``/``reference_netlist`` (already selected,
    pruned, and -- when ``options.combine_devices`` was set -- combined,
    identically to what the ``klayout`` engine compares) to temporary SPICE
    files via ``klayout.db.NetlistSpiceWriter``, then runs::

        <binary> -batch lvs "<layout.spice> <top>" "<reference.spice> <top>" \\
            <setup_file_or_""> <log_path>

    ``binary`` is the already-resolved netgen executable -- the caller passes
    :func:`_resolve_netgen_binary`'s result (issue #2373) rather than this
    function hardcoding the name ``netgen``, so a host whose distribution
    installs it as ``netgen-lvs`` is reachable without a ``PATH`` shim, and
    so ``environment.netgen_binary`` can record which executable actually
    produced the verdict.

    -- the syntax ``netgen::lvs`` (``tcltk/netgen.tcl.in``) expects: a
    ``"<file> <cell>"`` pair per side (a single argv token containing a
    space, which Tcl's ``eval $argv`` re-splits into the 2-element list the
    proc parses), an empty string for "no setup file" (netgen's own
    documented behaviour, verified directly against a from-source build for
    this issue), and the report log path.

    Never trusts the subprocess's exit code alone: like ``sim.py``'s
    ``_run_corner``, netgen exits ``0`` regardless of match/mismatch/most
    errors (verified empirically for this issue) -- the log file's own
    verdict text ("Final result:" or, on some packaged builds, "Result:" --
    issue #1192) is the primary trustworthy verdict signal, parsed by
    :func:`_parse_netgen_report`. The subprocess's own captured ``stdout`` is
    passed through as a fallback source for that same parse: at least one
    still-current netgen build only ever prints its verdict line to stdout,
    never into the log file (issue #1192). :func:`_parse_netgen_report`
    raises :class:`LvsError` rather than guessing when no verdict text is
    found in either source or the text found is unrecognised (the "must not
    silently produce a false match on unparseable output" requirement this
    issue exists to satisfy).
    """
    import klayout.db as kdb

    from .lvs import LvsError

    work_dir = tempfile.mkdtemp(prefix="klt-lvs-netgen-")
    try:
        layout_path = os.path.join(work_dir, "layout.spice")
        reference_path = os.path.join(work_dir, "reference.spice")
        log_path = os.path.join(work_dir, "comp.out")

        writer = kdb.NetlistSpiceWriter()
        writer.use_net_names = True
        try:
            layout_netlist.write(
                layout_path, writer, "klt lvs -- netgen engine layout netlist"
            )
            reference_netlist.write(
                reference_path, writer, "klt lvs -- netgen engine reference netlist"
            )
        except Exception as exc:
            raise LvsError(
                f"could not write a netlist for the netgen engine: {exc}"
            ) from exc

        cmd = [
            binary,
            "-batch",
            "lvs",
            f"{layout_path} {layout_circuit.name}",
            f"{reference_path} {reference_circuit.name}",
            setup_file or "",
            log_path,
        ]
        try:
            completed = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_s
            )
        except FileNotFoundError as exc:
            # `_resolve_netgen_binary` already proved this path was an
            # executable file, so reaching here means it stopped being one
            # between that check and the spawn (an upgrade/uninstall mid-run,
            # a broken symlink target). Still an actionable application
            # error, never a traceback -- and it names the resolved binary,
            # since "install netgen" is not the remediation for this one.
            raise LvsError(
                f"could not launch netgen: '{binary}' could not be executed "
                "-- it resolved on PATH but is no longer a runnable file. "
                f"({exc})"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise LvsError(
                f"netgen did not complete within {timeout_s}s (raise "
                "options.netgen_timeout_s to allow more time)"
            ) from exc

        engine_version = None
        version_match = _NETGEN_ENGINE_VERSION_RE.search(completed.stdout or "")
        if version_match:
            engine_version = version_match.group(1)

        if not os.path.isfile(log_path):
            # netgen exits 0 even when it never got as far as comparing
            # anything (e.g. a malformed netlist file) -- verified empirically
            # for this issue (see the design-doc addendum). No report file at
            # all means no trustworthy verdict is possible; surface netgen's
            # own stdout (its errors go there, not to the log file) rather
            # than a bare "no report" message.
            raise LvsError(
                "netgen did not produce a report file -- it likely failed to "
                "read one of the input netlists. netgen's own output:\n"
                # Issue #1969: netgen's text reaches the caller here too (as
                # the error envelope's `message`), so it gets the same
                # fold-in sanitization `_parse_netgen_report` applies.
                + _strip_ansi(completed.stdout or completed.stderr or "").strip()
            )
        try:
            with open(log_path, encoding="utf-8", errors="replace") as handle:
                log_text = handle.read()
        except OSError as exc:
            raise LvsError(f"could not read netgen report '{log_path}': {exc}") from exc

        status, mismatches = _parse_netgen_report(
            log_text, stdout=completed.stdout, engine_version=engine_version
        )
        return status, mismatches, engine_version
    finally:
        _cleanup_netgen_work_dir(work_dir)


def _cleanup_netgen_work_dir(work_dir: str) -> None:
    shutil.rmtree(work_dir, ignore_errors=True)


def _locate_netgen_verdict(text: str) -> tuple[str, int] | None:
    """Return ``(marker, index)`` for the right-most recognised verdict
    marker (:data:`_NETGEN_VERDICT_MARKERS`) in ``text``, or ``None`` if
    neither is present.

    ``"Final result:"`` is checked first and returned immediately when
    found, even if ``"Result:"`` also occurs (e.g. as part of some other
    line) -- it is the more specific marker and, per issue #343, the one a
    from-source netgen 1.5.323 build is verified to emit.
    """
    for marker in _NETGEN_VERDICT_MARKERS:
        idx = text.rfind(marker)
        if idx != -1:
            return marker, idx
    return None


def _parse_netgen_report(
    log_text: str,
    *,
    stdout: str | None = None,
    engine_version: str | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Classify netgen's ``comp.out`` log text into ``(status, mismatches)``.

    Verdict text (verified empirically against a from-source netgen 1.5.323
    build for this issue -- see the design-doc addendum for the four
    scenarios exercised):

    - ``"Final result: Circuits match uniquely."`` -- a unique topological
      match. Still downgraded to ``"mismatch"`` if a parameter difference was
      also found (``"Property errors were found."``), consistent with the
      ``klayout`` engine's own ``device.property`` semantics: a real
      parameter defect is never reported as a clean match.
    - ``"Final result: Netlists do not match."`` / ``"...Circuits do not
      match."`` -- a topology mismatch.
    - ``"Final result: Top level cell failed pin matching."`` -- a pin/port
      mismatch (the ``lvs`` Tcl proc's own pre-``verify`` short-circuit).
    - ``"Final result: Subcell(s) failed matching."`` -- a black-boxed
      subcircuit mismatch (same short-circuit).
    - ``"...Circuits match uniquely with port errors."`` -- topologically
      unique but with a pin-count/order disagreement; treated as a mismatch
      (never let a pin disagreement read as a clean match).

    At least one still-current *packaged* netgen build (Debian/Ubuntu's
    ``netgen-lvs`` 1.5.133, built 2022-12-01 -- issue #1192) uses ``"Result:
    ..."`` for this same line instead of ``"Final result: ..."``, *and* only
    ever prints it to the process's own stdout, never into the log file
    passed on the command line. ``log_text`` is always searched first (the
    log is the artifact a caller can inspect after the run, so it takes
    precedence when both have a verdict); ``stdout`` is consulted only when
    ``log_text`` has no recognised marker at all (:data:`_NETGEN_VERDICT_MARKERS`,
    via :func:`_locate_netgen_verdict`). Structured evidence (property-error
    blocks, NET/DEVICE mismatch tables) is always parsed from ``log_text``
    regardless of which source supplied the verdict, since that evidence is
    still written to the log file even on builds affected by this gap.

    The property-error downgrade is keyed on netgen's own declaration
    (:data:`_NETGEN_PROPERTY_ERROR_MARKERS`), not on whether the supporting
    per-parameter lines parsed: when netgen says property errors exist but no
    structured entry could be recovered, a generic ``device.property`` entry
    carrying netgen's raw text in ``details.raw`` is emitted and the verdict is
    still ``"mismatch"``. A recognised verdict line with unparseable *evidence*
    must never read as a clean match either (issue #343 review).

    Raises :class:`LvsError` when no recognised verdict marker is found at
    all (in either ``log_text`` or ``stdout``), or its text matches none of
    the above **and** no other structured evidence (a parsed
    parameter-difference block) was found either -- this is the "must fail
    loud, not soft, on unparseable output" requirement: never let
    unrecognised report text default to ``"match"``. When ``engine_version``
    is supplied (from parsing netgen's own startup banner) and no verdict was
    found at all, it is included in the raised message: this parser's known
    verdict wordings are verified against netgen 1.5.323 (``"Final
    result:"``) and 1.5.133 (``"Result:"``), so a differently-worded verdict
    on some other version is distinguishable at a glance from a genuinely
    malformed netlist, without re-running netgen by hand.
    """
    from .lvs import (
        CATEGORY_DEVICE_PROPERTY,
        CATEGORY_PIN_UNMATCHED,
        CATEGORY_TOPOLOGY,
        LvsError,
    )

    # Issue #1969: strip ANSI escapes from *both* netgen text sources before
    # anything is parsed out of them, so every field this function can
    # populate -- a structured `description`/`property` value, a bucketed
    # `details.raw`, the raw report tail embedded in the unparseable-verdict
    # error below -- is clean by construction rather than per-field. See
    # `_strip_ansi`.
    log_text = _strip_ansi(log_text)
    stdout = _strip_ansi(stdout) if stdout else stdout

    verdict = _locate_netgen_verdict(log_text)
    source_text = log_text
    if verdict is None and stdout:
        verdict = _locate_netgen_verdict(stdout)
        if verdict is not None:
            source_text = stdout
    if verdict is None:
        version_note = (
            f" netgen version detected: {engine_version}."
            if engine_version
            else " netgen version could not be detected."
        )
        raise LvsError(
            "could not parse netgen's LVS report: no 'Final result:' or "
            "'Result:' verdict line found in the report log or netgen's own "
            "stdout -- the report format may be unrecognised or netgen may "
            "have exited before completing the compare. This parser is "
            "verified against netgen 1.5.323 and 1.5.133." + version_note + " "
            "Raw report (last 2000 chars):\n" + log_text.strip()[-2000:]
        )
    marker, idx = verdict
    tail = source_text[idx + len(marker) :].strip()
    first_line = tail.splitlines()[0] if tail else ""

    mismatches = _parse_netgen_property_errors(log_text)

    # netgen's own declaration that property errors exist is the authoritative
    # signal for the match -> mismatch downgrade -- NOT whether the supporting
    # per-parameter lines happened to parse. Keying the downgrade on the parse
    # result is how a real string-valued property difference
    # (`(exact match req'd)`, which the old line regex could not match) became
    # a clean `"match"` with an empty `mismatches[]` (issue #343 review).
    if _declares_netgen_property_errors(log_text) and not mismatches:
        mismatches.append(
            _mismatch(
                CATEGORY_DEVICE_PROPERTY,
                "error",
                "netgen reported property errors on one or more matched "
                "devices, but the per-parameter detail lines could not be "
                "parsed -- see the 'details.raw' field for netgen's own text",
                "both",
                details={"raw": _netgen_property_error_context(log_text)},
            )
        )

    is_clean_unique_match = tail.startswith("Circuits match uniquely.") and (
        "port errors" not in tail
    )
    if is_clean_unique_match:
        if mismatches:
            return "mismatch", mismatches
        return "match", []

    mismatches.extend(_parse_netgen_section_blocks(log_text))

    if tail.startswith("Top level cell failed pin matching."):
        mismatches.append(
            _mismatch(
                CATEGORY_PIN_UNMATCHED,
                "error",
                "netgen: top-level cell failed pin matching",
                "both",
            )
        )
    elif tail.startswith("Subcell(s) failed matching."):
        mismatches.append(
            _mismatch(
                CATEGORY_TOPOLOGY,
                "error",
                "netgen: one or more subcircuits failed to match",
                "both",
            )
        )
    elif "do not match" in tail or ("match uniquely" in tail and "port errors" in tail):
        # "Netlists do not match." / "Circuits do not match." / "Circuits
        # match uniquely with port errors." -- if the section-block parse
        # above already found NET/DEVICE mismatch blocks (the common case
        # for a topology mismatch), those are the detailed findings; only
        # add a generic entry when nothing more specific was recovered, so
        # the caller has *something* rather than an empty `mismatches[]` on
        # a documented mismatch verdict.
        if not mismatches:
            mismatches.append(
                _mismatch(
                    CATEGORY_TOPOLOGY,
                    "error",
                    f"netgen: {first_line}",
                    "both",
                )
            )
    elif not mismatches:
        # An unrecognised, non-empty verdict text with no other structured
        # evidence at all: never guess this is a match.
        raise LvsError(
            f"could not classify netgen's LVS verdict: unrecognised {marker!r} "
            f"text {first_line!r}. Raw report tail:\n" + tail[:2000]
        )

    return "mismatch", mismatches


def _declares_netgen_property_errors(log_text: str) -> bool:
    """Whether netgen itself declared parameter (property) errors anywhere in
    the report -- see :data:`_NETGEN_PROPERTY_ERROR_MARKERS`."""
    return any(marker in log_text for marker in _NETGEN_PROPERTY_ERROR_MARKERS)


def _netgen_property_error_context(log_text: str) -> str:
    """Best-effort raw text for a property error netgen declared but whose
    per-parameter lines this module could not structure.

    Returns the ``"...match uniquely with property errors"`` summary line and
    the indented block that follows it when present (netgen's own evidence),
    otherwise the tail of the report -- so ``details.raw`` always carries
    something a human/agent can act on rather than an empty string.
    """
    lines = log_text.splitlines()
    for line_no, line in enumerate(lines):
        if "match uniquely with property errors" not in line:
            continue
        block = [line.strip()]
        for following in lines[line_no + 1 :]:
            if not following.strip():
                break
            block.append(following.rstrip())
        return "\n".join(block)
    return log_text.strip()[-2000:]


def _describe_netgen_property_delta(qualifier: str | None) -> str:
    """Render netgen's trailing per-property qualifier for a ``description``.

    Handles both observed shapes -- ``delta=66.7%, cutoff=1%`` (numeric
    tolerance compare) and ``exact match req'd`` (string-valued property) --
    and passes any other wording through verbatim rather than dropping the
    line, so an unfamiliar qualifier still yields a structured entry.
    """
    if not qualifier:
        return "netgen reported a property difference"
    delta_match = _NETGEN_PROPERTY_DELTA_RE.match(qualifier.strip())
    if delta_match is not None:
        delta, cutoff = delta_match.groups()
        return f"delta={delta}, cutoff={cutoff}"
    return qualifier.strip()


def _parse_netgen_property_errors(log_text: str) -> list[dict[str, Any]]:
    """Parse netgen's parameter-difference block(s) into ``device.property``
    ``mismatches[]`` entries -- see :data:`_NETGEN_PROPERTY_BLOCK_RE` for the
    exact text shape this matches.

    A body line that does not match :data:`_NETGEN_PROPERTY_LINE_RE` at all is
    **not** dropped: it becomes a best-effort entry carrying the raw line in
    ``details.raw``. Silently discarding evidence netgen printed is what
    allowed a declared property error to surface as a clean ``"match"``
    (issue #343 review); the caller's marker-based guard is the backstop, and
    this is the per-line half of the same rule.
    """
    from .lvs import CATEGORY_DEVICE_PROPERTY

    entries: list[dict[str, Any]] = []
    for block_match in _NETGEN_PROPERTY_BLOCK_RE.finditer(log_text):
        class1, index1, class2, index2, body = block_match.groups()
        device_layout = f"{class1}:{index1}"
        device_reference = f"{class2}:{index2}"

        def _device(
            layout: str = device_layout,
            reference: str = device_reference,
            device_class: str = class1,
        ) -> dict[str, Any]:
            # A fresh dict per entry: `mismatches[]` entries must not share
            # mutable sub-objects across the list.
            return {
                "layout": layout,
                "reference": reference,
                "class": device_class,
            }

        for line in body.splitlines():
            if not line.strip():
                continue
            line_match = _NETGEN_PROPERTY_LINE_RE.match(line)
            if line_match is None:
                entries.append(
                    _mismatch(
                        CATEGORY_DEVICE_PROPERTY,
                        "error",
                        "netgen reported a matched-device property difference "
                        "in a form this parser does not structure -- see the "
                        "'details.raw' field for netgen's own text",
                        "both",
                        device=_device(),
                        details={"raw": line.strip()},
                    )
                )
                continue
            name, layout_value, reference_value, qualifier = line_match.groups()
            entries.append(
                _mismatch(
                    CATEGORY_DEVICE_PROPERTY,
                    "error",
                    f"netgen: matched device parameter '{name}' differs "
                    f"({_describe_netgen_property_delta(qualifier)})",
                    "both",
                    device=_device(),
                    property_={
                        "name": name,
                        "layout": layout_value,
                        "reference": reference_value,
                    },
                )
            )
    return entries


def _parse_netgen_section_blocks(log_text: str) -> list[dict[str, Any]]:
    """Bucket netgen's ``NET mismatches:``/``DEVICE mismatches:`` side-by-side
    report tables into one generic entry per section, with the raw block
    preserved verbatim in ``details.raw``.

    These tables are fixed-width, pipe-delimited, and use filler cells like
    ``"(no matching net)"`` for a one-sided row -- parsing them into precise
    per-net/per-device entries (mirroring the ``klayout`` engine's
    ``net.unmatched``/``device.unmatched`` granularity) would require
    trusting column alignment that is not a documented, versioned contract
    of netgen's own report format. Per this issue's scope ("fields that
    don't map cleanly onto that shape go into a mismatch-level `details`
    object, not a schema fork"), this module buckets instead of guessing.
    """
    from .lvs import CATEGORY_DEVICE_UNMATCHED, CATEGORY_NET_UNMATCHED

    headers = (
        ("NET mismatches:", CATEGORY_NET_UNMATCHED, "net mismatch(es)"),
        ("DEVICE mismatches:", CATEGORY_DEVICE_UNMATCHED, "device mismatch(es)"),
    )

    entries: list[dict[str, Any]] = []
    for header, category, label in headers:
        start = log_text.find(header)
        if start == -1:
            continue
        end = len(log_text)
        for boundary in _NETGEN_SECTION_BOUNDARIES:
            boundary_pos = log_text.find(boundary, start + len(header))
            if boundary_pos != -1:
                end = min(end, boundary_pos)
        block = log_text[start:end].strip()
        entries.append(
            _mismatch(
                category,
                "error",
                f"netgen reported one or more {label} -- see the "
                "'details.raw' field for netgen's own side-by-side report",
                "both",
                details={"raw": block},
            )
        )
    return entries
