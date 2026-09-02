"""Scrie vocabularul de domeniu al unui tenant în `businesses.settings.domain_pack` — și,
înainte de asta, îl CONFRUNTĂ cu catalogul lui.

Pachetul e config, nu cod: `src/domain/loader.py` îl deep-merge-uiește peste default-ul
per-vertical și îl normalizează. Dar un pachet care nu se potrivește catalogului nu e o
configurare inertă, e o minciună tăcută: o nevoie fără produse promite ceva ce întoarce zero,
iar o expandare către un termen inexistent adaugă cuvinte care nu potrivesc nimic. De-asta
scriptul măsoară ÎNAINTE să scrie, și de-asta `--check` există separat: pachetul nu îmbătrânește
singur, catalogul se mișcă sub el.

Trei verificări, toate pe catalogul REAL al tenantului:

  1. **Se încarcă?** Pachetul trece prin `load_domain_pack` exact ca în producție. O fațetă
     invalidă e respinsă TĂCUT de loader (fail-closed, corect) — aici o vedem, fiindcă un pachet
     din care jumătate cade nu e un pachet.
  2. **Nevoile au marfă?** Fiecare cheie canonică din `concern_map` se caută în textul semantic
     al catalogului. Sub `--min-products` iese avertisment: nevoia e în vocabular, dar magazinul
     n-o poate servi.
  3. **Expandările potrivesc ceva?** Fiecare termen-țintă din `query_expansions` se caută în
     textul INDEXAT (nume + `ai_summary` + `description` — adică exact ce vede `search_tsv`,
     nu proza care nu e indexată).

DRY-RUN implicit: fără `--apply` nu scrie nimic.

    PACK=db/seed/domain_pack_sole_ro.json
    python scripts/set_domain_pack.py --business sole-ro --pack $PACK           # dry-run
    python scripts/set_domain_pack.py --business sole-ro --pack $PACK --apply   # scrie
    python scripts/set_domain_pack.py --business sole-ro --check   # confruntă ce e ÎN DB

Ieșire ≠ 0 dacă vreo verificare pică — deci se poate lega la un gate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import socket
import ssl
import sys
import types
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlparse

import asyncpg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

from src.domain.loader import load_domain_pack  # noqa: E402

DSN = os.environ.get("SUPABASE_DB_URL") or os.environ.get("DATABASE_URL")

SETTINGS_KEY = "domain_pack"

_SET_SQL = f"""
update businesses
   set settings = jsonb_set(coalesce(settings, '{{}}'::jsonb),
                            '{{{SETTINGS_KEY}}}', $2::jsonb, true)
 where id = $1
"""

# Textul SEMANTIC (secțiuni) spune ce nevoie servește produsul; textul INDEXAT (nume + ai_summary
# + description) e ce vede `search_tsv`. Sunt mulțimi DIFERITE, iar confuzia lor e exact greșeala
# care face o expandare să pară acoperită: „riduri" apare de 351 de ori în proza `aura` și de 42
# de ori în textul indexat, iar căutarea o vede doar pe a doua.
_SEMANTIC_SQL = """
select p.id::text as id,
       p.name || ' ' || coalesce(string_agg(s.body, ' '), '') as txt
  from products p
  left join product_sections s
         on s.product_id = p.id and s.business_id = p.business_id
 where p.business_id = $1
 group by p.id, p.name
"""

_INDEXED_SQL = """
select p.name || ' ' || coalesce(p.ai_summary, '') || ' ' || coalesce(p.description, '') as txt
  from products p
 where p.business_id = $1
