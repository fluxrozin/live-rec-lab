"""録画ジョブの実行。"""

from __future__ import annotations

import asyncio
import logging
import re
import stat
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from live_rec_lab.config import utcnow
from live_rec_lab.db.models import (
    CollectionGap,
    RecordingJob,
    RecordingSegment,
    Streamer,
    StreamSession,
)
from live_rec_lab.db.queries import add_event
from live_rec_lab.db.session import session_scope
from live_rec_lab.monitoring.state import ACTIVE_RECORDING, OPEN_SESSION
from live_rec_lab.ops.logging_config import redact
from live_rec_lab.recording.engines import build_command, run_command
from live_rec_lab.recording.hls_playlist import FilteredPlaylist, HlsMirror, HlsPlaylistError
from live_rec_lab.recording.segments import (
    attempt_is_empty,
    clean_published_staging,
    finalize_segment,
    is_twitcasting_preroll,
    reserve_segment_path,
    unlink_empty_files,
)
from live_rec_lab.storage import broadcast_filename_time, safe_managed_path, streamer_directory

log = logging.getLogger("live_rec_lab.recording")


_OFFLINE_STDERR = (
    "not currently live",
    "channel is not currently live",
    "no live stream available",
    "user is offline",
    "useroffline",
)
_ACCESS_ERRORS = (
    "captcha", "log in", "login", "access denied", "not authorized", "forbidden",
    "region restricted", "members only", "members-only", "private stream", "drm",
)
_ACCESS_STATUS = re.compile(r"(?:http(?: error)?|status(?: code)?|response)\s*[:=]?\s*(401|403|429)\b")


def recording_engines(platform: str, preferred: str | None, tiktok_engine_first: str = "streamlink") -> list[str]:
    """同じジョブでは書けたエンジンを優先する。"""
    if platform == "twitcasting":
        return ["ffmpeg"]
    engines = ["streamlink", "yt-dlp"]
    if (preferred or tiktok_engine_first) == "yt-dlp":
        engines.reverse()
    return engines


def recording_exit_kind(*, stderr: str, returncode: int | None, stopped: bool) -> str:
    """停止、オフラインを示す標準エラー、終了コード 0、それ以外の中断を分ける。"""
    if stopped:
        return "stopped"
    text = stderr.lower()
    if _ACCESS_STATUS.search(text) or any(marker in text for marker in _ACCESS_ERRORS):
        return "restricted"
    if any(marker in text for marker in _OFFLINE_STDERR):
        return "offline"
    if returncode == 0:
        return "ended"
    return "interrupted"


def reconnect_action(
    *,
    kind: str,
    bytes_written: int,
    engine_index: int,
    engine_count: int,
    session_live: bool = False,
) -> str:
    """空のまま配信中なら待つ。取得物がある中断では同じ実取得経路を試す。"""
    if kind in {"stopped", "restricted"}:
        return "stop"
    useful = bytes_written > 0
    if kind in {"ended", "offline"} and (useful or not session_live):
        return "stop"
    if kind in {"ended", "offline"}:
        return "wait"
    if useful:
        return "same"
    if engine_index + 1 < engine_count:
        return "switch"
    return "same"


def engine_switch_notice(previous: str | None, engine: str, fallback_noted: bool) -> str | None:
    """順序の先頭へ戻ることと、同じフォールバックの繰り返しは記録しない。"""
    if not previous or previous == engine or engine == "streamlink":
        return None
    if previous == "streamlink" and engine == "yt-dlp" and fallback_noted:
        return None
    return f"録画エンジンを {previous} から {engine} に切り替えました"


def recording_continues(status: str | None, *, stopped: bool) -> bool:
    """猶予中の終了確認は録画を続ける。終わった配信と停止要求だけ止める。"""
    return not stopped and status in OPEN_SESSION


def next_segment_number(current: int, *, kept: bool) -> int:
    """残した試行だけ番号を進める。空で捨てた試行は同じ番号を使う。"""
    return current + 1 if kept else current


def counts_as_recording_failure(segment: RecordingSegment) -> bool:
    """行に残る失敗だけ、ジョブを部分完了にする。"""
    return segment.validation_status != "valid" and not attempt_is_empty(segment)


