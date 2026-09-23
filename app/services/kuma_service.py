import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import httpx
from app.config import settings


_cache: Dict[str, Any] = {
    "data": None,
    "timestamp": 0.0,
}

STATUS_MAP = {
    1: ("UP", "Sehat"),
    0: ("DOWN", "Gangguan"),
    2: ("PENDING", "Memeriksa"),
    3: ("MAINTENANCE", "Pemeliharaan"),
}


def build_unknown_summary(reason: str = "Data status belum tersedia dari Uptime Kuma.") -> Dict[str, Any]:
    """Return a safe fallback summary when Uptime Kuma cannot be reached or returns invalid data."""
    return {
        "available": False,
        "overall_status": "UNKNOWN",
        "overall_label": "Tidak diketahui",
        "title": "DigitalNeeds System Status",
        "slug": settings.kuma_status_slug,
        "public_url": settings.kuma_public_url,
        "admin_url": f"{settings.kuma_public_url.rstrip('/')}/dashboard",
        "total_monitors": 0,
        "up_monitors": 0,
        "down_monitors": 0,
        "maintenance_monitors": 0,
        "unknown_monitors": 0,
        "degraded_monitors": [],
        "groups": [],
        "monitors": [],
        "incident": None,
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "error": reason,
    }


def parse_kuma_status_payload(page_data: Any, heartbeat_data: Any) -> Dict[str, Any]:
    """Validate and transform Uptime Kuma public status-page and heartbeat JSON payloads."""
    if not isinstance(page_data, dict) or not isinstance(heartbeat_data, dict):
        return build_unknown_summary("Format respons Uptime Kuma tidak valid.")

    config = page_data.get("config")
    public_groups = page_data.get("publicGroupList")
    heartbeat_list = heartbeat_data.get("heartbeatList")
    uptime_list = heartbeat_data.get("uptimeList")

    if not isinstance(config, dict) or not isinstance(public_groups, list):
        return build_unknown_summary("Struktur halaman status Uptime Kuma tidak lengkap.")
    if not isinstance(heartbeat_list, dict) or not isinstance(uptime_list, dict):
        return build_unknown_summary("Data heartbeat Uptime Kuma tidak lengkap.")

    monitors: List[Dict[str, Any]] = []
    groups_summary: List[Dict[str, Any]] = []

    up_count = 0
    down_count = 0
    maintenance_count = 0
    unknown_count = 0
    degraded_monitors: List[Dict[str, Any]] = []
    latest_time: Optional[str] = None

    for group in public_groups:
        if not isinstance(group, dict):
            continue
        group_name = str(group.get("name") or "Layanan")
        monitor_list = group.get("monitorList")
        if not isinstance(monitor_list, list):
            continue

        group_monitors: List[Dict[str, Any]] = []
        for mon in monitor_list:
            if not isinstance(mon, dict) or "id" not in mon:
                continue
            mon_id = mon["id"]
            mon_key = str(mon_id)
            mon_name = str(mon.get("name") or f"Monitor {mon_id}")
            mon_url = mon.get("url") if mon.get("sendUrl") else None

            beats = heartbeat_list.get(mon_key)
            latest_beat = beats[-1] if isinstance(beats, list) and len(beats) > 0 and isinstance(beats[-1], dict) else None

            if latest_beat is not None:
                raw_status = latest_beat.get("status")
                state_code, state_label = STATUS_MAP.get(raw_status, ("UNKNOWN", "Tidak diketahui"))
                ping_ms = latest_beat.get("ping")
                beat_time = latest_beat.get("time")
                if isinstance(beat_time, str) and (latest_time is None or beat_time > latest_time):
                    latest_time = beat_time
            else:
                state_code, state_label = ("UNKNOWN", "Tidak diketahui")
                ping_ms = None
                beat_time = None

            if state_code == "UP":
                up_count += 1
            elif state_code == "DOWN":
                down_count += 1
            elif state_code == "MAINTENANCE":
                maintenance_count += 1
            else:
                unknown_count += 1

            raw_uptime = uptime_list.get(f"{mon_key}_24")
            uptime_24h_pct = round(float(raw_uptime) * 100, 2) if isinstance(raw_uptime, (int, float)) else None

            item = {
                "id": mon_id,
                "name": mon_name,
                "group": group_name,
                "url": mon_url,
                "status": state_code,
                "label": state_label,
                "ping_ms": ping_ms if isinstance(ping_ms, (int, float)) else None,
                "uptime_24h_pct": uptime_24h_pct,
                "last_check": beat_time,
            }
            monitors.append(item)
            group_monitors.append(item)

            if state_code in ("DOWN", "MAINTENANCE", "PENDING", "UNKNOWN"):
                degraded_monitors.append(item)

        if group_monitors:
            groups_summary.append({
                "id": group.get("id"),
                "name": group_name,
                "monitors": group_monitors,
            })

    total_monitors = len(monitors)
    if total_monitors == 0:
        return build_unknown_summary("Belum ada monitor publik yang terdaftar pada halaman status.")

    if down_count > 0:
        overall_status = "DEGRADED" if up_count > 0 else "DOWN"
        overall_label = f"{down_count} Layanan Gangguan"
    elif maintenance_count > 0:
        overall_status = "MAINTENANCE"
        overall_label = "Pemeliharaan Terjadwal"
    elif up_count == total_monitors:
        overall_status = "HEALTHY"
        overall_label = "Semua Layanan Sehat"
    else:
        overall_status = "UNKNOWN"
        overall_label = "Sebagian Status Tidak Diketahui"

    incident_raw = page_data.get("incident")
    incident = None
    if isinstance(incident_raw, dict) and incident_raw.get("title"):
        incident = {
            "title": str(incident_raw.get("title")),
            "content": str(incident_raw.get("content") or ""),
            "style": str(incident_raw.get("style") or "warning"),
        }

    return {
        "available": True,
        "overall_status": overall_status,
        "overall_label": overall_label,
        "title": str(config.get("title") or "DigitalNeeds System Status"),
        "slug": str(config.get("slug") or settings.kuma_status_slug),
        "public_url": settings.kuma_public_url,
        "admin_url": f"{settings.kuma_public_url.rstrip('/')}/dashboard",
        "total_monitors": total_monitors,
        "up_monitors": up_count,
        "down_monitors": down_count,
        "maintenance_monitors": maintenance_count,
        "unknown_monitors": unknown_count,
        "degraded_monitors": degraded_monitors,
        "groups": groups_summary,
        "monitors": monitors,
        "incident": incident,
        "last_updated": latest_time or datetime.now(timezone.utc).isoformat(),
        "error": None,
    }


