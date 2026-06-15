"""Settings centrale — citite din environment (.env în dev, secrets pe VPS).

Sursa unică de configurare. Orice variabilă nouă din cod se adaugă AICI și în
`.env.example` (regula din T007). Nimic hardcodat, nimic citit din os.environ
direct prin cod — totul prin `settings`.
"""

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # .env are și variabile pt seed-ul node (SUPABASE_URL etc.)
    )

    # --- Postgres / Supabase ---
    # admin_pool (control plane + joburi): rol privilegiat. Acceptă și numele
    # vechi DATABASE_URL ca alias, pt compat cu .env existente.
    supabase_db_url: str = Field(validation_alias=AliasChoices("SUPABASE_DB_URL", "DATABASE_URL"))
    # bot_pool (tenant path, NX-50): conexiune DIRECTĂ (port 5432) cu rol de LOGIN
    # `bot_runtime` (parolă proprie, fără bypassrls). Gol în dev înainte de
    # provisioning → bot_pool cade grațios pe supabase_db_url + SET ROLE.
    database_url_bot: str = Field(default="", validation_alias="DATABASE_URL_BOT")
    # Plasa NX-04: assert rol + app.business_id la fiecare checkout din bot_pool.
    # 'strict' (default) → IsolationError înainte de primul query; 'off' → sare
    # verificarea (cu WARNING la boot), pt măsurare/oprire la scară.
    db_isolation_assert: str = Field(default="strict", validation_alias="DB_ISOLATION_ASSERT")

    # --- OpenAI ---
    openai_api_key: str = Field(default="", validation_alias="OPENAI_API_KEY")
    model_agent: str = Field(default="gpt-5.4-mini", validation_alias="MODEL_AGENT")
    model_triage: str = Field(default="gpt-5.4-nano", validation_alias="MODEL_TRIAGE")
    model_embed: str = Field(default="text-embedding-3-small", validation_alias="MODEL_EMBED")

    # --- Meta WhatsApp Cloud API ---
    meta_access_token: str = Field(default="", validation_alias="META_ACCESS_TOKEN")
    meta_app_secret: str = Field(default="", validation_alias="META_APP_SECRET")
    meta_verify_token: str = Field(default="", validation_alias="META_VERIFY_TOKEN")
    meta_phone_number_id: str = Field(default="", validation_alias="META_PHONE_NUMBER_ID")

    # --- Redis ---
    redis_url: str = Field(default="redis://redis:6379/0", validation_alias="REDIS_URL")

    # --- Telegram (canal de TEST — long polling) ---
    telegram_bot_token: str = Field(default="", validation_alias="TELEGRAM_BOT_TOKEN")

    # --- App ---
    env: str = Field(default="dev", validation_alias="ENV")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    daily_cost_cap_usd: float = Field(default=5.0, validation_alias="DAILY_COST_CAP_USD")
    operator_alert_webhook: str = Field(default="", validation_alias="OPERATOR_ALERT_WEBHOOK")
    # Gates (G5a): cât timp tace botul după un handoff (risc / preluare de om).
    # Agentul (consola, ulterior) poate prelungi/curăța fereastra.
    handoff_window_minutes: int = Field(default=45, validation_alias="HANDOFF_WINDOW_MINUTES")

    # --- Cache semantic (G5b) ---
    cache_enabled: bool = Field(default=True, validation_alias="CACHE_ENABLED")
    # τ_high: prag de auto-accept pentru L2 semantic (cosine similarity). Conservator
    # (precizie peste recall); calibrat cu instrumentarea înainte de a coborî.
    cache_tau_high: float = Field(default=0.92, validation_alias="CACHE_TAU_HIGH")
    cache_ttl_static_days: int = Field(default=7, validation_alias="CACHE_TTL_STATIC_DAYS")
    # TTL dynamic (recomandări de produs, G5b-2): backstop SCURT — invalidarea reală e
    # price-check + data_version la lookup, nu expirarea. Default 30 min.
    cache_ttl_dynamic_minutes: int = Field(default=30, validation_alias="CACHE_TTL_DYNAMIC_MINUTES")

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


@lru_cache
def get_settings() -> Settings:
    """Singleton — citit o singură dată per proces."""
    return Settings()
