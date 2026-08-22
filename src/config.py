"""Settings centrale — citite din environment (.env în dev, secrets pe VPS).

Sursa unică de configurare. Orice variabilă nouă din cod se adaugă AICI și în
`.env.example` (regula din T007). Nimic hardcodat, nimic citit din os.environ
direct prin cod — totul prin `settings`.
"""

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import AliasChoices, Field, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

# NX-121: cap dur de lungime pe corpul inbound (text/caption/titlu interactiv), aliniat cu
# validarea web (`src/web/app.py` max_length=2000). Constantă structurală (paritate canale), nu
# setare per-tenant. Folosit la margine (webhook/meta.py) + ca plasă în gate (Vision-derived body).
INBOUND_BODY_MAX = 2000


class FileSecretsSource(PydanticBaseSettingsSource):
    """NX-248 — livrarea secretelor prin FIȘIER: `OPENAI_API_KEY_FILE=/run/secrets/openai`.

    Un `.env` cu secrete are trei scurgeri pe care nimeni nu le închide complet: e vizibil în
    `docker inspect` (env-ul containerului), e vizibil în `/proc/<pid>/environ` pentru orice
    proces din același container, și ajunge în orice dump de mediu pe care îl face o bibliotecă
    de erori. Un fișier montat read-only cu mod 0400 n-are niciuna dintre ele: nu e în env, nu e
    în `inspect`, iar procesul îl citește o dată la boot.

    **Ambiguitatea e eroare, nu preferință.** Dacă sunt setate ȘI `X`, ȘI `X_FILE`, procesul
    refuză să pornească. Alternativa („fișierul câștigă") pare prietenoasă până când cineva
    rotește secretul în fișier, uită env-ul vechi în `.env`, și jumătate din flotă rulează cu
    credentialul revocat — tăcut, fiindcă ambele „funcționează".
    """

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        # Nefolosit: livrăm tot dicționarul în `__call__` (avem nevoie de aliasuri, nu de nume).
        raise NotImplementedError

    def __call__(self) -> dict[str, Any]:
        # Cheile sunt ALIASURI de mediu (`OPENAI_API_KEY`), nu nume de câmp: exact ca
        # `EnvSettingsSource`. Pe câmpurile cu `validation_alias`, un dicționar cheiat pe numele
        # câmpului e ignorat TĂCUT de validator — adică un secret livrat corect prin fișier ar
        # cădea pe default, iar procesul ar porni „fără cheie" fără nicio eroare.
        values: dict[str, Any] = {}
        for name, field in self.settings_cls.model_fields.items():
            for alias in _env_aliases(name, field):
                raw = os.environ.get(f"{alias}_FILE")
                if not raw:
                    continue
                if os.environ.get(alias):
                    raise ValueError(
                        f"{alias} și {alias}_FILE sunt AMBELE setate — livrarea secretului e "
                        "ambiguă. Șterge-l pe cel vechi (vezi docs/SECRETS-ROTATION.md)."
                    )
                path = Path(raw)
                try:
                    # `strip()`: un `echo secret > file` lasă un `\n` care ar strica o cheie API
                    # într-un mod care se vede abia la primul apel, ca 401 fără explicație.
                    values[alias] = path.read_text(encoding="utf-8").strip()
                except OSError as e:
                    raise ValueError(
                        f"{alias}_FILE={raw} nu poate fi citit ({type(e).__name__}) — secretul nu "
                        "e livrat, deci procesul nu pornește (fail-closed)"
                    ) from e
                break
        return values


def _env_aliases(name: str, field: FieldInfo) -> tuple[str, ...]:
    """Numele de mediu sub care poate veni un câmp (`validation_alias` sau MAJUSCULE)."""
    alias = field.validation_alias
    if isinstance(alias, str):
        return (alias,)
    if isinstance(alias, AliasChoices):
        return tuple(a for a in alias.choices if isinstance(a, str))
    return (name.upper(),)


