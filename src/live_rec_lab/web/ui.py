"""テンプレート環境・表示変換（時刻はIANAタイムゾーン、保存はUTC）。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from live_rec_lab import queries, settings_store

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

NAV = [
    ("main", "メイン", [("dashboard", "/", "ダッシュボード", "dash"), ("streamers", "/streamers", "配信者", "tower"),
                      ("recordings", "/recordings", "録画履歴", "film"), ("comments", "/comments", "コメント", "msg"),
                      ("speech", "/speech", "音声字幕", "file")]),
    ("system", "システム", [("settings", "/settings", "設定", "gear"), ("system", "/system", "システム状態", "act"),
                          ("logs", "/logs", "ログ", "list")]),
]
TITLES = {k: label for _g, _n, items in NAV for k, _h, label, _i in items}


def parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def make_filters(tz: str):
    zone = ZoneInfo(tz)

    def dt(iso: str | None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
        d = parse(iso)
        return "—" if d is None else d.astimezone(zone).strftime(fmt)

    def hhmmss(ms) -> str:
        if not ms:
            return "—"
        s = int(ms // 1000)
        return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"

    def size(n) -> str:
        if n is None:
            return "—"
        n = float(n)
        for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
            if n < 1024 or unit == "TiB":
                return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
            n /= 1024
        return "—"

    def ago(iso: str | None) -> str:
        d = parse(iso)
        if d is None:
            return "—"
        sec = int((datetime.now(UTC) - d).total_seconds())
        if sec < 60:
            return f"{max(sec, 0)}秒前"
        if sec < 3600:
            return f"{sec // 60}分前"
        if sec < 86400:
            return f"{sec // 3600}時間前"
        return f"{sec // 86400}日前"

    return {"dt": dt, "hhmmss": hhmmss, "size": size, "ago": ago}


def base_context(request: Request, active: str, **extra) -> dict:
    ctx = request.app.state.ctx
    with ctx.engine.begin() as conn:
        st = settings_store.get_all(conn)
        rec_now = bool(queries.active_sessions(conn) and any(s["recording"] for s in queries.active_sessions(conn)))
    tz = st["display_timezone"]
    env = templates.env
    for name, fn in make_filters(tz).items():
        env.filters[name] = fn
    return {"request": request, "app_ctx": ctx, "csrf": ctx.csrf_token, "font": st["display_font"], "tz": tz,
            "refresh": st["web_refresh_interval_seconds"], "nav": NAV, "active": active, "title": TITLES.get(active, ""),
            "recording_active": None if ctx.scheduler_disabled else rec_now,
            "now": datetime.now(ZoneInfo(tz)).strftime("%H:%M:%S"), "settings": st, "mode": ctx.env.mode, **extra}


def render(request: Request, template: str, active: str, status_code: int = 200, **extra):
    return templates.TemplateResponse(request, template, base_context(request, active, **extra), status_code=status_code)


def safe(html: str) -> Markup:
    return Markup(html)
