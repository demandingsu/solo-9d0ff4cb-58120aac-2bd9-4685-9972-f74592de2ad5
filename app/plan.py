"""Planned meter-reading import and missing-reading verification.

Business rules implemented here (in addition to CSV validation):

* A plan point is dispatched (错排 / mis-scheduled) when the account has a
  replacement record but the planned meter/time falls outside that meter's
  service window.  It is *not* a missed reading.
* When the account has no replacement record at all, the service window cannot
  be established, so the point is held for review (待核 / pending review) and
  is *not* a missed reading either.
* A committed reading can verify a plan point only when it is for the same
  account AND same meter, lies inside that meter's service window, and its
  timestamp differs from the planned time by at most 15 minutes.
* One reading can verify at most one plan point.  When candidates overlap the
  assignment maximises the number of verified points (maximum bipartite
  matching), so a greedy first-come match cannot hide a retrievable point.
* Remaining in-window, unverified points are the true missed readings (漏采).

A check is a snapshot of committed inputs: its plan batch, reading batches and
replacement batch are recorded, and later committed uploads (or a newer check)
expire it so an old conclusion is never presented as the current gap.
"""
from __future__ import annotations

import csv
import io
import sqlite3
from dataclasses import dataclass

from .ingestion import BatchRejected, format_iso_utc, now_utc_iso, parse_iso_utc
PLAN_HEADERS = ["account", "meter", "planned_at"]
MATCH_TOLERANCE_SECONDS = 15 * 60

STATUS_MATCHED = "MATCHED"
STATUS_MISSED = "MISSED"
STATUS_MIS_SCHEDULED = "MIS_SCHEDULED"
STATUS_PENDING_REVIEW = "PENDING_REVIEW"

STATUS_LABELS = {
    STATUS_MATCHED: "已核销",
    STATUS_MISSED: "漏采",
    STATUS_MIS_SCHEDULED: "错排",
    STATUS_PENDING_REVIEW: "待核",
}

# Machine-readable detail codes (shown alongside the human label in the UI/CSV).
DETAIL_MISSED = "no_reading_within_15_minutes"
DETAIL_OUTSIDE_WINDOW = "planned_outside_meter_service_window"
DETAIL_UNKNOWN_METER = "planned_meter_not_in_replacement_record"
DETAIL_NO_REPLACEMENT = "no_replacement_record"


class CheckError(Exception):
    """A verification cannot be run (e.g. no plan or no committed readings)."""


@dataclass(frozen=True)
class PlanPoint:
    account: str
    meter: str
    planned_at: str


def parse_plan_csv(text: str) -> list[PlanPoint]:
    rows = [r for r in csv.reader(io.StringIO(text))]
    while rows and not any(cell.strip() for cell in rows[-1]):
        rows.pop()
    if not rows:
        raise BatchRejected("计划 CSV 文件为空")
    header = [h.strip() for h in rows[0]]
    if header != PLAN_HEADERS:
        raise BatchRejected(
            f"表头必须为 {','.join(PLAN_HEADERS)}，"
            f"实际为 {','.join(header) if header else '(空)'}"
        )
    parsed: list[PlanPoint] = []
    seen: set[tuple[str, str, str]] = set()
    for line_no, row in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue
        if len(row) != len(PLAN_HEADERS):
            raise BatchRejected(
                f"第 {line_no} 行字段数应为 {len(PLAN_HEADERS)}，实际为 {len(row)}"
            )
        account, meter, raw_ts = (c.strip() for c in row)
        if not account or not meter:
            raise BatchRejected(f"第 {line_no} 行 account/meter 不能为空")
        try:
            ts = format_iso_utc(parse_iso_utc(raw_ts))
        except ValueError as exc:
            raise BatchRejected(f"第 {line_no} 行应采时刻无效: {exc}") from exc
        key = (account, meter, ts)
        if key in seen:
            raise BatchRejected(
                f"第 {line_no} 行与批内更早的计划点重复（账户 {account} 表 {meter} 时刻 {ts}）"
            )
        seen.add(key)
        parsed.append(PlanPoint(account, meter, ts))
    if not parsed:
        raise BatchRejected("CSV 不含任何有效计划点")
    return parsed


