"""Audit READ-ONLY al coerenței catalogului demo — câte produse au nume↔categorie nepotrivite.

Context: la enrich live (3 produse) am văzut „Pensula de machiaj" în categoria „geluri de duș"
→ enrich-ul produce nonsens. ÎNAINTE de orice re-seed/re-enrich, măsurăm AMPLOAREA: câte
produse, ce tipare, ce categorii sunt cele mai sparte. NU scrie nimic (doar SELECT-uri).

Heuristică (precizie mare, recall parțial): din NUMELE produsului extragem „familia" tipului
(ser/cremă=îngrijire față, șampon=păr, pensulă=unelte, parfum=parfum, fond de ten=machiaj,
gel de duș=corp...) și din NUMELE CATEGORIEI la fel. Mismatch = ambele familii cunoscute și
DIFERITE. Familie necunoscută (nume/categorie generic) → nu numărăm (conservator, fără fals-poz).

    python scripts/audit_catalog_coherence.py            # raport pe tot catalogul demo
    python scripts/audit_catalog_coherence.py --examples 8
"""

import argparse
import asyncio
import os
import socket
import ssl
import sys
import unicodedata
from collections import Counter, defaultdict
from urllib.parse import unquote, urlparse

import asyncpg
from dotenv import load_dotenv

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv(".env")
DSN = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")
BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"

# Familie → fraze-cheie (normalizate, fără diacritice). Frazele MULTI-cuvânt întâi (precizie):
# „crema de fata" bate „crema" generic. Ordinea contează → cea mai specifică potrivire câștigă.
FAMILIES: dict[str, list[str]] = {
    "unelte": ["pensula", "burete machiaj", "buretel", "aplicator", "set pensule"],
    "parfum": ["apa parfumata", "apa de toaleta", "apa de parfum", "eau de", "parfum"],
    "par": [
        "sampon", "balsam de par", "masca de par", "vopsea", "fixativ", "spuma de par",
        "ulei de par", "tratament de par", "ser de par", "spray de par",
    ],
    "machiaj": [
        "fond de ten", "pudra", "ruj", "luciu de buze", "creion de", "rimel", "mascara",
        "fard", "paleta", "corector", "anticearcan", "iluminator", "primer", "tus de ochi",
    ],
    "ingrijire_corp": [
        "gel de dus", "lotiune de corp", "ulei de corp", "scrub de corp", "unt de corp",
        "crema de corp", "crema de maini", "sapun", "deodorant", "spuma de baie",
    ],
    "ingrijire_fata": [
        "ser", "contur ochi", "apa micelara", "tonic", "demachiant", "masca de fata",
        "crema de fata", "gel de curatare", "exfoliant", "crema hidratanta", "spf",
        "protectie solara", "crema anti", "crema de zi", "crema de noapte",
    ],
}


def _norm(s: str) -> str:
    d = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in d if not unicodedata.combining(c))


