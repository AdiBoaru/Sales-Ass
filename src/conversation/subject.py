"""NX-314 — subiectul conversației: CE cumpără clientul, cu un singur proprietar.

## Problema

Sistemul n-avea nicăieri un obiect care să spună „discutăm despre creme de față, pentru
hidratare". Fiecare drum care re-caută îl reconstruia din ce avea la îndemână: «ceva mai ieftin»
păstra doar `primary_category_id`-ul produselor afișate și sorta crescător pe preț. Pe catalogul
SOLE categoria unei creme cuprinde și măștile sheet și benzile de nas, deci după o cremă de 110 lei
clientul primea o bandă de 3 lei și cinci măști de 10 lei (conversația reală din 2026-09-23).
Produsele și prețurile erau REALE, deci validatorul și `grounding_guard` le-au lăsat să treacă:
sunt porți de adevăr, nu de potrivire.

## Obiectul

Trei câmpuri, fiecare cu sursa lui și fără nicio listă de cuvinte (P11), deci valabile pe orice
vertical:

- `product_type` — tipul DOMINANT al setului arătat: valoarea care acoperă STRICT peste jumătate
  din produsele cu tip cunoscut, și cel puțin 2 produse. Un set amestecat (o rutină, „arată-mi ce
  ai") nu are tip, iar asta e informație corectă, nu o lipsă. Un set cu UN singur produs tipat
  (turul de detaliu) nu schimbă un subiect existent și ancorează unul doar când nu există altul —
  măsurat în sondă: pe conversația reală, setul dinaintea lui «mai ieftin» avea exact o cremă.
- `shelf_key` — raftul pe care a căutat agentul, REZOLVAT prin vocabular la o cheie reală de
  catalog. Doar un verdict `KNOWN` se persistă: un sinonim nerezolvabil ar deveni un șir inert pe
  care niciun consumator nu-l poate lega de catalog.
- `needs` — perechi `(dimensiune, cheie)` pentru nevoile ROSTITE de client (coroborate de
  `corroborated_by` înainte să ajungă aici) și rezolvate la o cheie de fațetă `KNOWN`.

## Reguli de continuitate

Un tur fără produse noi (cum se folosește, detalii, recenzii) MOȘTENEȘTE subiectul: e exact turul
3 din conversația reală, după care clientul cere „mai ieftin" despre aceeași cremă. Un tur cu
produse noi îl înlocuiește; un raft nou numit de client (`topic_switched`) sau un tip dominant nou
resetează și nevoile moștenite.

Modulul e PUR: fără DB, fără ceas, fără model. Apelantul îi dă vocabularul deja încărcat.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from src.catalog.vocabulary import (
    CATEGORY_DIMENSION,
    CatalogVocabulary,
    ResolutionStatus,
    resolve,
    resolve_any,
)

#: Cheia din `search_constraints` (v1) sub care stă subiectul. Una singură, citită de toți.
SUBJECT_KEY = "subject"

#: Bugetul subiectului în starea persistată (P4). Stă în cod, nu în disciplina apelanților.
MAX_SUBJECT_BYTES = 200

#: Câte nevoi poartă subiectul. Mai multe nu schimbă ordinea în practică, dar consumă bugetul.
MAX_SUBJECT_NEEDS = 3

#: Minimum de produse care trebuie să poarte tipul dominant: un singur produs cu tip într-un set de
#: șase nu e un subiect, e o întâmplare.
_MIN_TYPED = 2

_MAX_VALUE_CHARS = 48


@dataclass(frozen=True, slots=True)
class ConversationSubject:
    """Ce cumpără clientul. Doar chei de catalog, niciun produs, niciun text de client (P8, P12)."""

    shelf_key: str | None = None
    product_type: str | None = None
    needs: tuple[tuple[str, str], ...] = ()
    source_turn_id: str | None = None

    @property
    def is_empty(self) -> bool:
        return not (self.shelf_key or self.product_type or self.needs)

    def need_values(self, dimension: str) -> tuple[str, ...]:
        return tuple(v for d, v in self.needs if d == dimension)

    def to_dict(self) -> dict[str, Any]:
        """Forma persistată, compactă și MĂRGINITĂ la `MAX_SUBJECT_BYTES`.

        Se sacrifică întâi nevoile (cele mai vechi), apoi id-ul turului sursă. Tipul și raftul nu:
        fără ele subiectul nu mai spune nimic."""
        doc: dict[str, Any] = {}
        if self.shelf_key:
            doc["shelf"] = self.shelf_key
        if self.product_type:
            doc["type"] = self.product_type
        if self.needs:
            doc["needs"] = [[d, v] for d, v in self.needs]
        if self.source_turn_id:
            doc["turn"] = self.source_turn_id
        while _size(doc) > MAX_SUBJECT_BYTES and doc.get("needs"):
            doc["needs"] = doc["needs"][1:] or None
            if doc["needs"] is None:
                del doc["needs"]
        if _size(doc) > MAX_SUBJECT_BYTES:
            doc.pop("turn", None)
        return doc

    @classmethod
    def from_dict(cls, raw: object) -> ConversationSubject | None:
        """Hidratare DEFENSIVĂ. Orice formă coruptă ⇒ `None` (P6: memorie pierdută, nu tur rupt)."""
        if not isinstance(raw, Mapping):
            return None
        needs: list[tuple[str, str]] = []
        for item in raw.get("needs") or ():
            if (
                isinstance(item, (list, tuple))
                and len(item) == 2
                and all(isinstance(x, str) and x for x in item)
            ):
                needs.append((item[0][:32], item[1][:_MAX_VALUE_CHARS]))
        subject = cls(
            shelf_key=_clean(raw.get("shelf")),
            product_type=_clean(raw.get("type")),
            needs=tuple(needs[-MAX_SUBJECT_NEEDS:]),
            source_turn_id=_clean(raw.get("turn")),
        )
        return None if subject.is_empty else subject


def dominant_type(types: Iterable[str | None]) -> str | None:
    """Tipul care acoperă STRICT peste jumătate din produsele cu tip cunoscut, și cel puțin 2.

    Produsele FĂRĂ tip nu intră la numitor: pe SOLE 24% din catalog n-are tip extras, iar a le
    număra ar face ca un set de patru creme și două produse netipate să pară amestecat."""
    known = [t for t in types if isinstance(t, str) and t]
    if len(known) < _MIN_TYPED:
        return None
    value, count = Counter(known).most_common(1)[0]
    if count < _MIN_TYPED or count * 2 <= len(known):
        return None
    return value


def product_type_of(product: Mapping[str, Any]) -> str | None:
    attrs = product.get("attributes")
    if not isinstance(attrs, Mapping):
        return None
    value = attrs.get("product_type")
    return value if isinstance(value, str) and value else None


def resolve_shelf(vocab: CatalogVocabulary | None, raw: str | None) -> str | None:
    """Șirul modelului → cheie de categorie `KNOWN`, altfel `None`.

    Aceeași funcție pe care o folosește căutarea (`_resolve_search_terms`), deci „raftul"
    înseamnă același lucru pe ambele drumuri. Vocabular absent ⇒ `None` (fail-open, ca NX-295)."""
    if vocab is None or not raw or not raw.strip():
        return None
    try:
        r = resolve(vocab, raw, CATEGORY_DIMENSION)
    except Exception:  # noqa: BLE001 — un vocabular stricat nu rupe turul (P6)
        return None
    return r.key if r.status is ResolutionStatus.KNOWN else None


def resolve_needs(
    vocab: CatalogVocabulary | None,
    terms: Sequence[str],
    *,
    overlays: Mapping[str, Mapping[str, str]] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Nevoile rostite → `(dimensiune, cheie)` `KNOWN` pe fațetele catalogului.

    Același apel ca la căutare (`resolve_any` peste `vocab.facet_names`, cu overlay-urile
    pachetului), deci «hidratare» ajunge la cheia `hydration` exact cum ajunge filtrul."""
    if vocab is None or not terms:
        return ()
    out: list[tuple[str, str]] = []
    for term in terms:
        if not isinstance(term, str) or not term.strip():
            continue
        try:
            r = resolve_any(vocab, term, overlays=overlays, dimensions=vocab.facet_names)
        except Exception:  # noqa: BLE001 — fail-open
            continue
        if r.status is ResolutionStatus.KNOWN and r.key:
            pair = (r.dimension, r.key[:_MAX_VALUE_CHARS])
            if pair not in out:
                out.append(pair)
    return tuple(out)