class RecordingManager:
    def __init__(self) -> None:
        self.tasks: dict[int, asyncio.Task[None]] = {}
        self.stops: dict[int, asyncio.Event] = {}
        self.processes: dict[int, asyncio.subprocess.Process] = {}
        self.filename_clocks: dict[int, datetime] = {}
        self.sources: dict[int, str | None] = {}
        self.stop_reasons: dict[int, str] = {}

    def filename_clock(self, session_id: int) -> datetime:
        """同じ配信のファイル名は、最初に決めた配信開始時刻のままにする。"""
        cached = self.filename_clocks.get(session_id)
        if cached is not None:
            return cached
        clock = session_filename_time(session_id)
        self.filename_clocks[session_id] = clock
        return clock

    def alive(self, session_id: int) -> bool:
        task = self.tasks.get(session_id)
        return task is not None and not task.done()

    def start(
        self,
        session_id: int,
        platform: str,
        account: str,
        media_url: str | None,
        root: Path,
        timezone_name: str,
        quality: str,
        *,
        tiktok_engine_first: str = "streamlink",
        refresh_source: Callable[[], Awaitable[str | None]] | None = None,
    ) -> None:
        if self.alive(session_id):
            return
        self.sources[session_id] = media_url
        self.stop_reasons.pop(session_id, None)
        stop = asyncio.Event()
        self.stops[session_id] = stop
        self.tasks[session_id] = asyncio.create_task(
            self._run(session_id, platform, account, root, timezone_name, quality, stop, tiktok_engine_first,
                      refresh_source)
        )

    def update_source(self, session_id: int, media_url: str | None) -> None:
        if self.alive(session_id):
            self.sources[session_id] = media_url

    async def stop(self, session_id: int, *, manual: bool, reason: str | None = None) -> None:
        self.stop_reasons[session_id] = "manual_stop" if manual else reason or "session_ended"
        if manual:
            with session_scope() as db:
                stream = db.get(StreamSession, session_id)
                if stream is not None:
                    stream.manual_stop = True
                    stream.updated_at = utcnow()
                job = _active_job(db, session_id)
                if job is not None:
                    job.manual_stop_requested = True
        event = self.stops.get(session_id)
        if event is not None:
            event.set()
        task = self.tasks.get(session_id)
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=8)
            except TimeoutError:
                await self._kill_live(session_id)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    async def stop_all(self) -> None:
        await asyncio.gather(*(self.stop(session_id, manual=False, reason="app_shutdown")
                               for session_id in list(self.tasks) if self.alive(session_id)))

    async def _kill_live(self, session_id: int) -> None:
        """確定の前に、書き込み中の子プロセスを終わらせる。"""
        process = self.processes.get(session_id)
        if process is None or process.returncode is not None:
            return
        process.kill()
        await process.wait()

    async def _run(
        self,
        session_id: int,
        platform: str,
        account: str,
        root: Path,
        timezone_name: str,
        quality: str,
        stop: asyncio.Event,
        tiktok_engine_first: str,
        refresh_source: Callable[[], Awaitable[str | None]] | None,
    ) -> None:
        job_id = _open_job(session_id)
        if job_id is None:
            return
        filename_time = self.filename_clock(session_id)
        retries = 0
        empty_waits = 0
        any_valid = False
        retried = False
        preferred: str | None = None
        fallback_noted = False
        open_segment_id: int | None = None
        capture_error = False
        finish_reason = "source_ended"
        captured_finite: set[str] = set()
        retry_finite: str | None = None
        next_playlist: FilteredPlaylist | None = None
        awaiting_platform_end = False
        try:
            while not stop.is_set() and retries <= 5:
                mirror: HlsMirror | None = None
                try:
                    if platform == "twitcasting":
                        if retries and next_playlist is None and refresh_source is not None:
                            if refreshed := await refresh_source():
                                self.sources[session_id] = refreshed
                        if stop.is_set() or not _session_open(session_id):
                            break
                        media_url = self.sources.get(session_id)
                        if not media_url:
                            _remember_error(job_id, "録画用の公開 URL がありません")
                            capture_error = True
                            finish_reason = "source_unavailable"
                            break
                        mirror = HlsMirror(media_url, quality=quality)
                        try:
                            url = await mirror.ffmpeg_input(
                                stop, captured_finite=captured_finite, retry_finite=retry_finite,
                                initial_playlist=next_playlist,
                            )
                        except HlsPlaylistError as exc:
                            if not awaiting_platform_end or exc.status_code in {401, 403}:
                                _remember_error(job_id, str(exc))
                                log.warning(
                                    str(exc), extra={
                                        "event": "recording_hls_unavailable", "job_id": job_id,
                                        "stream_session_id": session_id, "metadata": {"http_status": exc.status_code},
                                    },
                                )
                            if exc.status_code in {401, 403}:
                                capture_error = True
                                finish_reason = "source_restricted"
                                _remember_gap(session_id, "録画元がアクセスを拒否しました")
                                break
                            url = None
                        next_playlist = None
                        if not url:
                            if stop.is_set() or not _session_open(session_id):
                                break
                            retried |= not awaiting_platform_end
                            if not _schedule_retry(job_id, session_id, retries, record_gap=not awaiting_platform_end):
                                finish_reason = "retry_limit"
                                break
                            retries += 1
                            await _wait_to_retry(stop, retries)
                            continue
                        retry_finite = None
                        if awaiting_platform_end:
                            capture_error = True
                            _remember_gap(session_id, "録画元が一度終了したため次のファイルで再開します")
                            awaiting_platform_end = False
                    else:
                        url = f"https://www.tiktok.com/@{account}/live"
                    segment_id, temp = await asyncio.to_thread(
                        _open_attempt, job_id, session_id, root, platform, account, filename_time, timezone_name
                    )
                    open_segment_id = segment_id
                    engines = recording_engines(platform, preferred, tiktok_engine_first)
                    code = 1
                    error = ""
                    last_index = 0
                    for index, engine in enumerate(engines):
                        last_index = index
                        try:
                            command = build_command(engine, url, temp, quality)
                        except FileNotFoundError:
                            error = f"{engine} がありません"
                            continue
                        with session_scope() as db:
                            job = db.get(RecordingJob, job_id)
                            if job is not None:
                                notice = engine_switch_notice(
                                    job.recorder_engine,
                                    engine,
                                    fallback_noted,
                                )
                                if notice:
                                    add_event(
                                        db,
                                        "recording_engine_switch",
                                        "warning",
                                        notice,
                                        session_id,
                                    )
                                    if engine == "yt-dlp":
                                        fallback_noted = True
                                job.recorder_engine = engine
                        code, error = await run_command(
                            command,
                            stop,
                            on_tick=lambda: _note_media(segment_id, temp),
                            on_process=lambda process: self.processes.__setitem__(session_id, process),
                        )
                        if (attempt_bytes := temp.stat().st_size if temp.is_file() else 0):
                            preferred = engine
                        if code == 0 or stop.is_set() or attempt_bytes or recording_exit_kind(
                            stderr=error, returncode=code, stopped=False,
                        ) == "restricted":
                            break
                        temp.unlink(missing_ok=True)
                    written = temp.stat().st_size if temp.is_file() else 0
                    exit_kind = recording_exit_kind(
                        stderr=error,
                        returncode=code,
                        stopped=stop.is_set(),
                    )
                    if mirror is not None:
                        await mirror.aclose()
                    finalized = asyncio.create_task(asyncio.to_thread(_finalize_open, segment_id, session_id, root))
                    try:
                        outcome = await asyncio.shield(finalized)
                    except asyncio.CancelledError:
                        await finalized
                        raise
                    any_valid |= outcome is True
                    capture_error |= outcome is False
                    if outcome is None and error:
                        _remember_error(job_id, error)
                    open_segment_id = None
                    if exit_kind == "restricted":
                        finish_reason = "source_restricted"
                        capture_error = True
                        _remember_error(job_id, error)
                        _remember_gap(session_id, "録画元がアクセスを拒否しました")
                        break
                    status = _session_status(session_id)
                    if mirror is not None:
                        if mirror.last_error and mirror.last_error.status_code in {401, 403}:
                            finish_reason = "source_restricted"
                            capture_error = True
                            _remember_error(job_id, str(mirror.last_error))
                            _remember_gap(session_id, "録画元がアクセスを拒否しました")
                            break
                        if outcome == "preroll":
                            next_playlist = mirror.next_playlist
                            if not recording_continues(status, stopped=stop.is_set()):
                                finish_reason = "preroll_only"
                                _remember_error(job_id, "録画用HLSにプリロールしかありませんでした")
                                break
                            if next_playlist is not None:
                                continue
                            if discarded := mirror.finite_identity():
                                captured_finite.add(discarded)
                            if not _schedule_retry(job_id, session_id, retries, record_gap=False):
                                finish_reason = "retry_limit"
                                _remember_error(job_id, "再接続の上限までに本編を取得できませんでした")
                                break
                            retries += 1
                            await _wait_to_retry(stop, retries)
                            continue
                        resource_failed = mirror.resource_failed()
                        next_playlist = mirror.next_playlist
                        retry_hls = (
                            status == "LIVE" and not stop.is_set() and next_playlist is None
                            and (not mirror.end_confirmed or resource_failed)
                        )
                        capture_complete = (
                            exit_kind == "ended" and outcome is True and not resource_failed and not mirror.last_error
                        )
                        finite = mirror.finite_identity()
                        if finite is not None:
                            if capture_complete:
                                captured_finite.add(finite)
                            elif exit_kind == "interrupted" or resource_failed or outcome is False:
                                retry_finite = finite
                        awaiting_platform_end = (
                            retry_hls and capture_complete and finite is not None and not mirror.unconfirmed
                        )
                        if (
                            mirror.unconfirmed or mirror.last_error or resource_failed or exit_kind == "interrupted"
                            or (retry_hls and not awaiting_platform_end)
                        ):
                            if mirror.last_error:
                                reason = str(mirror.last_error)
                            elif resource_failed:
                                reason = "HLSの初期化ファイルまたはメディアを取得できませんでした"
                            elif exit_kind == "interrupted":
                                reason = "FFmpegが録画中に異常終了しました"
                            else:
                                reason = "HLSで本編の終了を確認できません"
                            _remember_error(job_id, reason)
                            _remember_gap(session_id, reason)
                            capture_error = True
                            log.warning(
                                reason, extra={
                                    "event": "recording_hls_incomplete", "job_id": job_id,
                                    "stream_session_id": session_id,
                                    "metadata": {
                                        **mirror.diagnostics(), "returncode": code, "exit_kind": exit_kind,
                                        "bytes_written": written, "stderr": redact(error),
                                    },
                                },
                            )
                        if retry_hls and exit_kind in {"ended", "offline"}:
                            exit_kind = "interrupted"
                    if not recording_continues(status, stopped=stop.is_set()):
                        break
                    if next_playlist is not None:
                        continue
                    action = reconnect_action(
                        kind=exit_kind,
                        bytes_written=written,
                        engine_index=last_index,
                        engine_count=len(engines),
                        session_live=status == "LIVE",
                    )
                    if action == "wait":
                        preferred = None
                        _note_waiting(job_id)
                        empty_waits += 1
                        if not _schedule_retry(job_id, session_id, retries):
                            finish_reason = "retry_limit"
                            break
                        retries += 1
                        try:
                            await asyncio.wait_for(stop.wait(), min(2**empty_waits, 30))
                        except TimeoutError:
                            pass
                        if stop.is_set():
                            break
                        continue
                    if action == "switch" and last_index + 1 < len(engines):
                        preferred = engines[last_index + 1]
                    elif action == "stop":
                        break
                    if not _schedule_retry(job_id, session_id, retries, record_gap=not awaiting_platform_end):
                        finish_reason = "retry_limit"
                        break
                    retried |= not awaiting_platform_end
                    retries += 1
                    await _wait_to_retry(stop, retries)
                finally:
                    if mirror is not None:
                        await mirror.aclose()
        except asyncio.CancelledError:
            outcome = await asyncio.to_thread(_finalize_open, open_segment_id, session_id, root)
            if outcome is True:
                any_valid = True
            elif outcome is False:
                retried = True
            manual = _manual(job_id)
            _finish(
                job_id,
                any_valid=any_valid,
                manual=manual,
                failed=(capture_error or retried or not any_valid or (
                    awaiting_platform_end and self.stop_reasons.get(session_id) != "platform_end"
                )) and not manual,
                reason=self.stop_reasons.get(session_id, "internal_cancel"),
            )
            await _resync_captured_session(session_id)
            raise
        except (OSError, ValueError) as exc:
            capture_error = True
            finish_reason = "recording_io_error"
            _remember_error(job_id, str(exc))
            _remember_gap(session_id, "録画中のファイル操作を完了できませんでした")
            any_valid |= await asyncio.to_thread(_finalize_open, open_segment_id, session_id, root) is True
        finally:
            self.processes.pop(session_id, None)
            self.sources.pop(session_id, None)
        manual = stop.is_set() and _manual(job_id)
        if awaiting_platform_end and not manual and (
            self.stop_reasons.get(session_id) != "platform_end" and _session_status(session_id) != "ENDED"
        ):
            capture_error = True
            reason = "HLSの終了後に配信の終了または本編の再開を確認できませんでした"
            _remember_gap(session_id, reason)
            if finish_reason != "source_restricted":
                _remember_error(job_id, reason)
        _finish(job_id, any_valid=any_valid, manual=manual, failed=capture_error or retried or not any_valid,
                reason=self.stop_reasons.pop(session_id, finish_reason))
        await _resync_captured_session(session_id)


