"""Run a cocotb regression against Icarus/Verilator, headless.

Pure library: :func:`run_functional_verification` returns plain Python data
(a ``dict`` of JSON-serialisable primitives) and never prints, mirroring
``lvs.py``/``sim.py``/``synthesize.py``. Serialisation and human-readable
formatting live in the CLI command module
(``cli/functional_verification_cmd.py``).

This is Phase 3 of [Epic #391](https://github.com/2AMLogic/klayout-tools/issues/391)
("adopt the digital engine class -- Yosys + OpenROAD -- RTL->GDS as a
first-class ``klt`` flow"), the build carried by two accepted Phase 1
spikes -- read them first:

- ``docs/design/cocotb-verification-spike.md`` (#398) settles the engine
  survey, the invocation surface, and the request/response contract.
- ``docs/design/digital-flow-contracts-spike.md`` section 6 (#399) restates
  that contract alongside the synthesis/place-and-route ones.

Five findings from those spikes are load-bearing here, and each is
implemented deliberately rather than rediscovered:

1. **Never trust a raw subprocess/``make`` exit code** (spike section 4).
   The same deliberately-failing regression was observed exiting ``2``,
   ``1``, and ``0`` through three different invocation paths. This module
   invokes cocotb's first-party Python ``Runner`` API (never the Makefile
   flow), and derives *every* count and the final verdict from the
   ``results.xml`` artifact it parses itself. ``cocotb_tools.runner``'s own
   ``Runner.test()`` can either ``sys.exit()`` on a simulator's nonzero
   return *or* return normally on a run with failing tests (both observed
   live) -- so the :class:`SystemExit` it may raise is caught and discarded
   in favour of the parsed artifact.
2. **``get_results()`` is not the source of truth** (spike section 4). Its
   ``num_tests`` counts skipped tests too, so it cannot produce the
   passed/failed/skipped breakdown the contract reports. This module parses
   ``results.xml`` directly: a ``<testcase>`` with no child is a pass, a
   ``<failure>`` child is a failure, a ``<skipped>`` child is a skip.
3. **``timescale`` must be passed to both ``Runner.build()`` and
   ``Runner.test()``** (spike section 3b) -- Icarus elaboration otherwise
   fails the moment a testbench's ``Clock(..., unit="ns")`` meets an unset
   (default 1 s) simulator precision.
4. **``options.coverage: true`` requires ``engine: "verilator"``** (spike
   sections 1/5) -- Icarus has no coverage path through this flow at all, so
   the combination is rejected up front rather than silently no-op'ing.
5. **``random_seed`` reproducibility** (spike's own "open questions", issue
   #423). ``request.options.random_seed``, when given, is forwarded to
   ``Runner.test()``'s own ``seed`` parameter (which sets
   ``COCOTB_RANDOM_SEED`` in the simulator subprocess's environment) so a
   pinned seed reproduces cocotb's own seeded ``random`` module state
   run-to-run. Whether pinned or left to cocotb's own generator, the
   *effective* seed is always echoed back in ``environment.random_seed`` --
   read from ``results.xml``'s own ``<property name="random_seed">``
   element (spike section 4), the same artifact-derived-truth discipline as
   every other count this module reports -- matching the reproducibility
   bar ``klt sim``'s Monte Carlo seeding and ``klt lvs``'s ``environment``
   hashes already set.

6. **SDF back-annotation is Icarus-only, and every one of its failure modes
   is non-fatal to the simulator's own exit code** (issue #1002,
   ``docs/design/sdf-annotate-feasibility-spike.md``, verified live on Icarus
   13.0). ``request.options.sdf`` re-runs a gate-level regression with real
   post-route delays back-annotated from an SDF file (the one
   ``klt place-and-route``'s ``request.post_route_sdf`` writes). Four things
   that spike established are implemented here rather than rediscovered:
   the ``$sdf_annotate`` call rides in a *generated second elaboration root*
   (:func:`_write_sdf_annotate_shim`) because a cocotb regression's
   ``hdl_toplevel`` is the DUT itself and has nowhere to put an ``initial``
   block; ``-gspecify`` and ``-ginterconnect`` are both mandatory and their
   absence is a **silent** zero-delay run; min/typ/max corner selection is
   the compile-time ``iverilog -T`` flag, not an ``$sdf_annotate`` argument;
   and the SDF path must be **absolute**, since a relative one resolves
   against ``vvp``'s own working directory. Above all, ``vvp`` exits ``0``
   after skipping an annotation it could not apply -- so
   :func:`_scan_sdf_diagnostics` treats an ``SDF WARNING``/``SDF ERROR`` line
   in either transcript as a failed run. This is the same "never trust a raw
   exit code, derive the verdict from the artifact" discipline as finding 1,
   one level deeper.

7. **A top-level-port-attached ``INTERCONNECT`` entry needs the DUT nested,
   not rooted** (issue #1056, live on Icarus 13.0). Every real post-route
   SDF's top design ``CELL`` block mixes purely-internal
   ``INTERCONNECT <inst>.<pin> <inst>.<pin>`` entries (always resolved fine)
   with entries touching a bare top-level port name, e.g.
   ``INTERCONNECT a buffer0.in`` (a primary input) or
   ``INTERCONNECT buffer2.out b`` (a primary output). The latter always
   failed with ``SDF ERROR: ... Could not find intermodpath!`` /
   ``Could not find net`` -- live testing ruled out the previously-suspected
   mechanism (this module's own generated-sibling-elaboration-root shim):
   calling ``$sdf_annotate`` from *inside* the DUT's own scope reproduces the
   identical failure, so the shim's placement was never the cause. The real
   mechanism: Icarus cannot resolve a bare port identifier against a module
   elaborated as its *own* ``-s`` root at all -- only against a module
   elaborated as a *nested child instance* of another root (confirmed against
   Icarus's own ``ivtest`` SDF regression fixtures, which use exactly that
   shape). :func:`_write_sdf_dut_wrapper` supplies it: a generated,
   transparent pass-through wrapper re-declares ``hdl_toplevel``'s exact port
   list, instantiates the real (unmodified) DUT as a nested child, and
   becomes the new elaboration root in ``hdl_toplevel``'s place -- verified
   live end-to-end through real cocotb + Icarus 13.0, not just raw
   ``iverilog``/``vvp``. Every other build path (baseline gate-level, RTL,
   coverage) is unaffected: the wrapper is only generated when
   ``request.options.sdf`` is present.

Engines: ``"icarus"`` (default -- the CI-cheap interpreter) and
``"verilator"`` (opt-in, required for coverage). cocotb itself is an
*optional* runtime dependency, deliberately not in ``pyproject.toml``'s
``dependencies``: cocotb 2.0.1 refuses to run on Python 3.14+ while this
repo supports 3.10+ with no upper bound, so pinning it would break ``klt``
installs that never verify anything. It is discovered at call time and a
missing install is a clear, actionable error -- exactly the posture ``klt
synthesize`` takes toward a missing ``yosys`` binary.
"""

from __future__ import annotations

import importlib.metadata
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ElementTree
from typing import Any

from ._paths import _load_request_json
from ._paths import load_request_arg as _shared_load_request_arg
from ._paths import validate_request_shape as _shared_validate_request_shape

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- see docs/json-contract.md.
SCHEMA_VERSION = 1

#: ``"icarus"`` first: it is the documented default, per the spike's own
#: CI-cost finding (~0.7 s vs. Verilator's ~8 s for the worked example).
SUPPORTED_ENGINES = ("icarus", "verilator")
DEFAULT_ENGINE = "icarus"

#: Only Verilator has a coverage path through this flow (spike section 1).
COVERAGE_ENGINES = ("verilator",)

#: Verilator build args for a coverage run, per the spike's section 5 recipe.
COVERAGE_BUILD_ARGS = ("--coverage", "--trace")

#: ``[unit, precision]``, passed to *both* build and test (see gotcha 3).
DEFAULT_TIMESCALE = ("1ns", "1ps")

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

#: ``iverilog`` flags an SDF-annotated build requires, all three verified
#: live (spike §3.1/§3.2/§2.1): without ``-gspecify`` Icarus omits every
#: specify block *and* the ``$sdf_annotate`` call itself, then runs at zero
#: delay and exits ``0``; without ``-ginterconnect`` every ``INTERCONNECT``
#: entry -- which is exactly what a post-route SDF carries -- fails at run
#: time; and ``-s <module>`` is what makes the generated shim a second
#: elaboration root rather than dead code.
SDF_BUILD_ARGS = ("-gspecify", "-ginterconnect", "-s", SDF_ANNOTATE_MODULE)

