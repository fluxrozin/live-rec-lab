"""録画ジョブ: 子プロセス管理・進捗監視・有限再試行・セグメント確定（要求§6、設計§9.4）。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import signal
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Engine, delete, update

from live_rec_lab import media_tools, sessions, storage
from live_rec_lab.db.models import recording_jobs, recording_segments
from live_rec_lab.platforms.base import LiveStatus, PlatformAdapter
from live_rec_lab.runtime import utcnow

log = logging.getLogger("live_rec_lab.recording")


@dataclass
class Policy:
    """内部ポリシー（利用者設定ではない・テストで注入可）。"""

    start_timeout_s: float = 60.0  # 最初のメディアまでの待機
    stall_timeout_s: float = 45.0  # 出力が増えない期間
    poll_s: float = 0.5
    backoff_base_s: float = 5.0
    backoff_cap_s: float = 60.0
    min_keep_bytes: int = 4096  # これ未満は空試行
    stop_grace_s: float = 12.0


@dataclass
class AttemptResult:
    kind: str  # kept / empty / invalid / spec_unavailable
    exit_code: int | None = None
    reason: str | None = None
    ended_by_stop: bool = False
    got_data: bool = False
    first_media_at: str | None = None
    capture_ended_at: str | None = None


@dataclass
class RecorderContext:
    engine: Engine
    tools: media_tools.MediaTools
    adapter: PlatformAdapter
    session: dict
    streamer: dict
    status: LiveStatus
    job_id: int
    budget_id: int
    get_settings: Callable[[], dict]
    root: Path
    policy: Policy = field(default_factory=Policy)
    on_budget: Callable[[int, str], str | None] | None = None


async def terminate_group(proc: asyncio.subprocess.Process, grace: float) -> None:
    """子プロセスグループを INT → TERM → KILL の順で回収する。"""
    if proc.returncode is not None:
        return
    for sig, wait in ((signal.SIGINT, grace), (signal.SIGTERM, 5.0), (signal.SIGKILL, 5.0)):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, sig)
        try:
            await asyncio.wait_for(proc.wait(), wait)
            return
        except TimeoutError:
            continue


class Recorder:
    def __init__(self, rc: RecorderContext) -> None:
        self.rc = rc
        self.stop_requested = asyncio.Event()
        self.stop_reason: str | None = None
        self.current: asyncio.subprocess.Process | None = None
        self.final_status = "PENDING"
        self.attempts = 0
        self.kept_segments = 0
        self.interrupted_at: str | None = None

    async def stop(self, reason: str) -> None:
        self.stop_reason = self.stop_reason or reason
        self.stop_requested.set()

    # -------------------------------------------------------------- main loop
    async def run(self) -> str:
        rc = self.rc
        await asyncio.to_thread(self._job, status="STARTING")
        failures = 0
        outcome = "COMPLETED"
        while True:
            ok, why = await asyncio.to_thread(self._capacity)
            if not ok:  # 容量・空き不足: 予算を消費せず待機（条件回復で再開）。停止指示を優先
                await asyncio.to_thread(self._note, f"容量のため録画を保留: {why}")
                await asyncio.to_thread(self._job, status="RECONNECTING", last_error=why)
                try:
                    await asyncio.wait_for(self.stop_requested.wait(), 60.0)
                    outcome = "COMPLETED" if self.kept_segments else "CANCELLED"
                    break
                except TimeoutError:
                    continue
            kind = await asyncio.to_thread(self._reserve)
            if kind is None:
                outcome = "FAILED" if self.kept_segments == 0 else "PARTIAL"
                await asyncio.to_thread(self._note, "retry_exhausted")
                break
            self.attempts += 1
            attempt_started_at = utcnow()
            res = await self._attempt()
            if res.kind == "kept":
                self.kept_segments += 1
                resumed_at = res.first_media_at or attempt_started_at
                if self.interrupted_at and self.interrupted_at < resumed_at:
                    await asyncio.to_thread(self._gap, self.interrupted_at, resumed_at)
                self.interrupted_at = None
            else:
                self.interrupted_at = self.interrupted_at or attempt_started_at
            if self.stop_requested.is_set() or res.ended_by_stop:
                outcome = "COMPLETED" if res.kind == "kept" or self.kept_segments else "CANCELLED"
                if self.stop_reason == "manual":
                    await asyncio.to_thread(self._manual_stop)
                break
            # 有効な区間の直後だけ新しい中断を始める。空試行を挟んでも最初の時刻を保つ。
            if res.kind == "kept":
                self.interrupted_at = res.capture_ended_at or attempt_started_at
            if res.kind == "spec_unavailable":
                await asyncio.to_thread(self._suppress, "limited")
                outcome = "FAILED" if self.kept_segments == 0 else "PARTIAL"
                break
            failures = 0 if res.got_data else failures + 1
            await asyncio.to_thread(self._job, status="RECONNECTING", last_error=res.reason)
            delay = min(rc.policy.backoff_cap_s, rc.policy.backoff_base_s * (2 ** max(0, failures - 1)))
            delay *= random.uniform(0.8, 1.2)
            try:
                await asyncio.wait_for(self.stop_requested.wait(), delay)
                outcome = "COMPLETED" if self.kept_segments else "CANCELLED"
                break
            except TimeoutError:
                pass
            # ライブが継続している間だけ再試行（監督側が stop を呼ぶと上のwaitで抜ける）
        await asyncio.to_thread(self._job, status=outcome, finished_at=utcnow())
        self.final_status = outcome
        return outcome

    # -------------------------------------------------------------- one attempt
    async def _attempt(self) -> AttemptResult:
        rc = self.rc
        st = await asyncio.to_thread(self._settings)
        s = await asyncio.to_thread(self._naming)
        directory = rc.root / rc.streamer["folder_name"] / sessions.date_folder_for(s, st["display_timezone"])
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return AttemptResult("spec_unavailable", reason=f"保存先を作成できません: {exc}")
        attempt_id = uuid.uuid4().hex[:10]
        raw = directory / f".rec-{attempt_id}.raw{storage.TMP_SUFFIX}"
        spec = await rc.adapter.record_spec(rc.streamer["platform_user_id"], rc.status, str(raw),
                                            quality=st["recording_quality"], engine_order=st["tiktok_engine_order"])
        if spec is None:
            return AttemptResult("spec_unavailable", reason="録画に使える経路がありません")
        seg_id = await asyncio.to_thread(self._add_pending, spec.engine, str(raw), attempt_id)
        await asyncio.to_thread(self._job, status="RECORDING", recorder_engine=spec.engine)
        env = {**os.environ, **spec.env}
        proc = await asyncio.create_subprocess_exec(*spec.argv, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                                                    start_new_session=True, env=env)
        self.current = proc
        tail: list[bytes] = []

        async def drain() -> None:
            assert proc.stderr
            while chunk := await proc.stderr.read(2048):
                tail.append(chunk)
                del tail[:-4]

        drain_task = asyncio.create_task(drain())
        started = time.monotonic()
        first_data: float | None = None
        first_data_at: str | None = None
        last_growth_at: str | None = None
        last_size, last_growth = 0, started
        stalled = False
        while proc.returncode is None:
            await asyncio.sleep(rc.policy.poll_s)
            size = raw.stat().st_size if raw.exists() else 0
            now = time.monotonic()
            if size > last_size:
                last_growth_at = utcnow()
                if first_data is None and size > 0:
                    first_data, first_data_at = now, last_growth_at
                last_size, last_growth = size, now
            limit = rc.policy.stall_timeout_s if first_data else rc.policy.start_timeout_s
            if now - (last_growth if first_data else started) > limit:
                stalled = True
                break
            if self.stop_requested.is_set():
                break
        if proc.returncode is None:
            await terminate_group(proc, rc.policy.stop_grace_s)
        capture_ended_at = (last_growth_at if stalled else None) or utcnow()
        await drain_task
        self.current = None
        stderr_tail = b"".join(tail)[-600:].decode("utf-8", "replace")
        by_stop = self.stop_requested.is_set()
        got = raw.exists() and raw.stat().st_size >= rc.policy.min_keep_bytes
        reason = None
        if stalled:
            reason = "stalled" if first_data else "no_media"
        elif proc.returncode not in (0, None) and not by_stop:
            reason = f"exit_{proc.returncode}"
        res = await asyncio.to_thread(self._finalize, seg_id, raw, directory, spec, first_data_at, got, reason,
                                      stderr_tail, st)
        res.exit_code, res.ended_by_stop, res.got_data = proc.returncode, by_stop, got and res.kind != "preroll"
        res.first_media_at, res.capture_ended_at = first_data_at, capture_ended_at
        res.reason = res.reason or reason
        hook = getattr(rc.adapter, "record_result", None)
        if hook is not None and not by_stop and res.kind != "preroll":
            hook(rc.streamer["platform_user_id"], spec.engine, res.kind == "kept", res.reason)
        return res

    # -------------------------------------------------------------- finalize (別スレッド)
    def _finalize(self, seg_id: int, raw: Path, directory: Path, spec, first_data_at, got: bool, reason,
                  stderr_tail: str, st: dict) -> AttemptResult:
        rc = self.rc
        if not got:
            raw.unlink(missing_ok=True)
            with rc.engine.begin() as conn:
                conn.execute(delete(recording_segments).where(recording_segments.c.id == seg_id))
                sessions.add_event(conn, "recording.empty_attempt", "warning",
                                   f"空の録画試行を破棄しました（{reason or 'no_data'}）", rc.session["id"],
                                   {"engine": spec.engine, "stderr": stderr_tail[-300:]})
            return AttemptResult("empty", reason=reason or "no_data")
        tools = rc.tools
        pr = media_tools.probe(tools.ffprobe, raw)
        inspect = getattr(rc.adapter, "inspect_head", None)
        if inspect is not None:
            # プリロール判定は正式保存・採番の前に行う（本編と判別できない候補は保全する）
            verdict = inspect(tools, raw, pr)
            if verdict and verdict["action"] == "drop":
                raw.unlink(missing_ok=True)
                with rc.engine.begin() as conn:
                    conn.execute(delete(recording_segments).where(recording_segments.c.id == seg_id))
                    sessions.add_event(conn, "recording.preroll_excluded", "info",
                                       f"確認済みのプリロールのみの取得を正式保存から除外しました（{verdict['reason']}）",
                                       rc.session["id"], {"engine": spec.engine, "duration_ms": pr.duration_ms})
                return AttemptResult("preroll", reason="preroll_only")
            if verdict and verdict["action"] == "trim":
                trimmed = raw.with_name(raw.name.replace(".raw", ".trim"))
                r = subprocess.run([tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{verdict['start']:.3f}",
                                    "-i", str(raw), "-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-f", spec.out_format
                                    if spec.out_format != "flv" else "mpegts", str(trimmed)],
                                   capture_output=True, text=True, timeout=600)
                if r.returncode == 0 and trimmed.exists() and trimmed.stat().st_size > 0:
                    with rc.engine.begin() as conn:
                        sessions.add_event(conn, "recording.preroll_trimmed", "info",
                                           f"先頭の確認済みプリロール（{verdict['start']:.2f}秒）を除外し本編から保存します",
                                           rc.session["id"], {"engine": spec.engine})
                    raw.unlink(missing_ok=True)
                    trimmed.rename(raw)
                    pr = media_tools.probe(tools.ffprobe, raw)
                # トリムに失敗した場合は判別不能として全体を保全（下の通常処理へ）
        mp4_tmp = directory / f".rec-{seg_id}.mp4{storage.TMP_SUFFIX}"
        container, final_tmp, ext = "mp4", None, ".mp4"
        probe_final: media_tools.Probe | None = None
        # ライブFLV等は読取時に長さが取れない。映像コーデックが識別できればstream copyでMP4化し、確定後の媒体で長さ・形式を検証する
        if pr.ok or (pr.video_codec and pr.video_codec != "unknown"):
            cmd = [tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw), "-map", "0:v:0",
                   "-map", "0:a?", "-c", "copy", "-f", "mp4", "-movflags", "+faststart"]
            if pr.video_codec == "hevc":
                cmd += ["-tag:v", "hvc1"]
            r = subprocess.run(cmd + [str(mp4_tmp)], capture_output=True, text=True, timeout=1800)
            if r.returncode == 0:
                probe_final = media_tools.probe(tools.ffprobe, mp4_tmp)
            if probe_final is not None and probe_final.ok:
                final_tmp = mp4_tmp
            else:
                mp4_tmp.unlink(missing_ok=True)
                # MP4化できない場合は元形式が再生可能と確認できたときだけ代替成功
                final_tmp, container, ext, probe_final = raw, spec.out_format, "." + spec.out_format, pr
        if final_tmp is None or probe_final is None or not probe_final.ok:
            with rc.engine.begin() as conn:
                conn.execute(update(recording_segments).where(recording_segments.c.id == seg_id).values(
                    validation_status="invalid", finished_at=utcnow(), temp_path=str(raw),
                    container_format=pr.container, video_codec=pr.video_codec, file_size=pr.size,
                    duration_ms=pr.duration_ms, recorder_engine=spec.engine, first_media_at=first_data_at))
                sessions.add_event(conn, "recording.invalid_segment", "error",
                                   f"録画ファイルを検証できませんでした（一時ファイルを保全）: {pr.error}",
                                   rc.session["id"], {"temp": str(raw)})
                sessions.add_gap(conn, rc.session["id"], "recording", None, utcnow(), "invalid_media",
                                 start_basis="unknown", end_basis="measured")
            return AttemptResult("invalid", reason=pr.error)
        # 保持すべき実データ: 番号とstemを確定→非上書きで正式名へ
        with rc.engine.begin() as conn:
            number, stem, final_dir = sessions.reserve_number_and_name(
                conn, rc.session["id"], rc.root, rc.streamer["folder_name"], st["display_timezone"],
                rc.streamer["platform"])
        final = final_dir / (stem + ext)
        # 一時ファイルは最終ファイルと同一ディレクトリ（同日付フォルダ）にある
        try:
            ident = storage.commit_new(final_tmp, final)
        except storage.StorageError as exc:
            with rc.engine.begin() as conn:
                conn.execute(update(recording_segments).where(recording_segments.c.id == seg_id).values(
                    validation_status="invalid", segment_number=number, finished_at=utcnow(), temp_path=str(final_tmp)))
                sessions.add_event(conn, "recording.commit_failed", "error",
                                   f"正式名への確定に失敗（一時ファイルを保全）: {exc}", rc.session["id"])
            return AttemptResult("invalid", reason=str(exc))
        if final_tmp is not raw:
            raw.unlink(missing_ok=True)
        rel = str(final.relative_to(rc.root))
        with rc.engine.begin() as conn:
            conn.execute(update(recording_segments).where(recording_segments.c.id == seg_id).values(
                segment_number=number, file_path=rel, temp_path=None, file_size=ident["size"],
                duration_ms=probe_final.duration_ms, container_format=container, video_codec=probe_final.video_codec,
                validation_status="valid", finished_at=utcnow(), first_media_at=first_data_at,
                sync_method="first_data_observed" if first_data_at else "approx_process_start",
                file_identity=json.dumps(ident), recorder_engine=spec.engine))
            sessions.add_event(conn, "recording.segment_committed", "info", f"セグメント s{number:03d} を確定しました",
                               rc.session["id"], {"path": rel, "engine": spec.engine})
        return AttemptResult("kept")

    # -------------------------------------------------------------- DB helpers (別スレッド)
    def _capacity(self) -> tuple[bool, str | None]:
        from live_rec_lab import archives

        return archives.capacity_ok(self.rc.engine, self.rc.root, self.rc.get_settings())

    def _reserve(self) -> str | None:
        with self.rc.engine.begin() as conn:
            return sessions.reserve_attempt(conn, self.rc.budget_id, f"r{self.rc.job_id}-{self.attempts + 1}")

    def _job(self, **vals) -> None:
        with self.rc.engine.begin() as conn:
            sessions.update_job(conn, recording_jobs, self.rc.job_id, **vals)

    def _add_pending(self, engine: str, tmp: str, attempt_id: str) -> int:
        with self.rc.engine.begin() as conn:
            return sessions.add_pending_segment(conn, self.rc.job_id, self.rc.session["id"], engine, tmp, attempt_id)

    def _gap(self, a: str, b: str) -> None:
        with self.rc.engine.begin() as conn:
            sessions.add_gap(conn, self.rc.session["id"], "recording", a, b, "recording_reconnect",
                             start_basis="approximate", end_basis="approximate")

    def _suppress(self, reason: str) -> None:
        with self.rc.engine.begin() as conn:
            sessions.suppress(conn, self.rc.budget_id, reason)

    def _manual_stop(self) -> None:
        with self.rc.engine.begin() as conn:
            sessions.suppress(conn, self.rc.budget_id, "manual_stop")
            sessions.update_job(conn, recording_jobs, self.rc.job_id, manual_stop_requested=True)

    def _note(self, msg: str) -> None:
        with self.rc.engine.begin() as conn:
            sessions.add_event(conn, "recording.budget", "warning", f"録画の再試行を終了: {msg}", self.rc.session["id"])

    def _settings(self) -> dict:
        return self.rc.get_settings()

    def _naming(self) -> dict:
        st = self.rc.get_settings()
        with self.rc.engine.begin() as conn:
            return sessions.ensure_naming(conn, self.rc.session["id"], st["display_timezone"])
