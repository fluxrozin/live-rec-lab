"""サービスごとの監視間隔。確認の時刻は配信者ごとに持つ。"""

from __future__ import annotations

import time
from collections import defaultdict


class RateGate:
    """認証単位の残枠を保守的に共有し、コメント用の予約枠を保持する。"""

    def __init__(self) -> None:
        self._remaining: int | None = None
        self.reset_at: float | None = None

    @property
    def remaining(self) -> int | None:
        if self.reset_at is not None and time.time() >= self.reset_at:
            self._remaining = None
            self.reset_at = None
        return self._remaining

    def note(self, remaining: int | None, reset_at: float | None = None) -> None:
        current = self.remaining
        if remaining is not None:
            self._remaining = max(0, remaining) if current is None else min(current, max(0, remaining))
            if reset_at is None:
                reset_at = time.time() + 60
        if reset_at is not None:
            self.reset_at = max(self.reset_at or 0, reset_at)

    def allow(self, priority: str) -> bool:
        remaining = self.remaining
        reserve = {"comments": 0, "recovery": 1, "monitor": 5, "manual": 8}[priority]
        return remaining is None or remaining > reserve

    def acquire(self, priority: str) -> bool:
        if not self.allow(priority):
            return False
        if self._remaining is not None:
            self._remaining -= 1
        return True


class ServiceSchedule:
    """定期確認と復旧確認を同じサービス枠で調整する。"""

    def __init__(self) -> None:
        self._last_request: dict[str, float] = {}
        self._account_checks: dict[str, dict[int, float]] = defaultdict(dict)

    def acquire(self, platform: str, now: float, interval: float) -> bool:
        last = self._last_request.get(platform)
        if last is not None and now - last < interval:
            return False
        self._last_request[platform] = now
        return True

    def choose(self, platform: str, eligible: list[int]) -> int | None:
        last_checks = self._account_checks[platform]
        return min(eligible, key=lambda key: (last_checks.get(key, -1), key), default=None)

    def checked(self, platform: str, account: int, now: float) -> None:
        self._account_checks[platform][account] = now


def service_interval(per_minute: float) -> float:
    return 60.0 / per_minute


def grace_seconds(per_minute: float) -> float:
    return max(2 * service_interval(per_minute), 30.0)


def personal_period(service_interval: float, count: int) -> float:
    """サービス全体の回数を、監視中の人数で分ける。"""
    return service_interval * max(count, 1)


def initial_last_check(now: float, period: float, index: int, count: int) -> float:
    """同時に始めず、人数分の枠へずらして置く。"""
    count = max(count, 1)
    slot = period / count
    return now + index * slot - period
