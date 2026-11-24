"""Read-only machine-readable runtime diagnosis."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime

from .endpoint import fixture_status
from .isolation import IsolationError, Workspace


def diagnose_application(workspace: Workspace) -> dict:
    observed = datetime.now(UTC).isoformat()
    report = {
        "schema_version": 1,
        "instance_id": None,
        "mode": workspace.mode,
        "data_environment": "live" if workspace.mode == "normal" else "isolated",
        "code_revision": None,
        "started_at": None,
        "observed_at": observed,
        "status": "ok",
        "components": {},
        "warnings": [],
        "errors": [],
    }
    components = report["components"]
    try:
        workspace.recheck()
    except FileNotFoundError:
        report["status"] = "unavailable"
        report["errors"].append({"code": "workspace_unavailable", "message": "Management data have not been verified."})
        return report
    except (OSError, IsolationError, ValueError):
        report["status"] = "target_mismatch"
        report["errors"].append({"code": "workspace_unconfirmed", "message": "The management boundary is unconfirmed."})
        return report

    try:
        runtime = fixture_status(workspace, settings=False)
        components["runtime"] = {
            "state": runtime["state"],
            "observed_at": observed,
            "current_port": runtime.get("port"),
            "configured_port": runtime.get("configured_port"),
            "restart_required": runtime.get("restart_required"),
        }
        for key in ("instance_id", "code_revision", "started_at"):
            report[key] = runtime.get(key)
    except (OSError, ValueError, sqlite3.Error):
        components["runtime"] = {"state": "unknown", "observed_at": observed, "reason": "runtime_unconfirmed"}

    try:
        workspace.require_media()
        components["recordings"] = {"state": "available", "observed_at": observed}
    except (OSError, ValueError) as error:
        components["recordings"] = {"state": "blocked", "observed_at": observed, "reason": str(error)}

    if workspace.restore_pending:
        components["sqlite"] = {"state": "blocked", "observed_at": observed, "reason": "restore_in_progress"}
    else:
        try:
            wal = workspace.database.with_name(workspace.database.name + "-wal")
            if wal.exists() and wal.stat().st_size:
                raise sqlite3.OperationalError("committed database may require WAL")
            uri = workspace.database.as_uri() + "?mode=ro&immutable=1"
            with closing(sqlite3.connect(uri, timeout=1, uri=True)) as db:
                db.execute("PRAGMA query_only=ON")
                db.execute("PRAGMA trusted_schema=OFF")
                version = db.execute("PRAGMA user_version").fetchone()[0]
                db.execute("SELECT id FROM stream_sessions LIMIT 1").fetchone()
                components["sqlite"] = {"state": "available", "observed_at": observed, "schema_version": version}
        except sqlite3.Error:
            components["sqlite"] = {"state": "unknown", "observed_at": observed, "reason": "database_unavailable"}
    if any(value["state"] in {"unknown", "blocked", "unconfirmed"} for value in components.values()):
        report["status"] = "partial"
    return report
