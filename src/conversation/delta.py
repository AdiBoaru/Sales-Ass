"""NX-330 (kernel v1.0, pasul 3a) — interpretarea validată → propuneri pentru reducer.

Contractul (`docs/KERNEL-CONTRACT-v1.md`, „Reducer, thread and parking"): reducerul e SINGURUL
scriitor de stare (I3), iar el primește doar propuneri typed. Modulul traduce `CheckedChange`-urile
din `provenance.py` în `StateUpdateProposal`, în ordinea din contract: schimbările de subiect întâi,
apoi restul, în ordinea scrisă de model.

Trei reguli pe care le ține modulul, nu reducerul:

- **I23:** o schimbare `inferred` nu produce propunere. Devine `RankingSignal`, care ordonează
  rezultatele turului curent și nu se persistă niciodată.
- **Valoarea relativă** («mai ieftin decât ăsta») o calculează CODUL din prețul RECITIT în tur al
  produsului rezolvat `exact`. Fără o țintă exactă sau fără preț cunoscut, schimbarea e respinsă
  (`unknown_reference`), nu ghicită.
- **Paranteza cu schimbări** (`thread=aside` + `changes` nevide) se tratează ca `continue` și se
  numără (`aside_with_changes`): contractul, „Thread".
- **Direcția unei limite numerice** (NX-334) ajunge neschimbată în stare: `lte` pe cheia de sus,
  `gte` pe cea de jos, `eq` pe amândouă, pe preț și pe orice fațetă numerică, printr-un singur tabel
  (`_BOUND_KEYS`). O direcție pe care fațeta n-o declară se respinge (`polarity_conflict`).

Fiecare propunere se construiește cu op-ul LITERAL, ca extractorul NX-327 (poarta I3) să poată
clasifica static fiecare scriitor. Modul PUR: aceleași intrări ⇒ același `TurnDelta`, byte cu byte
(I20, partea pasului 3a)."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from statistics import median
from typing import Any, Literal

from src.conversation.interpretation import CheckedChange, ResolvedRef, TurnInterpretation
from src.conversation.needs import PRICE_BOUNDS, UNMAPPED_KEY, NeedKind, NeedVocabulary
from src.conversation.provenance import (
    MAX_ACTS,
    MAX_CHANGES,
    MAX_REFERENCES,
    PRICE,
    UNMAPPED,
    Handle,
)
from src.conversation.references import ReferenceFacts
from src.conversation.state_reducer import StateUpdateProposal
from src.conversation.state_v2 import MAX_TYPE_UMBRELLA

CATEGORY = "category"
#: Cheia universală a unei excluderi, când fațeta nu are propriul operator `not_contains`.
RESTRICTION_KEY = "restriction"

_SOURCE_BY_PROVENANCE: Mapping[str, str] = {
    "explicit": "user_explicit",
    "implicit": "user_implicit",
}

#: NX-334 — relație → limitele scrise, ca indici în perechea (jos, sus) a dimensiunii. UN tabel
#: pentru preț și fațete: pe `main` prețul avea unul, iar restul niciunul, deci «minim 256 GB»
#: ajungea plafon. `eq` pe o fațetă = ambele limite („3 locuri" = exact 3). Pe preț, `eq` e plafon:
#: regula contractului, „a bare price number" înseamnă `lte` („am 100 de lei").
_BOUND_KEYS: Mapping[str, tuple[int, ...]] = {"gte": (0,), "lte": (1,), "eq": (0, 1)}
_PRICE_EQ: tuple[int, ...] = (1,)


@dataclass(frozen=True)
class RankingSignal:
    """Un `inferred`: ordonează rezultatele turului curent, nu se persistă niciodată (I23)."""

    dimension: str
    value: str | float | None
    relation: str


@dataclass(frozen=True)
class TurnDelta:
    thread: Literal["continue", "aside", "resume"]
    proposals: tuple[StateUpdateProposal, ...] = ()
    ranking: tuple[RankingSignal, ...] = ()
    rejected: tuple[CheckedChange, ...] = ()
    counters: Mapping[str, int] = field(default_factory=dict)


#: NX-348: tipul de produs e a doua jumătate a SUBIECTULUI (`Topic.product_type`, NX-314/NX-331),
#: nu o nevoie. Trimis ca `set_need`, reducerul îl respingea mereu (`topic_key`), deci pe calea
#: interpretată un «vreau un ser» explicit nu ajungea niciodată în stare.
PRODUCT_TYPE = "product_type"
SUBJECT_DIMENSIONS = frozenset({CATEGORY, PRODUCT_TYPE})


def _is_subject(c: CheckedChange) -> bool:
    return (c.change.op in ("set", "add") and c.dimension in SUBJECT_DIMENSIONS) or (
        c.change.op == "clear" and c.change.target == "topic"
    )


def _merge_umbrellas(umbrellas: Sequence[tuple[str, ...]]) -> tuple[str, ...]:
    """NX-350 (recenzia, constatarea 5): mai multe tipuri vagi în același tur («o cremă sau un
    ser») se unesc PE RÂND (primul cod al fiecăreia, apoi al doilea…), ca plafonul să nu taie
    tăcut o umbrelă întreagă. Fiecare umbrelă își are codul presupus de model primul."""
    merged: list[str] = []
    for rank in range(max(len(u) for u in umbrellas)):
        for codes in umbrellas:
            if rank < len(codes) and codes[rank] not in merged:
                merged.append(codes[rank])
    return tuple(merged[:MAX_TYPE_UMBRELLA])


def _subject_proposal(
    changes: Sequence[CheckedChange], turn_id: str, counters: dict[str, int]
) -> StateUpdateProposal | None:
    """NX-348: schimbările de subiect ale turului (raft și/sau tip) → UN `set_topic` pe PERECHE.
    Două propuneri separate ar parca de două ori: raftul nou ar goli tipul, apoi tipul ar schimba
    iar perechea. Mai multe valori pe aceeași jumătate («un ruj și un fond de ten»): perechea nu mai
    e sigură, deci rămâne doar ULTIMA schimbare de subiect scrisă, singură (o pereche amestecată
    din valori diferite ar numi un subiect pe care clientul nu l-a cerut), numărat
    `subject_multiple`. Sursa propunerii e cea mai tare dintre schimbări (`explicit` întâi)."""
    category = [c for c in changes if c.dimension == CATEGORY]
    kind = [c for c in changes if c.dimension == PRODUCT_TYPE]
    if not category and not kind:
        return None
    if len(category) > 1 or len(kind) > 1:
        counters["subject_multiple"] = counters.get("subject_multiple", 0) + 1
        last = changes[-1]
        category = [last] if last.dimension == CATEGORY else []
        kind = [last] if last.dimension == PRODUCT_TYPE else []
    lead = next(
        (c for c in changes if c.provenance == "explicit" and c in (*category, *kind)), None
    )
    lead = lead or (category or kind)[-1]
    return StateUpdateProposal(
        "set_topic",
        category_key=str(category[-1].canonical_value) if category else None,
        product_type=str(kind[-1].canonical_value) if kind else None,
        **_common(lead, _SOURCE_BY_PROVENANCE[lead.provenance], turn_id),
    )


#: NX-390: actul pe care raftul numit într-o nevoie devine subiect (rutina; familia e raftul).
_BUNDLE = "bundle"


def _named_part(
    interp: TurnInterpretation,
    checked: Sequence[CheckedChange],
    subject: Sequence[CheckedChange],
    rejected: Sequence[CheckedChange],
) -> tuple[str, CheckedChange] | None:
    """NX-390 (`kernel.v10.0`): pe o rutină (`bundle`), raftul rădăcină numit în fraza unei nevoi
    spuse explicit («rutină pentru TEN gras» ⇒ `ten`) e subiectul, chiar când modelul a scris doar
    nevoia. Turul real `d847f351` (setul `routines-2026-10-09`): fără raft, poarta a întrebat
    „machiaj, ten sau păr?”, iar la «doar pt seara» a făcut rutina pe familia majoritară a tenului
    gras, adică machiajul. Regula de prompt NX-388 cerea modelului să scrie raftul, dar nu e o
    garanție; dovada e a validatorului (`CheckedChange.shelf`).

    Se aplică doar când turul nu are un raft spus EXPLICIT și nici un tip de produs spus clar
    (clientul a numit atunci ce vrea), doar pe un singur raft numit, iar un raft scris de model
    ne-explicit (ghicit) cedează locul celui numit de client. Tipurile spuse vag (umbrela, NX-350:
    «un toner, o cremă») nu spun pentru ce e rutina, deci raftul se adaugă lângă ele (setul
    `mixed-2026-10-09`, m5: «rutina de dimineata pt ten sensibil: un demachiant, un toner si o
    crema cu spf» a primit întrebarea de familie). `(raft, schimbarea care l-a numit)` sau None."""
    if not any(a.kind == _BUNDLE for a in interp.acts):
        return None
    if any(c.dimension == PRODUCT_TYPE or c.provenance == "explicit" for c in subject):
        return None
    refused = {id(r.change) for r in rejected}
    named = [
        c
        for c in checked
        if c.shelf
        and c.rejected is None
        and c.provenance == "explicit"
        and id(c.change) not in refused
        and (c.change.relation or "eq") != "avoid"
    ]
    if not named or len({c.shelf for c in named}) != 1:
        return None
    return str(named[0].shelf), named[0]


def _relative_price(
    c: CheckedChange,
    resolved: Sequence[ResolvedRef],
    facts: ReferenceFacts,
    targets: Collection[str] = frozenset(),
) -> float | None:
    """Prețul RECITIT al produsului la care se raportează schimbarea, sau None.

    NX-352 (`kernel.v5.0`, decis de Adi pe 2026-09-29): o țintă AMBIGUĂ («ceva mai ieftin» cu mai
    multe produse pe ecran) se raportează la MEDIANA prețurilor recitite ale candidaților, deci
    «mai ieftin decât majoritatea celor arătate». Recenzia NX-352 o îngrădește:
    - doar pe o referință care arată spre ECRAN (deictică, de atribut), nu pe un nume sau un ordinal
      ambigue (acolo clientul a numit ceva anume, doar nu știm care);
    - nu când referința e și ȚINTA unui act al turului: poarta întreabă atunci «la care te referi?»,
      iar o limită dură scrisă din ce sistemul însuși n-a lămurit ar supraviețui răspunsului;
    - doar pe candidații DISPONIBILI (un produs epuizat nu e o alternativă de preț).
    Fără cel puțin doi candidați rămași cu preț cunoscut, nimic (ca înainte)."""
    ref = next((r for r in resolved if r.ref_id == c.change.relative_to), None)
    if ref is None:
        return None
    if ref.outcome == "exact" and len(ref.product_ids) == 1:
        price = getattr(facts.products.get(ref.product_ids[0]), "price", None)
        return float(price) if price is not None else None
    if (
        ref.outcome != "ambiguous"
        or ref.kind not in _MEDIAN_KINDS
        or c.change.relative_to in targets
    ):
        return None
    prices = [
        float(fact.price)
        for pid in ref.product_ids
        if (fact := facts.products.get(pid)) is not None
        and fact.price is not None
        and fact.available is not False
    ]
    return float(median(prices)) if len(prices) >= 2 else None


#: NX-352: referințele care arată spre ECRAN; pe ele o țintă ambiguă de preț relativ are mediană.
_MEDIAN_KINDS = frozenset({"deictic", "attribute"})
#: Actele a căror țintă ambiguă NU aduce o întrebare a porții (căutarea și rutina acționează).
_NO_QUESTION_ACTS = frozenset({"find", "show_more", "bundle"})


def _ambiguous(c: CheckedChange, resolved: Sequence[ResolvedRef]) -> bool:
    ref = next((r for r in resolved if r.ref_id == c.change.relative_to), None)
    return ref is not None and ref.outcome == "ambiguous"


def _common(c: CheckedChange, source: str, turn_id: str) -> dict[str, Any]:
    """Câmpurile comune ale unei propuneri (sursă, tur, tărie, origine). `origin` (NX-331) spune
    reducerului că propunerea vine din interpretare: acolo o schimbare a perechii (raft, tip)
    parchează subiectul, pe când propunerile vechi ale lui `_learn_subject` actualizează tipul fără
    parcare."""
    strength = c.strength if c.strength in ("hard", "soft") else None
    return {"source": source, "turn_id": turn_id, "strength": strength, "origin": "interpretation"}


def _bound_keys(dimension: str, relation: str, needs: NeedVocabulary) -> list[str] | None:
    """Cheile limitelor pe care le scrie o relație pe o dimensiune numerică. `[]` = dimensiunea
    nu e numerică (nu e treaba tabelului); `None` = e numerică, dar direcția nu există pe ea
    (fațeta n-o declară, sau relația nu e o limită)."""
    pair = needs.bounds_for(dimension)
    if pair is None:
        return []
    positions = _PRICE_EQ if dimension == PRICE and relation == "eq" else _BOUND_KEYS.get(relation)
    if positions is None:
        # Pe preț, o relație care nu e limită (`contains`, `avoid`) își păstrează forma de dinainte:
        # ambele chei. Pe o fațetă numerică n-are formă (cheia simplă nu mai e o nevoie).
        return list(PRICE_BOUNDS) if dimension == PRICE else None
    keys = [pair[p] for p in positions]
    return None if any(k is None for k in keys) else [k for k in keys if k is not None]


def _need_proposals(
    c: CheckedChange,
    source: str,
    turn_id: str,
    needs: NeedVocabulary,
    value: str | float | None,
) -> list[StateUpdateProposal] | None:
    """Propunerile unei schimbări cu valoare. `None` = direcția nu există pe dimensiune (fațeta
    n-o declară): apelantul o respinge, nu o înghesuie în cealaltă limită."""
    common = _common(c, source, turn_id)
    relation = c.change.relation or "eq"
    if c.dimension == PRICE:
        keys = _bound_keys(PRICE, relation, needs) or list(PRICE_BOUNDS)
        return [StateUpdateProposal("set_need", key=k, value=value, **common) for k in keys]
    if c.dimension == UNMAPPED and relation == "avoid":
        # NX-333 (recenzia): «dar nu pentru gaming» e o EXCLUDERE fără dimensiune. Scris ca nevoie
        # `unmapped` pozitivă, plannerul l-ar fi pus în `rank_terms`, adică ar fi URCAT exact
        # produsele ocolite. Merge pe cheia universală de excludere, tot `soft` (I25: `unmapped`
        # nu e niciodată dur), iar plannerul o raportează ca gol (`exclusion`), nu ca filtru.
        soft = {**common, "strength": "soft"}
        return [StateUpdateProposal("set_need", key=RESTRICTION_KEY, value=value, **soft)]
    if c.dimension == UNMAPPED:
        # `unmapped` nu e niciodată dur (I25), oricât de explicit ar fi citatul.
        soft = {**common, "strength": "soft"}
        return [StateUpdateProposal("set_need", key=UNMAPPED_KEY, value=value, **soft)]
    if relation == "avoid":
        spec = needs.spec_for(c.dimension)
        exclusion = spec is not None and spec.kind is NeedKind.EXCLUSION
        key = c.dimension if exclusion else RESTRICTION_KEY
        return [StateUpdateProposal("set_need", key=key, value=value, **common)]
    bound = _bound_keys(c.dimension, relation, needs)
    if bound is None:
        return None
    if bound:
        return [StateUpdateProposal("set_need", key=k, value=value, **common) for k in bound]
    return [StateUpdateProposal("set_need", key=c.dimension, value=value, **common)]


def _structural_proposals(
    c: CheckedChange, source: str, turn_id: str, handle: Handle | None, needs: NeedVocabulary
) -> list[StateUpdateProposal] | None:
    """`clear` / `remove` / `replace`: operațiile pe handle-uri sau pe tot subiectul.

    `replace cN` pe o cheie de LISTĂ (NX-331) înseamnă „ACEA valoare, nu alta": retragerea valorii
    din handle, apoi valoarea nouă. Un `supersede` ar purta doar valoarea nouă, iar pe o listă
    reducerul ar ADĂUGA-o lângă cea veche (două nevoi de ten coexistă). Pe o cheie scalară
    `supersede` înlocuiește deja, atomic."""
    common = _common(c, source, turn_id)
    op = c.change.op
    if op == "clear" and c.change.target == "topic":
        return [StateUpdateProposal("clear_topic", **common)]
    if op == "clear":
        return [StateUpdateProposal("clear_all", **common)]
    assert handle is not None  # validarea a respins deja un handle necunoscut (`unknown_handle`)
    removed = StateUpdateProposal("revoke", key=handle.key, value=handle.value, **common)
    if op == "remove":
        return [removed]
    bound = _replaced_bound(c, handle, needs)
    if bound is None:
        return None
    if bound and bound != [handle.key]:
        # NX-334: «minim 256» → «de fapt maxim 512» e un `replace` pe handle-ul limitei de JOS cu
        # o relație de SUS. Un `supersede` pe aceeași cheie ar fi scris `storage_min = 512`, adică
        # direcția inversată; limita veche se retrage, iar valoarea nouă merge pe cheia relației.
        return [
            removed,
            *(
                StateUpdateProposal("set_need", key=k, value=c.canonical_value, **common)
                for k in bound
            ),
        ]
    spec = needs.spec_for(handle.key)
    if spec is not None and spec.kind is NeedKind.BOOLEAN:
        # NX-349 (recenzia): pe un fanion înlocuirea E starea nouă. Ca `supersede`, o stare spusă
        # doar descriptiv («de fapt vreau cu parfum», `implicit`) era respinsă tăcut
        # (`hard_downgrade`), deși același lucru scris ca `set` trecea.
        return [StateUpdateProposal("set_need", key=handle.key, value=c.canonical_value, **common)]
    if spec is not None and spec.kind is NeedKind.LIST:
        return [
            removed,
            StateUpdateProposal("set_need", key=handle.key, value=c.canonical_value, **common),
        ]
    return [StateUpdateProposal("supersede", key=handle.key, value=c.canonical_value, **common)]


def _replaced_bound(c: CheckedChange, handle: Handle, needs: NeedVocabulary) -> list[str] | None:
    """Cheile limitelor pe care le scrie un `replace` pe handle-ul unei limite numerice. `[]` =
    handle-ul nu e o limită, sau schimbarea nu spune o direcție (atunci rămâne pe cheia lui);
    `None` = direcția nu există pe dimensiune (respinsă, ca la `set`/`add`)."""
    if c.change.relation is None or needs.bounds_for(handle.dimension) is None:
        return []
    return _bound_keys(handle.dimension, c.change.relation, needs)


def to_delta(
    interp: TurnInterpretation,
    checked: Sequence[CheckedChange],
    resolved: Sequence[ResolvedRef] = (),
    facts: ReferenceFacts | None = None,
    *,
    handles: Sequence[Handle] = (),
    needs: NeedVocabulary | None = None,
    turn_id: str = "",
) -> TurnDelta:
    """`CheckedChange`-uri → `TurnDelta`. PUR. Nu atinge starea: propunerile le aplică reducerul."""
    needs = needs or NeedVocabulary()
    facts = facts or ReferenceFacts()
    # A doua recenzie NX-352: doar țintele actelor pe care poarta le poate întreba («la care te
    # referi?»: citiri și mutații). Un `find` sau o rutină care arată spre ecran nu întreabă.
    targets = {t for act in interp.acts if act.kind not in _NO_QUESTION_ACTS for t in act.targets}
    by_handle = {h.handle: h for h in handles}
    counters: dict[str, int] = {}

    thread = interp.thread
    if thread == "aside" and interp.changes:
        thread = "continue"
        counters["aside_with_changes"] = 1
    overflow = (
        max(0, len(interp.changes) - MAX_CHANGES)
        + max(0, len(interp.references) - MAX_REFERENCES)
        + max(0, len(interp.acts) - MAX_ACTS)
    )
    if overflow:
        counters["interpretation_truncated"] = overflow
    if thread == "aside":
        # Paranteza pură: nimic de propus. Identitatea pe stare (I5) o ține reducerul (NX-331).
        return TurnDelta(thread="aside", counters=counters)

    ordered = [c for c in checked if _is_subject(c)] + [c for c in checked if not _is_subject(c)]
    proposals: list[StateUpdateProposal] = []
    ranking: list[RankingSignal] = []
    rejected: list[CheckedChange] = []
    subject: list[CheckedChange] = []
    umbrella: list[tuple[str, ...]] = []
    umbrella_lead: CheckedChange | None = None

    for c in ordered:
        if c.rejected is not None:
            rejected.append(c)
            continue
        if c.provenance == "inferred" or c.strength == "ranking":
            if c.change.op in ("set", "add", "replace") and c.canonical_value is not None:
                ranking.append(
                    RankingSignal(c.dimension, c.canonical_value, c.change.relation or "eq")
                )
            continue
        source = _SOURCE_BY_PROVENANCE[c.provenance]
        if c.change.op in ("clear", "remove", "replace"):
            handle = by_handle.get(c.change.target or "")
            structural = _structural_proposals(c, source, turn_id, handle, needs)
            if structural is None:
                rejected.append(
                    c.model_copy(update={"rejected": "polarity_conflict", "strength": "ranking"})
                )
                continue
            proposals += structural
            continue
        avoided = (c.change.relation or "eq") == "avoid"
        # NX-384 (`kernel.v7.0`): un raft sau un tip OCOLIT («nu vreau creme») nu e subiectul: ca
        # pe orice altă dimensiune, `avoid` merge pe excludere (`_need_proposals`), pe care
        # plannerul o face filtru doar când catalogul poartă exact valoarea și clientul a spus-o.
        # Înainte devenea umbrela subiectului, iar căutarea urca exact cremele.
        if (
            c.dimension == PRODUCT_TYPE
            and not avoided
            and c.provenance != "explicit"
            and c.change.op in ("set", "add")
            and c.canonical_value is not None
        ):
            # NX-350 (kernel.v4.0, decis de Adi pe 2026-09-29): un tip spus VAG («cremă de
            # hidratare» → `crema de fata`, deși putea fi de corp sau de mâini) nu devine tipul
            # subiectului: subiectul ține minte UMBRELA cuvintelor clientului (toate codurile care
            # se potrivesc, calculate de validator), care ordonează în fiecare tur și nu parchează.
            if not subject and not umbrella:
                proposals.append(None)  # type: ignore[arg-type]  # locul propunerii de subiect
            umbrella_lead = umbrella_lead or c
            umbrella.append(c.umbrella or (str(c.canonical_value),))
            counters["subject_type_umbrella"] = counters.get("subject_type_umbrella", 0) + 1
            continue
        if c.dimension in SUBJECT_DIMENSIONS and not avoided and c.canonical_value is not None:
            # NX-348: se adună, apoi UN `set_topic` pe pereche, în poziția subiectului (primul).
            if not subject and not umbrella:
                proposals.append(None)  # type: ignore[arg-type]  # locul propunerii de subiect
            subject.append(c)
            continue
        value = c.canonical_value
        if c.change.relative_to is not None:
            value = _relative_price(c, resolved, facts, targets)
            if value is None:
                rejected.append(
                    c.model_copy(update={"rejected": "unknown_reference", "strength": "ranking"})
                )
                continue
            if _ambiguous(c, resolved):
                counters["relative_price_median"] = counters.get("relative_price_median", 0) + 1
        made = _need_proposals(c, source, turn_id, needs, value)
        if made is None:
            rejected.append(
                c.model_copy(update={"rejected": "polarity_conflict", "strength": "ranking"})
            )
            continue
        proposals += made

    named = _named_part(interp, checked, subject, rejected)
    if named is not None:
        # un raft ghicit de model cedează locul celui numit de client; umbrela tipurilor spuse vag
        # («un toner, o cremă») rămâne și se adaugă pe aceeași propunere de subiect
        subject = [c for c in subject if c.dimension != CATEGORY]
        counters["subject_from_named_part"] = 1
        if not any(p is None for p in proposals):
            proposals.insert(0, None)  # type: ignore[arg-type]  # locul propunerii de subiect

    if subject or umbrella or named is not None:
        made = _subject_proposal(subject, turn_id, counters) if subject else None
        if umbrella and umbrella_lead is not None:
            if any(s.dimension == PRODUCT_TYPE for s in subject):
                # Un tip spus clar lângă unul vag: clarul câștigă (reducerul), ca la NX-348.
                counters["subject_multiple"] = counters.get("subject_multiple", 0) + 1
            codes = _merge_umbrellas(umbrella)
            if made is None:
                made = StateUpdateProposal(
                    "set_topic",
                    type_umbrella=codes,
                    **_common(
                        umbrella_lead, _SOURCE_BY_PROVENANCE[umbrella_lead.provenance], turn_id
                    ),
                )
            else:
                made = replace(made, type_umbrella=codes)
        if named is not None:
            shelf, lead = named
            made = (
                replace(made, category_key=shelf)
                if made is not None
                else StateUpdateProposal(
                    "set_topic",
                    category_key=shelf,
                    product_type=None,
                    **_common(lead, _SOURCE_BY_PROVENANCE["explicit"], turn_id),
                )
            )
        proposals = [made if p is None else p for p in proposals]
    return TurnDelta(
        thread=thread,
        proposals=tuple(p for p in proposals if p is not None),
        ranking=tuple(ranking),
        rejected=tuple(rejected),
        counters=counters,
    )


def accepted_changes(
    checked: Sequence[CheckedChange], delta: TurnDelta
) -> tuple[CheckedChange, ...]:
    """NX-352: schimbările validate pe care și delta le-a primit (nici validatorul, nici delta nu
    le-a respins). Doar ele pot scoate cuvinte din textul căutării (`plan_turn(checked=...)`)."""
    refused = {id(r.change) for r in delta.rejected}
    return tuple(c for c in checked if c.rejected is None and id(c.change) not in refused)


__all__ = ["UNMAPPED_KEY", "RankingSignal", "TurnDelta", "accepted_changes", "to_delta"]
