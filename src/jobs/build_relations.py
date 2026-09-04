"""NX-270 — construiește graful de relații din FAPTE. Job admin, dry-run implicit.

`product_relations` are 0 rânduri, iar codul de traversare e complet și generic. Consecința,
măsurată: **391 de produse epuizate n-au niciun substitut**, deși 390 au unul în stoc în aceeași
categorie, 381 și în ±30% preț, 339 și de la același brand. Graful poate recupera practic toate
cele 391; azi recuperează zero, iar „nu mai avem" e răspunsul final.

## Graful e o VEDERE peste fapte, nu o sursă de adevăr

Derivat, versionat, regenerabil. O muchie scrisă o dată și lăsată acolo driftează garantat — e exact
defectul care a ținut `concern_map` cinci săptămâni să trimită spre valori inexistente. De aia
fiecare muchie poartă `rule_id`: „regula R s-a dovedit greșită" trebuie să se repare global, nu
muchie cu muchie.

Și de aia poartă `source`. La scară, muchia valoroasă nu e „aceste două produse se aseamănă ca
text", ci „oamenii care s-au uitat la ăsta au cumpărat pe ălălalt". Graful comportamental bate
graful de conținut, iar în ziua în care există trafic trebuie să-l poată ÎNLOCUI fără rescriere:
aceeași tabelă, aceeași formă de rând, alt `source`. **Ce construim aici e schelă.**

## De ce nu se derivă pe „aceeași categorie"

Un graf peste fapte goale înseamnă „același raft, preț apropiat" — iar „îți dau altă cremă de 90 de
lei din același raft" nu e o recomandare, e o resemnare. `substitute` cere deci o NEVOIE comună, nu
doar o categorie comună. Consecința practică e că jobul depinde de NX-268: fără nevoile scrise în
`products.attributes`, ancorele n-au pe ce să se potrivească, iar jobul raportează zero în loc să
cadă pe raft. `--needs-from-derivation` măsoară ce AR produce graful după ce faptele sunt scrise,
fără să scrie nimic — altfel singurul mod de a afla ar fi să scrii mai întâi în producție.

    python -m src.jobs.build_relations --business <uuid>                       # dry-run
    python -m src.jobs.build_relations --business <uuid> --needs-from-derivation
    python -m src.jobs.build_relations --business <uuid> --apply               # scrie

`accessory` rămâne NEDERIVAT: n-avem sursă onestă, iar o muchie ghicită e mai rea decât absența ei.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import logging
import sys
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# Regulile sunt VERSIONATE — vezi comentariul migrării 048.
RULE_SUBSTITUTE = "content.same_category_shared_need_price_band.v1"
RULE_COMPLEMENT = "content.cross_category_shared_need.v1"
RULE_ROUTINE = "content.routine_step_representative.v1"
RULE_VARIANT = "content.shade_group.v1"

# Toate muchiile derivate din CONȚINUT poartă aceeași sursă. Când apare traficul, graful
# comportamental se scrie cu `behavioral` și îl înlocuiește pe acesta, nu se amestecă cu el.
SOURCE_DERIVED = "derived_content"

# Câte muchii ies dintr-un produs, per tip. La 2.758 de noduri, ~16.000 de muchii — un tabel mic, cu
# indexul de ancoră care există deja din 027. Plafonul nu e o optimizare: fără el, un produs dintr-o
# categorie mare ar avea sute de muchii, iar prima pagină ar fi aleasă de ordinea din DB.
MAX_EDGES_PER_ANCHOR = 6

# Cât de departe poate fi prețul unui substitut. Nu e o preferință: un „substitut" la jumătate de
# preț e alt segment, iar unul la dublu e un upsell deghizat în alternativă.
PRICE_BAND = 0.30

# Tipurile pe care le SCRIE jobul. Lista asta e una dintre cele TREI care trebuie să coincidă
# (schema, pachet, job) — vezi testul dedicat. Două care coincid și una care minte e exact clasa de
# defect care a ținut `messages.content_type = 'action'` ascunsă până la primul Postgres real.
WRITTEN_KINDS = ("substitute", "complement", "routine_next", "variant_of")


@dataclass
class Edge:
    """O muchie derivată, cu tot ce trebuie ca să poată fi explicată sau ștearsă global."""

    product_id: str
    related_id: str
    kind: str
    position: int
    rule_id: str
    reason: dict[str, Any]
    source: str = SOURCE_DERIVED


@dataclass
class BuildReport:
    """Ce a produs construcția. `anchors_without_edges` e la fel de important ca restul: un produs
    epuizat fără alternativă trebuie RAPORTAT, nu ascuns — altfel „n-am găsit" arată identic cu
    „n-am căutat"."""

    edges: list[Edge] = field(default_factory=list)
    out_of_stock: int = 0
    out_of_stock_covered: int = 0
    # DOUĂ liste, nu una, fiindcă sunt două lucruri diferite și confundarea lor ar ascunde exact
    # cauza: „n-are alternativă" e o afirmație despre catalog, „nu știm nimic despre el" e o
    # afirmație despre datele NOASTRE. Prima e o informație pentru client, a doua e o sarcină
    # pentru NX-268.
    anchors_without_edges: list[str] = field(default_factory=list)
    out_of_stock_without_needs: list[str] = field(default_factory=list)
    by_kind: collections.Counter = field(default_factory=collections.Counter)
    skipped_no_needs: int = 0

    def add(self, edge: Edge) -> None:
        self.edges.append(edge)
        self.by_kind[edge.kind] += 1


