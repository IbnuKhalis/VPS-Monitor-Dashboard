import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "VPS Mission Control"
    app_version: str = "1.1.0"
    environment: str = os.getenv("ENVIRONMENT", "development").lower()
    dashboard_pin: str = os.getenv("DASHBOARD_PIN", "123456")
    secret_key: str = os.getenv("SECRET_KEY", "super-secret-vps-monitoring-key-change-in-prod-78234")
    session_cookie_name: str = "vps_mission_session"
    session_max_age_seconds: int = 60 * 60 * 24 * 7  # 7 days
    session_cookie_secure: bool = os.getenv("SESSION_COOKIE_SECURE", "true").lower() in ("true", "1")
    backup_dir: str = os.getenv("BACKUP_DIR", "/opt/backups")
    refresh_interval_seconds: int = 4

    # Trusted Reverse Proxies (Codex R2)
    # Comma-separated list of trusted IP/CIDR subnets (Caddy, Docker bridge, localhost)
    trusted_proxies: str = os.getenv("TRUSTED_PROXIES", "127.0.0.1,::1,172.16.0.0/12,10.0.0.0/8,192.168.0.0/16")

    # Uptime Kuma status page integration settings
    kuma_base_url: str = os.getenv("KUMA_BASE_URL", "http://uptime-kuma:3001")
    kuma_fallback_url: str = os.getenv("KUMA_FALLBACK_URL", "https://status.digitalneeds.my.id")
    kuma_public_url: str = os.getenv("KUMA_PUBLIC_URL", "https://status.digitalneeds.my.id")
    kuma_status_slug: str = os.getenv("KUMA_STATUS_SLUG", "default")
    kuma_cache_ttl_seconds: int = int(os.getenv("KUMA_CACHE_TTL_SECONDS", "45"))
    kuma_timeout_seconds: float = float(os.getenv("KUMA_TIMEOUT_SECONDS", "3.0"))

    # Operations Executor settings (Phase 5 / R1 zero-socket)
    ops_executor_url: str = os.getenv("OPS_EXECUTOR_URL", "http://ops-executor:9092")
    ops_executor_token: str = os.getenv("OPS_EXECUTOR_TOKEN", "vps-ops-executor-internal-secret-token-prod")
    ops_rate_limit_seconds: int = int(os.getenv("OPS_RATE_LIMIT_SECONDS", "30"))
    ops_audit_file: str = os.getenv("OPS_AUDIT_FILE", "data/operations_audit.json")

    # PIN Rate Limit settings (Codex audit fix & R2)
    pin_rate_limit_max_attempts: int = int(os.getenv("PIN_RATE_LIMIT_MAX_ATTEMPTS", "5"))
    pin_rate_limit_lockout_seconds: int = int(os.getenv("PIN_RATE_LIMIT_LOCKOUT_SECONDS", "60"))
    pin_rate_limit_max_tracked_ips: int = int(os.getenv("PIN_RATE_LIMIT_MAX_TRACKED_IPS", "2000"))

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()


def validate_production_secrets():
    """Ensure production deployment rejects default/placeholder credentials (Codex R5)."""
    if settings.environment in ("production", "prod"):
        if settings.dashboard_pin in ("123456", ""):
            raise RuntimeError("CRITICAL: DASHBOARD_PIN cannot be default '123456' or empty in production mode.")
        if "change-in-prod" in settings.secret_key or settings.secret_key == "":
            raise RuntimeError("CRITICAL: SECRET_KEY cannot be default placeholder or empty in production mode.")
        if "secret-token-prod" == settings.ops_executor_token or settings.ops_executor_token == "":
            raise RuntimeError("CRITICAL: OPS_EXECUTOR_TOKEN must be explicitly set and secure in production mode.")
