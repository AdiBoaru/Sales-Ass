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
    model_moderation: str = Field(
        default="omni-moderation-latest", validation_alias="MODEL_MODERATION"
    )

    # --- Moderation gate (NX-15) ---
    # Poartă în Gates înaintea triajului: mesaj flagged → răspuns neutru (gratuit la OpenAI).
    moderation_enabled: bool = Field(default=True, validation_alias="MODERATION_ENABLED")
    # Câte flag-uri într-o fereastră de 24h trec contactul pe abuse blocklist.
    moderation_block_threshold: int = Field(
        default=3, validation_alias="MODERATION_BLOCK_THRESHOLD"
    )

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

    # --- Mesaj de întâmpinare (free layer, stagiul 4) ---
    # Un pur salut → mesaj de welcome branded, determinist (fără LLM). Numele botului și
    # sugestiile pot fi override-uite per business din businesses.settings["welcome"].
    welcome_enabled: bool = Field(default=True, validation_alias="WELCOME_ENABLED")
    welcome_bot_name: str = Field(default="Native", validation_alias="WELCOME_BOT_NAME")

    # --- Cache semantic (G5b) ---
    cache_enabled: bool = Field(default=True, validation_alias="CACHE_ENABLED")
    # τ_high: prag de auto-accept pentru L2 semantic (cosine similarity). Conservator
    # (precizie peste recall); calibrat cu instrumentarea înainte de a coborî.
    cache_tau_high: float = Field(default=0.92, validation_alias="CACHE_TAU_HIGH")
    cache_ttl_static_days: int = Field(default=7, validation_alias="CACHE_TTL_STATIC_DAYS")
    # TTL dynamic (recomandări de produs, G5b-2): backstop SCURT — invalidarea reală e
    # price-check + data_version la lookup, nu expirarea. Default 30 min.
    cache_ttl_dynamic_minutes: int = Field(default=30, validation_alias="CACHE_TTL_DYNAMIC_MINUTES")

    # --- Strat gratuit FAQ (NX-74, stagiul 4) ---
    # Întrebări de cunoștințe (retur/livrare/garanție/plată) → răspuns din `faqs` ÎNAINTE de
    # triaj/agent (early-exit fără LLM de generare). Lookup ÎNTOTDEAUNA business_id + locale +
    # cosine. Doar `embed()`, niciodată generare (principiul 2). Kill-switch global.
    faq_enabled: bool = Field(default=True, validation_alias="FAQ_ENABLED")
    # τ_high strat gratuit: prag de auto-accept (cosine). FAQ-ul e curat (editat de client) →
    # poate fi puțin mai relaxat decât cache_tau_high, dar precision-first. Default 0.82.
    faq_tau_high: float = Field(default=0.82, validation_alias="FAQ_TAU_HIGH")
    # τ tool: agentul parafrazează oricum răspunsul → un match aproximativ e util. Default 0.75.
    faq_tau_tool: float = Field(default=0.75, validation_alias="FAQ_TAU_TOOL")

    # --- Cost guard + rate limit (G2c, stagiul 2) ---
    # Cost guard: peste plafonul zilnic (businesses.daily_cost_cap_usd or daily_cost_cap_usd)
    # dezactivează LLM-ul pt restul zilei. Estimare-plasă; facturarea reală = usage_daily.
    cost_guard_enabled: bool = Field(default=True, validation_alias="COST_GUARD_ENABLED")
    cost_triage_usd: float = Field(default=0.0003, validation_alias="COST_TRIAGE_USD")
    cost_agent_usd: float = Field(default=0.003, validation_alias="COST_AGENT_USD")
    # Rate limit per contact: max mesaje într-o fereastră (peste debounce R1).
    rate_limit_enabled: bool = Field(default=True, validation_alias="RATE_LIMIT_ENABLED")
    rate_limit_max: int = Field(default=20, validation_alias="RATE_LIMIT_MAX")
    rate_limit_window_seconds: int = Field(default=60, validation_alias="RATE_LIMIT_WINDOW_SECONDS")

    # --- Comerț / bucla de bani (F2) ---
    # Base URL de checkout (fallback global; businesses.settings["checkout_url"] are prioritate).
    # Gol → checkout_link întoarce ok=False (nu inventăm domeniu). `?ref=<turn_id>` adăugat în cod.
    checkout_base_url: str = Field(default="", validation_alias="CHECKOUT_BASE_URL")
    # Cât timp e valabil un link de checkout (zile) → checkout_links.expires_at.
    checkout_link_ttl_days: int = Field(default=7, validation_alias="CHECKOUT_LINK_TTL_DAYS")
    # Secret HMAC pt webhookul de comenzi (F2-2): semnătura X-Orders-Signature peste corpul
    # brut (NX-94). Gol → endpoint 403 (fail-closed).
    orders_webhook_secret: str = Field(default="", validation_alias="ORDERS_WEBHOOK_SECRET")

    # --- Summarizer conversații lungi (G6-2 felia 2, stagiul 6) ---
    # Generare POST-TUR async (nano), citire deterministă în context builder. Kill-switch global.
    summary_enabled: bool = Field(default=True, validation_alias="SUMMARY_ENABLED")
    # Prag de declanșare: nr. total de mesaje pe conversație de la care se sumarizează.
    # CLAUDE.md zice „>20 mesaje"; interpretarea practică = la >= prag (default 20).
    summary_threshold: int = Field(default=20, validation_alias="SUMMARY_THRESHOLD")
    # Anti-regenerare: re-sumarizăm doar când s-au acumulat >= atâtea mesaje noi peste watermark
    # (nu la fiecare tur). Acoperirea rămâne corectă: feed-ul ia tot de la watermark.
    summary_regen_delta: int = Field(default=12, validation_alias="SUMMARY_REGEN_DELTA")
    # Buget de caractere al blocului de rezumat injectat în prompt (P4).
    summary_max_chars: int = Field(default=600, validation_alias="SUMMARY_MAX_CHARS")

    # --- Mini-scheduler joburi de mentenanță (NX-83) ---
    # Orchestrează funcțiile run() existente la intervale fixe (rollup nocturn,
    # purjă dedupe, embed incremental). Embed gated suplimentar pe prezența cheii OpenAI.
    embed_job_enabled: bool = Field(default=True, validation_alias="EMBED_JOB_ENABLED")
    scheduler_rollup_hour_utc: int = Field(default=0, validation_alias="SCHEDULER_ROLLUP_HOUR_UTC")
    scheduler_dedupe_interval_seconds: int = Field(
        default=21600, validation_alias="SCHEDULER_DEDUPE_INTERVAL_SECONDS"
    )
    scheduler_embed_interval_seconds: int = Field(
        default=3600, validation_alias="SCHEDULER_EMBED_INTERVAL_SECONDS"
    )

    # --- Motor proactiv (NX-70, scheduler separat peste proactive_jobs) ---
    # Producătorul pentru outbox: AWB / back-in-stock / coș abandonat / follow-up.
    # Gating-ul (consent / fereastră 24h / template) e poarta NX-71. v1 = doar type=text.
    proactive_enabled: bool = Field(default=True, validation_alias="PROACTIVE_ENABLED")
    proactive_batch_size: int = Field(default=20, validation_alias="PROACTIVE_BATCH_SIZE")
    proactive_idle_sleep_s: float = Field(default=5.0, validation_alias="PROACTIVE_IDLE_SLEEP_S")

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


@lru_cache
def get_settings() -> Settings:
    """Singleton — citit o singură dată per proces."""
    return Settings()
