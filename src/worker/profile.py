"""Extractor de profil + lead_score (NX-88) — pasul prin care botul „învață" clientul.

Logică PURĂ (testabilă fără DB/Redis), în aceeași familie cu `summarizer.py`: după un tur,
un singur apel NANO (model_triage) pe istoricul scurt extrage semnale de profil + de lead.
Codul determinist preia de aici:
  • filtrează `profile_patch` pe o WHITELIST de chei per vertical → modelul nu poate scrie chei
    arbitrare (sau PII) în `contacts.profile`; cheile necunoscute se aruncă (semnal pentru NX-43);
  • calculează `lead_score` dintr-o FORMULĂ transparentă (NU numărul inventat de LLM) → scor
    explicabil și stabil între versiuni de model (intră în contracte / export CRM, NX-31).

Orchestrarea hook-ului (scriere DB, cost guard, analytics) stă în
`processor._extract_profile_and_score` — modulul ăsta nu atinge DB/Redis.

LLM = NANO prin adaptorul unic `classify_json` (JSON object mode), al treilea apel acceptat
post-tur (principiul 2, ca summarizer-ul). PII (principiul 12): redactare defensivă a
secvențelor tip telefon înainte de model, ÎN PLUS de instrucțiunea anti-PII din system prompt.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.models import Direction

if TYPE_CHECKING:
    from src.models import Message

log = logging.getLogger(__name__)

# --- Whitelist de chei de profil per vertical -------------------------------------------
# v1 MINIMĂ (NX-88). Lista bogată per vertical (mărimi, atribute fine...) se mută în taxonomie
# = NX-43 (dependență de date, nu blochează). Cheile de contact (telefon/email/nume/adresă) NU
# apar AICI prin construcție → modelul nu le poate scrie în DB, oricât ar încerca.
_WHITELIST: dict[str, frozenset[str]] = {
    "beauty": frozenset(
        {"skin_type", "hair_type", "concerns", "budget_band", "fav_brands", "age_band", "gender"}
    ),
    "hvac": frozenset({"property_type", "area_band", "fuel_type", "budget_band", "brand_pref"}),
    "auto": frozenset(
        {"vehicle_make", "vehicle_model", "vehicle_year_band", "part_category", "budget_band"}
    ),
    "salon": frozenset({"preferred_service", "hair_type", "preferred_stylist", "budget_band"}),
}
# Minim vertical-agnostic (verticale necunoscute / ecommerce generic).
_WHITELIST_DEFAULT = frozenset({"budget_band", "fav_brands", "concerns"})

# Igienă: o cheie de profil e un scalar SCURT (nu eseuri / obiecte / liste).
_MAX_VALUE_LEN = 120
_MAX_KEYS = 16

# Secvențe tip telefon (E.164-ish): +? urmat de cifre/spații/cratime, ≥8 cifre. PII (P12) —
# telefonul trăiește în channel_identities, nu trebuie să ajungă în promptul extractorului.
_PHONE_RE = re.compile(r"\+?\d[\d\s\-]{6,}\d")


def _redact_pii(text: str) -> str:
    """Înlocuiește secvențele tip telefon cu „***" (best-effort, P12)."""
    return _PHONE_RE.sub("***", text or "")


# --- Contractul de output al modelului (Pydantic, validare strictă a JSON-ului nano) -----


class LeadSignals(BaseModel):
    """Semnale de cumpărare extrase de nano. Booleeni + o etapă — codul calculează scorul final,
    modelul NU dă numărul (vezi `compute_lead_score`). `extra=ignore`: chei în plus de la model
    nu rup validarea."""

    model_config = ConfigDict(extra="ignore")

    buying_stage: str = "browsing"  # browsing | narrowing | comparing | ready_to_buy
    has_budget: bool = False
    asked_price: bool = False
    mentioned_product: bool = False
    ready_to_buy: bool = False


class FactCandidate(BaseModel):
    """Un fact structurat CANDIDAT extras LIBER de model. NX-160: `raw_key` = cheia liberă a
    modelului; `canonical_key` = slotul canonic sugerat (verificat/rescris determinist aval de
    `memory.process_facts`). `fact_type` rămâne acceptat ca alias backcompat (NX-148) — dacă
    modelul emite `fact_type` în loc de `raw_key`, îl folosim ca `raw_key`. `fact_value` liber
    (`Any`) — o valoare proastă nu invalidează restul; confidence 0..1 (clampat aval)."""

    model_config = ConfigDict(extra="ignore")

    raw_key: str | None = None
    canonical_key: str | None = None
    fact_type: str | None = None  # alias backcompat (NX-148)
    fact_value: Any = None
    confidence: float = 0.5
    # NX-160: ref-ul mesajului-sursă (m1/m2/… din transcriptul numerotat) unde clientul a spus
    # faptul → mapat la id-ul real de processor (trasabilitate reală, nu „mesajul curent").
    source_ref: str | None = None

    @property
    def key(self) -> str | None:
        """Cheia liberă efectivă: `raw_key` sau `fact_type` (backcompat)."""
        return self.raw_key or self.fact_type


