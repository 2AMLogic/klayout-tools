"""``klt drc --check --rerun`` PDK resolution over the curated engine
(issue #6986).

The rerun used to rebuild its request with no PDK selection at all, so a
report produced *with* ``--pdk``/``--pdk-root`` always drifted
``provenance.pdk`` with ``fresh: null`` -- the sole drift entry even when
every verdict-bearing field agreed, and regardless of the flags or
``$PDK``/``$PDK_ROOT`` the rerun invocation itself carried. These tests pin
the curated-engine half of the fix (explicit flags, environment-plus-
recorded-name fallback, other-variant drift, unresolvable error, null-PDK
invariance, cheap-mode hash-only); the native-engine half lives in
``tests/test_drc_rerun_klayout_provenance.py``.

Kept out of ``tests/test_drc.py``: that file sits at its per-file size
ceiling.
"""

from __future__ import annotations

import json
from pathlib import Path

import klayout.db as kdb
import pytest

from klayout_tools.cli import main
from klayout_tools.drc import DrcError, check_drc_report, rerun_drc_report, run_drc
from klayout_tools.drc_rerun_replay import _rerun_pdk_selection


@pytest.fixture
def tmp_path(tmp_path: Path) -> Path:
    """Shadows pytest's own built-in `tmp_path` fixture (the same shadow
    `tests/test_drc.py` casts, issue #2659): seeds a `.git` marker so
    `tmp_path` resolves as this run's own repo root and the report's
    `file` envelope records a locatable repo-scoped path `--check`/
    `--rerun` can verify -- without it the input hash check has nothing
    to re-hash."""
    (tmp_path / ".git").mkdir()
    return tmp_path


def _make_clean_layout() -> kdb.Layout:
    """A layout with a poly shape wide enough to pass every deck rule --
    the same fixture ``tests/test_drc.py``'s helper of that name builds."""
    layout = kdb.Layout()
    top = layout.create_cell("TOP")
    poly = layout.layer(66, 20)
    layout.set_info(poly, kdb.LayerInfo(66, 20, "poly.drawing"))
    # width 200 dbu >= 150 dbu threshold.
    top.shapes(poly).insert(kdb.Box(0, 0, 200, 2000))
    return layout


def _make_pdk_install(tmp_path: Path, variant: str) -> str:
    """Minimal on-disk PDK install `find_pdk` can resolve -- mirrors
    `tests/test_extract.py`'s helper of the same name/shape."""
    root = tmp_path / "pdk_install"
    (root / variant / "libs.tech").mkdir(parents=True, exist_ok=True)
    return str(root)


def _write_pdk_resolved_report(tmp_path: Path, variant: str = "sky130A") -> Path:
    """A committed curated report produced with an explicit PDK selection,
    against the install root the caller can re-select."""
    path = tmp_path / "clean.gds"
    _make_clean_layout().write(str(path))
    root = _make_pdk_install(tmp_path, variant)
    report_path = tmp_path / "clean.drc.json"
    report_path.write_text(
        json.dumps(run_drc(str(path), "sky130", pdk_variant=variant, pdk_root=root))
    )
    return report_path


def test_rerun_resolves_pdk_from_explicit_flags(tmp_path):
    """A report produced with `--pdk`/`--pdk-root` re-verifies in full mode
    when the rerun passes the same explicit flags (issue #6986) -- the exact
    repro of the issue, previously always
    `provenance.pdk` `{committed: {...}, fresh: null}` drift."""
    report_path = _write_pdk_resolved_report(tmp_path)
    root = str(tmp_path / "pdk_install")

    result = rerun_drc_report(str(report_path), pdk_variant="sky130A", pdk_root=root)

    assert result["status"] == "match"
    assert result["drift"] == []
    assert result["fresh"]["provenance"]["pdk"]["name"] == "sky130A"


def test_rerun_resolves_pdk_from_the_environment_via_the_recorded_name(
    tmp_path, monkeypatch
):
    """The rerun needs no explicit flags when the install is discoverable
    through the environment: `$PDK_ROOT` plus the variant name the committed
    report itself records (issue #6986's recorded-name fallback). The
    producing run also resolves through `$PDK_ROOT` (variant flag, no root
    flag), so both sides name the same discovery source."""
    monkeypatch.delenv("PDK", raising=False)
    path = tmp_path / "clean.gds"
    _make_clean_layout().write(str(path))
    root = _make_pdk_install(tmp_path, "sky130A")
    monkeypatch.setenv("PDK_ROOT", root)
    report_path = tmp_path / "clean.drc.json"
    report_path.write_text(
        json.dumps(run_drc(str(path), "sky130", pdk_variant="sky130A"))
    )

    result = rerun_drc_report(str(report_path))

    assert result["status"] == "match"
    assert result["fresh"]["provenance"]["pdk"]["name"] == "sky130A"


