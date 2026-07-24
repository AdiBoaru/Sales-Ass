"""NX-186 — raport de COVERAGE per business + category + facet.

Poarta care decide ce fațetă poate ajunge la enforcement (NX-188) și ce fațetă poate fi mutată în
SQL tri-state (NX-189). Măsoară, pe catalogul REAL (read-only), pentru fiecare (categorie × fațetă):

  - **denominator explicit** = produsele PUBLICATE din categoria X (nu totalul optimist);
  - **3 stări de provenance DISTINCTE:** `present` (are valoare) ≠ `valid` (validă în registru) ≠
    `verified` (claim cu `claim_provenance`+`verified_at`; structurale = verified când valid);
  - **coverage** = valid / denominator; **unknown_rate** = 1 − coverage (fracția care ar fi UNKNOWN
    sub enforcement — D7: UNKNOWN ≠ MISMATCH; unknown_rate mare = enforce hard prăbușește recall);
  - **date insuficiente** dacă denominator < `MIN_PRODUCTS` (NU coverage 100% fals pe 3 produse);
  - **value_distribution** (enum/list) → din ea NX-188 estimează MATCH (au valoarea cerută) vs
    MISMATCH (au ALTĂ valoare) vs UNKNOWN (lipsă), per valoare de query;
  - **enforce_ready** = coverage ≥ `min_coverage` (prag PER FAȚETĂ) ȘI date suficiente.

Registrul de fațete vine din DomainPack-ul tenantului; dacă nu-l are seedat, cade pe defaults-ul
verticalului `--vertical` (beauty_salon). Rulare de dev, NU CI. Read-only. `WHERE business_id = $1`.
"""

import argparse
import asyncio
import collections
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(
        encoding="utf-8"
    )  # F4: diacritice pe consola Windows fără PYTHONIOENCODING

from src.db.connection import close_pool, tenant_conn  # noqa: E402
from src.db.queries.businesses import load_business  # noqa: E402
from src.domain.facets import FacetType, extract_value, is_valid_value  # noqa: E402
from src.domain.loader import load_domain_pack  # noqa: E402
from src.domain.normalize import normalize  # noqa: E402
from src.models import BusinessConfig  # noqa: E402

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"
MIN_PRODUCTS = 5  # sub acest prag pe categorie → „date insuficiente", nu coverage fals
QRELS = ROOT / "tests" / "golden" / "retrieval_qrels_compound.json"

# Vocabularul `claim_provenance.kind` care SUSȚINE o fațetă (catalogul demo folosește `ingredient`).
# O fațetă-claim NEMAPATĂ aici NU poate fi merchant-verified (fail-safe) — semnalul de care are
# nevoie NX-188 (nu enforce-ui un claim fără proveniență ca „confirmat").
_PROVENANCE_KINDS_BY_FACET: dict[str, set[str]] = {
    "key_ingredients": {"ingredient"},
}

# Cheie qrels (NX-208) → cheie registru (unele fațete apar la singular în qrels).
_FACET_KEY_ALIASES: dict[str, str] = {"key_ingredient": "key_ingredients", "concern": "concerns"}


def _claim_verified(attributes: dict, facet, value) -> bool:
    """F1 (sound): valoarea PREZENTĂ pe produs e merchant-verified DOAR dacă o intrare
    `claim_provenance` cu `verified_at`, cu `kind` MAPAT pentru fațetă, are o `value` care chiar
    CORESPUNDE valorii produsului (D5). Proveniența pt alt ingredient NU confirmă acest produs;
    o fațetă nemapată → niciodată verified. Evită fals-pozitivul de „verified"."""
    kinds = _PROVENANCE_KINDS_BY_FACET.get(facet.key)
    if kinds is None:
        return False
    cp = attributes.get("claim_provenance")
    if not isinstance(cp, list):
        return False
    # Contractul catalogului (NX-168d): o proveniență CONFIRMATĂ are kind + value + source +
    # source_ref + verified_at. O intrare parțială (fără source/source_ref) NU e merchant-verified.
    backed = {
        normalize(str(e["value"]))
        for e in cp
        if isinstance(e, dict)
        and e.get("kind") in kinds
        and e.get("value")
        and e.get("verified_at")
        and e.get("source")
        and e.get("source_ref")
    }
    if not backed:
        return False
    if facet.value_type is FacetType.LIST and isinstance(value, list):
        # Review #247: verified DOAR dacă TOATE valorile listei sunt susținute de proveniență — o
        # proveniență parțială (un singur ingredient confirmat) NU validează toată lista.
        return bool(value) and all(normalize(str(x)) in backed for x in value)
    if isinstance(value, str):
        return normalize(value) in backed
    return False  # bool/number claim fără proveniență la nivel de valoare → nu confirmăm


