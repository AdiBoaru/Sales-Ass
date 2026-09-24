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

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from src.catalog.need_menu import NeedArg, split_args
from src.catalog.routine_compose import RoutinePlan, compose
from src.catalog.vocabulary import facet_overlays, resolve_any
from src.catalog.vocabulary_cache import get_vocabulary
from src.config import get_settings
from src.conversation.needs import corroborated_by
from src.db.queries.catalog import (
    get_products_by_ids,
    routine_candidates,
    routine_steps_of,
    traverse_relation_chain,
)
from src.domain.constraints import UnitRegistry
from src.domain.routine_steps import SEP
from src.tools.base import ToolResult, register
from src.tools.catalog_tools import (
    _is_relative_price_request,
    _safety_gate,
    apply_need_menu,
    client_texts,
    price_bound_source,
    price_units,
)
from src.web.localization import amount_text

if TYPE_CHECKING:
    from src.models import TurnContext
    from src.worker.runner import PipelineDeps

#: Câți candidați aducem pe pas. Mai mulți decât ne trebuie (1), fiindcă bugetul „urcă" prin ei și
#: fiindcă o excludere de siguranță trebuie să aibă pe ce cădea. Ieftin: rândurile n-au laterale.
CANDIDATES_PER_STEP = 8

#: Câți termeni nerezolvați raportăm (telemetrie + `llm_view`). Plafon, fiindcă lista vine din
#: argumentele modelului: vocabular scurt și normalizat, dar tot plafonat (P12).
_UNRESOLVED_MAX = 5

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

        Nu `range(1, 10)`: un plafon generos ar deschide poarta pentru orice cifră mică inventată,
        exact în turul în care listăm prețuri.

        NX-323: numerotarea e cea DENSĂ a pașilor arătați, 1..N, adică exact ce vede clientul pe
        carduri. Înainte erau pozițiile din ȘABLONUL familiei, iar pe conversația `bc7a356e`
        pașii acoperiți erau {1, 4, 5, 6}: modelul a scris «1.», «2.», «3.», ca orice om, iar
        poarta de cifre a aruncat toată rutina. Flag stins ⇒ pozițiile de șablon."""
        if get_settings().routine_dense_ordinals_enabled:
            return {str(i) for i in range(1, len(self.steps) + 1)}
        return {str(s.position) for s in self.steps}


@dataclass(frozen=True)
class RoutineArgVerdict:
    """NX-321: ce din argumentele modelului are voie să EXCLUDĂ produse din rutină."""

    budget_source: str | None  # PRICE_BOUND_* sau None (bugetul iese)
    unit_rejected: bool  # bugetul a picat DOAR fiindcă numărul purta altă unitate («100 ml»)
    kept_needs: tuple[str, ...]  # termeni rostiți de client
    dropped_needs: tuple[str, ...]  # termeni doar ai modelului


def routine_arg_provenance(
    budget_max: float | None,
    needs: Sequence[str],
    *,
    texts: Sequence[str],
    relative_request: bool,
    units: UnitRegistry | None,
) -> RoutineArgVerdict:
    """Bugetul și nevoile rutinei au o SURSĂ în ce a scris clientul? PURĂ.

    Conversația `bc7a356e`: la «fa mi o rutina» modelul a trimis un buget de 100 lei pe care
    clientul nu-l spusese (suma rutinei a ieșit exact 30+10+30+30, cu tonifierea și esența tăiate
    la buget) și o nevoie de roșeață pe care o scrisese BOTUL cu un tur înainte. `routine_plan` le
    executa pe amândouă ca `WHERE`. E aceeași regulă ca pe căutare (NX-319, NX-266): o deducție nu
    are voie să excludă. Bugetul trece prin ACEEAȘI `price_bound_source`; o nevoie trece doar dacă
    e coroborată de un mesaj al clientului (`corroborated_by`, pe prefix, ca `uttered_by_client`).

    Coroborarea literală e o limită declarată: «mi se usucă pielea» nu coroborează `dry`. De aceea
    o nevoie scoasă nu devine altceva aici; rezolvarea semantică e a NX-322."""
    source: str | None = None
    unit_rejected = False
    if budget_max is not None:
        kwargs = {"texts": texts, "relative_request": relative_request, "session_price_max": None}
        source = price_bound_source(budget_max, units=units, **kwargs)
        unit_rejected = (
            source is None
            and units is not None
            and price_bound_source(budget_max, **kwargs) is not None
        )
    kept: list[str] = []
    dropped: list[str] = []
    for term in needs:
        if any(corroborated_by(text, term) for text in texts):
            kept.append(term)
        else:
            dropped.append(term)
    return RoutineArgVerdict(source, unit_rejected, tuple(kept), tuple(dropped))


class RoutineArgs(BaseModel):
    """Argumentele tool-ului. `family` e validat contra pachetului în handler, nu aici: mesajul de
    eroare trebuie să numească familiile REALE ale tenantului, iar modelul nu le are în schemă
    decât ca enum."""

    family: str
    #: NX-322: string-uri (schema de azi) sau `{key, quote}` din meniul de nevoi.
    concerns: list[str | NeedArg] = Field(default_factory=list)
    budget_max: float | None = None
    anchor_id: str | None = None
    #: Momentul zilei, dacă clientul l-a spus. Validat contra `time_markers` în handler, ca
    #: `family`: un moment necunoscut se IGNORĂ (cade pe ordinea de zi întreagă), nu respinge
    #: turul — e un rafinament, nu o constrângere.
    moment: str | None = None


def _price(row: dict[str, Any]) -> Decimal | None:
    value = row.get("price")
    return Decimal(str(value)) if value is not None else None


def _fit_budget(
    steps: list[str],
    by_step: dict[str, list[dict[str, Any]]],
    budget: Decimal,
    *,
    sacrifice: tuple[str, ...] = (),
) -> tuple[dict[str, list[str]], Decimal, dict[str, Decimal]]:
    """Candidați reordonați ca rutina să intre în buget, MINIMUL pentru toți pașii, și pașii lăsați
    deoparte ca să încapă (cu cât ar costa fiecare).

    Algoritmul e „podea, apoi scurtare, apoi urcare":

    1. **Podeaua** = cel mai ieftin candidat pe fiecare pas, adică minimul pentru rutina COMPLETĂ.
    2. **Scurtarea**, dacă podeaua depășește bugetul: se renunță la pași în ordinea de sacrificiu
       DECLARATĂ (`sacrifice`), de la coadă, până ce restul încape. Lungimea rutinei devine astfel o
       consecință a bugetului, nu o constantă — un client cu 200 de lei primește o rutină de patru
       pași, nu un refuz. Măsurat pe `sole-ro`: rutina de față pentru ten uscat cerea 375 lei pe
       toți cei șase pași, iar patru costă 165. „Nu se poate sub 200" era fals, și era un fals
       produs de insistența noastră pe lungimea maximă, nu de catalog.
    3. **Urcarea**: restul bugetului se cheltuie înlocuind candidați cu unii mai bine cotați.

    Fără `sacrifice` (tenantul n-a declarat prioritate) pasul 2 NU rulează: se păstrează toți pașii
    și se declară minimul, adică exact comportamentul de dinainte. Config lipsă degradează într-un
    răspuns onest, nu într-unul arbitrar — a scurta după ordinea de APLICARE ar tăia protecția
    solară prima, fiindcă e ultimul pas aplicat și aproape primul în importanță.

    De ce nu o simplă filtrare pe preț: un plafon per produs nu e ce cere clientul. „Rutină completă
    sub 200 lei" e o constrângere pe SUMĂ, iar filtrarea per produs ori taie pași care ar fi
    încăput, ori lasă o sumă care depășește. De ce nu un knapsack exact: ar cere o funcție de
    utilitate pe care n-o avem (cât „valorează" un rang mai bun), iar o optimizare peste un scor
    inventat ar fi precizie falsă.

    Podeaua se întoarce ÎNTOTDEAUNA, chiar dacă depășește bugetul: e cifra pe care clientul trebuie
    s-o audă („toți pașii ar costa X"), nu un eșec de ascuns.
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

    # Scurtarea: renunțăm la pași de la coada ordinii de sacrificiu, dar DOAR la cei care au un
    # cost de plătit. Un pas fără candidat nu contribuie la podea, deci renunțarea la el n-ar
    # elibera nimic, iar raportul ar spune „l-am scos pentru buget" despre un pas care oricum
    # lipsea (`no_candidate` ≠ `budget`).
    dropped: dict[str, Decimal] = {}
    remaining = floor
    for step in reversed([s for s in sacrifice if s in cheapest]):
        if remaining <= budget:
            break
        price = _price(cheapest[step]) or Decimal(0)
        dropped[step] = price
        remaining -= price
        del cheapest[step]

    # Dacă nici renunțând la tot în afară de primul pas nu încape, nu s-a câștigat nimic: rutina
    # ciuntită ar fi la fel de imposibilă, dar și mai puțin utilă. Se revine la forma completă și
    # se declară minimul, ca înainte.
    if dropped and remaining > budget:
        for step, _price_of in dropped.items():
            cheapest[step] = min(
                (c for c in by_step.get(step, []) if _price(c) is not None),
                key=lambda c: (_price(c), c["id"]),
            )
        dropped = {}

    # Re-adăugarea: bucla de mai sus renunță în ordine, deci poate tăia mai mult decât trebuie.
    # Măsurat pe `sole-ro`, „rutină de dimineață sub 150 lei" scotea patru pași și lăsa 25 de lei
    # nefolosiți, deși tratamentul costă 10 — clientul plătea în pași o precizie de care nimeni
    # n-avea nevoie. O trecere greedy în ordinea priorității recuperează ce încape.
    #
    # Se face ÎNAINTEA urcării, și ordinea celor două nu e arbitrară: un pas în plus valorează mai
    # mult pentru client decât un produs mai bine cotat pe un pas care există deja. Rămâne în
    # ordinea de sacrificiu, deci nu poate readuce un pas mai puțin esențial peste unul mai
    # esențial care încă nu încape.
    for step in [s for s in sacrifice if s in dropped]:
        price = dropped[step]
        if remaining + price <= budget:
            cheapest[step] = min(
                (c for c in by_step.get(step, []) if _price(c) is not None),
                key=lambda c: (_price(c), c["id"]),
            )
            remaining += price
            del dropped[step]

    chosen = dict(cheapest)
    spent = sum((_price(p) or Decimal(0) for p in chosen.values()), Decimal(0))
    if spent <= budget:
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
    # Pasul la care s-a renunțat iese GOL, nu cu alternative: dacă ar rămâne candidați în listă,
    # compunerea l-ar umple oricum și bugetul ar fi depășit tăcut.
    out: dict[str, list[str]] = {}
    for step in steps:
        if step in dropped:
            out[step] = []
            continue
        pool = by_step.get(step, [])
        head = chosen.get(step)
        ids = [c["id"] for c in pool]
        if head is not None:
            ids = [head["id"]] + [i for i in ids if i != head["id"]]
        out[step] = ids
    return out, floor, dropped


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
    unresolved: list[str] | None = None,
    dropped: dict[str, Decimal] | None = None,
    skipped_for_moment: list[str] | None = None,
    moment: str | None = None,
    budget_ignored: bool = False,
    needs_ignored: int = 0,
) -> str:
    """Vederea pentru MODEL: pașii numerotați, cu pasul numit explicit, și golurile declarate.

    Numerotarea e a serverului. NX-323: e cea DENSĂ a pașilor acoperiți (1..N), aceeași pe care o
    vede clientul pe carduri și pe care o citește `reference_resolver` din starea turului, iar
    golurile stau pe linia lor, fără număr. Ancora stabilă a unui follow-up e ECRANUL, nu
    șablonul familiei: cu pozițiile de șablon, modelul vedea «1., 2. LIPSĂ, 3. LIPSĂ, 4.»,
    numerota el 1-2-3, iar clientul vedea încă o numerotare. Flag stins ⇒ forma de dinainte."""
    from src.catalog.render_text import display_name

    dense = get_settings().routine_dense_ordinals_enabled

    # Antetul spune ACOPERIT din DECLARAT, nu doar declarat: la un buget strâns, „Rutina fata,
    # 6 pași" urmat de patru LIPSĂ îl invită pe model să anunțe o rutină în șase pași.
    #
    # Formulat ca „pași acoperiți: 1 din 6", nu „1 pași acoperiți": cifra nu stă lipită de
    # substantiv, deci nu cere acord de plural. Alternativa ar fi fost un tabel de forme CLDR
    # pentru un text pe care îl citește modelul, nu clientul — iar „1 pași" în promptul lui e
    # exact felul de greșeală pe care o repetă în proză.
    covered = len(plan.covered_slots)
    total = len(plan.slots)
    head = f"{total} pași" if covered == total else f"pași acoperiți: {covered} din {total}"
    lines = [f"Rutina {plan.family}, {head}:"]
    number = 0
    missing: list[str] = []
    for slot in plan.slots:
        if slot.product_id is None:
            if dense:
                missing.append(f"{slot.step} ({slot.uncovered_reason})")
            else:
                lines.append(f"{slot.position}. {slot.step} — LIPSĂ ({slot.uncovered_reason})")
            continue
        number += 1
        row = products.get(slot.product_id) or {}
        price = row.get("sale_price") or row.get("price")
        price_text = f", {amount_text(float(price), language)} lei" if price is not None else ""
        lines.append(
            f"{number if dense else slot.position}. {slot.step} — [{slot.product_id}] "
            f"{display_name(row.get('name'))}{price_text}"
        )
    if missing:
        lines.append("Lipsesc: " + ", ".join(missing) + ".")

    # NX-321: fără liniile astea modelul își scrie bugetul în proză („rutina rămâne sub 100 lei")
    # chiar dacă nu l-a aplicat nimeni, iar poarta de cifre aruncă apoi toată fraza. Numărul de
    # nevoi, nu fraza: modelul o are deja în argumente, iar aici nu repetăm text al clientului.
    if budget_ignored:
        lines.append(
            "Buget: clientul NU a cerut un plafon de preț, deci rutina nu are unul. "
            "Nu menționa un buget."
        )
    if needs_ignored:
        lines.append(
            f"Nevoi ignorate, fiindcă nu le-a spus clientul: {needs_ignored}. "
            "Nu le prezenta ca fiind ale lui."
        )
    if moment:
        lines.append(f"Rutina cerută e pentru momentul «{moment}».")
    if skipped_for_moment:
        # Distinct de LIPSĂ, și distincția e a clientului, nu a noastră: un pas care nu se aplică
        # seara nu e un gol de acoperit, e un pas care n-are ce căuta acolo. Fără rândul ăsta,
        # modelul ar putea să-l „adauge" din proprie inițiativă ca să pară rutina completă.
        lines.append(
            "Pași care NU se aplică în momentul cerut, deci nu lipsesc: "
            + ", ".join(skipped_for_moment)
            + "."
        )
    if dropped:
        # Cifrele sunt cele mai MICI disponibile pe pasul respectiv, deci un prag inferior, nu un
        # preț. „Ar costa 210 lei" ar fi o afirmație pe care clientul o ia ca exactă și pe care
        # nimic din aval n-o poate contrazice: prețurile sunt reale, doar citite ca altceva.
        extra = sum(dropped.values(), Decimal(0))
        lines.append(
            "Am scurtat rutina ca să încapă în buget. Pași lăsați deoparte: "
            + ", ".join(
                f"{s} (de la {amount_text(float(p), language)} lei)" for s, p in dropped.items()
            )
            + f". Adăugați toți, ar costa cel puțin {amount_text(float(extra), language)} lei în "
            "plus. Spune-i clientului ce i-ai dat și ce poate adăuga mai târziu, nu că nu se poate."
        )
    if budget is not None and floor is not None and floor > budget and not dropped:
        lines.append(
            f"Bugetul cerut nu acoperă toți pașii. Cel mai ieftin se face cu "
            f"{amount_text(float(floor), language)} lei. Spune-i asta, nu tăia pași în tăcere."
        )
    if plan.uncovered_slots:
        lines.append(
            "Pașii de la «Lipsesc» nu au produs. Spune-i clientului care lipsește și de ce, "
            "nu inventa unul. Numerotează pașii exact ca mai sus."
            if dense
            else "Pașii marcați LIPSĂ nu au produs. Spune-i clientului care lipsește și de ce, "
            "nu inventa unul și nu renumerota restul."
        )
    if unresolved:
        # Un termen pe care catalogul nu-l cunoaște nu a filtrat nimic. Fără rândul ăsta, modelul
        # ar confirma o constrângere care n-a rulat („rutină fără parfum, gata") pe produse alese
        # fără ea. Validatorul nu poate prinde asta: prețurile și produsele sunt reale.
        lines.append(
            "Nu am putut filtra pe: "
            + ", ".join(f"«{t}»" for t in unresolved[:_UNRESOLVED_MAX])
            + ". Nu confirma cerința asta ca îndeplinită, spune că n-o pot verifica."
        )
    return "\n".join(lines)


async def _resolve_needs(
    ctx: TurnContext, deps: PipelineDeps, raw: list[str] | None
) -> tuple[dict[str, list[str]], list[str]]:
    """Termenii clientului → `dimensiune → chei canonice`, prin ACELAȘI vocabular ca căutarea.

    De ce nu se filtrează direct pe ce a scris modelul. Argumentul `concerns` ajunge aici cu
    cuvintele clientului („ten uscat", „hidratare"), fiindcă exact așa funcționează pe
    `search_products`, unde fiecare termen trece prin rezoluția contra catalogului. Aici nu trecea:
    lista mergea brut în `attributes->'concerns' ?| ...`. Măsurat pe `sole-ro` (2026-09-16), «rutina
    pentru ten uscat» scotea toți cei șase pași ai familiei `fata` ca `LIPSĂ (filtered)`, deși
    fiecare are peste o sută de produse vandabile. Iar `llm_view` îi spunea modelului „prea puțini
    pași au produs", adică îl trimitea să anunțe clientul că magazinul n-are rutină.

    Două cauze, amândouă închise de rezoluție. Prima e canonicitatea: cheia reală e `hydration`, nu
    „hidratare". A doua e mai adâncă și e chiar lecția NX-257: o nevoie a clientului nu e
    întotdeauna un `concern`. «ten uscat» e `skin_type=dry`, o dimensiune DISTINCTĂ (partiționantă,
    nu aditivă), deci nici cheia canonică n-ar fi ajutat cât timp SQL-ul filtra pe o singură
    dimensiune numită în cod.

    Termenul nerezolvat se ARUNCĂ, nu se pasează (P6: mai bine fără filtru decât cu unul care
    golește tăcut), dar se și RAPORTEAZĂ modelului, ca să nu confirme o constrângere pe care n-a
    aplicat-o nimeni. Vocabularul vine din cache-ul cu TTL (`get_vocabulary`), nu direct din DB, și
    își aduce propriul checkout scurt: rezoluția se face ÎNAINTE de checkout-ul de candidați, ca să
    nu ținem o conexiune peste altă muncă (NX-231).
    """
    terms = [t for t in (raw or []) if isinstance(t, str) and t.strip()]
    if not terms:
        return {}, []

    vocab = await get_vocabulary(deps, ctx.business.id)
    overlays = facet_overlays(getattr(ctx.business, "domain_pack", None), vocab.facet_names)

    filters: dict[str, list[str]] = {}
    unresolved: list[str] = []
    for term in terms:
        r = resolve_any(vocab, term, overlays=overlays, dimensions=vocab.facet_names)
        ctx.emit(
            "vocabulary_resolved",
            dimension=r.dimension,
            status=r.status.value,
            matched_by=r.matched_by,
            reason=r.reason,
            n_keys=len(r.constraint_keys),
            evidence=r.evidence,
            consumer="routine_plan",
        )
        if keys := r.constraint_keys:
            filters.setdefault(r.dimension, []).extend(keys)
        else:
            unresolved.append(r.term or term)
    for dim in filters:
        filters[dim] = sorted(dict.fromkeys(filters[dim]))
    if unresolved:
        ctx.emit("concern_unmapped", terms=unresolved[:_UNRESOLVED_MAX], locale=ctx.language)
    return filters, unresolved


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
    legacy_needs, need_args = split_args(a.concerns)
    a.concerns = legacy_needs

    # NX-321: argumentele fără sursă ies ÎNAINTE de rezoluție și de buget, deci nu ating nici
    # `routine_candidates(include_cheapest=…)`, nici `_fit_budget`.
    provenance: RoutineArgVerdict | None = None
    if get_settings().routine_arg_provenance_enabled:
        texts = client_texts(ctx)
        provenance = routine_arg_provenance(
            a.budget_max,
            a.concerns,
            texts=texts,
            relative_request=_is_relative_price_request(texts[0]),
            units=price_units(ctx),
        )
        if a.budget_max is not None and provenance.budget_source is None:
            a.budget_max = None
        a.concerns = list(provenance.kept_needs)

    facet_filters, unresolved = await _resolve_needs(ctx, deps, a.concerns)
    # NX-322: nevoile alese din meniu. `hard` (citat + alias al tenantului) filtrează ca orice
    # nevoie rezolvată; `soft` doar ordonează candidații în interiorul pasului.
    prefer: dict[str, list[str]] | None = None
    if need_args:
        vocab = await get_vocabulary(deps, ctx.business.id)
        prefer, hard = apply_need_menu(ctx, need_args, vocab, consumer="routine_plan")
        for dim, keys in hard.items():
            facet_filters[dim] = sorted({*facet_filters.get(dim, []), *keys})
    if provenance is not None:
        asked_budget = args.get("budget_max") is not None
        ctx.emit(
            "routine_arg_provenance",
            budget_source=provenance.budget_source or ("unsupported" if asked_budget else "none"),
            budget_kept=a.budget_max is not None,
            unit_rejected=provenance.unit_rejected,
            needs_kept=len(provenance.kept_needs),
            needs_dropped=len(provenance.dropped_needs),
            # Chei CANONICE ale pachetului (vocabular închis), nu textul clientului (P12).
            keys=sorted({k for keys in facet_filters.values() for k in keys})[:8],
        )

    async with deps.db("routine_candidates") as conn:
        # Cu buget cerem și cei mai ieftini de pe fiecare pas: un pool ales doar pe rang face
        # bugetul să MINTĂ. Măsurat pe catalogul SOLE — „rutină de față sub 200 lei" raporta
        # minimul 291 (cei mai bine cotați opt sunt scumpi), deși una de 187 există.
        rows = await routine_candidates(
            conn,
            ctx.business.id,
            values=values,
            facet_filters=facet_filters or None,
            per_step=CANDIDATES_PER_STEP,
            include_cheapest=a.budget_max is not None,
            prefer=prefer,
        )
        by_step: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_step.setdefault(str(row["step"]).partition(SEP)[2], []).append(row)

        # `no_candidate` ≠ `filtered`: pentru pașii goliți întrebăm A DOUA OARĂ fără nevoi, DOAR ca
        # să aflăm care dintre cele două e. Nu relaxăm nimic — rezultatul nu intră în candidați.
        reasons: dict[str, str] = {}
        empty = [s for s in steps if not by_step.get(s)]
        if empty and facet_filters:
            probe = await routine_candidates(
                conn,
                ctx.business.id,
                values=[f"{a.family}{SEP}{s}" for s in empty],
                facet_filters=None,
                per_step=1,
            )
            exists = {str(r["step"]).partition(SEP)[2] for r in probe}
            reasons = {s: ("filtered" if s in exists else "no_candidate") for s in empty}
        else:
            reasons = dict.fromkeys(empty, "no_candidate")

    # Momentul necunoscut se ignoră: ordinea de zi întreagă e răspunsul corect pentru un client
    # care n-a spus când, iar un 422 pe un rafinament ar pierde turul.
    moment = a.moment if a.moment in (spec.time_markers or {}) else None

    # Pasul care nu se aplică în momentul cerut nu e un gol, e o neaplicabilitate: o rutină de
    # seară nu „ratează" protecția solară. Iese din secvență ÎNAINTE de compunere, ca să nu ocupe
    # o poziție și să nu ceară o explicație pentru ceva ce nimeni nu se aștepta să fie acolo.
    if moment:
        skipped_for_moment = [s for s in steps if not spec.applies_at(s, moment)]
        steps = [s for s in steps if s not in skipped_for_moment]
        for s in skipped_for_moment:
            by_step.pop(s, None)
            reasons.pop(s, None)
        if not steps:
            # Toți pașii familiei sunt ai celuilalt moment. Nu e atins pe pachetul de azi (doar
            # protecția solară e legată de dimineață), dar un pachet în care ar fi e o configurare
            # validă, iar `compose` refuză o subsecvență goală. Un tool nu are voie să ridice:
            # răspundem cu ce E adevărat, ca modelul să poată spune de ce n-are ce oferi (P6).
            return ToolResult(
                ok=False,
                products=[],
                error="no_step_at_moment",
                llm_view=(
                    f"Niciun pas din rutina «{a.family}» nu se aplică în momentul «{moment}». "
                    "Spune-i clientului că pașii pe care îi avem sunt pentru celălalt moment al "
                    "zilei, nu inventa pași și nu compune o rutină."
                ),
            )
    else:
        skipped_for_moment = []

    floor: Decimal | None = None
    dropped: dict[str, Decimal] = {}
    budget = Decimal(str(a.budget_max)) if a.budget_max else None
    if budget is not None:
        candidates, floor, dropped = _fit_budget(
            steps,
            by_step,
            budget,
            sacrifice=tuple(s for s in spec.sacrifice_order(a.family, moment) if s in steps),
        )
        reasons.update(dict.fromkeys(dropped, "budget"))
    else:
        candidates = {s: [c["id"] for c in by_step.get(s, [])] for s in steps}

    seed = await _seed_from_anchor(ctx, deps, a.anchor_id, a.family) if a.anchor_id else []
    plan = compose(a.family, spec, candidates=candidates, seed=seed, reasons=reasons, steps=steps)

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
            reasons={
                **reasons,
                **{s: "safety" for s in steps if not candidates.get(s) and s not in dropped},
            },
            steps=steps,
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

    view_args: dict[str, Any] = {
        "floor": floor,
        "budget": budget,
        "unresolved": unresolved,
        "dropped": dropped,
        "skipped_for_moment": skipped_for_moment,
        "moment": moment,
        "budget_ignored": provenance is not None
        and args.get("budget_max") is not None
        and a.budget_max is None,
        "needs_ignored": len(provenance.dropped_needs) if provenance is not None else 0,
    }
    if not plan.is_routine:
        return ToolResult(
            ok=False,
            products=ordered,
            error="no_sequence",
            llm_view=(
                (
                    "Bugetul cerut ajunge pentru un singur pas, deci nu e o rutină. Spune-i cât "
                    "costă cel mai ieftin pas complet următor și oferă ce ai.\n"
                    if dropped
                    else "Nu pot compune o secvență pentru asta: prea puțini pași au produs. "
                    "Spune-i clientului și oferă ce ai, nu inventa pași.\n"
                )
                + _view(plan, hydrated, ctx.language, **view_args)
            ),
        )
    return ToolResult(
        ok=True, products=ordered, llm_view=_view(plan, hydrated, ctx.language, **view_args)
    )
