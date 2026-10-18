"""Tests for `klt sim`'s pre-composed deck transport (issue #2804,
`src/klayout_tools/sim_decks.py`) -- caller-owned decks on the same
``remote``/``batch`` capacity the corner requests use.

Two disciplines mirror the neighboring suites:

- **No paid infrastructure.** The remote tests substitute fakes for
  ``RemoteLauncher`` and the ``remote_transport`` push/run/pull/cleanup
  calls (the same recorded-call-log discipline ``tests/test_sim.py``'s
  ``_FakeRemoteLauncher`` applies); the batch tests substitute fakes for
  ``sim_batch``'s submit/launch/poll/collect. No test here ever touches
  AWS, SSH, or S3.
- **The fake transport runs the real worker.** The remote/batch fakes
  "execute" the pushed worker request by calling the real
  :func:`sim_decks.run_deck_request` against a local job directory --
  exactly what the real worker host does -- so the local-vs-worker
  artifact-equality acceptance criterion is verified through the same
  code path a fleet job runs, with a deterministic stub engine standing
  in for ngspice (``options.ngspice_binary``, issue #2423's seam).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from klayout_tools import sim
from klayout_tools import sim_batch as sb
from klayout_tools import sim_decks as sd
from klayout_tools.sim import SimError

# --------------------------------------------------------------------------- #
# Deterministic stub engine
# --------------------------------------------------------------------------- #

_STUB_ENGINE = r"""#!/usr/bin/env python3
# Deterministic ngspice stand-in: reads `* stub-*:` protocol lines from the
# deck it is handed and performs them. Never touches a network.
import os
import sys
import time


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] != "-b" or len(argv) != 2:
        print("stub: expected `-b <deck>`", file=sys.stderr)
        return 2
    deck = argv[1]
    exit_code = 0
    with open(deck, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line.startswith("* stub-"):
                continue
            directive, _, rest = line[len("* stub-") :].partition(":")
            rest = rest.strip()
            if directive == "exit":
                exit_code = int(rest)
            elif directive == "sleep":
                time.sleep(float(rest))
            elif directive == "output":
                name, _, count = rest.partition(" ")
                parent = os.path.dirname(name)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                count = int(count)
                pattern = bytes(range(256)) * (count // 256 + 1)
                with open(name, "wb") as out:
                    out.write(pattern[:count])
            elif directive == "emit-stderr":
                sys.stderr.buffer.write(bytes(int(rest)))
                sys.stderr.buffer.flush()
            elif directive == "copy-input":
                source, _, dest = rest.partition(" ")
                shutil_copy(source, dest)
    print("ngspice-46 stub banner")
    sys.stdout.flush()
    return exit_code


def shutil_copy(source: str, dest: str) -> None:
    import shutil

    parent = os.path.dirname(dest)
    if parent:
        os.makedirs(parent, exist_ok=True)
    shutil.copyfile(source, dest)


if __name__ == "__main__":
    sys.exit(main())
"""


def write_stub_engine(tmp_path: Path) -> str:
    path = tmp_path / "stub-ngspice"
    path.write_text(_STUB_ENGINE, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def deck_request(tmp_path: Path, **overrides) -> dict:
    """A minimal valid deck-list request: one two-unit campaign whose stub
    directives produce a rawfile-shaped binary output and a nonzero-exit
    variant, overridable per test. Also writes the stub engine and the
    deck/input files it names."""
    write_stub_engine(tmp_path)
    (tmp_path / "deck.spice").write_text(
        "* caller-composed deck\n"
        "* stub-output: out/measure.raw 300\n"
        "* stub-emit-stderr: 7\n",
        encoding="utf-8",
    )
    (tmp_path / "extra.spice").write_text("* extra input\n", encoding="utf-8")
    request = {
        "backend": "local",
        "engine": "ngspice",
        "options": {"ngspice_binary": str(tmp_path / "stub-ngspice")},
        "decks": [
            {
                "id": "tt_27c",
                "deck": "deck.spice",
                "inputs": ["extra.spice"],
                "outputs": ["out/measure.raw"],
                "timeout_s": 30,
            },
            {
                "id": "ss_125c",
                "deck": "deck.spice",
                "inputs": [{"path": "extra.spice", "name": "bench/extra.spice"}],
                "outputs": [],
            },
        ],
    }
    request.update(overrides)
    return request


def write_request(tmp_path: Path, request: dict, name: str = "request.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(request), encoding="utf-8")
    return path


def run_local(tmp_path: Path, request: dict, outdir: str = "out") -> dict:
    write_request(tmp_path, request)
    return sim.run_sim(
        str(tmp_path / "request.json"),
        backend=request.get("backend", "local"),
        artifacts_dir=str(tmp_path / outdir),
    )


# --------------------------------------------------------------------------- #
# Validation: refuse before anything runs
# --------------------------------------------------------------------------- #


class TestValidation:
    def test_duplicate_ids_rejected(self, tmp_path):
        request = deck_request(tmp_path)
        request["decks"][1]["id"] = "tt_27c"
        with pytest.raises(sim.SimError, match="duplicates an earlier deck id"):
            run_local(tmp_path, request)

    @pytest.mark.parametrize("bad_id", ["../escape", "sub/dir", ".", "..", ""])
    def test_unsafe_ids_rejected(self, tmp_path, bad_id):
        request = deck_request(tmp_path)
        request["decks"][0]["id"] = bad_id
        with pytest.raises(sim.SimError, match=r"request\.decks\[0\]\.id"):
            run_local(tmp_path, request)

    def test_missing_deck_file_rejected(self, tmp_path):
        request = deck_request(tmp_path)
        request["decks"][0]["deck"] = "nope.spice"
        with pytest.raises(sim.SimError, match="does not resolve to a readable"):
            run_local(tmp_path, request)

    def test_input_destination_traversal_rejected(self, tmp_path):
        request = deck_request(tmp_path)
        request["decks"][0]["inputs"] = [
            {"path": "extra.spice", "name": "../outside.spice"}
        ]
        with pytest.raises(sim.SimError, match=r"inputs\[0\]\.name"):
            run_local(tmp_path, request)

    def test_input_absolute_destination_rejected(self, tmp_path):
        request = deck_request(tmp_path)
        request["decks"][0]["inputs"] = [
            {"path": "extra.spice", "name": "/abs/extra.spice"}
        ]
        with pytest.raises(sim.SimError, match=r"inputs\[0\]\.name"):
            run_local(tmp_path, request)

    def test_destination_collision_rejected(self, tmp_path):
        request = deck_request(tmp_path)
        request["decks"][0]["inputs"] = [{"path": "extra.spice", "name": "deck.spice"}]
        with pytest.raises(sim.SimError, match="already claimed"):
            run_local(tmp_path, request)

    def test_two_inputs_colliding_on_one_name_rejected(self, tmp_path):
        (tmp_path / "a.spice").write_text("* a\n", encoding="utf-8")
        (tmp_path / "b.spice").write_text("* b\n", encoding="utf-8")
        request = deck_request(tmp_path)
        request["decks"][0]["inputs"] = [
            {"path": "a.spice", "name": "same.spice"},
            {"path": "b.spice", "name": "same.spice"},
        ]
        with pytest.raises(sim.SimError, match="already claimed"):
            run_local(tmp_path, request)

    @pytest.mark.parametrize("bad_output", ["../x.raw", "/tmp/x.raw", "a/../b.raw"])
    def test_unsafe_outputs_rejected(self, tmp_path, bad_output):
        request = deck_request(tmp_path)
        request["decks"][0]["outputs"] = [bad_output]
        with pytest.raises(sim.SimError, match=r"outputs\[0\]"):
            run_local(tmp_path, request)

    def test_output_colliding_with_input_rejected(self, tmp_path):
        request = deck_request(tmp_path)
        request["decks"][0]["outputs"] = ["extra.spice"]
        with pytest.raises(sim.SimError, match="collides with a staged"):
            run_local(tmp_path, request)

    @pytest.mark.parametrize("stream", ["stdout.bin", "stderr.bin"])
    def test_output_naming_a_stream_rejected(self, tmp_path, stream):
        request = deck_request(tmp_path)
        request["decks"][0]["outputs"] = [stream]
        with pytest.raises(sim.SimError, match="collides with a staged"):
            run_local(tmp_path, request)

    def test_input_naming_a_stream_rejected(self, tmp_path):
        request = deck_request(tmp_path)
        request["decks"][0]["inputs"] = [{"path": "extra.spice", "name": "stdout.bin"}]
        with pytest.raises(sim.SimError, match="reserved for the unit's captured"):
            run_local(tmp_path, request)

    @pytest.mark.parametrize("backend", ["remote", "batch"])
    def test_worker_form_refused_on_off_host_submit(self, tmp_path, backend):
        """A caller-written worker-form deck would skip the closure scan on
        its way to a billable launch -- refused by name before staging."""
        request = deck_request(tmp_path, backend=backend)
        request["decks"] = [
            {"id": "u0", "deck": {"staged": "deck.spice", "name": "deck.spice"}}
        ]
        with pytest.raises(sim.SimError, match="worker {staged, name} form"):
            sd.stage_deck_job(request, request_dir=str(tmp_path))

    def test_worker_form_staged_resolves_against_request_dir(self, tmp_path):
        request = deck_request(tmp_path)
        request["decks"] = [
            {"id": "u0", "deck": {"staged": "deck.spice", "name": "deck.spice"}}
        ]
        unit = sd.plan_deck_units(request, request_dir=str(tmp_path))[0]
        assert unit["deck"]["staged"] == os.path.join(str(tmp_path), "deck.spice")

    @pytest.mark.parametrize("bad_timeout", [0, -1, "30", True])
    def test_bad_timeouts_rejected(self, tmp_path, bad_timeout):
        request = deck_request(tmp_path)
        request["decks"][0]["timeout_s"] = bad_timeout
        with pytest.raises(sim.SimError, match=r"decks\[0\]\.timeout_s"):
            run_local(tmp_path, request)

    @pytest.mark.parametrize(
        "field", ["netlist", "corners", "analysis", "measurements"]
    )
    def test_corner_fields_rejected_on_deck_requests(self, tmp_path, field):
        request = deck_request(tmp_path)
        request[field] = {} if field == "corners" else "x"
        with pytest.raises(sim.SimError, match=f"must not declare {field!r}"):
            run_local(tmp_path, request)

    def test_non_ngspice_engine_rejected(self, tmp_path):
        request = deck_request(tmp_path, engine="xyce")
        with pytest.raises(sim.SimError, match="engine 'ngspice' only"):
            run_local(tmp_path, request)

    def test_caller_declaring_includes_rejected(self, tmp_path):
        request = deck_request(tmp_path)
        request["decks"][0]["includes"] = [{"staged": "x", "name": "y"}]
        with pytest.raises(sim.SimError, match=r"includes is internal"):
            run_local(tmp_path, request)

    def test_empty_decks_list_rejected(self, tmp_path):
        request = deck_request(tmp_path, decks=[])
        with pytest.raises(sim.SimError, match="non-empty list"):
            run_local(tmp_path, request)

    def test_non_list_decks_rejected(self, tmp_path):
        request = deck_request(tmp_path, decks={"id": "x"})
        with pytest.raises(sim.SimError, match="non-empty list"):
            run_local(tmp_path, request)

    def test_decks_entry_must_be_object(self, tmp_path):
        request = deck_request(tmp_path, decks=["deck.spice"])
        with pytest.raises(sim.SimError, match=r"decks\[0\] must be a JSON object"):
            run_local(tmp_path, request)

    def test_default_timeout_comes_from_options(self, tmp_path):
        request = deck_request(tmp_path)
        del request["decks"][0]["timeout_s"]
        request["options"]["timeout_s"] = 42
        units = sd.plan_deck_units(request, request_dir=str(tmp_path))
        assert units[0]["timeout_s"] == 42.0

    def test_default_timeout_falls_back_to_corner_default(self, tmp_path):
        request = deck_request(tmp_path)
        del request["decks"][0]["timeout_s"]
        request["options"].pop("timeout_s", None)
        units = sd.plan_deck_units(request, request_dir=str(tmp_path))
        assert units[0]["timeout_s"] == sim.DEFAULT_TIMEOUT_S

    def test_load_request_requires_non_empty_list(self, tmp_path):
        path = write_request(tmp_path, {"decks": []})
        with pytest.raises(sim.SimError, match="non-empty list"):
            sim.load_request(str(path))

    def test_corner_requests_still_require_netlist(self, tmp_path):
        path = write_request(tmp_path, {"analysis": {"kind": "op", "args": ""}})
        with pytest.raises(sim.SimError, match="netlist"):
            sim.load_request(str(path))


# --------------------------------------------------------------------------- #
# Include closure: preserve the deck, stage the closure
# --------------------------------------------------------------------------- #


class TestIncludeClosure:
    def _deck(self, tmp_path: Path, body: str) -> dict:
        request = deck_request(tmp_path, decks=[{"id": "u0", "deck": "deck.spice"}])
        (tmp_path / "deck.spice").write_text(body, encoding="utf-8")
        return request

    def test_relative_include_staged_at_declared_path(self, tmp_path):
        (tmp_path / "models").mkdir()
        (tmp_path / "models" / "sky.lib").write_text("* model lib\n", encoding="utf-8")
        request = self._deck(tmp_path, "* deck\n.include models/sky.lib\n")
        unit = sd.plan_deck_units(request, request_dir=str(tmp_path))[0]
        assert unit["includes"] == [
            {"staged": str(tmp_path / "models" / "sky.lib"), "name": "models/sky.lib"}
        ]

    def test_quoted_include_and_dot_normalization(self, tmp_path):
        (tmp_path / "inc.spice").write_text("* inc\n", encoding="utf-8")
        request = self._deck(tmp_path, '* deck\n.include "./inc.spice"\n')
        unit = sd.plan_deck_units(request, request_dir=str(tmp_path))[0]
        assert unit["includes"][0]["name"] == "inc.spice"

    def test_nested_include_resolves_relative_to_including_file(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "a.spice").write_text(
            "* a\n.include b.spice\n", encoding="utf-8"
        )
        (tmp_path / "sub" / "b.spice").write_text("* b\n", encoding="utf-8")
        request = self._deck(tmp_path, "* deck\n.inc sub/a.spice\n")
        unit = sd.plan_deck_units(request, request_dir=str(tmp_path))[0]
        assert [entry["name"] for entry in unit["includes"]] == [
            "sub/a.spice",
            "sub/b.spice",
        ]

    def test_parent_climbing_include_rejected(self, tmp_path):
        (tmp_path / "outside.spice").write_text("* outside\n", encoding="utf-8")
        request = self._deck(tmp_path, "* deck\n.include ../outside.spice\n")
        with pytest.raises(sim.SimError, match="climbs above the deck"):
            sd.plan_deck_units(request, request_dir=str(tmp_path))

    def test_absolute_include_rejected(self, tmp_path):
        request = self._deck(tmp_path, f"* deck\n.include {tmp_path}/abs.spice\n")
        with pytest.raises(sim.SimError, match="cannot be staged without modifying"):
            sd.plan_deck_units(request, request_dir=str(tmp_path))

    def test_unresolvable_include_rejected(self, tmp_path):
        request = self._deck(tmp_path, "* deck\n.include missing.spice\n")
        with pytest.raises(sim.SimError, match="does not resolve to a readable"):
            sd.plan_deck_units(request, request_dir=str(tmp_path))

    def test_pdk_rooted_include_left_to_the_executing_host(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PDK_ROOT", raising=False)
        request = self._deck(
            tmp_path,
            "* deck\n.include "
            "$PDK_ROOT/sky130A/libs.ref/sky130_fd_pr/models/all.spice\n",
        )
        request["models"] = {"pdk_root": str(tmp_path / "pdk")}
        unit = sd.plan_deck_units(request, request_dir=str(tmp_path))[0]
        assert unit["includes"] == []
        assert unit["host_resolved"] == [
            "$PDK_ROOT/sky130A/libs.ref/sky130_fd_pr/models/all.spice"
        ]

    # -- `.lib <file> <section>` model references (judge feedback, #2804) --

    def test_relative_lib_reference_staged_and_followed(self, tmp_path):
        (tmp_path / "models").mkdir()
        (tmp_path / "models" / "corners.lib").write_text(
            "* lib\n.lib tt\n.include tt.spice\n.endl tt\n", encoding="utf-8"
        )
        (tmp_path / "models" / "tt.spice").write_text("* tt\n", encoding="utf-8")
        request = self._deck(tmp_path, '* deck\n.lib "models/corners.lib" tt\n')
        unit = sd.plan_deck_units(request, request_dir=str(tmp_path))[0]
        assert unit["includes"] == [
            {
                "staged": str(tmp_path / "models" / "corners.lib"),
                "name": "models/corners.lib",
            },
            {
                "staged": str(tmp_path / "models" / "tt.spice"),
                "name": "models/tt.spice",
            },
        ]
        assert unit["host_resolved"] == []

    def test_lib_section_definition_is_not_a_file_reference(self, tmp_path):
        request = self._deck(tmp_path, "* deck\n.lib tt\n.endl tt\n")
        unit = sd.plan_deck_units(request, request_dir=str(tmp_path))[0]
        assert unit["includes"] == [] and unit["host_resolved"] == []

    def test_pdk_rooted_lib_left_to_the_executing_host(self, tmp_path):
        pdk = tmp_path / "pdk"
        target = f"{pdk}/sky130A/libs.tech/ngspice/sky130.lib.spice"
        request = self._deck(tmp_path, f"* deck\n.lib {target} tt\n")
        request["models"] = {"pdk_root": str(pdk)}
        unit = sd.plan_deck_units(request, request_dir=str(tmp_path))[0]
        assert unit["includes"] == []
        assert unit["host_resolved"] == [target]

    def test_env_var_lib_left_to_the_executing_host(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PDK_ROOT", raising=False)
        target = "$PDK_ROOT/sky130A/libs.tech/ngspice/sky130.lib.spice"
        request = self._deck(tmp_path, f"* deck\n.lib {target} tt\n")
        unit = sd.plan_deck_units(request, request_dir=str(tmp_path))[0]
        assert unit["host_resolved"] == [target]

    def test_missing_relative_lib_rejected(self, tmp_path):
        request = self._deck(tmp_path, "* deck\n.lib models.lib tt\n")
        with pytest.raises(sim.SimError, match=r"`\.lib` target 'models.lib' does"):
            sd.plan_deck_units(request, request_dir=str(tmp_path))

    def test_absolute_nonexistent_pdk_lib_rejected(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PDK_ROOT", raising=False)
        request = self._deck(
            tmp_path, "* deck\n.lib /nonexistent/pdk/sky130.lib.spice tt\n"
        )
        with pytest.raises(sim.SimError, match=r"absolute `\.lib` target"):
            sd.plan_deck_units(request, request_dir=str(tmp_path))

    def test_parent_climbing_lib_rejected(self, tmp_path):
        (tmp_path / "up.lib").write_text("* up\n", encoding="utf-8")
        (tmp_path / "decks").mkdir()
        request = deck_request(
            tmp_path, decks=[{"id": "u0", "deck": "decks/deck.spice"}]
        )
        (tmp_path / "decks" / "deck.spice").write_text(
            "* deck\n.lib ../up.lib tt\n", encoding="utf-8"
        )
        with pytest.raises(sim.SimError, match="climbs above the deck"):
            sd.plan_deck_units(request, request_dir=str(tmp_path))

    def test_unsupported_lib_form_rejected(self, tmp_path):
        request = self._deck(tmp_path, "* deck\n.lib a.lib tt extra\n")
        with pytest.raises(sim.SimError, match="unsupported `.lib` directive form"):
            sd.plan_deck_units(request, request_dir=str(tmp_path))

    def test_bad_lib_refused_before_any_staging(self, tmp_path):
        request = self._deck(tmp_path, "* deck\n.lib models.lib tt\n")
        request["backend"] = "batch"
        with pytest.raises(sim.SimError, match="does not resolve"):
            sd.stage_deck_job(request, request_dir=str(tmp_path))

    def test_closure_caps_bounded(self, tmp_path):
        for index in range(70):
            (tmp_path / f"inc{index}.spice").write_text("* inc\n", encoding="utf-8")
        body = "* deck\n" + "".join(
            f".include inc{index}.spice\n" for index in range(70)
        )
        request = self._deck(tmp_path, body)
        with pytest.raises(sim.SimError, match="include closure exceeds"):
            sd.plan_deck_units(request, request_dir=str(tmp_path))


# --------------------------------------------------------------------------- #
# Local execution: evidence, isolation, preservation
# --------------------------------------------------------------------------- #


class TestLocalRunner:
    def test_happy_path_report_and_streams(self, tmp_path):
        write_stub_engine(tmp_path)
        report = run_local(tmp_path, deck_request(tmp_path))
        assert report["mode"] == "decks"
        # Versioned per command (docs/json-contract.md): klt sim's version,
        # with `mode` as the additive discriminator.
        assert report["schema_version"] == sim.SCHEMA_VERSION
        assert report["status"] == "pass"
        assert report["passed"] == 2
        assert report["unit_count"] == 2
        assert [unit["id"] for unit in report["units"]] == ["tt_27c", "ss_125c"]
        outdir = Path(report["outdir"])
        unit = report["units"][0]
        assert unit["status"] == "pass"
        assert unit["exit_code"] == 0
        assert unit["engine_version"] == "46"
        assert unit["outputs"] == ["out/measure.raw"]
        assert unit["missing_outputs"] == []
        stdout = (outdir / unit["stdout"]).read_bytes()
        assert stdout == b"ngspice-46 stub banner\n"
        # The stub's 7 stderr bytes: raw binary evidence, not text.
        assert (outdir / unit["stderr"]).read_bytes() == bytes(7)
        assert len(unit["deck_sha256"]) == 64

    def test_non_utf8_streams_and_outputs_are_byte_exact(self, tmp_path):
        request = deck_request(
            tmp_path,
            decks=[{"id": "u0", "deck": "deck.spice", "outputs": ["out/bin.raw"]}],
        )
        (tmp_path / "deck.spice").write_text(
            "* stub-output: out/bin.raw 1000\n* stub-emit-stderr: 600\n",
            encoding="utf-8",
        )
        report = run_local(tmp_path, request)
        report["units"][0]
        outdir = Path(report["outdir"])
        raw = (outdir / "u0" / "out" / "bin.raw").read_bytes()
        assert len(raw) == 1000
        assert raw == (bytes(range(256)) * 5)[:1000]
        assert len((outdir / "u0" / "stderr.bin").read_bytes()) == 600

    def test_units_sharing_filenames_stay_isolated(self, tmp_path):
        write_stub_engine(tmp_path)
        (tmp_path / "one.spice").write_text("* one\n", encoding="utf-8")
        (tmp_path / "two.spice").write_text("* two\n", encoding="utf-8")
        request = deck_request(
            tmp_path,
            decks=[
                {
                    "id": "arm-a",
                    "deck": "one.spice",
                    "inputs": ["two.spice"],
                },
                {
                    "id": "arm-b",
                    "deck": "two.spice",
                    "inputs": ["one.spice"],
                },
            ],
        )
        report = run_local(tmp_path, request)
        outdir = Path(report["outdir"])
        assert (outdir / "arm-a" / "two.spice").read_text().startswith("* two")
        assert (outdir / "arm-b" / "one.spice").read_text().startswith("* one")

    def test_deck_bytes_preserved_verbatim(self, tmp_path):
        request = deck_request(tmp_path, decks=[{"id": "u0", "deck": "deck.spice"}])
        payload = b"* deck with weird bytes: \xc3\x28 not-utf8\n* stub-exit: 0\n"
        (tmp_path / "deck.spice").write_bytes(payload)
        report = run_local(tmp_path, request)
        assert report["status"] == "pass"
        run_deck = (Path(report["outdir"]) / "u0" / "deck.spice").read_bytes()
        assert run_deck == payload
        import hashlib

        assert report["units"][0]["deck_sha256"] == hashlib.sha256(payload).hexdigest()

    def test_nonzero_exit_is_error_with_streams_kept(self, tmp_path):
        request = deck_request(tmp_path, decks=[{"id": "u0", "deck": "deck.spice"}])
        (tmp_path / "deck.spice").write_text(
            "* stub-exit: 3\n* stub-emit-stderr: 5\n", encoding="utf-8"
        )
        report = run_local(tmp_path, request)
        unit = report["units"][0]
        assert report["status"] == "error"
        assert unit["status"] == "error"
        assert unit["exit_code"] == 3
        assert (Path(report["outdir"]) / "u0" / "stderr.bin").read_bytes() == bytes(5)
        assert any(d["code"] == "engine_exit" for d in unit["diagnostics"])

    def test_timeout_kills_unit_and_keeps_partial_streams(self, tmp_path):
        request = deck_request(
            tmp_path,
            decks=[{"id": "u0", "deck": "deck.spice", "timeout_s": 0.5}],
        )
        (tmp_path / "deck.spice").write_text("* stub-sleep: 30\n", encoding="utf-8")
        report = run_local(tmp_path, request)
        unit = report["units"][0]
        assert report["status"] == "error"
        assert report["timed_out"] == 1
        assert unit["status"] == "timeout"
        assert unit["timed_out"] is True
        assert unit["exit_code"] is None
        assert any(d["code"] == "timeout" for d in unit["diagnostics"])
        assert (Path(report["outdir"]) / "u0" / "stdout.bin").exists()

    def test_missing_outputs_explicit(self, tmp_path):
        request = deck_request(
            tmp_path,
            decks=[{"id": "u0", "deck": "deck.spice", "outputs": ["out/x.raw"]}],
        )
        (tmp_path / "deck.spice").write_text("* writes nothing\n", encoding="utf-8")
        report = run_local(tmp_path, request)
        unit = report["units"][0]
        assert unit["outputs"] == []
        assert unit["missing_outputs"] == ["out/x.raw"]

    def test_launch_failure_leaves_uniform_shape(self, tmp_path, monkeypatch):
        write_stub_engine(tmp_path)

        def explode(argv, **kwargs):
            raise FileNotFoundError("no such engine")

        monkeypatch.setattr(sd.subprocess, "run", explode)
        request = deck_request(tmp_path, decks=[{"id": "u0", "deck": "deck.spice"}])
        report = run_local(tmp_path, request)
        unit = report["units"][0]
        assert report["status"] == "error"
        assert unit["status"] == "error"
        assert unit["exit_code"] is None
        assert unit["diagnostics"][0]["code"] == "engine_not_found"
        outdir = Path(report["outdir"])
        assert (outdir / "u0" / "stdout.bin").read_bytes() == b""
        assert (outdir / "u0" / "stderr.bin").read_bytes() == b""

    def test_unrunnable_explicit_binary_refused_up_front(self, tmp_path):
        write_stub_engine(tmp_path)
        request = deck_request(tmp_path)
        request["options"]["ngspice_binary"] = str(tmp_path / "absent-engine")
        with pytest.raises(sim.SimError, match="does not name a runnable"):
            run_local(tmp_path, request)

    def test_stale_unit_directory_is_recreated(self, tmp_path):
        write_stub_engine(tmp_path)
        request = deck_request(tmp_path, decks=[{"id": "u0", "deck": "deck.spice"}])
        outdir = tmp_path / "out"
        outdir.mkdir()
        (outdir / "u0").mkdir()
        (outdir / "u0" / "stale.raw").write_bytes(b"stale")
        report = run_local(tmp_path, request)
        assert not (Path(report["outdir"]) / "u0" / "stale.raw").exists()

    def test_local_parallel_matches_serial_byte_for_byte(self, tmp_path):
        write_stub_engine(tmp_path)
        serial = run_local(tmp_path, deck_request(tmp_path), outdir="out-serial")
        request = deck_request(tmp_path)
        request["backend"] = "local-parallel"
        request["options"]["max_workers"] = 2
        pooled = run_local(tmp_path, request, outdir="out-pooled")
        assert serial["status"] == pooled["status"] == "pass"
        for a, b in zip(serial["units"], pooled["units"], strict=False):
            assert a["id"] == b["id"]
            assert a["deck_sha256"] == b["deck_sha256"]
            root_a = Path(serial["outdir"])
            root_b = Path(pooled["outdir"])
            assert (root_a / a["stdout"]).read_bytes() == (
                root_b / b["stdout"]
            ).read_bytes()
            assert (root_a / a["stderr"]).read_bytes() == (
                root_b / b["stderr"]
            ).read_bytes()
            for name in a["outputs"]:
                assert (root_a / a["id"] / name).read_bytes() == (
                    root_b / b["id"] / name
                ).read_bytes()

    def test_results_reported_in_request_order_when_pooled(self, tmp_path):
        write_stub_engine(tmp_path)
        request = deck_request(tmp_path)
        request["backend"] = "local-parallel"
        request["options"]["max_workers"] = 4
        report = run_local(tmp_path, request)
        assert [u["id"] for u in report["units"]] == ["tt_27c", "ss_125c"]


# --------------------------------------------------------------------------- #
# Remote dispatch (fake transport, real worker)
# --------------------------------------------------------------------------- #


class _FakeRemoteLauncher:
    """Drop-in stand-in for ``remote_launcher.RemoteLauncher`` (the same
    shape ``tests/test_sim.py`` patches into the corner path)."""

    last_instance: _FakeRemoteLauncher | None = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.job_id = kwargs.get("job_id", "fake-deck-job")
        self.terminated = False
        type(self).last_instance = self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.terminated = True
        return False

    def provision(self):
        return {
            "provider": "aws",
            "region": self.kwargs["region"],
            "instance_type": "c7i.xlarge",
            "instance_id": "i-fake",
            "spot": True,
            "estimated_hourly_cost_usd": 0.1785,
            "ami_id": "ami-fake",
            "pdk_snapshot": "sky130A-fake",
            "spin_up_s": 1.0,
        }

    def get_public_ip(self):
        return "203.0.113.9"


def install_fake_remote(monkeypatch, tmp_path, *, report_override=None):
    """Patch the remote transport with fakes that *execute* the worker
    request locally: push materializes the inputs into a job directory,
    run calls the real :func:`sim_decks.run_deck_request` on it (exactly
    what the provisioned box would run), pull copies the unit trees back.

    ``remote_transport.job_dir`` is redirected under ``tmp_path``: the real
    one returns ``/home/<user>/<job_id>``, which is unwritable on CI
    runners (user ``runner``) and litters dev hosts with stray job
    directories.
    """
    state: dict = {}
    fake_remote_home = tmp_path / "fake-remote-home"
    monkeypatch.setattr(
        sd.remote_transport,
        "job_dir",
        lambda user, job_id: str(fake_remote_home / user / job_id),
    )

    def fake_push_job(*, host, remote_job_dir, job, **kwargs):
        state["host"] = host
        state["remote_job_dir"] = remote_job_dir
        state["job"] = job
        os.makedirs(remote_job_dir, exist_ok=True)
        for item in job.inputs:
            dest = os.path.join(remote_job_dir, item.remote_name)
            if item.local_path is not None:
                parent = os.path.dirname(dest)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                shutil.copyfile(item.local_path, dest)
            else:
                with open(dest, "w", encoding="utf-8") as handle:
                    handle.write(item.content)

    def fake_run_remote_job(*, host, remote_job_dir, job, timeout_s, **kwargs):
        state["timeout_s"] = timeout_s
        state["command"] = job.command
        state["success_exit_codes"] = job.success_exit_codes
        if report_override is not None:
            return report_override
        request_path = os.path.join(remote_job_dir, "request.json")
        return sd.run_deck_request(
            sim.load_request(request_path),
            request_dir=remote_job_dir,
            backend="local-parallel",
        )

    def fake_pull_artifacts(
        *, host, remote_job_dir, local_artifacts_dir, job, **kwargs
    ):
        state["pull_called"] = True
        shutil.copytree(
            os.path.join(remote_job_dir, job.artifacts_relative_dir),
            local_artifacts_dir,
            dirs_exist_ok=True,
        )

    def fake_cleanup_job(**kwargs):
        state["cleanup_called"] = True

    monkeypatch.setattr(sd, "RemoteLauncher", _FakeRemoteLauncher)
    monkeypatch.setattr(sd.remote_transport, "wait_for_ssh", lambda *a, **k: 0.5)
    monkeypatch.setattr(sd.remote_transport, "push_job", fake_push_job)
    monkeypatch.setattr(sd.remote_transport, "run_remote_job", fake_run_remote_job)
    monkeypatch.setattr(sd.remote_transport, "pull_artifacts", fake_pull_artifacts)
    monkeypatch.setattr(sd.remote_transport, "cleanup_job", fake_cleanup_job)
    return state


def remote_deck_request(tmp_path: Path, **overrides) -> dict:
    request = deck_request(tmp_path)
    request["backend"] = "remote"
    request["models"] = {"pdk": "sky130A"}
    request["remote"] = {
        "region": "us-east-1",
        "key_name": "fake-key",
        "ssh_key_path": "/fake/key.pem",
        "launcher_cidr": "203.0.113.4/32",
    }
    request.update(overrides)
    return request


class TestRemoteDispatch:
    def test_remote_runs_the_real_worker_and_collects_streams(
        self, tmp_path, monkeypatch
    ):
        write_stub_engine(tmp_path)
        state = install_fake_remote(monkeypatch, tmp_path)
        report = run_local(tmp_path, remote_deck_request(tmp_path), outdir="remote-out")
        assert report["status"] == "pass"
        assert state["pull_called"] is True
        assert state["cleanup_called"] is True
        assert state["command"] == (
            "klt sim request.json --backend local-parallel --format json"
        )
        assert state["success_exit_codes"] == (0, 4)
        # The launcher sizes the instance for the deck count, exactly as
        # for a corner matrix.
        assert _FakeRemoteLauncher.last_instance.kwargs["corner_count"] == 2
        assert _FakeRemoteLauncher.last_instance.kwargs["pdk"] == "sky130A"
        assert _FakeRemoteLauncher.last_instance.terminated is True
        # The worker's report is delivered with the LOCAL outdir: unit
        # paths are relative, so the collected streams resolve here.
        outdir = Path(report["outdir"])
        assert outdir.name == "remote-out"
        unit = report["units"][0]
        assert (outdir / unit["stdout"]).read_bytes() == b"ngspice-46 stub banner\n"
        assert (outdir / unit["id"] / "out" / "measure.raw").is_file()
        assert report["environment"]["remote"]["provider"] == "aws"
        assert report["backend"] == "remote"

    def test_worker_request_shape(self, tmp_path, monkeypatch):
        write_stub_engine(tmp_path)
        state = install_fake_remote(monkeypatch, tmp_path)
        run_local(tmp_path, remote_deck_request(tmp_path), outdir="out")
        worker = json.loads(
            next(
                item.content
                for item in state["job"].inputs
                if item.remote_name == "request.json"
            )
        )
        assert worker["backend"] == "local-parallel"
        assert worker["engine"] == "ngspice"
        assert "remote" not in worker and "batch" not in worker
        unit = worker["decks"][0]
        assert set(unit) == {
            "id",
            "deck",
            "inputs",
            "includes",
            "outputs",
            "timeout_s",
            "host_resolved",
        }
        assert unit["deck"]["name"] == "deck.spice"
        assert unit["deck"]["staged"].endswith(".spice")
        assert unit["inputs"][0]["name"] == "extra.spice"
        # Every staged file is pushed beside the request document.
        staged_names = {item.remote_name for item in state["job"].inputs}
        assert unit["deck"]["staged"] in staged_names
        assert unit["inputs"][0]["staged"] in staged_names

    def test_missing_pdk_refused_before_provisioning(self, tmp_path, monkeypatch):
        write_stub_engine(tmp_path)
        install_fake_remote(monkeypatch, tmp_path)
        _FakeRemoteLauncher.last_instance = None
        request = remote_deck_request(tmp_path)
        request["models"] = {}
        with pytest.raises(sim.SimError, match="requires request.models.pdk"):
            run_local(tmp_path, request, outdir="out")
        assert _FakeRemoteLauncher.last_instance is None

    def test_transport_failure_recovers_evidence_before_teardown(
        self, tmp_path, monkeypatch
    ):
        """An SSH drop mid-job must not lose what the box already wrote:
        the unit trees are pulled back *before* teardown and every unit
        reports ``remote_job_failed`` with the transport error, pointing at
        the streams that were recovered."""
        from klayout_tools import remote_transport

        write_stub_engine(tmp_path)
        state = install_fake_remote(monkeypatch, tmp_path)
        real_run = sd.remote_transport.run_remote_job

        def failing_run(**kwargs):
            # The worker ran (streams exist on the box), then SSH dropped.
            real_run(**kwargs)
            assert _FakeRemoteLauncher.last_instance.terminated is False
            raise remote_transport.RemoteTransportError("ssh went away")

        monkeypatch.setattr(sd.remote_transport, "run_remote_job", failing_run)
        report = run_local(tmp_path, remote_deck_request(tmp_path), outdir="out")
        assert state["pull_called"] is True
        assert _FakeRemoteLauncher.last_instance.terminated is True
        assert report["status"] == "error"
        assert report["backend"] == "remote"
        assert report["schema_version"] == sim.SCHEMA_VERSION
        assert report["environment"]["remote"]["instance_id"] == "i-fake"
        outdir = Path(report["outdir"])
        unit = report["units"][0]
        diagnostic = unit["diagnostics"][0]
        assert diagnostic["code"] == "remote_job_failed"
        assert "ssh went away" in diagnostic["message"]
        assert (
            "recovered from the instance: stdout.bin, stderr.bin"
            in (diagnostic["message"])
        )
        assert (outdir / unit["stdout"]).read_bytes() == b"ngspice-46 stub banner\n"
        assert unit["outputs"] == ["out/measure.raw"]

    def test_transport_failure_with_failed_recovery_still_reports(
        self, tmp_path, monkeypatch
    ):
        from klayout_tools import remote_transport

        write_stub_engine(tmp_path)
        install_fake_remote(monkeypatch, tmp_path)

        def failing(**kwargs):
            raise remote_transport.RemoteTransportError("ssh went away")

        monkeypatch.setattr(sd.remote_transport, "run_remote_job", failing)
        monkeypatch.setattr(sd.remote_transport, "pull_artifacts", failing)
        report = run_local(tmp_path, remote_deck_request(tmp_path), outdir="out")
        assert report["status"] == "error"
        assert _FakeRemoteLauncher.last_instance.terminated is True
        message = report["units"][0]["diagnostics"][0]["message"]
        assert "artifact recovery failed" in message
        assert "no unit streams were recovered" in message
        assert report["units"][0]["missing_outputs"] == ["out/measure.raw"]

    def test_failed_recovery_never_reports_stale_local_files(
        self, tmp_path, monkeypatch
    ):
        """Judge repro: a previous run left unit trees in the (reused)
        outdir; this run's SSH run and pull both fail. Nothing came back
        from the instance, so nothing may be reported as recovered and
        every declared output is missing -- the stale trees are cleared,
        while non-unit content of the outdir is left alone."""
        from klayout_tools import remote_transport

        write_stub_engine(tmp_path)
        local = run_local(tmp_path, deck_request(tmp_path), outdir="out")
        assert local["status"] == "pass"
        outdir = tmp_path / "out"
        assert (outdir / "tt_27c" / "stdout.bin").is_file()
        assert (outdir / "tt_27c" / "out" / "measure.raw").is_file()
        (outdir / "keep.txt").write_text("not a unit\n", encoding="utf-8")

        install_fake_remote(monkeypatch, tmp_path)

        def failing(**kwargs):
            raise remote_transport.RemoteTransportError("ssh went away")

        monkeypatch.setattr(sd.remote_transport, "run_remote_job", failing)
        monkeypatch.setattr(sd.remote_transport, "pull_artifacts", failing)
        report = run_local(tmp_path, remote_deck_request(tmp_path), outdir="out")
        assert report["status"] == "error"
        for unit in report["units"]:
            message = unit["diagnostics"][0]["message"]
            assert "recovered from the instance:" not in message
            assert "no unit streams were recovered" in message
            assert unit["outputs"] == []
            assert not (outdir / unit["id"]).exists()
        assert report["units"][0]["missing_outputs"] == ["out/measure.raw"]
        assert (outdir / "keep.txt").read_text(encoding="utf-8") == "not a unit\n"

    def test_recovery_reports_only_what_the_pull_returned(self, tmp_path, monkeypatch):
        """A partial pull over a pre-filled outdir: only the files this
        pull brought back count as recovered, per unit."""
        from klayout_tools import remote_transport

        write_stub_engine(tmp_path)
        run_local(tmp_path, deck_request(tmp_path), outdir="out")
        outdir = tmp_path / "out"
        assert (outdir / "ss_125c" / "stdout.bin").is_file()

        install_fake_remote(monkeypatch, tmp_path)

        def failing_run(**kwargs):
            raise remote_transport.RemoteTransportError("ssh went away")

        def partial_pull(*, local_artifacts_dir, **kwargs):
            unit_dir = os.path.join(local_artifacts_dir, "tt_27c")
            os.makedirs(unit_dir, exist_ok=True)
            with open(os.path.join(unit_dir, "stdout.bin"), "wb") as handle:
                handle.write(b"partial\n")

        monkeypatch.setattr(sd.remote_transport, "run_remote_job", failing_run)
        monkeypatch.setattr(sd.remote_transport, "pull_artifacts", partial_pull)
        report = run_local(tmp_path, remote_deck_request(tmp_path), outdir="out")
        tt, ss = report["units"]
        assert tt["diagnostics"][0]["message"].endswith(
            "(recovered from the instance: stdout.bin)"
        )
        assert tt["outputs"] == []
        assert tt["missing_outputs"] == ["out/measure.raw"]
        assert (outdir / "tt_27c" / "stdout.bin").read_bytes() == b"partial\n"
        assert not (outdir / "tt_27c" / "stderr.bin").exists()
        assert not (outdir / "tt_27c" / "out").exists()
        assert "no unit streams were recovered" in ss["diagnostics"][0]["message"]
        assert not (outdir / "ss_125c").exists()

    def test_success_replaces_stale_unit_trees(self, tmp_path, monkeypatch):
        write_stub_engine(tmp_path)
        stale_unit = tmp_path / "out" / "tt_27c"
        (stale_unit / "out").mkdir(parents=True)
        (stale_unit / "stale.txt").write_text("old\n", encoding="utf-8")
        (stale_unit / "out" / "old.raw").write_bytes(b"old")
        install_fake_remote(monkeypatch, tmp_path)
        report = run_local(tmp_path, remote_deck_request(tmp_path), outdir="out")
        assert report["status"] == "pass"
        assert not (stale_unit / "stale.txt").exists()
        assert not (stale_unit / "out" / "old.raw").exists()
        assert (stale_unit / "out" / "measure.raw").is_file()
        assert (stale_unit / "stdout.bin").read_bytes() == b"ngspice-46 stub banner\n"

    def test_push_failure_still_raises_sim_error(self, tmp_path, monkeypatch):
        from klayout_tools import remote_transport

        write_stub_engine(tmp_path)
        install_fake_remote(monkeypatch, tmp_path)

        def failing_push(**kwargs):
            raise remote_transport.RemoteTransportError("scp refused")

        monkeypatch.setattr(sd.remote_transport, "push_job", failing_push)
        with pytest.raises(sim.SimError, match="remote backend failed"):
            run_local(tmp_path, remote_deck_request(tmp_path), outdir="out")

    def test_non_deck_report_refused(self, tmp_path, monkeypatch):
        write_stub_engine(tmp_path)
        install_fake_remote(monkeypatch, tmp_path, report_override={"corners": []})
        with pytest.raises(sim.SimError, match="did not return a deck-list report"):
            run_local(tmp_path, remote_deck_request(tmp_path), outdir="out")


# --------------------------------------------------------------------------- #
# Batch dispatch (fake fleet, real worker)
# --------------------------------------------------------------------------- #


class _FakeFleet:
    """Stands in for the batch transport primitives ``_run_decks_batch``
    uses: submit captures the job spec, poll returns a canned status, and
    collect *executes* the captured worker request locally into the
    collected tree (report.json plus the artifacts/ subtree), exactly what
    the real harness's outputs/ upload carries back."""

    def __init__(self, status: dict, *, fail_collect: bool = False):
        self.status = status
        self.spec: sb.BatchJobSpec | None = None
        self.job_id = "batch-fake-0001"
        self.fail_collect = fail_collect

    def submit(self, config, job_id, spec, **kwargs):
        self.spec = spec
        self.job_id = job_id

    def launch(self, config, job_id, **kwargs):
        pass

    def poll(self, config, job_id, **kwargs):
        return dict(self.status)

    def collect(self, config, job_id, local_dir, **kwargs):
        if self.fail_collect:
            raise sb.BatchError("s3 collect failed")
        assert self.spec is not None
        job_dir = os.path.join(local_dir, "..", "jobdir")
        os.makedirs(job_dir, exist_ok=True)
        worker_request = None
        for item in self.spec.inputs:
            dest = os.path.join(job_dir, item.name)
            if item.local_path is not None:
                shutil.copyfile(item.local_path, dest)
            else:
                worker_request = json.loads(item.content)
                with open(dest, "w", encoding="utf-8") as handle:
                    handle.write(item.content)
        assert worker_request is not None
        artifacts = os.path.join(local_dir, sb.BATCH_ARTIFACTS_DIRNAME)
        report = sd.run_deck_request(
            worker_request,
            request_dir=job_dir,
            backend="local-parallel",
            artifacts_dir=artifacts,
        )
        with open(
            os.path.join(local_dir, sb.BATCH_REPORT_FILENAME), "w", encoding="utf-8"
        ) as handle:
            json.dump(report, handle)


def install_fake_batch(monkeypatch, fleet: _FakeFleet) -> _FakeFleet:
    monkeypatch.setattr(sb, "submit_job", fleet.submit)
    monkeypatch.setattr(sb, "launch_job", fleet.launch)
    monkeypatch.setattr(sb, "poll_status", fleet.poll)
    monkeypatch.setattr(sb, "collect_outputs", fleet.collect)
    return fleet


def batch_deck_request(tmp_path: Path, **overrides) -> dict:
    provision = tmp_path / "provision.sh"
    provision.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    provision.chmod(0o755)
    request = deck_request(tmp_path)
    request["backend"] = "batch"
    request["models"] = {"pdk": "sky130A"}
    request["batch"] = {
        "provision_script_path": str(provision),
        "bucket": "fake-bucket",
    }
    request.update(overrides)
    return request


class TestBatchDispatch:
    def test_spec_shape_and_collection(self, tmp_path, monkeypatch):
        write_stub_engine(tmp_path)
        fleet = install_fake_batch(
            monkeypatch, _FakeFleet({"state": "done", "exit_code": "0"})
        )
        report = run_local(tmp_path, batch_deck_request(tmp_path), outdir="batch-out")
        spec = fleet.spec
        # The deck job command: report redirect + ALWAYS an outdir into the
        # collected tree (unit streams are the result, not an opt-in).
        assert 'klt sim "$EDA_INPUT_DIR/request.json"' in spec.cmd
        assert "--backend local-parallel --format json" in spec.cmd
        assert '--outdir "$EDA_OUTPUT_DIR/artifacts"' in spec.cmd
        assert '> "$EDA_OUTPUT_DIR/report.json"' in spec.cmd
        assert spec.pdk_variant == "sky130A"
        # Cosmetic tool label: echoes the forwarded ngspice_binary override,
        # exactly as a corner job would (issue #2423 parity).
        assert spec.tool == str(tmp_path / "stub-ngspice")
        # Serial worst case: 30 + 120 (default) + 120 slack.
        assert spec.timeout_seconds == 30 + 120 + 120
        staged_names = {item.name for item in spec.inputs}
        assert {"request.json"} <= staged_names
        assert report["status"] == "pass"
        assert report["backend"] == "batch"
        outdir = Path(report["outdir"])
        assert outdir.name == "batch-out"
        assert (outdir / report["units"][0]["stdout"]).read_bytes() == (
            b"ngspice-46 stub banner\n"
        )
        assert report["environment"]["remote"]["provider"] == "aws-batch-fleet"

    def test_failed_job_recovers_envelope_and_keeps_failure_terminal(
        self, tmp_path, monkeypatch
    ):
        write_stub_engine(tmp_path)
        fleet = _FakeFleet({"state": "failed", "exit_code": "1", "detail": "boom"})
        fleet.fail_collect = True
        install_fake_batch(monkeypatch, fleet)
        report = run_local(tmp_path, batch_deck_request(tmp_path), outdir="out")
        assert report["status"] == "error"
        assert report["errored"] == 2
        for unit in report["units"]:
            assert unit["status"] == "error"
            diagnostic = unit["diagnostics"][0]
            assert diagnostic["code"] == "batch_job_failed"
            assert "finished 'failed' (boom)" in diagnostic["message"]
            assert "output recovery" in diagnostic["message"]

    def test_failed_job_prefers_a_collected_deck_report(self, tmp_path, monkeypatch):
        request = batch_deck_request(
            tmp_path, decks=[{"id": "u0", "deck": "deck.spice"}]
        )
        (tmp_path / "deck.spice").write_text("* stub-exit: 3\n", encoding="utf-8")
        install_fake_batch(
            monkeypatch, _FakeFleet({"state": "failed", "exit_code": "4"})
        )
        report = run_local(tmp_path, request, outdir="out")
        # The worker's own deck report (status error from the nonzero-exit
        # unit) beats the job's coarse `failed` state label.
        assert report["mode"] == "decks"
        assert report["status"] == "error"
        assert report["units"][0]["exit_code"] == 3

    def test_success_replaces_stale_unit_trees(self, tmp_path, monkeypatch):
        write_stub_engine(tmp_path)
        stale_unit = tmp_path / "out" / "tt_27c"
        stale_unit.mkdir(parents=True)
        (stale_unit / "stale.txt").write_text("old\n", encoding="utf-8")
        install_fake_batch(monkeypatch, _FakeFleet({"state": "done", "exit_code": "0"}))
        report = run_local(tmp_path, batch_deck_request(tmp_path), outdir="out")
        assert report["status"] == "pass"
        assert not (stale_unit / "stale.txt").exists()
        assert (stale_unit / "out" / "measure.raw").is_file()

    def test_failed_job_installs_the_collected_unit_trees(self, tmp_path, monkeypatch):
        """A collected deck report from a failed job is delivered with its
        unit trees in the local outdir (replacing any previous run's), so
        the report's unit-relative paths resolve to this job's evidence."""
        request = batch_deck_request(
            tmp_path, decks=[{"id": "u0", "deck": "deck.spice"}]
        )
        (tmp_path / "deck.spice").write_text("* stub-exit: 3\n", encoding="utf-8")
        stale_unit = tmp_path / "out" / "u0"
        stale_unit.mkdir(parents=True)
        (stale_unit / "stdout.bin").write_bytes(b"stale\n")
        (stale_unit / "stale.txt").write_bytes(b"stale\n")
        install_fake_batch(
            monkeypatch, _FakeFleet({"state": "failed", "exit_code": "4"})
        )
        report = run_local(tmp_path, request, outdir="out")
        assert report["units"][0]["exit_code"] == 3
        outdir = Path(report["outdir"])
        assert (outdir / report["units"][0]["stdout"]).read_bytes() == (
            b"ngspice-46 stub banner\n"
        )
        assert not (stale_unit / "stale.txt").exists()

    def test_failed_collection_clears_stale_unit_trees(self, tmp_path, monkeypatch):
        write_stub_engine(tmp_path)
        run_local(tmp_path, deck_request(tmp_path), outdir="out")
        outdir = tmp_path / "out"
        assert (outdir / "tt_27c" / "out" / "measure.raw").is_file()
        fleet = _FakeFleet({"state": "failed", "exit_code": "1"}, fail_collect=True)
        install_fake_batch(monkeypatch, fleet)
        report = run_local(tmp_path, batch_deck_request(tmp_path), outdir="out")
        assert report["status"] == "error"
        assert report["units"][0]["missing_outputs"] == ["out/measure.raw"]
        assert not (outdir / "tt_27c").exists()
        assert not (outdir / "ss_125c").exists()

    def test_stderr_error_envelope_reaches_the_units(self, tmp_path, monkeypatch):
        write_stub_engine(tmp_path)

        fleet = _FakeFleet({"state": "failed", "exit_code": "1"})

        def collect_no_deck_report(config, job_id, local_dir, **kwargs):
            with open(
                os.path.join(local_dir, sb.BATCH_STDERR_FILENAME),
                "w",
                encoding="utf-8",
            ) as handle:
                json.dump(
                    {
                        "schema_version": 1,
                        "error": {
                            "command": "sim",
                            "code": "bad_request",
                            "message": "the runner refused the request",
                        },
                    },
                    handle,
                )

        fleet.collect = collect_no_deck_report  # type: ignore[method-assign]
        install_fake_batch(monkeypatch, fleet)
        report = run_local(tmp_path, batch_deck_request(tmp_path), outdir="out")
        unit = report["units"][0]
        diagnostic = unit["diagnostics"][0]
        assert diagnostic["code"] == "batch_job_failed"
        assert diagnostic["runner_code"] == "bad_request"
        assert "the runner refused the request" in diagnostic["message"]

    def test_poll_timeout_reports_every_unit(self, tmp_path, monkeypatch):
        write_stub_engine(tmp_path)

        def poll_timeout(config, job_id, **kwargs):
            raise sb.BatchPollTimeout(
                "job did not reach a terminal state", last_state="running"
            )

        install_fake_batch(monkeypatch, _FakeFleet({}))
        monkeypatch.setattr(sb, "poll_status", poll_timeout)
        report = run_local(tmp_path, batch_deck_request(tmp_path), outdir="out")
        assert report["status"] == "error"
        assert all(
            u["diagnostics"][0]["code"] == "batch_poll_timeout" for u in report["units"]
        )

    def test_interrupted_reports_every_unit_with_interrupted_s(
        self, tmp_path, monkeypatch
    ):
        write_stub_engine(tmp_path)

        def interrupted(config, job_id, **kwargs):
            raise sb.BatchJobInterrupted("job stayed interrupted", interrupted_s=123.45)

        install_fake_batch(monkeypatch, _FakeFleet({}))
        monkeypatch.setattr(sb, "poll_status", interrupted)
        report = run_local(tmp_path, batch_deck_request(tmp_path), outdir="out")
        assert report["status"] == "error"
        assert all(
            u["diagnostics"][0]["code"] == sb.BATCH_JOB_INTERRUPTED_CODE
            for u in report["units"]
        )
        remote = report["environment"]["remote"]
        assert remote["interrupted_s"] == 123.5
        assert remote["state"] == "interrupted"

    def test_batch_error_raises_sim_error(self, tmp_path, monkeypatch):
        write_stub_engine(tmp_path)
        install_fake_batch(monkeypatch, _FakeFleet({"state": "done"}))

        def failing_submit(config, job_id, spec, **kwargs):
            raise sb.BatchError("no such bucket")

        monkeypatch.setattr(sb, "submit_job", failing_submit)
        with pytest.raises(sim.SimError, match="batch backend failed"):
            run_local(tmp_path, batch_deck_request(tmp_path), outdir="out")


# --------------------------------------------------------------------------- #
# run_sim / CLI integration
# --------------------------------------------------------------------------- #


class TestRunSimIntegration:
    def test_corner_flags_rejected_for_deck_requests(self, tmp_path):
        write_stub_engine(tmp_path)
        write_request(tmp_path, deck_request(tmp_path))
        request_path = str(tmp_path / "request.json")
        outdir = str(tmp_path / "out")
        with pytest.raises(sim.SimError, match="--hosts"):
            sim.run_sim(request_path, backend="local", hosts=2, artifacts_dir=outdir)
        with pytest.raises(sim.SimError, match="--resume"):
            sim.run_sim(
                request_path, backend="local", resume=True, artifacts_dir=outdir
            )
        with pytest.raises(sim.SimError, match="--budget-s"):
            sim.run_sim(
                request_path,
                backend="local",
                budget_s=60.0,
                artifacts_dir=outdir,
            )
        with pytest.raises(sim.SimError, match="--plot"):
            sim.run_sim(
                request_path,
                backend="local",
                plot_dir=outdir,
                artifacts_dir=outdir,
            )
        with pytest.raises(sim.SimError, match="--fail-fast-probe"):
            sim.run_sim(
                request_path,
                backend="local",
                fail_fast_probe=True,
                artifacts_dir=outdir,
            )
        with pytest.raises(sim.SimError, match="--fail-on-diagnostic"):
            sim.run_sim(
                request_path,
                backend="local",
                fail_on_diagnostic=("singular_matrix",),
                artifacts_dir=outdir,
            )

    def test_default_outdir_is_klt_sim_next_to_the_request(self, tmp_path):
        write_stub_engine(tmp_path)
        write_request(tmp_path, deck_request(tmp_path))
        report = sim.run_sim(str(tmp_path / "request.json"), backend="local")
        assert Path(report["outdir"]) == tmp_path / ".klt" / "sim"
        assert (Path(report["outdir"]) / "tt_27c" / "stdout.bin").is_file()

    def test_cli_exit_codes_and_text_renderer(self, tmp_path, monkeypatch, capsys):
        from klayout_tools.cli import sim_cmd

        write_stub_engine(tmp_path)
        write_request(tmp_path, deck_request(tmp_path))
        argv = argparse_namespace(str(tmp_path / "request.json"))

        def passing_report(*a, **k):
            return {
                "mode": "decks",
                "status": "pass",
                "unit_count": 1,
                "passed": 1,
                "errored": 0,
                "timed_out": 0,
                "units": [],
                "outdir": str(tmp_path),
                "environment": {"engine": "ngspice", "engine_version": "46"},
            }

        def failing_report(*a, **k):
            return {
                "mode": "decks",
                "status": "error",
                "unit_count": 1,
                "passed": 0,
                "errored": 1,
                "timed_out": 0,
                "units": [
                    {
                        "id": "u0",
                        "status": "error",
                        "exit_code": 1,
                        "duration_s": 0.1,
                        "stdout": "u0/stdout.bin",
                        "diagnostics": [
                            {
                                "severity": "error",
                                "code": "engine_exit",
                                "message": "ngspice exited with code 1",
                            }
                        ],
                        "missing_outputs": ["out/x.raw"],
                    }
                ],
                "outdir": str(tmp_path),
                "environment": {"engine": "ngspice", "engine_version": None},
            }

        # sim_cmd binds `run_sim` by value at import time, so patch it there.
        monkeypatch.setattr(sim_cmd, "run_sim", passing_report)
        assert sim_cmd.run(argv) == 0
        monkeypatch.setattr(sim_cmd, "run_sim", failing_report)
        assert sim_cmd.run(argv) == 4
        text = capsys.readouterr().out
        assert "mode: decks" in text
        assert "u0 [error] exit=1" in text
        assert "missing outputs: out/x.raw" in text


def argparse_namespace(request: str):
    import argparse

    return argparse.Namespace(
        request=request,
        outdir=None,
        backend="local",
        max_workers=None,
        hosts=None,
        budget_s=None,
        resume=None,
        plot=None,
        fail_fast_probe=None,
        fail_on_diagnostic=None,
        max_units_per_job=None,
        max_concurrent_jobs=None,
        op_lint=False,
        op_lint_corner=None,
        format="text",
    )


# --------------------------------------------------------------------------- #
# Runner decks[] capability gate (issue #3303)
# --------------------------------------------------------------------------- #


_DECK_VERSIONS = [
    ("0.7.0", False),
    ("0.7.0.dev12+gabc", False),
    ("0.6.9", False),
    ("0.7.1.dev306+gabc", False),
    ("0.7.1.dev307+gabc", True),
    ("0.7.1.dev500+g247e3c89b14b.dirty", True),
    ("0.7.1", True),
    ("0.7.2", True),
    ("0.8.0", True),
    ("0.8.0.dev3+gabc", True),
    ("1.0.0", True),
    ("7", None),
    ("", None),
]


@pytest.mark.parametrize(("version", "expected"), _DECK_VERSIONS)
def test_runner_supports_decks_rule(version, expected):
    assert sb._runner_supports_decks(version) is expected


def _run_deck_preflight(tmp_path: Path, version: str):
    import subprocess

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    klt = bin_dir / "klt"
    klt.write_text(f"#!/bin/sh\necho 'klt {version}'\n", encoding="utf-8")
    klt.chmod(0o755)
    outdir = tmp_path / "eda-out"
    outdir.mkdir(exist_ok=True)
    script = sb._runner_preflight_script("0.7.1.dev500", enforce=False)
    script += sb._deck_preflight_lines() + "echo reached-klt-sim\n"
    result = subprocess.run(
        ["bash", "-c", script],
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "EDA_OUTPUT_DIR": str(outdir),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result, outdir


@pytest.mark.parametrize(("version", "expected"), _DECK_VERSIONS)
def test_runner_shell_probe_matches_python_rule(tmp_path, version, expected):
    result, outdir = _run_deck_preflight(tmp_path, version)
    if expected:
        assert result.returncode == 0, result.stderr
        assert "reached-klt-sim" in result.stdout
    else:
        assert result.returncode == sb._BATCH_PREFLIGHT_REJECT_EXIT
        assert "reached-klt-sim" not in result.stdout
        envelope = json.loads((outdir / "report.json").read_text(encoding="utf-8"))
        assert envelope["error"]["code"] == "batch_runner_lacks_decks"


def _publish_runner(monkeypatch, *versions):
    images = [
        {
            "pdk": "sky130A",
            "region": "us-east-1",
            "ami_id": f"ami-{index}",
            "klt_version": version,
            "klt_ref": None,
        }
        for index, version in enumerate(versions)
    ]
    monkeypatch.setattr(sb, "published_runner_identities", lambda pdk, region: images)


class TestRunnerDecksGate:
    def test_known_old_runner_is_refused_before_any_submit(self, tmp_path, monkeypatch):
        write_stub_engine(tmp_path)
        _publish_runner(monkeypatch, "0.7.0")
        monkeypatch.setattr(sb._provenance, "_klt_version", lambda: "0.7.1.dev500")
        fleet = install_fake_batch(
            monkeypatch, _FakeFleet({"state": "done", "exit_code": "0"})
        )
        with pytest.raises(SimError) as raised:
            run_local(tmp_path, batch_deck_request(tmp_path), outdir="out")
        assert raised.value.code == "batch_runner_lacks_decks"
        assert fleet.spec is None

    def test_refused_even_under_warn(self, tmp_path, monkeypatch):
        write_stub_engine(tmp_path)
        _publish_runner(monkeypatch, "0.7.0")
        monkeypatch.setattr(sb._provenance, "_klt_version", lambda: "0.7.1.dev500")
        fleet = install_fake_batch(
            monkeypatch, _FakeFleet({"state": "done", "exit_code": "0"})
        )
        request = batch_deck_request(tmp_path)
        request["batch"]["runner_version_check"] = "warn"
        with pytest.raises(SimError) as raised:
            run_local(tmp_path, request, outdir="out")
        assert raised.value.code == "batch_runner_lacks_decks"
        assert fleet.spec is None

    def test_matching_runner_passes_and_probe_rides_the_command(
        self, tmp_path, monkeypatch
    ):
        write_stub_engine(tmp_path)
        _publish_runner(monkeypatch, "0.7.1.dev500+gabc")
        monkeypatch.setattr(sb._provenance, "_klt_version", lambda: "0.7.1.dev500")
        fleet = install_fake_batch(
            monkeypatch, _FakeFleet({"state": "done", "exit_code": "0"})
        )
        report = run_local(tmp_path, batch_deck_request(tmp_path), outdir="out")
        assert report["status"] == "pass"
        assert "batch_runner_lacks_decks" in fleet.spec.cmd

    def test_unknown_identity_defers_to_the_runner_probe(self, tmp_path, monkeypatch):
        write_stub_engine(tmp_path)
        _publish_runner(monkeypatch, None)
        fleet = install_fake_batch(
            monkeypatch, _FakeFleet({"state": "done", "exit_code": "0"})
        )
        run_local(tmp_path, batch_deck_request(tmp_path), outdir="out")
        assert fleet.spec is not None
        assert "batch_runner_lacks_decks" in fleet.spec.cmd

    def test_corner_style_envelope_gets_a_decks_diagnostic(self, tmp_path, monkeypatch):
        write_stub_engine(tmp_path)
        fleet = _FakeFleet({"state": "failed", "exit_code": "1"})

        def collect(config, job_id, local_dir, **kwargs):
            Path(local_dir, sb.BATCH_STDERR_FILENAME).write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "error": {
                            "command": "sim",
                            "code": "invalid_request",
                            "message": "request is missing required field: netlist",
                        },
                    }
                ),
                encoding="utf-8",
            )

        fleet.collect = collect
        install_fake_batch(monkeypatch, fleet)
        report = run_local(tmp_path, batch_deck_request(tmp_path), outdir="out")
        message = report["units"][0]["diagnostics"][0]["message"]
        assert "missing required field: netlist" in message
        assert "predates decks[] support" in message
