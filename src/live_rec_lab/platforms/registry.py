"""実アダプターの組み立て。資格情報が未設定のサービスは構築しても外部通信を保留する。"""

from __future__ import annotations

from live_rec_lab import settings_store
from live_rec_lab.context import AppContext
from live_rec_lab.external import Budget
from live_rec_lab.platforms.tiktok import TikTokAdapter
from live_rec_lab.platforms.twitcasting import TwitCastingAdapter


def build_adapters(ctx: AppContext) -> dict:
    budget = ctx.budget or Budget()
    ctx.budget = budget

    def poll() -> float:
        with ctx.engine.begin() as conn:
            return float(settings_store.get(conn, "twitcasting_comment_poll_seconds"))

    return {
        "tiktok": TikTokAdapter(ctx.gate, budget, ctx.env.secrets),
        "twitcasting": TwitCastingAdapter(ctx.gate, budget, ctx.env.secrets, poll, ctx.tools.ffmpeg, ctx.tools.https),
    }
