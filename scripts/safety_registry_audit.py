"""NX-367 — auditul registrului de siguranță pe catalogul REAL: ce scapă regula de retinoizi.

    python scripts/safety_registry_audit.py --business sole-ro

Read-only, 0 $. Pentru fiecare produs activ caută, în numele și în listele de ingrediente (exact
textul pe care îl scanează regula, `contraindications._ingredient_text`), termenii familiei
vitaminei A de mai jos, și raportează produsele care îi poartă dar NU sunt blocate în sarcină.
De ce: registrul e scris de mână, iar o formă nouă pe etichetă (pe 2026-10-01, „hydroxypinacolone
retinoate", HPR) trecea neobservată: serul DR.REJU-ALL Retino-Mela era recomandabil în sarcină.

Lista de termeni de AUDIT e intenționat mai largă decât regula: un rezultat al auditului e o
întrebare pentru om (adaug forma în registru?), nu o excludere automată. Iese 1 dacă rămân scăpări.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: Familia vitaminei A, în formele de pe etichete (INCI + RO). Pe text normalizat (fără diacritice).
AUDIT_TERMS = (
    r"\bretin(?!er)",  # nu „reținere" fără diacritice (găsit pe un filtru de duș)
    r"\btretinoin",
    r"\bizotretinoin",
    r"\bisotretinoin",
    r"\badapalen",
    r"\btazaroten",
    r"\bhydroxypinacolon",
    r"\bhpr\b",
    r"\bvitamina a\b",
    r"\bvitamin a\b",
    r"\bacid retinoic",
    r"\bretinoic acid",  # domain-leak: ok — denumire INCI pe etichetă, termenul auditului P0
)

SQL = """
select p.id::text as id, p.name, p.availability, p.attributes
  from products p
 where p.business_id = $1::uuid and p.status = 'active'
"""


def _attrs(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return {}
    return dict(raw or {})


def audit(products: list[dict[str, Any]], contexts: frozenset[str]) -> dict[str, Any]:
    """PUR pe produse + registrul încărcat: cine poartă un termen al familiei și cine scapă."""
    from src.safety import contraindications as ci  # noqa: PLC0415

    pattern = re.compile("|".join(AUDIT_TERMS))
    flagged, escaped = [], []
    for p in products:
        text = ci._ingredient_text(p)
        hit = pattern.search(text)
        if not hit:
            continue
        flagged.append(p["id"])
        if ci.check_product(p, contexts) is None:
            escaped.append(
                {
                    "id": p["id"],
                    "name": str(p.get("name") or "")[:90],
                    "availability": p.get("availability"),
                    "term": hit.group(0),
                }
            )
    return {"with_terms": len(flagged), "escaped": escaped}


async def _main(business: str) -> dict[str, Any]:
    from src.db.connection import admin_conn, close_pool, get_pool, tenant_conn  # noqa: PLC0415

    pool = await get_pool()
    try:
        async with admin_conn(pool) as conn:
            bid = await conn.fetchval(
                "select id::text from businesses where slug = $1 or id::text = $1", business
            )
        if bid is None:
            raise SystemExit(f"tenant necunoscut: {business!r}")
        async with tenant_conn(bid) as conn:
            rows = [dict(r) for r in await conn.fetch(SQL, bid)]
    finally:
        await close_pool()
    for r in rows:
        r["attributes"] = _attrs(r["attributes"])
    report = audit(rows, frozenset({"pregnancy"}))
    report["products"] = len(rows)
    return report


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--business", required=True)
    args = p.parse_args(argv)
    report = asyncio.run(_main(args.business))
    print(
        f"{report['products']} produse active · {report['with_terms']} poartă un termen al "
        f"familiei vitaminei A · {len(report['escaped'])} NU sunt blocate în sarcină"
    )
    for e in report["escaped"]:
        print(f"  [{e['availability']}] {e['name']}  (termen: {e['term']})")
    return 1 if report["escaped"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
