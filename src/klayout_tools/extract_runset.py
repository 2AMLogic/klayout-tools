"""``"klayout-runset"`` engine for ``klt extract`` (issue #5481): run a PDK's
own KLayout LVS runset in batch and take the **extracted netlist** out of the
``.lvsdb`` it writes.

The engine shares the launcher, timeout handling and error reporting of
``klt lvs --engine klayout-runset`` through :mod:`.lvs_runset`.

A vendor runset can extract device families outside the curated decks, such
as IHP's ``pnpMPA`` on ``sg13cmos5l``. Its extracted circuit feeds the same
device and net report as a curated run.

**What it does NOT do.** No parasitics (``--parasitics`` and everything built
on it is refused by name, not silently ignored): joining runset devices with
curated-deck parasitics would need a geometric net correspondence between two
independent, independently-numbered extractions, which nothing in this repo
provides. ``klt pex`` can still drive this engine parasitics-free (``klt pex
--engine klayout-runset``, issue #6559 -- :mod:`klayout_tools.pex_runset`),
and its report discloses the gap as ``extraction.parasitics_carried: false``.
See ``docs/cli/extract.md``'s "Engine" section.

How a netlist is obtained
-------------------------

1. A PDK runset is written to run a *complete* LVS flow and hard-requires a
   schematic (it reaches ``align``/``compare`` unconditionally unless its own
   ``net_only`` switch is set). This engine therefore synthesizes a trivial
   empty ``.SUBCKT <top> / .ENDS <top>`` stub purely to satisfy that
   requirement, and **discards the compare verdict entirely** -- a stub never
   matches, and matching is `klt lvs`'s job, not this verb's.

   Measured against IHP's ``sg13cmos5l.lvs`` for issue #5481 (the open question
   the issue asked to settle first): the stub does **not** cost devices. The
   runset's own log reports ``Devices to compare: layout=2 schematic=0`` after
   ``align``, its default ``SIMPLIFY`` pass keeps both, and the written
   ``.lvsdb`` carries them. Its ``net_only=true`` switch -- which skips the
   compare and would need no stub at all -- was also measured and **rejected**:
   on that runset it writes neither the ``.lvsdb`` nor a side netlist, so there
   is nothing to read back.

2. The netlist is read from the ``.lvsdb`` with
   ``kdb.LayoutVsSchematic.read()`` + ``.netlist()`` -- the same database
   ``lvs_runset`` already reads for its census and power-connectivity blocks --
   and **never** from a runset's own ``<top>_extracted.cir`` side file. That
   file's location is runset-specific (``sg13cmos5l.lvs`` derives it from the
   *layout's* directory, not from the process cwd), and depending on it would
   make this engine runset-specific too.

3. SPICE is written by ``klt``'s own writer, through the delegate below.

Device cards
------------

Every extracted device is written as an ``X`` subcircuit call onto its own
class name -- see :mod:`klayout_tools.extract_runset_spice` for why KLayout's
default ``Q``/``R`` cards do not bind against a PDK model library, and why no
measured parameter rides the call unless ``--device-param`` declares how
(issue #5513). What this engine accepts and refuses is
:mod:`klayout_tools.extract_runset_flags`; both are re-exported here.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Mapping
from typing import Any

from . import env_provenance
from ._provenance import build_provenance, sha256_file
from .extract_base import SCHEMA_VERSION, _build_metrics
from .extract_progress import _staged

# Public face: callers import from here; sibling modules are an internal split only.
from .extract_runset_flags import (
    DEFAULT_ENGINE as DEFAULT_ENGINE,
)
from .extract_runset_flags import (
    ENGINES as ENGINES,
)
from .extract_runset_flags import (
    REFUSED_FLAG_BY_PARAM as REFUSED_FLAG_BY_PARAM,
)
from .extract_runset_flags import (
    RESERVED_RUNSET_VARS as RESERVED_RUNSET_VARS,
)
from .extract_runset_flags import (
    RUNSET_ENGINE as RUNSET_ENGINE,
)
from .extract_runset_flags import (
    parse_deck_vars as parse_deck_vars,
)
from .extract_runset_flags import (
    parse_device_params as parse_device_params,
)
from .extract_runset_flags import (
    provenance_hash_check as provenance_hash_check,
)
from .extract_runset_flags import (
    refuse_batch_for_runset_engine as refuse_batch_for_runset_engine,
)
from .extract_runset_flags import (
    refuse_unsupported as refuse_unsupported,
)
from .extract_runset_flags import (
    refused_flags as refused_flags,
)
from .extract_runset_flags import (
    require_deck_file as require_deck_file,
)
from .extract_runset_flags import (
    require_runset_engine_for_deck_vars as require_runset_engine_for_deck_vars,
)
from .extract_runset_flags import (
    validate_engine as validate_engine,
)
from .extract_runset_labels import (
    NumberedLabels,
    numbered_label_net_ids,
    numbered_labels,
)
from .extract_runset_spice import (
    DeviceParams,
    _check_device_params,
    _device_params_echo,
    _dropped_parameter_warnings,
    _instantiated_device_counts,
    _prepare_merged_node_names,
    _uncarried_parameters,
    _write_netlist,
)
from .extract_spef import ExtractError

#: Default wall-clock budget for the runset, shared with ``klt lvs``.
RUNSET_DEFAULT_TIMEOUT_S = 600.0


def _resolve_path(path: str, base_dir: str) -> str:
    expanded = os.path.expanduser(os.path.expandvars(path))
    return expanded if os.path.isabs(expanded) else os.path.join(base_dir, expanded)


def _sha_prefixed(path: str | None) -> str | None:
    digest = sha256_file(path)
    return f"sha256:{digest}" if digest is not None else None


def _stub_schematic(work_dir: str, top: str) -> str:
    """Write the empty ``.SUBCKT``/``.ENDS`` stub the runset's ``align`` step
    needs, and return its path. See this module's docstring for why a stub is
    sound here and what was measured to establish that."""
    path = os.path.join(work_dir, "klt-extract-stub.cir")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(
            "* synthesized by klt extract --engine klayout-runset: an empty\n"
            "* stub purely to satisfy the runset's schematic requirement. The\n"
            "* compare verdict it produces is discarded -- use klt lvs for a\n"
            "* real comparison.\n"
            f".SUBCKT {top}\n"
            f".ENDS {top}\n"
        )
    return path


def _read_runset_netlist(lvsdb_path: str) -> tuple[Any, Any]:
    """``(owner, netlist)`` for the layout side of the runset's ``.lvsdb``.

    ``LayoutVsSchematic.netlist()`` hands back a *borrowed* reference into the
    database object, not an owned copy: let the ``LayoutVsSchematic`` be
    collected and the next call on the netlist raises ``Object has been
    destroyed already``. The owner is returned alongside so the caller can
    keep it alive for as long as the netlist is used.
    """
    import klayout.db as kdb

    owner = kdb.LayoutVsSchematic()
    try:
        owner.read(lvsdb_path)
    except Exception as exc:  # any read failure is one error
        raise ExtractError(
            f"could not read the runset's .lvsdb database '{lvsdb_path}': {exc}"
        ) from exc
    netlist = owner.netlist()
    # `read()` does not raise on a file that is not a database at all -- it
    # hands back an empty one -- so "no circuits" is the real unreadable /
    # never-extracted signal, not an exception.
    if netlist is None or not any(True for _ in netlist.each_circuit()):
        raise ExtractError(
            f"could not read the runset's .lvsdb database '{lvsdb_path}': it "
            "carries no extracted layout netlist (no circuits). The runset "
            "wrote a file but never extracted the layout, or what it wrote is "
            "not a KLayout LVS database"
        )
    return owner, netlist


def _select_circuit(netlist: Any, top: str) -> Any:
    """The circuit the netlist was extracted for: ``top`` by name, else the
    netlist's sole top circuit."""
    circuit = netlist.circuit_by_name(top)
    if circuit is not None:
        return circuit
    tops = list(netlist.top_circuits())
    if len(tops) == 1:
        return tops[0]
    names = ", ".join(sorted(c.name for c in netlist.each_circuit())) or "(none)"
    raise ExtractError(
        f"the runset's extracted netlist has no circuit named '{top}' and no "
        f"single top circuit to fall back on (circuits: {names}). The runset "
        "may have flattened or renamed the top cell during alignment"
    )