def _needs(product: dict[str, Any]) -> set[str]:
    """Nevoile unui produs, din `attributes->concerns`. Set gol = nu știm, NU „nicio nevoie".

    Distincția contează la `substitute`: un produs fără nevoi cunoscute nu primește substituți
    (D7 — n-avem pe ce potrivi), în loc să cadă pe „aceeași categorie", care ar fi raftul."""
    attributes = product.get("attributes")
    if not isinstance(attributes, dict):
        return set()
    values = attributes.get("concerns")
    if isinstance(values, list):
        return {str(v) for v in values if isinstance(v, str)}
    return {str(values)} if isinstance(values, str) else set()


def _price(product: dict[str, Any]) -> float | None:
    value = product.get("price")
    return float(value) if isinstance(value, (int, float)) else None


def _in_band(anchor_price: float | None, other_price: float | None) -> tuple[bool, float | None]:
    if anchor_price is None or other_price is None or anchor_price <= 0:
        return False, None
    delta = (other_price - anchor_price) / anchor_price
    return abs(delta) <= PRICE_BAND, round(delta, 3)


def _sellable(product: dict[str, Any]) -> bool:
    return product.get("availability") in ("in_stock", "low_stock")


def _rank(product: dict[str, Any]) -> tuple:
    """Ordonare DETERMINISTĂ a candidaților. Rating-ul „shrunk" (aceeași formulă ca în SQL: un 5.0
    cu o recenzie nu bate un 4.6 cu 200), apoi id-ul — ca a doua rulare să aleagă aceiași șase."""
    rating = float(product.get("rating") or 0)
    reviews = int(product.get("review_count") or 0)
    shrunk = (reviews * rating + 30 * 4.0) / (reviews + 30)
    return (-shrunk, str(product.get("id")))