def derive_subject(
    *,
    displayed: Sequence[Mapping[str, Any]],
    shelf: str | None,
    needs: Sequence[tuple[str, str]],
    vocab: CatalogVocabulary | None,
    previous: ConversationSubject | None,
    topic_switched: bool = False,
    turn_id: str | None = None,
) -> ConversationSubject:
    """Subiectul de după tur. PURĂ.

    `displayed` = produsele aduse în tur (cu `attributes`); gol ⇒ turul n-a arătat nimic nou și
    subiectul se moștenește. `needs` sunt deja rezolvate (`resolve_needs`) și coroborate."""
    shelf_key = resolve_shelf(vocab, shelf)
    typed = [t for t in (product_type_of(p) for p in displayed) if t]
    prev_type = previous.product_type if previous is not None else None
    # Câtă dovadă aduce setul turului despre tip. Sub `_MIN_TYPED` produse cu tip, setul nu e o
    # recomandare, e un DETALIU (howto, „spune-mi mai multe"): pe conversația reală turul dinaintea
    # lui «mai ieftin» arăta exact o cremă, iar a-l trata ca set nou ar fi șters subiectul fix
    # acolo unde contează. Un singur produs ANCOREAZĂ un subiect doar când nu există altul.
    if len(typed) >= _MIN_TYPED:
        evidence = True
        new_type = dominant_type(typed)
    elif len(typed) == 1 and prev_type is None:
        evidence = True
        new_type = typed[0]
    else:
        evidence = False
        new_type = prev_type

    type_changed = evidence and prev_type is not None and new_type != prev_type
    reset = topic_switched or type_changed
    base = None if reset else previous
    product_type = new_type if evidence else (base.product_type if base else None)
    has_new_set = evidence

    merged_needs: list[tuple[str, str]] = list(base.needs) if base else []
    for pair in needs:
        if pair in merged_needs:
            merged_needs.remove(pair)
        merged_needs.append(pair)

    subject = ConversationSubject(
        shelf_key=shelf_key or (base.shelf_key if base else None),
        product_type=product_type,
        needs=tuple(merged_needs[-MAX_SUBJECT_NEEDS:]),
        source_turn_id=turn_id if has_new_set else (base.source_turn_id if base else turn_id),
    )
    return subject