#: ``options.sdf.corner`` values, mapping 1:1 onto ``iverilog -T``. Corner
#: selection is a *compile-time* flag for Icarus, not an ``$sdf_annotate``
#: argument: Icarus ignores every argument past the second ("$sdf_annotate
#: currently only uses the first two argument", its own wording), so a
#: request that named a corner there would be silently ignored (spike §3.5).
SDF_CORNERS = ("min", "typ", "max")

#: Icarus's own default when ``-T`` is not passed; named explicitly so the
#: response's echo is never ambiguous about which corner was simulated.
DEFAULT_SDF_CORNER = "typ"

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

#: A human-readable reason per benign class (:data:`SDF_BENIGN_DIAGNOSTIC_SUBSTRINGS`,
#: lowercased), surfaced in ``environment.sdf.dropped`` (issue #1102) so a
#: caller can explain *why* a class was dropped without re-deriving it from
#: this module's own comments.
SDF_BENIGN_DIAGNOSTIC_REASONS: dict[str, str] = {
    "timingcheck": (
        "Icarus Verilog implements SDF delay annotation (IOPATH/INTERCONNECT) "
        "but not SDF TIMINGCHECK -- every TIMINGCHECK section in the SDF is "
        "dropped, so $setup/$hold/$width checks run against the cell "
        "library's own placeholder timing, not the characterised limits in "
        "the SDF"
    ),
}

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

#: The exact transcript signature cocotb's ``libembed`` emits when its
#: compiled VPI module was ``dlopen``'d by a different CPython than the one
#: cocotb was built for (issue #1103's live finding) -- e.g. a ``cp312``
#: wheel loaded inside a CPython 3.14 simulator subprocess. The pre-flight
#: ``WHEEL``-tag check (:func:`_check_cocotb_abi_compatibility`) catches this
#: *before* the runner is invoked in the common case; these markers are the
#: fallback for whatever that check doesn't cover (unreadable wheel metadata,
#: a mismatch pattern the tag comparison misses), scanned only once
#: ``results.xml`` is confirmed missing.
INTERPRETER_MISMATCH_MARKERS = (
    "Unexpected sys.executable value",
    "_embed_init_python",
)

_ENGINE_VERSION_COMMANDS = {
    "icarus": (["iverilog", "-V"], re.compile(r"Icarus Verilog version (\S+)")),
    "verilator": (["verilator", "--version"], re.compile(r"Verilator (\S+)")),
}

_COVERAGE_SUMMARY_RE = re.compile(
    r"^\s*(line|toggle|branch|expr)\s*:\s*([0-9.]+)%", re.MULTILINE
)


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
    """


# ---------------------------------------------------------------------------
# request loading (same file / "-" / inline-JSON convention as `klt lvs`)
# ---------------------------------------------------------------------------


_REQUIRED_REQUEST_FIELDS = ("sources", "hdl_toplevel", "testbench")


def _validate_request_shape(data: Any, source: str) -> dict[str, Any]:
    """Shared shape check for a JSON-decoded request, however it was sourced
    (file, inline JSON, stdin). ``source`` is folded into the "must be a JSON
    object" error for context."""
    return _shared_validate_request_shape(
        data,
        source,
        error_cls=FunctionalVerificationError,
        required_fields=_REQUIRED_REQUEST_FIELDS,
    )


def load_request(request_path: str) -> dict[str, Any]:
    """Read and minimally validate a ``klt functional-verification`` request
    JSON file.

    Raises :class:`FunctionalVerificationError` if the file is missing/
    unreadable, not valid JSON, or missing a required top-level field
    (``sources``, ``hdl_toplevel``, ``testbench``). Does not require a
    ``schema`` field, matching ``klt lvs``/``klt sim``/``klt synthesize``'s
    own ``load_request`` (user-authored input, never emitted by this tool).
    """
    request = _load_request_json(request_path, FunctionalVerificationError)
    return _validate_request_shape(request, "request file")


def load_request_arg(value: str) -> tuple[dict[str, Any], str]:
    """Resolve the CLI ``request`` argument into a request dict plus the
    directory relative paths inside it should resolve against -- the same
    three forms ``klt lvs``'s own ``request`` argument accepts (a path to a
    JSON file, ``"-"`` for stdin, or an inline JSON object string), so this
    verb composes into ``klt eval``'s descriptor unchanged.
    """
    return _shared_load_request_arg(
        value,
        error_cls=FunctionalVerificationError,
        required_fields=_REQUIRED_REQUEST_FIELDS,
        load_request_fn=load_request,
    )


# ---------------------------------------------------------------------------
# request field validation
# ---------------------------------------------------------------------------


def _resolve_sources(sources: Any, request_dir: str) -> list[str]:
    """Validate ``request.sources`` and resolve each entry to an absolute,
    readable path (relative to ``request_dir`` -- the same convention every
    other request-taking verb uses)."""
    if not isinstance(sources, list) or not sources:
        raise FunctionalVerificationError(
            "request.sources must be a non-empty array of paths"
        )
    if not all(isinstance(entry, str) and entry for entry in sources):
        raise FunctionalVerificationError(
            "request.sources entries must be non-empty strings"
        )

    resolved: list[str] = []
    for entry in sources:
        path = entry if os.path.isabs(entry) else os.path.join(request_dir, entry)
        if not os.path.isfile(path):
            raise FunctionalVerificationError(f"RTL source not found: {entry}")
        try:
            with open(path, "rb"):
                pass
        except OSError as exc:
            raise FunctionalVerificationError(
                f"could not read RTL source '{entry}': {exc}"
            ) from exc
        resolved.append(os.path.abspath(path))
    return resolved


def _resolve_testbench(
    testbench: Any, request_dir: str
) -> tuple[str, str, str | list[str] | None]:
    """Validate ``request.testbench`` and return
    ``(module, module_dir, testcase)``.

    ``module`` is a Python *module name* (``"test_gcd"``), not a path -- it is
    what cocotb's ``Runner.test(test_module=...)`` imports. By default the
    module's file is located next to the request
    (``<request_dir>/<module>.py``) so a missing testbench is a clear
    request-level error here rather than a ``ModuleNotFoundError`` raised deep
    inside the simulator process (where, verified live, cocotb still exits
    ``0`` and simply writes no ``results.xml``).

    An optional ``testbench.search_path`` overrides *where* ``module`` is
    looked up -- resolved absolute-or-relative-to-``request_dir`` the same way
    ``_resolve_sources()`` resolves each ``request.sources`` entry -- so one
    unmodified testbench module can be shared by several requests that live in
    different directories. When omitted, behavior is unchanged: the module is
    resolved next to the request.
    """
    if not isinstance(testbench, dict):
        raise FunctionalVerificationError("request.testbench must be a JSON object")

    module = testbench.get("module")
    if not isinstance(module, str) or not module:
        raise FunctionalVerificationError(
            "request.testbench.module must be a non-empty string"
        )
    if module.endswith(".py") or os.sep in module or "/" in module:
        raise FunctionalVerificationError(
            f"request.testbench.module must be a Python module name, not a "
            f"path: {module!r}"
        )

    search_path = testbench.get("search_path")
    if search_path is not None:
        if not isinstance(search_path, str) or not search_path:
            raise FunctionalVerificationError(
                "request.testbench.search_path must be a non-empty string when given"
            )
        search_dir = (
            search_path
            if os.path.isabs(search_path)
            else os.path.join(request_dir, search_path)
        )
    else:
        search_dir = request_dir

    module_path = os.path.join(search_dir, f"{module}.py")
    if not os.path.isfile(module_path):
        location = (
            "request.testbench.search_path"
            if search_path is not None
            else "next to the request"
        )
        raise FunctionalVerificationError(
            f"testbench module not found: expected '{module_path}' "
            f"(request.testbench.module names a Python module resolved via {location})"
        )

    testcase = testbench.get("testcase")
    if testcase is not None:
        if isinstance(testcase, str):
            if not testcase:
                raise FunctionalVerificationError(
                    "request.testbench.testcase must be a non-empty string when given"
                )
        elif isinstance(testcase, list):
            if not testcase or not all(
                isinstance(entry, str) and entry for entry in testcase
            ):
                raise FunctionalVerificationError(
                    "request.testbench.testcase array entries must be non-empty strings"
                )
        else:
            raise FunctionalVerificationError(
                "request.testbench.testcase must be a string, an array of "
                "strings, or null"
            )

    return module, os.path.dirname(os.path.abspath(module_path)), testcase


