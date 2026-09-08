from fastapi import FastAPI, Request, Response, Depends, Form, Query, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import os

from app.config import settings
from app.auth import (
    create_session_token,
    verify_pin,
    is_authenticated,
    require_auth,
)
from app.services.metrics import get_system_metrics
from app.services.docker_service import get_containers_summary, get_container_logs
from app.services.backup_service import get_backup_status
from app.services.healthcheck_service import evaluate_overall_health

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="VPS Mission Control & Infrastructure Monitoring Dashboard",
)

# Template and static mounting
base_dir = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(base_dir, "templates"))
static_dir = os.path.join(base_dir, "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


class PinRequest(BaseModel):
    pin: str


@app.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    auth_status = is_authenticated(request)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "is_authenticated": auth_status,
            "app_name": settings.app_name,
            "app_version": settings.app_version,
            "refresh_interval": settings.refresh_interval_seconds,
        },
    )


@app.post("/api/verify-pin")
async def api_verify_pin(payload: PinRequest, response: Response):
    if not verify_pin(payload.pin):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Kode PIN salah. Silakan coba lagi.",
        )

    token = create_session_token()
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=False,  # Caddy handles SSL termination at Cloudflare edge
    )
    return {"success": True, "message": "Autentikasi PIN berhasil"}


@app.post("/api/logout")
async def api_logout(response: Response):
    response.delete_cookie(key=settings.session_cookie_name)
    return {"success": True, "message": "Berhasil logout"}


@app.get("/api/auth-status")
async def api_auth_status(request: Request):
    return {"authenticated": is_authenticated(request)}


@app.get("/api/metrics", dependencies=[Depends(require_auth)])
async def api_metrics():
    return get_system_metrics()


@app.get("/api/containers", dependencies=[Depends(require_auth)])
async def api_containers():
    return get_containers_summary()


@app.get("/api/containers/{container_name}/logs", dependencies=[Depends(require_auth)])
async def api_container_logs(container_name: str, tail: int = Query(default=60, ge=10, le=200)):
    return get_container_logs(container_name, tail=tail)


@app.get("/api/backup", dependencies=[Depends(require_auth)])
async def api_backup():
    return get_backup_status()


@app.get("/api/health", dependencies=[Depends(require_auth)])
async def api_health():
    return evaluate_overall_health()


@app.get("/api/ping")
async def api_ping():
    return {"status": "ok", "app": settings.app_name, "version": settings.app_version}
