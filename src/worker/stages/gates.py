"""Stagiul 3 — Gates. Decide DETERMINIST dacă botul are voie să răspundă.

Primul stagiu real de control, înaintea oricărui LLM (principiul 2). Trei porți,
în ordine, fiecare cu early-exit:
  1. bot_active=False  → tăcere (kill-switch per conversație; omul scrie din inbox)
  2. handoff activ     → tăcere (un om a preluat până la handoff_until)
  3. risc (pattern)    → request_human + UN mesaj de tranziție, apoi botul tace

AGNOSTIC de canal: gate-ul decide doar „răspunde botul?". CUM arată handoff-ul
(tăcere pe WhatsApp/TG vs agent live pe webchat) e treaba marginilor, nu a
gate-ului — aici doar setăm starea (`handoff_until`/`risk_flags`) și emitem
`handoff_requested`. Tăcerea intenționată (`ctx.halt_silent`) e singura excepție
documentată de la principiul 6.

Câmpuri TurnContext scrise aici: `ctx.halt` (via halt_silent), `ctx.reply` (risc) și
`ctx.message.body` (media routing NX-76: descrierea Vision a unei poze devine text de căutare).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from base64 import b64encode
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import asyncpg

from src.agent.llm import VISION_NOT_PRODUCT
from src.config import INBOUND_BODY_MAX, get_settings
from src.db.queries.contacts import block_contact
from src.db.queries.conversations import set_handoff
from src.models import TurnContext
from src.worker.limits import cost_add, rate_limit_count

if TYPE_CHECKING:
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)

# Media routing (NX-76): fail-soft pe orice eșec Vision → clarificare, NU tăcere/excepție (P6).
IMG_FALLBACK_BODY = "Am primit poza 🙂 Îmi spui ce produs cauți?"

# Răspuns neutru la un mesaj flagged (NX-15): ne-cache-uit, fără a premia abuzul cu un om.
NEUTRAL_MSG = (
    "Hai să păstrăm conversația respectuoasă, te rog 🙂 "
    "Cu ce te pot ajuta legat de produse sau comenzi?"
)

# Fereastra contorului de flag-uri (secunde) — 24h, ca pragul de blocklist să fie pe zi.
_FLAG_WINDOW_S = 24 * 60 * 60

# Mesaj de throttle (G2c): trimis O SINGURĂ dată la depășirea pragului de rate limit.
THROTTLE_MSG = (
    "Primesc multe mesaje deodată 🙂 Îți răspund imediat, mai trimite-mi în câteva secunde."
)

# Pattern-uri de risc (RO, normalizate fără diacritice/uppercase). Determinist, NU LLM.
# Extensibil per-business din settings = follow-up.
RISK_PATTERNS: dict[str, list[str]] = {
    "human_request": [
        "vreau sa vorbesc cu un om",
        "vorbesc cu un om",
        "cu un operator",
        "operator uman",
        "agent uman",
        "om real",
        "persoana reala",
    ],
    "legal_complaint": [
        "avocat",
        "anaf",
        "protectia consumatorului",
        "reclamatie",
        "instanta",
        "te dau in judecata",
        "in judecata",
    ],
}


def _norm(text: str) -> str:
    """Lowercase + fără diacritice (NFKD) → match robust pe „să"/„SA"/„sa"."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def detect_risk(text: str | None) -> str | None:
    """Întoarce motivul de escaladare (primul găsit) sau None. Pur, fără LLM."""
    if not text:
        return None
    norm = _norm(text)
    for reason, phrases in RISK_PATTERNS.items():
        if any(phrase in norm for phrase in phrases):
            return reason
    return None


# --- NX-121: guardrails de input (cod determinist, înainte de LLM) -----------