def _resolve_options(
    options: Any, engine: str, request_dir: str
) -> tuple[
    bool,
    tuple[str, str],
    int | None,
    dict[str, str | None],
    list[str],
    list[str],
    dict[str, str] | None,
]:
    """Validate ``request.options`` and return
    ``(coverage, timescale, random_seed, defines, build_args, includes, sdf)``.

    Enforces the spike's hard constraint that coverage is a Verilator-only
    capability -- ``options.coverage: true`` with ``engine: "icarus"`` is a
    request error (exit 1), never a silently-ignored flag -- and the exactly
    symmetric constraint for SDF back-annotation, which only Icarus has a
    path for (``options.sdf`` + ``engine: "verilator"`` is a request error,
    never a silent no-op).

    ``sdf`` is ``None`` when the block is absent, else
    ``{"file": <abs path>, "corner": "min"|"typ"|"max"}``. ``file`` is
    resolved relative to ``request_dir`` like every other path field, and is
    returned **absolute** because the generated ``$sdf_annotate`` shim
    embeds it verbatim and ``vvp`` resolves a relative path against its own
    working directory rather than the request's (spike §4.2).
    """
    if options is None:
        options = {}
    if not isinstance(options, dict):
        raise FunctionalVerificationError("request.options must be a JSON object")

    coverage = options.get("coverage", False)
    if not isinstance(coverage, bool):
        raise FunctionalVerificationError("request.options.coverage must be a boolean")
    if coverage and engine not in COVERAGE_ENGINES:
        raise FunctionalVerificationError(
            f"options.coverage requires engine 'verilator' -- engine "
            f"'{engine}' has no coverage path"
        )

    timescale = options.get("timescale")
    if timescale is None:
        resolved_timescale = DEFAULT_TIMESCALE
    elif (
        not isinstance(timescale, (list, tuple))
        or len(timescale) != 2
        or not all(isinstance(entry, str) and entry for entry in timescale)
    ):
        raise FunctionalVerificationError(
            "request.options.timescale must be a [unit, precision] pair of "
            'non-empty strings, e.g. ["1ns", "1ps"]'
        )
    else:
        resolved_timescale = (timescale[0], timescale[1])

    random_seed = options.get("random_seed")
    if random_seed is not None and (
        isinstance(random_seed, bool) or not isinstance(random_seed, int)
    ):
        raise FunctionalVerificationError(
            "request.options.random_seed must be an integer when given"
        )

    defines = _resolve_defines(options.get("defines"))
    build_args = _resolve_build_args(options.get("build_args"))
    includes = _resolve_includes(options.get("includes"), request_dir)
    sdf = _resolve_sdf_option(options.get("sdf"), engine, request_dir)
    _reject_sdf_with_functional_models(sdf, defines)

    return (
        coverage,
        resolved_timescale,
        random_seed,
        defines,
        build_args,
        includes,
        sdf,
    )


def _resolve_defines(defines: Any) -> dict[str, str | None]:
    """Validate the optional ``request.options.defines`` object and return it
    unchanged (or ``{}`` when omitted).

    Forwarded to ``Runner.build(defines=...)`` -- cocotb's own ``Runner``
    already accepts a ``Mapping[str, str | None]`` (a ``None`` value defines
    the macro with no value, e.g. ``` `define USE_POWER_PINS ```), so this
    module never needs to translate the mapping itself.
    """
    if defines is None:
        return {}
    if not isinstance(defines, dict):
        raise FunctionalVerificationError(
            "request.options.defines must be a JSON object of string -> string|null"
        )
    for key, value in defines.items():
        if not isinstance(key, str) or not key:
            raise FunctionalVerificationError(
                "request.options.defines keys must be non-empty strings"
            )
        if value is not None and not isinstance(value, str):
            raise FunctionalVerificationError(
                f"request.options.defines[{key!r}] must be a string or null "
                f"-- got a {type(value).__name__}"
            )
    return defines


def _resolve_build_args(build_args: Any) -> list[str]:
    """Validate the optional ``request.options.build_args`` array and return
    it unchanged (or ``[]`` when omitted).

    Composed with -- never replacing -- the fixed :data:`COVERAGE_BUILD_ARGS`
    list when ``options.coverage: true``: the caller appends these *after*
    the coverage args, so a user-supplied flag can still override a coverage
    default if the two conflict (see :func:`run_functional_verification`).
    """
    if build_args is None:
        return []
    if not isinstance(build_args, list) or not all(
        isinstance(entry, str) and entry for entry in build_args
    ):
        raise FunctionalVerificationError(
            "request.options.build_args must be an array of non-empty strings"
        )
    return list(build_args)


def _resolve_includes(includes: Any, request_dir: str) -> list[str]:
    """Validate the optional ``request.options.includes`` array and resolve
    each entry to an absolute directory path (relative to ``request_dir`` --
    the same convention :func:`_resolve_sources`/:func:`_resolve_testbench`
    already use), or ``[]`` when omitted.

    Forwarded to ``Runner.build(includes=...)`` for a cell library split
    across multiple files with `` `include `` directives.
    """
    if includes is None:
        return []
    if not isinstance(includes, list) or not all(
        isinstance(entry, str) and entry for entry in includes
    ):
        raise FunctionalVerificationError(
            "request.options.includes must be an array of non-empty strings"
        )

    resolved: list[str] = []
    for entry in includes:
        path = entry if os.path.isabs(entry) else os.path.join(request_dir, entry)
        if not os.path.isdir(path):
            raise FunctionalVerificationError(f"include directory not found: {entry}")
        resolved.append(os.path.abspath(path))
    return resolved


def _reject_sdf_with_functional_models(
    sdf: dict[str, str] | None, defines: dict[str, str | None]
) -> None:
    """``options.sdf`` together with a ``FUNCTIONAL`` define is
    self-contradictory, and is rejected rather than run (issue #1004's second
    finding, folded into #1002's own implementation).

    A PDK's behavioural cell models put their zero-delay models and their
    SDF-annotatable *timing* models in the two branches of the same
    `` `ifdef FUNCTIONAL `` guard, and only the non-``FUNCTIONAL`` branch
    carries the ``specify`` blocks an SDF's ``IOPATH`` entries annotate. So a
    request that asks for real post-route delays *and* selects the zero-delay
    models is asking for two incompatible things: at best the annotation
    matches nothing, at worst (Icarus 12.0, observed in #1004) the run
    silently mis-simulates and every flop samples ``x`` with no error raised.

    Either failure lands as a *quietly wrong* verdict, which is the exact
    class this feature's transcript gate exists to prevent -- so it is
    checked at request-validation time, the same posture
    ``options.coverage`` + ``engine: "icarus"`` already takes.
    """
    if sdf is None or "FUNCTIONAL" not in defines:
        return
    raise FunctionalVerificationError(
        "options.sdf cannot be combined with a 'FUNCTIONAL' define -- a PDK's "
        "FUNCTIONAL cell models are the zero-delay branch of the same `ifdef "
        "that guards the timing models, so they carry none of the specify "
        "blocks an SDF's IOPATH entries annotate. Drop the FUNCTIONAL define "
        "to re-simulate with real delays, or drop options.sdf to keep the "
        "zero-delay run"
    )


def _resolve_sdf_option(
    sdf: Any, engine: str, request_dir: str
) -> dict[str, str] | None:
    """Validate the optional ``request.options.sdf`` block (issue #1002) and
    return ``{"file": <abs path>, "corner": ...}``, or ``None`` when absent.

    Unknown keys are rejected rather than ignored. The block's whole surface
    is two fields, and both of the plausible typos (``"corners"``,
    ``"path"``) would otherwise degrade to a *silently different run* -- the
    wrong timing corner, or no annotation at all -- which is precisely the
    class of failure this feature's own transcript gate exists to prevent.
    """
    if sdf is None:
        return None
    if not isinstance(sdf, dict):
        raise FunctionalVerificationError("request.options.sdf must be a JSON object")
    if engine not in SDF_ENGINES:
        raise FunctionalVerificationError(
            f"options.sdf requires engine 'icarus' -- engine '{engine}' has no "
            "SDF back-annotation path ($sdf_annotate is an Icarus-only entry "
            "point through this flow)"
        )

    unknown = sorted(set(sdf) - {"file", "corner"})
    if unknown:
        raise FunctionalVerificationError(
            "request.options.sdf has unknown field(s): "
            + ", ".join(unknown)
            + " (supported: file, corner)"
        )

    file_value = sdf.get("file")
    if not isinstance(file_value, str) or not file_value:
        raise FunctionalVerificationError(
            "request.options.sdf.file must be a non-empty string"
        )
    path = (
        file_value
        if os.path.isabs(file_value)
        else os.path.join(request_dir, file_value)
    )
    if not os.path.isfile(path):
        raise FunctionalVerificationError(f"SDF file not found: {file_value}")
    try:
        with open(path, "rb"):
            pass
    except OSError as exc:
        raise FunctionalVerificationError(
            f"could not read SDF file '{file_value}': {exc}"
        ) from exc

    corner = sdf.get("corner", DEFAULT_SDF_CORNER)
    if corner not in SDF_CORNERS:
        raise FunctionalVerificationError(
            "request.options.sdf.corner must be one of: " + ", ".join(SDF_CORNERS)
        )

    return {"file": os.path.abspath(path), "corner": corner}


