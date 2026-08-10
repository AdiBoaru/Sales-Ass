"""NX-226 — comparație READ-ONLY între rangul lexical vechi și cel nou, pe date reale.

De ce există: `lexical_rank_v2_enabled` schimbă ORDINEA candidaților lexicali. Ordinea nu se
argumentează din fotoliu (D15 — nicio schimbare de ranking pe speranță): scriptul rulează
aceleași query-uri pe ACELAȘI catalog, o dată cu formula veche și o dată cu cea nouă, și pune
top-urile alături. Decizia de aprindere se ia pe diff, nu pe teorie.

Ce NU face: nu scrie NIMIC (doar SELECT), nu cheamă niciun LLM (zero cost), nu atinge `WHERE` —
deci nici recall-ul. Verifică inclusiv asta: setul de id-uri din pool trebuie să fie identic
între cele două formule; dacă nu e, e un bug, nu o preferință, și scriptul o spune.

Utilizare:
    python scripts/lexical_rank_compare.py                     # tenantul demo, lista implicită
    python scripts/lexical_rank_compare.py --business-id <uuid> --queries-file q.txt
    python scripts/lexical_rank_compare.py --output reports/nx226-diff.md

Ieșire: 0 = a rulat (cu sau fără diferențe), 2 = eroare de utilizare/date (business inexistent,
fișier gol). Diferențele NU sunt eșec — ele sunt rezultatul.
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import get_settings  # noqa: E402
from src.db.connection import close_pool, tenant_conn  # noqa: E402
from src.db.queries.businesses import load_business  # noqa: E402
from src.db.queries.catalog import search_products_lexical  # noqa: E402

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"  # DOAR default de CLI, nu cod de producție

# Lista implicită: interogări RO reale ca FORMĂ, acoperind cele patru regimuri care contează
# pentru scală — frază naturală (FTS puternic), nume/brand scurt (trgm puternic), typo (doar
# trgm) și diacritice lipsă (NX-178). NB: `unmet_query` NU stochează textul brut al clientului
# (P12: doar atribute normalizate + locale), deci lista nu se poate genera din analytics —
# se scrie de mână din vocabularul demo și se rafinează cu `--queries-file`.
DEFAULT_QUERIES = [
    # frază naturală (aici trebuie să câștige FTS)
    "crema hidratanta pentru ten uscat",
    "sampon pentru par gras",
    "ser cu vitamina c pentru pete",
    "crema de fata cu protectie solara",
    "masca de par pentru par vopsit",
    "gel de curatare pentru ten gras",
    "crema de maini pentru piele uscata",
    "balsam de buze hidratant",
    # nume / brand scurt (aici trgm e legitim puternic)
    "fond de ten",
    "apa micelara",
    "acid hialuronic",
    "retinol",
    "niacinamida",
    # typo (plasa de siguranță trgm — nu are voie să dispară)
    "sanpon anti matreata",
    "crema hidratatna",
    "aci hialuronic",
    # diacritice lipsă vs prezente (aceeași marfă, NX-178)
    "șampon anticădere",
    "sampon anticadere",
    "cremă antirid",
    "crema antirid",
]


async def _top(conn, business_id: str, query: str, *, pool: int, top: int, v2: bool):
    """Rulează calea lexicală REALĂ cu flagul poziționat; întoarce (top-N, toate id-urile)."""
    get_settings().lexical_rank_v2_enabled = v2
    rows = await search_products_lexical(conn, business_id, query, pool=pool)
    return rows[:top], [str(r["id"]) for r in rows]


def _render(query: str, old, new, *, top: int) -> list[str]:
    """Un bloc side-by-side per query. `=` = aceeași poziție, `↕` = mutat, `+` = intrat în top."""
    old_ids = [str(r["id"]) for r in old]
    lines = [
        f"### {query}",
        "",
        "| # | vechi (ts_rank_cd + similarity) | nou (0.6/0.4 normalizat) |",
    ]
    lines.append("|---|---|---|")
    for i in range(top):
        o = old[i]["name"] if i < len(old) else ""
        if i < len(new):
            n_row = new[i]
            mark = (
                "="
                if i < len(old) and str(n_row["id"]) == old_ids[i]
                else ("↕" if str(n_row["id"]) in old_ids else "+")
            )
            n = f"{mark} {n_row['name']}"
        else:
            n = ""
        if not o and not n:
            continue
        lines.append(f"| {i + 1} | {o} | {n} |")
    lines.append("")
    return lines


async def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if sys.platform == "win32" and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    ap = argparse.ArgumentParser(description="NX-226: diff de ranking lexical (read-only)")
    ap.add_argument("--business-id", default=DEMO_BIZ)
    ap.add_argument(
        "--queries-file",
        type=pathlib.Path,
        default=None,
        help="un query per linie; liniile goale și cele cu # se ignoră",
    )
    ap.add_argument("--pool", type=int, default=50, help="mărimea pool-ului lexical (ca în prod)")
    ap.add_argument("--top", type=int, default=6, help="câte poziții se compară")
    ap.add_argument("--output", type=pathlib.Path, default=None, help="scrie raportul markdown")
    args = ap.parse_args()

    queries = DEFAULT_QUERIES
    if args.queries_file:
        if not args.queries_file.exists():
            print(f"EROARE: fișierul {args.queries_file} nu există", file=sys.stderr)
            return 2
        queries = [
            ln.strip()
            for ln in args.queries_file.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")
        ]
        if not queries:
            print(f"EROARE: {args.queries_file} nu conține niciun query", file=sys.stderr)
            return 2

    out: list[str] = [
        f"# NX-226 — diff de ranking lexical · business `{args.business_id}`",
        "",
        f"{len(queries)} query-uri · pool={args.pool} · top={args.top} · READ-ONLY, zero LLM.",
        "",
    ]
    changed = 0
    recall_breaks: list[str] = []

    try:
        async with tenant_conn(args.business_id) as conn:
            biz = await load_business(conn, args.business_id)
            if biz is None:
                print(
                    f"EROARE: business `{args.business_id}` nu există (zero scrieri efectuate)",
                    file=sys.stderr,
                )
                return 2

            for q in queries:
                old, old_all = await _top(
                    conn, args.business_id, q, pool=args.pool, top=args.top, v2=False
                )
                new, new_all = await _top(
                    conn, args.business_id, q, pool=args.pool, top=args.top, v2=True
                )

                # Contractul cardului: se schimbă DOAR ordinea. Set diferit = bug de `WHERE`.
                # ATENȚIE la falsul pozitiv: când match-ul depășește `pool`, `LIMIT` taie DUPĂ
                # sortare, deci două ordini diferite întorc legitim ultimele rânduri diferite.
                # Verificăm egalitatea de set doar când pool-ul NU e plin.
                truncated = len(old_all) >= args.pool or len(new_all) >= args.pool
                if not truncated and set(old_all) != set(new_all):
                    recall_breaks.append(q)

                same_top = [str(r["id"]) for r in old] == [str(r["id"]) for r in new]
                if not same_top:
                    changed += 1
                flag = "  (identic)" if same_top else "  DIFERIT"
                print(f"{q:42.42} vechi={len(old_all):3d} nou={len(new_all):3d}{flag}")
                out += _render(q, old, new, top=args.top)
    finally:
        get_settings().lexical_rank_v2_enabled = False  # nu lăsăm procesul cu flagul aprins
        await close_pool()

    summary = f"**{changed}/{len(queries)} query-uri cu top-{args.top} schimbat.**"
    if recall_breaks:
        summary += (
            f"\n\n⚠ RECALL SCHIMBAT pe {len(recall_breaks)} query-uri "
            f"({', '.join(recall_breaks[:5])}) — formula nouă n-are voie să atingă `WHERE`; "
            "asta e un bug, nu o preferință."
        )
    out.insert(3, summary)
    out.insert(4, "")
    print("\n" + summary)

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text("\n".join(out), encoding="utf-8")
        print(f"raport: {args.output}")
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    raise SystemExit(asyncio.run(main()))
