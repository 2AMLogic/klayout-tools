"""``attestation.description``: the inventory's free-text scope disclosure
carried into the ``klt signoff --attest`` envelope (issue #5573)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from klayout_tools.signoff import build_tier_report
from klayout_tools.signoff_attest import (
    build_attestation,
    check_attestation,
    render_envelope,
)
from klayout_tools.signoff_attest_drift import VOLATILE_ATTESTATION_PATHS
from klayout_tools.signoff_attest_input import input_line
from klayout_tools.signoff_attest_stream import (
    build_stream_attestation,
    check_stream_attestation,
)
from test_signoff_attest_duration import _audit_seconds_masked

_DISCLOSURE = "planning-level geometry; not DRC-clean, not extraction-checked"
#: A bare command spec (items 1/8/9: no `name`).
_CMD = {"argv": [sys.executable, "-c", "pass"]}
#: An item-10-style named audit check (items 2/10).
_CHECK = {"name": "ci", "argv": [sys.executable, "-c", "pass"]}
_CHECK_FAIL = {"name": "ci", "argv": [sys.executable, "-c", "raise SystemExit(1)"]}
#: The key order a description-less envelope must keep, so it renders
#: byte-identically to pre-#5573 output.
_PRE_5573_ATTESTATION_KEYS = [
    "producer",
    "inventory_schema_version",
    "hash_algorithm",
    "check_count",
    "passed_count",
    "checks",
]
_PRE_5573_STREAM_ATTESTATION_KEYS = [
    key for key in _PRE_5573_ATTESTATION_KEYS if key != "inventory_schema_version"
] + ["inventory"]


def _repo(tmp_path) -> Path:
    """A throwaway git repository (a ``.git`` entry is all
    ``find_repo_root`` needs) holding the files every item's inventory
    names, plus ``attest/`` and ``evidence/`` directories."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / "attest").mkdir()
    (repo / "evidence").mkdir()
    files = {
        "design/ota.sch": "v {xschem}\n",
        "netlist/ota.spice": "* ota\n.end\n",
        "tb/ota_ac.spice": "* ac\n",
        "report.md": "# characterization\n",
        "README.md": "# ota\n",
        "LICENSE": "MIT\n",
    }
    for rel, content in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (repo / "evidence" / "sim_tt.json").write_text(
        json.dumps({"status": "pass", "result": {"fmax": 100, "area": 5, "power": 2}})
    )
    return repo


def _inventory(item: int, description: str | None = ...) -> dict:
    """A well-formed inventory for ``item``; ``description`` is carried
    verbatim, dropped for ``None`` and set to the given string otherwise
    (``...`` means "no description key at all", the falsy cases need the
    key present)."""
    base: dict = {
        "schema_version": 1,
        "kind": "t1-attestation-inventory",
        "t1_item": item,
    }
    if item == 1:
        base["sources"] = ["design/ota.sch"]
        base["derived"] = [
            {"path": "netlist/ota.spice", "generator": "klt netlist", "check": _CMD}
        ]
    elif item == 8:
        base.update(
            report="report.md",
            spec_rows=["gain"],
            citations={"gain": ["evidence/sim_tt.json"]},
            envelope_checks={"evidence/sim_tt.json": _CMD},
        )
    elif item == 9:
        base["testbenches"] = ["tb/ota_ac.spice"]
        base["cold_start"] = _CMD
        base["pins"] = {
            "pdk": {
                "name": "sky130A",
                "revision": "0fe599b2afb6708d281543108caf8310912f54af",
            },
            "tools": {"ngspice": "42"},
        }
    else:
        base["hygiene"] = ["README.md", "LICENSE"]
        base["checks"] = [_CHECK]
    if description is not ...:
        base["description"] = description
    return base


def _attest(repo: Path, item: int, description: str | None = ...) -> dict:
    path = repo / "attest" / f"item{item}.json"
    path.write_text(json.dumps(_inventory(item, description)))
    return build_attestation(item, str(path))