def build_substitutes(products: list[dict[str, Any]], report: BuildReport) -> None:
    """`substitute` — aceeași categorie, ≥1 nevoie comună, preț în bandă. Prioritate: epuizatele.

    Ancorele fără nevoi cunoscute sunt SĂRITE, nu servite din raft (test dedicat). Asta e diferența
    dintre „rezolvă aceeași problemă, în aceeași bandă de preț" și „altă cremă de 90 de lei"."""
    by_category: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for product in products:
        by_category[str(product.get("category_id") or "")].append(product)

    for anchor in products:
        anchor_needs = _needs(anchor)
        if not anchor_needs:
            report.skipped_no_needs += 1
            continue
        anchor_price = _price(anchor)
        candidates: list[tuple[tuple, dict[str, Any], list[str], float | None]] = []
        for other in by_category[str(anchor.get("category_id") or "")]:
            if other["id"] == anchor["id"] or not _sellable(other):
                continue
            shared = sorted(anchor_needs & _needs(other))
            if not shared:
                continue
            ok, delta = _in_band(anchor_price, _price(other))
            if not ok:
                continue
            candidates.append((_rank(other), other, shared, delta))
        candidates.sort(key=lambda c: c[0])
        for position, (_, other, shared, delta) in enumerate(candidates[:MAX_EDGES_PER_ANCHOR]):
            report.add(
                Edge(
                    product_id=str(anchor["id"]),
                    related_id=str(other["id"]),
                    kind="substitute",
                    position=position,
                    rule_id=RULE_SUBSTITUTE,
                    reason={
                        "shared_needs": shared,
                        "same_category": True,
                        "price_delta_pct": delta,
                    },
                )
            )


def build_complements(products: list[dict[str, Any]], report: BuildReport) -> None:
    """`complement` — categorie DIFERITĂ, nevoie comună. Brandul singur nu e complementaritate:
    e vecinătate de raft, și e exact ce face azi heuristica veche în lipsa nevoilor."""
    by_need: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for product in products:
        for need in _needs(product):
            by_need[need].append(product)

    for anchor in products:
        anchor_needs = _needs(anchor)
        if not anchor_needs:
            continue
        anchor_category = str(anchor.get("category_id") or "")
        seen: dict[str, tuple[tuple, dict[str, Any], list[str]]] = {}
        for need in anchor_needs:
            for other in by_need[need]:
                other_id = str(other["id"])
                if other_id == str(anchor["id"]) or not _sellable(other):
                    continue
                if str(other.get("category_id") or "") == anchor_category:
                    continue  # aceeași categorie = substitut, nu complement
                shared = sorted(anchor_needs & _needs(other))
                if other_id not in seen:
                    seen[other_id] = (_rank(other), other, shared)
        for position, (_, other, shared) in enumerate(
            sorted(seen.values(), key=lambda c: c[0])[:MAX_EDGES_PER_ANCHOR]
        ):
            report.add(
                Edge(
                    product_id=str(anchor["id"]),
                    related_id=str(other["id"]),
                    kind="complement",
                    position=position,
                    rule_id=RULE_COMPLEMENT,
                    reason={"shared_needs": shared, "same_category": False},
                )
            )


def build_variants(products: list[dict[str, Any]], report: BuildReport) -> None:
    """`variant_of` — membrii aceluiași grup de nuanță (NX-269), legați între ei.

    Muchia asta e motivul pentru care migrarea 048 trebuia să vină ÎNAINTEA jobului: CHECK-ul de la
    027 admitea patru valori, iar un insert cu `variant_of` ar fi crăpat cu `CheckViolationError`.
    Și e motivul pentru care consumatorul a trebuit extins în același card — o muchie scrisă corect
    și necitită de nimeni e a treia formă a aceleiași greșeli."""
    by_group: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for product in products:
        attributes = product.get("attributes") or {}
        group = attributes.get("shade_group") if isinstance(attributes, dict) else None
        if isinstance(group, str) and group:
            by_group[group].append(product)

    for group, members in by_group.items():
        if len(members) < 2:
            continue
        for anchor in members:
            others = sorted(
                (m for m in members if m["id"] != anchor["id"] and _sellable(m)), key=_rank
            )
            for position, other in enumerate(others[:MAX_EDGES_PER_ANCHOR]):
                shade = (other.get("attributes") or {}).get("shade")
                report.add(
                    Edge(
                        product_id=str(anchor["id"]),
                        related_id=str(other["id"]),
                        kind="variant_of",
                        position=position,
                        rule_id=RULE_VARIANT,
                        reason={"shade_group": group, "shade": shade},
                    )
                )


