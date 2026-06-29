"""NX-117 — validator de text-claim PARTAJAT (pur, fără I/O).

Pattern-urile de proză neverificabilă (cifre/procente/claim-uri/superlative + stoc/disponibilitate)
trăiesc AICI, ca un singur loc canonic. `compose` (calea bogată) și `agent._valid` (calea de proză)
deleagă spre predicatele de mai jos — fără duplicare, fără drift de pattern.

Trei niveluri (calea bogată vs proză au verificări de cifre diferite):
  • `has_marketing_claim`   = procent + claim + superlativ (FĂRĂ cifre brute, FĂRĂ stoc)
  • `has_unverifiable_claim` = cifre + procente + claim + superlativ (semantica scrub_prose bogat)
  • `has_text_claim`        = claim + superlativ (calea de proză; cifrele = `_prices_ok`/
    `_bare_numbers_ok`)
  • `has_stock_claim`       = DOAR afirmația de stoc/disponibilitate. NX-118: stocul nu mai e
    respins necondiționat — e validat AVAILABILITY-aware de caller (drop doar dacă niciun produs
    retrievat nu e efectiv pe stoc), deci stă separat de `has_text_claim`.
"""

from __future__ import annotations

import re
import unicodedata

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
# NX-117/NX-118: claim de STOC / disponibilitate (RO + EN + HU). Livrarea e deja prinsă de
# `_CLAIMY` („livrare"/„zile"). Aici țintim stocul: „pe stoc", „în stoc", „disponibil", „in stock".
_STOCK_CLAIM = re.compile(
    r"\b(pe stoc|[iî]n stoc|disponibil\w*|[iî]n stock|in stock|on stock|available|k[ée]szlet\w*)\b",
    re.IGNORECASE,
)
# NX-118: NEGAȚIE / VIITOR înaintea lexemului de stoc → NU e o afirmație POZITIVĂ de disponibilitate
# curentă (ci „nu mai e pe stoc" / „revine pe stoc" / „out of stock"). Fără asta, validatorul ar
# respinge un răspuns ONEST de indisponibilitate. Fereastră mică înainte de match (~24 caractere).
_STOCK_NEG = re.compile(
    r"\b(nu|n-|fără|fara|niciun|nicio|ne-|not|no|n't|out of|no longer"
    r"|nincs|elfogyott|revine|reapare|reaprovizion\w*|[iî]napoi|back)\b",
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
    """Claim ne-numeric pentru calea de PROZĂ: superlativ / claim cuantificabil. FĂRĂ digit/pct
    (proza are deja `_prices_ok`/`_bare_numbers_ok` pe cifre). NX-118: stocul s-a MUTAT în
    `has_stock_claim` (validat availability-aware de caller), nu mai e respins aici."""
    if not text:
        return False
    return bool(_CLAIMY.search(text) or _SUPER.search(text))


def has_stock_claim(text: str | None) -> bool:
    """NX-118: textul afirmă POZITIV stoc/disponibilitate curentă („pe stoc", „disponibil",
    „in stock")? Sare peste lexemele negate / la viitor („nu mai e pe stoc", „revine pe stoc",
    „out of stock") — alea sunt răspunsuri ONESTE de indisponibilitate, nu claim-uri de validat.
    Caller-ul decide dacă e fondat (drop DOAR când niciun produs retrievat nu e efectiv pe stoc)."""
    if not text:
        return False
    for m in _STOCK_CLAIM.finditer(text):
        if _STOCK_NEG.search(text[max(0, m.start() - 24) : m.start()]):
            continue  # negat / viitor → nu e afirmație pozitivă de disponibilitate
        return True
    return False


# --------------------------------------------------------------------------- #
# P0-safety (CONV-COMMERCE) — claim MEDICAL/terapeutic nepermis (răspundere juridică, beauty).
# Detector PUR, normalizat (cu/fără diacritice). Țintește 4 categorii de claim periculos, FĂRĂ a
# prinde claim-uri cosmetice legitime („hidratează", „pentru ten uscat", „reduce ridurile") sau
# redirectarea SIGURĂ („pentru acnee, consultă un dermatolog" — fără verb terapeutic). Caller-ul
# gardează cu kill-switch (`safety_medical_guardrail_enabled`); aici e doar predicatul.
# --------------------------------------------------------------------------- #

# AFECȚIUNI medicale (nu nevoi cosmetice). „verb terapeutic + afecțiune" = claim de tratament.
_MED_CONDITION = re.compile(
    r"acne|eczem|psoriaz|rozacee|rosacea|dermatit|micoz|fungic|ciuperc|negi|veruci|wart"
    r"|herpes|melasma|vitiligo|alopeci|matreat|seboree|seborrh|celulit|vergetur|stretch mark"
    r"|cicatric|infect|inflamat|inflammat|alergi|allerg|iritat|cupero|impetigo"
)
_TREAT_VERB = re.compile(
    r"trateaz|tratament|vindec|cureaz|elimina|scapa de|combate"
    r"|treat|cure|heal|clears up|gets rid of|fights"
)
# Verdict de SIGURANȚĂ în sarcină/alăptare (ambele ordine). „recomandat" NU e cuvânt de siguranță:
# ar prinde redirectarea SIGURĂ „îți recomand să consulți medicul în sarcină" (fals-pozitiv).
_PREG_SAFE = re.compile(
    r"(sigur|safe|fara risc|poti folosi|se poate folosi)\b.{0,30}"
    r"(sarcin|insarcin|alaptar|pregnan|breastfeed|nursing)"
    r"|(sarcin|insarcin|alaptar|pregnan|breastfeed|nursing).{0,30}"
    r"(sigur|safe|fara risc|poti folosi|se poate folosi)"
)
# Garanție ABSOLUTĂ de inocuitate (nu „testat dermatologic", care e claim cosmetic uzual).
_ALLERGEN_FREE = re.compile(
    r"fara alergeni|fara niciun alergen|allergen[ -]?free|fara efecte adverse"
    r"|fara efecte secundare|no side effects|nu provoaca alergii|nu provoaca reactii"
    r"|nu da reactii alergice|100% hipoalergenic|garantat hipoalergenic"
)
# Falsă autoritate medicală (nu „consultă un medic", care e redirect SIGUR). `\w*` prinde
# inflexiunile RO (recomandat/recomandată/recomandate). NU „testat dermatologic" (claim cosmetic).
_MED_AUTHORITY = re.compile(
    r"recomandat\w* de (medic|dermatolog|specialist|doctor)|recomandat\w* medical"
    r"|prescris\w* de medic|aprobat\w* medical|dovedit\w* clinic|clinically proven"
    r"|doctor recommended|dermatologist recommended"
)


def _norm_claim(text: str) -> str:
    """Lowercase + fără diacritice (NFKD) → match robust pe text generat (cu/fără diacritice)."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def has_medical_claim(text: str | None) -> bool:
    """P0-safety: textul face un claim MEDICAL/terapeutic nepermis (răspundere juridică, beauty)?
    Patru categorii: (1) produsul TRATEAZĂ/VINDECĂ o afecțiune (verb terapeutic + afecțiune),
    (2) e „sigur în sarcină/alăptare", (3) „fără alergeni / fără efecte adverse", (4) „recomandat
    de medic/dermatolog". NU prinde claim-uri COSMETICE legitime (hidratează, ten uscat, riduri)
    și NICI redirectarea SIGURĂ („pentru acnee, consultă un dermatolog"). Pur, normalizat."""
    if not text:
        return False
    t = _norm_claim(text)
    if _TREAT_VERB.search(t) and _MED_CONDITION.search(t):
        return True
    return bool(_PREG_SAFE.search(t) or _ALLERGEN_FREE.search(t) or _MED_AUTHORITY.search(t))
