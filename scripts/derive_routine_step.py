"""Derivă `attributes.routine_step` din `product_type` + harta din pachet. Dry-run implicit.

De ce există fațeta, de ce cheia e compusă (`familie:pas`) și de ce promovarea pe `spf` e mărginită
la familie: vezi `src/domain/routine_steps.py`. Aici e doar jobul — citește pachetul TENANTULUI,
aplică modulul pur, raportează, și scrie DOAR cu `--apply`.

Harta NU e în acest fișier, deliberat (P9). Un dicționar „gel de curatare → curățare" scris aici ar
fi scurgere de domeniu: face sistemul mai bun pe clientul de azi și mai prost pe următorul, fără
niciun semnal. Harta trăiește în `businesses.settings.domain_pack.routine_steps`, scrisă cu
`scripts/set_domain_pack.py`.

Ordinea operațiilor contează: pachetul ÎNTÂI, derivarea după. Fără pachet în DB, jobul refuză să
ruleze (`build_spec` strict) în loc să scrie pași ghiciți.

Proprietar UNIC (lecția NX-277): singurul job care scrie `routine_step` e acesta. Scrierea e
idempotentă (`jsonb_set`, aceeași valoare) și NU șterge: un produs al cărui pas nu se poate
determina rămâne fără cheie, fiindcă absența înseamnă „nu știm".

    python scripts/derive_routine_step.py --business <uuid>            # dry-run + raport
    python scripts/derive_routine_step.py --business <uuid> --sample 15
    python scripts/derive_routine_step.py --business <uuid> --apply    # scrie
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

from src.db.connection import admin_conn, close_pool, get_pool  # noqa: E402
from src.domain.routine_steps import (  # noqa: E402
    SEP,
    RoutineStepConfigError,
    build_spec,
    resolve,
)

_SELECT = """
    select id, name, attributes, attributes->>'routine_step' as current
    from products
    where business_id = $1 and status = 'active'
    order by id
"""

_PACK = "select settings -> 'domain_pack' -> 'routine_steps' from businesses where id = $1"

# `jsonb_set` cu `create_if_missing` — scrie doar cheia, nu înlocuiește `attributes`.
#
# UN SINGUR statement pentru tot catalogul, nu unul per rând. Nu e micro-optimizare: round-trip-ul
# către Supabase eu-west-2 e ~101 ms MĂSURAT, deci 2.037 de update-uri secvențiale iau ~3,6 minute
# și țin o tranzacție admin deschisă tot timpul ăla, pe un pooler care dă 15 sesiuni. Varianta în
# lot face aceeași muncă într-un round-trip. `is distinct from` păstrează idempotența (a doua
# rulare raportează 0 rânduri), iar `updated_at` se mișcă doar pentru rândurile chiar schimbate.
_UPDATE = """
    update products p
       set attributes = jsonb_set(
               coalesce(p.attributes, '{}'::jsonb), '{routine_step}', to_jsonb(v.step), true
           ),
           updated_at = now()
      from unnest($2::uuid[], $3::text[]) as v(id, step)
     where p.business_id = $1
       and p.id = v.id
       and p.attributes->>'routine_step' is distinct from v.step
