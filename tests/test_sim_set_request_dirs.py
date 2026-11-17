"""Issue #5633: ``_request_dirs`` strips a trailing ``.request.json`` from the
request basename, so a request named ``<stem>.request.json`` -- the repo's own
documented convention -- lands in ``<outdir>/<stem>/`` (and its
``requests[].report`` under ``<outdir>/<stem>/report.json``), not
``<outdir>/<stem>.request/``. Stems without that suffix are unchanged."""

import json
import os

from klayout_tools import sim_set


def _dirs(tmp_path, names, outdir="out"):
    paths = []
    for name in names:
        path = tmp_path / name
        path.write_text("{}")
        paths.append(str(path))
    base = None if outdir is None else str(tmp_path / outdir)
    return sim_set._request_dirs(paths, base), paths, base


def test_request_json_suffix_is_stripped(tmp_path):
    dirs, _, _ = _dirs(tmp_path, ["foo.request.json"])
    assert dirs == [str(tmp_path / "out" / "foo")]


def test_stems_without_request_json_suffix_are_unchanged(tmp_path):
    dirs, _, _ = _dirs(tmp_path, ["foo.json", "foo.sim.json", "sim_request.json"])
    assert dirs == [
        str(tmp_path / "out" / "foo"),
        str(tmp_path / "out" / "foo.sim"),
        str(tmp_path / "out" / "sim_request"),
    ]


def test_bare_request_json_falls_back_to_index(tmp_path):
    dirs, _, _ = _dirs(tmp_path, [".request.json"])
    assert dirs == [str(tmp_path / "out" / "request-0")]


def test_without_outdir_stem_appends_to_default_location(tmp_path):
    dirs, _, _ = _dirs(tmp_path, ["foo.request.json"], outdir=None)
    assert dirs == [str(tmp_path / ".klt" / "sim" / "foo")]


def test_request_and_plain_stem_collision_stays_distinct(tmp_path):
    dirs, _, _ = _dirs(tmp_path, ["foo.request.json", "foo.json"])
    assert os.path.abspath(dirs[0]) != os.path.abspath(dirs[1])
    assert dirs == [str(tmp_path / "out" / "foo"), str(tmp_path / "out" / "foo-1")]


def test_same_basename_in_different_source_dirs_stays_distinct(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    dirs, _, _ = _dirs(tmp_path, ["a/foo.request.json", "b/foo.request.json"])
    assert os.path.abspath(dirs[0]) != os.path.abspath(dirs[1])
    assert dirs == [str(tmp_path / "out" / "foo"), str(tmp_path / "out" / "foo-1")]


def _fake_run(_path, **_kwargs):
    return {"status": "pass", "environment": {"remote": {"job_id": "j1"}}}


def test_report_path_and_manifest_dir_follow_member_dir(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    paths = []
    for name in ("alpha.request.json", "beta.json"):
        path = src / name
        path.write_text("{}")
        paths.append(str(path))
    outdir = tmp_path / "out"

    report = sim_set.run_sim_set(
        paths, outdir=str(outdir), run=_fake_run, max_in_flight=2
    )
    manifest = json.loads((outdir / "set-manifest.json").read_text(encoding="utf-8"))[
        "requests"
    ]

    assert report["requests"][0]["report"] == str(outdir / "alpha" / "report.json")
    assert report["requests"][1]["report"] == str(outdir / "beta" / "report.json")
    for entry, m_entry in zip(report["requests"], manifest, strict=False):
        assert os.path.isfile(entry["report"])
        assert os.path.dirname(entry["report"]) == m_entry["dir"]
        assert m_entry["dir"] == str(outdir / os.path.basename(m_entry["dir"]))


def test_member_error_before_its_directory_reports_null_report(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    path = src / "alpha.request.json"
    path.write_text("{}")

    def _boom(_path, **_kwargs):
        raise RuntimeError("no launch")

    report = sim_set.run_sim_set([str(path)], outdir=str(tmp_path / "out"), run=_boom)
    assert report["requests"][0]["report"] is None
    assert report["requests"][0]["status"] == "error"
