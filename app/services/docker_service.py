import json
import logging
import re
import threading
import time
import urllib.request
import urllib.error
from typing import Any, Dict, Optional
import docker
from app.config import settings

logger = logging.getLogger("docker_service")

MAX_LOG_CHARS = 50000
CONTAINER_NAME_REGEX = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]+$")

SECRET_PATTERNS = [
    (re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.\~]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"((?:X-Ops-Token|OPS_PROXY_TOKEN)\s*[:=]\s*)[A-Za-z0-9_\-]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b([A-Za-z0-9_]*(?:password|passwd|secret|token|api_?key|private_?key))(\s*[:=]\s*)(['\"]?)([^\'\"\s\r\n]{4,})\3"), r"\1\2\3[REDACTED]\3"),
    (re.compile(r"-----BEGIN [A-Z ]+ PRIVATE KEY-----[\s\S]*?-----END [A-Z ]+ PRIVATE KEY-----"), "[PRIVATE KEY REDACTED]"),
]


def sanitize_log_output(text: str) -> str:
    """Sanitize sensitive credentials, tokens, and private keys from container logs."""
    if not text:
        return ""
    sanitized = text
    for pattern, replacement in SECRET_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


def get_docker_client():
    """Attempt to initialize Docker client from environment or socket path (local dev fallback)."""
    try:
        return docker.from_env(timeout=3)
    except Exception as e:
        logger.debug(f"Could not connect to local Docker socket: {e}")
        return None


