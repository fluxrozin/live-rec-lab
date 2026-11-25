"""起動モードと外部通信排他の回帰確認（通信なし）。"""

import httpx
import pytest

from live_rec_lab import external, server
from live_rec_lab.config import ConfigError, Environment


@pytest.mark.parametrize(
    ("dev", "allow_live", "disable_scheduler", "permitted", "label"),
    [(False, False, False, True, "許可（有効な登録対象）"),
     (False, False, True, False, "無効（スケジューラ停止中）"),
     (True, False, False, False, "既定で拒否"),
     (True, True, False, True, "許可（--allow-live）")],
)
def test_start_external_gate(tmp_path, monkeypatch, dev, allow_live, disable_scheduler, permitted, label):
    monkeypatch.setattr(external, "LOCK_FILE", tmp_path / "shared" / "external.lock")
    flags = {"LIVE_REC_LAB_DISABLE_SCHEDULER": "1"} if disable_scheduler else {}
    env = Environment(dev, tmp_path / "data", tmp_path / "state", tmp_path / "recordings", 8083, flags=flags)
    url = "https://www.tiktok.com/"
    other = external.LiveLock()

    class OfflineServer:
        def __init__(self, config):
            self.app = config.app

        async def serve(self, *, sockets):
            ctx = self.app.state.ctx
            if permitted:
                ctx.gate.check("tiktok", url)
                with pytest.raises(ConfigError, match="共通ロック"):
                    other.acquire()
            else:
                with pytest.raises(external.ExternalDenied):
                    ctx.gate.check("tiktok", url)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app), base_url=f"http://127.0.0.1:{ctx.port}"
            ) as client:
                response = await client.get("/system")
                assert response.status_code == 200
                assert f"<dd>{label}</dd>" in response.text

    monkeypatch.setattr(server, "bind_port", lambda port: None)
    monkeypatch.setattr(server.uvicorn, "Server", OfflineServer)
    if not permitted:
        other.acquire()
    try:
        assert server.run_server(env, allow_live=allow_live) == 0
    finally:
        other.release()
    other.acquire()
    try:
        if permitted:
            with pytest.raises(ConfigError, match="共通ロック"):
                server.run_server(env, allow_live=allow_live)
    finally:
        other.release()
