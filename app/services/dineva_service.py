import logging
import re
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from app.services.docker_service import (
    get_container_inspect,
    get_container_logs,
    get_containers_summary,
)

logger = logging.getLogger("dineva_service")

DINEVA_CONTAINER_NAME = "digitalneeds04-runtime"
SIDECAR_CONTAINER_NAME = "digitalneeds04-ops-sidecar"

# Regex for parsing safe log events from Hermes runtime
LOG_TIMESTAMP_REGEX = re.compile(
    r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:,\d{3})?)\s+(?:\[?[A-Z]+\]?)\s+([a-zA-Z0-9_\.-]+):?\s*(.*)"
)


def _extract_safe_last_activity(raw_logs: str) -> Dict[str, Any]:
    """Extract and sanitize timestamp and event component from the most recent log line.

    Strict boundary: Never exposes raw prompts, user messages, tokens, or vault paths.
    """
    if not raw_logs or not raw_logs.strip():
        return {
            "timestamp": None,
            "component": "idle",
            "summary": "Menunggu aktivitas terjadwal",
        }

    lines = [ln.strip() for ln in raw_logs.strip().splitlines() if ln.strip()]
    # Search backwards for a structured line with timestamp
    for line in reversed(lines):
        match = LOG_TIMESTAMP_REGEX.match(line)
        if match:
            ts, component, raw_msg = match.groups()

            # Sanitize component name to safe category
            safe_component = component.lower()
            if "cron" in safe_component or "scheduler" in safe_component:
                summary = "Eksekusi pemantauan berkala (Cron Scheduler)"
            elif "gateway" in safe_component or "discord" in safe_component:
                summary = "Koneksi gateway komunikasi aktif"
            elif "tool" in safe_component:
                summary = "Pemeriksaan perkakas terisolasi selesai"
            elif "chat" in safe_component or "agent" in safe_component:
                summary = "Pemrosesan penalaran internal"
            else:
                summary = "Siklus operasi rutin"

            return {
                "timestamp": ts.split(",")[0],  # remove milliseconds for display
                "component": component,
                "summary": summary,
            }

    return {
        "timestamp": None,
        "component": "idle",
        "summary": "Menunggu aktivitas terjadwal",
    }


def get_dineva_status() -> Dict[str, Any]:
    """Retrieve read-only telemetry for Dineva autonomous agent via ops-executor or fallback.

    Enforces strict read-only boundary: no mutative commands, no secret exposure, no raw Docker socket.
    """
    # 1. Query containers summary to get resource metrics without touching raw docker socket
    summary = get_containers_summary()
    containers_by_name = {c["name"]: c for c in summary.get("containers", [])} if summary.get("available") else {}

    runtime_c = containers_by_name.get(DINEVA_CONTAINER_NAME)
    sidecar_c = containers_by_name.get(SIDECAR_CONTAINER_NAME)

    # 2. Inspect runtime
    runtime_inspect = get_container_inspect(DINEVA_CONTAINER_NAME)
    is_running = False
    started_at = None
    restart_count = 0

    if runtime_inspect:
        is_running = runtime_inspect.get("is_running", False)
        state = runtime_inspect.get("state", {})
        restart_count = state.get("RestartCount", 0)
        started_at = state.get("StartedAt", "")
        if started_at:
            started_at = started_at[:19].replace("T", " ")
    elif runtime_c:
        is_running = runtime_c.get("is_running", False)
        restart_count = runtime_c.get("restart_count", 0)

    # Metrics
    cpu_percent = runtime_c.get("cpu_percent", 0.0) if runtime_c else 0.0
    mem_info = runtime_c.get("memory", {"used_mb": 0.0, "percent": 0.0}) if runtime_c else {"used_mb": 0.0, "percent": 0.0}

    # Safe log inspection
    last_act = {
        "timestamp": None,
        "component": "idle",
        "summary": "Runtime offline" if not is_running else "Menunggu aktivitas terjadwal",
    }
    if is_running:
        log_res = get_container_logs(DINEVA_CONTAINER_NAME, tail=15)
        if log_res.get("success"):
            last_act = _extract_safe_last_activity(log_res.get("logs", ""))

    # 3. Inspect sidecar
    sidecar_inspect = get_container_inspect(SIDECAR_CONTAINER_NAME)
    sidecar_running = False
    if sidecar_inspect:
        sidecar_running = sidecar_inspect.get("is_running", False)
    elif sidecar_c:
        sidecar_running = sidecar_c.get("is_running", False)

    sidecar_info = {
        "name": SIDECAR_CONTAINER_NAME,
        "status": "ONLINE" if sidecar_running else "OFFLINE",
        "is_running": sidecar_running,
        "port": 9091,
    }

    # 4. Compile sanitized response
    return {
        "available": summary.get("available", False) or (runtime_inspect is not None),
        "agent_id": "digitalneeds04",
        "name": "Dineva",
        "role": "Autonomous VPS Worker & Second Brain Custodian",
        "platform": "Hermes Gateway",
        "status": "ONLINE" if is_running else "STOPPED",
        "is_running": is_running,
        "started_at": started_at,
        "restart_count": restart_count,
        "cpu_percent": cpu_percent,
        "memory": {
            "used_mb": mem_info.get("used_mb", 0.0),
            "percent": mem_info.get("percent", 0.0),
        },
        "last_activity": last_act,
        "sidecar": sidecar_info,
        "mode": "READ_ONLY",
        "mutations_allowed": False,
    }