"""


def _as_dict(raw: object) -> dict:
    """`attributes` vine ca dict SAU ca text JSON, în funcție de driver și de query.

    Nu e paranoia: aceeași ambiguitate a produs deja două măsurători greșite pe cardul ăsta
    (`key_ingredients` citit ca string, deci iterat pe CARACTERE, a raportat 0 produse cu retinol
    în loc de 53). O conversie explicită costă o linie și elimină clasa.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--business", required=True, help="business_id (uuid)")
    ap.add_argument("--apply", action="store_true", help="scrie în catalog (altfel dry-run)")
    ap.add_argument("--sample", type=int, default=0, help="afișează N exemple de clasificare")
    args = ap.parse_args()

    # `admin_conn`, nu `tenant_conn`: job OFFLINE care scrie în catalog, iar `bot_runtime` are
    # SELECT-only pe `products` prin proiectare. RLS e bypass-at, deci izolarea cade integral pe
    # `where business_id = $1`, prezent în FIECARE statement al jobului. Ca `derive_product_type`.
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        raw_spec = await conn.fetchval(_PACK, args.business)
        raw_spec = json.loads(raw_spec) if isinstance(raw_spec, str) else raw_spec
        if not raw_spec:
            print(
                "EROARE: tenantul n-are `domain_pack.routine_steps`.\n"
                "Scrie ÎNTÂI pachetul:\n"
                "  python scripts/set_domain_pack.py --business <slug> "
                "--pack db/seed/domain_pack_<tenant>.json --apply",
                file=sys.stderr,
            )
            return 2
        try:
            # STRICT aici, tolerant la citirea de runtime (`load_routine_steps`) — vezi modulul.
            spec = build_spec(raw_spec)
        except RoutineStepConfigError as e:
            print(f"EROARE: hartă de pași invalidă, nu scriu nimic: {e}", file=sys.stderr)
            return 2

        rows = await conn.fetch(_SELECT, args.business)
        if not rows:
            print("0 produse active — nimic de derivat.")
            return 2

        assigned = {
            r["id"]: resolve(_as_dict(r["attributes"]), spec, name=r["name"] or "") for r in rows
        }
        typed = sum(1 for r in rows if _as_dict(r["attributes"]).get("product_type"))
        n_step = sum(1 for v in assigned.values() if v)
        n_change = sum(1 for r in rows if assigned[r["id"]] and assigned[r["id"]] != r["current"])

        print(f"produse active     : {len(rows)}")
        print(f"cu `product_type`  : {typed} ({100 * typed / len(rows):.1f}%)")
        print(
            f"pas determinat     : {n_step} ({100 * n_step / len(rows):.1f}% din catalog, "
            f"{100 * n_step / max(typed, 1):.1f}% din cele cu tip)"
        )
        print(f"rânduri de actualizat: {n_change}\n")

        counts = Counter(v for v in assigned.values() if v)
        print("--- pași, în ordinea din pachet ---")
        for fam, steps in spec.families.items():
            total = sum(counts.get(f"{fam}:{s}", 0) for s in steps)
            print(f"  {fam}  ({total} produse)")
            for i, s in enumerate(steps, 1):
                print(f"    {i}. {s:<14} {counts.get(f'{fam}:{s}', 0):>5}")

        # Trei categorii distincte de „fără pas", și distincția e tot ce contează în raport:
        # `not_a_step` + `ambiguous` sunt DECIZII declarate, restul sunt SCĂPĂRI de reparat.
        declared = spec.not_a_step | spec.ambiguous
        types_in_catalog = Counter(
            t for r in rows if (t := _as_dict(r["attributes"]).get("product_type"))
        )
        for name, bucket in (("not_a_step", spec.not_a_step), ("ambiguous", spec.ambiguous)):
            present = {t: types_in_catalog[t] for t in sorted(bucket) if types_in_catalog[t]}
            if present:
                total = sum(present.values())
                print(f"\n  `{name}` (decizie declarată): {total} produse, fără pas")
                for t, n in sorted(present.items(), key=lambda kv: -kv[1]):
                    print(f"      {n:>5}  {t}")

        missing = Counter(
            {t: n for t, n in types_in_catalog.items() if t not in spec.by_product_type}
        )
        for t in declared:
            missing.pop(t, None)
        if missing:
            print(f"\n  ! {len(missing)} tipuri NEmapate și NEdeclarate (scăpări, nu decizii):")
            for t, n in missing.most_common(10):
                print(f"      {n:>5}  {t}")
        else:
            print("\n  fiecare tip din catalog e fie mapat, fie o decizie declarată.")

        if args.sample:
            print(f"\n--- {args.sample} exemple ---")
            for r in rows[: args.sample]:
                a = _as_dict(r["attributes"])
                spf = f" spf={a['spf']}" if a.get("spf") else ""
                print(
                    f"  {str(assigned[r['id']]):<18} ← {str(a.get('product_type')):<26}{spf}"
                    f"  {r['name'][:44]}"
                )

        if not args.apply:
            print("\nDRY-RUN. Nimic scris. Adaugă --apply ca să scrii.")
            return 0

        ids = [r["id"] for r in rows if assigned[r["id"]] is not None]
        steps = [assigned[i] for i in ids]
        t0 = time.perf_counter()
        res = await conn.execute(_UPDATE, args.business, ids, steps)
        written = int(res.rsplit(" ", 1)[-1] or 0)
        print(f"\nSCRIS: {written} rânduri în {(time.perf_counter() - t0) * 1000:.0f} ms.")

        # Verificare pe DB, nu pe intenție. Inclusiv invariantul care contează cel mai mult:
        # `fata:protectie` trebuie să fie EXACT produsele cu `spf` din familia `fata`.
        check = await conn.fetch(
            """select attributes->>'routine_step' v, count(*) n from products
               where business_id = $1 and status = 'active' and attributes ? 'routine_step'
               group by 1 order by n desc""",
            args.business,
        )
        print("verificat în DB:", json.dumps([(c["v"], c["n"]) for c in check], ensure_ascii=False))

        # Invariantul promovărilor, verificat pe DB pentru FIECARE regulă declarată — nu doar
        # pentru cea de azi. Valorile se construiesc din `spec`, niciodată scrise aici: un
        # `'fata:protectie'` literal ar fi vocabular de domeniu în cod (poarta NX-264 l-a și
        # prins), și ar tăcea la a doua promovare.
        for promo in spec.promotions:
            value = f"{promo.within_family}{SEP}{promo.to_step}"
            leaked = await conn.fetchval(
                """select count(*) from products
                   where business_id = $1 and status = 'active'
                     and attributes->>'routine_step' = $2
                     and attributes -> $3 is null""",
                args.business,
                value,
                promo.when_attribute,
            )
            print(f"  {value} fără `{promo.when_attribute}` (trebuie 0): {leaked}")
    return 0


async def _run() -> int:
    try:
        return await main()
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
