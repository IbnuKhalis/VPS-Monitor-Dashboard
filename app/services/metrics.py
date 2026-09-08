import os
import platform
import time
from datetime import datetime, timezone, timedelta
import psutil

# Track previous network counters for rate calculation
_last_net_check_time = 0.0
_last_net_bytes_sent = 0
_last_net_bytes_recv = 0
_net_rate_tx_kbps = 0.0
_net_rate_rx_kbps = 0.0


def get_network_rates():
    global _last_net_check_time, _last_net_bytes_sent, _last_net_bytes_recv
    global _net_rate_tx_kbps, _net_rate_rx_kbps

    now = time.time()
    net = psutil.net_io_counters()

    if _last_net_check_time > 0:
        elapsed = now - _last_net_check_time
        if elapsed >= 0.5:
            delta_sent = net.bytes_sent - _last_net_bytes_sent
            delta_recv = net.bytes_recv - _last_net_bytes_recv
            _net_rate_tx_kbps = round((delta_sent / elapsed) / 1024, 1)
            _net_rate_rx_kbps = round((delta_recv / elapsed) / 1024, 1)
            _last_net_check_time = now
            _last_net_bytes_sent = net.bytes_sent
            _last_net_bytes_recv = net.bytes_recv
    else:
        _last_net_check_time = now
        _last_net_bytes_sent = net.bytes_sent
        _last_net_bytes_recv = net.bytes_recv

    return {
        "tx_kbps": _net_rate_tx_kbps,
        "rx_kbps": _net_rate_rx_kbps,
        "total_sent_mb": round(net.bytes_sent / (1024 * 1024), 1),
        "total_recv_mb": round(net.bytes_recv / (1024 * 1024), 1),
    }


def get_system_uptime():
    boot_time = psutil.boot_time()
    uptime_seconds = int(time.time() - boot_time)

    days = uptime_seconds // 86400
    hours = (uptime_seconds % 86400) // 3600
    minutes = (uptime_seconds % 3600) // 60

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")

    return {
        "seconds": uptime_seconds,
        "formatted": " ".join(parts),
        "boot_time": datetime.fromtimestamp(boot_time).strftime("%Y-%m-%d %H:%M:%S"),
    }


def get_system_metrics():
    # 1. CPU
    cpu_percent = psutil.cpu_percent(interval=0.1)
    cpu_per_core = psutil.cpu_percent(interval=None, percpu=True)
    cpu_count = psutil.cpu_count(logical=True)
    cpu_freq = psutil.cpu_freq()

    # 2. RAM
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()

    # 3. Disk (Root partition)
    # Determine appropriate mount path
    root_path = "/" if os.name != "nt" else "C:\\"
    try:
        disk = psutil.disk_usage(root_path)
    except Exception:
        # Fallback to current working directory
        disk = psutil.disk_usage(".")

    # 4. Network
    net_data = get_network_rates()

    # 5. Uptime & Time
    uptime_data = get_system_uptime()

    # Local server time in WIB (UTC+7)
    tz_wib = timezone(timedelta(hours=7))
    now_wib = datetime.now(tz_wib).strftime("%Y-%m-%d %H:%M:%S WIB")

    return {
        "timestamp": now_wib,
        "hostname": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "arch": platform.machine(),
        "uptime": uptime_data,
        "cpu": {
            "percent": round(cpu_percent, 1),
            "cores": cpu_count,
            "per_core": [round(c, 1) for c in cpu_per_core],
            "freq_mhz": round(cpu_freq.current, 0) if cpu_freq else None,
        },
        "ram": {
            "total_gb": round(mem.total / (1024**3), 2),
            "used_gb": round(mem.used / (1024**3), 2),
            "available_gb": round(mem.available / (1024**3), 2),
            "percent": round(mem.percent, 1),
            "total_mb": round(mem.total / (1024**2), 0),
            "used_mb": round(mem.used / (1024**2), 0),
            "available_mb": round(mem.available / (1024**2), 0),
        },
        "swap": {
            "total_gb": round(swap.total / (1024**3), 2),
            "used_gb": round(swap.used / (1024**3), 2),
            "percent": round(swap.percent, 1),
        },
        "disk": {
            "mount": root_path,
            "total_gb": round(disk.total / (1024**3), 1),
            "used_gb": round(disk.used / (1024**3), 1),
            "free_gb": round(disk.free / (1024**3), 1),
            "percent": round(disk.percent, 1),
        },
        "network": net_data,
    }
