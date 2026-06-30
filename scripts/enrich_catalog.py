"""Îmbogățire catalog cu LLM (mini) — levierul de CALITATE al demo-ului.

Pentru fiecare produs ACTIV, mini generează din nume+categorie:
  • o DESCRIERE reală, naturală în RO (înlocuiește ai_summary formulaic) — copy de
    magazin, cu 1-2 INGREDIENTE/componente PLAUZIBILE pentru categorie (acid hialuronic
    la hidratant etc.), fără procente/preț/brand inventate, fără claim medical;
  • TAG-uri `concerns` dintr-un VOCABULAR CONTROLAT (consistență → filtrare fiabilă
    în search), + un `key_benefit` scurt.

Scrie (DOAR produse `status='active'` — cele arhivate de fix_catalog_coherence sunt sărite):
  • products.ai_summary  = descrierea
  • products.attributes  = attributes || {concerns, key_benefit, enrich_v:2}
    (păstrează cheile existente — EAN etc.)

Self-contained (apel OpenAI direct, ca check_openai.py) — script one-off de date,
nu cod de runtime. Idempotent: produsele cu attributes.enrich_v='2' sunt sărite
(re-rulare gratuită), dacă nu dai --force. Rulează ca ADMIN. Necesită OPENAI_API_KEY.

    python scripts/enrich_catalog.py --limit 3     # test pe 3 (verifici calitatea)
    python scripts/enrich_catalog.py               # toate cele ne-îmbogățite
    python scripts/enrich_catalog.py --force        # re-îmbogățește tot
"""

import argparse
import asyncio
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

VOCAB = [
    "ten gras",
    "ten uscat",
    "ten mixt",
    "ten sensibil",
    "ten normal",
    "acnee",
    "pori dilatați",
    "riduri",
    "anti-aging",
    "hidratare",
    "luminozitate",
    "pete pigmentare",
    "calmare",
    "fermitate",
    "exfoliere",
    "protecție solară",
    "cearcăne",
    "păr uscat",
    "păr gras",
    "volum păr",
    "anti-mătreață",
    "păr vopsit",
    "acoperire machiaj",
    "buze",
    "ochi",
    "sprâncene",
    "uz zilnic",
    "cadou",
]

_SYSTEM = (
    "Ești copywriter pentru un magazin de beauty online din România. Primești numele "
    "și categoria unui produs și scrii conținut de magazin CREDIBIL, în limba română.\n"
    "Răspunzi DOAR cu JSON:\n"
    '{"description": "<2-3 fraze, ton de magazin, natural; include 1-2 INGREDIENTE/componente '
    "TIPICE categoriei (plauzibile: acid hialuronic/glicerină la hidratant, niacinamidă la ten "
    "gras, retinol/peptide la anti-aging, filtre UV la SPF, vitamina C la luminozitate) ȘI "
    'pentru ce tip de ten/uz se potrivește>", '
    '"concerns": ["<2-4 valori DOAR din vocabularul dat>"], '
    '"key_benefit": "<o frază foarte scurtă>"}\n'
    "Reguli: ingredientele să fie PLAUZIBILE pentru categorie (catalog DEMO), dar NU inventa "
    "procente, preț sau brand. NU face afirmații MEDICALE (fără «tratează/vindecă», fără «sigur în "
    "sarcină»). concerns DOAR din vocabular, relevante. Fără superlative goale."
)


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


async def _enrich_one(client, conn, sem, lock, row, *, verbose=False) -> bool:
    async with sem:
        user = (
            f"Produs: {row['name']}\nCategorie: {row['category'] or 'beauty'}\n"
            f"Vocabular concerns (alege din astea): {', '.join(VOCAB)}"
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
            desc = (raw.get("description") or "").strip()
            concerns = [c for c in (raw.get("concerns") or []) if c in VOCAB][:4]
            benefit = (raw.get("key_benefit") or "").strip()
            if not desc:
                return False
        except Exception as e:  # noqa: BLE001 — un produs eșuat nu oprește restul
            print(f"  ! eșuat {row['name'][:40]}: {type(e).__name__}: {e}")
            return False

        patch = json.dumps({"concerns": concerns, "key_benefit": benefit, "enrich_v": 2})
        async with lock:  # o singură scriere pe conexiune odată (asyncpg nu e concurent-safe)
            await conn.execute(
                "update products set ai_summary = $2, attributes = attributes || $3::jsonb "
                "where business_id = $1 and id = $4",
                BIZ,
                desc,
                patch,
                row["id"],
            )
        if verbose:
            print(f"  ✓ {row['name'][:38]} | {concerns}")
            print(f"    {desc}")
        return True


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0 = toate")
    ap.add_argument("--force", action="store_true", help="re-îmbogățește și cele deja făcute")
    args = ap.parse_args()

    if not DSN:
        sys.exit("SUPABASE_DB_URL lipsește în .env")
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        sys.exit("OPENAI_API_KEY lipsește — nu pot îmbogăți")
    client = AsyncOpenAI(api_key=key)

    conn = await _connect()
    try:
        where = "" if args.force else "and (attributes->>'enrich_v') is distinct from '2'"
        limit = f"limit {args.limit}" if args.limit else ""
        rows = await conn.fetch(
            f"""
            select p.id::text as id, p.name, coalesce(c.name,'') as category
            from products p
            left join categories c on c.id = p.primary_category_id
            where p.business_id = $1 and p.status = 'active' {where}
            order by p.name {limit}
            """,
            BIZ,
        )
        print(f"De îmbogățit: {len(rows)} produse (model={MODEL})")
        if not rows:
            return
        sem = asyncio.Semaphore(CONCURRENCY)
        lock = asyncio.Lock()
        verbose = bool(args.limit)
        results = await asyncio.gather(
            *(_enrich_one(client, conn, sem, lock, r, verbose=verbose) for r in rows)
        )
        print(f"\nReușite: {sum(results)}/{len(rows)}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
