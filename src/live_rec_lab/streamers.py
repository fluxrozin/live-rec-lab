"""配信者の入力検証・正規化・登録/編集/論理削除（要求§5.1.1）。UI/API/CLIが共通で呼ぶ業務サービス。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from sqlalchemy import Connection, select, update

from live_rec_lab import storage
from live_rec_lab.db.models import streamers
from live_rec_lab.runtime import utcnow

PLATFORMS = ("tiktok", "twitcasting")
_TIKTOK_ID = re.compile(r"^[A-Za-z0-9_.]{2,24}$")
_TWITCAS_ID = re.compile(r"^[A-Za-z0-9_]{1,50}$")


class InputError(ValueError):
    def __init__(self, fields: dict[str, str]) -> None:
        super().__init__("入力が不正です")
        self.fields = fields


class Conflict(Exception):
    pass


class NotFound(Exception):
    pass


def _clean(raw: str) -> str:
    if not isinstance(raw, str):
        raise InputError({"account": "文字列で指定してください"})
    s = raw.strip()
    if not s:
        raise InputError({"account": "IDまたはURLを入力してください"})
    if len(s) > 2048:
        raise InputError({"account": "長すぎます（最大2048文字）"})
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in s) or "\\" in s:
        raise InputError({"account": "使用できない文字が含まれます"})
    return s


def _check_url(s: str, hosts: tuple[str, ...]) -> list[str]:
    u = urlsplit(s)
    if u.scheme != "https":
        raise InputError({"account": "URLはHTTPSのみ指定できます"})
    if u.username or u.password or (u.port not in (None, 443)):
        raise InputError({"account": "資格情報や非既定ポートを含むURLは使用できません"})
    if (u.hostname or "").lower() not in hosts:
        raise InputError({"account": "対応していないホストです"})
    if u.query or u.fragment:
        raise InputError({"account": "クエリやフラグメント付きのURLは使用できません"})
    if "%" in u.path and unquote(u.path) != u.path.replace("%40", "@"):
        raise InputError({"account": "符号化されたパスは使用できません"})
    return [p for p in u.path.split("/")]


def normalize_account(platform: str, raw: str) -> str:
    """プラットフォーム別に検証し正規IDを返す。未確認の形式は黙って受理しない。"""
    if platform not in PLATFORMS:
        raise InputError({"platform": "対応していないサービスです"})
    s = _clean(raw)
    if platform == "tiktok":
        if s.startswith(("http://", "https://")):
            parts = _check_url(s, ("tiktok.com", "www.tiktok.com", "m.tiktok.com"))
            segs = [p for p in parts if p != ""]
            if not (len(segs) in (1, 2) and segs[0].startswith("@") and (len(segs) == 1 or segs[1] == "live")):
                raise InputError({"account": "TikTokは /@ID または /@ID/live のURLのみ対応しています"})
            ident = segs[0][1:]
        else:
            ident = s[1:] if s.startswith("@") else s
        if not _TIKTOK_ID.match(ident) or ident in (".", "..") or ".." in ident:
            raise InputError({"account": "TikTokのIDは英数字・_・.の2〜24文字で指定してください"})
        return ident.lower()
    if s.startswith(("http://", "https://")):
        parts = _check_url(s, ("twitcasting.tv", "www.twitcasting.tv"))
        segs = [p for p in parts if p != ""]
        if len(segs) != 1:
            raise InputError({"account": "ツイキャスは /ID のURLのみ対応しています"})
        ident = segs[0]
    else:
        ident = s
    if not _TWITCAS_ID.match(ident):
        raise InputError({"account": "ツイキャスのIDは英数字・_の1〜50文字で指定してください"})
    return ident.lower()


def profile_url(platform: str, account: str) -> str:
    return f"https://www.tiktok.com/@{account}" if platform == "tiktok" else f"https://twitcasting.tv/{account}"


def _display_name(name: str | None, account: str) -> str:
    if name is None or not name.strip():
        return account
    n = name.strip()
    if len(n) > 100:
        raise InputError({"display_name": "表示名は100文字以内で指定してください"})
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in n):
        raise InputError({"display_name": "制御文字は使用できません"})
    return n


def _folder_name(conn: Connection, account: str) -> str:
    """配信者フォルダ名。同じ正規ID（サービス横断）は同一フォルダ。別IDが同名に写像されたときだけ識別子を付加。"""
    same = conn.execute(select(streamers.c.folder_name).where(
        streamers.c.platform_user_id == account, streamers.c.folder_name.isnot(None))).first()
    if same:
        return same[0]
    base = storage.safe_component(account, fallback="streamer")
    taken = {r[0].casefold() for r in conn.execute(select(streamers.c.folder_name).where(
        streamers.c.folder_name.isnot(None)))}
    name = base
    if name.casefold() in taken or name != account:
        name = f"{base}_{storage.folder_token(account)}"
    n = 8
    while name.casefold() in taken:
        n += 2
        name = f"{base}_{storage.folder_token(account + str(n)) }"
    return name


@dataclass
class StreamerInput:
    platform: str
    account: str
    display_name: str | None = None
    monitoring_enabled: bool | None = None
    recording_enabled: bool | None = None
    comments_enabled: bool | None = None


def register(conn: Connection, inp: StreamerInput) -> dict:
    flags = {"monitoring_enabled": inp.monitoring_enabled, "recording_enabled": inp.recording_enabled,
             "comments_enabled": inp.comments_enabled}
    errs = {k: "true/falseを明示してください" for k, v in flags.items() if not isinstance(v, bool)}
    if errs:
        raise InputError(errs)
    account = normalize_account(inp.platform, inp.account)
    name = _display_name(inp.display_name, account)
    now = utcnow()
    row = conn.execute(select(streamers).where(
        streamers.c.platform == inp.platform, streamers.c.platform_user_id == account)).mappings().first()
    if row is not None:
        if row["archived_at"] is None:
            raise Conflict("同じ配信者が既に登録されています")
        order = _next_order(conn)
        conn.execute(update(streamers).where(streamers.c.id == row["id"]).values(
            archived_at=None, display_name=name, sort_order=order, updated_at=now, **flags))
        return get(conn, row["id"])
    res = conn.execute(streamers.insert().values(
        platform=inp.platform, platform_user_id=account, display_name=name, profile_url=profile_url(inp.platform, account),
        sort_order=_next_order(conn), folder_name=_folder_name(conn, account), created_at=now, updated_at=now, **flags))
    return get(conn, res.inserted_primary_key[0])


def _next_order(conn: Connection) -> int:
    from sqlalchemy import func

    m = conn.execute(select(func.max(streamers.c.sort_order)).where(streamers.c.archived_at.is_(None))).scalar()
    return (m or 0) + 1


def get(conn: Connection, streamer_id: int) -> dict:
    row = conn.execute(select(streamers).where(streamers.c.id == streamer_id)).mappings().first()
    if row is None:
        raise NotFound()
    return dict(row)


def list_active(conn: Connection) -> list[dict]:
    q = select(streamers).where(streamers.c.archived_at.is_(None)).order_by(streamers.c.sort_order, streamers.c.id)
    return [dict(r) for r in conn.execute(q).mappings()]


def update_settings(conn: Connection, streamer_id: int, **values) -> dict:
    allowed = {"display_name", "monitoring_enabled", "recording_enabled", "comments_enabled"}
    bad = set(values) - allowed
    if bad:
        raise InputError({k: "変更できません" for k in bad})
    cur = get(conn, streamer_id)
    if cur["archived_at"]:
        raise NotFound()
    upd = {}
    for k, v in values.items():
        if k == "display_name":
            upd[k] = _display_name(v, cur["platform_user_id"])
        elif not isinstance(v, bool):
            raise InputError({k: "true/falseで指定してください"})
        else:
            upd[k] = v
    conn.execute(update(streamers).where(streamers.c.id == streamer_id).values(updated_at=utcnow(), **upd))
    return get(conn, streamer_id)


def archive(conn: Connection, streamer_id: int) -> None:
    """論理削除（履歴は残す）。稼働中の確認は呼び出し側（監督）で行う。"""
    cur = get(conn, streamer_id)
    if cur["archived_at"]:
        return
    conn.execute(update(streamers).where(streamers.c.id == streamer_id).values(
        archived_at=utcnow(), sort_order=None, monitoring_enabled=False, updated_at=utcnow()))