def build_routine(
    products: list[dict[str, Any]],
    steps_by_product: dict[str, list[str]],
    report: BuildReport,
) -> None:
    """`routine_next` — pașii din `routine_integration`, instanțiați pe un REPREZENTANT.

    Capcana pe care o numește cardul: pașii referă TIPURI de produs („curăță", „aplică tonic"), nu
    produse anume, iar muchia e produs→produs. Deci pasul se instanțiază pe un reprezentant al
    categoriei următoare — același brand întâi, apoi rating shrunk — și motivul o SPUNE:
    `representative: true`. Fără asta, o muchie ar afirma „exact produsul ăsta urmează", ceea ce
    conținutul nu susține.

    Un pas fără niciun produs în stoc în categoria țintă LIPSEȘTE din lanț; lanțul continuă (P6)."""
    by_category: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for product in products:
        if _sellable(product):
            by_category[str(product.get("category_id") or "")].append(product)

    for anchor in products:
        targets = steps_by_product.get(str(anchor["id"])) or []
        position = 0
        for category_id in targets:
            pool = [p for p in by_category.get(category_id, []) if p["id"] != anchor["id"]]
            if not pool:
                continue  # pasul lipsește, lanțul continuă
            same_brand = [p for p in pool if p.get("brand_id") == anchor.get("brand_id")]
            chosen = sorted(same_brand or pool, key=_rank)[0]
            report.add(
                Edge(
                    product_id=str(anchor["id"]),
                    related_id=str(chosen["id"]),
                    kind="routine_next",
                    position=position,
                    rule_id=RULE_ROUTINE,
                    reason={
                        "step": position + 1,
                        "target_category": category_id,
                        # „un pas de tipul ăsta", nu „exact produsul ăsta"
                        "representative": True,
                        "same_brand": bool(same_brand),
                    },
                )
            )
            position += 1
            if position >= MAX_EDGES_PER_ANCHOR:
                break


def summarize_out_of_stock(products: list[dict[str, Any]], report: BuildReport) -> None:
    """Cele 391. Câte au primit un substitut, și CARE n-au primit — explicit, nu ascunse.

    Măsurat, cauza dominantă nu e catalogul, sunt datele: **234 din 391 de produse epuizate n-au
    NICIO nevoie derivată**, deci nu există pe ce potrivi un substitut. Dintre cele 157 care au,
    155 primesc unul. Rata reală a regulii e 98,7% pe ancorele pe care poate lucra; plafonul e
    acoperirea faptelor, adică NX-268.

    De aia cele două se numără separat. Cifra din card („381 au un candidat în ±30% preț") a fost
    măsurată FĂRĂ cerința de nevoie comună — adică măsura raftul, exact lucrul pe care tot cardul îl
    respinge ca „resemnare, nu recomandare"."""
    covered = {e.product_id for e in report.edges if e.kind == "substitute"}
    for product in products:
        if _sellable(product):
            continue
        report.out_of_stock += 1
        product_id = str(product["id"])
        if product_id in covered:
            report.out_of_stock_covered += 1
        elif not _needs(product):
            report.out_of_stock_without_needs.append(product_id)
        else:
            report.anchors_without_edges.append(product_id)


def build_all(
    products: list[dict[str, Any]],
    steps_by_product: dict[str, list[str]] | None = None,
) -> BuildReport:
    """Toată construcția, PURĂ: primește rândurile, întoarce muchiile. Fără DB, deci testabilă pe
    date inventate și determinist rerulabilă."""
    report = BuildReport()
    build_substitutes(products, report)
    build_complements(products, report)
    build_variants(products, report)
    build_routine(products, steps_by_product or {}, report)
    summarize_out_of_stock(products, report)
    return report


