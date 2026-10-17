"""``"klayout-runset"`` engine for ``klt lvs`` (issue #2849): run a PDK's own
KLayout LVS runset in batch and take the verdict from its ``.lvsdb``.

This is the LVS-side sibling of ``klt drc --engine klayout``
(:func:`klayout_tools.drc.run_drc_klayout_engine`). Where the default
``"klayout"`` engine extracts with a curated, pip-only deck, this engine
wraps a PDK's *own* runset (e.g. IHP's ``sg13g2.lvs``) so device families the
curated deck does not declare (HBTs, varactors, inductors, ...) are still
recognized and compared. It launches the standalone ``klayout`` application::

    klayout -b -r <deck_file> -rd input=<gds> -rd schematic=<spice> \\
        -rd report=<tmp>.lvsdb -rd top=<top> -rd topcell=<top> [-rd k=v ...]

and then reads KLayout's cross-reference database back with
``klayout.db.LayoutVsSchematic.read()`` + ``xref()``.

**The verdict comes from the database, never from the process.** A PDK
driver exits ``0`` when the netlists do not match; a non-zero exit is
therefore only ever treated as "the runset failed to run" (an application
error), and a zero exit proves nothing. Per circuit pair the cross-reference
carries a ``Status``; the run is a ``"match"`` only when every circuit pair
is ``Match``/``MatchWithWarning`` and none has an unpaired circuit.

**Coverage honesty.** ``coverage`` lists, per device class, what the runset
declared in its layout netlist versus what it recognized (instances > 0) on
each side. A runset that recognizes no device on either side yields
``status: "inconclusive"`` (exit 4) with ``coverage.nothing_checked`` set --
a clean-looking empty compare is not evidence.

This module has no load-time dependency on ``lvs.py`` (it imports from it
inside functions), mirroring ``lvs_netgen.py``.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from typing import TYPE_CHECKING, Any

from ._paths import resolve_tool_binary
from ._provenance import build_provenance, sha256_file
from .coverage import build_check_coverage
from .lvs_mismatch import _mismatch

if TYPE_CHECKING:
    import klayout.db as kdb

RUNSET_ENGINE = "klayout-runset"

#: Default wall-clock budget for the runset (a real PDK runset on a sizeable
#: block can run for minutes). Override via ``options.runset_timeout_s``.
RUNSET_DEFAULT_TIMEOUT_S = 600.0

_KLAYOUT_BINARY_ENV_VAR = "KLT_KLAYOUT_BINARY"

#: ``-rd`` names this engine sets itself; ``deck_vars`` may not override them.
RESERVED_RUNSET_VARS = ("input", "schematic", "report", "top", "topcell")

#: Request ``options`` keys this engine understands. Anything else is refused
#: rather than silently ignored (an option the caller believes is in force
#: but is not would be worse than not supporting it).
_ALLOWED_OPTIONS = ("runset_timeout_s", "klayout_binary")

_ERROR_LINE_RE = re.compile(r"^\s*ERROR\b", re.IGNORECASE)

#: Status vocabulary shared with the other engines (see ``cli/lvs_cmd.py``).
STATUS_MATCH = "match"
STATUS_MISMATCH = "mismatch"
STATUS_INCONCLUSIVE = "inconclusive"

CATEGORY_NO_DEVICES = "coverage.no_devices_recognized"
CATEGORY_RUNSET_INCOMPLETE = "runset.incomplete"


def _resolve_path(path: str, base_dir: str) -> str:
    expanded = os.path.expanduser(os.path.expandvars(path))
    return expanded if os.path.isabs(expanded) else os.path.join(base_dir, expanded)


def _sha_prefixed(path: str | None) -> str | None:
    digest = sha256_file(path)
    return f"sha256:{digest}" if digest is not None else None


def _status_name(status: Any) -> str:
    """Stable name of a ``NetlistCrossReference.Status`` value."""
    return str(status)


def _name(obj: Any) -> str | None:
    if obj is None:
        return None
    name = getattr(obj, "name", None)
    if callable(name):
        name = name()
    if name:
        return str(name)
    return None


def _net_label(net: Any) -> str | None:
    if net is None:
        return None
    return _name(net) or f"${net.cluster_id}"


def _device_label(device: Any) -> str | None:
    if device is None:
        return None
    return _name(device) or str(device.id())


def _refuse_unsupported(
    request: dict[str, Any], layout_spec: dict[str, Any], ref_spec: dict[str, Any]
) -> None:
    from .lvs import LvsError

    for field in ("hints", "parameter_tolerance"):
        if request.get(field) is not None:
            raise LvsError(
                f"request.{field} is only supported for engine 'klayout' -- "
                f"the {RUNSET_ENGINE} engine delegates the whole compare to "
                "the PDK's own runset"
            )
    for side, spec, allowed, why in (
        ("layout", layout_spec, ("file", "top"), "the runset does its own extraction"),
        (
            "reference",
            ref_spec,
            ("netlist", "top"),
            "the reference must be a SPICE netlist in the runset's own dialect",
        ),
    ):
        extra = [k for k in spec if k not in allowed]
        if extra:
            raise LvsError(
                f"{side}.{extra[0]} is not supported by engine '{RUNSET_ENGINE}' "
                f"({why}; only {side}.{' and '.join(allowed)} apply)"
            )
    if "file" not in layout_spec:
        raise LvsError(
            f"engine '{RUNSET_ENGINE}' requires layout.file (a GDS/OASIS stream)"
        )
    if "netlist" not in ref_spec:
        raise LvsError(f"engine '{RUNSET_ENGINE}' requires reference.netlist")


def _existing_file(raw: Any, request_dir: str, label: str) -> str:
    from .lvs import LvsError

    if not isinstance(raw, str) or not raw:
        raise LvsError(f"engine '{RUNSET_ENGINE}' requires {label}")
    path = _resolve_path(raw, request_dir)
    if not os.path.isfile(path):
        raise LvsError(f"{label} not found: {path}")
    return path


def _parse_deck_vars(raw_vars: Any) -> dict[str, str]:
    from .lvs import LvsError

    raw_vars = raw_vars or {}
    if not isinstance(raw_vars, dict):
        raise LvsError("request.deck_vars must be a JSON object of name -> value")
    deck_vars: dict[str, str] = {}
    for name, value in raw_vars.items():
        if name in RESERVED_RUNSET_VARS:
            raise LvsError(
                f"deck_vars may not set '{name}': the {RUNSET_ENGINE} engine "
                f"sets {', '.join(RESERVED_RUNSET_VARS)} itself"
            )
        if isinstance(value, (dict, list)) or value is None:
            raise LvsError(f"deck_vars['{name}'] must be a string, number or boolean")
        deck_vars[name] = str(value).lower() if isinstance(value, bool) else str(value)
    return deck_vars


def _parse_options(raw: Any) -> dict[str, Any]:
    from .lvs import LvsError

    options = raw or {}
    if not isinstance(options, dict):
        raise LvsError("request.options must be a JSON object")
    bad = [k for k in options if k not in _ALLOWED_OPTIONS]
    if bad:
        raise LvsError(
            f"options.{bad[0]} is not supported by engine '{RUNSET_ENGINE}' "
            f"(supported: {', '.join(_ALLOWED_OPTIONS)})"
        )
    return options


def _validate_request(
    request: dict[str, Any], request_dir: str
) -> tuple[str, str, str, dict[str, str], dict[str, Any]]:
    """Validate the request shape; return ``(layout, reference, deck_file,
    deck_vars, options)`` with paths resolved. Raises ``LvsError``."""
    _refuse_unsupported(request, request["layout"], request["reference"])
    deck_file = _existing_file(request.get("deck_file"), request_dir, "deck_file")
    layout = _existing_file(request["layout"]["file"], request_dir, "layout.file")
    reference = _existing_file(
        request["reference"]["netlist"], request_dir, "reference.netlist"
    )
    return (
        layout,
        reference,
        deck_file,
        _parse_deck_vars(request.get("deck_vars")),
        _parse_options(request.get("options")),
    )


def _resolve_klayout_binary(options: dict[str, Any], request_dir: str) -> str:
    from .lvs import LvsError

    resolved = resolve_tool_binary(
        "klayout",
        options,
        option_key="klayout_binary",
        option_label="options.klayout_binary",
        env_var=_KLAYOUT_BINARY_ENV_VAR,
        request_dir=request_dir,
        fallback_names=("klayout",),
        error_cls=LvsError,
        install_hint=(
            "Install the standalone KLayout application "
            "(https://www.klayout.de/build.html) -- the pip `klayout` package "
            "cannot run a PDK runset -- or use engine 'klayout' instead."
        ),
    )
    assert resolved is not None
    return resolved


def _run_runset(
    *,
    binary: str,
    deck_file: str,
    layout: str,
    reference: str,
    top: str | None,
    deck_vars: dict[str, str],
    timeout_s: float,
    work_dir: str,
) -> tuple[str, subprocess.CompletedProcess[str]]:
    """Launch the runset; return ``(lvsdb_path, completed)``. Raises
    ``LvsError`` for a launch failure, timeout, a non-zero exit, or no
    database written."""
    from .lvs import LvsError

    report_path = os.path.join(work_dir, "report.lvsdb")
    cmd = [
        binary,
        "-b",
        "-r",
        deck_file,
        "-rd",
        f"input={layout}",
        "-rd",
        f"schematic={reference}",
        "-rd",
        f"report={report_path}",
    ]
    if top:
        cmd.extend(["-rd", f"top={top}", "-rd", f"topcell={top}"])
    for name, value in deck_vars.items():
        cmd.extend(["-rd", f"{name}={value}"])
    try:
        completed = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s
        )
    except FileNotFoundError as exc:
        raise LvsError(
            f"could not launch klayout: '{binary}' could not be executed ({exc})"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise LvsError(
            f"the LVS runset did not complete within {timeout_s}s (raise "
            "options.runset_timeout_s to allow more time)"
        ) from exc

    output = (completed.stdout or "") + (completed.stderr or "")
    if completed.returncode != 0:
        raise LvsError(
            f"klayout exited with status {completed.returncode} while running "
            f"the LVS runset -- no trustworthy verdict. klayout's own "
            f"output:\n{output.strip()}"
        )
    if not os.path.isfile(report_path):
        raise LvsError(
            "the LVS runset did not write an .lvsdb database -- it likely "
            "failed before comparing (klayout can exit 0 in that case). "
            f"klayout's own output:\n{output.strip()}"
        )
    return report_path, completed


def _class_census(netlist: kdb.Netlist | None) -> tuple[list[str], dict[str, int]]:
    """``(declared_classes, instances_per_class)`` over every circuit."""
    declared = sorted(dc.name for dc in netlist.each_device_class()) if netlist else []
    counts: dict[str, int] = {}
    if netlist is not None:
        for circuit in netlist.each_circuit():
            for device in circuit.each_device():
                cls = device.device_class().name
                counts[cls] = counts.get(cls, 0) + 1
    return declared, counts


def _device_params(device: Any) -> dict[str, float]:
    cls = device.device_class()
    return {
        p.name: device.parameter(p.id())
        for p in cls.parameter_definitions()
        if p.is_primary
    }


def _ok_statuses() -> tuple[Any, Any]:
    import klayout.db as kdb

    x = kdb.NetlistCrossReference
    return (x.Match, x.MatchWithWarning)


def _paired_ok(pair: Any) -> bool:
    return (
        pair.first() is not None
        and pair.second() is not None
        and pair.status() in _ok_statuses()
    )


def _device_findings(xref: Any, cp: Any, cname: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for dp in xref.each_device_pair(cp):
        d1, d2 = dp.first(), dp.second()
        if _paired_ok(dp):
            continue
        dev = d1 if d1 is not None else d2
        dev_echo = {
            "layout": _device_label(d1),
            "reference": _device_label(d2),
            "class": dev.device_class().name,
        }
        if d1 is None or d2 is None:
            side = "reference" if d1 is None else "layout"
            out.append(
                _mismatch(
                    "device.unmatched",
                    "error",
                    f"{side} device '{_device_label(dev)}' (class "
                    f"'{dev.device_class().name}') has no counterpart",
                    side,
                    device=dev_echo,
                    circuit=cname,
                )
            )
        else:
            out.append(
                _mismatch(
                    "device.property",
                    "error",
                    f"devices '{_device_label(d1)}' / '{_device_label(d2)}' "
                    "were paired but do not compare equal",
                    "both",
                    device=dev_echo,
                    details={
                        "layout_parameters": _device_params(d1),
                        "reference_parameters": _device_params(d2),
                    },
                    circuit=cname,
                )
            )
    return out


def _net_findings(xref: Any, cp: Any, cname: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for np_ in xref.each_net_pair(cp):
        n1, n2 = np_.first(), np_.second()
        if _paired_ok(np_):
            continue
        side = (
            "both"
            if n1 is not None and n2 is not None
            else ("reference" if n1 is None else "layout")
        )
        what = (
            f"{side} net '{_net_label(n2 if n1 is None else n1)}' has no counterpart"
            if side != "both"
            else f"nets '{_net_label(n1)}' / '{_net_label(n2)}' were paired but "
            "their connectivity differs"
        )
        out.append(
            _mismatch(
                "net.unmatched",
                "error",
                what,
                side,
                net={"layout": _net_label(n1), "reference": _net_label(n2)},
                circuit=cname,
            )
        )
    return out


def _pin_findings(xref: Any, cp: Any, cname: dict[str, Any]) -> list[dict[str, Any]]:
    out = []
    for pp in xref.each_pin_pair(cp):
        p1, p2 = pp.first(), pp.second()
        if _paired_ok(pp):
            continue
        side = (
            "both"
            if p1 is not None and p2 is not None
            else ("reference" if p1 is None else "layout")
        )
        out.append(
            _mismatch(
                "pin.unmatched",
                "error",
                f"pin '{_name(p1) or _name(p2)}' of circuit "
                f"'{cname['layout']}' is not matched",
                side,
                net={"layout": _name(p1), "reference": _name(p2)},
                circuit=cname,
            )
        )
    return out


def _circuit_findings(xref: Any, cp: Any) -> tuple[list[dict[str, Any]], bool]:
    """Findings for one circuit pair and whether the runset left it
    uncompared."""
    import klayout.db as kdb

    x = kdb.NetlistCrossReference
    first, second = cp.first(), cp.second()
    cname = {"layout": _name(first), "reference": _name(second)}
    status = cp.status()
    if first is None or second is None:
        side = "layout" if second is None else "reference"
        return [
            _mismatch(
                "topology",
                "error",
                f"circuit '{cname[side]}' exists only on the {side} side",
                side,
                circuit=cname,
            )
        ], False
    if status in (x.Skipped, x.None_):
        return [
            _mismatch(
                CATEGORY_RUNSET_INCOMPLETE,
                "error",
                f"circuit '{cname['layout']}' was not compared by the runset "
                f"(status {_status_name(status)})",
                "both",
                circuit=cname,
            )
        ], True
    found = (
        _device_findings(xref, cp, cname)
        + _net_findings(xref, cp, cname)
        + _pin_findings(xref, cp, cname)
    )
    if not found and status not in _ok_statuses():
        # The circuit's own status says no-match but no finer-grained pair
        # explains it: still a mismatch, and say so.
        found.append(
            _mismatch(
                "topology",
                "error",
                f"circuit '{cname['layout']}' does not match (status "
                f"{_status_name(status)}) with no finer-grained pair "
                "reporting why",
                "both",
                circuit=cname,
            )
        )
    return found, False


def _classify(
    xref: kdb.NetlistCrossReference,
) -> tuple[list[dict[str, Any]], bool, bool]:
    """Walk the cross-reference. Returns ``(mismatches, any_error,
    any_not_compared)``."""
    mismatches: list[dict[str, Any]] = []
    not_compared = False
    for cp in xref.each_circuit_pair():
        found, skipped = _circuit_findings(xref, cp)
        mismatches.extend(found)
        not_compared = not_compared or skipped
    any_error = any(
        m["severity"] == "error" and m["category"] != CATEGORY_RUNSET_INCOMPLETE
        for m in mismatches
    )
    return mismatches, any_error, not_compared


def _pair_counts(xref: Any) -> dict[str, dict[str, int]]:
    counts = {
        k: {"layout": 0, "reference": 0, "matched": 0}
        for k in ("nets", "devices", "pins")
    }
    for cp in xref.each_circuit_pair():
        for key, pairs in (
            ("nets", xref.each_net_pair(cp)),
            ("devices", xref.each_device_pair(cp)),
            ("pins", xref.each_pin_pair(cp)),
        ):
            bucket = counts[key]
            for pair in pairs:
                bucket["layout"] += pair.first() is not None
                bucket["reference"] += pair.second() is not None
                bucket["matched"] += _paired_ok(pair)
    return counts


def _net_correspondence(xref: Any) -> list[dict[str, Any]]:
    """Every net the runset's compare paired successfully, in ``klt lvs``'s
    documented ``net_correspondence[]`` shape (issue #2870).

    Item 11's LVS half proves a supply "took part in the compare" by finding
    it paired here (``signoff_power_delivery._lvs_reference_carries_supplies``),
    so this lists only *matched* nets -- same rule as the curated engine's
    ``_build_net_correspondence``. Deduplicated on ``(circuit, layout,
    reference)`` and sorted for a deterministic report.
    """
    from .lvs import _name_or_none

    rows: dict[tuple[str, str | None, str | None], dict[str, Any]] = {}
    for cp in xref.each_circuit_pair():
        circuit = cp.first() if cp.first() is not None else cp.second()
        scope = circuit.name if circuit is not None else ""
        for pair in xref.each_net_pair(cp):
            if not _paired_ok(pair):
                continue
            layout_net = pair.first()
            key = (scope, _name_or_none(layout_net), _name_or_none(pair.second()))
            rows.setdefault(
                key,
                {
                    "layout": key[1],
                    "reference": key[2],
                    "pin": bool(layout_net.pin_count() > 0),
                },
            )
    return sorted(
        rows.values(), key=lambda r: (r["reference"] or "", r["layout"] or "")
    )


def _build_coverage(
    layout_declared: list[str],
    ref_declared: list[str],
    layout_counts: dict[str, int],
    ref_counts: dict[str, int],
) -> dict[str, Any]:
    recognized = sorted(set(layout_counts) | set(ref_counts))
    not_recognized = sorted(
        c for c in set(layout_declared) | set(ref_declared) if c not in recognized
    )
    coverage = build_check_coverage(
        checked=recognized,
        skipped=[
            {"id": c, "reason": "declared_not_recognized"} for c in not_recognized
        ],
    )
    coverage["declared_device_classes"] = {
        "layout": layout_declared,
        "reference": ref_declared,
    }
    coverage["recognized_device_classes"] = {
        "layout": sorted(layout_counts),
        "reference": sorted(ref_counts),
    }
    coverage["declared_not_recognized"] = not_recognized
    return coverage


def read_lvsdb_verdict(lvsdb_path: str) -> dict[str, Any]:
    """Read ``lvsdb_path`` and derive the verdict, mismatches, census and
    coverage from its cross-reference. Raises ``LvsError`` when the database
    is unreadable or carries no cross-reference."""
    import klayout.db as kdb

    from .lvs import LvsError

    l2n = kdb.LayoutVsSchematic()
    try:
        l2n.read(lvsdb_path)
        xref = l2n.xref()
    except Exception as exc:
        raise LvsError(
            f"could not read the runset's .lvsdb database '{lvsdb_path}': {exc}"
        ) from exc
    if xref is None or xref.circuit_count() == 0:
        raise LvsError(
            "the runset's .lvsdb database carries no layout-vs-schematic "
            "cross-reference (the runset extracted the layout but did not "
            "compare it to the schematic) -- no verdict is possible"
        )

    mismatches, any_error, not_compared = _classify(xref)
    layout_declared, layout_counts = _class_census(xref.netlist_a())
    ref_declared, ref_counts = _class_census(xref.netlist_b())
    nothing = not (layout_counts or ref_counts)
    if nothing:
        mismatches.append(
            _mismatch(
                CATEGORY_NO_DEVICES,
                "error",
                "the runset recognized no devices on either side -- an "
                "empty compare is not a verdict",
                "both",
            )
        )
    if any_error:
        status = STATUS_MISMATCH
    elif not_compared or nothing:
        status = STATUS_INCONCLUSIVE
    else:
        status = STATUS_MATCH
    return {
        "status": status,
        "mismatches": mismatches,
        "coverage": _build_coverage(
            layout_declared, ref_declared, layout_counts, ref_counts
        ),
        "device_census": {"layout": layout_counts, "reference": ref_counts},
        "counts": _pair_counts(xref),
        "net_correspondence": _net_correspondence(xref),
        "top": _first_circuit_name(xref, "first"),
        "reference_top": _first_circuit_name(xref, "second"),
    }


def _first_circuit_name(xref: Any, side: str) -> str | None:
    """Name of the top circuit on ``side``: the first circuit pair's circuit
    (the cross-reference lists the top-down order)."""
    netlist = xref.netlist_a() if side == "first" else xref.netlist_b()
    tops = list(netlist.top_circuits()) if netlist is not None else []
    return tops[0].name if tops else None


def _error_lines(output: str) -> list[str]:
    return [ln.strip() for ln in output.splitlines() if _ERROR_LINE_RE.match(ln)]


def run_lvs_klayout_runset(request: dict[str, Any], request_dir: str) -> dict[str, Any]:
    """Run ``request`` through the PDK runset and return the ``klt lvs``
    envelope. See this module's docstring; request fields are documented in
    ``docs/cli/lvs.md`` ("klayout-runset")."""
    from .lvs import SCHEMA_VERSION, _power_connectivity_unchecked

    layout, reference, deck_file, deck_vars, options = _validate_request(
        request, request_dir
    )
    top = request["layout"].get("top")
    timeout_s = float(options.get("runset_timeout_s", RUNSET_DEFAULT_TIMEOUT_S))
    binary = _resolve_klayout_binary(options, request_dir)

    work_dir = tempfile.mkdtemp(prefix="klt-lvs-runset-")
    try:
        lvsdb_path, completed = _run_runset(
            binary=binary,
            deck_file=deck_file,
            layout=layout,
            reference=reference,
            top=top,
            deck_vars=deck_vars,
            timeout_s=timeout_s,
            work_dir=work_dir,
        )
        verdict = read_lvsdb_verdict(lvsdb_path)
    finally:
        import shutil

        shutil.rmtree(work_dir, ignore_errors=True)

    mismatches = verdict["mismatches"]
    category_counts: dict[str, int] = {}
    category_error_counts: dict[str, int] = {}
    for m in mismatches:
        category_counts[m["category"]] = category_counts.get(m["category"], 0) + 1
        if m["severity"] == "error":
            category_error_counts[m["category"]] = (
                category_error_counts.get(m["category"], 0) + 1
            )

    provenance = build_provenance(input_path=layout)
    provenance["deck"] = {
        "name": os.path.basename(deck_file),
        "content_hash": _sha_prefixed(deck_file),
        "released": None,
    }
    runset_block = {
        "deck_file": request["deck_file"],
        "deck_vars": deck_vars,
        "content_hash": _sha_prefixed(deck_file),
        "klayout_application_version": _application_version(binary),
        "engine_error_lines": _error_lines(
            (completed.stdout or "") + (completed.stderr or "")
        ),
    }
    provenance["runset"] = runset_block

    return {
        "schema_version": SCHEMA_VERSION,
        "engine": RUNSET_ENGINE,
        "layout": request["layout"]["file"],
        "reference": request["reference"]["netlist"],
        "top": top or verdict["top"],
        "reference_top": request["reference"].get("top") or verdict["reference_top"],
        "deck_file": request["deck_file"],
        "deck_vars": deck_vars,
        "options": {k: options[k] for k in _ALLOWED_OPTIONS if k in options},
        "hints_applied": None,
        "status": verdict["status"],
        # The curated engine's gate-level power/ground walk needs a layout
        # netlist this engine does not produce (the runset's own extracted
        # netlist is not routed through lvs_supply.py yet); say so rather
        # than omit the block.
        "power_connectivity": _power_connectivity_unchecked(
            f"engine '{RUNSET_ENGINE}' compares the runset's own extraction; "
            "the power/ground connectivity walk is not applied to it"
        ),
        "body_verification": None,
        "device_parameter_coverage": None,
        "coverage": verdict["coverage"],
        "device_census": verdict["device_census"],
        "mismatch_count": len(mismatches),
        "error_count": sum(category_error_counts.values()),
        "category_counts": dict(sorted(category_counts.items())),
        "category_error_counts": dict(sorted(category_error_counts.items())),
        "counts": verdict["counts"],
        "device_classes": verdict["coverage"]["declared_device_classes"]["layout"],
        "environment": {
            "engine": RUNSET_ENGINE,
            "engine_version": runset_block["klayout_application_version"],
            "klayout_binary": binary,
            "netgen_binary": None,
            "layout_sha256": sha256_file(layout),
            "reference_sha256": sha256_file(reference),
            "extracted_netlist": None,
        },
        "provenance": provenance,
        "mismatches": mismatches,
        "net_correspondence": verdict["net_correspondence"],
    }


def _application_version(binary: str) -> str | None:
    """Version string of the standalone application (``klayout -v``)."""
    try:
        out = subprocess.run(
            [binary, "-v"], capture_output=True, text=True, timeout=30
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"(\d+\.\d+(?:\.\d+)?)", out or "")
    return match.group(1) if match else None
