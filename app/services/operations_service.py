import asyncio
from datetime import datetime, timezone
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional
import uuid

from fastapi import HTTPException, status
import httpx
from app.config import settings
from app.services.docker_service import get_container_inspect

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


def _check_audit_writable() -> bool:
    """Pre-flight check: ensure audit trail destination is writable before performing mutations (Codex R4)."""
    audit_file = settings.ops_audit_file
    target_dir = os.path.dirname(os.path.abspath(audit_file))
    try:
        os.makedirs(target_dir, exist_ok=True)
        # Test file write permissions
        test_file = os.path.join(target_dir, f".write_test_{uuid.uuid4().hex[:6]}")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("probe")
        if os.path.exists(test_file):
            os.remove(test_file)
        return True
    except Exception as e:
        logger.critical(f"DURABILITY PRE-CHECK FAILED: Audit directory {target_dir} is not writable: {e}")
        return False


def _load_audit_trail_from_disk() -> List[Dict[str, Any]]:
    """Load persistent audit entries from disk."""
    audit_file = settings.ops_audit_file
    if os.path.exists(audit_file):
        try:
            with open(audit_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and "audit_persisted" not in item:
                            item["audit_persisted"] = True
                    return data[:MAX_AUDIT_ENTRIES]
        except Exception as e:
            logger.warning(f"Failed to read audit trail file {audit_file}: {e}")
    return []


def _save_audit_trail_to_disk() -> bool:
    """Write in-memory audit trail to disk atomically. Returns True on success, False on failure (Codex R4)."""
    audit_file = settings.ops_audit_file
    try:
        os.makedirs(os.path.dirname(os.path.abspath(audit_file)), exist_ok=True)
        tmp_file = f"{audit_file}.tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(_audit_trail[:MAX_AUDIT_ENTRIES], f, indent=2)
        os.replace(tmp_file, audit_file)
        return True
    except Exception as e:
        logger.error(f"DURABILITY FAILURE: Failed to write audit trail to {audit_file}: {e}")
        return False


_audit_trail: List[Dict[str, Any]] = _load_audit_trail_from_disk()


def _record_audit_entry(entry: Dict[str, Any]) -> bool:
    """Add an entry to audit trail, maintain retention limit, and persist with durability flag (Codex S4)."""
    if "error" in entry:
        entry["error"] = _sanitize_audit_error(entry["error"])

    # Optimistically set audit_persisted: True so that serialized file on disk contains the flag (Codex S4)
    entry["audit_persisted"] = True
    _audit_trail.insert(0, entry)
    while len(_audit_trail) > MAX_AUDIT_ENTRIES:
        _audit_trail.pop()

    persisted = _save_audit_trail_to_disk()
    if not persisted:
        entry["audit_persisted"] = False
        logger.warning(f"Audit entry {entry.get('correlation_id')} recorded in-memory only (disk write failed).")
    return persisted


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
    """Actively verify that the container returned to running and healthy state via executor/inspect."""
    start_wait = time.time()
    while time.time() - start_wait < max_wait_seconds:
        inspect = get_container_inspect(container_name)
        if inspect:
            is_running = inspect.get("is_running", False)
            health = inspect.get("health", "unknown").lower()

            # Healthy or running without failing healthcheck
            if is_running and health in ("healthy", "unknown"):
                return True
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
        logger.warning(f"Forbidden operation attempt with unapproved action '{action}' on target '{target}' by {actor}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Aksi '{action}' tidak didukung untuk target '{target}'.",
        )

    # 2. Rate limiting check (e.g. 30 seconds interval between mutations)
    now = time.time()
    elapsed = now - _last_operation_timestamp
    if elapsed < settings.ops_rate_limit_seconds:
        remaining = int(settings.ops_rate_limit_seconds - elapsed)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Batas waktu antar-operasi ({settings.ops_rate_limit_seconds}s) belum terpenuhi. Tunggu {remaining} detik sebelum mengeksekusi operasi berikutnya.",
        )


    # 3. Pre-flight check: ensure audit trail destination is writable before performing mutation (Codex R4)
    if not _check_audit_writable():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Penyimpanan audit trail tidak dapat ditulis (storage read-only / unwriteable). Operasi dibatalkan demi integritas audit.",
        )

    # 4. Anti-concurrency lock
    if _ops_lock.locked():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Operasi lain sedang berlangsung. Mohon tunggu proses selesai.",
        )

    correlation_id = f"op-{uuid.uuid4().hex[:12]}"
    start_time = time.time()

    async with _ops_lock:
        _last_operation_timestamp = time.time()
        logger.info(f"Starting operation [{correlation_id}]: {action} on {target} by {actor}")

        # 5. Call dedicated backend executor (isolated internal service)
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

        # 6. Verify post-operation health (request accepted != operation verified)
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
        persisted = _record_audit_entry(audit_entry)

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
            "audit_persisted": persisted,
            "persistence_warning": None if persisted else "Perhatian: Operasi berhasil namun penulisan berkas audit ke disk mengalami kendala.",
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