# --- I/O -----------------------------------------------------------------------------------------

_PRODUCTS_SQL = """
select p.id::text            as id,
       p.primary_category_id::text as category_id,
       p.brand_id::text      as brand_id,
       p.availability        as availability,
       p.rating::float8      as rating,
       p.review_count        as review_count,
       coalesce(case when p.sale_price is not null and p.sale_price < p.price
                     then p.sale_price else p.price end, 0)::float8 as price,
       p.attributes          as attributes,
       p.name                as name
  from products p
 where p.business_id = $1 and p.status = 'active'
"""

# Pașii de rutină: secțiunea `routine_integration` a produsului. Textul referă TIPURI, deci aici
# aducem doar corpul; instanțierea pe categorie se face în cod, cu motivul declarat.
_ROUTINE_SQL = """
select product_id::text as id, coalesce(body, '') as body
  from product_sections
 where business_id = $1 and kind = 'routine_integration'
"""

_DELETE_RULE = """
delete from product_relations
 where business_id = $1 and rule_id = any($2::text[])
   and (product_id, related_id, kind) not in (
       select unnest($3::uuid[]), unnest($4::uuid[]), unnest($5::text[]))
"""

_UPSERT = """
insert into product_relations
       (business_id, product_id, related_id, kind, position, source, rule_id, reason)
values ($1, $2::uuid, $3::uuid, $4, $5, $6, $7, $8::jsonb)
on conflict (business_id, product_id, related_id, kind) do update
   set position = excluded.position,
       source   = excluded.source,
       rule_id  = excluded.rule_id,
       reason   = excluded.reason
 where product_relations.position is distinct from excluded.position
    or product_relations.source   is distinct from excluded.source
    or product_relations.rule_id  is distinct from excluded.rule_id
    or product_relations.reason   is distinct from excluded.reason
"""


def _routine_target_categories(
    body: str, category_order: list[str], anchor_category: str
) -> list[str]:
    """Pașii unei rutine → categoriile ȚINTĂ, în ordine.

    Textul spune „curăță, apoi tonic, apoi tratament" în cuvintele magazinului, iar codul n-are voie
    să știe ce e un tonic (P9). Ce poate ști e ORDINEA categoriilor declarată de tenant și poziția
    ancorei în ea: pașii următori sunt categoriile de după. Numărul de pași din text dă LUNGIMEA
    lanțului, nu conținutul lui — atât se poate citi onest dintr-o listă numerotată."""
    if not body.strip() or anchor_category not in category_order:
        return []
    steps = sum(1 for line in body.splitlines() if line.strip()[:1].isdigit())
    if steps < 1:
        return []
    start = category_order.index(anchor_category) + 1
    return category_order[start : start + steps]


