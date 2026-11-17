"""Issue #5666: ``render_path_field`` accepts a bare ``str`` -- some fields a
report legitimately carries as the request's own string echo (a `klt lvs`
`klayout-runset` `reference`, or a pre-normalisation committed report's
`layout`) -- and renders it verbatim instead of raising `AttributeError`: a
schema slip elsewhere degrades to a less-informative line, never a traceback.

Split from ``test_env_provenance`` (file-size ratchet)."""

from __future__ import annotations

from klayout_tools import env_provenance as ep


def _make_repo(tmp_path):
    root = tmp_path / "canary"
    (root / ".git").mkdir(parents=True)
    (root / "sim" / "bandgap").mkdir(parents=True)
    return root


def test_render_path_field_renders_the_envelope_branches(tmp_path):
    root = _make_repo(tmp_path)
    inside = str(root / "design.gds")
    entry = ep.repo_relative_path(inside, repo_root=str(root))

    assert ep.render_path_field(entry) == "design.gds"
    assert ep.render_path_field({"path": None, "scope": "external"}) == "<outside repo>"
    assert ep.render_path_field(None) == "<unresolved>"
    assert ep.render_path_field({}) == "<unresolved>"
    assert ep.render_path_field({"scope": "unknown"}) == "<unresolved>"


def test_render_path_field_renders_a_bare_string_verbatim():
    assert ep.render_path_field("ref.sp") == "ref.sp"
    assert ep.render_path_field("/tmp/elsewhere/ref.sp") == "/tmp/elsewhere/ref.sp"
    assert ep.render_path_field("") == ""
