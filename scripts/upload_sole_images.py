"""Urcă imaginile de produs în Supabase Storage și marchează în DB ce e găzduit.

Trei pași, în ordinea asta și nu alta:

  1. `--create-bucket`  creează bucketul public, o dată
  2. `--upload`         urcă imaginile principale (2.758 fișiere, ~391 MB), concurent
  3. `--mark-hosted`    rescrie `product_images.url` și pune `storage='self'`

Pasul 3 vine DUPĂ 2, deliberat: `storage='self'` e o AFIRMAȚIE că fișierul e la noi. Marcat
înainte de upload, baza ar minți, iar widgetul ar cere poze care nu există. Aceeași regulă ca la
`commerce_action_receipts` (NX-237): mai întâi realitatea, apoi înregistrarea ei.

Idempotent: fișierele deja urcate cu aceeași dimensiune se sar, iar `upsert` acoperă restul. O
reluare după o cădere de rețea nu re-urcă tot.

Nu folosește `supabase-py`: ar aduce o dependență întreagă pentru trei apeluri HTTP, iar clientul
Data API e stins pe proiectul ăsta oricum. Storage e alt serviciu și rămâne disponibil.

Rulare:
    export SUPABASE_URL=https://<ref>.supabase.co
    export SUPABASE_SECRET_KEY=sb_secret_...        # Settings -> API Keys
    python scripts/upload_sole_images.py --create-bucket
    python scripts/upload_sole_images.py --upload --src D:/tmp/sole-images
    TARGET_DB_URL=... python scripts/upload_sole_images.py --mark-hosted
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import asyncpg
import httpx

BUCKET = "product-images"
PREFIX = "sole"
CONCURRENCY = 12
# Numele conține SKU-ul și nu se schimbă sub el: o poză nouă înseamnă altă cale. Deci putem
# promite cache imuabil un an, iar a doua vizualizare a aceluiași card nu mai costă egress —
# lucrul care contează cel mai mult pe planul free, unde bugetul e împărțit cu baza de date.
CACHE_CONTROL = "31536000"

CONTENT_TYPES = {
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
}

OK, WARN, BAD = "  ok  ", " ATENTIE ", " ESUAT "


def creds() -> tuple[str, str]:
    url = (os.environ.get("SUPABASE_URL") or "").rstrip("/")
    key = os.environ.get("SUPABASE_SECRET_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        sys.exit("SUPABASE_URL si SUPABASE_SECRET_KEY sunt obligatorii")
    return url, key


def local_files(src: Path) -> list[tuple[str, Path]]:
    """(cale_in_bucket, fisier_local). Structura e `<prefix>/<sku>/1.<ext>`."""
    out = []
    for sku_dir in sorted(p for p in src.iterdir() if p.is_dir()):
        for f in sorted(sku_dir.iterdir()):
            if f.suffix.lower() in CONTENT_TYPES:
                out.append((f"{PREFIX}/{sku_dir.name}/{f.name}", f))
                break  # o singură poză per produs
    return out


async def create_bucket() -> int:
    url, key = creds()
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(
            f"{url}/storage/v1/bucket",
            headers={"Authorization": f"Bearer {key}", "apikey": key},
            json={
                "name": BUCKET,
                "id": BUCKET,
                # PUBLIC: widgetul randează `<img src>` fără sesiune. Un bucket privat ar cere
                # URL-uri semnate cu expirare, adică un round-trip pe fiecare card afișat.
                "public": True,
                "file_size_limit": 10 * 1024 * 1024,
                "allowed_mime_types": sorted(set(CONTENT_TYPES.values())),
            },
        )
    if r.status_code in (200, 201):
        print(f"[{OK}] bucket `{BUCKET}` creat (public)")
        return 0
    if r.status_code == 409 or "already exists" in r.text.lower():
        print(f"[{OK}] bucket `{BUCKET}` exista deja")
        return 0
    print(f"[{BAD}] {r.status_code}: {r.text[:300]}")
    return 1


async def existing_objects() -> dict[str, int]:
    """Ce e deja în bucket, ca reluarea să nu re-urce. Cheie completă → dimensiune.

    Sursa e tabelul `storage.objects` din Postgres, NU API-ul `/storage/v1/object/list`.
    Motivul e că listarea prin API e **nerecursivă**: cu `prefix='sole'` întoarce pseudo-foldere
    (`{"name": "F26146", "id": null, "metadata": null}`), nu fișiere. Un inventar construit din ele
    iese gol, iar scriptul re-urcă liniștit toate cele 403 MB — exact ce s-a întâmplat la prima
    rulare. `upsert` face rezultatul corect, deci defectul nu se vede în date, doar în trafic.

    Interogarea directă e și exactă, și mai ieftină: un query în loc de zeci de pagini de API.
    Fără `TARGET_DB_URL` întoarce gol, adică „urcă tot" — degradare sigură, nu tăcută.
    """
    dsn = os.environ.get("TARGET_DB_URL")
    if not dsn:
        print(f"[{WARN}] fara TARGET_DB_URL nu pot inventaria bucketul; urc tot")
        return {}
    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        rows = await conn.fetch(
            "select name, (metadata->>'size')::bigint as size from storage.objects "
            "where bucket_id = $1 and name like $2",
            BUCKET,
            f"{PREFIX}/%",
        )
    finally:
        await conn.close()
    return {r["name"]: int(r["size"]) for r in rows if r["size"] is not None}


async def upload(src: Path) -> int:
    url, key = creds()
    files = local_files(src)
    if not files:
        print(f"[{BAD}] niciun fisier in {src}")
        return 1
    total_mb = sum(f.stat().st_size for _, f in files) / 1e6
    print(f"[{OK}] {len(files)} fisiere, {total_mb:.0f} MB")

    limits = httpx.Limits(max_connections=CONCURRENCY, max_keepalive_connections=CONCURRENCY)
    async with httpx.AsyncClient(timeout=120, limits=limits) as client:
        have = await existing_objects()
        todo = [(k, f) for k, f in files if have.get(k) != f.stat().st_size]
        print(f"[{OK}] de urcat: {len(todo)} (deja acolo: {len(files) - len(todo)})")
        if not todo:
            return 0

        sem = asyncio.Semaphore(CONCURRENCY)
        done = failed = 0
        lock = asyncio.Lock()

        async def put(key_path: str, path: Path) -> None:
            nonlocal done, failed
            async with sem:
                body = path.read_bytes()
                try:
                    r = await client.post(
                        f"{url}/storage/v1/object/{BUCKET}/{key_path}",
                        headers={
                            "Authorization": f"Bearer {key}",
                            "apikey": key,
                            "Content-Type": CONTENT_TYPES[path.suffix.lower()],
                            "Cache-Control": CACHE_CONTROL,
                            # `upsert` face reluarea sigură: un fișier pe jumătate urcat la o
                            # cădere se rescrie, nu dă 409 și nu rămâne trunchiat.
                            "x-upsert": "true",
                        },
                        content=body,
                    )
                except httpx.HTTPError as exc:
                    async with lock:
                        failed += 1
                        print(f"[{WARN}] {key_path}: {type(exc).__name__}")
                    return
                async with lock:
                    if r.status_code in (200, 201):
                        done += 1
                        if done % 250 == 0:
                            print(f"[{OK}] {done}/{len(todo)}", flush=True)
                    else:
                        failed += 1
                        if failed <= 5:
                            print(f"[{WARN}] {key_path}: {r.status_code} {r.text[:120]}")

        await asyncio.gather(*(put(k, f) for k, f in todo))
        print(f"\n[{OK if not failed else WARN}] urcate {done}, esuate {failed}")
        return 1 if failed else 0


async def mark_hosted(dsn: str, slug: str, src: Path) -> int:
    """Rescrie `url` DOAR pentru imaginile principale care chiar există local (deci urcate).

    Rândurile de galerie rămân `storage='source'`: baza nu are voie să pretindă că găzduiește
    ceva ce n-a fost urcat.
    """
    url, _ = creds()
    base = f"{url}/storage/v1/object/public/{BUCKET}/{PREFIX}"
    have = {k.split("/")[1]: k.split("/")[-1] for k, _ in local_files(src)}

    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        biz = await conn.fetchval("select id from businesses where slug = $1", slug)
        if not biz:
            print(f"[{BAD}] tenantul `{slug}` nu exista")
            return 1
        rows = await conn.fetch(
            """select i.id, p.external_id from product_images i
               join products p on p.id = i.product_id
               where i.business_id = $1 and i.position = 0""",
            biz,
        )
        updates = [
            (r["id"], f"{base}/{r['external_id']}/{have[r['external_id']]}")
            for r in rows
            if r["external_id"] in have
        ]
        await conn.executemany(
            "update product_images set url = $2, storage = 'self' where id = $1", updates
        )
        print(f"[{OK}] {len(updates)} imagini marcate gazduite")
        for r in await conn.fetch(
            "select storage, count(*) n from product_images where business_id=$1 group by 1", biz
        ):
            print(f"        {r['storage']:14s} {r['n']:>6}")
        sample = await conn.fetchval(
            "select url from product_images where business_id=$1 and storage='self' limit 1", biz
        )
        print(f"\n  exemplu de URL: {sample}")
    finally:
        await conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--create-bucket", action="store_true")
    ap.add_argument("--upload", action="store_true")
    ap.add_argument("--mark-hosted", action="store_true")
    ap.add_argument("--src", type=Path, default=Path(r"D:\tmp\sole-images"))
    ap.add_argument("--slug", default="sole-ro")
    args = ap.parse_args()

    if args.create_bucket:
        return asyncio.run(create_bucket())
    if args.upload:
        if not args.src.exists():
            sys.exit(f"lipseste {args.src} — ruleaza intai prepare_sole_images.py --prepare")
        return asyncio.run(upload(args.src))
    if args.mark_hosted:
        dsn = os.environ.get("TARGET_DB_URL")
        if not dsn:
            sys.exit("TARGET_DB_URL lipseste")
        return asyncio.run(mark_hosted(dsn, args.slug, args.src))
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
