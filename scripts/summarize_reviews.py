"""Rezumate de recenzii (D3) — credibilitate pentru recomandări.

Recenziile demo sunt fictive (toate 5★, formulaice). Pentru un demo credibil,
generăm cu LLM (mini) un rezumat realist al feedback-ului + pro-uri/contra, și
variem ratingul determinist (4.3-4.9, nu toate 5★ — uniform = nereal). Grounded
pe produs (nume, descriere, concerns) — fără cifre/ingrediente inventate.

Scrie:
  • product_review_summaries (summary, sentiment, top_pros[], top_cons[],
    review_count_at_build, built_at)
  • products.rating = ratingul variat (ca search/agent să-l afișeze: „4.7★")

Self-contained (ca enrich/check_openai). Idempotent (skip dacă există summary, fără
--force). Rulează ca ADMIN. Necesită OPENAI_API_KEY.

    python scripts/summarize_reviews.py --limit 3
    python scripts/summarize_reviews.py
    python scripts/summarize_reviews.py --force
"""

import argparse
import asyncio
import hashlib
import json
import os
import socket
import ssl
import sys
from urllib.parse import unquote, urlparse

import asyncpg
from dotenv import load_dotenv
from openai import AsyncOpenAI

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv(".env")
DSN = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"
MODEL = os.environ.get("MODEL_AGENT", "gpt-5.4-mini")
CONCURRENCY = 8

_SYSTEM = (
    "Rezumi feedback-ul clienților pentru un produs de beauty, în limba română. "
    "Primești numele, descrierea și concerns-urile produsului. Generezi un rezumat "
    "SCURT și CREDIBIL al a ceea ce apreciază clienții.\n"
    "Răspunzi DOAR JSON:\n"
    '{"summary": "<1-2 fraze, ce spun clienții>", '
    '"top_pros": ["<2-3 puncte forte scurte>"], '
    '"top_cons": ["<0-1 minus minor, sau listă goală>"]}\n'
    "NU inventa cifre, procente, ingrediente sau prețuri. Ton neutru, realist."
)


def _rating(product_id: str) -> float:
    """Rating determinist variat 4.3–4.9 (mostly high, dar nu uniform 5★)."""
    h = int(hashlib.sha256(product_id.encode()).hexdigest(), 16)
    return round(4.3 + (h % 7) * 0.1, 1)


async def _connect() -> asyncpg.Connection:
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
        database=(p.path or "/postgres").lstrip("/"),
        ssl=ctx,
    )


async def _one(client, conn, sem, lock, row, *, verbose=False) -> bool:
    async with sem:
        concerns = ", ".join(row["concerns"] or [])
        user = (
            f"Produs: {row['name']}\nDescriere: {row['ai_summary'] or ''}\n"
            f"Concerns: {concerns}\nNr recenzii: {row['n']} (majoritar pozitive)."
        )
        try:
            resp = await client.chat.completions.create(
                model=MODEL,
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
            )
            raw = json.loads(resp.choices[0].message.content or "{}")
            summary = (raw.get("summary") or "").strip()
            pros = [str(x).strip() for x in (raw.get("top_pros") or [])][:3]
            cons = [str(x).strip() for x in (raw.get("top_cons") or [])][:2]
            if not summary:
                return False
        except Exception as e:  # noqa: BLE001 — un produs eșuat nu oprește restul
            print(f"  ! eșuat {row['name'][:40]}: {type(e).__name__}: {e}")
            return False

        rating = _rating(row["id"])
        sentiment = round(rating / 5.0, 2)
        async with lock:
            async with conn.transaction():
                await conn.execute(
                    """
                    insert into product_review_summaries
                        (product_id, business_id, summary, sentiment, top_pros, top_cons,
                         review_count_at_build, built_at)
                    values ($1, $2, $3, $4, $5, $6, $7, now())
                    on conflict (product_id) do update set
                        summary = excluded.summary, sentiment = excluded.sentiment,
                        top_pros = excluded.top_pros, top_cons = excluded.top_cons,
                        review_count_at_build = excluded.review_count_at_build, built_at = now()
                    """,
                    row["id"],
                    BIZ,
                    summary,
                    sentiment,
                    pros,
                    cons,
                    row["n"],
                )
                await conn.execute(
                    "update products set rating = $2 where business_id = $1 and id = $3",
                    BIZ,
                    rating,
                    row["id"],
                )
        if verbose:
            print(f"  ✓ {row['name'][:36]} | {rating}★ | pros={pros}")
        return True


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    if not DSN:
        sys.exit("SUPABASE_DB_URL lipsește în .env")
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        sys.exit("OPENAI_API_KEY lipsește")
    client = AsyncOpenAI(api_key=key)

    conn = await _connect()
    try:
        where = "" if args.force else "and prs.product_id is null"
        limit = f"limit {args.limit}" if args.limit else ""
        rows = await conn.fetch(
            f"""
            select p.id::text as id, p.name, p.ai_summary,
                   (select array_agg(v) from jsonb_array_elements_text(
                        case when jsonb_typeof(p.attributes->'concerns')='array'
                             then p.attributes->'concerns' else '[]'::jsonb end) v) as concerns,
                   (select count(*) from reviews r where r.product_id = p.id) as n,
                   prs.product_id as has_summary
            from products p
            left join product_review_summaries prs on prs.product_id = p.id
            where p.business_id = $1 and p.status = 'active'
              and exists (select 1 from reviews r where r.product_id = p.id) {where}
            order by p.id {limit}
            """,
            BIZ,
        )
        print(f"De rezumat: {len(rows)} produse (model={MODEL})")
        if not rows:
            return
        sem = asyncio.Semaphore(CONCURRENCY)
        lock = asyncio.Lock()
        verbose = bool(args.limit)
        results = await asyncio.gather(
            *(_one(client, conn, sem, lock, r, verbose=verbose) for r in rows)
        )
        print(f"\nReușite: {sum(results)}/{len(rows)}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
