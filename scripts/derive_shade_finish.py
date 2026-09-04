"""NX-269 — derivă nuanța și finish-ul pentru produsele unde ele SUNT decizia de cumpărare.

Măsurat pe baza live: `machiaj` are 681 de produse (un sfert din catalog), toate cele 2.755 de
variante poartă eticheta „Standard", iar `shade` / `color_hex` / `undertone` sunt goale peste tot.
Un magazin de beauty în care nu poți cere „ruj roșu mat" nu e un magazin de beauty.

De ce nu s-a văzut mai devreme: la derivarea NX-268, categoria `Buze` a ieșit cu 92% acoperire pe
nevoi. Arăta excelent. Nevoile erau „hidratare" și „luminozitate" — corecte, și complet irelevante:
nimeni nu-și alege rujul după hidratare. **Acoperirea poate fi mare și fațeta complet greșită.**

Regula de derivare a nuanței e în `src/catalog/shade.py` (pură, testabilă fără DB) și se descoperă
COMPARATIV, nu dintr-o listă de culori — vezi acolo de ce o listă ar fi și scurgere de domeniu, și
greșită. Finish-ul se derivă ca fațetă obișnuită, cu valorile din pachet (ratificate pe măsurătoare
în `_finish_note`), prin aceeași cale ca NX-268.

    python scripts/derive_shade_finish.py --business <uuid>            # dry-run + raport
    python scripts/derive_shade_finish.py --business <uuid> --sample 20
    python scripts/derive_shade_finish.py --business <uuid> --apply    # scrie

Dry-run implicit. Scrierea e idempotentă prin aceeași cheie ca la NX-268.
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

from src.catalog.derivation import signal_name  # noqa: E402
from src.catalog.shade import derive_shades, shade_appears_in_name, tokenize  # noqa: E402
from src.db.connection import admin_conn, close_pool, get_pool  # noqa: E402
from src.db.queries.businesses import load_business  # noqa: E402
from src.domain.loader import load_domain_pack  # noqa: E402
from src.domain.normalize import normalize  # noqa: E402

RULE_SHADE = "name.variable_suffix.v1"
RULE_FINISH = "pack.name_values.finish.v1"
BATCH = 200

# Rădăcinile unde nuanța ÎNSEAMNĂ ceva. E o listă de rădăcini de CATALOG, nu de vocabular: cheile
# vin din arborele de categorii al tenantului. Un vertical de anvelope ar pune aici altceva, iar
# codul n-ar afla niciodată ce e o culoare.
DEFAULT_ROOTS = ("machiaj",)

_TREE_SQL = """
with recursive tree as (
  select id, slug, parent_id, slug as root from categories
   where business_id = $1 and parent_id is null
  union all
  select c.id, c.slug, c.parent_id, t.root
    from categories c join tree t on c.parent_id = t.id
   where c.business_id = $1)
select p.id::text as id, p.name, coalesce(b.name, '') as brand, t.root
  from products p
  join tree t on t.id = p.primary_category_id
  left join brands b on b.id = p.brand_id
 where p.business_id = $1 and p.status = 'active'
"""

_UPSERT_SIGNAL = """
insert into product_derived_signals
       (business_id, product_id, signal, derived_from, rule_id, locale)
values ($1, $2::uuid, $3, $4::text[], $5, $6)
on conflict (business_id, product_id, signal, rule_id, locale) do update
   set derived_from = excluded.derived_from
 where product_derived_signals.derived_from is distinct from excluded.derived_from
"""

_PROJECT_ATTRS = """
update products
   set attributes = (coalesce(attributes, '{}'::jsonb) - $3::text[]) || $4::jsonb
 where business_id = $1 and id = $2::uuid
   and attributes is distinct from ((coalesce(attributes, '{}'::jsonb) - $3::text[]) || $4::jsonb)
