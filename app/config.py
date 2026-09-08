import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_name: str = "VPS Mission Control"
    app_version: str = "1.0.0"
    dashboard_pin: str = os.getenv("DASHBOARD_PIN", "123456")
    secret_key: str = os.getenv("SECRET_KEY", "super-secret-vps-monitoring-key-change-in-prod-78234")
    session_cookie_name: str = "vps_mission_session"
    session_max_age_seconds: int = 60 * 60 * 24 * 7  # 7 days
    backup_dir: str = os.getenv("BACKUP_DIR", "/opt/backups")
    docker_socket: str = os.getenv("DOCKER_SOCKET", "unix://var/run/docker.sock")
    refresh_interval_seconds: int = 4

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
