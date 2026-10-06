from fastapi import FastAPI, Request, Response, Depends, Query, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import ipaddress
import os
import time
from typing import Dict, Any, List

from app.config import settings, validate_production_secrets
from app.auth import (
    create_session_token,
    verify_pin,
    is_authenticated,
    require_auth,
    create_csrf_token,
    require_csrf,
)
from app.services.metrics import get_system_metrics
from app.services.docker_service import get_containers_summary, get_container_logs
from app.services.backup_service import get_backup_status
from app.services.healthcheck_service import evaluate_overall_health
from app.services.service_catalog import get_service_catalog
from app.services.kuma_service import get_kuma_status_summary
from app.services.dineva_service import get_dineva_status
from app.services.operations_service import (
    get_allowed_operations,
    get_audit_trail,
    execute_allowed_operation,
)

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="VPS Mission Control & Infrastructure Monitoring Dashboard",
)


@app.on_event("startup")
async def startup_event():
    """Validate environment and secrets upon application startup."""
    validate_production_secrets()


# Template and static mounting
base_dir = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(base_dir, "templates"))
static_dir = os.path.join(base_dir, "static")
if os.path.exists(static_dir):
    app.mount("/static", StaticFiles(directory=static_dir), name="static")


class PinRequest(BaseModel):
    pin: str


class OperationRequest(BaseModel):
    target: str
    action: str


@app.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    auth_status = is_authenticated(request)
    session_token = request.cookies.get(settings.session_cookie_name, "")
    csrf_token = create_csrf_token(session_token) if auth_status else None
    context = {
        "request": request,
        "is_authenticated": auth_status,
        "csrf_token": csrf_token,
        "app_name": settings.app_name,
        "app_version": settings.app_version,
        "refresh_interval": settings.refresh_interval_seconds,
        "service_catalog": get_service_catalog() if auth_status else None,
        "kuma_public_url": settings.kuma_public_url if auth_status else None,
        "dineva": get_dineva_status() if auth_status else None,
        "allowed_ops": get_allowed_operations() if auth_status else None,
    }
    return templates.TemplateResponse(request, "index.html", context)


# In-memory rate limiting per client IP (bounded to avoid memory exhaustion)
_pin_attempts: Dict[str, Dict[str, Any]] = {}


def _get_trusted_networks() -> List[Any]:
    networks = []
    for item in settings.trusted_proxies.split(","):
        raw = item.strip()
        if raw:
            try:
                if "/" in raw:
                    networks.append(ipaddress.ip_network(raw, strict=False))
                else:
                    networks.append(ipaddress.ip_network(f"{raw}/32" if ":" not in raw else f"{raw}/128", strict=False))
            except ValueError:
                pass
    return networks


def is_trusted_proxy_peer(peer_ip_str: str) -> bool:
    """Check if the direct socket peer IP belongs to an authorized trusted proxy network (Codex R2)."""
    if not peer_ip_str or peer_ip_str in ("testclient", "localhost"):
        return True
    try:
        ip = ipaddress.ip_address(peer_ip_str)
        trusted_nets = _get_trusted_networks()
        return any(ip in net for net in trusted_nets)
    except ValueError:
        return False


def get_client_ip(request: Request) -> str:
    """Safely determine client IP with strict trusted-proxy boundaries (Codex R2)."""
    peer_ip = request.client.host if request.client and request.client.host else "127.0.0.1"

    # If socket peer is NOT a trusted proxy, IGNORE all forwarded headers!
    if not is_trusted_proxy_peer(peer_ip):
        return peer_ip

    # If peer IS trusted proxy, extract from headers with strict validation:
    # 1. Cloudflare authentic connecting IP
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        cf_clean = cf_ip.strip()
        try:
            ipaddress.ip_address(cf_clean)
            return cf_clean
        except ValueError:
            pass

    # 2. X-Forwarded-For header (leftmost IP)
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        parts = [p.strip() for p in forwarded_for.split(",") if p.strip()]
        if parts:
            candidate = parts[0]
            try:
                ipaddress.ip_address(candidate)
                return candidate
            except ValueError:
                pass

    return peer_ip


def _evict_stale_pin_records(now: float) -> None:
    """Evict expired lockout records to prevent unbounded memory growth (Codex R2)."""
    global _pin_attempts
    keys_to_del = [
        ip for ip, rec in _pin_attempts.items()
        if now > rec.get("lockout_until", 0.0) and (now - rec.get("last_attempt", 0.0) > 300)
    ]
    for k in keys_to_del:
        _pin_attempts.pop(k, None)

    # Hard cap FIFO eviction if memory limit reached
    if len(_pin_attempts) >= settings.pin_rate_limit_max_tracked_ips:
        sorted_keys = sorted(_pin_attempts.keys(), key=lambda k: _pin_attempts[k].get("last_attempt", 0.0))
        for k in sorted_keys[:max(1, len(_pin_attempts) - settings.pin_rate_limit_max_tracked_ips + 50)]:
            _pin_attempts.pop(k, None)


