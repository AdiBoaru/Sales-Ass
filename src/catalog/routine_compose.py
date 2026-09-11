"""Compunerea unei rutine: pași din pachet × candidați din catalog → sloturi (NX-292, felia 1).

Pur: fără DB, fără I/O, fără LLM, fără ceas. Aceeași intrare dă aceiași octeți, deci un raport de
acoperire rulat de două ori pe același catalog nu poate să difere, iar o rutină deja dată nu poate
fi rescrisă de un catalog schimbat după commit (aceeași regulă ca la projectorul NX-240).

## Ce decide modulul ăsta și ce nu

Decide **așezarea**: ce pas ocupă ce poziție, ce produs intră în ce slot, ce rămâne neacoperit și
DE CE. Nu decide **ordinea pașilor** (e în `domain_pack.routine_steps`, NX-280) și nu decide
**ranking-ul candidaților** (e al căutării). Apelantul dă candidații deja filtrați și deja
ordonați; modulul ia primul care mai e liber.

Împărțirea nu e cosmetică. Dacă ranking-ul ar intra aici, ar exista două locuri care decid „care
produs e cel mai bun pentru nevoia asta" — iar al doilea ar diverge tăcut de primul exact în turele
în care contează (buget, concerns, safety). Un slot e o POZIȚIE, nu o preferință.

## De unde vin candidații: fațeta, nu graful

Precedența e fațetă-întâi, graf-ca-ancoră, și motivul e în date. Muchiile `routine_next` nu
afirmă „exact produsul ăsta urmează": `build_relations.build_routine` le instanțiază pe un
REPREZENTANT al categoriei următoare și o declară în motiv (`representative: true`), fiindcă pașii
din conținut referă TIPURI, iar muchia e produs→produs.

Deci graful e autoritatea pe FORMĂ (ce urmează după ce, conform propriei fișe a produsului) și
fațeta `routine_step` e autoritatea pe INVENTAR (ce produse pot ocupa pasul ăsta). Fără ancoră
reală, graful n-are de unde porni, iar un lanț pornit dintr-un produs ales la întâmplare ar fi o
formă împrumutată de la un produs pe care clientul nu l-a cerut.

## Un pas neacoperit e un RĂSPUNS, nu un eșec

Un pas fără candidat rămâne slot cu `product_id=None` și un motiv din vocabular ÎNCHIS. Nu se sare,
nu se umple cu „ceva apropiat", nu scurtează rutina în tăcere. E aceeași regulă pe care
`build_relations` o respectă prin `anchors_without_edges`: „pentru pasul de esență n-am nimic care
să intre în bugetul tău" e o informație pentru client, iar un pas dispărut în tăcere e o minciună
structurală, pe care nimic din aval n-o prinde (validatorul și `grounding_guard` sunt porți de
ADEVĂR, nu de POTRIVIRE).

Din același motiv motivele sunt un vocabular închis și nu text liber: „n-am găsit" și „n-am căutat"
trebuie să rămână distincte în raport, iar un motiv inventat le-ar amesteca.

## Un produs ocupă cel mult un slot

Prin construcție un produs are un singur `routine_step`, deci coliziunea ar trebui să fie
imposibilă. Garda rămâne fiindcă apelantul poate greși listele, iar o rutină care recomandă același
produs la doi pași e un sfat absurd („pune crema, apoi crema") pe care validatorul l-ar lăsa să
treacă: produsul e real, prețul e real.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from src.domain.routine_steps import RoutineSpec

__all__ = [
    "MIN_SLOTS_FOR_ROUTINE",
    "ORIGINS",
    "UNCOVERED_REASONS",
    "RoutinePlan",
    "RoutineSlot",
    "UnknownFamilyError",
    "compose",
]


class UnknownFamilyError(ValueError):
    """Familie care nu există în pachet. Fail-closed: o rutină pentru o familie necunoscută n-are
    ordine de pași, deci n-are ce compune. Apelantul rezolvă familia DIN pachet, deci ajungerea
    aici e un bug, nu o intrare de client."""


#: Cum a ajuns produsul în slot, la ACEASTĂ compunere. `pinned` e o valoare de sine stătătoare,
#: nu o combinație: la o recompunere, un slot pinuit n-a fost ALES de aici, a fost preluat. Cum
#: ajunsese acolo prima oară e o proprietate a rândului persistat, nu a compunerii curente.
ORIGINS: frozenset[str] = frozenset({"facet", "graph", "pinned"})

#: De ce un pas a rămas neacoperit. Vocabular ÎNCHIS (ca `ACTION_ERROR_CODES`): intră în raport și
#: în metrici, iar un motiv liber ar face două rulări incomparabile exact când cineva compară.
#:
#: `no_candidate` = apelantul n-a dat niciun candidat și n-a spus de ce (nu știm dacă lipsa e a
#: catalogului sau a filtrelor). Celelalte le declară apelantul, fiindcă el e cel care a filtrat:
#: modulul ăsta nu vede prețuri, stoc sau reguli de siguranță.
UNCOVERED_REASONS: frozenset[str] = frozenset(
    {
        "no_candidate",  # nicio intrare, fără explicație din partea apelantului
        "out_of_stock",  # existau produse pe pas, dar niciunul vandabil
        "budget",  # existau vandabile, dar peste bugetul cerut
        "safety",  # excluse de SafetyPolicy (NX-173)
        "filtered",  # excluse de constrângerile hard ale turului (concerns, skin_type, brand)
        "all_taken",  # toți candidații ocupau deja alt slot (vezi docstringul modulului)
    }
)

#: Sub două sloturi acoperite nu e o secvență, e o recomandare. Pragul e ACELAȘI pe care îl impune
#: `_routine_sequence_ok` (NX-280) în aval; e declarat aici ca apelantul să poată degrada ONEST
#: (P6) înainte de a promite o rutină, nu ca să se dubleze poarta.
MIN_SLOTS_FOR_ROUTINE = 2


@dataclass(frozen=True)
class RoutineSlot:
    """O poziție din rutină. `product_id=None` ⇒ pas DECLARAT neacoperit, cu motiv."""

    position: int  # 1-based: e o poziție pentru client, nu un index
    step: str  # pasul FĂRĂ familie ('curatare') — familia e a rutinei, nu a slotului
    product_id: str | None = None
    origin: str | None = None  # din ORIGINS; None ⇔ neacoperit
    uncovered_reason: str | None = None  # din UNCOVERED_REASONS; None ⇔ acoperit

    @property
    def covered(self) -> bool:
        return self.product_id is not None


@dataclass(frozen=True)
class RoutinePlan:
    """Rezultatul compunerii. Imutabil, serializabil, fără nimic derivat din ceas."""

    family: str
    slots: tuple[RoutineSlot, ...]
    #: Produse din `seed` care n-au putut fi așezate (pas din altă familie, pas necunoscut, sau
    #: slotul era deja pinuit). RAPORTATE, nu înghițite: un lanț de graf care nu se potrivește
    #: familiei cerute e o informație despre datele noastre, nu un detaliu de implementare.
    dropped_seeds: tuple[str, ...] = ()

    @property
    def covered_slots(self) -> tuple[RoutineSlot, ...]:
        return tuple(s for s in self.slots if s.covered)

    @property
    def uncovered_slots(self) -> tuple[RoutineSlot, ...]:
        return tuple(s for s in self.slots if not s.covered)

    @property
    def is_complete(self) -> bool:
        """TOȚI pașii familiei au produs. Distinct de `is_routine`: o rutină de 3 pași din 6 e
        prezentabilă, dar nu e completă, iar raportul trebuie să le poată deosebi."""
        return bool(self.slots) and all(s.covered for s in self.slots)

    @property
    def is_routine(self) -> bool:
        """Destui pași acoperiți cât să fie o SECVENȚĂ (vezi `MIN_SLOTS_FOR_ROUTINE`).

        False nu înseamnă „eșec": înseamnă că răspunsul onest e o recomandare pe un pas, nu o
        rutină. Apelantul degradează, nu tace (P6)."""
        return len(self.covered_slots) >= MIN_SLOTS_FOR_ROUTINE

    def product_ids(self) -> tuple[str, ...]:
        """Id-urile ocupate, în ordinea pașilor. Fără None-uri."""
        return tuple(s.product_id for s in self.slots if s.product_id is not None)


def compose(
    family: str,
    spec: RoutineSpec,
    *,
    candidates: Mapping[str, Sequence[str]],
    seed: Sequence[tuple[str, str]] = (),
    pinned: Mapping[int, str] | None = None,
    reasons: Mapping[str, str] | None = None,
) -> RoutinePlan:
    """Așază produsele pe pașii familiei. Pur și determinist.

    `candidates`  pas (fără familie) → id-uri ORDONATE de apelant (cel mai bun primul).
    `seed`        perechi `(product_id, pas)` din lanțul de graf, în ordinea lanțului. Primul
                  seed pentru un pas câștigă; restul intră în `dropped_seeds`.
    `pinned`      poziție (1-based) → produs pe care clientul l-a ales deja. Nu se mișcă
                  NICIODATĂ. Fără regula asta, un al doilea „ceva mai ieftin" pe alt pas ar putea
                  re-alege tăcut primul, iar clientul ar vedea cum i se anulează alegerea.
    `reasons`     pas → motiv (din `UNCOVERED_REASONS`) pentru care lista lui e goală. Apelantul
                  e singurul care ȘTIE: el a filtrat pe buget, stoc și siguranță.

    Precedența e pin > graf > fațetă, și e ordinea autorității: alegerea clientului bate forma
    dedusă din conținut, iar aceea bate umplerea din inventar.
    """
    steps = spec.families.get(family)
    if not steps:
        raise UnknownFamilyError(
            f"familia {family!r} nu e declarată în pachet (declarate: {sorted(spec.families)})"
        )

    pinned = dict(pinned or {})
    reasons = dict(reasons or {})
    if unknown := set(reasons.values()) - UNCOVERED_REASONS:
        raise ValueError(f"motive necunoscute: {sorted(unknown)}")
    if bad := {p for p in pinned if not 1 <= p <= len(steps)}:
        raise ValueError(f"poziții pinuite în afara familiei {family!r}: {sorted(bad)}")

    used: set[str] = set(pinned.values())

    # Seed-urile se indexează pe PAS, nu pe poziție: un lanț de graf spune „produsul ăsta e un pas
    # de tonifiere", iar poziția tonifierii o știe pachetul. Primul câștigă, deci ordinea lanțului
    # e respectată fără ca modulul să știe ce e un lanț.
    by_step: dict[str, str] = {}
    dropped: list[str] = []
    step_positions = {step: i + 1 for i, step in enumerate(steps)}
    for product_id, step in seed:
        position = step_positions.get(step)
        if position is None or position in pinned or step in by_step or product_id in used:
            dropped.append(product_id)
            continue
        by_step[step] = product_id
        used.add(product_id)

    slots: list[RoutineSlot] = []
    for position, step in enumerate(steps, start=1):
        if (product_id := pinned.get(position)) is not None:
            slots.append(RoutineSlot(position, step, product_id, "pinned"))
            continue
        if (product_id := by_step.get(step)) is not None:
            slots.append(RoutineSlot(position, step, product_id, "graph"))
            continue

        pool = candidates.get(step) or ()
        pick = next((pid for pid in pool if pid not in used), None)
        if pick is not None:
            used.add(pick)
            slots.append(RoutineSlot(position, step, pick, "facet"))
            continue

        # Trei feluri de gol, distincte în raport. `all_taken` e separat fiindcă spune ceva despre
        # COMPUNERE (candidatul exista, dar ocupa alt pas), nu despre catalog sau despre filtre.
        reason = reasons.get(step) or ("all_taken" if pool else "no_candidate")
        slots.append(RoutineSlot(position, step, None, None, reason))

    return RoutinePlan(family=family, slots=tuple(slots), dropped_seeds=tuple(dropped))
