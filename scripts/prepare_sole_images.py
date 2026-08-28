"""Pregătește imaginile SOLE pentru găzduire proprie și marchează în DB ce e găzduit.

Trei pași, deliberat separați, fiindcă doar al doilea are nevoie de acces la VPS:

  1. `--prepare`      construiește local arborele care se urcă (o poză per produs, ORIGINAL)
  2. (manual)         `rsync` pe VPS
  3. `--mark-hosted`  rescrie `product_images.url` și pune `storage='self'`

De ce pasul 3 e separat și vine DUPĂ upload: `storage` spune unde e fișierul cu adevărat. Dacă
l-am marca `self` înainte de a-l urca, baza ar afirma un lucru fals, iar widgetul ar cere o poză
care nu există. Ordinea „mai întâi realitatea, apoi înregistrarea ei" e aceeași peste tot în
proiect (vezi `commerce_action_receipts`, NX-237).

Se urcă DOAR imaginea principală: 2.758 de fișiere, ~403 MB. Galeria completă (15.487 fișiere,
4,78 GB) rămâne în arhiva locală; rândurile ei sunt DEJA în `product_images` cu `storage='source'`,
deci un al doilea val de upload e un `update`, nu un re-import.

Rulare:
    python scripts/prepare_sole_images.py --prepare --out D:\\tmp\\sole-images
    # rsync -av D:/tmp/sole-images/ user@vps:/srv/nativx/images/sole/
    TARGET_DB_URL=... python scripts/prepare_sole_images.py --mark-hosted \\
        --base-url https://img.nativextech.com/sole
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sqlite3
import sys
from pathlib import Path

import asyncpg

SOURCE_DB = os.environ.get("SOLE_SOURCE_DB", r"D:\Work\SOLE SCRIPT\sole_data.db")
OK, WARN, BAD = "  ok  ", " ATENTIE ", " ESUAT "


def primary_images() -> list[tuple[str, str, str]]:
    """(sku, cale_locala, url_sursa) pentru PRIMA imagine a fiecărui produs.

    „Prima" e `min(images.id)`, adică ordinea în care le-a găsit scraperul pe pagină, care e
    ordinea din galeria magazinului. Nu se alege după nume de fișier: folderele au scheme
    diferite (`43323E`, `4524`, `AQ-102`), deci `1.webp` nu există peste tot.
    """
    conn = sqlite3.connect(f"file:{SOURCE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """select p.memox_code, p.sku, i.local_path, i.url
           from images i
           join products p on p.id = i.product_id
           where i.id = (select min(i2.id) from images i2 where i2.product_id = i.product_id)
             and p.name is not null and p.price_regular is not null"""
    ).fetchall()
    conn.close()
    return [(r["memox_code"] or r["sku"], r["local_path"], r["url"]) for r in rows]


def prepare(out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    copied = missing = 0
    total_bytes = 0
    for sku, local_path, _url in primary_images():
        src = Path(local_path)
        if not src.exists():
            missing += 1
            continue
        dst = out_dir / sku / f"1{src.suffix.lower()}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            shutil.copy2(src, dst)
        total_bytes += dst.stat().st_size
        copied += 1

    print(f"[{OK}] {copied} imagini pregatite in {out_dir}  ({total_bytes / 1e6:.0f} MB)")
    if missing:
        print(f"[{WARN}] {missing} fisiere lipsesc din arhiva locala")
    print("\nUrmatorul pas (manual, are nevoie de VPS):")
    print(f"  rsync -av --progress '{out_dir.as_posix()}/' user@VPS:/srv/nativx/images/sole/")
    return 0


async def mark_hosted(dsn: str, slug: str, base_url: str) -> int:
    """Rescrie `url` DOAR pentru imaginile principale, și doar dacă fișierul chiar există local.

    Rândurile de galerie rămân `storage='source'` cu URL-ul magazinului: baza nu are voie să
    pretindă că găzduiește ceva ce n-a fost urcat.
    """
    have = {sku: Path(p).suffix.lower() for sku, p, _ in primary_images() if Path(p).exists()}
    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        biz = await conn.fetchval("select id from businesses where slug = $1", slug)
        if not biz:
            print(f"[{BAD}] tenantul `{slug}` nu exista")
            return 1
        rows = await conn.fetch(
            """select i.id, p.external_id, i.url
               from product_images i join products p on p.id = i.product_id
               where i.business_id = $1 and i.position = 0""",
            biz,
        )
        updates = [
            (r["id"], f"{base_url.rstrip('/')}/{r['external_id']}/1{have[r['external_id']]}")
            for r in rows
            if r["external_id"] in have
        ]
        await conn.executemany(
            "update product_images set url = $2, storage = 'self' where id = $1", updates
        )
        print(f"[{OK}] {len(updates)} imagini marcate gazduite de noi")
        for r in await conn.fetch(
            "select storage, count(*) n from product_images where business_id = $1 group by 1", biz
        ):
            print(f"        {r['storage']:14s} {r['n']:>6}")
    finally:
        await conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prepare", action="store_true")
    ap.add_argument("--out", type=Path, default=Path(r"D:\tmp\sole-images"))
    ap.add_argument("--mark-hosted", action="store_true")
    ap.add_argument("--slug", default="sole-ro")
    ap.add_argument("--base-url", default="https://img.nativextech.com/sole")
    args = ap.parse_args()

    if not Path(SOURCE_DB).exists():
        sys.stderr.write(f"sursa lipseste: {SOURCE_DB}\n")
        return 2
    if args.prepare:
        return prepare(args.out)
    if args.mark_hosted:
        dsn = os.environ.get("TARGET_DB_URL")
        if not dsn:
            sys.stderr.write("TARGET_DB_URL lipseste\n")
            return 2
        return asyncio.run(mark_hosted(dsn, args.slug, args.base_url))
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
