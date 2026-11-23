"""状態の表示名。"""

from __future__ import annotations

LIVE_LABELS = {
    "OFFLINE": "オフライン",
    "LIVE": "配信中",
    "UNKNOWN": "未確認",
    "ERROR": "障害",
    "DISABLED": "監視停止",
}

SESSION_LABELS = {
    "LIVE": "配信中",
    "ENDING": "終了確認中",
    "ENDED": "終了",
    "ENDED_UNCONFIRMED": "終了未確認",
}

RECORDING_LABELS = {
    "PENDING": "待機",
    "STARTING": "開始中",
    "RECORDING": "録画中",
    "RECONNECTING": "再接続中",
    "FINALIZING": "確定中",
    "COMPLETED": "完了",
    "PARTIAL": "部分完了",
    "FAILED": "失敗",
    "CANCELLED": "取り消し",
}

COMMENT_LABELS = {
    "PENDING": "待機",
    "CONNECTING": "接続中",
    "COLLECTING": "収集中",
    "RECONNECTING": "再接続中",
    "COMPLETED": "完了",
    "PARTIAL": "部分完了",
    "FAILED": "失敗",
    "DISABLED": "無効",
}

ACTIVE_RECORDING = {"PENDING", "STARTING", "RECORDING", "RECONNECTING", "FINALIZING"}
ACTIVE_COMMENTS = {"PENDING", "CONNECTING", "COLLECTING", "RECONNECTING"}
OPEN_SESSION = {"LIVE", "ENDING"}