async def _wait_to_retry(stop: asyncio.Event, retries: int) -> None:
    try:
        await asyncio.wait_for(stop.wait(), min(2**retries, 30))
    except TimeoutError:
        pass


async def _resync_captured_session(session_id: int) -> None:
    from live_rec_lab.comments.srt import resync_session

    await asyncio.to_thread(resync_session, session_id)


def _open_attempt(
    job_id: int, session_id: int, root: Path, platform: str, account: str, filename_time: datetime, timezone_name: str,
) -> tuple[int, Path]:
    now = utcnow()
    with session_scope(write_lock=True) as db:
        stream = db.get(StreamSession, session_id)
        job = db.get(RecordingJob, job_id)
        if stream is None or job is None:
            raise ValueError("録画ジョブの配信セッションを確認できません")
        folder = streamer_directory(db, root, account)
        if not stream.filename_stem:
            local = filename_time.astimezone(ZoneInfo(timezone_name))
            stream.filename_stem = f"{local:%Y-%m-%d}/{platform}_{folder.name}_{local:%Y%m%d_%H%M%S}"
        directory = safe_managed_path(root, folder / Path(stream.filename_stem).parent)
        existing = db.scalar(select(RecordingSegment.reserved_path).where(
            RecordingSegment.stream_session_id == session_id, RecordingSegment.reserved_path.is_not(None),
        ).limit(1))
        if existing and Path(existing).parent != directory:
            raise ValueError("同一配信の録画保存先が変更されています。元の配信フォルダを確認してください")
        directory.mkdir(exist_ok=True)
        temp_path = safe_managed_path(root, directory / f".live-rec-lab-{uuid4().hex}.capture.ts")
        segment = RecordingSegment(recording_job_id=job_id, stream_session_id=session_id, started_at=now,
                                   temp_path=str(temp_path))
        db.add(segment)
        job.status = "RECORDING"
        job.updated_at = now
        db.flush()
        return segment.id, temp_path


