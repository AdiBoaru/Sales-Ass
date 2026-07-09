"""Stagiul 6 (logică) — Context builder: pregătește contextul conversației pentru
prompturile LLM (triaj + agent), cu BUGET impus în cod (principiul 4).

Istoricul e deja încărcat în `ctx.history` de processor (max 8 mesaje, cel mai
recent ultimul — INCLUSIV mesajul curent). Aici îl formatăm compact + bugetat, plus
blocuri de **rezumat de conversație** (`conversation_summaries`, felia 2), **profil client**
(`contacts.profile`) și **state references** (produse arătate + constrângeri, principiul 8).
Rezumatul e DOAR citit aici (din `ctx.summary`, seedat de processor) — generarea lui rulează
post-tur async (vezi `src.worker.summarizer`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from src.config import get_settings
from src.models import Direction, Message

if TYPE_CHECKING:
    from src.models import Contact, ConversationState, TurnContext


def conversation_transcript(
    history: list[Message], *, max_turns: int = 6, max_chars: int = 1200
) -> str:
    """Transcript compact „Client/Asistent" al mesajelor ANTERIOARE (fără cel curent
    — ultimul din `history` e mesajul în curs de procesare). Gol dacă nu există
    context anterior. Bugetat: ultimele `max_turns` mesaje, tăiat la `max_chars`."""
    prior = history[:-1] if history else []
    lines: list[str] = []
    for m in prior[-max_turns:]:
        body = (m.body or "").strip()
        if not body:
            continue
        role = "Client" if m.direction == Direction.INBOUND else "Asistent"
        lines.append(f"{role}: {body}")
    return "\n".join(lines)[-max_chars:]


def customer_profile_block(contact: Contact, *, max_chars: int = 300) -> str:
    """Bloc compact de profil din `contacts.profile` (+ stadiu lifecycle dacă ≠ new).
    Sare valorile goale; listele se taie la 4 elemente. Gol → "" (nimic injectat).
    Profilul NU conține PII de canal (telefonul stă în channel_identities, P12)."""
    profile = contact.profile or {}
    parts: list[str] = []
    for key, value in profile.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value[:4])
        parts.append(f"{key}: {value}")
    if contact.lifecycle and contact.lifecycle != "new":
        parts.append(f"stadiu: {contact.lifecycle}")
    if not parts:
        return ""
    return ("Profil client: " + "; ".join(parts))[:max_chars]


# NX-160: etichete prezentabile pt cheile canonice frecvente (RO). Fallback = cheia humanizată
# (snake_case → „snake case"). GENERIC — o cheie necunoscută nu crapă, doar se afișează humanizat.
_FACT_LABELS: dict[str, str] = {
    "budget_band": "Buget",
    "fav_brands": "Brand preferat",
    "restriction": "Restricție",
    "size": "Mărime",
    "use_case": "Scop",
    "recipient": "Pentru",
    "style_pref": "Stil",
    "preferred_time": "Program preferat",
    "skin_type": "Tip de ten",
    "hair_type": "Tip de păr",
    "concerns": "Nevoie",
    "vehicle_model": "Mașină",
    "fuel_type": "Combustibil",
    "part_category": "Piesă",
    "diet_preference": "Preferință alimentară",
}


def _fact_label(f: dict) -> str:
    """Eticheta afișată a unui fact: preferă `canonical_key` (label RO), apoi `raw_key`/`fact_type`
    humanizat. Nu expunem snake_case brut clientului în prompt."""
    key = f.get("canonical_key") or f.get("raw_key") or f.get("fact_type") or ""
    return _FACT_LABELS.get(key, key.replace("_", " ").strip().capitalize())


def facts_block(ctx: TurnContext, *, max_facts: int = 6, max_chars: int = 400) -> str:
    """Bloc compact de facts STABILE știute despre client (buget/brand/restricții/…), memoria
    structurată peste mesajele ieșite din istoricul de 8. Bugetat (P4). Seed de processor în
    `ctx.facts` (gol când memoria e OFF → bloc gol, degradare).

    NX-160: `ctx.facts` conține DOAR facts `visibility='inject'` (PII/medical filtrate la sursă +
    la citire), formatate cu etichete prezentabile (nu snake_case brut). Fără PII (P12)."""
    parts: list[str] = []
    for f in (ctx.facts or [])[:max_facts]:
        value = f.get("fact_value")
        if value in (None, "", [], {}) or not (
            f.get("canonical_key") or f.get("raw_key") or f.get("fact_type")
        ):
            continue
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value[:4])
        parts.append(f"{_fact_label(f)}: {value}")
    if not parts:
        return ""
    return ("Ce știu despre client: " + "; ".join(parts))[:max_chars]


def state_block(state: ConversationState, *, max_products: int = 3, max_chars: int = 600) -> str:
    """Bloc de state references: produse arătate recent (id + nume + preț, ref-uri — principiul 8)
    + constrângeri știute (buget, tip de ten…). Memoria scurtă pt follow-up coerent. Gol → "".

    R3: expunem `product_id`-ul (UUID) ca agentul să poată chema get_product_details /
    compare_products / checkout_link pe produsele DEJA arătate, fără re-căutare. Fără id în
    context, un follow-up de tip „care e cea mai bună?" pasa un id inventat → DataError pe cast."""
    lines: list[str] = []
    if state.displayed_products:
        shown = "; ".join(
            f"[{p.product_id}] {p.name} ({p.price:.2f} lei)"
            for p in state.displayed_products[:max_products]
        )
        lines.append(
            "Produse arătate recent (folosește id-ul din [] pt detalii/comparație/checkout): "
            + shown
        )
    if state.constraints:
        cons = "; ".join(
            f"{k}: {v}" for k, v in state.constraints.items() if v not in (None, "", [], {})
        )
        if cons:
            lines.append(f"Constrângeri știute: {cons}")
    return "\n".join(lines)[:max_chars]


