from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore")

    app_name: str = "MCP Client"
    support_email: str = "support@example.com"
    public_base_url: str = "http://localhost:8000"
    app_store_url: str | None = None

    redis_url: str = "redis://localhost:6379/0"
    redis_key_prefix: str = "mc"
    redis_max_connections: int = Field(default=20, ge=1)

    apns_key_p8: str | None = None
    apns_key_path: Path | None = None
    apns_key_id: str | None = None
    apns_team_id: str | None = None
    apns_bundle_id: str | None = None
    apns_watch_bundle_id: str | None = None

    rate_limit_per_minute: int = Field(default=600, ge=0)
    rate_limit_accounts_per_hour: int = Field(default=20, ge=0)
    mcp_allowed_origins: str = ""

    # Render sets RENDER=true in every service. Proxy headers are trusted only then.
    render: bool = False

    host: str = "0.0.0.0"
    port: int = 8000
    web_concurrency: int = Field(default=1, ge=1)
    log_level: str = "INFO"

    @property
    def base_url(self) -> str:
        return self.public_base_url.rstrip("/")

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}/mcp"

    @property
    def allowed_origins(self) -> frozenset[str]:
        parts = urlsplit(self.base_url)
        origins = {f"{parts.scheme}://{parts.netloc}"}
        origins.update(o.strip().rstrip("/") for o in self.mcp_allowed_origins.split(",") if o.strip())
        return frozenset(origins)

    def apns_private_key(self) -> str | None:
        if self.apns_key_p8:
            return self.apns_key_p8.replace("\\n", "\n")
        if self.apns_key_path:
            return self.apns_key_path.read_text()
        return None

    @property
    def push_configured(self) -> bool:
        has_key = bool(self.apns_key_p8 or self.apns_key_path)
        return has_key and bool(self.apns_key_id and self.apns_team_id and self.apns_bundle_id)


@lru_cache
def get_settings() -> Settings:
    return Settings()
