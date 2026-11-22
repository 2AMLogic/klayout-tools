"""The original results-parser imports remain available after the module split."""

from klayout_tools import functional_verification, functional_verification_results


def test_original_results_imports_reexport_each_moved_object():
    for name in (
        "parse_results_xml",
        "_executed_result_counts",
        "_SIM_TIME_UNIT_NS_EXPONENT",
        "_testcase_properties",
        "_maybe_float",
        "_testcase_sim_time_ns",
        "_extract_random_seed_property",
    ):
        assert getattr(functional_verification, name) is getattr(
            functional_verification_results, name
        )
