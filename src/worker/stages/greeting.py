"""Stagiul 4 (free layer) — Mesaj de întâmpinare la deschiderea conversației.

Când clientul deschide conversația cu un PUR salut ("salut", "bună ziua", "hi",
"szia"), botul răspunde DETERMINIST (fără LLM, principiul 2/4) cu un mesaj de
întâmpinare branded: se prezintă, întreabă ce caută, oferă câteva sugestii de
start și afișează disclaimer-ul AI (art. 50 AI Act). Comportament inspirat de
iZi/eMAG, dar cu numele asistentului nostru.

Rulează DUPĂ Gates (deci un contact blocat/handoff nu primește welcome) și DUPĂ
Limbă (`ctx.language` setat), ÎNAINTE de Cache/Triaj — un salut nu trebuie să
coste un apel de triaj (free layer). Dacă mesajul NU e un pur salut (ex. „salut,
caut o cremă" sau „caut telefon"), stagiul nu face nimic și pipeline-ul continuă.

Conținutul e CONFIGURABIL per business (`businesses.settings["welcome"]`,
principiul 9) — numele botului, dacă e activ, sugestiile și textul de întâmpinare
(`ask`) — cu fallback pe vertical/limbă.

Câmpuri TurnContext scrise aici: `ctx.reply` (early-exit la Sender).

NX-239: sub `single_brain_enabled`, stagiul e un FAST PATH EXACT — completitudinea e dovedită
prin construcție (`is_greeting` = match EXACT pe tot mesajul, deci un salut servit aici nu putea
purta altă obligație). Control plane-ul verifică oricum obligațiile; contractul e declarat mai jos.
"""

from __future__ import annotations

import unicodedata
from itertools import zip_longest
from typing import TYPE_CHECKING

from src.config import get_settings
from src.domain import vocab_examples
from src.models import BusinessConfig, TurnContext

if TYPE_CHECKING:
    from src.worker.runner import PipelineDeps


# Saluturi PURE (normalizate: lowercase, fără diacritice, doar litere+spații). RO/EN/HU.
# Conservator: dacă mesajul curățat NU e exact în set, nu e „pur salut" → lăsăm pipeline-ul
# să decidă (mai bine ratăm un salut decât să trântim welcome peste o întrebare de produs).
_GREETINGS: frozenset[str] = frozenset(
    {
        # RO
        "salut",
        "salutare",
        "buna",
        "buna ziua",
        "buna seara",
        "buna dimineata",
        "neata",
        "buna neata",
        "servus",
        "noroc",
        "hei",
        "ceau",
        "ciao",
        "hello",
        # EN
        "hi",
        "hiya",
        "hey",
        "yo",
        "good morning",
        "good evening",
        "good afternoon",
        # HU
        "szia",
        "sziasztok",
        # NX-126: „helló" (HU) normalizează la „hello" (NFKD) — deja acoperit de intrarea EN ASCII.
        # Intrarea veche „hellо" avea un „о" CHIRILIC (homoglif) ce nu se match-uia pe input ASCII →
        # ștearsă. Guard: test_greeting verifică `_norm(g)==g` pe tot setul.
        "jo napot",
        "jo napot kivanok",
        "udv",
        "udvozlom",
        "csa",
    }
)

# Șabloane de welcome per limbă (RO/HU/EN). `{bot}` = numele botului, `{shop}` = numele magazinului.
_WELCOME: dict[str, dict[str, str]] = {
    "ro": {
        "intro": "Bună! 👋 Eu sunt {bot}, asistentul tău de shopping {shop}.",
        "ask": "Cu ce te ajut azi? Poți să-mi scrii produsul, bugetul sau pentru cine cauți.",
        "try": "Poți încerca:",
        "disclaimer": "Funcționez cu inteligență artificială, așa că pot greși uneori.",
    },
    "en": {
        "intro": "Hi! 👋 I'm {bot}, your shopping assistant at {shop}.",
        "ask": "How can I help today? Tell me the product, the budget, or who it's for.",
        "try": "You can try:",
        "disclaimer": "I run on artificial intelligence, so I can be wrong sometimes.",
    },
    "hu": {
        "intro": "Szia! 👋 {bot} vagyok, a(z) {shop} vásárlási asszisztense.",
        "ask": "Miben segíthetek ma? Írd le a terméket, a kereted, vagy hogy kinek keresel.",
        "try": "Kipróbálhatod:",
        "disclaimer": "Mesterséges intelligenciával működöm, ezért néha tévedhetek.",
    },
}

