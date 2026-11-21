"""``results.xml`` parsing for ``klt functional-verification``.

Lives apart from ``functional_verification.py`` (issue #6704): that module
sits at its ``file-size-baseline.json`` ceiling, so any change that had to add
code to it paid for the lines by deleting existing text -- and prose is the
cheapest thing to delete. This block is self-contained (it needs only
``ElementTree``, ``os`` and the verb's error class), so moving it out gives
the main module real headroom without condensing any of the design rationale
kept in the docstrings below.

``functional_verification`` re-imports :func:`parse_results_xml`,
:func:`_executed_result_counts` and :func:`_extract_random_seed_property`, so
``klayout_tools.functional_verification.parse_results_xml`` (and the other two,
which tests and ``functional_verification_mutation`` reach through that module)
resolves exactly as before.

``FunctionalVerificationError`` is imported inside the two functions that
raise it: ``functional_verification`` imports this module at load time, so a
module-level import back would be circular (the same pattern as
``functional_verification_pdk`` and ``functional_verification_sdf``).
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ElementTree
from typing import Any

# ---------------------------------------------------------------------------
# `results.xml` parsing -- the only source of truth for the verdict
# ---------------------------------------------------------------------------


def parse_results_xml(results_xml: str) -> list[dict[str, Any]]:
    """Parse a cocotb ``results.xml`` into the contract's ``tests[]`` list.

    A testcase passes unless it contains a failure or skip. Only failures
    carry an error type and message. The parser reads the raw diagnostic
    from the failure attributes or element text.

    Simulated time comes from :func:`_testcase_sim_time_ns`, which reads
    *both* JUnit shapes cocotb has emitted (the 2.0.x ``sim_time_ns``
    attribute and the 2.1.0 ``<properties>`` block) -- see that function.

    Raises :class:`FunctionalVerificationError` if the file is missing or
    unparseable -- either means the run produced no trustworthy evidence.
    """
    from .functional_verification import FunctionalVerificationError

    if not os.path.isfile(results_xml):
        raise FunctionalVerificationError(
            f"simulation produced no results file '{results_xml}' -- the "
            "regression did not run to completion"
        )
    try:
        tree = ElementTree.parse(results_xml)  # noqa: S314 - cocotb results.xml generated in the verb-owned sim build dir
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
                "sim_time_ns": _testcase_sim_time_ns(testcase),
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


def _executed_result_counts(
    tests: list[dict[str, Any]], module: str
) -> tuple[int, int, int]:
    """Count parsed outcomes, refusing regressions that verified nothing."""
    from .functional_verification import FunctionalVerificationError

    if not tests:
        raise FunctionalVerificationError(
            f"testbench module '{module}' registered no tests -- the run "
            "verified nothing (a regression with zero @cocotb.test() "
            "functions is not a pass)"
        )
    passed = sum(1 for test in tests if test["status"] == "passed")
    failed = sum(1 for test in tests if test["status"] == "failed")
    skipped = sum(1 for test in tests if test["status"] == "skipped")
    if passed + failed == 0:
        raise FunctionalVerificationError(
            f"testbench module '{module}': all tests were skipped -- "
            "zero executed tests verified nothing; this regression is not a pass"
        )
    return passed, failed, skipped


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


# cocotb's own simulator-time unit tokens, as they appear in the
# `<property name="sim_time_unit" value="..."/>` element of a cocotb >= 2.1.0
# `results.xml`, mapped to the power of ten that converts that unit to
# nanoseconds. `sec` is cocotb's own spelling for seconds; `s` is accepted
# too so a future/alternate spelling still resolves rather than nulling out.
_SIM_TIME_UNIT_NS_EXPONENT: dict[str, int] = {
    "fs": -6,
    "ps": -3,
    "ns": 0,
    "us": 3,
    "ms": 6,
    "s": 9,
    "sec": 9,
}


def _testcase_properties(testcase: ElementTree.Element) -> dict[str, str]:
    """``{name: value}`` for the ``<properties>`` block cocotb >= 2.1.0 hangs
    off each ``<testcase>``.

    Only the testcase's *own* direct ``<properties>/<property>`` children are
    read -- never a ``<testsuite>``-level property (where cocotb 2.0.x put
    ``random_seed``, see :func:`_extract_random_seed_property`) -- so a
    per-suite property can never be mistaken for a per-test one. A property
    missing either attribute is skipped rather than raising.
    """
    properties: dict[str, str] = {}
    for prop in testcase.iterfind("properties/property"):
        name = prop.get("name")
        value = prop.get("value")
        if name is not None and value is not None:
            properties[name] = value
    return properties


def _testcase_sim_time_ns(testcase: ElementTree.Element) -> float | None:
    """Simulated time for one ``<testcase>``, in nanoseconds, across both
    JUnit XML shapes cocotb has emitted (issue #2592).

    cocotb 2.0.x put the value on the element itself::

        <testcase name="test_gcd_known_pairs" time="0.0056" sim_time_ns="520.0" />

    cocotb 2.1.0 restructured its JUnit writer and moved the timing facts into
    a child ``<properties>`` block instead, with the unit reported separately::

        <testcase name="test_gcd_known_pairs" time="0.014">
          <properties>
            <property name="sim_time_unit" value="ns" />
            <property name="sim_time_duration" value="520.0" />
          </properties>
        </testcase>

    The 2.0.x attribute is tried **first** and used unchanged when present, so
    nothing that parses today -- including stored historical ``results.xml``
    evidence -- changes behaviour. Only when the attribute is absent is
    ``sim_time_duration`` read and scaled by the sibling ``sim_time_unit``;
    the unit is never assumed to be ``ns``, since cocotb reports whatever
    unit the run actually used.

    Never raises. A ``<testcase>`` carrying neither the attribute nor a
    usable ``<properties>`` block (a test that never ran, or a unit token
    this mapping does not recognise -- guessing a scale would be worse than
    reporting nothing) degrades to ``None``, exactly as before: a purely
    informational timing field must never sink a run whose pass/fail verdict
    is otherwise perfectly well defined.
    """
    attribute = _maybe_float(testcase.get("sim_time_ns"))
    if attribute is not None:
        return attribute

    properties = _testcase_properties(testcase)
    duration = _maybe_float(properties.get("sim_time_duration"))
    if duration is None:
        return None
    unit = (properties.get("sim_time_unit") or "").strip().lower()
    exponent = _SIM_TIME_UNIT_NS_EXPONENT.get(unit)
    if exponent is None:
        return None
    if exponent == 0:
        return duration
    if exponent > 0:
        return duration * float(10**exponent)
    # Divide rather than multiply by a fractional scale: `520000 / 1000` is
    # exact where `520000 * 1e-3` is not.
    return duration / float(10**-exponent)


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
        tree = ElementTree.parse(results_xml)  # noqa: S314 - cocotb results.xml generated in the verb-owned sim build dir
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
