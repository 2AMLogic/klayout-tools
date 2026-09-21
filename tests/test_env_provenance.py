"""`klt env-provenance`: the committable environment-provenance emitter and
the leak scan that keeps it honest (issue #1254).

The rule under test is the one `docs/design-evidence-tiers.md` states: an
evidence record carries repo-relative paths only, a stable pseudonymous host
id, and no login/author field. These tests assert the emitter *cannot* emit
the three identifier classes the disclosure audit found in ~3,937 committed
canary records -- the author's home-directory path, the dispatch host's name,
and the author's login.
"""

from __future__ import annotations

import json
import os
import re
import subprocess

import pytest

from klayout_tools import env_provenance as ep
from klayout_tools.cli import main

# --------------------------------------------------------------------------- #
# pseudonymous host id
# --------------------------------------------------------------------------- #

_HOST_ID_RE = re.compile(r"^host-[0-9a-f]{8}$")


def test_host_id_has_the_fleet_host_8hex_shape():
    assert _HOST_ID_RE.match(ep.opaque_host_id("robb-pro"))


def test_host_id_is_stable_for_the_same_hostname():
    assert ep.opaque_host_id("robb-pro") == ep.opaque_host_id("robb-pro")


def test_host_id_differs_between_hosts():
    assert ep.opaque_host_id("robb-pro") != ep.opaque_host_id("other-host")


def test_host_id_does_not_contain_the_hostname():
    assert "robb" not in ep.opaque_host_id("robb-pro")


def test_host_id_normalises_case_and_the_mdns_local_suffix():
    """macOS reports `gethostname()` inconsistently as `robb-pro` or
    `Robb-Pro.local` depending on the network; both are the same host and
    must pseudonymise to the same id, or the id is not stable."""
    canonical = ep.opaque_host_id("robb-pro")
    assert ep.opaque_host_id("Robb-Pro.local") == canonical
    assert ep.opaque_host_id("robb-pro.") == canonical


def test_host_id_salt_changes_the_id():
    assert ep.opaque_host_id("robb-pro", salt="a") != ep.opaque_host_id(
        "robb-pro", salt="b"
    )


def test_host_id_salt_env_var_overrides_the_default(monkeypatch):
    default = ep.opaque_host_id("robb-pro")
    monkeypatch.setenv(ep.HOST_ID_SALT_ENV_VAR, "project-specific-salt")
    assert ep.opaque_host_id("robb-pro") != default


def test_host_id_defaults_to_this_hosts_own_name(monkeypatch):
    monkeypatch.setattr(ep.socket, "gethostname", lambda: "robb-pro")
    assert ep.opaque_host_id() == ep.opaque_host_id("robb-pro")


def test_unresolvable_hostname_is_reported_as_unknown_not_fabricated(monkeypatch):
    def _boom():
        raise OSError("no hostname")

    monkeypatch.setattr(ep.socket, "gethostname", _boom)
    assert ep.opaque_host_id() == ep.UNKNOWN_HOST_ID


# --------------------------------------------------------------------------- #
# repo-relative path normalisation
# --------------------------------------------------------------------------- #


def _make_repo(tmp_path):
    root = tmp_path / "canary"
    (root / ".git").mkdir(parents=True)
    (root / "sim" / "bandgap").mkdir(parents=True)
    return root


def test_path_inside_the_repo_is_reported_repo_relative(tmp_path):
    root = _make_repo(tmp_path)
    target = root / "sim" / "bandgap" / "bandgap.spice"
    target.write_text("* netlist\n")

    entry = ep.repo_relative_path(str(target), repo_root=str(root))

    assert entry == {"path": "sim/bandgap/bandgap.spice", "scope": "repo"}


def test_repo_relative_path_uses_posix_separators(tmp_path):
    root = _make_repo(tmp_path)
    entry = ep.repo_relative_path(
        os.path.join(str(root), "sim", "bandgap"), repo_root=str(root)
    )
    assert entry["path"] == "sim/bandgap"
    assert "\\" not in entry["path"]


