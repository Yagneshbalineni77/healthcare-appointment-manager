"""Central configuration.

Every knob is an environment variable so the same image runs locally (SQLite,
console email, stubbed calendar) and in production (Postgres, SendGrid, real
Google Calendar) with nothing but ``.env`` changing.

Design note: integrations are *optional by construction*. ``Settings`` exposes
``<integration>_enabled`` booleans, and every service consults them before
attempting a network call. A missing credential therefore degrades the feature
instead of breaking the request path.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Load .env from the project root; real environment variables always win so that
# platform-injected config (Render/Railway) is never shadowed by a stray file.
load_dotenv(BASE_DIR / ".env", override=False)


def _env(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key) or default)
    except ValueError:
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env(key).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_list(key: str) -> list[str]:
    return [part.strip() for part in _env(key).split(",") if part.strip()]


#: Development-only signing key. 64 hex chars so HMAC-SHA256 is used at full
#: strength even before the operator sets their own. :func:`assert_production_ready`
#: refuses to boot with this value when ENVIRONMENT=production.
DEV_JWT_SECRET = "dev-only-insecure-key-" + "0f2c" * 12


@dataclass(frozen=True)
class Settings:
    # ---- app -----------------------------------------------------------
    app_name: str = field(default_factory=lambda: _env("APP_NAME", "Clinix — Healthcare Appointment & Follow-up Manager"))
    clinic_name: str = field(default_factory=lambda: _env("CLINIC_NAME", "Clinix Care Clinic"))
    environment: str = field(default_factory=lambda: _env("ENVIRONMENT", "development"))
    debug: bool = field(default_factory=lambda: _env_bool("DEBUG", True))
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"))
    cors_origins: list[str] = field(default_factory=lambda: _env_list("CORS_ORIGINS") or ["*"])

    # ---- database ------------------------------------------------------
    database_url: str = field(
        default_factory=lambda: _env("DATABASE_URL", f"sqlite:///{BASE_DIR / 'clinix.db'}")
    )

    # ---- auth ----------------------------------------------------------
    jwt_secret: str = field(default_factory=lambda: _env("JWT_SECRET", DEV_JWT_SECRET))
    jwt_algorithm: str = field(default_factory=lambda: _env("JWT_ALGORITHM", "HS256"))
    access_token_ttl_minutes: int = field(default_factory=lambda: _env_int("ACCESS_TOKEN_TTL_MINUTES", 720))

    # ---- clinic rules --------------------------------------------------
    clinic_timezone: str = field(default_factory=lambda: _env("CLINIC_TIMEZONE", "Asia/Kolkata"))
    slot_hold_minutes: int = field(default_factory=lambda: _env_int("SLOT_HOLD_MINUTES", 5))
    booking_horizon_days: int = field(default_factory=lambda: _env_int("BOOKING_HORIZON_DAYS", 45))
    min_lead_minutes: int = field(default_factory=lambda: _env_int("MIN_LEAD_MINUTES", 30))
    reminder_lead_hours: int = field(default_factory=lambda: _env_int("REMINDER_LEAD_HOURS", 24))

    # ---- LLM (Google Gemini) -------------------------------------------
    gemini_api_key: str = field(default_factory=lambda: _env("GEMINI_API_KEY"))
    gemini_model: str = field(default_factory=lambda: _env("GEMINI_MODEL", "gemini-2.5-flash"))
    llm_timeout_seconds: float = field(default_factory=lambda: float(_env_int("LLM_TIMEOUT_SECONDS", 25)))
    llm_max_attempts: int = field(default_factory=lambda: _env_int("LLM_MAX_ATTEMPTS", 3))
    llm_breaker_threshold: int = field(default_factory=lambda: _env_int("LLM_BREAKER_THRESHOLD", 4))
    llm_breaker_cooldown_seconds: int = field(default_factory=lambda: _env_int("LLM_BREAKER_COOLDOWN_SECONDS", 120))

    # ---- email ---------------------------------------------------------
    email_provider: str = field(default_factory=lambda: _env("EMAIL_PROVIDER", "auto").lower())
    email_from: str = field(default_factory=lambda: _env("EMAIL_FROM", "no-reply@clinix.health"))
    email_from_name: str = field(default_factory=lambda: _env("EMAIL_FROM_NAME", "Clinix Care Clinic"))
    sendgrid_api_key: str = field(default_factory=lambda: _env("SENDGRID_API_KEY"))
    smtp_host: str = field(default_factory=lambda: _env("SMTP_HOST"))
    smtp_port: int = field(default_factory=lambda: _env_int("SMTP_PORT", 587))
    smtp_user: str = field(default_factory=lambda: _env("SMTP_USER"))
    smtp_password: str = field(default_factory=lambda: _env("SMTP_PASSWORD"))
    smtp_use_tls: bool = field(default_factory=lambda: _env_bool("SMTP_USE_TLS", True))
    outbox_dir: str = field(default_factory=lambda: _env("OUTBOX_DIR", str(BASE_DIR / "var" / "outbox")))

    # ---- google calendar ------------------------------------------------
    google_client_id: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_ID"))
    google_client_secret: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_SECRET"))
    google_redirect_uri: str = field(default_factory=lambda: _env("GOOGLE_REDIRECT_URI"))

    # ---- background worker ----------------------------------------------
    worker_enabled: bool = field(default_factory=lambda: _env_bool("WORKER_ENABLED", True))
    worker_interval_seconds: int = field(default_factory=lambda: _env_int("WORKER_INTERVAL_SECONDS", 20))
    notification_max_attempts: int = field(default_factory=lambda: _env_int("NOTIFICATION_MAX_ATTEMPTS", 5))

    # ---- bootstrap ------------------------------------------------------
    seed_demo_data: bool = field(default_factory=lambda: _env_bool("SEED_DEMO_DATA", True))
    admin_email: str = field(default_factory=lambda: _env("ADMIN_EMAIL", "admin@clinix.health"))
    admin_password: str = field(default_factory=lambda: _env("ADMIN_PASSWORD", "Admin@12345"))

    # ---- derived --------------------------------------------------------
    @property
    def tz(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.clinic_timezone)
        except Exception:  # pragma: no cover - bad tz string in env
            return ZoneInfo("UTC")

    @property
    def llm_enabled(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def google_calendar_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret and self.google_redirect_uri)

    @property
    def resolved_email_provider(self) -> str:
        """`auto` picks the best configured transport, else falls back to outbox."""
        if self.email_provider in {"sendgrid", "smtp", "outbox", "console"}:
            return self.email_provider
        if self.sendgrid_api_key:
            return "sendgrid"
        if self.smtp_host and self.smtp_user:
            return "smtp"
        return "outbox"

    @property
    def email_live(self) -> bool:
        return self.resolved_email_provider in {"sendgrid", "smtp"}

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def using_dev_secret(self) -> bool:
        return self.jwt_secret == DEV_JWT_SECRET

    def assert_production_ready(self) -> list[str]:
        """Fail fast on unsafe production config; warn about the rest.

        Returns the list of non-fatal warnings so the caller can log them.
        """
        fatal: list[str] = []
        warnings: list[str] = []

        if self.environment.lower() == "production":
            if self.using_dev_secret:
                fatal.append("JWT_SECRET is still the built-in development key. Generate one: python -c \"import secrets;print(secrets.token_hex(32))\"")
            if self.debug:
                warnings.append("DEBUG=true in production — tracebacks may leak into responses.")
            if self.is_sqlite:
                warnings.append("Running on SQLite in production. Set DATABASE_URL to a Postgres URL for multi-instance deployments.")
            if not self.email_live:
                warnings.append("No live email transport configured — notifications will only be written to the on-disk outbox.")
            if not self.google_calendar_enabled:
                warnings.append("Google Calendar is not configured — calendar tasks will be skipped.")
            if not self.llm_enabled:
                warnings.append("GEMINI_API_KEY is not set — every AI summary will use the rule-based fallback.")
            if "*" in self.cors_origins:
                warnings.append("CORS_ORIGINS is '*' — set it to your real frontend origin.")

        if fatal:
            raise RuntimeError("Unsafe production configuration:\n  - " + "\n  - ".join(fatal))
        return warnings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
