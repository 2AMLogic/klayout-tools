"""Mount-namespace hint tests for `klt sta` engine errors (issue #5753).

Split out of `tests/test_post_route_sta.py` when the shared
`mount_namespace_hint` moved to `klayout_tools._openroad_mount_hint`, to
keep that 7.8k-line module from growing (the file-size ratchet, #2766).
"""

from __future__ import annotations

from helpers.subprocess_fakes import fake_completed
from klayout_tools import post_route_sta


def test_engine_error_message_appends_mount_hint_for_readable_path(tmp_path):
    """Issue #5753: an OpenROAD ``cannot open '<abs path>'`` failure for a
    path this process can itself read is the container-wrapper mount gap,
    not a missing file -- the same hint `klt place-and-route` appends
    (issue #1868) is appended as a further `--` clause, with the engine's
    own bracketed diagnostic still leading the message."""
    script = tmp_path / "sta_top_synth.tcl"
    script.write_text("read_liberty x\n", encoding="utf-8")
    completed = fake_completed(
        returncode=1,
        stderr=f"[ERROR STA-0340] cannot open '{script}'\n",
    )

    message = post_route_sta._engine_error_message(completed)

    assert message.startswith("openroad sta run failed: [ERROR STA-0340] cannot open '")
    assert str(script) in message
    assert "this process can read it" in message
    assert "container/wrapper" in message
    assert "docs/cli/place-and-route.md" in message


def test_engine_error_message_mount_hint_names_pdk_root_when_path_under_it(
    tmp_path,
):
    """`pdk_info` (the session's resolved liberty/PDK) feeds the shared
    hint's PDK aside when the unreadable path lives under the PDK root --
    `resolved_via` next to the path, the same as place-and-route."""
    liberty = tmp_path / "sky130_fd_sc_hd__tt_025C_1v80.lib"
    liberty.write_text("library fake {}\n", encoding="utf-8")
    completed = fake_completed(
        returncode=1,
        stderr=f"Error: sta_top.tcl, 1\ncannot read file {liberty}.\n",
    )
    pdk_info = {
        "root": str(tmp_path),
        "resolved_via": "search root: ~/.volare",
        "variant": "sky130A",
    }

    message = post_route_sta._engine_error_message(completed, pdk_info)

    assert "this process can read it" in message
    assert "resolved PDK root" in message
    assert "search root: ~/.volare" in message
    assert str(tmp_path) in message


def test_engine_error_message_no_mount_hint_when_path_unreadable(tmp_path):
    """A `cannot open <path>` failure for a path that does NOT exist (or is
    not readable) on this host is a genuinely missing file, not a
    mount-namespace mismatch -- the message keeps its pre-#5753 shape."""
    missing = tmp_path / "does" / "not" / "exist.tcl"
    completed = fake_completed(
        returncode=1,
        stderr=f"[ERROR STA-0340] cannot open '{missing}'\n",
    )

    assert post_route_sta._engine_error_message(completed) == (
        f"openroad sta run failed: [ERROR STA-0340] cannot open '{missing}'"
    )
