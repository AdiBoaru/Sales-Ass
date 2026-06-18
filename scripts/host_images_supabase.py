"""Mută imaginile produselor din Pexels (hot-link) în Supabase Storage propriu.

`seed_product_images.py` pune URL-uri Pexels (CDN extern) în `product_images.url`.
Pentru ca pozele să fie GĂZDUITE de tine (nu dependente de Pexels), scriptul ăsta:
  • descarcă fiecare poză Pexels UNICĂ folosită în catalog (deduplicat — multe
    produse împart aceeași poză din pool);
  • o urcă în bucket-ul public Supabase Storage `product-images` (creat dacă lipsește),
    la calea `pexels/<photo_id>.jpg`;
  • rescrie `product_images.url` la URL-ul public din proiectul TĂU
    (`<API>/storage/v1/object/public/product-images/pexels/<id>.jpg`).

Auth Storage = cheia `SUPABASE_SERVICE_ROLE_KEY` (format nou `sb_secret_...`).
API base derivat din ref-ul proiectului (din `SUPABASE_DB_URL`), ca să nu depindem
de `SUPABASE_URL` (care era setat greșit). DB update e scoped pe business_id prin
join pe `products`. Idempotent: URL-urile care arată deja spre Storage sunt sărite;
upload-ul face upsert (re-rulare gratuită). Rulează ca ADMIN.

    python scripts/host_images_supabase.py --dry-run   # ce ar urca/rescrie
    python scripts/host_images_supabase.py             # mută tot în Storage
"""

import argparse
import asyncio
import os
import re
import socket
import ssl
import sys
from urllib.parse import unquote, urlparse

import asyncpg
import httpx
from dotenv import load_dotenv

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

DSN = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"
BUCKET = "product-images"
CONCURRENCY = 8


def api_base() -> str | None:
    """Derivă https://<ref>.supabase.co din ref-ul proiectului (host/user din DSN)."""
    p = urlparse(DSN)
    m = re.search(r"db\.([a-z0-9]+)\.supabase", p.hostname or "") or re.search(
        r"postgres\.([a-z0-9]+)", p.username or ""
    )
    return f"https://{m.group(1)}.supabase.co" if m else None


async def connect() -> asyncpg.Connection:
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


async def ensure_bucket(client: httpx.AsyncClient, api: str) -> None:
    r = await client.get(f"{api}/storage/v1/bucket/{BUCKET}")
    if r.status_code == 200:
        return
    r = await client.post(
        f"{api}/storage/v1/bucket",
        json={"id": BUCKET, "name": BUCKET, "public": True},
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"creare bucket eșuată: {r.status_code} {r.text[:200]}")
    print(f"Bucket public «{BUCKET}» creat.")


def photo_id(url: str) -> str | None:
    m = re.search(r"/photos/(\d+)/", url)
    return m.group(1) if m else None


async def migrate_one(
    client: httpx.AsyncClient, api: str, sem: asyncio.Semaphore, pexels_url: str
) -> tuple[str, str] | None:
    """Descarcă + urcă o poză. Întoarce (pexels_url, public_url) sau None la eșec."""
    pid = photo_id(pexels_url)
    if not pid:
        print(f"  ⚠️  nu pot extrage id din {pexels_url[:60]} — sărit")
        return None
    path = f"pexels/{pid}.jpg"
    public_url = f"{api}/storage/v1/object/public/{BUCKET}/{path}"
    async with sem:
        try:
            img = (await client.get(pexels_url, timeout=30)).content
            up = await client.post(
                f"{api}/storage/v1/object/{BUCKET}/{path}",
                content=img,
                headers={"Content-Type": "image/jpeg", "x-upsert": "true"},
                timeout=30,
            )
            if up.status_code not in (200, 201):
                print(f"  ⚠️  upload {pid} eșuat: {up.status_code} {up.text[:120]}")
                return None
            return pexels_url, public_url
        except Exception as e:  # noqa: BLE001 — continuăm pe restul pozelor
            print(f"  ⚠️  {pid}: {e}")
            return None


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="doar primele N poze unice (test)")
    ap.add_argument("--dry-run", action="store_true", help="nu urcă/scrie, doar raportează")
    args = ap.parse_args()

    if not DSN:
        print("EROARE: SUPABASE_DB_URL lipsește din .env")
        sys.exit(2)
    if not SERVICE_KEY:
        print("EROARE: SUPABASE_SERVICE_ROLE_KEY lipsește din .env")
        sys.exit(2)
    api = api_base()
    if not api:
        print("EROARE: nu pot deriva API base din SUPABASE_DB_URL")
        sys.exit(2)

    conn = await connect()
    try:
        # Poze Pexels DISTINCTE folosite în catalog (deduplicat → urcăm o singură dată).
        urls = await conn.fetch(
            """
            select distinct pi.url
            from product_images pi
            join products p on p.id = pi.product_id
            where p.business_id = $1 and pi.url like '%images.pexels.com%'
            """,
            BIZ,
        )
        pexels_urls = [r["url"] for r in urls]
        if args.limit:
            pexels_urls = pexels_urls[: args.limit]

        if not pexels_urls:
            print("Nimic de mutat: niciun URL Pexels în product_images (deja găzduit?).")
            return

        print(f"{len(pexels_urls)} poze Pexels unice de mutat în Supabase Storage → «{BUCKET}».")
        if args.dry_run:
            for u in pexels_urls[:6]:
                pid = photo_id(u)
                print(f"  {u[:70]}  →  {api}/storage/v1/object/public/{BUCKET}/pexels/{pid}.jpg")
            print("--dry-run: nu urc/scriu nimic.")
            return

        headers = {"Authorization": f"Bearer {SERVICE_KEY}", "apikey": SERVICE_KEY}
        sem = asyncio.Semaphore(CONCURRENCY)
        async with httpx.AsyncClient(headers=headers) as client:
            await ensure_bucket(client, api)
            results = await asyncio.gather(
                *(migrate_one(client, api, sem, u) for u in pexels_urls)
            )
        mapping = [r for r in results if r]
        print(f"Urcate {len(mapping)}/{len(pexels_urls)} poze. Rescriu product_images.url...")

        # Rescriere scoped pe business prin join pe products (product_images n-are business_id).
        async with conn.transaction():
            await conn.executemany(
                "update product_images set url = $2 from products p "
                "where product_images.product_id = p.id "
                "and p.business_id = $3 and product_images.url = $1",
                [(old, new, BIZ) for old, new in mapping],
            )

        remaining = await conn.fetchval(
            "select count(*) from product_images pi join products p on p.id = pi.product_id "
            "where p.business_id = $1 and pi.url like '%images.pexels.com%'",
            BIZ,
        )
        hosted = await conn.fetchval(
            "select count(*) from product_images pi join products p on p.id = pi.product_id "
            "where p.business_id = $1 and pi.url like '%/storage/v1/object/public/%'",
            BIZ,
        )
        print(
            f"\nOK ✓ — {hosted} imagini pointează acum spre Supabase Storage. "
            f"Rămase pe Pexels: {remaining}."
        )
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
