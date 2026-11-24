"""LiveRecorder command line."""

import argparse
import json
import signal
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

from .collection import FIXTURE_STREAMS, run_fixture
from .endpoint import fixture_status, stop_fixture
from .isolation import FixtureWorkspace
from .maintenance import expire_fixture_comments, inspect_fixture_restore
from .postprocess import postprocess_fixture
from .restoration import finish_fixture_restore, prepare_fixture_restore
from .runtime import backup_fixture, diagnose_fixture, serve_fixture
from .speech_jobs import enqueue_speech, process_speech, public_attempt, speech_attempts
from .web_port import read_web_port, save_web_port


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="live-rec-lab")
    commands = parser.add_subparsers(dest="command", required=True)
    fixture = commands.add_parser("fixture", help="run a synthetic recording in a private /tmp workspace")
    fixture_actions = fixture.add_subparsers(dest="action", required=True)
    fixture_actions.add_parser("init").add_argument("root", type=Path)
    run = fixture_actions.add_parser("run")
    run.add_argument("root", type=Path)
    run.add_argument("--service", choices=FIXTURE_STREAMS, default="tiktok")
    postprocess = fixture_actions.add_parser("postprocess")
    postprocess.add_argument("root", type=Path)
    postprocess.add_argument("--service", choices=FIXTURE_STREAMS, default="tiktok")
    expiry = fixture_actions.add_parser("expire-comments")
    expiry.add_argument("root", type=Path)
    expiry.add_argument("--service", choices=FIXTURE_STREAMS, default="tiktok")
    diagnosis = fixture_actions.add_parser("diagnose")
    diagnosis.add_argument("root", type=Path)
    diagnosis.add_argument("--json", action="store_true", help="emit the JSON diagnosis (the default)")
    fixture_actions.add_parser("backup").add_argument("root", type=Path)
    restore_check = fixture_actions.add_parser("restore-check", help="inspect a managed backup without restoring it")
    restore_check.add_argument("root", type=Path)
    restore_check.add_argument("snapshot")
    restore_prepare = fixture_actions.add_parser("restore-prepare", help="check a backup and fence the stopped fixture")
    restore_prepare.add_argument("root", type=Path)
    restore_prepare.add_argument("snapshot")
    fixture_actions.add_parser("restore-finish", help="verify a manually replaced fixture database").add_argument(
        "root", type=Path
    )
    server = fixture_actions.add_parser("serve")
    server.add_argument("root", type=Path)
    server.add_argument("--port", type=int, help="override the saved port for this start")
    for operation in ("status", "url", "stop"):
        fixture_actions.add_parser(operation).add_argument("root", type=Path)
    config = fixture_actions.add_parser("config")
    config.add_argument("root", type=Path)
    config_actions = config.add_subparsers(dest="config_action", required=True)
    config_actions.add_parser("get").add_argument("key", choices=["web_port"])
    config_set = config_actions.add_parser("set")
    config_set.add_argument("key", choices=["web_port"])
    config_set.add_argument("value", type=int)
    speech_queue = fixture_actions.add_parser("speech-queue", help="queue a verified recorded segment")
    speech_queue.add_argument("root", type=Path)
    speech_queue.add_argument("segment_id", type=int)
    speech_work = fixture_actions.add_parser("speech-work", help="process one offline speech attempt")
    speech_work.add_argument("root", type=Path)
    speech_work.add_argument("--automatic", action="store_true", help="also queue eligible recordings")
    fixture_actions.add_parser("speech-status").add_argument("root", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action == "init":
            workspace = FixtureWorkspace.initialize(args.root)
            result = {"mode": "fixture", "root": str(workspace.root)}
        else:
            workspace = FixtureWorkspace.open(args.root)
            match args.action:
                case "run":
                    result = run_fixture(workspace, args.service)
                case "postprocess":
                    result = postprocess_fixture(workspace, args.service)
                case "expire-comments":
                    result = expire_fixture_comments(workspace, args.service)
                case "diagnose":
                    result = diagnose_fixture(workspace)
                case "backup":
                    result = {"mode": "fixture", "snapshot": str(backup_fixture(workspace))}
                case "restore-check":
                    result = inspect_fixture_restore(workspace, args.snapshot)
                case "restore-prepare":
                    result = prepare_fixture_restore(workspace, args.snapshot)
                case "restore-finish":
                    result = finish_fixture_restore(workspace)
                case "serve":
                    serve_fixture(workspace, args.port)
                    return 0
                case "status":
                    result = fixture_status(workspace)
                case "url":
                    status = fixture_status(workspace)
                    if status["state"] != "running":
                        raise ValueError("fixture server URL is unavailable")
                    print(status["url"])
                    return 0
                case "stop":
                    result = stop_fixture(workspace)
                case "config":
                    if args.config_action == "get":
                        result = asdict(read_web_port(workspace))
                    else:
                        with workspace.exclusive(worker="server"):
                            result = asdict(save_web_port(workspace, args.value))
                case "speech-queue":
                    result = public_attempt(enqueue_speech(workspace, args.segment_id))
                case "speech-work":
                    completed = _speech_work(workspace, args.automatic)
                    result = public_attempt(completed) if completed else {"state": "idle"}
                case "speech-status":
                    result = {"items": [public_attempt(attempt) for attempt in speech_attempts(workspace)]}
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, sqlite3.Error) as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    if args.action in {"restore-prepare", "restore-finish"} and result["state"] == "blocked":
        return 1
    return 0


def _speech_work(workspace: FixtureWorkspace, automatic: bool):
    def interrupt(_signal, _frame):
        raise KeyboardInterrupt

    watched = (signal.SIGTERM, signal.SIGHUP)
    original = {event: signal.signal(event, interrupt) for event in watched}
    try:
        return process_speech(workspace, automatic=automatic)
    finally:
        for event, handler in original.items():
            signal.signal(event, handler)


if __name__ == "__main__":
    raise SystemExit(main())
