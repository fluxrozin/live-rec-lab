"""手動復元: 排他・退避・検査・マーカー・削除済み記録の照合。"""

from __future__ import annotations

import shutil
import sqlite3

import pytest
from sqlalchemy import func, select

from live_rec_lab import archives, restore
from live_rec_lab.config import ConfigError
from live_rec_lab.db import session as dbs
from live_rec_lab.db.models import stream_sessions
from test_archives import _seed


def test_restore_flow_blocks_and_rechecks_tombstones(ctx, tmp_path):
    sid, v, c = _seed(ctx)
    ctx.engine.dispose()
    old = tmp_path / "old-backup.sqlite3"  # 削除前の状態を持つ古いバックアップ
    src = sqlite3.connect(ctx.env.db_path)
    dst = sqlite3.connect(old)
    src.backup(dst)
    dst.close()
    src.close()
    engine = dbs.make_engine(ctx.env.db_path)
    assert archives.delete_archive(engine, ctx.env, sid)["status"] == "completed"
    engine.dispose()
    bad = tmp_path / "broken.sqlite3"
    bad.write_bytes(b"not sqlite")
    with pytest.raises(ConfigError):
        restore.prepare(ctx.env, bad)  # 検査に失敗した候補は拒否
    m = restore.prepare(ctx.env, old)
    assert restore.marker(ctx.env) and m["backup"]
    with pytest.raises(ConfigError):
        restore.prepare(ctx.env, old)  # マーカー中は二重に開始しない
    for suffix in ("-wal", "-shm"):
        (ctx.env.db_path.parent / (ctx.env.db_path.name + suffix)).unlink(missing_ok=True)
    shutil.copy(old, ctx.env.db_path)  # 利用者の手動交換
    restore.finish(ctx.env)
    assert restore.marker(ctx.env) is None
    engine = dbs.make_engine(ctx.env.db_path)
    with engine.begin() as conn:  # 古いDBからの「削除済みアーカイブの復活」は防がれる
        assert conn.execute(select(func.count()).select_from(stream_sessions)).scalar() == 0
    engine.dispose()