def _write_sdf_annotate_shim(path: str, *, sdf_path: str, scope: str) -> None:
    """Write the extra elaboration root that carries the ``$sdf_annotate``
    call (spike §2.1, verified live; the same shape cocotb's own Icarus
    runner generates for waveform dumping).

    ``sdf_path`` must already be absolute -- ``vvp`` runs with its own
    working directory, so a relative path here is one ``SDF WARNING`` away
    from a silent zero-delay run. ``scope`` is the Verilog hierarchical
    reference ``$sdf_annotate``'s SDF paths resolve relative to -- since
    issue #1056, this is **not** ``hdl_toplevel`` directly, but
    ``<SDF_WRAPPER_MODULE>.<SDF_WRAPPER_DUT_INSTANCE>``: the DUT nested one
    level under the generated pass-through wrapper (see
    :func:`_write_sdf_dut_wrapper`), which is what lets Icarus resolve a
    bare top-level-port ``INTERCONNECT`` endpoint at all. The DUT's own
    hierarchy (the thing the Python testbench addresses as ``dut.<port>``,
    now via the wrapper's identically-named ports) is untouched either way.
    """
    escaped = sdf_path.replace("\\", "\\\\").replace('"', '\\"')
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            "// Generated by klt functional-verification -- do not edit.\n"
            "// Extra elaboration root carrying the $sdf_annotate call, since\n"
            "// a cocotb regression's hdl_toplevel is the DUT itself and has\n"
            "// nowhere to host an initial block (issue #1002).\n"
            f"module {SDF_ANNOTATE_MODULE}();\n"
            f'  initial $sdf_annotate("{escaped}", {scope});\n'
            "endmodule\n"
        )


#: Two Verilog module-header conventions :func:`_parse_toplevel_ports`
#: supports: non-ANSI (``module <name>(<bare port names>);``, directions
#: declared separately in the body -- what both `klt synthesize`'s Yosys
#: ``write_verilog`` and `klt place-and-route`'s OpenROAD ``write_verilog``
#: emit, verified against ``tests/corpus/statime/gcd_netlist.v``) and
#: ANSI-style (``module <name>(input wire a, output [7:0] y);``, direction
#: inline per port -- what this module's own hand-authored integration-test
#: fixtures use). A *mixed* header (some ports carry an inline direction,
#: others don't -- Verilog's "inherit the previous port's direction" rule)
#: is rejected rather than guessed at.
_ANSI_PORT_TOKEN_RE = re.compile(
    r"^(input|output|inout)\b(?:\s+(?:reg|wire|signed))*\s*(\[[^\]]+\])?\s*"
    r"([A-Za-z_$][A-Za-z0-9_$]*)$"
)


def _parse_toplevel_ports(
    source_paths: list[str], hdl_toplevel: str
) -> list[tuple[str, str, str]]:
    """Return ``[(direction, width_or_empty, name), ...]`` for every port of
    ``hdl_toplevel``'s module declaration, in header order -- the port list
    :func:`_write_sdf_dut_wrapper` needs to re-declare identically on the
    generated wrapper (issue #1056).

    Raises :class:`FunctionalVerificationError` (never mis-parses silently)
    when the module cannot be found, its port list mixes ANSI and non-ANSI
    ports, or a non-ANSI port's direction/width declaration cannot be
    located -- any of which would otherwise produce a wrapper that fails to
    compile or, worse, compiles with the wrong port shape.
    """
    header_re = re.compile(
        r"module\s+" + re.escape(hdl_toplevel) + r"\s*\(([^;]*?)\)\s*;", re.DOTALL
    )
    text: str | None = None
    header_match = None
    for path in source_paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                candidate = handle.read()
        except OSError:
            continue
        match = header_re.search(candidate)
        if match is not None:
            text, header_match = candidate, match
            break
    if header_match is None or text is None:
        raise FunctionalVerificationError(
            f"options.sdf could not find a 'module {hdl_toplevel}(...)' "
            "declaration in request.sources to build the SDF top-level-port "
            "workaround wrapper (issue #1056)"
        )

    tokens = [
        " ".join(token.split()).lstrip("\\")
        for token in header_match.group(1).split(",")
        if token.strip()
    ]
    if not tokens:
        raise FunctionalVerificationError(
            f"options.sdf: module '{hdl_toplevel}' declares no ports -- "
            "nothing for the SDF top-level-port workaround wrapper to forward"
        )

    ansi_matches = [_ANSI_PORT_TOKEN_RE.match(token) for token in tokens]
    is_ansi = [match is not None for match in ansi_matches]
    if any(is_ansi) and not all(is_ansi):
        raise FunctionalVerificationError(
            f"options.sdf: module '{hdl_toplevel}' mixes ANSI-style ports "
            "(inline direction keywords) with bare port names -- the SDF "
            "top-level-port workaround (issue #1056) does not resolve "
            "Verilog's 'inherit the previous port's direction' rule for this"
        )

    if all(is_ansi):
        return [
            (match.group(1), (match.group(2) or "").strip(), match.group(3))
            for match in ansi_matches
        ]

    # Non-ANSI: `tokens` are bare port names; direction/width is declared
    # separately in the body, one `input`/`output`/`inout` line per port
    # (Yosys/OpenROAD convention).
    ports: list[tuple[str, str, str]] = []
    for name in tokens:
        decl_re = re.compile(
            r"^[ \t]*(input|output|inout)\b"
            r"(?:[ \t]+(?:reg|wire|signed))*"
            r"[ \t]*(\[[^\]]+\])?[ \t]+" + re.escape(name) + r"[ \t]*;",
            re.MULTILINE,
        )
        decl_match = decl_re.search(text)
        if decl_match is None:
            raise FunctionalVerificationError(
                "options.sdf could not find a direction declaration for "
                f"port '{name}' of module '{hdl_toplevel}' -- cannot build "
                "the SDF top-level-port workaround wrapper (issue #1056)"
            )
        ports.append((decl_match.group(1), (decl_match.group(2) or "").strip(), name))
    return ports


def _write_sdf_dut_wrapper(
    path: str, *, hdl_toplevel: str, ports: list[tuple[str, str, str]]
) -> None:
    """Write a transparent pass-through wrapper module around ``hdl_toplevel``
    -- the client-side fix for issue #1056.

    Icarus's ``$sdf_annotate`` cannot resolve an ``INTERCONNECT`` entry whose
    endpoint is a bare top-level port of a module elaborated as its own
    ``-s`` root (verified live: every purely-internal
    ``INTERCONNECT <inst>.<pin> <inst>.<pin>`` entry resolves fine; every
    entry touching a top-level port fails with ``Could not find
    intermodpath!``/``Could not find net`` -- regardless of whether
    ``$sdf_annotate`` is called from a sibling elaboration root or from
    inside the DUT's own initial block, which rules out this module's
    previous separate-elaboration-root shim as the mechanism). The identical
    bare-port SDF syntax resolves cleanly when the named module is instead a
    *nested child instance* of another root (Icarus's own ``ivtest`` SDF
    regression fixtures use exactly this shape). This wrapper supplies that
    shape without touching the DUT's own netlist, ports, or hierarchy: it
    re-declares ``hdl_toplevel``'s exact port list (``ports``, from
    :func:`_parse_toplevel_ports`), instantiates the unmodified DUT as
    :data:`SDF_WRAPPER_DUT_INSTANCE`, and becomes the new elaboration root in
    its place -- so cocotb's own ``dut.<port>`` handles keep resolving
    unchanged (the wrapper's ports carry the identical names and widths),
    while ``$sdf_annotate``'s scope argument
    (:func:`_write_sdf_annotate_shim`) can now name
    ``<SDF_WRAPPER_MODULE>.<SDF_WRAPPER_DUT_INSTANCE>``, a genuinely nested
    scope. Verified live end-to-end through real cocotb + Icarus 13.0, not
    just raw ``iverilog``/``vvp``.
    """
    port_lines = ",\n".join(
        f"  {direction} {(width + ' ') if width else ''}{name}".rstrip()
        for direction, width, name in ports
    )
    connections = ",\n".join(f"    .{name}({name})" for _, _, name in ports)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            "// Generated by klt functional-verification -- do not edit.\n"
            "// Transparent pass-through wrapper working around Icarus's\n"
            "// inability to resolve a top-level-port INTERCONNECT entry\n"
            "// against a module elaborated as its own -s root (issue #1056).\n"
            f"module {SDF_WRAPPER_MODULE} (\n{port_lines}\n);\n"
            f"  {hdl_toplevel} {SDF_WRAPPER_DUT_INSTANCE} (\n{connections}\n  );\n"
            "endmodule\n"
        )


