"""Both sizing modes diagnose invalid sampling before topology iteration settings."""

import pytest

from klayout_tools import size
from test_size import _base_topology_request, _write_models_lib, _write_request


@pytest.mark.parametrize("extra", [{}, {"max_joint_iterations": 0}])
def test_topology_rejects_invalid_sweep_first(tmp_path, extra):
    _write_models_lib(tmp_path)
    request = _base_topology_request(options={"sweep_points": 2, **extra})
    path = _write_request(tmp_path, request)
    with pytest.raises(size.SizeError, match="sweep_points"):
        size.run_size(str(path))