#: Valorile lui `ENV` care înseamnă „producție". Duplicat controlat în `scripts/migrate.py`
#: (care nu importă nimic din `src/`); egalitatea lor e verificată de un test.
_PROD_ENVS = frozenset({"prod", "production"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # .env are și variabile pt seed-ul node (SUPABASE_URL etc.)
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
        """NX-248: `*_FILE` intră ÎNAINTEA env-ului și a `.env`.

        Ordinea contează doar teoretic — coliziunea `X` + `X_FILE` e deja eroare de boot (vezi
        `FileSecretsSource`) — dar o punem prima ca intenția să fie citibilă: pe VPS, sursa
        canonică de secrete e fișierul montat, iar `.env` e tranziția documentată, nu ținta.
        """
        return (
            init_settings,
            FileSecretsSource(settings_cls),
            env_settings,
            dotenv_settings,
            file_secret_settings,
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
    # Modelul agentului pentru turul OBIȘNUIT. Majoritatea turelor (un răspuns, o recomandare,
    # o clarificare) nu au nevoie de vârful de gamă — plăteau însă ca și cum ar avea.
    model_agent: str = Field(default="gpt-5.6-luna", validation_alias="MODEL_AGENT")
    # Escaladarea pentru turele COMPLICATE (comparație, mesaj mixt, mutație).
    # GOL IMPLICIT — decizie explicită: totul rulează pe `model_agent`. Mecanismul de selecție
    # rămâne în cod și e testat, dar pornit ar plăti modelul scump pe o BĂNUIALĂ, iar D15 cere ca
    # alegerea de model să vină din măsurători. În plus, fiecare clasă pe care am fi escaladat-o
    # are deja un validator determinist în spate (`obligation_uncovered` pentru mesajul mixt,
    # evidence obligatoriu pe celulele de comparație, `CartService` + `policy.allows()` pentru
    # mutații), deci costul unei greșeli e un repair, nu un răspuns greșit livrat clientului.
    # Se aprinde punând numele modelului aici — o variabilă, fără schimbare de cod.
    model_agent_complex: str = Field(default="", validation_alias="MODEL_AGENT_COMPLEX")
    model_triage: str = Field(default="gpt-5.4-nano", validation_alias="MODEL_TRIAGE")
    model_embed: str = Field(default="text-embedding-3-small", validation_alias="MODEL_EMBED")
    model_moderation: str = Field(
        default="omni-moderation-latest", validation_alias="MODEL_MODERATION"
    )
    # Override de tarife LLM pentru observabilitatea de cost (NX-103). JSON parțial, merge peste
    # implicitul din src/agent/pricing.py — tunabil în prod fără redeploy. Gol → tarifele din cod.
    # Ex: {"gpt-5.6-terra": {"input": 2.50, "cached_input": 0.25, "output": 15.00}}
    llm_pricing_json: str = Field(default="", validation_alias="LLM_PRICING_JSON")

    # --- Media routing: Vision poză→catalog (NX-76, stagiul 3) ---
    # O poză de produs (content_type=image) e descrisă de Vision (prin adaptorul unic, ca
    # embed/moderate — extracție, NU generare) și descrierea devine text de căutare în
    # ctx.message.body → triaj rutează SALES → agentul cheamă search_products. Imagine→text→search.
    vision_enabled: bool = Field(default=True, validation_alias="VISION_ENABLED")
    # Modelul Vision: agentul (mini) are vedere; nano NU. Default = model_agent.
    model_vision: str = Field(default="gpt-5.6-terra", validation_alias="MODEL_VISION")
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
    # NX-221: serializare ture per conversație pe calea web SINCRONĂ (/web/chat). Lock Redis scurt
    # (SET NX PX) la margine, ÎNAINTE de handle_turn — două mesaje în rafală nu mai rulează două
    # pipeline-uri concurente pe același snapshot de stare. Default ON (fix de corectitudine, nu
    # feature); OFF = comportamentul vechi, byte-identic. Timeout de așteptare → BYPASS cu event
    # (principiul 6 — un lock blocat nu lasă clientul fără răspuns); TTL = plasă anti-deadlock.
    web_turn_lock_enabled: bool = Field(default=True, validation_alias="WEB_TURN_LOCK_ENABLED")
    turn_lock_ttl_ms: int = Field(default=15000, validation_alias="TURN_LOCK_TTL_MS")
    turn_lock_wait_max_ms: int = Field(default=10000, validation_alias="TURN_LOCK_WAIT_MAX_MS")
    # NX-232 — ledgerul durabil al turelor web (`web_turns`): idempotency pe
    # (tenant, conversație, client_turn_id) + replay exact al rezultatului terminal. Cutover pe
    # demo prin flag; OFF = calea veche byte-identică (dedupe → blank rămâne pe v1). Lockul NX-221
    # rămâne optimizare deasupra; ledgerul e garanția de corectitudine.
    web_turn_ledger_enabled: bool = Field(default=False, validation_alias="WEB_TURN_LEDGER_ENABLED")
    # Lease-ul de claim, ALINIAT deliberat cu CLAIM_TTL_S al inbound_dedupe (300s): un reclaim
    # de ledger înaintea expirării claim-ului durabil ar găsi turul „deduped" și n-ar putea rula.
    web_turn_lease_ttl_s: int = Field(default=300, validation_alias="WEB_TURN_LEASE_TTL_S")
    # Cheia HMAC a fingerprint-ului de request. Gol = HMAC cu cheie goală — tot ne-inversabil,
    # dar un DB scurs permite CONFIRMAREA unei ghiciri pe mesaje scurte; producția o setează.
    web_turn_fingerprint_secret: str = Field(
        default="", validation_alias="WEB_TURN_FINGERPRINT_SECRET"
    )
    # Retenția ledgerului: terminalele după fereastra de replay (ore); ne-terminalele abandonate
    # (accept fără follow-through / crash nerecuperat) după N zile. Purjate de cleanup_web_turns.
    web_turns_retention_hours: int = Field(
        default=168, validation_alias="WEB_TURNS_RETENTION_HOURS"
    )
    web_turns_stale_days: int = Field(default=7, validation_alias="WEB_TURNS_STALE_DAYS")
    # Retenția NU e gated pe `web_turn_ledger_enabled`: dacă flagul se stinge după ce s-au
    # acumulat rânduri, conținutul de conversație ar rămâne pe disc pentru totdeauna. Jobul e
    # no-op pe o DB fără migrarea 040 (guard `to_regclass`), deci pornirea lui e sigură oriunde.
    scheduler_web_turns_interval_seconds: int = Field(
        default=21600, validation_alias="SCHEDULER_WEB_TURNS_INTERVAL_SECONDS"
    )
    # NX-229 — sesiune v2: claims semnate cu expirare + key id + rotație dual-key + origin binding.
    # Emiterea e în spatele flagului; VERIFICAREA acceptă mereu ambele versiuni în overlap, ca o
    # întoarcere pe v1 să nu invalideze sesiunile v2 deja emise (cerința de rollback din card).
    web_session_v2_enabled: bool = Field(default=False, validation_alias="WEB_SESSION_V2_ENABLED")
    # Cutover: refuză semnăturile v1. Se aprinde DUPĂ ce toate sesiunile v1 au expirat natural.
    web_session_v2_required: bool = Field(default=False, validation_alias="WEB_SESSION_V2_REQUIRED")
    # 12h: destul cât o sesiune de cumpărături să nu se rupă în mijloc, destul de scurt cât o
    # semnătură scursă să nu fie utilă mult timp. v1 nu expira NICIODATĂ.
    web_session_ttl_s: int = Field(default=43200, validation_alias="WEB_SESSION_TTL_S")
    # Leagă sesiunea de originul care a cerut-o. Separat de flagul v2: se aprinde după ce
    # allowlistul e confirmat în producție, altfel ar rupe tenanții cu origini nedeclarate.
    web_session_origin_binding: bool = Field(
        default=False, validation_alias="WEB_SESSION_ORIGIN_BINDING"
    )
    # NX-229 — poarta de acces la site-ul demo (`Authorization: Bearer <jwt>`). NU e identitate de
    # cumpărător: `verify_demo_access` întoarce doar (ok, reason), niciodată claims. Identitatea
    # shopperului rămâne `id_token` în body, singurul transport canonic în v2. Suportă DOAR HS256;
    # un proiect migrat pe chei asimetrice cade pe `bad_alg` — fail-closed, vizibil.
    web_demo_access_enabled: bool = Field(default=False, validation_alias="WEB_DEMO_ACCESS_ENABLED")
    web_demo_access_secret: str = Field(default="", validation_alias="WEB_DEMO_ACCESS_SECRET")
    web_demo_access_issuer: str = Field(default="", validation_alias="WEB_DEMO_ACCESS_ISSUER")
    web_demo_access_audience: str = Field(default="", validation_alias="WEB_DEMO_ACCESS_AUDIENCE")
    web_demo_access_leeway_s: int = Field(default=30, validation_alias="WEB_DEMO_ACCESS_LEEWAY_S")
    # NX-229 — rate limit pe bootstrap. Lipsea complet: emiterea de sesiuni era nelimitată, deci
    # un atacator putea coase oricâte visitor_id-uri proaspete ca să ocolească limita per-visitor.
    web_bootstrap_rate_limit_max: int = Field(
        default=10, validation_alias="WEB_BOOTSTRAP_RATE_LIMIT_MAX"
    )

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
    # NX-175: rerank determinist (calificatori + marjă → clarify) vs top-1 orb pe cosine.
    # Măsurat: „Cum pot face un retur?" servea excepția „produs desfăcut" (0.619) în loc de
    # procedura generală (0.592) — marjă 0.026. Rerank demotează EXCEPȚIILE când întrebarea nu are
    # calificatorul → procedura câștigă. OFF (kill-switch) → top-1 orb (byte-identic cu #171).
    faq_rerank_enabled: bool = Field(default=True, validation_alias="FAQ_RERANK_ENABLED")
    # Câți candidați aduce top-k pentru rerank (5 = suficient pt clusterele reale; cost cosine mic).
    faq_topk: int = Field(default=5, validation_alias="FAQ_TOPK")
    # NX-124a: fallback de locale — user pe o limbă fără cunoștințe seedate, dar `default_locale`
    # le are → servim cunoștința existentă (NU traducem). DEFAULT OFF (opt-in: doar tenanții care
    # servesc o limbă fără FAQ seedat, ex. RO→HU). Prag STRICT (precision-first).
    faq_locale_fallback_enabled: bool = Field(
        default=False, validation_alias="FAQ_LOCALE_FALLBACK_ENABLED"
    )
    faq_fallback_tau: float = Field(default=0.85, validation_alias="FAQ_FALLBACK_TAU")

    # NX-208: QuerySpec în SHADOW (ADR D6/D11). ON → triajul emite `query_spec_shadow` (fără PII:
    # intent/sort/fațete/nr. constrângeri) pe turul sales — ZERO schimbare de comportament, DOAR
    # observabilitate. Extracția din triaj e provizorie (comparație); owner-ul ȚINTĂ = agentul
    # principal (F7). Default OFF → întoarcere completă la comportamentul vechi (byte-identic).
    query_spec_shadow_enabled: bool = Field(
        default=False, validation_alias="QUERY_SPEC_SHADOW_ENABLED"
    )
    # NX-187: Match Gate în SHADOW (post-retrieval). ON → planner-ul calculează MatchSet-ul (clase
    # exact/alternative/rejected din verdicte MATCH/MISMATCH/UNKNOWN) pe candidați + telemetrie
    # (`match_gate_shadow`, `match_gate_outcome`) — ZERO schimbare de răspuns. Enforce = NX-188
    # (înghețat). Default OFF → byte-identic cu azi.
    match_gate_shadow_enabled: bool = Field(
        default=False, validation_alias="MATCH_GATE_SHADOW_ENABLED"
    )

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
    # NX-218: creează din timp partițiile lunare (analytics_events/messages). Fără el, scrierile
    # cad în partiția DEFAULT (s-a întâmplat de la 1 aug 2026) → scanări în creștere + retenție
    # pe interval imposibilă. Zilnic e suficient: asigurăm luna curentă + următoarea.
    partition_job_enabled: bool = Field(default=True, validation_alias="PARTITION_JOB_ENABLED")
    scheduler_partition_interval_seconds: int = Field(
        default=86400, validation_alias="SCHEDULER_PARTITION_INTERVAL_SECONDS"
    )
    partition_months_ahead: int = Field(default=1, validation_alias="PARTITION_MONTHS_AHEAD")
    # NX-217: rollup nocturn al faptelor de cerere (demand_daily). Rulează în aceeași fereastră
    # cu rollup_usage — ambele citesc ziua UTC încheiată din analytics_events.
    demand_rollup_enabled: bool = Field(default=True, validation_alias="DEMAND_ROLLUP_ENABLED")

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

    # --- Dispatcher outbox (NX-147) ---
    # Bounded concurrency keeps user replies responsive without flooding provider APIs or DB pools.
    dispatcher_batch_size: int = Field(default=10, validation_alias="DISPATCHER_BATCH_SIZE")
    dispatcher_global_concurrency: int = Field(
        default=16, validation_alias="DISPATCHER_GLOBAL_CONCURRENCY"
    )
    dispatcher_tenant_concurrency: int = Field(
        default=4, validation_alias="DISPATCHER_TENANT_CONCURRENCY"
    )
    dispatcher_idle_sleep_s: float = Field(default=0.5, validation_alias="DISPATCHER_IDLE_SLEEP_S")

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
    # NX-211: dormant until NX-210 H3 GO. OFF preserves the pre-NX-211 render path.
    answer_plan_enabled: bool = Field(default=False, validation_alias="ANSWER_PLAN_ENABLED")
    answer_plan_critic_enabled: bool = Field(
        default=False,
        validation_alias="ANSWER_PLAN_CRITIC_ENABLED",
    )
    answer_plan_critic_coverage_threshold: float = Field(
        default=0.99,
        ge=0,
        le=1,
        validation_alias="ANSWER_PLAN_CRITIC_COVERAGE_THRESHOLD",
    )
    answer_plan_max_quality: bool = Field(default=False, validation_alias="ANSWER_PLAN_MAX_QUALITY")
    # NX-239: MainBrain unic + control plane determinist. OFF (default) = pipeline-ul de azi,
    # byte-identic. ON = dark/shadow DOAR — producția rămâne OFF până la GO-ul pairwise NX-246.
    single_brain_enabled: bool = Field(default=False, validation_alias="SINGLE_BRAIN_ENABLED")
    # NX-251: triajul nano IESE de pe drumul sincron. Sub single-brain el nu mai era writer
    # (control plane-ul îi demota reply-ul), dar APELUL rămânea: fiecare tur plătea o clasificare
    # nano care primea contextul complet, după care ACELEAȘI blocuri plecau încă o dată la brain —
    # exact cascada „un model mic clasifică înaintea creierului" pe care D1 o interzice.
    # ON = clasificarea se mută POST-tur (măsurătoare), nu mai stă între client și răspuns.
    triage_sync_shadow_enabled: bool = Field(
        default=False, validation_alias="TRIAGE_SYNC_SHADOW_ENABLED"
    )
    # Kill-switch al MĂSURĂTORII, nu al comportamentului: cât timp comparăm ce a făcut brain-ul cu
    # ce ar fi rutat triajul, plătim un apel nano post-tur. Se stinge separat când shadow-ul și-a
    # spus cuvântul, fără schimbare de cod și fără să readucă triajul pe calea sincronă.
    triage_shadow_enabled: bool = Field(default=True, validation_alias="TRIAGE_SHADOW_ENABLED")
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
    # NX-207: citirea embeddings-urilor shadow se activează numai după benchmark. OFF păstrează
    # exact doc_type='product', deci este kill switch-ul de revenire imediată la retrieval-ul live.
    search_shadow_enabled: bool = Field(default=False, validation_alias="SEARCH_SHADOW_ENABLED")
    # NX-226: rangul lexical pe `relevance` cu ambele semnale normalizate în [0,1] relativ la
    # pool-ul query-ului + ponderi explicite FTS/trgm (0.6/0.4). OFF (default) → suma brută
    # `ts_rank_cd + similarity`, byte-identic cu main. Se aprinde DUPĂ diff-ul produs de
    # `scripts/lexical_rank_compare.py` (D15: nicio schimbare de ranking pe speranță).
    lexical_rank_v2_enabled: bool = Field(default=False, validation_alias="LEXICAL_RANK_V2_ENABLED")
    # NX-169: proiecția faptelor canonice v3 (suitable_for/finish/texture/ingrediente/usage/badges/
    # best_for) în view-urile text ale agentului (_brief/_detail/_compare) + compare pe DIFERENȚE.
    # OFF → view-urile vechi (nume+preț+rating+ai_summary+pros/cons) byte-identic (degradare lină).
    catalog_projection_v2_enabled: bool = Field(
        default=True, validation_alias="CATALOG_PROJECTION_V2_ENABLED"
    )
    # NX-170: reason_codes (concern/budget/ingredient_match) + gate `not_recommended_for` (hard
    # exclude / soft atenționare) la retrieval. OFF → fără reason_codes/excludere (byte-identic).
    catalog_reason_codes_enabled: bool = Field(
        default=True, validation_alias="CATALOG_REASON_CODES_ENABLED"
    )
    # NX-173 (P0): gate DETERMINIST de contraindicații — context declarat de client (sarcină/
    # alăptare) × registru curat (db/seed/safety_rules.json) → produsele incompatibile nu intră în
    # retrieval/pool/carduri. NU e un feature-flag obișnuit: OFF readuce comportamentul care a
    # afișat un ser cu retinol unei cliente însărcinate. Se stinge DOAR ca ultim resort operațional
    # (ex. over-blocking dovedit pe fals pozitiv „sarcină" = *task*), niciodată „ca să curgă".
    safety_contraindications_enabled: bool = Field(
        default=True, validation_alias="SAFETY_CONTRAINDICATIONS_ENABLED"
    )
    # NX-171b: cross-sell/rutină din `product_relations` (relații explicite curate) în loc de
    # heuristica same-brand/concern. ON → relations-first cu fallback la heuristică DOAR când ancora
    # n-are nicio relație. OFF (kill-switch) → mereu heuristica veche (byte-identic).
    relations_first_enabled: bool = Field(default=True, validation_alias="RELATIONS_FIRST_ENABLED")
    # NX-171c: quality-gate `content_status` — DOAR produsele 'published' sunt servite clientului.
    # Kill-switch GLOBAL (feature disponibil); filtrarea EFECTIVĂ cere OPT-IN PER-TENANT
    # (businesses.settings->>'content_status_filter' = true), activat DOAR după ce backfill-ul a
    # rulat pt acel tenant (altfel catalog gol). Default: feature ON dar per-tenant OFF → nimeni
    # filtrat până nu optează. Env OFF → filtrul nu se aplică nicăieri (kill de urgență).
    content_status_filter_enabled: bool = Field(
        default=True, validation_alias="CONTENT_STATUS_FILTER_ENABLED"
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
    # Follow-up de recenzii pe produsele deja afișate: rezolvă produsul din nume/ordinal sau cere
    # explicit alegerea, apoi compune numai din review_summary/top_pros/top_cons/rating.
    review_intent_enabled: bool = Field(default=True, validation_alias="REVIEW_INTENT_ENABLED")
    # Follow-up de DETALIU pe un produs deja afișat („spune-mi mai multe”, „detalii despre primul”)
    # → răspuns determinist din catalog, nu o nouă recomandare compusă de model.
    detail_intent_enabled: bool = Field(default=True, validation_alias="DETAIL_INTENT_ENABLED")
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
    # NX-139: axele de decizie DERIVATE din setul afișat (fațete DomainPack cu ≥2 valori distincte
    # + interval de preț) intră ca input grounded în compunerea rich → intro-ul numește axe REALE
    # (tip de ten / fitment / material — per vertical), nu superficiale. OFF → fără linia de axe
    # (prompturile devin inerte pe partea asta), byte-identic cu azi.
    decision_axes_enabled: bool = Field(default=True, validation_alias="DECISION_AXES_ENABLED")
    # NX-139: cifrele de SPECIFICAȚIE prezente în datele produselor AFIȘATE (nume/fațete: „SPF 30",
    # „50 ml", „9000 BTU") devin permise în intro/education — grounded, nu inventate. Prețurile NU
    # intră niciodată în setul permis. OFF → doar cifrele clientului (comportamentul de azi).
    spec_digits_grounded_enabled: bool = Field(
        default=True, validation_alias="SPEC_DIGITS_GROUNDED_ENABLED"
    )
    # NX-134 (IZI-parity P2): prima pagină de rezultate (pe `relevance`) se DIVERSIFICĂ — scară de
    # preț (terțe) + max 2 produse per brand — în loc de top-N aproape identice. Selecție greedy
    # deterministă peste candidații DEJA rankați (top-1/pick neschimbat). OFF (fail-safe) → ordinea
    # de relevanță pură, byte-identic cu azi. Nu se aplică pe sort explicit / produs numit.
    search_diversify_enabled: bool = Field(
        default=True, validation_alias="SEARCH_DIVERSIFY_ENABLED"
    )
    # NX-167 (A): filtrul de categorie prinde produsul dacă ORICARE din categoriile lui (primary
    # SAU product_category_map) e categoria cerută SAU un DESCENDENT al ei (materialized path
    # `categories.path`). Repară „cerere pe părinte (machiaj) ratează copiii (fond-de-ten)". OFF
    # (fail-safe) → match exact pe slug/nume al `primary_category_id`, byte-identic cu azi.
    search_category_tree_enabled: bool = Field(
        default=True, validation_alias="SEARCH_CATEGORY_TREE_ENABLED"
    )
    # O categorie explicită este o constrângere de raft, nu un criteriu soft. ON → ladder-ul poate
    # relaxa nevoi/features, dar nu scoate categoria doar pentru a umple numărul de rezultate.
    search_category_hard_enabled: bool = Field(
        default=True, validation_alias="SEARCH_CATEGORY_HARD_ENABLED"
    )
    # NX-167 (B): la o cerere CLARĂ de categorie (triajul a dat `category`) în care search a fost
    # nevoit s-o relaxeze (`category_dropped`), NU afișa carduri din altă ramură — întoarce gol +
    # semnal de clarificare, în loc să prezinte off-category ca match. OFF → relaxarea de azi.
    search_offcategory_guard_enabled: bool = Field(
        default=True, validation_alias="SEARCH_OFFCATEGORY_GUARD_ENABLED"
    )
    # NX-167 (C): „compară primele 2" refuză produse din ramuri incoerente (root-branch diferit din
    # `categories.path`, ex. machiaj vs. par) → cade pe bucla LLM (re-caută coerent). Fail-open la
    # `path` lipsă. OFF → comportamentul vechi (compară orice 2 afișate).
    compare_coherence_guard_enabled: bool = Field(
        default=True, validation_alias="COMPARE_COHERENCE_GUARD_ENABLED"
    )
    # Fragmentele din recenzii sunt dovezi sociale, nu motive contextuale de recomandare. OFF
    # implicit → cardul folosește doar fit-ul grounded din descriere/fațete; recenziile rămân în
    # răspunsurile explicite de review/detail și în tabelul comparativ.
    rich_review_anchor_enabled: bool = Field(
        default=False, validation_alias="RICH_REVIEW_ANCHOR_ENABLED"
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
    # NX-136 (IZI-parity P12): la ÎNCHIDERE („mulțumesc, asta vreau") triajul atașează chips pe
    # categorii ADIACENTE celei discutate (cross-sell prin rutină). OFF → mesajul cald simplu, fără
    # chips (byte-identic cu azi pe `simple`).
    closure_chips_enabled: bool = Field(default=True, validation_alias="CLOSURE_CHIPS_ENABLED")
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
    # NX-225: buget de TIMP pentru embed-ul de query din `search_products` (P4 — bugetul stă în cod,
    # nu în speranță). `llm_timeout_s` × retry = până la ~90s de așteptare pe un furnizor lent, deși
    # piciorul lexical răspunde în milisecunde: la depășire cădem pe lexical-only, ca la eroare
    # (P6). 800ms lasă loc de jitter peste p99-ul normal al `text-embedding-3-small` (~sub 500ms).
    # 0 = dezactivat (fără wait_for, comportamentul de dinainte) — kill-switch numeric, fără flag.
    embed_timeout_ms: int = Field(default=800, validation_alias="EMBED_TIMEOUT_MS")
    llm_sampling_enabled: bool = Field(default=True, validation_alias="LLM_SAMPLING_ENABLED")
    # GPT-5.6 Terra: reasoning effort explicit pentru agent. Gol = omis (fallback complet la
    # comportamentul endpointului/modelului). Triage ramane nano si nu primeste effort aici.
    llm_reasoning_effort_agent: str = Field(
        default="high", validation_alias="LLM_REASONING_EFFORT_AGENT"
    )
    # Temperatură pe ROL (independentă de corectitudine — aia o asigură validatorul stagiului 8):
    # triajul (clasificare) vrea determinism → mic; agentul (copy către client) vrea variație → mai
    # mare, ca răspunsurile să NU fie repetitive. Active doar când llm_sampling_enabled.
    llm_temperature_triage: float = Field(default=0.2, validation_alias="LLM_TEMPERATURE_TRIAGE")
    llm_temperature_agent: float = Field(default=0.7, validation_alias="LLM_TEMPERATURE_AGENT")
    llm_max_tokens_agent: int = Field(default=800, validation_alias="LLM_MAX_TOKENS_AGENT")
    # Dezvăluirea AI (art. 50 AI Act): OFF = NU o adăugăm la mesaje (decizie 2026-06-26 — clientul o
    # consideră repetitivă). Reversibilă: ON o repune (o singură dată, idempotent în Sender).
    ai_disclaimer_enabled: bool = Field(default=False, validation_alias="AI_DISCLAIMER_ENABLED")
    # NX-146: Turn Replay poate stoca corpul promptului (redactat) în evenimentul agent_prompt.
    # Default OFF (PII + volum) — se aprinde doar pe dev / TTL scurt pentru debugging profund.
    replay_store_prompt_enabled: bool = Field(
        default=False, validation_alias="REPLAY_STORE_PROMPT_ENABLED"
    )
    # NX-148: extragerea + injectarea de conversation_facts (memorie structurată). OFF →
    # fără extractor de facts și fără facts_block (degradare la memoria de bază: history + state).
    conversation_facts_enabled: bool = Field(
        default=True, validation_alias="CONVERSATION_FACTS_ENABLED"
    )
    # NX-160: Memory v2 generic (capture broad → classify safety → canonicalize → inject safe).
    # Master kill-switch: OFF → comportament NX-148 (whitelist fail-closed per vertical, fără
    # captură deschisă/safety/canonicalizare). ON → pipeline-ul generic pe orice business.
    memory_v2_enabled: bool = Field(default=True, validation_alias="MEMORY_V2_ENABLED")
    # captura LARGĂ (raw_key liber, nu whitelist fail-closed). OFF → doar cheile canonice.
    memory_open_capture_enabled: bool = Field(
        default=True, validation_alias="MEMORY_OPEN_CAPTURE_ENABLED"
    )
    # injectarea facts sigure în prompt. OFF → facts se persistă, dar `facts_block` nu injectează.
    memory_safe_injection_enabled: bool = Field(
        default=True, validation_alias="MEMORY_SAFE_INJECTION_ENABLED"
    )
    # canonicalizarea raw_key → canonical_key. OFF → facts rămân raw (codul nu le mapează).
    memory_canonicalize_enabled: bool = Field(
        default=True, validation_alias="MEMORY_CANONICALIZE_ENABLED"
    )
    # NX-159 felia 1: telemetrie de CALITATE a formei răspunsului (response_shape +
    # completeness_gap), emisă GLOBAL din runner post-reply pe TOATE căile. Pur observabilitate
    # (P10), zero LLM, ZERO text/PII (P12). OFF → nu se emit evenimentele (turul neschimbat).
    response_telemetry_enabled: bool = Field(
        default=True, validation_alias="RESPONSE_TELEMETRY_ENABLED"
    )
    # NX-159 felia 2 (thin-path repair): fiecare cale subțire cu kill-switch propriu.
    # short-ack guard: un răspuns `simple`/nano SCURT („Da.") pe un context de vânzare deschis
    # primește chips deterministe (categorii adiacente) → nu mai închide conversația sec.
    short_ack_guard_enabled: bool = Field(default=True, validation_alias="SHORT_ACK_GUARD_ENABLED")
    # no-result (sales fără produse): pe lângă întrebare, atașează chips deterministe cu căi
    # concrete de continuare (popular / alt buget / altă categorie) → nu fundătură generică.
    no_result_alternatives_enabled: bool = Field(
        default=True, validation_alias="NO_RESULT_ALTERNATIVES_ENABLED"
    )
    # cheapest-already (nimic mai ieftin): atașează aceleași chips de continuare la mesajul
    # existent (care are deja o întrebare) → opțiuni clickabile, nu doar text.
    cheapest_alternatives_enabled: bool = Field(
        default=True, validation_alias="CHEAPEST_ALTERNATIVES_ENABLED"
    )
    # NX-159 felia 3: injectează profilul de STIL (DomainPack.response_style) ca ghid în compunerea
    # răspunsurilor agentului (proză/order/rich). OFF / stil gol → byte-identic.
    response_style_enabled: bool = Field(default=True, validation_alias="RESPONSE_STYLE_ENABLED")

    # --- Pool metrics (NX-161 Felia 0A) — instrumentare bot_pool ---
    # Emite `pool_metrics` per tur (acquire-wait al checkout-ului + ocuparea pool-ului) → semnalul
    # de WAIT în prod care declanșează fix-ul conn-per-op (docs/CONN-HOLD-ANALYSIS-2026.md). Pur
    # observabilitate (P10), ZERO PII (P12 — business_id e UUID de tenant). OFF → nu se emite
    # evenimentul (gauge-ul inflight din pool_metrics rămâne, folosit și de health).
    pool_metrics_enabled: bool = Field(default=True, validation_alias="POOL_METRICS_ENABLED")
    # NX-231: `db_ops` per tur — cât ține FIECARE operație conexiunea (checkout/hold pe operație).
    # Cu conn-per-op un tur face N checkout-uri, nu unul: fără defalcare nu poți spune care
    # operație e cea care contează. Pur observabilitate (P10), zero PII (nume de operație din cod).
    db_op_metrics_enabled: bool = Field(default=True, validation_alias="DB_OP_METRICS_ENABLED")
    # Mod DIAGNOSTIC: proxy de timing pe conexiune → `query_ms` real, deci `idle_held_ms` real
    # (hold − query). E cifra care demonstrează fix-ul, dar cere un wrapper pe fiecare query →
    # OFF pe calea fierbinte, ON doar la rularea de raport (scripts/sim/conn_hold_probe.py).
    db_query_timing_enabled: bool = Field(default=False, validation_alias="DB_QUERY_TIMING_ENABLED")

    # --- Admission control (NX-161 Felia 0C) — frâna EXPLICITĂ de concurență a tururilor ---
    # Semafor GLOBAL: câte tururi procesează SIMULTAN (bounds LLM concurrency). Poolul DB (max 10) e
    # azi frâna accidentală; conn-per-op o scoate → ĂSTA devine frâna. Setat > pool ca să NU bindeze
    # înainte de conn-per-op (plasă, nu no-op și nici reducere de throughput). OFF → fără frână.
    admission_enabled: bool = Field(default=True, validation_alias="ADMISSION_ENABLED")
    admission_max_inflight: int = Field(default=24, validation_alias="ADMISSION_MAX_INFLIGHT")
    # Plafon OPȚIONAL per-business (fairness minimă — un tenant nu monopolizează sloturile). 0 = off
    # (fairness complet per-tenant = epic separat). Peste plafon → re-queue (nu așteaptă slot).
    admission_max_per_business: int = Field(
        default=0, validation_alias="ADMISSION_MAX_PER_BUSINESS"
    )
    # Cât aștept un slot înainte de re-queue cu backoff (P6, nu drop). Sub TTL conv_lock (30s).
    admission_acquire_timeout_ms: int = Field(
        default=2000, validation_alias="ADMISSION_ACQUIRE_TIMEOUT_MS"
    )
    # Backoff re-queue admission. SEPARAT de conv_lock: admission NU are cap de DROP (P6 — peste
    # capacitate re-punem la infinit cu backoff, niciodată pierdut tăcut). Overload SUSȚINUT →
    # WARNING zgomotos la fiecare `admission_requeue_warn_every` re-puneri (operatorul scalează).
    admission_requeue_delay_ms: int = Field(
        default=200, validation_alias="ADMISSION_REQUEUE_DELAY_MS"
    )
    admission_requeue_warn_every: int = Field(
        default=20, validation_alias="ADMISSION_REQUEUE_WARN_EVERY"
    )

    # --- Admission DISTRIBUIT (NX-231) ---
    # 0C avea un semafor PER PROCES: cu N replici, capacitatea reală era N × cap, tăcut. Conn-per-op
    # scoate poolul DB din rolul de frână accidentală, deci frâna trebuie să fie reală și comună.
    # Backend Redis (aceeași instanță ca lock-ul de tur), leasing cu TTL → o replică moartă nu-și
    # ține sloturile la infinit.
    admission_distributed_enabled: bool = Field(
        default=True, validation_alias="ADMISSION_DISTRIBUTED_ENABLED"
    )
    # TTL-ul unui slot (lease). Peste el, slotul e considerat abandonat și recuperat de următorul
    # acquire. Trebuie > durata maximă plauzibilă a unui tur (LLM lent), altfel se eliberează sub
    # un tur încă viu; menținut mic cât să nu blocheze capacitatea după un crash.
    admission_lease_ttl_ms: int = Field(default=120_000, validation_alias="ADMISSION_LEASE_TTL_MS")
    # Fairness: plafonul per-business e ACTIV implicit acum (0C îl avea 0 = off). Un tenant în
    # burst nu poate lua mai mult decât atât din capacitatea globală → alt tenant găsește mereu loc.
    admission_max_per_business_distributed: int = Field(
        default=6, validation_alias="ADMISSION_MAX_PER_BUSINESS_DISTRIBUTED"
    )
    # Store indisponibil (Redis jos) → NU „cap N×proces în tăcere". Cădem pe un semafor local
    # BOUNDED, explicit mai mic, și raportăm `admission_degraded`. 0 = fail-closed total (respinge
    # traficul nou cât timp nu putem coordona).
    admission_local_fallback_max: int = Field(
        default=4, validation_alias="ADMISSION_LOCAL_FALLBACK_MAX"
    )
    # Deadline de așteptare în coadă pe calea SINCRONĂ web (request/response). Mai scurt decât cel
    # al workerului: acolo re-queue-ul e gratis, aici clientul stă pe un socket deschis.
    admission_web_wait_ms: int = Field(default=1500, validation_alias="ADMISSION_WEB_WAIT_MS")
    # Poarta de admission pe ruta web sincronă. OFF → comportamentul de dinainte (web fără frână).
    web_admission_enabled: bool = Field(default=True, validation_alias="WEB_ADMISSION_ENABLED")

    # --- NX-233: executorul web asincron (accept 202 + executor + SSE) -------
    # Flags SEPARATE (rollout gradat): accept/GET v2, executorul din worker, sweeperul de
    # recovery și SSE se aprind independent; toate OFF = zero schimbare de comportament.
    # V1 (/web/chat sincron) rămâne neatins până la cutoverul NX-249.
    web_turn_v2_enabled: bool = Field(default=False, validation_alias="WEB_TURN_V2_ENABLED")
    web_turn_executor_enabled: bool = Field(
        default=False, validation_alias="WEB_TURN_EXECUTOR_ENABLED"
    )
    web_turn_recovery_enabled: bool = Field(
        default=False, validation_alias="WEB_TURN_RECOVERY_ENABLED"
    )
    web_turn_sse_enabled: bool = Field(default=False, validation_alias="WEB_TURN_SSE_ENABLED")
    # Bugetul TOTAL al unui turn, fixat la accept (`deadline_at`). NU se prelungește la reclaim;
    # NX-241 îl strânge pe măsurători. Peste el → terminal onest `deadline_exceeded` (P6).
    web_turn_deadline_s: int = Field(default=120, validation_alias="WEB_TURN_DEADLINE_S")
    # Heartbeat-ul lease-ului (renew CAS pe owner+epoch). Trebuie să încapă de cel puțin 2 ori
    # în lease (validare mai jos): un tick ratat nu pierde lease-ul.
    web_turn_heartbeat_s: float = Field(default=60.0, validation_alias="WEB_TURN_HEARTBEAT_S")
    # Plafonul de claim-uri (attempt-uri) per turn: peste el, sweeperul/executorul terminalizează
    # cu error-view (`attempts_exhausted`) în loc să reia la nesfârșit un tur care crapă.
    web_turn_max_attempts: int = Field(default=3, validation_alias="WEB_TURN_MAX_ATTEMPTS")
    # Cadența sweeperului de recovery (bounded; advisory lock → unul singur mătură per flotă).
    web_turn_sweep_interval_s: float = Field(
        default=30.0, validation_alias="WEB_TURN_SWEEP_INTERVAL_S"
    )
    # Câte ture ia executorul dintr-un scan (fair, cele mai vechi primele).
    web_turn_claim_batch: int = Field(default=8, validation_alias="WEB_TURN_CLAIM_BATCH")
    # Cât doarme executorul între scanuri când nu vine niciun wake (Redis BRPOP timeout).
    web_turn_executor_poll_s: float = Field(
        default=2.0, validation_alias="WEB_TURN_EXECUTOR_POLL_S"
    )
    # Hint-ul server-owned de polling pentru client (202) + cadența pollului SSE intern.
    web_turn_poll_after_ms: int = Field(default=1000, validation_alias="WEB_TURN_POLL_AFTER_MS")
    web_turn_sse_poll_ms: int = Field(default=1000, validation_alias="WEB_TURN_SSE_POLL_MS")
    # Durata maximă a unei sesiuni SSE (bounded; clientul reconectează cu Last-Event-ID).
    web_turn_sse_max_s: float = Field(default=600.0, validation_alias="WEB_TURN_SSE_MAX_S")
    # Grația de shutdown a executorului: cât așteptăm turul curent înainte de cancel.
    web_turn_shutdown_grace_s: float = Field(
        default=10.0, validation_alias="WEB_TURN_SHUTDOWN_GRACE_S"
    )

    # --- NX-234: contextul de pagină ID-only + TurnSnapshot ------------------
    # DOUĂ flaguri, deliberat separate (rollout: shadow → enforcement → prompt):
    #   • `web_context_enabled` — validare + rehidratare + evenimente. Contextul EXISTĂ în
    #     snapshot, dar nu ajunge în prompt. Aici se măsoară cross-tenant/mismatch/freshness.
    #   • `web_context_prompt_enabled` — abia el lasă faptele rehidratate să atingă promptul și
    #     ancora deterministă. Fără primul, al doilea nu face nimic (poarta e AND).
    # Ambele OFF = zero schimbare de comportament, pe orice canal.
    web_context_enabled: bool = Field(default=False, validation_alias="WEB_CONTEXT_ENABLED")
    web_context_prompt_enabled: bool = Field(
        default=False, validation_alias="WEB_CONTEXT_PROMPT_ENABLED"
    )
    # Deadline BOUNDED al rehidratării de catalog. Un catalog lent nu are voie să țină turul:
    # peste el → `unavailable` (terminal onest downstream), nu o așteptare nelimitată.
    web_context_hydration_timeout_ms: int = Field(
        default=1500, validation_alias="WEB_CONTEXT_HYDRATION_TIMEOUT_MS"
    )
    # Peste ce vechime un fapt de catalog e marcat `stale`. NU îl aruncă: îl obligă să se
    # declare (disclosure/refresh e politica consumatorului, dar are nevoie de semnal).
    web_context_freshness_sla_s: int = Field(
        default=86400, validation_alias="WEB_CONTEXT_FRESHNESS_SLA_S"
    )
    # Contractul de pipeline scris în snapshot (legat de `web_turns.pipeline_version`).
    web_context_pipeline_version: str = Field(
        default="web-chat.v1", validation_alias="WEB_CONTEXT_PIPELINE_VERSION"
    )

    # --- NX-235: ConversationStateV2 (nevoi, revocări, clarificare cu information gain) ------
    # Rollout în trei trepte, ca la NX-234 — fiecare treaptă e o proprietate a obiectului, nu un
    # `if` împrăștiat prin stagii:
    #   • `conversation_state_v2_enabled` — SHADOW: starea v1 se hidratează în paralel ca v2,
    #     propunerile stagiilor se reduc determinist, se emite diff-ul pe valori canonice. `v1`
    #     rămâne autoritatea la scriere. Zero schimbare de comportament.
    #   • `conversation_state_v2_write_enabled` — abia el face v2 formatul PERSISTAT. Se scrie UN
    #     SINGUR format; cititorii v1 primesc o PROIECȚIE calculată la citire (`project_v1`), deci
    #     rollback-ul e sigur și nu există două copii care pot diverge.
    #   • `clarification_policy_v2_enabled` / `reference_precedence_v2_enabled` — independente de
    #     formatul persistat (lucrează pe starea hidratată, indiferent de versiune).
    conversation_state_v2_enabled: bool = Field(
        default=False, validation_alias="CONVERSATION_STATE_V2_ENABLED"
    )
    conversation_state_v2_write_enabled: bool = Field(
        default=False, validation_alias="CONVERSATION_STATE_V2_WRITE_ENABLED"
    )
    clarification_policy_v2_enabled: bool = Field(
        default=False, validation_alias="CLARIFICATION_POLICY_V2_ENABLED"
    )
    reference_precedence_v2_enabled: bool = Field(
        default=False, validation_alias="REFERENCE_PRECEDENCE_V2_ENABLED"
    )
    # Pragul de information gain sub care NU întrebăm (răspundem cu ce avem + declarăm ce nu
    # știm). Siguranța și conflictele hard trec peste el — sunt corectitudine, nu UX.
    clarification_min_information_gain: float = Field(
        default=0.30, validation_alias="CLARIFICATION_MIN_INFORMATION_GAIN"
    )
    # Fapte sensibile în memorie (NX-230): fără consimțământ/politică nu se PERSISTĂ nimic
    # sensibil — turul le poate folosi request-scoped, starea nu le primește.
    conversation_sensitive_memory_enabled: bool = Field(
        default=False, validation_alias="CONVERSATION_SENSITIVE_MEMORY_ENABLED"
    )

    # --- NX-236: acțiuni opace semnate (token sigilat + kernel determinist) --
    # UN kill-switch: OFF → nicio acțiune emisă în ViewModel și `input.type=action` refuzat onest
    # (exact comportamentul de dinainte de card). ON → butoanele v2 poartă tokenuri sigilate, iar
    # semantica lor trăiește EXCLUSIV pe server.
    web_actions_enabled: bool = Field(default=False, validation_alias="WEB_ACTIONS_ENABLED")
    # Inelul de chei: `kid:base64master[,kid_vechi:base64master]`. Prima EMITE, toate VERIFICĂ —
    # fereastra de rotație. SECRET: nu intră în repo, nu se loghează, nu apare în erori.
    web_action_keys: str = Field(default="", validation_alias="WEB_ACTION_KEYS")
    # Cât trăiește un buton. Ancorat în `completed_at`-ul turului-sursă (nu în ceasul cititorului),
    # deci un token e valabil TTL secunde de la momentul în care răspunsul a fost comis.
    web_action_ttl_s: int = Field(default=1800, validation_alias="WEB_ACTION_TTL_S")
    # Toleranța de ceas între emitent și verificator (procese diferite, VPS-uri diferite).
    web_action_clock_skew_s: int = Field(default=60, validation_alias="WEB_ACTION_CLOCK_SKEW_S")

    # --- NX-237: coșul canonic al conversației (CartService + mutation receipts) --
    # OFF (default) → byte-identic: coșul rămâne în `conversations.state.cart` (NX-79), tool-urile
    # merg pe calea legacy. ON → toate mutațiile de coș (tool LLM + acțiuni NX-236) trec prin
    # `CartService`: rehidratare + revalidare la fiecare mutație, receipt idempotent, versiune
    # monotonă; starea păstrează DOAR `cart_ref` (id + versiune), nu linii cu preț copiat.
    # Fără dual-write: cu flagul ON, `state.cart` legacy nu se mai scrie și nu se mai citește
    # ca autoritate (liniile vechi NU se importă cu preț stale — se pornește curat, documentat).
    conversation_cart_enabled: bool = Field(
        default=False, validation_alias="CONVERSATION_CART_ENABLED"
    )
    # Pragul de prospețime al faptelor comerciale (preț/stoc) — peste el, snapshotul se declară
    # `stale` (disclosure, nu blocaj — aceeași filozofie ca WEB_CONTEXT_FRESHNESS_SLA_S).
    commerce_facts_sla_s: int = Field(default=86400, validation_alias="COMMERCE_FACTS_SLA_S")

    # --- NX-240: grounding strict + projector pur `web-view.v2` ------------------
    # OFF (default) → byte-identic: turul persistă exact ce persista și înainte, iar proiecția v2
    # rămâne cea derivată din payload-ul v1 (NX-233). ON → la commit se îngheață verdictul de
    # grounding (`grounded_v2`), iar `terminal_view` îl proiectează cu `render_v2` — pur, zero I/O,
    # fapte care nu se mai pot schimba după commit. Cere `WEB_TURN_V2_ENABLED` (fără contractul v2
    # n-are unde livra) și `SINGLE_BRAIN_ENABLED` (fără `AnswerPlanV2` n-are ce proiecta).
    web_view_v2_projector_enabled: bool = Field(
        default=False, validation_alias="WEB_VIEW_V2_PROJECTOR_ENABLED"
    )

    # --- NX-238: promovarea măsurată a candidatului `search_entities` ------------
    # OFF (default) → `selector.select_provider` întoarce ÎNTOTDEAUNA `current_live`, fără să
    # atingă discul. ON singur NU e suficient: candidatul cere ȘI un artefact de decizie cu
    # verdict GO, amprentă validă și semnătură verificabilă. Verdictul măsurat azi e NOT-READY
    # (H3 are 0 cazuri sigilate din 50 cerute) — vezi reports/nx238/ și docs/NX-238-DECISION.md.
    retrieval_candidate_enabled: bool = Field(
        default=False, validation_alias="RETRIEVAL_CANDIDATE_ENABLED"
    )
    # Procentul de conversații care merg pe candidat, pe bucket STABIL din (business, conversație).
    # 0 = shadow/dark chiar și cu GO semnat: codul e deployat, dar nicio conversație nu-l atinge.
    retrieval_candidate_rollout_pct: int = Field(
        default=0, validation_alias="RETRIEVAL_CANDIDATE_ROLLOUT_PCT", ge=0, le=100
    )
    # Unde stă verdictul. Un fișier, nu o valoare de env: decizia trebuie să fie un ARTEFACT
    # citibil, cu amprentă și semnătură, nu un boolean pe care îl poate flipui un deploy.
    retrieval_decision_path: str = Field(
        default="reports/nx238/decision.json", validation_alias="RETRIEVAL_DECISION_PATH"
    )
    # Cheia cu care se verifică semnătura verdictului. SECRET de operare: nu intră în repo.
    # Absentă → un GO nu poate fi verificat, deci nu e crezut (`decision_no_signing_key`).
    retrieval_decision_key: str = Field(default="", validation_alias="RETRIEVAL_DECISION_KEY")
    # Versiunea de pipeline capturată în selecție (anti-drift: nu comutăm providerul în mijlocul
    # unei conversații, iar un bundle vechi nu se amestecă cu unul nou).
    retrieval_pipeline_version: str = Field(
        default="retrieval.v1", validation_alias="RETRIEVAL_PIPELINE_VERSION"
    )
    # Bugetul de timp al unui retrieval prin port. 0 = fără buget (kill-switch numeric).
    retrieval_deadline_ms: int = Field(default=0, validation_alias="RETRIEVAL_DEADLINE_MS", ge=0)

    # --- NX-241: deadline TOTAL de tur + bugete de execuție ----------------------
    # Trei flag-uri, în ordinea rollout-ului din card. Niciunul nu schimbă răspunsul: primul doar
    # MĂSOARĂ, al doilea impune TIMPUL, al treilea impune NUMĂRUL de apeluri.
    #
    # 1) spans: defalcarea pe faze (queue/load/retrieval/model/tools/validation/projection/commit)
    #    într-un singur event `turn_latency` per tur. Observe-only, ca `db_ops` (NX-231): fără ea
    #    nu există baseline, deci nici dreptul de a strânge pragurile.
    turn_latency_spans_enabled: bool = Field(
        default=True, validation_alias="TURN_LATENCY_SPANS_ENABLED"
    )
    # 2) deadline: UN singur buget monoton propagat tuturor operațiilor (LLM/embed/tool/retrieval).
    #    OFF (default) → fiecare strat își păstrează timeoutul de azi, byte-identic.
    turn_deadline_enabled: bool = Field(default=False, validation_alias="TURN_DEADLINE_ENABLED")
    # 3) budgets: plafoanele de apeluri (runde de model, tool calls, mutații, repair) IMPUSE.
    #    OFF → contoarele se numără și se raportează, dar nu refuză nimic (observe-only).
    turn_budget_enforced: bool = Field(default=False, validation_alias="TURN_BUDGET_ENFORCED")
    # Paralelismul citirilor independente în bucla de tool-calling. OFF → plafon 1 = serializarea
    # de azi. ON cere conn-per-op REAL (`tenant_db`): pe un provider static (teste/sim) rămâne 1,
    # fiindcă o conexiune asyncpg partajată nu suportă două operații simultane.
    turn_parallel_reads_enabled: bool = Field(
        default=False, validation_alias="TURN_PARALLEL_READS_ENABLED"
    )
    # Plafonul DUR de execuție al unui tur, indiferent ce spune `deadline_at` (ceas sărit, config
    # veche). Peste el → terminal onest. Stage 1 provizoriu: 15s (ADR).
    turn_hard_deadline_ms: int = Field(
        default=15_000, validation_alias="TURN_HARD_DEADLINE_MS", ge=1_000
    )
    # Totalul per clasă de tur (SLO Stage 1). Restul rapoartelor din manifest se scalează
    # proporțional — nu poți seta un total fără rezervă terminală (vezi `build_manifest`).
    turn_budget_exact_ms: int = Field(default=3_000, validation_alias="TURN_BUDGET_EXACT_MS", ge=1)
    turn_budget_recommendation_ms: int = Field(
        default=6_000, validation_alias="TURN_BUDGET_RECOMMENDATION_MS", ge=1
    )
    turn_budget_complex_ms: int = Field(
        default=10_000, validation_alias="TURN_BUDGET_COMPLEX_MS", ge=1
    )
    turn_budget_mutation_ms: int = Field(
        default=8_000, validation_alias="TURN_BUDGET_MUTATION_MS", ge=1
    )
    # Plafonul de timp al UNUI apel de model. Sub `llm_timeout_s` (30s): un singur apel n-are voie
    # să mănânce tot bugetul unui tur de 6s. Efectiv rămâne `min(cap, remaining − rezervă)`.
    llm_call_cap_ms: int = Field(default=8_000, validation_alias="LLM_CALL_CAP_MS", ge=100)
    # Minimul util pentru a mai PORNI un retry: sub el, un apel pe care oricum îl vom anula costă
    # bani și latență fără nicio șansă de rezultat.
    llm_retry_min_budget_ms: int = Field(
        default=600, validation_alias="LLM_RETRY_MIN_BUDGET_MS", ge=0
    )
    # Aftercare-ul rulează STRICT după terminal, dar tot bounded: un backlog nu are voie să țină
    # workerul (și deci următorul tur). Peste el → abandon + `aftercare_lag_ms{outcome=timeout}`.
    aftercare_deadline_ms: int = Field(
        default=20_000, validation_alias="AFTERCARE_DEADLINE_MS", ge=0
    )

    # --- NX-246: observabilitate (traces + metrici) + markeri de release ---------
    # `observability_enabled` e master switch-ul și e ABSORBANT: stins, `record_*`/`span()` se
    # întorc după un singur bool, deci calea fierbinte e byte-identică cu cea de dinainte de card.
    # Traces și metrici au kill-switch-uri INDEPENDENTE, iar exportul de rețea e al treilea: în
    # incident vrei „taie exportul, păstrează măsurarea locală", nu un singur buton care le stinge
    # pe toate (ai pierde exact datele care explică incidentul).
    observability_enabled: bool = Field(default=False, validation_alias="OBSERVABILITY_ENABLED")
    observability_traces_enabled: bool = Field(
        default=True, validation_alias="OBSERVABILITY_TRACES_ENABLED"
    )
    observability_metrics_enabled: bool = Field(
        default=True, validation_alias="OBSERVABILITY_METRICS_ENABLED"
    )
    # `none` (default) = nimic nu iese din proces · `capture` = memorie (teste/drive local) ·
    # `otlp` = rețea, și cere endpoint valid + master switch pornit (validat mai jos).
    observability_exporter: str = Field(default="", validation_alias="OBSERVABILITY_EXPORTER")
    observability_otlp_endpoint: str = Field(
        default="", validation_alias="OBSERVABILITY_OTLP_ENDPOINT"
    )
    observability_otlp_headers: str = Field(
        default="", validation_alias="OBSERVABILITY_OTLP_HEADERS"
    )
    observability_otlp_timeout_ms: int = Field(
        default=2000, validation_alias="OBSERVABILITY_OTLP_TIMEOUT_MS", ge=100
    )
    # Fracțiune de ture de SUCCES păstrate. Erorile/deadline-urile ies ÎNTOTDEAUNA (eșantionare pe
    # coadă, `tracing._flush_trace`) — altfel exact evenimentele rare pe care le investighezi ar fi
    # cele mai probabil aruncate.
    observability_sample_ratio: float = Field(
        default=0.05, validation_alias="OBSERVABILITY_SAMPLE_RATIO", ge=0.0, le=1.0
    )
    observability_queue_max: int = Field(
        default=2048, validation_alias="OBSERVABILITY_QUEUE_MAX", ge=1
    )
    observability_export_batch: int = Field(
        default=256, validation_alias="OBSERVABILITY_EXPORT_BATCH", ge=1
    )
    observability_flush_timeout_ms: int = Field(
        default=2000, validation_alias="OBSERVABILITY_FLUSH_TIMEOUT_MS", ge=1
    )
    # Secretul din care se derivă trace-id-ul unui tur (HMAC peste `web_turns.id`). Gol = derivare
    # tot deterministă, dar reproductibilă de oricine cunoaște `turn_id` (care e public). Nu e o
    # problemă de izolare — un trace-id nu dă acces la nimic — ci de confidențialitate a corelării.
    observability_trace_secret: str = Field(
        default="", validation_alias="OBSERVABILITY_TRACE_SECRET"
    )
    # NX-246 felia 2 — feedback one-tap. OFF = niciun prompt emis ⇒ niciun token ⇒ endpointul
    # n-are ce autoriza (poarta e dublă, ca la comerțul NX-237: și emiterea, și consumul).
    # Cere `WEB_TURN_V2_ENABLED` + `WEB_ACTIONS_ENABLED`: promptul e o acțiune opacă semnată, deci
    # fără mecanismul de acțiuni n-ar avea cum să existe (validat la boot).
    web_feedback_enabled: bool = Field(default=False, validation_alias="WEB_FEEDBACK_ENABLED")
    # Secretul din care se derivă `feedback_prompt_id` (HMAC peste turn_id). Gol = derivare tot
    # deterministă, dar ghicibilă de cine cunoaște `turn_id` — acceptabil în dev, nu în prod.
    web_feedback_prompt_secret: str = Field(
        default="", validation_alias="WEB_FEEDBACK_PROMPT_SECRET"
    )
    service_name: str = Field(default="nativx-assistant", validation_alias="SERVICE_NAME")
    release_sha: str = Field(default="", validation_alias="RELEASE_SHA")
    #: Trackul PROCESULUI (NX-246): ce build rulează instanța asta. NU e trackul unui turn — de la
    #: NX-249 încolo, trackul unui turn e capturat pe rândul lui de ledger, fiindcă un proces poate
    #: servi ambele cohorturi. Rămâne util pentru metricile de instanță.
    release_track: str = Field(default="champion", validation_alias="RELEASE_TRACK")

    # --- NX-249: controllerul de release (canary + cutover) -------------------
    # OFF (default) = zero schimbare: niciun policy nu se citește, nicio coloană de captură nu se
    # scrie, iar `release_track` rămâne ce era. Aprins, fiecare accept v2 primește o asignare
    # SERVER-OWNED, capturată durabil înainte de orice claim.
    release_controller_enabled: bool = Field(
        default=False, validation_alias="RELEASE_CONTROLLER_ENABLED"
    )
    # Mediul căruia îi aparțin policy-urile. Gol → `env`. Explicit fiindcă „staging" și „prod" pot
    # rula pe același `env=prod` din perspectiva codului, iar un policy de staging aplicat în
    # producție ar promova trafic real pe baza unei aprobări date pentru altceva.
    release_environment: str = Field(default="", validation_alias="RELEASE_ENVIRONMENT")
    # Saltul de bucketing. NU intră niciodată în policy, în DB sau în loguri (policy-ul poartă doar
    # `stable_salt_id`). Gol = bucketing determinist dar PUBLIC calculabil: acceptabil în dev,
    # refuzat la boot în prod (vezi `_release_relations`).
    release_assignment_salt: str = Field(default="", validation_alias="RELEASE_ASSIGNMENT_SALT")
    # TTL-ul cache-ului de policy. E și fereastra maximă dintre „am apăsat kill-switch" și „ultimul
    # proces a aflat" — deci ținta de ≤5 minute a cardului îi pune un plafon dur (validat la boot).
    release_policy_refresh_s: float = Field(
        default=15.0, validation_alias="RELEASE_POLICY_REFRESH_S", gt=0
    )

    # --- NX-248: operare (health, deploy, credential de migrare) ---
    # Tokenul pentru `/health/detail` (vederea de operator: sonde + reason codes). GOL = endpointul
    # răspunde 404, nu 401: un 401 confirmă că ruta există și invită la ghicit. Nu e un secret de
    # produs — e un secret de recon, deci se rotește separat (docs/SECRETS-ROTATION.md).
    ops_health_token: str = Field(default="", validation_alias="OPS_HEALTH_TOKEN")
    # Fereastra de freshness a heartbeat-ului proceselor non-HTTP (worker/scheduler). Peste ea,
    # healthcheckul din compose declară procesul nesănătos. Vezi `src/ops/worker_health.py`:
    # freshness e DOAR primul dintre cele trei teste (PID viu + boot id fac restul).
    ops_heartbeat_max_age_s: float = Field(
        default=90.0, validation_alias="OPS_HEARTBEAT_MAX_AGE_S", gt=0
    )
    # DSN-ul cu drept de DDL. Există EXCLUSIV în jobul de migrare (un serviciu compose separat,
    # sub profil), niciodată în `env_file`-ul serviciilor de runtime — vezi
    # `tests/test_ops_deploy_manifest.py::test_credentialul_de_migrare_nu_ajunge_in_runtime`,
    # care citește docker-compose.prod.yml și pică dacă cineva îl mută în ancora comună.
    # Gol în runtime = corect: `scripts/migrate.py --check` are nevoie doar de SELECT.
    database_url_migration: str = Field(default="", validation_alias="DATABASE_URL_MIGRATION")

    @model_validator(mode="after")
    def _observability_relations(self) -> "Settings":
        """NX-246: config de observabilitate imposibilă = proces care nu pornește.

        Poarta e aceeași ca la NX-233/241, din același motiv: un endpoint scris greșit care
        eșuează tăcut la fiecare export produce EXACT patologia pe care cardul o numește — un
        dashboard verde peste un sistem care nu raportează. Validarea trăiește în
        `src/observability/config.py` (unde e și restul contractului); aici o chemăm ca să crape
        la boot, nu la primul span.
        """
        from src.observability.config import ObservabilityConfigError, from_settings

        try:
            from_settings(self)
        except ObservabilityConfigError as e:
            raise ValueError(str(e)) from e
        # NX-246 felia 2: promptul de feedback E o acțiune opacă semnată. Fără mecanismul de
        # acțiuni n-ar exista nici token de emis, nici ce autoriza la consum — deci un flag aprins
        # singur ar sugera că se strâng voturi când, de fapt, nu se strânge nimic.
        if self.web_feedback_enabled:
            missing = [
                name
                for name, on in (
                    ("WEB_TURN_V2_ENABLED", self.web_turn_v2_enabled),
                    ("WEB_ACTIONS_ENABLED", self.web_actions_enabled),
                )
                if not on
            ]
            if missing:
                raise ValueError(
                    f"WEB_FEEDBACK_ENABLED cere {' + '.join(missing)} (promptul de feedback e o "
                    "acțiune opacă semnată; fără ele nu există nici emitere, nici consum)"
                )
        return self

    @model_validator(mode="after")
    def _turn_budget_relations(self) -> "Settings":
        """NX-241: manifestul de bugete se VALIDEAZĂ la boot (poartă fail-fast).

        Alternativa la crash e „nelimitat în tăcere" — adică exact starea de dinainte de card, dar
        cu un flag aprins care sugerează contrariul. `build_manifest` ridică `ValueError` cu motivul
        exact (rezervă terminală lipsă, raport de fază peste total, repair > 1)."""
        from src.runtime.turn_budget import TurnClass, build_manifest  # noqa: PLC0415 — ciclu

        build_manifest(
            totals_ms={
                TurnClass.EXACT: self.turn_budget_exact_ms,
                TurnClass.RECOMMENDATION: self.turn_budget_recommendation_ms,
                TurnClass.COMPLEX: self.turn_budget_complex_ms,
                TurnClass.MUTATION: self.turn_budget_mutation_ms,
            },
            hard_cap_ms=self.turn_hard_deadline_ms,
            cost_ceiling_usd=self.turn_cost_budget_usd,
        )
        if self.turn_budget_enforced and not self.turn_deadline_enabled:
            raise ValueError(
                "TURN_BUDGET_ENFORCED cere TURN_DEADLINE_ENABLED: plafoanele de apeluri fără un "
                "deadline propagat opresc apelurile, dar nu și așteptarea pe ele"
            )
        if self.turn_parallel_reads_enabled and not self.turn_deadline_enabled:
            raise ValueError(
                "TURN_PARALLEL_READS_ENABLED cere TURN_DEADLINE_ENABLED: paralelismul fără "
                "deadline "
                "partajat înmulțește apelurile în zbor la depășire, nu le taie"
            )
        if self.llm_call_cap_ms > self.turn_hard_deadline_ms:
            raise ValueError(
                f"LLM_CALL_CAP_MS ({self.llm_call_cap_ms}ms) nu poate depăși TURN_HARD_DEADLINE_MS "
                f"({self.turn_hard_deadline_ms}ms) — un singur apel ar consuma tot turul"
            )
        return self

    @model_validator(mode="after")
    def _retrieval_candidate_relations(self) -> "Settings":
        """NX-238: un ramp fără cheie de verificare e o configurație imposibilă, oprită la BOOT.

        Fără `RETRIEVAL_DECISION_KEY` niciun GO nu poate fi verificat — deci un
        `RETRIEVAL_CANDIDATE_ROLLOUT_PCT > 0` ar promite un canary care nu poate porni niciodată.
        Mai rău, ar arăta în config ca și cum candidatul rulează. Preferăm eroarea la boot
        adevărului ascuns: poarta rămâne închisă oricum, dar acum se și vede."""
        if not self.retrieval_candidate_enabled:
            return self
        if self.retrieval_candidate_rollout_pct > 0 and not self.retrieval_decision_key:
            raise ValueError(
                "RETRIEVAL_CANDIDATE_ROLLOUT_PCT > 0 cere RETRIEVAL_DECISION_KEY: fără cheie, "
                "verdictul GO nu poate fi verificat, deci candidatul nu poate fi selectat"
            )
        return self

    @model_validator(mode="after")
    def _web_action_relations(self) -> "Settings":
        """NX-236: configurația imposibilă oprește procesul la BOOT, ca la NX-233/234.

        Trei relații, fiecare cu o consecință concretă dacă e greșită:
          • fără contractul v2 nu există unde livra acțiuni (v1 rămâne neatins până la NX-249) —
            un flag aprins singur ar sugera o funcționalitate care nu poate ajunge la client;
          • fără inel de chei valid nu se poate sigila nimic (fail-closed, nu „emite nesemnat");
          • un TTL mai lung decât retenția ledgerului ar produce tokenuri care supraviețuiesc
            dovezii lor de emitere ȘI evidenței de consum — adică butoane care redevin
            re-consumabile după ce jobul de retenție trece peste rândul-sursă."""
        if not self.web_actions_enabled:
            return self
        if not self.web_turn_v2_enabled:
            raise ValueError(
                "WEB_ACTIONS_ENABLED cere WEB_TURN_V2_ENABLED (acțiunile opace există doar pe "
                "contractul web-view.v2; v1 rămâne neatins până la NX-249)"
            )
        from src.web.action_crypto import parse_key_ring  # noqa: PLC0415 — evită ciclul de import

        parse_key_ring(self.web_action_keys)  # ridică KeyRingError (ValueError) cu motivul exact
        if self.web_action_ttl_s <= 0 or self.web_action_clock_skew_s < 0:
            raise ValueError("WEB_ACTION_TTL_S trebuie > 0, WEB_ACTION_CLOCK_SKEW_S >= 0")
        if self.web_action_ttl_s >= self.web_turns_retention_hours * 3600:
            raise ValueError(
                f"WEB_ACTION_TTL_S ({self.web_action_ttl_s}s) trebuie să fie sub retenția "
                f"ledgerului ({self.web_turns_retention_hours}h): un token care supraviețuiește "
                "rândului-sursă își pierde dovada de emitere și evidența de consum"
            )
        return self

    @model_validator(mode="after")
    def _conversation_state_relations(self) -> "Settings":
        """NX-235: a scrie v2 fără a-l hidrata/reduce ar însemna un format persistat pe care
        nimic nu l-a produs. Poarta e AND, validată la boot (ca la NX-233/234)."""
        if self.conversation_state_v2_write_enabled and not self.conversation_state_v2_enabled:
            raise ValueError(
                "CONVERSATION_STATE_V2_WRITE_ENABLED cere CONVERSATION_STATE_V2_ENABLED "
                "(fără reducer nu există document v2 de scris)"
            )
        if not 0.0 <= self.clarification_min_information_gain <= 1.0:
            raise ValueError("CLARIFICATION_MIN_INFORMATION_GAIN trebuie să fie în [0, 1]")
        return self

    @model_validator(mode="after")
    def _web_turn_relations(self) -> "Settings":
        """NX-233: relațiile dintre parametrii executorului se VALIDEAZĂ la boot, nu se
        descoperă sub incident. Config imposibilă → proces care refuză să pornească (ca poarta
        de migrări), nu un lease care expiră între două heartbeat-uri."""
        if self.web_turn_heartbeat_s * 2 > self.web_turn_lease_ttl_s:
            raise ValueError(
                f"WEB_TURN_HEARTBEAT_S ({self.web_turn_heartbeat_s}s) trebuie să încapă de cel "
                f"puțin 2 ori în WEB_TURN_LEASE_TTL_S ({self.web_turn_lease_ttl_s}s) — altfel "
                "un singur tick ratat pierde lease-ul"
            )
        if self.web_turn_deadline_s <= 0 or self.web_turn_max_attempts < 1:
            raise ValueError("WEB_TURN_DEADLINE_S și WEB_TURN_MAX_ATTEMPTS trebuie să fie >= 1")
        if self.web_turn_executor_poll_s <= 0 or self.web_turn_sweep_interval_s <= 0:
            raise ValueError("WEB_TURN_EXECUTOR_POLL_S / WEB_TURN_SWEEP_INTERVAL_S: > 0")
        # NX-234: expunerea în prompt fără validare/rehidratare ar însemna fapte necontrolate în
        # prompt — poarta e AND, deci configurația care sugerează altceva refuză să pornească.
        if self.web_context_prompt_enabled and not self.web_context_enabled:
            raise ValueError(
                "WEB_CONTEXT_PROMPT_ENABLED cere WEB_CONTEXT_ENABLED (fără rehidratare validată "
                "nu există fapte de expus în prompt)"
            )
        if self.web_context_hydration_timeout_ms <= 0 or self.web_context_freshness_sla_s <= 0:
            raise ValueError("WEB_CONTEXT_HYDRATION_TIMEOUT_MS / _FRESHNESS_SLA_S: > 0")
        # NX-240: projectorul grounded consumă `AnswerPlanV2` (produs DOAR de MainBrain) și
        # livrează pe contractul v2. Aprins singur ar fi un flag care nu poate face nimic — și,
        # mai rău, ar sugera că răspunsurile sunt grounded când de fapt sunt tot proiecția v1.
        if self.web_view_v2_projector_enabled:
            missing = [
                name
                for name, on in (
                    ("WEB_TURN_V2_ENABLED", self.web_turn_v2_enabled),
                    ("SINGLE_BRAIN_ENABLED", self.single_brain_enabled),
                )
                if not on
            ]
            if missing:
                raise ValueError(
                    f"WEB_VIEW_V2_PROJECTOR_ENABLED cere {' + '.join(missing)} (projectorul "
                    "proiectează AnswerPlanV2 pe contractul web-view.v2; fără ele n-are nici "
                    "sursă, nici destinație)"
                )
        # NX-251: fără MainBrain, a scoate triajul de pe calea sincronă lasă turul FĂRĂ writer —
        # nimeni n-ar mai seta ruta, iar `agent_stage` ar ieși imediat, deci fiecare mesaj ar cădea
        # în fallback-ul generic. Combinația e imposibilă, nu „degradată": refuzăm la boot.
        if self.triage_sync_shadow_enabled and not self.single_brain_enabled:
            raise ValueError(
                "TRIAGE_SYNC_SHADOW_ENABLED cere SINGLE_BRAIN_ENABLED (fără creierul unic nimeni "
                "nu mai decide ruta, iar turul ar răspunde doar cu fallback)"
            )
        return self

    @model_validator(mode="after")
    def _release_relations(self) -> "Settings":
        """NX-249: un controller de release configurat imposibil nu pornește.

        Trei relații, fiecare cu o consecință care s-ar descoperi altfel în incident:
          • candidate ÎNSEAMNĂ contractul v2 — fără el, controllerul ar asigna conversații către
            un pipeline care nu are cum să livreze;
          • fără salt în producție, bucketul e calculabil de oricine cunoaște `conversation_id`,
            deci un client își poate căuta o conversație care intră în canary. În dev e acceptabil
            (și util: face testele reproductibile fără secrete);
          • un TTL de refresh mai mare decât ținta operațională ar face kill-switchul mai lent
            decât promisiunea din runbook — iar promisiunea e ce se măsoară în drill.
        """
        if not self.release_controller_enabled:
            return self
        if not self.web_turn_v2_enabled:
            raise ValueError(
                "RELEASE_CONTROLLER_ENABLED cere WEB_TURN_V2_ENABLED (candidate = contractul "
                "web-view.v2; fără el n-ar exista unde livra cohortul candidate)"
            )
        if self.is_prod and not self.release_assignment_salt.strip():
            raise ValueError(
                "RELEASE_ASSIGNMENT_SALT e obligatoriu în prod: fără salt, bucketul de canary e "
                "calculabil de oricine cunoaște conversation_id"
            )
        if self.release_policy_refresh_s > 300:
            raise ValueError(
                f"RELEASE_POLICY_REFRESH_S ({self.release_policy_refresh_s}s) depășește ținta de "
                "5 minute pentru oprirea accepturilor candidate (docs/STAGE1-CANARY-RUNBOOK.md)"
            )
        return self

    @property
    def release_env(self) -> str:
        """Mediul de release efectiv. `RELEASE_ENVIRONMENT` explicit, altfel `env`."""
        return (self.release_environment or self.env).strip()

    @property
    def is_prod(self) -> bool:
        """Producția are UN singur înțeles în tot sistemul.

        Înainte, aici era `self.env == "prod"` — potrivire exactă — în timp ce
        `scripts/migrate.py` accepta `("prod", "production")`. Cu `ENV=production`, runnerul
        de migrări te trata ca în producție (refuza credentialul de runtime pentru DDL), iar
        restul aplicației nu: `RELEASE_ASSIGNMENT_SALT` redevenea opțional, adică bucketul de
        canary devenea calculabil de oricine cunoaște `conversation_id`. Două definiții ale
        aceluiași cuvânt, în același repo, care se contraziceau exact pe protecții.

        Rezolvarea nu e simetrică: acceptăm AMBELE forme, în loc să restrângem `migrate.py`.
        Un mediu în plus tratat ca producție înseamnă reguli mai STRICTE aplicate poate unde
        nu trebuiau; invers, un `ENV` scris altfel decât se aștepta cineva ar stinge tăcut
        protecții. Când un default se poate greși, se greșește în direcția care nu doare.
        """
        return self.env.strip().lower() in _PROD_ENVS


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