def _call_executor(path: str, timeout: float = 4.0) -> Optional[Dict[str, Any]]:
    """Call restricted ops executor over internal network."""
    url = f"{settings.ops_executor_url.rstrip('/')}{path}"
    headers = {
        "X-Executor-Token": settings.ops_executor_token,
        "User-Agent": "VPS-Mission-Control-App/1.1",
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            if response.status == 200:
                return json.loads(response.read().decode("utf-8"))
    except Exception as e:
        logger.debug(f"Ops-executor call {path} failed: {e}")
    return None


def calculate_cpu_percent(stats: dict) -> float:
    """Calculate CPU usage percentage from Docker container stats payload."""
    try:
        cpu_stats = stats.get("cpu_stats", {})
        precpu_stats = stats.get("precpu_stats", {})

        cpu_delta = cpu_stats.get("cpu_usage", {}).get("total_usage", 0) - precpu_stats.get(
            "cpu_usage", {}
        ).get("total_usage", 0)
        system_delta = cpu_stats.get("system_cpu_usage", 0) - precpu_stats.get(
            "system_cpu_usage", 0
        )

        online_cpus = cpu_stats.get("online_cpus") or len(
            cpu_stats.get("cpu_usage", {}).get("percpu_usage", []) or [1]
        )
        if online_cpus == 0:
            online_cpus = 1

        if system_delta > 0.0 and cpu_delta > 0.0:
            return round((cpu_delta / system_delta) * online_cpus * 100.0, 1)
    except Exception:
        pass
    return 0.0


def calculate_memory_stats(stats: dict) -> dict:
    """Extract memory usage and limit from Docker container stats payload."""
    try:
        mem_stats = stats.get("memory_stats", {})
        usage = mem_stats.get("usage", 0)
        limit = mem_stats.get("limit", 1)

        stats_detail = mem_stats.get("stats", {})
        cache = stats_detail.get("inactive_file", stats_detail.get("total_inactive_file", 0))
        actual_usage = max(0, usage - cache)

        percent = round((actual_usage / limit) * 100.0, 1) if limit > 0 else 0.0
        return {
            "used_mb": round(actual_usage / (1024 * 1024), 1),
            "limit_mb": round(limit / (1024 * 1024), 1),
            "percent": percent,
        }
    except Exception:
        return {"used_mb": 0.0, "limit_mb": 0.0, "percent": 0.0}


_summary_cache = None
_summary_cache_time = 0.0
_summary_lock = threading.Lock()
SUMMARY_CACHE_TTL = 3.0  # seconds


def reset_docker_summary_cache_for_tests() -> None:
    """Helper for testing to reset summary cache."""
    global _summary_cache, _summary_cache_time
    with _summary_lock:
        _summary_cache = None
        _summary_cache_time = 0.0


def get_containers_summary() -> Dict[str, Any]:
    """Retrieve list and status of all Docker containers via ops-executor or local fallback (Codex S5)."""
    global _summary_cache, _summary_cache_time
    now = time.time()
    with _summary_lock:
        if _summary_cache is not None and (now - _summary_cache_time < SUMMARY_CACHE_TTL):
            return _summary_cache

    # 1. Primary least-privilege route: query isolated ops-executor daemon
    executor_data = _call_executor("/containers")
    if executor_data and executor_data.get("available") is True:
        with _summary_lock:
            _summary_cache = executor_data
            _summary_cache_time = time.time()
        return executor_data

    # 2. Local fallback if socket exists (unit tests / local dev)
    client = get_docker_client()
    if not client:
        return {
            "available": False,
            "error": "Docker socket not accessible and ops-executor unreachable",
            "containers": [],
            "running_count": 0,
            "total_count": 0,
        }

    try:
        containers = client.containers.list(all=True)
        result = []
        running_count = 0

        for c in containers:
            status = c.status.lower()
            is_running = status == "running"
            if is_running:
                running_count += 1

            health = "unknown"
            state_dict = c.attrs.get("State", {})
            if "Health" in state_dict:
                health = state_dict["Health"].get("Status", "unknown")

            mem_data = {"used_mb": 0.0, "limit_mb": 0.0, "percent": 0.0}
            cpu_pct = 0.0

            if is_running:
                try:
                    stats = c.stats(stream=False)
                    mem_data = calculate_memory_stats(stats)
                    cpu_pct = calculate_cpu_percent(stats)
                except Exception:
                    pass

            ports_raw = c.attrs.get("NetworkSettings", {}).get("Ports", {})
            ports_summary = []
            if ports_raw:
                for port_proto, bindings in ports_raw.items():
                    if bindings:
                        for b in bindings:
                            ports_summary.append(f"{b.get('HostPort')}:{port_proto}")
                    else:
                        ports_summary.append(port_proto)

            result.append(
                {
                    "id": c.short_id,
                    "name": c.name,
                    "image": c.image.tags[0] if c.image.tags else c.image.short_id,
                    "status": status,
                    "health": health,
                    "is_running": is_running,
                    "created": c.attrs.get("Created", "")[:19].replace("T", " "),
                    "restart_count": state_dict.get("RestartCount", 0),
                    "ports": ", ".join(ports_summary) if ports_summary else "-",
                    "cpu_percent": cpu_pct,
                    "memory": mem_data,
                }
            )

        result.sort(key=lambda x: (not x["is_running"], x["name"]))
        payload = {
            "available": True,
            "containers": result,
            "running_count": running_count,
            "total_count": len(containers),
        }
        with _summary_lock:
            _summary_cache = payload
            _summary_cache_time = time.time()
        return payload
    except Exception as e:
        logger.error(f"Error fetching container summaries from local client: {e}")
        return {
            "available": False,
            "error": str(e),
            "containers": [],
            "running_count": 0,
            "total_count": 0,
        }


def get_container_logs(container_name: str, tail: int = 60) -> dict:
    """Retrieve recent log lines for a container with sanitization and size cap."""
    if not container_name or not CONTAINER_NAME_REGEX.match(container_name):
        return {
            "success": False,
            "error": f"Invalid container name format: '{container_name}'",
            "logs": "",
        }

    # 1. Primary route: ops-executor daemon
    executor_data = _call_executor(f"/containers/{container_name}/logs?tail={tail}")
    if executor_data and "success" in executor_data:
        return executor_data

    # 2. Local fallback if socket exists
    client = get_docker_client()
    if not client:
        return {"success": False, "error": "Docker socket not accessible and ops-executor unreachable", "logs": ""}

    try:
        container = client.containers.get(container_name)
        logs_bytes = container.logs(tail=tail, timestamps=True)
        logs_text = logs_bytes.decode("utf-8", errors="replace")

        truncated_notice = ""
        if len(logs_text) > MAX_LOG_CHARS:
            logs_text = logs_text[-MAX_LOG_CHARS:]
            truncated_notice = "[... Log dipotong demi keamanan & batas ukuran (maks 50KB) ...]\n"

        sanitized_logs = truncated_notice + sanitize_log_output(logs_text)
        return {
            "success": True,
            "name": container.name,
            "status": container.status,
            "logs": sanitized_logs,
        }
    except docker.errors.NotFound:
        return {"success": False, "error": f"Container '{container_name}' not found", "logs": ""}
    except Exception as e:
        return {"success": False, "error": str(e), "logs": ""}


def get_container_inspect(container_name: str) -> Optional[Dict[str, Any]]:
    """Retrieve status and health inspection for a specific container."""
    if not container_name or not CONTAINER_NAME_REGEX.match(container_name):
        return None

    # 1. Primary route: ops-executor
    inspect_data = _call_executor(f"/containers/{container_name}/inspect")
    if inspect_data and inspect_data.get("success") is True:
        return inspect_data

    # 2. Local fallback
    client = get_docker_client()
    if not client:
        return None

    try:
        c = client.containers.get(container_name)
        state = c.attrs.get("State", {})
        is_running = c.status.lower() == "running"
        health = state.get("Health", {}).get("Status", "unknown").lower()
        return {
            "success": True,
            "id": c.short_id,
            "name": c.name,
            "status": c.status,
            "is_running": is_running,
            "health": health,
            "state": state,
        }
    except Exception:
        return None