"""

_SHADE_KEYS = ["shade", "shade_code", "shade_group", "finish"]


def _finish_values(business) -> dict[str, list[str]]:
    """Valorile canonice de `finish` + formele lor, din PACHET. Gol → finish-ul nu se derivă.

    Fațeta trebuie să declare `derived_from: "name"`, nu `source: "name"`. Cele două spun lucruri
    diferite: `source` e de unde se CITEȘTE valoarea la interogare (`attributes->finish`),
    `derived_from` e de unde a fost EXTRASĂ. Confundarea lor era un bug tăcut — `FacetSource` n-are
    valoarea `name`, deci loader-ul respingea fațeta fail-closed și nimeni n-ar fi observat că nu
    există pentru `facet_coverage`, poarta de relevanță sau comparație.

    Extragerea se face din NUME, nu din proză, din același motiv ca la NX-268: în proză „mat" apare
    și în „se aplică peste fondul de ten mat", care descrie ALT produs."""
    raw = ((business.settings or {}).get("domain_pack") or {}).get("facets") or []
    for spec in raw:
        if isinstance(spec, dict) and spec.get("key") == "finish":
            if spec.get("derived_from") != "name":
                continue
            aliases = spec.get("aliases") or {}
            out: dict[str, list[str]] = {}
            for value in spec.get("values") or []:
                forms = [normalize(str(value))]
                forms += [normalize(a) for a, canon in aliases.items() if canon == value]
                out[str(value)] = sorted({f for f in forms if f})
            return out
    return {}


