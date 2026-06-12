"""T037 — spot-check calitate date produse demo (read-only).

Scanează TOT catalogul pentru probleme sistemice + 20 eșantioane detaliate.
Rulează: python scripts/spot_check.py
"""

import asyncio
import os
import re
import socket
import ssl
import sys
from collections import Counter
from urllib.parse import unquote, urlparse

import asyncpg
from dotenv import load_dotenv

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()
DSN = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"
HTML_ENTITY = re.compile(r"&[a-z]+;|&#\d+;")


async def connect():
    p = urlparse(DSN)
    ip = socket.getaddrinfo(p.hostname, p.port or 5432, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return await asyncpg.connect(
        host=ip,
        port=p.port or 5432,
        user=unquote(p.username),
        password=unquote(p.password),
        database="postgres",
        ssl=ctx,
    )


async def main():
    c = await connect()
    try:
        prods = await c.fetch(
            """
            select p.id, p.name, p.price, p.sale_price, p.ai_summary, p.review_count,
                   p.status, p.product_url, b.name as brand, cat.name as category
            from products p
            left join brands b on b.id = p.brand_id
            left join categories cat on cat.id = p.primary_category_id
            where p.business_id = $1
            """,
            BIZ,
        )
        total = len(prods)
        print(f"=== SCAN SISTEMIC ({total} produse) ===\n")

        # name artifacts
        html_names = [p for p in prods if HTML_ENTITY.search(p["name"] or "")]
        ws_names = [p for p in prods if p["name"] != (p["name"] or "").strip()]
        # brand/category mapping
        no_brand = [p for p in prods if not p["brand"]]
        no_cat = [p for p in prods if not p["category"]]
        # price
        bad_price = [p for p in prods if not p["price"] or p["price"] <= 0]
        sale_gt_price = [
            p for p in prods if p["sale_price"] and p["price"] and p["sale_price"] > p["price"]
        ]
        # ai_summary
        no_summary = [p for p in prods if not (p["ai_summary"] or "").strip()]
        short_summary = [p for p in prods if 0 < len((p["ai_summary"] or "").strip()) < 40]
        summ_counts = Counter((p["ai_summary"] or "").strip() for p in prods if p["ai_summary"])
        dup_summaries = {s: n for s, n in summ_counts.items() if n > 1}
        # url
        no_url = [p for p in prods if not p["product_url"]]

        def line(label, items):
            flag = "OK " if not items else "!! "
            print(f"  [{flag}] {label}: {len(items)}")

        line("nume cu entități HTML (&amp; etc.)", html_names)
        line("nume cu spații la capete", ws_names)
        line("fără brand mapat", no_brand)
        line("fără categorie mapată", no_cat)
        line("preț lipsă/≤0", bad_price)
        line("sale_price > price", sale_gt_price)
        line("fără ai_summary", no_summary)
        line("ai_summary foarte scurt (<40 char)", short_summary)
        line("fără product_url", no_url)
        print(
            f"  [{'OK ' if not dup_summaries else '!! '}] ai_summary DUPLICATE "
            f"(grupuri): {len(dup_summaries)}"
        )
        if dup_summaries:
            top = sorted(dup_summaries.items(), key=lambda x: -x[1])[:3]
            for s, n in top:
                print(f'        × {n}: "{s[:70]}..."')

        # variante: sku + stock
        var_stats = await c.fetchrow(
            """
            select count(*) as total,
                   count(*) filter (where sku is null or sku = '') as no_sku,
                   count(*) filter (where stock is null) as no_stock
            from product_variants where business_id = $1
            """,
            BIZ,
        )
        print(
            f"\n  Variante: {var_stats['total']} total | fără sku: "
            f"{var_stats['no_sku']} | fără stock: {var_stats['no_stock']}"
        )

        # preț produs vs min variantă
        mismatch = await c.fetch(
            """
            select p.id, p.name, p.price,
                   min(coalesce(v.sale_price, v.price)) as min_variant
            from products p
            join product_variants v on v.product_id = p.id
            where p.business_id = $1
            group by p.id, p.name, p.price
            having abs(p.price - min(coalesce(v.sale_price, v.price))) > 0.01
            limit 200
            """,
            BIZ,
        )
        print(f"  Produse cu price != min(variant price): {len(mismatch)}")

        # 20 eșantioane (15 random + top 5 review_count)
        sample = await c.fetch(
            """
            (select name, brand, price from
              (select p.name, b.name as brand, p.price from products p
               left join brands b on b.id=p.brand_id
               where p.business_id=$1 order by random() limit 15) s)
            union all
            (select p.name, b.name as brand, p.price from products p
             left join brands b on b.id=p.brand_id
             where p.business_id=$1 order by p.review_count desc nulls last limit 5)
            """,
            BIZ,
        )
        print("\n=== 20 EȘANTIOANE (15 random + top 5 review) ===")
        for s in sample:
            print(f"  • {s['name'][:55]:55} | {str(s['brand'])[:18]:18} | {s['price']} RON")
    finally:
        await c.close()


if __name__ == "__main__":
    asyncio.run(main())
