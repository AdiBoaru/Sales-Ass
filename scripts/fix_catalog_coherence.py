"""Fix ȚINTIT de coerență (opțiunea C) — arhivează produsele cu nume INTERN nonsens.

Tip ⊗ beneficiu imposibil: pensulă/parfum + beneficiu de skincare-față („Pensula de machiaj
pentru calmare", „Apa parfumata pentru uniformizare"), sau șampon/balsam + beneficiu DOAR de
față (șampon „pentru riduri/luminozitate"). Astea fac enrich-ul să producă nonsens („pensulă cu
acid hialuronic") și strică demo-ul.

Acțiune: status 'active' → 'archived' (le scoate din search; search filtrează status='active').
NU ȘTERGE — reversibil 100%: status înapoi pe 'active'. Idempotent (re-rulare = aceleași).
Conservator: NU atinge produsele coerente (ser/cremă + skincare) și nici șampon „pentru hidratare/
calmare" (legitim pentru păr). Familie necunoscută → neatins.

    python scripts/fix_catalog_coherence.py            # DRY-RUN: doar listează ce ar arhiva
    python scripts/fix_catalog_coherence.py --apply     # arhivează + printează lista de revert
    python scripts/fix_catalog_coherence.py --revert     # re-activează tot ce e 'archived' (undo)
"""

import argparse
import asyncio
import os
import socket
import ssl
import sys
import unicodedata
from collections import Counter
from urllib.parse import unquote, urlparse

import asyncpg
from dotenv import load_dotenv

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv(".env")
DSN = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"

FAMILIES: dict[str, list[str]] = {
    "unelte": ["pensula", "burete machiaj", "buretel", "aplicator", "set pensule"],
    "parfum": ["apa parfumata", "apa de toaleta", "apa de parfum", "eau de", "parfum"],
    "par": [
        "sampon",
        "balsam de par",
        "masca de par",
        "vopsea",
        "fixativ",
        "spuma de par",
        "ulei de par",
        "tratament de par",
        "ser de par",
        "spray de par",
        "balsam",
    ],
}
# Beneficii de skincare-față (pe ORICE produs non-skincare = nonsens).
SKIN_FACE = [
    "hidratare",
    "calmare",
    "riduri",
    "anti-rid",
    "anti-aging",
    "anti-imbatranire",
    "luminozitate",
    "uniformizare",
    "acnee",
    "pete",
    "pori",
    "cearcan",
    "fermitate",
    "exfoliere",
    "ten gras",
    "ten uscat",
    "ten sensibil",
    "ten mixt",
    "ten normal",
]
# Beneficii care NU pot fi de păr (deci șampon „pentru" ele = nonsens). Exclude hidratare/calmare/
# volum/fermitate (legitime pentru păr).
SKIN_ONLY = [
    "riduri",
    "anti-rid",
    "anti-aging",
    "anti-imbatranire",
    "luminozitate",
    "uniformizare",
    "acnee",
    "pete",
    "pori",
    "cearcan",
    "exfoliere",
    "ten gras",
    "ten uscat",
    "ten sensibil",
    "ten mixt",
    "ten normal",
]


def _norm(s: str) -> str:
    d = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in d if not unicodedata.combining(c))


def _family(text: str) -> str | None:
    t = _norm(text)
    for fam, phrases in FAMILIES.items():
        if any(ph in t for ph in phrases):
            return fam
    return None


def _is_nonsense(name: str) -> str | None:
    """Întoarce motivul (familia) dacă numele e tip⊗beneficiu imposibil, altfel None."""
    nf = _family(name)
    n = _norm(name)
    if "pentru" not in n:
        return None
    if nf in ("unelte", "parfum") and any(b in n for b in SKIN_FACE):
        return nf
    if nf == "par" and any(b in n for b in SKIN_ONLY):
        return "par"
    return None


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


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="chiar scrie (status → archived)")
    ap.add_argument("--revert", action="store_true", help="re-activează tot ce e 'archived'")
    args = ap.parse_args()
    if not DSN:
        sys.exit("SUPABASE_DB_URL lipsește în .env")

    conn = await _connect()
    try:
        if args.revert:
            n = await conn.fetchval(
                "with u as (update products set status='active' "
                "where business_id=$1 and status='archived' returning 1) select count(*) from u",
                BIZ,
            )
            print(f"REVERT: {n} produse re-activate (archived → active).")
            return

        rows = await conn.fetch(
            "select id::text as id, name, status from products where business_id=$1 order by name",
            BIZ,
        )
        targets = [(r["id"], r["name"], _is_nonsense(r["name"])) for r in rows]
        targets = [(i, n, why) for i, n, why in targets if why and rows]
        # exclude ce e deja arhivat (idempotent)
        active = {r["id"]: r["status"] for r in rows}
        targets = [(i, n, why) for i, n, why in targets if active.get(i) == "active"]

        by_fam = Counter(why for _, _, why in targets)
        print(f"=== FIX COERENȚĂ (dry-run={not args.apply}) — {len(rows)} produse în catalog ===")
        print(f"De ARHIVAT (nume tip⊗beneficiu imposibil): {len(targets)}")
        print(f"  pe familie: {dict(by_fam)}\n")
        for i, name, why in targets:
            print(f"  [{why:7}] {name[:60]}")
        print()

        if not args.apply:
            print("DRY-RUN: nimic scris. Rulează cu --apply ca să arhivezi. Undo: --revert.")
            return

        ids = [i for i, _, _ in targets]
        if ids:
            await conn.execute(
                "update products set status='archived' "
                "where business_id=$1 and id = any($2::uuid[])",
                BIZ,
                ids,
            )
        print(f"APLICAT: {len(ids)} produse arhivate (status → 'archived').")
        print("Undo complet: python scripts/fix_catalog_coherence.py --revert")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