def _attest_item8(repo: Path, description: str | None = ...) -> dict:
    """An item-8 attestation that passes (so it grades `met`): the cited
    evidence envelope must itself be a recognised one, which the shared
    `_repo` fixture's bare `{status, result}` file is not."""
    (repo / "evidence" / "sim_tt.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "generic",
                "status": "pass",
                "result": {"fmax": 100, "area": 5, "power": 2},
            }
        )
    )
    return _attest(repo, 8, description)


def _stream(repo: Path, *, dbu: float = 0.001, empty: bool = False) -> str:
    import klayout.db as kdb

    layout = kdb.Layout()
    layout.dbu = dbu
    top = layout.create_cell("top")
    if not empty:
        top.shapes(layout.layer(1, 0)).insert(kdb.Box(0, 0, 1000, 2000))
    (repo / "layout").mkdir(exist_ok=True)
    layout.write(str(repo / "layout" / "top.gds"))
    return str(repo / "layout" / "top.gds")


def _stream_inventory(repo: Path, description: str | None = ..., checks=None) -> str:
    inventory: dict = {
        "schema_version": 1,
        "kind": "t1-attestation-inventory",
        "t1_item": 2,
        "checks": checks if checks is not None else [_CHECK],
    }
    if description is not ...:
        inventory["description"] = description
    path = repo / "evidence" / "item2-inv.json"
    path.write_text(json.dumps(inventory))
    return str(path)


def _grade(repo: Path, envelope: dict, item: int) -> dict:
    """Cite ``envelope`` for ``item`` through ``klt signoff --manifest``
    grading and return the tier-report item."""
    env_path = repo / "evidence" / f"item{item}.json"
    env_path.write_text(render_envelope(envelope))
    manifest = {
        "block": "demo",
        "kind": "analog",
        "evidence": {
            str(item): {
                "file": str(env_path),
                "content_hash": envelope["provenance"]["input"]["content_hash"],
            }
        },
    }
    report = build_tier_report(manifest)
    return next(i for i in report["items"] if i["id"] == item)


@pytest.mark.parametrize("item", [1, 8, 9, 10])
def test_description_is_carried_verbatim(tmp_path, item):
    """AC 1: an inventory declaring a non-empty `description` produces an
    envelope whose `attestation.description` is that string verbatim, for
    every inventory-attested item."""
    repo = _repo(tmp_path)
    envelope = _attest(repo, item, _DISCLOSURE)
    assert envelope["attestation"]["description"] == _DISCLOSURE


def test_item2_description_rides_the_optional_inventory(tmp_path):
    """AC 1 (item 2): the disclosure is carried via `--inventory`."""
    repo = _repo(tmp_path)
    stream = _stream(repo)
    inventory = _stream_inventory(repo, _DISCLOSURE)

    envelope = build_stream_attestation(stream, inventory_path=inventory)

    assert envelope["attestation"]["description"] == _DISCLOSURE


@pytest.mark.parametrize("description", [None, "", "   \t "])
def test_description_is_absent_not_null_or_empty(tmp_path, description):
    """AC 2: absent (not `null`, not `""`) when the inventory declares no
    `description`, an empty or a whitespace-only one."""
    repo = _repo(tmp_path)
    envelope = _attest(repo, 10, description)
    assert "description" not in envelope["attestation"]


def test_item2_without_inventory_has_no_description(tmp_path):
    """AC 2 (item 2): the plainest invocation -- no `--inventory` -- has
    nowhere to put a disclosure."""
    repo = _repo(tmp_path)
    stream = _stream(repo)

    envelope = build_stream_attestation(stream)

    assert "description" not in envelope["attestation"]


