import logging
import re
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from app.services.docker_service import (
    get_docker_client,
    calculate_cpu_percent,
    calculate_memory_stats,
    sanitize_log_output,
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
    """Retrieve read-only telemetry for Dineva autonomous agent.

    Enforces strict read-only boundary: no mutative commands, no secret exposure.
    """
    client = get_docker_client()
    if not client:
        return {
            "available": False,
            "agent_id": "digitalneeds04",
            "name": "Dineva",
            "role": "Autonomous VPS Worker & Second Brain Custodian",
            "status": "UNKNOWN",
            "is_running": False,
            "message": "Docker socket tidak dapat diakses",
            "last_activity": {
                "timestamp": None,
                "component": "idle",
                "summary": "Docker socket tidak dapat diakses",
            },
            "sidecar": {
                "name": SIDECAR_CONTAINER_NAME,
                "status": "OFFLINE",
                "is_running": False,
                "port": 9091,
            },
            "mode": "READ_ONLY",
            "mutations_allowed": False,
        }

    # 1. Inspect main runtime container
    runtime_info = {
        "status": "STOPPED",
        "is_running": False,
        "uptime": "-",
        "started_at": None,
        "restart_count": 0,
        "cpu_percent": 0.0,
        "memory": {"used_mb": 0.0, "percent": 0.0},
        "last_activity": {
            "timestamp": None,
            "component": "idle",
            "summary": "Runtime offline",
        },
    }

    try:
        container = client.containers.get(DINEVA_CONTAINER_NAME)
        status_raw = container.status.lower()
        is_running = status_raw == "running"
        runtime_info["is_running"] = is_running
        runtime_info["status"] = "ONLINE" if is_running else "STOPPED"
        runtime_info["restart_count"] = container.attrs.get("State", {}).get("RestartCount", 0)
        started_at = container.attrs.get("State", {}).get("StartedAt", "")
        if started_at:
            runtime_info["started_at"] = started_at[:19].replace("T", " ")

        if is_running:
            try:
                stats = container.stats(stream=False)
                runtime_info["cpu_percent"] = calculate_cpu_percent(stats)
                mem = calculate_memory_stats(stats)
                runtime_info["memory"] = {
                    "used_mb": mem.get("used_mb", 0.0),
                    "percent": mem.get("percent", 0.0),
                }
            except Exception:
                pass

            # Safe log inspection (last 15 lines max)
            try:
                raw_bytes = container.logs(tail=15, timestamps=False)
                raw_text = raw_bytes.decode("utf-8", errors="replace")
                runtime_info["last_activity"] = _extract_safe_last_activity(raw_text)
            except Exception as e:
                logger.warning(f"Could not read Dineva logs: {e}")

    except Exception:
        # Container not found or stopped
        pass

    # 2. Inspect ops sidecar container
    sidecar_info = {
        "name": SIDECAR_CONTAINER_NAME,
        "status": "OFFLINE",
        "is_running": False,
        "port": 9091,
    }

    try:
        sidecar_c = client.containers.get(SIDECAR_CONTAINER_NAME)
        sidecar_running = sidecar_c.status.lower() == "running"
        sidecar_info["is_running"] = sidecar_running
        sidecar_info["status"] = "ONLINE" if sidecar_running else "OFFLINE"
    except Exception:
        pass

    # 3. Compile sanitized response
    return {
        "available": True,
        "agent_id": "digitalneeds04",
        "name": "Dineva",
        "role": "Autonomous VPS Worker & Second Brain Custodian",
        "platform": "Hermes Gateway",
        "status": runtime_info["status"],
        "is_running": runtime_info["is_running"],
        "started_at": runtime_info["started_at"],
        "restart_count": runtime_info["restart_count"],
        "cpu_percent": runtime_info["cpu_percent"],
        "memory": runtime_info["memory"],
        "last_activity": runtime_info["last_activity"],
        "sidecar": sidecar_info,
        "mode": "READ_ONLY",
        "mutations_allowed": False,
    }
