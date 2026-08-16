"""Shared cocotb-runner test double (issue #994).

`FakeCocotbRunner` stands in for a `cocotb_tools.runner.Runner` -- the
`build()`/`test()` object `klayout_tools.functional_verification` drives --
without invoking any real simulator. It records the exact kwargs the library
passed, writes whatever `results.xml`/log text a test asks for, and can
inject a `build()`/`test()` exception to exercise error paths.

Originally defined in `tests/test_functional_verification.py` (24+ call
sites) with a stripped-down, behaviorally-equivalent duplicate in
`tests/test_digital_flow_eval_trajectory.py` (single call site). Both now
import this shared definition.
"""

from __future__ import annotations

import os
from pathlib import Path


class FakeCocotbRunner:
    """Stands in for a `cocotb_tools.runner.Runner`, recording the exact
    kwargs the library passed and writing whatever `results.xml` the test
    asked for."""

    def __init__(
        self,
        results_xml_text,
        *,
        build_exc=None,
        test_exc=None,
        coverage_dat_text=None,
        extra_build_log="",
        extra_test_log="",
    ):
        self.results_xml_text = results_xml_text
        self.build_exc = build_exc
        self.test_exc = test_exc
        # Appended to the respective transcript, so a test can stand in for
        # simulator chatter the library itself scans (issue #1002's own
        # `SDF WARNING`/`SDF ERROR` gate).
        self.extra_build_log = extra_build_log
        self.extra_test_log = extra_test_log
        # When set, `test()` writes a fresh `coverage.dat` into `test_dir` --
        # standing in for Verilator flushing coverage data as part of *this*
        # run, the way `results_xml_text` stands in for `results.xml`.
        self.coverage_dat_text = coverage_dat_text
        self.build_kwargs = None
        self.test_kwargs = None

    def build(self, **kwargs):
        self.build_kwargs = kwargs
        Path(kwargs["build_dir"]).mkdir(parents=True, exist_ok=True)
        with open(kwargs["log_file"], "w", encoding="utf-8") as handle:
            handle.write("fake build log\nELABORATION DETAIL\n")
            handle.write(self.extra_build_log)
        if self.build_exc is not None:
            raise self.build_exc

    def test(self, **kwargs):
        self.test_kwargs = kwargs
        with open(kwargs["log_file"], "w", encoding="utf-8") as handle:
            handle.write("fake test log\n")
            handle.write(self.extra_test_log)
        if self.results_xml_text is not None:
            with open(kwargs["results_xml"], "w", encoding="utf-8") as handle:
                handle.write(self.results_xml_text)
        if self.coverage_dat_text is not None:
            coverage_dat = os.path.join(kwargs["test_dir"], "coverage.dat")
            with open(coverage_dat, "w", encoding="utf-8") as handle:
                handle.write(self.coverage_dat_text)
        if self.test_exc is not None:
            raise self.test_exc