# NX-273 — sugestiile de start se DERIVĂ din catalogul tenantului, nu se scriu.
#
# Aici erau patru fraze de beauty pe vertical („Caut o cremă pentru ten uscat", „Ce aveți pentru
# păr vopsit?"). Pe un magazin de electrocasnice, primul lucru pe care îl vedea clientul era o
# sugestie despre ten uscat — și nimic nu pica, fiindcă o sugestie greșită nu e o eroare.
#
# ȘABLOANELE de mai jos sunt ale LIMBII, nu ale magazinului: „Caut {x}" / „Ce aveți pentru {x}?"
# funcționează la fel pe orice raft. Ce se pune în ele vine din categorii și din nevoile
# pachetului. Un magazin fără pachet primește sugestii pe categorii; unul fără nici categorii cade
# pe cele generice, care nu numesc niciun produs.
_SUGGESTION_TEMPLATES: dict[str, dict[str, str]] = {
    "ro": {"category": "Caut {x}", "need": "Ce aveți pentru {x}?"},
    "en": {"category": "I'm looking for {x}", "need": "What do you have for {x}?"},
    "hu": {"category": "{x} keresek", "need": "Mi van {x} esetén?"},
}

_GENERIC_SUGGESTIONS: dict[str, list[str]] = {
    "ro": ["Caut un produs anume", "Vreau o recomandare", "Am o întrebare despre o comandă"],
    "en": [
        "I'm looking for a specific product",
        "I'd like a recommendation",
        "I have a question about an order",
    ],
    "hu": ["Egy konkrét terméket keresek", "Ajánlást szeretnék", "Kérdésem van egy rendelésről"],
}


# NX-239: contractul de fast-path (citit de `control_plane`): un reply de aici acoperă DOAR
# obligația de salut, iar copy-ul e authored (șabloanele de mai sus), nu LLM.
FAST_PATH_COVERS: tuple[str, ...] = ("greeting",)


