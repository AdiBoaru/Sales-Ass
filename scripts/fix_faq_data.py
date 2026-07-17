"""NX-175 — repară DATELE de FAQ: typo + duplicate. Idempotent, dry-run default.

Separat de reranking (cod): reranking-ul evită FAQ-ul prost la selecție, dar typo-ul/duplicatul
rămân în DB și se văd dacă clientul întreabă EXACT de excepție. Astea-s probleme de DATE.

Ce repară (auditat live 2026-07-17 pe tenantul demo):
  1. TYPO „14 zile. calendaristice." → „14 zile calendaristice." (punct în mijlocul frazei),
     în răspunsurile excepției de retur;
  2. DUPLICATE — „Pot returna un produs desfăcut?" ≡ „Pot returna un produs cosmetic desigilat sau
     deschis?" au răspuns IDENTIC. NU ștergem un FAQ (ambele întrebări sunt formulări reale pe care
     clienții le pun) — le PĂSTRĂM pe amândouă, dar răspunsul (corectat) rămâne consistent.
     Reranking-ul le tratează oricum ca duplicate (nu clarifică între răspunsuri identice).

GARANȚII:
  - dry-run by default — arată diff-ul; scrie DOAR cu `--apply`;
  - idempotent — a doua rulare nu mai găsește nimic de reparat;
  - scoped pe `--business` + `locale` (P7/P11); doar rânduri care CHIAR au typo-ul (match exact);
  - re-embed DOAR rândurile schimbate (răspunsul intră în embedding via seed? — NU: FAQ-ul
    embed-uiește ÎNTREBAREA, nu răspunsul, deci corectarea răspunsului NU cere re-embed. Verificat
    în src/jobs/seed_faqs.py). Dacă asta se schimbă, adaugă re-embed aici.
  - audit_log per rulare aplicată.

Rulare:
    PYTHONPATH=. python scripts/fix_faq_data.py                    # dry-run
    PYTHONPATH=. python scripts/fix_faq_data.py --apply            # scrie
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

from src.db.connection import admin_conn, close_pool, get_pool  # noqa: E402

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"

# (substring greșit, corect) — match EXACT, idempotent: după fix, substringul greșit nu mai există.
_TYPO_FIXES: tuple[tuple[str, str], ...] = (("14 zile. calendaristice", "14 zile calendaristice"),)


async def _find(conn, business_id: str) -> list[dict]:
    """Rândurile FAQ cu un typo cunoscut (orice locale — typo-ul e independent de limbă)."""
    rows = await conn.fetch(
        "select id::text id, question, answer, locale from faqs where business_id = $1", business_id
    )
    out = []
    for r in rows:
        answer = r["answer"] or ""
        fixed = answer
        for bad, good in _TYPO_FIXES:
            fixed = fixed.replace(bad, good)
        if fixed != answer:
            out.append({**dict(r), "answer_fixed": fixed})
    return out


async def main() -> int:
    ap = argparse.ArgumentParser(description="Repară typo/duplicate în faqs (idempotent, dry-run)")
    ap.add_argument("--business", default=DEMO_BIZ, help=f"business_id (default: {DEMO_BIZ})")
    ap.add_argument("--apply", action="store_true", help="SCRIE (fără el: dry-run)")
    args = ap.parse_args()

    # `faqs` e knowledge: `bot_runtime` (tenant_conn) are DOAR SELECT → scrierea cere ADMIN, ca
    # `seed_faqs.py`. admin_conn bypass-ează RLS → fiecare query PĂSTREAZĂ `WHERE business_id=$1`
    # explicit (P7, defense-in-depth).
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        targets = await _find(conn, args.business)
        print(f"Tenant: {args.business}")
        print(f"Mod:    {'APPLY (scrie)' if args.apply else 'DRY-RUN (nu scrie)'}\n")
        if not targets:
            print("Nimic de reparat (idempotent: typo-urile cunoscute nu mai există).")
            return 0
        for t in targets:
            print(f"  [{t['locale']}] {t['question']}")
            print(f"      - {t['answer'][:88]}")
            print(f"      + {t['answer_fixed'][:88]}")
        print(f"\n{len(targets)} rând(uri) de corectat.")
        if not args.apply:
            print("\nDRY-RUN: nimic nu s-a scris. Rulează cu --apply.")
            return 0
        async with conn.transaction():
            for t in targets:
                await conn.execute(
                    "update faqs set answer = $3 where business_id = $1 and id = $2::uuid",
                    args.business,
                    t["id"],
                    t["answer_fixed"],
                )
            await conn.execute(
                "insert into audit_log (business_id, actor, action, entity, entity_id, details) "
                "values ($1, $2, $3, $4, $5, $6)",
                args.business,
                "script:fix_faq_data",
                "fix_faq_typo",
                "faqs",
                None,
                __import__("json").dumps({"rows": len(targets), "ids": [t["id"] for t in targets]}),
            )
        print(f"\nAPPLY: {len(targets)} rând(uri) corectate + audit_log scris.")
    return 0


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    async def _run() -> int:
        try:
            return await main()
        finally:
            await close_pool()

    raise SystemExit(asyncio.run(_run()))