async def main() -> int:
    # Raportul se printează ÎNAINTE de `_write_edges`, iar consola Windows e cp1252: fără garda
    # asta, un „ă" din eticheta unui contor omoară jobul la print și `--apply` nu scrie NIMIC.
    # Eșecul e cu atât mai urât cu cât apare doar după ce toată munca a fost făcută. Aceeași gardă
    # ca în `scripts/derive_shade_finish.py` și `scripts/derive_product_attributes.py`.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser()
    ap.add_argument("--business", required=True)
    ap.add_argument("--apply", action="store_true", help="chiar scrie (fără el: doar raportează)")
    ap.add_argument(
        "--needs-from-derivation",
        action="store_true",
        help=(
            "calculează nevoile ÎN MEMORIE (NX-268) în loc să le citească din `attributes`. "
            "Măsoară ce AR produce graful după ce faptele sunt scrise, fără să scrie nimic."
        ),
    )
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    from src.db.connection import admin_conn, close_pool, get_pool

    # `admin_conn`, nu `tenant_conn` — job offline care scrie în catalog (`product_relations`),
    # iar `bot_runtime` e SELECT-only acolo prin proiectare. Motivul complet:
    # `scripts/derive_product_attributes.py`.
    try:
        pool = await get_pool()
        async with admin_conn(pool) as conn:
            rows = await conn.fetch(_PRODUCTS_SQL, args.business)
            products = []
            for row in rows:
                product = dict(row)
                attributes = product.get("attributes")
                product["attributes"] = (
                    json.loads(attributes) if isinstance(attributes, str) else (attributes or {})
                )
                products.append(product)
            routine_rows = await conn.fetch(_ROUTINE_SQL, args.business)

            if args.needs_from_derivation:
                if args.apply:
                    print(
                        "EROARE: --needs-from-derivation e doar pentru măsurare, nu scriere",
                        file=sys.stderr,
                    )
                    return 2
                await _fill_needs_in_memory(conn, args.business, products)

            # Ordinea categoriilor: rădăcinile catalogului în ordinea în care apar la tenant. E o
            # dată, nu o listă în cod — pe alt vertical, „ordinea rutinei" e altceva sau lipsește.
            category_order = [
                r["id"]
                for r in await conn.fetch(
                    "select id::text as id from categories where business_id = $1 "
                    "order by coalesce(path, slug), slug",
                    args.business,
                )
            ]
            by_anchor_category = {str(p["id"]): str(p.get("category_id") or "") for p in products}
            steps = {
                str(r["id"]): _routine_target_categories(
                    r["body"], category_order, by_anchor_category.get(str(r["id"]), "")
                )
                for r in routine_rows
            }

            report = build_all(products, steps)
            _print(report, len(products))

            out = _write_report(args, report)
            print(f"\nraport: {out}")

            if not args.apply:
                print("\n(dry-run — nu s-a scris nicio muchie; adaugă --apply)")
                return 0
            written = await _write_edges(conn, args.business, report)
            print(f"\nscris/actualizat: {written} muchii din {len(report.edges)}")
            print("rerulează: a doua trecere trebuie să raporteze 0 (idempotență)")
            return 0
    finally:
        await close_pool()


async def _fill_needs_in_memory(conn, business_id: str, products: list[dict[str, Any]]) -> None:
    """Nevoile calculate acum, cu regula NX-268, fără să atingă catalogul. Import LOCAL: jobul
    trebuie să poată rula și fără drumul de derivare, când faptele sunt deja scrise."""
    from src.catalog.derivation import build_matchers, match_keys, tokens
    from src.db.queries.businesses import load_business
    from src.domain.loader import load_domain_pack
    from src.domain.normalize import normalize

    business = await load_business(conn, business_id)
    pack = load_domain_pack(business) if business else None
    if pack is None:
        print("nu pot deriva nevoi: tenantul n-are domain pack", file=sys.stderr)
        return
    concern_values = {f.key: set(f.values or ()) for f in pack.facets}.get("concerns", set())
    matchers = build_matchers(dict(pack.concern_map), normalize=normalize)
    sections = await conn.fetch(
        "select product_id::text as id, kind, coalesce(body,'') as body "
        "from product_sections where business_id = $1",
        business_id,
    )
    positive = ("fit", "problem", "purpose", "recommendation_trigger", "summary", "questions")
    text_by_product: dict[str, list[str]] = collections.defaultdict(list)
    for row in sections:
        if row["kind"] in positive:
            text_by_product[row["id"]].append(row["body"])
    for product in products:
        body = " ".join(text_by_product.get(str(product["id"]), []))
        hits = match_keys(tokens(normalize(body)), matchers) if body else {}
        values = sorted(k for k in hits if k in concern_values)
        if values:
            product["attributes"] = {**(product["attributes"] or {}), "concerns": values}