def _resolve_binary(klayout_binary: str | None, base_dir: str) -> str:
    """The standalone ``klayout`` application, through ``klt lvs``'s own
    resolver (and its own error text, re-raised as an ``ExtractError``)."""
    from . import lvs_runset
    from .lvs import LvsError

    options: dict[str, Any] = {}
    if klayout_binary is not None:
        options["klayout_binary"] = klayout_binary
    try:
        return lvs_runset._resolve_klayout_binary(options, base_dir)
    except LvsError as exc:
        raise ExtractError(str(exc)) from exc


def _launch_runset(
    *,
    binary: str,
    deck_file: str,
    layout: str,
    top: str,
    deck_vars: dict[str, str],
    timeout_s: float,
    work_dir: str,
) -> tuple[str, Any]:
    """Synthesize the stub schematic and run the runset; return
    ``(lvsdb_path, completed)``."""
    from . import lvs_runset
    from .extract_progress import stage
    from .lvs import LvsError

    stub = _stub_schematic(work_dir, top)
    try:
        with stage("runset"):
            return lvs_runset._run_runset(
                binary=binary,
                deck_file=deck_file,
                layout=layout,
                reference=stub,
                top=top,
                deck_vars=deck_vars,
                timeout_s=timeout_s,
                work_dir=work_dir,
            )
    except LvsError as exc:
        # The launcher's text is `klt lvs`'s (deliberately shared), but its
        # one knob reference names that verb's request field; point it at
        # this verb's own flag instead.
        raise ExtractError(
            str(exc).replace(
                "raise options.runset_timeout_s to allow more time",
                "raise --runset-timeout to allow more time",
            )
        ) from exc