def _check_sdf_engine_capability(version: str | None) -> None:
    """Reject an ``options.sdf`` request the resolved Icarus cannot serve
    (issue #1004's finding, folded into #1002's own implementation).

    ``-ginterconnect`` -- mandatory for a post-route SDF, whose entire
    net-delay content is ``INTERCONNECT`` entries -- does not exist before
    Icarus 13.0. On 12.0 ``iverilog`` fails the *build* with ``Unknown/
    Unsupported Language generation interconnect`` and exit 255, four layers
    below this API, which reads as "the simulator is broken" rather than
    "this host's Icarus is too old for SDF". Probing
    :data:`SDF_MIN_ICARUS_MAJOR` up front converts that into a request error
    naming the actual constraint.

    An **unresolvable** version (``None`` -- ``iverilog -V`` missing or
    unparsable) is deliberately *not* an error: the probe is a courtesy, not
    a gate, and a failed build plus the transcript scan still catch a real
    incompatibility. Refusing to run on an unreadable version string would
    trade a clear downstream failure for a spurious upstream one.
    """
    if version is None:
        return
    match = re.match(r"(\d+)", version)
    if match is None:
        return
    if int(match.group(1)) >= SDF_MIN_ICARUS_MAJOR:
        return
    raise FunctionalVerificationError(
        f"options.sdf requires Icarus Verilog {SDF_MIN_ICARUS_MAJOR}.0 or "
        f"newer -- the resolved iverilog is version {version}, which has no "
        "'-ginterconnect' flag ('Unknown/Unsupported Language generation "
        "interconnect'). A post-route SDF's net delays are INTERCONNECT "
        "entries, so they cannot be annotated on this build at all"
    )