def _norm(text: str) -> str:
    """Lowercase + fără diacritice + doar litere/spații, colapsate. „Bună ziua!" → „buna ziua"."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    letters = "".join(c if (c.isalpha() or c.isspace()) else " " for c in stripped)
    return " ".join(letters.split())


def is_greeting(text: str | None) -> bool:
    """True dacă mesajul e un PUR salut (după normalizare, exact în setul de saluturi)."""
    if not text:
        return False
    return _norm(text) in _GREETINGS


def _welcome_config(business: BusinessConfig) -> tuple[bool, str, object, object]:
    """(enabled, bot_name, suggestions_override, ask_override) — settings business au prioritate."""
    s = get_settings()
    bw = (business.settings or {}).get("welcome") or {}
    enabled = bool(bw.get("enabled", s.welcome_enabled))
    bot_name = (bw.get("bot_name") or s.welcome_bot_name).strip()
    return enabled, bot_name, bw.get("suggestions"), bw.get("ask")


def _suggestions(business: BusinessConfig, language: str, override: object) -> list[str]:
    """Sugestii pentru limba dată: override din settings (listă plată sau dict pe limbă),
    altfel implicit pe vertical, altfel generic. Fallback de limbă pe 'ro'."""
    if isinstance(override, dict):
        return list(override.get(language) or override.get("ro") or [])
    if isinstance(override, list):
        return [str(x) for x in override]
    derived = _derived_suggestions(business, language)
    if derived:
        return derived
    src = _GENERIC_SUGGESTIONS
    return list(src.get(language) or src.get("ro") or [])


def _derived_suggestions(business: BusinessConfig, language: str) -> list[str]:
    """Sugestii compuse din CATALOGUL tenantului: categorii + nevoi, prin șabloane de limbă.

    Ordinea e categorie, nevoie, categorie, nevoie — alternate deliberat: patru sugestii de același
    fel arată ca un meniu, iar amestecul arată că se poate cere și după raft, și după problemă.
    Selecția e cea din pachet/catalog (vezi `domain/vocab_examples`), deci stabilă între rulări.

    Fără pachet ȘI fără categorii → listă goală, iar apelantul cade pe generice. Nu inventăm."""
    templates = _SUGGESTION_TEMPLATES.get(language) or _SUGGESTION_TEMPLATES["ro"]
    pack = getattr(business, "domain_pack", None)
    examples = vocab_examples.from_pack(pack)
    # Categoriile vin din `settings.welcome.categories` — o declarație EXPLICITĂ a tenantului, nu
    # o citire de catalog: salutul e fast path și n-are voie să adauge un query pe drumul cel mai
    # scurt al conversației. Nevoile vin din pachet, unde sunt oricum.
    categories = tuple((business.settings or {}).get("welcome", {}).get("categories") or ())
    out: list[str] = []
    for category, need in zip_longest(categories[:2], examples.needs[:2]):
        if category:
            out.append(templates["category"].format(x=category))
        if need:
            out.append(templates["need"].format(x=need))
    return out


def _ask(language: str, override: object) -> str:
    """Textul de întâmpinare (`ask`) pentru limba dată: override din settings (string plat SAU
    dict pe limbă), altfel șablonul implicit pe limbă. Fallback de limbă pe 'ro'."""
    if isinstance(override, dict):
        val = override.get(language) or override.get("ro")
        if val:
            return str(val)
    elif isinstance(override, str) and override.strip():
        return override.strip()
    t = _WELCOME.get(language) or _WELCOME["ro"]
    return t["ask"]


def build_welcome(
    business: BusinessConfig,
    language: str,
    *,
    bot_name: str,
    suggestions: list[str],
    ask: str | None = None,
) -> str:
    """Compune textul de întâmpinare. Determinist, fără LLM. Limba necunoscută → 'ro'.
    `ask` = textul rezolvat (override/limbă); None → șablonul pe limbă."""
    t = _WELCOME.get(language) or _WELCOME["ro"]
    parts = [
        t["intro"].format(bot=bot_name, shop=business.name),
        "",
        ask or t["ask"],
    ]
    if suggestions:
        parts += ["", t["try"], *(f"• {s}" for s in suggestions)]
    if get_settings().ai_disclaimer_enabled:  # art. 50 AI Act — gated (decizie 2026-06-26: OFF)
        parts += ["", t["disclaimer"]]
    return "\n".join(parts)


async def greeting_stage(ctx: TurnContext, deps: PipelineDeps) -> None:  # noqa: ARG001 — free layer, fără DB
    """La un pur salut → mesaj de întâmpinare branded (early-exit). Altfel: no-op."""
    enabled, bot_name, sugg_override, ask_override = _welcome_config(ctx.business)
    if not enabled:
        return
    if not is_greeting(ctx.message.body):
        return
    suggestions = _suggestions(ctx.business, ctx.language, sugg_override)
    ask = _ask(ctx.language, ask_override)
    text = build_welcome(
        ctx.business, ctx.language, bot_name=bot_name, suggestions=suggestions, ask=ask
    )
    ctx.emit("welcome_sent", language=ctx.language)
    # cacheable=False: salutul e tratat determinist aici, nu vrem să poluăm cache-ul semantic.
    ctx.set_reply(text, cacheable=False)