def compute_coverage(facets, by_cat: dict[str, list[dict]], min_products: int) -> list[dict]:
    """PUR (fără DB): per (categorie × fațetă) → rând de coverage. `by_cat[cat]` = listă de produse
    cu `price`/`category_slug`/`stock_total`/... la nivel de rând și `attributes` deja parsat."""
    out: list[dict] = []
    for category in sorted(by_cat):
        prods = by_cat[category]
        denom = len(prods)
        for facet in facets:
            present = valid = verified = 0
            dist: collections.Counter = collections.Counter()
            for p in prods:
                v = extract_value(facet, p, p.get("attributes") or {})
                if v is not None:
                    present += 1
                if is_valid_value(facet, v):
                    valid += 1
                    if facet.provenance == "structural" or _claim_verified(
                        p.get("attributes") or {}, facet, v
                    ):
                        verified += 1
                    if facet.value_type in (FacetType.ENUM, FacetType.TEXT):
                        dist[str(v)] += 1
                    elif facet.value_type is FacetType.LIST and isinstance(v, list):
                        for item in v:
                            dist[str(item)] += 1
            coverage = round(valid / denom, 3) if denom else 0.0
            insufficient = denom < min_products
            out.append(
                {
                    "category": category,
                    "facet": facet.key,
                    "denominator": denom,
                    "present": present,
                    "valid": valid,
                    "verified": verified,
                    "coverage": coverage,
                    "unknown_rate": round(1 - coverage, 3),
                    "insufficient_data": insufficient,
                    "enforce_ready": (not insufficient) and coverage >= facet.min_coverage,
                    "value_distribution": dict(dist.most_common(12)) or None,
                }
            )
    return out


def evaluate_constraint(facet, op: str, value, product_value) -> str:
    """F3 — tri-state de MĂSURARE (previziune pe query-uri reale): MATCH | MISMATCH | UNKNOWN (D7).
    Valoare lipsă/nevalidă = UNKNOWN, NU MISMATCH. NU e Match Gate-ul de runtime (acela e NX-187, cu
    MatchSet disjunct + precedență); aici e doar evaluatorul pt distribuția pe qrels."""
    if product_value is None or not is_valid_value(facet, product_value):
        return "UNKNOWN"
    if facet.value_type is FacetType.NUMBER:
        try:
            pv, val = float(product_value), float(value)
        except (TypeError, ValueError):
            return "UNKNOWN"
        ok = {"lte": pv <= val, "gte": pv >= val, "eq": pv == val}.get(op, pv == val)
        return "MATCH" if ok else "MISMATCH"
    if facet.value_type is FacetType.BOOL:
        return "MATCH" if bool(product_value) == bool(value) else "MISMATCH"
    if facet.value_type is FacetType.LIST and isinstance(product_value, list):
        vals = {normalize(str(x)) for x in product_value}
        return "MATCH" if normalize(str(value)) in vals else "MISMATCH"
    return "MATCH" if normalize(str(product_value)) == normalize(str(value)) else "MISMATCH"


def query_match_distribution(facets, queries: list[dict], all_products: list[dict]) -> dict:
    """F3 — distribuția MATCH/MISMATCH/UNKNOWN a constrângerilor REALE (qrels) peste produsele din
    scope-ul query-ului (categoria lui, altfel tot catalogul). PUR. Input NX-188: un UNKNOWN mare =
    fațeta nu e gata de enforcement hard pe query-urile reale."""
    by_key = {f.key: f for f in facets}
    by_cat: dict[str, list[dict]] = collections.defaultdict(list)
    for p in all_products:
        by_cat[p.get("category_slug")].append(p)
    per_facet: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    n = 0
    for q in queries:
        cat = q.get("category")
        scope = by_cat.get(cat, []) if cat else all_products
        for hc in q.get("hard_constraints", []):
            fkey = _FACET_KEY_ALIASES.get(hc.get("facet"), hc.get("facet"))
            facet = by_key.get(fkey)
            if facet is None:  # fațete non-registru (ex. compare_set_size) → skip
                continue
            n += 1
            for p in scope:
                pv = extract_value(facet, p, p.get("attributes") or {})
                per_facet[fkey][evaluate_constraint(facet, hc.get("op"), hc.get("value"), pv)] += 1
    return {
        "constraints_evaluated": n,
        "per_facet": {k: dict(v) for k, v in sorted(per_facet.items())},
    }