def summary_block(ctx: TurnContext, *, max_chars: int | None = None) -> str:
    """Bloc de rezumat al conversației anterioare (felia 2), din `ctx.summary` (seedat de
    processor — fără I/O aici). Acoperă mesajele de dinaintea ultimelor 8 (care rămân în
    transcript). Bugetat (P4); gol/lipsă → "" (degradare: doar ultimele 8)."""
    text = (ctx.summary or "").strip()
    if not text:
        return ""
    cap = max_chars if max_chars is not None else get_settings().summary_max_chars
    return ("Rezumat conversație anterioară: " + text)[:cap]


def context_blocks(ctx: TurnContext) -> str:
    """Unește blocurile ne-goale de context (rezumat + profil + state) pentru prompturile
    triaj/agent. Ordine CRONOLOGICĂ: rezumatul (fundalul vechi) ÎNAINTEA profilului/state-ului;
    transcriptul ultimelor 8 e concatenat downstream (în triage/agent), deci rezumat→…→recent.
    Stă în mesajul USER (dinamic), nu în system — promptul static rămâne byte-identic (prompt
    caching neatins). Gol → "" (nimic de adăugat)."""
    blocks = [
        summary_block(ctx),
        customer_profile_block(ctx.contact),
        facts_block(ctx),  # NX-148: memorie structurată (după profil, înainte de state)
        state_block(ctx.state),
    ]
    return "\n".join(b for b in blocks if b)


def search_query(history: list[Message], current: str, *, n: int = 2) -> str:
    """Textul pentru căutare = ultimele `n` mesaje ale CLIENTULUI (inclusiv cel
    curent), ca follow-up-urile scurte („ceva mai ieftin", „și pentru păr") să
    caute în contextul corect, nu izolat. Fallback: mesajul curent."""
    users = [
        (m.body or "").strip()
        for m in history
        if m.direction == Direction.INBOUND and (m.body or "").strip()
    ]
    if not users:
        return current.strip()
    return " ".join(users[-n:])
