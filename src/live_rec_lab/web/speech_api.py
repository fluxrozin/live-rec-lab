"""Local settings and enqueue-only operations for the isolated speech worker."""

from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import FastAPI, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from starlette.exceptions import HTTPException

from ..isolation import FixtureWorkspace
from ..speech_jobs import (
    SpeechConfiguration,
    SpeechSettingsConflict,
    enqueue_speech,
    public_attempt,
    read_speech_configuration,
    retry_failed_speech,
    save_speech_configuration,
    speech_attempts,
)
from .queries import read_database


class SpeechSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool
    model: Literal["large-v3", "large-v3-turbo"]
    language: Annotated[str, Field(pattern=r"^[a-z]{2,3}$")]
    device: Literal["cuda", "cpu"]
    revision: Annotated[int, Field(strict=True, ge=0, le=2**63 - 2)]


class RetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirmed: StrictBool


def mount_speech_api(app: FastAPI, workspace: FixtureWorkspace) -> None:
    @app.get("/api/v1/settings/speech")
    def speech_settings():
        with read_database(workspace) as database:
            return asdict(read_speech_configuration(database))

    @app.put("/api/v1/settings/speech")
    def update_speech_settings(request: Request, settings: SpeechSettingsRequest):
        try:
            return asdict(save_speech_configuration(workspace, SpeechConfiguration(**settings.model_dump())))
        except SpeechSettingsConflict:
            return JSONResponse(
                {
                    "error": {
                        "code": "speech_settings_conflict",
                        "message": "別の画面で音声字幕の設定が更新されました。",
                        "correlation_id": request.state.correlation_id,
                    },
                },
                status_code=409,
            )
        except ValueError as error:
            raise HTTPException(422) from error

    @app.get("/api/v1/speech")
    def list_speech_queue():
        return {"items": [public_attempt(attempt) for attempt in speech_attempts(workspace)]}

    @app.post("/api/v1/segments/{segment_id}/speech", status_code=202)
    def submit_speech(request: Request, segment_id: Annotated[int, Path(ge=1, le=2**63 - 1)]):
        try:
            return public_attempt(enqueue_speech(workspace, segment_id))
        except ValueError:
            return JSONResponse(
                {
                    "error": {
                        "code": "speech_segment_unavailable",
                        "message": "対象は処理可能な確定録画として確認できません。",
                        "correlation_id": request.state.correlation_id,
                    },
                },
                status_code=409,
            )

    @app.post("/api/v1/speech/retry-failed", status_code=202)
    def retry_failed(retry: RetryRequest):
        if retry.confirmed is not True:
            raise HTTPException(422)
        accepted, skipped = retry_failed_speech(workspace)
        return {"items": [public_attempt(attempt) for attempt in accepted], "skipped_segment_ids": skipped}
