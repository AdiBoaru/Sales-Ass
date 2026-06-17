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
principiul 9) — numele botului, dacă e activ, sugestiile — cu fallback pe vertical.

Câmpuri TurnContext scrise aici: `ctx.reply` (early-exit la Sender).
"""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING

from src.config import get_settings
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
        "hellо",
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
        "ask": "Spune-mi ce cauți — un produs anume, o idee de cadou sau un sfat de alegere.",
        "try": "Poți încerca:",
        "disclaimer": "Funcționez cu inteligență artificială, așa că pot greși uneori.",
    },
    "en": {
        "intro": "Hi! 👋 I'm {bot}, your shopping assistant at {shop}.",
        "ask": "Tell me what you need — a specific product, a gift idea, or advice on a choice.",
        "try": "You can try:",
        "disclaimer": "I run on artificial intelligence, so I can be wrong sometimes.",
    },
    "hu": {
        "intro": "Szia! 👋 {bot} vagyok, a(z) {shop} vásárlási asszisztense.",
        "ask": "Mondd el, mit keresel — terméket, ajándékötletet vagy tanácsot a választáshoz.",
        "try": "Kipróbálhatod:",
        "disclaimer": "Mesterséges intelligenciával működöm, ezért néha tévedhetek.",
    },
}

# Sugestii implicite pe VERTICAL (nu hardcodate global — multi-tenant). Override din
# settings["welcome"]["suggestions"]. Cheie pe limbă; fallback pe 'ro'.
_DEFAULT_SUGGESTIONS: dict[str, dict[str, list[str]]] = {
    "beauty": {
        "ro": [
            "Caut o cremă pentru ten uscat",
            "Recomandă-mi un parfum sub 200 lei",
            "Ce aveți pentru păr vopsit?",
            "Idei de cadou pentru ea",
        ],
        "en": [
            "I'm looking for a cream for dry skin",
            "Recommend a perfume under 200 lei",
            "What do you have for colored hair?",
            "Gift ideas for her",
        ],
        "hu": [
            "Krémet keresek száraz bőrre",
            "Ajánlj egy parfümöt 200 lej alatt",
            "Mi van festett hajra?",
            "Ajándékötletek neki",
        ],
    },
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


def _welcome_config(business: BusinessConfig) -> tuple[bool, str, object]:
    """(enabled, bot_name, suggestions_override) — settings business au prioritate peste config."""
    s = get_settings()
    bw = (business.settings or {}).get("welcome") or {}
    enabled = bool(bw.get("enabled", s.welcome_enabled))
    bot_name = (bw.get("bot_name") or s.welcome_bot_name).strip()
    return enabled, bot_name, bw.get("suggestions")


def _suggestions(business: BusinessConfig, language: str, override: object) -> list[str]:
    """Sugestii pentru limba dată: override din settings (listă plată sau dict pe limbă),
    altfel implicit pe vertical, altfel generic. Fallback de limbă pe 'ro'."""
    if isinstance(override, dict):
        return list(override.get(language) or override.get("ro") or [])
    if isinstance(override, list):
        return [str(x) for x in override]
    by_vertical = _DEFAULT_SUGGESTIONS.get(business.vertical)
    src = by_vertical or _GENERIC_SUGGESTIONS
    return list(src.get(language) or src.get("ro") or [])


def build_welcome(
    business: BusinessConfig, language: str, *, bot_name: str, suggestions: list[str]
) -> str:
    """Compune textul de întâmpinare. Determinist, fără LLM. Limba necunoscută → 'ro'."""
    t = _WELCOME.get(language) or _WELCOME["ro"]
    parts = [
        t["intro"].format(bot=bot_name, shop=business.name),
        "",
        t["ask"],
    ]
    if suggestions:
        parts += ["", t["try"], *(f"• {s}" for s in suggestions)]
    parts += ["", t["disclaimer"]]
    return "\n".join(parts)


async def greeting_stage(ctx: TurnContext, deps: PipelineDeps) -> None:  # noqa: ARG001 — free layer, fără DB
    """La un pur salut → mesaj de întâmpinare branded (early-exit). Altfel: no-op."""
    enabled, bot_name, override = _welcome_config(ctx.business)
    if not enabled:
        return
    if not is_greeting(ctx.message.body):
        return
    suggestions = _suggestions(ctx.business, ctx.language, override)
    text = build_welcome(ctx.business, ctx.language, bot_name=bot_name, suggestions=suggestions)
    ctx.emit("welcome_sent", language=ctx.language)
    # cacheable=False: salutul e tratat determinist aici, nu vrem să poluăm cache-ul semantic.
    ctx.set_reply(text, cacheable=False)
