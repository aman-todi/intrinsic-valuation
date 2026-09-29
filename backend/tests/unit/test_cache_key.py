from app.runs.cache_key import compute_cache_key

BASE = ("AAPL", "fcff", "0000320193-25-000079", "v1", "v1")


def test_any_input_change_changes_key() -> None:
    base = compute_cache_key(*BASE)
    assert compute_cache_key(*BASE) == base  # deterministic
    for i, alt in enumerate(["MSFT", "fcfe", "0000320193-26-000001", "v2", "v2"]):
        args = list(BASE)
        args[i] = alt
        assert compute_cache_key(*args) != base