def run_extract_klayout_runset(
    path: str,
    *,
    deck_file: str,
    output: str | None = None,
    top: str | None = None,
    deck_vars: Mapping[str, str] | None = None,
    device_models: Mapping[str, str] | None = None,
    device_params: DeviceParams | None = None,
    no_netlist: bool = False,
    runset_timeout_s: float | None = None,
    klayout_binary: str | None = None,
    request_dir: str | None = None,
    lvsdb_out: str | None = None,
) -> dict[str, Any]:
    """Extract ``path`` by running the PDK runset ``deck_file`` and return the
    ``klt extract`` report. See this module's docstring for the mechanism and
    ``docs/cli/extract.md``'s "Engine" section for the CLI contract.

    Raises :class:`ExtractError` for every failure -- a missing ``klayout``
    binary, a runset that wrote no database, a timeout, an unreadable
    database -- reusing ``klt lvs``'s own message text wherever the shared
    launcher produces it.

    ``lvsdb_out`` (issue #6606, internal -- no CLI flag) copies the runset's
    ``.lvsdb`` to that path before the scratch work directory is removed, so
    a caller (:mod:`klayout_tools.pex_net_correspondence`) can read the
    database's per-net geometry back after this function returns.
    """
    from . import lvs_runset
    from .extract import _normalized_netlist_sha256
    from .extract_progress import stage
    from .extract_report import _describe_devices, _describe_nets

    base_dir = request_dir or os.getcwd()
    resolved_deck = _resolve_path(deck_file, base_dir)
    if not os.path.isfile(resolved_deck):
        raise ExtractError(f"--deck-file not found: {resolved_deck}")
    if not os.path.isfile(path):
        raise ExtractError(f"file not found: {path}")

    vars_ = parse_deck_vars(dict(deck_vars) if deck_vars else {})
    models = dict(device_models) if device_models else {}
    declared = dict(device_params) if device_params else {}
    timeout_s = float(
        runset_timeout_s if runset_timeout_s is not None else RUNSET_DEFAULT_TIMEOUT_S
    )

    # The top cell has to be known before the runset runs: the stub schematic
    # is named after it, and the runset is told which cell to extract.
    with stage("layout_load"):
        top_cell_name, dbu_um, labels = _layout_identity(path, top)

    binary = _resolve_binary(klayout_binary, base_dir)

    work_dir = tempfile.mkdtemp(prefix="klt-extract-runset-")
    try:
        lvsdb_path, completed = _launch_runset(
            binary=binary,
            deck_file=resolved_deck,
            layout=path,
            top=top_cell_name,
            deck_vars=vars_,
            timeout_s=timeout_s,
            work_dir=work_dir,
        )
        if lvsdb_out is not None:
            shutil.copy2(lvsdb_path, lvsdb_out)

        with stage("lvsdb_read"):
            # `lvsdb` is the owner of `netlist`; it must stay referenced for
            # as long as the netlist is read or written below.
            lvsdb, netlist = _read_runset_netlist(lvsdb_path)
            circuit = _select_circuit(netlist, top_cell_name)
            # Every circuit the runset produced is written, not just `circuit`:
            # dropping the others would orphan any `X` call the top makes into
            # them. A runset in its default flat mode (and IHP's `align` step,
            # which flattens unmatched sub-circuits into their callers) leaves
            # exactly one, which is the shape the curated engine also has; more
            # than one is disclosed below rather than silently summarised away.
            # Both are read out as plain strings here -- the report carries
            # names, never live KLayout objects. `circuit`/`netlist` are
            # borrowed references into `lvsdb`, which stays referenced (the
            # local above) for as long as the reads and writes below need it.
            circuit_name = circuit.name
            extra_circuits = sorted(
                c.name for c in netlist.each_circuit() if c.name != circuit_name
            )

        # Checked before anything is written: an unknown `--device-param`
        # parameter is an error, never a device silently left at its default.
        aliases = _prepare_merged_node_names(circuit) if lvsdb_out else []
        declared_warnings = _check_device_params(netlist, declared)
        uncarried = _uncarried_parameters(netlist, declared)
        reported_netlist_path, netlist_sha256 = _write_netlist(
            netlist=netlist,
            path=path,
            output=output,
            no_netlist=no_netlist,
            deck_basename=os.path.basename(resolved_deck),
            models=models,
            device_params=declared,
            uncarried=uncarried,
        )

        devices, device_counts = _staged("describe_devices", _describe_devices, circuit)
        instantiated = _instantiated_device_counts(circuit)
        nets = _staged(
            "describe_nets",
            _describe_nets,
            circuit,
            runset_label_ids=numbered_label_net_ids(lvsdb, circuit, labels),
        )
        runset_block = {
            "deck_file": deck_file,
            "deck_vars": vars_,
            "content_hash": _sha_prefixed(resolved_deck),
            "klayout_application_version": lvs_runset._application_version(binary),
            "exit_status": completed.returncode,
            "engine_error_lines": lvs_runset._error_lines(
                (completed.stdout or "") + (completed.stderr or "")
            ),
        }
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    warnings = declared_warnings + _dropped_parameter_warnings(
        uncarried, models, declared
    )
    if extra_circuits:
        warnings.append(
            f"the runset's netlist holds {len(extra_circuits) + 1} circuits "
            f"({', '.join([circuit_name] + extra_circuits)}); all of them are "
            f"written, but devices[]/nets[] and every count other than "
            f"instantiated_device_counts describe '{circuit_name}' alone. "
            "Re-run the runset in flat mode (for "
            "sg13cmos5l.lvs: --deck-var run_mode=flat) for the single-circuit "
            "shape the curated engine always produces"
        )
    if not instantiated:
        warnings.append(
            "the runset recognised no devices in this layout -- an empty "
            "extraction is not evidence. Check that the runset matches this "
            "PDK and that --top names the cell the devices are drawn in"
        )

    provenance = build_provenance(input_path=path)
    provenance["deck"] = {
        "name": os.path.basename(resolved_deck),
        "content_hash": _sha_prefixed(resolved_deck),
        "released": None,
    }
    provenance["runset"] = runset_block

    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        # Present only under this engine, so a curated-deck report is
        # byte-identical to one produced before this engine existed.
        "engine": RUNSET_ENGINE,
        "file": env_provenance.repo_relative_path(
            path, repo_root=env_provenance.repo_root_for(path)
        ),
        # No curated deck ran: the runset *is* the deck, named by `deck_file`.
        "deck": None,
        "deck_file": deck_file,
        "deck_vars": vars_,
        "top": top_cell_name,
        "dbu_um": dbu_um,
        "netlist_path": reported_netlist_path,
        "netlist_sha256": netlist_sha256,
        # Same contract as the curated engine (issue #3407): `null` exactly
        # when `netlist_sha256` is, otherwise a numbering-independent digest
        # of the flat `devices[]`/`nets[]` connectivity -- so two runs of the
        # same layout compare equal despite KLayout's unstable `$N` names.
        "netlist_normalized_sha256": (
            None
            if netlist_sha256 is None
            else _normalized_netlist_sha256(devices, nets)
        ),
        "status": "extracted",
        "device_count": len(devices),
        "net_count": len(nets),
        "pin_count": sum(1 for net in nets if net["pin"]),
        "device_counts": dict(sorted(device_counts.items())),
        "device_classes": sorted(device_counts),
        "devices": devices,
        "nets": nets,
        **({"runset_node_renames": aliases} if aliases else {}),
        "device_models": dict(sorted(models.items())) if models else None,
        # Additive (issue #5513): present only when `--device-param` was given.
        **_device_params_echo(declared),
        # Additive (issue #6559): the measured parameters each X call does NOT
        # carry, per class -- the structured form of the `warnings[]` prose,
        # `{}` once every parameter is declared. `klt pex` copies it.
        "params_not_carried": {k: sorted(v) for k, v in sorted(uncarried.items())},
        # Additive (issue #6559, hierarchical runsets): `device_counts` covers
        # the selected circuit alone, so these two say what is outside it --
        # the devices instantiated through every called circuit, and the
        # circuits written besides the selected one (`[]` for a flat runset,
        # where the former equals `device_counts`).
        "instantiated_device_counts": instantiated,
        "extra_circuits": extra_circuits,
        # This engine extracts devices and connectivity only, so there are no
        # parasitics; `klt pex --engine klayout-runset` reports that as
        # `extraction.parasitics_carried: false` (docs/cli/pex.md, "Runset
        # engine").
        "parasitics": None,
        "pdk": None,
        "warnings": warnings,
        "provenance": provenance,
    }
    result["metrics"] = _build_metrics(result, None)
    return result


def _layout_identity(path: str, top: str | None) -> tuple[str, float, NumberedLabels]:
    """Resolve the top, units and labels which can resemble serialized net numbers."""
    from ._layout import load_layout, resolve_top_cell

    layout = load_layout(path, ExtractError)
    cell = resolve_top_cell(layout, top, ExtractError, path=path)
    return cell.name, layout.dbu, numbered_labels(layout, cell)
