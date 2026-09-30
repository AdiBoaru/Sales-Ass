"""NX-360 — resincronizează disponibilitatea și prețul produselor SOLE din paginile live sole.ro.

    # 1. citește paginile, NU scrie nimic; raportul local cu diferențele
    PYTHONPATH=. python scripts/sole_refresh.py fetch --business sole-ro [--only-oos] [--limit N]
    # 2. aplică un raport (rulat de owner, pe credentialul de scriere în catalog)
    TARGET_DB_URL=postgresql://... PYTHONPATH=. python scripts/sole_refresh.py apply \
        --report reports/nx360/refresh-<stamp>.json --apply

Catalogul e o fotografie din 2026-08-28, fără sincronizare: 391 de produse `out_of_stock`, toate cu
prețul voucherului WELCOME15 drept preț de listă, iar o parte sunt de fapt în stoc (ROUND LAB Birch
Juice: epuizat la 101,91 în baza noastră, în stoc la 120 lei pe site). Regulile paginii sunt în
`src/catalog/sole_live.py` (pur, testat): disponibilitatea din JSON-LD, prețul de listă DOAR din
rândul de preț, niciodată dedus. Pe un produs care rămâne epuizat, prețul de listă nu se poate afla,
deci nu se atinge (`price_unverified` în raport).

`fetch` e read-only față de baza noastră (citește lista de produse) și face o cerere HTTP pe
produs, cu pauză între cereri. `apply` scrie într-o singură tranzacție, idempotent, doar din raport:
`products` (disponibilitate; preț, reducere, voucher când pagina le arată; `synced_at`) și varianta
unică a produsului (preț, reducere). Un produs cu mai multe variante nu primește preț (raportat).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.catalog.sole_live import Refresh, decide, parse_page  # noqa: E402

OUT_DIR = ROOT / "reports" / "nx360"
USER_AGENT = "Mozilla/5.0 (compatible; NativxCatalogSync/1.0; +https://nativxtech.com)"
DELAY_S = (1.0, 2.0)
TIMEOUT_S = 20.0

PRODUCTS_SQL = """
select p.id::text as id, p.product_url, p.availability, p.price, p.sale_price,
       p.coupon_code, p.coupon_price,
       (select count(*) from product_variants v where v.product_id = p.id) as n_variants
from products p
where p.business_id = $1::uuid and p.status = 'active' and p.product_url is not null
  and ($2::bool is false or p.availability = 'out_of_stock')
order by p.id
"""
BUSINESS_SQL = "select id::text from businesses where slug = $1 or id::text = $1"


def _money(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def diff_row(current: dict[str, Any], refresh: Refresh) -> dict[str, Any]:
    """Rândul de raport: ce e în baza noastră, ce spune pagina, ce s-ar scrie. PUR."""
    price = refresh.price
    new_price = (
        {
            "price": _money(price.price),
            "sale_price": _money(price.sale_price),
            "coupon_code": price.coupon_code,
            "coupon_price": _money(price.coupon_price),
        }
        if price
        else None
    )
    old_price = {
        "price": _money(current.get("price")),
        "sale_price": _money(current.get("sale_price")),
        "coupon_code": current.get("coupon_code"),
        "coupon_price": _money(current.get("coupon_price")),
    }
    return {
        "product_id": refresh.product_id,
        "url": current.get("product_url"),
        "status": refresh.status,
        "availability": {"old": current.get("availability"), "new": refresh.availability},
        "price": {"old": old_price, "new": new_price},
        "price_unverified": refresh.price_unverified,
        "n_variants": int(current.get("n_variants") or 0),
        "anomalies": list(price.anomalies) if price else [],
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Agregatele raportului. PUR."""
    flips = Counter(
        f"{r['availability']['old']}→{r['availability']['new']}"
        for r in rows
        if r["status"] == "ok" and r["availability"]["old"] != r["availability"]["new"]
    )
    price_changed = sum(
        1 for r in rows if r["price"]["new"] is not None and r["price"]["new"] != r["price"]["old"]
    )
    return {
        "fetched": len(rows),
        "status": dict(Counter(r["status"] for r in rows)),
        "availability_flips": dict(flips),
        "price_changed": price_changed,
        "price_unverified": sum(1 for r in rows if r["price_unverified"]),
        "multi_variant_price_skipped": sum(
            1 for r in rows if r["price"]["new"] is not None and r["n_variants"] > 1
        ),
        "anomalies": dict(Counter(a for r in rows for a in r["anomalies"])),
    }


async def _products(business: str, only_oos: bool) -> tuple[str, list[dict[str, Any]]]:
    from src.db.connection import admin_conn, close_pool, get_pool, tenant_conn  # noqa: PLC0415

    try:
        pool = await get_pool()
        async with admin_conn(pool) as conn:
            business_id = await conn.fetchval(BUSINESS_SQL, business)
        if business_id is None:
            raise SystemExit(f"tenant necunoscut: {business!r}")
        async with tenant_conn(business_id) as conn:
            rows = await conn.fetch(PRODUCTS_SQL, business_id, only_oos)
        return business_id, [dict(r) for r in rows]
    finally:
        await close_pool()


