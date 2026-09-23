import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "VPS Mission Control"
    app_version: str = "1.1.0"
    dashboard_pin: str = os.getenv("DASHBOARD_PIN", "123456")
    secret_key: str = os.getenv("SECRET_KEY", "super-secret-vps-monitoring-key-change-in-prod-78234")
    session_cookie_name: str = "vps_mission_session"
    session_max_age_seconds: int = 60 * 60 * 24 * 7  # 7 days
    backup_dir: str = os.getenv("BACKUP_DIR", "/opt/backups")
    docker_socket: str = os.getenv("DOCKER_SOCKET", "unix://var/run/docker.sock")
    refresh_interval_seconds: int = 4

    # Uptime Kuma status page integration settings
    kuma_base_url: str = os.getenv("KUMA_BASE_URL", "http://uptime-kuma:3001")
    kuma_fallback_url: str = os.getenv("KUMA_FALLBACK_URL", "https://status.digitalneeds.my.id")
    kuma_public_url: str = os.getenv("KUMA_PUBLIC_URL", "https://status.digitalneeds.my.id")
    kuma_status_slug: str = os.getenv("KUMA_STATUS_SLUG", "default")
    kuma_cache_ttl_seconds: int = int(os.getenv("KUMA_CACHE_TTL_SECONDS", "45"))
    kuma_timeout_seconds: float = float(os.getenv("KUMA_TIMEOUT_SECONDS", "3.0"))

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
