from antmonitor.storage import Store


def test_raw_endpoint_deduplication_and_lossless_compression(tmp_path):
    store = Store(tmp_path)
    try:
        initial = {"metadata": {"universe": ["instrument"] * 5000}, "position": {"quantity": "1.25"}}
        store.raw(100, "hl", initial)
        store.raw(101, "hl", initial)
        rows = store.db.execute("SELECT value FROM raw").fetchall()
        assert len(rows) == 1
        assert len(rows[0][0]) < 1500
        assert Store.decode_raw(rows[0][0]) == initial
        store.raw(102, "hl", {**initial, "position": {"quantity": "2.5"}})
        rows = store.db.execute("SELECT value FROM raw ORDER BY id").fetchall()
        assert len(rows) == 2
        assert Store.decode_raw(rows[1][0]) == {"position": {"quantity": "2.5"}}
        store.close()
        store = Store(tmp_path)
        store.raw(103, "hl", {**initial, "position": {"quantity": "2.5"}})
        assert store.db.execute("SELECT count(*) FROM raw").fetchone()[0] == 2
    finally:
        store.close()


def test_history_scope_all_and_prune_keeps_strategy_history(tmp_path):
    import time
    store = Store(tmp_path)
    try:
        now = int(time.time() * 1000)
        old_history = now - 90 * 86400000
        recent_history = now - 60_000
        old_raw = now - 8 * 86400000
        store.sample(old_history, {"gross_spread_pct": 21})
        store.sample(recent_history, {"gross_spread_pct": 10})
        store.raw(old_raw, "okx", {"market": {"mark": 1}})
        assert [row["gross_spread_pct"] for row in store.history(None)] == [21, 10]
        assert [row["gross_spread_pct"] for row in store.history(1)] == [10]
        store.prune()
        assert [row["gross_spread_pct"] for row in store.history(None)] == [21, 10]
        assert store.db.execute("SELECT count(*) FROM raw").fetchone()[0] == 0
    finally:
        store.close()
