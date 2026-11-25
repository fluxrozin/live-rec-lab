"""diagnose --json（読み取り専用。DB作成・移行・外部通信を行わない）。"""

from __future__ import annotations

import json
import shutil
import sys

from live_rec_lab import server
from live_rec_lab.config import Environment
from live_rec_lab.db import session as dbs
from live_rec_lab.media_tools import resolve
from live_rec_lab.runtime import read_endpoint, utcnow

SCHEMA_VERSION = 1


def collect(env: Environment) -> dict:
    now = utcnow()
    warnings: list[dict] = []
    errors: list[dict] = []
    comps: dict[str, dict] = {}
    ep = read_endpoint(env)
    comps["instance"] = {"observed_at": now, "status": "running" if ep else "stopped", "endpoint": ep}
    rev = dbs.current_revision(env.db_path)
    db_state = "missing" if not env.db_path.exists() else ("ok" if rev == dbs.head_revision() else "unknown_revision")
    comps["database"] = {"observed_at": now, "status": db_state, "revision": rev, "path_mode": "private"}
    if db_state == "unknown_revision":
        warnings.append({"code": "DB_REVISION", "message": "DBの版が現行と異なります"})
    tools = resolve(env.flags.get("LIVE_REC_LAB_FFMPEG_BIN"))
    comps["media_tools"] = {"observed_at": now, "status": "ok" if tools.ok else "degraded", "ffmpeg": tools.ffmpeg,
                            "ffprobe": tools.ffprobe, "ffmpeg_version": tools.ffmpeg_version,
                            "ffprobe_version": tools.ffprobe_version, "decoders": tools.decoders,
                            "source": tools.source}
    if not tools.ok:
        errors.append({"code": "MEDIA_TOOLS", "message": tools.error or "解決不能"})
    root = env.recording_root
    storage = {"observed_at": now, "root": str(root), "exists": root.is_dir()}
    if root.is_dir():
        u = shutil.disk_usage(root)
        storage.update(free_bytes=u.free, total_bytes=u.total)
    storage["status"] = "ok" if root.is_dir() else "unavailable"
    comps["storage"] = storage
    comps["external"] = {"observed_at": now, "status": "denied_by_default",
                         "twitcasting_credentials": "set" if "TWITCASTING_CLIENT_ID" in env.secrets else "unset",
                         "sign_api_key": "set" if "SIGN_API_KEY" in env.secrets else "unset",
                         "hf_token": "set" if "HF_TOKEN" in env.secrets else "unset"}
    status = "ok" if not errors and db_state in ("ok", "missing") else "partial"
    return {
        "schema_version": SCHEMA_VERSION,
        "instance_id": ep["instance_id"] if ep else None,
        "mode": env.mode,
        "data_environment": env.data_environment,
        "code_revision": server.code_revision(),
        "started_at": ep["started_at"] if ep else None,
        "observed_at": now,
        "status": status,
        "components": comps,
        "warnings": warnings,
        "errors": errors,
    }


def run_diagnose(env: Environment, *, as_json: bool) -> int:
    data = collect(env)
    if as_json:
        sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
    else:
        print(f"status={data['status']} mode={data['mode']}")
    return {"ok": 0, "partial": 1, "unavailable": 2}.get(data["status"], 1)
