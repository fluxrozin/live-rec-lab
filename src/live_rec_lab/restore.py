"""停止中の手動DB復元の安全手順（要求§15.2.1）: 排他→退避→検査→マーカー→(手動交換)→照合→マーカー解除。

自動復元エンジンは設けない。マーカーがある間は通常起動・自動処理を拒否する。外部予算と削除済みの最小記録は
DBの外にあり、復元で戻らない。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

from sqlalchemy import select, update

from live_rec_lab import archives
from live_rec_lab.config import ConfigError, Environment, ensure_private_dir
from live_rec_lab.db import session as dbs
from live_rec_lab.db.models import stream_sessions, streamers
from live_rec_lab.runtime import utcnow


def marker_path(env: Environment) -> Path:
    return env.state_dir / "restore-pending.json"


def marker(env: Environment) -> dict | None:
    f = marker_path(env)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text("utf-8"))
    except (OSError, ValueError):
        return {"error": "マーカーを読めません"}


def check_candidate(path: Path) -> list[str]:
    problems = []
    if not path.is_file():
        return ["候補ファイルが存在しません"]
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            r = con.execute("pragma integrity_check").fetchone()
            if not r or r[0] != "ok":
                problems.append(f"integrity_check: {r}")
            rev = con.execute("select version_num from alembic_version").fetchone()
            if not rev or rev[0] != dbs.head_revision():
                problems.append(f"スキーマ版が現行と一致しません: {rev[0] if rev else None} != {dbs.head_revision()}")
        finally:
            con.close()
    except sqlite3.Error as exc:
        problems.append(f"候補を開けません: {exc}")
    return problems


def prepare(env: Environment, candidate: Path) -> dict:
    if marker(env):
        raise ConfigError("復元が未完了です（restore finish / abort を実行してください）")
    problems = check_candidate(candidate)
    if problems:
        raise ConfigError("候補DBを検査できません: " + "; ".join(problems))
    ensure_private_dir(env.state_dir / "restore-backups")
    backup = env.state_dir / "restore-backups" / f"pre-restore-{utcnow().replace(':', '').replace('.', '')}.sqlite3"
    if env.db_path.is_file():
        src = sqlite3.connect(f"file:{env.db_path}?mode=ro", uri=True)
        dst = sqlite3.connect(backup)
        try:
            src.backup(dst)  # SQLite Online Backup（WAL混在を避ける）
        finally:
            dst.close()
            src.close()
        backup.chmod(0o600)
    m = {"created_at": utcnow(), "candidate": str(candidate), "backup": str(backup) if env.db_path.is_file() else None,
         "db": str(env.db_path)}
    marker_path(env).write_text(json.dumps(m, ensure_ascii=False), "utf-8")
    marker_path(env).chmod(0o600)
    return m


def finish(env: Environment) -> dict:
    m = marker(env)
    if not m:
        raise ConfigError("復元マーカーがありません")
    problems = check_candidate(env.db_path)
    if problems:
        raise ConfigError("現在のDBを検査できません（マーカーを維持）: " + "; ".join(problems))
    engine = dbs.make_engine(env.db_path)
    held = []
    try:
        # 古いDBだけで稼働状態・削除済み・再送を復活させない: 稼働中の残骸を終了未確認で収束し、削除済みを照合する
        with engine.begin() as conn:
            conn.execute(update(stream_sessions).where(stream_sessions.c.status.in_(("LIVE", "ENDING"))).values(
                status="ENDED", end_reason="unconfirmed", ended_at=utcnow(), updated_at=utcnow()))
        tomb = archives.tombstone_file(env)
        if tomb.exists():
            for line in tomb.read_text("utf-8").splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                with engine.begin() as conn:
                    row = conn.execute(select(stream_sessions.c.id, stream_sessions.c.status).select_from(
                        stream_sessions.join(streamers, streamers.c.id == stream_sessions.c.streamer_id)).where(
                        streamers.c.platform == r["platform"], streamers.c.platform_user_id == r["account"],
                        stream_sessions.c.platform_stream_id == r["stream_id"])).first()
                if row:  # 削除済みアーカイブの復活防止: 既存の削除処理で再削除（世代確認つき）
                    try:
                        # 手動収束済み扱いにしてから通常の削除経路を使う
                        with engine.begin() as conn:
                            conn.execute(update(stream_sessions).where(stream_sessions.c.id == row[0]).values(
                                manual_convergence_at=utcnow()))
                        res = archives.delete_archive(engine, env, row[0], "restore")
                        if res["status"] != "completed":
                            held.append(f"session {row[0]}: {res.get('reason')}")
                    except archives.Refused as exc:
                        held.append(f"session {row[0]}: {exc}")
    finally:
        engine.dispose()
    if held:
        raise ConfigError("照合できない対象があるためマーカーを維持します: " + "; ".join(held))
    marker_path(env).unlink()
    return {"cleared": True}


def abort(env: Environment) -> dict:
    m = marker(env)
    if not m:
        raise ConfigError("復元マーカーがありません")
    if m.get("backup") and Path(m["backup"]).is_file():
        for suffix in ("", "-wal", "-shm"):
            Path(str(env.db_path) + suffix).unlink(missing_ok=True)
        shutil.copy2(m["backup"], env.db_path)
    marker_path(env).unlink()
    return {"restored_backup": m.get("backup")}
