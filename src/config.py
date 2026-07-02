"""Settings centrale — citite din environment (.env în dev, secrets pe VPS).

Sursa unică de configurare. Orice variabilă nouă din cod se adaugă AICI și în
`.env.example` (regula din T007). Nimic hardcodat, nimic citit din os.environ
direct prin cod — totul prin `settings`.
"""

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# NX-121: cap dur de lungime pe corpul inbound (text/caption/titlu interactiv), aliniat cu
# validarea web (`src/web/app.py` max_length=2000). Constantă structurală (paritate canale), nu
# setare per-tenant. Folosit la margine (webhook/meta.py) + ca plasă în gate (Vision-derived body).
INBOUND_BODY_MAX = 2000


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
    # Override de tarife LLM pentru observabilitatea de cost (NX-103). JSON parțial, merge peste
    # implicitul din src/agent/pricing.py — tunabil în prod fără redeploy. Gol → tarifele din cod.
    # Ex: {"gpt-5.4-mini": {"input": 0.30, "cached_input": 0.03, "output": 2.40}}
    llm_pricing_json: str = Field(default="", validation_alias="LLM_PRICING_JSON")

    # --- Media routing: Vision poză→catalog (NX-76, stagiul 3) ---
    # O poză de produs (content_type=image) e descrisă de Vision (prin adaptorul unic, ca
    # embed/moderate — extracție, NU generare) și descrierea devine text de căutare în
    # ctx.message.body → triaj rutează SALES → agentul cheamă search_products. Imagine→text→search.
    vision_enabled: bool = Field(default=True, validation_alias="VISION_ENABLED")
    # Modelul Vision: agentul (mini) are vedere; nano NU. Default = model_agent.
    model_vision: str = Field(default="gpt-5.4-mini", validation_alias="MODEL_VISION")
    # Cap dur de mărime al pozei descărcate (bytes) — peste = fail-soft (nu trimitem la Vision).
    vision_max_bytes: int = Field(default=5_000_000, validation_alias="VISION_MAX_BYTES")
    # Estimare cost/apel Vision (ca un apel de agent) pt contorul zilnic G2c (plasă, nu facturare).
    cost_vision_usd: float = Field(default=0.003, validation_alias="COST_VISION_USD")

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

    # --- Web Widget (NX-20, E26 — al treilea canal, V1.5) ---
    # Gateway SSE pe app-ul FastAPI: POST /web/messages (→ envelope neutru, ca Telegram) +
    # GET /web/stream (Server-Sent Events). Sesiune anonimă semnată HMAC (token public per tenant
    # + visitor_id); secretul din channels.settings (control plane, cache). Default OFF (V1.5).
    web_enabled: bool = Field(default=False, validation_alias="WEB_ENABLED")
    # TTL cache control-plane pt public_token → (business_id, session_secret). Scurt → revocare/seed
    # rapid; suficient cât să nu lovim DB la fiecare mesaj/heartbeat.
    web_session_secret_ttl_s: float = Field(
        default=60.0, validation_alias="WEB_SESSION_SECRET_TTL_S"
    )
    # Rate limit web (NX-20): public anonim → praguri mai strânse decât WhatsApp, pe DOUĂ chei
    # (IP prinde rotirea de visitor_id; visitor prinde spam-ul unui client legit).
    web_rate_limit_max_visitor: int = Field(
        default=15, validation_alias="WEB_RATE_LIMIT_MAX_VISITOR"
    )
    web_rate_limit_max_ip: int = Field(default=40, validation_alias="WEB_RATE_LIMIT_MAX_IP")
    web_rate_limit_window_s: int = Field(default=60, validation_alias="WEB_RATE_LIMIT_WINDOW_S")
    # SSE: heartbeat (ține proxy-ul deschis) + backlog per vizitator pt reconectare (Last-Event-ID).
    web_sse_heartbeat_s: float = Field(default=15.0, validation_alias="WEB_SSE_HEARTBEAT_S")
    web_backlog_size: int = Field(default=20, validation_alias="WEB_BACKLOG_SIZE")
    web_backlog_ttl_s: int = Field(default=300, validation_alias="WEB_BACKLOG_TTL_S")
    # CORS allowlist pt POST /web/chat (NX-25b — gateway web SINCRON request/response). Browserul
    # shop-ului apelează endpointul cross-origin → preflight-ul (înainte de body, deci fără token)
    # se gate-uiește la nivel de browser pe ACEASTĂ listă. Token public + sig + rate-limit rămân
    # gardele server-side. CSV (`https://shop.ro,http://localhost:5173`); gol → CORS dezactivat
    # (doar same-origin). Binding fin origin↔token per canal (channels.settings) = follow-up NX-25.
    web_cors_origins: str = Field(default="", validation_alias="WEB_CORS_ORIGINS")
    # NX-120 (DoS hardening): cap de body pe ingestie — respinge POST-uri mari ÎNAINTE de a le citi
    # integral (VPS mic, 0-swap → un singur request mare poate OOM-ui procesul). Web e mic
    # (text capat la 2000 char) → 16KB; webhook Meta/orders → 256KB (generos). Plus cap zilnic de
    # cost per-vizitator: un token public furat NU poate goli tot bugetul tenantului.
    web_max_body_bytes: int = Field(default=16384, validation_alias="WEB_MAX_BODY_BYTES")
    webhook_max_body_bytes: int = Field(default=262144, validation_alias="WEBHOOK_MAX_BODY_BYTES")
    web_cost_cap_per_visitor_usd: float = Field(
        default=0.50, validation_alias="WEB_COST_CAP_PER_VISITOR_USD"
    )
    # NX-129 (login passthrough): web-ul devine „identificat" când site-ul gazdă pasează un JWT
    # HS256 semnat cu `identity_secret`-ul per-tenant (din channels.settings). Verificat la marginea
    # web → `sub` = customer_ref. Default OFF (feature opt-in, ca web_enabled). Leeway de ceas pt
    # `exp` (toleranță mică la drift între gazdă și bot).
    web_identity_enabled: bool = Field(default=False, validation_alias="WEB_IDENTITY_ENABLED")
    web_identity_leeway_s: int = Field(default=30, validation_alias="WEB_IDENTITY_LEEWAY_S")

    @property
    def web_cors_origins_list(self) -> list[str]:
        """Origin-urile CORS permise pentru /web/chat (CSV → listă, fără goluri)."""
        return [o.strip() for o in self.web_cors_origins.split(",") if o.strip()]

    # --- App ---
    env: str = Field(default="dev", validation_alias="ENV")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    daily_cost_cap_usd: float = Field(default=5.0, validation_alias="DAILY_COST_CAP_USD")
    operator_alert_webhook: str = Field(default="", validation_alias="OPERATOR_ALERT_WEBHOOK")
    # Gates (G5a): cât timp tace botul după un handoff (risc / preluare de om).
    # Agentul (consola, ulterior) poate prelungi/curăța fereastra.
    handoff_window_minutes: int = Field(default=45, validation_alias="HANDOFF_WINDOW_MINUTES")
    # Handoff per-canal: pe ce canale e PERMIS transferul la om (CSV). Web (`webchat`) e exclus
    # by default — anonim (src/web/session.py) și fără operator conectat (consolă/inbox = task
    # viitor), deci o escaladare ar fi tăcere/fundătură, nu un om real → botul asistă singur.
    # WhatsApp/Telegram (operator real, planificat) → permis. Codul de handoff rămâne intact,
    # doar gardat aici. Reversibil din env fără cod: adaugă `webchat` când web-ul are operator.
    handoff_enabled_channels: str = Field(
        default="whatsapp,telegram", validation_alias="HANDOFF_ENABLED_CHANNELS"
    )

    @property
    def handoff_enabled_channels_set(self) -> frozenset[str]:
        """CSV → set de canale unde handoff-ul la om e permis (vezi `handoff_enabled_channels`)."""
        return frozenset(c.strip() for c in self.handoff_enabled_channels.split(",") if c.strip())

    # --- Mesaj de întâmpinare (free layer, stagiul 4) ---
    # Un pur salut → mesaj de welcome branded, determinist (fără LLM). Numele botului și
    # sugestiile pot fi override-uite per business din businesses.settings["welcome"].
    welcome_enabled: bool = Field(default=True, validation_alias="WELCOME_ENABLED")
    welcome_bot_name: str = Field(default="Native", validation_alias="WELCOME_BOT_NAME")

    # --- Strat gratuit alias (NX-73, stagiul 4) ---
    # Match EXACT al frazei normalizate în `intent_aliases` (status='approved'), ÎNAINTE de cache
    # + triaj → early-exit FĂRĂ niciun apel LLM (nici embed). Stratul cel mai ieftin (index).
    # Valoarea apare după ce shadow mode (NX-93) populează aliasurile (gol pe demo). Kill-switch.
    alias_enabled: bool = Field(default=True, validation_alias="ALIAS_ENABLED")

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
    # poate fi puțin mai relaxat decât cache_tau_high, dar precision-first. NX-124a: cu paritate de
    # normalizare (canonicalize seed↔lookup) similaritățile question↔question cresc → 0.78 (tunat
    # empiric pe setul RO: matchurile corecte ~0.79-1.0, întrebarea greșită cade mult sub).
    faq_tau_high: float = Field(default=0.78, validation_alias="FAQ_TAU_HIGH")
    # τ tool: agentul parafrazează oricum răspunsul (el e filtrul de precizie pe hint) → un match
    # aproximativ e util. NX-124a: 0.66 după paritate + variante de formulare (recall RO bun;
    # agentul decide dacă folosește hint-ul).
    faq_tau_tool: float = Field(default=0.66, validation_alias="FAQ_TAU_TOOL")
    # τ POLICY: prag relaxat DOAR când mesajul conține o întrebare CLARĂ de livrare/plată/retur/
    # garanție (regex în faq_stage). Măsurat live: „aveti livrare in cat timp ajunge" atinge doar
    # ~0.56 cosine față de FAQ-urile de livrare (chiar și pur ~0.62), sub faq_tau_high=0.78 → nu se
    # aprindea NICIODATĂ, iar agentul re-recomanda (bug „copy-paste"). Regexul dă precizia; 0.45
    # lasă întrebarea de livrare să prindă FAQ-ul real. Tunabil din env.
    faq_tau_policy: float = Field(default=0.45, validation_alias="FAQ_TAU_POLICY")
    # NX-138 (R7): pragul relaxat de politică se aplică DOAR dacă FAQ-ul potrivit e el însuși de
    # politică (întrebarea lui match-uiește regexul). Fără asta, pragul jos „salva" un FAQ de
    # CONSULTANȚĂ produs pe un mesaj MIXT (produs + livrare) → deflecta cererea de produs (live).
    # OFF (False) → comportamentul #171 (relaxare pe orice FAQ dacă mesajul e de politică).
    faq_policy_gate_on_faq_kind: bool = Field(
        default=True, validation_alias="FAQ_POLICY_GATE_ON_FAQ_KIND"
    )
    # NX-124a: fallback de locale — user pe o limbă fără cunoștințe seedate, dar `default_locale`
    # le are → servim cunoștința existentă (NU traducem). DEFAULT OFF (opt-in: doar tenanții care
    # servesc o limbă fără FAQ seedat, ex. RO→HU). Prag STRICT (precision-first).
    faq_locale_fallback_enabled: bool = Field(
        default=False, validation_alias="FAQ_LOCALE_FALLBACK_ENABLED"
    )
    faq_fallback_tau: float = Field(default=0.85, validation_alias="FAQ_FALLBACK_TAU")

    # --- Cost guard + rate limit (G2c, stagiul 2) ---
    # Cost guard: peste plafonul zilnic (businesses.daily_cost_cap_usd or daily_cost_cap_usd)
    # dezactivează LLM-ul pt restul zilei. Estimare-plasă; facturarea reală = usage_daily.
    cost_guard_enabled: bool = Field(default=True, validation_alias="COST_GUARD_ENABLED")
    cost_triage_usd: float = Field(default=0.0003, validation_alias="COST_TRIAGE_USD")
    cost_agent_usd: float = Field(default=0.003, validation_alias="COST_AGENT_USD")
    # NX-125: plafon SOFT de cheltuială per-contact (canale identificate), fereastră 24h. O singură
    # conversație în buclă nu mai poate arde plafonul întregului tenant. 0 = dezactivat (opt-in,
    # tunabil per-vertical/tenant). Web anonim are deja plafon per-vizitor (NX-120).
    contact_daily_cost_cap_usd: float = Field(
        default=0.0, validation_alias="CONTACT_DAILY_COST_CAP_USD"
    )
    # Buget de LATENȚĂ/COST PER TUR (CONV-COMMERCE P0): plafonul ZILNIC (cost guard) e separat —
    # ăsta e OBSERVABILITATE per-tur. Când un tur depășește bugetul (wall-clock end-to-end SAU
    # cost LLM), runner-ul emite `turn_over_budget` (cu stagiul cel mai lent) → vezi tururile
    # lente/scumpe ÎNAINTE să se plângă clientul. NU schimbă comportamentul (nu taie turul, P6).
    # Default 5000ms (doc: pipeline-ul poate face 5-8s, iZi 2-3s) → strânge pragul când optimizezi.
    turn_budget_alerts_enabled: bool = Field(
        default=True, validation_alias="TURN_BUDGET_ALERTS_ENABLED"
    )
    turn_latency_budget_ms: int = Field(default=5000, validation_alias="TURN_LATENCY_BUDGET_MS")
    turn_cost_budget_usd: float = Field(default=0.01, validation_alias="TURN_COST_BUDGET_USD")
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
    # Val3 (CONV-COMMERCE): job nocturn de LIFECYCLE — scrie contacts.lifecycle determinist
    # (new/engaged/customer/repeat/churn_risk) din comenzi + recență. Era nescris → toți „new".
    lifecycle_job_enabled: bool = Field(default=True, validation_alias="LIFECYCLE_JOB_ENABLED")
    lifecycle_hour_utc: int = Field(default=2, validation_alias="LIFECYCLE_HOUR_UTC")
    lifecycle_churn_days: int = Field(default=30, validation_alias="LIFECYCLE_CHURN_DAYS")

    # --- Extractor profil + lead_score (NX-88, post-tur stagiul 9) ---
    # Botul „învață" clientul: nano extrage semnale de profil → patch whitelist pe
    # contacts.profile + lead_score determinist. POST-TUR async (nu blochează livrarea), guardat
    # de cost guard (peste plafon → llm None → sărit). Rulează DOAR pe tururi cu rută (triajul a
    # angajat LLM-ul), NU pe free-layer/cache. Modelul e nano (model_triage); whitelist-ul de chei
    # per vertical e în src/worker/profile.py (mutat în taxonomie la NX-43). Kill-switch global.
    profile_extraction_enabled: bool = Field(
        default=True, validation_alias="PROFILE_EXTRACTION_ENABLED"
    )
    # Val3 (CONV-COMMERCE): lead_score (0..100, calculat post-tur) era NEcitit de agent. La scor
    # RIDICAT (≥ prag) injectăm un hint per-tur spre finalizare (bias checkout). Câmp mort → viu.
    lead_score_hint_enabled: bool = Field(default=True, validation_alias="LEAD_SCORE_HINT_ENABLED")
    lead_score_high_threshold: float = Field(
        default=70.0, validation_alias="LEAD_SCORE_HIGH_THRESHOLD"
    )

    # --- Motor proactiv (NX-70, scheduler separat peste proactive_jobs) ---
    # Producătorul pentru outbox: AWB / back-in-stock / coș abandonat / follow-up.
    # Gating-ul (consent / fereastră 24h / template) e poarta NX-71. Calea template = PR #142.
    proactive_enabled: bool = Field(default=True, validation_alias="PROACTIVE_ENABLED")
    proactive_batch_size: int = Field(default=20, validation_alias="PROACTIVE_BATCH_SIZE")
    proactive_idle_sleep_s: float = Field(default=5.0, validation_alias="PROACTIVE_IDLE_SLEEP_S")

    # --- Inițiatori proactivi (PL-1): sweeper-e care CREEAZĂ proactive_jobs ---
    # Până la PR2, NIMENI nu insera joburi → zero proactiv în prod (gap CRITICAL). Sweeper-ele
    # (coș abandonat + back-in-stock) rulează în mini-scheduler-ul intern (src/jobs/scheduler.py),
    # gardat ȘI de `proactive_enabled`. OFF → niciun job nou creat (motorul rămâne, dar fără hrană).
    proactive_initiators_enabled: bool = Field(
        default=True, validation_alias="PROACTIVE_INITIATORS_ENABLED"
    )
    proactive_initiators_interval_s: int = Field(
        default=900, validation_alias="PROACTIVE_INITIATORS_INTERVAL_S"
    )
    proactive_initiators_batch: int = Field(
        default=200, validation_alias="PROACTIVE_INITIATORS_BATCH"
    )
    # Coș abandonat: reamintim după `after` de la creare, dar NU coșuri mai vechi de `max_age`
    # (stale → spam). Default: reminder după 1h, ignoră > 7 zile.
    abandoned_cart_after_seconds: int = Field(
        default=3600, validation_alias="ABANDONED_CART_AFTER_SECONDS"
    )
    abandoned_cart_max_age_seconds: int = Field(
        default=604800, validation_alias="ABANDONED_CART_MAX_AGE_SECONDS"
    )

    # --- Validator cifre bare (NX-91, stagiul 8 inline în agent) ---
    # Pe lângă prețurile cu valută (_PRICE_RE), validatorul prinde și cifrele «grele» FĂRĂ valută
    # („costă 89", „47 pe stoc", „rating 4.9") care nu sunt grounded în ctx.retrieval → retry/
    # fallback. Kill-switch FAIL-OPEN: la fals-pozitive în prod, dezactivează fără redeploy de cod.
    validator_bare_numbers_enabled: bool = Field(
        default=True, validation_alias="VALIDATOR_BARE_NUMBERS_ENABLED"
    )
    # NX-117: pe calea de PROZĂ, claim-uri ne-numerice neverificabile (superlativ „best seller",
    # claim de stoc/disponibilitate „pe stoc") → retry/fallback determinist. FAIL-OPEN: OFF lasă
    # textul să treacă fără redeploy. (Calea bogată scrub-uiește deja câmp-cu-câmp în compose.)
    validator_claims_enabled: bool = Field(
        default=True, validation_alias="VALIDATOR_CLAIMS_ENABLED"
    )
    # NX-118: afirmație POZITIVĂ de stoc/disponibilitate („pe stoc", „in stock") validată
    # AVAILABILITY-aware — drop (rich) / invalid+retry+fallback (proză) DOAR dacă niciun produs
    # retrievat nu e pe stoc (in_stock/low_stock). `has_stock_claim` sare peste negat/viitor
    # („nu mai e pe stoc", „revine pe stoc"). DEFAULT OFF (opt-in): depinde de calitatea datelor
    # `availability` (frecvent stale/NULL) — activează-l per-tenant când stocul e fiabil. Flag
    # SEPARAT de `validator_claims_enabled` (NX-117): a opri claim-urile NU oprește stocul.
    validator_stock_claims_enabled: bool = Field(
        default=False, validation_alias="VALIDATOR_STOCK_CLAIMS_ENABLED"
    )
    # P0-safety (CONV-COMMERCE): guardrail pe sfat MEDICAL/beauty — RĂSPUNDERE JURIDICĂ. Blochează
    # structural claim-urile periculoase din răspuns (produsul „tratează/vindecă" o afecțiune, e
    # „sigur în sarcină/alăptare", „fără alergeni / efecte adverse", „recomandat de medic") pe
    # AMBELE căi: proză (invalid → retry → fallback determinist) + bogată (scrub câmp → DROP).
    # Promptul interzice preventiv claim-urile; ăsta e plasa structurală (P8). DEFAULT ON: la
    # fals-pozitive în prod, dezactivează fără redeploy (degradare la formulare cosmetică sigură).
    safety_medical_guardrail_enabled: bool = Field(
        default=True, validation_alias="SAFETY_MEDICAL_GUARDRAIL_ENABLED"
    )
    # NX-121: guardrails de input la gate (cod determinist, înainte de LLM). PII mask ON (defense-
    # in-depth peste channel_identities — PII liber-tastat nu intră în prompt/analytics, P12).
    # Injection screen OFF până e seedat DomainPack-ul per-tenant (fallback neutru în cod); e
    # DETECTARE/observabilitate, NU apărarea load-bearing (aia = validatorul de stagiul 8).
    input_pii_mask_enabled: bool = Field(default=True, validation_alias="INPUT_PII_MASK_ENABLED")
    injection_screen_enabled: bool = Field(
        default=False, validation_alias="INJECTION_SCREEN_ENABLED"
    )
    # --- Typing indicator + spargere reply (NX-90, stagiul 9 + transport) ---
    # Typing/read trimis INSTANT pe inbound (best-effort, direct prin ChannelSender, NU outbox).
    # Reply > reply_split_chars → spart în max 2 mesaje (citire ușoară pe telefon). Pur transport.
    typing_enabled: bool = Field(default=True, validation_alias="TYPING_ENABLED")
    reply_split_chars: int = Field(default=200, validation_alias="REPLY_SPLIT_CHARS")

    # --- Lock per conversație (NX-85, stagiul 2 — ordonare multi-consumer) ---
    # Serializează tururile aceleiași conversații între REPLICI de worker (lock Redis SET NX EX pe
    # business+expeditor). Ocupat → re-queue cu backoff scurt (cap dur). Fail-open dacă Redis e jos.
    conv_lock_enabled: bool = Field(default=True, validation_alias="CONV_LOCK_ENABLED")
    conv_lock_ttl_seconds: int = Field(default=30, validation_alias="CONV_LOCK_TTL_SECONDS")
    conv_lock_requeue_delay_ms: int = Field(
        default=150, validation_alias="CONV_LOCK_REQUEUE_DELAY_MS"
    )
    conv_lock_max_requeues: int = Field(default=10, validation_alias="CONV_LOCK_MAX_REQUEUES")

    # --- Retrieval & ranking de produse (ARCH-product-retrieval, 2026) ---
    # P0: sortare explicită pe intenție (sort_mode: price_asc pt „cel mai ieftin") + tie-break
    # determinist p.id + shrunk_rating (cold-start). Kill-switch FAIL-SAFE: OFF → ORDER BY-ul vechi
    # (rating desc, price asc) ȘI relax-ladder-ul vechi (price relaxat primul) — byte-identic.
    search_sort_mode_enabled: bool = Field(
        default=True, validation_alias="SEARCH_SORT_MODE_ENABLED"
    )
    # P1: follow-up „mai ieftin" → re-căutare deterministă a produselor STRICT mai ieftine decât
    # cel mai ieftin afișat, în aceeași categorie (search_cheaper_than) — nu re-rank pe set afișat.
    cheaper_intent_enabled: bool = Field(default=True, validation_alias="CHEAPER_INTENT_ENABLED")
    # IZI-parity (Tier 1, G2): intenție de COMPARAȚIE pe un set deja afișat („compară primele două",
    # „ce diferență e între ele") → tabel structurat DETERMINIST pe produsele afișate (re-fetch +
    # build_comparison), ca link/show_more/cheaper — fără să depindem de modelul care cheamă
    # `compare_products` (dacă narativiza în loc de tool-call, dădea proză în loc de tabel).
    # Agnostic de vertical (rânduri = preț/rating/disponibilitate/avantaje/brand din retrieval).
    # OFF → cade
    # pe bucla LLM (modelul decide dacă compară).
    compare_intent_enabled: bool = Field(default=True, validation_alias="COMPARE_INTENT_ENABLED")
    # IZI-parity: întrebare de tip SUPERLATIV pe setul AFIȘAT („care dintre ele e cea mai
    # ușoară/ieftină/hidratantă") → re-hidratează ÎNTREGUL set afișat și lasă modelul să RĂSPUNDĂ
    # la superlativ peste toate candidatele (nu o căutare nouă, nu 1 produs). Precede cheaper.
    # OFF → cade pe R3 (re-hidratare doar când modelul n-a retrievat) / bucla LLM.
    attr_query_enabled: bool = Field(default=True, validation_alias="ATTR_QUERY_ENABLED")
    # IZI-parity (Tier 2): rânduri de FAȚETĂ de domeniu în tabelul de comparație (finish/acoperire/
    # potrivit-pentru/..., din products.attributes), config din DomainPack.comparison_facets.
    # Generic pe vertical; rândul TOT-gol e sărit (date sărace → tabel ca azi). OFF → doar rândurile
    # generice (preț/rating/avantaje/brand), byte-identic cu înainte de Tier 2.
    comparison_facets_enabled: bool = Field(
        default=True, validation_alias="COMPARISON_FACETS_ENABLED"
    )
    # IZI-parity (Tier 2b): fațetele de domeniu (aceleași DomainPack.comparison_facets) intră și în
    # BUNDLE-ul rich → modelul VEDE ingredientele/beneficiul/potrivirea reale și scrie fit_clause
    # grounded („cu acid hialuronic, pentru ten uscat"), nu tautologic. Generic pe vertical; date
    # sărace → segment gol (degradare lină). OFF → bundle ca înainte (doar descriere/ai_summary).
    rich_facets_enabled: bool = Field(default=True, validation_alias="RICH_FACETS_ENABLED")
    # IZI-parity (Tier 2b p2): filtru de FAȚETĂ în search — „ceva cu niacinamidă" → match NORMALIZAT
    # pe atributele din DomainPack.searchable_facets (ex. key_ingredients). Dedicat (NU prin
    # map_concerns, care aruncă termenii non-concern). Relaxează ULTIMUL în ladder (P6). Generic pe
    # vertical. OFF / fără searchable_facets → fără filtru de feature (byte-identic cu înainte).
    facet_search_enabled: bool = Field(default=True, validation_alias="FACET_SEARCH_ENABLED")
    # NX-131: cerere de LINK pe un produs deja arătat („trimite-mi linkul / dă-mi link direct") →
    # servită DETERMINIST (Offer open_url + card din product_url proaspăt), nu prin calea rich (care
    # interzice modelului linkurile → bucla de coaching repetat). OFF → cade pe bucla LLM (vechi).
    link_intent_enabled: bool = Field(default=True, validation_alias="LINK_INTENT_ENABLED")
    # NX-119: sesiuni de căutare (pool + paginare „mai arată-mi"). OFF → fără sesiune persistată
    # (fiecare căutare e fresh) ȘI fără ramura deterministă de paginare (cade pe bucla LLM normală).
    search_sessions_enabled: bool = Field(default=True, validation_alias="SEARCH_SESSIONS_ENABLED")
    # IZI: badge de card DERIVAT din semnale reale (rating+recenzii → „Top Favorit"; reducere reală
    # → „Super Preț"), prin praguri din DomainPack.badge_rules (default-uri agnostice de vertical).
    # Determinist, NU inventat. OFF → doar badge-uri pre-seedate curate (comportament vechi).
    card_badges_enabled: bool = Field(default=True, validation_alias="CARD_BADGES_ENABLED")
    # ARCH-2026 P0: pe `relevance`, scor de ranking BLENDED determinist (RRF + social-proof shrunk
    # rating + disponibilitate + reducere + concern), nu RRF pur cu rating doar pe tie. Repară „un
    # produs mai bine cotat (4.6×148) ajunge sub unul mai slab (4.4×28)". Ponderile din
    # DomainPack.rank_weights (override per-vertical), fallback pe RANK_WEIGHTS (fusion.py). OFF
    # (fail-safe) → fuziunea cade pe `deterministic_rerank` (RRF pur, byte-identic).
    search_blended_rank_enabled: bool = Field(
        default=True, validation_alias="SEARCH_BLENDED_RANK_ENABLED"
    )
    # NX-134 (IZI-parity P2): prima pagină de rezultate (pe `relevance`) se DIVERSIFICĂ — scară de
    # preț (terțe) + max 2 produse per brand — în loc de top-N aproape identice. Selecție greedy
    # deterministă peste candidații DEJA rankați (top-1/pick neschimbat). OFF (fail-safe) → ordinea
    # de relevanță pură, byte-identic cu azi. Nu se aplică pe sort explicit / produs numit.
    search_diversify_enabled: bool = Field(
        default=True, validation_alias="SEARCH_DIVERSIFY_ENABLED"
    )
    # ARCH-2026 P0: cardurile rich sunt ORDONATE de rankingul de retrieval (determinist), iar
    # „Recomandarea mea" = produsul cel mai bine clasat afișat — NU alegerea liberă a modelului
    # (popularity/position bias). Modelul doar NAREAZĂ (justificare/fit). OFF (fail-safe) →
    # ordinea + pick-ul modelului (comportament vechi).
    rich_pick_deterministic_enabled: bool = Field(
        default=True, validation_alias="RICH_PICK_DETERMINISTIC_ENABLED"
    )
    # Linia „👉 Recomandarea mea" (pick angajat din framing). PREFERINȚA FERMĂ A CLIENTULUI (Adi,
    # repetat): NU o vrea în NICIUN mesaj — o simțea „aruncată" / redundantă cu cardurile. Default
    # OFF pe TOATE canalele (gate în `flatten_framing` web ȘI `flatten` floor WhatsApp/Telegram).
    # (Fusese pornit temporar pt „iZi-parity Tier 1 G1"; cererea userului îl anulează.) Reactivare
    # DOAR explicit din env `RICH_PICK_WEB_ENABLED=true` (reversibil) — nu-l re-porni default.
    rich_pick_web_enabled: bool = Field(default=False, validation_alias="RICH_PICK_WEB_ENABLED")
    # izi-parity (hardening): dacă retrievalul e o potrivire OFF-CATEGORY (categoria cerută a fost
    # renunțată în relaxare SAU cel mai apropiat vector e peste pragul de distanță), NU mai emitem
    # „👉 Recomandarea mea" pe un produs din categoria greșită; în loc, un mesaj ONEST de redirect
    # („nu am exact ce cauți, dar astea sunt cele mai apropiate"). Cardurile rămân (alternative).
    # Fail-open: fără semnal ⇒ comportament vechi. Reversibil din env, fără cod.
    rich_pick_relevance_gate_enabled: bool = Field(
        default=True, validation_alias="RICH_PICK_RELEVANCE_GATE_ENABLED"
    )
    # Pragul de distanță cosine peste care cel mai apropiat produs vector e considerat OFF-CATEGORY
    # (semnalul care prinde căutarea free-text FĂRĂ filtru de categorie — ex. „fond de ten" pe
    # catalog skincare, unde category_dropped e False). CONSERVATOR (mare) → suprimă DOAR rezultate
    # clar depărtate (fail spre a ARĂTA pick-ul, evită over-refusal). Tunabil din env pe date live
    # (vezi analytics: product_search.top_cosine_distance). None ⇒ dezactivează jumătatea cosine
    # (rămâne doar category_dropped).
    rich_pick_relevance_cosine_max: float | None = Field(
        default=0.6, validation_alias="RICH_PICK_RELEVANCE_COSINE_MAX"
    )
    # #7b (IZI-parity): după ce clientul adaugă un produs în coș, sugerăm produse COMPLEMENTARE
    # (rutină/accesorii — ca iZi: contur ochi + cremă din aceeași gamă) ca CARDURI. Retrieval
    # determinist (brand/concern, categorie diferită), copy prin calea rich. OFF → fără cross-sell
    # (rămâne confirmarea de coș a agentului, comportament vechi).
    cross_sell_enabled: bool = Field(default=True, validation_alias="CROSS_SELL_ENABLED")
    # NX-137: purchase_intent onorat determinist — clientul a cerut cumpărarea, coșul are linii,
    # dar modelul n-a chemat checkout_link (non-compliance observat live pe sim) → codul creează
    # linkul (ref=turn_id, idempotent per tur) și îl atașează ca Offer(open_url). OFF →
    # comportamentul vechi (linkul apare doar dacă modelul cheamă tool-ul).
    checkout_intent_fallback_enabled: bool = Field(
        default=True, validation_alias="CHECKOUT_INTENT_FALLBACK_ENABLED"
    )
    # Guard ruta `simple` (compusă de nano, FĂRĂ validatorul stagiului 8): dacă mesajul cere
    # CONFIRMAREA unui fapt de business (reducere/preț/stoc/politică/brand), re-rutează la `sales`
    # ca agentul grounded (+ prompt întărit) să-l trateze, în loc de un „da" nevalidat al nano-ului.
    triage_factual_guard_enabled: bool = Field(
        default=True, validation_alias="TRIAGE_FACTUAL_GUARD_ENABLED"
    )
    # NX-114: DomainPack (config per-vertical din DB+seed). Kill-switch FAIL-SAFE: OFF →
    # BusinessConfig.domain_pack=None, consumatorii cad pe constantele lor de cod (byte-identic).
    domain_pack_enabled: bool = Field(default=True, validation_alias="DOMAIN_PACK_ENABLED")
    # NX-116: anti-bucla de clarificare — după atâtea re-întrebări pe ACELAȘI slot, escaladăm
    # (HANDOFF pe slot critic / best-effort SALES altfel), niciodată re-întrebare la infinit (P6).
    clarify_max_attempts: int = Field(default=2, validation_alias="CLARIFY_MAX_ATTEMPTS")
    # NX-126: reziliență adaptor OpenAI (llm.py). `timeout` anti-hang (mai ales pe web sincron);
    # retry bounded pe tranzitoriu (429/5xx/timeout). `sampling_enabled` = kill-switch pt modele
    # „reasoning" care resping `temperature` ne-default → OFF lasă apelurile fără sampling params.
    llm_timeout_s: float = Field(default=30.0, validation_alias="LLM_TIMEOUT_S")
    llm_retry_max: int = Field(default=2, validation_alias="LLM_RETRY_MAX")
    llm_sampling_enabled: bool = Field(default=True, validation_alias="LLM_SAMPLING_ENABLED")
    # Temperatură pe ROL (independentă de corectitudine — aia o asigură validatorul stagiului 8):
    # triajul (clasificare) vrea determinism → mic; agentul (copy către client) vrea variație → mai
    # mare, ca răspunsurile să NU fie repetitive. Active doar când llm_sampling_enabled.
    llm_temperature_triage: float = Field(default=0.2, validation_alias="LLM_TEMPERATURE_TRIAGE")
    llm_temperature_agent: float = Field(default=0.7, validation_alias="LLM_TEMPERATURE_AGENT")
    llm_max_tokens_agent: int = Field(default=800, validation_alias="LLM_MAX_TOKENS_AGENT")
    # Dezvăluirea AI (art. 50 AI Act): OFF = NU o adăugăm la mesaje (decizie 2026-06-26 — clientul o
    # consideră repetitivă). Reversibilă: ON o repune (o singură dată, idempotent în Sender).
    ai_disclaimer_enabled: bool = Field(default=False, validation_alias="AI_DISCLAIMER_ENABLED")

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"


@lru_cache
def get_settings() -> Settings:
    """Singleton — citit o singură dată per proces."""
    return Settings()


def handoff_enabled_for(channel_kind: str) -> bool:
    """Handoff la om permis pe acest canal? Web (`webchat`) e exclus by default — anonim, fără
    operator conectat → escaladarea ar fi tăcere. Reversibil din `HANDOFF_ENABLED_CHANNELS` (vezi
    `Settings.handoff_enabled_channels`). Consumatori: gates (risc), handoff_stage (triaj/clarify),
    tool `request_human`, poarta de comandă web (oferta de operator)."""
    return channel_kind in get_settings().handoff_enabled_channels_set
