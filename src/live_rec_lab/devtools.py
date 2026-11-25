"""開発用: 隔離環境への配信者登録（共通の登録サービスを呼ぶ短い入口）。通常運用では使えない。

    uv run python -m live_rec_lab.devtools register tiktok <ID> --name 表示名 --monitor --record --comments
"""

from __future__ import annotations

import argparse
import sys

from live_rec_lab import settings_store, streamers
from live_rec_lab.config import ConfigError, ensure_private_dir, resolve_environment
from live_rec_lab.db import session as dbs
from live_rec_lab.runtime import InstanceLock


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="live_rec_lab.devtools")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("register")
    r.add_argument("platform")
    r.add_argument("account")
    r.add_argument("--name")
    for flag in ("monitor", "record", "comments"):
        r.add_argument(f"--{flag}", action=argparse.BooleanOptionalAction, default=True)
    m = sub.add_parser("fetch-model", help="音声モデルを隔離のモデル領域へ取得（利用者許可済みの明示的な準備工程）")
    m.add_argument("model", choices=["large-v3", "large-v3-turbo"])
    a = p.parse_args(argv)
    if a.cmd == "fetch-model":
        return fetch_model(a.model)
    env = resolve_environment(True)  # 隔離専用
    ensure_private_dir(env.data_dir)
    ensure_private_dir(env.runtime_dir)
    lock = InstanceLock(env.runtime_dir / "instance.lock")
    try:
        lock.acquire()  # 稼働中のインスタンスがあれば拒否（Web登録を使う）
    except ConfigError as exc:
        print(f"拒否しました: {exc}", file=sys.stderr)
        return 3
    try:
        dbs.migrate(env.db_path)
        eng = dbs.make_engine(env.db_path)
        with eng.begin() as conn:
            settings_store.ensure_defaults(conn, {"recording_root": str(env.recording_root)})
            row = streamers.register(conn, streamers.StreamerInput(
                a.platform, a.account, a.name, a.monitor, a.record, a.comments))
        eng.dispose()
    finally:
        lock.release()
    print(f"登録しました: id={row['id']} {row['platform']}:{row['platform_user_id']} folder={row['folder_name']}")
    return 0


def fetch_model(name: str) -> int:
    """モデルは隔離のデータ領域 models/ 配下だけに取得する（通常キャッシュ・通常資格情報を継承しない）。"""
    import os

    env = resolve_environment(True)
    models = env.model_dir
    ensure_private_dir(models)
    os.environ["HF_HOME"] = str(models / ".hf-home")  # ライブラリ既定キャッシュを使わない
    os.environ.pop("HF_TOKEN", None)
    os.environ.pop("HUGGING_FACE_HUB_TOKEN", None)
    from faster_whisper.utils import download_model

    path = download_model(name, cache_dir=str(models))
    print(f"取得しました: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
