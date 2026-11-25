"""プラットフォーム共通契約（必要最小限）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

LIVE, OFFLINE, UNKNOWN, ERROR = "LIVE", "OFFLINE", "UNKNOWN", "ERROR"


class AccessRestricted(Exception):
    """合言葉・会員限定・年齢制限等のアクセス制限が確実な情報で判明した（要求§4.2.5）。回避しない。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class LiveStatus:
    state: str  # LIVE / OFFLINE / UNKNOWN / ERROR
    stream_id: str | None = None  # Room ID / Movie ID
    title: str | None = None
    started_at: datetime | None = None  # 取得できた場合のみ（UTC）
    reason: str | None = None  # UNKNOWN/ERRORの診断コード（connect_error, http_429 等）
    explicit_end: bool = False  # 明示的な終了根拠（OFFLINEの確認）
    extra: dict = field(default_factory=dict)


@dataclass
class CommentEvent:
    message: str
    received_at: datetime
    platform_comment_id: str | None = None
    platform_user_id: str | None = None
    nickname: str | None = None
    is_anonymous: bool | None = None
    anonymous_label: str | None = None
    posted_at: datetime | None = None
    source: str = "ws"


@dataclass
class RecordSpec:
    """録画子プロセスの起動仕様。shellは使わず引数リストで渡す。"""

    argv: list[str]
    engine: str
    env: dict[str, str] = field(default_factory=dict)
    out_format: str = "ts"  # 一時出力の形式（ts / flv / mp4）
    note: dict = field(default_factory=dict)


class CommentStream(Protocol):
    def __aiter__(self) -> AsyncIterator[CommentEvent]: ...
    async def aclose(self) -> None: ...


class PlatformAdapter(Protocol):
    name: str

    async def check(self, account: str) -> LiveStatus: ...

    async def record_spec(self, account: str, status: LiveStatus, out_path: str, *, quality: str,
                          engine_order: list[str]) -> RecordSpec | None: ...

    def comments(self, account: str, status: LiveStatus) -> CommentStream: ...
