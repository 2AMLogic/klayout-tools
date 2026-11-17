"""Issue #4984: ``klt lvs -o/--output`` and the ``{path, scope}`` ``reference``."""

import gzip
import json
import os

import pytest

from klayout_tools.cli import main
from klayout_tools.lvs import check_lvs_report, rerun_lvs_report, run_lvs

_SPICE = """
.subckt inv A Y VPWR VGND
M1 Y A VGND VGND nfet W=0.65U L=0.15U
M2 Y A VPWR VPWR pfet W=1.0U L=0.15U
.ends
"""


@pytest.fixture
def tmp_path(tmp_path):
    """Seed a `.git` marker so tmp_path is the repo root (`scope: "repo"`),
    as in tests/test_lvs.py."""
    (tmp_path / ".git").mkdir()
    return tmp_path


def _write(path, text):
    path.write_text(text)
    return str(path)


def _write_request(path, request):
    path.write_text(json.dumps(request))
    return str(path)


def _write_report(path, report):
    path.write_text(json.dumps(report))
    return str(path)


def _pair(tmp_path):
    pair = tmp_path / "pair"
    pair.mkdir()
    _write(pair / "layout.spice", _SPICE)
    _write(pair / "ref.spice", _SPICE)
    request = pair / "request.json"
    request.write_text(
        json.dumps(
            {
                "layout": {"netlist": "layout.spice", "top": "inv"},
                "reference": {"netlist": "ref.spice", "top": "inv"},
            }
        )
    )
    report = pair / "lvs.json"
    report.write_text(json.dumps(run_lvs(str(request))))
    return pair, report


def test_check_lvs_report_reference_resolves_from_any_cwd(tmp_path, monkeypatch):
    """Issue #4984: a v3 report's `reference` is `{path, scope}`, so
    `--check` from an unrelated cwd matches (no `input_not_found`)."""
    _pair(tmp_path)

    monkeypatch.chdir(tmp_path)
    result = check_lvs_report(os.path.join("pair", "lvs.json"))

    assert result["status"] == "match"
    assert not any("input_not_found" in check for check in result["checks"])


def test_lvs_output_flag_writes_envelope_and_rejects_check(tmp_path, capsys):
    """Issue #4984: `-o` mirrors stdout JSON; incompatible with --check;
    unwritable path is an error envelope."""
    layout_path, reference_path = (
        _write(tmp_path / "layout.spice", _SPICE),
        _write(tmp_path / "ref.spice", _SPICE),
    )
    request = json.dumps(
        {
            "layout": {"netlist": layout_path, "top": "inv"},
            "reference": {"netlist": reference_path, "top": "inv"},
        }
    )
    out = tmp_path / "report.json"
    assert main(["lvs", request, "-o", str(out), "--format", "json"]) == 0
    stdout = capsys.readouterr().out
    assert json.loads(out.read_text()) == json.loads(stdout)

    gz = tmp_path / "report.json.gz"
    assert main(["lvs", request, "-o", str(gz), "--format", "json"]) == 0
    capsys.readouterr()
    assert json.loads(gzip.open(gz, "rt").read())["status"] == "match"

    assert main(["lvs", "--check", str(out), "-o", str(gz), "--format", "json"]) == 1
    capsys.readouterr()

    bad = tmp_path / "no_such_dir" / "r.json"
    assert main(["lvs", request, "-o", str(bad), "--format", "json"]) == 1
    capsys.readouterr()


# ---------------------------------------------------------------------------
# Explicit --reference alongside --check / --rerun (issue #5845)
# ---------------------------------------------------------------------------


def _external_reference_report(tmp_path_factory):
    """A committed report inside a repo whose layout is repo-scoped (so it
    resolves by itself) but whose reference is outside any repo (`scope:
    "external"`), plus the reference netlist's path. Only the reference
    side needs rescuing."""
    repo = tmp_path_factory.mktemp("lvs-repo")
    (repo / ".git").mkdir()
    outside = tmp_path_factory.mktemp("outside-any-repo")
    layout_path = _write(repo / "layout.spice", _SPICE)
    reference_path = _write(outside / "ref.spice", _SPICE)
    report_path = repo / "lvs.json"
    report = run_lvs(
        _write_request(
            repo / "request.json",
            {
                "layout": {"netlist": layout_path, "top": "inv"},
                "reference": {"netlist": reference_path, "top": "inv"},
            },
        )
    )
    assert report["reference"]["scope"] == "external"
    assert report["layout"]["scope"] == "repo"
    _write_report(report_path, report)
    return repo, reference_path, report_path