async def _facets_for(conn, business_id: str, vertical: str):
    """Registru de fațete: din pack-ul tenantului dacă e seedat, altfel defaults-ul verticalului."""
    business = await load_business(conn, business_id)
    if business and business.domain_pack and business.domain_pack.facets:
        return business.domain_pack.facets, "tenant_pack"
    fallback = load_domain_pack(
        BusinessConfig(id=business_id, slug="x", name="x", vertical=vertical)
    )
    return (fallback.facets if fallback else ()), f"defaults:{vertical}"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--business", default=DEMO_BIZ)
    ap.add_argument(
        "--vertical", default="beauty_salon", help="fallback pt registru dacă tenantul nu-l are"
    )
    ap.add_argument("--date", required=True, help="AAAA-LL-ZZ (stamp determinist — fără Date.now)")
    args = ap.parse_args()

    async with tenant_conn(args.business) as conn:
        facets, registry_source = await _facets_for(conn, args.business, args.vertical)
        if not facets:
            raise SystemExit(
                "Niciun registru de fațete (nici tenant, nici defaults) — nimic de măsurat."
            )
        rows = await conn.fetch(
            """
            select p.id::text as id, p.price::float8 as price, p.sale_price::float8 as sale_price,
                   p.rating::float8 as rating, p.stock_total, p.attributes,
                   c.slug as category_slug
            from products p
            left join categories c on c.id = p.primary_category_id
            where p.business_id = $1 and p.status = 'active' and p.content_status = 'published'
            """,
            args.business,
        )

    products: list[dict] = []
    by_cat: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        prod = dict(r)
        attrs = prod["attributes"]
        prod["attributes"] = json.loads(attrs) if isinstance(attrs, str) else (attrs or {})
        products.append(prod)
        by_cat[prod["category_slug"] or "(necategorizat)"].append(prod)

    queries = []
    if QRELS.exists():  # F3: distribuția MATCH/MISMATCH/UNKNOWN pe query-uri REALE (best-effort)
        queries = json.loads(QRELS.read_text(encoding="utf-8")).get("queries", [])

    report: dict = {
        "_meta": {
            "generated": "NX-186 facet coverage (per business+category+facet)",
            "business_id": args.business,
            "date": args.date,
            "registry_source": registry_source,
            "min_products": MIN_PRODUCTS,
            "total_published": len(rows),
            "note": "coverage=valid/denominator; unknown_rate=1-coverage (fracția UNKNOWN sub "
            "enforcement, D7); enforce_ready = coverage≥min_coverage ȘI date suficiente. 3 stări "
            "provenance: present≠valid≠verified. value_distribution → MATCH/MISMATCH per query.",
        },
        "facets": {
            f.key: {
                "value_type": f.value_type.value,
                "min_coverage": f.min_coverage,
                "provenance": f.provenance,
            }
            for f in facets
        },
        "coverage": compute_coverage(facets, by_cat, MIN_PRODUCTS),
        "query_distribution": query_match_distribution(facets, queries, products),
    }

    out = ROOT / "reports" / f"facet-coverage-{args.business[:8]}-{args.date}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    ready = sum(1 for c in report["coverage"] if c["enforce_ready"])
    qd = report["query_distribution"]
    print(f"registru: {registry_source} · {len(facets)} fațete · {len(by_cat)} categorii")
    print(f"rânduri coverage: {len(report['coverage'])} · enforce_ready: {ready}")
    print(f"query_distribution: {qd['constraints_evaluated']} constrângeri reale evaluate")
    print(f"raport: {out.relative_to(ROOT)}")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
