"""CLI入口: start / restart / status / url / stop / backup / config / diagnose。"""

from __future__ import annotations

import argparse
import os
import signal
import sqlite3
import sys
from pathlib import Path

from live_rec_lab import server, settings_store
from live_rec_lab.config import ConfigError, Environment, ensure_private_dir, resolve_environment
from live_rec_lab.db import session as dbs
from live_rec_lab.runtime import InstanceLock, lock_held, read_endpoint, wait_until
from live_rec_lab.settings_defs import SettingError, validate_value


def _env(args: argparse.Namespace) -> Environment:
    return resolve_environment(bool(getattr(args, "dev", False)))


def cmd_start(args: argparse.Namespace) -> int:
    env = _env(args)
    return server.run_server(env, allow_live=args.allow_live)


def cmd_restart(args: argparse.Namespace) -> int:
    env = _env(args)
    if args.allow_live and not env.dev:
        raise ConfigError("--allow-live は隔離（--dev）起動専用です")
    stopped = _stop(env)
    if stopped != 0:
        return stopped
    return server.run_server(env, allow_live=args.allow_live)


def cmd_status(args: argparse.Namespace) -> int:
    env = _env(args)
    ep = read_endpoint(env)
    if not ep:
        print(f"停止中 (mode={env.mode})")
        return 1
    print(f"稼働中 (mode={env.mode}) {ep['url']} pid={ep['pid']} instance={ep['instance_id']} 起動={ep['started_at']}")
    return 0


def cmd_url(args: argparse.Namespace) -> int:
    ep = read_endpoint(_env(args))
    if not ep:
        print("稼働中のインスタンスがありません", file=sys.stderr)
        return 1
    print(ep["url"])
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    return _stop(_env(args))


def _stop(env: Environment) -> int:
    ep = read_endpoint(env)
    if not ep:
        print("稼働中のインスタンスはありません")
        return 0
    os.kill(int(ep["pid"]), signal.SIGTERM)
    if wait_until(lambda: not lock_held(env.runtime_dir / "instance.lock"), 30):
        print("停止しました")
        return 0
    print("停止が確認できません（強制終了はしません）", file=sys.stderr)
    return 1


def cmd_backup(args: argparse.Namespace) -> int:
    env = _env(args)
    dest = Path(args.dest).expanduser()
    if not dest.is_absolute():
        dest = Path.cwd() / dest
    if dest.exists():
        print(f"既に存在するため上書きしません: {dest}", file=sys.stderr)
        return 2
    if not env.db_path.is_file():
        print("DBがありません", file=sys.stderr)
        return 2
    ensure_private_dir(dest.parent) if not dest.parent.exists() else None
    src = sqlite3.connect(f"file:{env.db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    os.chmod(dest, 0o600)
    print(f"バックアップを作成しました: {dest}")
    print("注意: バックアップにはコメント本文など個人情報が含まれます。保存場所の権限と共有範囲に注意してください。")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    env = _env(args)
    if args.key != "web_port":
        print("設定できるキーは web_port のみです", file=sys.stderr)
        return 2
    if args.action == "get":
        print(server.configured_port(env))
        return 0
    try:
        port = validate_value("web_port", int(args.value))
    except (ValueError, SettingError) as exc:
        print(f"不正な値です: {exc}", file=sys.stderr)
        return 2
    ensure_private_dir(env.data_dir)
    ensure_private_dir(env.runtime_dir)
    lock = InstanceLock(env.runtime_dir / "instance.lock")
    lock.acquire()  # 稼働中は拒否
    try:
        dbs.migrate(env.db_path)
        eng = dbs.make_engine(env.db_path)
        with eng.begin() as conn:
            settings_store.ensure_defaults(conn, {"recording_root": str(env.recording_root)})
            settings_store.update(conn, {"web_port": port}, None)
        eng.dispose()
    finally:
        lock.release()
    print(f"web_port={port} を保存しました（次回起動から反映, mode={env.mode}）")
    return 0


def cmd_diagnose(args: argparse.Namespace) -> int:
    from live_rec_lab.diagnose import run_diagnose

    return run_diagnose(_env(args), as_json=args.json)


def cmd_restore(args: argparse.Namespace) -> int:
    from live_rec_lab import restore

    env = _env(args)
    ensure_private_dir(env.runtime_dir)
    lock = InstanceLock(env.runtime_dir / "instance.lock")
    lock.acquire()  # 停止中のみ（稼働中は拒否）
    try:
        if args.step == "prepare":
            m = restore.prepare(env, Path(args.candidate).expanduser())
            print("復元の準備ができました。次の手順で手動交換してください:")
            print(f"  1. 退避済み: {m['backup']}")
            print(f"  2. DBを手動交換: {m['candidate']} → {m['db']}（-wal/-shm は削除）")
            print("  3. `live-rec-lab restore finish` で照合してマーカーを解除")
            print("復元前のバックアップが残っている古いコピーにも個人情報が含まれます。不要になったら削除してください。")
        elif args.step == "finish":
            restore.finish(env)
            print("照合が完了し、マーカーを解除しました")
        else:
            r = restore.abort(env)
            print(f"退避DBへ差し戻しました: {r['restored_backup']}")
    finally:
        lock.release()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="live-rec-lab")
    sub = p.add_subparsers(dest="command", required=True)

    def add(name: str, fn, help_: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--dev", action="store_true", help="隔離データを対象にする")
        sp.set_defaults(fn=fn)
        return sp

    for command in (
        add("start", cmd_start, "起動"),
        add("restart", cmd_restart, "正常停止を確認して起動"),
    ):
        command.add_argument("--allow-live", action="store_true", help="隔離の実接続を許可（永続化しない）")
    add("status", cmd_status, "稼働状態")
    add("url", cmd_url, "URL表示")
    add("stop", cmd_stop, "停止")
    b = add("backup", cmd_backup, "DBバックアップ")
    b.add_argument("dest")
    c = add("config", cmd_config, "設定の確認・変更（停止中）")
    c.add_argument("action", choices=["get", "set"])
    c.add_argument("key")
    c.add_argument("value", nargs="?")
    r = add("restore", cmd_restore, "停止中の手動DB復元（prepare/finish/abort）")
    r.add_argument("step", choices=["prepare", "finish", "abort"])
    r.add_argument("candidate", nargs="?")
    d = add("diagnose", cmd_diagnose, "診断")
    d.add_argument("--json", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except ConfigError as exc:
        print(f"起動/操作を拒否しました: {exc}", file=sys.stderr)
        return 3
    except dbs.UnknownDatabase as exc:
        print(str(exc), file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
