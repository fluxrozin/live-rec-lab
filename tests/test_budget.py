import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from live_rec_lab.budget import BudgetDenied, ExternalBudget, Limit, LivePermission

START = datetime(2026, 11, 24, tzinfo=UTC)


def test_budget_charges_before_operation_and_persists_across_restart(tmp_path):
    path = tmp_path / "shared" / "external-budget.sqlite3"
    path.parent.mkdir(mode=0o700)
    permission = LivePermission("twitcasting", "night-boat", frozenset({"detect"}), START + timedelta(minutes=5))
    caps = (Limit("twitcasting", 2, timedelta(minutes=1)),)
    with (
        pytest.raises(RuntimeError),
        ExternalBudget(path).connection(
            permission,
            "twitcasting",
            "night-boat",
            "detect",
            caps,
            now=START,
        ),
    ):
        raise RuntimeError("network outcome unknown")
    with ExternalBudget(path).connection(permission, "twitcasting", "night-boat", "detect", caps, now=START):
        pass
    with (
        pytest.raises(BudgetDenied, match="quota"),
        ExternalBudget(path).connection(
            permission,
            "twitcasting",
            "night-boat",
            "detect",
            caps,
            now=START,
        ),
    ):
        pytest.fail("quota reset after reopening the store")
    with ExternalBudget(path).connection(
        permission,
        "twitcasting",
        "night-boat",
        "detect",
        caps,
        now=START + timedelta(minutes=1),
    ):
        pass


def test_denied_and_expired_requests_do_not_open_the_budget_store(tmp_path):
    path = tmp_path / "shared" / "external-budget.sqlite3"
    budget = ExternalBudget(path)
    limit = (Limit("tiktok-signing", 1, timedelta(minutes=1)),)
    expired = LivePermission("tiktok", "night-boat", frozenset({"comments"}), START)
    for permission in (None, expired):
        with (
            pytest.raises(BudgetDenied, match="permission"),
            budget.connection(
                permission,
                "tiktok",
                "night-boat",
                "comments",
                limit,
                now=START,
            ),
        ):
            pytest.fail("request entered without a current permission")
    assert not path.exists()


def test_only_one_external_owner_and_shared_cooldown(tmp_path):
    path = tmp_path / "shared" / "external-budget.sqlite3"
    path.parent.mkdir(mode=0o700)
    permit = LivePermission("twitcasting", "night-boat", frozenset({"detect"}), START + timedelta(minutes=5))
    limit = (Limit("twitcasting", 60, timedelta(minutes=1)),)
    args = (permit, "twitcasting", "night-boat", "detect", limit)
    with ExternalBudget(path).connection(*args, now=START) as active:
        with pytest.raises(BudgetDenied, match="busy"), ExternalBudget(path).connection(*args, now=START):
            pytest.fail("second external owner was admitted")
        active.defer("twitcasting", START + timedelta(seconds=75))
    with (
        pytest.raises(BudgetDenied, match="cooldown"),
        ExternalBudget(path).connection(
            *args,
            now=START + timedelta(seconds=60),
        ),
    ):
        pytest.fail("restart ignored the shared wait")
    with sqlite3.connect(path) as database:
        assert database.execute("SELECT COUNT(*) FROM external_requests").fetchone() == (1,)