def test_descriptionless_envelope_keeps_its_pre_5573_shape(tmp_path):
    """AC 2/8: an inventory without the field renders byte-identically to
    before -- key order preserved, `summary` template unchanged, no new
    key anywhere in the envelope."""
    repo = _repo(tmp_path)
    envelope = _attest(repo, 10)
    attestation = envelope["attestation"]
    assert list(attestation) == _PRE_5573_ATTESTATION_KEYS
    checks = attestation["check_count"]
    assert envelope["summary"] == (
        f"T1 item 10 (Repo hygiene): {checks}/{checks} "
        "attestation checks passed (klt signoff --attest)"
    )
    assert "description" not in render_envelope(envelope)

    stream = _stream(repo)
    with_inventory = build_stream_attestation(
        stream, inventory_path=_stream_inventory(repo)
    )
    assert list(with_inventory["attestation"]) == _PRE_5573_STREAM_ATTESTATION_KEYS


def test_manifest_echoes_description_into_the_artifact_binding(tmp_path):
    """AC 3: a cited attestation that carries one is echoed into
    `citation.artifact_binding.attestation.description`; the key is omitted
    otherwise."""
    repo = _repo(tmp_path)
    carried = _grade(repo, _attest(repo, 10, _DISCLOSURE), 10)
    binding = carried["citation"]["artifact_binding"]["attestation"]
    assert binding["description"] == _DISCLOSURE

    omitted = _grade(repo, _attest(repo, 10), 10)
    binding = omitted["citation"]["artifact_binding"]["attestation"]
    assert "description" not in binding


def test_item8_manifest_echoes_description_into_the_citation_attestation(tmp_path):
    """Issue #6111: item 8 carries no `artifact_binding`, so its met citation
    echoes the disclosure at `citation.attestation.description` instead."""
    repo = _repo(tmp_path)
    item = _grade(repo, _attest_item8(repo, _DISCLOSURE), 8)
    assert item["status"] == "met"
    assert "artifact_binding" not in item["citation"]
    disclosure = item["citation"]["attestation"]
    assert disclosure["tool_produced"] is True
    assert disclosure["description"] == _DISCLOSURE


@pytest.mark.parametrize("description", [..., None, "", "   \t "])
def test_item8_without_a_usable_description_keeps_its_four_key_block(
    tmp_path, description
):
    """A missing, null, empty or whitespace-only description omits the key, so
    the block keeps its pre-#6111 four-key shape."""
    repo = _repo(tmp_path)
    item = _grade(repo, _attest_item8(repo, description), 8)
    assert list(item["citation"]["attestation"]) == [
        "tool_produced",
        "producer",
        "check_count",
        "passed_count",
    ]


def test_item8_non_string_description_is_omitted(tmp_path):
    """A hand-edited envelope whose `description` is not a string is not
    echoed (and does not crash the grader)."""
    repo = _repo(tmp_path)
    envelope = _attest_item8(repo, _DISCLOSURE)
    envelope["attestation"]["description"] = {"scope": "not a string"}
    item = _grade(repo, envelope, 8)
    assert item["status"] == "met"
    assert "description" not in item["citation"]["attestation"]


def test_item8_hand_written_block_never_echoes_a_description(tmp_path):
    """`tool_produced: false` stays exactly that, even if a hand-written
    envelope's `attestation` block names a description but no producer."""
    repo = _repo(tmp_path)
    envelope = _attest_item8(repo, _DISCLOSURE)
    del envelope["attestation"]["producer"]
    item = _grade(repo, envelope, 8)
    assert item["status"] == "met"
    assert item["citation"]["attestation"] == {"tool_produced": False}


def test_description_is_never_graded(tmp_path):
    """AC 4: two citations differing only in the inventory's `description`
    (each manifest pinned to its own re-attested envelope, graded while its
    own inventory is still on disk) are both met and agree on every
    verdict the report carries."""
    repo = _repo(tmp_path)
    graded = [
        _grade(repo, _attest(repo, 10, disclosure), 10)
        for disclosure in (_DISCLOSURE, None)
    ]
    with_description, without_description = graded
    assert with_description["status"] == "met"
    assert without_description["status"] == "met"
    assert with_description["tier"] == without_description["tier"]