# PII liber-tastat de client (NU PII-ul de canal din channel_identities, ăla e autorizat). Defense-
# in-depth: ține PII-ul afară din promptul TURULUI CURENT (ctx.message.body → triaj/agent),
# write-back-ul de cache și analytics (doar contoare). NB (scope NX-121): `messages.body` stocat
# rămâne RAW (PII legitim în storage — explicit out-of-scope) → istoricul îl poate reintroduce în
# prompt la turul următor; redaction de storage/istoric = follow-up. Conservativ — semnal mare,
# fără să corupă coduri de produs/comenzi. EMAIL/IBAN întâi (semn distinctiv), CARD înaintea
# TELEFON (un card de 16 cifre nu trebuie spart de regexul de telefon).
_PII_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PII_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b")
# Candidat de card: 13-19 cifre cu separatori uzuali, dar separatorul stă ÎNTRE cifre (nu la
# coadă) → nu „înghite" spațiul de după (ar lipi `[card]` de cuvântul următor). _card decide.
_PII_CARD_CAND = re.compile(r"\b\d(?:[ -]?\d){12,18}\b")
# Lungimi REALE de PAN (Amex 15, majoritatea 16, Visa/Discover 19) + IIN 3-6 (Amex/Visa/MC/Disc).
# Exclude 13/14 → un EAN-13 care TRECE Luhn (~10% din ele) NU mai e mascat ca „card" (DoD).
_CARD_LEN = frozenset({15, 16, 19})
# Telefon: cere prefix de TELEFON (+țară sau 0 local RO) ca să NU prindem coduri/EAN-uri bare.
# Două alternative: internațional (+40 7xx...) sau local RO (07xx...). `(?<!\d)`/`(?!\d)` evită
# matching parțial al unei secvențe mai lungi (un cod de 13+ cifre nu devine „telefon").
_PII_PHONE = re.compile(
    r"(?<!\d)(?:"
    r"(?:\+|00)\d{1,3}(?:[ .-]?\d){8,11}"  # +40 / 0040 712 345 678 (intl: țară + 8-11 cifre)
    r"|0(?:[ .-]?\d){9}"  # 0712 345 678 (RO local: 0 + 9 cifre)
    r")(?!\d)"
)


def _luhn(digits: str) -> bool:
    """Checksum Luhn (validare card). Plasă SECUNDARĂ după lungime (_CARD_LEN) + IIN 3-6 — nu se
    bazează singur pe Luhn ca să excludă EAN-uri (un EAN-13 poate trece Luhn ~10% din timp)."""
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def mask_pii(text: str) -> tuple[str, dict[str, int]]:
    """Maschează PII liber-tastat (telefon/email/iban/card) → `[telefon]`/`[email]`/`[iban]`/
    `[card]`. Pur. Întoarce (text_mascat, contoare per tip). Cardul = secvență 13-19 cifre care
    trece Luhn (nu prinde EAN/cod produs). Ordine: email → iban → card → telefon."""
    counts = {"phone": 0, "email": 0, "iban": 0, "card": 0}

    def _email(_m: re.Match) -> str:
        counts["email"] += 1
        return "[email]"

    def _iban(_m: re.Match) -> str:
        counts["iban"] += 1
        return "[iban]"

    def _card(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) in _CARD_LEN and digits[0] in "3456" and _luhn(digits):
            counts["card"] += 1
            return "[card]"
        return m.group(0)  # secvență lungă dar nu card (EAN/cod produs) → neatins

    def _phone(_m: re.Match) -> str:
        counts["phone"] += 1
        return "[telefon]"

    text = _PII_EMAIL.sub(_email, text)
    text = _PII_IBAN.sub(_iban, text)
    text = _PII_CARD_CAND.sub(_card, text)
    text = _PII_PHONE.sub(_phone, text)
    return text, counts


# Pattern-uri NEUTRE de prompt-injection (vertical-agnostice, normalizate fără diacritice). Baza din
# cod; DomainPack (NX-114) ADAUGĂ per-tenant/vertical (P9). Detectare, NU apărare load-bearing.
_INJECTION_FALLBACK = (
    "ignore previous",
    "ignore all previous",
    "ignore the above",
    "disregard previous",
    "ignora instructiun",
    "ignora tot ce",
    "uita instructiun",
    "nu tine cont de instructiun",
    "you are now",
    "esti acum",
    "act as if",
    "pretinde ca esti",
    "system prompt",
    "reveal your instruct",
    "show your prompt",
    "arata-ti instructiun",
    "spune-mi promptul",
    "output the price",
    "set the price",
    "schimba pretul",
    "pretul este acum",
    "developer mode",
    "jailbreak",
)


