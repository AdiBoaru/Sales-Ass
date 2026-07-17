"""NX-173 — backfill `attributes.not_recommended_for` din registrul curat de siguranță.

DE CE există, dacă gate-ul ține și fără el: gate-ul aplică regula de ingredient **la runtime**, în
cod. Backfill-ul mută același adevăr **în date**, unde e auditabil, exportabil și vizibil în PDP /
audit de catalog. Codul NU depinde de el — dacă nu-l rulezi niciodată, clientul e la fel de în
siguranță (doar că restricția nu e vizibilă în catalog).

GARANȚII:
  - **dry-run by default** — scrie DOAR cu `--apply` explicit;
  - **idempotent** — o intrare cu același `(value, rule_id)` nu se dublează la re-rulare; re-rularea
    după o schimbare de registru ACTUALIZEAZĂ intrarea (provenance/verified_at), nu adaugă alta;
  - **provenance INLINE** pe fiecare intrare scrisă (`source`/`source_ref`/`verified_at`/`rule_id`)
    — conform contractului v3 (NX-168d R8: `hard` fără proveniență completă = violation);
  - **nedistructiv** — intrările `not_recommended_for` scrise de om/furnizor (fără `rule_id`-ul
    nostru) NU se ating; scriem doar ce am pus noi;
  - **scoped pe `business_id`** (P7) — un tenant per rulare, explicit;
  - **audit_log** — o intrare per rulare aplicată (cine/ce/câte).

Rulare:
    PYTHONPATH=. python scripts/backfill_safety_flags.py --business <uuid>            # dry-run
    PYTHONPATH=. python scripts/backfill_safety_flags.py --business <uuid> --apply    # scrie
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db.connection import close_pool, tenant_conn  # noqa: E402
from src.safety.contraindications import _ingredient_text, load_registry  # noqa: E402

# Marcaj al intrărilor GENERATE de scriptul ăsta → le putem re-scrie/curăța fără să atingem
# intrările curate de om sau de furnizor (care n-au cheia asta).
_GEN_KEY = "rule_id"


def _planned_entries(product: dict[str, Any]) -> list[dict[str, Any]]:
    """Intrările `not_recommended_for` pe care registrul le cere pentru acest produs. Aceeași regulă
    de potrivire ca runtime-ul (import din modulul de siguranță — o singură sursă de adevăr)."""
    hay = _ingredient_text(product)
    out: list[dict[str, Any]] = []
    for rule in load_registry().rules:
        matched = next((p for p in rule.prefixes if p and p in hay), None)
        if not matched:
            continue
        for cid in sorted(rule.contexts):
            out.append(
                {
                    "value": cid,
                    "level": rule.level,
                    "source": rule.source,
                    "source_ref": rule.source_ref,
                    "verified_at": rule.verified_at,
                    "reviewed_by": rule.reviewed_by,
                    _GEN_KEY: rule.id,
                    "matched_on": matched,
                }
            )
    return out


def merge_entries(
    existing: list[Any] | None, planned: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], bool]:
    """`(lista finală, s-a schimbat ceva)`. Idempotent: intrările noastre (cu `rule_id`) se
    ÎNLOCUIESC pe cheia `(value, rule_id)`; orice altceva rămâne neatins, în ordine."""
    keep = [e for e in (existing or []) if not (isinstance(e, dict) and e.get(_GEN_KEY))]
    ours = [e for e in (existing or []) if isinstance(e, dict) and e.get(_GEN_KEY)]
    ours_by_key = {(e.get("value"), e.get(_GEN_KEY)): e for e in ours}
    planned_by_key = {(e["value"], e[_GEN_KEY]): e for e in planned}
    changed = ours_by_key != planned_by_key
    return keep + list(planned_by_key.values()), changed


async def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill not_recommended_for din registrul curat")
    ap.add_argument("--business", required=True, help="business_id (uuid) — un tenant per rulare")
    ap.add_argument("--apply", action="store_true", help="SCRIE (fără el: dry-run)")
    ap.add_argument("--limit", type=int, default=0, help="procesează doar primele N (debug)")
    args = ap.parse_args()

    reg = load_registry()
    if not reg.rules:
        print("EROARE: registrul de siguranță e gol — nimic de aplicat.")
        return 1
    print(f"Registru: {len(reg.rules)} regul(i) — {', '.join(r.id for r in reg.rules)}")
    print(f"Tenant:   {args.business}")
    print(f"Mod:      {'APPLY (scrie)' if args.apply else 'DRY-RUN (nu scrie nimic)'}\n")

    planned_rows: list[tuple[str, str, list[dict[str, Any]]]] = []
    async with tenant_conn(args.business) as conn:
        rows = await conn.fetch(
            "select id::text id, name, attributes from products where business_id = $1 "
            "order by name",
            args.business,
        )
        for r in rows:
            attrs = r["attributes"]
            attrs = json.loads(attrs) if isinstance(attrs, str) else (attrs or {})
            product = {"id": r["id"], "name": r["name"], "attributes": attrs}
            planned = _planned_entries(product)
            if not planned:
                continue
            merged, changed = merge_entries(attrs.get("not_recommended_for"), planned)
            if changed:
                planned_rows.append((r["id"], r["name"], merged))
            if args.limit and len(planned_rows) >= args.limit:
                break

        print(f"Produse scanate: {len(rows)} | de actualizat: {len(planned_rows)}\n")
        for pid, name, merged in planned_rows:
            ours = [e for e in merged if e.get(_GEN_KEY)]
            vals = ", ".join(f"{e['value']}({e['level']}, pe «{e['matched_on']}»)" for e in ours)
            print(f"  {name}\n    → {vals}")

        if not planned_rows:
            print("Nimic de făcut (idempotent: registrul e deja reflectat în date).")
            return 0
        if not args.apply:
            print(f"\nDRY-RUN: {len(planned_rows)} produse ar fi actualizate. Rulează cu --apply.")
            return 0

        async with conn.transaction():
            for pid, _name, merged in planned_rows:
                await conn.execute(
                    "update products set attributes = "
                    "  jsonb_set(coalesce(attributes,'{}'::jsonb), '{not_recommended_for}', $3) "
                    "where business_id = $1 and id = $2::uuid",
                    args.business,
                    pid,
                    json.dumps(merged),
                )
            await conn.execute(
                "insert into audit_log (business_id, actor, action, entity, entity_id, details) "
                "values ($1, $2, $3, $4, $5, $6)",
                args.business,
                "script:backfill_safety_flags",
                "backfill_not_recommended_for",
                "products",
                None,
                json.dumps(
                    {
                        "products_updated": len(planned_rows),
                        "rules": [r.id for r in reg.rules],
                        "registry_version": 1,
                    }
                ),
            )
        print(f"\nAPPLY: {len(planned_rows)} produse actualizate + audit_log scris.")
    return 0


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
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
