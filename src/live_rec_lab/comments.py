"""コメント収集（録画から独立）。有限キュー・バッチ保存・重複排除・終了時ドレイン・欠損記録。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import Engine, update
from sqlalchemy.dialects.sqlite import insert

from live_rec_lab import sessions
from live_rec_lab.db.models import comment_collection_jobs, comments
from live_rec_lab.platforms.base import AccessRestricted, CommentEvent, CommentStream
from live_rec_lab.runtime import utcnow

log = logging.getLogger("live_rec_lab.comments")
QUEUE_MAX = 5000
BATCH = 200
FLUSH_S = 0.5


class CommentCollector:
    """1配信セッションのコメント収集ジョブ。factory() で受信ストリームを（再）作成する。"""

    def __init__(self, engine: Engine, session_id: int, job_id: int, budget_id: int | None,
                 factory: Callable[[], CommentStream], started_at: datetime | None, *,
                 on_budget: Callable[[int, str], str | None] | None = None, retry_wait: float = 3.0,
                 on_restricted: Callable[[str], None] | None = None) -> None:
        self.engine, self.session_id, self.job_id, self.budget_id = engine, session_id, job_id, budget_id
        self.factory = factory
        self.stream_started_at = started_at
        self.on_budget = on_budget  # (budget_id, attempt_id) -> 'initial'/'extra'/None
        self.on_restricted = on_restricted  # アクセス制限の確定を監督へ通知（録画も停止させる）
        self.retry_wait = retry_wait
        self.queue: asyncio.Queue[CommentEvent | None] = asyncio.Queue(QUEUE_MAX)
        self.stats = dict(received=0, stored=0, duplicate=0, discarded=0, save_failed=0, errors=0, reconnects=0)
        self.last_received: str | None = None
        self.last_durable: str | None = None
        self.last_error: str | None = None
        self.counts_complete = True
        self._stop = asyncio.Event()
        self._writer: asyncio.Task | None = None
        self._reader: asyncio.Task | None = None
        self.end_reason: str | None = None
        self.final_status: str | None = None

    # ---- lifecycle
    async def start(self) -> None:
        await asyncio.to_thread(self._set_job, status="CONNECTING")
        self._writer = asyncio.create_task(self._write_loop(), name=f"comments-writer-{self.session_id}")
        self._reader = asyncio.create_task(self._read_loop(), name=f"comments-reader-{self.session_id}")

    async def stop(self, reason: str = "session_end", *, timeout: float = 20.0) -> None:
        """受信を止め、キューを排出し、最終状態を確定する。"""
        self._stop.set()
        self.end_reason = self.end_reason or reason
        if self._reader:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
        await self.queue.put(None)
        if self._writer:
            try:
                await asyncio.wait_for(self._writer, timeout)
            except TimeoutError:
                self._writer.cancel()
                self.counts_complete = False
                self.last_error = "キュー排出がタイムアウトしました"
        await asyncio.to_thread(self._finish)

    @property
    def done(self) -> bool:
        return self.final_status is not None

    # ---- reader
    async def _read_loop(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            attempt += 1
            if self.on_budget and self.budget_id is not None:
                kind = await asyncio.to_thread(self.on_budget, self.budget_id, f"c{self.session_id}-{attempt}")
                if kind is None:
                    self.end_reason = "retry_exhausted"
                    self.last_error = self.last_error or "再試行予算を使い切りました"
                    # 予算枯渇は受信側の終端。ジョブを終端状態へ確定する（受信キューは排出される）
                    asyncio.get_running_loop().call_soon(lambda: asyncio.create_task(self.stop("retry_exhausted")))
                    return
                if kind == "extra":
                    self.stats["reconnects"] += 1
            gap_start = utcnow() if attempt > 1 else None
            stream = None
            try:
                stream = self.factory()
                await asyncio.to_thread(self._set_job, status="COLLECTING")
                if gap_start:
                    await asyncio.to_thread(self._gap, self._last_gap_start or gap_start, utcnow(), "reconnected")
                    self._last_gap_start = None
                async for ev in stream:
                    self.stats["received"] += 1
                    self.last_received = utcnow()
                    try:
                        self.queue.put_nowait(ev)
                    except asyncio.QueueFull:
                        self.stats["discarded"] += 1
                        self.counts_complete = False
                if self._stop.is_set():
                    return
                self.end_reason = "receiver_ended"
            except asyncio.CancelledError:
                raise
            except AccessRestricted as exc:  # 制限は回避せず、再接続も候補切替もしない
                self.end_reason = "restricted"
                self.last_error = f"アクセス制限: {exc.reason}"
                log.warning("コメント収集を停止: アクセス制限（%s）", exc.reason, extra={"session_id": self.session_id})
                if self.on_restricted:
                    self.on_restricted(exc.reason)
                return
            except Exception as exc:  # 受信例外は上位（ここ）で記録して再接続判断
                self.stats["errors"] += 1
                self.last_error = f"{type(exc).__name__}: {exc}"[:500]
                self.end_reason = "receiver_error"
                log.warning("コメント受信が終了: %s", self.last_error, extra={"session_id": self.session_id})
            finally:
                if stream is not None:
                    try:
                        await stream.aclose()
                    except Exception:
                        pass
            self._last_gap_start = utcnow()
            await asyncio.to_thread(self._set_job, status="RECONNECTING")
            self._fail_streak = self._fail_streak + 1 if self.end_reason in ("receiver_error",) else 0
            wait = min(120.0, self.retry_wait * (2 ** min(self._fail_streak, 6)))  # 外部が拒否/空応答のとき連打しない
            try:
                await asyncio.wait_for(self._stop.wait(), wait)
                return
            except TimeoutError:
                pass

    _last_gap_start: str | None = None
    _fail_streak = 0

    # ---- writer
    async def _write_loop(self) -> None:
        batch: list[CommentEvent] = []
        closing = False
        while not closing:
            try:
                item = await asyncio.wait_for(self.queue.get(), FLUSH_S)
                if item is None:
                    closing = True
                else:
                    batch.append(item)
                    while len(batch) < BATCH and not self.queue.empty():
                        nxt = self.queue.get_nowait()
                        if nxt is None:
                            closing = True
                            break
                        batch.append(nxt)
            except TimeoutError:
                pass
            if batch and (closing or len(batch) >= BATCH or self.queue.empty()):
                await self._save(batch)
                batch = []
        if batch:
            await self._save(batch)

    async def _save(self, batch: list[CommentEvent]) -> None:
        for attempt in range(3):
            try:
                stored, dup = await asyncio.to_thread(self._insert, batch)
                self.stats["stored"] += stored
                self.stats["duplicate"] += dup
                self.last_durable = utcnow()
                await asyncio.to_thread(self._flush_stats)
                return
            except Exception as exc:
                self.last_error = f"保存失敗: {type(exc).__name__}: {exc}"[:500]
                await asyncio.sleep(0.5 * (attempt + 1))
        self.stats["save_failed"] += len(batch)
        self.counts_complete = False

    # ---- DB (別スレッド・短い取引)
    def _insert(self, batch: list[CommentEvent]) -> tuple[int, int]:
        now = utcnow()
        stored = dup = 0
        with self.engine.begin() as conn:
            for ev in batch:
                eff = ev.posted_at or ev.received_at
                eff_s = sessions.fmt_ts(eff)
                off = None
                if self.stream_started_at is not None:
                    off = int((eff - self.stream_started_at).total_seconds() * 1000)
                stmt = insert(comments).values(
                    stream_session_id=self.session_id, platform_comment_id=ev.platform_comment_id,
                    platform_user_id=ev.platform_user_id, nickname=ev.nickname, is_anonymous=ev.is_anonymous,
                    anonymous_label=ev.anonymous_label, comment_source=ev.source, message=ev.message,
                    posted_at=sessions.fmt_ts(ev.posted_at), received_at=sessions.fmt_ts(ev.received_at),
                    effective_at=eff_s, stream_offset_ms=off, created_at=now)
                if ev.platform_comment_id is not None:
                    stmt = stmt.on_conflict_do_nothing()
                res = conn.execute(stmt)
                if res.rowcount:
                    stored += 1
                else:
                    dup += 1
                    if ev.anonymous_label:  # 重複取得で匿名番号が後から得られた場合だけ補完する（上書きしない）
                        conn.execute(update(comments).where(
                            comments.c.stream_session_id == self.session_id,
                            comments.c.platform_comment_id == ev.platform_comment_id,
                            comments.c.anonymous_label.is_(None)).values(anonymous_label=ev.anonymous_label))
        return stored, dup

    def _flush_stats(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(update(comment_collection_jobs).where(comment_collection_jobs.c.id == self.job_id).values(
                received_count=self.stats["received"], stored_count=self.stats["stored"],
                duplicate_count=self.stats["duplicate"], discarded_count=self.stats["discarded"],
                save_failed_count=self.stats["save_failed"], error_count=self.stats["errors"],
                reconnect_count=self.stats["reconnects"], last_received_at=self.last_received,
                last_durable_at=self.last_durable, counts_complete=self.counts_complete, last_error=self.last_error,
                updated_at=utcnow()))

    def _set_job(self, **vals) -> None:
        with self.engine.begin() as conn:
            sessions.update_job(conn, comment_collection_jobs, self.job_id, **vals)

    def _gap(self, start: str, end: str, reason: str) -> None:
        with self.engine.begin() as conn:
            sessions.add_gap(conn, self.session_id, "comments", start, end, reason, start_basis="approximate",
                             end_basis="approximate")

    def _finish(self) -> None:
        if self.stats["save_failed"] or not self.counts_complete or self.end_reason in ("receiver_error", "retry_exhausted", "restricted"):
            status = "PARTIAL" if self.stats["stored"] else "FAILED"
        else:
            status = "COMPLETED"
        self.final_status = status
        self._flush_stats()
        with self.engine.begin() as conn:
            sessions.update_job(conn, comment_collection_jobs, self.job_id, status=status, finished_at=utcnow(),
                                last_error=self.last_error or (None if status == "COMPLETED" else self.end_reason))
            if self._last_gap_start and not self._stop.is_set():
                pass