def _injection_patterns(ctx: TurnContext) -> list[str]:
    """Pattern-urile de injection NORMALIZATE: baza neutră din cod + cele din DomainPack (toate
    locale-urile, additiv). Fără DomainPack → doar baza (fail-open)."""
    pats = [_norm(p) for p in _INJECTION_FALLBACK]
    pack = getattr(ctx.business, "domain_pack", None)
    if pack is not None:
        for locale_pats in (pack.injection_patterns or {}).values():
            pats.extend(_norm(p) for p in locale_pats)
    return [p for p in pats if p]


def screen_injection(ctx: TurnContext) -> int:
    """Câte pattern-uri de injection apar în corpul inbound (normalizat). 0 = niciunul. Pur."""
    body = _norm(ctx.message.body or "")
    if not body:
        return 0
    return sum(1 for p in _injection_patterns(ctx) if p in body)


def _apply_input_guardrails(ctx: TurnContext) -> None:
    """NX-121: clamp lungime (idempotent — acoperă și body-ul derivat din Vision) → mascare PII →
    ecran injection. Cod determinist (P2), înainte de triaj/agent. Niciun guard nu setează `reply`
    și niciunul nu tace (P6): trunchiere, nu rejection; injection = doar observabilitate."""
    s = get_settings()
    body = ctx.message.body or ""
    if len(body) > INBOUND_BODY_MAX:
        ctx.message.body = body[:INBOUND_BODY_MAX]
        ctx.emit("body_truncated", chars=len(body))  # DOAR lungimea, nu corpul (P12)
    if s.input_pii_mask_enabled and ctx.message.body:
        masked, counts = mask_pii(ctx.message.body)
        if any(counts.values()):
            ctx.message.body = masked
            ctx.emit("input_pii_masked", **counts)  # DOAR contoare per tip (P12)
    if s.injection_screen_enabled:
        try:
            n = screen_injection(ctx)
            if n:
                # NU tăcem și NU escaladăm (ar premia abuzul); mesajul curge → validatorul de
                # stagiul 8 e apărarea reală. DOAR contorul, niciodată corpul (P12).
                ctx.emit("injection_screened", n=n)
        except Exception as e:  # noqa: BLE001 — ecran best-effort → fail-open (P6)
            log.warning("injection screen eșuat (%s) → fail-open", type(e).__name__)


async def request_human(
    conn: asyncpg.Connection,
    ctx: TurnContext,
    reason: str,
    *,
    source: str = "risk",
    assigned_user_id: str | None = None,
) -> None:
    """Escaladează la om: setează fereastra de handoff + risk_flag, emite evenimentul.

    `assigned_user_id` e un CÂRLIG (web-ready): G5a nu auto-asignează — îl umple
    consola de agent (task de margine). Partea activă acum = `handoff_until` +
    `risk_flags` + `handoff_requested` (channel-agnostic)."""
    window = get_settings().handoff_window_minutes
    await set_handoff(
        conn,
        ctx.business.id,
        ctx.conversation_id,
        window_minutes=window,
        risk_flag=reason,
        assigned_user_id=assigned_user_id,
    )
    ctx.emit("handoff_requested", reason=reason, source=source)


async def _record_flag_and_maybe_block(ctx: TurnContext, deps: PipelineDeps) -> None:
    """Contor de flag-uri în Redis (analytics e append-only fără SELECT din runtime).
    La ≥ prag într-o fereastră de 24h → abuse blocklist. Best-effort: orice eșec se
    loghează, NU rupe răspunsul neutru (deja setat)."""
    if deps.redis is None:
        return
    key = f"modflags:{ctx.business.id}:{ctx.contact.id}"
    try:
        count = await deps.redis.incr(key)
        if count == 1:
            await deps.redis.expire(key, _FLAG_WINDOW_S)
        if count >= get_settings().moderation_block_threshold:
            await block_contact(deps.conn, ctx.business.id, ctx.contact.id)
            ctx.emit("contact_blocked", flag_count=count)
    except Exception as e:  # noqa: BLE001 — contorul e best-effort, răspunsul neutru rămâne
        log.warning("moderation: contor/blocklist eșuat (%s)", type(e).__name__)