def test_rerun_pdk_prefers_the_explicit_variant_and_drifts_on_the_other(tmp_path):
    """With two installed variants of the family, explicitly selecting the
    other one on the rerun drifts and names the changed PDK (issue #6986)."""
    report_path = _write_pdk_resolved_report(tmp_path)
    _make_pdk_install(tmp_path, "sky130B")
    root = str(tmp_path / "pdk_install")

    result = rerun_drc_report(str(report_path), pdk_variant="sky130B", pdk_root=root)

    assert result["status"] == "drifted"
    by_field = {entry["field"]: entry for entry in result["drift"]}
    assert by_field["provenance.pdk.name"]["committed"] == "sky130A"
    assert by_field["provenance.pdk.name"]["fresh"] == "sky130B"


def test_rerun_unresolvable_explicit_pdk_is_a_drc_error(tmp_path, monkeypatch):
    """An explicit `--pdk` that resolves to nothing is the normal DRC error
    on the rerun too (issue #6986), never a silent `fresh: null`."""
    monkeypatch.delenv("PDK", raising=False)
    report_path = _write_pdk_resolved_report(tmp_path)

    with pytest.raises(DrcError):
        rerun_drc_report(
            str(report_path),
            pdk_variant="no-such-variant",
            pdk_root=str(tmp_path / "empty-pdk-root"),
        )


def test_rerun_of_a_null_pdk_report_stays_null_even_with_pdk_set(tmp_path, monkeypatch):
    """A report produced without PDK selection re-verifies with no selection
    -- even on a host with `$PDK`/`$PDK_ROOT` set to a real install: the
    rerun resolves a PDK only when flags are given or the report itself
    records one (issue #6986, mirrors `run_drc`'s own null-by-default
    guard)."""
    path = tmp_path / "clean.gds"
    _make_clean_layout().write(str(path))
    root = _make_pdk_install(tmp_path, "sky130A")
    monkeypatch.setenv("PDK", "sky130A")
    monkeypatch.setenv("PDK_ROOT", root)
    report_path = tmp_path / "clean.drc.json"
    report_path.write_text(json.dumps(run_drc(str(path), "sky130")))

    result = rerun_drc_report(str(report_path))

    assert result["status"] == "match"
    assert result["fresh"]["provenance"]["pdk"] is None


def _write_env_resolved_report(tmp_path: Path, monkeypatch) -> tuple[Path, str]:
    """A committed curated report for `sky130A`, produced by resolving the
    variant through `$PDK_ROOT` (no root flag) with both `sky130A` and
    `sky130B` installed and `$PDK` unset. Returns the report and the root."""
    path = tmp_path / "clean.gds"
    _make_clean_layout().write(str(path))
    root = _make_pdk_install(tmp_path, "sky130A")
    _make_pdk_install(tmp_path, "sky130B")
    monkeypatch.setenv("PDK_ROOT", root)
    monkeypatch.delenv("PDK", raising=False)
    report_path = tmp_path / "clean.drc.json"
    report_path.write_text(
        json.dumps(run_drc(str(path), "sky130", pdk_variant="sky130A"))
    )
    return report_path, root


@pytest.mark.parametrize("env_pdk", ["sky130B", "gf180mcuD"])
def test_rerun_recorded_name_outranks_a_conflicting_pdk_env(
    tmp_path, monkeypatch, env_pdk
):
    """`$PDK` never outranks the variant the committed report recorded
    (issue #6986): a fresh run's `provenance.pdk` is driven only by
    `--pdk`/`--pdk-root` (`$PDK` alone leaves it `null`), so a `$PDK` tier
    would reproduce neither the committed run nor a fresh one -- a same-family
    `sky130B` would drift `provenance.pdk.name` and a cross-family
    `gf180mcuD` would fail with a family-mismatch `DrcError`, both on a
    report that is perfectly reproducible."""
    report_path, _root = _write_env_resolved_report(tmp_path, monkeypatch)
    monkeypatch.setenv("PDK", env_pdk)

    result = rerun_drc_report(str(report_path))

    assert result["status"] == "match"
    assert result["drift"] == []
    assert result["fresh"]["provenance"]["pdk"]["name"] == "sky130A"


