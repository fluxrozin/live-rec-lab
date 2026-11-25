"""音声SRT・統合ASSの後処理（収集収束後、同時推論は1件、子プロセスで推論）。要求§7.9〜7.10。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

from sqlalchemy import select, update

from live_rec_lab import ass, media_tools, sessions, srt, storage
from live_rec_lab.db.models import recording_segments, stream_sessions
from live_rec_lab.runtime import utcnow

log = logging.getLogger("live_rec_lab.speech")
_TC = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{3})")


def parse_srt(text: str) -> list[tuple[int, int, str]]:
    cues = []
    for block in re.split(r"\n\s*\n", text.strip().replace("\r\n", "\n")):
        lines = block.split("\n")
        for i, ln in enumerate(lines):
            m = _TC.search(ln)
            if m:
                g = list(map(int, m.groups()))
                a = ((g[0] * 60 + g[1]) * 60 + g[2]) * 1000 + g[3]
                b = ((g[4] * 60 + g[5]) * 60 + g[6]) * 1000 + g[7]
                cues.append((a, b, "\n".join(lines[i + 1:]).strip()))
                break
    return cues


def clamp_cues(raw: list[dict], duration_ms: int) -> list[tuple[int, int, str]]:
    """全cueを媒体長内に収め、空・逆転・範囲外cueを発行しない。"""
    out = []
    for s in raw:
        text = (s.get("text") or "").strip()
        a, b = int(float(s["start"]) * 1000), int(float(s["end"]) * 1000)
        if not text or a >= duration_ms or a < 0:
            continue
        b = min(b, duration_ms)
        if b > a:
            out.append((a, b, text))
    out.sort()
    return out


class SpeechService:
    def __init__(self, sup) -> None:
        self.sup = sup
        self.ctx = sup.ctx
        self._sem = asyncio.Semaphore(1)  # 同時推論は1件
        self._session_locks: dict[int, asyncio.Lock] = {}
        self.tasks: set[asyncio.Task] = set()

    def _lock(self, session_id: int) -> asyncio.Lock:
        return self._session_locks.setdefault(session_id, asyncio.Lock())

    def enqueue_session(self, session_id: int) -> None:
        t = asyncio.create_task(self._session_job(session_id), name=f"speech-session-{session_id}")
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    def enqueue_segment(self, segment_id: int) -> None:
        t = asyncio.create_task(self.process_segment(segment_id), name=f"speech-segment-{segment_id}")
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    async def _session_job(self, session_id: int) -> None:
        st = await asyncio.to_thread(self.sup.settings)
        segs = await asyncio.to_thread(self.sup._segments, session_id)
        for seg in segs:
            if seg["validation_status"] != "valid":
                continue
            done = seg["speech_status"] in ("generated", "skipped")
            try:  # 1セグメントの失敗が他の処理を止めない
                if st["speech_subtitles_enabled"] and not done:
                    await self.process_segment(seg["id"])  # 完了後にASSも生成する
                elif seg["ass_status"] in ("none", "failed"):
                    await asyncio.to_thread(self.generate_ass, seg["id"], None, None)
            except Exception as exc:
                log.exception("後処理に失敗: segment %s", seg["id"])
                await asyncio.to_thread(self._set, seg["id"], ass_status="failed", ass_error=f"{type(exc).__name__}: {exc}"[:300])

    async def stop(self) -> None:
        for t in list(self.tasks):
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    # ---------------------------------------------------------------- 推論
    async def process_segment(self, segment_id: int) -> dict:
        async with self._lock(await asyncio.to_thread(self._session_id_of, segment_id)):
            async with self._sem:
                return await self._process(segment_id)

    def _session_id_of(self, segment_id: int) -> int:
        with self.ctx.engine.begin() as conn:
            return conn.execute(select(recording_segments.c.stream_session_id).where(
                recording_segments.c.id == segment_id)).scalar_one()

    async def _process(self, segment_id: int) -> dict:
        seg, sess = await asyncio.to_thread(self._load, segment_id)
        if sess["status"] != "ENDED" or seg["validation_status"] != "valid":
            return {"segment_id": segment_id, "status": "rejected", "error": "収束済みの確定動画のみ処理できます"}
        st = await asyncio.to_thread(self.sup.settings)  # 設定スナップショット
        root = Path(sess["recording_root"])
        video = root / seg["file_path"]
        await asyncio.to_thread(self._set, segment_id, speech_status="running", speech_error=None)
        tmpdir = self.ctx.env.data_dir / "tmp"
        tmpdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        out_json = Path(tempfile.mkdtemp(prefix="speech-", dir=tmpdir)) / "out.json"
        argv = [sys.executable, "-m", "live_rec_lab.speech_worker", "--video", str(video), "--out", str(out_json),
                "--model-dir", str(self.ctx.env.model_dir), "--model", st["speech_model"],
                "--language", st["speech_language"], "--device", st["speech_device"],
                "--ffmpeg", self.ctx.tools.ffmpeg or "ffmpeg"]
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "LD_LIBRARY_PATH")}
        try:
            proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                        stdin=asyncio.subprocess.DEVNULL, env=env, start_new_session=True)
            stdout, _stderr = await proc.communicate()
        except asyncio.CancelledError:
            with_suppress_kill(proc)
            await asyncio.to_thread(self._set, segment_id, speech_status="none", speech_error="中断されました")
            raise
        status_line = {}
        for line in reversed(stdout.decode("utf-8", "replace").splitlines()):
            try:
                status_line = json.loads(line)
                break
            except ValueError:
                continue
        if proc.returncode == 2:
            reason = status_line.get("reason", "blocked")
            await asyncio.to_thread(self._set, segment_id, speech_status="blocked", speech_error=reason)
            await asyncio.to_thread(self._event, "speech.blocked", "warning", f"音声文字起こしを実行できません: {reason}", seg)
            await asyncio.to_thread(self.generate_ass, segment_id, None, reason)
            return {"segment_id": segment_id, "status": "blocked", "reason": reason}
        if proc.returncode != 0 or not out_json.exists():
            reason = f"{status_line.get('reason', 'failed')}: {status_line.get('detail', '')}"[:300]
            await asyncio.to_thread(self._set, segment_id, speech_status="failed", speech_error=reason)
            await asyncio.to_thread(self._event, "speech.failed", "error", f"音声文字起こしに失敗: {reason}", seg)
            await asyncio.to_thread(self.generate_ass, segment_id, None, reason)
            return {"segment_id": segment_id, "status": "failed", "reason": reason}
        data = json.loads(out_json.read_text("utf-8"))
        out_json.unlink(missing_ok=True)
        with_suppress_rmdir(out_json.parent)
        result = await asyncio.to_thread(self._publish_speech, seg, sess, data)
        await asyncio.to_thread(self.generate_ass, segment_id, None, None)
        return result

    # ---------------------------------------------------------------- 公開（別スレッド）
    def _publish_speech(self, seg: dict, sess: dict, data: dict) -> dict:
        root = Path(sess["recording_root"])
        video = root / seg["file_path"]
        target = video.with_name(video.stem + "_speech.srt")
        cues = clamp_cues(data.get("segments", []), seg["duration_ms"])
        idents = json.loads(seg["file_identity"] or "{}")
        existing = root / seg["speech_srt_path"] if seg["speech_srt_path"] else None
        try:
            if not cues:
                # 無発話/音声なし: 空SRTは作らない。管理済みの旧版は所有確認のうえ安全に除去
                if existing and existing.exists() and storage.same_identity(storage.identity(existing), idents.get("speech") or {}):
                    existing.unlink()
                    idents.pop("speech", None)
                status, path = ("skipped", None)
                note = "音声なし" if data.get("no_audio") else "発話なし"
            else:
                text = "".join(f"{i}\n{srt.tc(a)} --> {srt.tc(b)}\n{t}\n\n" for i, (a, b, t) in enumerate(cues, 1))
                if existing and existing.exists():
                    ident = storage.replace_managed(existing, text, idents.get("speech"))
                    path = seg["speech_srt_path"]
                else:
                    ident = storage.create_text_new(target, text)
                    path = str(target.relative_to(root))
                idents["speech"] = ident
                status, note = "generated", None
        except storage.StorageError as exc:
            self._set(seg["id"], speech_status="failed", speech_error=str(exc))
            return {"segment_id": seg["id"], "status": "failed", "reason": str(exc)}
        self._set(seg["id"], speech_status=status, speech_error=note, speech_srt_path=path,
                  speech_generated_at=utcnow(), file_identity=json.dumps(idents))
        return {"segment_id": seg["id"], "status": status, "cues": len(cues)}

    def generate_ass(self, segment_id: int, _unused, speech_problem: str | None) -> dict:
        """統合ASSを生成（両入力が終端のとき）。片側が不足でも部分ASSを生成し不足理由を残す。"""
        seg, sess = self._load(segment_id)
        st = self.sup.settings()
        root = Path(sess["recording_root"])
        video = root / seg["file_path"]
        if not video.exists():
            self._set(seg["id"], ass_status="failed", ass_error="動画ファイルが見つかりません")
            return {"segment_id": seg["id"], "status": "failed", "reason": "動画ファイルが見つかりません"}
        idents = json.loads(seg["file_identity"] or "{}")
        # コメント入力
        try:
            with self.ctx.engine.begin() as conn:
                start, _m = srt.media_start_of(seg)
                dur = seg["duration_ms"]
                rows = srt.comments_for_window(conn, seg["stream_session_id"], start, start + timedelta(milliseconds=dur + 1000))
            comment_cues = [(a, b, t) for a, b, t in srt.build_cues(rows, start, dur, int(float(st["comment_srt_duration_seconds"]) * 1000),
                                                                     seg["srt_offset_ms"])]
            comment_ok = True
        except Exception as exc:
            comment_cues, comment_ok = [], False
            speech_problem = speech_problem or f"comment: {exc}"
        # 音声入力（管理済みSRTを解析）
        speech_cues: list[tuple[int, int, str]] = []
        speech_ok = seg["speech_status"] in ("generated", "skipped")
        if seg["speech_status"] == "generated" and seg["speech_srt_path"] and (root / seg["speech_srt_path"]).exists():
            speech_cues = parse_srt((root / seg["speech_srt_path"]).read_text("utf-8"))
        problems = []
        if not speech_ok:
            problems.append(f"音声: {seg['speech_status']}{(' ' + (speech_problem or seg['speech_error'] or '')) if (speech_problem or seg['speech_error']) else ''}")
        if not comment_ok:
            problems.append("コメント入力を取得できません")
        target = video.with_name(video.stem + ".ass")
        existing = root / seg["ass_path"] if seg["ass_path"] else None
        has_cues = bool(speech_cues or comment_cues)
        if not has_cues:
            if speech_ok and comment_ok:  # 両方が正常に0cue → 対象なし。管理済みの既存ASSを安全除去
                if existing and existing.exists() and storage.same_identity(storage.identity(existing), idents.get("ass") or {}):
                    existing.unlink()
                    idents.pop("ass", None)
                self._set(seg["id"], ass_status="skipped", ass_error=None, ass_path=None, ass_generated_at=utcnow(),
                          file_identity=json.dumps(idents))
                return {"segment_id": seg["id"], "status": "skipped"}
            self._set(seg["id"], ass_error="; ".join(problems) or "入力なし（既存の正常版を保持）")
            return {"segment_id": seg["id"], "status": "kept", "problems": problems}
        pr = media_tools.probe(self.ctx.tools.ffprobe, video)
        w, h = (pr.width or 1280), (pr.height or 720)
        text, diag = ass.build(w, h, ass.speech_display(speech_cues, seg["duration_ms"]), comment_cues, seg["duration_ms"])
        try:
            if existing and existing.exists():
                ident = storage.replace_managed(existing, text, idents.get("ass"), "utf-8-sig")
                path = seg["ass_path"]
            else:
                ident = storage.create_text_new(target, text, "utf-8-sig")
                path = str(target.relative_to(root))
        except storage.StorageError as exc:
            self._set(seg["id"], ass_status="failed", ass_error=str(exc))
            return {"segment_id": seg["id"], "status": "failed", "reason": str(exc)}
        idents["ass"] = ident
        note = "; ".join(problems + ([f"コメント{diag['omitted_comments']}件を省略（レーン飽和）"] if diag["omitted_comments"] else [])) or None
        self._set(seg["id"], ass_status="generated", ass_error=note, ass_path=path, ass_generated_at=utcnow(),
                  file_identity=json.dumps(idents))
        return {"segment_id": seg["id"], "status": "generated", "partial": bool(problems), **diag}

    # ---------------------------------------------------------------- DB helpers
    def _load(self, segment_id: int):
        with self.ctx.engine.begin() as conn:
            seg = dict(conn.execute(select(recording_segments).where(recording_segments.c.id == segment_id)).mappings().one())
            sess = dict(conn.execute(select(stream_sessions).where(stream_sessions.c.id == seg["stream_session_id"])).mappings().one())
        return seg, sess

    def _set(self, segment_id: int, **vals) -> None:
        with self.ctx.engine.begin() as conn:
            conn.execute(update(recording_segments).where(recording_segments.c.id == segment_id).values(**vals))

    def _event(self, t: str, sev: str, msg: str, seg: dict) -> None:
        with self.ctx.engine.begin() as conn:
            sessions.add_event(conn, t, sev, msg, seg["stream_session_id"])


def with_suppress_kill(proc) -> None:
    import contextlib
    import signal

    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGTERM)


def with_suppress_rmdir(p: Path) -> None:
    import contextlib

    with contextlib.suppress(OSError):
        p.rmdir()