async def _rate_limited(ctx: TurnContext, deps: PipelineDeps) -> bool:
    """Rate limit per contact (G2c). True ⇒ peste prag: a setat throttle (la depășire) sau
    a tăcut (deja peste) → early-exit. Fail-OPEN: Redis jos / dezactivat → False (nu blochează).
    Rulează ÎNAINTE de moderation (check Redis ieftin înaintea apelului de moderation API)."""
    settings = get_settings()
    if not settings.rate_limit_enabled or deps.redis is None:
        return False
    try:
        count = await rate_limit_count(
            deps.redis, ctx.business.id, ctx.contact.id, settings.rate_limit_window_seconds
        )
    except Exception as e:  # noqa: BLE001 — guard Redis jos → fail-open
        log.warning("rate limit: contor eșuat (%s) → fail-open", type(e).__name__)
        return False
    if count <= settings.rate_limit_max:
        return False
    ctx.emit("rate_limited", count=count)
    if count == settings.rate_limit_max + 1:
        # tocmai a depășit → un singur mesaj de throttle (apoi tăcere pe restul burst-ului).
        ctx.set_reply(THROTTLE_MSG, cacheable=False)
    else:
        ctx.halt_silent("rate_limited")
    return True


async def _moderation_blocked(ctx: TurnContext, deps: PipelineDeps) -> bool:
    """Poarta de moderare (NX-15). True ⇒ flagged: a setat răspunsul neutru → early-exit.

    Fail-OPEN: fără cheie / API jos → False (mesajul trece normal). Indisponibilitatea
    moderării NU trebuie să tacă tot traficul; e best-effort safety, nu o poartă dură."""
    settings = get_settings()
    if not settings.moderation_enabled or deps.llm is None:
        return False
    body = (ctx.message.body or "").strip()
    if not body:
        return False
    try:
        res = await deps.llm.moderate(body)
    except Exception as e:  # noqa: BLE001 — fail-open
        log.warning("moderation: apel eșuat (%s) → fail-open", type(e).__name__)
        return False
    if not res.flagged:
        return False
    # Flagged: NICIODATĂ corpul în analytics (principiul 12) — doar categoriile.
    ctx.emit("message_moderated", categories=res.categories)
    await _record_flag_and_maybe_block(ctx, deps)
    ctx.set_reply(NEUTRAL_MSG, cacheable=False)
    return True


async def _charge_vision_cost(ctx: TurnContext, deps: PipelineDeps) -> None:
    """Contează apelul Vision în contorul zilnic de cost (G2c), ca un apel de agent. Best-effort
    (ca summarizer-ul): un eșec de Redis NU rupe turul — descrierea e deja injectată."""
    settings = get_settings()
    if deps.redis is None or not settings.cost_guard_enabled:
        return
    try:
        await cost_add(deps.redis, ctx.business.id, settings.cost_vision_usd)
    except Exception as e:  # noqa: BLE001 — contor best-effort
        log.warning("vision: cost_add eșuat (%s)", type(e).__name__)


