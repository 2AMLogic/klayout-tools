"""The `environment.parameters` echo across the functional-verification
surfaces (issue #5697): the resolved `request.parameters` map is echoed
verbatim as `environment.parameters` (`{}` when omitted) so a stored
envelope states which parameterization it is evidence for.

Covers the cocotb run, the `--mutations` baseline report, and (when
`iverilog` is installed) the `testbench.verilog_top` path, where the echo
is always `{}` -- a non-empty `request.parameters` is rejected upstream.
"""

from klayout_tools.functional_verification import (
    run_functional_verification,
    run_functional_verification_with_mutations,
)
from test_functional_verification import (
    _MUTATION_BASELINE_XML,
    _MUTATION_DUT_RTL,
    _MUTATION_KILLED_XML,
    _RESULTS_XML_WITH_SKIP,
    _TESTBENCH,
    _base_request,
    _ContentAwareFakeRunner,
    _FakeRunner,
    _setup_inputs,
    _stub_mutation_runner,
    _stub_runner,
    _write,
    _write_mutation_proposals,
    _write_request,
)
from test_functional_verification_verilog_top import (
    _rewrite_bench,
    _stage,
    needs_icarus,
)


def test_stubbed_run_forwards_parameters_and_echoes_them(tmp_path, monkeypatch):
    """`request.parameters` -> `Runner.build(parameters=...)` /
    `Runner.test(parameters=...)`, echoed verbatim as
    `environment.parameters` (issue #5697) -- every scalar kind
    (integer, float, string, boolean) round-trips unchanged, so a stored
    envelope states which parameterization it is evidence for."""
    _setup_inputs(tmp_path)
    parameters = {"WIDTH": 8, "GAIN": 1.5, "NAME": "dut", "SYNC": True}
    request_path = _write_request(
        tmp_path / "request.json", _base_request(parameters=parameters)
    )
    runner = _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))

    response = run_functional_verification(request_path)

    assert runner.build_kwargs["parameters"] == parameters
    assert runner.test_kwargs["parameters"] == parameters
    assert response["environment"]["parameters"] == parameters


def test_stubbed_run_without_parameters_echoes_an_empty_mapping(tmp_path, monkeypatch):
    _setup_inputs(tmp_path)
    request_path = _write_request(tmp_path / "request.json", _base_request())
    runner = _stub_runner(monkeypatch, _FakeRunner(_RESULTS_XML_WITH_SKIP))

    response = run_functional_verification(request_path)

    assert runner.build_kwargs["parameters"] == {}
    assert response["environment"]["parameters"] == {}


def test_mutations_report_echoes_request_parameters(tmp_path, monkeypatch):
    """The `--mutations` response is the baseline report with the additive
    `mutation_testing` block, so its `environment.parameters` echo states
    the parameterization every mutant was compared under (issue #5697)."""
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "dut.v", _MUTATION_DUT_RTL)
    _write(tmp_path / "test_dut.py", _TESTBENCH)
    request_path = _write_request(
        tmp_path / "request.json",
        _base_request(
            sources=["dut.v"],
            hdl_toplevel="dut",
            testbench={"module": "test_dut", "testcase": None},
            parameters={"WIDTH": 8},
        ),
    )
    proposals_path = _write_mutation_proposals(tmp_path)
    _stub_mutation_runner(
        monkeypatch,
        _ContentAwareFakeRunner(
            _MUTATION_BASELINE_XML, _MUTATION_KILLED_XML, "wire a = 9;"
        ),
    )

    report = run_functional_verification_with_mutations(request_path, proposals_path)

    assert report["status"] == "pass"
    assert report["environment"]["parameters"] == {"WIDTH": 8}


@needs_icarus
def test_integration_verilog_top_echoes_an_empty_parameters_mapping(tmp_path):
    """The verilog-top path always echoes `{}`: a non-empty
    `request.parameters` is rejected with `testbench.verilog_top` upstream,
    but the field is present so the `environment` shape is uniform across
    both testbench kinds (issue #5697)."""
    _stage(tmp_path)
    _rewrite_bench(
        tmp_path,
        '    if (errors == 0) $display("PASS");',
        '    if (errors == 0 && $test$plusargs("go") && '
        '$value$plusargs("file=%s", f)) $display("PASS %0s", f);',
    )
    _rewrite_bench(
        tmp_path, "integer errors = 0;", "integer errors = 0; reg [255:0] f;"
    )
    absent = run_functional_verification(str(tmp_path / "request-rtl.json"))
    assert absent["status"] == "fail"
    assert absent["environment"]["plusargs"] == []
    assert absent["environment"]["parameters"] == {}
