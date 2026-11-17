"""Text rendering for ``klt pex``, split out of :mod:`pex_cmd` (size
ratchet, issue #5464).

``--format text`` is a courtesy; the JSON envelope is the contract (see
``docs/json-contract.md``). Everything here only formats a report that
:mod:`pex_cmd` has already obtained.
"""

from __future__ import annotations

import shlex

from ..env_provenance import render_path_field

# Issue #2686: the same one-line `label_layers:` text rendering `klt extract`
# prints for its own echo, reused rather than copied.
from .extract_cmd import _print_label_layers


def _print_submitted_text(report: dict) -> None:
    print(f"submitted: {report['leg_count']} legs  manifest: {report['manifest_path']}")
    for leg in report["legs"]:
        print(f"  {leg['testbench']} {leg['side']}: {leg['attach']}")


def _errored_rows_summary(report: dict) -> str | None:
    """One stderr line for a run that exited 4 on errored ``delta[]`` rows
    (issue #3062): the count, the first distinct cause and its stage -- so
    the cause is visible without opening the JSON. ``None`` when no row
    carries an ``error`` object."""
    errored = [row for row in report.get("delta", []) if row.get("error")]
    if not errored:
        return None
    first = errored[0]["error"]
    return (
        f"klt pex: {len(errored)} of {len(report['delta'])} delta rows errored; "
        f"first cause ({first['side']} side, stage {first['stage']}"
        + (f", {first['code']}" if first.get("code") else "")
        + f"): {' '.join(str(first['message']).split())}"
    )


def _print_mismatch(mismatch: dict | None, name: str) -> None:
    """Print ``mismatch`` (``pin_count_mismatch``, ``flat_dut_mismatch`` or
    ``pin_order_mismatch`` --
    both share the same structured shape: ``subcircuit``, a ``schematic``
    and ``extracted`` per-side detail dict, ``ngspice_message``, and
    ``detail``) under its own ``<name>:`` heading, or nothing at all when
    ``mismatch`` is ``None``."""
    if mismatch is None:
        return
    print()
    print(f"{name}:")
    for side in ("schematic", "extracted"):
        entry = mismatch[side]
        pins = entry["pins"]
        print(
            f"  {side}: {entry['netlist']}  "
            f"subckt={entry['subcircuit']}  pins={entry['pin_count']}"
            + (f"  ({', '.join(pins)})" if pins else "")
        )
    if mismatch["ngspice_message"]:
        print(f"  ngspice: {mismatch['ngspice_message']}")
    print(f"  {mismatch['detail']}")


def _print_schematic_flatten(block: dict | None) -> None:
    """One ``schematic_flatten:`` line (issue #2889), or nothing when
    ``--flatten-schematic`` was not given."""
    if block is None:
        return
    print(
        f"schematic_flatten: {render_path_field(block['netlist'])}  "
        f"inlined={block['inlined_instances']}  {block['netlist_sha256']}"
    )


def _print_device_only_leg(report: dict) -> None:
    """One ``device_only_netlist:`` line (issue #3298), or nothing when
    ``--device-only-leg`` was not given."""
    if "device_only_netlist" not in report:
        return
    print(
        f"device_only_netlist: {render_path_field(report['device_only_netlist'])}  "
        f"device_cards={report['device_card_count']}  "
        f"match={report['device_cards_match']}"
    )


def _device_only_desc(row: dict) -> str:
    """The ``--device-only-leg`` split of one delta row (issue #3298), or
    nothing for a row that carries none."""
    if "device_only_value" not in row:
        return ""

    def pct(value: float | None) -> str:
        return f"{value:+.3f}%" if value is not None else "-"

    return (
        f"  device_only={row['device_only_value']!r}  "
        f"representation={pct(row['representation_delta_pct'])}  "
        f"parasitic={pct(row['parasitic_delta_pct'])}"
    )


def _print_measurement(measurement: dict) -> None:
    """The text report's ``measurement:`` line(s): ``--extract-only``
    (issue #3739) and ``--measure-command`` runs only."""
    if measurement.get("mode") == "extract-only":
        print("measurement: extract-only  (nothing measured; no delta rows)")
    if measurement.get("mode") == "command":
        print(f"measurement: command  {shlex.join(measurement['command'])}")
        for side, entry in (measurement.get("sides") or {}).items():
            print(
                f"  {side}: {render_path_field(entry['netlist'])}  "
                f"exit={entry['exit_status']}  "
                f"corners={entry['corner_count']}  "
                f"measurements={', '.join(entry['measurement_names'])}"
            )


def _print_tops(tops: list | None) -> None:
    """The text report's ``tops:`` section (issue #5494) -- nothing at all on
    a single-top run, where ``tops`` is ``null``.

    One line per ``--top`` in ``--top`` order, naming the cell, its own staged
    extraction, its device/net counts and its ``body_bias`` verdict, so a
    reader of the courtesy output can attribute the summed counts on the
    ``extraction:`` line above to the tops that contributed them without
    reaching for ``--format json``.
    """
    if not tops:
        return
    print(f"tops: {len(tops)}  ({', '.join(entry['top'] for entry in tops)})")
    for entry in tops:
        extraction = entry["extraction"]
        print(
            f"  {entry['top']}: {render_path_field(entry['netlist'])}  "
            f"devices={extraction['device_count']}  "
            f"nets={extraction['net_count']}  "
            f"body_bias={entry['body_bias']['status']}"
        )
        for field in ("pin_count_mismatch", "flat_dut_mismatch", "pin_order_mismatch"):
            if entry[field] is not None:
                print(f"    {field}: {entry[field]['detail']}")