async def _route_image(ctx: TurnContext, deps: PipelineDeps) -> None:
    """Poză client → Vision → descriere ca text de căutare în `ctx.message.body`.

    NU setează `reply` și NU oprește pipeline-ul: doar ÎMBOGĂȚEȘTE inputul → triajul rutează SALES,
    agentul cheamă `search_products` pe textul derivat. Imagine → text → search (NU avem
    image-embedding în catalog). Fail-soft (P6) pe orice eșec: Vision off / fără llm sau fetcher /
    media lipsă/prea mare / eroare API / descriere goală / „nu pare un produs" → PĂSTRĂM caption-ul
    util al pozei ca text de căutare (intenție de cumpărare), altfel `body` = clarificare. Fără
    excepție propagată. NU descărcăm pe disk, NU logăm bytes/base64/url-ul semnat (P12)."""
    settings = get_settings()
    caption = (ctx.message.body or "").strip()  # caption-ul WA (poate avea intenție de cumpărare)

    def _failsoft(reason: str) -> None:
        # Caption cu intenție reală (ex. „mai aveți crema asta?") NU se aruncă: rămâne text de
        # căutare → triajul tot rutează SALES. Doar fără caption cădem pe clarificarea generică.
        ctx.message.body = caption or IMG_FALLBACK_BODY
        ctx.emit("image_route_failed", reason=reason)

    if not settings.vision_enabled or deps.llm is None or deps.media is None:
        _failsoft("disabled")
        return
    media_id = ctx.message.media_ref
    if not media_id:
        _failsoft("no_media")
        return
    fetcher = deps.media.get(ctx.message.channel_kind)  # margine de canal
    if fetcher is None:
        _failsoft("no_downloader")
        return
    try:
        blob, mime = await fetcher.fetch_media(
            ctx.message.channel_account_id, media_id, max_bytes=settings.vision_max_bytes
        )
        if len(blob) > settings.vision_max_bytes:  # plasă: cap și post-download (file_size lipsă)
            _failsoft("too_large")
            return
        desc = (await deps.llm.describe_image(b64encode(blob).decode(), mime) or "").strip()
    except Exception as e:  # noqa: BLE001 — fail-soft, NU tăcere (P6); FĂRĂ media_ref/url în log
        log.warning("vision: %s → fallback clarificare imagine", type(e).__name__)
        _failsoft("vision_error")
        return
    await _charge_vision_cost(ctx, deps)  # apelul Vision s-a făcut → contează costul (G2c)
    if not desc:
        _failsoft("empty_desc")
        return
    # Sentinel determinist din prompt (poză non-produs: selfie/screenshot/peisaj) → clarificare,
    # nu căutare pe text mort. Match normalizat ca să nu diveargă de promptul care îl cere.
    if _norm(VISION_NOT_PRODUCT) in _norm(desc):
        _failsoft("not_a_product")
        return
    ctx.message.body = f"[poză client] {desc}" + (f" — text client: {caption}" if caption else "")
    ctx.emit("image_routed", chars=len(desc))  # DOAR lungimea — niciodată conținutul (P12)


async def gates_stage(ctx: TurnContext, deps: PipelineDeps) -> None:
    """Porțile de control (vezi docstring-ul modulului). Early-exit pe oricare."""
    # 1. kill-switch: botul e oprit pe ACEASTĂ conversație → tăcere.
    if not ctx.bot_active:
        ctx.halt_silent("bot_inactive")
        return

    # 2. abuse blocklist (NX-15): contact blocat → tăcere (ca handoff).
    if ctx.contact.is_blocked:
        ctx.halt_silent("contact_blocked")
        return

    # 3. handoff activ: un om a preluat până la handoff_until → tăcere.
    if ctx.handoff_until is not None and ctx.handoff_until > datetime.now(UTC):
        ctx.halt_silent("handoff_active")
        return

    # 4. rate limit (G2c): prea multe mesaje/fereastră → throttle (ieftin, înaintea moderării).
    if await _rate_limited(ctx, deps):
        return

    # 5. moderare (NX-15): mesaj toxic → răspuns neutru, NU ajunge la triaj/agent.
    #    Înaintea riscului: abuzul primește răspuns neutru, nu escaladare la om.
    if await _moderation_blocked(ctx, deps):
        return

    # 6. risc → escaladează + UN mesaj de tranziție; turul următor va cădea pe (3).
    reason = detect_risk(ctx.message.body)
    if reason:
        await request_human(deps.conn, ctx, reason, source="risk")
        # NX-126: necacheabil — un mesaj de escaladare scris în semantic_cache ar fi servit altui
        # user FĂRĂ ca vreun om să fie notificat (cache poisoning → tăcere de facto, încalcă P6).
        ctx.set_reply("Te conectez cu un coleg, revin imediat 🙂", cacheable=False)
        return

    # 7. media routing (NX-76): poză → Vision → descriere ca text de căutare, apoi pipeline normal.
    #    DUPĂ rate-limit/moderare (nu cheltui Vision pe un contact throttled/blocat); NU early-exit
    #    (doar îmbogățește ctx.message.body) → triajul/agentul curg pe textul derivat din poză.
    if ctx.message.content_type == "image":
        await _route_image(ctx, deps)

    # 8. NX-121: guardrails de input (clamp lungime + mascare PII + ecran injection) ÎNAINTE de
    #    triaj/agent. DUPĂ media routing → plasă unică pe orice cale (web/WA/Vision). Nu early-exit.
    _apply_input_guardrails(ctx)
