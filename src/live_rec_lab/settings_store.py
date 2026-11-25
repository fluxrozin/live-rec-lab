"""system_settings の型付きJSON永続化と設定版（全値を原子的に検証・保存）。"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Connection, select
from sqlalchemy.dialects.sqlite import insert

from live_rec_lab.db.models import system_settings
from live_rec_lab.runtime import utcnow
from live_rec_lab.settings_defs import DEFINITIONS, INTERNAL_KEYS, SettingError, validate_value

VERSION_KEY = "settings_version"


class VersionConflict(Exception):
    def __init__(self, current: int) -> None:
        super().__init__("設定が他で更新されています")
        self.current = current


def _raw(conn: Connection) -> dict[str, Any]:
    return {r.key: json.loads(r.value) for r in conn.execute(select(system_settings))}


def ensure_defaults(conn: Connection, initial: dict[str, Any] | None = None) -> None:
    """欠落キーのみ既定値を補完（保存済みは上書きしない）。initialは未登録時の初期値（録画ルート等）。"""
    have = set(_raw(conn))
    now = utcnow()
    rows = []
    for key, d in DEFINITIONS.items():
        if key in have:
            continue
        value = (initial or {}).get(key, d.default)
        if value is None:
            continue
        rows.append({"key": key, "value": json.dumps(value), "updated_at": now})
    if VERSION_KEY not in have:
        rows.append({"key": VERSION_KEY, "value": "1", "updated_at": now})
    if rows:
        conn.execute(insert(system_settings).on_conflict_do_nothing(), rows)


def get_all(conn: Connection) -> dict[str, Any]:
    raw = _raw(conn)
    out = {k: raw.get(k, d.default) for k, d in DEFINITIONS.items()}
    out[VERSION_KEY] = raw.get(VERSION_KEY, 1)
    return out


def get(conn: Connection, key: str) -> Any:
    return get_all(conn)[key]


def public_settings(conn: Connection) -> dict[str, Any]:
    allv = get_all(conn)
    return {k: v for k, v in allv.items() if k not in INTERNAL_KEYS}


def update(conn: Connection, values: dict[str, Any], expected_version: int | None) -> int:
    """全値を検証して版比較のうえ一括保存。失敗時は何も保存しない。"""
    errors: dict[str, str] = {}
    clean: dict[str, Any] = {}
    for k, v in values.items():
        if k in ("settings_version",) or k not in DEFINITIONS:
            errors[k] = "未知の設定キーです"
            continue
        try:
            clean[k] = validate_value(k, v)
        except SettingError as exc:
            errors[k] = str(exc)
    if errors:
        raise SettingErrors(errors)
    cur = int(_raw(conn).get(VERSION_KEY, 1))
    if expected_version is not None and expected_version != cur:
        raise VersionConflict(cur)
    now = utcnow()
    rows = [{"key": k, "value": json.dumps(v), "updated_at": now} for k, v in clean.items()]
    rows.append({"key": VERSION_KEY, "value": json.dumps(cur + 1), "updated_at": now})
    stmt = insert(system_settings)
    conn.execute(stmt.on_conflict_do_update(
        index_elements=["key"], set_={"value": stmt.excluded.value, "updated_at": stmt.excluded.updated_at}), rows)
    return cur + 1


class SettingErrors(Exception):
    def __init__(self, fields: dict[str, str]) -> None:
        super().__init__("設定値が不正です")
        self.fields = fields
