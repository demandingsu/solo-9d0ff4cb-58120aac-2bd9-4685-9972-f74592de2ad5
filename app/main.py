"""FastAPI application: uploads, generation, review and download."""
from __future__ import annotations

import csv
import io
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .compute import GenerationError, generate_generation, reason_label
from .database import get_connection, init_db
from .ingestion import BatchRejected, ingest_readings, ingest_replacements
from .plan import (
    CheckError,
    STATUS_LABELS,
    check_summary_sql,
    create_check,
    ingest_plan,
    record_rejected_plan,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="供水账户用量明细服务", lifespan=lifespan)
STATIC_DIR = Path(__file__).parent / "static"


class Message(BaseModel):
    ok: bool
    message: str
    detail: dict | None = None


def _text_of(file: UploadFile) -> str:
    raw = file.file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传文件为空")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return raw.decode("gb18030")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400,
                                detail="文件必须为 UTF-8（或 GB18030）编码的 CSV") from exc


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/api/uploads/readings", response_model=Message)
def upload_readings(file: UploadFile = File(...)):
    text = _text_of(file)
    conn = get_connection()
    try:
        try:
            detail = ingest_readings(conn, file.filename or "readings.csv", text)
        except BatchRejected as exc:
            raise HTTPException(status_code=422, detail=exc.reason) from exc
        return Message(ok=True,
                       message=f"读数上传成功：新增 {detail['inserted']} 条，"
                               f"同表同时同值重传跳过 {detail['duplicates_skipped']} 条",
                       detail=detail)
    finally:
        conn.close()


@app.post("/api/uploads/replacements", response_model=Message)
def upload_replacements(file: UploadFile = File(...)):
    text = _text_of(file)
    conn = get_connection()
    try:
        try:
            detail = ingest_replacements(conn, file.filename or "replacements.csv", text)
        except BatchRejected as exc:
            raise HTTPException(status_code=422, detail=exc.reason) from exc
        return Message(ok=True,
                       message=f"换表整版上传成功：载入 {detail['inserted']} 户，旧版换表数据已被替换",
                       detail=detail)
    finally:
        conn.close()


@app.post("/api/generations", response_model=Message)
def create_generation():
    conn = get_connection()
    try:
        try:
            detail = generate_generation(conn)
        except GenerationError as exc:
            # Business-level failure: keep the previous result, mark it failed/expired.
            _mark_active_failed(conn, str(exc))
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:  # pragma: no cover - defensive
            _mark_active_failed(conn, f"生成失败: {exc}")
            raise HTTPException(status_code=500, detail=f"生成失败: {exc}") from exc
        return Message(
            ok=True,
            message=f"第 {detail['generation_id']} 代结果已发布为现役："
                    f"共 {detail['total_rows']} 行（正常 {detail['ok_rows']}，待核 {detail['review_rows']}）",
            detail=detail,
        )
    finally:
        conn.close()


