"""起動シーケンス: 排他 → DB移行 → 設定 → ポート確保 → endpoint公開 → Uvicorn。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import signal
import sqlite3
from pathlib import Path

import uvicorn
from sqlalchemy import update

from live_rec_lab import settings_store
from live_rec_lab.app import create_app
from live_rec_lab.config import ConfigError, Environment, ensure_private_dir
from live_rec_lab.context import AppContext
from live_rec_lab.db import session as dbs
from live_rec_lab.db.models import runtime_instances
from live_rec_lab.external import ExternalGate, LiveLock
from live_rec_lab.logs import register_secrets, setup_logging
from live_rec_lab.media_tools import resolve
from live_rec_lab.runtime import (
    Endpoint,
    InstanceLock,
    bind_port,
    new_instance_id,
    remove_endpoint,
    utcnow,
)

log = logging.getLogger("live_rec_lab.server")


def code_revision() -> str:
    """起動時の実行ソース内容（未コミット変更を含む）から確定する識別子。"""
    h = hashlib.sha256()
    for p in sorted(Path(__file__).resolve().parent.rglob("*.py")):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:12]


def configured_port(env: Environment) -> int:
    """DB保存値（無ければ既定）。DB未作成なら作成しない。"""
    if env.db_path.is_file():
        try:
            con = sqlite3.connect(f"file:{env.db_path}?mode=ro", uri=True)
            try:
                r = con.execute("select value from system_settings where key='web_port'").fetchone()
            finally:
                con.close()
            if r:
                return int(r[0])
        except sqlite3.Error:
            pass
    return env.default_port


def run_server(env: Environment, *, allow_live: bool = False) -> int:
    ensure_private_dir(env.data_dir)
    ensure_private_dir(env.state_dir)
    ensure_private_dir(env.runtime_dir)
    if env.dev and str(env.recording_root).startswith(str(env.data_dir.parent)):
        ensure_private_dir(env.recording_root)  # 隔離の専用録画領域
    setup_logging(env.log_dir)
    register_secrets(env.secrets.values())
    scheduler_disabled = env.flags.get("LIVE_REC_LAB_DISABLE_SCHEDULER", "0") not in ("0", "")
    if env.flags.get("LIVE_REC_LAB_DISABLE_SCHEDULER", "0") not in ("0", "1", ""):
        raise ConfigError("LIVE_REC_LAB_DISABLE_SCHEDULER は 0 か 1 のみ指定できます")
    if allow_live and not env.dev:
        raise ConfigError("--allow-live は隔離（--dev）起動専用です")

    from live_rec_lab import restore

    if restore.marker(env):
        raise ConfigError("DB復元が未完了です（マーカーあり）。`live-rec-lab restore finish|abort` を実行してください")
    lock = InstanceLock(env.runtime_dir / "instance.lock")
    lock.acquire()
    live_lock = LiveLock()
    sock = None
    instance_id = new_instance_id()
    try:
        if allow_live:
            live_lock.acquire()
        dbs.migrate(env.db_path)
        os.chmod(env.db_path, 0o600)
        engine = dbs.make_engine(env.db_path)
        with engine.begin() as conn:
            settings_store.ensure_defaults(conn, {"recording_root": str(env.recording_root)})
            if env.dev:
                # 隔離のポートは隔離既定（保存値 > 既定）。初期値は隔離既定を補完。
                saved = settings_store.get(conn, "web_port")
                if saved == 8082 and env.default_port != 8082:
                    settings_store.update(conn, {"web_port": env.default_port}, None)
            port = int(settings_store.get(conn, "web_port"))
        sock = bind_port(port)
        tools = resolve(env.flags.get("LIVE_REC_LAB_FFMPEG_BIN"))
        started = utcnow()
        ctx = AppContext(
            env=env, engine=engine, gate=ExternalGate(allow_live, live_lock if allow_live else None),
            tools=tools, instance_id=instance_id, started_at=started, port=port,
            code_revision=code_revision(), scheduler_disabled=scheduler_disabled,
        )
        with engine.begin() as conn:
            conn.execute(runtime_instances.insert().values(
                instance_id=instance_id, started_at=started, last_heartbeat_at=started,
                process_id=os.getpid(), status="running", code_revision=ctx.code_revision))
        app = create_app(ctx)
        url = f"http://127.0.0.1:{port}"
        Endpoint(url, port, os.getpid(), started, instance_id, env.mode).write(env)
        print(f"LiveRecorder ({env.mode}) {url}  instance={instance_id} ffmpeg={tools.ffmpeg_version}", flush=True)
        if not tools.ok:
            log.warning("ffmpeg/ffprobe 診断: %s", tools.error)
        config = uvicorn.Config(app, log_level="warning", lifespan="on", access_log=False)
        server = uvicorn.Server(config)

        async def main() -> None:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, lambda: setattr(server, "should_exit", True))
            await server.serve(sockets=[sock])

        asyncio.run(main())
        with engine.begin() as conn:
            conn.execute(update(runtime_instances).where(runtime_instances.c.instance_id == instance_id)
                         .values(stopped_at=utcnow(), status="stopped"))
        engine.dispose()
        return 0
    finally:
        remove_endpoint(env, instance_id)
        if sock is not None:
            sock.close()
        live_lock.release()
        lock.release()