async def _write_batch(conn, business_id: str, locale: str, rows: list[tuple[str, dict]]):
    """Scrie un lot într-o tranzacție, cu SAVEPOINT per produs (ca la NX-268: un rând stricat nu
    poate anula lotul și nu poate bloca derivarea la nesfârșit pe același produs)."""
    signals = touched = 0
    skipped: list[str] = []
    async with conn.transaction():
        for product_id, attrs in rows:
            try:
                async with conn.transaction():
                    for facet, value in attrs.items():
                        status = await conn.execute(
                            _UPSERT_SIGNAL,
                            business_id,
                            product_id,
                            signal_name(facet, str(value)),
                            ["name"],
                            RULE_FINISH if facet == "finish" else RULE_SHADE,
                            locale,
                        )
                        signals += int(status.split()[-1])
                    status = await conn.execute(
                        _PROJECT_ATTRS, business_id, product_id, _SHADE_KEYS, json.dumps(attrs)
                    )
                    touched += int(status.split()[-1])
            except Exception as e:  # noqa: BLE001 — un produs stricat nu oprește lotul
                skipped.append(product_id)
                print(f"  ! sărit {product_id}: {type(e).__name__}", file=sys.stderr)
    return signals, touched, skipped


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--business", required=True)
    ap.add_argument("--roots", default=",".join(DEFAULT_ROOTS), help="rădăcinile de categorie")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--out", default=None)
    ap.add_argument("--apply", action="store_true", help="chiar scrie (fără el: doar raportează)")
    ap.add_argument(
        "--pack",
        help=(
            "măsoară cu pachetul dintr-un FIȘIER, nu cu cel din DB. Există pentru că fațetele noi "
            "(`shade`, `finish`) trebuie verificate ÎNAINTE de a ajunge în `businesses.settings` "
            "— altfel singura cale de a afla dacă valorile de finish potrivesc ceva ar fi să le "
            "scrii mai întâi în producție. Read-only; refuzat cu --apply, ca să nu se scrie în "
            "catalog fapte derivate cu un pachet care nu e cel al tenantului."
        ),
    )
    args = ap.parse_args()
    roots = frozenset(r.strip() for r in args.roots.split(",") if r.strip())

    # `admin_conn`, nu `tenant_conn` — job offline care scrie în catalog, iar `bot_runtime` e
    # SELECT-only acolo prin proiectare. Motivul complet: `scripts/derive_product_attributes.py`.
    try:
        pool = await get_pool()
        async with admin_conn(pool) as conn:
            business = await load_business(conn, args.business)
            if business is None:
                print("business inexistent", file=sys.stderr)
                return 2
            pack = load_domain_pack(business)
            if pack is None:
                print("tenantul n-are domain pack", file=sys.stderr)
                return 2
            if args.pack:
                if args.apply:
                    print("EROARE: --pack e doar pentru măsurare, nu scriere", file=sys.stderr)
                    return 2
                business.settings = {
                    **(business.settings or {}),
                    "domain_pack": json.loads(pathlib.Path(args.pack).read_text(encoding="utf-8")),
                }
                pack = load_domain_pack(business)
                print(f"pachet din fișier: {args.pack} (măsurare, nimic nu se scrie)")
            rows = await conn.fetch(_TREE_SQL, args.business)

            products = [dict(r) for r in rows]
            in_roots = [p for p in products if p["root"] in roots]
            unit_aliases = dict(getattr(pack.units, "alias_facet", {}) or {})
            shades = derive_shades(in_roots, unit_aliases=unit_aliases, roots=roots)

            finish_values = _finish_values(business)
            finishes: dict[str, str] = {}
            for product in in_roots:
                name_norm = normalize(product["name"])
                words = set(tokenize(name_norm))
                for value, forms in finish_values.items():
                    if any(f in words for f in forms):
                        finishes[product["id"]] = value
                        break

            # --- invariantul: nicio nuanță inventată ---------------------------------------
            by_id = {p["id"]: p for p in products}
            violations = [
                pid
                for pid, a in shades.items()
                if not shade_appears_in_name(a.shade, by_id[pid]["name"])
            ]

            total_root = len(in_roots)
            with_digit = sum(
                1 for p in in_roots if any(any(c.isdigit() for c in t) for t in tokenize(p["name"]))
            )
            digit_with_shade = sum(
                1
                for p in in_roots
                if p["id"] in shades
                and any(any(c.isdigit() for c in t) for t in tokenize(p["name"]))
            )
            groups = collections.Counter(a.group for a in shades.values())

            print(f"rădăcini: {sorted(roots)} · produse: {total_root}")
            print(
                f"nuanțe derivate: {len(shades)} ({len(shades) / total_root:.1%}) "
                f"în {len(groups)} linii"
            )
            # Proxy-ul declarat: un produs cu cifră în nume are aproape sigur un cod de nuanță
            # („116 Candid"). Nu e o listă de culori, e o proprietate de FORMĂ a numelui.
            if with_digit:
                print(
                    f"  dintre cele {with_digit} cu cifră în nume: "
                    f"{digit_with_shade} ({digit_with_shade / with_digit:.1%})"
                )
            else:
                print("  (niciun produs cu cifră în nume)")
            print(f"nuanțe care NU apar în nume: {len(violations)} (trebuie 0)")
            if finish_values:
                dist = collections.Counter(finishes.values())
                top = dist.most_common(1)[0][1] / total_root if dist else 0
                print(
                    f"finish derivat: {len(finishes)} ({len(finishes) / total_root:.1%}) · "
                    f"valori {dict(dist)} · cea mai frecventă la {top:.1%} din rădăcină"
                )
            else:
                print("finish: pachetul nu declară `finish` cu `derived_from: name` → nu se derivă")

            if args.sample:
                print("\n--- exemple ---")
                for pid, a in list(shades.items())[: args.sample]:
                    print(f"  {by_id[pid]['name'][:80]}")
                    print(f"     trunchi «{a.trunk}» → nuanță «{a.shade}» cod={a.shade_code}")

            out = pathlib.Path(
                args.out or ROOT / "reports" / f"shade-finish-{args.business[:8]}.json"
            )
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps(
                    {
                        "business_id": args.business,
                        "roots": sorted(roots),
                        "products_in_roots": total_root,
                        "shades": len(shades),
                        "shade_groups": len(groups),
                        "largest_group": groups.most_common(1)[0][1] if groups else 0,
                        "with_digit_in_name": with_digit,
                        "with_digit_and_shade": digit_with_shade,
                        "violations": violations,
                        "finish": collections.Counter(finishes.values()),
                        "rules": {"shade": RULE_SHADE, "finish": RULE_FINISH},
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"\nraport: {out}")

            if violations:
                print("EȘEC: nuanțe care nu apar în nume — nu se scrie nimic", file=sys.stderr)
                return 1
            if not args.apply:
                print("\n(dry-run — nu s-a scris nimic; adaugă --apply)")
                return 0

            to_write: list[tuple[str, dict]] = []
            for pid in set(shades) | set(finishes):
                attrs: dict[str, str] = {}
                if a := shades.get(pid):
                    attrs["shade"] = a.shade
                    attrs["shade_group"] = a.group
                    if a.shade_code:
                        attrs["shade_code"] = a.shade_code
                if value := finishes.get(pid):
                    attrs["finish"] = value
                to_write.append((pid, attrs))

            written = touched = 0
            all_skipped: list[str] = []
            for i in range(0, len(to_write), BATCH):
                s, t, skipped = await _write_batch(
                    conn, args.business, business.default_locale or "ro", to_write[i : i + BATCH]
                )
                written += s
                touched += t
                all_skipped.extend(skipped)
            print(f"\nscris: {written} semnale, {touched} produse cu `attributes` schimbat")
            if all_skipped:
                print(f"sărite: {len(all_skipped)}")
            print("rerulează: a doua trecere trebuie să raporteze 0 și 0 (idempotență)")
            return 0
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
