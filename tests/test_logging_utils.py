from loader.logging_utils import RunSummary


def test_reconciled_true_when_counts_add_up():
    summary = RunSummary.start(source="s", target="t", mode="upsert")
    summary.rows_read = 10
    summary.rows_loaded = 8
    summary.rows_rejected = 2
    summary.finish()

    assert summary.reconciled is True


def test_reconciled_false_when_counts_mismatch():
    summary = RunSummary.start(source="s", target="t", mode="upsert")
    summary.rows_read = 10
    summary.rows_loaded = 8
    summary.rows_rejected = 1  # engineered mismatch: 8 + 1 != 10

    summary.finish()

    assert summary.reconciled is False


def test_finish_sets_duration_and_timestamps():
    summary = RunSummary.start(source="s", target="t", mode="upsert")
    summary.finish()

    assert summary.ended_at is not None
    assert summary.duration_seconds is not None
    assert summary.duration_seconds >= 0


def test_deliberate_ci_failure_sanity_check():
    """Temporary: verifies CI actually fails the build on a broken test (task 4.4.4). Reverted immediately after confirming red."""
    assert 1 == 2