def fetch(args: argparse.Namespace) -> int:
    import httpx  # noqa: PLC0415 — doar faza care vorbește cu site-ul

    business_id, products = asyncio.run(_products(args.business, args.only_oos))
    if args.limit:
        products = products[: args.limit]
    rows: list[dict[str, Any]] = []
    errors: Counter[str] = Counter()
    started = time.monotonic()
    with httpx.Client(
        headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S, follow_redirects=True
    ) as client:
        for i, p in enumerate(products, 1):
            try:
                resp = client.get(p["product_url"])
            except httpx.HTTPError as e:
                errors[type(e).__name__] += 1
                continue
            if resp.status_code == 404:
                page_html = ""
            elif resp.status_code != 200:
                errors[f"http_{resp.status_code}"] += 1
                continue
            else:
                page_html = resp.text
            rows.append(diff_row(p, decide(p["id"], parse_page(page_html))))
            if i % 100 == 0:
                print(f"  {i}/{len(products)}  ({time.monotonic() - started:.0f} s)", flush=True)
            time.sleep(random.uniform(*DELAY_S))
    report = {
        "taken_at": datetime.now(UTC).isoformat(),
        "business_id": business_id,
        "only_oos": args.only_oos,
        "summary": summarize(rows),
        "fetch_errors": dict(errors),
        "rows": rows,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"refresh-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({**report["summary"], "fetch_errors": report["fetch_errors"]}, indent=2))
    print(f"raport local: {path.relative_to(ROOT)}")
    return 0


#: Ce scrie `apply`, per produs. `synced_at` se mută pe orice rând `ok`: pagina a fost citită acum.
UPDATE_PRODUCT_SQL = """
update products set
  availability = $3,
  price = coalesce($4, price),
  sale_price = case when $4 is null then sale_price else $5 end,
  coupon_code = case when $4 is null then coupon_code else $6 end,
  coupon_price = case when $4 is null then coupon_price else $7 end,
  synced_at = now(),
  updated_at = now()
where business_id = $1::uuid and id = $2::uuid
"""
UPDATE_VARIANT_SQL = """
update product_variants set price = $3, sale_price = $4
where business_id = $1::uuid and product_id = $2::uuid
  and (select count(*) from product_variants v where v.product_id = $2::uuid) = 1
"""


def apply_params(business_id: str, row: dict[str, Any]) -> tuple[tuple, tuple | None] | None:
    """Parametrii de scriere pentru un rând de raport; None = nu se scrie nimic. PUR."""
    if row["status"] != "ok" or row["availability"]["new"] is None:
        return None
    new = row["price"]["new"]
    dec = lambda v: Decimal(v) if v is not None else None  # noqa: E731
    product = (
        business_id,
        row["product_id"],
        row["availability"]["new"],
        dec(new["price"]) if new else None,
        dec(new["sale_price"]) if new else None,
        new["coupon_code"] if new else None,
        dec(new["coupon_price"]) if new else None,
    )
    variant = (
        (business_id, row["product_id"], dec(new["price"]), dec(new["sale_price"]))
        if new and row["n_variants"] == 1
        else None
    )
    return product, variant


async def _apply(report: dict[str, Any], write: bool) -> dict[str, int]:
    import asyncpg  # noqa: PLC0415

    params = [p for p in (apply_params(report["business_id"], r) for r in report["rows"]) if p]
    counts = {"products": len(params), "variants": sum(1 for _, v in params if v)}
    if not write:
        return counts
    url = os.environ.get("TARGET_DB_URL")
    if not url:
        raise SystemExit("TARGET_DB_URL lipsește (credentialul de scriere în catalog)")
    conn = await asyncpg.connect(url)
    try:
        async with conn.transaction():
            await conn.executemany(UPDATE_PRODUCT_SQL, [p for p, _ in params])
            await conn.executemany(UPDATE_VARIANT_SQL, [v for _, v in params if v])
    finally:
        await conn.close()
    return counts


def apply(args: argparse.Namespace) -> int:
    report = json.loads(Path(args.report).read_text(encoding="utf-8"))
    counts = asyncio.run(_apply(report, args.apply))
    verb = "scrise" if args.apply else "de scris (dry-run, adaugă --apply)"
    print(f"produse {verb}: {counts['products']}, variante: {counts['variants']}")
    print(json.dumps(report["summary"], indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="citește paginile live și scrie raportul local")
    f.add_argument("--business", required=True)
    f.add_argument("--only-oos", action="store_true", help="doar produsele marcate epuizate")
    f.add_argument("--limit", type=int, default=0)
    a = sub.add_parser("apply", help="aplică un raport (dry-run fără --apply)")
    a.add_argument("--report", required=True)
    a.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    return fetch(args) if args.cmd == "fetch" else apply(args)


if __name__ == "__main__":
    raise SystemExit(main())
