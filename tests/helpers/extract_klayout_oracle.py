"""Test-only oracle: run a PDK-native KLayout LVS-DSL deck's device extraction.

Relocated from ``klayout_tools.extract`` (issue #3875): it has no production
caller and is not a ``klt extract`` CLI flag; only the native-extraction
cross-check tests (``tests/test_lvs_native_extraction_cross_check*.py``) use it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from typing import Any

from klayout_tools._layout import load_layout, resolve_top_cell
from klayout_tools.extract import ExtractError

#: Wall-clock budget (seconds) for the ``klayout`` subprocess
#: :func:`run_extract_klayout_engine` launches -- mirrors
#: ``drc.py``'s own ``KLAYOUT_ENGINE_DEFAULT_TIMEOUT_S`` (same value, a
#: separate module-level constant rather than a cross-module import so
#: ``extract.py``/``drc.py`` stay independent of each other, matching this
#: repo's existing "no cross-import between the two engine-wrapper modules"
#: shape).
EXTRACT_KLAYOUT_ENGINE_DEFAULT_TIMEOUT_S = 300.0

#: SPICE device-parameter names ``sky130.lvs``'s own custom
#: ``SubcircuitModels`` writer (a ``RBA::NetlistSpiceWriterDelegate``) emits
#: as *lengths* (microns) -- see :func:`run_extract_klayout_engine`'s
#: docstring, "Unit round-trip" for the empirically-verified reason a
#: generic re-read needs to undo a x1e6 scale for exactly these names.
_NATIVE_LVS_LENGTH_PARAMS = frozenset({"L", "W", "P", "PS", "PD"})

#: The same round-trip issue for *area* parameters (microns^2), needing a
#: x1e12 undo instead.
_NATIVE_LVS_AREA_PARAMS = frozenset({"A", "AS", "AD"})


def run_extract_klayout_engine(
    path: str,
    lvs_deck_file: str,
    top: str | None = None,
    timeout_s: float = EXTRACT_KLAYOUT_ENGINE_DEFAULT_TIMEOUT_S,
    extra_rd: Mapping[str, str] | None = None,
    bare_length_area_units: bool = True,
) -> dict[str, Any]:
    """Run a PDK-native KLayout LVS-DSL rule-deck script's own **device
    extraction** (``lvs_deck_file``, typically resolved via
    :func:`klayout_tools.pdk.lvs_deck_file`) against the layout at ``path``,
    via the standalone ``klayout`` application binary -- the sky130
    device-extraction cross-check oracle for issue #869 (Epic #711 Phase
    2c), the LVS-device-extraction counterpart of ``drc.py``'s
    ``run_drc_klayout_engine`` (issue #565/#747). See ``docs/cli/extract.md``,
    "sky130 native-deck (``sky130.lvs``) LVS device-extraction cross-check"
    for the full writeup this docstring summarises, and that module's own
    docstring for the DRC-side precedent this one mirrors structurally.

    A PDK's ``.lvs`` script is written to run a *complete* LVS flow --
    device extraction followed by ``compare`` against a reference schematic
    -- and hard-requires a schematic to be present at all (``align``, which
    the script always reaches, raises ``RuntimeError`` immediately without
    one; verified against a real ``sky130.lvs`` for this issue). This
    function is not interested in the compare *verdict* (there is no
    trustworthy independent reference schematic to compare against here --
    the entire point is testing *extraction* agreement) -- it only wants the
    extracted device netlist the script's own ``extract_devices``/
    ``target_netlist`` machinery produces along the way. So it always
    supplies a trivial synthesized stub schematic (an empty
    ``.SUBCKT <top> / .ENDS <top>``, named after the resolved top cell) purely
    to satisfy that hard requirement, and drives three ``-rd`` globals the
    script itself reads to keep the written netlist un-simplified/un-pruned
    relative to the reference it will (deliberately, harmlessly) fail to
    match:

    - ``net_only=true`` / ``top_lvl_pins=true`` -- disables the script's own
      default ``netlist.simplify`` pass (verified for this issue: with
      ``SIMPLIFY`` -- the script's default when neither flag is set --
      the written netlist came back with an empty top-level ``.SUBCKT``,
      *zero* devices, for every fixture tried, including ones the script's
      own extraction logging confirmed it had recognised a device on;
      ``net_only``/``top_lvl_pins`` avoid that pruning) and promotes every
      labelled net to a top-level pin (needed for :func:`resolve_top_cell`-
      selected fixtures whose only pins are drawn labels, mirroring how this
      module's own compiled-deck path always promotes them).

    Invokes::

        klayout -b -r <lvs_deck_file> -rd input=<path> -rd report=<tmp>.lvsdb \\
            -rd target_netlist=<tmp>.cir -rd schematic=<tmp-stub>.cir \\
            -rd net_only=true -rd top_lvl_pins=true [-rd <extra_rd key>=<value> ...]

    ``extra_rd`` (issue #904, Epic #711 Phase 3a) appends additional
    ``-rd key=value`` pairs after the six standard ones above -- needed for a
    native deck whose own variant-selection globals default to a stack this
    repo's compiled deck does not model. For example, gf180mcu's
    ``gf180mcu.lvs`` defaults ``$metal_level`` to ``'6LM'``
    (``topmin1_metal`` = ``Metal5``), but ``decks/gf180mcu.py``'s own MiM
    capacitor models the 5LM variant (``Metal4`` as the MiM stack's bottom
    plate, per its own docstring) -- cross-checking that entry needs
    ``extra_rd={"metal_level": "5LM"}`` to point the native deck's own
    variant selection at the same stack the compiled deck assumes. ``None``
    (the default) appends nothing, matching this function's pre-#904
    behaviour exactly.

    Mirrors ``run_drc_klayout_engine``'s completion discipline exactly:

    - **Binary resolution**: no ``shutil.which`` precheck -- ``subprocess.run``
      is attempted directly and a ``FileNotFoundError`` is caught and
      re-raised as an actionable :class:`ExtractError`.
    - **Timeout**: ``subprocess.TimeoutExpired`` -> :class:`ExtractError`.
    - **Never trusts the exit code.** The script's own ``compare`` against
      the synthesized empty-stub schematic is *expected* to report a
      mismatch (``exit(1)``, logged as ``"ERROR : Netlists don't match"``)
      on every real fixture -- that is not a failure of this function, it is
      the deliberate, harmless side effect of supplying a stub instead of a
      real reference. The *target-netlist file's own presence* is this
      function's only trustworthy completion signal (mirroring
      ``run_drc_klayout_engine``'s "report file's own presence" check and
      ``lvs.py``'s ``_run_netgen_lvs`` "no log file at all" check) -- a
      missing file (the script errored out *before* completing extraction,
      e.g. a malformed ``lvs_deck_file``) is :class:`ExtractError`, carrying
      klayout's own stdout/stderr.

    **Unit round-trip.** ``sky130.lvs``'s own SPICE writer (a custom
    ``RBA::NetlistSpiceWriterDelegate``) emits each device parameter's raw
    internal value directly (already in klayout's native micron/micron^2
    representation, since this deck runs with ``device_scaling`` off) --
    *not* rescaled to plain-SPICE meter/meter^2 convention, and with no
    engineering-notation unit suffix on the written number at all (a bare
    ``L=0.4``, not ``L=0.4U``). Reading that file back through a plain
    ``kdb.NetlistSpiceReader()`` (no custom delegate) re-applies the
    *opposite* assumption for a bare, suffix-less number: it treats a
    MOS/resistor device class's declared length-typed parameters
    (``L``/``W``/``P``/``PS``/``PD``) as meters and rescales by 1e6 to store
    them internally (and area-typed ``A``/``AS``/``AD`` by 1e12) -- verified
    empirically for issue #869 (a written ``L=0.4`` round-trips as
    ``400000.0``, a written ``A=6`` as ``6000000000000.0``; ``R``/``C`` are
    untouched, confirming only the length/area-typed parameters carry this
    reader-side rescale). ``bare_length_area_units=True`` (the default,
    preserving pre-#904 behaviour) undoes exactly that known, fixed factor
    (:data:`_NATIVE_LVS_LENGTH_PARAMS` / 1e6, :data:`_NATIVE_LVS_AREA_PARAMS`
    / 1e12) on read-back, so ``devices[].params`` reports the same
    micron/micron^2/ohm/farad convention :func:`run_extract`'s own
    ``devices[].params`` does, comparable value-for-value.

    **Not every native writer shares this bare-number quirk** (issue #904,
    Epic #711 Phase 3a): gf180mcu's ``gf180mcu.lvs`` uses KLayout's own
    built-in ``write_spice(...)`` (no custom delegate), which *does* emit an
    explicit engineering-notation unit suffix on length/area values
    (``L=0.4U``, ``AS=0.8P``) -- and ``kdb.NetlistSpiceReader()`` parses that
    suffix correctly on read-back, so the value comes back already in the
    same micron/micron^2 convention :func:`run_extract` uses, needing *no*
    further rescale at all (verified empirically for issue #904: a written
    ``L=0.4U`` round-trips as plain ``0.4``, not ``400000.0``). Pass
    ``bare_length_area_units=False`` for a native deck confirmed to write
    unit-suffixed numbers this way, to skip the sky130-specific undo instead
    of silently mis-scaling every length/area parameter by a further,
    incorrect factor of 1e6/1e12.

    Returns a dict shaped closely enough to :func:`run_extract`'s own
    ``devices``/``device_counts`` fields to compare directly (see
    ``docs/cli/extract.md``): ``{"schema_version": 1, "file": path, "deck":
    lvs_deck_file, "engine": "klayout", "top": <resolved top cell name>,
    "device_count": int, "device_counts": {<class name>: int, ...},
    "devices": [{"name": str, "class": str, "params": {<name>: float,
    ...}}, ...]}``. ``device_class`` names come back **upper-cased**
    (``kdb.NetlistSpiceReader``'s own case-folding of every SPICE model
    name it reads, a pre-existing, separately-documented quirk -- see
    ``lvs.py``'s netlist-reading notes) -- a caller comparing against this
    repo's own lower-case ``RuleProvenance.rule_id``/device-class-name
    strings must compare case-insensitively. Unlike :func:`run_extract`,
    there is no ``nets``/``warnings``/``coverage`` -- this is a narrow
    extraction-agreement oracle, not a second general-purpose engine.

    Raises :class:`ExtractError` for every failure mode above, plus a
    missing/unreadable ``path`` or ``lvs_deck_file``, or an unparseable
    extracted-netlist file (checked before/after the subprocess the same
    fail-fast way :func:`run_extract` and ``run_drc_klayout_engine`` do).
    """
    if not os.path.isfile(lvs_deck_file):
        raise ExtractError(f"LVS deck file not found: {lvs_deck_file}")

    layout = load_layout(path, ExtractError)
    top_cell = resolve_top_cell(layout, top, ExtractError, path=path)
    top_cell_name = top_cell.name

    work_dir = tempfile.mkdtemp(prefix="klt-extract-klayout-")
    try:
        stub_schematic_path = os.path.join(work_dir, "stub_schematic.cir")
        with open(stub_schematic_path, "w", encoding="utf-8") as handle:
            handle.write(f".SUBCKT {top_cell_name}\n.ENDS {top_cell_name}\n")

        report_path = os.path.join(work_dir, "report.lvsdb")
        netlist_path = os.path.join(work_dir, "extracted.cir")
        cmd = [
            "klayout",
            "-b",
            "-r",
            lvs_deck_file,
            "-rd",
            f"input={path}",
            "-rd",
            f"report={report_path}",
            "-rd",
            f"target_netlist={netlist_path}",
            "-rd",
            f"schematic={stub_schematic_path}",
            "-rd",
            "net_only=true",
            "-rd",
            "top_lvl_pins=true",
        ]
        for key, value in (extra_rd or {}).items():
            cmd.extend(["-rd", f"{key}={value}"])
        try:
            completed = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout_s
            )
        except FileNotFoundError as exc:
            raise ExtractError(
                "could not launch klayout: binary not found on PATH. "
                "Install KLayout (https://www.klayout.de/build.html) or "
                "use run_extract (the compiled deck) instead. "
                f"({exc})"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ExtractError(
                f"klayout did not complete within {timeout_s}s (raise "
                "timeout_s to allow more time)"
            ) from exc

        if not os.path.isfile(netlist_path):
            # Never trust the exit code alone (see this function's
            # docstring) -- no netlist file at all means the deck script
            # errored out before completing extraction, not merely that its
            # (expected) compare-against-the-stub reported a mismatch.
            raise ExtractError(
                "klayout did not produce an extracted-netlist file -- the "
                "LVS deck script likely failed before completing "
                "extraction. klayout's own output:\n"
                + (completed.stdout or completed.stderr or "").strip()
            )

        import klayout.db as kdb

        parsed = kdb.Netlist()
        reader = kdb.NetlistSpiceReader()
        try:
            parsed.read(netlist_path, reader)
        except Exception as exc:
            raise ExtractError(
                f"could not parse native-deck extracted netlist '{netlist_path}': {exc}"
            ) from exc
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    devices: list[dict[str, Any]] = []
    device_counts: dict[str, int] = {}
    circuit = parsed.circuit_by_name(top_cell_name)
    if circuit is not None:
        for device in circuit.each_device():
            device_class = device.device_class()
            class_name = device_class.name
            params: dict[str, float] = {}
            for param_def in device_class.parameter_definitions():
                raw = device.parameter(param_def.name)
                if bare_length_area_units:
                    if param_def.name in _NATIVE_LVS_LENGTH_PARAMS:
                        raw /= 1.0e6
                    elif param_def.name in _NATIVE_LVS_AREA_PARAMS:
                        raw /= 1.0e12
                params[param_def.name] = raw
            devices.append(
                {
                    # expanded_name() yields a bare $<n> for anonymous devices:
                    # deliberately NOT backslash-escaped, unlike net names. A
                    # device name always follows a class-letter prefix (M$1, R$22)
                    # on its SPICE instance line, so ngspice's leading-$ comment
                    # hazard that spice_safe_net_name() guards against cannot
                    # arise here. See issue #1439 and docs/cli/extract.md.
                    "name": device.expanded_name(),
                    "class": class_name,
                    "params": params,
                }
            )
            device_counts[class_name] = device_counts.get(class_name, 0) + 1

    devices.sort(key=lambda d: d["name"])

    return {
        "schema_version": 1,
        "file": path,
        "deck": lvs_deck_file,
        "engine": "klayout",
        "top": top_cell_name,
        "device_count": len(devices),
        "device_counts": dict(sorted(device_counts.items())),
        "devices": devices,
    }
