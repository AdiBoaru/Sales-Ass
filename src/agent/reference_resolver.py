"""NX-234 — rezolvarea REFERINȚEI la produs: „acesta", „prima", „serul cu niacinamidă".

Până acum deixis-ul se rezolva exclusiv din `ConversationState.displayed_products` (ce a arătat
asistentul). Consecința era vizibilă pe cazul cel mai comun al unui widget de magazin: un client
deschide o pagină de produs, scrie „ce părere ai despre acesta?" — și nu există nimic de ancorat,
fiindcă asistentul n-a afișat încă nimic. Singura „soluție" la îndemână era ca frontendul să
compună mesajul („despre produsul X"), adică exact ce interzice boundary-ul: browserul ar fi
scris conținutul întrebării și ar fi devenit din nou un motor de intenții.

Aici e API-ul comun, cu ancora paginii ca sursă de sine stătătoare. PRECEDENȚA (parțială
deliberat — algoritmul complet `action / named / page / selected / displayed` vine în NX-235):

    1. `named`    — clientul a numit produsul (nume complet sau tokeni unici din setul afișat);
    2. `ordinal`  — „prima" / „a doua" / „review 3", raportat la lista afișată;
    3. `page`     — ancora canonică a paginii curente (PDP), rehidratată server-side;
    4. `single`   — un singur produs afișat, fără altă ancoră.

`named` și `ordinal` sunt AFIRMAȚII ale clientului; ancora paginii e un fapt despre unde se află.
Când clientul spune explicit ce vrea, ce se vede pe ecran nu are cum să câștige — de aici ordinea.
Ancora bate însă „ultimul produs afișat": clientul se uită ACUM la pagină, iar setul afișat poate
fi de acum trei tururi.

Modulul e PUR: primește referințe, întoarce o decizie tipizată. Nu citește DB, nu vede
`TurnContext` și nu compune text — rehidratarea produsului rămâne a apelantului, cu
`business_id = $1` (P7)."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal, Protocol

ReferenceSource = Literal["named", "ordinal", "page", "single", "none"]
ReferenceOutcome = Literal["resolved", "ambiguous", "none"]

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
class ReferenceResolution:
    """Decizia, cu PROVENIENȚĂ. `source` intră în `web_reference_resolved` (observabilitate):
    fără el, „a rezolvat" și „a ghicit" arată la fel în metrici."""

    product_id: str | None
    source: ReferenceSource = "none"
    outcome: ReferenceOutcome = "none"
    name: str | None = None
    index: int | None = None  # poziția în lista afișată, când sursa e `ordinal`/`named`

    @property
    def resolved(self) -> bool:
        return self.product_id is not None


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
    """Precedența completă a acestui card: `named` > `ordinal` > `page` > `single`.

    Ancora paginii intră DUPĂ semnalele explicite ale clientului și ÎNAINTE de „ultimul produs
    afișat". Fără ancoră (orice canal non-web, sau web fără context de pagină) comportamentul e
    exact cel de dinainte — `resolve_from_displayed`."""
    if page is None:
        return resolve_from_displayed(query, refs)

    if refs:
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
    return ReferenceResolution(page.product_id, "page", "resolved", page.name or None)


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
    "PageAnchor",
    "ReferenceResolution",
    "match_name",
    "match_ordinal",
    "normalize_for_match",
    "page_anchor_from_snapshot",
    "resolve_from_displayed",
    "resolve_product_reference",
]
