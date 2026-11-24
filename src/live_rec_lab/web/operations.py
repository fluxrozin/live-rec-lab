"""Validated operations settings and managed-media requests."""

from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import FastAPI, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from starlette.exceptions import HTTPException

from ..diagnosis import diagnose_application
from ..isolation import Workspace
from ..retention import MediaDeletionConflict, delete_session_media, media_deletion_status
from ..settings import OperationsConflict, OperationsSettings, read_operations_settings, save_operations_settings
from ..storage_audit import cancel_storage_audit, enqueue_storage_audit, get_storage_audit
from .queries import read_database


class OperationsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    tiktok_per_minute: Annotated[float, Field(strict=True, gt=0, le=10)]
    twitcasting_per_minute: Annotated[float, Field(strict=True, gt=0, le=60)]
    recording_root: Annotated[str | None, Field(max_length=4096)] = None
    comment_retention_days: Annotated[int, Field(strict=True, ge=0, le=36500)]
    video_retention_days: Annotated[int, Field(strict=True, ge=0, le=36500)]
    minimum_free_gib: Annotated[float, Field(strict=True, ge=0, le=1_000_000)]
    maximum_recording_gib: Annotated[float, Field(strict=True, ge=0, le=1_000_000)]
    auto_delete_enabled: StrictBool
    comments_globally_enabled: StrictBool
    confirmed: StrictBool = False
    revision: Annotated[int, Field(strict=True, ge=0, lt=2**53 - 1)]


class DeletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["files", "files_and_history"]
    confirmed: StrictBool


SessionId = Annotated[int, Path(ge=1, le=2**63 - 1)]


def _conflict(request: Request, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message, "correlation_id": request.state.correlation_id}},
        status_code=409,
    )


def mount_operations(app: FastAPI, workspace: Workspace) -> None:
    @app.get("/api/v1/system/storage-audits")
    def latest_audit():
        return get_storage_audit(workspace)

    @app.get("/api/v1/system/storage-audits/{audit_id}")
    def storage_audit(audit_id: SessionId):
        try:
            return get_storage_audit(workspace, audit_id)
        except LookupError as error:
            raise HTTPException(404) from error

    @app.post("/api/v1/system/storage-audits", status_code=202)
    def start_audit():
        return enqueue_storage_audit(workspace)

    @app.post("/api/v1/system/storage-audits/{audit_id}/cancel")
    def cancel_audit(audit_id: SessionId):
        try:
            return cancel_storage_audit(workspace, audit_id)
        except LookupError as error:
            raise HTTPException(404) from error

    @app.get("/api/v1/settings/operations")
    def operations_settings():
        return asdict(read_operations_settings(workspace))

    @app.put("/api/v1/settings/operations")
    def update_operations(request: Request, update: OperationsUpdate):
        try:
            settings = OperationsSettings(**update.model_dump(exclude={"confirmed"}))
            return asdict(save_operations_settings(workspace, settings, confirmed=update.confirmed))
        except OperationsConflict:
            return _conflict(
                request, "operations_conflict", "設定の版・保存先の移行条件・削除範囲の確認を見直してください。"
            )
        except ValueError as error:
            raise HTTPException(422) from error

    @app.get("/api/v1/sessions/{session_id}/media-deletion")
    def get_deletion(session_id: SessionId):
        with read_database(workspace) as database:
            return {"operation": media_deletion_status(database, session_id)}

    @app.post("/api/v1/sessions/{session_id}/media-deletion")
    def delete_media(request: Request, session_id: SessionId, deletion: DeletionRequest):
        if not deletion.confirmed:
            raise HTTPException(422)
        try:
            return delete_session_media(workspace, session_id, deletion.mode)
        except MediaDeletionConflict:
            return _conflict(
                request, "media_deletion_conflict", "配信終了・処理の完了・媒体の管理状態を確認してください。"
            )

    if workspace.mode == "developer":

        @app.get("/api/v1/developer/diagnose")
        def development_diagnosis():
            return diagnose_application(workspace)
