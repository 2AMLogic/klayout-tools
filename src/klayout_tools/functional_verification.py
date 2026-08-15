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
    bool, tuple[str, str], int | None, dict[str, str | None], list[str], list[str]
]:
    """Validate ``request.options`` and return
    ``(coverage, timescale, random_seed, defines, build_args, includes)``.

    Enforces the spike's hard constraint that coverage is a Verilator-only
    capability -- ``options.coverage: true`` with ``engine: "icarus"`` is a
    request error (exit 1), never a silently-ignored flag.
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

    return coverage, resolved_timescale, random_seed, defines, build_args, includes


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
        build_args,
        includes,
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

    runner_module = _import_runner()
    engine_runner = _run_build(
        runner_module,
        engine=engine,
        sources=sources,
        hdl_toplevel=hdl_toplevel,
        build_dir=build_dir,
        build_args=(list(COVERAGE_BUILD_ARGS) if coverage_requested else [])
        + build_args,
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
        hdl_toplevel=hdl_toplevel,
        testcase=testcase,
        random_seed=random_seed,
        build_dir=build_dir,
        test_dir=output_dir,
        results_xml=results_xml,
        timescale=timescale,
        parameters=parameters,
        log_path=test_log,
    )

    tests = parse_results_xml(results_xml)
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
        },
    }
