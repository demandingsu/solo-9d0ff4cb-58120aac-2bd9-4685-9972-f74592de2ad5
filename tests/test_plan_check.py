"""Tests for planned-reading gap verification."""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture()
def client(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("WATER_DATA_DIR", tmp)
    monkeypatch.setenv("WATER_DB_PATH", os.path.join(tmp, "test.db"))
    import importlib
    import app.config as config
    import app.database as database
    import app.main as main
    importlib.reload(config)
    importlib.reload(database)
    importlib.reload(main)
    database.init_db()
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        yield c


REPL = (
    "account,old_meter,new_meter,switched_at\n"
    "A1,OLD1,NEW1,2026-01-05T00:00:00Z\n"
    "A2,OLD2,NEW2,2026-01-05T00:00:00Z\n"
)


def _upload(client, endpoint, content, name="x.csv"):
    return client.post(f"/api/uploads/{endpoint}",
                       files={"file": (name, content.encode("utf-8"), "text/csv")})


def _plan(client, content):
    return _upload(client, "plans", content, "plan.csv")


def _readings(client, content):
    return _upload(client, "readings", content, "readings.csv")


def _check(client):
    resp = client.post("/api/checks")
    assert resp.status_code == 200, resp.text
    return resp.json()["detail"]["check_id"]


def _results(client, check_id):
    body = client.get(f"/api/checks/{check_id}").json()
    return body["check"], {
        (r["account"], r["meter"], r["planned_at"]): r for r in body["results"]}


def _only(rows, planned_at):
    """Look up the single point at a timestamp (asserts uniqueness)."""
    found = [r for r in rows.values() if r["planned_at"] == planned_at]
    assert len(found) == 1, f"expected exactly one point at {planned_at}, got {found}"
    return found[0]


def test_plan_upload_requires_headers_and_valid_points(client):
    assert _upload(client, "plans", "a,b,c\n1,2,3\n").status_code == 422
    assert _plan(client, "account,meter,planned_at\nA1,OLD1,not-a-time\n").status_code == 422
    assert _plan(client, "account,meter,planned_at\nA1,OLD1,2026-01-01T00:00:00Z\n"
                              "A1,OLD1,2026-01-01T00:00:00Z\n").status_code == 422
    plans = client.get("/api/plans").json()["plans"]
    assert [p["status"] for p in plans] == ["rejected", "rejected", "rejected"]


def test_check_requires_plan_and_readings(client):
    assert client.post("/api/checks").status_code == 409  # no plan
    _plan(client, "account,meter,planned_at\nA1,OLD1,2026-01-01T00:00:00Z\n")
    assert client.post("/api/checks").status_code == 409  # no readings
    _readings(client, "account,meter,read_at,reading_cum\n"
                      "A1,OLD1,2026-01-01T00:00:00Z,10\n")
    assert client.post("/api/checks").status_code == 200


def test_matched_within_15_minutes_and_missed(client):
    _upload(client, "replacements", REPL)
    _readings(client, "account,meter,read_at,reading_cum\n"
                      "A1,OLD1,2026-01-01T00:00:00Z,10\n"
                      "A1,OLD1,2026-01-02T00:15:00Z,20\n"
                      "A1,OLD1,2026-01-03T00:00:00Z,30\n")
    _plan(client, "account,meter,planned_at\n"
                  "A1,OLD1,2026-01-01T00:00:00Z\n"     # exact match
                  "A1,OLD1,2026-01-02T00:00:00Z\n"     # 15 min late -> match at boundary
                  "A1,OLD1,2026-01-04T00:00:00Z\n")    # no reading -> missed
    cid = _check(client)
    check, rows = _results(client, cid)
    assert check["status"] == "active"
    assert check["matched"] == 2 and check["missed"] == 1
    r = _only(rows, "2026-01-02T00:00:00Z")
    assert r["status"] == "MATCHED"
    assert r["match_read_at"] == "2026-01-02T00:15:00Z"
    assert r["delta_seconds"] == 900
    assert _only(rows, "2026-01-04T00:00:00Z")["status"] == "MISSED"
    assert _only(rows, "2026-01-04T00:00:00Z")["match_reading_id"] is None


def test_reading_just_outside_tolerance_is_missed(client):
    _upload(client, "replacements", REPL)
    _readings(client, "account,meter,read_at,reading_cum\n"
                      "A1,OLD1,2026-01-01T00:15:01Z,10\n")
    _plan(client, "account,meter,planned_at\nA1,OLD1,2026-01-01T00:00:00Z\n")
    cid = _check(client)
    _, rows = _results(client, cid)
    assert _only(rows, "2026-01-01T00:00:00Z")["status"] == "MISSED"


def test_one_reading_verifies_only_one_point_and_overlap_is_maximised(client):
    # Planned points 1 min apart; only one reading exists.  A greedy matcher
    # could bind the reading to the first point, still leaving one missed, but
    # with a chain of overlap a greedy choice can hide a retrievable point.
    _upload(client, "replacements", REPL)
    _readings(client, "account,meter,read_at,reading_cum\n"
                      "A1,OLD1,2026-01-01T00:10:00Z,100\n")
    _plan(client, "account,meter,planned_at\n"
                  "A1,OLD1,2026-01-01T00:00:00Z\n"
                  "A1,OLD1,2026-01-01T00:20:00Z\n")
    cid = _check(client)
    check, rows = _results(client, cid)
    assert check["matched"] == 1 and check["missed"] == 1
    # The reading is equidistant; either point may win, but only one.
    used = [r for r in rows.values() if r["status"] == "MATCHED"]
    assert len(used) == 1 and used[0]["match_reading_id"] is not None


def test_overlapping_candidates_achievable_pairing_is_maximal(client):
    # Two readings, two planned points.  The early reading is within 15 min of
    # both points; the late reading only of the second.  Greedy (first point
    # takes its first candidate) would waste the late reading on point 1 if
    # ordering were naive; maximum matching must verify both points.
    _upload(client, "replacements", REPL)
    _readings(client, "account,meter,read_at,reading_cum\n"
                      "A1,OLD1,2026-01-01T00:00:00Z,100\n"
                      "A1,OLD1,2026-01-01T00:20:00Z,110\n")
    _plan(client, "account,meter,planned_at\n"
                  "A1,OLD1,2026-01-01T00:10:00Z\n"
                  "A1,OLD1,2026-01-01T00:25:00Z\n")
    cid = _check(client)
    check, rows = _results(client, cid)
    assert check["matched"] == 2 and check["missed"] == 0
    assert _only(rows, "2026-01-01T00:10:00Z")["match_read_at"] == "2026-01-01T00:00:00Z"
    assert _only(rows, "2026-01-01T00:25:00Z")["match_read_at"] == "2026-01-01T00:20:00Z"
    # Each reading used once.
    ids = [r["match_reading_id"] for r in rows.values()]
    assert len(ids) == len(set(ids)) == 2


def test_matching_requires_same_meter_and_account(client):
    _upload(client, "replacements", REPL)
    _readings(client, "account,meter,read_at,reading_cum\n"
                      "A1,NEW1,2026-01-01T00:00:00Z,5\n"
                      "A2,OLD2,2026-01-01T00:00:00Z,5\n")
    _plan(client, "account,meter,planned_at\n"
                  "A1,OLD1,2026-01-01T00:00:00Z\n"
                  "A1,NEW1,2026-01-01T00:00:00Z\n")
    cid = _check(client)
    _, rows = _results(client, cid)
    # A1 OLD1 planned point has no OLD1 reading (A2's reading is another account).
    assert rows[("A1", "OLD1", "2026-01-01T00:00:00Z")]["status"] == "MISSED"


def test_planned_point_outside_service_window_is_mis_scheduled(client):
    _upload(client, "replacements", REPL)
    _readings(client, "account,meter,read_at,reading_cum\n"
                      "A1,OLD1,2026-01-01T00:00:00Z,10\n")
    _plan(client, "account,meter,planned_at\n"
                  "A1,OLD1,2026-01-06T00:00:00Z\n"   # old meter after switch
                  "A1,NEW1,2026-01-04T00:00:00Z\n")  # new meter before switch
    cid = _check(client)
    check, rows = _results(client, cid)
    assert check["mis_scheduled"] == 2 and check["missed"] == 0
    assert all(r["status"] == "MIS_SCHEDULED" for r in rows.values())


def test_reading_outside_window_does_not_verify_in_window_plan(client):
    _upload(client, "replacements", REPL)
    # Reading on old meter after the switch: out of that meter's window.
    _readings(client, "account,meter,read_at,reading_cum\n"
                      "A1,OLD1,2026-01-06T00:00:00Z,10\n")
    _plan(client, "account,meter,planned_at\nA1,OLD1,2026-01-06T00:00:00Z\n")
    cid = _check(client)
    _, rows = _results(client, cid)
    # Both the point and the reading are out of window: mis-scheduled, not missed.
    assert _only(rows, "2026-01-06T00:00:00Z")["status"] == "MIS_SCHEDULED"


def test_out_of_window_reading_near_in_window_plan_is_not_a_match(client):
    _upload(client, "replacements", REPL)   # A1 switches 2026-01-05T00:00
    # Reading only 5 minutes after the plan, but already past the old meter's
    # service window, so it cannot verify the in-window planned point.
    _readings(client, "account,meter,read_at,reading_cum\n"
                      "A1,OLD1,2026-01-05T00:05:00Z,10\n")
    _plan(client, "account,meter,planned_at\nA1,OLD1,2026-01-04T23:58:00Z\n")
    cid = _check(client)
    check, rows = _results(client, cid)
    assert check["matched"] == 0 and check["missed"] == 1
    assert _only(rows, "2026-01-04T23:58:00Z")["status"] == "MISSED"


def test_unknown_meter_and_missing_replacement_are_not_missed(client):
    _upload(client, "replacements", REPL)
    _readings(client, "account,meter,read_at,reading_cum\n"
                      "A1,OLD1,2026-01-01T00:00:00Z,10\n"
                      "A9,ANY,2026-01-01T00:00:00Z,10\n")
    _plan(client, "account,meter,planned_at\n"
                  "A1,ZZZ,2026-01-01T00:00:00Z\n"     # meter unknown -> mis-scheduled
                  "A9,ANY,2026-01-01T00:00:00Z\n")    # account has no replacement record
    cid = _check(client)
    check, rows = _results(client, cid)
    assert check["mis_scheduled"] == 1 and check["pending_review"] == 1
    assert check["missed"] == 0
    assert rows[("A1", "ZZZ", "2026-01-01T00:00:00Z")]["status"] == "MIS_SCHEDULED"
    assert rows[("A9", "ANY", "2026-01-01T00:00:00Z")]["status"] == "PENDING_REVIEW"


def test_late_reading_expires_old_check_and_new_check_updates_gap(client):
    _upload(client, "replacements", REPL)
    _readings(client, "account,meter,read_at,reading_cum\n"
                      "A1,OLD1,2026-01-01T00:00:00Z,10\n")
    _plan(client, "account,meter,planned_at\n"
                  "A1,OLD1,2026-01-01T00:00:00Z\n"
                  "A1,OLD1,2026-01-02T00:00:00Z\n")
    c1 = _check(client)
    check, rows = _results(client, c1)
    assert check["status"] == "active"
    assert _only(rows, "2026-01-02T00:00:00Z")["status"] == "MISSED"

    # Backfill arrives -> the old check must be expired, never current gap data.
    _readings(client, "account,meter,read_at,reading_cum\n"
                      "A1,OLD1,2026-01-02T00:10:00Z,20\n")
    check, _ = _results(client, c1)
    assert check["status"] == "expired"
    assert check["stale"] is True

    c2 = _check(client)
    check2, rows2 = _results(client, c2)
    assert check2["status"] == "active"
    assert _only(rows2, "2026-01-02T00:00:00Z")["status"] == "MATCHED"
    # The old check remains readable/downloadable, flagged expired.
    assert _results(client, c1)[0]["status"] == "expired"
    dl = client.get(f"/api/checks/{c1}/download")
    assert dl.status_code == 200
    assert dl.headers["X-Check-Status"] == "expired"
    assert dl.headers["X-Check-Stale"] == "true"
    # The CSV itself labels every row as stale so a loose file cannot mislead.
    header = dl.text.splitlines()[0]
    assert "stale" in header
    assert dl.text.splitlines()[1].split(",")[2] == "yes"


def test_replacement_correction_expires_check(client):
    _upload(client, "replacements", REPL)
    _readings(client, "account,meter,read_at,reading_cum\n"
                      "A1,OLD1,2026-01-01T00:00:00Z,10\n")
    _plan(client, "account,meter,planned_at\nA1,OLD1,2026-01-01T00:00:00Z\n")
    c1 = _check(client)
    assert _results(client, c1)[0]["status"] == "active"

    _upload(client, "replacements",
            "account,old_meter,new_meter,switched_at\n"
            "A1,OLD1,NEW1,2025-12-31T00:00:00Z\n")
    assert _results(client, c1)[0]["status"] == "expired"

    c2 = _check(client)
    check, rows = _results(client, c2)
    # Corrected switch makes the January old-meter plan mis-scheduled.
    assert _only(rows, "2026-01-01T00:00:00Z")["status"] == "MIS_SCHEDULED"
    assert check["replacement_batch_id"] != _results(client, c1)[0]["replacement_batch_id"]


def test_plan_reupload_expires_check_and_swaps_points(client):
    _upload(client, "replacements", REPL)
    _readings(client, "account,meter,read_at,reading_cum\n"
                      "A1,OLD1,2026-01-01T00:00:00Z,10\n")
    _plan(client, "account,meter,planned_at\nA1,OLD1,2026-01-01T00:00:00Z\n")
    c1 = _check(client)
    assert _results(client, c1)[0]["status"] == "active"

    _plan(client, "account,meter,planned_at\nA1,OLD1,2026-01-03T00:00:00Z\n")
    assert _results(client, c1)[0]["status"] == "expired"
    c2 = _check(client)
    check, rows = _results(client, c2)
    assert check["plan_batch_id"] != _results(client, c1)[0]["plan_batch_id"]
    assert [key[2] for key in rows] == ["2026-01-03T00:00:00Z"]
    assert _only(rows, "2026-01-03T00:00:00Z")["status"] == "MISSED"


def test_check_records_input_batches_used(client):
    _upload(client, "replacements", REPL)
    _readings(client, "account,meter,read_at,reading_cum\n"
                      "A1,OLD1,2026-01-01T00:00:00Z,10\n")
    _plan(client, "account,meter,planned_at\nA1,OLD1,2026-01-01T00:00:00Z\n")
    cid = _check(client)
    check, _ = _results(client, cid)
    assert check["plan_batch_id"] == client.get("/api/plans").json()["plans"][0]["id"]
    reading_batches = [b["id"] for b in client.get("/api/batches").json()["batches"]
                       if b["batch_type"] == "reading"]
    assert check["reading_batch_ids"] == ",".join(str(i) for i in reading_batches)
    repl_batch = [b["id"] for b in client.get("/api/batches").json()["batches"]
                  if b["batch_type"] == "replacement"][0]
    assert check["replacement_batch_id"] == repl_batch


def test_fractional_second_timestamps_compare_as_instants(client):
    # "00Z" sorts lexically before "00.5Z", so a string comparison would treat
    # a reading 0.5 s BEFORE the switch as in-window on the old meter.
    _upload(client, "replacements",
            "account,old_meter,new_meter,switched_at\n"
            "A1,OLD1,NEW1,2026-01-05T00:00:00.500Z\n")
    _readings(client, "account,meter,read_at,reading_cum\n"
                      "A1,OLD1,2026-01-05T00:00:00Z,10\n"
                      "A1,OLD1,2026-01-05T00:00:00.750Z,11\n"
                      "A1,NEW1,2026-01-14T00:00:00.250Z,0\n")
    _plan(client, "account,meter,planned_at\n"
                  "A1,OLD1,2026-01-05T00:00:00Z\n"      # in old-meter window
                  "A1,OLD1,2026-01-06T00:00:00Z\n"      # after switch -> mis-scheduled
                  "A1,NEW1,2026-01-14T00:00:00Z\n")     # in new-meter window
    cid = _check(client)
    check, rows = _results(client, cid)
    assert _only(rows, "2026-01-05T00:00:00Z")["status"] == "MATCHED"
    assert _only(rows, "2026-01-06T00:00:00Z")["status"] == "MIS_SCHEDULED"
    # .250 s offset is well within tolerance: fractional times still match.
    new_plan = _only(rows, "2026-01-14T00:00:00Z")
    assert new_plan["status"] == "MATCHED"
    assert new_plan["delta_seconds"] == 0.25
    assert check["matched"] == 2 and check["mis_scheduled"] == 1


def test_duplicate_points_cannot_double_use_one_reading(client):
    _upload(client, "replacements", REPL)
    _readings(client, "account,meter,read_at,reading_cum\n"
                      "A1,OLD1,2026-01-01T12:00:00Z,10\n")
    _plan(client, "account,meter,planned_at\n"
                  "A1,OLD1,2026-01-01T12:00:00Z\n"
                  "A1,OLD1,2026-01-01T12:10:00Z\n"
                  "A1,OLD1,2026-01-01T12:20:00Z\n"
                  "A1,OLD1,2026-01-01T12:30:00Z\n")
    cid = _check(client)
    check, _ = _results(client, cid)
    # One reading can verify only one of the four points.
    assert check["matched"] == 1 and check["missed"] == 3


def test_check_download_marks_status(client):
    _upload(client, "replacements", REPL)
    _readings(client, "account,meter,read_at,reading_cum\n"
                      "A1,OLD1,2026-01-01T00:00:00Z,10\n")
    _plan(client, "account,meter,planned_at\nA1,OLD1,2026-01-01T00:00:00Z\n")
    cid = _check(client)
    dl = client.get(f"/api/checks/{cid}/download")
    assert dl.status_code == 200
    assert dl.headers["X-Check-Status"] == "active"
    assert dl.headers["X-Check-Stale"] == "false"
    lines = dl.text.strip().splitlines()
    assert lines[0].startswith("check_id,check_status,stale,plan_batch_id")
    assert lines[1].split(",")[2] == "no"
    assert "已核销" in dl.text
