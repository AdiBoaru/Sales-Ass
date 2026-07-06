"""Export the pilot data pack as JSON (NX-155).

Usage:
    python scripts/export_pilot_data_pack.py --business <business_id> \
        --output reports/pilot-data-pack.json

The export is read-only. It intentionally omits raw vector payloads; embeddings
should be regenerated after import, while this file keeps the embedding manifest.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import ssl
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

import asyncpg

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is present in the app env
    load_dotenv = None


DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"


if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass


def _dsn() -> str | None:
    if load_dotenv:
        load_dotenv()
    return os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")


def _connect_kwargs(dsn: str) -> dict:
    if sys.platform != "win32":
        return {"dsn": dsn, "statement_cache_size": 0}

    parsed = urlparse(dsn)
    ipv4 = socket.getaddrinfo(
        parsed.hostname, parsed.port or 5432, socket.AF_INET, socket.SOCK_STREAM
    )[0][4][0]
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return {
        "host": ipv4,
        "port": parsed.port or 5432,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "database": (parsed.path or "/postgres").lstrip("/"),
        "ssl": ctx,
        "statement_cache_size": 0,
    }


async def _connect() -> asyncpg.Connection:
    dsn = _dsn()
    if not dsn:
        raise SystemExit("SUPABASE_DB_URL or DATABASE_URL is missing")
    return await asyncpg.connect(**_connect_kwargs(dsn))


async def _table_exists(conn: asyncpg.Connection, table: str) -> bool:
    return bool(await conn.fetchval("select to_regclass($1)", f"public.{table}"))


def _jsonish(value):
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


async def _fetch_rows(
    conn: asyncpg.Connection, sql: str, *args, json_fields: tuple[str, ...] = ()
) -> list[dict]:
    rows: list[dict] = []
    for row in await conn.fetch(sql, *args):
        item = dict(row)
        for field in json_fields:
            item[field] = _jsonish(item.get(field))
        rows.append(item)
    return rows


async def _fetch_business(conn: asyncpg.Connection, business_id: str) -> dict:
    row = await conn.fetchrow(
        """
        select id::text, name, slug, vertical, status, default_locale,
               supported_locales, settings
        from businesses
        where id = $1
        """,
        business_id,
    )
    if row is None:
        raise SystemExit(f"business not found: {business_id}")
    business = dict(row)
    business["settings"] = _jsonish(business.get("settings"))
    return business


async def _export(conn: asyncpg.Connection, args: argparse.Namespace) -> dict:
    business = await _fetch_business(conn, args.business)
    product_status_filter = "" if args.include_inactive else "and p.status = 'active'"

    has_brands = await _table_exists(conn, "brands")
    has_categories = await _table_exists(conn, "categories")
    has_images = await _table_exists(conn, "product_images")
    has_variants = await _table_exists(conn, "product_variants")
    has_embeddings = await _table_exists(conn, "product_embeddings")
    has_faqs = await _table_exists(conn, "faqs")
    has_aliases = await _table_exists(conn, "intent_aliases")

    brands = (
        await _fetch_rows(
            conn,
            """
            select id::text, name, slug
            from brands
            where business_id = $1
            order by name, id
            """,
            args.business,
        )
        if has_brands
        else []
    )

    categories = (
        await _fetch_rows(
            conn,
            """
            select id::text, parent_id::text, name, slug, path
            from categories
            where business_id = $1
            order by coalesce(path, slug), name, id
            """,
            args.business,
        )
        if has_categories
        else []
    )

    products = await _fetch_rows(
        conn,
        f"""
        select p.id::text, p.brand_id::text, p.primary_category_id::text,
               p.external_id, p.name, p.slug, p.short_description,
               p.description, p.ai_summary, p.currency, p.price::float8 as price,
               p.sale_price::float8 as sale_price, p.availability, p.stock_total,
               p.rating::float8 as rating, p.review_count, p.status, p.attributes,
               p.seo, p.product_url, p.synced_at::text as synced_at
        from products p
        where p.business_id = $1 {product_status_filter}
        order by coalesce(p.review_count, 0) desc, coalesce(p.rating, 0) desc,
                 p.updated_at desc, p.id
        limit $2
        """,
        args.business,
        args.limit,
        json_fields=("attributes", "seo"),
    )

    product_images = (
        await _fetch_rows(
            conn,
            f"""
            select pi.id::text, pi.product_id::text, pi.url, pi.alt, pi.position
            from product_images pi
            join products p on p.id = pi.product_id
            where p.business_id = $1 {product_status_filter}
            order by pi.product_id, pi.position, pi.id
            """,
            args.business,
        )
        if has_images
        else []
    )

    product_variants = (
        await _fetch_rows(
            conn,
            f"""
            select v.id::text, v.product_id::text, v.label, v.sku, v.external_id,
                   v.price::float8 as price, v.sale_price::float8 as sale_price,
                   v.stock, v.color_hex, v.attributes
            from product_variants v
            join products p on p.id = v.product_id
            where v.business_id = $1 {product_status_filter}
            order by v.product_id, v.label, v.id
            """,
            args.business,
            json_fields=("attributes",),
        )
        if has_variants
        else []
    )

    product_embedding_manifest = (
        await _fetch_rows(
            conn,
            f"""
            select pe.product_id::text, pe.model, pe.content_hash,
                   pe.updated_at::text as updated_at
            from product_embeddings pe
            join products p on p.id = pe.product_id
            where pe.business_id = $1 {product_status_filter}
            order by pe.updated_at desc, pe.product_id
            """,
            args.business,
        )
        if has_embeddings
        else []
    )

    faqs = (
        await _fetch_rows(
            conn,
            """
            select id::text, question, answer, locale,
                   embedding is not null as has_embedding, is_active
            from faqs
            where business_id = $1
            order by is_active desc, locale, question, id
            """,
            args.business,
        )
        if has_faqs
        else []
    )

    intent_aliases = (
        await _fetch_rows(
            conn,
            """
            select id::text, phrase_norm, target_kind, target_id::text,
                   target_value, source, status
            from intent_aliases
            where business_id = $1
            order by status, target_kind, phrase_norm, id
            """,
            args.business,
        )
        if has_aliases
        else []
    )

    return {
        "format": "nativx-pilot-data-pack/v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "business": business,
        "export_options": {
            "include_inactive": args.include_inactive,
            "product_limit": args.limit,
            "raw_vectors_included": False,
        },
        "counts": {
            "brands": len(brands),
            "categories": len(categories),
            "products": len(products),
            "product_images": len(product_images),
            "product_variants": len(product_variants),
            "product_embedding_manifest": len(product_embedding_manifest),
            "faqs": len(faqs),
            "intent_aliases": len(intent_aliases),
        },
        "table_presence": {
            "brands": has_brands,
            "categories": has_categories,
            "product_images": has_images,
            "product_variants": has_variants,
            "product_embeddings": has_embeddings,
            "faqs": has_faqs,
            "intent_aliases": has_aliases,
        },
        "data": {
            "brands": brands,
            "categories": categories,
            "products": products,
            "product_images": product_images,
            "product_variants": product_variants,
            "product_embedding_manifest": product_embedding_manifest,
            "faqs": faqs,
            "intent_aliases": intent_aliases,
        },
    }


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Export read-only pilot data pack JSON (NX-155)")
    ap.add_argument(
        "--business",
        default=os.environ.get("PILOT_BUSINESS_ID", DEMO_BIZ),
        help="business_id to export (default: PILOT_BUSINESS_ID or demo tenant)",
    )
    ap.add_argument("--output", required=True, help="output JSON path")
    ap.add_argument("--limit", type=int, default=500, help="max products to export")
    ap.add_argument("--include-inactive", action="store_true")
    return ap.parse_args()


async def _main() -> int:
    args = _parse_args()
    conn = await _connect()
    try:
        pack = await _export(conn, args)
    finally:
        await conn.close()

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(pack, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"wrote {output} ({pack['counts']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
