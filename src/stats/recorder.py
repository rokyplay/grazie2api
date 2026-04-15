"""StatsRecorder: async-friendly SQLite stats recorder."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("grazie2api.stats")

_STATS_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  credential_id TEXT NOT NULL,
  model TEXT NOT NULL,
  endpoint TEXT NOT NULL,
  status_code INTEGER,
  latency_ms INTEGER,
  input_tokens INTEGER,
  output_tokens INTEGER,
  error_code TEXT,
  quota_spent REAL
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts);
CREATE INDEX IF NOT EXISTS idx_requests_cred ON requests(credential_id);
"""


class StatsRecorder:
    """Async-friendly SQLite stats recorder.

    Writes happen in a background worker loop so the request path is not blocked.
    """

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._queue: asyncio.Queue[dict | None] = asyncio.Queue(maxsize=10000)
        self._worker_task: asyncio.Task | None = None
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        try:
            conn.executescript(_STATS_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    async def start(self) -> None:
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        if self._worker_task is not None:
            await self._queue.put(None)
            try:
                await asyncio.wait_for(self._worker_task, timeout=5)
            except asyncio.TimeoutError:
                self._worker_task.cancel()
            self._worker_task = None

    async def _worker(self) -> None:
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        try:
            while True:
                item = await self._queue.get()
                if item is None:
                    break
                try:
                    conn.execute(
                        "INSERT INTO requests (ts, credential_id, model, endpoint, status_code, latency_ms, input_tokens, output_tokens, error_code, quota_spent) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            item["ts"],
                            item["credential_id"],
                            item["model"],
                            item["endpoint"],
                            item.get("status_code"),
                            item.get("latency_ms"),
                            item.get("input_tokens"),
                            item.get("output_tokens"),
                            item.get("error_code"),
                            item.get("quota_spent"),
                        ),
                    )
                    conn.commit()
                except Exception as e:
                    log.error("Stats insert failed: %s", e)
        finally:
            conn.close()

    def record(
        self,
        credential_id: str,
        model: str,
        endpoint: str,
        status_code: int | None,
        latency_ms: int | None,
        input_tokens: int | None,
        output_tokens: int | None,
        error_code: str | None = None,
        quota_spent: float | None = None,
    ) -> None:
        try:
            self._queue.put_nowait({
                "ts": int(time.time()),
                "credential_id": credential_id,
                "model": model,
                "endpoint": endpoint,
                "status_code": status_code,
                "latency_ms": latency_ms,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "error_code": error_code,
                "quota_spent": quota_spent,
            })
        except asyncio.QueueFull:
            log.warning("Stats queue full, dropping record")

    # -- Read helpers (synchronous, using short-lived connections) --

    def _query(self, sql: str, params: tuple = ()) -> list[tuple]:
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.execute(sql, params)
            return cur.fetchall()
        finally:
            conn.close()

    def today_usage_map(self) -> dict[str, int]:
        start_of_day = int(time.time()) - (int(time.time()) % 86400)
        rows = self._query(
            "SELECT credential_id, COUNT(*) FROM requests WHERE ts >= ? GROUP BY credential_id",
            (start_of_day,),
        )
        return {cred_id: int(cnt) for cred_id, cnt in rows}

    def aggregate(self, hours: int = 24) -> dict[str, Any]:
        since = int(time.time()) - hours * 3600
        total_row = self._query(
            "SELECT COUNT(*), SUM(latency_ms), SUM(input_tokens), SUM(output_tokens), SUM(quota_spent) "
            "FROM requests WHERE ts >= ?",
            (since,),
        )
        total_cnt, total_lat, total_in, total_out, total_spent = (total_row[0] if total_row else (0, 0, 0, 0, 0))
        ok_row = self._query(
            "SELECT COUNT(*) FROM requests WHERE ts >= ? AND status_code BETWEEN 200 AND 299",
            (since,),
        )
        ok_cnt = int(ok_row[0][0] if ok_row else 0)
        by_cred = self._query(
            "SELECT credential_id, COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(quota_spent), "
            "SUM(CASE WHEN status_code BETWEEN 200 AND 299 THEN 1 ELSE 0 END) "
            "FROM requests WHERE ts >= ? GROUP BY credential_id",
            (since,),
        )
        by_model = self._query(
            "SELECT model, COUNT(*) FROM requests WHERE ts >= ? GROUP BY model ORDER BY 2 DESC LIMIT 20",
            (since,),
        )
        errors = self._query(
            "SELECT error_code, COUNT(*) FROM requests WHERE ts >= ? AND error_code IS NOT NULL GROUP BY error_code",
            (since,),
        )
        total_cnt_i = int(total_cnt or 0)
        avg_lat = int((total_lat or 0) / total_cnt_i) if total_cnt_i else 0
        return {
            "hours": hours,
            "total": total_cnt_i,
            "success": ok_cnt,
            "success_rate": (ok_cnt / total_cnt_i) if total_cnt_i else 0,
            "avg_latency_ms": avg_lat,
            "input_tokens": int(total_in or 0),
            "output_tokens": int(total_out or 0),
            "quota_spent": float(total_spent or 0),
            "by_credential": [
                {
                    "credential_id": cid,
                    "requests": int(cnt),
                    "input_tokens": int(in_t or 0),
                    "output_tokens": int(out_t or 0),
                    "quota_spent": float(spent_t or 0),
                    "success": int(okc or 0),
                }
                for cid, cnt, in_t, out_t, spent_t, okc in by_cred
            ],
            "by_model": [
                {"model": m, "requests": int(c)} for m, c in by_model
            ],
            "errors": [
                {"error_code": ec, "count": int(c)} for ec, c in errors
            ],
        }

    def recent_requests(self, limit: int = 50) -> list[dict]:
        rows = self._query(
            "SELECT id, ts, credential_id, model, endpoint, status_code, latency_ms, "
            "input_tokens, output_tokens, error_code, quota_spent "
            "FROM requests ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return [
            {
                "id": r[0],
                "ts": r[1],
                "credential_id": r[2],
                "model": r[3],
                "endpoint": r[4],
                "status_code": r[5],
                "latency_ms": r[6],
                "input_tokens": r[7],
                "output_tokens": r[8],
                "error_code": r[9],
                "quota_spent": r[10],
            }
            for r in rows
        ]
