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
