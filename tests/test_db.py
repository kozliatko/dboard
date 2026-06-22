"""Tests for SQLite persistence layer in main.py."""
import time
import pytest
from collections import deque


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """Redirect DB_PATH to a temp file and reset all global state before each test."""
    import main

    monkeypatch.setattr(main, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(main, "_db_conn", None)

    for k in list(main._spark.keys()):
        main._spark[k] = deque(maxlen=main._SPARK_N)
    main._container_spark.clear()

    yield

    if main._db_conn:
        main._db_conn.close()
        monkeypatch.setattr(main, "_db_conn", None)


# ── Schema creation ───────────────────────────────────────────────────────────

def test_db_init_creates_tables():
    import main
    main._db_init()
    conn = main._db()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "sys_metrics" in tables
    assert "container_metrics" in tables


def test_db_init_is_idempotent():
    import main
    main._db_init()
    main._db_init()  # second call must not raise


# ── sys_metrics writes ────────────────────────────────────────────────────────

def test_write_sys_inserts_row():
    import main
    main._db_init()
    main._db_write_sys(int(time.time()), 42.5, 61.0, None, None, None)
    count = main._db().execute("SELECT count(*) FROM sys_metrics").fetchone()[0]
    assert count == 1


def test_write_sys_stores_correct_values():
    import main
    main._db_init()
    ts = int(time.time())
    main._db_write_sys(ts, 55.5, 70.0, 48.2,
                       {"rx_bps": 1000, "tx_bps": 500},
                       {"read_bps": 200, "write_bps": 100})
    row = main._db().execute(
        "SELECT cpu, mem, temp, net_rx, net_tx, disk_r, disk_w FROM sys_metrics"
    ).fetchone()
    assert row == (55.5, 70.0, 48.2, 1000, 500, 200, 100)


def test_write_sys_handles_none_rates():
    import main
    main._db_init()
    main._db_write_sys(int(time.time()), 10.0, 30.0, None, None, None)
    row = main._db().execute(
        "SELECT net_rx, net_tx, disk_r, disk_w FROM sys_metrics"
    ).fetchone()
    assert row == (None, None, None, None)


# ── container_metrics writes ──────────────────────────────────────────────────

def test_write_containers_inserts_rows():
    import main
    main._db_init()
    entries = [
        {"name": "web", "cpu_percent": 5.1, "mem_percent": 20.0},
        {"name": "db",  "cpu_percent": 1.2, "mem_percent": 45.0},
    ]
    main._db_write_containers(entries)
    count = main._db().execute("SELECT count(*) FROM container_metrics").fetchone()[0]
    assert count == 2


def test_write_containers_stores_name_and_stats():
    import main
    main._db_init()
    main._db_write_containers([{"name": "api", "cpu_percent": 12.3, "mem_percent": 55.0}])
    row = main._db().execute(
        "SELECT name, cpu, mem FROM container_metrics"
    ).fetchone()
    assert row == ("api", 12.3, 55.0)


# ── Persistence: data survives restart simulation ─────────────────────────────

def test_sys_sparklines_restored_after_reinit():
    import main

    main._db_init()
    ts = int(time.time())
    for i in range(5):
        main._db_write_sys(ts + i, float(10 + i), float(40 + i), None, None, None)

    # Simulate restart: reset in-memory state, re-init from DB
    for k in main._spark:
        main._spark[k] = deque(maxlen=main._SPARK_N)
    main._db_conn.close()
    main._db_conn = None

    main._db_init()

    assert list(main._spark["cpu"]) == [10.0, 11.0, 12.0, 13.0, 14.0]
    assert list(main._spark["mem"]) == [40.0, 41.0, 42.0, 43.0, 44.0]


def test_container_sparklines_restored_after_reinit():
    import main

    main._db_init()
    ts = int(time.time())
    for i in range(3):
        main._db_write_containers([
            {"name": "web", "cpu_percent": float(i * 10), "mem_percent": float(i * 5)},
        ])
        # Space rows 1s apart so ORDER BY ts works correctly
        time.sleep(1.1)

    main._container_spark.clear()
    main._db_conn.close()
    main._db_conn = None

    main._db_init()

    assert "web" in main._container_spark
    assert list(main._container_spark["web"]["cpu"]) == [0.0, 10.0, 20.0]


# ── Pruning ───────────────────────────────────────────────────────────────────

def test_prune_removes_old_rows():
    import main
    main._db_init()
    old_ts = int(time.time()) - main._DB_RETENTION - 10
    main._db().execute(
        "INSERT INTO sys_metrics(ts, cpu, mem) VALUES(?, ?, ?)", (old_ts, 5.0, 30.0)
    )
    main._db().commit()
    assert main._db().execute("SELECT count(*) FROM sys_metrics").fetchone()[0] == 1

    main._db_prune_sync()

    assert main._db().execute("SELECT count(*) FROM sys_metrics").fetchone()[0] == 0


def test_prune_keeps_recent_rows():
    import main
    main._db_init()
    recent_ts = int(time.time()) - 60
    main._db().execute(
        "INSERT INTO sys_metrics(ts, cpu, mem) VALUES(?, ?, ?)", (recent_ts, 5.0, 30.0)
    )
    main._db().commit()

    main._db_prune_sync()

    assert main._db().execute("SELECT count(*) FROM sys_metrics").fetchone()[0] == 1
