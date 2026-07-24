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

from src.db.connection import close_pool, tenant_conn  # noqa: E402
from src.db.queries.businesses import load_business  # noqa: E402
from src.domain.facets import FacetType, extract_value, is_valid_value  # noqa: E402
from src.domain.loader import load_domain_pack  # noqa: E402
from src.models import BusinessConfig  # noqa: E402

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"
MIN_PRODUCTS = 5  # sub acest prag pe categorie → „date insuficiente", nu coverage fals


# Vocabularul `claim_provenance.kind` care SUSȚINE o fațetă (catalogul demo folosește `ingredient`).
# Fațetele-claim neapărute aici rămân verified=0 CORECT (afirmate, nu merchant-verified) — exact
# semnalul de care are nevoie NX-188 (nu enforce-ui un claim fără proveniență ca „confirmat").
_PROVENANCE_KINDS_BY_FACET: dict[str, set[str]] = {
    "key_ingredients": {"ingredient"},
}


def _has_claim_provenance(attributes: dict, facet_key: str, source_key: str) -> bool:
    """True dacă există o intrare `claim_provenance` cu `verified_at` al cărei `kind` susține fațeta
    (D5). Conservator: fără verified_at → nu e verified; fără vocabular de proveniență → 0."""
    cp = attributes.get("claim_provenance")
    if not isinstance(cp, list):
        return False
    kinds = _PROVENANCE_KINDS_BY_FACET.get(facet_key, {facet_key, source_key})
    for entry in cp:
        if not isinstance(entry, dict) or not entry.get("verified_at"):
            continue
        if entry.get("kind") in kinds or entry.get("facet") in kinds:
            return True
    return False


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
                    if facet.provenance == "structural" or _has_claim_provenance(
                        p.get("attributes") or {}, facet.key, facet.source_key
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

    by_cat: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        prod = dict(r)
        attrs = prod["attributes"]
        prod["attributes"] = json.loads(attrs) if isinstance(attrs, str) else (attrs or {})
        by_cat[prod["category_slug"] or "(necategorizat)"].append(prod)

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
    }

    out = ROOT / "reports" / f"facet-coverage-{args.business[:8]}-{args.date}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    ready = sum(1 for c in report["coverage"] if c["enforce_ready"])
    print(f"registru: {registry_source} · {len(facets)} fațete · {len(by_cat)} categorii")
    print(f"rânduri coverage: {len(report['coverage'])} · enforce_ready: {ready}")
    print(f"raport: {out.relative_to(ROOT)}")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