def test_rerun_explicit_variant_outranks_a_conflicting_pdk_env(tmp_path, monkeypatch):
    """The explicit `--pdk` still wins over `$PDK` and the recorded name
    (issue #6986's CLI-over-environment precedence): the other installed
    variant lands in the fresh provenance and drifts the PDK name."""
    report_path, _root = _write_env_resolved_report(tmp_path, monkeypatch)
    monkeypatch.setenv("PDK", "sky130A")

    result = rerun_drc_report(str(report_path), pdk_variant="sky130B")

    assert result["status"] == "drifted"
    by_field = {entry["field"]: entry for entry in result["drift"]}
    assert by_field["provenance.pdk.name"]["fresh"] == "sky130B"


@pytest.mark.parametrize("malformed", [None, "sky130A", ["pdk"], 5])
def test_rerun_pdk_selection_tolerates_a_malformed_provenance(malformed):
    """`--check`/`--rerun` consume user-supplied report files, so a
    non-dict `provenance` (or `provenance.pdk`) is reachable input: it reads
    as "records no PDK", never an `AttributeError` traceback."""
    assert _rerun_pdk_selection({"provenance": malformed}, None, None) == (None, None)
    assert _rerun_pdk_selection({"provenance": {"pdk": malformed}}, None, None) == (
        None,
        None,
    )


@pytest.mark.parametrize("malformed", [None, "sky130A", ["pdk"]])
def test_cli_rerun_of_a_malformed_provenance_report_emits_an_envelope(
    tmp_path, capsys, malformed
):
    """The CLI face of the same hole: a report whose `provenance` is not an
    object is a JSON verdict (`drifted`, exit 3) or the error envelope --
    never a bare traceback under `--format json`."""
    path = tmp_path / "clean.gds"
    _make_clean_layout().write(str(path))
    report = run_drc(str(path), "sky130")
    report["provenance"] = malformed
    report_path = tmp_path / "bad.drc.json"
    report_path.write_text(json.dumps(report))
    capsys.readouterr()

    rc = main(["drc", "--check", str(report_path), "--rerun", "--format", "json"])

    captured = capsys.readouterr()
    assert rc in (1, 3)
    json.loads(captured.out if rc == 3 else captured.err)


def test_cheap_check_of_a_pdk_resolved_report_stays_hash_only(tmp_path):
    """Cheap mode never resolves the PDK (issue #6986): a PDK-resolved
    report verifies on its input/deck hashes alone."""
    report_path = _write_pdk_resolved_report(tmp_path)

    result = check_drc_report(str(report_path))

    assert result["mode"] == "check"
    assert result["status"] == "match"


def test_cli_rerun_round_trips_a_pdk_resolved_report(tmp_path, capsys):
    """CLI-level issue #6986 repro: produce with `--pdk`/`--pdk-root`, then
    full-mode re-verify with the same flags -> exit 0 `match`; with the
    other installed variant -> exit 3 naming `provenance.pdk.name`; with an
    unresolvable variant -> the normal exit-1 error envelope."""
    path = tmp_path / "clean.gds"
    _make_clean_layout().write(str(path))
    root = _make_pdk_install(tmp_path, "sky130A")
    _make_pdk_install(tmp_path, "sky130B")
    capsys.readouterr()

    assert (
        main(
            [
                "drc",
                str(path),
                "--deck",
                "sky130",
                "--pdk",
                "sky130A",
                "--pdk-root",
                root,
                "--format",
                "json",
            ]
        )
        == 0
    )
    report_path = tmp_path / "report.drc.json"
    report_path.write_text(capsys.readouterr().out)

    assert (
        main(
            [
                "drc",
                "--check",
                str(report_path),
                "--rerun",
                "--pdk",
                "sky130A",
                "--pdk-root",
                root,
                "--format",
                "json",
            ]
        )
        == 0
    )
    data = json.loads(capsys.readouterr().out)
    assert data["mode"] == "rerun"
    assert data["status"] == "match"
    assert data["fresh"]["provenance"]["pdk"]["name"] == "sky130A"

    assert (
        main(
            [
                "drc",
                "--check",
                str(report_path),
                "--rerun",
                "--pdk",
                "sky130B",
                "--pdk-root",
                root,
                "--format",
                "json",
            ]
        )
        == 3
    )
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "drifted"
    assert {entry["field"] for entry in data["drift"]} == {"provenance.pdk.name"}

    assert (
        main(
            [
                "drc",
                "--check",
                str(report_path),
                "--rerun",
                "--pdk",
                "no-such-variant",
                "--pdk-root",
                root,
                "--format",
                "json",
            ]
        )
        == 1
    )
    err = json.loads(capsys.readouterr().err)
    assert "PDK" in err["error"]["message"]
