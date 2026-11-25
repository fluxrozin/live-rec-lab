"""実行中インスタンスの共有コンテキスト。"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from sqlalchemy import Engine

from live_rec_lab.config import Environment
from live_rec_lab.external import Budget, ExternalGate
from live_rec_lab.media_tools import MediaTools


@dataclass
class AppContext:
    env: Environment
    engine: Engine
    gate: ExternalGate
    tools: MediaTools
    instance_id: str
    started_at: str
    port: int
    code_revision: str | None = None
    csrf_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    budget: Budget | None = None
    scheduler_disabled: bool = False
    services: dict = field(default_factory=dict)  # 稼働中のサービス（監視・録画など）