def _print_text(report: dict) -> None:
    # Issue #1261: `layout`/`netlist`/`reference_netlist` are the
    # `{path, scope}` shape `env_provenance.repo_relative_path` defines --
    # `render_path_field` is the same courtesy-rendering helper `klt
    # env-provenance`'s own text output uses, so a path outside the repo
    # never prints an absolute path here either.
    print(f"layout: {render_path_field(report['layout'])}")
    print(f"netlist: {render_path_field(report['netlist'])}")
    print(f"reference_netlist: {render_path_field(report['reference_netlist'])}")
    # Additive (issue #2889): only printed when --flatten-schematic was given.
    _print_schematic_flatten(report.get("schematic_flatten"))
    # Additive (issue #3298): only printed when --device-only-leg was given.
    _print_device_only_leg(report)
    print(f"status: {report['status']}")
    print(
        f"corners: {report['corner_count']}  "
        f"rows: {len(report['delta'])}  "
        f"passed: {report['passed']}  "
        f"failed: {report['failed']}  "
        f"errored: {report['errored']}"
    )

    extraction = report["extraction"]
    print(
        f"extraction: deck={extraction['deck']}  "
        f"devices={extraction['device_count']}  nets={extraction['net_count']}"
    )
    # Additive (issue #5494): only printed for a multi-top run, where the
    # counts above are sums over every `--top` and `netlist:` names the
    # combined file. Nothing is printed for a single-top run.
    _print_tops(report.get("tops"))
    # Additive (issue #2686): only printed when --label-layer was given.
    _print_label_layers(extraction.get("label_layers"))
    # Additive (issue #976): only printed when --critical-net was given.
    critical_nets = extraction.get("critical_nets")
    if critical_nets:
        print(f"critical_nets: {', '.join(critical_nets)}")
    # Additive (issue #977): only printed when --distributed-rc was given.
    if extraction.get("distributed_rc"):
        print("distributed_rc: on")
    # Additive (issue #4037): only printed when --resistance-graph was given.
    resistance_graph = extraction.get("resistance_graph")
    if resistance_graph is not None:
        graph_nets = [
            n for n in resistance_graph["nets"] if n["resistance_source"] == "graph"
        ]
        print(
            f"resistance_graph: on  graph_nets={len(graph_nets)}/"
            f"{len(resistance_graph['nets'])}"
        )
    # Additive (issue #988): only printed when --mom-rlc-net was given.
    mom_rlc_override = extraction.get("mom_rlc_override")
    if mom_rlc_override is not None:
        print(
            f"mom_rlc_override: net={mom_rlc_override['net']}  "
            f"r_ohm={mom_rlc_override['resistance_ohm']}  "
            f"c_ff={mom_rlc_override['capacitance_ff']}  "
            f"l_nh={mom_rlc_override['inductance_nh']}"
        )

    # Additive (issue #2478): only printed for a `--measure-command` run --
    # a testbench-mode report's `measurement` block carries no information
    # the `testbenches:` section below does not already show.
    measurement = report.get("measurement") or {}
    _print_measurement(measurement)

    # Additive (issue #1030): only printed when the extracted-side deck was
    # rejected for a schematic/extracted top-level pin-list mismatch.
    _print_mismatch(report.get("pin_count_mismatch"), "pin_count_mismatch")
    # Additive (issue #1255, Gap 2): only printed when the schematic DUT is
    # flat (no `.SUBCKT` wrapper) against a `.SUBCKT`-wrapped extraction.
    _print_mismatch(report.get("flat_dut_mismatch"), "flat_dut_mismatch")
    # Additive (issue #2890): only printed when both sides declare the same
    # pins in a different order.
    _print_mismatch(report.get("pin_order_mismatch"), "pin_order_mismatch")

    testbenches = report["testbenches"]
    if testbenches:
        print()
        print("testbenches:")
        for tb in testbenches:
            print(
                f"  {render_path_field(tb['request'])}  corners={tb['corner_count']}  "
                f"measurements={', '.join(tb['measurement_names'])}"
            )

    delta = report["delta"]
    if not delta:
        return

    print()
    print("delta:")
    for row in delta:
        delta_pct = row["delta_pct"]
        delta_desc = f"{delta_pct:+.3f}%" if delta_pct is not None else "-"
        print(
            f"  {row['spec_row']} @ {row['corner_id']}  [{row['status']}]  "
            f"schematic={row['schematic_value']!r}  "
            f"extracted={row['extracted_value']!r}  delta={delta_desc}"
            + _device_only_desc(row)
        )


def _print_dry_run_text(report: dict) -> None:
    """``klt pex --dry-run``'s text output (issue #5464): one summary line,
    then one line per ``not_checked`` code.

    Every leg expands to the same unit list (the extracted / device-only
    requests are DUT-swapped copies of the testbench request), so one
    per-leg unit count describes all of them.
    """
    legs = report["legs"]
    units = sum(tb["sim"]["unit_count"] for tb in report["testbenches"])
    print(
        f"klt pex --dry-run: valid ({len(report['testbenches'])} testbench(es), "
        f"legs={'+'.join(legs) if legs else 'none'}, {units} unit(s) per leg)"
    )
    for code in report["not_checked"]:
        print(f"  not checked: {code}")
