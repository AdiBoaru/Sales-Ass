"""Scoate etichetele unei locale din `businesses.settings.domain_pack`.

De ce e un script și nu o migrare: pachetul de domeniu e DATE de tenant într-un `jsonb`, nu
schemă. O migrare ar trebui să știe ce locale are fiecare tenant și ar rula o dată; scriptul e
idempotent și se poate rula pe tenantul care chiar are nevoie.

De ce e GENERIC (`--locale`) și nu „drop_hu": principiul D3 spune că nucleul rămâne locale-aware
chiar dacă pilotul e `ro`. Un script care știe numele unei singure limbi ar fi exact felul de
hardcodare pe care o evităm — data viitoare când se retrage o locale, se schimbă argumentul, nu
codul.

Poarta: refuză o locale care e ÎNCĂ declarată în `SUPPORTED_LOCALES` sau în `supported_locales`
al tenantului. Altfel scriptul ar șterge tăcut traduceri pe care sistemul chiar le servește, iar
`normalize_locale` le-ar înlocui cu româna fără ca nimeni să observe.

Dry-run implicit; `--apply` scrie.

    python -m scripts.prune_pack_locale --business <uuid> --locale hu
    python -m scripts.prune_pack_locale --business <uuid> --locale hu --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from src.db.connection import admin_conn, close_pool, get_pool
from src.web.localization import SUPPORTED_LOCALES


def prune(node: Any, locale: str) -> tuple[Any, int]:
    """Scoate recursiv cheia `locale` din orice dict. Funcție PURĂ; întoarce (nod_nou, câte)."""
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        removed = 0
        for key, value in node.items():
            if key == locale:
                removed += 1
                continue
            new_value, count = prune(value, locale)
            out[key] = new_value
            removed += count
        return out, removed
    if isinstance(node, list):
        items: list[Any] = []
        removed = 0
        for value in node:
            new_value, count = prune(value, locale)
            items.append(new_value)
            removed += count
        return items, removed
    return node, 0


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--business", required=True)
    ap.add_argument("--locale", required=True, help="subtagul de scos, ex. `hu`")
    ap.add_argument("--apply", action="store_true", help="scrie (implicit: dry-run)")
    args = ap.parse_args()

    locale = args.locale.strip().lower()
    if locale in SUPPORTED_LOCALES:
        print(
            f"REFUZ: `{locale}` e încă în SUPPORTED_LOCALES {SUPPORTED_LOCALES}. "
            "Scoate-o întâi din cod, altfel ai șterge traduceri care se servesc.",
            file=sys.stderr,
        )
        return 2

    pool = await get_pool()
    try:
        async with admin_conn(pool) as conn:
            row = await conn.fetchrow(
                "select slug, supported_locales, settings from businesses where id = $1",
                args.business,
            )
            if row is None:
                print(f"REFUZ: business {args.business} inexistent", file=sys.stderr)
                return 2
            declared = list(row["supported_locales"] or [])
            if locale in declared:
                print(
                    f"REFUZ: tenantul `{row['slug']}` declară `{locale}` în supported_locales "
                    f"{declared}. Retrage-o întâi de acolo.",
                    file=sys.stderr,
                )
                return 2

            raw = row["settings"]
            settings = raw if isinstance(raw, dict) else (json.loads(raw) if raw else {})
            pruned, removed = prune(settings, locale)

            print(f"business: {row['slug']} ({args.business})")
            print(f"supported_locales: {declared}")
            print(f"chei `{locale}` găsite în settings: {removed}")
            if removed == 0:
                print("nimic de făcut (idempotent)")
                return 0
            if not args.apply:
                print("\n(dry-run — rulează cu --apply ca să scrii)")
                return 0

            await conn.execute(
                "update businesses set settings = $2::jsonb where id = $1",
                args.business,
                json.dumps(pruned, ensure_ascii=False),
            )
            print(f"\nscris: {removed} chei `{locale}` scoase din settings")
            return 0
    finally:
        await close_pool()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