def _open_job(session_id: int) -> int | None:
    with session_scope() as db:
        if _active_job(db, session_id) is not None:
            return None
        now = utcnow()
        job = RecordingJob(
            stream_session_id=session_id,
            status="STARTING",
            retry_count=0,
            manual_stop_requested=False,
            started_at=now,
            created_at=now,
            updated_at=now,
        )
        db.add(job)
        db.flush()
        add_event(db, "recording_started", "info", "録画を開始しました", session_id)
        return job.id


def _active_job(db, session_id: int) -> RecordingJob | None:
    return db.scalar(
        select(RecordingJob).where(
            RecordingJob.stream_session_id == session_id,
            RecordingJob.status.in_(ACTIVE_RECORDING),
        )
    )


def _note_media(segment_id: int, temp: Path) -> None:
    if not temp.is_file() or temp.stat().st_size <= 0:
        return
    with session_scope() as db:
        segment = db.get(RecordingSegment, segment_id)
        if segment is not None and segment.first_media_at is None:
            segment.first_media_at = utcnow()
            segment.sync_method = "approximate"


def session_filename_time(session_id: int) -> datetime:
    """ファイル名に使う時刻。配信開始が無いときは検知時刻で、セグメントの録画開始は使わない。"""
    with session_scope() as db:
        stream = db.get(StreamSession, session_id)
        if stream is None:
            return utcnow()
        return broadcast_filename_time(stream.platform_started_at, stream.detected_at)


