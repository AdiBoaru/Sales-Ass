"""Derivă lista de termeni de DOMENIU ai unui tenant — datele pe care codul n-are voie să le știe.

Poarta din `tests/test_domain_leak.py` compară codul cu lista asta. Lista NU se scrie de mână: ar fi
exact greșeala pe care o previne (o listă de cuvinte scrisă de un om, care driftează față de
catalog și pe care nimeni n-o mai verifică). Se derivă din două surse, amândouă date:

1. **Vocabularele declarate** — `concern_map`, valorile fațetelor și expandările din toate pachetele
   de domeniu (`src/domain/defaults/*.json` + pachetele seedate). Astea sunt limbajul clientului,
   scris explicit ca dată.
2. **Catalogul însuși** — tokenii de conținut din numele produselor și ale categoriilor, trecuți
   prin `content_terms` (deci fără cuvintele goale ale locale-i). Aici apar „crema", „ser", „ulei",
   „masca": cuvinte pe care nimeni nu le-a declarat nicăieri, dar care descriu exact verticalul.

A doua sursă e cea care contează. Fără ea, poarta ar prinde doar ce e deja în pachete și ar rata fix
clasa de scurgere care s-a întâmplat: `TEXTURE_TERMS = {"crema": ..., "ser": ...}` într-un script.

**De ce doar NUMELE, nu și proza.** S-a încercat și cu `composition` + `key_ingredients`: lista a
crescut cu verbe și cuvinte de legătură („reduce", „calmeaza", „imbunatateste", „fara"), fiindcă
proza descrie ACȚIUNI, nu obiecte. Numele și categoria sunt câmpurile în care oamenii NUMESC
lucruri, deci acolo tokenii sunt substantive de vertical.

Limita, spusă pe față: „parfum" nu apare în niciun nume de produs, deci NU intră în listă, deși e
vocabular de domeniu curat. Poarta nu urmărește recall complet — urmărește ZERO fals pozitive, ca să
rămână crezută. O poartă care țipă des e o poartă pe care nimeni n-o mai citește.

Ieșirea e un artefact versionat, cu provenance și frecvențe, comis în repo. Regenerabil:

    python scripts/build_domain_terms.py --business <uuid> --out tests/domain_terms.json

Read-only pe DB. Nu depinde de nicio scriere și nu atinge calea de rulare.
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.catalog.query_terms import content_terms  # noqa: E402
from src.db.connection import close_pool, tenant_conn  # noqa: E402
from src.domain.normalize import normalize  # noqa: E402

# BANDĂ, nu prag. Măsurat pe catalogul SOLE, un prag simplu de frecvență minimă produce o listă
# inutilizabilă în ambele capete:
#
#   * sus — „contribuie" (83%), „pielii" (51%), „fata" (52%), „formulata" (44%) sunt BOILERPLATE
#     din șablonul de nume, nu vocabular. Un termen adevărat la tot catalogul nu descrie verticalul
#     și nu poate fi semnal de scurgere.
#   * jos — „from" (1,1%), „body" (2,0%), „control" (0,9%), „patch" (1,4%) sunt cuvinte de limbă
#     GAZDĂ care nimeresc întâmplător în câteva nume de produs. Ca termeni de poartă, ar produce o
#     eroare la fiecare `select ... from ...` din repo, iar o poartă care țipă mereu e ignorată.
#
# Banda [3%, 25%] păstrează exact stratul discriminant: „crema" 16,5%, „acid" 22,7%, „extract" 21%,
# „niacinamida" 10,3%, „gel" 5,8%. Sunt procente din catalog, deci se mută cu el, nu constante
# calibrate pe 2.758 de produse.
MIN_PRODUCT_FREQ_RATIO = 0.03
MAX_PRODUCT_FREQ_RATIO = 0.25

# Tokenii de sub lungimea asta sunt zgomot ca semnal de scurgere: „ml", „gr", „pa" apar peste tot și
# nu spun nimic despre vertical. Cei declarați în pachete intră oricum, indiferent de lungime.
MIN_TOKEN_LEN = 4

# Câți tokeni de catalog păstrăm, ordonați după frecvență. Coada lungă e alcătuită din nume de
# produs, care nu sunt vocabular de domeniu.
MAX_CATALOG_TERMS = 400


def _pack_terms() -> dict[str, list[str]]:
    """Termenii DECLARAȚI, din toate pachetele găsite în repo, cu fișierul din care vin."""
    out: dict[str, set[str]] = collections.defaultdict(set)
    files = sorted((ROOT / "src" / "domain" / "defaults").glob("*.json"))
    files += sorted((ROOT / "db" / "seed").glob("domain_pack_*.json"))
    for path in files:
        try:
            pack = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rel = str(path.relative_to(ROOT)).replace("\\", "/")
        for phrase, canonical in (pack.get("concern_map") or {}).items():
            out[normalize(str(phrase))].add(rel)
            out[normalize(str(canonical))].add(rel)
        for facet in pack.get("facets") or []:
            for value in facet.get("values") or []:
                out[normalize(str(value))].add(rel)
        for key, values in (pack.get("query_expansions") or {}).items():
            out[normalize(str(key))].add(rel)
            for value in values or []:
                out[normalize(str(value))].add(rel)
    return {term: sorted(src) for term, src in out.items() if term}


async def _catalog_terms(business_id: str, locale: str) -> tuple[collections.Counter, int]:
    """Tokenii de conținut ai catalogului, numărați pe PRODUSE distincte, plus câte produse sunt.

    Numărăm produse, nu apariții: un token repetat de zece ori în același nume lung nu e mai
    reprezentativ pentru vertical decât unul apărut o dată. Denominatorul iese explicit, ca banda
    să fie un procent din catalog, nu un număr calibrat pe catalogul de azi.

    Sursele sunt câmpurile în care oamenii NUMESC lucrurile — numele produsului și al categoriei.
    Nu proza: acolo tokenii cei mai frecvenți sunt verbe de șablon („contribuie la menținerea") și
    cuvinte de legătură, care nu spun nimic despre vertical."""
    counter: collections.Counter = collections.Counter()
    async with tenant_conn(business_id) as conn:
        rows = await conn.fetch(
            """select p.id::text as id, p.name, coalesce(cat.name, '') as category
                 from products p
                 left join categories cat on cat.id = p.primary_category_id
                where p.business_id = $1 and p.status = 'active'""",
            business_id,
        )
    for row in rows:
        seen = set(content_terms(f"{row['name']} {row['category']}", locale))
        counter.update(t for t in seen if len(t) >= MIN_TOKEN_LEN and not t.isdigit())
    return counter, len(rows)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--business", required=True)
    ap.add_argument("--locale", default="ro")
    ap.add_argument("--out", default="tests/domain_terms.json")
    args = ap.parse_args()

    try:
        declared = _pack_terms()
        catalog, n_products = await _catalog_terms(args.business, args.locale)
    finally:
        await close_pool()

    low = max(3, int(n_products * MIN_PRODUCT_FREQ_RATIO))
    high = int(n_products * MAX_PRODUCT_FREQ_RATIO)
    frequent = [(term, count) for term, count in catalog.most_common() if low <= count <= high][
        :MAX_CATALOG_TERMS
    ]

    payload = {
        "_provenance": {
            "business_id": args.business,
            "locale": args.locale,
            "declared_from": "src/domain/defaults/*.json + db/seed/domain_pack_*.json",
            "catalog_from": (
                "products.name + categories.name (status='active'), prin content_terms"
            ),
            "products_scanned": n_products,
            "band_products": [low, high],
            "band_ratio": [MIN_PRODUCT_FREQ_RATIO, MAX_PRODUCT_FREQ_RATIO],
            "min_token_len": MIN_TOKEN_LEN,
            "max_catalog_terms": MAX_CATALOG_TERMS,
            "regenerate": (
                f"python scripts/build_domain_terms.py --business {args.business} --out {args.out}"
            ),
        },
        "declared": {term: sources for term, sources in sorted(declared.items())},
        "catalog": dict(sorted(frequent, key=lambda kv: (-kv[1], kv[0]))),
    }

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"declarați: {len(declared)} · din catalog: {len(frequent)} "
        f"(bandă {low}-{high} din {n_products} produse)"
    )
    print(f"top catalog: {[t for t, _ in frequent[:15]]}")
    print(f"scris: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
