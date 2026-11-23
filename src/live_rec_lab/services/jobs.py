"""監視、録画、コメント収集の統括。"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import shutil
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from live_rec_lab.comments import srt
from live_rec_lab.comments.store import save_comments
from live_rec_lab.config import load_paths, load_secrets, utcnow
from live_rec_lab.db.models import (
    CollectionGap,
    Comment,
    CommentCollectionJob,
    RecordingJob,
    RecordingSegment,
    RuntimeInstance,
    Streamer,
    StreamSession,
)
from live_rec_lab.db.queries import add_event, load_settings, seed_settings
from live_rec_lab.db.session import session_scope, upgrade_database
from live_rec_lab.monitoring.scheduler import ServiceSchedule, service_interval
from live_rec_lab.monitoring.state import ACTIVE_RECORDING, OPEN_SESSION
from live_rec_lab.ops.logging_config import configure_logging, set_log_level
from live_rec_lab.ops.recovery import recover
from live_rec_lab.platforms.base import CommentIn, Snapshot
from live_rec_lab.platforms.tiktok import CommentConnectionError, TikTokAdapter
from live_rec_lab.platforms.twitcasting import TwitCastingAdapter
from live_rec_lab.recording.manager import RecordingManager
from live_rec_lab.services.retention import deletion_pending, resume_operations, run_retention
from live_rec_lab.services.sessions import Intent, apply_snapshot, end_session, job_intents, open_sessions
from live_rec_lab.storage import ensure_recording_root

log = logging.getLogger("live_rec_lab.jobs")
_RATES = {"tiktok": "monitor_per_minute_tiktok", "twitcasting": "monitor_per_minute_twitcasting"}
_COMMENT_BATCH = 50


class MonitorDetails:
    """同じ確認失敗の連打を抑え、最後の発生と回復を追跡する。"""

    def __init__(self) -> None:
        self._failures: dict[int, tuple[str, datetime, datetime, int]] = {}
        self.recovered: dict[int, tuple[str, datetime, datetime, int]] = {}

    def notice(self, streamer_id: int, status: str, detail: str | None) -> str | None:
        now = utcnow()
        if status in {"LIVE", "OFFLINE"}:
            failure = self._failures.pop(streamer_id, None)
            if failure is not None:
                self.recovered[streamer_id] = failure
            return detail or None
        if not detail:
            return None
        previous = self._failures.get(streamer_id)
        if previous and previous[0] == detail:
            self._failures[streamer_id] = (detail, previous[1], now, previous[3] + 1)
            return None
        self._failures[streamer_id] = (detail, now, now, 1)
        return detail


@dataclass
class _CommentRun:
    stop: asyncio.Event = field(default_factory=asyncio.Event)
    stop_reason: str | None = None
    healthy: bool = False
    any_connected: bool = False
    gap_started: datetime | None = None
    gap_reason: str | None = None
    had_gap: bool = False


class Coordinator:
    def __init__(
        self,
        tiktok: TikTokAdapter | None = None,
        twitcasting: TwitCastingAdapter | None = None,
    ) -> None:
        secrets = load_secrets()
        self.paths = load_paths()
        self.tiktok = tiktok or TikTokAdapter()
        self.twitcasting = twitcasting or TwitCastingAdapter(
            secrets.twitcasting_client_id,
            secrets.twitcasting_client_secret,
        )
        self.recorder = RecordingManager()
        self.comment_tasks: dict[int, asyncio.Task[None]] = {}
        self._comment_runs: dict[int, _CommentRun] = {}
        self._comment_retry_at: dict[int, float] = {}
        self._schedule = ServiceSchedule()
        self._checking: set[int] = set()
        self._check_tasks: set[asyncio.Task[None]] = set()
        self._last_live: dict[int, Snapshot] = {}
        self._starting_recording: set[int] = set()
        self._recording_block: dict[int, str] = {}
        self.stop = asyncio.Event()
        self.instance_id = str(uuid.uuid4())
        self.started_at = utcnow()
        self._retention_at = 0.0
        self._retention_ready = True
        self._retention_task: asyncio.Task[bool] | None = None
        self._global_comments: bool | None = None
        self.monitor_details = MonitorDetails()

    @property
    def retention_ready(self) -> bool:
        return self._retention_ready

    def startup(self) -> None:
        self.paths.ensure()
        upgrade_database()
        with session_scope() as db:
            settings = load_settings(db)
            self.startup_web_port = int(settings["web_port"])
            configure_logging(self.paths.log_dir, str(settings["log_level"]))
            seed_settings(db)
            recovered_sessions = recover(db)
            now = utcnow()
            db.add(
                RuntimeInstance(
                    instance_id=self.instance_id,
                    started_at=self.started_at,
                    last_heartbeat_at=now,
                    process_id=os.getpid(),
                    status="running",
                )
            )
            add_event(db, "app_started", "info", "LiveRecorder を起動しました")
        self._retention_ready = self._check_retention(settings)
        self._retention_at = time.monotonic()
        if self._retention_ready:
            for session_id in recovered_sessions or ():
                srt.resync_session(session_id)

    @staticmethod
    def _check_retention(settings: dict) -> bool:
        try:
            return resume_operations() and run_retention(settings)
        except (OSError, ValueError, OperationalError):
            log.exception("削除台帳と期限の照合に失敗しました", extra={"event": "retention_failed"})
            return False

    async def loop(self) -> None:
        while not self.stop.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("監視ループで障害", extra={"event": "monitor_error"})
            if await _pause(self.stop, 0.25):
                return

    def _schedule_retention(self, settings: dict, now: float) -> None:
        task = self._retention_task
        if task is not None:
            if not task.done():
                return
            self._retention_task = None
            healthy = task.result()
            if healthy:
                self._retention_ready = True
            elif not self._retention_ready:
                log.warning("削除・復元の照合が完了するまで監視を待機します", extra={"event": "retention_pending"})
        delay = 3600 if self._retention_ready else 15
        if now - self._retention_at >= delay:
            self._retention_at = now
            self._retention_task = asyncio.create_task(asyncio.to_thread(self._check_retention, settings))

    async def tick(self) -> None:
        if self.stop.is_set():
            return
        now = time.monotonic()
        with session_scope() as db:
            settings = load_settings(db)
            set_log_level(str(settings["log_level"]))
            instance = db.scalar(select(RuntimeInstance).where(RuntimeInstance.instance_id == self.instance_id))
            if instance is not None:
                instance.last_heartbeat_at = utcnow()
            streamers = db.scalars(select(Streamer).where(
                Streamer.monitoring_enabled.is_(True), Streamer.archived_at.is_(None),
            )).all()
        self._schedule_retention(settings, now)
        if not self._retention_ready:
            return
        if bool(settings["comments_enabled"]) != self._global_comments:
            await self.enforce_comment_setting()
        by_platform: dict[str, dict[int, str]] = {platform: {} for platform in _RATES}
        for streamer in streamers:
            if streamer.platform in by_platform and streamer.id not in self._checking:
                by_platform[streamer.platform][streamer.id] = streamer.platform_user_id
        for platform, accounts in by_platform.items():
            streamer_id = self._schedule.choose(platform, list(accounts))
            if streamer_id is None or not self._schedule.acquire(
                platform, now, service_interval(float(settings[_RATES[platform]])),
            ):
                continue
            self._schedule.checked(platform, streamer_id, now)
            self._checking.add(streamer_id)
            task = asyncio.create_task(self.check_streamer(
                streamer_id, platform, accounts[streamer_id], float(settings["end_grace_seconds"]), settings,
            ))
            self._check_tasks.add(task)
            task.add_done_callback(self._finish_check)

    def _finish_check(self, task: asyncio.Task[None]) -> None:
        self._check_tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            log.error("配信確認の適用に失敗しました", exc_info=error, extra={"event": "monitor_apply_failed"})

    async def check_streamer(
        self,
        streamer_id: int,
        platform: str,
        account: str,
        grace: float,
        settings: dict,
    ) -> None:
        try:
            if self.stop.is_set():
                return
            adapter = self.tiktok if platform == "tiktok" else self.twitcasting
            try:
                snap = await adapter.check(account)
            except Exception:
                log.exception(
                    "配信確認に失敗しました",
                    extra={"event": "monitor_request_failed", "service": platform, "streamer_id": streamer_id},
                )
                snap = Snapshot("UNKNOWN", detail="request_error")
            with session_scope(write_lock=True) as db:
                streamer = db.get(Streamer, streamer_id)
                if streamer is None or self.stop.is_set():
                    return
                disabled = not streamer.monitoring_enabled or streamer.archived_at is not None
                if not disabled:
                    self._log_monitor(db, streamer, snap)
                intents = apply_snapshot(
                    db, streamer, snap, utcnow(),
                    grace=grace,
                    recorder_alive=self.recorder.alive,
                    comments_alive=self.comments_alive,
                    orphan_recheck=float(settings.get("orphan_recheck_seconds", 120)),
                )
                valid_snapshot = (
                    snap.status == "LIVE" and snap.platform_stream_id and not disabled
                    and streamer.live_status == "LIVE"
                )
                if valid_snapshot:
                    session = db.scalar(select(StreamSession).where(
                        StreamSession.streamer_id == streamer_id,
                        StreamSession.platform_stream_id == snap.platform_stream_id,
                    ))
                    if session is not None:
                        self._last_live[session.id] = snap
                for intent in intents:
                    if intent.reason in {"platform_end", "platform_end_unconfirmed", "superseded_by_new_stream"}:
                        self._last_live.pop(intent.session_id, None)
                        self._comment_retry_at.pop(intent.session_id, None)
            if valid_snapshot and session is not None and platform == "twitcasting":
                self.recorder.update_source(session.id, snap.media_url)
            await asyncio.gather(*(self._execute(intent) for intent in intents))
        finally:
            self._checking.discard(streamer_id)

    def _log_monitor(self, db, streamer: Streamer, snap: Snapshot) -> None:
        if snap.paused:
            return
        metadata = {"streamer_id": streamer.id, "service": streamer.platform}
        detail = self.monitor_details.notice(streamer.id, snap.status, snap.detail)
        recovery = self.monitor_details.recovered.pop(streamer.id, None)
        if recovery is not None:
            reason, first, last, count = recovery
            add_event(
                db, "monitor_recovered", "info", "配信状態を確認できるようになりました",
                metadata={**metadata, "previous_code": reason, "failure_count": count,
                          "first_failure_at": first.isoformat(), "last_failure_at": last.isoformat()},
            )
        if detail:
            if snap.retry_at is not None:
                metadata["retry_at"] = snap.retry_at
            message = detail
            if snap.diagnostics:
                metadata.update(snap.diagnostics, code=detail)
                if detail == "rate_limit":
                    message = f"ツイキャスAPIの利用上限に到達しました（HTTP {snap.diagnostics['http_status']}）"
                elif detail == "rate_limit_wait":
                    message = (
                        "LiveRecorderがコメント収集用のAPI残枠を確保するため監視を待機しました"
                        if snap.diagnostics.get("rate_remaining")
                        else "API残枠が尽きたため、LiveRecorderは監視リクエストの送信を見送りました"
                    )
            add_event(
                db, "monitor_detail", "error" if snap.status == "ERROR" else "warning",
                f"{streamer.platform} / {streamer.platform_user_id}: {message}",
                metadata=metadata,
            )

    async def _execute(self, intent: Intent) -> None:
        if intent.kind == "start_recording":
            await self._start_recording(intent)
        elif intent.kind == "stop_recording":
            await self.recorder.stop(intent.session_id, manual=intent.manual, reason=intent.reason)
        elif intent.kind == "start_comments":
            if not self.stop.is_set() and self._retention_ready:
                self._start_comments(intent)
        elif intent.kind == "stop_comments":
            await self._stop_comments(intent.session_id, reason=intent.reason or "platform_end")

    def _recording_allowed(self, db, intent: Intent) -> bool:
        if self.stop.is_set() or not self._retention_ready:
            return False
        session = db.get(StreamSession, intent.session_id)
        if session is None or session.status != "LIVE" or session.manual_stop or deletion_pending(db, session.id):
            return False
        streamer = db.get(Streamer, session.streamer_id)
        snap = self._last_live.get(session.id)
        return bool(
            streamer and streamer.monitoring_enabled and streamer.recording_enabled
            and streamer.archived_at is None and streamer.live_status == "LIVE"
            and snap is not None and not snap.restricted
            and (streamer.platform != "twitcasting" or snap.media_url)
        )

    async def _start_recording(self, intent: Intent) -> None:
        session_id = intent.session_id
        if self.recorder.alive(session_id) or session_id in self._starting_recording:
            return
        self._starting_recording.add(session_id)
        try:
            with session_scope() as db:
                if not self._recording_allowed(db, intent):
                    return
                root_text = str(load_settings(db)["recording_root"])
            try:
                root = await asyncio.to_thread(ensure_recording_root, Path(root_text)) if root_text else None
                free = (await asyncio.to_thread(shutil.disk_usage, root)).free if root is not None else None
            except (ValueError, OSError) as exc:
                self._recording_warning(session_id, "recording_blocked", str(exc))
                return
            if root is None:
                self._recording_warning(session_id, "recording_blocked", "録画保存先が未設定です")
                return
            with session_scope() as db:
                if not self._recording_allowed(db, intent):
                    return
                settings = load_settings(db)
                if settings["recording_root"] != root_text:
                    return
                if free is None or free < int(settings["min_free_bytes"]):
                    self._recording_warning(session_id, "disk_space", "空き容量が下限未満です")
                    return
                capacity = int(settings["max_recording_bytes"])
                if capacity:
                    stored = db.scalar(select(func.sum(RecordingSegment.file_size)).where(
                        RecordingSegment.validation_status == "valid",
                        RecordingSegment.file_path.is_not(None),
                        RecordingSegment.files_deleted_at.is_(None),
                    )) or 0
                    if stored >= capacity:
                        self._recording_warning(session_id, "storage_limit", "録画の保存容量が上限に達しました")
                        return
                active = db.scalar(
                    select(func.count()).select_from(RecordingJob).where(RecordingJob.status.in_(ACTIVE_RECORDING)),
                ) or 0
            scheduled = sum(1 for task in self.recorder.tasks.values() if not task.done())
            if max(active, scheduled) >= int(settings["max_parallel_recordings"]):
                self._recording_warning(session_id, "recording_delayed", "同時録画の上限のため待機しています")
                return
            self._recording_block.pop(session_id, None)
            self.recorder.start(
                session_id, intent.platform, intent.account, self._last_live[session_id].media_url,
                root, str(settings["timezone"]), str(settings["recording_quality"]),
                tiktok_engine_first=str(settings["tiktok_engine_first"]),
                refresh_source=(lambda: self._refresh_twitcasting_source(intent))
                if intent.platform == "twitcasting" else None,
            )
        finally:
            self._starting_recording.discard(session_id)

    async def _refresh_twitcasting_source(self, intent: Intent) -> str | None:
        with session_scope() as db:
            stream = db.get(StreamSession, intent.session_id)
            if stream is None or stream.status not in OPEN_SESSION:
                return None
            frequency = float(load_settings(db)["monitor_per_minute_twitcasting"])
            stream_id = stream.platform_stream_id
        if self.stop.is_set() or not self.twitcasting.gate.allow("recovery"):
            return None
        if not self._schedule.acquire("twitcasting", time.monotonic(), service_interval(frequency)):
            return None
        snapshot = await self.twitcasting.check(intent.account, priority="recovery")
        if snapshot.restricted and snapshot.platform_stream_id == stream_id:
            self.recorder.update_source(intent.session_id, None)
            return None
        if snapshot.status == "LIVE" and snapshot.platform_stream_id == stream_id:
            self.recorder.update_source(intent.session_id, snapshot.media_url)
            return snapshot.media_url
        return None

    def _recording_warning(self, session_id: int, code: str, message: str) -> None:
        if self._recording_block.get(session_id) == code:
            return
        self._recording_block[session_id] = code
        with session_scope() as db:
            add_event(db, code, "warning", message, session_id)

    def comments_alive(self, session_id: int) -> bool:
        task = self.comment_tasks.get(session_id)
        return task is not None and not task.done()

    def _start_comments(self, intent: Intent) -> None:
        retry_at = self._comment_retry_at.get(intent.session_id, 0)
        if self.comments_alive(intent.session_id) or time.monotonic() < retry_at:
            return
        run = _CommentRun()
        self._comment_runs[intent.session_id] = run
        task = asyncio.create_task(self._collect(intent, run))
        self.comment_tasks[intent.session_id] = task
        task.add_done_callback(lambda item: self._finish_comments(intent.session_id, item))

    def _finish_comments(self, session_id: int, task: asyncio.Task[None]) -> None:
        if self.comment_tasks.get(session_id) is task:
            self.comment_tasks.pop(session_id, None)
            self._comment_runs.pop(session_id, None)
        if not task.cancelled() and (error := task.exception()) is not None:
            log.error(
                "コメント収集の確定に失敗しました", exc_info=error,
                extra={"event": "comments_finalize_failed", "stream_session_id": session_id},
            )

    def _open_comment_job(self, intent: Intent) -> tuple[int, str, str | None] | None:
        with session_scope(write_lock=True) as db:
            stream = db.get(StreamSession, intent.session_id)
            if stream is None or stream.status != "LIVE" or stream.comments_purged_at is not None:
                return None
            streamer = db.get(Streamer, stream.streamer_id)
            snap = self._last_live.get(stream.id)
            allowed = (
                streamer is not None and streamer.monitoring_enabled and streamer.comments_enabled
                and streamer.archived_at is None and streamer.live_status == "LIVE"
                and snap is not None and not snap.restricted and load_settings(db)["comments_enabled"]
                and not deletion_pending(db, stream.id)
            )
            if not allowed:
                return None
            previous_id = db.scalar(
                select(Comment.platform_comment_id).where(
                    Comment.stream_session_id == stream.id, Comment.platform_comment_id.is_not(None),
                ).order_by(
                    func.length(Comment.platform_comment_id).desc(), Comment.platform_comment_id.desc(),
                ).limit(1),
            ) if intent.platform == "twitcasting" else None
            job = CommentCollectionJob(stream_session_id=stream.id, status="PENDING", started_at=utcnow())
            db.add(job)
            db.flush()
            add_event(
                db, "comments_started", "info", "コメント収集の接続を開始します", stream.id,
                metadata={"job_id": job.id, "service": intent.platform, "streamer_id": stream.streamer_id},
            )
            return job.id, stream.platform_stream_id, previous_id

    @staticmethod
    def _comment_progress(job_id: int, status: str) -> None:
        with session_scope() as db:
            job = db.get(CommentCollectionJob, job_id)
            if job is not None:
                job.status = status
                job.updated_at = utcnow()

    @staticmethod
    def _comment_gap(job_id: int, run: _CommentRun) -> None:
        if run.gap_started is None:
            return
        with session_scope() as db:
            job = db.get(CommentCollectionJob, job_id)
            if job is not None:
                db.add(CollectionGap(
                    stream_session_id=job.stream_session_id, job_type="comments",
                    started_at=run.gap_started, ended_at=utcnow(),
                    reason=run.gap_reason or "comment_connection_interrupted",
                ))
        run.gap_started = run.gap_reason = None
        run.had_gap = True

    def _connected_comments(self, job_id: int, run: _CommentRun) -> None:
        if not run.healthy:
            run.healthy = run.any_connected = True
            self._comment_progress(job_id, "COLLECTING")
            self._comment_gap(job_id, run)

    @staticmethod
    async def _persist_batch(session_id: int, job_id: int, items: list[CommentIn]) -> None:
        if not items:
            return
        for attempt in range(3):
            try:
                await asyncio.to_thread(save_comments, session_id, job_id, items)
                return
            except OperationalError as exc:
                if attempt == 2:
                    raise CommentConnectionError("comment_write_failed", dropped=len(items)) from exc
                await asyncio.sleep(0.1 * (2**attempt))

    async def _stream_comment_batches(self, intent: Intent, job_id: int, stream_id: str, run: _CommentRun) -> None:
        stream = self.tiktok.stream_comments(
            intent.account, stream_id, run.stop, on_connect=lambda: self._connected_comments(job_id, run),
        )
        next_item: asyncio.Task | None = None
        pending: list[CommentIn] = []
        try:
            while True:
                if next_item is None:
                    next_item = asyncio.create_task(anext(stream))
                await asyncio.wait((next_item,), timeout=0.5 if pending else None)
                if next_item.done():
                    try:
                        pending.append(next_item.result())
                    except StopAsyncIteration:
                        break
                    finally:
                        next_item = None
                    if len(pending) < _COMMENT_BATCH:
                        continue
                batch, pending = pending, []
                await self._persist_batch(intent.session_id, job_id, batch)
        finally:
            if next_item is not None and not next_item.done():
                next_item.cancel()
                await asyncio.gather(next_item, return_exceptions=True)
            try:
                await stream.aclose()
            finally:
                await self._persist_batch(intent.session_id, job_id, pending)

    def _comment_failure(
        self, job_id: int, run: _CommentRun, code: str, *, dropped: int = 0, retry_at: float | None = None,
    ) -> None:
        now = utcnow()
        run.healthy = False
        with session_scope() as db:
            job = db.get(CommentCollectionJob, job_id)
            if job is None:
                return
            run.gap_started = run.gap_started or job.last_received_at or job.started_at or now
            run.gap_reason = run.gap_reason or code
            previous = job.last_error
            job.status = "RECONNECTING"
            job.last_error = code
            job.updated_at = now
            job.reconnect_count += 1
            job.received_count += dropped
            job.error_count += dropped
            if previous != code:
                add_event(
                    db, "comments_failed", "warning", "コメント収集が中断しました", job.stream_session_id,
                    metadata={"job_id": job.id, "reason": code, "retry_at": retry_at, "lost_count": dropped},
                )

    async def _collect(self, intent: Intent, run: _CommentRun) -> None:
        opened = self._open_comment_job(intent)
        if opened is None:
            return
        job_id, stream_id, cursor = opened
        attempts = 0
        fallback_reason = "comment_source_ended"
        try:
            while not run.stop.is_set():
                if not run.healthy:
                    self._comment_progress(job_id, "CONNECTING" if not attempts else "RECONNECTING")
                try:
                    if intent.platform == "tiktok":
                        await self._stream_comment_batches(intent, job_id, stream_id, run)
                        if run.stop.is_set():
                            break
                        raise CommentConnectionError("comment_source_ended")
                    batch = await self.twitcasting.comments(intent.account, stream_id, cursor)
                    if batch.items:
                        await self._persist_batch(intent.session_id, job_id, batch.items)
                    if batch.error:
                        raise CommentConnectionError(batch.error, retry_at=batch.retry_at)
                    self._connected_comments(job_id, run)
                    cursor = batch.next_after_id or cursor
                    if batch.gap:
                        self._comment_failure(job_id, run, batch.gap)
                    attempts = 0
                    await _pause(run.stop, 3)
                except asyncio.CancelledError:
                    fallback_reason = "internal_cancel"
                    raise
                except Exception as exc:
                    code = exc.code if isinstance(exc, CommentConnectionError) else type(exc).__name__
                    dropped = exc.dropped if isinstance(exc, CommentConnectionError) else 0
                    retry_at = exc.retry_at if isinstance(exc, CommentConnectionError) else None
                    if not isinstance(exc, CommentConnectionError):
                        log.exception(
                            "コメント収集の通信または書き込みで障害",
                            extra={
                                "event": "comment_io_failed", "stream_session_id": intent.session_id, "job_id": job_id,
                            },
                        )
                    if run.healthy:
                        attempts = 0
                    self._comment_failure(job_id, run, code, dropped=dropped, retry_at=retry_at)
                    attempts += 1
                    fallback_reason = code
                    if attempts >= 5 or code in {"authorization_denied", "credentials_missing",
                                                 "comment_access_restricted", "comment_room_mismatch"}:
                        delay = max(30, (retry_at or 0) - time.time())
                        self._comment_retry_at[intent.session_id] = time.monotonic() + delay
                        break
                    delay = max(min(2**attempts, 30) + random.random(), (retry_at or 0) - time.time())
                    await _pause(run.stop, delay)
        finally:
            reason = run.stop_reason or fallback_reason
            self._comment_gap(job_id, run)
            with session_scope() as db:
                job = db.get(CommentCollectionJob, job_id)
                if job is not None:
                    if reason in {"comments_disabled", "global_comments_disabled", "monitoring_disabled",
                                  "restricted_movie"}:
                        job.status = "DISABLED"
                    elif reason == "platform_end" and run.any_connected and not run.had_gap:
                        job.status = "COMPLETED"
                    else:
                        job.status = "PARTIAL" if run.any_connected or job.stored_count else "FAILED"
                    job.end_reason = reason
                    job.finished_at = job.updated_at = utcnow()
                    add_event(
                        db, "comments_finished", "info", "コメント収集とキューの書き込みを終了しました",
                        intent.session_id, metadata={"job_id": job_id, "reason": reason, "status": job.status},
                    )
            await asyncio.to_thread(srt.resync_session, intent.session_id)

    async def _stop_comments(self, session_id: int, *, reason: str) -> None:
        run = self._comment_runs.get(session_id)
        task = self.comment_tasks.get(session_id)
        if run is None or task is None or task.done():
            return
        if not run.stop.is_set():
            run.stop_reason = reason
            run.stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=25)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def enforce_switches(self, streamer_id: int) -> None:
        with session_scope(write_lock=True) as db:
            streamer = db.get(Streamer, streamer_id)
            if streamer is None:
                return
            now = utcnow()
            intents: list[Intent] = []
            for stream in open_sessions(db, streamer.id):
                if not streamer.monitoring_enabled or streamer.archived_at is not None:
                    self._last_live.pop(stream.id, None)
                    self._comment_retry_at.pop(stream.id, None)
                    intents.extend(end_session(stream, now, reason="monitoring_disabled"))
                    continue
                if not streamer.recording_enabled:
                    intents.append(Intent("stop_recording", stream.id, reason="recording_disabled"))
                if not streamer.comments_enabled:
                    self._comment_retry_at.pop(stream.id, None)
                    intents.append(Intent("stop_comments", stream.id, reason="comments_disabled"))
                snap = self._last_live.get(stream.id)
                if snap and stream.status == "LIVE" and streamer.live_status == "LIVE":
                    intents.extend(intent for intent in job_intents(stream, streamer, snap)
                                   if intent.kind.startswith("start_"))
        await asyncio.gather(*(self._execute(intent) for intent in intents))

    async def enforce_comment_setting(self) -> None:
        with session_scope() as db:
            enabled = bool(load_settings(db)["comments_enabled"])
            self._global_comments = enabled
            streamer_ids = db.scalars(select(Streamer.id).where(
                Streamer.monitoring_enabled.is_(True), Streamer.comments_enabled.is_(True),
                Streamer.archived_at.is_(None),
            )).all() if enabled else []
        if not enabled:
            self._comment_retry_at.clear()
            await asyncio.gather(*(self._stop_comments(
                session_id, reason="global_comments_disabled",
            ) for session_id in list(self.comment_tasks)))
        else:
            await asyncio.gather(*(self.enforce_switches(streamer_id) for streamer_id in streamer_ids))

    async def manual_stop(self, session_id: int) -> bool:
        with session_scope(write_lock=True) as db:
            stream = db.get(StreamSession, session_id)
            if stream is None or stream.status not in OPEN_SESSION:
                return False
            stream.manual_stop = True
            stream.updated_at = utcnow()
        await self.recorder.stop(session_id, manual=True, reason="manual_stop")
        return True

    async def manual_resume(self, session_id: int) -> bool:
        with session_scope(write_lock=True) as db:
            stream = db.get(StreamSession, session_id)
            if stream is None or stream.status != "LIVE" or deletion_pending(db, session_id):
                return False
            streamer = db.get(Streamer, stream.streamer_id)
            snap = self._last_live.get(session_id)
            if (
                streamer is None or not streamer.monitoring_enabled or not streamer.recording_enabled
                or streamer.live_status != "LIVE" or streamer.archived_at is not None
                or snap is None or snap.restricted
            ):
                return False
            stream.manual_stop = False
            stream.updated_at = utcnow()
            intent = Intent(
                "start_recording", session_id, platform=streamer.platform, account=streamer.platform_user_id,
                media_url=snap.media_url,
            )
        await self._start_recording(intent)
        return True

    async def shutdown(self) -> None:
        self.stop.set()
        checks = list(self._check_tasks)
        for task in checks:
            task.cancel()
        await asyncio.gather(*checks, return_exceptions=True)
        await asyncio.gather(
            self.recorder.stop_all(),
            *(self._stop_comments(session_id, reason="app_shutdown") for session_id in list(self.comment_tasks)),
        )
        if self._retention_task is not None:
            await self._retention_task
        await self.twitcasting.aclose()
        with session_scope() as db:
            instance = db.scalar(select(RuntimeInstance).where(RuntimeInstance.instance_id == self.instance_id))
            if instance is not None:
                instance.status = "stopped"
                instance.stopped_at = utcnow()
            add_event(db, "app_stopped", "info", "LiveRecorder を停止しました")


async def _pause(stop: asyncio.Event, seconds: float) -> bool:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return False
    return True