"""


def fold(s: str) -> str:
    """lower + fără diacritice + doar litere/cifre/spații. Aceeași formă ca `domain.normalize`
    plus curățarea punctuației, ca potrivirea pe cuvânt întreg să fie posibilă."""
    nfkd = unicodedata.normalize("NFKD", (s or "").strip().lower())
    plain = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", plain).strip()


def _has_all_words(bag: set[str], phrase: str) -> bool:
    words = fold(phrase).split()
    return bool(words) and all(w in bag for w in words)


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
        statement_cache_size=0,
    )


async def _resolve_business(conn: asyncpg.Connection, ref: str) -> tuple[str, str, str] | None:
    row = await conn.fetchrow(
        "select id::text, slug, vertical from businesses where slug = $1 or id::text = $1", ref
    )
    return (row["id"], row["slug"], row["vertical"]) if row else None


def _load_through_real_loader(pack_json: dict, vertical: str):
    """Încarcă pachetul EXACT pe drumul de producție. Un `BusinessConfig` fals e suficient:
    `load_domain_pack` citește doar `vertical` + `settings` (P7 — zero atingere de DB)."""
    business = types.SimpleNamespace(vertical=vertical, settings={SETTINGS_KEY: pack_json})
    return load_domain_pack(business)


async def _check(conn, business_id: str, pack_json: dict, vertical: str, min_products: int) -> int:
    """Confruntă pachetul cu catalogul. Întoarce numărul de probleme (0 = curat)."""
    problems = 0
    pack = _load_through_real_loader(pack_json, vertical)

    # 1. Ce a supraviețuit încărcării. Loader-ul respinge fail-closed o fațetă invalidă și doar
    # loghează — deci diferența dintre „am declarat" și „s-a încărcat" e invizibilă în producție.
    declared_facets = {f.get("key") for f in (pack_json.get("facets") or []) if isinstance(f, dict)}
    loaded_facets = {f.key for f in pack.facets}
    print(
        f"încărcat   : {len(pack.concern_map)} fraze de nevoie → "
        f"{len(set(pack.concern_map.values()))} chei canonice, "
        f"{len(pack.facets)} fațete, {len(pack.query_expansions)} expandări, "
        f"{len(pack.relation_kinds.specs)} tipuri de relație"
    )
    if dropped := declared_facets - loaded_facets:
        print(f"  ! fațete RESPINSE de loader: {sorted(dropped)}")
        problems += len(dropped)
    declared_kinds = set(pack_json.get("relation_kinds") or {})
    if dropped_kinds := declared_kinds - set(pack.relation_kinds.specs):
        print(f"  ! tipuri de relație RESPINSE de loader: {sorted(dropped_kinds)}")
        problems += len(dropped_kinds)

    # 2. Nevoile au marfă în spate?
    sem = await conn.fetch(_SEMANTIC_SQL, business_id)
    bags = [set(fold(r["txt"]).split()) for r in sem]
    by_key: dict[str, list[str]] = {}
    for phrase, canonical in pack.concern_map.items():
        by_key.setdefault(canonical, []).append(phrase)
    print(f"\nnevoi      : {len(by_key)} chei canonice, măsurate pe {len(bags)} produse")
    for key in sorted(by_key, key=lambda k: -len(by_key[k])):
        phrases = by_key[key]
        hits = sum(1 for b in bags if any(_has_all_words(b, p) for p in phrases))
        flag = ""
        if hits < min_products:
            flag = f"   ! sub pragul de {min_products} produse"
            problems += 1
        print(f"  {key:<20}{hits:>6} produse   ({len(phrases)} fraze){flag}")

    # 3. Expandările potrivesc ceva în textul INDEXAT?
    if pack.query_expansions:
        idx = [set(fold(r["txt"]).split()) for r in await conn.fetch(_INDEXED_SQL, business_id)]
        targets = sorted({t for terms in pack.query_expansions.values() for t in terms})
        print(
            f"\nexpandări  : {len(pack.query_expansions)} fraze → {len(targets)} termeni-țintă, "
            f"măsurați pe textul INDEXAT"
        )
        for t in targets:
            hits = sum(1 for b in idx if _has_all_words(b, t))
            flag = ""
            if hits < min_products:
                flag = f"   ! sub pragul de {min_products} — expandarea nu potrivește nimic"
                problems += 1
            print(f"  {t:<24}{hits:>6} produse{flag}")
    else:
        print("\nexpandări  : niciuna (vezi `_query_expansions_note` din pachet)")

    return problems


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--business", required=True, help="slug sau business_id")
    ap.add_argument("--pack", help="fișier JSON cu pachetul (db/seed/domain_pack_*.json)")
    ap.add_argument("--check", action="store_true", help="confruntă pachetul din DB, nu un fișier")
    ap.add_argument("--min-products", type=int, default=10, help="pragul sub care se avertizează")
    ap.add_argument("--apply", action="store_true", help="chiar scrie (fără el: doar arată)")
    args = ap.parse_args()

    if not DSN:
        sys.stderr.write("EROARE: SUPABASE_DB_URL lipsește din .env\n")
        sys.exit(2)
    if not args.pack and not args.check:
        sys.stderr.write("EROARE: dă --pack <fișier> sau --check\n")
        sys.exit(2)

    conn = await connect()
    try:
        found = await _resolve_business(conn, args.business)
        if found is None:
            sys.stderr.write(f"EROARE: tenant inexistent: {args.business!r}\n")
            sys.exit(1)
        business_id, slug, vertical = found

        current = await conn.fetchval(
            "select settings -> $2 from businesses where id = $1", business_id, SETTINGS_KEY
        )
        current_json = json.loads(current) if isinstance(current, str) else current
        print(f"tenant     : {slug} ({business_id}), vertical `{vertical}`")
        print(f"în DB acum : {'pachet prezent' if current_json else '(absent)'}")

        if args.pack:
            pack_json = json.loads(Path(args.pack).read_text(encoding="utf-8"))
            print(f"fișier     : {args.pack}")
        else:
            if not current_json:
                sys.stderr.write("EROARE: --check dar tenantul n-are niciun pachet scris\n")
                sys.exit(1)
            pack_json = current_json

        problems = await _check(conn, business_id, pack_json, vertical, args.min_products)

        if not args.pack:
            print(f"\n{problems} problem(e).")
            sys.exit(1 if problems else 0)

        if not args.apply:
            print(f"\n{problems} problem(e). DRY-RUN: nimic nu s-a scris. Adaugă --apply.")
            sys.exit(1 if problems else 0)

        result = await conn.execute(_SET_SQL, business_id, json.dumps(pack_json))
        if int(result.split()[-1]) == 0:
            raise RuntimeError(f"0 rânduri actualizate pentru {business_id}")
        print(f"\nscris. ({problems} problem(e) raportate mai sus)")
        sys.exit(1 if problems else 0)
    finally:
        await conn.close()


asyncio.run(main())
