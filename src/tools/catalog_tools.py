"""Tool-uri de catalog (G7 Faza 1) — read-only, grounded pe catalog real.

Trei tool-uri pe care agentul le poate chema (max 3/tur): `search_products` (caută),
`get_product_details` (detalii + recenzii D3), `compare_products` (compară 2-4). Toate scoped
pe `ctx.business.id` (modelul NU primește business_id). Argumentele modelului sunt validate
Pydantic ÎNAINTE de execuție. `llm_view` = reprezentare COMPACTĂ (fără PII).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from src.analytics.demand import product_ids_from_dicts
from src.catalog.folding import fold_text
from src.catalog.query_terms import content_terms
from src.catalog.render_text import cut_at_sentence, display_name
from src.catalog.vocabulary import (
    CATEGORY_DIMENSION,
    CatalogVocabulary,
    Resolution,
    ResolutionStatus,
    category_on_menu,
    facet_overlays,
    named_only_as_subshelf,
    resolve,
    resolve_any,
    text_is_redundant_as_gate,
)
from src.catalog.vocabulary_cache import get_vocabulary
from src.commerce.project import delivery_for
from src.config import card_slots, get_settings
from src.config import max_per_type as cfg_max_per_type
from src.conversation.needs import corroborated_by
from src.db.queries.catalog import (
    get_products_by_ids,
    get_substitutes,
    has_embeddings,
    search_products_lexical,
    search_products_semantic,
)
from src.db.queries.fusion import fuse_candidates
from src.domain.constraints import (
    ENFORCING_SOURCES,
    MATCH,
    MISMATCH,
    OP_LTE,
    REASON_INFERRED,
    SOURCE_MODEL,
    SOURCE_USER,
    UNKNOWN,
    BoundConstraint,
    Rejection,
    TypedConstraint,
    apply_constraints,
    bind_constraints,
    constraint_from_value,
    extract_constraints,
    merge_constraints,
)
from src.domain.normalize import normalize
from src.models import MAX_SEARCH_POOL, Relevance
from src.safety.compose import model_hint as safety_model_hint
from src.safety.policy import SafetyPolicy
from src.tools.base import ToolResult, register
from src.tools.reason_codes import annotate as annotate_reasons
from src.web.localization import amount_text

# Candidați per retriever înainte de fuziune (P4: pool intern mare, dar tool result rămâne 6×8
# spre model). ~50 = standardul de product-RAG; recall bun fără să umfle latența.
_FUSION_POOL = 50

# NX-163b: cât de lungă poate fi eticheta de variantă dusă în evenimente ca ATRIBUT normalizat
# (nuanță/mărime). Triaj-ul o extrage deja structurat (max 80 la parsare); aici o scurtăm ca să
# rămână o dimensiune de raport, nu o propoziție (P12: atribute, nu text de user).
_VARIANT_ATTR_MAX = 40

# NX-227: câți termeni de nevoie NEMAPAȚI intră într-un event. Semnalul e „ce vocabular ne
# lipsește", nu transcrierea cererii — un plafon ține evenimentul o dimensiune de raport
# (P12), iar lungimea per termen reia `_VARIANT_ATTR_MAX` (aceeași politică).
_UNMAPPED_TERMS_MAX = 5

# Pool epuizat: semnal pt agent (NX-119b randează mesajul determinist cacheable=False per-locale).
_NO_MORE_VIEW = (
    "(sesiune de căutare epuizată — nu mai sunt alte produse pe filtrele curente. "
    "Spune-i clientului că asta e tot ce ai pe aceste criterii; nu inventa produse.)"
)

# NX-173: ce vede modelul când gate-ul a respins produsul cerut. Scurt și COMPORTAMENTAL („nu-l
# prezenta, oferă-te să cauți altceva") — fraza de siguranță spre client o scrie CODUL la
# compunere (messages.py), nu modelul. Fără jargon intern, fără majuscule (review Codex).
_SAFETY_TOOL_VIEW = (
    "(produsul nu poate fi prezentat în contextul declarat de client. Nu-l numi, nu-i da preț, "
    "detalii sau link. Oferă-te să cauți o alternativă.)"
)

if TYPE_CHECKING:
    from src.models import TurnContext
    from src.worker.runner import PipelineDeps


# --- argumente (validare strictă a inputului de la model) --------------------


class SearchArgs(BaseModel):
    query: str = Field(min_length=1)
    price_max: float | None = Field(default=None, ge=0)
    category: str | None = None
    brand: str | None = None
    concerns: list[str] | None = None
    # Tier 2b p2: ingrediente/caracteristici cerute EXPLICIT („cu niacinamidă") → filtru pe
    # DomainPack.searchable_facets (key_ingredients), match normalizat. Altfel [] (fără filtru).
    features: list[str] | None = None
    sort_mode: str = "relevance"  # relevance | price_asc | price_desc | rating_desc (clamp în SQL)
    in_stock_only: bool = False
    # NX-298: plafonul nu mai e o a CINCEA cifră scrisă de mână. Default-ul vine de la proprietar
    # (`Settings.card_slots`, citit la instanțiere, nu la import), iar `le` e capul ABSOLUT al
    # contractului de unealtă — nu plafonul de produs. Cu `le=6` fix, o creștere a lui `card_slots`
    # ar fi produs exact defectul pe care cardul îl repară, doar mutat: modelul ar cere cifra pe
    # care i-o spune schema și ar primi eroare de validare, adică un tur fără căutare.
    limit: int = Field(default_factory=card_slots, ge=1, le=8)
    # A1 (Val1): numele EXACT al unui produs ANUME cerut de client (ex. „Hidra Boost Ultra"). DOAR
    # când clientul numește un produs specific, nu o nevoie. Dacă search NU întoarce un produs care
    # să-l conțină → disclosure „nu există ca atare" (anti-bait-and-switch, ca brand-not-found).
    product_name: str | None = None
    # NX-135: eticheta EXACTĂ de variantă cerută de client (nuanță/mărime, ex. „Warm Beige", „03").
    # Filtru DUR → doar produse care AU o variantă cu eticheta asta (fallback gradat, nivelul 3:
    # „alte game care chiar au Warm Beige"). Cap de lungime = o etichetă, nu o frază.
    variant_label: str | None = Field(default=None, max_length=80)


class DetailArgs(BaseModel):
    product_id: str = Field(min_length=1)


class CompareArgs(BaseModel):
    product_ids: list[str] = Field(min_length=2, max_length=4)


# --- vederi compacte pentru model (≤6×8, fără PII) ---------------------------


def _normname(s: str) -> str:
    """Lowercase + fără diacritice → match robust de nume produs (A1).

    `fold_text`, nu `fold`: ambele capete sunt stringuri Python (numele rostit de model față de
    numele venit din retrieval), nu o confruntare cu `search_tsv`.
    """
    return fold_text(s or "")


def _named_product_found(name: str, products: list[dict[str, Any]]) -> bool:
    """A1: vreun produs ÎNTORS chiar e produsul NUMIT de client? Heuristică deterministă, fără
    wordlist: cele mai LUNGI ≤2 tokenuri distinctive (≥4 litere — brand/model, nu „crema"/„ser")
    trebuie să apară TOATE în numele unui produs. Conservator spre „găsit" (evită disclosure fals):
    declară lipsă DOAR când tokenurile distinctive nu apar în niciun produs (zero incluse)."""
    toks = sorted(
        {t for t in re.findall(r"[a-z0-9]+", _normname(name)) if len(t) >= 4},
        key=len,
        reverse=True,
    )
    key = toks[:2]
    if not key:
        return True  # nimic distinctiv de verificat → nu disclose
    return any(all(t in _normname(p.get("name") or "") for t in key) for p in products)


# --- NX-169: proiecția faptelor canonice v3 în view-urile text (generic din DomainPack) ------

_USAGE_RO = {
    "morning": "dimineața",
    "evening": "seara",
    "daily": "zilnic",
    "occasional": "ocazional",
}
# Câte fațete de domeniu intră într-o linie de brief (buget de tokeni); restul apar în detail.
# CARE fațete = primele din `pack.comparison_facets` care au valoare pe produs — ordinea din
# pachet e ordinea de importanță declarată de tenant. Înainte era un set fix de chei scris pentru
# catalogul demo, care pe primul catalog real lăsa afară exact fațetele derivate (`skin_type`,
# `spf`, `routine_time`) și cerea cod pentru fiecare vertical nou (P9).
_BRIEF_FACETS_MAX = 3

# Secțiunile de fișă randate la detaliu când pachetul NU declară `detail_sections` (catalogul
# demo v3). Un pachet cu declarație le înlocuiește integral — ordinea și plafoanele sunt ale lui.
_LEGACY_DETAIL_SECTIONS: tuple[tuple[str, int], ...] = (
    ("usage", 150),
    ("warnings", 150),
    ("features", 220),
    ("benefits", 220),
    ("scenarios", 220),
)
#: Plafonul unui citat de recenzie în vederea de detaliu (tăiat la graniță de propoziție).
_REVIEW_QUOTE_CHARS = 160


def _projection_on() -> bool:
    return get_settings().catalog_projection_v2_enabled


def _pattrs(p: dict[str, Any]) -> dict[str, Any]:
    a = p.get("attributes")
    return a if isinstance(a, dict) else {}


def _product_type(p: dict[str, Any]) -> str:
    """Tipul canonic al produsului (`attributes.product_type`, derivat determinist — NX-268), sau
    șirul gol când nu a putut fi extras. Gol NU e o clasă: un produs fără tip nu poate fi numărat
    lângă altul fără tip, fiindcă nu știm dacă sunt același lucru."""
    value = _pattrs(p).get("product_type")
    return str(value).strip() if isinstance(value, str) else ""


def _facet_pairs(
    attrs: dict[str, Any],
    pack: Any,
    locale: str,
    *,
    keys: set[str] | None = None,
    exclude: set[str] | None = None,
) -> list[tuple[str, str]]:
    """(etichetă, valoare) pt fațetele de DOMENIU prezente în `attrs`, GENERIC din
    `pack.comparison_facets` (nimic hardcodat de vertical). Valorile fără `value_labels` sunt deja
    display-ready. `keys` → subset; `exclude` → chei sărite (buget tokens). Pack absent → []."""
    out: list[tuple[str, str]] = []
    seen_labels: set[str] = (
        set()
    )  # concerns + suitable_for împart „Potrivit pentru" → o singură dată
    for spec in getattr(pack, "comparison_facets", ()) or ():
        if keys is not None and spec.key not in keys:
            continue
        if exclude and spec.key in exclude:
            continue
        val = attrs.get(spec.key)
        if not val or isinstance(val, dict):  # obiecte (usage/net_content) au randare dedicată
            continue
        label = spec.labels.get(locale) or spec.labels.get("ro") or spec.key
        if label in seen_labels:
            continue
        seen_labels.add(label)

        def _lbl(v: Any, _spec: Any = spec) -> str:
            vl = _spec.value_labels.get(str(v), {})
            return vl.get(locale) or vl.get("ro") or str(v)

        vals = val if isinstance(val, list) else [val]
        out.append((label, ", ".join(_lbl(v) for v in vals[:4])))
    return out


# NX-170: coduri de potrivire → frază scurtă RO (motivul CONTEXTUAL, grounded) + atenționare soft.
_REASON_RO = {
    "concern_match": "pe nevoia ta",
    "budget_match": "în buget",
    "ingredient_match": "cu ingredientul cerut",
    "finish_match": "finish potrivit",
}


# NX-293 — ce a produs de fapt acest rezultat, spus MODELULUI, nu doar telemetriei.
#
# Până acum `lexical_step` se publica exclusiv în `product_search`: un rezultat de pe o treaptă
# degradată ajungea la model indistinct de o potrivire exactă, deci modelul îl prezenta drept
# „uite ce ai cerut". Pe `relaxed`/`fuzzy` asta era deja o gaură (măsurat pe SOLE: «parfum de dama»
# → un spray parfumat de păr, prezentat ca parfum). Odată cu `filters_only` devine obligatorie:
# treapta aia întoarce produse de pe raftul CORECT care nu răspund formulării, iar diferența dintre
# „asta am pe raft" și „asta ai cerut" e chiar diferența dintre onest și fals.
#
# Nimic din aval nu poate prinde asta: produsele și prețurile sunt REALE, deci validatorul
# (stagiul 8) și `grounding_guard` le lasă să treacă — sunt porți de ADEVĂR, nu de POTRIVIRE.
# Singurul loc unde se poate repara e aici, înainte ca modelul să scrie.
_STEP_NOTE_RO: dict[str, str] = {
    "relaxed": "potrivire parțială, nu toate cuvintele cerute",
    "relaxed_any": "potrivire parțială, nu toate cuvintele cerute",
    "fuzzy": "potrivire aproximativă, posibil o scriere greșită",
    "filters_only": "nu potrivește formularea, e de pe raftul cerut",
}


#: NX-314 — nota pentru un produs adus pe «mai ieftin» care NU are tipul subiectului.
_SUBJECT_FILLER_NOTE_RO = "completare, alt tip de produs decât cel discutat"


#: NX-305 — cât de bună e o treaptă, ca ORDINE. Mic = potrivire mai curată. Vocabularul e ÎNCHIS și
#: identic cu al lui `_STEP_NOTE_RO` plus `strict` (treapta fără notă, fiindcă tăcerea acolo e chiar
#: informația). Sincronizarea celor două e verificată de suită, nu lăsată pe seama atenției: o
#: treaptă nouă adăugată doar într-unul ar primi tăcut rangul „necunoscut" și n-ar putea salva
#: nimic.
_RUNG_ORDER: dict[str, int] = {
    "strict": 0,
    "relaxed": 1,
    "relaxed_any": 2,
    "fuzzy": 3,
    "filters_only": 4,
}


def _rung_of(rows: list[dict[str, Any]]) -> str:
    """Treapta lexicală pe care a fost SERVIT un set. Aceeași regulă ca la publicarea în
    `product_search`: primul rând care poartă eticheta o dă pentru tot setul, iar absența ei
    înseamnă `strict`. Set gol ⇒ `filters_only`, adică cea mai proastă treaptă: un set vid nu are
    cum să bată nimic, iar tratarea lui ca `strict` ar face ca „n-am găsit" să pară o potrivire
    curată."""
    if not rows:
        return "filters_only"
    return next((str(p["lexical_step"]) for p in rows if p.get("lexical_step")), "strict")


def should_rescue_guessed_filters(
    *,
    enabled: bool,
    category_uttered: bool,
    facets_uttered: bool,
    has_guessed_subject: bool,
    has_content_terms: bool,
    degraded: bool,
) -> bool:
    """NX-305 — merită să reîncercăm interogarea fără filtrele de subiect GHICITE? PURĂ.

    Patru condiții, fiecare pentru alt motiv, și toate necesare:

    `category_uttered` / `facets_uttered` — dacă clientul a ROSTIT raftul sau fațeta, a le scoate
    ar însemna să ignorăm cererea. E aceeași sursă de adevăr ca la ordonarea treptelor de relaxare
    (`corroborated_by`, NX-251), deci nu apare o a doua noțiune de „cine a spus asta".

    `has_guessed_subject` — fără niciun filtru de subiect nu e nimic de scos, iar a rula a doua
    oară aceeași interogare ar fi un cost fără cauză.

    `has_content_terms` — după ce scoatem filtrele, TEXTUL e tot ce rămâne. O interogare fără
    cuvinte de conținut ar căuta în gol, deci salvarea n-ar avea pe ce să stea.

    `degraded` — pe o potrivire curată nu avem ce repara. Asta e și plafonul de cost al regulii:
    a doua interogare rulează doar pe turele care oricum au ieșit prost.
    """
    return (
        enabled
        and not category_uttered
        and not facets_uttered
        and has_guessed_subject
        and has_content_terms
        and degraded
    )


def rescue_wins(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> bool:
    """A aterizat interogarea fără filtre ghicite pe o treaptă STRICT mai bună? PURĂ.

    Egalitatea nu ajunge, deliberat. Fără filtre, orice interogare are șanse mai mari să prindă
    ceva, iar „mai multe rezultate" nu înseamnă „rezultate mai bune" — pe treaptă egală, setul
    filtrat e cel care poartă și ipoteza modelului, deci rămâne.
    """
    if not after:
        return False
    return _RUNG_ORDER.get(_rung_of(after), 99) < _RUNG_ORDER.get(_rung_of(before), 99)


def one_per_family(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """NX-313: prima apariție a fiecărui nume AFIȘAT, în ordinea dată, apoi repetițiile. PURĂ.

    Cheia e exact ce vede clientul pe card (`display_name`), nu `name`: două rânduri cu nume
    întregi diferite („…Light Ivory 30 ml" / „…Natural Beige 30 ml") sunt identice pe ecran. O
    reordonare stabilă, nu o excludere: lungimea și conținutul listei rămân aceleași."""
    first: list[dict[str, Any]] = []
    later: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in rows:
        key = display_name(str(p.get("name") or "")).casefold()
        (later if key in seen else first).append(p)
        seen.add(key)
    return first + later


def should_probe_guessed_filters(
    *,
    enabled: bool,
    category_guessed: bool,
    facets_guessed: bool,
    has_content_terms: bool,
) -> bool:
    """NX-313: merită să întrebăm catalogul dacă ce a GHICIT modelul se potrivește cererii? PURĂ.

    Spre deosebire de NX-305 (`should_rescue_guessed_filters`), nu cere ca NIMIC să nu fie rostit
    și nici ca rezultatul să fie degradat. Un filtru rostit rămâne pe loc în interogarea de probă,
    deci nu mai poate proteja un vecin ghicit, iar un raft greșit în care textul prinde ceva
    STRICT arată exact ca o potrivire curată. Prețul e o interogare lexicală în plus, doar pe
    turele cu un filtru ghicit.

    `has_content_terms` rămâne: interogarea de probă nu cere `filters_only`, deci fără cuvinte de
    conținut n-ar avea pe ce să potrivească."""
    return enabled and (category_guessed or facets_guessed) and has_content_terms


def guessed_filter_verdict(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    *,
    comparable: bool,
    min_share: float,
    min_rows: int,
) -> tuple[str | None, float | None]:
    """NX-313: se scot filtrele ghicite? Întoarce `(motiv, proporție)`; motiv `None` = nu. PURĂ.

    `before` = setul cu filtrele ghicite, `after` = aceeași cerere fără ele (filtrele rostite
    rămân în ambele). Două motive de a le scoate:

    `better_rung` — regula NX-305: fără ghicitură, textul aterizează pe o treaptă strict mai bună.

    `contradicted` — pe treaptă EGALĂ, câtă din cererea deschisă cade în filtrul ghicit. Setul cu
    filtru e un SUBSET al celui deschis (același text, aceleași filtre rostite, plus ghicitura),
    iar ordinea lexicală e per rând, deci rândurile din `after` care sunt și în `before` sunt
    exact cele din interiorul ghiciturii. Un raft ghicit corect doar ÎNGUSTEAZĂ: o parte mare din
    potriviri e deja acolo. Unul ghicit greșit e contrazis de catalog. Pe turul `bcd8e5c6`
    raftul „Fata" (machiaj) ținea 4 din 50 de potriviri pentru «crema de hidratare».

    `comparable=False` când scara de filtre a relaxat ceva: atunci `before` a rulat alt `WHERE`,
    iar relația de subset nu mai ține. `min_rows` ține proporția departe de seturi prea mici ca să
    spună ceva. Proporția se întoarce și când nu decide, ca să poată fi măsurată."""
    if not after:
        return None, None
    rung_before = _RUNG_ORDER.get(_rung_of(before), 99)
    rung_after = _RUNG_ORDER.get(_rung_of(after), 99)
    if rung_after < rung_before:
        # O treaptă mai bună pe UN rând nu e o cerere mai bine servită: pe trafic real, «dar eu am
        # zis sa nu fie cremos» ar fi schimbat raftul de buze pe un singur produs de ten. Cerem
        # cel puțin cât avea setul cu ghicitură, plafonat la `min_rows`.
        if len(after) < min(min_rows, len(before)):
            return None, None
        return "better_rung", None
    # Doar pe `strict`: acolo textul a potrivit TOATE cuvintele, deci setul deschis e cererea.
    # Pe treptele relaxate (SAU, typo) setul deschis e dominat de cuvinte comune, iar proporția
    # ar măsura zgomotul, nu ghicitura — pe trafic real asta scotea rafturi corecte.
    if (
        not comparable
        or rung_after != rung_before
        or rung_after != _RUNG_ORDER["strict"]
        or len(after) < min_rows
    ):
        return None, None
    inside = {str(p.get("id")) for p in before}
    share = sum(1 for p in after if str(p.get("id")) in inside) / len(after)
    return ("contradicted" if share < min_share else None), share


def _step_note(p: dict[str, Any]) -> str:
    """Nota de treaptă pentru un produs servit degradat. `strict` (potrivire curată) n-are notă:
    tăcerea ACOLO e informație, iar o notă pe fiecare rând ar deveni zgomot pe care modelul îl
    ignoră exact când contează."""
    note = _STEP_NOTE_RO.get(str(p.get("lexical_step") or ""))
    out = f" | {note}" if note else ""
    # NX-314: pe «mai ieftin» setul îl alege serverul, ordonat pe tipul subiectului. Un rând de ALT
    # tip e o completare, nu o potrivire, iar modelul trebuie să știe asta înainte să scrie, ca la
    # treptele lexicale. `True` și absența tac, din același motiv ca `strict`.
    if p.get("subject_match") is False:
        out += f" | {_SUBJECT_FILLER_NOTE_RO}"
    return out


def _reason_str(p: dict[str, Any]) -> str:
    codes = p.get("reason_codes") or []
    parts = [_REASON_RO.get(c, c) for c in codes]
    out = ""
    if parts:
        out += " | potrivire: " + ", ".join(parts)
    if p.get("warning"):
        out += f" | ⚠ {p['warning']}"
    return out


def _brief(products: list[dict[str, Any]], pack: Any = None, locale: str = "ro") -> str:
    if not products:
        return "Niciun produs găsit."
    proj = _projection_on()
    lines = []
    for p in products:
        rating = f" | {float(p['rating']):.1f}★" if p.get("rating") else ""
        # NX-118: tokenul de stoc → modelul nu mai scrie „este pe stoc" fără bază pe ruta de search.
        avail = f" | stoc: {p['availability']}" if p.get("availability") else ""
        # NX-135: produsul are varianta cerută (search pe variant_label) → fit grounded.
        vmatch = " | are varianta cerută" if p.get("variant_match") else ""
        variants = _variant_view(p.get("variants"), limit=4, locale=locale)
        vline = f" | variante: {variants}" if variants else ""
        # Rezumatul e opțional în date (NULL pe tot primul catalog real): fără el nu lăsăm un
        # separator gol la coada fiecărei linii.
        summ = (p.get("ai_summary") or "")[:120]
        sline = f" | {summ}" if summ else ""
        base = (
            f"[{p['id']}] {p['name']} | {p.get('brand') or '-'} | "
            f"{amount_text(p['price'], locale)} lei{rating}{avail}{vmatch}{vline}"
        )
        step = _step_note(p)
        if proj:
            a = _pattrs(p)
            # NX-169: fapte-cheie canonice (fit specific, nu tautologie) + best_for static, compact.
            facts = _facet_pairs(a, pack, locale)
            bits = [f"{lbl}: {val}" for lbl, val in facts[:_BRIEF_FACETS_MAX]]
            if a.get("best_for"):
                bits.append(f"bun pt {a['best_for']}")
            fline = (" | " + " · ".join(bits)) if bits else ""
            lines.append(f"{base}{fline}{sline}{_reason_str(p)}{step}")
        else:
            lines.append(f"{base}{sline}{_reason_str(p)}{step}")
    return "\n".join(lines)


def _variant_view(raw_variants: Any, *, limit: int, locale: str = "ro") -> str:
    """Compact variant labels for the model, including per-variant stock when present.

    NX-197: NU suprimăm varianta unică aici, deși pe cardul web selectorul cu o singură opțiune e
    ascuns. Motivul: varianta poartă gramajul și prețul pe unitate („50ml, 84.00 lei/100ml"), adică
    exact faptele cu care botul răspunde la «care e mai avantajos». Zgomot pe UI ≠ zgomot pentru
    model — testele de la NX-171a au prins diferența."""
    labels: list[str] = []
    for v in (raw_variants or [])[:limit]:
        if not isinstance(v, dict):
            continue
        lbl = v.get("label")
        vid = v.get("variant_id") or v.get("id")
        if not lbl or not vid:
            continue
        pr = v.get("price")
        price_str = f", {amount_text(pr, locale)} lei" if pr is not None else ""
        stock = v.get("stock")
        stock_str = f", stoc {int(stock)}" if stock is not None else ""
        attrs = v.get("attributes") if isinstance(v.get("attributes"), dict) else {}
        bits = [
            str(attrs.get(k) or v.get(k))
            for k in ("shade", "undertone", "depth")
            if attrs.get(k) or v.get(k)
        ]
        attrs_str = f", {'/'.join(bits)}" if bits else ""
        # NX-171a: gramaj + preț/unitate (preț/100ml sau /100g) — comparație „cel mai bun raport".
        ncv, ncu = v.get("net_content_value"), v.get("net_content_unit")
        nc_str = f", {float(ncv):g}{ncu}" if ncv and ncu else ""
        ppu = v.get("price_per_unit")
        base = {"ml": "ml", "l": "ml", "g": "g", "kg": "g"}.get(ncu or "")
        ppu_str = f", {amount_text(ppu, locale)} lei/100{base}" if ppu and base else ""
        labels.append(f"[{vid}] {lbl}{attrs_str}{nc_str}{price_str}{ppu_str}{stock_str}")
    return ", ".join(labels)


def _detail_view(
    p: dict[str, Any],
    pack: Any = None,
    locale: str = "ro",
    delivery: str | None = None,
    *,
    noise_badges: frozenset[str] | set[str] = frozenset(),
) -> str:
    parts = [
        f"[{p['id']}] {p['name']} ({p.get('brand') or '-'}) — "
        f"{amount_text(p['price'], locale)} lei",
        f"stoc: {p.get('availability') or '-'}",
    ]
    if p.get("rating"):
        parts.append(f"rating: {float(p['rating']):.1f}★")
    if p.get("ai_summary"):
        parts.append(f"descriere: {p['ai_summary'][:200]}")
    if _projection_on():
        a = _pattrs(p)
        # NX-169: fațetele de domeniu (finish/coverage/suitable_for/...); key_ingredients îl randăm
        # din tabelul NORMALIZAT (mai jos), nu din fațeta pe attributes.
        for lbl, val in _facet_pairs(a, pack, locale, exclude={"key_ingredients"}):
            parts.append(f"{lbl.lower()}: {val}")
        # INCI din tabelul normalizat `product_ingredients` (168e-2); fallback attributes.
        inci = p.get("ingredients_db") or a.get("key_ingredients") or []
        if inci:
            parts.append("ingrediente (INCI): " + ", ".join(str(x) for x in list(inci)[:6]))
        if a.get("best_for"):
            parts.append(f"recomandat pentru: {a['best_for']}")
        times = (a.get("usage") or {}).get("time") or []
        if times:
            parts.append("cum se folosește: " + ", ".join(_USAGE_RO.get(t, t) for t in times))
        warns = [
            x.get("value")
            for x in a.get("not_recommended_for") or []
            if isinstance(x, dict) and x.get("value")
        ]
        if warns:
            parts.append("de reținut — nepotrivit pentru: " + ", ".join(warns[:3]))
        # NX-168e-2 graf PDP (consumat aici): secțiunile fișei, cu buget per secțiune — modelul
        # primește esențialul, nu fișa întreagă; descrierea lungă rămâne pe pagina de produs.
        # CARE secțiuni și CÂT din fiecare e declarat de pachet (`detail_sections`), în ordinea
        # lui: lista de tipuri era scrisă în cod pentru catalogul demo și, pe primul catalog real,
        # randa UNA din cele 17 secțiuni ale fișei („cui i se potrivește", „când nu e alegerea
        # potrivită", „pe scurt" cădeau tăcut — docs/DB-QUERY-PROBE-2026-09-08.md). Tăierea e la
        # graniță de propoziție, nu la caracter.
        declared = getattr(pack, "detail_sections", None) or ()
        section_caps: dict[str, int] = (
            {s.kind: s.max_chars for s in declared} if declared else dict(_LEGACY_DETAIL_SECTIONS)
        )
        by_kind: dict[str, dict[str, Any]] = {}
        for sec in p.get("sections") or []:
            if isinstance(sec, dict) and sec.get("body") and sec.get("kind") in section_caps:
                by_kind.setdefault(str(sec["kind"]), sec)  # prima secțiune per tip
        for kind, cap in section_caps.items():
            sec = by_kind.get(kind)
            if sec is None:
                continue
            body = cut_at_sentence(str(sec["body"]), cap)
            parts.append(f"{(sec.get('title') or kind).lower()}: {body}")
        # Badge-urile care nu deosebesc produsul de restul catalogului (`noise_badges`, calculate
        # în vocabular) nu ajung la model: „CPNP" pe 2.711 din 2.758 de produse nu e o etichetă.
        badges = [str(b) for b in list(p.get("badges") or []) if str(b) not in noise_badges]
        if badges:
            parts.append("etichete: " + ", ".join(badges[:4]))
        # NX-169: recenzii INDIVIDUALE (tabelul reviews, 168e-2) — 1-2 citate reale + autor/rating.
        quotes = []
        for rv in (p.get("reviews_list") or [])[:2]:
            if isinstance(rv, dict) and rv.get("body"):
                who = rv.get("author") or "client"
                star = f", {int(rv['rating'])}★" if rv.get("rating") else ""
                quote = cut_at_sentence(str(rv["body"]), _REVIEW_QUOTE_CHARS)
                quotes.append(f"„{quote}” ({who}{star})")
        if quotes:
            parts.append("recenzii clienți: " + "; ".join(quotes))
    if p.get("review_summary"):
        parts.append(f"recenzii: {p['review_summary'][:200]}")
    if p.get("top_pros"):
        parts.append("plusuri: " + ", ".join(list(p["top_pros"])[:3]))
    if p.get("top_cons"):
        parts.append("minusuri: " + ", ".join(list(p["top_cons"])[:2]))
    # NX-118: variante (nuanțe/mărimi) cu id + PREȚ real → modelul răspunde grounded la „aveți
    # nuanța 03?", recomandă un preț per-variantă acceptat de validator, și poate trimite un
    # `variant_id` REAL la cart_add (membership-ul rămâne plasa). Format `[id] etichetă (preț)`.
    if variants := _variant_view(p.get("variants"), limit=8):
        parts.append("variante: " + variants)
    # NX-191: livrarea e un FAPT de vânzare, nu un detaliu — „în cât timp ajunge?" e printre cele
    # mai frecvente întrebări, iar până acum botul n-avea ce citi. Textul e CALCULAT (cod), nu
    # compus de model.
    if delivery:
        parts.append(f"livrare: {delivery}")
    # NX-194: FAQ per produs — citit DOAR la detaliu (nu intră în căutare). Cap 3 în vederea
    # modelului: restul rămân pe pagina de produs, ca să nu umflăm contextul.
    faqs = [f for f in (p.get("faqs") or []) if isinstance(f, dict) and f.get("question")]
    if faqs:
        qa = "; ".join(f"{f['question']} — {str(f.get('answer') or '')[:110]}" for f in faqs[:3])
        parts.append(f"întrebări frecvente: {qa}")
    return " | ".join(parts)


def _compare_view(products: list[dict[str, Any]], pack: Any = None, locale: str = "ro") -> str:
    # Kill-switch OFF → comportamentul vechi (detail per produs).
    if not _projection_on():
        return "\n".join(_detail_view(p) for p in products)
    # NX-169: comparație pe DIFERENȚE — o linie/produs cu identitatea + preț, apoi DOAR axele
    # (preț + fațete de domeniu) care DIFERĂ între produse. Diferențiere reală, nu tabel identic.
    heads = [
        f"[{p['id']}] {p['name']} ({p.get('brand') or '-'}) — {amount_text(p['price'], locale)} lei"
        + (f", {float(p['rating']):.1f}★" if p.get("rating") else "")
        for p in products
    ]
    lines = list(heads)
    diffs: list[str] = []
    # preț ca axă de diferență (dacă nu-s toate egale)
    prices = [round(float(p.get("price") or 0), 2) for p in products]
    # Pe axe, produsul e numit SCURT (`display_name`): numele întreg e deja în antet, iar pe
    # primul catalog real are ~190 de caractere — repetat pe fiecare axă, comparația a două
    # produse ajungea la 1.400 de caractere din care ~800 erau numele de patru ori.
    short = [display_name(p.get("name")) for p in products]
    if len(set(short)) < len(short):  # capete identice (familie de nuanțe) → numele întreg
        short = [str(p.get("name") or "") for p in products]
    if len(set(prices)) > 1:
        diffs.append(
            "preț: "
            + " vs ".join(f"{nm}={amount_text(pr, locale)} lei" for nm, pr in zip(short, prices))
        )
    # fațete de domeniu: afișează axa DOAR dacă valorile diferă între produse
    seen_keys: list[str] = []
    for p in products:
        for lbl, _ in _facet_pairs(_pattrs(p), pack, locale):
            if lbl not in seen_keys:
                seen_keys.append(lbl)
    for lbl in seen_keys:
        vals = []
        for p in products:
            pair = dict(_facet_pairs(_pattrs(p), pack, locale))
            vals.append(pair.get(lbl, "—"))
        if len({v for v in vals}) > 1:  # diferă → axă utilă
            diffs.append(
                f"{lbl.lower()}: " + " vs ".join(f"{nm}={v}" for nm, v in zip(short, vals))
            )
    if diffs:
        lines.append("diferențe: " + " | ".join(diffs))
    else:
        lines.append("diferențe: preț/atribute similare — decide pe rating/recenzii/preferință")
    return "\n".join(lines)


# --- tool-uri ----------------------------------------------------------------


def uttered_by_client(ctx: TurnContext, *values: object) -> bool:
    """Vreuna dintre valori a fost ROSTITĂ de client — în turul ăsta sau într-unul recent?

    Extinderea lui `corroborated_by` (NX-251) de la mesajul curent la istoric, cerută de o
    ASIMETRIE de consecințe. La NX-251, un `False` producea o nevoie mai SLABĂ, deci „orice dubiu
    întoarce False" era partea sigură. Aici consecința lui `False` e opusă: valoarea devine
    relaxabilă, adică poate fi ARUNCATĂ din `WHERE`. Cu dubiul rezolvat tot spre `False`, secvența
    «vreau sa vad produse de par» → «altceva ?» ar relaxa raftul pe al doilea tur, fiindcă al
    doilea mesaj nu-l mai numește — și clientul ar primi tot catalogul.

    Deci partea sigură se inversează: verificăm ce a scris clientul în ORICE mesaj din fereastra de
    istoric (`ctx.history` e deja plafonat la 8 de context builder, P4), și numai mesajele LUI
    (`direction == "inbound"`) — o parafrază a botului nu e o afirmație a clientului. Pură, fără
    I/O, agnostică de limbă: nicio listă de cuvinte, doar coroborarea pe prefix din NX-251.
    """
    return any(
        corroborated_by(text, value) for text in client_texts(ctx) for value in values if value
    )


def client_texts(ctx: TurnContext) -> list[str]:
    """Ce a scris CLIENTUL: mesajul curent primul, apoi mesajele lui din fereastra de istoric.

    Un singur loc care decide ce înseamnă „a spus clientul" pentru porțile de proveniență (raft,
    fațete, preț), ca să nu existe două ferestre care să difere tăcut."""
    texts: list[str] = [str(getattr(ctx.message, "body", "") or "")]
    for m in getattr(ctx, "history", None) or []:
        if getattr(m, "direction", None) == "inbound" and getattr(m, "body", None):
            texts.append(str(m.body))
    return texts


#: NX-319: de ce a rămas `price_max` al modelului în `WHERE`. Vocabular ÎNCHIS (telemetrie).
PRICE_BOUND_SPOKEN_NOW = "spoken_now"
PRICE_BOUND_SPOKEN_EARLIER = "spoken_earlier"
PRICE_BOUND_RELATIVE_REQUEST = "relative_request"
PRICE_BOUND_SESSION = "session"


def price_bound_source(
    price_max: float,
    *,
    texts: Sequence[str],
    relative_request: bool,
    session_price_max: object,
) -> str | None:
    """NX-319: are marginea de preț a modelului o SURSĂ? Întoarce sursa sau `None`. PURĂ.

    Un filtru de preț EXCLUDE produse, iar NX-266 spune deja că o deducție n-are voie să excludă:
    doar calea moștenită a lui `price_max` scăpa de regulă. Pe turul real `38b47d4a`, «ai ceva anti
    aging?» a plecat cu `price_max=29.99` dus de model din «mai ieftin»-ul de dinainte, adică o
    margine relativă la ALT set, aplicată unui subiect nou.

    Sursele, în ordinea în care se verifică:
      • numărul e rostit în mesajul curent (`texts[0]`) sau într-unul anterior al clientului: un
        buget spus acum trei ture rămâne buget, exact ca în starea v2 (NX-235);
      • mesajul curent e o cerere RELATIVĂ de preț («mai ieftin», «cam scumpe»): marginea e
        derivată de model din setul afișat, iar derivarea e chiar ce a cerut clientul;
      • marginea e identică cu cea a sesiunii active: e aceeași cerere, paginată.
    Numerele se compară prin `corroborated_by` (aceleași lecturi ale separatorului zecimal)."""
    if texts and corroborated_by(texts[0], price_max):
        return PRICE_BOUND_SPOKEN_NOW
    if any(corroborated_by(t, price_max) for t in texts[1:]):
        return PRICE_BOUND_SPOKEN_EARLIER
    if relative_request:
        return PRICE_BOUND_RELATIVE_REQUEST
    if isinstance(session_price_max, (int, float)) and not isinstance(session_price_max, bool):
        if abs(float(session_price_max) - float(price_max)) <= 0.005:
            return PRICE_BOUND_SESSION
    return None


def _is_relative_price_request(text: str) -> bool:
    """Același detector ca ramura deterministă «mai ieftin» (un singur proprietar al intenției).
    Import leneș: `deterministic` trage `compose`, care la rândul lui ajunge la unelte."""
    from src.agent.deterministic import _CHEAPER_RE

    return _CHEAPER_RE.search(text or "") is not None


def _relax_ladder(
    *,
    price_max: float | None,
    facet_filters: dict[str, list[str]] | None,
    category: Sequence[str] | None,
    in_stock_only: bool,
    features: list[str] | None = None,
    constraints: Sequence[BoundConstraint] = (),
    category_uttered: bool = True,
    facets_uttered: bool = True,
) -> list[dict[str, Any]]:
    """Trepte de filtre dure, relaxate CUMULATIV ca să iasă ceva relevant înainte de listă goală
    (P6). Brand-ul NU se relaxează niciodată.

    Cu `SEARCH_SORT_MODE_ENABLED` (ARCH-product-retrieval): prețul + disponibilitatea sunt
    constrângeri DURE, NU se relaxează — relaxăm doar SOFTUL (fațete → category). Altfel un
    „sub 80" supra-constrâns ar scoate bound-ul de preț și ar întoarce un 149.99 (bug-ul de preț).
    Fără flag (kill-switch OFF): comportamentul vechi (price → fațete → category).

    `category` e acum o LISTĂ de chei REZOLVATE, nu un cuvânt venit de la model. Diferența e toată
    povestea: un token neverificat făcea din relaxare singura scăpare (și una imposibilă, fiindcă
    `category` e dură), în timp ce o listă rezolvată are produse în spate prin construcție — deci
    treapta zero nu mai poate ieși goală din vina vocabularului.

    NX-266: constrângerile numerice NE-de-preț nu se relaxează pe nicio treaptă. Asta e teza
    cardului — un număr rostit („SPF minim 30") nu e o preferință de care să scapi ca să iasă ceva,
    ci o cerință pe care produsele fie o îndeplinesc, fie nu. Cele de PREȚ se relaxează exact acolo
    unde se relaxa `price_max`, ca migrarea bugetului pe noul contract să nu schimbe nimic."""
    base = {
        "price_max": price_max,
        "facet_filters": facet_filters or None,
        "category": list(category) if category else None,
        "in_stock_only": in_stock_only,
        "features": features,  # Tier 2b p2: relaxat ULTIMUL (hard requirement „cu niacinamidă")
        "constraints": tuple(constraints),
    }
    settings = get_settings()
    by_provenance = getattr(settings, "search_relax_by_provenance_enabled", False)
    hard_category = getattr(settings, "search_category_hard_enabled", True)
    # „Categoria e dură" a fost scrisă pentru «clientul a cerut raftul X, nu-i servi raftul Y» —
    # corect, dar regula nu deosebea un raft CERUT de unul GHICIT. Cu proveniența la îndemână,
    # duritatea se leagă de afirmație, nu de câmp: raftul rostit rămâne inviolabil, cel ghicit
    # capătă o treaptă. Cu flagul stins, `category_uttered` rămâne `True` pe toți apelanții, deci
    # condiția se reduce exact la cea de dinainte.
    #
    # …dar NUMAI dacă cererea mai are un SUBIECT după ea. `corroborated_by` e o potrivire
    # literală: la «vreau makeup» cu `category="machiaj"` întoarce `False`, deși modelul a
    # tradus corect, nu a ghicit. Sinonimul, cuvântul străin și forma flexionată produc toate
    # același fals „ghicit" (P: model+context, nu liste de cuvinte). Consecința e acceptabilă cât
    # timp rămâne CE a cerut clientul — fațeta îl poartă mai departe — și inacceptabilă când
    # categoria e singurul subiect: acolo relaxarea ar lăsa interogarea fără niciun subiect, iar
    # pagina ar deveni „cele mai bine notate produse", exact eșecul măsurat și respins la NX-298.
    category_relaxable = bool(category) and (
        not hard_category or (by_provenance and not category_uttered and bool(facet_filters))
    )

    # Treptele SOFT, în ordinea în care se renunță la ele. Ordinea e (proveniență, tip): mai întâi
    # ce a ghicit modelul, apoi ce a rostit clientul. Fără felia asta, ordinea era fixată de tip —
    # fațete înaintea categoriei — deci un raft ghicit supraviețuia unei nevoi rostite, iar treapta
    # terminală `filters_only` servea raftul greșit în loc de fațeta corectă. `sorted` e STABIL,
    # deci la proveniență egală rămâne ordinea istorică (fațete, apoi categorie) și nimic nu se
    # mișcă pe turele în care clientul a numit ambele.
    soft: list[tuple[bool, str]] = []
    if facet_filters:
        soft.append((facets_uttered if by_provenance else True, "facet_filters"))
    if category_relaxable:
        soft.append((category_uttered if by_provenance else True, "category"))
    soft.sort(key=lambda item: item[0])

    steps: list[dict[str, Any]] = [base]
    if not settings.search_sort_mode_enabled:
        priced = _without_price(constraints) != tuple(constraints)
        if price_max is not None or priced:
            steps.append(
                {
                    **steps[-1],
                    "price_max": None,
                    "constraints": _without_price(steps[-1]["constraints"]),
                }
            )
    # cu `search_sort_mode_enabled`: prețul + stocul rămân fixate; relaxăm doar softul
    for _, field in soft:
        steps.append({**steps[-1], field: None})
    if features:  # feature relaxat DUPĂ category (păstrat cât mai mult; P6 la epuizare)
        steps.append({**steps[-1], "features": None})
    return steps


@dataclass(frozen=True, slots=True)
class _ResolvedTerms:
    """Ce a rămas dintr-o cerere după ce fiecare cuvânt a fost confruntat cu catalogul."""

    category_keys: tuple[str, ...]
    facet_filters: dict[str, list[str]]
    flat_facet_keys: list[str]
    unresolved: list[str]
    emitted: list[Resolution]
    #: Verdictul pe CATEGORIE, păstrat întreg (nu doar cheile). „N-am putut judeca" (vocabular
    #: indisponibil → `unknown_dimension`) și „am judecat, nu există" (`not_in_vocabulary`) produc
    #: amândouă zero chei, dar cer reacții OPUSE: prima e o degradare a noastră și nu are voie să
    #: schimbe nimic pentru client, a doua e o eroare de argument a modelului.
    category: Resolution | None = None


async def _subject_filter_tail(
    conn: Any,
    ctx: TurnContext,
    a: SearchArgs,
    step: dict[str, Any],
    *,
    searchable_facets: Any,
) -> list[dict[str, Any]]:
    """NX-303 — restul setului pe care filtrul îl NUMEȘTE, ordonat după cuvintele clientului.

    Treapta terminală NX-298 (`only_filters_step`): `WHERE` = filtrele treptei câștigătoare, fără
    predicat de text; `ORDER BY` = `ts_rank_cd` peste aceleași cuvinte. Deci rândurile vin deja în
    ordinea „cât de bine potrivesc formularea", iar apelantul le pune strict după pagină.

    Fiecare rând poartă `lexical_step='filters_only'`: dacă ajunge vreodată în fața modelului
    (pagina 2), se prezintă ca „asta am pe raft", nu ca „uite ce ai cerut".
    """
    rows = await search_products_lexical(
        conn,
        ctx.business.id,
        query_text=a.query,
        price_max=step["price_max"],
        constraints=step["constraints"],
        facet_filters=step["facet_filters"],
        features=step["features"],
        searchable_facets=searchable_facets,
        variant_label=a.variant_label,
        category=step["category"],
        brand=a.brand,
        sort_mode=a.sort_mode,
        in_stock_only=step["in_stock_only"],
        locale=ctx.language,
        allow_filters_only=True,
        only_filters_step=True,
        pool=MAX_SEARCH_POOL,
    )
    for r in rows:
        r["lexical_step"] = "filters_only"  # eticheta treptei NX-293, ca în `catalog.py`
    return rows


def _text_gate_is_redundant(
    ctx: TurnContext, a: SearchArgs, vocab: CatalogVocabulary, resolutions: _ResolvedTerms
) -> bool:
    """NX-303 — poarta de TEXT mai spune ceva peste filtre, sau doar taie arbitrar?

    Vezi `vocabulary.text_is_redundant_as_gate` pentru regula și pentru măsurătoare. Aici trăiesc
    doar porțile de politică, în ordinea în care e ieftin să pice:

    1. kill-switch;
    2. scara lexicală v2 — treptele (deci și cea terminală) există doar sub ea;
    3. `filters_only` trebuie să fie permisă de politica NX-293, altfel am cere o treaptă care
       oricum se întoarce goală;
    4. trebuie să EXISTE un filtru de subiect. Fără el, `only_filters_step` n-are ce servi, iar
       poarta NX-293 refuză oricum — dar verificat aici, verdictul rămâne onest în telemetrie.

    `features` nu intră în cheile aplicate: ele se potrivesc pe valori de fațetă NORMALIZATE, prin
    alt predicat, deci a le trata drept „chei purtate" ar declara redundant un text pe care WHERE-ul
    nu-l garantează. Conservator în direcția corectă.
    """
    s = get_settings()
    if not getattr(s, "search_text_gate_when_redundant_enabled", True):
        return False
    if not getattr(s, "lexical_query_v2_enabled", True):
        return False
    if not getattr(s, "search_filters_only_fallback_enabled", True):
        return False
    applied = set(resolutions.flat_facet_keys) | set(resolutions.category_keys)
    if not (applied or a.brand or a.variant_label):
        return False  # fără subiect, treapta terminală nu există (NX-293)
    if not applied:
        return False  # brandul/varianta nu rezolvă CUVINTE, deci nu pot face textul redundant
    overlays = facet_overlays(getattr(ctx.business, "domain_pack", None), vocab.facet_names)
    return text_is_redundant_as_gate(
        vocab, content_terms(a.query, ctx.language), applied, overlays=overlays
    )


def _resolve_search_terms(
    ctx: TurnContext, a: SearchArgs, vocab: CatalogVocabulary
) -> _ResolvedTerms:
    """Termenii ceruți de model → chei REALE de catalog, cu verdict și dovadă.

    Nimic din ce urmează nu numește o dimensiune: nevoile clientului se rezolvă peste fațetele
    DESCOPERITE (`vocab.facet_names`), iar dimensiunea câștigătoare o alege catalogul. Un magazin
    unde „mat" e o valoare de `finish` filtrează pe `finish`; unul unde e `tip_suprafata`
    filtrează pe aia. Codul nu trebuie să știe care, și tocmai de-asta ține pe orice client.

    Harta de sinonime a tenantului e tratată ca overlay de LIMBĂ, deci se încearcă pe toate
    fațetele — validarea contra vocabularului decide unde se potrivește, iar o țintă moartă iese
    ca `UNKNOWN(overlay_target_dead)`, nu ca filtru.

    Peste ea vin aliasurile DECLARATE ale fiecărei fațete (`TypedFacet.aliases`), aplicate DOAR
    fațetei lor. Erau inerte până acum: `load_vocabulary` descoperă dimensiunile din cheile reale
    ale lui `attributes` și nu citește pachetul, iar singurul overlay pasat aici era `concern_map`.
    Deci `routine_time` își declara cele 9 aliasuri („seara" → `pm`) și niciunul nu era consultat —
    măsurat, „seara" se rezolva pe `compliance` cu verdict `UNKNOWN`, adică filtrul nu rula, deși
    atributul e populat pe 2.758/2.758 de produse.

    Aliasurile fațetei NU intră în `concern_map`, deliberat: acela e overlay-ul de NEVOI, iar
    pachetul are un invariant testat care cere ca fiecare valoare din el să fie purtată de
    `skin_type` sau `concerns`. „seara" nu e o nevoie, e un moment al rutinei — iar o hartă în care
    încap amândouă n-ar mai putea fi verificată de nimic.
    """
    # Formula celor două straturi trăiește în `facet_overlays` (un singur loc): `routine_plan` are
    # nevoie de EXACT aceeași rezoluție, iar când o avea pe cont propriu nu o avea deloc.
    overlays = facet_overlays(getattr(ctx.business, "domain_pack", None), vocab.facet_names)

    emitted: list[Resolution] = []

    category_keys: tuple[str, ...] = ()
    category_verdict: Resolution | None = None
    if a.category:
        # Modelul alege raftul dintr-un meniu de CHEI (conversația `cd98a513`: «Fata» = Machiaj >
        # Fata la o cerere de cremă de față). O valoare din afara meniului cade pe rezolvarea
        # liberă de dinainte, cu gărzile ei (NX-319 omograf, NX-313 ghicit), și se NUMĂRĂ: blocată,
        # ar fi stricat mai mult decât repară. Măsurat cu `scripts/shelf_menu_probe.py` pe 66 de
        # căutări reale: fără raft, «si ceva de volum ?» (păr) aducea plumpere de buze, iar
        # numele unice («buze», «styling») erau corecte. Rata `category_off_menu` spune dacă
        # modelul a adoptat cheile.
        r = resolve(vocab, a.category, CATEGORY_DIMENSION)
        if getattr(get_settings(), "search_category_menu_enabled", False):
            on_menu = category_on_menu(vocab, a.category)
            if on_menu.reason == "off_menu":
                ctx.emit(
                    "category_off_menu",
                    value=str(a.category)[:_VARIANT_ATTR_MAX],
                    resolved=r.status.value,
                )
            elif on_menu.status is ResolutionStatus.KNOWN:
                r = on_menu
        emitted.append(r)
        category_keys = r.constraint_keys
        category_verdict = r

    facet_filters: dict[str, list[str]] = {}
    unresolved: list[str] = []
    for term in a.concerns or []:
        if not isinstance(term, str) or not term.strip():
            continue
        r = resolve_any(vocab, term, overlays=overlays, dimensions=vocab.facet_names)
        emitted.append(r)
        if keys := r.constraint_keys:
            facet_filters.setdefault(r.dimension, []).extend(keys)
        else:
            unresolved.append(normalize(term))

    for dim in facet_filters:
        facet_filters[dim] = sorted(dict.fromkeys(facet_filters[dim]))
    flat = sorted({v for values in facet_filters.values() for v in values})
    return _ResolvedTerms(
        category_keys=category_keys,
        facet_filters=facet_filters,
        flat_facet_keys=flat,
        unresolved=sorted(dict.fromkeys(unresolved)),
        emitted=emitted,
        category=category_verdict,
    )


def _rank_weights(ctx: TurnContext) -> dict[str, float] | None:
    """Ponderile scorului de ranking BLENDED (ARCH-2026 P0) pentru `fuse_candidates`: din
    `DomainPack.rank_weights` (override per-vertical, parțial), altfel `{}` → default-urile din
    fusion.py (`RANK_WEIGHTS`, merge acolo). `None` când kill-switch-ul e OFF → fuziunea cade pe
    `deterministic_rerank` (RRF pur, rating doar pe tie — byte-identic). Determinist, fără I/O."""
    if not get_settings().search_blended_rank_enabled:
        return None
    pack = getattr(ctx.business, "domain_pack", None)
    return (pack.rank_weights if pack else None) or {}


# NX-134: diversificare sortiment. Max produse per brand în prima pagină + acoperirea terțelor de
# preț → sortiment ca un arbore de decizie (ieftin/mediu/scump, mărci diferite), nu top-N clone.
_MAX_PER_BRAND = 2
# NX-298: max produse din același TIP canonic în prima pagină. 2 la o pagină de 6 înseamnă cel
# puțin trei clase de soluție când catalogul le are — forma răspunsului iZi („plasturi ȘI
# tratamente/creme"), nu șase variații ale aceluiași lucru. Nu e o excludere: e ordine (vezi
# `diversify_pool`), iar faza 2 relaxează cota dacă tipurile nu ajung.


def _price_tertile(price: float, lo: float, hi: float) -> int:
    """Terța de preț (0=ieftin, 1=mediu, 2=scump) a unui preț în intervalul [lo, hi] al setului.
    Interval degenerat (hi<=lo) → 0 (o singură terță)."""
    if hi <= lo:
        return 0
    frac = (price - lo) / (hi - lo)
    return 0 if frac < 1 / 3 else (1 if frac < 2 / 3 else 2)


#: NX-303 — santinelă, fiindcă `None` are DEJA un sens aici („fără cotă pe tip"), folosit de suită
#: ca să măsoare exact contribuția cotei. Un default care ar fi citit configul prin `None` ar fi
#: șters tăcut acea posibilitate. Rezolvarea se face la APEL, nu la import: altfel un
#: `monkeypatch` pe settings n-ar mai fi vizibil, iar cifra ar îngheța la primul import.
_QUOTA_FROM_OWNER = -1


def diversify_pool(
    candidates: list[dict[str, Any]],
    limit: int,
    *,
    max_per_brand: int = _MAX_PER_BRAND,
    max_per_type: int | None = _QUOTA_FROM_OWNER,
) -> list[dict[str, Any]]:
    """Reordonează candidații (DEJA ordonați pe relevanță) ca PRIMELE `limit` să fie DIVERSE — scară
    de preț (terțe) + max `max_per_brand` per brand + max `max_per_type` per TIP de produs —
    păstrând top-1 primul și ordinea de relevanță ÎN INTERIORUL selecției. Restul pool-ului urmează
    în ordinea de relevanță (pt paginare). Greedy DETERMINIST (fără random). `len <= limit` →
    neschimbat (nimic de diversificat).

    Fază 1: acoperă terțele de preț prezente (câte una întâi), respectând cotele. Fază 2: umple
    sloturile rămase pe relevanță, relaxând cotele când nu ajung (ex. toate produsele de la un
    singur brand → tot `limit` rezultate, nu 2).

    NX-298 — de ce și TIPUL, nu doar brandul și prețul: la «vreau ceva sa scap de cosuri», iZi
    răspunde cu plasturi ȘI tratament ȘI gel de spălare, adică acoperă CLASELE de soluție; noi
    diversificam pe două axe care nu spun nimic despre ce E produsul, deci șase seruri de la șase
    branduri, la trei prețuri, treceau drept „sortiment". Raftul de acnee al tenantului pilot are
    ser 60, cremă 50, mască 43, spumă de curățare 20, plasturi 18 — paleta există în date.
    Cota NU e o excludere: `diversify_pool` doar REORDONEAZĂ, iar ce trece de `limit` rămâne în
    pool pentru paginare. Distincția contează, fiindcă `product_type` e declarat `enforce_ready:
    false` (acoperire 75,7%) — n-are dreptul să arunce candidați, dar are dreptul să-i așeze.
    Tipul LIPSĂ nu formează o clasă: produsele fără tip nu se numără între ele (vezi
    `_product_type`), altfel un sfert din catalog ar fi plafonat ca și cum ar fi un singur lucru.
    """
    if max_per_type == _QUOTA_FROM_OWNER:  # NX-303: cifra vine de la proprietarul ei
        max_per_type = cfg_max_per_type()
    n = len(candidates)
    if limit <= 0 or n <= limit:
        return list(candidates)

    prices = [p["price"] for p in candidates if p.get("price") is not None]
    lo, hi = (min(prices), max(prices)) if prices else (0.0, 0.0)
    tert = [
        None if p.get("price") is None else _price_tertile(float(p["price"]), lo, hi)
        for p in candidates
    ]
    present = {t for t in tert if t is not None}

    selected: list[int] = [0]
    brand_count: dict[Any, int] = {}
    type_count: dict[str, int] = {}
    covered: set[int] = set()
    if candidates[0].get("brand"):
        brand_count[candidates[0]["brand"]] = 1
    if first_type := _product_type(candidates[0]):
        type_count[first_type] = 1
    if tert[0] is not None:
        covered.add(tert[0])

    # Fază 1: greedy pe acoperirea terțelor de preț, sub cotele de brand și de tip.
    for i in range(1, n):
        if len(selected) >= limit:
            break
        brand = candidates[i].get("brand")
        if brand and brand_count.get(brand, 0) >= max_per_brand:
            continue
        ptype = _product_type(candidates[i])
        if ptype and max_per_type is not None and type_count.get(ptype, 0) >= max_per_type:
            continue
        all_covered = present <= covered  # toate terțele prezente deja acoperite
        if tert[i] is None or tert[i] not in covered or all_covered:
            selected.append(i)
            if brand:
                brand_count[brand] = brand_count.get(brand, 0) + 1
            if ptype:
                type_count[ptype] = type_count.get(ptype, 0) + 1
            if tert[i] is not None:
                covered.add(tert[i])

    # Fază 2: umple pe relevanță → niciodată < limit când există candidați. Cotele se relaxează
    # PAS CU PAS, nu dintr-odată: fiecare rundă mărește plafonul cu unu și reia lista în ordinea
    # de relevanță. Varianta de dinainte le abandona complet la prima ratare, iar efectul măsurat
    # pe pool-ul real de acnee era ZERO: faza 1 sărea al treilea ser, faza 2 îl lua înapoi imediat,
    # deci cota de tip exista în cod și nu exista în rezultat.
    if len(selected) < limit:
        chosen = set(selected)
        allow_brand = max_per_brand
        allow_type = max_per_type if max_per_type is not None else n
        for _ in range(n + 1):  # mărginit: la `allow > n` orice candidat trece
            if len(selected) >= limit:
                break
            progressed = False
            for i in range(1, n):
                if len(selected) >= limit:
                    break
                if i in chosen:
                    continue
                brand = candidates[i].get("brand")
                if brand and brand_count.get(brand, 0) >= allow_brand:
                    continue
                ptype = _product_type(candidates[i])
                if ptype and type_count.get(ptype, 0) >= allow_type:
                    continue
                selected.append(i)
                chosen.add(i)
                progressed = True
                if brand:
                    brand_count[brand] = brand_count.get(brand, 0) + 1
                if ptype:
                    type_count[ptype] = type_count.get(ptype, 0) + 1
            if not progressed:
                allow_brand += 1
                allow_type += 1

    selected_set = set(selected)
    front = [candidates[i] for i in sorted(selected_set)]  # ordinea de relevanță (top-1 primul)
    rest = [candidates[i] for i in range(n) if i not in selected_set]
    return front + rest


def _searchable_facets(ctx: TurnContext) -> tuple[str, ...]:
    """Tier 2b p2: cheile de attributes filtrabile de search (DomainPack.searchable_facets), gated
    de kill-switch. OFF / fără pack → () → fără filtru de feature."""
    if not get_settings().facet_search_enabled:
        return ()
    pack = getattr(ctx.business, "domain_pack", None)
    return pack.searchable_facets if pack else ()


# NX-266 — fațeta pe care o poartă `SearchArgs.price_max`. Numele e al REGISTRULUI de fațete (cel
# din pachet), nu al catalogului: `price` e cheia sub care fiecare pachet își declară prețul.
_PRICE_FACET = "price"


@dataclass(frozen=True, slots=True)
class _Constraints:
    """Ce a rămas din numerele cererii după tipizare: ce se poate executa, ce s-a respins și dacă
    prețul a devenit constrângere (caz în care `price_max` nu mai pleacă separat spre SQL)."""

    bounds: tuple[BoundConstraint, ...] = ()
    rejected: tuple[Rejection, ...] = ()
    owns_price: bool = False


def _typed_constraints(ctx: TurnContext, a: SearchArgs) -> _Constraints:
    """Numerele cererii → constrângeri EXECUTABILE (NX-266). Flag OFF → nimic, byte-identic.

    Două surse, tratate diferit fiindcă nu au aceeași autoritate:
      • mesajul BRUT al clientului — rostit, deci `user_explicit`, deci poate exclude;
      • `price_max` propus de model — devine `user_explicit` doar dacă `corroborated_by` (NX-251)
        confirmă că numărul chiar a fost rostit în turul ăsta. Altfel rămâne `model_inferred`.

    Distincția nu e decorativă: modelul poate deduce un buget din context („căutam ceva
    accesibil"), iar o deducție n-are voie să șteargă produse din catalog. Poarta e a codului, nu
    a modelului — exact ca la nevoi.

    Consecința practică: o constrângere `model_inferred` **nu intră** în stratul tipizat. Pentru
    preț asta înseamnă că `price_max` continuă să curgă pe calea veche, exact ca azi — singura
    constrângere din sistem care are o cale moștenită. A i-o tăia aici ar fi o schimbare de
    comportament care nu e a acestui card (și care ar LĂRGI rezultatele, nu le-ar îngusta).
    Pentru orice altă fațetă, o inferență nu are cale, deci pur și simplu nu exclude."""
    if not get_settings().typed_constraints_enabled:
        return _Constraints()
    pack = getattr(ctx.business, "domain_pack", None)
    units = getattr(pack, "units", None)
    if pack is None or units is None or not units.specs:
        return _Constraints()

    message = getattr(getattr(ctx, "message", None), "body", "") or ""
    spoken, rejected = extract_constraints(
        message, units=units, locale=ctx.language, source=SOURCE_USER
    )
    candidates = list(spoken)
    if a.price_max is not None:
        source = SOURCE_USER if corroborated_by(message, a.price_max) else SOURCE_MODEL
        proposed, rejection = constraint_from_value(
            _PRICE_FACET, OP_LTE, a.price_max, units=units, source=source
        )
        if proposed is not None:
            candidates.append(proposed)
        elif rejection is not None:
            rejected += (rejection,)

    # Inferențele se opresc AICI, înainte de compunere. Dacă ar intra în merge, o margine dedusă
    # s-ar putea combina cu una rostită și ar moșteni autoritatea ei — „sub 100 lei" (client) +
    # `price_max=80` (dedus) ar deveni o excludere la 80 pe care clientul n-a cerut-o.
    enforceable: list[TypedConstraint] = []
    for c in candidates:
        if c.source in ENFORCING_SOURCES:
            enforceable.append(c)
        else:
            rejected += (Rejection(REASON_INFERRED, facet=c.facet, detail=c.source),)

    merged, merge_rejected = merge_constraints(enforceable)
    bounds, bind_rejected = bind_constraints(merged, pack.facets)
    owns_price = any(_is_price(b) for b in bounds)
    return _Constraints(
        bounds=bounds,
        rejected=rejected + merge_rejected + bind_rejected,
        owns_price=owns_price,
    )


def _facet_label(ctx: TurnContext, key: str) -> str:
    """Eticheta de afișare a fațetei în limba turului (din registrul tipizat, NX-186). Fără pachet
    sau fără etichetă → cheia, care e tot un cuvânt lizibil. P11: eticheta vine din date."""
    pack = getattr(ctx.business, "domain_pack", None)
    for spec in getattr(pack, "facets", ()) or ():
        if spec.key == key:
            return spec.label(ctx.language, fallback_locale=getattr(ctx.business, "locale", None))
    return key


def _without_price(bounds: Sequence[BoundConstraint]) -> tuple[BoundConstraint, ...]:
    """Constrângerile FĂRĂ cele de preț — treapta de relaxare care renunța la `price_max` trebuie
    să renunțe la același lucru și când prețul călătorește ca valoare tipizată. Altfel migrarea lui
    `budget_max` pe noul contract ar schimba tăcut comportamentul scării."""
    return tuple(b for b in bounds if not _is_price(b))


def _is_price(bound: BoundConstraint) -> bool:
    """Constrângerea asta e pe preț? Se citește din LEGĂTURĂ (coloana reală), nu din numele
    fațetei: un pachet care își numește prețul altfel rămâne corect."""
    return bound.source_kind == "column" and bound.source_key in ("price", "sale_price")


def _displayed_ids(ctx: TurnContext) -> set[str]:
    """Id-urile produselor deja afișate (din `state.displayed_products`, ref-uri P8) — pentru
    dedup la „arată-mi altele". State gol / lipsă → set gol (fără efect)."""
    state = getattr(ctx, "state", None)
    if state is None:
        return set()
    return {str(p.product_id) for p in getattr(state, "displayed_products", [])}


def _safety_gate(
    ctx: TurnContext, products: list[dict[str, Any]], *, purpose: str
) -> tuple[list[dict[str, Any]], str]:
    """NX-173 (P0): scoate produsele contraindicate pentru contextul declarat, prin `SafetyPolicy`
    (punctul UNIC de decizie). Chemat de căile de catalog ÎNAINTE de pool/sesiune/vedere.

    NU mai întoarce text: nota internă de tip „EXCLUS determinist / REGULI DURE" trimisă modelului
    era jargon intern folosit ca pseudo-copy (review Codex). Fraza de siguranță e garantată de cod
    la compunere, o singură dată, localizată — vezi `safety/messages.py`.

    Întoarce `(păstrate, hint)` — `hint` = O linie de context pentru model (ca framing-ul lui
    comercial să fie coerent), NU copy. Fără context / kill-switch OFF → `("", pass-through)`."""
    kept, decision = SafetyPolicy.for_turn(ctx).gate(ctx, products, purpose=purpose)
    return kept, safety_model_hint(decision)


def _session_filters(
    a: SearchArgs,
    concern_keys: list[str] | None,
    features: list[str] | None = None,
    constraints: Sequence[BoundConstraint] = (),
) -> dict[str, Any]:
    """Setul canonic de filtre care DEFINEȘTE o sesiune de căutare (baza fp-ului). Rafinarea
    oricăruia (preț, concerns, features, brand...) schimbă fp → sesiune nouă (NX-119).

    NX-223 — REGULA: fp-ul conține ORICE argument care schimbă setul de rezultate. `variant_label`
    e filtru DUR în ambele retrievere (NX-135) și `product_name` schimbă diversificarea + named-miss
    disclosure; fără ele, „aveți în Warm Beige?" pe același text de query dădea fp identic →
    `continue_search_session` servea pool-ul VECHI, nefiltrat pe variantă. Un argument nou de
    filtrare adăugat fără cheie aici = bug tăcut → păzit de guard-ul de completitudine din teste.

    Cele două se NORMALIZEAZĂ (lower + fără diacritice, ca restul lanțului): „warm beige" ≡
    „Warm Beige" nu trebuie să spargă sesiunea degeaba (P11). String gol → `None` (același fp ca
    lipsa lui — exact truthiness-ul cu care retrieverul decide dacă aplică clauza).

    NX-266: constrângerile numerice intră în fp ca text canonic, dar **doar când există**. Fără
    ele, „mai arată-mi, dar cu SPF minim 50" ar avea fp identic cu turul dinainte și ar pagina
    pool-ul VECHI, nefiltrat pe număr — exact clasa de bug pe care o descrie NX-223, cu un filtru
    pe care clientul îl vede. Cheia lipsește când nu sunt constrângeri (nu e `None`), ca fp-ul cu
    flagul stins să rămână IDENTIC cu cel de dinainte de card — altfel toate sesiunile în curs ar
    fi invalidate de o schimbare care, pe flagul stins, nu face nimic."""
    out: dict[str, Any] = {
        "query": a.query,
        "category": a.category,
        "brand": a.brand,
        "concerns": concern_keys,
        "features": features,
        "price_max": a.price_max,
        "sort_mode": a.sort_mode,
        "in_stock_only": a.in_stock_only,
        "variant_label": normalize(a.variant_label) if a.variant_label else None,
        "product_name": normalize(a.product_name) if a.product_name else None,
    }
    if constraints:
        out["constraints"] = sorted(b.constraint.describe() for b in constraints)
    return out


def _fp(filters: dict[str, Any]) -> str:
    """Fingerprint determinist al filtrelor → invalidează sesiunea când se schimbă (rafinare)."""
    canon = json.dumps(filters, sort_keys=True, default=str)
    return hashlib.sha1(canon.encode()).hexdigest()[:16]


def _next_page(pool: list[str], cursor: int, seen: set[str], limit: int) -> tuple[list[str], int]:
    """Următoarea pagină de ≤`limit` id-uri NEVĂZUTE din `pool[cursor:]` → (ids, cursor_nou);
    cursor_nou trece peste tot ce s-a consumat (inclusiv id-urile sărite ca deja-văzute)."""
    page: list[str] = []
    i = cursor
    while i < len(pool) and len(page) < limit:
        if pool[i] not in seen:
            page.append(pool[i])
        i += 1
    return page, i


async def continue_search_session(
    ctx: TurnContext, deps: PipelineDeps, sess: dict[str, Any], limit: int
) -> ToolResult:
    """Servește pagina URMĂTOARE dintr-o sesiune activă (NX-119): paginare din `pool[cursor:]` FĂRĂ
    re-fetch/embed, unseen-dedup vs displayed, cursor monotonic. Folosit de `search_products_tool`
    (fp identic) ȘI de ramura deterministă „mai arată-mi" din agent (NX-119b). Pool epuizat / toate
    inactive → semnal determinist `_NO_MORE_VIEW` (P6). Scrie `ctx.state_patch` (persistă proc)."""
    pool: list[str] = sess.get("pool") or []
    cursor = int(sess.get("cursor") or 0)
    seen = _displayed_ids(ctx)
    page_ids, new_cursor = _next_page(pool, cursor, seen, limit)
    page = int(sess.get("page") or 0) + 1
    ctx.state_patch["active_search"] = {**sess, "cursor": new_cursor, "page": page}
    products: list[dict[str, Any]] = []
    if page_ids:
        # NX-171c: pagina servește produse NOI nevăzute din pool → respectă filtrul published
        # (spre deosebire de re-hidratarea produselor deja afișate, care NU se filtrează).
        async with deps.db("search_page_products") as conn:
            products = await get_products_by_ids(
                conn, ctx.business.id, page_ids, limit=limit, respect_content_status=True
            )
    # NX-173 (P0): pool-ul poate fi SEMĂNAT ÎNAINTE ca clientul să declare contextul („arată-mi
    # seruri" → „…dar sunt însărcinată" → „mai arată-mi") sau de un build fără gate. Re-filtrăm la
    # servire — pool-ul stocat nu e de încredere, doar ce iese pe pagină contează.
    products, _ = _safety_gate(ctx, products, purpose="page")
    if products:
        ctx.emit(
            "search_session",
            action="page",
            page_index=page,
            pool_size=len(pool),
            served=len(products),
            unseen=len(page_ids),
        )
        return ToolResult(
            ok=True,
            products=products,
            llm_view=_brief(products, getattr(ctx.business, "domain_pack", None), ctx.language),
        )
    ctx.emit(
        "search_session",
        action="exhausted",
        page_index=page,
        pool_size=len(pool),
        served=0,
        unseen=len(page_ids),
    )
    return ToolResult(ok=True, products=[], llm_view=_NO_MORE_VIEW)


# Răspunsul lui `has_embeddings` se schimbă doar când rulează jobul de embed, dar înainte se
# plătea un checkout întreg (set_config + select + reset = 3 round-trip-uri) la FIECARE căutare.
# Cache per tenant cu TTL-ul vocabularului: aceeași politică plictisitoare, același argument.
_EMBEDDINGS_TTL_S = 300.0
_embeddings_cache: dict[str, tuple[float, bool]] = {}


def clear_embeddings_cache() -> None:
    _embeddings_cache.clear()


async def _embeddings_available(deps: PipelineDeps, business_id: str) -> bool:
    now = time.monotonic()
    hit = _embeddings_cache.get(business_id)
    if hit is not None and (now - hit[0]) < _EMBEDDINGS_TTL_S:
        return hit[1]
    # NX-231: checkout scurt DOAR pentru verificare; embed-ul (extern, cu buget de timp propriu)
    # rulează cu poolul liber — el era exact locul unde o conexiune stătea blocată pe rețea.
    async with deps.db("has_embeddings") as conn:
        available = await has_embeddings(conn, business_id)
    _embeddings_cache[business_id] = (now, bool(available))
    return bool(available)


@register("search_products")
async def search_products_tool(
    ctx: TurnContext, deps: PipelineDeps, args: dict[str, Any]
) -> ToolResult:
    """Caută în catalog cu filtre dure (preț, categorie, brand, concerns). Întoarce până la
    6 produse REALE — niciodată „indisponibil".

    HIBRID (NX-113b): rulează AMÂNDOUĂ retrieverele pe pool (~50) — lexical REAL (FTS+pg_trgm,
    NX-113a) ȘI vector (când avem LLM + embeddings) — fuzionate prin RRF (`relevance`) sau
    re-sortate determinist (preț/rating). Filtrele dure care golesc tot se relaxează progresiv
    ÎNAINTE de a întoarce gol (P6). Înainte de trunchierea la 6: dedup vs `displayed_products`
    (paritate „arată altele", P8). Degradare grațioasă la lexical-only fără LLM/embeddings sau
    dacă `embed` pică. Singurul apel extern rămâne `embed([query])` (P2)."""
    a = SearchArgs(**args)
    # IZI-anti-drift: rafinare ÎN sesiune activă, fără categorie/nevoi NOI → moștenește-le pe ale
    # sesiunii (ține „raftul" curent). Bug „mai ieftin → mască/ser/toner": user scrie „mai ifetin"
    # (typo) → `cheaper_intent` (regex) ratează → modelul re-caută `price_asc` fără categorie →
    # drift pe alt raft. Moștenirea repară fără wordlist (model+context); DOAR când câmpul NU e
    # re-specificat (schimbare de subiect = modelul setează explicit category → fără moștenire).
    # Observabil prin `search_filter_inherited`.
    #
    # Rulează ÎNAINTEA rezoluției, deliberat: altfel termenii moșteniți ar sări peste confruntarea
    # cu catalogul și ar ajunge în `WHERE` neverificați — exact drumul pe care îl închidem.
    sessions_on = (
        get_settings().search_sessions_enabled
    )  # kill-switch (OFF → fiecare căutare fresh)
    sess_filters = (ctx.state.active_search or {}).get("filters") or {}
    inherited: list[str] = []
    if sessions_on and sess_filters:
        if a.category is None and sess_filters.get("category"):
            a.category = sess_filters["category"]
            inherited.append("category")
        if not a.concerns and sess_filters.get("concerns"):
            a.concerns = [str(x) for x in sess_filters["concerns"]]
            inherited.append("concerns")
        if inherited:
            ctx.emit("search_filter_inherited", fields=inherited)

    # NX-319: marginea de preț a modelului filtrează doar cu sursă (vezi `price_bound_source`).
    # Înaintea amprentei de sesiune, deliberat: o margine respinsă nu are voie să definească
    # sesiunea, altfel „mai arată-mi" ar pagina un pool tăiat la un preț pe care clientul nu l-a
    # cerut.
    if a.price_max is not None and get_settings().search_price_bound_provenance_enabled:
        texts = client_texts(ctx)
        source = price_bound_source(
            a.price_max,
            texts=texts,
            relative_request=_is_relative_price_request(texts[0]),
            session_price_max=sess_filters.get("price_max"),
        )
        ctx.emit("price_bound_provenance", source=source or "unsupported", kept=source is not None)
        if source is None:
            a.price_max = None

    # === REZOLVARE ÎNAINTE DE CONSTRÂNGERE =====================================================
    # Un filtru SQL nu e o comparație, e o execuție: `WHERE slug = 'ten'` nu întreabă dacă «ten»
    # există, ci întoarce 0 rânduri — același rezultat ca pentru un raft real, dar gol. Din
    # momentul ăla informația s-a pierdut și niciun strat de deasupra n-o mai poate recupera, așa
    # că „n-am găsit" devine singura interpretare disponibilă. De aceea fiecare termen venit de la
    # model trece întâi prin vocabularul DERIVAT din catalog și primește un verdict cu dovadă:
    # `KNOWN` (constrânge pe cheia reală), `AMBIGUOUS` (constrânge pe uniune — «cremă» rămâne o
    # cerere de cremă, deci măștile cu textură cremoasă nu mai câștigă pe text), `UNKNOWN` (nu
    # ajunge NICIODATĂ în WHERE; se raportează și i se spune modelului că filtrul n-a rulat).
    vocab = await get_vocabulary(deps, ctx.business.id)
    resolutions = _resolve_search_terms(ctx, a, vocab)
    category_keys = resolutions.category_keys
    facet_filters = resolutions.facet_filters
    unmapped_concerns = resolutions.unresolved
    for r in resolutions.emitted:
        ctx.emit(
            "vocabulary_resolved",
            dimension=r.dimension,
            status=r.status.value,
            matched_by=r.matched_by,
            reason=r.reason,
            n_keys=len(r.constraint_keys),
            evidence=r.evidence,  # câte produse susțin rezoluția — 0 doar la UNKNOWN
        )
    # Compatibilitate cu straturile care mai vorbesc despre „concerns" ca listă plată (rerank,
    # sesiune, telemetrie): cheile rezolvate, indiferent de dimensiunea din care provin.
    concern_keys = resolutions.flat_facet_keys or None
    # NX-298 — o variantă ÎNCERCATĂ ȘI RESPINSĂ, scrisă aici ca să nu fie reintrodusă: să SCĂDEM
    # din text termenii pe care filtrele îi poartă deja („cosuri", când `concerns=acne` e în
    # WHERE). Pare curat — aceeași cerere nu trebuie pusă de două ori — și pe hârtie repară exact
    # pool-ul de 5 din 518. Măsurat pe catalogul real, strică mai mult decât repară: la «vreau
    # ceva sa scap de cosuri» rămâne fără NICIUN cuvânt, deci fără niciun semnal de ordonare, iar
    # pagina devine „cele mai bine notate produse cu eticheta acnee": un aparat sonic de curățare
    # și un tonic, în locul plasturilor anti-acnee. Un termen poate fi redundant ca POARTĂ și
    # informativ ca ORDONATOR; scăderea le confundă. Reparația e la capătul celălalt: textul
    # rămâne întreg, iar treapta terminală îl coboară din poartă în ordonator (`filters_only`,
    # `db/queries/catalog.py`) și completează pagina de acolo.
    # Tier 2b p2: features („cu niacinamidă") → filtru pe searchable_facets, NORMALIZAT (lower+strip
    # diacritice, ca SQL) → „niacinamida"/„niacinamidă" se potrivesc. Fără searchable_facets → None.
    searchable_facets = _searchable_facets(ctx)
    norm_features: list[str] | None = None
    if searchable_facets and a.features:
        norm_features = [
            normalize(f) for f in a.features if isinstance(f, str) and f.strip()
        ] or None
    # NX-266: numerele cererii, tipizate. Cu flagul stins e `_Constraints()` gol, deci tot ce
    # urmează (fp, scară, SQL, plasă) e byte-identic cu azi.
    tc = _typed_constraints(ctx, a)
    # Migrarea lui `budget_max`: când prețul a devenit constrângere tipizată, NU mai pleacă și ca
    # `price_max` — ar fi același predicat de două ori, iar valoarea autoritară trebuie să fie una
    # singură. Predicatul rezultat e echivalent (fațeta declară `missing_value: skip`, adică un
    # produs fără preț cade, exact ca la `NULL <= x`).
    price_max_sql = None if tc.owns_price else a.price_max
    seen = _displayed_ids(ctx)
    filters = _session_filters(a, concern_keys, norm_features, tc.bounds)
    fp = _fp(filters)
    sess = ctx.state.active_search or {}

    # === CONTINUARE sesiune (NX-119): aceleași filtre (fp) + pool stocat → pagina URMĂTOARE,
    # FĂRĂ re-fetch/embed. Paginare deterministă (pool stabil, tie-break p.id) + unseen-dedup.
    if sessions_on and sess.get("fp") == fp and sess.get("pool"):
        return await continue_search_session(ctx, deps, sess, a.limit)

    # === SESIUNE NOUĂ: retrieval hibrid → pool stabil (top MAX_SEARCH_POOL) + prima pagină ===
    # NX-227: nevoile pe care `concern_map` nu le cunoaște = gol de vocabular, prioritizabil pe
    # frecvență reală (aceeași logică ca `unmet_query`, aplicată la vocabular, nu la produse).
    # Emis AICI, nu la mapare: pe continuarea de sesiune (pagina 2, 3...) e aceeași cerere, nu
    # una nouă — numărătoarea ar fi inflată de paginare. Doar termeni NORMALIZAȚI, scurți și
    # plafonați (P12: vocabular, nu text de user, fără PII).
    if unmapped_concerns:
        ctx.emit(
            "concern_unmapped",
            terms=[t[:_VARIANT_ATTR_MAX] for t in unmapped_concerns[:_UNMAPPED_TERMS_MAX]],
            locale=ctx.language,
            category_key=a.category,
        )
    # NX-299 — PROVENIENȚA constrângerilor soft, pentru ordinea de relaxare. Se corroborează
    # ARGUMENTUL pe care l-a trimis modelul (`a.category`, `a.concerns`), nu cheia rezolvată:
    # modelul TRANSCRIE ce a scris clientul, codul confirmă (NX-251). Pe turul măsurat, clientul
    # a scris «cosuri», modelul a trimis `concerns=["coșuri"]` (coroborat) și cheia s-a rezolvat
    # `acne` — cuvânt pe care clientul nu l-a rostit niciodată. Confruntarea cu cheia ar fi
    # declarat fațeta drept ghicită, adică exact inversul adevărului.
    #
    # Cheile rezolvate se adaugă TOTUȘI pe categorie, fiindcă acolo cresc doar șansa unui `True`,
    # iar `True` e partea conservatoare (nu relaxăm). Un filtru MOȘTENIT din sesiune (NX-119) e
    # tratat ca rostit: a fost stabilit pe un tur anterior al aceluiași client, iar a-l relaxa aici
    # ar arunca raftul pe care tocmai naviga.
    by_provenance = getattr(get_settings(), "search_relax_by_provenance_enabled", False)
    hard_category = getattr(get_settings(), "search_category_hard_enabled", True)
    category_uttered = "category" in inherited or uttered_by_client(ctx, a.category, *category_keys)
    # NX-319: coroborarea literală nu deosebește un RAFT de un cuvânt obișnuit care îi poartă numele
    # („crema de fata" ≠ raftul Machiaj > Fata). Un subraft rostit fără rădăcina lui devine ipoteză,
    # deci îl judecă NX-313 pe date, mai jos. Moștenit din sesiune rămâne rostit (NX-299).
    if (
        category_uttered
        and "category" not in inherited
        and get_settings().search_subshelf_homograph_guard_enabled
        and named_only_as_subshelf(vocab, category_keys, client_texts(ctx))
    ):
        category_uttered = False
        ctx.emit("category_subshelf_homograph", category_key=category_keys[0])
    facets_uttered = "concerns" in inherited or uttered_by_client(ctx, *(a.concerns or []))
    ladder = _relax_ladder(
        price_max=price_max_sql,
        facet_filters=facet_filters,
        category=category_keys,
        in_stock_only=a.in_stock_only,
        features=norm_features,
        constraints=tc.bounds,
        category_uttered=category_uttered,
        facets_uttered=facets_uttered,
    )
    if category_keys and not category_uttered:
        # Raftul e o IPOTEZĂ a modelului, nu o cerere. Se emite indiferent dacă treapta apucă să
        # ruleze: „am ghicit un raft" și „am ghicit un raft prost" sunt întrebări diferite, iar
        # prima e numitorul celei de-a doua.
        ctx.emit(
            "category_inferred",
            category_key=category_keys[0],
            category_evidence=(resolutions.category.evidence if resolutions.category else 0),
            facet_evidence=max(
                (r.evidence for r in resolutions.emitted if r.dimension != CATEGORY_DIMENSION),
                default=0,
            ),
            facets_uttered=facets_uttered,
        )

    # Vector de query: O SINGURĂ DATĂ (P2), doar cu LLM + embeddings. Dacă `embed` pică → None →
    # degradare grațioasă la lexical-only (P6), fără tăcere.
    # NX-225: LENTOAREA furnizorului = același drum ca EȘECUL lui. Un embed care răspunde în 8s nu
    # aruncă nimic, dar ține tot turul (clientul stă pe typing indicator) deși lexicalul e gata în
    # milisecunde → buget de timp explicit (P4). Două events distincte: lent (`embed_timeout`) vs
    # mort (`embed_failed`) — fără ele degradarea semantică rămâne invizibilă (layer mort tăcut).
    query_vec: list[float] | None = None
    embeddings_available = False
    # `search_semantic_enabled` OFF (default, decizie 2026-09-08) = brațul vector nu există:
    # niciun embed, niciun checkout de verificare — căutarea e scara lexicală + filtre.
    if deps.llm is not None and get_settings().search_semantic_enabled:
        embeddings_available = await _embeddings_available(deps, ctx.business.id)
    if embeddings_available:
        timeout_ms = get_settings().embed_timeout_ms
        try:
            call = deps.llm.embed([a.query])
            # 0 = kill-switch numeric → await direct, fără wait_for (comportamentul de dinainte).
            if timeout_ms > 0:
                call = asyncio.wait_for(call, timeout=timeout_ms / 1000)
            query_vec = (await call)[0]
        except TimeoutError as e:
            # `embed_timeout` înseamnă „bugetul NOSTRU a tăiat" (adaptorul ridică
            # `openai.APITimeoutError`, nu builtin-ul — llm.py). Cu bugetul oprit nu putem tăia
            # nimic, deci un TimeoutError venit de altundeva e „mort", nu „lent": nu raportăm
            # `embed_timeout{timeout_ms: 0}`, care ar minți dashboardul.
            if timeout_ms > 0:
                ctx.emit("embed_timeout", timeout_ms=timeout_ms)
            else:
                ctx.emit("embed_failed", error_type=type(e).__name__)
            query_vec = None
        except Exception as e:  # noqa: BLE001 — embed/rețea pică → cădem pe lexical-only (P6)
            ctx.emit("embed_failed", error_type=type(e).__name__)  # tipul, NU mesajul (P12)
            query_vec = None

    # ARCH-2026 P0: ponderile scorului blended (din DomainPack / defaults); None = kill-switch OFF
    # (RRF pur). Calculate O DATĂ (nu se schimbă între treptele de relaxare).
    rank_weights = _rank_weights(ctx)
    # NX-303 — textul mai adaugă ceva peste filtre, sau doar taie arbitrar? Calculat O DATĂ (nu se
    # schimbă între trepte) și doar pe prima treaptă a scării: pe treptele de relaxare filtrele
    # sunt deja mai slabe, deci „purtat de filtre" ar fi o afirmație despre alt WHERE.
    text_redundant = _text_gate_is_redundant(ctx, a, vocab, resolutions)
    if text_redundant:
        ctx.emit(
            "text_gate_skipped",
            reason="carried_by_filters",
            n_terms=len(content_terms(a.query, ctx.language)),
        )
    ranked_final: list[dict[str, Any]] = []  # ordinea fuzionată+re-rankată la treapta care a produs
    vector_final: list[dict[str, Any]] = []
    relaxed = False
    winning_step: dict[str, Any] | None = None  # treapta din ladder care a produs rezultate
    had_any_match = False  # vreun retriever a întors ceva (semnal brand-not-found)
    relax_depth = 0  # treapta de relaxare la care s-a oprit (0 = filtre stricte)
    lexical_pool_n = vector_pool_n = 0  # mărimea pool-urilor la treapta finală
    top_cosine = None  # cea mai mică distanță cosine (cel mai apropiat vector) — semnal de calitate
    guessed_category_dropped = False  # raftul GHICIT a fost scos de NX-305/313 (vezi `Relevance`)
    # NX-231: treptele de relaxare sunt PUR DB (fuziunea/rankarea sunt cod pur, fără await extern)
    # → un singur checkout pentru toată scara, eliberat înainte de restul tool-ului. Embed-ul a
    # rulat deja, mai sus, cu poolul liber.
    async with deps.db("search_products_ladder") as conn:
        for i, f in enumerate(ladder):
            lexical = await search_products_lexical(
                conn,
                ctx.business.id,
                query_text=a.query,
                price_max=f["price_max"],
                constraints=f["constraints"],  # NX-266: numerele, pe aceeași treaptă
                facet_filters=f["facet_filters"],
                features=f["features"],
                searchable_facets=searchable_facets,
                variant_label=a.variant_label,  # NX-135: filtru DUR (nu se relaxează), ca brand
                category=f["category"],
                brand=a.brand,
                sort_mode=a.sort_mode,
                in_stock_only=f["in_stock_only"],
                locale=ctx.language,  # 046: locala alege lista de cuvinte goale (P11)
                # NX-293: a renunța la CUVINTELE clientului e ultima concesie din sistem, deci se
                # oferă abia pe ULTIMA treaptă de filtre. Ordinea contează și e ușor de greșit:
                # oferită pe treapta 0, ar servi setul filtrelor stricte ÎNAINTE să fi încercat
                # măcar o relaxare de filtre cu textul încă în joc — adică ar prefera „ce am pe
                # raft" în locul unui răspuns care chiar potrivește ce a cerut clientul, doar
                # fiindcă o fațetă era prea îngustă. Precizia întâi pe AMBELE axe, nu doar pe a ei.
                allow_filters_only=(i == len(ladder) - 1),
                pool=_FUSION_POOL,
            )
            vector: list[dict[str, Any]] = []
            if query_vec is not None:
                try:
                    vector = await search_products_semantic(
                        conn,
                        ctx.business.id,
                        query_vec,
                        price_max=f["price_max"],
                        constraints=f["constraints"],  # NX-266: identic pe ambele retrievere
                        facet_filters=f["facet_filters"],
                        features=f["features"],
                        searchable_facets=searchable_facets,
                        variant_label=a.variant_label,  # NX-135: filtru DUR pe variantă
                        category=f["category"],
                        brand=a.brand,  # brand = filtru DUR și pe vector (nu se relaxează)
                        sort_mode=a.sort_mode,
                        in_stock_only=f["in_stock_only"],
                        pool=_FUSION_POOL,
                    )
                except Exception:  # noqa: BLE001 — semantic pică în tur → lexical rămâne (P6)
                    vector = []
            relax_depth, lexical_pool_n, vector_pool_n = i, len(lexical), len(vector)
            cosines = [p["cosine_distance"] for p in vector if p.get("cosine_distance") is not None]
            if cosines:
                top_cosine = min(cosines)
            ranked = fuse_candidates(
                lexical, vector, sort_mode=a.sort_mode, concerns=concern_keys, weights=rank_weights
            )
            had_any_match = had_any_match or bool(ranked)
            if ranked:
                ranked_final = ranked
                vector_final = vector
                relaxed = i > 0
                winning_step = f
                break

        # NX-305 — filtrul GHICIT trebuie să-și merite locul.
        #
        # Turul real `78b347fa` (`sole-ro`, «cat cost una?» despre mănușa de aplicare pe care botul
        # tocmai o pomenise): modelul a trimis `category="accesorii"` (adică `Machiaj > Accesorii`,
        # raftul pensulelor) și `concerns=["autobronzant","aplicare"]`, din care „autobronzant" s-a
        # rezolvat pe `product_type` cu 7 produse. Cele două erau AMÂNDOUĂ ghicituri ale modelului,
        # se contraziceau, iar intersecția era goală. Scara de relaxare le-a ordonat după TIP, cum
        # face de la NX-299 când proveniența e la egalitate, deci a păstrat-o pe cea GROSIERĂ
        # (raftul) și a aruncat-o pe cea SPECIFICĂ (tipul): `relaxed_any`, seturi de pensule de
        # 1.300 de lei. Aceeași interogare fără filtrele ghicite aterizează pe `strict` și scoate pe
        # locul 1 mănușa de 50 de lei, de pe raftul din care botul recomandase cu patru mesaje mai
        # devreme.
        #
        # Regula NU e „raftul conversației câștigă". Varianta aia a fost încercată și MĂSURATĂ:
        # cu `category="creme-autobronzante-si-bronzere"` interogarea cade pe `filters_only` și
        # servește șase geluri autobronzante, adică tot nu mănușa. Filtrele ghicite nu trebuie
        # ÎNLOCUITE, ci SCOASE, iar textul lăsat să răspundă singur.
        #
        # Condițiile trăiesc în `should_rescue_guessed_filters` / `rescue_wins` — PURE, deci
        # testabile fără DB, și un singur loc unde se citește regula.
        #
        # Rulează în ACELAȘI checkout ca scara (e tot muncă pură de DB, fără await extern, NX-231).
        #
        # NX-313 — judecata se generalizează: se scot DOAR filtrele ghicite (cele rostite rămân în
        # interogarea de probă), iar proba rulează și pe potrivirile curate, fiindcă un raft greșit
        # în care textul prinde ceva arată exact ca o potrivire curată. Regula trăiește în
        # `should_probe_guessed_filters` / `guessed_filter_verdict`, tot PURE. Cu flagul stins,
        # blocul de mai jos e NX-305 byte-identic.
        settings_now = get_settings()
        coherence = settings_now.search_guessed_filter_coherence_enabled
        category_guessed = bool(category_keys) and not category_uttered
        facets_guessed = bool(facet_filters) and not facets_uttered
        has_terms = bool(content_terms(a.query, ctx.language))
        if coherence:
            probe = should_probe_guessed_filters(
                enabled=settings_now.search_guessed_filter_rescue_enabled,
                category_guessed=category_guessed,
                facets_guessed=facets_guessed,
                has_content_terms=has_terms,
            )
        else:
            probe = should_rescue_guessed_filters(
                enabled=settings_now.search_guessed_filter_rescue_enabled,
                category_uttered=category_uttered,
                facets_uttered=facets_uttered,
                has_guessed_subject=bool(category_keys or facet_filters),
                has_content_terms=has_terms,
                degraded=relaxed or _rung_of(ranked_final) != "strict",
            )
            category_guessed = facets_guessed = True  # NX-305: scoate tot
        if probe:
            base = ladder[0]
            kept_category = base["category"] if not category_guessed else ()
            kept_facets = base["facet_filters"] if not facets_guessed else {}
            rescued = await search_products_lexical(
                conn,
                ctx.business.id,
                query_text=a.query,
                price_max=base["price_max"],
                constraints=base["constraints"],
                facet_filters=kept_facets or {},
                features=base["features"],
                searchable_facets=searchable_facets,
                variant_label=a.variant_label,
                category=kept_category or (),
                brand=a.brand,  # ROSTIT sau nu, brandul nu se relaxează niciodată (NX-135)
                sort_mode=a.sort_mode,
                in_stock_only=base["in_stock_only"],
                locale=ctx.language,
                # Fără filtru de subiect, `filters_only` ar însemna „catalogul ordonat după rating".
                # E exact zgomotul de care se apără `_lexical_steps`, deci nu se cere.
                allow_filters_only=False,
                pool=_FUSION_POOL,
            )
            if coherence:
                reason, share = guessed_filter_verdict(
                    ranked_final,
                    rescued,
                    comparable=not relaxed,
                    min_share=settings_now.search_guessed_filter_min_share,
                    min_rows=settings_now.search_guessed_filter_min_rows,
                )
                # Proba se publică ORICUM, și când nu decide: pragul e o primă calibrare, iar
                # recalibrarea cere distribuția proporției pe trafic, nu doar pe turele adoptate.
                ctx.emit(
                    "guessed_filter_probe",
                    category_guessed=category_guessed,
                    facets_guessed=facets_guessed,
                    share=round(share, 3) if share is not None else None,
                    verdict=reason or "kept",
                    before=len(ranked_final),
                    after=len(rescued),
                )
                adopt = reason is not None
            else:
                reason, adopt = "better_rung", rescue_wins(ranked_final, rescued)
            if adopt:
                guessed_category_dropped = bool(category_keys) and category_guessed
                ctx.emit(
                    "guessed_filter_rescued",
                    dropped_category=bool(category_keys) and category_guessed,
                    dropped_facets=len(facet_filters) if facets_guessed else 0,
                    reason=reason,
                    from_step=_rung_of(ranked_final),
                    to_step=_rung_of(rescued),
                    before=len(ranked_final),
                    after=len(rescued),
                )
                ranked_final = fuse_candidates(
                    rescued,
                    [],
                    sort_mode=a.sort_mode,
                    concerns=concern_keys,
                    weights=rank_weights,
                )
                vector_final = []
                relaxed = False
                relax_depth = 0
                lexical_pool_n = len(rescued)
                # Treapta câștigătoare devine cea FĂRĂ filtrele ghicite, altfel completarea NX-298
                # de mai jos ar umple pagina exact din raftul pe care tocmai l-am scos. Filtrele
                # ROSTITE rămân (NX-313): completarea vine din fațeta pe care a cerut-o clientul.
                winning_step = {
                    **base,
                    "category": kept_category or (),
                    "facet_filters": kept_facets or {},
                }

        # NX-298: pagina nu s-a umplut, iar cererea NUMEȘTE un set (raft/fațetă/brand/variantă).
        # Sloturile rămase se completează din setul filtrului, ordonat după ACELEAȘI cuvinte ale
        # clientului (treapta `filters_only`, unde textul e ordonator, nu poartă). Trei proprietăți
        # care o țin onestă:
        #   • nu ÎNLOCUIEȘTE nimic — potrivirile de text rămân primele, în ordinea lor; completarea
        #     vine strict după, deci un răspuns bun nu poate fi împins în jos de raft;
        #   • nu poate ieși din cerere — rândurile sunt prin construcție o submulțime a ceea ce
        #     filtrele TREPTEI CÂȘTIGĂTOARE permit (aceleași filtre, fără predicatul de text);
        #   • nu e tăcută — fiecare rând adus așa poartă `lexical_step='filters_only'`, care ajunge
        #     la model (`_brief`), deci se prezintă „asta am pe raft", nu „uite ce ai cerut".
        # De ce e nevoie de ea, deși scara are deja `filters_only` pe ultima treaptă: aceea se
        # atinge doar când textul n-a găsit NIMIC. Cazul măsurat e celălalt — textul a găsit CINCI
        # din 518, iar cinci e destul cât să oprească scara și prea puțin cât să fie un răspuns.
        # NX-303 — al doilea motiv pentru care merită cerut restul setului, pe lângă „pagina nu s-a
        # umplut": textul a umplut-o, dar ca POARTĂ nu spunea nimic peste filtre. Diferența e că
        # aici nu completăm PAGINA (e plină, și pe bună dreptate: sunt potriviri literale), ci
        # POOL-UL — `pool_ids` se seamănă mai jos din `ranked_final` și e tot ce va avea „mai
        # arată-mi". Măsurat pe turul real: 6 rânduri dintr-un set de 518.
        #
        # ORDINEA rămâne a NX-298 și nu se negociază: potrivirile de text stau primele, completarea
        # strict după. O variantă care extindea pool-ul ÎNAINTE de fuziune a fost încercată și
        # respinsă pe măsurătoare — rerank-ul reordona tot, iar SKIN1004 BHA Foam (spumă cu BHA,
        # chiar despre coșuri) ieșea din pagină, înlocuită de un toner de strălucire.
        fill_n = 0
        pool_tail: list[dict[str, Any]] = []
        if (
            get_settings().search_fill_from_subject_filter_enabled
            and winning_step is not None
            and len(ranked_final) < a.limit
        ):
            # Cu `search_filters_only_fallback_enabled` stins, treapta nu există, deci lista vine
            # goală și completarea e un no-op: kill-switch-urile se compun, nu se contrazic.
            extra = await search_products_lexical(
                conn,
                ctx.business.id,
                query_text=a.query,
                price_max=winning_step["price_max"],
                constraints=winning_step["constraints"],
                facet_filters=winning_step["facet_filters"],
                features=winning_step["features"],
                searchable_facets=searchable_facets,
                variant_label=a.variant_label,
                category=winning_step["category"],
                brand=a.brand,
                sort_mode=a.sort_mode,
                in_stock_only=winning_step["in_stock_only"],
                locale=ctx.language,
                allow_filters_only=True,
                only_filters_step=True,
                pool=_FUSION_POOL,
            )
            have = {str(p.get("id")) for p in ranked_final}
            for p in extra:
                if len(ranked_final) >= a.limit:
                    break
                if str(p.get("id")) in have:
                    continue
                ranked_final.append(p)
                fill_n += 1

        # NX-303 — coada de POOL. Textul a umplut pagina, dar ca POARTĂ nu spunea nimic peste
        # filtre, deci „mai arată-mi" ar fi avut 6 rânduri dintr-un set de 518. Rândurile se
        # COLECTEAZĂ aici (cât mai avem conexiunea) și se aplică abia DUPĂ diversificare: pagina
        # rămâne exact cea de azi, byte-identic, iar coada intră doar în `pool_ids`.
        #
        # Două variante anterioare, măsurate și respinse, ca să nu fie reintroduse:
        #   • extinderea pool-ului ÎNAINTE de fuziune — rerank-ul reordona tot, iar SKIN1004 BHA
        #     Foam (spumă cu BHA, chiar despre coșuri) ieșea din pagină, înlocuită de un toner;
        #   • extinderea în blocul NX-298 de mai sus — `diversify_pool` primea 50 de rânduri și
        #     rearanja pagina, scoțând plasturii, adică exact clasa cea mai potrivită cererii.
        # Ambele au aceeași formă: au atins PAGINA, când ce lipsea era MATERIALUL de după ea.
        if text_redundant and winning_step is not None:
            pool_tail = await _subject_filter_tail(
                conn, ctx, a, winning_step, searchable_facets=searchable_facets
            )

    # NX-173 (P0): gate de contraindicații pe setul FUZIONAT, ÎNAINTE de diversificare/pool/pagină.
    # Poziția e esențială: `pool_ids` (mai jos) semănează sesiunea din `ranked_final`, iar sesiunea
    # supraviețuiește turului → un produs filtrat mai târziu (ex. la `annotate_reasons`, după pool)
    # ar rămâne în `active_search.pool` și ar reapărea la „arată-mi altele". Filtrat aici, dispare
    # din pool, pagină, `llm_view`, `ctx.retrieval`, carduri și `displayed_products` deodată.
    ranked_final, safety_hint = _safety_gate(ctx, ranked_final, purpose="search")

    # NX-266: plasa de DUPĂ fuziune/rerankare, în ACELAȘI loc cu gate-ul de siguranță și din
    # același motiv: pool-ul sesiunii se seamănă mai jos din `ranked_final` și supraviețuiește
    # turului, deci un produs filtrat mai târziu ar reapărea la „arată-mi altele". Constrângerile
    # sunt cele ale TREPTEI CÂȘTIGĂTOARE — dacă scara a renunțat la preț ca să iasă ceva, plasa nu
    # are voie să-l reaplice; ar goli exact rezultatele pe care relaxarea le-a produs.
    step_bounds: tuple[BoundConstraint, ...] = (
        tuple(winning_step.get("constraints") or ()) if winning_step else tc.bounds
    )
    if step_bounds:
        before = len(ranked_final)
        ranked_final, constraint_stats = apply_constraints(ranked_final, step_bounds)
        for bound in step_bounds:
            counts = constraint_stats[bound.facet]
            ctx.emit(
                "constraint_applied",
                facet=bound.facet,
                op=bound.constraint.op,
                unit=bound.constraint.unit,
                source=bound.constraint.source,
                # P12: numărătoare + vocabular de config, NICIODATĂ valoarea rostită de client.
                matched=counts[MATCH],
                mismatched=counts[MISMATCH],
                unknown=counts[UNKNOWN],
                dropped_total=before - len(ranked_final),
            )

    # NX-134: diversificare sortiment — reordonează pool-ul ca prima pagină să acopere scara de preț
    # + branduri (nu top-N clone). DOAR pe `relevance` (sort explicit = ordinea cerută de client,
    # neatinsă) și NU pe produs numit (A1: căutăm exact acel produs). Top-1/pick-ul nu se mișcă.
    diversified = False
    if (
        get_settings().search_diversify_enabled
        and a.sort_mode == "relevance"
        and a.product_name is None
        and len(ranked_final) > a.limit
    ):
        ranked_final = diversify_pool(ranked_final, a.limit)
        diversified = True

    # NX-313: un card per FAMILIE pe pagină. Clientul vede numele SCURT (`display_name`, NX-301),
    # iar nuanțele și gramajele aceluiași produs îl au identic: pe traficul real 10 din 76 de ture
    # cu ≥2 carduri (13%) arătau două carduri cu același nume, iar turul `bcd8e5c6` avea patru
    # „VILLAGE 11 FACTORY MY Skin Fit BB Cream" din șase. Nu se scoate nimic: repetițiile trec
    # după toate primele apariții, deci rămân în pool pentru „mai arată-mi". Pe produs numit căutăm
    # exact acel produs (și nuanța lui), deci acolo nu se aplică.
    #
    # NX-319: și pe sort EXPLICIT. Condiția de dinainte copia diversificarea („pe sort explicit
    # ordinea e a clientului"), dar reordonarea asta nu schimbă ordinea dintre produse DIFERITE:
    # primele apariții rămân exact în ordinea cerută, doar gemenii coboară. Turul real `8aaec031`
    # («da ceva aparat as vrea», `price_asc`) a arătat cinci role GESKE cu același nume din șase.
    if get_settings().search_one_card_per_family_enabled and a.product_name is None:
        ranked_final = one_per_family(ranked_final)

    # NX-303: coada intră ABIA acum, după ce pagina a fost aleasă. `pool_ids` (mai jos) e tot ce
    # va avea paginarea; pagina însăși rămâne cea de dinainte de card, byte-identic.
    if pool_tail:
        have = {str(p.get("id")) for p in ranked_final}
        added = 0
        for p in pool_tail:
            if len(ranked_final) >= MAX_SEARCH_POOL:
                break
            if str(p.get("id")) in have:
                continue
            ranked_final.append(p)
            added += 1
        ctx.emit("pool_extended_from_filter", page=len(have), added=added)

    # Pool-ul sesiunii = ordinea fuzionată COMPLETĂ (top MAX_SEARCH_POOL), NU dedup-uită: dacă l-am
    # semăna din setul minus-displayed, produsele deja afișate ar fi excluse PERMANENT din sesiune +
    # epuizare falsă (review #1). Prima pagină se servește prin ACELAȘI `_next_page` ca paginarea
    # (unseen-dedup vs displayed, P8) → cursorul reflectă poziția în pool, paritate cu paginarea.
    pool_ids = [str(p["id"]) for p in ranked_final][:MAX_SEARCH_POOL]
    by_id = {str(p["id"]): p for p in ranked_final}
    page_ids, cursor = _next_page(pool_ids, 0, seen, a.limit)
    products = [by_id[i] for i in page_ids]
    # NX-135: search filtrat pe variant_label → TOATE rezultatele au varianta cerută (construcție).
    # Marcăm fiecare produs → `_brief` îl semnalează → modelul scrie fit grounded, nu inventat.
    if a.variant_label:
        for p in products:
            p["variant_match"] = True
    # mode=semantic DOAR dacă un produs din vector a SUPRAVIEȚUIT în pagina întoarsă (nu doar
    # „vectorul a întors ceva"): dedup/RRF pot elimina toate hiturile vector → altfel minte.
    vector_ids = {str(v["id"]) for v in vector_final}
    vector_contributed = any(i in vector_ids for i in page_ids)

    # mode=lexical = semnal că jobul de embed trebuie rulat pe tenant (fără vector); =semantic când
    # vectorul a contribuit la setul ÎNTORS. `fused` = ambele retrievere au întors candidați la
    # treapta finală. FĂRĂ `query`/`concerns` text (P12 — doar flag-uri/counts/distanță numerică).
    mode = "semantic" if vector_contributed else "lexical"
    # NX-163: produs NUMIT cerut dar absent din setul întors — precomputat aici (o dată) fiindcă e
    # și semnalul de unmet «named_not_found» (mai jos) și condiția de disclosure (nota de mai jos).
    named_miss = bool(a.product_name) and not _named_product_found(a.product_name, products)
    # Treapta de TEXT care a servit pagina, calculată o dată: o citesc și evenimentul de căutare, și
    # captura de cerere neîmplinită de mai jos (NX-293 — un set servit din filtre înseamnă că
    # formularea n-a găsit nimic, chiar dacă raftul a găsit).
    served_step = next((p["lexical_step"] for p in products if p.get("lexical_step")), "strict")
    ctx.emit(
        "product_search",
        mode=mode,
        count=len(products),
        had_price_filter=a.price_max is not None,
        had_category=a.category is not None,
        had_brand=a.brand is not None,
        n_concerns=len(concern_keys or []),
        relaxed=relaxed,
        # 046: pe ce treaptă a potrivirii de TEXT s-a servit pagina. `relaxed`/`relax_depth` de mai
        # sus descriu relaxarea FILTRELOR; asta e cealaltă axă, iar confundarea lor ar ascunde exact
        # cazul periculos: cerere fără filtre, servită din plasa de typo. `strict` = cererea a
        # potrivit cum a fost formulată.
        lexical_step=served_step,
        fused=bool(lexical_pool_n) and bool(vector_pool_n),
        lexical_pool=lexical_pool_n,
        vector_pool=vector_pool_n,
        relax_depth=relax_depth,
        zero_result=not products,
        top_cosine_distance=top_cosine,
        had_variant_label=a.variant_label
        is not None,  # NX-135: căutare de variantă (nuanță/mărime)
        diversified=diversified,  # NX-134: prima pagină a fost re-compusă divers
        brands_in_result=len({p.get("brand") for p in products if p.get("brand")}),
        # NX-298: câte sloturi din pagină le-a umplut RAFTUL, fiindcă formularea clientului n-a
        # ajuns pentru atâtea. Zero = textul a fost destul. Mare și des = limba catalogului și
        # limba clienților au divergit, iar asta se repară în date (sinonime, `search_document`),
        # nu în scara de căutare.
        filled_from_filter=fill_n,
        types_in_result=len({_product_type(p) for p in products if _product_type(p)}),
        # NX-163 Demand Capture: ce s-a cerut, ca ref-uri/atribute NORMALIZATE (P8/P12) →
        # raportul de cerere (NX-164). `category_key`/`brand` = filtrele cerute (structurate de
        # triaj, nu text de user); `top_product_ids` = ce a întors search-ul. FĂRĂ query brut/PII.
        top_product_ids=product_ids_from_dicts(products),
        category_key=a.category,
        brand=a.brand,
    )
    # NX-163: cerere neîmplinită = gap de catalog, capturat determinist la sursă (nu inferență LLM).
    # Exclusiv, ca o singură căutare să nu se dubleze: «named_not_found» (produs numit, absent) e
    # mai specific decât «no_result» (nimic nu s-a potrivit — `had_any_match` False peste toate
    # treptele de relaxare). Emis ÎNAINTE de early-return-ul de brand-absent, ca „brand X, 0
    # rezultate" (cel mai valoros semnal) să NU fie pierdut. Doar atribute normalizate + locale.
    if named_miss:
        ctx.emit(
            "unmet_query",
            reason="named_not_found",
            category_key=a.category,
            brand=a.brand,
            locale=ctx.language,
        )
    elif a.variant_label and not products:
        # NX-163b: `variant_label` e filtru DUR (NX-135) → zero rezultate NU spune singur dacă
        # lipsește PRODUSUL sau doar VARIANTA. Verificăm o dată, DOAR pe calea de eșec: aceeași
        # căutare fără filtrul de variantă. Produs prezent → «missing_variant» (extinde gama);
        # absent → cade pe «no_result» (adu în catalog). Precizia contează: eticheta greșită aici
        # trimite comerciantul să cumpere stoc de care n-are nevoie.
        async with deps.db("search_variant_probe") as conn:
            base = await search_products_lexical(
                conn,
                ctx.business.id,
                query_text=a.query,
                category=a.category,
                brand=a.brand,
                searchable_facets=searchable_facets,
                locale=ctx.language,  # 046: aceeași normalizare ca pe calea principală
                pool=6,
            )
        ctx.emit(
            "unmet_query",
            reason="missing_variant" if base else "no_result",
            # Atribut NORMALIZAT (nuanță/mărime), nu text de user: lower + fără diacritice + cap.
            variant_attr=normalize(a.variant_label)[:_VARIANT_ATTR_MAX] if base else None,
            product_ids=product_ids_from_dicts(base) if base else None,
            category_key=a.category,
            brand=a.brand,
            locale=ctx.language,
        )
    elif not had_any_match:
        ctx.emit(
            "unmet_query",
            reason="no_result",
            category_key=a.category,
            brand=a.brand,
            locale=ctx.language,
        )
    elif served_step == "filters_only":
        # NX-293: raftul a răspuns, formularea NU. Fără branch-ul ăsta, fixul ar fi stins tăcut
        # semnalul de gap din catalog: înainte turul ieșea cu `no_result` (marfă lipsă), acum iese
        # cu produse, deci n-ar mai emite nimic — și exact cazul interesant („am raftul, dar n-am
        # NIMIC pentru ce a cerut clientul") ar dispărea din raportul de cerere fix când devine
        # măsurabil. Motiv SEPARAT, nu `no_result`: ăla înseamnă „n-am marfa", ăsta „am marfa,
        # n-am potrivirea" — două acțiuni diferite pentru comerciant.
        ctx.emit(
            "unmet_query",
            reason="text_unmatched",
            category_key=a.category,
            brand=a.brand,
            locale=ctx.language,
        )
    # NX-119: semează sesiunea de căutare — DOAR id-uri (pool, cap MAX_SEARCH_POOL) + cursor + fp +
    # filtre mici (P8). Următorul „mai arată-mi" (fp identic) paginează din pool fără re-fetch. Doar
    # dacă avem rezultate (zero → nicio sesiune de paginat). Owner scriere: processor (state_patch).
    if products and sessions_on:
        ctx.state_patch["active_search"] = {
            "filters": filters,
            "pool": pool_ids,
            "cursor": cursor,
            "fp": fp,
            "page": 0,
        }
        ctx.emit(
            "search_session",
            action="new",
            page_index=0,
            pool_size=len(pool_ids),
            served=len(products),
            unseen=len(page_ids),
        )
    # Brand cerut + ZERO match real (nu doar zero după dedup) = brandul nu e în catalog. Semnal
    # EXPLICIT pentru agent („nu lucrăm cu brandul X"), nu prezenta alt brand ca al lui (CAT-001).
    # `had_any_match` separă „brand absent" de „brand prezent dar tot ce avea e deja afișat" — în al
    # doilea caz cădem pe răspunsul gol normal (P6), NU pe negarea falsă a brandului (NX-113b).
    if not products and a.brand and not had_any_match:
        return ToolResult(
            ok=True,
            products=[],
            llm_view=(
                f"Nu am găsit niciun produs de la brandul «{a.brand}» în catalog. "
                f"Nu prezenta alt brand ca fiind «{a.brand}». Poți oferi alternative din alte "
                f"branduri, dar spune explicit că sunt alt brand."
            ),
        )

    # A1: produs NUMIT inexistent (nu doar brand) → disclosure anti-bait-and-switch. Produsele
    # întoarse (dacă există) rămân ALTERNATIVE, dar agentul spune clar că nu e cel cerut.
    notes: list[str] = []
    if (
        named_miss
    ):  # NX-163: precomputat mai sus (= același predicat, reuse — și driver de unmet_query)
        ctx.emit("named_product_not_found", alternatives=len(products))
        notes.append(
            f"(produsul «{a.product_name}» nu există ca atare în catalog — NU prezenta alt produs "
            f"ca fiind «{a.product_name}»; cele de mai jos sunt ALTERNATIVE similare, spune clar)"
        )
    # NX-227: nevoia cerută nu are corespondent în `concern_map` → filtrul pe ea NU a rulat.
    # Fără disclosure, modelul poate prezenta rezultatele ca potrivite „pentru ten reactiv" deși
    # nimic nu le-a verificat. Formularea e precisă: semanticul tot a văzut termenul (e în textul
    # embedat), deci „nu sunt FILTRATE", nu „nu s-a ținut cont deloc". Clarificarea rămâne decizia
    # agentului, nu a codului (anti over-clarify, NX-176a).
    if unmapped_concerns:
        termeni = ", ".join(unmapped_concerns[:_UNMAPPED_TERMS_MAX])
        notes.append(
            f"(nevoile «{termeni}» nu sunt filtre cunoscute în catalog — rezultatele NU sunt "
            f"filtrate pe ele; nu afirma potrivirea pe aceste nevoi, poți cere o clarificare)"
        )
    # Aceeași onestitate pentru categoria care n-a putut fi confruntată cu catalogul. NU suprimăm
    # (vezi `category_dropped` mai jos): filtrul n-a existat, deci rezultatele nu sunt „de pe alt
    # raft", sunt doar nefiltrate. Ce nu are voie modelul e să le prezinte drept categoria cerută.
    if a.category and not category_keys:
        notes.append(
            f"(«{a.category}» nu e o categorie din catalog — rezultatele NU sunt filtrate pe ea; "
            f"nu afirma că sunt din categoria asta, propune o categorie reală sau cere o "
            f"clarificare)"
        )
    # NX-266: o constrângere numerică a golit setul. Nu se repopulează tăcut — asta ar fi exact
    # minciuna pe care o previne cardul (produse care contrazic numărul, prezentate ca potrivite).
    # Se NUMEȘTE cerința: modelul trebuie să poată spune „nimic sub 100 lei", nu „n-am găsit".
    if not products and step_bounds:
        cerinte = "; ".join(b.constraint.describe(_facet_label(ctx, b.facet)) for b in step_bounds)
        notes.append(
            f"(niciun produs din catalog nu îndeplinește: {cerinte}. Spune-i clientului exact ce "
            f"cerință nu poate fi îndeplinită, nu prezenta produse care o încalcă. Poți întreba "
            f"dacă acceptă o valoare diferită.)"
        )
    # Relaxare cu disclosure: search a renunțat la o constrângere SOFT (nevoie/categorie) ca să iasă
    # ceva → agentul trebuie să fie sincer că nu e potrivire exactă pe ce a cerut (P6, nu tăcere).
    if relaxed:
        notes.append(
            "(au fost relaxate doar criterii secundare; nu afirma că lipsește categoria dacă "
            "produsele sunt încă în categoria cerută)"
        )
    # NX-170: reason_codes (de ce se potrivește) + gate not_recommended_for (hard exclude / soft
    # atenționare), determinist, sub kill-switch. Modifică `products` (excluderi + adnotări).
    if get_settings().catalog_reason_codes_enabled:
        products = annotate_reasons(
            products, concerns=a.concerns, price_max=a.price_max, features=a.features
        )
    # NX-173: O linie de context (nu copy) — modelul alege coerent („în sarcină, aș merge pe ceva
    # simplu"), dar NU scrie avertismentul: fraza garantată o adaugă codul la compunere.
    if safety_hint:
        notes.insert(0, safety_hint)
    view = _brief(products, getattr(ctx.business, "domain_pack", None), ctx.language)
    if notes:
        view = "\n".join(notes) + "\n" + view
    # izi-parity hardening: semnal de RELEVANȚĂ pentru compose (suprimă „Recomandarea mea"
    # off-category). `category_dropped` = filtrul de categorie cerut a fost renunțat ca să iasă ceva
    # (categorie inexistentă). `top_cosine` = cât de departe e cel mai apropiat vector (prinde
    # free-text fără categorie). Determinist, fără LLM. Fail-open la consumator (None ⇒ exact).
    # Condiția e pe categoria REZOLVATĂ, nu pe cuvântul cerut, și distincția contează: „a fost
    # relaxată" (categorie reală, dar prea îngustă → rezultatele vin de pe alt raft, deci nu le
    # prezenta ca fiind ce s-a cerut) NU e același lucru cu „n-am putut-o verifica" (vocabular
    # indisponibil sau termen necunoscut → filtrul n-a existat niciodată). Confundându-le, o
    # degradare a catalogului s-ar transforma în suprimarea TUTUROR rezultatelor — adică fix
    # tăcerea pe care încercăm s-o eliminăm. Pentru al doilea caz punem o notă, mai jos.
    category_relaxed = bool(category_keys) and (
        winning_step is not None and winning_step.get("category") is None
    )
    # NX-299 — `category_dropped` înseamnă „rezultatele vin de pe alt raft decât cel CERUT", iar
    # asta presupune că s-a cerut unul. Când raftul a fost o IPOTEZĂ a modelului, relaxarea lui e
    # chiar reparația: clientul n-a numit niciun raft, deci setul potrivit pe NEVOIA lui nu e
    # off-category, e on-request. Fără distincția asta, felia 1 s-ar fi anulat singură în modul cel
    # mai prost cu putință: treapta nou-deblocată ar fi făcut garda de mai jos să suprime TOT setul
    # și să întoarcă zero carduri acolo unde înainte erau două greșite.
    #
    # Scutirea e însă îngustă DELIBERAT, la exact treapta pe care felia asta a creat-o: cu
    # `search_category_hard_enabled` stins, categoria avea deja o treaptă de relaxare, indiferent de
    # proveniență, iar acolo garda trebuie să se comporte byte-identic. Scris pe
    # `not category_uttered` singur, fixul ar fi DEZARMAT garda pe calea veche — protecția
    # off-category ar fi căzut tăcut pentru orice client care numește raftul cu alt cuvânt decât
    # slug-ul lui („makeup" pentru «machiaj»), iar suita a prins-o.
    hypothesis_relaxed = (
        category_relaxed and by_provenance and hard_category and not category_uttered
    )
    category_dropped = category_relaxed and not hypothesis_relaxed
    if hypothesis_relaxed:
        ctx.emit(
            "category_hypothesis_relaxed",
            category_key=category_keys[0],
            relax_depth=relax_depth,
            pool_size=len(products),
        )
        # Aceeași regulă ca la `lexical_step` (NX-293): o degradare care ajunge la client trebuie
        # să ajungă și la model. Fără nota asta, produsele de mai jos arată identic cu un set
        # filtrat pe raftul cerut, iar modelul le-ar putea prezenta ca atare.
        #
        # Fără liniuță de pauză, deliberat (P13): nota intră în contextul modelului, iar un
        # exemplu cu semnul interzis îl învață exact ce îi cerem să nu scrie.
        view = (
            f"Filtrul pe categoria «{a.category}» nu a întors nimic, iar clientul nu a "
            f"cerut-o, a fost presupunerea ta. Produsele de mai jos sunt potrivite pe NEVOIA "
            f"lui, nu pe acel raft. Nu le prezenta ca fiind din «{a.category}».\n"
        ) + view
    # …dar până la NX-299 garda acoperea aici mulțimea VIDĂ, iar asta nu se vedea din cod:
    # singurele trepte care puneau `category: None` erau gated pe
    # `not search_category_hard_enabled`,
    # iar flagul e `True` implicit. Deci cu categorie REZOLVATĂ nu se relaxa niciodată (al doilea
    # conjunct fals), iar cu categorie NEREZOLVATĂ `category_keys` e gol (primul conjunct fals).
    # Măsurat pe tenantul SOLE: `offcategory_suppressed` = 0 declanșări, vreodată — inclusiv pe
    # turul care a servit farduri de obraz la o cerere de produse de păr.
    #
    # NX-299 a deblocat treapta, dar DOAR pentru raftul ghicit de model, iar `category_dropped` a
    # rămas legat de raftul CERUT (vezi mai sus). Garda rămâne deci pe același caz ca înainte —
    # clientul a numit un raft, noi servim de pe altul — și devine în sfârșit atingibilă.
    #
    # Al doilea caz e cel care doare, dar NU pe toată întinderea lui. Când filtrul de categorie
    # n-a rulat, întrebarea corectă e „ce a format atunci setul?":
    #   • potrivirea pe TEXTUL cererii (cazul obișnuit) → rezultatele sunt despre ce a cerut
    #     clientul, doar nefiltrate pe raft; suprimarea lor ar fi exact tăcerea pe care o evităm,
    #     deci rămâne doar nota de onestitate de mai sus;
    #   • ALT filtru DUR — brand sau variantă, singurele care nu se relaxează NICIODATĂ (vezi
    #     `_relax_ladder`) → setul e „tot ce are brandul", nu un răspuns la cerere. Asta s-a
    #     întâmplat: `category="ingrijirea-parului"` a ieșit `UNKNOWN`, `brand="MUZIGAE"` a rămas
    #     dur, iar rezultatul a fost catalogul de machiaj al brandului, servit cu prețuri reale —
    #     deci validatorul (poartă de ADEVĂR) l-a lăsat să treacă, corect.
    #
    # Condiția e pe VERDICT, nu pe „zero chei", și distincția a fost găsită de un test existent:
    # cu vocabularul indisponibil (DB degradat) TOT se rezolvă `UNKNOWN`, deci o regulă scrisă pe
    # absența cheilor ar fi transformat o clipeală de DB în „niciun rezultat" pe fiecare căutare cu
    # brand. `unknown_dimension` = n-am putut judeca ⇒ nu schimbăm nimic pentru client;
    # `not_in_vocabulary` = am judecat pe un vocabular VIU și categoria nu există ⇒ e o eroare de
    # argument a modelului.
    shelf_forced_by = "brand" if a.brand else ("variant" if a.variant_label else None)
    verdict = resolutions.category
    category_judged_absent = verdict is not None and verdict.reason == "not_in_vocabulary"
    suppress = category_dropped or (category_judged_absent and shelf_forced_by is not None)
    # NX-167 (B): cerere CLARĂ de categorie, dar potrivirea a picat pe ALTĂ ramură (categoria cerută
    # a fost renunțată în relaxare — nici pe arbore nu s-a găsit nimic pe ea) → NU prezenta produse
    # off-category ca match. Suprimă cardurile + semnal de clarificare (P6: nu tăcere — agentul
    # întreabă / oferă o subcategorie, nu minte că e ce a cerut). Curăță și sesiunea, ca
    # „arată-mi altele" (fp identic) să NU pagineze gunoiul off-category suprimat.
    if get_settings().search_offcategory_guard_enabled and suppress and products:
        ctx.emit(
            "offcategory_suppressed",
            category_key=a.category,
            relax_depth=relax_depth,
            pool_size=len(products),
            # DE CE s-a suprimat: „categoria a fost relaxată" și „categoria n-a existat niciodată,
            # iar setul l-a format brandul" sunt defecte diferite, cu reparații diferite.
            reason="category_relaxed"
            if category_dropped
            else f"unverified_category_{shelf_forced_by}",
        )
        ctx.state_patch.pop("active_search", None)
        if category_dropped:
            view = (
                f"Nu am găsit produse pe categoria «{a.category}» în catalog. NU prezenta produse "
                f"din altă categorie ca fiind «{a.category}». Întreabă clientul ce anume caută sau "
                f"propune-i o categorie înrudită — nu inventa o potrivire."
            )
        else:
            # Formulare precisă, fiindcă modelul POATE răspunde util din ea: nu „n-am găsit nimic",
            # ci „brandul ăsta nu are așa ceva" — care e adevărul și e un răspuns de vânzare.
            forced = f"brandul «{a.brand}»" if a.brand else f"varianta «{a.variant_label}»"
            view = (
                f"«{a.category}» nu e o categorie din catalog, deci filtrul pe ea NU a rulat, iar "
                f"rezultatele au fost restrânse DOAR la {forced} — sunt produsele lui, nu un "
                f"răspuns la ce a cerut clientul. NU le prezenta. Spune-i onest că {forced} nu "
                f"pare să acopere ce caută și propune-i o căutare fără el sau o categorie reală."
            )
        return ToolResult(ok=True, products=[], llm_view=view)
    relevance = Relevance(
        relaxed=relaxed,
        category_dropped=category_dropped,
        top_cosine=top_cosine,
        guessed_category_dropped=guessed_category_dropped,
    )
    return ToolResult(ok=True, products=products, llm_view=view, relevance=relevance)


@register("get_product_details")
async def get_product_details_tool(
    ctx: TurnContext, deps: PipelineDeps, args: dict[str, Any]
) -> ToolResult:
    """Detalii complete + rezumat de recenzii (D3) pentru un produs."""
    a = DetailArgs(**args)
    async with deps.db("get_product_details") as conn:
        products = await get_products_by_ids(conn, ctx.business.id, [a.product_id], limit=1)
    if not products:
        return ToolResult(ok=False, error="not_found", llm_view="Produsul nu există în catalog.")
    # NX-173 (P0): și calea de DETALIU e o cale de afișare — „spune-mi mai multe despre X" nu are
    # voie să reintroducă produsul exclus din listă (nici cu preț/link grounded pentru validator).
    products, _ = _safety_gate(ctx, products, purpose="details")
    if not products:
        return ToolResult(ok=False, error="safety_excluded", llm_view=_SAFETY_TOOL_VIEW)
    p = products[0]
    # Vocabularul (cache 5 min) știe care badge-uri sunt pe tot catalogul, deci zgomot.
    vocab = await get_vocabulary(deps, ctx.business.id)
    view = _detail_view(
        p,
        getattr(ctx.business, "domain_pack", None),
        ctx.language,
        delivery=delivery_for(p, ctx.business).text,
        noise_badges=vocab.noise_badges,
    )
    # NX-195: produs epuizat → propunem ALTERNATIVA, nu doar „nu mai avem". Relațiile de substitut
    # (222, din NX-171b) existau și nu le citea nimeni. Alternativele intră și în `products`, ca
    # validatorul (stagiul 8) să accepte prețul lor dacă modelul îl rostește.
    if (p.get("availability") or "") == "out_of_stock":
        # NX-163b: cerere pe un produs EPUIZAT = semnal de reaprovizionare, capturat determinist
        # la sursă (clientul a cerut explicit produsul ăsta, nu e inferență). Dimensiunea e
        # `product_id` (ref, P8) + brandul; fără text de user (P12). Alimentează acțiunea
        # `restock` din raportul de cerere (NX-217), alături de back_in_stock_subscriptions.
        ctx.emit(
            "unmet_query",
            reason="out_of_stock",
            product_id=p["id"],
            brand=p.get("brand"),
            locale=ctx.language,
        )
        async with deps.db("get_substitutes") as conn:
            subs = await get_substitutes(conn, ctx.business.id, p["id"], limit=2)
        subs, _ = _safety_gate(ctx, subs, purpose="details")
        if subs:
            alt = ", ".join(
                f"[{s['id']}] {display_name(s['name'])}, "
                f"{amount_text(s['price'], ctx.language)} lei"
                for s in subs
            )
            view += f" | alternative pe stoc: {alt}"
            products = products + subs
    return ToolResult(ok=True, products=products, llm_view=view)


@register("compare_products")
async def compare_products_tool(
    ctx: TurnContext, deps: PipelineDeps, args: dict[str, Any]
) -> ToolResult:
    """Compară 2-4 produse (preț, rating, plusuri/minusuri din recenzii)."""
    a = CompareArgs(**args)
    async with deps.db("compare_products") as conn:
        products = await get_products_by_ids(conn, ctx.business.id, a.product_ids, limit=4)
    # NX-173 (P0): comparația e tot afișare — un produs exclus nu are voie să reintre pe ușa asta.
    # Gate ÎNAINTE de pragul `need_2`: dacă din 2 produse unul e contraindicat, rezultatul corect e
    # „nu compar asta", nu o comparație tăcută pe restul (și nici `need_2`, care ar minți despre
    # cauză — vezi NX-174 pentru „vorbește de 2, afișează 1").
    n_before = len(products)
    products, _ = _safety_gate(ctx, products, purpose="compare")
    if len(products) < 2 and len(products) < n_before:
        return ToolResult(
            ok=False, products=[], error="safety_excluded", llm_view=_SAFETY_TOOL_VIEW
        )
    if len(products) < 2:
        return ToolResult(
            ok=False,
            products=products,
            error="need_2",
            llm_view="Am nevoie de cel puțin 2 produse existente pentru comparație.",
        )
    return ToolResult(
        ok=True,
        products=products,
        llm_view=_compare_view(products, getattr(ctx.business, "domain_pack", None), ctx.language),
    )


class RelatedArgs(BaseModel):
    anchor_id: str = Field(min_length=1, max_length=64)
    relation: str = Field(min_length=1, max_length=64)
    limit: int = Field(default=4, ge=1, le=6)


def _related_view(products: list[dict[str, Any]], spec: Any, locale: str, *, ordered: bool) -> str:
    """Vederea pentru model: o linie per produs, cu indexul pasului DOAR când relația e ordonată.

    Titlul e eticheta declarată de tenant (`spec.label(locale)`) sau NIMIC. Nu inventăm un titlu:
    un bloc fără titlu e onest, unul cu titlu greșit („Pașii următori" peste niște accesorii) e o
    afirmație pe care nicio poartă din aval n-o verifică — produsele EXISTĂ, prețurile sunt REALE,
    doar relația ar fi inventată."""
    if not products:
        return ""
    title = spec.label(locale) if spec is not None else None
    lines = [f"{title}:"] if title else []
    for i, p in enumerate(products, start=1):
        prefix = f"{i}. " if ordered else "- "
        price = amount_text(p.get("price"), locale) if p.get("price") is not None else ""
        avail = f" | stoc: {p['availability']}" if p.get("availability") else ""
        lines.append(
            f"{prefix}[{p.get('id')}] {display_name(p.get('name'))}"
            f"{f' | {price}' if price else ''}{avail}"
        )
    return "\n".join(lines)


@register("related_products")
async def related_products_tool(
    ctx: TurnContext, deps: PipelineDeps, args: dict[str, Any]
) -> ToolResult:
    """NX-275 felia 5 — vecinii unei ancore în graful de relații al tenantului.

    **Serverul face graful, nu modelul.** Modelul spune doar de unde pornim (`anchor_id`) și ce fel
    de legătură urmărim (`relation`, enum din registrul tenantului). CÂT de adânc se merge și DACĂ
    rezultatul e o secvență sunt proprietăți DECLARATE ale tipului de muchie
    (`DomainPack.relation_kinds`, NX-262), nu decizii de model: un traversal „ca la rutine" aplicat
    pe compatibilitate produce recomandări care trec și validatorul, și grounding guardul, fiindcă
    produsele există și prețurile sunt reale. Doar relația ar fi inventată.

    Refolosește funcțiile de graf existente (`traverse_relation_chain` + `walk_chain` pentru
    secvențe, `traverse_relations` pentru rest) — cross-sell-ul determinist de după `cart_add`
    rămâne neatins și nu se duplică nimic.

    Ancoră fără muchii → `ok=False, error="no_relations"` cu o vedere ONESTĂ, ca modelul să pună
    `unknowns` în loc să inventeze pași.
    """
    a = RelatedArgs(**args)
    pack = getattr(ctx.business, "domain_pack", None)
    registry = getattr(pack, "relation_kinds", None)
    spec = registry.get(a.relation) if registry is not None else None
    if spec is None or a.relation not in getattr(registry, "specs", {}):
        # Tip nedeclarat: NU traversăm. Registrul dă un default `NEIGHBORS`/1 tocmai ca apelantul
        # să n-aibă ramură de `None`, dar aici a cere o relație pe care tenantul n-a declarat-o e
        # o interogare pe gol care ar arăta ca un răspuns.
        return ToolResult(
            ok=False,
            products=[],
            error="unknown_relation",
            llm_view="Magazinul nu are declarat tipul ăsta de legătură între produse.",
        )

    from src.catalog.relation_chain import walk_chain  # noqa: PLC0415 — evită cuplaj la import
    from src.db.queries.catalog import traverse_relation_chain, traverse_relations  # noqa: PLC0415

    ordered = bool(getattr(spec, "ordered", False))
    depth = int(getattr(spec, "max_depth", 1) or 1)
    async with deps.db("related_products") as conn:
        if ordered:
            hops = await traverse_relation_chain(
                conn, ctx.business.id, anchor_id=a.anchor_id, kind=a.relation, max_depth=depth
            )
            refs = walk_chain(hops, a.anchor_id, a.limit)
        else:
            refs = await traverse_relations(
                conn,
                ctx.business.id,
                anchor_id=a.anchor_id,
                kind=a.relation,
                max_depth=depth,
                limit=a.limit,
            )
        ids = [str(r["id"]) for r in refs][: a.limit]
        products = (
            await get_products_by_ids(
                conn, ctx.business.id, ids, limit=a.limit, respect_content_status=True
            )
            if ids
            else []
        )

    # Ordinea contează pentru o secvență: `get_products_by_ids` nu promite ordinea cerută, iar o
    # rutină cu pașii amestecați e mai rea decât niciuna.
    by_id = {str(p.get("id")): p for p in products}
    products = [by_id[i] for i in ids if i in by_id]

    # Cumpărabilitate: un pas pe care clientul nu-l poate cumpăra nu e un pas. `availability`
    # necunoscut NU exclude (UNKNOWN ≠ epuizat, NX-240/047).
    products = [p for p in products if str(p.get("availability") or "") != "out_of_stock"]

    # NX-173 (P0): e o cale de AFIȘARE, deci trece prin aceeași poartă ca search/details/compare.
    products, _ = _safety_gate(ctx, products, purpose="related")
    if not products:
        return ToolResult(
            ok=False,
            products=[],
            error="no_relations",
            llm_view=(
                "Nu am legături declarate de tipul ăsta pentru produsul ăsta. Spune-i clientului "
                "că nu ai o secvență pentru el, nu inventa pași."
            ),
        )
    return ToolResult(
        ok=True,
        products=products,
        llm_view=_related_view(products, spec, ctx.language, ordered=ordered),
    )
