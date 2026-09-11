"""`routine_plan` — secvența de pași a unei familii, compusă de SERVER (NX-292, felia 2).

## De ce un tool și nu o instrucțiune în prompt

Modelul știe perfect ce e o rutină: curățare, tonic, tratament, hidratare, protecție. Ce nu poate
ști e CARE dintre cele șase produse primite dintr-o căutare e gelul de curățare. Pe catalogul SOLE
numele are mediana 200 de caractere (nume plus frază de marketing), iar «rutina ten uscat» întorcea
același ruj în două nuanțe pe pozițiile 4 și 6 (NX-280). Un model care primește asta scrie
„Pasul 1: curățare" peste un produs care nu e curățare — cu preț real, deci validatorul (stagiul 8)
și `grounding_guard` (NX-240) îl lasă să treacă: sunt porți de ADEVĂR, nu de POTRIVIRE.

Deci serverul decide AȘEZAREA (`src/catalog/routine_compose.py`, pur) și modelul scrie proza. E
aceeași împărțire ca la `related_products`: serverul face graful, modelul spune de unde pornim.

## Ce e al pachetului și ce e al codului (P9)

Pașii, ordinea lor și harta `product_type → familie:pas` sunt ale tenantului
(`domain_pack.routine_steps`). Aici nu apare niciun cuvânt de cosmetică: `family` e un enum
completat din pachet la construcția schemei, exact ca `relation` la `related_products`. Un tenant
de electrocasnice declară alte familii (pașii de instalare) și tool-ul funcționează neschimbat.

## Trei reguli de onestitate, fiecare pentru un eșec măsurat

1. **Un pas fără candidat rămâne DECLARAT**, cu motiv, nu dispare din secvență. O rutină scurtată
   în tăcere pare completă, iar clientul n-are cum să afle ce lipsește.
2. **`no_candidate` ≠ `filtered`.** „Nu există produs pe pasul ăsta" și „există, dar nu pentru
   nevoia cerută" cer reparații diferite. De aceea, când o nevoie golește un pas, întrebăm a doua
   oară FĂRĂ nevoie — doar pentru pașii goliți — ca să știm care dintre cele două e.
3. **Bugetul nu taie pași.** Dacă nici cea mai ieftină combinație nu intră în buget, compunem
   oricum cea mai ieftină și SPUNEM cât e minimul. Un client care cere „rutină sub 200" și primește
   patru pași din șase, fără explicație, crede că atâtea există.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from src.catalog.routine_compose import RoutinePlan, compose
from src.db.queries.catalog import (
    get_products_by_ids,
    routine_candidates,
    routine_steps_of,
    traverse_relation_chain,
)
from src.domain.routine_steps import SEP
from src.tools.base import ToolResult, register
from src.tools.catalog_tools import _safety_gate
from src.web.localization import amount_text

if TYPE_CHECKING:
    from src.models import TurnContext
    from src.worker.runner import PipelineDeps

#: Câți candidați aducem pe pas. Mai mulți decât ne trebuie (1), fiindcă bugetul „urcă" prin ei și
#: fiindcă o excludere de siguranță trebuie să aibă pe ce cădea. Ieftin: rândurile n-au laterale.
CANDIDATES_PER_STEP = 8

#: Câte runde de re-alegere după poarta de siguranță. UNA: dacă și înlocuitorul e contraindicat,
#: pasul se declară neacoperit. O buclă nemărginită ar plăti hidratări la infinit pe un tur.
SAFETY_RETRIES = 1


@dataclass(frozen=True)
class RoutineStepRef:
    """Un pas ACOPERIT, așa cum îl vede compunerea răspunsului."""

    position: int
    step: str  # cheia din pachet ('curatare')
    label: str  # eticheta localizată, din pachet (fallback: cheia humanizată)
    product_id: str


@dataclass(frozen=True)
class RoutineView:
    """Secvența turului, pentru randare. Scrisă DOAR de `routine_plan` (owner unic, P3).

    Există fiindcă trei decizii de randare depind de ea și, fără ea, toate trei se iau greșit —
    tăcut:

    1. **Ordinalele din proză sunt FAPTE.** `scrub_intro` aruncă ÎNTREG intro-ul dacă întâlnește o
       cifră negrounded — apărare corectă împotriva prețurilor inventate. Dar pașii se scriu
       numerotat, iar „1. Curățare… 2. Tonifiere…" conține cifre pe care serverul le-a atribuit.
       Fără lista asta, răspunsul la o rutină ieșea cu cardurile pe ecran și FĂRĂ niciun text.
    2. **Cardul trebuie să spună ce pas e.** Șase carduri la rând, fără etichetă, obligă clientul
       să potrivească singur proza cu produsele.
    3. **Ordinea cardurilor e a SLOTURILOR.** Compunerea reordonează determinist după rankingul de
       retrieval; într-o secvență, asta ar pune „pasul 3" din text în dreptul cardului 5.
    """

    family: str
    steps: tuple[RoutineStepRef, ...]

    def by_product(self) -> dict[str, RoutineStepRef]:
        return {s.product_id: s for s in self.steps}

    def ordered_ids(self) -> tuple[str, ...]:
        return tuple(s.product_id for s in self.steps)

    def ordinals(self) -> set[str]:
        """Cifrele pe care modelul are voie să le rostească fiindcă le-a atribuit SERVERUL.

        Doar pozițiile sloturilor ACOPERITE, nu `range(1, 10)`: un plafon generos ar deschide
        poarta pentru orice cifră mică inventată, exact în turul în care listăm prețuri."""
        return {str(s.position) for s in self.steps}


class RoutineArgs(BaseModel):
    """Argumentele tool-ului. `family` e validat contra pachetului în handler, nu aici: mesajul de
    eroare trebuie să numească familiile REALE ale tenantului, iar modelul nu le are în schemă
    decât ca enum."""

    family: str
    concerns: list[str] = Field(default_factory=list)
    budget_max: float | None = None
    anchor_id: str | None = None


def _price(row: dict[str, Any]) -> Decimal | None:
    value = row.get("price")
    return Decimal(str(value)) if value is not None else None


def _fit_budget(
    steps: list[str], by_step: dict[str, list[dict[str, Any]]], budget: Decimal
) -> tuple[dict[str, list[str]], Decimal]:
    """Candidați reordonați ca rutina să intre în buget, plus MINIMUL realizabil.

    Algoritmul e „podea, apoi urcare": pornim de la cel mai ieftin candidat pe fiecare pas (podeaua
    = minimul pe care îl poate costa rutina) și cheltuim restul urcând pașii, în ordine, către
    candidatul mai bine cotat care încă încape.

    De ce nu o simplă filtrare pe preț: un plafon per produs nu e ce cere clientul. „Rutină completă
    sub 200 lei" e o constrângere pe SUMĂ, iar filtrarea per produs ori taie pași care ar fi
    încăput, ori lasă o sumă care depășește. De ce nu un knapsack exact: ar cere o funcție de
    utilitate pe care n-o avem (cât „valorează" un rang mai bun), iar o optimizare peste un scor
    inventat ar fi precizie falsă.

    Podeaua se întoarce ÎNTOTDEAUNA, chiar dacă depășește bugetul: e cifra pe care clientul trebuie
    s-o audă („cel mai ieftin se face cu X"), nu un eșec de ascuns.
    """
    floor = Decimal(0)
    cheapest: dict[str, dict[str, Any]] = {}
    for step in steps:
        pool = [c for c in by_step.get(step, []) if _price(c) is not None]
        if not pool:
            continue
        pick = min(pool, key=lambda c: (_price(c), c["id"]))
        cheapest[step] = pick
        floor += _price(pick) or Decimal(0)

    chosen = {step: pick for step, pick in cheapest.items()}
    spent = floor
    if floor <= budget:
        # Urcare, în ordinea pașilor: primul pas al rutinei e cel pe care clientul îl vede primul.
        for step in steps:
            pool = by_step.get(step, [])
            current = chosen.get(step)
            if current is None:
                continue
            for candidate in pool:  # deja ordonați după rang
                extra = (_price(candidate) or Decimal(0)) - (_price(current) or Decimal(0))
                if candidate["id"] != current["id"] and extra > 0 and spent + extra <= budget:
                    chosen[step] = candidate
                    spent += extra
                    break

    # Candidatul ales trece în FAȚĂ, restul rămân ca alternative (pentru re-alegerea de siguranță).
    out: dict[str, list[str]] = {}
    for step in steps:
        pool = by_step.get(step, [])
        head = chosen.get(step)
        ids = [c["id"] for c in pool]
        if head is not None:
            ids = [head["id"]] + [i for i in ids if i != head["id"]]
        out[step] = ids
    return out, floor


async def _seed_from_anchor(
    ctx: TurnContext, deps: PipelineDeps, anchor_id: str, family: str
) -> list[tuple[str, str]]:
    """Lanțul de graf al ancorei, tradus în perechi `(product_id, pas)`.

    Graful e autoritatea pe FORMĂ, nu pe produs: `build_relations.build_routine` instanțiază
    muchiile `routine_next` pe un REPREZENTANT al categoriei următoare și o declară în motiv
    (`representative: true`). Deci un hop spune „aici urmează un pas de tipul ăsta", iar pasul se
    citește din fațeta produsului-țintă, nu din muchie.

    Fără tip de muchie ORDONAT declarat de tenant, întoarce gol — nu ghicim o secvență."""
    pack = getattr(ctx.business, "domain_pack", None)
    registry = getattr(pack, "relation_kinds", None)
    sequences = tuple(s.kind for s in registry.sequences()) if registry is not None else ()
    if not sequences:
        return []

    async with deps.db("routine_seed") as conn:
        hops = await traverse_relation_chain(
            conn, ctx.business.id, anchor_id=anchor_id, kind=sequences[0], max_depth=6
        )
        ids = [str(h["id"]) for h in hops]
        if anchor_id not in ids:
            ids = [anchor_id, *ids]
        steps = await routine_steps_of(conn, ctx.business.id, ids) if ids else {}

    seed: list[tuple[str, str]] = []
    for product_id in ids:
        value = steps.get(product_id)
        if not value:
            continue
        fam, _, step = value.partition(SEP)
        if fam == family:  # un pas din altă familie nu e un pas al rutinei cerute
            seed.append((product_id, step))
    return seed


def _view(
    plan: RoutinePlan,
    products: dict[str, dict[str, Any]],
    language: str | None,
    *,
    floor: Decimal | None,
    budget: Decimal | None,
) -> str:
    """Vederea pentru MODEL: pașii numerotați, cu pasul numit explicit, și golurile declarate.

    Numerotarea e a serverului. Dacă am lăsa modelul să deducă ordinea din lista de produse, un tur
    în care un pas lipsește ar renumerota tăcut restul, iar „pasul 3" din conversație n-ar mai fi
    „pasul 3" la turul următor — exact ancora pe care se sprijină un follow-up."""
    from src.catalog.render_text import display_name

    lines = [f"Rutina {plan.family}, {len(plan.slots)} pași:"]
    for slot in plan.slots:
        if slot.product_id is None:
            lines.append(f"{slot.position}. {slot.step} — LIPSĂ ({slot.uncovered_reason})")
            continue
        row = products.get(slot.product_id) or {}
        price = row.get("sale_price") or row.get("price")
        price_text = f", {amount_text(float(price), language)} lei" if price is not None else ""
        lines.append(
            f"{slot.position}. {slot.step} — [{slot.product_id}] "
            f"{display_name(row.get('name'))}{price_text}"
        )

    if budget is not None and floor is not None and floor > budget:
        lines.append(
            f"Bugetul cerut nu acoperă toți pașii. Cel mai ieftin se face cu "
            f"{amount_text(float(floor), language)} lei. Spune-i asta, nu tăia pași în tăcere."
        )
    if plan.uncovered_slots:
        lines.append(
            "Pașii marcați LIPSĂ nu au produs. Spune-i clientului care lipsește și de ce, "
            "nu inventa unul și nu renumerota restul."
        )
    return "\n".join(lines)


@register("routine_plan")
async def routine_plan_tool(
    ctx: TurnContext, deps: PipelineDeps, args: dict[str, Any]
) -> ToolResult:
    """Secvența de pași a familiei, din fațeta `routine_step` (+ graful, dacă există ancoră)."""
    a = RoutineArgs(**args)
    pack = getattr(ctx.business, "domain_pack", None)
    spec = getattr(pack, "routine_steps", None)
    families = getattr(spec, "families", {}) or {}
    if not families:
        return ToolResult(
            ok=False,
            products=[],
            error="no_routine_support",
            llm_view="Magazinul nu are declarate rutine. Recomandă produse, nu pași.",
        )
    if a.family not in families:
        return ToolResult(
            ok=False,
            products=[],
            error="unknown_family",
            llm_view=(
                f"Nu am rutină pentru «{a.family}». Am pentru: {', '.join(sorted(families))}."
            ),
        )

    steps = list(families[a.family])
    values = [f"{a.family}{SEP}{s}" for s in steps]
    concerns = [c for c in (a.concerns or []) if c]

    async with deps.db("routine_candidates") as conn:
        # Cu buget cerem și cei mai ieftini de pe fiecare pas: un pool ales doar pe rang face
        # bugetul să MINTĂ. Măsurat pe catalogul SOLE — „rutină de față sub 200 lei" raporta
        # minimul 291 (cei mai bine cotați opt sunt scumpi), deși una de 187 există.
        rows = await routine_candidates(
            conn,
            ctx.business.id,
            values=values,
            concerns=concerns,
            per_step=CANDIDATES_PER_STEP,
            include_cheapest=a.budget_max is not None,
        )
        by_step: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_step.setdefault(str(row["step"]).partition(SEP)[2], []).append(row)

        # `no_candidate` ≠ `filtered`: pentru pașii goliți întrebăm A DOUA OARĂ fără nevoi, DOAR ca
        # să aflăm care dintre cele două e. Nu relaxăm nimic — rezultatul nu intră în candidați.
        reasons: dict[str, str] = {}
        empty = [s for s in steps if not by_step.get(s)]
        if empty and concerns:
            probe = await routine_candidates(
                conn,
                ctx.business.id,
                values=[f"{a.family}{SEP}{s}" for s in empty],
                concerns=None,
                per_step=1,
            )
            exists = {str(r["step"]).partition(SEP)[2] for r in probe}
            reasons = {s: ("filtered" if s in exists else "no_candidate") for s in empty}
        else:
            reasons = dict.fromkeys(empty, "no_candidate")

    floor: Decimal | None = None
    budget = Decimal(str(a.budget_max)) if a.budget_max else None
    if budget is not None:
        candidates, floor = _fit_budget(steps, by_step, budget)
    else:
        candidates = {s: [c["id"] for c in by_step.get(s, [])] for s in steps}

    seed = await _seed_from_anchor(ctx, deps, a.anchor_id, a.family) if a.anchor_id else []
    plan = compose(a.family, spec, candidates=candidates, seed=seed, reasons=reasons)

    # NX-173 (P0): poarta de siguranță e pe produsele HIDRATATE (are nevoie de ingrediente), deci
    # după alegere. Un pas al cărui produs e contraindicat primește următorul candidat; dacă și
    # acela cade, pasul se declară neacoperit — niciodată înlocuit tăcut cu ceva „apropiat".
    hydrated: dict[str, dict[str, Any]] = {}
    blocked: set[str] = set()
    for _ in range(SAFETY_RETRIES + 1):
        wanted = [s.product_id for s in plan.covered_slots if s.product_id not in hydrated]
        if not wanted:
            break
        async with deps.db("routine_hydrate") as conn:
            fetched = await get_products_by_ids(
                conn, ctx.business.id, wanted, limit=len(wanted), respect_content_status=True
            )
        kept, _ = _safety_gate(ctx, fetched, purpose="routine")
        kept_ids = {str(p.get("id")) for p in kept}
        hydrated.update({str(p.get("id")): p for p in kept})
        blocked |= {i for i in wanted if i not in kept_ids}
        if not blocked & {s.product_id for s in plan.covered_slots}:
            break
        # Re-alegere: candidații blocați ies din pool, compunerea se reia cu aceleași reguli.
        candidates = {s: [i for i in ids if i not in blocked] for s, ids in candidates.items()}
        plan = compose(
            a.family,
            spec,
            candidates=candidates,
            seed=[(p, s) for p, s in seed if p not in blocked],
            reasons={**reasons, **{s: "safety" for s in steps if not candidates.get(s)}},
        )

    ordered = [hydrated[s.product_id] for s in plan.covered_slots if s.product_id in hydrated]

    # Seam-ul către compunere. Scris ÎNAINTE de ramura de eșec? Nu: o secvență pe care n-o afirmăm
    # nu trebuie să dicteze randarea. `ctx.routine` rămâne None, cardurile se randează normal.
    if plan.is_routine:
        ctx.routine = RoutineView(
            family=a.family,
            steps=tuple(
                RoutineStepRef(
                    position=slot.position,
                    step=slot.step,
                    label=spec.label_of(slot.step, ctx.language),
                    product_id=slot.product_id,
                )
                for slot in plan.covered_slots
                if slot.product_id in hydrated
            ),
        )

    if not plan.is_routine:
        return ToolResult(
            ok=False,
            products=ordered,
            error="no_sequence",
            llm_view=(
                "Nu pot compune o secvență pentru asta: prea puțini pași au produs. "
                "Spune-i clientului și oferă ce ai, nu inventa pași.\n"
                + _view(plan, hydrated, ctx.language, floor=floor, budget=budget)
            ),
        )
    return ToolResult(
        ok=True,
        products=ordered,
        llm_view=_view(plan, hydrated, ctx.language, floor=floor, budget=budget),
    )
