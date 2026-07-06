"""Read-only audit for the pilot data pack (NX-155).

Usage:
    python scripts/audit_pilot_data.py --business <business_id>
    python scripts/audit_pilot_data.py --business <business_id> --format json
    python scripts/audit_pilot_data.py --business <business_id> --output reports/pilot.md

The script reads SUPABASE_DB_URL or DATABASE_URL from .env and never writes to DB.
Exit code is 1 when strict pilot gates fail. Use --warn-only for exploratory runs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import ssl
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

import asyncpg

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is present in the app env
    load_dotenv = None


DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"
URL_OK = ("http://", "https://")
FAQ_TOPICS = {
    "livrare": ("livrare", "transport", "curier", "awb"),
    "retur": ("retur", "returnare", "rambursare"),
    "plata": ("plata", "card", "ramburs", "transfer"),
    "garantie": ("garantie", "warranty"),
    "program": ("program", "orar", "contact"),
    "factura": ("factura", "invoice"),
    "gdpr": ("gdpr", "date personale", "privacy"),
}


if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass


@dataclass
class Gate:
    name: str
    ok: bool
    actual: str
    expected: str
    severity: str = "P0"
    details: str = ""


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


async def _count(conn: asyncpg.Connection, sql: str, *args) -> int:
    return int(await conn.fetchval(sql, *args) or 0)


def _percent(n: int, d: int) -> str:
    if d <= 0:
        return "0%"
    return f"{(n / d * 100):.0f}%"


def _missing_for_top(row: asyncpg.Record) -> list[str]:
    missing: list[str] = []
    url = (row["product_url"] or "").strip().lower()
    if not url.startswith(URL_OK):
        missing.append("product_url")
    if row["price"] is None:
        missing.append("price")
    if row["primary_category_id"] is None:
        missing.append("category")
    if not row["has_image"]:
        missing.append("image")
    return missing


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
    return dict(row)


async def _audit(conn: asyncpg.Connection, args: argparse.Namespace) -> dict:
    business = await _fetch_business(conn, args.business)
    has_images = await _table_exists(conn, "product_images")
    has_embeddings = await _table_exists(conn, "product_embeddings")
    has_faqs = await _table_exists(conn, "faqs")
    has_aliases = await _table_exists(conn, "intent_aliases")

    active = await _count(
        conn,
        "select count(*) from products where business_id = $1 and status = 'active'",
        args.business,
    )
    total_products = await _count(
        conn, "select count(*) from products where business_id = $1", args.business
    )
    with_url = await _count(
        conn,
        """
        select count(*) from products
        where business_id = $1 and status = 'active'
          and product_url is not null and product_url <> ''
        """,
        args.business,
    )
    with_abs_url = await _count(
        conn,
        """
        select count(*) from products
        where business_id = $1 and status = 'active'
          and (lower(product_url) like 'http://%' or lower(product_url) like 'https://%')
        """,
        args.business,
    )
    with_price = await _count(
        conn,
        "select count(*) from products where business_id = $1 and status = 'active' "
        "and price is not null",
        args.business,
    )
    with_category = await _count(
        conn,
        """
        select count(*) from products
        where business_id = $1 and status = 'active' and primary_category_id is not null
        """,
        args.business,
    )
    with_summary = await _count(
        conn,
        """
        select count(*) from products
        where business_id = $1 and status = 'active'
          and ai_summary is not null and length(trim(ai_summary)) >= $2
        """,
        args.business,
        args.min_summary_chars,
    )
    in_stock = await _count(
        conn,
        """
        select count(*) from products
        where business_id = $1 and status = 'active'
          and (availability in ('in_stock', 'low_stock') or coalesce(stock_total, 0) > 0)
        """,
        args.business,
    )

    if has_images:
        with_image = await _count(
            conn,
            """
            select count(distinct p.id)
            from products p
            join product_images pi on pi.product_id = p.id
            where p.business_id = $1 and p.status = 'active'
              and pi.url is not null and pi.url <> ''
            """,
            args.business,
        )
        with_abs_image = await _count(
            conn,
            """
            select count(distinct p.id)
            from products p
            join product_images pi on pi.product_id = p.id
            where p.business_id = $1 and p.status = 'active'
              and (lower(pi.url) like 'http://%' or lower(pi.url) like 'https://%')
            """,
            args.business,
        )
    else:
        with_image = 0
        with_abs_image = 0

    if has_embeddings:
        with_embedding = await _count(
            conn,
            """
            select count(distinct p.id)
            from products p
            join product_embeddings pe on pe.product_id = p.id and pe.business_id = p.business_id
            where p.business_id = $1 and p.status = 'active'
            """,
            args.business,
        )
        embedding_models = [
            dict(r)
            for r in await conn.fetch(
                """
                select model, count(*)::int as count
                from product_embeddings
                where business_id = $1
                group by model
                order by count(*) desc, model
                """,
                args.business,
            )
        ]
    else:
        with_embedding = 0
        embedding_models = []

    image_expr = (
        "exists (select 1 from product_images pi where pi.product_id = p.id "
        "and pi.url is not null and pi.url <> '')"
        if has_images
        else "false"
    )
    embedding_expr = (
        "exists (select 1 from product_embeddings pe "
        "where pe.product_id = p.id and pe.business_id = p.business_id)"
        if has_embeddings
        else "false"
    )
    top_rows = await conn.fetch(
        f"""
        select p.id::text, p.name, p.product_url, p.price::float8 as price,
               p.currency, p.primary_category_id::text, c.name as category,
               p.ai_summary, p.rating::float8 as rating, p.review_count,
               {image_expr} as has_image,
               {embedding_expr} as has_embedding
        from products p
        left join categories c on c.id = p.primary_category_id
        where p.business_id = $1 and p.status = 'active'
        order by coalesce(p.review_count, 0) desc, coalesce(p.rating, 0) desc,
                 p.updated_at desc, p.id
        limit $2
        """,
        args.business,
        args.top_n,
    )
    top_missing = []
    top_core_ready = 0
    top_summary_ready = 0
    top_embedding_ready = 0
    for row in top_rows:
        missing = _missing_for_top(row)
        if not missing:
            top_core_ready += 1
        else:
            top_missing.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "missing": missing,
                }
            )
        if row["ai_summary"] and len(row["ai_summary"].strip()) >= args.min_summary_chars:
            top_summary_ready += 1
        if row["has_embedding"]:
            top_embedding_ready += 1

    faqs_active = 0
    faqs_with_embedding = 0
    faq_locales: list[dict] = []
    faq_topic_counts: dict[str, int] = {}
    if has_faqs:
        faqs_active = await _count(
            conn,
            "select count(*) from faqs where business_id = $1 and is_active = true",
            args.business,
        )
        faqs_with_embedding = await _count(
            conn,
            """
            select count(*) from faqs
            where business_id = $1 and is_active = true and embedding is not null
            """,
            args.business,
        )
        faq_locales = [
            dict(r)
            for r in await conn.fetch(
                """
                select locale, count(*)::int as count,
                       count(*) filter (where embedding is not null)::int as embedded
                from faqs
                where business_id = $1 and is_active = true
                group by locale
                order by locale
                """,
                args.business,
            )
        ]
        for topic, words in FAQ_TOPICS.items():
            like_terms = [f"%{w}%" for w in words]
            faq_topic_counts[topic] = await _count(
                conn,
                # translate() strips RO diacritics (garanție→garantie) so ASCII search terms
                # match diacritic content — no dependency on the unaccent extension.
                """
                select count(*) from faqs
                where business_id = $1 and is_active = true
                  and exists (
                    select 1 from unnest($2::text[]) q
                    where translate(lower(question || ' ' || answer),
                                    'ăâîșşțţ', 'aaisstt') like q
                  )
                """,
                args.business,
                like_terms,
            )

    aliases_approved = 0
    alias_breakdown: list[dict] = []
    if has_aliases:
        aliases_approved = await _count(
            conn,
            """
            select count(*) from intent_aliases
            where business_id = $1 and status = 'approved'
            """,
            args.business,
        )
        alias_breakdown = [
            dict(r)
            for r in await conn.fetch(
                """
                select target_kind, count(*)::int as count
                from intent_aliases
                where business_id = $1 and status = 'approved'
                group by target_kind
                order by count(*) desc, target_kind
                """,
                args.business,
            )
        ]

    categories = await _count(
        conn, "select count(*) from categories where business_id = $1", args.business
    )
    active_categories_used = await _count(
        conn,
        """
        select count(distinct primary_category_id)
        from products
        where business_id = $1 and status = 'active' and primary_category_id is not null
        """,
        args.business,
    )
    currencies = [
        dict(r)
        for r in await conn.fetch(
            """
            select currency, count(*)::int as count
            from products
            where business_id = $1 and status = 'active'
            group by currency
            order by count(*) desc, currency
            """,
            args.business,
        )
    ]
    templated_names = await _count(
        conn,
        """
        select count(*) from products
        where business_id = $1 and status = 'active' and name ~ $2
        """,
        args.business,
        r"\s[0-9]{2,4}$",
    )
    duplicate_name_groups = await _count(
        conn,
        """
        select count(*) from (
            select lower(trim(name)) as n
            from products
            where business_id = $1 and status = 'active'
            group by lower(trim(name))
            having count(*) > 1
        ) d
        """,
        args.business,
    )

    gates = [
        Gate(
            "active products",
            active >= args.top_n,
            str(active),
            f">= {args.top_n}",
        ),
        Gate(
            f"top {args.top_n} core card fields",
            len(top_rows) >= args.top_n and top_core_ready >= args.top_n,
            f"{top_core_ready}/{args.top_n}",
            "all have absolute product_url, price, category, image",
        ),
        Gate(
            "product summaries",
            with_summary >= args.min_summaries,
            str(with_summary),
            f">= {args.min_summaries} active products with ai_summary",
            severity="P1",
        ),
        Gate(
            "product embeddings",
            with_embedding >= args.min_embeddings,
            str(with_embedding),
            f">= {args.min_embeddings} active products with embeddings",
        ),
        Gate(
            "active FAQ",
            faqs_active >= args.min_faqs,
            str(faqs_active),
            f">= {args.min_faqs}",
        ),
        Gate(
            "embedded FAQ",
            faqs_with_embedding >= args.min_faqs,
            str(faqs_with_embedding),
            f">= {args.min_faqs}",
            severity="P1",
        ),
        Gate(
            "approved aliases",
            aliases_approved >= args.min_aliases,
            str(aliases_approved),
            f">= {args.min_aliases}",
            severity="P1",
        ),
    ]

    metrics = {
        "products_total": total_products,
        "products_active": active,
        "products_in_stock_or_low_stock": in_stock,
        "active_with_product_url": with_url,
        "active_with_absolute_product_url": with_abs_url,
        "active_with_price": with_price,
        "active_with_category": with_category,
        "active_with_image": with_image,
        "active_with_absolute_image": with_abs_image,
        "active_with_ai_summary": with_summary,
        "active_with_embedding": with_embedding,
        "categories_total": categories,
        "active_categories_used": active_categories_used,
        "faqs_active": faqs_active,
        "faqs_with_embedding": faqs_with_embedding,
        "aliases_approved": aliases_approved,
        "templated_name_suffix_count": templated_names,
        "duplicate_name_groups": duplicate_name_groups,
    }

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "business": business,
        "settings": {
            "top_n": args.top_n,
            "min_summaries": args.min_summaries,
            "min_summary_chars": args.min_summary_chars,
            "min_embeddings": args.min_embeddings,
            "min_faqs": args.min_faqs,
            "min_aliases": args.min_aliases,
        },
        "verdict": "pass" if all(g.ok for g in gates) else "fail",
        "gates": [asdict(g) for g in gates],
        "metrics": metrics,
        "coverage": {
            "product_url": _percent(with_abs_url, active),
            "image": _percent(with_abs_image, active),
            "summary": _percent(with_summary, active),
            "embedding": _percent(with_embedding, active),
            "faq_embedding": _percent(faqs_with_embedding, faqs_active),
        },
        "currencies": currencies,
        "embedding_models": embedding_models,
        "faq_locales": faq_locales,
        "faq_topic_counts": faq_topic_counts,
        "alias_breakdown": alias_breakdown,
        "top_products": {
            "checked": len(top_rows),
            "core_ready": top_core_ready,
            "summary_ready": top_summary_ready,
            "embedding_ready": top_embedding_ready,
            "missing_samples": top_missing[: args.examples],
        },
        "table_presence": {
            "product_images": has_images,
            "product_embeddings": has_embeddings,
            "faqs": has_faqs,
            "intent_aliases": has_aliases,
        },
        "next_actions": _next_actions(gates),
    }


def _next_actions(gates: list[Gate]) -> list[str]:
    failed = {g.name for g in gates if not g.ok}
    actions: list[str] = []
    if "active products" in failed or "top " in " ".join(failed):
        actions.append(
            "Import/curate the top pilot products with image, product_url, price, category."
        )
    if "product embeddings" in failed:
        actions.append(
            "Run product embedding job after catalog is stable: "
            "python -m src.jobs.embed_products --force"
        )
    if "active FAQ" in failed or "embedded FAQ" in failed:
        actions.append("Seed curated FAQ: python -m src.jobs.seed_faqs --business <business_id>")
    if "approved aliases" in failed:
        actions.append(
            "Seed/approve intent_aliases for frequent web questions and category synonyms."
        )
    if "product summaries" in failed:
        actions.append(
            "Enrich product ai_summary/details for at least the representative demo subset."
        )
    return actions


def _render_markdown(report: dict) -> str:
    biz = report["business"]
    lines = [
        "# Pilot Data Pack Audit",
        "",
        f"Generated: `{report['generated_at']}`",
        f"Business: `{biz['name']}` (`{biz['id']}`)",
        f"Slug: `{biz['slug']}`  Vertical: `{biz['vertical']}`  Status: `{biz['status']}`",
        f"Verdict: **{report['verdict'].upper()}**",
        "",
        "## Gates",
        "",
        "| Gate | Status | Actual | Expected | Severity |",
        "|---|---:|---:|---|---|",
    ]
    for gate in report["gates"]:
        status = "PASS" if gate["ok"] else "FAIL"
        lines.append(
            f"| {gate['name']} | {status} | {gate['actual']} "
            f"| {gate['expected']} | {gate['severity']} |"
        )

    lines += [
        "",
        "## Metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
    ]
    for key, value in report["metrics"].items():
        lines.append(f"| `{key}` | {value} |")

    lines += [
        "",
        "## Coverage",
        "",
        "| Area | Coverage |",
        "|---|---:|",
    ]
    for key, value in report["coverage"].items():
        lines.append(f"| `{key}` | {value} |")

    lines += ["", "## Currencies", ""]
    lines.extend([f"- `{r['currency']}`: {r['count']}" for r in report["currencies"]] or ["- none"])

    lines += ["", "## FAQ Topics", ""]
    if report["faq_topic_counts"]:
        for topic, count in report["faq_topic_counts"].items():
            lines.append(f"- `{topic}`: {count}")
    else:
        lines.append("- FAQ table missing or empty")

    lines += ["", "## Alias Breakdown", ""]
    lines.extend(
        [f"- `{r['target_kind']}`: {r['count']}" for r in report["alias_breakdown"]]
        or ["- no approved aliases"]
    )

    lines += ["", "## Top Product Gaps", ""]
    gaps = report["top_products"]["missing_samples"]
    if not gaps:
        lines.append("- none in sampled top products")
    else:
        for item in gaps:
            lines.append(
                f"- `{item['name']}` (`{item['id']}`): missing {', '.join(item['missing'])}"
            )

    lines += ["", "## Next Actions", ""]
    lines.extend([f"- {a}" for a in report["next_actions"]] or ["- none"])
    lines.append("")
    return "\n".join(lines)


def _parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Read-only pilot data pack audit (NX-155)")
    ap.add_argument(
        "--business",
        default=os.environ.get("PILOT_BUSINESS_ID", DEMO_BIZ),
        help="business_id to audit (default: PILOT_BUSINESS_ID or demo tenant)",
    )
    ap.add_argument("--format", choices=("markdown", "json"), default="markdown")
    ap.add_argument("--output", help="optional output path")
    ap.add_argument("--top-n", type=int, default=50)
    ap.add_argument("--min-summaries", type=int, default=20)
    ap.add_argument("--min-summary-chars", type=int, default=40)
    ap.add_argument("--min-embeddings", type=int, default=50)
    ap.add_argument("--min-faqs", type=int, default=8)
    ap.add_argument("--min-aliases", type=int, default=5)
    ap.add_argument("--examples", type=int, default=10)
    ap.add_argument("--warn-only", action="store_true", help="always exit 0")
    return ap.parse_args()


async def _main() -> int:
    args = _parse_args()
    conn = await _connect()
    try:
        report = await _audit(conn, args)
    finally:
        await conn.close()

    if args.format == "json":
        rendered = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    else:
        rendered = _render_markdown(report)

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(rendered, encoding="utf-8")
    print(rendered)
    if args.warn_only:
        return 0
    return 0 if report["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