def test_the_repo_root_itself_is_dot(tmp_path):
    root = _make_repo(tmp_path)
    assert ep.repo_relative_path(str(root), repo_root=str(root)) == {
        "path": ".",
        "scope": "repo",
    }


def test_path_outside_the_repo_never_emits_the_absolute_path(tmp_path):
    """The exact leak the audit found: a PDK resolved under the author's home
    directory. Outside-the-repo paths are identified by their `scope`, never
    by their location."""
    root = _make_repo(tmp_path)
    outside = tmp_path / "home" / "someone" / ".volare" / "gf180mcuD"
    outside.mkdir(parents=True)

    entry = ep.repo_relative_path(str(outside), repo_root=str(root))

    assert entry == {"path": None, "scope": "external"}


def test_a_parent_of_the_repo_root_is_external_not_a_dotdot_path(tmp_path):
    root = _make_repo(tmp_path)
    entry = ep.repo_relative_path(str(tmp_path), repo_root=str(root))
    assert entry["scope"] == "external"
    assert entry["path"] is None


def test_a_sibling_with_a_shared_prefix_is_external(tmp_path):
    """`/x/canary-secrets` must not be treated as living inside `/x/canary`."""
    root = _make_repo(tmp_path)
    sibling = tmp_path / "canary-secrets"
    sibling.mkdir()
    assert ep.repo_relative_path(str(sibling), repo_root=str(root))["scope"] == (
        "external"
    )


def test_absent_path_is_distinct_from_an_external_one():
    assert ep.repo_relative_path(None) == {"path": None, "scope": "absent"}


def test_no_repo_root_makes_every_path_external(tmp_path):
    """Fail closed: with no repo to be relative *to*, a path is never
    emitted."""
    stray = tmp_path / "elsewhere.txt"
    stray.write_text("x")
    assert ep.repo_relative_path(str(stray), repo_root=None)["scope"] == "external"


def test_find_repo_root_walks_up_to_the_dot_git_marker(tmp_path):
    root = _make_repo(tmp_path)
    found = ep.find_repo_root(str(root / "sim" / "bandgap"))
    assert found is not None
    assert os.path.realpath(found) == os.path.realpath(str(root))


def test_find_repo_root_returns_none_outside_a_repo(tmp_path):
    assert ep.find_repo_root(str(tmp_path)) is None


# --------------------------------------------------------------------------- #
# the emitted record
# --------------------------------------------------------------------------- #


def test_environment_provenance_shape(tmp_path):
    root = _make_repo(tmp_path)
    report = ep.environment_provenance(
        repo_root=str(root),
        hostname="robb-pro",
        paths={"netlist": str(root / "sim" / "bandgap")},
    )

    assert report["schema_version"] == ep.SCHEMA_VERSION
    assert _HOST_ID_RE.match(report["host_id"])
    assert set(report["os"]) == {"system", "release", "machine"}
    assert report["paths"]["netlist"] == {"path": "sim/bandgap", "scope": "repo"}
    assert "klt_version" in report
    assert "klayout_version" in report
    assert "python_version" in report


def test_environment_provenance_carries_no_login_field(tmp_path):
    report = ep.environment_provenance(
        repo_root=str(_make_repo(tmp_path)), hostname="robb-pro"
    )
    for forbidden in ("user", "login", "username", "author", "hostname", "home"):
        assert forbidden not in report


def test_environment_provenance_leaks_no_identifier(tmp_path, monkeypatch):
    """The end-to-end assertion: nothing in the emitted JSON contains the
    hostname, the login, or the home directory of the machine that produced
    it."""
    home = tmp_path / "home" / "rwalters"
    home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USER", "rwalters")
    monkeypatch.setattr(ep.socket, "gethostname", lambda: "robb-pro")

    root = _make_repo(tmp_path)
    serialised = json.dumps(
        ep.environment_provenance(
            repo_root=str(root),
            paths={"pdk": str(home / ".volare" / "gf180mcuD")},
        )
    )

    assert "rwalters" not in serialised
    assert "robb-pro" not in serialised
    assert str(home) not in serialised
    assert ep.find_leaks(serialised) == []