def _scan_sdf_diagnostics(*log_paths: str) -> tuple[list[str], dict[str, int]]:
    """Every *actionable* SDF diagnostic Icarus emitted across ``log_paths``,
    plus a per-class count of the *benign* diagnostics filtered out alongside
    them.

    Non-empty ``actionable`` means the annotation did not fully apply --
    regardless of the run's own pass/fail verdict, which is exactly the trap:
    `vvp` exits ``0`` in every SDF failure mode (spike §3.3).

    The second element counts diagnostics that matched
    :data:`SDF_BENIGN_DIAGNOSTIC_SUBSTRINGS` and were therefore excluded from
    ``actionable`` -- a real ``write_sdf`` emits ``TIMINGCHECK`` sections
    Icarus does not implement, and a gate that rejected them would reject
    every real post-route SDF. Counting them, rather than silently discarding
    them the way this function did before issue #1102, is what lets a caller
    tell "every check in the SDF was applied" apart from "every delay was
    applied and every TIMINGCHECK was dropped" -- both of which otherwise
    report ``annotated: true`` identically.

    Never raises: a missing/unreadable transcript contributes nothing, the
    same posture :func:`_log_tail` takes.
    """
    actionable: list[str] = []
    dropped: dict[str, int] = {}
    for log_path in log_paths:
        try:
            with open(log_path, encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if SDF_OMITTED_ANNOTATION_MARKER in stripped:
                actionable.append(stripped)
                continue
            if not any(marker in stripped for marker in SDF_DIAGNOSTIC_MARKERS):
                continue
            benign_class = next(
                (
                    benign
                    for benign in SDF_BENIGN_DIAGNOSTIC_SUBSTRINGS
                    if benign in stripped
                ),
                None,
            )
            if benign_class is not None:
                key = benign_class.lower()
                dropped[key] = dropped.get(key, 0) + 1
                continue
            actionable.append(stripped)
    return actionable, dropped


def _scan_interpreter_mismatch_diagnostics(*log_paths: str) -> str | None:
    """The first line in ``log_paths`` naming cocotb's ABI-mismatch failure
    mode (issue #1103), or ``None``.

    Fallback for whatever :func:`_check_cocotb_abi_compatibility`'s pre-flight
    ``WHEEL``-tag check doesn't catch. Modeled directly on
    :func:`_scan_sdf_diagnostics`: never raises -- a missing/unreadable
    transcript contributes nothing, the same posture :func:`_log_tail` takes.
    """
    for log_path in log_paths:
        try:
            with open(log_path, encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except OSError:
            continue
        for line in lines:
            stripped = line.strip()
            if any(marker in stripped for marker in INTERPRETER_MISMATCH_MARKERS):
                return stripped
    return None


def _resolve_parameters(parameters: Any) -> dict[str, Any]:
    """Validate the optional ``request.parameters`` object and return it
    unchanged (or ``{}`` when omitted).

    Forwarded verbatim to both ``Runner.build(parameters=...)`` and
    ``Runner.test(parameters=...)`` -- cocotb's own per-simulator backends
    already translate the mapping into the elaboration-time override syntax
    (Icarus: ``-P<toplevel>.<name>=<value>``; Verilator: ``-G<name>=<value>``),
    so this module never needs to know that syntax itself.
    """
    if parameters is None:
        return {}
    if not isinstance(parameters, dict):
        raise FunctionalVerificationError(
            "request.parameters must be a JSON object (string key -> scalar value)"
        )

    for key, value in parameters.items():
        if not isinstance(key, str) or not key:
            raise FunctionalVerificationError(
                "request.parameters keys must be non-empty strings"
            )
        if not isinstance(value, (bool, int, float, str)):
            raise FunctionalVerificationError(
                f"request.parameters[{key!r}] must be a scalar (integer, "
                "float, string, or boolean) -- got a "
                f"{type(value).__name__}"
            )

    return parameters


# ---------------------------------------------------------------------------
# `results.xml` parsing -- the only source of truth for the verdict
# ---------------------------------------------------------------------------


def parse_results_xml(results_xml: str) -> list[dict[str, Any]]:
    """Parse a cocotb ``results.xml`` into the contract's ``tests[]`` list.

    Structure (spike section 4, captured live): a ``<testcase>`` with no
    child element is a pass, a ``<failure>`` child is a failure, a
    ``<skipped>`` child is a skip. ``error_type``/``error_message`` are taken
    verbatim from the ``<failure>`` element's own attributes and are present
    only on failing entries.

    Raises :class:`FunctionalVerificationError` if the file is missing or
    unparseable -- either means the run produced no trustworthy evidence.
    """
    if not os.path.isfile(results_xml):
        raise FunctionalVerificationError(
            f"simulation produced no results file '{results_xml}' -- the "
            "regression did not run to completion"
        )
    try:
        tree = ElementTree.parse(results_xml)
    except ElementTree.ParseError as exc:
        raise FunctionalVerificationError(
            f"results file '{results_xml}' is not parseable XML: {exc}"
        ) from exc
    except OSError as exc:
        raise FunctionalVerificationError(
            f"could not read results file '{results_xml}': {exc}"
        ) from exc

    tests: list[dict[str, Any]] = []
    for testsuite in tree.iter("testsuite"):
        for testcase in testsuite.iter("testcase"):
            entry: dict[str, Any] = {
                "name": testcase.get("name"),
                "status": "passed",
                "sim_time_ns": _maybe_float(testcase.get("sim_time_ns")),
                "real_time_s": _maybe_float(testcase.get("time")),
            }
            failure = testcase.find("failure")
            skipped = testcase.find("skipped")
            if failure is not None:
                entry["status"] = "failed"
                entry["error_type"] = failure.get("error_type") or failure.get("type")
                entry["error_message"] = (
                    failure.get("error_msg")
                    or failure.get("message")
                    or (failure.text or "").strip()
                    or None
                )
            elif skipped is not None:
                entry["status"] = "skipped"
            tests.append(entry)
    return tests


def _maybe_float(value: str | None) -> float | None:
    """``float(value)``, or ``None`` when the attribute is absent or not a
    number -- an unparseable timing attribute must never sink a run whose
    pass/fail verdict is otherwise perfectly well defined."""
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _extract_random_seed_property(results_xml: str) -> int | None:
    """The effective ``random_seed`` cocotb's regression manager used for
    this run, read back out of ``results.xml``'s own
    ``<property name="random_seed" value="...">`` element (spike section 4,
    captured live -- every ``results.xml`` this verb produces carries one).

    This is the authoritative echo for ``environment.random_seed``: cocotb
    always logs the seed it actually used, whether or not
    ``request.options.random_seed`` pinned one explicitly (an unpinned run
    still gets a randomly-generated seed, itself worth knowing so that
    *specific* run can be reproduced later by feeding this value back in as
    ``request.options.random_seed``).

    Never raises -- a missing/unparseable/absent property degrades to
    ``None``, since this purely-informational field must never sink a run
    whose pass/fail verdict (from :func:`parse_results_xml`) is otherwise
    perfectly well defined.
    """
    try:
        tree = ElementTree.parse(results_xml)
    except (ElementTree.ParseError, OSError):
        return None
    for prop in tree.iter("property"):
        if prop.get("name") == "random_seed":
            value = prop.get("value")
            if value is None:
                return None
            try:
                return int(value)
            except ValueError:
                return None
    return None


# ---------------------------------------------------------------------------
# engine invocation
# ---------------------------------------------------------------------------


def _import_runner():
    """Import ``cocotb_tools.runner`` lazily, turning a missing/too-new-Python
    cocotb install into an actionable :class:`FunctionalVerificationError`
    rather than an ImportError traceback at ``klt`` startup."""
    try:
        from cocotb_tools import runner
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise FunctionalVerificationError(
            f"cocotb is not installed (import failed: {exc}) -- install it with "
            "`pip install cocotb` (cocotb 2.0 supports Python <= 3.13)"
        ) from exc
    return runner


def _check_cocotb_abi_compatibility() -> None:
    """Reject an installed cocotb wheel whose compiled ABI tag does not match
    the running interpreter (issue #1103).

    ``import cocotb_tools.runner`` (:func:`_import_runner`) succeeds even when
    cocotb was installed as e.g. a ``cp312`` wheel into a CPython 3.14
    environment -- pure Python still loads fine. The mismatch only manifests
    later, when cocotb's compiled VPI module is ``dlopen``'d *inside the
    simulator subprocess* and pulls in a ``libpython`` that does not match the
    interpreter actually running, which surfaces as the simulator exiting
    without ever writing ``results.xml`` -- four layers below anything this
    module otherwise sees. Reading the installed distribution's ``WHEEL``
    metadata (the same two-line check the issue's own diagnosis settled on)
    catches this before the runner is ever invoked.

    Never raises for a reason *other than* a confirmed mismatch: missing or
    malformed ``WHEEL`` metadata (e.g. a pure-Python wheel, an editable
    install, or a packaging layout this check doesn't recognise) degrades to
    "skip this check", the same discipline ``_engine_version()``/
    ``_cocotb_version()`` already follow -- an unreadable probe must never
    itself crash a run the runner might still serve.
    """
    try:
        wheel_text = importlib.metadata.distribution("cocotb").read_text("WHEEL")
    except Exception:  # pragma: no cover - defensive, any lookup failure
        return
    if not wheel_text:
        return
    tags = [
        line.split(":", 1)[1].strip()
        for line in wheel_text.splitlines()
        if line.startswith("Tag:")
    ]
    if not tags:
        return
    interpreter_tag = f"cp{sys.version_info[0]}{sys.version_info[1]}"
    if any(interpreter_tag in tag for tag in tags):
        return

    try:
        installed_version = importlib.metadata.version("cocotb")
    except Exception:  # pragma: no cover - defensive
        installed_version = "unknown"
    running_version = (
        f"{sys.version_info[0]}.{sys.version_info[1]}.{sys.version_info[2]}"
    )
    raise FunctionalVerificationError(
        f"cocotb {installed_version} is installed as a {'/'.join(tags)} wheel "
        f"but klt is running on CPython {running_version} -- its compiled VPI "
        f"module was built for {interpreter_tag} and will dlopen a mismatched "
        "libpython, so the simulator will exit without writing results.xml. "
        "Install a cocotb build for this interpreter, or run klt on an "
        "interpreter cocotb publishes a wheel for."
    )


def _log_tail(log_path: str, lines: int = 8) -> str:
    """The last few lines of a captured engine transcript, for folding into
    an error message. Never raises -- a missing/unreadable log degrades to a
    fixed placeholder, since it is only ever used to *explain* another
    failure."""
    try:
        with open(log_path, encoding="utf-8", errors="replace") as handle:
            captured = [line.rstrip() for line in handle if line.strip()]
    except OSError:
        return "no output captured"
    if not captured:
        return "no output captured"
    return " | ".join(captured[-lines:])


def _run_build(
    runner_module,
    *,
    engine: str,
    sources: list[str],
    hdl_toplevel: str,
    build_dir: str,
    build_args: list[str],
    defines: dict[str, str | None],
    includes: list[str],
    timescale: tuple[str, str],
    parameters: dict[str, Any],
    log_path: str,
):
    """``Runner.build()``, with every failure mode collapsed into
    :class:`FunctionalVerificationError`. Returns the constructed ``Runner``
    so the caller can reuse it for the test step. ``timescale`` is passed
    here *and* in :func:`_run_test` -- see this module's docstring, gotcha 3.
    ``parameters``, when non-empty, overrides Verilog ``parameter``/VHDL
    ``generic`` values at elaboration time (see :func:`_resolve_parameters`).
    ``defines`` and ``includes`` are forwarded verbatim to cocotb's own
    ``Runner.build(defines=..., includes=...)`` (see :func:`_resolve_defines`/
    :func:`_resolve_includes`).
    """
    try:
        # `get_runner` raises ValueError for an unknown name; an engine
        # class's own __init__ may raise anything at all while probing for a
        # missing simulator binary, so the arm is deliberately broad.
        engine_runner = runner_module.get_runner(engine)
    except Exception as exc:
        raise FunctionalVerificationError(
            f"could not initialise the '{engine}' simulator: {exc}"
        ) from exc

    try:
        engine_runner.build(
            sources=sources,
            hdl_toplevel=hdl_toplevel,
            build_dir=build_dir,
            build_args=build_args,
            defines=defines,
            includes=includes,
            always=True,
            timescale=timescale,
            parameters=parameters,
            log_file=log_path,
        )
    except subprocess.CalledProcessError as exc:
        raise FunctionalVerificationError(
            f"{engine} build/elaboration failed (exit {exc.returncode}): "
            f"{_log_tail(log_path)}"
        ) from exc
    except SystemExit as exc:
        raise FunctionalVerificationError(
            f"{engine} build/elaboration failed (exit {exc.code}): "
            f"{_log_tail(log_path)}"
        ) from exc
    except OSError as exc:
        raise FunctionalVerificationError(
            f"could not launch the {engine} build: {exc}"
        ) from exc
    except Exception as exc:
        raise FunctionalVerificationError(
            f"{engine} build/elaboration failed: {exc}"
        ) from exc

    return engine_runner


def _run_test(
    engine_runner,
    *,
    engine: str,
    module: str,
    module_dir: str,
    hdl_toplevel: str,
    testcase: str | list[str] | None,
    random_seed: int | None,
    build_dir: str,
    test_dir: str,
    results_xml: str,
    timescale: tuple[str, str],
    parameters: dict[str, Any],
    log_path: str,
) -> None:
    """``Runner.test()``, with the simulator's own exit code deliberately
    discarded (spike section 4): a :class:`SystemExit` here means only "the
    simulator process returned nonzero", which was observed to mean anything
    from "one test failed" to "nothing ran". The verdict comes from
    :func:`parse_results_xml` instead, which the caller runs next.

    ``module_dir`` is prepended to ``sys.path`` for the duration of the call
    because ``Runner._set_env`` propagates the *calling* process's ``sys.path``
    to the simulator as ``PYTHONPATH`` (verified live) -- there is no
    ``test_module`` search-path parameter to use instead.

    ``random_seed``, when given, is forwarded to ``Runner.test()``'s own
    ``seed`` parameter, which sets ``COCOTB_RANDOM_SEED`` in the simulator
    subprocess's environment (spike's "open question" on `random_seed`
    reproducibility, issue #423) -- cocotb seeds its regression manager's
    ``random`` module from this value, so a pinned seed reproduces the same
    randomized-test content run-to-run, not merely the same *logged* value
    after the fact. ``None`` leaves cocotb to generate (and itself log) its
    own seed, still recovered afterwards from ``results.xml``'s own
    ``<property name="random_seed">`` element (see
    :func:`_extract_random_seed_property`) so every run's effective seed is
    echoed in the response regardless of whether the request pinned one.

    ``parameters``, when non-empty, is forwarded unchanged -- see
    :func:`_resolve_parameters` and :func:`_run_build` (the same mapping is
    passed to both the build and test steps, matching cocotb's own
    ``Runner`` contract).
    """
    inserted = module_dir not in sys.path
    if inserted:
        sys.path.insert(0, module_dir)
    try:
        engine_runner.test(
            test_module=module,
            hdl_toplevel=hdl_toplevel,
            hdl_toplevel_lang="verilog",
            testcase=testcase,
            seed=random_seed,
            build_dir=build_dir,
            test_dir=test_dir,
            results_xml=results_xml,
            timescale=timescale,
            parameters=parameters,
            log_file=log_path,
        )
    except (SystemExit, subprocess.CalledProcessError):
        # Expected on a failing regression -- the results file is the truth.
        pass
    except OSError as exc:
        raise FunctionalVerificationError(
            f"could not launch the {engine} simulation: {exc}"
        ) from exc
    except Exception as exc:
        raise FunctionalVerificationError(
            f"{engine} simulation failed: {exc} ({_log_tail(log_path)})"
        ) from exc
    finally:
        if inserted:
            try:
                sys.path.remove(module_dir)
            except ValueError:  # pragma: no cover - defensive
                pass


def _engine_version(engine: str) -> str | None:
    """The resolved simulator version string, or ``None`` if unresolvable --
    never raises, and never trusts the probe's exit code (``iverilog -V``
    prints its version *and* returns nonzero on some builds)."""
    command, pattern = _ENGINE_VERSION_COMMANDS[engine]
    try:
        completed = subprocess.run(command, capture_output=True, text=True)
    except OSError:
        return None
    match = pattern.search(f"{completed.stdout}\n{completed.stderr}")
    return match.group(1) if match else None


def _cocotb_version() -> str | None:
    """cocotb's own version string, or ``None`` -- never raises."""
    try:
        import cocotb

        return str(cocotb.__version__)
    except Exception:  # pragma: no cover - defensive
        return None


# ---------------------------------------------------------------------------
# coverage extraction (Verilator only -- spike section 5)
# ---------------------------------------------------------------------------


def _find_coverage_dat(test_dir: str, build_dir: str) -> str | None:
    """Locate Verilator's ``coverage.dat``. It is written by the compiled
    model into the *simulation's* working directory (``test_dir``, verified
    live); ``build_dir`` and a recursive sweep are checked as fallbacks so a
    future Verilator/cocotb layout change degrades to a slower search rather
    than a false "no coverage" report."""
    for candidate in (
        os.path.join(test_dir, "coverage.dat"),
        os.path.join(test_dir, "logs", "coverage.dat"),
        os.path.join(build_dir, "coverage.dat"),
    ):
        if os.path.isfile(candidate):
            return candidate
    for root in (test_dir, build_dir):
        for dirpath, _dirnames, filenames in os.walk(root):
            if "coverage.dat" in filenames:
                return os.path.join(dirpath, "coverage.dat")
    return None


def collect_coverage(
    *, test_dir: str, build_dir: str, info_path: str
) -> dict[str, Any]:
    """Post-process Verilator's ``coverage.dat`` into the contract's
    ``coverage`` block (spike section 5).

    Two ``verilator_coverage`` passes: ``--write-info`` emits the portable
    lcov ``.info`` artifact the contract references *by path* (never inlined),
    and ``--report summary`` gives the per-category percentages. Raises
    :class:`FunctionalVerificationError` when coverage was requested but
    cannot be produced -- a requested-and-missing coverage block would
    otherwise be indistinguishable from "not requested" (``null``).
    """
    coverage_dat = _find_coverage_dat(test_dir, build_dir)
    if coverage_dat is None:
        raise FunctionalVerificationError(
            "options.coverage was requested but the run produced no "
            "coverage.dat -- verify the Verilator build accepted --coverage"
        )

    try:
        info = subprocess.run(
            ["verilator_coverage", "--write-info", info_path, coverage_dat],
            capture_output=True,
            text=True,
        )
        summary = subprocess.run(
            ["verilator_coverage", "--report", "summary", coverage_dat],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise FunctionalVerificationError(
            f"could not launch verilator_coverage: {exc}"
        ) from exc

    if info.returncode != 0 or not os.path.isfile(info_path):
        raise FunctionalVerificationError(
            "verilator_coverage --write-info failed: "
            f"{(info.stderr or info.stdout or '').strip() or 'no output captured'}"
        )
    if summary.returncode != 0:
        raise FunctionalVerificationError(
            "verilator_coverage --report summary failed: "
            f"{(summary.stderr or summary.stdout or '').strip() or 'no output'}"
        )

    percentages = {
        key: float(value)
        for key, value in _COVERAGE_SUMMARY_RE.findall(
            f"{summary.stdout}\n{summary.stderr}"
        )
    }
    return {
        "line_pct": percentages.get("line"),
        "toggle_pct": percentages.get("toggle"),
        "branch_pct": percentages.get("branch"),
        "expr_pct": percentages.get("expr"),
        "info_path": info_path,
    }


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def run_functional_verification(request: str) -> dict[str, Any]:
    """Run the cocotb regression declared by ``request``.

    ``request`` accepts the same three forms every other ``klt`` request
    argument does (see :func:`load_request_arg`): a path to a request JSON
    file, ``"-"`` to read the request from stdin, or an inline JSON object
    string.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/functional-verification.md``). Raises
    :class:`FunctionalVerificationError` for anything that prevents a
    trustworthy verdict from being produced at all -- a failing *test* is
    never an error, it is ``status: "fail"`` in the returned report (which
    the CLI turns into exit code 3).

    Artifacts (the build directory, the raw ``results.xml``, the engine
    transcripts, and any coverage ``.info``) are written to
    ``.klt/functional-verification/`` next to the request file -- the same
    "next to the input" default ``klt sim``/``klt synthesize`` already use --
    and kept, never deleted.
    """
    request_doc, request_dir = load_request_arg(request)

    engine = request_doc.get("engine", DEFAULT_ENGINE)
    if engine not in SUPPORTED_ENGINES:
        raise FunctionalVerificationError(
            f"unsupported engine '{engine}' (supported: {', '.join(SUPPORTED_ENGINES)})"
        )

    sources = _resolve_sources(request_doc["sources"], request_dir)

    hdl_toplevel = request_doc["hdl_toplevel"]
    if not isinstance(hdl_toplevel, str) or not hdl_toplevel:
        raise FunctionalVerificationError(
            "request.hdl_toplevel must be a non-empty string"
        )

    module, module_dir, testcase = _resolve_testbench(
        request_doc["testbench"], request_dir
    )
    (
        coverage_requested,
        timescale,
        random_seed,
        defines,
        user_build_args,
        includes,
        sdf,
    ) = _resolve_options(request_doc.get("options"), engine, request_dir)
    parameters = _resolve_parameters(request_doc.get("parameters"))

    output_dir = os.path.join(request_dir, ".klt", "functional-verification")
    build_dir = os.path.join(output_dir, f"sim_build_{engine}")
    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as exc:
        raise FunctionalVerificationError(
            f"could not create output directory '{output_dir}': {exc}"
        ) from exc

    results_xml = os.path.join(output_dir, f"results_{engine}.xml")
    build_log = os.path.join(output_dir, f"build_{engine}.log")
    test_log = os.path.join(output_dir, f"test_{engine}.log")
    # A stale artifact from a previous run must never be mistaken for this
    # run's evidence if the build below fails outright.
    try:
        os.remove(results_xml)
    except OSError:
        pass
    if coverage_requested:
        # Same staleness guard for coverage.dat: a leftover file from a prior
        # run must never be picked up by _find_coverage_dat() if this run's
        # test process crashes after results.xml is written but before a
        # fresh coverage.dat is flushed. Only the coverage-requested path
        # reads coverage.dat, so the removal only needs to happen here.
        for candidate in (
            os.path.join(output_dir, "coverage.dat"),
            os.path.join(output_dir, "logs", "coverage.dat"),
            os.path.join(build_dir, "coverage.dat"),
        ):
            try:
                os.remove(candidate)
            except OSError:
                pass

    # Build args are computed, not selected from a constant (spike §4.2's
    # one structural change): coverage and SDF annotation each contribute
    # their own flags, and they compose rather than overwrite -- even though
    # the two are mutually exclusive by engine today (coverage is
    # Verilator-only, SDF is Icarus-only), so is any later addition. The
    # caller's own `options.build_args` composes last, after both (see
    # :func:`_resolve_build_args`).
    build_args: list[str] = []
    # The Verilog toplevel actually passed to `-s`/cocotb's `hdl_toplevel`
    # for this build+test -- the real DUT unless `options.sdf` swaps in the
    # generated wrapper below (issue #1056).
    build_hdl_toplevel = hdl_toplevel
    if coverage_requested:
        build_args += list(COVERAGE_BUILD_ARGS)
    if sdf is not None:
        # Before generating anything: is this host's Icarus new enough to
        # have `-ginterconnect` at all? (issue #1004 -- 12.0 does not, and
        # fails the build with a message that names neither SDF nor this
        # request field.)
        _check_sdf_engine_capability(_engine_version(engine))
        if parameters:
            # cocotb's own Icarus parameter-override syntax is always
            # `-P<hdl_toplevel>.<name>=<value>` (its Runner, not this
            # module, formats it) -- once `hdl_toplevel` becomes the
            # generated wrapper below, that would silently target the
            # wrapper (which declares no parameters) instead of the real
            # DUT rather than erroring, so the combination is rejected up
            # front instead of risking a parameter override that looks
            # applied but was not.
            raise FunctionalVerificationError(
                "request.parameters cannot currently be combined with "
                "options.sdf on Icarus: the top-level-port workaround "
                "(issue #1056) elaborates a generated wrapper module as the "
                "new -s root, and cocotb's own parameter-override syntax "
                "would then target that wrapper instead of the real DUT"
            )
        # The transparent pass-through wrapper (issue #1056) that makes the
        # DUT a nested child instance rather than its own elaboration root
        # -- required for Icarus to resolve a bare top-level-port
        # `INTERCONNECT` endpoint at all -- plus the generated second
        # elaboration root carrying the `$sdf_annotate` call itself, and the
        # three flags that make it do anything -- plus `-T <corner>`, which
        # is how Icarus selects a member of each SDF `min:typ:max` triplet
        # (a compile-time flag, not an `$sdf_annotate` argument; see
        # `SDF_CORNERS`).
        ports = _parse_toplevel_ports(sources, hdl_toplevel)
        wrapper_path = os.path.join(output_dir, f"{SDF_WRAPPER_MODULE}.v")
        _write_sdf_dut_wrapper(wrapper_path, hdl_toplevel=hdl_toplevel, ports=ports)
        shim_path = os.path.join(output_dir, f"{SDF_ANNOTATE_MODULE}.v")
        _write_sdf_annotate_shim(
            shim_path,
            sdf_path=sdf["file"],
            scope=f"{SDF_WRAPPER_MODULE}.{SDF_WRAPPER_DUT_INSTANCE}",
        )
        sources = [*sources, wrapper_path, shim_path]
        build_args += [*SDF_BUILD_ARGS, "-T", sdf["corner"]]
        build_hdl_toplevel = SDF_WRAPPER_MODULE
    build_args += user_build_args

    runner_module = _import_runner()
    _check_cocotb_abi_compatibility()
    engine_runner = _run_build(
        runner_module,
        engine=engine,
        sources=sources,
        hdl_toplevel=build_hdl_toplevel,
        build_dir=build_dir,
        build_args=build_args,
        defines=defines,
        includes=includes,
        timescale=timescale,
        parameters=parameters,
        log_path=build_log,
    )
    _run_test(
        engine_runner,
        engine=engine,
        module=module,
        module_dir=module_dir,
        hdl_toplevel=build_hdl_toplevel,
        testcase=testcase,
        random_seed=random_seed,
        build_dir=build_dir,
        test_dir=output_dir,
        results_xml=results_xml,
        timescale=timescale,
        parameters=parameters,
        log_path=test_log,
    )

    sdf_dropped_counts: dict[str, int] = {}
    if sdf is not None:
        # The hard gate (spike §3.3): every SDF failure mode -- unopenable
        # file, unmatched instance, unmatched IOPATH, INTERCONNECT without
        # `-ginterconnect` -- is *non-fatal*. Icarus prints a line, drops
        # that annotation, and `vvp` exits 0; the regression then reports a
        # perfectly clean PASS derived from a zero-delay run. Scanning both
        # transcripts is the only signal there is, and it runs before the
        # verdict is parsed so an annotation failure is reported as one
        # rather than as a (misleadingly green) result.
        diagnostics, sdf_dropped_counts = _scan_sdf_diagnostics(build_log, test_log)
        if diagnostics:
            shown = " | ".join(diagnostics[:5])
            more = f" (+{len(diagnostics) - 5} more)" if len(diagnostics) > 5 else ""
            raise FunctionalVerificationError(
                f"SDF back-annotation of '{sdf['file']}' did not fully apply: "
                f"{len(diagnostics)} diagnostic(s) in the {engine} transcript "
                f"-- {shown}{more}. The simulator exits 0 after skipping an "
                "annotation it cannot apply, so this run's verdict would have "
                "been a zero-delay one reported as if it were annotated."
            )

    try:
        tests = parse_results_xml(results_xml)
    except FunctionalVerificationError as exc:
        # The pre-flight ABI check catches the common case up front; this is
        # the fallback for whatever it doesn't (issue #1103) -- only fires
        # when `results.xml` is confirmed missing, so a genuinely unparseable
        # or unreadable results file (a different failure mode entirely)
        # keeps its own unaugmented message.
        diagnostic = None
        if not os.path.isfile(results_xml):
            diagnostic = _scan_interpreter_mismatch_diagnostics(build_log, test_log)
        if diagnostic is None:
            raise
        raise FunctionalVerificationError(
            f"{exc} -- probable cause: the {engine} transcript contains "
            f"{diagnostic!r}, which usually means the installed cocotb was "
            "built for a different CPython than the one running klt (an "
            "ABI-mismatched wheel)"
        ) from exc
    if not tests:
        # An empty regression is not a pass. Reporting `status: "pass"` here
        # would hand `klt eval` a vacuous `valid: true` for a design nothing
        # was ever checked against -- the exact failure Epic #391's own
        # framing ("the hard gate in #387's valid field") exists to prevent.
        raise FunctionalVerificationError(
            f"testbench module '{module}' registered no tests -- the run "
            "verified nothing (a regression with zero @cocotb.test() "
            "functions is not a pass)"
        )

    passed_count = sum(1 for test in tests if test["status"] == "passed")
    failed_count = sum(1 for test in tests if test["status"] == "failed")
    skipped_count = sum(1 for test in tests if test["status"] == "skipped")

    coverage = None
    if coverage_requested:
        coverage = collect_coverage(
            test_dir=output_dir,
            build_dir=build_dir,
            info_path=os.path.join(output_dir, "coverage.info"),
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "engine": engine,
        "hdl_toplevel": hdl_toplevel,
        "testbench": module,
        "status": "fail" if failed_count else "pass",
        "test_count": len(tests),
        "passed_count": passed_count,
        "failed_count": failed_count,
        "skipped_count": skipped_count,
        "tests": tests,
        "coverage": coverage,
        "environment": {
            "engine": engine,
            "engine_version": _engine_version(engine),
            "cocotb_version": _cocotb_version(),
            "results_xml": results_xml,
            "random_seed": _extract_random_seed_property(results_xml),
            # Additive (issue #1002, spike §4.2 item 5): `null` on an
            # ordinary run, an object on an SDF-annotated one -- so a caller
            # can tell an annotated run from an unannotated one *from the
            # JSON*, without reading a transcript. `annotated: true` is
            # load-bearing rather than redundant: reaching this point at all
            # means the diagnostic scan above found nothing *actionable*,
            # i.e. every non-benign diagnostic class applied cleanly.
            #
            # `annotated: true` alone cannot distinguish "every delay and
            # every timing check applied" from "every IOPATH applied and
            # every TIMINGCHECK section was dropped" -- the normal case on
            # Icarus, which has no SDF TIMINGCHECK support at all (issue
            # #1102). `partial`/`dropped` make that distinction
            # machine-readable: `partial` is true whenever any benign class
            # was filtered out of the diagnostic gate above, and `dropped`
            # names each such class with the count of diagnostic lines
            # filtered and a human-readable reason
            # (:data:`SDF_BENIGN_DIAGNOSTIC_REASONS`). An ordinary fully
            # -clean run reports `partial: false` and `dropped: {}`.
            "sdf": (
                None
                if sdf is None
                else {
                    "file": sdf["file"],
                    "corner": sdf["corner"],
                    "annotated": True,
                    "partial": bool(sdf_dropped_counts),
                    "dropped": {
                        klass: {
                            "count": count,
                            "reason": SDF_BENIGN_DIAGNOSTIC_REASONS.get(klass, ""),
                        }
                        for klass, count in sorted(sdf_dropped_counts.items())
                    },
                }
            ),
        },
    }
