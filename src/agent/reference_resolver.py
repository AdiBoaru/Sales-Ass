"""NX-234/NX-235 — rezolvarea REFERINȚEI la produs: „acesta", „prima", „serul cu niacinamidă".

Până la NX-234 deixis-ul se rezolva exclusiv din `ConversationState.displayed_products` (ce a
arătat asistentul). Consecința era vizibilă pe cazul cel mai comun al unui widget de magazin: un
client deschide o pagină de produs, scrie „ce părere ai despre acesta?" — și nu există nimic de
ancorat, fiindcă asistentul n-a afișat încă nimic. Singura „soluție" la îndemână era ca frontendul
să compună mesajul („despre produsul X"), adică exact ce interzice boundary-ul: browserul ar fi
scris conținutul întrebării și ar fi devenit din nou un motor de intenții.

NX-235 închide precedența, o dată pentru toate căile (P3 — o singură ordine, nu una per apelant):

    1. `action`   — referință SEMNATĂ din tokenul acțiunii curente (NX-236), legată de revizie;
    2. `named`    — clientul a numit produsul, univoc, în mesajul curent;
    3. `ordinal`  — „prima" / „a doua" / „review 3", peste lista EXACT afișată, cu revizia ei;
    4. `page`     — ancora paginii, când expresia e deictică („acesta") sau nu există alt candidat;
    5. `selected` — produsul confirmat explicit într-un tur anterior;
    6. `single`   — singurul produs discutat recent, dacă e univoc;
    7. altfel     → `ambiguous`. Niciodată „primul card", niciodată o ghicire tăcută.

Ordinea nu e arbitrară: `named`/`ordinal` sunt AFIRMAȚII ale clientului, ancora paginii e un fapt
despre unde se află. Când clientul spune explicit ce vrea, ce se vede pe ecran nu poate câștiga.
Ancora bate însă „ultimul produs afișat": clientul se uită ACUM la pagină, iar setul afișat poate
fi de acum trei tururi.

**Ce se REFUZĂ explicit.** O ancoră de acțiune stale (emisă peste altă revizie a listei) sau
invalidă nu cade pe pagina curentă și nu alege primul card: întoarce `stale`. Un ordinal exprimat
clar dar în afara listei nu selectează nimic. Un răspuns greșit cu aer de siguranță e mai
scump decât o întrebare.

Modulul e PUR: primește referințe, întoarce o decizie tipizată. Nu citește DB, nu vede
`TurnContext` și nu compune text — rehidratarea produsului rămâne a apelantului, cu
`business_id = $1` (P7)."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal, Protocol

ReferenceSource = Literal["action", "named", "ordinal", "page", "selected", "single", "none"]
ReferenceOutcome = Literal["resolved", "ambiguous", "stale", "none"]
# Motive low-cardinality — intră în `reference_resolution{source,outcome,reason}` (P10).
ReferenceReason = Literal[
    "action_anchor",
    "named_unique",
    "ordinal_in_list",
    "page_deictic",
    "page_only_candidate",
    "page_legacy",
    "selected_previously",
    "single_candidate",
    "anchor_invalid",
    "anchor_stale",
    "ordinal_out_of_range",
    "no_anchor",
    "empty",
    "",
]

# Ordinalele conversaționale, în RO/EN/HU. Rulează pe text normalizat FĂRĂ diacritice, ca aceeași
# intenție să funcționeze și când clientul tastează „prima" fără să aibă diacritice pe tastatură.
ORDINALS: tuple[tuple[int, re.Pattern[str]], ...] = (
    (0, re.compile(r"(?<![a-z0-9])(?:prima|primul|intai|first|elso)(?![a-z0-9])")),
    (1, re.compile(r"(?<![a-z0-9])(?:a[ ]+doua|al[ ]+doilea|second|masodik)(?![a-z0-9])")),
    (2, re.compile(r"(?<![a-z0-9])(?:a[ ]+treia|al[ ]+treilea|third|harmadik)(?![a-z0-9])")),
)
# „recenzii 2" / „review #3": ordinal NUMERIC, valid doar lipit de un cuvânt de recenzie — un „2"
# liber într-o propoziție nu e o referință („am 2 copii" nu selectează al doilea produs).
NUMERIC_ORDINALS: tuple[tuple[int, re.Pattern[str]], ...] = tuple(
    (
        index - 1,
        re.compile(rf"(?:recenzi|parer|opini|review|velemen)[a-z]*[ :#.-]*{index}(?:[^0-9]|$)"),
    )
    for index in range(1, 5)
)


class HasProductRef(Protocol):
    """Structural: orice obiect cu `product_id` + `name` (ex. `models.ProductRef`)."""

    product_id: str
    name: str


@dataclass(frozen=True)
class PageAnchor:
    """Ancora canonică a paginii curente. Construită DOAR din evidence rehidratat server-side
    (`SurfaceContext.product`) — niciodată din câmpuri afirmate de browser."""

    product_id: str
    name: str = ""
    price: float | None = None


@dataclass(frozen=True)
class ActionAnchor:
    """Referința purtată de tokenul acțiunii pe care clientul tocmai a apăsat-o.

    `valid` e verdictul CRIPTOGRAFIC al lui NX-236 — aici nu se verifică semnături, se respectă
    răspunsul. `revision` e revizia listei peste care a fost emisă: dacă între timp lista s-a
    schimbat, ancora nu mai descrie ce vede clientul, iar „al doilea" de atunci nu e „al doilea"
    de acum."""

    product_id: str
    revision: int = 0
    valid: bool = True


@dataclass(frozen=True)
class ReferenceResolution:
    """Decizia, cu PROVENIENȚĂ. `source`/`reason` intră în `web_reference_resolved`
    (observabilitate): fără ele, „a rezolvat" și „a ghicit" arată la fel în metrici."""

    product_id: str | None
    source: ReferenceSource = "none"
    outcome: ReferenceOutcome = "none"
    name: str | None = None
    index: int | None = None  # poziția în lista afișată, când sursa e `ordinal`/`named`
    reason: ReferenceReason = ""

    @property
    def resolved(self) -> bool:
        return self.product_id is not None

    @property
    def stale(self) -> bool:
        return self.outcome == "stale"


@dataclass(frozen=True)
class ReferenceRequest:
    """Tot ce poate ancora o referință, într-un singur obiect — ca precedența să fie o proprietate
    a datelor, nu a ordinii în care apelantul se întâmplă să verifice lucrurile."""

    query: str
    refs: tuple[HasProductRef, ...] = ()
    page: PageAnchor | None = None
    anchor: ActionAnchor | None = None
    selected_product: str | None = None
    displayed_revision: int = 0
    # NX-234 avea pagina ca fallback necondiționat. Modul legacy îl păstrează byte-identic până la
    # cutoverul de contract; modul v2 cere deixis sau absența oricărui alt candidat.
    legacy_page_fallback: bool = False


def normalize_for_match(text: str) -> str:
    """Lowercase + fără diacritice (NFKD). Aceeași normalizare pe ambele capete ale comparației —
    altfel „Ser Petală" nu s-ar potrivi niciodată cu „ser petala"."""
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def match_ordinal(query: str, count: int) -> int | None:
    """Indexul cerut explicit prin ordinal, sau None. `count` mărginește: „a treia" pe o listă
    de două nu selectează nimic (mai bine o clarificare decât produsul greșit)."""
    normalized = normalize_for_match(query)
    for index, pattern in (*ORDINALS, *NUMERIC_ORDINALS):
        if index < count and pattern.search(normalized):
            return index
    return None


def match_name(query: str, names: list[str]) -> int | None:
    """Indexul produsului NUMIT în query, sau None dacă nu e unic.

    Două trepte: numele complet conținut în query (chipul poate fi scurtat, deci comparăm în sens
    invers), apoi scor pe tokeni ≥3 caractere. Un scor egal între două produse NU alege arbitrar:
    un termen comun („cremă") nu are voie să decidă. Ambiguitatea se întoarce ca „nu știu", ca
    apelantul să poată întreba."""
    normalized = normalize_for_match(query)
    norm_names = [normalize_for_match(n).strip() for n in names]
    exact = [i for i, n in enumerate(norm_names) if n and n in normalized]
    if len(exact) == 1:
        return exact[0]

    query_tokens = set(re.findall(r"[a-z0-9]+", normalized))
    scores: list[int] = []
    for name in norm_names:
        tokens = {t for t in re.findall(r"[a-z0-9]+", name) if len(t) >= 3}
        scores.append(sum(1 for t in tokens if t in query_tokens))
    best = max(scores, default=0)
    winners = [i for i, score in enumerate(scores) if score == best]
    if best >= 1 and len(winners) == 1:
        return winners[0]
    return None


def resolve_from_displayed(query: str, refs: list[HasProductRef]) -> ReferenceResolution:
    """Rezolvarea ISTORICĂ (pre-NX-234), extrasă ca API comun: doar din setul afișat.

    Păstrată byte-identic ca semantică (`deterministic._resolve_review_product` delegă aici):
    produs unic → el; ordinal → poziția; nume → potrivirea unică; altfel ambiguu."""
    if not refs:
        return ReferenceResolution(None, "none", "none")
    if len(refs) == 1:
        return ReferenceResolution(refs[0].product_id, "single", "resolved", refs[0].name, 0)
    index = match_ordinal(query, len(refs))
    if index is not None:
        return ReferenceResolution(
            refs[index].product_id, "ordinal", "resolved", refs[index].name, index
        )
    index = match_name(query, [r.name for r in refs])
    if index is not None:
        return ReferenceResolution(
            refs[index].product_id, "named", "resolved", refs[index].name, index
        )
    return ReferenceResolution(None, "none", "ambiguous")


def resolve_product_reference(
    query: str,
    refs: list[HasProductRef],
    *,
    page: PageAnchor | None = None,
) -> ReferenceResolution:
    """API-ul NX-234, păstrat byte-identic ca semantică: `ordinal` > `named` > `page` > `single`.

    Delegă în `resolve_reference` cu `legacy_page_fallback=True` — o singură implementare a
    precedenței, două configurații, ca modul nou să nu fie o a doua copie care poate diverge.
    Dispare la cutoverul de contract (NX-249), împreună cu restul căilor v1."""
    if page is None:
        return resolve_from_displayed(query, refs)
    return resolve_reference(
        ReferenceRequest(query=query, refs=tuple(refs), page=page, legacy_page_fallback=True)
    )


# Deixis: „acesta / asta / ăsta / acest produs" + echivalentele EN/HU. Rulează pe text normalizat
# fără diacritice, ca și ordinalele. Deliberat NU conține pronume slabe („îl", „o"): ele apar în
# fraze care nu se referă la pagină („mi-o recomanzi pe cea de ieri?"), iar o ancorare greșită e
# mai scumpă decât una ratată.
_DEICTIC_RE = re.compile(
    r"(?<![a-z0-9])(?:"
    r"acest|acesta|aceasta|aceste|acestea|acestia|acestui|acestei|"
    r"asta|astea|astia|asa?ta|"
    r"this|these|it|"
    r"ez|ezt|ezek|ezeket|ezzel"
    r")(?![a-z0-9])"
)
# Ordinalele EXPRIMATE, independent de lungimea listei. `match_ordinal` mărginește la `count`;
# aici ne interesează dacă clientul A CERUT un ordinal, ca „a treia" pe o listă de două să nu cadă
# tăcut pe altă sursă (ar selecta un produs pe care clientul nu l-a cerut). PUBLIC: gardul de
# rafinare (`deterministic.carries_new_constraints`) scade referințele din mesaj cu ACELEAȘI
# tipare — un ordinal e formulă de selecție, nu o cerere nouă, iar două tabele ar diverge.
ANY_ORDINAL_RE = tuple(pattern for _, pattern in (*ORDINALS, *NUMERIC_ORDINALS))


def is_deictic(query: str) -> bool:
    """`True` dacă expresia arată spre ceva prezent („acesta"), adică spre pagina curentă."""
    return _DEICTIC_RE.search(normalize_for_match(query)) is not None


def expresses_ordinal(query: str) -> bool:
    """`True` dacă mesajul cere un ordinal, indiferent dacă lista îl poate onora."""
    normalized = normalize_for_match(query)
    return any(pattern.search(normalized) for pattern in ANY_ORDINAL_RE)


def resolve_reference(request: ReferenceRequest) -> ReferenceResolution:
    """Precedența UNICĂ (vezi docstringul modulului). PURĂ și deterministă."""
    refs = list(request.refs)
    query = request.query or ""

    # 1. Ancoră semnată. O ancoră respinsă OPREȘTE rezolvarea: a cădea pe pagină ar transforma un
    #    token manipulat/expirat într-o selecție tăcută, exact ce trebuie să nu se întâmple.
    anchor = request.anchor
    if anchor is not None:
        if not anchor.valid:
            return ReferenceResolution(None, "action", "stale", reason="anchor_invalid")
        if (
            anchor.revision
            and request.displayed_revision
            and anchor.revision != request.displayed_revision
        ):
            return ReferenceResolution(None, "action", "stale", reason="anchor_stale")
        name = next((r.name for r in refs if r.product_id == anchor.product_id), None)
        return ReferenceResolution(
            anchor.product_id, "action", "resolved", name, reason="action_anchor"
        )

    # 2. Numit explicit, univoc.
    if refs:
        index = match_name(query, [r.name for r in refs])
        if index is not None:
            return ReferenceResolution(
                refs[index].product_id, "named", "resolved", refs[index].name, index, "named_unique"
            )

    # 3. Ordinal peste lista EXACT afișată.
    if refs:
        index = match_ordinal(query, len(refs))
        if index is not None:
            return ReferenceResolution(
                refs[index].product_id,
                "ordinal",
                "resolved",
                refs[index].name,
                index,
                "ordinal_in_list",
            )
    if not request.legacy_page_fallback and expresses_ordinal(query):
        # Ordinal cerut dar imposibil (listă mai scurtă / reordonată): nu servim alt produs.
        return ReferenceResolution(None, "ordinal", "ambiguous", reason="ordinal_out_of_range")

    # 4. Ancora paginii — deictic, sau singurul candidat din lume.
    page = request.page
    if page is not None:
        no_competitor = not refs and not request.selected_product
        if request.legacy_page_fallback:
            return ReferenceResolution(
                page.product_id, "page", "resolved", page.name or None, reason="page_legacy"
            )
        if is_deictic(query):
            return ReferenceResolution(
                page.product_id, "page", "resolved", page.name or None, reason="page_deictic"
            )
        if no_competitor:
            return ReferenceResolution(
                page.product_id, "page", "resolved", page.name or None, reason="page_only_candidate"
            )

    # 5. Produsul confirmat explicit anterior.
    if request.selected_product:
        name = next((r.name for r in refs if r.product_id == request.selected_product), None)
        return ReferenceResolution(
            request.selected_product, "selected", "resolved", name, reason="selected_previously"
        )

    # 6. Un singur produs discutat recent, univoc.
    if len(refs) == 1:
        return ReferenceResolution(
            refs[0].product_id, "single", "resolved", refs[0].name, 0, "single_candidate"
        )

    # 7. Fără ancoră: ambiguu (sau „nimic", când n-a existat niciun candidat).
    if not refs and page is None:
        return ReferenceResolution(None, "none", "none", reason="empty")
    return ReferenceResolution(None, "none", "ambiguous", reason="no_anchor")


def page_anchor_from_snapshot(snapshot: object) -> PageAnchor | None:
    """`TurnSnapshot` → `PageAnchor`, sau None.

    Tolerant la `None`/obiect străin DELIBERAT: `TurnContext.snapshot` e tipizat `Any` (ca
    `match_set`/`answer_plan`, ca să nu importăm `worker` în `models`), iar consumatorii sunt pe
    hot path. Un snapshot absent înseamnă „fără ancoră", nu o excepție într-un tur real."""
    surface = getattr(snapshot, "surface", None)
    product = getattr(surface, "product", None)
    if product is None or not getattr(product, "product_id", None):
        return None
    return PageAnchor(
        product_id=str(product.product_id),
        name=str(getattr(product, "name", "") or ""),
        price=getattr(product, "price", None),
    )


__all__ = [
    "NUMERIC_ORDINALS",
    "ORDINALS",
    "ActionAnchor",
    "PageAnchor",
    "ReferenceRequest",
    "ReferenceResolution",
    "expresses_ordinal",
    "is_deictic",
    "match_name",
    "match_ordinal",
    "normalize_for_match",
    "page_anchor_from_snapshot",
    "resolve_from_displayed",
    "resolve_product_reference",
    "resolve_reference",
]
