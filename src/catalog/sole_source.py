"""Regulile de citire a sursei SOLE. PUR: zero I/O, zero ceas, zero DB.

De ce trăiește aici și nu în `scripts/import_sole.py`: corectitudinea importului nu stă în
bucla care scrie rânduri, ci în deciziile de clasificare și parsare. Alea trebuie testabile
fără SQLite, fără Postgres și fără să rulezi un import de 2.767 de produse ca să afli că ai
clasificat greșit un badge.

Principiul întregului import: **nimic nu se exclude la scriere**. Funcțiile de aici nu aruncă
date; ele ETICHETEAZĂ. Ce se rostește către client e decizie de citire, nu de import.

Contract cu apelantul:
  • funcțiile întorc `None` pentru „nu știu", niciodată o valoare implicită plauzibilă;
  • orice text neparsabil se păstrează în `raw`, ca să se poată re-extrage altfel mai târziu;
  • `business_id` nu apare nicăieri: e server-owned, îl injectează importerul.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# ============================================================================
# Secțiuni: cele 17 chei din `sections_json`, în două familii
# ============================================================================


@dataclass(frozen=True, slots=True)
class SectionClass:
    """Cum se așază o cheie din sursă în `product_sections`."""

    kind: str  # taxonomia NOASTRĂ de secțiuni, stabilă
    source: str  # 'merchant_pdp' | 'aura'
    voice: str  # 'brand' | 'assistant'
    # Rolul din `product_evidence_chunks` (vocabular ÎNCHIS, contractul NX-205 din
    # `src/domain/contracts.py`). `None` = textul NU devine evidence citabil.
    evidence_role: str | None


# F1 — fapte de PDP, scrise de magazin sau producător. Diacritice: 0-5%.
#
# Al doilea element e rolul de evidence. Maparea nu e cosmetică: `role` decide ce poate CITA
# `grounding_guard` când confruntă o afirmație. Compoziția INCI e `ingredient`, nu `benefit`,
# fiindcă o listă de ingrediente nu susține o afirmație de beneficiu; condițiile de păstrare
# sunt `policy`, nu `usage`, fiindcă „se ține la 5-25°C" nu e o instrucțiune de aplicare.
_F1: dict[str, tuple[str, str]] = {
    "Descriere": ("description", "benefit"),
    "Compozitie": ("composition", "ingredient"),
    "Ingrediente-cheie": ("key_ingredients", "ingredient"),
    "Cum se foloseste": ("usage", "usage"),
    "Depozitare si valabilitate": ("storage", "policy"),
    "Cand se utilizeaza": ("routine_time", "usage"),
    "Cantitate Recomandata De Aplicare": ("dosage", "usage"),
}

# F2 — proza generată de asistentul AURA al SOLE. Diacritice: 99-100%.
# `evidence_role=None` peste tot, deliberat: e text derivat de ALTCINEVA din fapte pe care nu
# le avem. Ca evidence ar deveni sursă citabilă pentru `grounding_guard`, care ar confirma
# afirmații fără să le poată verifica. Structura se extrage separat, în `product_derived_signals`.
_F2: dict[str, tuple[str, None]] = {
    "Cui i se potrivește": ("fit", None),
    "Când s-ar putea să nu fie alegerea potrivită": ("anti_fit", None),
    "Ce problemă rezolvă": ("problem", None),
    "Cum se compară cu alte produse": ("comparison", None),
    "Când apare ca recomandare": ("recommendation_trigger", None),
    "Întrebări la care răspunde acest produs": ("questions", None),
    "Cum se integreaza in rutina ta": ("routine_integration", None),
    "Pe scurt despre acest produs": ("summary", None),
    "Pentru ce este acest produs": ("purpose", None),
    "Recomandare AURA": ("editorial", None),
}

SECTION_KEYS: frozenset[str] = frozenset(_F1) | frozenset(_F2)


def classify_section(source_key: str) -> SectionClass | None:
    """Cheia exactă din `sections_json` → clasificarea noastră. `None` = cheie necunoscută.

    `None` nu înseamnă „aruncă". Importerul scrie oricum secțiunea, cu `kind='unclassified'`,
    și ridică o alertă în `catalog_quality_alerts`: o cheie nouă la sursă e un semnal, nu un
    caz de ignorat în tăcere.
    """
    if source_key in _F1:
        kind, role = _F1[source_key]
        return SectionClass(kind=kind, source="merchant_pdp", voice="brand", evidence_role=role)
    if source_key in _F2:
        kind, role = _F2[source_key]
        return SectionClass(kind=kind, source="aura", voice="assistant", evidence_role=role)
    return None


# ============================================================================
# Badge-uri: 111 valori distincte, șase categorii
# ============================================================================

_BADGE_FACT = {
    "AM PM dimineata si seara",
    "AM dimineata",
    "PM seara",
    "Aprobat pentru copii",
}
_BADGE_CLAIM = {"Eficienta demonstrata stintific"}
_BADGE_COMPLIANCE = {"CPNP"}
_BADGE_MERCHANT = {"Cadou", "SOLE Exclusiv"}


def classify_badge(label: str) -> str:
    """Eticheta brută → `product_badges.kind`. Acoperă toate cele 111 valori din sursă.

    Distincția care contează: `merchant_marketing` sunt afirmații despre MAGAZINUL SOLE, nu
    despre produs („SOLE.ro este magazin oficial al brandului X"). Se importă, fiindcă importul
    e lossless, dar un bot care le rostește face afirmații despre alt magazin.

    `claim` e separat de `fact` fiindcă „Eficienta demonstrata stintific" nu vine cu un studiu
    citabil. Ca `fact` ar deveni argument de vânzare; ca `claim` rămâne informație păstrată.
    """
    text = label.strip()
    if text in _BADGE_COMPLIANCE:
        return "compliance"
    if text in _BADGE_CLAIM:
        return "claim"
    if text in _BADGE_FACT or text.startswith("Protectie UV"):
        return "fact"
    if text in _BADGE_MERCHANT or text.startswith("SOLE"):
        return "merchant_marketing"
    return "other"


# ============================================================================
# Momentul din rutină (AM / PM)
# ============================================================================

_ROUTINE = {
    "AM/PM": "am_pm",
    "AM": "am",
    "PM": "pm",
}


def parse_routine_time(text: str | None) -> str | None:
    """„AM/PM - Include produsul in rutina ta…" → `am_pm` | `am` | `pm` | None.

    Sursa are exact patru valori, dintre care una goală (374 de produse). Prefixul dinaintea
    liniuței e singurul purtător de informație; restul e aceeași frază peste tot.
    """
    if not text or not text.strip():
        return None
    head = text.split("-", 1)[0].strip().upper()
    return _ROUTINE.get(head)


# ============================================================================
# Depozitare: data de durabilitate + temperatura. NU PAO.
# ============================================================================


@dataclass(frozen=True, slots=True)
class StorageFacts:
    min_durability_date: date | None = None
    temp_min_c: Decimal | None = None
    temp_max_c: Decimal | None = None
    # Rămâne mereu None. Vezi `parse_storage`.
    pao_months: None = None


_RE_DURABILITY = re.compile(r":\s*(\d{2})\.(\d{2})\.(\d{4})")
_RE_TEMP = re.compile(r"între\s*(-?\d+(?:[.,]\d+)?)\s*°C\s*și\s*(-?\d+(?:[.,]\d+)?)\s*°C", re.I)


def parse_storage(text: str | None) -> StorageFacts:
    """Secțiunea „Depozitare si valabilitate" → fapte structurate.

    **PAO nu se extrage, deliberat.** Linia din sursă e:
        „Perioada de utilizare după deschidere: conform simbolului PAO înscris pe ambalaj —
         de exemplu, 12M, 24M sau 36M"
    Textul e IDENTIC pe toate cele 2.251 de produse care îl au: e boilerplate cu valori DE
    EXEMPLU. Un parser care ia „12" de acolo fabrică un fapt pentru 2.251 de produse, iar
    validatorul îl confirmă fiindcă cifra ajunge în baza noastră. Cel mai periculos tip de
    eroare: una care trece toate porțile.
    """
    if not text:
        return StorageFacts()

    dob: date | None = None
    if m := _RE_DURABILITY.search(text):
        try:
            dob = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            dob = None  # 31.02.2029 și altele; UNKNOWN, nu o dată apropiată

    lo = hi = None
    if m := _RE_TEMP.search(text):
        try:
            lo = Decimal(m.group(1).replace(",", "."))
            hi = Decimal(m.group(2).replace(",", "."))
        except InvalidOperation:
            lo = hi = None
        else:
            if hi < lo:
                lo = hi = None  # interval inversat = sursă stricată, nu îl „reparăm"

    return StorageFacts(min_durability_date=dob, temp_min_c=lo, temp_max_c=hi)


# ============================================================================
# Volum → cantitate netă
# ============================================================================

_UNITS = {
    "ml": "ml",
    "l": "l",
    "g": "g",
    "gr": "g",
    "kg": "kg",
    "buc": "buc",
    "bucati": "buc",
}
_RE_VOLUME = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(ml|l|gr|g|kg|bucati|buc)\.?\s*$", re.I)


def parse_volume(raw: str | None) -> tuple[Decimal | None, str | None]:
    """„50 ml" → `(Decimal('50'), 'ml')`. Neparsabil sau lipsă → `(None, None)`.

    Cele 41 de valori compuse din sursă („4 gr x 40 gr", „28 gr + 2 ml", „6 X 1 gr") NU se
    aproximează la primul număr: un „4 gr" pentru un pachet de 160 g ar strica prețul per
    unitate, care e coloană generated și ar deveni fals fără ca nimeni să scrie o cifră greșită.
    Rămân NULL, iar stringul brut se păstrează în `attributes.volume_raw`.
    """
    if not raw or not raw.strip():
        return (None, None)
    m = _RE_VOLUME.match(raw)
    if not m:
        return (None, None)
    try:
        value = Decimal(m.group(1).replace(",", "."))
    except InvalidOperation:
        return (None, None)
    if value <= 0:
        return (None, None)
    return (value, _UNITS[m.group(2).lower()])


# ============================================================================
# Preț: reducere reală vs. preț condiționat de cupon
# ============================================================================


@dataclass(frozen=True, slots=True)
class PriceFacts:
    price: Decimal
    sale_price: Decimal | None = None
    coupon_code: str | None = None
    coupon_price: Decimal | None = None
    # Ce n-a fost coerent în sursă. Importerul le scrie în `catalog_quality_alerts`.
    # Nu blochează importul: principiul catalogului e „alertă, nu publicare".
    anomalies: tuple[str, ...] = ()


_CENT = Decimal("0.01")


def _money(value: float | Decimal | None) -> Decimal | None:
    """Rotunjește la banul comercial (2 zecimale), adică exact ce stochează `numeric(12,2)`.

    Fără asta, comparăm altceva decât scriem. Sursa are 134 de produse unde prețul „promoțional"
    e mai mare decât cel normal cu praf de virgulă mobilă (22.0 vs 22.00022) — reziduu dintr-un
    calcul procentual la scraping. Necuantificat, fiecare ar fi arătat ca o anomalie de preț și
    ar fi îngropat cele 4 anomalii adevărate în 138 de false pozitive. Postgres ar fi rotunjit
    oricum la scriere, deci comparația s-ar fi făcut pe cifre care nu ajung niciodată în DB.
    """
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError):
        return None


def parse_price(
    price_regular: float | Decimal | None,
    price_promo: float | Decimal | None,
    promo_code: str | None,
) -> PriceFacts | None:
    """Cele trei câmpuri de preț din sursă → contractul nostru comercial.

    Regula, și motivul pentru care nu e o linie de cod:

    În sursă, 2.131 din 2.767 de produse au `price_promo < price_regular` DAR cu
    `promo_code = WELCOME15`, adică un preț obtenabil doar cu un cod de bun venit. Doar 102
    au reducere necondiționată.

    `sale_price` înseamnă, în contractul nostru, „prețul pe care îl plătește oricine, acum".
    Un preț de cupon pus acolo produce lanțul: widgetul afișează reducerea → `grounding_guard`
    o confirmă, fiindcă e în baza noastră → clientul aude un preț pe care nu-l poate obține.

    `None` = rând fără preț (cele 9 scrape-uri eșuate), care nu se importă ca produs.

    Anomalii semnalate, nu „reparate": 8 produse din sursă au un preț „promoțional" mai MARE
    decât cel normal (90 lei → 216,66). Una dintre cele două cifre e greșită și nu se poate
    ști care, deci promoția se ignoră, prețul normal rămâne, iar cazul se raportează. A alege
    tăcut una dintre cifre ar însemna să inventăm care e adevărată.
    """
    if price_regular is None:
        return None
    base = _money(price_regular)
    if base is None or base <= 0:
        return None

    promo = _money(price_promo)
    code = (promo_code or "").strip() or None
    flags: list[str] = []

    if promo is not None and promo > base:
        flags.append("promo_price_above_regular")
    if code and (promo is None or promo >= base):
        flags.append("coupon_without_discount")

    if promo is None or promo >= base or promo <= 0:
        return PriceFacts(price=base, anomalies=tuple(flags))
    if code:
        return PriceFacts(price=base, coupon_code=code, coupon_price=promo, anomalies=tuple(flags))
    return PriceFacts(price=base, sale_price=promo, anomalies=tuple(flags))


# ============================================================================
# Disponibilitate
# ============================================================================

_AVAILABILITY = {
    "in stoc": "in_stock",
    "stoc epuizat": "out_of_stock",
}


def parse_availability(raw: str | None) -> str:
    """Vocabularul sursei → CHECK-ul nostru. Necunoscut → `out_of_stock`.

    Necunoscutul cade pe `out_of_stock`, nu pe `in_stock`: a promite un produs pe care nu-l ai
    costă o comandă și încrederea; a nu-l promite pe unul pe care îl ai costă o afișare.
    """
    return _AVAILABILITY.get((raw or "").strip().lower(), "out_of_stock")


# ============================================================================
# Categorii, brand, slug
# ============================================================================


def parse_category_path(raw: str | None) -> list[str]:
    """„Ten > Ingrijirea tenului" → `['Ten', 'Ingrijirea tenului']`. Lipsă → `[]`."""
    if not raw or not raw.strip() or raw.strip().lower() == "none":
        return []
    return [seg.strip() for seg in raw.split(">") if seg.strip()]


_RE_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(text: str, *, max_len: int = 90) -> str:
    """Slug ASCII, stabil și idempotent.

    Diacriticele se pliază (`ă`→`a`), deci un nume rescris cu diacritice la sursă produce
    ACELAȘI slug. Fără asta, o corectură de ortografie la SOLE ar crea un produs duplicat.
    """
    folded = unicodedata.normalize("NFKD", text.lower())
    ascii_only = "".join(c for c in folded if not unicodedata.combining(c))
    ascii_only = ascii_only.replace("ș", "s").replace("ț", "t").replace("ı", "i")
    slug = _RE_SLUG_STRIP.sub("-", ascii_only).strip("-")
    return slug[:max_len].rstrip("-") or "produs"


# ============================================================================
# INCI
# ============================================================================

_RE_INCI_LABEL = re.compile(r"^\s*(ingrediente|ingredients|compozitie|compoziție)\s*:?\s*$", re.I)
_RE_INCI_INLINE_LABEL = re.compile(r"^\s*(ingrediente|ingredients)\s*:\s*", re.I)
# Sub-etichete din produsele multi-componentă („Upper Sheet", „Step 1"): fără virgulă, scurte.
_MAX_INGREDIENT_LEN = 80


def split_inci(raw: str | None) -> list[str]:
    """Blocul de ingrediente → listă INCI, în ORDINEA din sursă (care e semnificativă).

    Ordinea contează: reglementarea cere ingredientele în ordinea descrescătoare a concentrației,
    deci poziția 1 și poziția 40 nu sunt același fapt. Se păstrează în
    `product_ingredients.position`.

    Ce se elimină: etichetele („Ingrediente:", „Upper Sheet"), nu ingredientele. Ce nu se
    recunoaște ca ingredient rămâne afară din listă, dar textul brut e păstrat integral în
    `product_sections` cu `kind='composition'`, deci nu se pierde.
    """
    if not raw:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for line in raw.splitlines():
        line = line.strip()
        if not line or _RE_INCI_LABEL.match(line):
            continue
        line = _RE_INCI_INLINE_LABEL.sub("", line)
        if "," not in line and len(line) < 30:
            continue  # sub-etichetă de componentă, nu ingredient
        for part in line.split(","):
            name = part.strip().strip(".;")
            if not name or len(name) > _MAX_INGREDIENT_LEN:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(name)
    return out


def ingredient_slug(name: str) -> str:
    """Cheia de deduplicare pentru `ingredients.slug`, insensibilă la formă."""
    return slugify(name, max_len=120)


def product_slug(name: str, external_id: str) -> str:
    """Slug de produs GARANTAT unic, prin sufix de SKU. Pur și idempotent.

    `products` are `unique (business_id, slug)`, iar numele singur nu ajunge: măsurat pe sursă,
    **125 de slug-uri se ciocnesc, afectând 427 de produse**. Cauza e trunchierea, care taie
    exact partea care distinge două variante („…crema hidratanta pentru fata si corp" apare de
    două ori, cu volume diferite tăiate de la coadă).

    De ce sufix ÎNTOTDEAUNA, nu doar la coliziune: „doar la coliziune" ar depinde de întregul
    set, deci de ordinea importului. Al doilea import, cu un produs nou intercalat, ar da alt
    slug aceluiași produs, iar `on conflict (business_id, slug)` ar insera un duplicat în loc
    să actualizeze. Sufixul necondiționat păstrează funcția PURĂ și importul idempotent.
    """
    sku = slugify(external_id, max_len=24)
    return f"{slugify(name, max_len=90 - len(sku) - 1)}-{sku}"


# ============================================================================
# Amprente
# ============================================================================


def content_hash(*parts: object) -> str:
    """SHA-256 peste o formă canonică. Deterministă între rulări și între mașini."""
    h = hashlib.sha256()
    for p in parts:
        h.update(b"\x1f")
        h.update(("" if p is None else str(p)).encode("utf-8"))
    return h.hexdigest()


# ============================================================================
# Rândul de sursă, gata de scris
# ============================================================================


@dataclass(slots=True)
class ParsedProduct:
    """Tot ce se poate ști despre un produs din sursă, fără nicio decizie de afișare."""

    external_id: str
    name: str
    slug: str
    brand: str | None
    category_path: list[str]
    price: PriceFacts
    availability: str
    product_url: str
    description: str | None = None
    rating: Decimal | None = None
    review_count: int | None = None
    net_content_value: Decimal | None = None
    net_content_unit: str | None = None
    gtin: str | None = None
    storage: StorageFacts = field(default_factory=StorageFacts)
    routine_time: str | None = None
    ingredients: list[str] = field(default_factory=list)
    attributes: dict[str, object] = field(default_factory=dict)
    fingerprint: str = ""