def test_environment_provenance_refuses_to_emit_a_leaking_value(monkeypatch, tmp_path):
    """The self-check is the mechanism, not the convention: if any collected
    value ever carries an identifier, the emitter raises instead of writing a
    record that leaks it."""
    monkeypatch.setattr(ep.socket, "gethostname", lambda: "robb-pro")
    monkeypatch.setattr(ep.platform, "release", lambda: "26.6.1-robb-pro")

    with pytest.raises(ep.EnvironmentProvenanceError):
        ep.environment_provenance(repo_root=str(_make_repo(tmp_path)))


def test_render_text_lines_is_leak_free(tmp_path, monkeypatch):
    monkeypatch.setattr(ep.socket, "gethostname", lambda: "robb-pro")
    root = _make_repo(tmp_path)
    report = ep.environment_provenance(
        repo_root=str(root), paths={"pdk": str(tmp_path / "outside")}
    )

    lines = ep.render_text_lines(report)

    assert any(report["host_id"] in line for line in lines)
    assert ep.find_leaks("\n".join(lines)) == []
    assert "robb-pro" not in "\n".join(lines)


# --------------------------------------------------------------------------- #
# the leak scan
# --------------------------------------------------------------------------- #

_AUDITED_RECORD_LINE = (
    "  - PDK: volare `gf180mcuD`, open_pdks `c6d73a3` "
    "(/Users/rwalters/.volare/gf180mcuD, found via search_root:~/.volare)"
)


def test_find_leaks_flags_a_macos_home_path():
    leaks = ep.find_leaks(_AUDITED_RECORD_LINE)
    assert [leak["kind"] for leak in leaks] == ["home-path"]
    assert leaks[0]["match"].startswith("/Users/rwalters")
    assert leaks[0]["line"] == 1


def test_find_leaks_flags_a_linux_home_path():
    assert ep.find_leaks("run from /home/rwalters/canary/sim")


def test_find_leaks_flags_a_ci_runner_home_path():
    """`/home/runner/work/...` is not personally identifying but is still an
    absolute path -- the rule is repo-relative paths, not merely non-personal
    ones."""
    assert ep.find_leaks("/home/runner/work/canary/canary")


def test_find_leaks_flags_a_windows_home_path():
    assert ep.find_leaks(r"C:\Users\rwalters\canary")
    assert ep.find_leaks("C:/Users/rwalters/canary")


def test_find_leaks_ignores_a_tilde_search_root():
    assert ep.find_leaks("found via search_root:~/.volare") == []


def test_find_leaks_ignores_a_repo_relative_path():
    assert ep.find_leaks("sim/bandgap/records/20260820T101500Z-9f2c1a3.md") == []


def test_find_leaks_ignores_a_documentation_placeholder():
    assert ep.find_leaks("/Users/<author>/.volare/gf180mcuD") == []


def test_find_leaks_reports_line_numbers():
    text = "clean line\nanother clean line\n/home/rwalters/x\n"
    assert [leak["line"] for leak in ep.find_leaks(text)] == [3]


def test_find_leaks_flags_extra_identifiers_as_whole_words():
    assert ep.find_leaks("host robb-pro", extra_identifiers=["robb-pro"])
    assert ep.find_leaks("robb-prometheus", extra_identifiers=["robb-pro"]) == []


def test_find_leaks_ignores_short_identifiers():
    """A two-character login would match everywhere; the scan would be
    useless (and would false-positive on a legitimate record)."""
    assert ep.find_leaks("a ci run", extra_identifiers=["ci"]) == []