async def _fetch_from_base_url(client: httpx.AsyncClient, base_url: str, slug: str) -> Optional[Dict[str, Any]]:
    base = base_url.rstrip("/")
    page_resp = await client.get(f"{base}/api/status-page/{slug}")
    if page_resp.status_code != 200:
        return None
    hb_resp = await client.get(f"{base}/api/status-page/heartbeat/{slug}")
    if hb_resp.status_code != 200:
        return None
    return parse_kuma_status_payload(page_resp.json(), hb_resp.json())


async def get_kuma_status_summary(force_refresh: bool = False) -> Dict[str, Any]:
    """Fetch Uptime Kuma public status summary with short timeout, caching, and safe fallback."""
    now = time.monotonic()
    cached_data = _cache.get("data")
    if (
        not force_refresh
        and cached_data is not None
        and (now - _cache.get("timestamp", 0.0)) < settings.kuma_cache_ttl_seconds
    ):
        return cached_data

    slug = settings.kuma_status_slug
    candidate_urls = [settings.kuma_base_url]
    if settings.kuma_fallback_url and settings.kuma_fallback_url not in candidate_urls:
        candidate_urls.append(settings.kuma_fallback_url)

    last_error = "Tidak dapat menghubungi Uptime Kuma."
    async with httpx.AsyncClient(timeout=settings.kuma_timeout_seconds, follow_redirects=True) as client:
        for base_url in candidate_urls:
            try:
                summary = await _fetch_from_base_url(client, base_url, slug)
                if summary is not None:
                    _cache["data"] = summary
                    _cache["timestamp"] = now
                    return summary
                last_error = f"Endpoint status page {slug} mengembalikan kode non-200."
            except httpx.TimeoutException:
                last_error = "Waktu tunggu koneksi ke Uptime Kuma habis (timeout)."
            except Exception as exc:
                last_error = f"Gagal memuat status Uptime Kuma: {type(exc).__name__}"

    fallback = build_unknown_summary(last_error)
    _cache["data"] = fallback
    _cache["timestamp"] = now
    return fallback