def test_check_matches_an_unchanged_inventory(tmp_path):
    """AC 5: `--check` reports `status: "match"` when the inventory
    (description included) is unchanged."""
    repo = _repo(tmp_path)
    envelope = _attest(repo, 10, _DISCLOSURE)
    committed = repo / "evidence" / "item10.json"
    committed.write_text(render_envelope(envelope))

    result = check_attestation(10, str(repo / "attest" / "item10.json"), str(committed))

    assert result["status"] == "match"


def test_check_names_the_description_path_on_description_only_drift(tmp_path):
    """AC 5: editing only the `description` text is drift that names the
    `attestation.description` path (beside the input-hash drift proving the
    text is inventory-pinned); `description` is not volatile."""
    repo = _repo(tmp_path)
    envelope = _attest(repo, 10, "scope A")
    committed = repo / "evidence" / "item10.json"
    committed.write_text(render_envelope(envelope))
    (repo / "attest" / "item10.json").write_text(json.dumps(_inventory(10, "scope B")))

    result = check_attestation(10, str(repo / "attest" / "item10.json"), str(committed))

    assert result["status"] == "drifted"
    named = {entry["field"]: entry for entry in result["drift"]}
    assert named["attestation.description"]["committed"] == "scope A"
    assert named["attestation.description"]["fresh"] == "scope B"
    assert ("attestation", "description") not in VOLATILE_ATTESTATION_PATHS


def test_item2_check_names_the_description_path(tmp_path):
    """AC 5 (item 2): the stream producer's description drift is named too."""
    repo = _repo(tmp_path)
    stream = _stream(repo)
    inventory = _stream_inventory(repo, "plan A")
    committed = repo / "evidence" / "item2.json"
    committed.write_text(
        render_envelope(build_stream_attestation(stream, inventory_path=inventory))
    )

    _stream_inventory(repo, "plan B")
    result = check_stream_attestation(
        stream, str(committed), inventory_path=str(repo / "evidence" / "item2-inv.json")
    )

    assert result["status"] == "drifted"
    assert "attestation.description" in {entry["field"] for entry in result["drift"]}


def test_attest_text_output_surfaces_the_description(tmp_path):
    """AC 6: the `--attest` text output shows the disclosure without
    opening the JSON, and shows nothing for a description-less result."""
    repo = _repo(tmp_path)
    with_description = _attest(repo, 10, _DISCLOSURE)
    assert f"description: {_DISCLOSURE}" in input_line(with_description, 2)

    without_description = _attest(repo, 10)
    assert "description:" not in input_line(without_description, 2)


def test_long_and_unicode_descriptions_round_trip_untouched(tmp_path):
    """Edge cases: a very long description is not truncated and non-ASCII
    text with embedded newlines survives the round trip."""
    repo = _repo(tmp_path)
    disclosure = "ümläut ✅ with\nnewline\tand " + "x" * 5000
    envelope = _attest(repo, 10, disclosure)
    assert envelope["attestation"]["description"] == disclosure
    reloaded = json.loads(render_envelope(envelope))
    assert reloaded["attestation"]["description"] == disclosure


def test_failing_item2_inventory_still_carries_the_description(tmp_path):
    """Edge case: the disclosure still rides a `status: "fail"` envelope --
    it qualifies the evidence, it is not part of the verdict."""
    repo = _repo(tmp_path)
    stream = _stream(repo, empty=True)
    inventory = _stream_inventory(repo, _DISCLOSURE, checks=[_CHECK_FAIL])

    envelope = build_stream_attestation(stream, inventory_path=inventory)

    assert envelope["status"] == "fail"
    assert envelope["attestation"]["description"] == _DISCLOSURE


#: A valid disclosure no Windows-1252 stdout can encode, and how a text
#: render must show it there (issue #5573 review: the passing attestation
#: used to die with UnicodeEncodeError *after* writing the envelope).
_JA = "レビュー待ち"
_JA_ESCAPED = "\\u30ec\\u30d3\\u30e5\\u30fc\\u5f85\\u3061"