def test_check_lvs_report_explicit_reference_verifies_an_external_reference(
    tmp_path_factory,
):
    _, reference_path, report_path = _external_reference_report(tmp_path_factory)

    result = check_lvs_report(str(report_path), reference_path=reference_path)

    assert result["status"] == "match"


@pytest.mark.parametrize("scope", ["external", "absent"])
def test_check_lvs_report_explicit_reference_rescues_unresolvable_scopes(
    tmp_path_factory, scope
):
    """Both no-path scopes (`external`, `absent`) are unverifiable without
    the override and rescued by it (issue #5845)."""
    _, reference_path, report_path = _external_reference_report(tmp_path_factory)
    report = json.loads(report_path.read_text())
    report["reference"] = {"path": None, "scope": scope}
    report_path.write_text(json.dumps(report))

    without = check_lvs_report(str(report_path))
    assert without["status"] == "drifted"
    fields = {check["field"]: check for check in without["checks"]}
    assert fields["environment.reference_sha256"]["actual"] is None

    with_override = check_lvs_report(str(report_path), reference_path=reference_path)
    assert with_override["status"] == "match"


def test_lvs_explicit_reference_overrides_a_recorded_reference(tmp_path_factory):
    """Precedence rule (always override): a disagreeing explicit reference is
    reported `drifted`, never silently accepted."""
    _, reference_path, report_path = _external_reference_report(tmp_path_factory)
    other = _write(tmp_path_factory.mktemp("other") / "other.spice", _SPICE + "* x\n")

    result = check_lvs_report(str(report_path), reference_path=other)

    assert result["status"] == "drifted"


def test_rerun_lvs_report_explicit_reference_reruns_an_external_reference(
    tmp_path_factory,
):
    _, reference_path, report_path = _external_reference_report(tmp_path_factory)

    result = rerun_lvs_report(str(report_path), reference_path=reference_path)

    assert result["status"] == "match"


def test_lvs_explicit_reference_wins_over_a_pre_4984_bare_string(
    tmp_path_factory,
):
    """A report whose `reference` is a bare string (pre-#4984 echo) still
    honours the override."""
    _, reference_path, report_path = _external_reference_report(tmp_path_factory)
    report = json.loads(report_path.read_text())
    report["reference"] = reference_path
    report_path.write_text(json.dumps(report))
    other = _write(tmp_path_factory.mktemp("other") / "other.spice", _SPICE + "* y\n")

    result = check_lvs_report(str(report_path), reference_path=other)

    assert result["status"] == "drifted"
    fields = {check["field"]: check for check in result["checks"]}
    assert fields["environment.reference_sha256"]["actual"] is not None


