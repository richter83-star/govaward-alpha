"""Configuration loaded from environment / .env file."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources import EnvSettingsSource, PydanticBaseSettingsSource


class _NonEmptyEnvSettingsSource(EnvSettingsSource):
    """EnvSettingsSource variant that ignores empty-string values.

    Some shells export e.g. ``ANTHROPIC_API_KEY=""`` for tooling reasons,
    which under default pydantic-settings precedence (env > .env) silently
    shadows a real value present in ``.env``. Filtering empty strings here
    preserves the normal "non-empty shell env overrides .env" behaviour
    while making accidental empties harmless.
    """

    def __call__(self) -> dict[str, Any]:
        return {k: v for k, v in super().__call__().items() if v not in ("", None)}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Replace the default env source with one that ignores empty strings."""
        return (
            init_settings,
            _NonEmptyEnvSettingsSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )

    # SAM.gov
    sam_api_key: str = Field(default="", alias="SAM_API_KEY")

    # Google Gemini AI
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-3.1-flash-lite", alias="GEMINI_MODEL")

    # Anthropic / Claude (legacy aliases for backward compatibility)
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    claude_model: str = Field(default="gemini-3.1-flash-lite", alias="CLAUDE_MODEL")

    # SEC — the EDGAR fair-access policy asks automated clients to identify
    # themselves with a real contact address. Set SEC_CONTACT_EMAIL in .env;
    # requests still work without it but SEC may throttle or block them.
    sec_contact_email: str = Field(default="", alias="SEC_CONTACT_EMAIL")

    # Core thresholds
    award_min_amount: float = Field(default=50_000_000, alias="AWARD_MIN_AMOUNT")
    lookback_days: int = Field(default=3, alias="LOOKBACK_DAYS")

    # Market cap filter
    market_cap_min: float = Field(default=50_000_000, alias="MARKET_CAP_MIN")
    market_cap_max: float = Field(default=10_000_000_000, alias="MARKET_CAP_MAX")

    # Alert threshold
    alert_min_score: int = Field(default=70, alias="ALERT_MIN_SCORE")

    # Storage
    db_path: str = Field(default="govaward.db", alias="DB_PATH")
    cache_dir: str = Field(default=".cache", alias="CACHE_DIR")

    # Email
    email_enabled: bool = Field(default=False, alias="EMAIL_ENABLED")
    smtp_host: str = Field(default="", alias="SMTP_HOST")
    smtp_port: int = Field(default=587, alias="SMTP_PORT")
    smtp_user: str = Field(default="", alias="SMTP_USER")
    smtp_password: str = Field(default="", alias="SMTP_PASSWORD")
    email_from: str = Field(default="", alias="EMAIL_FROM")
    email_to: str = Field(default="", alias="EMAIL_TO")

    # Telegram
    telegram_enabled: bool = Field(default=False, alias="TELEGRAM_ENABLED")
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")

    # Discord
    discord_enabled: bool = Field(default=False, alias="DISCORD_ENABLED")
    discord_webhook_url: str = Field(default="", alias="DISCORD_WEBHOOK_URL")

    # Alpaca Brokerage API
    alpaca_api_key: str = Field(default="", alias="ALPACA_API_KEY")
    alpaca_secret_key: str = Field(default="", alias="ALPACA_SECRET_KEY")
    alpaca_endpoint: str = Field(default="https://api.alpaca.markets", alias="ALPACA_ENDPOINT")
    alpaca_live: bool = Field(default=False, alias="ALPACA_LIVE")

    # Sources switched off on purpose, comma-separated (e.g. "sam_gov,usaspending").
    # A parked source is skipped by `scan` and shown as PARKED, not as broken.
    parked_sources: str = Field(default="", alias="GOVAWARD_PARKED_SOURCES")

    @property
    def parked(self) -> set[str]:
        return {s.strip() for s in self.parked_sources.split(",") if s.strip()}

    @property
    def cache_path(self) -> Path:
        p = Path(self.cache_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def data_dir(self) -> Path:
        """Resolve data/ directory relative to repo root."""
        here = Path(__file__).parent
        candidates = [
            here.parent.parent.parent / "data",   # src/govaward_alpha -> repo root
            Path("data"),
        ]
        for c in candidates:
            if c.exists():
                return c
        return Path("data")


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
