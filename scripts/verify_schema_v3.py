"""Verifică FUNCȚIONAL garanțiile din `docs/schema_v3_delta.sql`, încercând să le încalce.

„Constrângerea există" nu e același lucru cu „constrângerea prinde". Un CHECK scris greșit
(paranteze, `or` în loc de `and`, o coloană comparată cu ea însăși) apare în `pg_constraint`,
trece orice inventar de schemă, și nu respinge nimic. Singura dovadă e o scriere care TREBUIE
să eșueze și chiar eșuează.

Fiecare caz rulează în tranzacție proprie, cu rollback: baza rămâne exact cum a fost.

Rulare:
    TARGET_DB_URL=postgresql://... python scripts/verify_schema_v3.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

import asyncpg

PASS, FAIL = "  ok  ", " ESUAT "

# (descriere, SQL care TREBUIE să eșueze, clasa de eroare așteptată)
MUST_REJECT: list[tuple[str, str, str]] = [
    (
        "A4 pret de cupon fara cod",
        "insert into products (business_id,name,slug,price,coupon_price) "
        "values ('{biz}','X','x-1',100,80)",
        "CheckViolationError",
    ),
    (
        "A4 cod de cupon fara pret",
        "insert into products (business_id,name,slug,price,coupon_code) "
        "values ('{biz}','X','x-2',100,'WELCOME15')",
        "CheckViolationError",
    ),
    (
        "A4 pret de cupon MAI MARE decat pretul",
        "insert into products (business_id,name,slug,price,coupon_code,coupon_price) "
        "values ('{biz}','X','x-3',100,'W',150)",
        "CheckViolationError",
    ),
    (
        "A5 temperatura minima fara maxima",
        "insert into products (business_id,name,slug,price,storage_temp_min_c) "
        "values ('{biz}','X','x-4',100,5)",
        "CheckViolationError",
    ),
    (
        "A5 interval de temperatura inversat",
        "insert into products (business_id,name,slug,price,"
        "storage_temp_min_c,storage_temp_max_c) values ('{biz}','X','x-5',100,25,5)",
        "CheckViolationError",
    ),
    (
        "A5 PAO in afara intervalului",
        "insert into products (business_id,name,slug,price,pao_months) "
        "values ('{biz}','X','x-6',100,999)",
        "CheckViolationError",
    ),
    (
        "A3 badge cu tip necunoscut",
        "insert into product_badges (business_id,product_id,label,kind) "
        "values ('{biz}','{prod}','L','ceva_nou')",
        "CheckViolationError",
    ),
    (
        "A6 rating 0 (are text bun, dar nota lipseste -> NULL, nu 0)",
        "insert into reviews (business_id,product_id,rating,body) values ('{biz}','{prod}',0,'t')",
        "CheckViolationError",
    ),
    (
        "A6 rating 6",
        "insert into reviews (business_id,product_id,rating,body) values ('{biz}','{prod}',6,'t')",
        "CheckViolationError",
    ),
    (
        "B1 sursa de sectiune necunoscuta",
        "insert into product_sections (business_id,product_id,kind,title,body,source) "
        "values ('{biz}','{prod}','description','T','B','wikipedia')",
        "CheckViolationError",
    ),
    (
        "B2 storage de imagine necunoscut",
        "insert into product_images (business_id,product_id,url,storage) "
        "values ('{biz}','{prod}','http://x','cdn_extern')",
        "CheckViolationError",
    ),
    (
        "A1 badge legat de produsul ALTUI tenant",
        "insert into product_badges (business_id,product_id,label) values ('{biz2}','{prod}','L')",
        "ForeignKeyViolationError",
    ),
    (
        "A2 imagine legata de produsul ALTUI tenant",
        "insert into product_images (business_id,product_id,url) "
        "values ('{biz2}','{prod}','http://x')",
        "ForeignKeyViolationError",
    ),
]

# (descriere, SQL care TREBUIE să reușească)
MUST_ACCEPT: list[tuple[str, str]] = [
    (
        "A6 recenzie cu rating NULL si text pastrat",
        "insert into reviews (business_id,product_id,rating,body) "
        "values ('{biz}','{prod}',null,'text bun, nota lipsa')",
    ),
    (
        "A4 cupon coerent",
        "insert into products (business_id,name,slug,price,coupon_code,coupon_price) "
        "values ('{biz}','X','x-ok',100,'WELCOME15',85)",
    ),
    (
        "A5 interval de temperatura valid",
        "insert into products (business_id,name,slug,price,"
        "storage_temp_min_c,storage_temp_max_c) values ('{biz}','X','x-ok2',100,5,25)",
    ),
    (
        "A3 badge de tip merchant_marketing (se IMPORTA, nu se arunca)",
        "insert into product_badges (business_id,product_id,label,kind) "
        "values ('{biz}','{prod}','SOLE Exclusiv','merchant_marketing')",
    ),
    (
        "B1 proza AURA se importa, etichetata",
        "insert into product_sections (business_id,product_id,kind,title,body,source,voice,"
        "source_key) values ('{biz}','{prod}','fit','T','B','aura','assistant',"
        "'Cui i se potriveste')",
    ),
]


async def main() -> int:
    dsn = os.environ.get("TARGET_DB_URL")
    if not dsn:
        sys.stderr.write("TARGET_DB_URL lipseste\n")
        return 2

    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    failures = 0
    try:
        # Fixture, într-o tranzacție care se anulează la final: nimic nu rămâne în baza.
        tx = conn.transaction()
        await tx.start()
        biz = await conn.fetchval(
            "insert into businesses (slug,name,vertical,status) "
            "values ($1,'V','ecommerce','active') returning id",
            f"verify-{uuid.uuid4().hex[:8]}",
        )
        biz2 = await conn.fetchval(
            "insert into businesses (slug,name,vertical,status) "
            "values ($1,'V2','ecommerce','active') returning id",
            f"verify-{uuid.uuid4().hex[:8]}",
        )
        prod = await conn.fetchval(
            "insert into products (business_id,name,slug,price) "
            "values ($1,'P','p-verify',10) returning id",
            biz,
        )
        fmt = {"biz": biz, "biz2": biz2, "prod": prod}

        print("=== SCRIERI CARE TREBUIE RESPINSE ===")
        for label, sql, expected in MUST_REJECT:
            sp = conn.transaction()
            await sp.start()
            try:
                await conn.execute(sql.format(**fmt))
            except Exception as exc:
                got = type(exc).__name__
                if got == expected:
                    print(f"[{PASS}] {label}  -> {got}")
                else:
                    print(f"[{FAIL}] {label}  -> {got}, asteptam {expected}")
                    failures += 1
            else:
                print(f"[{FAIL}] {label}  -> ACCEPTAT, constrangerea NU prinde")
                failures += 1
            finally:
                await sp.rollback()

        print("\n=== SCRIERI CARE TREBUIE ACCEPTATE ===")
        for label, sql in MUST_ACCEPT:
            sp = conn.transaction()
            await sp.start()
            try:
                await conn.execute(sql.format(**fmt))
            except Exception as exc:
                print(f"[{FAIL}] {label}  -> respins: {type(exc).__name__}: {str(exc)[:90]}")
                failures += 1
            else:
                print(f"[{PASS}] {label}")
            finally:
                await sp.rollback()

        # Unicitatea badge-urilor: al doilea insert identic trebuie sa pice.
        print("\n=== UNICITATE ===")
        sp = conn.transaction()
        await sp.start()
        try:
            for _ in range(2):
                await conn.execute(
                    "insert into product_badges (business_id,product_id,label,kind) "
                    "values ($1,$2,'CPNP','compliance')",
                    biz,
                    prod,
                )
        except asyncpg.exceptions.UniqueViolationError:
            print(f"[{PASS}] A3 acelasi badge de doua ori pe acelasi produs -> respins")
        except Exception as exc:
            print(f"[{FAIL}] A3 unicitate badge -> {type(exc).__name__}")
            failures += 1
        else:
            print(f"[{FAIL}] A3 acelasi badge de doua ori -> ACCEPTAT")
            failures += 1
        finally:
            await sp.rollback()

        await tx.rollback()
    finally:
        await conn.close()

    total = len(MUST_REJECT) + len(MUST_ACCEPT) + 1
    print(f"\n{total - failures}/{total} garantii verificate functional")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
