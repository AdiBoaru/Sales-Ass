"""Canonicalizare + clasificare de volatilitate pentru cache-ul semantic (G5b).

Cod PUR, determinist (fără LLM). Două funcții:
  • `canonicalize` — normalizează query-ul → `canonical_str` + `canonical_hash`.
    Colapsează paraphrase-urile pe un singur entry (ridică hit-rate) și alimentează
    stratul L1 exact prin hash.
  • `classify_volatility` — rutează query-ul: `realtime` (comandă/personal),
    `contextual` (REFERENȚIAL la setul afișat — „mai ieftin", „compară primele
    două", „dă-mi linkul", „mai arată-mi") și `dynamic` (produs/preț) NU se
    servesc/scriu în G5b-1 (precision-first: când e dubiu, bypass). Doar `static`
    (FAQ/generic) e cacheabil acum.

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
# Bypass contextual: refinare de preț RELATIVĂ la ce a văzut clientul în acest tur
# („mai ieftin decât ce-ai văzut TU"). Un astfel de răspuns e specific conversației —
# servit din cache-ul partajat (alt client, alt set afișat) = cache poisoning. Trebuie
# să ajungă mereu la agent (acolo `cheaper_intent`, agent.py:_CHEAPER_RE, îl tratează
# determinist). Oglindă normalizată (fără diacritice) a lui `_CHEAPER_RE` din agent.py;
# duplicat intenționat — cache-ul e strat inferior, nu importă din stagiul agent.
_CONTEXTUAL_RE = re.compile(
    r"\bmai\s+ieftin\w*|\bcea\s+mai\s+ieftin\w*|\bmai\s+accesibil\w*"
    r"|\bmai\s+scump\w*|\bpret\s+mai\s+mic|\bmai\s+mic\s+la\s+pret|\bbuget\s+mai\s+mic"
    r"|\bprea\s+scump\w*|\bcam\s+scump\w*"
    r"|\bcheaper\b|\bcheapest\b|\bolcsobb\w*|\blegolcsobb\w*"
)
# Bypass contextual (NX-165) — INTENȚII REFERENȚIALE la setul deja afișat: „compară
# primele două", „dă-mi linkul", „mai arată-mi". Răspunsul depinde de `displayed_products`
# din ACEA conversație, nu de textul query-ului; cache-ul e cheiat pe text (+business+locale)
# → un hit din cache-ul partajat servește comparația/linkul/pagina altui client = poisoning
# (confirmat live: „compara primele doua" hit_count=3 sărea compare_intent la cache_stage).
# Trebuie să ajungă mereu la agent, unde intențiile deterministe le tratează. Oglindă
# normalizată (fără diacritice) a `_COMPARE_RE`/`_LINK_RE`/`_MORE_RE` din agent/deterministic.py
# — duplicat intenționat (cache-ul e strat inferior, nu importă din agent). Blochează ȘI
# lookup-ul (cache.py) ȘI writeback-ul (aftercare.py) — ambele cheamă classify_volatility.
_DEIXIS_RE = re.compile(
    # compară / versus / vs / HU összehasonlít, hasonlíts (NU „compartiment": compar+t)
    r"\bcompar[aie]\w*|\bversus\b|\bvs\.?\b|\bosszehasonl\w*|\bhasonlits\w*"
    # link / „unde (o/îl/le) (pot) cumpăr|comand|găsesc" / where…buy|get|find / HU hol…veszem
    r"|\blink\w*"
    r"|\bunde\s+(?:o\s+|il\s+|le\s+)?(?:pot\s+)?(?:cumpar|comand|gasesc)\w*"
    r"|\bwhere\s+(?:can\s+i\s+|to\s+)?(?:buy|get|find)\b"
    r"|\bhol\s+(?:tudom\s+)?(?:veszem|vehetem|megvenni|megveszem)\b"
    # „mai arată-mi" / „mai multe" (paginare) / „alte opțiuni" / altele / show more / HU többet
    r"|\bmai\s+arat\w*|\bmai\s+multe\b(?!\s+\w)"
    r"|\bmai\s+multe\s+(?:produse|optiuni|variante|rezultate|exemple)\b"
    r"|\balte\s+(?:optiuni|variante|produse)\b|\bsi\s+alte\s+(?:optiuni|variante|produse)\b"
    r"|\baltele\b|\bmai\s+vreau\b"
    r"|\bshow\s+more\b|\bmore\s+(?:options|products|results)\b|\bother\s+(?:options|ones)\b"
    r"|\btobbet\b"
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
    """`'realtime' | 'contextual' | 'dynamic' | 'static'`. Precision-first:
    realtime/contextual/dynamic → bypass în G5b-1; restul (FAQ/generic) → static
    (cacheabil). `contextual` ÎNAINTE de `dynamic`: „caut ceva mai ieftin" / „compară
    primele două" sunt referențiale la setul afișat (bypass), nu query-uri cacheabile."""
    if not text:
        return "static"
    norm = _norm(text)
    if any(p in norm for p in _REALTIME):
        return "realtime"
    if _CONTEXTUAL_RE.search(norm) or _DEIXIS_RE.search(norm):
        return "contextual"
    if _BUDGET_RE.search(norm) or any(w in norm for w in _DYNAMIC_WORDS):
        return "dynamic"
    return "static"
