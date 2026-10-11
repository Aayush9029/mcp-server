from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BeforeValidator, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from mcp_client_server import totp

LogLevel = Literal["CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"]


def _upper(value: object) -> object:
    return value.upper() if isinstance(value, str) else value


@dataclass(frozen=True, slots=True)
class ApnsConfig:
    private_key: str
    key_id: str
    team_id: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_ignore_empty=True, extra="ignore", frozen=True)

    app_name: str = "MCP Client"
    support_email: str = "support@example.com"
    public_base_url: str = "http://localhost:8000"
    app_store_url: str | None = None

    redis_url: str = Field(default="redis://localhost:6379/0", repr=False)
    redis_key_prefix: str = "mc"
    redis_max_connections: int = Field(default=20, ge=1)

    apns_key_p8: str | None = Field(default=None, repr=False)
    apns_key_path: Path | None = None
    apns_key_id: str | None = None
    apns_team_id: str | None = None
    apns_bundle_id: str | None = None
    apns_watch_bundle_id: str | None = None

    # App integrity. Each check is off if its setting is empty.
    app_attest_app_ids: str = ""
    app_attest_environment: Literal["production", "development"] = "production"
    app_totp_secret: str | None = Field(default=None, repr=False)

    rate_limit_per_minute: int = Field(default=600, ge=0)
    rate_limit_accounts_per_hour: int = Field(default=20, ge=0)
    mcp_allowed_origins: str = ""

    # Render sets RENDER=true in every service. Proxy headers are trusted only then.
    render: bool = False

    host: str = "0.0.0.0"  # noqa: S104  The container must listen on all interfaces.
    port: int = Field(default=8000, ge=1, le=65535)
    web_concurrency: int = Field(default=1, ge=1)
    log_level: Annotated[LogLevel, BeforeValidator(_upper)] = "INFO"

    @field_validator("app_totp_secret")
    @classmethod
    def _secret_is_base32(cls, value: str | None) -> str | None:
        if value is not None:
            totp.decode_secret(value)
        return value

    @field_validator("app_attest_app_ids")
    @classmethod
    def _app_ids_have_a_team_prefix(cls, value: str) -> str:
        for app_id in value.split(","):
            if app_id.strip() and "." not in app_id.strip(". "):
                raise ValueError("Each app ID is TEAMID.bundle.identifier.")
        return value

    @property
    def attest_app_ids(self) -> tuple[str, ...]:
        return tuple(app_id.strip() for app_id in self.app_attest_app_ids.split(",") if app_id.strip())

    @property
    def totp_key(self) -> bytes | None:
        return totp.decode_secret(self.app_totp_secret) if self.app_totp_secret else None

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

    def apns(self) -> ApnsConfig | None:
        """The APNs credentials, or None if one of them is missing. Push is off then."""
        if not (self.apns_key_id and self.apns_team_id and self.apns_bundle_id):
            return None
        if self.apns_key_p8:
            key = self.apns_key_p8.replace("\\n", "\n")
        elif self.apns_key_path:
            key = self.apns_key_path.read_text()
        else:
            return None
        return ApnsConfig(private_key=key, key_id=self.apns_key_id, team_id=self.apns_team_id)


@lru_cache
def get_settings() -> Settings:
    return Settings()
