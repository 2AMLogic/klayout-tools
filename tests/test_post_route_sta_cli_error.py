"""`klt sta` CLI error-contract regressions split out of
`tests/test_post_route_sta.py` (issue #5629 review): that file sits at its
file-size ratchet ceiling, so new CLI error-contract tests land here
instead. The helpers are imported from the original module, mirroring
`tests/test_asap7_passives.py`'s import of `tests/test_asap7_finfet.py`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from helpers.subprocess_fakes import fake_completed, patch_openroad_run
from klayout_tools._tcl import tcl_literal
from klayout_tools.cli import main
from test_post_route_sta import (
    _base_request,
    _isolate_pdk,
    _make_pdk_install,
    _write,
    _write_request,
)

pytestmark = pytest.mark.usefixtures("real_build_identity_git")


def test_cli_error_openroad_rejects_truncated_liberty(tmp_path, monkeypatch, capsys):
    """Issue #5629: a truncated corner `.lib` is rejected by OpenROAD's own
    `read_liberty` -- the CLI must answer with the documented JSON error
    envelope (exit 1, stdout empty), never a Python traceback. The fixture
    is the same truncation the Rust regression in
    `native/statime/src/liberty.rs` covers; this test pins the separate
    OpenROAD-path half of the contract."""
    _isolate_pdk(monkeypatch, tmp_path)
    install_root = tmp_path / "install"
    _make_pdk_install(install_root, "sky130A")
    truncated_lib = (
        install_root
        / "sky130A"
        / "libs.ref"
        / "sky130_fd_sc_hd"
        / "lib"
        / "sky130_fd_sc_hd__tt_025C_1v80.lib"
    )
    truncated_lib.write_text("library (demo) {\n", encoding="utf-8")
    monkeypatch.setenv("PDK_ROOT", str(install_root))
    # The complete command the generated session must issue for the
    # truncated fixture: the Tcl-quoted *full* path, not just its filename,
    # so a same-named valid lib in another directory cannot satisfy it.
    expected_read_liberty = f"read_liberty {tcl_literal(str(truncated_lib))}"
    _write(tmp_path / "top.def", "# fake routed def\n")
    request_path = _write_request(tmp_path / "request.json", _base_request())

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["openroad", "-version"]:
            return fake_completed(stdout="26Q3-771-gdeadbeef\n")
        # The real invocation is `openroad -no_init -exit -metrics
        # <metrics> <script>` (see `_openroad_engine.run_openroad`).
        assert cmd[:4] == ["openroad", "-no_init", "-exit", "-metrics"]
        script = Path(cmd[5]).read_text(encoding="utf-8")
        read_liberty_lines = [
            line for line in script.splitlines() if line.startswith("read_liberty")
        ]
        # The generated session reads exactly the truncated corner lib (full
        # path, compared whole)...
        assert read_liberty_lines == [expected_read_liberty]
        # ...and a real OpenROAD refuses to parse it: simulate that failure.
        return fake_completed(
            returncode=1,
            stderr=(
                f'[ERROR STA-0402] error parsing liberty file "{truncated_lib}"\n'
                "The liberty file ended before the group was closed.\n"
            ),
        )

    patch_openroad_run(monkeypatch, fake_run)

    exit_code = main(["sta", request_path, "--format", "json"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    # A single JSON envelope -- `json.loads` would reject a second one
    # concatenated after it, or a traceback leaking onto stderr.
    err = json.loads(captured.err)
    assert err["schema_version"] == 1
    assert err["error"]["command"] == "sta"
    assert "openroad sta run failed" in err["error"]["message"]
    assert "error parsing liberty file" in err["error"]["message"]