class ProfileDelta(BaseModel):
    """Output-ul extractorului. `profile_patch` = chei CANDIDATE (filtrate apoi pe whitelist);
    tipăm valorile liber (`Any`) ca o singură valoare proastă să nu invalideze tot patch-ul —
    `filter_profile_patch` aruncă non-scalarii. `facts` (NX-148) = memorie structurată candidată."""

    model_config = ConfigDict(extra="ignore")

    profile_patch: dict[str, Any] = Field(default_factory=dict)
    lead_signals: LeadSignals = Field(default_factory=LeadSignals)
    facts: list[FactCandidate] = Field(default_factory=list)


_SYSTEM = """Ești un extractor de profil pentru un asistent de vânzări dintr-un magazin online.
Primești o conversație scurtă și extragi DOAR fapte STABILE despre client + semnale de cumpărare.
Răspunzi NUMAI cu JSON, fără text în plus.

Format:
{"profile_patch": {<cheie_snake_case>: <valoare scalară scurtă>, ...},
 "lead_signals": {"buying_stage": "browsing|narrowing|comparing|ready_to_buy",
                  "has_budget": <bool>, "asked_price": <bool>,
                  "mentioned_product": <bool>, "ready_to_buy": <bool>}}

Reguli:
- profile_patch: DOAR atribute pe care clientul le-a declarat EXPLICIT despre el sau nevoia lui
  (ex. skin_type, budget_band, fav_brands, concerns). Chei în snake_case ENGLEZĂ, valori scurte.
  Dacă nimic clar → {} (obiect gol). NU ghici, NU completa din context general.
- NICIODATĂ date personale de contact (telefon, email, nume, adresă) — nici cheie, nici valoare.
- buying_stage: cât de avansat e clientul (browsing = doar se uită; ready_to_buy = gata de comandă).
- Nu inventa produse, prețuri sau preferințe nedeclarate."""


# NX-148: varianta CU facts — folosită DOAR când `conversation_facts_enabled` (include_facts=True).
# Cu flag-ul OFF folosim `_SYSTEM` de bază → modelul NU cere/emite facts (flag-ul e complet OFF,
# nu doar la persistare — nu arde tokeni pe ceva ce feature-flag-ul promite că e oprit).
_SYSTEM_WITH_FACTS = """\
Ești un extractor de memorie pentru un asistent de vânzări dintr-un magazin online (ORICE
domeniu: beauty, auto, restaurant, servicii, retail). Primești o conversație scurtă și extragi
DOAR fapte STABILE despre client + semnale de cumpărare. Răspunzi NUMAI cu JSON, fără text în plus.

Format:
{"profile_patch": {<cheie_snake_case>: <valoare scalară scurtă>, ...},
 "lead_signals": {"buying_stage": "browsing|narrowing|comparing|ready_to_buy",
                  "has_budget": <bool>, "asked_price": <bool>,
                  "mentioned_product": <bool>, "ready_to_buy": <bool>},
 "facts": [{"raw_key": <snake_case>, "canonical_key": <cheie canonică sau null>,
            "fact_value": <valoare scurtă>, "confidence": <0..1>,
            "source_ref": <ref-ul mesajului m1/m2/… unde clientul a spus faptul>}, ...]}

Reguli:
- profile_patch: DOAR atribute pe care clientul le-a declarat EXPLICIT despre el sau nevoia lui.
  Chei în snake_case ENGLEZĂ, valori scurte. Dacă nimic clar → {}. NU ghici.
- facts: fapte STABILE, reutilizabile despre client (buget, brand preferat, restricții/preferințe,
  mărime, mașină/model, scop, program). `confidence` = cât de sigur ești (0..1). Nimic → [].
  REGULĂ CHEIE: `raw_key` = DOAR TIPUL faptului (categoria), în snake_case ENGLEZĂ scurt —
  NICIODATĂ valoarea în cheie. Valoarea concretă merge DOAR în `fact_value`.
    ✓ {"raw_key":"skin_type","fact_value":"sensibil"}   NU {"raw_key":"skin_type_sensitive",...}
    ✓ {"raw_key":"budget","fact_value":"100 lei"}       NU {"raw_key":"budget_amount_lei",...}
    ✓ {"raw_key":"restriction","fact_value":"fără parfum"} NU {"raw_key":"restriction_no_fragrance"}
  Dacă `raw_key` se potrivește cu una din CHEILE CANONICE oferite în mesaj, pune-o EXACT în
  `canonical_key`; altfel `canonical_key=null`. Preferă cheile canonice.
  `source_ref` = ref-ul (m1/m2/…) al mesajului CLIENTULUI din care reiese faptul; dacă nu ești
  sigur, omite-l.
- NICIODATĂ date de contact (telefon, email, nume, adresă) sau financiare (card, IBAN, CNP) — nici
  cheie, nici valoare.
- O CONDIȚIE medicală (diabet, sarcină, boală, alergie ca diagnostic) NU e memorie de preferință —
  NU o extrage ca fapt. DAR o preferință comercială derivată („fără zahăr", „fără gluten") formulată
  de client ca cerință de produs ESTE un fapt valid (raw_key=restriction).
- buying_stage: cât de avansat e clientul (browsing = doar se uită; ready_to_buy = gata de comandă).
- Nu inventa produse, prețuri sau preferințe nedeclarate."""


