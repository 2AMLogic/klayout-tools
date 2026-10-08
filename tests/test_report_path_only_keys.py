"""Registry-completeness tests for the path-only report keys (issue #2723).

Two analyses of identical inputs run from different directories must differ
only in the keys registered in ``_report_verify.PATH_ONLY_KEYS_BY_VERB``
(documented in ``docs/json-contract.md``). Adding a path-bearing key to the
``klt drc``/``klt extract`` report without registering it fails here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from klayout_tools._report_verify import (
    DRC_PATH_ONLY_PATHS,
    EXTRACT_PATH_ONLY_PATHS,
    LIST_ITEM,
    PATH_ONLY_KEYS_BY_VERB,
    VOLATILE_PROVENANCE_PATHS,
    strip_path_only_keys,
)
from klayout_tools.drc import run_drc
from klayout_tools.extract import run_extract
from test_drc import _make_violation_layout, _write_gds_without_timestamps
from test_extract import (
    _make_subcircuit_slice_layout,
)
from test_extract import _write_gds_without_timestamps as _write_extract_gds


def _leaves(value: Any, path: tuple[str, ...] = ()):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _leaves(item, path + (key,))
    elif isinstance(value, list):
        for item in value:
            yield from _leaves(item, path + (LIST_ITEM,))
    else:
        yield path, value


def _drc_report(root: Path) -> dict:
    root.mkdir()
    path = _write_gds_without_timestamps(_make_violation_layout(), root / "v.gds")
    return run_drc(path, "sky130")


def _extract_report(root: Path) -> dict:
    root.mkdir()
    path = _write_extract_gds(_make_subcircuit_slice_layout(), root / "block.gds")
    return run_extract(
        path,
        "sky130",
        output=str(root / "block.spice"),
        top_cell_pins_only=True,
        subcircuit_cell="STAGE",
        parasitics=True,
        spef_output=str(root / "block.spef"),
    )


def _check(report_a: dict, report_b: dict, root_a: Path, root_b: Path, verb: str):
    registered = PATH_ONLY_KEYS_BY_VERB[verb]
    # 1. Nothing outside the registry may embed the invocation directory.
    for report, root in ((report_a, root_a), (report_b, root_b)):
        for path, leaf in _leaves(report):
            if path in registered:
                continue
            assert not (isinstance(leaf, str) and str(root) in leaf), (
                f"unregistered path-bearing key {'.'.join(path)} in {verb} report"
            )
    # 2. Stripping the registry makes the two runs byte-identical (modulo
    # volatile provenance, which is held fixed here by running one build).
    assert strip_path_only_keys(report_a, verb) == strip_path_only_keys(report_b, verb)
    # 3. The raw reports do differ at registered keys (the registry is live).
    assert report_a != report_b


def test_drc_path_only_keys_registry_is_complete(tmp_path):
    a, b = tmp_path / "checkout_a", tmp_path / "nested" / "checkout_b"
    b.parent.mkdir()
    _check(_drc_report(a), _drc_report(b), a, b, "drc")


def test_extract_path_only_keys_registry_is_complete(tmp_path):
    a, b = tmp_path / "checkout_a", tmp_path / "nested" / "checkout_b"
    b.parent.mkdir()
    report_a, report_b = _extract_report(a), _extract_report(b)
    for path in (("subcircuit", "path"), ("spef_path",), ("netlist_path",)):
        assert dict(_leaves(report_a))[path] is not None
    _check(report_a, report_b, a, b, "extract")


def test_registry_shape():
    assert set(PATH_ONLY_KEYS_BY_VERB) == {"drc", "extract"}
    assert {("file",), ("netlist_path",)} <= EXTRACT_PATH_ONLY_PATHS
    assert ("file",) in DRC_PATH_ONLY_PATHS
    assert all(isinstance(p, tuple) and p for p in EXTRACT_PATH_ONLY_PATHS)


def test_strip_path_only_keys_volatile_flag():
    report = {"file": "/x", "provenance": {"klt_version": "1", "deck": "d"}}
    assert strip_path_only_keys(report, "drc") == {
        "provenance": {"klt_version": "1", "deck": "d"}
    }
    assert strip_path_only_keys(report, "drc", include_volatile_provenance=True) == {
        "provenance": {"deck": "d"}
    }
    assert ("provenance", "klt_version") in VOLATILE_PROVENANCE_PATHS


_DOC_ROW = re.compile(r"`([a-z_.*\[\]]+)`")


def test_json_contract_documents_every_registered_key():
    doc = (Path(__file__).parent.parent / "docs" / "json-contract.md").read_text()
    section = doc.split("## Path-only (non-result) keys", 1)[1].split("\n## ", 1)[0]
    documented = {m for m in _DOC_ROW.findall(section)}
    for registry in PATH_ONLY_KEYS_BY_VERB.values():
        for path in registry:
            assert ".".join(path) in documented, path
