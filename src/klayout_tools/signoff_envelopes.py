"""Typed envelope shapes ``klt signoff`` validates ingested evidence against.

Pure declarations: one ``TypedDict`` per :func:`klayout_tools.signoff._classify`
kind, the union they narrow to, and the two runtime lookup tables
(:data:`_SCALAR_CHECKS`, :data:`_UNION_ORIGINS`)
:func:`klayout_tools.signoff._validate_envelope` walks them with. No runtime
logic lives here -- ``signoff.py`` imports every name below by name, so
``signoff.<name>`` resolves exactly as it did before this split (issue #2313).
"""

from __future__ import annotations

import types
from typing import Any, TypedDict, Union

# --------------------------------------------------------------------------- #
# Typed envelope shapes + runtime validation (issue #2033)
#
# One TypedDict per `_classify` kind. Each kind's *required* keys are the
# ones this boundary genuinely cannot work without -- the fields
# `_classify` discriminates on, plus the field `_check_passed` derives that
# kind's verdict from. Everything else this boundary reads is declared
# `total=False`: optional by construction, so evidence committed before a
# later-added block existed (a `drc` report with no `coverage`, an `lvs`
# report with no `power_connectivity`) still validates and still grades
# exactly as it always did.
#
# Deliberately *not* an exhaustive transcription of each verb's full JSON
# schema -- that lives in each verb's own `docs/cli/<verb>.md`, and
# duplicating it here would create a second contract to keep in sync. These
# declare the consumer's view: what `klt signoff` reads.
#
# See ``signoff.py``'s "Typed, runtime-validated evidence ingestion"
# docstring section for the scope of what this catches (malformed/incomplete
# envelopes) and what it explicitly does not (semantic mismatches between
# two well-formed values).
# --------------------------------------------------------------------------- #


class _EnvelopeCommon(TypedDict):
    """The one field every ``klt`` JSON envelope carries
    (``docs/json-contract.md``) -- already the first thing
    :func:`_classify` checks for."""

    schema_version: int


class _DrcRequired(_EnvelopeCommon):
    status: str
    violations: list[Any]


class _DrcEnvelope(_DrcRequired, total=False):
    file: Any
    deck: Any
    violation_count: Any
    coverage: dict[str, Any]
    metrics: dict[str, Any]
    provenance: dict[str, Any] | None


class _LvsRequired(_EnvelopeCommon):
    status: str
    mismatches: list[Any]


class _LvsEnvelope(_LvsRequired, total=False):
    layout: Any
    reference: Any
    mismatch_count: Any
    counts: Any
    power_connectivity: dict[str, Any] | None
    body_verification: dict[str, Any] | None
    metrics: dict[str, Any]
    provenance: dict[str, Any] | None


class _SimRequired(_EnvelopeCommon):
    status: str
    measurements: list[Any]
    corner_count: int


class _SimEnvelope(_SimRequired, total=False):
    netlist: Any
    passed: Any
    failed: Any
    errored: Any
    metrics: dict[str, Any]
    provenance: dict[str, Any] | None


class _YieldRequired(_EnvelopeCommon):
    status: str
    measurements: list[Any]
    measurement_count: int
    source: dict[str, Any]


class _YieldEnvelope(_YieldRequired, total=False):
    samples: Any
    limits: Any
    metrics: dict[str, Any]
    provenance: dict[str, Any] | None


class _ExtractRequired(_EnvelopeCommon):
    # `status` is required even though `_check_passed` counts every extract
    # envelope as passing: an extract report that reached this boundary
    # without one is truncated, and grading it as the one unconditionally-
    # passing kind is exactly the #1987/#1988 failure this validation
    # exists to stop.
    status: str
    device_count: int
    nets: list[Any]


class _ExtractEnvelope(_ExtractRequired, total=False):
    file: Any
    deck: Any
    net_count: Any
    metrics: dict[str, Any]
    provenance: dict[str, Any] | None