def _initial_segment_number(session_id: int) -> int:
    """同じ配信で残している最後の番号の次。新しいジョブも 1 には戻さない。"""
    with session_scope() as db:
        job_ids = select(RecordingJob.id).where(RecordingJob.stream_session_id == session_id)
        highest = db.scalar(
            select(func.max(RecordingSegment.segment_number)).where(
                RecordingSegment.recording_job_id.in_(job_ids)
            )
        )
        return int(highest or 0) + 1


def _session_status(session_id: int) -> str | None:
    with session_scope() as db:
        stream = db.get(StreamSession, session_id)
        return None if stream is None else stream.status


def _session_open(session_id: int) -> bool:
    return recording_continues(_session_status(session_id), stopped=False)


def _capture_identity(path: Path) -> tuple[int, int, int, int, int] | None:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        return None
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _preroll_capture(
    segment_id: int, session_id: int, root: Path,
) -> tuple[Path, tuple[int, int, int, int, int]] | None:
    with session_scope() as db:
        path = db.scalar(
            select(RecordingSegment.temp_path)
            .join(StreamSession, StreamSession.id == RecordingSegment.stream_session_id)
            .join(Streamer, Streamer.id == StreamSession.streamer_id)
            .where(
                RecordingSegment.id == segment_id,
                RecordingSegment.stream_session_id == session_id,
                RecordingSegment.validation_status == "pending",
                RecordingSegment.segment_number.is_(None),
                RecordingSegment.reserved_path.is_(None),
                RecordingSegment.file_path.is_(None),
                Streamer.platform == "twitcasting",
            )
        )
        if not path or db.scalar(
            select(RecordingSegment.id).where(
                RecordingSegment.stream_session_id == session_id,
                RecordingSegment.id < segment_id,
                RecordingSegment.validation_status == "valid",
            ).limit(1)
        ) is not None:
            return None
    try:
        candidate = safe_managed_path(root, Path(path))
        identity = _capture_identity(candidate)
        if identity is not None and is_twitcasting_preroll(candidate):
            return candidate, identity
    except (ValueError, OSError):
        return None
    return None


