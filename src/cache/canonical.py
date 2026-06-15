"""Canonicalizare + clasificare de volatilitate pentru cache-ul semantic (G5b).

Cod PUR, determinist (fără LLM). Două funcții:
  • `canonicalize` — normalizează query-ul → `canonical_str` + `canonical_hash`.
    Colapsează paraphrase-urile pe un singur entry (ridică hit-rate) și alimentează
    stratul L1 exact prin hash.
  • `classify_volatility` — rutează query-ul: `realtime` (comandă/personal) și
    `dynamic` (produs/preț) NU se servesc/scriu în G5b-1 (precision-first: când e
    dubiu, bypass). Doar `static` (FAQ/generic) e cacheabil acum.

Vezi docs/semantic-cache-design.md §2.
"""

import hashlib
import re
import unicodedata

# Bypass realtime: comandă / date personale (răspuns specific userului, niciodată cache).
_REALTIME = (
    "comanda",
    "comenzi",
    "comanda mea",
    "awb",
    "colet",
    "coletul",
    "unde e",
    "unde este",
    "status comanda",
    "statusul comenzii",
    "factura",
    "contul meu",
    "datele mele",
)

# Bypass dynamic: depinde de date live de produs/preț (cacheabil DOAR în G5b-2 cu invalidare).
_DYNAMIC_WORDS = (
    "caut",
    "cumpar",
    "recomanzi",
    "recomandare",
    "recomanda",
    "pret",
    "preturi",
    "cat costa",
    "costa",
    "reducere",
    "oferta",
    "promotie",
    "stoc",
    "disponibil",
)
# Buget / număr + monedă („sub 80 lei", „100 ron") → query de produs.
_BUDGET_RE = re.compile(r"\d+\s*(lei|ron)")
# Pentru canonicalize: orice nu e literă/cifră/spațiu → spațiu.
_NON_WORD_RE = re.compile(r"[^a-z0-9\s]")
_WS_RE = re.compile(r"\s+")


def _norm(text: str) -> str:
    """lowercase + fără diacritice (NFKD)."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def canonicalize(text: str) -> tuple[str, str]:
    """Întoarce `(canonical_str, canonical_hash)`. Normalizare stabilă:
    lowercase + fără diacritice + punctuație→spațiu + colaps spații + strip.
    Hash-ul (sha256) e cheia stratului L1 exact."""
    norm = _norm(text or "")
    norm = _NON_WORD_RE.sub(" ", norm)
    canonical = _WS_RE.sub(" ", norm).strip()
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return canonical, digest


def classify_volatility(text: str | None) -> str:
    """`'realtime' | 'dynamic' | 'static'`. Precision-first: realtime/dynamic →
    bypass în G5b-1; restul (FAQ/generic) → static (cacheabil)."""
    if not text:
        return "static"
    norm = _norm(text)
    if any(p in norm for p in _REALTIME):
        return "realtime"
    if _BUDGET_RE.search(norm) or any(w in norm for w in _DYNAMIC_WORDS):
        return "dynamic"
    return "static"
