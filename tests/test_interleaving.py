"""Concurrency: uploads interleaving with a gap check must be serialised.

A check computes inside one BEGIN IMMEDIATE transaction and records the batch
ids it actually read.  An upload racing it either commits before the snapshot
(batch id included, new row visible) or after it (batch id absent, and the
just-created check is expired).  There must never be a check that omits a
committed batch's effect while still being presented as current.
"""
import os
import sys
import tempfile
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def db_path(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("WATER_DATA_DIR", tmp)
    monkeypatch.setenv("WATER_DB_PATH", os.path.join(tmp, "test.db"))
    import importlib
    import app.config as config
    import app.database as database
    importlib.reload(config)
    importlib.reload(database)
    database.init_db()
    return os.path.join(tmp, "test.db")


def _conn(db_path):
    from app.database import get_connection
    return get_connection(db_path)


REPL = ("account,old_meter,new_meter,switched_at\n"
        "A1,OLD1,NEW1,2026-02-01T00:00:00Z\n")
PLAN = ("account,meter,planned_at\n"
        "A1,OLD1,2026-01-01T00:00:00Z\n"
        "A1,OLD1,2026-01-02T00:00:00Z\n")


def test_interleaved_upload_and_check_never_half_includes(db_path):
    from app.ingestion import ingest_readings, ingest_replacements
    from app.plan import CheckError, create_check, ingest_plan

    conn = _conn(db_path)
    ingest_replacements(conn, "r.csv", REPL)
    ingest_readings(conn, "r.csv",
                    "account,meter,read_at,reading_cum\n"
                    "A1,OLD1,2026-01-01T00:00:00Z,10\n")
    ingest_plan(conn, "p.csv", PLAN)
    conn.close()

    errors = []

    def checker():
        c = _conn(db_path)
        try:
            for _ in range(12):
                try:
                    create_check(c)
                except CheckError:
                    pass
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            c.close()

    def uploader():
        c = _conn(db_path)
        try:
            for _ in range(12):
                ingest_readings(c, "late.csv",
                                "account,meter,read_at,reading_cum\n"
                                "A1,OLD1,2026-01-02T00:10:00Z,20\n")
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=checker),
               threading.Thread(target=uploader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors

    c = _conn(db_path)
    try:
        rows = list(c.execute("SELECT * FROM checks ORDER BY id"))
        assert rows, "checks should have been created"
        active = [r for r in rows if r["status"] == "active"]
        assert len(active) <= 1
        reading_batches = {r["id"] for r in c.execute(
            "SELECT id FROM upload_batches WHERE batch_type='reading' AND status='committed'")}
        for check in rows:
            recorded = {int(x) for x in check["reading_batch_ids"].split(",")}
            # Every recorded batch existed at check time and was committed.
            assert recorded <= reading_batches
            results = list(c.execute(
                "SELECT status, match_reading_id FROM check_results WHERE check_id=?",
                (check["id"],)))
            matched = [r for r in results if r["status"] == "MATCHED"]
            if check["status"] == "active":
                # The current check must reflect the latest committed input,
                # i.e. it recorded every committed reading batch.
                assert recorded == reading_batches
                # Late reading has arrived, so both points are verifiable.
                assert len(matched) == 2
            # No reading is used twice inside a single check.
            ids = [r["match_reading_id"] for r in matched]
            assert len(ids) == len(set(ids))
    finally:
        c.close()


def test_upload_after_check_expires_it_and_restored_by_recheck(db_path):
    from app.ingestion import ingest_readings, ingest_replacements
    from app.plan import create_check, ingest_plan

    c = _conn(db_path)
    try:
        ingest_replacements(c, "r.csv", REPL)
        ingest_readings(c, "r.csv",
                        "account,meter,read_at,reading_cum\n"
                        "A1,OLD1,2026-01-01T00:00:00Z,10\n")
        ingest_plan(c, "p.csv", PLAN)
        first = create_check(c)["check_id"]

        # Duplicate-only upload (no new reading) still supersedes the check:
        # the input set was re-committed, so a fresh check is required.
        ingest_readings(c, "r.csv",
                        "account,meter,read_at,reading_cum\n"
                        "A1,OLD1,2026-01-01T00:00:00Z,10\n")
        status = c.execute("SELECT status FROM checks WHERE id=?", (first,)).fetchone()[0]
        assert status == "expired"

        second = create_check(c)["check_id"]
        assert c.execute("SELECT status FROM checks WHERE id=?", (second,)).fetchone()[0] == "active"
        assert c.execute("SELECT status FROM checks WHERE id=?", (first,)).fetchone()[0] == "expired"
    finally:
        c.close()