def _finalize_open(segment_id: int | None, session_id: int, root: Path) -> bool | Literal["preroll"] | None:
    """録画を確定する。既知のプリロールは採番前に破棄する。"""
    if segment_id is None:
        return None
    preroll = _preroll_capture(segment_id, session_id, root)
    with session_scope(write_lock=True) as db:
        segment = db.get(RecordingSegment, segment_id)
        if segment is None or segment.stream_session_id != session_id:
            return None
        if segment.validation_status != "pending":
            return segment.validation_status == "valid"
        try:
            for path in (segment.temp_path, segment.reserved_path, segment.file_path):
                if path:
                    safe_managed_path(root, Path(path))
            if preroll and segment.segment_number is None and not segment.reserved_path and not segment.file_path:
                candidate, identity = preroll
                if str(candidate) == segment.temp_path and _capture_identity(candidate) == identity:
                    candidate.unlink()
                    db.delete(segment)
                    return "preroll"
            if attempt_is_empty(segment) and segment.segment_number is None and not segment.reserved_path:
                unlink_empty_files(segment)
                db.delete(segment)
                add_gap(db, session_id, "recording", "録画試行に保存できるメディアがありませんでした",
                        started_at=segment.started_at)
                return None
            reserve_segment_path(db, segment, root)
        except (ValueError, OSError) as exc:
            add_event(db, "recording_reservation_failed", "warning", str(exc), session_id)
            return False
        job = db.get(RecordingJob, segment.recording_job_id)
        if job is not None and job.status in ACTIVE_RECORDING:
            job.status = "FINALIZING"

    # ffmpeg・ffprobeの実行は予約を書き込んだトランザクションの外で行う。
    probe_error = finalize_segment(segment, retain_staging=True)
    with session_scope(write_lock=True) as db:
        db.merge(segment)
        job = db.get(RecordingJob, segment.recording_job_id)
        if job is not None and probe_error:
            job.last_error = redact(probe_error)[:500]
            add_event(db, "recording_finalize_failed", "warning", probe_error, session_id, {"job_id": job.id})
            add_gap(db, session_id, "recording", "録画ファイルの確定が完了しませんでした",
                    started_at=segment.started_at)
    if segment.validation_status == "valid":
        try:
            with session_scope(write_lock=True) as db:
                current = db.get(RecordingSegment, segment_id)
                if current is not None:
                    clean_published_staging(current)
        except OSError:
            log.warning("録画の一時ファイルを片付けられませんでした", extra={"stream_session_id": session_id})
        return True
    return False