def test_cli_lvs_check_with_explicit_reference_on_external_report(
    tmp_path_factory, capsys
):
    _, reference_path, report_path = _external_reference_report(tmp_path_factory)
    capsys.readouterr()

    code = main(
        [
            "lvs",
            "--check",
            str(report_path),
            "--reference",
            reference_path,
            "--format",
            "json",
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "match"

    # A different file drifts (exit 3), naming the reference check.
    other = _write(tmp_path_factory.mktemp("other") / "other.spice", _SPICE + "* z\n")
    assert (
        main(
            [
                "lvs",
                "--check",
                str(report_path),
                "--reference",
                other,
                "--format",
                "json",
            ]
        )
        == 3
    )
    result = json.loads(capsys.readouterr().out)
    checks = {check["field"]: check for check in result["checks"]}
    assert checks["environment.reference_sha256"]["match"] is False


def test_cli_lvs_check_rerun_with_explicit_reference(tmp_path_factory, capsys):
    _, reference_path, report_path = _external_reference_report(tmp_path_factory)
    capsys.readouterr()

    code = main(
        [
            "lvs",
            "--check",
            str(report_path),
            "--rerun",
            "--reference",
            reference_path,
            "--format",
            "json",
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "match"


def test_cli_lvs_layout_and_reference_compose(tmp_path_factory, capsys):
    """Both overrides at once replace both recorded inputs."""
    outside = tmp_path_factory.mktemp("outside-any-repo")
    layout_path = _write(outside / "layout.spice", _SPICE)
    reference_path = _write(outside / "ref.spice", _SPICE)
    report_path = outside / "lvs.json"
    report = run_lvs(
        _write_request(
            outside / "request.json",
            {
                "layout": {"netlist": layout_path, "top": "inv"},
                "reference": {"netlist": reference_path, "top": "inv"},
            },
        )
    )
    assert report["layout"]["scope"] == "external"
    _write_report(report_path, report)
    capsys.readouterr()

    code = main(
        [
            "lvs",
            "--check",
            str(report_path),
            "--layout",
            layout_path,
            "--reference",
            reference_path,
            "--format",
            "json",
        ]
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out)["status"] == "match"


def test_cli_lvs_reference_without_check_is_rejected(tmp_path, capsys):
    assert main(["lvs", "{}", "--reference", "x.spice", "--format", "json"]) == 1
    assert (
        "--reference requires --check"
        in json.loads(capsys.readouterr().err)["error"]["message"]
    )


def test_cli_lvs_reference_naming_a_missing_file_drifts_cleanly(
    tmp_path_factory, capsys
):
    """A nonexistent override is a clean `drifted` (with the
    `input_not_found` diagnostic), never a traceback."""
    _, reference_path, report_path = _external_reference_report(tmp_path_factory)
    capsys.readouterr()

    code = main(
        [
            "lvs",
            "--check",
            str(report_path),
            "--reference",
            str(tmp_path_factory.mktemp("gone") / "nope.spice"),
            "--format",
            "json",
        ]
    )
    assert code == 3
    result = json.loads(capsys.readouterr().out)
    checks = {check["field"]: check for check in result["checks"]}
    assert "input_not_found" in checks["environment.reference_sha256"]


def test_check_input_not_found_names_the_resolved_repo_root(tmp_path):
    """Issue #5845: a repo-scoped echo that resolves to a missing file names
    the repo root `--check` anchored on (additive key)."""
    repo = tmp_path / "repo"
    (repo / "nested").mkdir(parents=True)
    (repo / ".git").mkdir()
    report = repo / "nested" / "lvs.json"
    committed = {
        "schema_version": 3,
        "layout": {"path": "layout.spice", "scope": "repo"},
        "reference": {"path": "ref.spice", "scope": "repo"},
        "environment": {
            "layout_sha256": "sha256:aaa",
            "reference_sha256": "sha256:bbb",
        },
    }
    report.write_text(json.dumps(committed))
    (repo / "layout.spice").write_text(_SPICE)  # layout resolves; ref does not

    result = check_lvs_report(str(report))
    checks = {check["field"]: check for check in result["checks"]}
    missing = checks["environment.reference_sha256"]["input_not_found"]

    assert missing["resolved_repo_root"] == str(repo)
    assert missing["resolved"] == str(repo / "ref.spice")
    # The layout check (which resolved) carries no diagnostic at all.
    assert "input_not_found" not in checks["environment.layout_sha256"]


def test_check_text_renders_the_resolved_repo_root(tmp_path, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    report = repo / "lvs.json"
    committed = {
        "schema_version": 3,
        "layout": {"path": "layout.spice", "scope": "repo"},
        "reference": {"path": "ref.spice", "scope": "repo"},
        "environment": {
            "layout_sha256": "sha256:aaa",
            "reference_sha256": "sha256:bbb",
        },
    }
    report.write_text(json.dumps(committed))
    (repo / "layout.spice").write_text(_SPICE)
    capsys.readouterr()

    assert main(["lvs", "--check", str(report), "--format", "text"]) == 3
    out = capsys.readouterr().out
    assert f"repo root: {repo}" in out