def test_scan_files_reports_per_file_leaks(tmp_path):
    clean = tmp_path / "clean.md"
    clean.write_text("- Host: host-1f4c8a21 (Darwin arm64)\n")
    dirty = tmp_path / "dirty.md"
    dirty.write_text(_AUDITED_RECORD_LINE + "\n")

    report = ep.scan_files([str(clean), str(dirty)])

    assert report["schema_version"] == ep.SCHEMA_VERSION
    assert report["status"] == "leaked"
    assert report["leak_count"] == 1
    by_path = {entry["file"]: entry for entry in report["files"]}
    assert by_path[str(clean)]["leaks"] == []
    assert len(by_path[str(dirty)]["leaks"]) == 1


def test_scan_files_is_clean_when_nothing_leaks(tmp_path):
    clean = tmp_path / "clean.md"
    clean.write_text("- Host: host-1f4c8a21\n")
    report = ep.scan_files([str(clean)])
    assert report["status"] == "clean"
    assert report["leak_count"] == 0


def test_scan_files_raises_for_an_unreadable_file(tmp_path):
    with pytest.raises(ep.EnvironmentProvenanceError):
        ep.scan_files([str(tmp_path / "missing.md")])


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_emit_json(capsys, monkeypatch):
    monkeypatch.setattr(ep.socket, "gethostname", lambda: "robb-pro")
    assert main(["env-provenance", "emit", "--format", "json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert _HOST_ID_RE.match(payload["host_id"])
    assert payload["paths"] == {}


def test_cli_emit_with_a_labelled_path(tmp_path, capsys, monkeypatch):
    root = _make_repo(tmp_path)
    monkeypatch.chdir(root)
    (root / "sim" / "bandgap" / "bandgap.spice").write_text("* netlist\n")

    assert (
        main(
            [
                "env-provenance",
                "emit",
                "--path",
                "netlist=sim/bandgap/bandgap.spice",
                "--format",
                "json",
            ]
        )
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["paths"]["netlist"] == {
        "path": "sim/bandgap/bandgap.spice",
        "scope": "repo",
    }


def test_cli_emit_rejects_a_malformed_path_argument(capsys):
    assert main(["env-provenance", "emit", "--path", "no-equals-sign"]) == 1
    assert "LABEL=PATH" in capsys.readouterr().err


def test_cli_emit_text(capsys, monkeypatch):
    monkeypatch.setattr(ep.socket, "gethostname", lambda: "robb-pro")
    assert main(["env-provenance", "emit"]) == 0
    out = capsys.readouterr().out
    assert "host-" in out
    assert "robb-pro" not in out


def test_cli_scan_exits_zero_when_clean(tmp_path, capsys):
    clean = tmp_path / "clean.md"
    clean.write_text("- Host: host-1f4c8a21\n")
    assert main(["env-provenance", "scan", str(clean), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "clean"


def test_cli_scan_exits_three_on_a_leak(tmp_path, capsys):
    dirty = tmp_path / "dirty.md"
    dirty.write_text(_AUDITED_RECORD_LINE + "\n")
    assert main(["env-provenance", "scan", str(dirty), "--format", "json"]) == (
        ep.LEAKS_FOUND_EXIT_CODE
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "leaked"
    assert payload["files"][0]["leaks"][0]["kind"] == "home-path"


def test_cli_scan_missing_file_is_an_application_error(tmp_path, capsys):
    assert main(["env-provenance", "scan", str(tmp_path / "missing.md")]) == 1
    assert "missing.md" in capsys.readouterr().err


def test_cli_group_without_a_subcommand_reports_usage(capsys):
    assert main(["env-provenance"]) == 2


# --------------------------------------------------------------------------- #
# `klt env-provenance lint-envelope` (issue #2224): the reproducibility scan
# --------------------------------------------------------------------------- #


def _write_envelope(path, payload) -> str:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_lint_envelope_flags_an_injected_home_path_and_names_the_field():
    """The negative control from issue #2224: an envelope carrying an
    absolute `/Users/...` path fails the lint, and the finding names the
    offending field, not just a line number."""
    findings = ep.find_absolute_path_fields(
        {
            "schema_version": 1,
            "def_path": "/Users/someone/work/gcd.def",
            "gds_path": "build/gcd.gds",
        }
    )
    assert [finding["field"] for finding in findings] == ["def_path"]
    assert findings[0]["match"] == "/Users/someone/work/gcd.def"


def test_lint_envelope_passes_a_clean_envelope():
    """A record using the repo-relative `{path, scope}` shape (and hashes)
    has nothing to flag."""
    assert (
        ep.find_absolute_path_fields(
            {
                "schema_version": 1,
                "netlist_path": {"path": "build/gcd_synth.v", "scope": "repo"},
                "liberty": {"path": None, "scope": "external"},
                "provenance": {"input": {"content_hash": "sha256:abc123"}},
            }
        )
        == []
    )


def test_lint_envelope_flags_non_home_absolute_paths_scan_would_miss():
    """The distinction that justifies a second scan: `/opt/build/...` and
    `/tmp/...` name nobody -- `find_leaks` (the disclosure scan) correctly
    ignores them -- yet they break byte comparison on another checkout just
    as thoroughly as a home path does."""
    payload = {"def_path": "/opt/build/out.def", "gds_path": "/tmp/run-3/top.gds"}
    assert ep.find_leaks(json.dumps(payload)) == []
    assert sorted(
        finding["field"] for finding in ep.find_absolute_path_fields(payload)
    ) == ["def_path", "gds_path"]


def test_lint_envelope_names_nested_and_indexed_fields():
    findings = ep.find_absolute_path_fields(
        {"macros": [{"lef": "lef/sram.lef"}, {"lef": "/Users/x/sram.lef"}]}
    )
    assert [finding["field"] for finding in findings] == ["macros.1.lef"]


def test_lint_envelope_flags_a_leaking_mapping_key():
    """A dict *keyed* by an absolute path leaks exactly as thoroughly as one
    valued by it."""
    findings = ep.find_absolute_path_fields({"per_file": {"/Users/x/a.gds": 3}})
    assert findings == [{"field": "per_file./Users/x/a.gds", "match": "/Users/x/a.gds"}]


def test_lint_envelope_allows_a_declared_install_prefix():
    payload = {"liberty": "/usr/share/pdk/sky130A/libs.ref/x.lib"}
    assert ep.find_absolute_path_fields(payload) != []
    assert (
        ep.find_absolute_path_fields(payload, allow_prefixes=["/usr/share/pdk"]) == []
    )


def test_lint_envelope_allow_prefix_respects_component_boundaries():
    """`--allow-prefix /opt/pdk` must not admit `/opt/pdk-scratch/...`."""
    payload = {"liberty": "/opt/pdk-scratch/x.lib"}
    assert ep.find_absolute_path_fields(payload, allow_prefixes=["/opt/pdk"]) != []


def test_lint_envelope_ignores_urls_and_relative_paths():
    """A `https://` reference and a repo-relative path are not host paths --
    flagging them would make the lint fire on every `see also` string."""
    assert (
        ep.find_absolute_path_fields(
            {
                "docs": "see https://example.com/cli/drc.md for the contract",
                "deck": "decks/sky130.py",
                "token": "$PDK_ROOT/libs.ref/x.lib",
                "home_token": "~/klayout-tools/x.gds",
            }
        )
        == []
    )


def test_lint_envelope_ignores_an_angle_bracket_template_root():
    """`<path-to-your-checkout>/infra/aws/x.sh` is a template a reader
    substitutes into, not a path that resolves on this or any other host
    (issue #2230: `examples/sim-batch/matrix-batch.request.json`'s
    `batch.provision_script_path`). Flagging it would demand a content edit
    that makes the example *less* honest."""
    assert (
        ep.find_absolute_path_fields(
            {
                "provision_script_path": (
                    "<path-to-your-2am-checkout>/infra/aws/batch-fleet-provision.sh"
                ),
                "pdk_root": "<pdk-root>/sky130A/libs.tech/ngspice/sky130.lib.spice",
            }
        )
        == []
    )


def test_lint_envelope_still_flags_a_real_path_beside_a_template_root():
    """The template exemption is adjacency-scoped: a genuine absolute path
    elsewhere in the same string is still a finding."""
    findings = ep.find_absolute_path_fields(
        {"note": "copy <your-checkout>/infra/run.sh to /Users/rob/bin/run.sh"}
    )
    assert [finding["match"] for finding in findings] == ["/Users/rob/bin/run.sh"]


def test_lint_envelope_ignores_a_json_pointer_uri_fragment():
    """`02-architecture.json#/blocks/ota_buffer` is a relative document
    reference plus an RFC 6901 JSON Pointer -- `/blocks/ota_buffer` addresses
    a node *inside* that document, not a directory on this host (issue #2230:
    `examples/design-pipeline/03-blockspec.json`'s `input_ref`)."""
    assert (
        ep.find_absolute_path_fields(
            {
                "input_ref": "02-architecture.json#/blocks/ota_buffer",
                "bare_pointer": "#/definitions/corner/properties/vdd_v",
            }
        )
        == []
    )


def test_lint_envelope_still_flags_the_document_half_of_a_fragment_ref():
    """Only the fragment is exempt -- an absolute host path on the document
    side of the `#` is still a finding."""
    findings = ep.find_absolute_path_fields(
        {"input_ref": "/Users/rob/design/02-architecture.json#/blocks/ota_buffer"}
    )
    assert [finding["match"] for finding in findings] == [
        "/Users/rob/design/02-architecture.json"
    ]


def test_lint_envelope_flags_windows_paths():
    findings = ep.find_absolute_path_fields({"out": "C:\\Users\\rob\\top.gds"})
    assert findings[0]["field"] == "out"


def test_lint_envelope_files_reports_status_and_recommendation(tmp_path):
    dirty = _write_envelope(tmp_path / "dirty.json", {"def_path": "/Users/x/a.def"})
    clean = _write_envelope(tmp_path / "clean.json", {"def_path": "build/a.def"})

    report = ep.lint_envelope_files([clean])
    assert report["status"] == "clean"
    assert report["finding_count"] == 0
    assert report["recommendation"] == ep.ENVELOPE_LINT_RECOMMENDATION

    report = ep.lint_envelope_files([dirty])
    assert report["status"] == "violations"
    assert report["finding_count"] == 1
    assert report["files"][0]["file"] == dirty


def test_lint_envelope_files_rejects_a_non_json_file(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(ep.EnvironmentProvenanceError, match="not valid JSON"):
        ep.lint_envelope_files([str(path)])


def test_lint_envelope_files_rejects_an_unreadable_file(tmp_path):
    with pytest.raises(ep.EnvironmentProvenanceError, match="could not read"):
        ep.lint_envelope_files([str(tmp_path / "missing.json")])


def test_cli_lint_envelope_exits_three_and_names_the_field(tmp_path, capsys):
    path = _write_envelope(tmp_path / "report.json", {"def_path": "/Users/x/a.def"})
    assert main(["env-provenance", "lint-envelope", path, "--format", "json"]) == (
        ep.LEAKS_FOUND_EXIT_CODE
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "violations"
    assert payload["files"][0]["findings"][0]["field"] == "def_path"


def test_cli_lint_envelope_exits_zero_when_clean(tmp_path, capsys):
    path = _write_envelope(
        tmp_path / "report.json",
        {"netlist_path": {"path": "build/a.v", "scope": "repo"}},
    )
    assert main(["env-provenance", "lint-envelope", path, "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "clean"


def test_cli_lint_envelope_allow_prefix(tmp_path, capsys):
    path = _write_envelope(tmp_path / "report.json", {"lib": "/usr/share/pdk/x.lib"})
    assert (
        main(
            [
                "env-provenance",
                "lint-envelope",
                path,
                "--allow-prefix",
                "/usr/share/pdk",
                "--format",
                "json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "clean"


def test_cli_lint_envelope_text_output_prints_the_recommendation(tmp_path, capsys):
    path = _write_envelope(tmp_path / "report.json", {"def_path": "/Users/x/a.def"})
    main(["env-provenance", "lint-envelope", path])
    out = capsys.readouterr().out
    assert "def_path: /Users/x/a.def" in out
    assert "repo-relative" in out


def test_cli_lint_envelope_missing_file_is_an_application_error(tmp_path, capsys):
    assert main(["env-provenance", "lint-envelope", str(tmp_path / "no.json")]) == 1
    assert "no.json" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# this repository's own committed examples/ tree (issue #2230)
# --------------------------------------------------------------------------- #

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _committed_example_envelopes() -> list[str]:
    """Every committed JSON artifact under `examples/`, via `git ls-files`.

    `git ls-files` rather than a filesystem walk so the set is exactly what is
    committed -- a scratch report a developer left in `examples/` is not this
    gate's business, and a walk would fail the suite on it.
    """
    result = subprocess.run(
        ["git", "ls-files", "-z", "examples/**/*.json"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("not a git checkout (or git unavailable)")
    return [name for name in result.stdout.split("\0") if name]


def test_committed_example_envelopes_carry_no_absolute_host_paths():
    """The acceptance criterion of issue #2230, asserted in-suite so it fails
    locally before CI does.

    Three `examples/critical-net-mom-fidelity/*.json` `klt extract` reports
    shipped with the generating worktree's absolute path in
    `file`/`netlist_path` -- a record that resolves nowhere else, byte-differs
    on every regeneration elsewhere, and discloses an author path in a public
    repo. Note the **empty** allow-list: every finding was dispositioned on
    its own merits (content fix or lint fix), none blanket-allowed, and
    keeping the allow-list empty is the property worth protecting.
    """
    paths = _committed_example_envelopes()
    assert paths, "expected committed JSON artifacts under examples/"
    report = ep.lint_envelope_files([os.path.join(_REPO_ROOT, name) for name in paths])
    assert report["status"] == "clean", [
        entry for entry in report["files"] if entry["findings"]
    ]


def test_ci_gates_the_committed_example_envelopes():
    """The lint is only a gate if CI runs it -- a test asserting the tree is
    clean would otherwise be the whole enforcement, and a future PR touching
    only `.github/workflows/ci.yml` could drop the step silently. Mirrors the
    same wiring assertion `test_check_complexity_baseline.py` makes for the
    C901 ratchet.
    """
    workflow = os.path.join(_REPO_ROOT, ".github", "workflows", "ci.yml")
    with open(workflow, encoding="utf-8") as handle:
        text = handle.read()
    lint_job = text.split("\n  native:", 1)[0]
    # Comments stripped before the assertions below: the step's own comment
    # names `--allow-prefix` (to tell a future reader not to reach for it),
    # which must not be mistaken for the step actually passing one.
    commands = "\n".join(
        line for line in lint_job.splitlines() if not line.lstrip().startswith("#")
    )
    assert "env-provenance lint-envelope" in commands, (
        "the examples/ envelope lint must stay wired into the CI lint job"
    )
    assert "git ls-files 'examples/**/*.json'" in commands
    assert "--allow-prefix" not in commands, (
        "the examples/ tree passes with an empty allow-list -- adding one here "
        "would hide exactly the class of finding this gate exists to catch"
    )