def expire_active_checks(conn: sqlite3.Connection, detail: str | None = None) -> None:
    """Expire the current check because committed inputs changed."""
    conn.execute(
        """UPDATE checks
              SET status = 'expired',
                  status_detail = COALESCE(?, status_detail),
                  superseded_at = ?
            WHERE status = 'active'""",
        (detail, now_utc_iso()),
    )


def ingest_plan(conn: sqlite3.Connection, filename: str, text: str) -> dict:
    points = parse_plan_csv(text)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            """INSERT INTO plan_batches (filename, status, row_count, reject_reason, created_at)
               VALUES (?, 'committed', ?, NULL, ?)""",
            (filename, len(points), now_utc_iso()),
        )
        batch_id = int(cur.lastrowid)
        created = now_utc_iso()
        conn.executemany(
            """INSERT INTO plan_points (batch_id, account, meter, planned_at, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            [(batch_id, p.account, p.meter, p.planned_at, created) for p in points],
        )
        expire_active_checks(conn, "计划整版已更新，旧核查结论过期")
        conn.commit()
    except BatchRejected:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    return {"batch_id": batch_id, "rows": len(points)}


def record_rejected_plan(conn: sqlite3.Connection, filename: str, reason: str) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """INSERT INTO plan_batches (filename, status, row_count, reject_reason, created_at)
               VALUES (?, 'rejected', 0, ?, ?)""",
            (filename, reason, now_utc_iso()),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


@dataclass
class CheckRow:
    plan_point_id: int
    account: str
    meter: str
    planned_at: str
    status: str
    match_reading_id: int | None = None
    match_read_at: str | None = None
    match_reading_cum: float | None = None
    delta_seconds: float | None = None
    detail: str | None = None


def _load_replacements(conn: sqlite3.Connection) -> dict[str, dict]:
    result: dict[str, dict] = {}
    for row in conn.execute(
        "SELECT account, old_meter, new_meter, switched_at FROM replacements"
    ):
        result[row["account"]] = {
            "old_meter": row["old_meter"],
            "new_meter": row["new_meter"],
            "switched_at": row["switched_at"],
        }
    return result


def _window_for(repl: dict, meter: str):
    """Return an in-window predicate on datetime values (None = unknown meter).

    Parsed datetimes are compared, never ISO strings: timestamps with and
    without fractional seconds ("...00Z" vs "...00.5Z") do not sort lexically
    in the same order as instants.
    """
    switch = parse_iso_utc(repl["switched_at"])
    if meter == repl["old_meter"]:
        return lambda ts: ts <= switch
    if meter == repl["new_meter"]:
        return lambda ts: ts >= switch
    return None


def _seconds_between(a: str, b: str) -> float:
    delta = parse_iso_utc(a) - parse_iso_utc(b)
    return round(delta.total_seconds(), 6)


def _maximum_matching(
    eligible: list[sqlite3.Row],
    candidates: dict[int, list[sqlite3.Row]],
) -> dict[int, sqlite3.Row]:
    """Kuhn's augmenting-path algorithm: maximum plan-point/reading matching.

    ``candidates`` maps plan_point_id -> candidate readings sorted by time
    distance.  Every reading is used at most once; the returned map is
    point id -> reading.  A greedy assignment would be wrong here because one
    reading can lie within 15 minutes of several planned points.
    """
    match_point: dict[int, int] = {}           # reading id -> point id
    readings_by_id = {r["id"]: r for rs in candidates.values() for r in rs}

    def augment(start: int) -> bool:
        # Explicit stack instead of recursion, so deep overlap chains are safe.
        frames: list[dict] = [{"point": start, "index": 0, "via": None}]
        visited_points = {start}
        while frames:
            frame = frames[-1]
            options = candidates.get(frame["point"], [])
            descended = False
            while frame["index"] < len(options):
                reading_id = options[frame["index"]]["id"]
                frame["index"] += 1
                if reading_id in match_point:
                    other = match_point[reading_id]
                    if other in visited_points:
                        continue
                    visited_points.add(other)
                    frames.append({"point": other, "index": 0,
                                   "via": reading_id})
                    descended = True
                    break
                # Free reading reached: flip the whole alternating path.
                match_point[reading_id] = frame["point"]
                for depth in range(len(frames) - 1, 0, -1):
                    match_point[frames[depth]["via"]] = frames[depth - 1]["point"]
                return True
            if not descended:
                frames.pop()
        return False

    for point in eligible:
        augment(point["id"])

    return {pid: readings_by_id[rid] for rid, pid in match_point.items()}


def compute_check(conn: sqlite3.Connection, plan_batch_id: int) -> list[CheckRow]:
    replacements = _load_replacements(conn)
    points = list(conn.execute(
        """SELECT id, account, meter, planned_at
             FROM plan_points
            WHERE batch_id = ?
         ORDER BY planned_at, id""",
        (plan_batch_id,),
    ))
    readings = list(conn.execute(
        """SELECT id, account, meter, read_at, reading_cum
             FROM readings
         ORDER BY read_at, id"""
    ))

    by_key: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for reading in readings:
        by_key.setdefault((reading["account"], reading["meter"]), []).append(reading)

    results: dict[int, CheckRow] = {}
    eligible: list[sqlite3.Row] = []
    candidates: dict[int, list[sqlite3.Row]] = {}
    window_cache: dict[tuple[str, str], object] = {}

    for point in points:
        repl = replacements.get(point["account"])
        if repl is None:
            results[point["id"]] = CheckRow(
                plan_point_id=point["id"], account=point["account"],
                meter=point["meter"], planned_at=point["planned_at"],
                status=STATUS_PENDING_REVIEW, detail=DETAIL_NO_REPLACEMENT,
            )
            continue
        cache_key = (point["account"], point["meter"])
        if cache_key not in window_cache:
            window_cache[cache_key] = _window_for(repl, point["meter"])
        in_window = window_cache[cache_key]
        if in_window is None:
            results[point["id"]] = CheckRow(
                plan_point_id=point["id"], account=point["account"],
                meter=point["meter"], planned_at=point["planned_at"],
                status=STATUS_MIS_SCHEDULED, detail=DETAIL_UNKNOWN_METER,
            )
            continue
        if not in_window(parse_iso_utc(point["planned_at"])):
            results[point["id"]] = CheckRow(
                plan_point_id=point["id"], account=point["account"],
                meter=point["meter"], planned_at=point["planned_at"],
                status=STATUS_MIS_SCHEDULED, detail=DETAIL_OUTSIDE_WINDOW,
            )
            continue

        # Both the plan point and the actual reading must be in the service
        # window, so out-of-window readings can never verify a point.
        viable = []
        for r in by_key.get((point["account"], point["meter"]), []):
            if not in_window(parse_iso_utc(r["read_at"])):
                continue
            delta = _seconds_between(r["read_at"], point["planned_at"])
            if abs(delta) <= MATCH_TOLERANCE_SECONDS:
                viable.append((abs(delta), r["read_at"], r["id"], r))
        viable.sort(key=lambda item: item[:3])
        eligible.append(point)
        candidates[point["id"]] = [item[3] for item in viable]

    matched = _maximum_matching(eligible, candidates)

    for point in eligible:
        reading = matched.get(point["id"])
        if reading is None:
            results[point["id"]] = CheckRow(
                plan_point_id=point["id"], account=point["account"],
                meter=point["meter"], planned_at=point["planned_at"],
                status=STATUS_MISSED, detail=DETAIL_MISSED,
            )
        else:
            results[point["id"]] = CheckRow(
                plan_point_id=point["id"], account=point["account"],
                meter=point["meter"], planned_at=point["planned_at"],
                status=STATUS_MATCHED,
                match_reading_id=reading["id"],
                match_read_at=reading["read_at"],
                match_reading_cum=reading["reading_cum"],
                delta_seconds=_seconds_between(reading["read_at"], point["planned_at"]),
                detail=None,
            )

    return [results[p["id"]] for p in points]


def _committed_reading_batch_ids(conn: sqlite3.Connection) -> list[int]:
    return [
        int(r["id"]) for r in conn.execute(
            """SELECT id FROM upload_batches
                WHERE batch_type='reading' AND status='committed' ORDER BY id"""
        )
    ]


def _latest_plan_batch_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        """SELECT id FROM plan_batches
            WHERE status='committed' ORDER BY id DESC LIMIT 1"""
    ).fetchone()
    return int(row["id"]) if row else None


def _latest_replacement_batch_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        """SELECT id FROM upload_batches
            WHERE batch_type='replacement' AND status='committed'
            ORDER BY id DESC LIMIT 1"""
    ).fetchone()
    return int(row["id"]) if row else None


def create_check(conn: sqlite3.Connection) -> dict:
    """Run one verification over a single snapshot of committed inputs."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        plan_batch_id = _latest_plan_batch_id(conn)
        if plan_batch_id is None:
            raise CheckError("尚无任何已提交的抄表计划，无法核查")
        reading_batch_ids = _committed_reading_batch_ids(conn)
        if not reading_batch_ids:
            raise CheckError("尚无任何已提交的读数，无法核查")

        repl_batch_id = _latest_replacement_batch_id(conn)
        rows = compute_check(conn, plan_batch_id)

        cur = conn.execute(
            """INSERT INTO checks
                   (status, status_detail, plan_batch_id, reading_batch_ids,
                    replacement_batch_id, created_at, superseded_at)
               VALUES ('active', NULL, ?, ?, ?, ?, NULL)""",
            (plan_batch_id, ",".join(str(i) for i in reading_batch_ids),
             repl_batch_id, now_utc_iso()),
        )
        check_id = int(cur.lastrowid)
        conn.executemany(
            """INSERT INTO check_results
                   (check_id, plan_point_id, account, meter, planned_at, status,
                    match_reading_id, match_read_at, match_reading_cum,
                    delta_seconds, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (check_id, x.plan_point_id, x.account, x.meter, x.planned_at,
                 x.status, x.match_reading_id, x.match_read_at,
                 x.match_reading_cum, x.delta_seconds, x.detail)
                for x in rows
            ],
        )
        # A newer check supersedes the previous active one.
        conn.execute(
            """UPDATE checks
                  SET status='expired', superseded_at=?,
                      status_detail=COALESCE(status_detail, '已由更新的核查取代')
                WHERE status='active' AND id<>?""",
            (now_utc_iso(), check_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    counts = {status: 0 for status in STATUS_LABELS}
    for x in rows:
        counts[x.status] += 1
    return {
        "check_id": check_id,
        "plan_batch_id": plan_batch_id,
        "reading_batch_ids": reading_batch_ids,
        "replacement_batch_id": repl_batch_id,
        "total_points": len(rows),
        "matched": counts[STATUS_MATCHED],
        "missed": counts[STATUS_MISSED],
        "mis_scheduled": counts[STATUS_MIS_SCHEDULED],
        "pending_review": counts[STATUS_PENDING_REVIEW],
    }


def check_summary_sql() -> str:
    """Aggregate counts for the check list endpoint."""
    return """
        SELECT c.*, pb.filename AS plan_filename,
               COUNT(cr.id) AS total_points,
               SUM(CASE WHEN cr.status='MATCHED' THEN 1 ELSE 0 END) AS matched,
               SUM(CASE WHEN cr.status='MISSED' THEN 1 ELSE 0 END) AS missed,
               SUM(CASE WHEN cr.status='MIS_SCHEDULED' THEN 1 ELSE 0 END)
                   AS mis_scheduled,
               SUM(CASE WHEN cr.status='PENDING_REVIEW' THEN 1 ELSE 0 END)
                   AS pending_review
          FROM checks c
          JOIN plan_batches pb ON pb.id = c.plan_batch_id
          LEFT JOIN check_results cr ON cr.check_id = c.id
         GROUP BY c.id
    """