def _numbered_transcript(history: list[Message]) -> tuple[str, dict[str, str]]:
    """Conversația scurtă ca text (PII redactat), cu fiecare linie PREFIXATĂ de un ref `[m{i}]`
    (Client/Asistent). Întoarce (text, ref_map) unde `ref_map[m{i}] = message.id` (doar mesajele
    cu id real din DB). Refs-urile permit modelului să indice mesajul-SURSĂ al fiecărui fapt
    (`source_ref`), pe care processorul îl mapează la id-ul real (NX-160). Numerotarea sare
    mesajele goale — IDENTIC în text și în ref_map (același iterator), deci refs-urile coincid."""
    lines: list[str] = []
    ref_map: dict[str, str] = {}
    i = 0
    for m in history:
        body = _redact_pii((m.body or "").strip())
        if not body:
            continue
        i += 1
        ref = f"m{i}"
        role = "Client" if m.direction == Direction.INBOUND else "Asistent"
        lines.append(f"[{ref}] {role}: {body}")
        mid = getattr(m, "id", None)
        if mid:
            ref_map[ref] = mid
    return "\n".join(lines), ref_map


def build_ref_map(history: list[Message]) -> dict[str, str]:
    """`ref_map` (m{i} → message.id) pentru istoricul dat — folosit de processor ca să mapeze
    `source_ref`-ul întors de model la id-ul real. Aceeași numerotare ca `_numbered_transcript`
    (un singur iterator) → refs-urile din prompt și cele din map coincid."""
    return _numbered_transcript(history)[1]


def _transcript(history: list[Message]) -> str:
    """Doar textul numerotat (compat cu apelurile care nu au nevoie de ref_map)."""
    return _numbered_transcript(history)[0]


def build_profile_prompt(
    history: list[Message],
    message: Any,
    language: str,
    *,
    include_facts: bool = True,
    canonical_keys: list[str] | None = None,
) -> tuple[str, str]:
    """(system, user) pentru apelul de extracție. `include_facts=False` (memoria OFF) →
    promptul NU menționează facts (system de bază + user fără „facts") → modelul nu emite/nu
    arde tokeni pe ele. User = istoric scurt redactat + ultimul mesaj + (NX-160) cheile canonice
    disponibile pentru businessul curent (P9 — din DomainPack, nu hardcodat). Cheile stau în USER
    (dinamic), NU în system → prefixul static rămâne byte-identic (prompt caching)."""
    transcript = _transcript(history)
    latest = _redact_pii((getattr(message, "body", None) or "").strip())
    ask = (
        "profile_patch + lead_signals + facts" if include_facts else "profile_patch + lead_signals"
    )
    keys_line = ""
    if include_facts and canonical_keys:
        keys_line = (
            "Chei canonice disponibile (folosește-le în canonical_key dacă se potrivesc): "
            + ", ".join(canonical_keys)
            + "\n\n"
        )
    user = (
        f"Limba clientului: {language}\n"
        f"Conversație recentă:\n{transcript or '(fără istoric)'}\n\n"
        f"Ultimul mesaj al clientului: {latest or '(gol)'}\n\n"
        f"{keys_line}"
        f"Extrage {ask} ca JSON."
    )
    return (_SYSTEM_WITH_FACTS if include_facts else _SYSTEM), user