class _PexRequired(_EnvelopeCommon):
    status: str
    delta: list[Any]
    # `Any`, not `str`: `klt pex` emits the repo-relative `{path, scope}`
    # object `env_provenance.repo_relative_path` builds (issue #1261),
    # while older committed evidence carries a bare path string. Presence is
    # what this boundary discriminates on; the value's own shape belongs to
    # `docs/cli/pex.md`, not here.
    reference_netlist: Any


class _PexEnvelope(_PexRequired, total=False):
    # The layout stream this run extracted from, in the `{path, scope}`
    # shape `klt pex` echoes every input path under (issue #1261) -- read
    # since issue #2196 to re-hash the artifact `provenance.input` pins.
    layout: Any
    netlist: Any
    corner_count: Any
    passed: Any
    failed: Any
    errored: Any
    body_bias: dict[str, Any] | None
    metrics: dict[str, Any]
    provenance: dict[str, Any] | None


class _PowerRequired(_EnvelopeCommon):
    # `klt power` carries no top-level `status` at all (docs/cli/power.md);
    # `em_verdict` is what `_check_passed` derives its verdict from, and is
    # always present -- `None` when the spec declared no solve.
    power_nets: list[Any]
    networks: list[Any]
    em_verdict: dict[str, Any] | None


class _PowerEnvelope(_PowerRequired, total=False):
    file: Any
    spec: Any
    worst_case_droop_mv: Any
    metrics: dict[str, Any]
    provenance: dict[str, Any] | None


class _StaRequired(_EnvelopeCommon):
    # `status` is always `"ok"` (docs/cli/sta.md) -- this verb has no
    # pass/fail concept of its own, so the verdict comes from the per-corner
    # timing fields below, either flat or under `corners`.
    status: str
    geometry_source: str


class _StaEnvelope(_StaRequired, total=False):
    def_path: Any
    verilog_path: Any
    spef_path: Any
    corners: list[Any]
    timing_status: Any
    worst_slack_ns: Any
    worst_hold_slack_ns: Any
    metrics: dict[str, Any]
    provenance: dict[str, Any] | None


class _FunctionalVerificationRequired(_EnvelopeCommon):
    status: str
    tests: list[Any]
    test_count: int


class _FunctionalVerificationEnvelope(_FunctionalVerificationRequired, total=False):
    hdl_toplevel: Any
    testbench: Any
    passed_count: Any
    failed_count: Any
    skipped_count: Any
    environment: dict[str, Any] | None
    metrics: dict[str, Any]
    provenance: dict[str, Any] | None


class _ErcRequired(_EnvelopeCommon):
    # `status` is what `_check_passed` grades ("clean"/"violations",
    # docs/cli/erc.md); `gates`/`gate_role` are the two fields `_classify`
    # recognises this shape by (issue #2025). Item 11's own grading
    # (`_erc_supply_spec`/`_erc_supply_findings`) reads `spec`/`erc_findings`
    # defensively via `.get(...) or []`, so neither is required here.
    status: str
    gates: list[Any]
    gate_role: Any


class _ErcEnvelope(_ErcRequired, total=False):
    # The layout stream this run checked -- read since issue #2196 to
    # re-hash the artifact `provenance.input` pins.
    file: Any
    spec: Any
    stackup: Any
    erc_findings: list[Any]
    # The connectivity half's own roll-up and checked-work scope (issue
    # #2179), optional because every `klt erc` envelope written before it
    # carries neither -- and an envelope that does not state a connectivity
    # verdict must keep grading exactly as it did (see `_coverage_refusal`).
    erc_status: str
    erc_coverage: dict[str, Any]
    metrics: dict[str, Any]
    provenance: dict[str, Any] | None