def _klt(repo: Path, *args: str, encoding: str) -> subprocess.CompletedProcess:
    """``klt signoff ARGS`` run from ``repo`` under a stdout of ``encoding``."""
    return subprocess.run(
        [sys.executable, "-m", "klayout_tools.cli", "signoff", *args],
        cwd=repo,
        env={**os.environ, "PYTHONIOENCODING": encoding},
        capture_output=True,
        encoding=encoding,
        check=False,
    )


def _escaped(text: str) -> str:
    """``text`` as a Windows-1252 stdout shows it: unencodable -> ``\\uXXXX``."""
    return text.encode("cp1252", "backslashreplace").decode("cp1252")


def _attest_argv(repo: Path, item: int, description: str) -> list[str]:
    """``--attest`` argv for the inventory producer (10) or the stream one (2),
    writing the inventory (at a fixed path, so a rewrite is a drift) first."""
    if item == 10:
        inventory = repo / "attest" / "item10.json"
        inventory.write_text(json.dumps(_inventory(10, description)))
        return ["--attest", "10", "--inventory", str(inventory)]
    inventory_path = _stream_inventory(repo, description)
    return ["--attest", "2", "--input", _stream(repo), "--inventory", inventory_path]


@pytest.mark.parametrize("item", [10, 2])
def test_text_output_is_printable_on_a_legacy_stdout(tmp_path, item):
    """A passing attestation exits 0 with the *complete* text report under a
    Windows-1252 stdout: the disclosure prints as ``\\uXXXX`` escapes (never a
    UnicodeEncodeError mid-report), and the written envelope keeps the
    original string. Both producers (inventory item 10, stream item 2)."""
    repo = _repo(tmp_path)
    argv = [*_attest_argv(repo, item, _JA), "-o", "attest/env.json"]
    reference = _klt(repo, *argv, encoding="utf-8")
    assert reference.returncode == 0, reference.stderr
    assert f"description: {_JA}\n" in reference.stdout
    (repo / "attest" / "env.json").unlink()  # the legacy run must write its own

    legacy = _klt(repo, *argv, encoding="cp1252")

    assert legacy.returncode == 0, legacy.stderr
    assert "Traceback" not in legacy.stderr
    assert f"description: {_JA_ESCAPED}\n" in legacy.stdout
    # Complete: every line the UTF-8 run prints, in order, only escaped --
    # but for the audit line's own seconds, which no two runs share (issue
    # #6868's CI flake): mask that one value on each side before comparing.
    assert _audit_seconds_masked(legacy.stdout) == _audit_seconds_masked(
        _escaped(reference.stdout)
    )
    envelope = json.loads((repo / "attest" / "env.json").read_text(encoding="utf-8"))
    assert envelope["status"] == "pass"
    assert envelope["attestation"]["description"] == _JA


def test_check_drift_text_is_printable_on_a_legacy_stdout(tmp_path):
    """The ``--check`` drift report prints the old and new ``description``
    (``{value!r}``, which keeps non-ASCII): a description-only drift must not
    crash under Windows-1252 either, and keeps its drifted exit status."""
    repo = _repo(tmp_path)
    committed = repo / "attest" / "env.json"
    argv = _attest_argv(repo, 10, "planning-level only")
    assert _klt(repo, *argv, "-o", str(committed), encoding="utf-8").returncode == 0
    argv = _attest_argv(repo, 10, _JA)  # same inventory path, new disclosure
    check = [*argv, "--check", str(committed)]
    reference = _klt(repo, *check, encoding="utf-8")
    assert f"fresh:     {_JA!r}" in reference.stdout

    legacy = _klt(repo, *check, encoding="cp1252")

    assert "Traceback" not in legacy.stderr
    assert legacy.returncode == reference.returncode != 1
    assert f"fresh:     '{_JA_ESCAPED}'" in legacy.stdout
    assert "attestation.description" in legacy.stdout
    assert legacy.stdout == _escaped(reference.stdout)