def _print(report: BuildReport, n_products: int) -> None:
    print(f"produse active: {n_products}")
    print(f"muchii derivate: {len(report.edges)}  {dict(report.by_kind)}")
    print(f"ancore fără nevoi cunoscute (sărite la substitut): {report.skipped_no_needs}")
    if report.out_of_stock:
        rate = report.out_of_stock_covered / report.out_of_stock
        knowable = report.out_of_stock - len(report.out_of_stock_without_needs)
        print(
            f"epuizate: {report.out_of_stock} · cu substitut: "
            f"{report.out_of_stock_covered} ({rate:.1%} din total, "
            f"{report.out_of_stock_covered / knowable:.1%} din cele cu nevoi cunoscute)"
        )
        print(
            f"  fără nevoi derivate (nu se poate potrivi nimic): "
            f"{len(report.out_of_stock_without_needs)} · "
            f"cu nevoi dar fără alternativă în catalog: {len(report.anchors_without_edges)}"
        )
    chains = collections.Counter(e.product_id for e in report.edges if e.kind == "routine_next")
    print(f"lanțuri de rutină cu ≥2 pași: {sum(1 for n in chains.values() if n >= 2)}")
    anchors = len({e.product_id for e in report.edges})
    print(f"ancore cu muchii: {anchors}")


def _write_report(args, report: BuildReport):
    import pathlib

    out = pathlib.Path(
        args.out
        or pathlib.Path(__file__).resolve().parents[2]
        / "reports"
        / f"relations-{args.business[:8]}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {
                "business_id": args.business,
                "edges": len(report.edges),
                "by_kind": dict(report.by_kind),
                "out_of_stock": report.out_of_stock,
                "out_of_stock_covered": report.out_of_stock_covered,
                # Cele care N-AU alternativă sunt publicate, nu ascunse: un „nu mai avem" onest
                # e o informație, unul care ascunde că n-am căutat e o minciună.
                "out_of_stock_without_substitute": report.anchors_without_edges,
                "out_of_stock_without_needs": len(report.out_of_stock_without_needs),
                "routine_chains_2plus": sum(
                    1
                    for n in collections.Counter(
                        e.product_id for e in report.edges if e.kind == "routine_next"
                    ).values()
                    if n >= 2
                ),
                "skipped_no_needs": report.skipped_no_needs,
                "rules": {
                    "substitute": RULE_SUBSTITUTE,
                    "complement": RULE_COMPLEMENT,
                    "routine_next": RULE_ROUTINE,
                    "variant_of": RULE_VARIANT,
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return out


async def _write_edges(conn, business_id: str, report: BuildReport) -> int:
    """Scrie muchiile într-o tranzacție și ȘTERGE ce nu mai produce regula.

    Ștergerea e partea care face graful o VEDERE: fără ea, o muchie devenită falsă (produsul a
    revenit în stoc, nevoia s-a schimbat) ar rămâne acolo pentru totdeauna, iar graful ar deveni un
    depozit de afirmații vechi. Se șterge doar ce poartă regulile NOASTRE — muchiile scrise de altă
    sursă (merchant, comportamental) nu sunt ale acestui job."""
    written = 0
    keep_anchors = [e.product_id for e in report.edges]
    keep_related = [e.related_id for e in report.edges]
    keep_kinds = [e.kind for e in report.edges]
    rules = [RULE_SUBSTITUTE, RULE_COMPLEMENT, RULE_ROUTINE, RULE_VARIANT]
    async with conn.transaction():
        await conn.execute(_DELETE_RULE, business_id, rules, keep_anchors, keep_related, keep_kinds)
        for edge in report.edges:
            status = await conn.execute(
                _UPSERT,
                business_id,
                edge.product_id,
                edge.related_id,
                edge.kind,
                edge.position,
                edge.source,
                edge.rule_id,
                json.dumps(edge.reason, ensure_ascii=False),
            )
            written += int(status.split()[-1])
    return written


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
