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

    # Trusted Reverse Proxies (Codex R2 & S2)
    # Strictly scoped to loopback and local Docker bridge networks (Caddy container)
    # Wide private subnets (10.0.0.0/8, 192.168.0.0/16) are strictly excluded to prevent spoofing from rogue peers.
    trusted_proxies: str = os.getenv("TRUSTED_PROXIES", "127.0.0.1,::1,172.16.0.0/12")

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
    """Ensure production deployment rejects default/placeholder credentials (Codex R5 / S1)."""
    if settings.environment in ("production", "prod"):
        # 1. PIN Check
        disallowed_pins = {"123456", "000000", "admin", ""}
        if settings.dashboard_pin in disallowed_pins or len(settings.dashboard_pin) < 6:
            raise RuntimeError("CRITICAL: DASHBOARD_PIN cannot be default '123456', empty, or less than 6 digits in production mode.")

        # 2. Secret Key Check
        disallowed_keys = {
            "",
            "super-secret-vps-monitoring-key-change-in-prod-78234",
            "vps-monitoring-secret-key-prod-random-89234",
            "change-this-in-production",
            "secret",
        }
        if settings.secret_key in disallowed_keys or "change-in-prod" in settings.secret_key.lower() or "secret-key-prod" in settings.secret_key.lower():
            raise RuntimeError("CRITICAL: SECRET_KEY cannot be default fallback or empty in production mode.")

        # 3. Ops Executor Token Check
        disallowed_tokens = {
            "",
            "vps-ops-executor-internal-secret-token-prod",
            "change-me",
            "default-token",
            "secret-token-prod",
        }
        if settings.ops_executor_token in disallowed_tokens or "secret-token-prod" in settings.ops_executor_token.lower():
            raise RuntimeError("CRITICAL: OPS_EXECUTOR_TOKEN cannot be default fallback or empty in production mode.")
