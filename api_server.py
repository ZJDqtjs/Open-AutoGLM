"""Unified API server for local execution mode.

This server is designed for cloud planning + local execution (Flutter + Shizuku):
- Backend receives screenshot/state from frontend
- Backend calls model to plan next action
- Backend returns standardized JSON command packet
- Backend never executes adb commands

Start server (example):
    uvicorn api_server:app --host 0.0.0.0 --port 8002

Endpoints:
    GET  /health
    POST /v1/local/next
    POST /v1/local/reset
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from threading import Lock
from typing import Any, Dict, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    BadRequestError,
    RateLimitError,
)
from pydantic import BaseModel, Field
from starlette.responses import JSONResponse

from phone_agent.agent import AgentConfig
from phone_agent.actions.handler import finish, parse_action
from phone_agent.local_command import build_local_command_packet
from phone_agent.model import ModelConfig
from phone_agent.model.client import MessageBuilder, ModelClient


def _get_env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _load_local_api_config() -> Dict[str, Any]:
    config_path = _get_env(
        "PHONE_AGENT_LOCAL_API_CONFIG",
        os.path.join(os.path.dirname(__file__), "local_api_config.json"),
    )
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError as e:
        raise RuntimeError(
            f"Local API config file not found: {config_path}. "
            "Create local_api_config.json with base_url/model/api_key/lang/max_steps."
        ) from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON in local API config file: {config_path}") from e

    required = ["base_url", "model", "api_key"]
    missing = [k for k in required if not str(data.get(k, "")).strip()]
    if missing:
        raise RuntimeError(
            f"Missing required fields in local API config: {', '.join(missing)}"
        )
    return data


LOCAL_API_CONFIG = _load_local_api_config()


DEFAULT_BASE_URL = str(LOCAL_API_CONFIG.get("base_url"))
DEFAULT_MODEL = str(LOCAL_API_CONFIG.get("model"))
DEFAULT_MODEL_API_KEY = str(LOCAL_API_CONFIG.get("api_key"))
DEFAULT_LANG = str(LOCAL_API_CONFIG.get("lang", "cn"))
DEFAULT_MAX_STEPS = int(LOCAL_API_CONFIG.get("max_steps", 100) or 100)
SERVER_TOKEN = _get_env("PHONE_AGENT_SERVER_TOKEN")
LOCAL_SESSION_TTL_SEC = int(
    _get_env("PHONE_AGENT_LOCAL_SESSION_TTL_SEC", "3600") or 3600
)


class LocalStepRequest(BaseModel):
    user_id: str = Field(..., min_length=1, description="Unique user identifier")
    task: Optional[str] = Field(None, description="Task, required when creating a new session")
    session_id: Optional[str] = Field(None, description="Planning session id")
    screenshot_base64: str = Field(..., description="Current device screenshot base64 (PNG/JPG)")
    current_app: str = Field("Unknown", description="Current app name from local client")
    screen_width: int = Field(..., ge=1, description="Screen width in pixels")
    screen_height: int = Field(..., ge=1, description="Screen height in pixels")
    extra_screen_info: Dict[str, Any] = Field(
        default_factory=dict, description="Optional extra UI/device state"
    )
    previous_step_result: Optional[Dict[str, Any]] = Field(
        None, description="Optional result feedback of previous local execution"
    )
    max_steps: int = Field(DEFAULT_MAX_STEPS, ge=1, le=500)
    lang: str = Field(DEFAULT_LANG, description="cn | en")


class LocalStepResponse(BaseModel):
    session_id: str
    step: int
    finished: bool
    message: str
    thinking: str
    action: Dict[str, Any]
    command_packet: Dict[str, Any]
    duration_ms: int


class LocalResetRequest(BaseModel):
    session_id: str


@dataclass
class LocalPlannerSession:
    session_id: str
    user_id: str
    task: str
    context: list[dict[str, Any]]
    step_count: int
    max_steps: int
    lang: str
    updated_at: float


LOCAL_SESSIONS: dict[str, LocalPlannerSession] = {}
LOCAL_SESSIONS_LOCK = Lock()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("api_server")


app = FastAPI(title="Open-AutoGLM Local API", version="1.0.0")

cors_origins = _get_env("PHONE_AGENT_SERVER_CORS", "*")
if cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[o.strip() for o in cors_origins.split(",") if o.strip()],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    request_id = str(uuid.uuid4())[:8]
    logger.info(
        "[%s] %s %s from %s",
        request_id,
        request.method,
        request.url.path,
        request.client.host if request.client else "unknown",
    )
    try:
        response = await call_next(request)
    except Exception:
        logger.exception("[%s] Request failed", request_id)
        raise

    duration_ms = int((time.time() - start) * 1000)
    logger.info(
        "[%s] -> %s (%dms)",
        request_id,
        response.status_code,
        duration_ms,
    )
    return response


def _require_token(x_server_token: Optional[str]) -> None:
    if SERVER_TOKEN and x_server_token != SERVER_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _cleanup_local_sessions() -> None:
    now = time.time()
    expired_ids = []

    for sid, session in LOCAL_SESSIONS.items():
        if now - session.updated_at > LOCAL_SESSION_TTL_SEC:
            expired_ids.append(sid)

    for sid in expired_ids:
        LOCAL_SESSIONS.pop(sid, None)

    if expired_ids:
        logger.info("Cleaned %d expired local sessions", len(expired_ids))


def _get_or_create_local_session(req: LocalStepRequest) -> LocalPlannerSession:
    with LOCAL_SESSIONS_LOCK:
        _cleanup_local_sessions()

        if req.session_id:
            session = LOCAL_SESSIONS.get(req.session_id)
            if not session:
                raise HTTPException(status_code=404, detail="session_id not found")

            if req.user_id != session.user_id:
                raise HTTPException(
                    status_code=403,
                    detail="user_id does not match session owner",
                )

            if req.task and req.task != session.task:
                raise HTTPException(
                    status_code=400,
                    detail="task cannot be changed for an existing session",
                )

            session.max_steps = req.max_steps
            session.lang = req.lang or session.lang
            session.updated_at = time.time()
            return session

        if not req.task:
            raise HTTPException(
                status_code=400,
                detail="task is required when creating a new local session",
            )

        sid = str(uuid.uuid4())
        system_prompt = AgentConfig(lang=req.lang or DEFAULT_LANG).system_prompt
        context = [MessageBuilder.create_system_message(system_prompt)]
        session = LocalPlannerSession(
            session_id=sid,
            user_id=req.user_id,
            task=req.task,
            context=context,
            step_count=0,
            max_steps=req.max_steps,
            lang=req.lang or DEFAULT_LANG,
            updated_at=time.time(),
        )
        LOCAL_SESSIONS[sid] = session
        logger.info("Created local session: %s", sid)
        return session


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "api_server",
        "sessions": len(LOCAL_SESSIONS),
    }


@app.post("/v1/local/next")
def local_next_step(
    req: LocalStepRequest, x_server_token: Optional[str] = Header(default=None)
):
    """Generate next action and return local executable command packet.

    The backend only plans action JSON and never executes adb commands.
    """
    _require_token(x_server_token)

    start = time.time()
    session = _get_or_create_local_session(req)
    logger.info(
        "Planning step: user_id=%s session=%s step=%d current_app=%s",
        req.user_id,
        session.session_id,
        session.step_count + 1,
        req.current_app,
    )

    resolved_base_url = DEFAULT_BASE_URL
    resolved_model = DEFAULT_MODEL
    resolved_api_key = DEFAULT_MODEL_API_KEY
    resolved_lang = session.lang or DEFAULT_LANG

    logger.info(
        "Model target: session=%s base_url=%s model=%s api_key_present=%s",
        session.session_id,
        resolved_base_url,
        resolved_model,
        bool(resolved_api_key),
    )

    if not resolved_api_key:
        raise HTTPException(
            status_code=500,
            detail=(
                "Backend model api_key is missing. "
                "Please configure local_api_config.json."
            ),
        )

    model_client = ModelClient(
        ModelConfig(
            base_url=resolved_base_url,
            model_name=resolved_model,
            api_key=resolved_api_key,
            lang=resolved_lang,
        )
    )

    is_first = session.step_count == 0
    screen_info = {
        "current_app": req.current_app,
        "screen_width": req.screen_width,
        "screen_height": req.screen_height,
        **(req.extra_screen_info or {}),
    }
    if req.previous_step_result is not None:
        screen_info["previous_step_result"] = req.previous_step_result

    text_prefix = req.task if is_first else "** Screen Info **"
    text_content = f"{text_prefix}\n\n{json.dumps(screen_info, ensure_ascii=False)}"

    session.context.append(
        MessageBuilder.create_user_message(
            text=text_content,
            image_base64=req.screenshot_base64,
        )
    )

    try:
        response = model_client.request(session.context)
    except AuthenticationError as e:
        logger.exception(
            "Model auth failed: session=%s base_url=%s model=%s",
            session.session_id,
            resolved_base_url,
            resolved_model,
        )
        raise HTTPException(
            status_code=401,
            detail=(
                "Model authentication failed. Check local_api_config.json api_key "
                "and endpoint permissions."
            ),
        ) from e
    except BadRequestError as e:
        logger.exception(
            "Model bad request: session=%s base_url=%s model=%s",
            session.session_id,
            resolved_base_url,
            resolved_model,
        )
        raise HTTPException(
            status_code=400,
            detail=(
                "Model request rejected (400). Check model name, request format, "
                "and image payload size."
            ),
        ) from e
    except RateLimitError as e:
        logger.exception(
            "Model rate limited: session=%s base_url=%s model=%s",
            session.session_id,
            resolved_base_url,
            resolved_model,
        )
        raise HTTPException(
            status_code=429,
            detail="Model provider rate limited the request. Retry later.",
        ) from e
    except APITimeoutError as e:
        logger.exception(
            "Model timeout: session=%s base_url=%s model=%s",
            session.session_id,
            resolved_base_url,
            resolved_model,
        )
        raise HTTPException(
            status_code=504,
            detail="Model upstream timeout. Retry later or increase upstream timeout.",
        ) from e
    except APIConnectionError as e:
        logger.exception(
            "Model connection failed: session=%s base_url=%s model=%s",
            session.session_id,
            resolved_base_url,
            resolved_model,
        )
        raise HTTPException(
            status_code=502,
            detail=(
                "Model upstream connection failed (server disconnected or network/proxy "
                "interrupted). Verify base_url connectivity and retry."
            ),
        ) from e
    except APIStatusError as e:
        logger.exception(
            "Model API status error: session=%s base_url=%s model=%s status=%s",
            session.session_id,
            resolved_base_url,
            resolved_model,
            getattr(e, "status_code", None),
        )
        status_code = getattr(e, "status_code", 502) or 502
        raise HTTPException(
            status_code=status_code,
            detail=f"Model upstream returned HTTP {status_code}.",
        ) from e
    except Exception as e:
        logger.exception(
            "Model request failed: session=%s base_url=%s model=%s",
            session.session_id,
            resolved_base_url,
            resolved_model,
        )
        raise HTTPException(status_code=500, detail=f"Model error: {e}") from e

    try:
        action = parse_action(response.action)
    except ValueError:
        action = finish(message=response.action)

    session.context[-1] = MessageBuilder.remove_images_from_message(session.context[-1])
    session.context.append(
        MessageBuilder.create_assistant_message(
            f"<think>{response.thinking}</think><answer>{response.action}</answer>"
        )
    )

    session.step_count += 1
    reached_max_steps = session.step_count >= session.max_steps
    finished_flag = action.get("_metadata") == "finish" or reached_max_steps
    if action.get("message"):
        message = action["message"]
    elif action.get("_metadata") == "finish":
        message = "Task completed"
    elif reached_max_steps:
        message = "Max steps reached"
    else:
        message = "Continue"

    packet = build_local_command_packet(
        action=action,
        thinking=response.thinking,
        message=message,
        finished=finished_flag,
        step=session.step_count,
        session_id=session.session_id,
        screen_width=req.screen_width,
        screen_height=req.screen_height,
    )

    duration_ms = int((time.time() - start) * 1000)

    with LOCAL_SESSIONS_LOCK:
        if finished_flag:
            LOCAL_SESSIONS.pop(session.session_id, None)
            logger.info("Finished session: %s", session.session_id)
        else:
            session.updated_at = time.time()

    logger.info(
        "Step planned: session=%s step=%d finished=%s duration=%dms",
        session.session_id,
        session.step_count,
        finished_flag,
        duration_ms,
    )

    return JSONResponse(
        LocalStepResponse(
            session_id=session.session_id,
            step=session.step_count,
            finished=finished_flag,
            message=message,
            thinking=response.thinking,
            action=action,
            command_packet=packet,
            duration_ms=duration_ms,
        ).model_dump()
    )


@app.post("/v1/local/reset")
def local_reset_session(
    req: LocalResetRequest, x_server_token: Optional[str] = Header(default=None)
):
    _require_token(x_server_token)
    with LOCAL_SESSIONS_LOCK:
        existed = LOCAL_SESSIONS.pop(req.session_id, None) is not None
    logger.info("Reset session: %s removed=%s", req.session_id, existed)
    return {"session_id": req.session_id, "removed": existed}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "api_server:app",
        host="0.0.0.0",
        port=int(_get_env("PHONE_AGENT_API_PORT", "8002") or 8002),
        reload=False,
        log_level="info",
    )
