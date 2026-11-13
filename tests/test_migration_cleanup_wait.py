"""The migration scratch teardown must wait for signalled backends, then fail boundedly."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from test_migration_project_model import SCRATCH, _wait_for_scratch_connections


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def fetchall(self):
        return self.rows


class _Activity:
    def __init__(self, observations):
        self.observations = iter(observations)
        self.queries = 0

    def execute(self, query, parameters):
        assert "pg_stat_activity" in query
        assert parameters == (SCRATCH,)
        self.queries += 1
        return _Rows(next(self.observations))


def test_teardown_waits_until_signalled_app_backend_disappears():
    activity = _Activity([
        [(123, "blocks_app")],
        [(123, "blocks_app")],
        [],
    ])
    with patch("test_migration_project_model.time.monotonic", return_value=0), \
         patch("test_migration_project_model.time.sleep") as sleep:
        _wait_for_scratch_connections(activity)
    assert activity.queries == 3
    assert sleep.call_count == 2


def test_teardown_rejects_persistent_backend_after_bound():
    activity = _Activity([[(456, "blocks_app")]])
    with patch("test_migration_project_model.time.monotonic", side_effect=[0, 5.1]):
        with pytest.raises(AssertionError, match="blocks_migration_drill still has 1 connection"):
            _wait_for_scratch_connections(activity)
    assert activity.queries == 1


def test_teardown_does_not_wait_when_scratch_has_no_connections():
    activity = _Activity([[]])
    with patch("test_migration_project_model.time.monotonic", return_value=0), \
         patch("test_migration_project_model.time.sleep") as sleep:
        _wait_for_scratch_connections(activity)
    assert activity.queries == 1
    sleep.assert_not_called()
