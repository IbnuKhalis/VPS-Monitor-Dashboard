import asyncio
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Any, List
import httpx
from fastapi import HTTPException, status

from app.config import settings
from app.services.docker_service import get_docker_client

import json
import os

logger = logging.getLogger("operations_service")

# Strict Allowlist: Only approved containers and actions
ALLOWED_OPERATIONS: Dict[str, List[str]] = {
    "cv-builder": ["restart"],
}

OPERATION_METADATA: Dict[str, Dict[str, Any]] = {
    "cv-builder": {
        "id": "cv-builder",
        "name": "CV Builder (ATS)",
        "allowed_actions": ["restart"],
        "impact": "Kontainer cv-builder akan dimuat ulang. Permintaan HTTP aktif ke cv.digitalneeds.my.id akan mengalami jeda sesaat (1-3 detik) hingga proses kembali ke status HEALTHY. Sesi penyuntingan CV di browser pengguna tidak hilang karena state tersimpan di localStorage.",
        "service_url": "https://cv.digitalneeds.my.id",
        "port": 8080,
    }
}

# Concurrency Mutex and Rate Limiting
_ops_lock = asyncio.Lock()
_last_operation_timestamp: float = 0.0
MAX_AUDIT_ENTRIES = 50


def _sanitize_audit_error(error_msg: str) -> str:
    """Remove any potential tokens or secrets from audit error messages."""
    if not error_msg:
        return ""
    cleaned = str(error_msg)
    for secret in [settings.ops_executor_token, settings.secret_key, settings.dashboard_pin]:
        if secret and len(secret) > 3:
            cleaned = cleaned.replace(secret, "[REDACTED]")
    return cleaned[:300]