#: Drumurile care aleg un set, derivate din evenimentele turului (P10: stagiile nu știu că sunt
#: măsurate). Ordinea e precedența: un tur de cross-sell poate avea și o căutare în el.
_PATHS: tuple[tuple[str, str], ...] = (
    ("cross_sell", "cross_sell"),
    ("cheaper_followup", "cheaper"),
    ("show_more", "pagination"),
    ("product_search", "search"),
)

#: Drumurile care schimbă tipul INTENȚIONAT: se raportează, dar nu se citesc ca eșec de potrivire.
TYPE_CHANGING_PATHS: frozenset[str] = frozenset({"cross_sell"})


def subject_match_report(
    subject: ConversationSubject | None,
    products: Sequence[Mapping[str, Any]],
    event_types: Iterable[str],
    retrieved: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any] | None:
    """Evenimentul `subject_match` al unui tur cu carduri, sau `None` fără carduri. PURĂ.

    Prima măsurătoare de POTRIVIRE din sistem (clasa NX-309), pe o singură dimensiune, fără să
    blocheze nimic. Fără text de client și fără id-uri de produs (P12): doar numărători și
    drumul. Un tur cu carduri fără niciun eveniment de retrieval le-a re-arătat din stare
    (`rehydrate`).

    `retrieved` = produsele turului CU `attributes` (`ctx.retrieval`). Cardurile servite pe calea
    bogată sunt compacte (`compose.card_products`: id, nume, preț, url, imagine), deci n-au tip:
    citit doar de pe card, raportul dădea `type_matched=0` pe ORICE tur bogat (turul `a623c53e`:
    două creme de față servite, subiect «crema de fata», share 0,0). Tipul se ia de pe rândul de
    retrieval cu același id, iar cardul rămâne sursa când îl poartă el însuși."""
    if not products:
        return None
    seen = set(event_types)
    path = next((name for event, name in _PATHS if event in seen), "rehydrate")
    subject_type = subject.product_type if subject else None
    by_id: dict[str, str] = {}
    for row in retrieved:
        rid = row.get("product_id") or row.get("id")
        ptype = product_type_of(row)
        if rid and ptype:
            by_id.setdefault(str(rid), ptype)

    def _type(p: Mapping[str, Any]) -> str | None:
        own = product_type_of(p)
        if own:
            return own
        pid = p.get("product_id") or p.get("id")
        return by_id.get(str(pid)) if pid else None

    matched = sum(1 for p in products if _type(p) == subject_type) if subject_type else 0
    return {
        "path": path,
        "subject_type_known": subject_type is not None,
        "served": len(products),
        "type_matched": matched,
        "share": round(matched / len(products), 2) if subject_type else None,
        "needs_count": len(subject.needs) if subject else 0,
        "type_change_expected": path in TYPE_CHANGING_PATHS,
    }


