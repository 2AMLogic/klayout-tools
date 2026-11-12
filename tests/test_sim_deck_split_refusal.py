"""Issue #5377: a deck-list request is one batch job, so the job-splitting
knobs (``--max-units-per-job`` / ``--max-concurrent-jobs`` and their
``batch.*`` request fields) are refused by name before any S3 write."""

from __future__ import annotations

import pytest

from klayout_tools import sim
from test_sim_decks import deck_request, write_request


class TestDeckSplitRefusal:
    """Issue #5377: a deck list is one job; split knobs are refused by name."""

    def _run(self, tmp_path, request, **kwargs):
        path = write_request(tmp_path, request)
        return sim.run_sim(str(path), artifacts_dir=str(tmp_path / "out"), **kwargs)

    @pytest.mark.parametrize(
        "kwargs",
        [{"max_units_per_job": 1}, {"max_concurrent_jobs": 1}],
    )
    def test_cli_flags_refused(self, tmp_path, kwargs):
        with pytest.raises(sim.SimError) as err:
            self._run(tmp_path, deck_request(tmp_path), **kwargs)
        assert err.value.code == "batch_decks_split_unsupported"

    @pytest.mark.parametrize("field", ["max_units_per_job", "max_concurrent_jobs"])
    def test_request_fields_refused(self, tmp_path, field):
        request = deck_request(tmp_path, batch={field: 2})
        with pytest.raises(sim.SimError) as err:
            self._run(tmp_path, request)
        assert err.value.code == "batch_decks_split_unsupported"
        assert f"batch.{field}" in str(err.value)

    def test_collect_partial_message_is_deck_aware(self, tmp_path):
        with pytest.raises(sim.SimError, match="deck-list requests") as err:
            self._run(tmp_path, deck_request(tmp_path), collect_partial=True)
        assert err.value.code == "collect_partial_requires_attach"

    def test_unset_runs_as_before(self, tmp_path):
        report = self._run(tmp_path, deck_request(tmp_path), backend="local")
        assert report
