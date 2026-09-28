from app.runs.cache_key import compute_cache_key

BASE = ("AAPL", "fcff", "0000320193-25-000079", "v1", "v1")


def test_same_inputs_same_key() -> None:
    assert compute_cache_key(*BASE) == compute_cache_key(*BASE)
    assert len(compute_cache_key(*BASE)) == 32


def test_any_input_change_changes_key() -> None:
    base = compute_cache_key(*BASE)
    for i, alt in enumerate(["MSFT", "fcfe", "0000320193-26-000001", "v2", "v2"]):
        args = list(BASE)
        args[i] = alt
        assert compute_cache_key(*args) != base
