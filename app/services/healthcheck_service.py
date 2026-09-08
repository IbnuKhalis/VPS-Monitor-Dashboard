from app.services.metrics import get_system_metrics
from app.services.docker_service import get_containers_summary
from app.services.backup_service import get_backup_status


def evaluate_overall_health():
    metrics = get_system_metrics()
    docker_data = get_containers_summary()
    backup_data = get_backup_status()

    # 1. Disk evaluation
    disk_pct = metrics["disk"]["percent"]
    if disk_pct >= 85:
        disk_status = "CRITICAL"
    elif disk_pct >= 75:
        disk_status = "WARNING"
    else:
        disk_status = "OK"

    # 2. RAM evaluation
    ram_avail_mb = metrics["ram"]["available_mb"]
    if ram_avail_mb < 512:
        ram_status = "CRITICAL"
    elif ram_avail_mb < 1024:
        ram_status = "WARNING"
    else:
        ram_status = "OK"

    # 3. Docker evaluation
    running_containers = docker_data.get("running_count", 0)
    docker_available = docker_data.get("available", False)
    if not docker_available:
        docker_status = "WARNING"
    elif running_containers < 3:
        docker_status = "WARNING"
    else:
        docker_status = "OK"

    # 4. Backup evaluation
    backup_status = backup_data.get("status", "WARNING")

    # Overall Status computation
    if disk_status == "CRITICAL" or ram_status == "CRITICAL":
        overall = "CRITICAL"
        color = "rose"
    elif (
        disk_status == "WARNING"
        or ram_status == "WARNING"
        or docker_status == "WARNING"
        or backup_status != "HEALTHY"
    ):
        overall = "WARNING"
        color = "amber"
    else:
        overall = "HEALTHY"
        color = "emerald"

    return {
        "overall_status": overall,
        "badge_color": color,
        "timestamp": metrics["timestamp"],
        "checks": {
            "disk": {
                "status": disk_status,
                "used_percent": disk_pct,
                "used_gb": metrics["disk"]["used_gb"],
                "total_gb": metrics["disk"]["total_gb"],
            },
            "ram": {
                "status": ram_status,
                "used_percent": metrics["ram"]["percent"],
                "used_mb": metrics["ram"]["used_mb"],
                "available_mb": ram_avail_mb,
                "total_mb": metrics["ram"]["total_mb"],
            },
            "docker": {
                "status": docker_status,
                "running": running_containers,
                "total": docker_data.get("total_count", 0),
            },
            "backup": {
                "status": backup_status,
                "message": backup_data.get("message"),
                "latest_time": backup_data.get("latest_time"),
                "age_hours": backup_data.get("age_hours"),
            },
        },
    }