class _PlaceAndRouteRequired(_EnvelopeCommon):
    # `status` is what `_check_passed` grades ("ok" = the run completed,
    # docs/cli/place-and-route.md); `stage_reached`/`power` are the two
    # fields `_classify` recognises this shape by (issue #2025). `power` is
    # always present in a real response (possibly `{}`), so it is required
    # here even though `_strap_layers` still reads it via `.get(...) or {}`
    # for the same "defend the reader anyway" reason every other kind does.
    status: str
    stage_reached: str
    power: dict[str, Any]


class _PlaceAndRouteEnvelope(_PlaceAndRouteRequired, total=False):
    worst_setup_slack_ns: Any
    worst_hold_slack_ns: Any
    timing_status: Any
    corners: list[Any]
    metrics: dict[str, Any]
    provenance: dict[str, Any] | None


class _GenericRequired(_EnvelopeCommon):
    # `kind` and `status` are the two fields docs/cli/signoff.md's "Generic
    # evidence" section declares required; `summary`/`source` are explicitly
    # optional there and stay optional here.
    kind: str
    status: str


class _GenericEnvelope(_GenericRequired, total=False):
    summary: Any
    source: Any
    metrics: dict[str, Any]
    provenance: dict[str, Any] | None


class _ErrorRequired(_EnvelopeCommon):
    error: dict[str, Any]


class _ErrorEnvelope(_ErrorRequired, total=False):
    metrics: dict[str, Any]
    provenance: dict[str, Any] | None


#: The union every envelope read at this boundary narrows to once
#: :func:`_classify` has both recognised *and* validated it -- what
#: :func:`_build_check`/:func:`_check_passed`/:func:`_detail` take in place
#: of a bare ``dict[str, Any]``.
_EvidenceEnvelope = (
    _DrcEnvelope
    | _LvsEnvelope
    | _SimEnvelope
    | _YieldEnvelope
    | _ExtractEnvelope
    | _PexEnvelope
    | _PowerEnvelope
    | _StaEnvelope
    | _FunctionalVerificationEnvelope
    | _ErcEnvelope
    | _PlaceAndRouteEnvelope
    | _GenericEnvelope
    | _ErrorEnvelope
)

#: Every kind :func:`_classify` can return, mapped to the shape
#: :func:`_validate_envelope` enforces for it. A kind added to
#: :func:`_classify` without an entry here would silently re-open the
#: unvalidated path for that kind, so the mapping is looked up (not
#: ``.get``-ed with a fallback) and is covered by a drift test in
#: ``tests/test_signoff.py``.
_ENVELOPE_SHAPES: dict[str, Any] = {
    "drc": _DrcEnvelope,
    "lvs": _LvsEnvelope,
    "sim": _SimEnvelope,
    "yield": _YieldEnvelope,
    "extract": _ExtractEnvelope,
    "pex": _PexEnvelope,
    "power": _PowerEnvelope,
    "sta": _StaEnvelope,
    "functional-verification": _FunctionalVerificationEnvelope,
    "erc": _ErcEnvelope,
    "place-and-route": _PlaceAndRouteEnvelope,
    "generic": _GenericEnvelope,
    "error": _ErrorEnvelope,
}

#: Runtime checks for the scalar annotations used above. ``bool`` is split
#: out of ``int`` deliberately: ``isinstance(True, int)`` is ``True`` in
#: Python, so a ``"schema_version": true`` envelope would otherwise validate
#: as an integer.
_SCALAR_CHECKS: dict[Any, Any] = {
    bool: lambda value: isinstance(value, bool),
    int: lambda value: isinstance(value, int) and not isinstance(value, bool),
    float: lambda value: (
        isinstance(value, (int, float)) and not isinstance(value, bool)
    ),
    str: lambda value: isinstance(value, str),
    type(None): lambda value: value is None,
}

#: ``X | None`` (PEP 604) and ``Optional[X]`` produce different origins at
#: runtime; both appear in the shapes above.
_UNION_ORIGINS = (Union, types.UnionType)