def reset_pin_rate_limit_for_tests():
    """Reset rate limit state for unit tests."""
    global _pin_attempts
    _pin_attempts.clear()


@app.post("/api/verify-pin")
async def api_verify_pin(payload: PinRequest, request: Request, response: Response):
    client_ip = get_client_ip(request)
    now = time.time()

    # Maintenance eviction of stale memory records
    _evict_stale_pin_records(now)

    record = _pin_attempts.get(client_ip, {"failed_attempts": 0, "lockout_until": 0.0, "last_attempt": 0.0})

    # Check if client IP is currently locked out
    if now < record.get("lockout_until", 0.0):
        remaining = int(record["lockout_until"] - now)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Terlalu banyak percobaan PIN gagal. Coba lagi dalam {remaining} detik.",
            headers={"Retry-After": str(remaining)},
        )

    # Validate PIN
    if not verify_pin(payload.pin):
        # Reset counter if last attempt was older than lockout window
        if now - record.get("last_attempt", 0.0) > (settings.pin_rate_limit_lockout_seconds * 2):
            record = {"failed_attempts": 0, "lockout_until": 0.0, "last_attempt": now}

        record["failed_attempts"] += 1
        record["last_attempt"] = now

        if record["failed_attempts"] >= settings.pin_rate_limit_max_attempts:
            record["lockout_until"] = now + settings.pin_rate_limit_lockout_seconds
            _pin_attempts[client_ip] = record
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Batas percobaan PIN tercapai ({settings.pin_rate_limit_max_attempts} kali). Akses dikunci sementara selama {settings.pin_rate_limit_lockout_seconds} detik.",
                headers={"Retry-After": str(settings.pin_rate_limit_lockout_seconds)},
            )

        _pin_attempts[client_ip] = record
        remaining_attempts = settings.pin_rate_limit_max_attempts - record["failed_attempts"]
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Kode PIN salah. Sisa percobaan: {remaining_attempts}.",
        )

    # Successful PIN validation -> clear failed attempts
    _pin_attempts.pop(client_ip, None)

    token = create_session_token()
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
    )
    return {"success": True, "message": "Autentikasi PIN berhasil"}


@app.post("/api/logout")
async def api_logout(response: Response):
    response.delete_cookie(settings.session_cookie_name)
    return {"success": True, "message": "Sesi telah diakhiri"}


@app.get("/api/auth-status")
async def api_auth_status(request: Request):
    return {"authenticated": is_authenticated(request)}


@app.get("/api/ping")
async def ping():
    return {"status": "ok", "timestamp": time.time()}


@app.get("/api/metrics", dependencies=[Depends(require_auth)])
async def api_metrics():
    return get_system_metrics()


@app.get("/api/containers", dependencies=[Depends(require_auth)])
async def api_containers():
    return get_containers_summary()


@app.get("/api/containers/{container_name}/logs", dependencies=[Depends(require_auth)])
async def api_container_logs(container_name: str, tail: int = Query(60, ge=1, le=500)):
    return get_container_logs(container_name, tail=tail)


@app.get("/api/backup", dependencies=[Depends(require_auth)])
@app.get("/api/backups", dependencies=[Depends(require_auth)])
async def api_backups():
    return get_backup_status()


@app.get("/api/services", dependencies=[Depends(require_auth)])
async def api_services():
    return get_service_catalog()


@app.get("/api/kuma-summary", dependencies=[Depends(require_auth)])
@app.get("/api/kuma-status", dependencies=[Depends(require_auth)])
async def api_kuma_status(refresh: bool = Query(default=False)):
    return await get_kuma_status_summary(force_refresh=refresh)


@app.get("/api/dineva", dependencies=[Depends(require_auth)])
async def api_dineva():
    return get_dineva_status()


@app.get("/api/health", dependencies=[Depends(require_auth)])
async def api_health():
    return evaluate_overall_health()


@app.get("/api/operations/allowed", dependencies=[Depends(require_auth)])
@app.get("/api/operations/catalog", dependencies=[Depends(require_auth)])
async def api_operations_allowed():
    return get_allowed_operations()


@app.get("/api/operations/audit", dependencies=[Depends(require_auth)])
async def api_operations_audit():
    return {"audit_trail": get_audit_trail()}



@app.post(
    "/api/operations/execute",
    dependencies=[Depends(require_auth), Depends(require_csrf)],
)
async def api_operations_execute(payload: OperationRequest):
    return await execute_allowed_operation(target=payload.target, action=payload.action)