async def extract_profile(
    llm: Any,
    history: list[Message],
    message: Any,
    language: str,
    *,
    include_facts: bool = True,
    canonical_keys: list[str] | None = None,
) -> ProfileDelta | None:
    """Apel NANO (JSON mode) → `ProfileDelta`. `None` la orice fail (parse/validare/API) = fail-soft
    (hook-ul nu scrie nimic). Nu cheamă modelul dacă nu există conținut de analizat (zero cost).
    `include_facts=False` (memoria OFF) → nu se cer facts în prompt (feature-flag complet OFF)."""
    if not history and not (getattr(message, "body", None) or "").strip():
        return None
    system, user = build_profile_prompt(
        history, message, language, include_facts=include_facts, canonical_keys=canonical_keys
    )
    try:
        raw = await llm.classify_json(system, user, model=llm.model_triage)
        return ProfileDelta.model_validate(raw)
    except (ValidationError, ValueError, KeyError, TypeError) as e:
        log.warning("extractor profil: output invalid (%s) → deltă goală", type(e).__name__)
        return None
    except Exception as e:  # noqa: BLE001 — eroare API/rețea → fail-soft, turul a răspuns deja
        log.warning("extractor profil: apel LLM eșuat (%s)", type(e).__name__)
        return None


def _safe_drop_key(raw_key: Any) -> str:
    """Cheia RESPINSĂ ajunge în analytics (`profile_key_dropped`, semnal NX-43). Whitelist-ul
    oprește scrierea în DB, dar modelul ar putea pune PII într-o POZIȚIE de cheie
    (`{"client_0712...": true}`) — deci redactăm + truncăm înainte de event (P12: niciun PII în
    analytics). NX-43 are nevoie de CARE chei propune modelul, nu de textul lor literal."""
    return _redact_pii(str(raw_key).strip())[:64]


def filter_profile_patch(patch: dict[str, Any], vertical: str) -> tuple[dict[str, Any], list[str]]:
    """Păstrează DOAR cheile din whitelist-ul verticalului, cu valori scalare scurte. Restul →
    aruncate (lista `dropped` = semnal pentru NX-43, redactat de PII). Modelul nu poate scrie chei
    arbitrare în DB (P-cheie). Cheile se normalizează (strip + lower); valorile se trim-uiesc.
    """
    allowed = _WHITELIST.get(vertical, _WHITELIST_DEFAULT)
    kept: dict[str, Any] = {}
    dropped: list[str] = []
    for raw_key, value in (patch or {}).items():
        key = str(raw_key).strip().lower()
        if key not in allowed or len(kept) >= _MAX_KEYS:
            dropped.append(_safe_drop_key(raw_key))
            continue
        if isinstance(value, (int, float)):  # include bool (subclasă de int)
            kept[key] = value
        elif isinstance(value, str):
            v = value.strip()
            if v and len(v) <= _MAX_VALUE_LEN:
                kept[key] = v
            else:
                dropped.append(_safe_drop_key(raw_key))  # gol sau prea lung
        else:
            dropped.append(_safe_drop_key(raw_key))  # listă / obiect / None → nu e scalar
    return kept, dropped


# Etapa de cumpărare → bază de scor. Restul semnalelor adaugă ponderi (vezi compute_lead_score).
_STAGE_BASE: dict[str, int] = {
    "browsing": 10,
    "narrowing": 35,
    "comparing": 55,
    "ready_to_buy": 80,
}


def _engaged_products(ctx: Any) -> bool:
    """Apropiere de checkout: turul a pus produse CONCRETE în față (reply sau state)."""
    reply = getattr(ctx, "reply", None)
    if reply is not None and getattr(reply, "products", None):
        return True
    state = getattr(ctx, "state", None)
    return bool(getattr(state, "displayed_products", None))


def compute_lead_score(signals: LeadSignals, ctx: Any) -> float:
    """Scor 0..100 dintr-o FORMULĂ deterministă (NU numărul LLM-ului). Transparent → explicabil
    și stabil între versiuni de model. Bază pe etapa de cumpărare + ponderi pe semnale + un bonus
    pentru produse concrete afișate în tur (semnal de cod, nu de model). Clampat 0..100."""
    score: float = _STAGE_BASE.get(signals.buying_stage, 10)
    if signals.has_budget:
        score += 12
    if signals.asked_price:
        score += 8
    if signals.mentioned_product:
        score += 5
    if signals.ready_to_buy:
        score += 20
    if _engaged_products(ctx):
        score += 5
    return float(max(0.0, min(100.0, score)))