def _manual(job_id: int) -> bool:
    with session_scope() as db:
        job = db.get(RecordingJob, job_id)
        return bool(job and job.manual_stop_requested)


def _remember_error(job_id: int, message: str) -> None:
    with session_scope() as db:
        job = db.get(RecordingJob, job_id)
        if job is None:
            return
        job.last_error = redact(message)[:500]
        job.updated_at = utcnow()


def _remember_gap(session_id: int, reason: str) -> None:
    with session_scope() as db:
        add_gap(db, session_id, "recording", reason)


def _note_waiting(job_id: int) -> None:
    """映像がまだ無いあいだは、同じジョブを再接続中のままにする。"""
    with session_scope() as db:
        job = db.get(RecordingJob, job_id)
        if job is None:
            return
        job.status = "RECONNECTING"
        job.last_error = "映像が始まるまで待ちます"
        job.updated_at = utcnow()


def _schedule_retry(job_id: int, session_id: int, retries: int, *, record_gap: bool = True) -> bool:
    """次の再接続を予約する。上限を超えるときは False。"""
    nxt = retries + 1
    with session_scope() as db:
        job = db.get(RecordingJob, job_id)
        if job is not None:
            job.retry_count = nxt
            job.updated_at = utcnow()
            job.status = "FINALIZING" if nxt > 5 else "RECONNECTING"
        if record_gap:
            add_gap(
                db,
                session_id,
                "recording",
                "再接続の上限に達しました" if nxt > 5 else "録画が中断したため次のファイルで再開します",
            )
    return nxt <= 5


def _finish(job_id: int, *, any_valid: bool, manual: bool, failed: bool, reason: str = "source_ended") -> None:
    with session_scope() as db:
        job = db.get(RecordingJob, job_id)
        if job is None or job.status in {"COMPLETED", "PARTIAL", "FAILED", "CANCELLED"}:
            return
        job.finished_at = utcnow()
        job.updated_at = job.finished_at
        job.end_reason = reason
        if manual:
            job.status = "CANCELLED" if not any_valid else "PARTIAL"
            add_event(db, "recording_finished", "info", "録画を停止しました", job.stream_session_id)
        elif any_valid and not failed:
            job.status = "COMPLETED"
            add_event(db, "recording_finished", "info", "録画を確定しました", job.stream_session_id)
        elif any_valid:
            job.status = "PARTIAL"
            add_event(db, "recording_finished", "warning", "録画の一部だけ確定しました", job.stream_session_id)
        else:
            job.status = "FAILED"
            add_event(db, "recording_failed", "error", job.last_error or "録画に失敗しました", job.stream_session_id)


def add_gap(db, session_id: int, job_type: str, reason: str, *, started_at: datetime | None = None) -> None:
    now = utcnow()
    db.add(
        CollectionGap(
            stream_session_id=session_id,
            job_type=job_type,
            started_at=started_at or now,
            ended_at=now,
            reason=reason[:500],
            created_at=now,
        )
    )
