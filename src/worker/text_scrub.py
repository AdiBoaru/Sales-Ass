"""NX-117 — validator de text-claim PARTAJAT (pur, fără I/O).

Pattern-urile de proză neverificabilă (cifre/procente/claim-uri/superlative + stoc/disponibilitate)
trăiesc AICI, ca un singur loc canonic. `compose` (calea bogată) și `agent._valid` (calea de proză)
deleagă spre predicatele de mai jos — fără duplicare, fără drift de pattern.

Trei niveluri (calea bogată vs proză au verificări de cifre diferite):
  • `has_marketing_claim`   = procent + claim + superlativ (FĂRĂ cifre brute, FĂRĂ stoc)
  • `has_unverifiable_claim` = cifre + procente + claim + superlativ (semantica scrub_prose bogat)
  • `has_text_claim`        = claim + superlativ + STOC/disponibilitate (calea de proză, care are
    deja `_prices_ok`/`_bare_numbers_ok` pe cifre → fără digit/pct aici)
"""

from __future__ import annotations

import re

_DIGIT = re.compile(r"\d")
_PCT = re.compile(r"%|\bla sută\b", re.IGNORECASE)
_CLAIMY = re.compile(
    r"\b(stele|stea|recenzii|review|rating|zile|ore|livrare|reducere|garan)\w*", re.IGNORECASE
)
_SUPER = re.compile(
    r"\b(cel mai|cea mai|cei mai|cele mai|nr\.?\s*1|#\s*1|best\s*seller"
    r"|recomandat de specialiști)\b",
    re.IGNORECASE,
)
# NX-117: claim de STOC / disponibilitate (RO + EN + HU). Livrarea e deja prinsă de `_CLAIMY`
# („livrare"/„zile"). Aici țintim stocul: „pe stoc", „în stoc", „disponibil", „in stock", „készlet".
_STOCK_CLAIM = re.compile(
    r"\b(pe stoc|[iî]n stoc|disponibil\w*|[iî]n stock|in stock|on stock|available|k[ée]szlet\w*)\b",
    re.IGNORECASE,
)


def has_marketing_claim(text: str | None) -> bool:
    """Procent / claim cuantificabil / superlativ — FĂRĂ cifre brute, FĂRĂ stoc. Folosit de
    `compose.scrub_intro` (permite cifrele clientului, respinge claim-urile de marketing)."""
    if not text:
        return False
    return bool(_PCT.search(text) or _CLAIMY.search(text) or _SUPER.search(text))


def has_unverifiable_claim(text: str | None) -> bool:
    """Toată proza neverificabilă a căii BOGATE: cifre + procente + claim + superlativ. Paritate
    EXACTĂ cu vechiul `scrub_prose` (NU include stoc → zero regresie pe calea bogată)."""
    if not text:
        return False
    return bool(_DIGIT.search(text)) or has_marketing_claim(text)


def has_text_claim(text: str | None) -> bool:
    """Claim ne-numeric pentru calea de PROZĂ: superlativ / claim cuantificabil / STOC. FĂRĂ
    digit/pct (proza are deja `_prices_ok`/`_bare_numbers_ok` pe cifre)."""
    if not text:
        return False
    return bool(_CLAIMY.search(text) or _SUPER.search(text) or _STOCK_CLAIM.search(text))