def spoken_needs(state: object) -> tuple[tuple[str, str], ...]:
    """NX-316: nevoile pe care clientul le-a ROSTIT, ca `(dimensiune, cheie)`, din subiectul
    persistat în starea v1. Gol când nu există subiect (flag stins, conversație nouă, stare
    tăiată). Citit de chips ca să nu ofere o îngustare pe care clientul a făcut-o deja."""
    constraints = getattr(state, "search_constraints", None)
    if not isinstance(constraints, dict):
        return ()
    subject = ConversationSubject.from_dict(constraints.get(SUBJECT_KEY))
    return subject.needs if subject is not None else ()


def subject_is_new(state: object, turn_id: str | None) -> bool:
    """NX-316 felia 3: e turul ăsta PRIMUL al subiectului curent? Adevărat și fără subiect
    (conversație nouă, flag stins, stare tăiată) sau fără `source_turn_id` (sacrificat la
    `MAX_SUBJECT_BYTES`): în dubiu păstrăm comportamentul de azi, adică raftul vecin oferit."""
    constraints = getattr(state, "search_constraints", None)
    if not isinstance(constraints, dict):
        return True
    subject = ConversationSubject.from_dict(constraints.get(SUBJECT_KEY))
    if subject is None or not subject.source_turn_id:
        return True
    return subject.source_turn_id == turn_id


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()[:_MAX_VALUE_CHARS]
    return value or None


def _size(doc: Mapping[str, Any]) -> int:
    return len(json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


__all__ = [
    "MAX_SUBJECT_BYTES",
    "MAX_SUBJECT_NEEDS",
    "SUBJECT_KEY",
    "TYPE_CHANGING_PATHS",
    "ConversationSubject",
    "derive_subject",
    "dominant_type",
    "product_type_of",
    "resolve_needs",
    "resolve_shelf",
    "spoken_needs",
    "subject_match_report",
]
