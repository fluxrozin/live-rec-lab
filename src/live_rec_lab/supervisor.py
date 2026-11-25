"""監督: 配信監視 → セッション → 録画/コメントジョブ → 収束 → コメントSRT。単一プロセス内のasyncio。"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select, update

from live_rec_lab import archives, recording, sessions, settings_store, srt, storage, streamers
from live_rec_lab import comments as comments_mod
from live_rec_lab.context import AppContext
from live_rec_lab.db.models import recording_segments, stream_sessions
from live_rec_lab.external import ExternalDenied
from live_rec_lab.platforms.base import ERROR, LIVE, OFFLINE, UNKNOWN, LiveStatus, PlatformAdapter
from live_rec_lab.runtime import utcnow

log = logging.getLogger("live_rec_lab.supervisor")


@dataclass
class SupervisorPolicy:
    """内部ポリシー（UI設定に出さない。テストで注入）。"""

    ending_grace_s: float = 15.0
    orphan_reconfirm_s: float = 120.0
    tick_s: float = 1.0
    check_timeout_s: float = 30.0
    comment_retry_wait_s: float = 3.0
    maintenance_s: float = 60.0
    recording: recording.Policy = field(default_factory=recording.Policy)


@dataclass
class SessionRun:
    session_id: int
    streamer_id: int
    platform: str
    status: LiveStatus
    recorder: recording.Recorder | None = None
    rec_task: asyncio.Task | None = None
    collector: comments_mod.CommentCollector | None = None
    ending_since: float | None = None
    closing: bool = False
    last_live_mono: float = 0.0
    rec_job_id: int | None = None


class Supervisor:
    def __init__(self, ctx: AppContext, adapters: dict[str, PlatformAdapter], policy: SupervisorPolicy | None = None):
        self.ctx = ctx
        self.adapters = adapters
        self.policy = policy or SupervisorPolicy()
        self.runs: dict[int, SessionRun] = {}  # session_id -> run
        self.tasks: list[asyncio.Task] = []
        self.stopping = False
        self.last_check: dict[int, float] = {}
        self.diag: dict[int, dict] = {}  # streamer_id -> 直近の監視診断
        self._check_tasks: set[asyncio.Task] = set()
        self._closing_tasks: set[asyncio.Task] = set()
        from live_rec_lab.speech import SpeechService

        self.speech = SpeechService(self)
        self._last_maint = 0.0
        self.scan_state: dict = {"id": None, "running": False, "result": None}
        self._scan_cancel = False

    # ------------------------------------------------------------ settings
    def settings(self) -> dict:
        with self.ctx.engine.begin() as conn:
            return settings_store.get_all(conn)

    def root(self) -> Path:
        return Path(self.settings()["recording_root"] or str(self.ctx.env.recording_root))

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        import logging

        logging.getLogger("live_rec_lab").setLevel((await asyncio.to_thread(self.settings))["log_level"])
        if self.ctx.scheduler_disabled:
            log.warning("スケジューラは無効化されています（監視・新規ジョブを開始しません）")
            return
        await asyncio.to_thread(self._recover)
        await self._postprocess_pending()
        for name in self.adapters:
            self.tasks.append(asyncio.create_task(self._monitor_loop(name), name=f"monitor-{name}"))
        self.tasks.append(asyncio.create_task(self._housekeeping(), name="housekeeping"))

    async def stop(self, timeout: float = 25.0) -> None:
        """安全な停止: 監視停止 → 録画の回収・確定 → コメントのドレイン。"""
        self.stopping = True
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        await self.speech.stop()
        for t in list(self._check_tasks):
            t.cancel()
        await asyncio.gather(*self._check_tasks, return_exceptions=True)
        runs = list(self.runs.values())
        await asyncio.wait_for(asyncio.gather(*(self._shutdown_run(r) for r in runs), return_exceptions=True), timeout)

    async def _shutdown_run(self, run: SessionRun) -> None:
        """アプリ停止: 確認済み終了とは扱わず、取得済みを確定して収束（再起動で再開可能な終了未確認）。"""
        await self._close_jobs(run)
        await asyncio.to_thread(self._mark_unconfirmed, run.session_id, "app_stopped")

    async def _postprocess_pending(self) -> None:
        """終了処理（アプリ停止・再起動）で後処理（コメントSRT・音声・ASS）まで進めなかった収束済みセッションを再処理する。

        前回の字幕生成より後に（再開後の再）収束したセッションは、遅れて届いたコメントを反映するためSRT/ASSを作り直す。
        """
        with self.ctx.engine.begin() as conn:
            rows = conn.execute(select(
                recording_segments.c.stream_session_id, recording_segments.c.id, recording_segments.c.srt_status,
                recording_segments.c.srt_generated_at, recording_segments.c.speech_status, recording_segments.c.ass_status,
                stream_sessions.c.collection_converged_at).join(
                stream_sessions, stream_sessions.c.id == recording_segments.c.stream_session_id).where(
                stream_sessions.c.status == "ENDED", recording_segments.c.validation_status == "valid")).all()
        pending: set[int] = set()
        for sid, seg_id, srt_s, srt_at, sp_s, ass_s, converged in rows:
            stale = srt_s == "none" or (converged is not None and (srt_at is None or srt_at < converged))
            if stale:
                try:
                    await asyncio.to_thread(self.generate_segment_srt, seg_id)
                    if sp_s in ("generated", "skipped"):  # 音声が済んでいてもASSは新しいコメントで作り直す
                        await asyncio.to_thread(self.speech.generate_ass, seg_id, None, None)
                except Exception:
                    log.exception("コメントSRT/ASSの再処理に失敗: segment %s", seg_id)
            if sp_s in ("none", "failed", "blocked") or ass_s in ("none", "failed"):
                pending.add(sid)
        for sid in sorted(pending):
            self.speech.enqueue_session(sid)

    def _recover(self) -> None:
        """再起動時回収: 未確定セグメントの一時ファイルを保全のまま invalid とし、LIVE残骸を終了未確認で収束。"""
        with self.ctx.engine.begin() as conn:
            for r in conn.execute(select(recording_segments).where(recording_segments.c.validation_status == "pending")).mappings():
                conn.execute(update(recording_segments).where(recording_segments.c.id == r["id"]).values(
                    validation_status="invalid", finished_at=utcnow()))
                sessions.add_event(conn, "recovery.pending_segment", "warning",
                                   "再起動時に未確定の録画セグメントを検出（一時ファイルを保全）", r["stream_session_id"],
                                   {"temp": r["temp_path"]})
            for s in conn.execute(select(stream_sessions).where(stream_sessions.c.status.in_(("LIVE", "ENDING")))).mappings():
                conn.execute(update(stream_sessions).where(stream_sessions.c.id == s["id"]).values(
                    status="ENDED", end_reason="unconfirmed", ended_at=utcnow(), updated_at=utcnow()))
                sessions.add_event(conn, "recovery.session", "warning", "再起動時に稼働中だったセッションを終了未確認で収束",
                                   s["id"])

    # ------------------------------------------------------------ monitor
    async def _monitor_loop(self, name: str) -> None:
        adapter = self.adapters[name]
        nxt = 0.0
        while not self.stopping:
            await asyncio.sleep(self.policy.tick_s)
            now = asyncio.get_running_loop().time()
            if now < nxt:
                continue
            st = await asyncio.to_thread(self.settings)
            rate = float(st[f"{name}_monitor_rate"])
            nxt = now + 60.0 / rate * random.uniform(0.9, 1.1)
            targets = await asyncio.to_thread(self._targets, name)
            if not targets:
                continue
            target = min(targets, key=lambda s: self.last_check.get(s["id"], 0.0))
            self.last_check[target["id"]] = now
            if getattr(adapter, "external", True) and not self.ctx.gate.allow_live:
                self.diag[target["id"]] = {"state": "DISABLED", "reason": "external_denied", "at": utcnow()}
                continue
            t = asyncio.create_task(self._check_one(adapter, target), name=f"check-{target['id']}")
            self._check_tasks.add(t)
            t.add_done_callback(self._check_tasks.discard)

    def _targets(self, platform: str) -> list[dict]:
        with self.ctx.engine.begin() as conn:
            return [s for s in streamers.list_active(conn) if s["platform"] == platform and s["monitoring_enabled"]]

    async def _check_one(self, adapter: PlatformAdapter, streamer: dict) -> None:
        try:
            status = await asyncio.wait_for(adapter.check(streamer["platform_user_id"]), self.policy.check_timeout_s)
        except ExternalDenied as exc:
            status = LiveStatus(UNKNOWN, reason=f"external_denied: {exc}")
        except TimeoutError:
            status = LiveStatus(UNKNOWN, reason="timeout")
        except Exception as exc:  # 1人の失敗が全体へ波及しない
            status = LiveStatus(ERROR, reason=f"{type(exc).__name__}: {exc}"[:200])
        await self.handle_status(streamer, status)

    # ------------------------------------------------------------ status handling
    async def handle_status(self, streamer: dict, status: LiveStatus) -> None:
        sid = streamer["id"]
        prev = self.diag.get(sid, {})
        self.diag[sid] = {"state": status.state, "reason": status.reason, "at": utcnow()}
        if status.state in (UNKNOWN, ERROR) and prev.get("reason") != status.reason:
            await asyncio.to_thread(self._event, "monitor.unhealthy", "warning",
                                    f"{streamer['display_name']}: 監視が{status.state}（{status.reason}）")
        run = next((r for r in self.runs.values() if r.streamer_id == sid and not r.closing), None)
        loop = asyncio.get_running_loop()
        if status.state == LIVE:
            await asyncio.to_thread(self._mark_checked, sid, True)
            if run is None:
                if streamer["monitoring_enabled"]:
                    await self.start_session(streamer, status)
            else:
                run.last_live_mono = loop.time()
                if run.ending_since is not None:  # 猶予中にLIVE確認 → 元のセッションへ復帰
                    run.ending_since = None
                    await asyncio.to_thread(self._set_status, run.session_id, "LIVE")
                await asyncio.to_thread(self._touch, run.session_id, status.title)
                await self.reconcile(run, streamer)
        elif status.state == OFFLINE:
            await asyncio.to_thread(self._mark_checked, sid, False)
            if run is not None and status.explicit_end:
                if run.ending_since is None:
                    run.ending_since = loop.time()
                    await asyncio.to_thread(self._set_status, run.session_id, "ENDING")
                elif loop.time() - run.ending_since >= self.policy.ending_grace_s:
                    await self._end_session(run, "confirmed")  # 猶予満了時の再確認もOFFLINE
        # UNKNOWN/ERROR は終了根拠にしない

    # ------------------------------------------------------------ start / reconcile / end
    async def start_session(self, streamer: dict, status: LiveStatus) -> SessionRun | None:
        st = await asyncio.to_thread(self.settings)
        root = Path(st["recording_root"] or str(self.ctx.env.recording_root))
        if archives.was_deleted(self.ctx.env, streamer["platform"], streamer["platform_user_id"], status.stream_id):
            return None  # 削除済みアーカイブは再検知しても再取得しない
        session, created = await asyncio.to_thread(self._open, streamer, status, root)
        if session["id"] in self.runs:
            return self.runs[session["id"]]
        if session["status"] not in ("LIVE", "ENDING"):
            return None  # 確認済み終了済み（再開禁止）
        run = SessionRun(session["id"], streamer["id"], streamer["platform"], status,
                         last_live_mono=asyncio.get_running_loop().time())
        self.runs[session["id"]] = run
        await asyncio.to_thread(self._event, "session.started", "info",
                                f"{streamer['display_name']}: 配信を検知（{'新規' if created else '再開'}セッション）",
                                session["id"])
        await self.reconcile(run, streamer)
        return run

    def _open(self, streamer: dict, status: LiveStatus, root: Path):
        with self.ctx.engine.begin() as conn:
            return sessions.open_session(conn, streamer, status.stream_id, status.title, status.started_at, root)

    async def reconcile(self, run: SessionRun, streamer: dict | None = None) -> None:
        """設定に応じて録画/コメントジョブを起動・停止（重複起動しない）。"""
        if run.closing:
            return
        if streamer is None:
            streamer = await asyncio.to_thread(self._streamer, run.streamer_id)
        st = await asyncio.to_thread(self.settings)
        want_rec = streamer["monitoring_enabled"] and streamer["recording_enabled"]
        want_cmt = streamer["monitoring_enabled"] and streamer["recording_enabled"] is not None and \
            streamer["comments_enabled"] and st["comments_globally_enabled"]
        adapter = self.adapters[run.platform]
        # 録画
        rec_alive = run.rec_task is not None and not run.rec_task.done()
        if want_rec and not rec_alive and not (run.recorder and run.recorder.stop_reason == "manual"):
            if await asyncio.to_thread(self._suppressed, run.session_id, "recording"):
                pass  # 枯渇・制限等の抑止中は新ジョブを作らない（明示再開または別の配信まで）
            else:
                await self._start_recording(run, streamer, adapter, st)
        elif not want_rec and rec_alive:
            await run.recorder.stop("config_off")
            await run.rec_task
        # コメント
        cmt_alive = run.collector is not None and not run.collector.done
        if want_cmt and not cmt_alive and (run.collector is None or run.collector.final_status is None):
            await self._start_comments(run, streamer, adapter, st)
        elif not want_cmt and cmt_alive:
            await run.collector.stop("config_off")

    async def _start_recording(self, run: SessionRun, streamer: dict, adapter, st: dict) -> None:
        job_id, budget_id = await asyncio.to_thread(self._new_rec_job, run.session_id, int(st["reconnect_max_attempts"]))
        rec = recording.Recorder(recording.RecorderContext(
            engine=self.ctx.engine, tools=self.ctx.tools, adapter=adapter,
            session=await asyncio.to_thread(self._session, run.session_id), streamer=streamer, status=run.status,
            job_id=job_id, budget_id=budget_id, get_settings=self.settings, root=Path(st["recording_root"]),
            policy=self.policy.recording))
        run.recorder, run.rec_job_id = rec, job_id
        run.rec_task = asyncio.create_task(rec.run(), name=f"record-{run.session_id}")

    async def _start_comments(self, run: SessionRun, streamer: dict, adapter, st: dict) -> None:
        job_id, budget_id = await asyncio.to_thread(self._new_cmt_job, run.session_id, int(st["reconnect_max_attempts"]))
        started = run.status.started_at
        col = comments_mod.CommentCollector(
            self.ctx.engine, run.session_id, job_id, budget_id,
            lambda: adapter.comments(streamer["platform_user_id"], run.status), started,
            on_budget=self._reserve_budget, retry_wait=self.policy.comment_retry_wait_s,
            on_restricted=lambda reason, sid=run.session_id: asyncio.get_running_loop().call_soon(
                lambda: asyncio.create_task(self._restricted(sid, reason))))
        run.collector = col
        await col.start()

    def _reserve_budget(self, budget_id: int, attempt_id: str) -> str | None:
        with self.ctx.engine.begin() as conn:
            return sessions.reserve_attempt(conn, budget_id, attempt_id)

    async def streamer_changed(self, streamer_id: int) -> None:
        """配信者設定の変更を稼働中のセッションへ反映（監視OFFは安全に停止し終了未確認で収束）。"""
        run = next((r for r in self.runs.values() if r.streamer_id == streamer_id and not r.closing), None)
        if run is None:
            return
        streamer = await asyncio.to_thread(self._streamer, streamer_id)
        if not streamer["monitoring_enabled"]:
            await self._close_jobs(run)
            await asyncio.to_thread(self._mark_unconfirmed, run.session_id, "monitoring_off")
            await asyncio.to_thread(self._event, "session.stopped", "info", "監視OFFのため録画とコメント収集を停止しました（終了未確認）",
                                    run.session_id)
            self.runs.pop(run.session_id, None)
            await self.generate_session_srt(run.session_id)
            self.speech.enqueue_session(run.session_id)
        else:
            await self.reconcile(run, streamer)

    def has_active_session(self, streamer_id: int) -> bool:
        return any(r.streamer_id == streamer_id and not r.closing for r in self.runs.values())

    async def _restricted(self, session_id: int, reason: str) -> None:
        """アクセス制限が確実に判明: 録画・コメントの双方を停止し、制限解除が確認されるまで再開しない（要求§4.2.5）。"""
        run = self.runs.get(session_id)
        if run is None or run.closing:
            return
        try:
            await self._close_jobs(run)
            await asyncio.to_thread(self._mark_restricted, session_id, reason)
            self.runs.pop(session_id, None)
            await self.generate_session_srt(session_id)
            self.speech.enqueue_session(session_id)
        except Exception:
            log.exception("アクセス制限による停止処理に失敗")

    def _mark_restricted(self, session_id: int, reason: str) -> None:
        from sqlalchemy import update as upd

        from live_rec_lab.db.models import retry_budgets

        with self.ctx.engine.begin() as conn:
            conn.execute(upd(retry_budgets).where(retry_budgets.c.stream_session_id == session_id).values(
                suppression="restricted"))
            sessions.set_status(conn, session_id, "ENDED", end_reason="restricted", ended_at=utcnow(),
                                collection_converged_at=utcnow())
            sessions.add_event(conn, "session.restricted", "warning",
                               f"アクセス制限（{reason}）が確認されたため、録画とコメント収集を停止しました。制限は回避しません",
                               session_id)

    async def stop_recording(self, session_id: int) -> bool:
        """手動停止: 同じ配信を自動再開しない（停止指示を保持）。"""
        run = self.runs.get(session_id)
        if not run or not run.recorder:
            return False
        await run.recorder.stop("manual")
        return True

    async def _close_jobs(self, run: SessionRun) -> None:
        run.closing = True
        if run.recorder is not None:
            await run.recorder.stop(run.recorder.stop_reason or "session_end")
        if run.rec_task is not None:
            await asyncio.gather(run.rec_task, return_exceptions=True)
        if run.collector is not None and not run.collector.done:
            await run.collector.stop("session_end")

    async def _end_session(self, run: SessionRun, reason: str) -> None:
        if run.closing:
            return
        await self._close_jobs(run)
        await asyncio.to_thread(self._converge, run.session_id, reason)
        self.runs.pop(run.session_id, None)
        await self.generate_session_srt(run.session_id)
        self.speech.enqueue_session(run.session_id)

    # ------------------------------------------------------------ housekeeping (孤立セッション)
    async def _housekeeping(self) -> None:
        while not self.stopping:
            await asyncio.sleep(max(1.0, self.policy.tick_s))
            loop = asyncio.get_running_loop()
            if loop.time() - self._last_maint > self.policy.maintenance_s:
                self._last_maint = loop.time()
                try:
                    await asyncio.to_thread(self.maintenance)
                except Exception:
                    log.exception("保守周期の処理に失敗")
            for run in list(self.runs.values()):
                if run.closing:
                    continue
                rec_done = run.rec_task is None or run.rec_task.done()
                cmt_done = run.collector is None or run.collector.done
                if rec_done and cmt_done and loop.time() - run.last_live_mono > self.policy.orphan_reconfirm_s:
                    await self._end_session(run, "unconfirmed")
                if run.ending_since is not None and loop.time() - run.ending_since >= self.policy.ending_grace_s * 4:
                    # 監視枠の再確認が来ないまま猶予を大きく超えた場合も終了未確認で収束
                    await self._end_session(run, "unconfirmed")

    # ------------------------------------------------------------ 保守（期限・容量の自動削除、既定OFF）
    def maintenance(self) -> None:
        st = self.settings()
        if not st["auto_delete_enabled"]:
            return
        active = {r.session_id for r in self.runs.values()}
        for sid in archives.expired_candidates(self.ctx.engine, int(st["archive_retention_days"])):
            if sid in active:
                continue
            try:
                archives.delete_archive(self.ctx.engine, self.ctx.env, sid, "expired", automatic=True)
            except (archives.Refused, LookupError):
                continue
        root = Path(st["recording_root"])
        ok, _why = archives.capacity_ok(self.ctx.engine, root, st)
        if not ok and float(st["max_recording_gib"]) > 0 or (not ok and _why and "空き容量" in _why):
            for sid in archives.oldest_deletable(self.ctx.engine):
                if sid in active:
                    continue
                try:
                    archives.delete_archive(self.ctx.engine, self.ctx.env, sid, "capacity", automatic=True)
                except (archives.Refused, LookupError):
                    continue
                if archives.capacity_ok(self.ctx.engine, root, self.settings())[0]:
                    break

    def start_scan(self) -> dict:
        if self.scan_state["running"]:
            return {"scan_id": self.scan_state["id"], "status": "running", "already_running": True}
        import uuid

        self.scan_state = {"id": uuid.uuid4().hex[:10], "running": True, "result": None}
        self._scan_cancel = False
        sid = self.scan_state["id"]

        async def run() -> None:
            res = await asyncio.to_thread(archives.scan_storage, self.ctx.engine, self.root(), lambda: self._scan_cancel)
            self.scan_state.update(running=False, result=res)

        t = asyncio.create_task(run(), name=f"scan-{sid}")
        self.tasks.append(t)
        return {"scan_id": sid, "status": "running", "already_running": False}

    def cancel_scan(self) -> None:
        self._scan_cancel = True

    # ------------------------------------------------------------ SRT
    async def generate_session_srt(self, session_id: int) -> list[dict]:
        results = []
        segs = await asyncio.to_thread(self._segments, session_id)
        for seg in segs:
            if seg["validation_status"] == "valid":
                results.append(await asyncio.to_thread(self.generate_segment_srt, seg["id"]))
        return results

    def generate_segment_srt(self, segment_id: int, manual_offset_ms: int | None = None) -> dict:
        """コメントSRTを生成（初回は非上書き、再生成は管理済み置換）。0cueは対象なし。"""
        st = self.settings()
        with self.ctx.engine.begin() as conn:
            seg = dict(conn.execute(select(recording_segments).where(recording_segments.c.id == segment_id)).mappings().one())
            sess = sessions.get_session(conn, seg["stream_session_id"])
            offset = seg["srt_offset_ms"] if manual_offset_ms is None else manual_offset_ms
            try:
                text, n, method = srt.segment_srt(conn, seg, int(float(st["comment_srt_duration_seconds"]) * 1000), offset)
            except ValueError as exc:
                conn.execute(update(recording_segments).where(recording_segments.c.id == segment_id).values(
                    srt_status="failed", srt_error=str(exc)))
                return {"segment_id": segment_id, "status": "failed", "error": str(exc)}
        root = Path(sess["recording_root"])
        video = root / seg["file_path"]
        target = video.with_name(video.stem + "_comment.srt")
        try:
            existing = Path(seg["srt_path"]) if seg["srt_path"] else None
            if n == 0:
                status, ident = "skipped", None  # 対象なし（空ファイルは作らない）
            elif existing is not None and (root / existing).exists():
                ident = storage.replace_managed(root / existing, text, self._srt_ident(seg))
                status = "generated"
            else:
                ident = storage.create_text_new(target, text)
                status = "generated"
        except storage.StorageError as exc:
            with self.ctx.engine.begin() as conn:
                conn.execute(update(recording_segments).where(recording_segments.c.id == segment_id).values(
                    srt_status="failed", srt_error=str(exc)))
            return {"segment_id": segment_id, "status": "failed", "error": str(exc)}
        with self.ctx.engine.begin() as conn:
            vals = dict(srt_status=status, srt_error=None, srt_generated_at=utcnow(), srt_offset_ms=offset)
            if status == "generated":
                vals["srt_path"] = str(target.relative_to(root)) if not seg["srt_path"] else seg["srt_path"]
                vals["file_identity"] = json.dumps({**json.loads(seg["file_identity"] or "{}"), "srt": ident})
            conn.execute(update(recording_segments).where(recording_segments.c.id == segment_id).values(**vals))
        return {"segment_id": segment_id, "status": status, "cues": n, "sync": method}

    @staticmethod
    def _srt_ident(seg: dict) -> dict | None:
        try:
            return json.loads(seg["file_identity"] or "{}").get("srt")
        except ValueError:
            return None

    # ------------------------------------------------------------ DB helpers (別スレッド)
    def _event(self, t: str, sev: str, msg: str, session_id: int | None = None) -> None:
        with self.ctx.engine.begin() as conn:
            sessions.add_event(conn, t, sev, msg, session_id)

    def _mark_checked(self, streamer_id: int, live: bool) -> None:
        from live_rec_lab.db.models import streamers as st

        vals = {"last_checked_at": utcnow()}
        if live:
            vals["last_live_at"] = utcnow()
        with self.ctx.engine.begin() as conn:
            conn.execute(update(st).where(st.c.id == streamer_id).values(**vals))

    def _set_status(self, session_id: int, status: str) -> None:
        with self.ctx.engine.begin() as conn:
            sessions.set_status(conn, session_id, status)

    def _touch(self, session_id: int, title: str | None) -> None:
        with self.ctx.engine.begin() as conn:
            sessions.touch_live(conn, session_id, title)

    def _streamer(self, streamer_id: int) -> dict:
        with self.ctx.engine.begin() as conn:
            return streamers.get(conn, streamer_id)

    def _session(self, session_id: int) -> dict:
        with self.ctx.engine.begin() as conn:
            return sessions.get_session(conn, session_id)

    def _segments(self, session_id: int) -> list[dict]:
        with self.ctx.engine.begin() as conn:
            return sessions.segments_of(conn, session_id)

    def _suppressed(self, session_id: int, kind: str) -> str | None:
        with self.ctx.engine.begin() as conn:
            return sessions.suppression_of(conn, session_id, kind)

    def _new_rec_job(self, session_id: int, max_extra: int) -> tuple[int, int]:
        with self.ctx.engine.begin() as conn:
            budget = sessions.budget_for(conn, session_id, "recording", max_extra)
            return sessions.create_recording_job(conn, session_id, budget), budget

    def _new_cmt_job(self, session_id: int, max_extra: int) -> tuple[int, int]:
        with self.ctx.engine.begin() as conn:
            budget = sessions.budget_for(conn, session_id, "comments", max_extra)
            return sessions.create_comment_job(conn, session_id, budget), budget

    def _converge(self, session_id: int, reason: str) -> None:
        now = utcnow()
        with self.ctx.engine.begin() as conn:
            sessions.set_status(
                conn, session_id, "ENDED", end_reason="confirmed" if reason == "confirmed" else "unconfirmed",
                ended_at=now, collection_converged_at=now,
                platform_ended_at=now if reason == "confirmed" else None)
            sessions.add_event(conn, "session.ended", "info" if reason == "confirmed" else "warning",
                               "配信セッションを収束しました（確認済み終了）" if reason == "confirmed"
                               else "配信セッションを収束しました（終了未確認）", session_id)

    def _mark_unconfirmed(self, session_id: int, why: str) -> None:
        self._converge(session_id, "unconfirmed")
