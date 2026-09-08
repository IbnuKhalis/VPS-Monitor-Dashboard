import os
import glob
import time
from datetime import datetime
from app.config import settings


def get_backup_status():
    backup_base = settings.backup_dir

    # Check potential subfolders: /daily or direct root
    daily_dirs = [
        os.path.join(backup_base, "daily"),
        backup_base,
        "D:\\Backups\\VPS",  # Local dev fallback
    ]

    all_files = []
    for d in daily_dirs:
        if os.path.isdir(d):
            # Look for sql.gz, tar.gz, or sql files
            patterns = ["*.sql.gz", "*.tar.gz", "db_*.sql.gz", "*backup*"]
            for p in patterns:
                found = glob.glob(os.path.join(d, p))
                all_files.extend(found)

    # Remove duplicates
    all_files = list(set(all_files))

    if not all_files:
        return {
            "status": "WARNING",
            "message": "Belum ditemukan file backup di direktori backup",
            "latest_file": None,
            "latest_time": None,
            "age_hours": None,
            "size_mb": 0.0,
            "total_files": 0,
            "sla_healthy": False,
        }

    # Sort by modification time descending
    file_stats = []
    total_size_bytes = 0

    for f in all_files:
        try:
            st = os.stat(f)
            total_size_bytes += st.st_size
            file_stats.append(
                {
                    "path": f,
                    "name": os.path.basename(f),
                    "size_mb": round(st.st_size / (1024 * 1024), 2),
                    "mtime": st.st_mtime,
                    "time_formatted": datetime.fromtimestamp(st.st_mtime).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                }
            )
        except OSError:
            continue

    if not file_stats:
        return {
            "status": "WARNING",
            "message": "Tidak dapat membaca metadata file backup",
            "latest_file": None,
            "latest_time": None,
            "age_hours": None,
            "size_mb": 0.0,
            "total_files": 0,
            "sla_healthy": False,
        }

    file_stats.sort(key=lambda x: x["mtime"], reverse=True)
    latest = file_stats[0]

    now = time.time()
    age_seconds = now - latest["mtime"]
    age_hours = round(age_seconds / 3600, 1)

    # 26 hours SLA window (daily backup at 02:00 UTC)
    sla_healthy = age_hours <= 26.0
    status = "HEALTHY" if sla_healthy else "WARNING"
    message = (
        f"Backup terbaru dibuat {age_hours} jam yang lalu"
        if sla_healthy
        else f"Peringatan: Usia backup ({age_hours} jam) telah melampaui batas SLA 26 jam"
    )

    return {
        "status": status,
        "message": message,
        "latest_file": latest["name"],
        "latest_time": latest["time_formatted"],
        "age_hours": age_hours,
        "size_mb": latest["size_mb"],
        "total_size_mb": round(total_size_bytes / (1024 * 1024), 2),
        "total_files": len(file_stats),
        "sla_healthy": sla_healthy,
        "recent_backups": file_stats[:5],
    }
