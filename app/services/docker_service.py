import logging
from datetime import datetime
import docker
from app.config import settings

logger = logging.getLogger("docker_service")


def get_docker_client():
    """Attempt to initialize Docker client from environment or socket path."""
    try:
        return docker.from_env(timeout=3)
    except Exception as e:
        logger.warning(f"Could not connect to Docker socket: {e}")
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

        # Subtract cache memory if available (standard docker stats formula)
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


def get_containers_summary():
    """Retrieve list and status of all Docker containers."""
    client = get_docker_client()
    if not client:
        return {
            "available": False,
            "error": "Docker socket not accessible or Docker is not running",
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

            # Health check status if container has HEALTHCHECK defined
            health = "unknown"
            state_dict = c.attrs.get("State", {})
            if "Health" in state_dict:
                health = state_dict["Health"].get("Status", "unknown")

            # Quick stats for running containers
            mem_data = {"used_mb": 0.0, "limit_mb": 0.0, "percent": 0.0}
            cpu_pct = 0.0

            if is_running:
                try:
                    stats = c.stats(stream=False)
                    mem_data = calculate_memory_stats(stats)
                    cpu_pct = calculate_cpu_percent(stats)
                except Exception:
                    pass

            # Port mappings
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

        # Sort: running first, then alphabetically by name
        result.sort(key=lambda x: (not x["is_running"], x["name"]))

        return {
            "available": True,
            "containers": result,
            "running_count": running_count,
            "total_count": len(containers),
        }
    except Exception as e:
        logger.error(f"Error fetching container summaries: {e}")
        return {
            "available": False,
            "error": str(e),
            "containers": [],
            "running_count": 0,
            "total_count": 0,
        }


def get_container_logs(container_name: str, tail: int = 60) -> dict:
    """Retrieve recent log lines for a specific container."""
    client = get_docker_client()
    if not client:
        return {"success": False, "error": "Docker socket not accessible", "logs": ""}

    try:
        container = client.containers.get(container_name)
        logs_bytes = container.logs(tail=tail, timestamps=True)
        logs_text = logs_bytes.decode("utf-8", errors="replace")
        return {
            "success": True,
            "name": container.name,
            "status": container.status,
            "logs": logs_text,
        }
    except docker.errors.NotFound:
        return {"success": False, "error": f"Container '{container_name}' not found", "logs": ""}
    except Exception as e:
        return {"success": False, "error": str(e), "logs": ""}