def _mark_active_failed(conn: sqlite3.Connection, detail: str) -> None:
    """A failed generation publishes nothing new. The previous result stays
    downloadable but is marked expired (with the failure reason recorded),
    so no row claims to be the current live result."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """UPDATE generations
                  SET status='expired',
                      status_detail=?,
                      superseded_at=COALESCE(superseded_at, ?)
                WHERE status='active'""",
            (f"后续生成失败，旧结果保留：{detail}", _now()),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _now() -> str:
    from .ingestion import now_utc_iso
    return now_utc_iso()


@app.get("/api/generations")
def list_generations():
    conn = get_connection()
    try:
        gens = [dict(r) for r in conn.execute(
            "SELECT * FROM generations ORDER BY id DESC")]
        for g in gens:
            g["row_count"] = conn.execute(
                "SELECT COUNT(*) FROM result_rows WHERE generation_id=?",
                (g["id"],)).fetchone()[0]
            g["review_count"] = conn.execute(
                "SELECT COUNT(*) FROM result_rows WHERE generation_id=? AND status='REVIEW'",
                (g["id"],)).fetchone()[0]
        return {"generations": gens}
    finally:
        conn.close()


@app.get("/api/batches")
def list_batches():
    conn = get_connection()
    try:
        return {
            "batches": [
                dict(r) for r in conn.execute(
                    "SELECT * FROM upload_batches ORDER BY id DESC")
            ]
        }
    finally:
        conn.close()


CSV_COLUMNS = [
    "generation_id", "account", "meter",
    "interval_start_utc", "interval_end_utc",
    "start_reading_m3", "end_reading_m3", "consumption_m3",
    "attributed_utc_day", "status", "review_reason", "review_reason_detail",
    "source_reading_ids",
]


def _generation_or_404(conn: sqlite3.Connection, generation_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM generations WHERE id=?",
                       (generation_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"不存在第 {generation_id} 代结果")
    return row


@app.get("/api/generations/latest/download")
def download_latest():
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT id FROM generations ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="尚未生成任何结果")
        gen_id = row["id"]
    finally:
        conn.close()
    return download_generation(gen_id)


@app.get("/api/generations/{generation_id}/download")
def download_generation(generation_id: int):
    conn = get_connection()
    try:
        gen = _generation_or_404(conn, generation_id)
        rows = conn.execute(
            "SELECT * FROM result_rows WHERE generation_id=? ORDER BY id",
            (generation_id,)).fetchall()

        def iter_csv():
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(CSV_COLUMNS)
            yield buffer.getvalue()
            for r in rows:
                buffer = io.StringIO()
                writer = csv.writer(buffer)
                writer.writerow([
                    generation_id, r["account"], r["meter"],
                    r["interval_start"] or "", r["interval_end"] or "",
                    "" if r["start_reading"] is None else r["start_reading"],
                    "" if r["end_reading"] is None else r["end_reading"],
                    "" if r["consumption_m3"] is None else r["consumption_m3"],
                    r["attributed_day"] or "",
                    r["status"],
                    r["review_reason"] or "",
                    reason_label(r["review_reason"]) or "",
                    r["source_reading_ids"],
                ])
                yield buffer.getvalue()

        headers = {
            "Content-Disposition":
                f'attachment; filename="usage_generation_{generation_id}.csv"',
            "X-Generation-Status": gen["status"],
        }
        return StreamingResponse(
            iter_csv(), media_type="text/csv; charset=utf-8", headers=headers)
    finally:
        conn.close()


@app.get("/api/generations/{generation_id}/rows")
def generation_rows(generation_id: int, limit: int = 1000):
    conn = get_connection()
    try:
        _generation_or_404(conn, generation_id)
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM result_rows WHERE generation_id=? ORDER BY id LIMIT ?",
            (generation_id, limit))]
        for r in rows:
            r["review_reason_detail"] = reason_label(r["review_reason"])
        return {"generation_id": generation_id, "rows": rows}
    finally:
        conn.close()


@app.post("/api/uploads/plans", response_model=Message)
def upload_plan(file: UploadFile = File(...)):
    text = _text_of(file)
    conn = get_connection()
    try:
        try:
            detail = ingest_plan(conn, file.filename or "plan.csv", text)
        except BatchRejected as exc:
            record_rejected_plan(conn, file.filename or "plan.csv", exc.reason)
            raise HTTPException(status_code=422, detail=exc.reason) from exc
        return Message(
            ok=True,
            message=f"抄表计划整版上传成功：载入 {detail['rows']} 个计划点，"
                    f"旧版计划已被替换；现有核查结论已标过期",
            detail=detail,
        )
    finally:
        conn.close()


@app.post("/api/checks", response_model=Message)
def create_gap_check():
    conn = get_connection()
    try:
        try:
            detail = create_check(conn)
        except CheckError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return Message(
            ok=True,
            message=f"核查 #{detail['check_id']} 完成：计划 {detail['total_points']} 点，"
                    f"已核销 {detail['matched']}，漏采 {detail['missed']}，"
                    f"错排 {detail['mis_scheduled']}，待核 {detail['pending_review']}",
            detail=detail,
        )
    finally:
        conn.close()


def _check_snapshot_state(conn: sqlite3.Connection, check: sqlite3.Row) -> tuple[bool, bool]:
    """Return (inputs_changed, stale) for a stored check.

    The check's recorded batch ids are the authoritative snapshot.  We also
    recompute the current committed batch ids so a missed expiry can never let
    an old conclusion masquerade as the current gap.
    """
    current_plan = conn.execute(
        """SELECT id FROM plan_batches
            WHERE status='committed' ORDER BY id DESC LIMIT 1""").fetchone()
    current_readings = [
        int(r["id"]) for r in conn.execute(
            """SELECT id FROM upload_batches
                WHERE batch_type='reading' AND status='committed' ORDER BY id""")
    ]
    current_repl = conn.execute(
        """SELECT id FROM upload_batches
            WHERE batch_type='replacement' AND status='committed'
            ORDER BY id DESC LIMIT 1""").fetchone()
    inputs_changed = (
        check["plan_batch_id"] != (current_plan["id"] if current_plan else None)
        or check["reading_batch_ids"] != ",".join(str(i) for i in current_readings)
        or check["replacement_batch_id"] != (current_repl["id"] if current_repl else None)
    )
    return bool(inputs_changed), check["status"] != "active" or inputs_changed


@app.get("/api/checks")
def list_checks():
    conn = get_connection()
    try:
        rows = [dict(r) for r in conn.execute(
            check_summary_sql() + " ORDER BY c.id DESC")]
        for r in rows:
            for key in ("total_points", "matched", "missed",
                        "mis_scheduled", "pending_review"):
                r[key] = r[key] or 0
            _, r["stale"] = _check_snapshot_state(conn, r)
        return {"checks": rows}
    finally:
        conn.close()


@app.get("/api/checks/{check_id}")
def check_detail(check_id: int):
    conn = get_connection()
    try:
        check = conn.execute(
            check_summary_sql() + " HAVING c.id=?", (check_id,)).fetchone()
        if check is None:
            raise HTTPException(status_code=404, detail=f"不存在核查 #{check_id}")
        inputs_changed, stale = _check_snapshot_state(conn, check)
        rows = [dict(r) for r in conn.execute(
            """SELECT * FROM check_results
                WHERE check_id=? ORDER BY planned_at, id""", (check_id,))]
        for r in rows:
            r["status_label"] = STATUS_LABELS.get(r["status"], r["status"])
        result = dict(check)
        for key in ("total_points", "matched", "missed",
                    "mis_scheduled", "pending_review"):
            result[key] = result[key] or 0
        result["inputs_changed"] = inputs_changed
        result["stale"] = stale
        return {"check": result, "results": rows}
    finally:
        conn.close()


@app.get("/api/checks/{check_id}/download")
def download_check(check_id: int):
    conn = get_connection()
    try:
        check = conn.execute(
            "SELECT * FROM checks WHERE id=?", (check_id,)).fetchone()
        if check is None:
            raise HTTPException(status_code=404, detail=f"不存在核查 #{check_id}")
        inputs_changed, stale = _check_snapshot_state(conn, check)
        rows = conn.execute(
            """SELECT * FROM check_results
                WHERE check_id=? ORDER BY planned_at, id""", (check_id,)).fetchall()

        def iter_csv():
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(["check_id", "check_status", "stale",
                             "plan_batch_id", "reading_batch_ids",
                             "replacement_batch_id",
                             "account", "meter", "planned_at_utc", "status",
                             "status_label", "match_reading_id",
                             "match_read_at_utc", "match_reading_cum",
                             "delta_seconds", "detail"])
            yield buffer.getvalue()
            for r in rows:
                buffer = io.StringIO()
                writer = csv.writer(buffer)
                writer.writerow([
                    check_id, check["status"], "yes" if stale else "no",
                    check["plan_batch_id"],
                    check["reading_batch_ids"],
                    "" if check["replacement_batch_id"] is None
                    else check["replacement_batch_id"],
                    r["account"], r["meter"], r["planned_at"], r["status"],
                    STATUS_LABELS.get(r["status"], r["status"]),
                    "" if r["match_reading_id"] is None else r["match_reading_id"],
                    r["match_read_at"] or "",
                    "" if r["match_reading_cum"] is None else r["match_reading_cum"],
                    "" if r["delta_seconds"] is None else r["delta_seconds"],
                    r["detail"] or "",
                ])
                yield buffer.getvalue()

        headers = {
            "Content-Disposition":
                f'attachment; filename="gap_check_{check_id}.csv"',
            "X-Check-Status": check["status"],
            "X-Check-Stale": "true" if stale else "false",
        }
        return StreamingResponse(
            iter_csv(), media_type="text/csv; charset=utf-8", headers=headers)
    finally:
        conn.close()


@app.get("/api/plan-points")
def list_plan_points(batch_id: int | None = None):
    """Read-only listing helper used by the UI when no check has been run."""
    conn = get_connection()
    try:
        if batch_id is None:
            row = conn.execute(
                """SELECT id FROM plan_batches
                    WHERE status='committed' ORDER BY id DESC LIMIT 1""").fetchone()
            batch_id = row["id"] if row else None
        if batch_id is None:
            return {"plan_batch": None, "points": []}
        batch = conn.execute("SELECT * FROM plan_batches WHERE id=?",
                             (batch_id,)).fetchone()
        if batch is None:
            raise HTTPException(status_code=404, detail=f"不存在计划批次 {batch_id}")
        points = [dict(r) for r in conn.execute(
            """SELECT id, account, meter, planned_at FROM plan_points
                WHERE batch_id=? ORDER BY planned_at, id""", (batch_id,))]
        return {"plan_batch": dict(batch), "points": points}
    finally:
        conn.close()


@app.get("/api/plans")
def list_plans():
    conn = get_connection()
    try:
        plans = [dict(r) for r in conn.execute(
            "SELECT * FROM plan_batches ORDER BY id DESC")]
        return {"plans": plans}
    finally:
        conn.close()


@app.get("/healthz")
def healthz():
    return {"ok": True}