def _load_audit_trail_from_disk() -> List[Dict[str, Any]]:
    """Load persistent audit entries from disk."""
    audit_file = settings.ops_audit_file
    if os.path.exists(audit_file):
        try:
            with open(audit_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data[:MAX_AUDIT_ENTRIES]
        except Exception as e:
            logger.warning(f"Failed to read audit trail file {audit_file}: {e}")
    return []


def _save_audit_trail_to_disk() -> None:
    """Write in-memory audit trail to disk atomically."""
    audit_file = settings.ops_audit_file
    try:
        os.makedirs(os.path.dirname(os.path.abspath(audit_file)), exist_ok=True)
        tmp_file = f"{audit_file}.tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(_audit_trail[:MAX_AUDIT_ENTRIES], f, indent=2)
        os.replace(tmp_file, audit_file)
    except Exception as e:
        logger.warning(f"Failed to write audit trail to {audit_file}: {e}")


_audit_trail: List[Dict[str, Any]] = _load_audit_trail_from_disk()


def _record_audit_entry(entry: Dict[str, Any]) -> None:
    """Add an entry to audit trail, maintain retention limit, and persist."""
    if "error" in entry:
        entry["error"] = _sanitize_audit_error(entry["error"])
    _audit_trail.insert(0, entry)
    while len(_audit_trail) > MAX_AUDIT_ENTRIES:
        _audit_trail.pop()
    _save_audit_trail_to_disk()


def get_allowed_operations() -> Dict[str, Any]:
    """Return dictionary of allowed operational targets, metadata, and impacts."""
    return {
        "targets": OPERATION_METADATA,
        "allowed_operations": ALLOWED_OPERATIONS,
        "rate_limit_seconds": settings.ops_rate_limit_seconds,
    }


def get_audit_trail() -> List[Dict[str, Any]]:
    """Return persistent audit log of past operations without secrets."""
    global _audit_trail
    if not _audit_trail and os.path.exists(settings.ops_audit_file):
        _audit_trail = _load_audit_trail_from_disk()
    return _audit_trail[:MAX_AUDIT_ENTRIES]


async def _poll_container_health(container_name: str, max_wait_seconds: int = 15) -> bool:
    """Actively verify that the container returned to running and healthy state."""
    client = get_docker_client()
    if not client:
        logger.warning(f"Docker client unavailable when verifying health for '{container_name}'.")
        return False

    start_wait = time.time()
    while time.time() - start_wait < max_wait_seconds:
        try:
            c = client.containers.get(container_name)
            is_running = c.status.lower() == "running"
            state = c.attrs.get("State", {})
            health = state.get("Health", {}).get("Status", "unknown").lower()

            # Healthy or running without failing healthcheck
            if is_running and health in ("healthy", "unknown"):
                return True
        except Exception as e:
            logger.warning(f"Health verification poll error for {container_name}: {e}")

        await asyncio.sleep(1.0)

    return False


async def execute_allowed_operation(
    target: str,
    action: str,
    actor: str = "owner-session",
) -> Dict[str, Any]:
    """Execute an allowlisted container operation via the dedicated backend executor."""
    global _last_operation_timestamp

    # 1. Target and action allowlist enforcement
    if target not in ALLOWED_OPERATIONS:
        logger.warning(f"Forbidden operation attempt on non-allowlisted target: '{target}' by {actor}")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Target '{target}' tidak diizinkan. Hanya target allowlisted yang dapat dioperasikan.",
        )

    if action not in ALLOWED_OPERATIONS[target]:
        logger.warning(f"Unsupported action '{action}' on target '{target}' by {actor}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Aksi '{action}' tidak didukung untuk target '{target}'.",
        )

    # 2. Rate limit and concurrency check
    now = time.time()
    elapsed = now - _last_operation_timestamp
    if elapsed < settings.ops_rate_limit_seconds:
        wait_remaining = int(settings.ops_rate_limit_seconds - elapsed)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Batas waktu antar-operasi adalah {settings.ops_rate_limit_seconds}s. Harap tunggu {wait_remaining}s lagi.",
        )

    if _ops_lock.locked():
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Operasi lain sedang berlangsung. Mohon tunggu proses selesai.",
        )

    correlation_id = f"op-{uuid.uuid4().hex[:12]}"
    start_time = time.time()

    async with _ops_lock:
        _last_operation_timestamp = time.time()
        logger.info(f"Starting operation [{correlation_id}]: {action} on {target} by {actor}")

        # 3. Call dedicated backend executor (isolated internal service)
        executor_success = False
        executor_error = None

        try:
            async with httpx.AsyncClient(timeout=15.0) as http_client:
                resp = await http_client.post(
                    f"{settings.ops_executor_url}/execute",
                    headers={"X-Executor-Token": settings.ops_executor_token},
                    json={
                        "target": target,
                        "action": action,
                        "correlation_id": correlation_id,
                    },
                )
                if resp.status_code == 200:
                    executor_success = True
                else:
                    executor_error = f"Executor returned HTTP {resp.status_code}: {resp.text}"
                    logger.error(executor_error)
        except Exception as e:
            executor_error = f"Koneksi ke executor gagal: {e}"
            logger.error(f"Could not reach ops-executor at {settings.ops_executor_url}: {e}")

        if not executor_success:
            duration_ms = int((time.time() - start_time) * 1000)
            audit_entry = {
                "correlation_id": correlation_id,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "actor": actor,
                "target": target,
                "action": action,
                "status": "FAILED",
                "health_verified": False,
                "duration_ms": duration_ms,
                "error": executor_error or "Executor failed",
            }
            _record_audit_entry(audit_entry)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Gagal mengeksekusi operasi: {executor_error or 'Koneksi ke executor gagal'}",
            )

        # 4. Verify post-operation health (request accepted != operation verified)
        health_verified = await _poll_container_health(target, max_wait_seconds=15)
        duration_ms = int((time.time() - start_time) * 1000)

        op_status = "SUCCESS" if health_verified else "WARNING_TIMEOUT"
        audit_entry = {
            "correlation_id": correlation_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "target": target,
            "action": action,
            "status": op_status,
            "health_verified": health_verified,
            "duration_ms": duration_ms,
        }
        _record_audit_entry(audit_entry)

        msg = (
            f"Layanan '{target}' berhasil di-restart dan terverifikasi sehat ({duration_ms}ms)."
            if health_verified
            else f"Layanan '{target}' telah di-restart namun verifikasi kesehatan melebihi batas waktu ({duration_ms}ms)."
        )

        return {
            "success": health_verified,
            "target": target,
            "action": action,
            "correlation_id": correlation_id,
            "health_verified": health_verified,
            "duration_ms": duration_ms,
            "message": msg,
            "audit_entry": audit_entry,
        }


def reset_operations_state_for_tests():
    """Helper for deterministic testing to reset rate limit timer and audit log."""
    global _last_operation_timestamp, _audit_trail
    _last_operation_timestamp = 0.0
    _audit_trail.clear()
    if os.path.exists(settings.ops_audit_file):
        try:
            os.remove(settings.ops_audit_file)
        except OSError:
            pass
