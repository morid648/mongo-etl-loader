from datetime import datetime

from loader.state import WatermarkStore


def test_watermark_persists_across_simulated_restart(tmp_path):
    state_path = tmp_path / "watermark.json"
    value = datetime(2024, 6, 1, 12, 30, 0)

    store = WatermarkStore(state_path)
    assert store.get() is None
    store.set(value)

    reloaded_store = WatermarkStore(state_path)
    assert reloaded_store.get() == value


def test_watermark_updates_overwrite_previous_value(tmp_path):
    state_path = tmp_path / "watermark.json"
    store = WatermarkStore(state_path)

    store.set(datetime(2024, 1, 1))
    store.set(datetime(2024, 2, 1))

    assert WatermarkStore(state_path).get() == datetime(2024, 2, 1)