def _family(text: str) -> str | None:
    """Prima familie a cărei frază-cheie apare în text (normalizat). None = necunoscut."""
    t = _norm(text)
    for fam, phrases in FAMILIES.items():
        if any(ph in t for ph in phrases):
            return fam
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
    ap.add_argument("--examples", type=int, default=10, help="câte exemple de mismatch să listez")
    args = ap.parse_args()
    if not DSN:
        sys.exit("SUPABASE_DB_URL lipsește în .env")

    conn = await _connect()
    try:
        rows = await conn.fetch(
            """
            select p.id::text as id, p.name as name,
                   coalesce(c.name, '(fără categorie)') as category,
                   (p.attributes->>'enrich_v') as enrich_v
            from products p
            left join categories c on c.id = p.primary_category_id
            where p.business_id = $1
            order by category, name
            """,
            BIZ,
        )
    finally:
        await conn.close()

    total = len(rows)
    print(f"=== AUDIT COERENȚĂ CATALOG (business demo) — {total} produse ===\n")

    # enrich_v
    ev = Counter(r["enrich_v"] for r in rows)
    print("enrich_v:", dict(ev), "\n")

    # per categorie: count + familii ale numelor din ea
    by_cat: dict[str, list] = defaultdict(list)
    for r in rows:
        by_cat[r["category"]].append(r)

    print(f"Categorii: {len(by_cat)}\n")
    print("--- Per categorie: câte produse + ce FAMILII de nume conține (ideal: una singură) ---")
    fragmented = []
    for cat, items in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        cat_fam = _family(cat)
        name_fams = Counter(_family(it["name"]) or "?" for it in items)
        known = {k: v for k, v in name_fams.items() if k != "?"}
        distinct_known = len(known)
        tag = f"[cat→{cat_fam}]" if cat_fam else "[cat→?]"
        print(f"  {cat[:34]:34} n={len(items):3}  {tag:18} nume: {dict(name_fams)}")
        if distinct_known >= 2:
            fragmented.append((cat, distinct_known, len(items)))

    # mismatch: familia numelui ≠ familia categoriei (ambele cunoscute)
    mismatches = []
    cat_known = name_known = 0
    for r in rows:
        cf = _family(r["category"])
        nf = _family(r["name"])
        if cf:
            cat_known += 1
        if nf:
            name_known += 1
        if cf and nf and cf != nf:
            mismatches.append((r["name"], r["category"], nf, cf))

    print("\n--- REZUMAT ---")
    print(f"Produse cu CATEGORIE clasificabilă: {cat_known}/{total}")
    print(f"Produse cu NUME clasificabil:       {name_known}/{total}")
    classifiable = sum(1 for r in rows if _family(r["category"]) and _family(r["name"]))
    rate = (len(mismatches) / classifiable * 100) if classifiable else 0.0
    print(
        f"MISMATCH nume↔categorie (ambele clasificabile): {len(mismatches)}/{classifiable} "
        f"= {rate:.0f}%"
    )
    print(f"Categorii FRAGMENTATE (≥2 familii de nume): {len(fragmented)}/{len(by_cat)}")

    if mismatches:
        print(f"\n--- EXEMPLE mismatch (primele {args.examples}) ---")
        for name, cat, nf, cf in mismatches[: args.examples]:
            print(f"  „{name[:46]}" + "”")
            print(f"      nume={nf}  ÎN categoria „{cat}" + f"” (cat={cf})")

    # --- VERIFICARE: nume INTERN incoerent (tip unelte/parfum/păr + beneficiu de skincare-față) ---
    skin_benefit = [
        "hidrat", "calmar", "ridur", "anti-rid", "anti-aging", "luminoz", "uniformiz",
        "ten gras", "ten uscat", "ten sensibil", "acnee", "pete", "pori", "cearcan",
        "fermitate", "exfoliere", "anti-imbatranire",
    ]

    def _incoherent(name: str) -> bool:
        nf = _family(name)
        if nf not in ("unelte", "parfum", "par"):
            return False
        n = _norm(name)
        return "pentru" in n and any(b in n for b in skin_benefit)

    incoherent = [r for r in rows if _incoherent(r["name"])]
    coherent_core = [
        r for r in rows
        if _family(r["name"]) == "ingrijire_fata"
        and "pentru" in _norm(r["name"])
        and any(b in _norm(r["name"]) for b in skin_benefit)
    ]
    print("\n--- VERIFICARE NUME INTERN (tip ⊗ beneficiu) ---")
    print(f"INCOERENTE (unelte/parfum/păr + beneficiu de skincare-față): {len(incoherent)}/{total}")
    for r in incoherent[: args.examples]:
        print(f"    ✗ „{r['name'][:58]}”")
    print(f"\nNUCLEU COERENT (îngrijire-față + skincare): {len(coherent_core)}/{total}")
    for r in coherent_core[:6]:
        print(f"    ✓ „{r['name'][:58]}”")


if __name__ == "__main__":
    asyncio.run(main())
