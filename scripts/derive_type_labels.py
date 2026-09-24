"""NX-325 — propune ETICHETE cu diacritice pentru cheile `product_type`. Read-only, zero model.

Cheile sunt derivate din nume de catalog (NX-271) și n-au diacritice: «ulei de curatare», «masca
de fata». Puse direct în încadrarea serverului („Ți-am ales ulei de curatare…") arată a bază de
date (P13). Eticheta se caută în textul produselor (descrieri + secțiuni de fișă): fiecare șir de
cuvinte care, fără diacritice, e exact cheia, iar forma cea mai frecventă CU diacritice devine
propunerea.

Restaurarea diacriticelor nu e deterministă în general („fata" poate fi „față" sau „fată"), deci
ieșirea e o PROPUNERE cu numărul de apariții ca dovadă, revizuită de un om înainte să intre în
fișierul sursă al pachetului (`db/seed/domain_pack_<tenant>.json`, intrarea `product_type` din
`comparison_facets`, cu `"in_comparison": false`). Cheile fără nicio apariție cu diacritice rămân
fără etichetă și sunt listate separat.

    PYTHONPATH=. python scripts/derive_type_labels.py [--business sole-ro] [--locale ro]

Scrie `reports/nx325/type_labels.proposed.json`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import types
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import load_dotenv

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

from src.catalog.folding import fold_text  # noqa: E402
from src.db.connection import admin_conn, close_pool, get_pool  # noqa: E402
from src.domain.loader import load_domain_pack  # noqa: E402

_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def propose(keys: list[str], texts: list[str]) -> dict[str, dict]:
    """Pentru fiecare cheie: formele din text care, pliate, sunt exact cheia. PURĂ."""
    by_len: dict[int, set[str]] = defaultdict(set)
    for key in keys:
        by_len[len(key.split())].add(key)
    seen: dict[str, Counter] = defaultdict(Counter)
    for text in texts:
        words = _WORD.findall(text or "")
        for n, wanted in by_len.items():
            for i in range(len(words) - n + 1):
                chunk = words[i : i + n]
                folded = " ".join(fold_text(w) for w in chunk)
                if folded in wanted:
                    seen[folded][" ".join(w.lower() for w in chunk)] += 1
    out: dict[str, dict] = {}
    for key in keys:
        variants = seen.get(key, Counter())
        accented = [(form, n) for form, n in variants.most_common() if form != key]
        if accented:
            label, count = accented[0]
        elif variants.get(key):
            # Forma simplă apare, iar nicio variantă cu diacritice nu: cuvântul e corect așa
            # («balsam de buze», «fond de ten», «ruj»), nu o etichetă lipsă.
            label, count = key, variants[key]
        else:
            label, count = None, 0
        out[key] = {"label": label, "count": count, "variants": dict(variants.most_common(4))}
    return out


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--business", default="sole-ro")
    ap.add_argument("--locale", default="ro")
    args = ap.parse_args()

    pool = await get_pool()
    async with admin_conn(pool) as conn:
        biz = await conn.fetchrow(
            "select id::text as id, vertical, settings from businesses where slug = $1",
            args.business,
        )
        if biz is None:
            raise SystemExit(f"tenant necunoscut: {args.business}")
        settings = biz["settings"]
        pack = load_domain_pack(
            types.SimpleNamespace(
                vertical=biz["vertical"],
                settings=json.loads(settings) if isinstance(settings, str) else settings,
            )
        )
        facet = next((f for f in pack.facets if f.key == "product_type"), None)
        if facet is None or not facet.values:
            raise SystemExit("pachetul n-are fațeta `product_type` cu valori declarate")
        # Descrierea comerciantului SOLE n-are diacritice deloc; fișele `aura` din
        # `product_sections` au. Ambele intră, iar numărătoarea decide.
        rows = await conn.fetch(
            "select coalesce(description, '') as d from products "
            "where business_id = $1 and status = 'active' "
            "union all select coalesce(body, '') from product_sections where business_id = $1",
            biz["id"],
        )
    await close_pool()

    proposal = propose(list(facet.values), [r["d"] for r in rows])
    missing = sorted(k for k, v in proposal.items() if not v["label"])
    entry = {
        "key": "product_type",
        "in_comparison": False,
        "value_labels": {
            k: {args.locale: v["label"]} for k, v in sorted(proposal.items()) if v["label"]
        },
    }
    out = Path("reports/nx325/type_labels.proposed.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            {"business": args.business, "proposal": proposal, "missing": missing, "entry": entry},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"chei: {len(proposal)} · cu etichetă propusă: {len(proposal) - len(missing)}")
    for key, info in sorted(proposal.items()):
        mark = info["label"] or "(fără propunere)"
        print(f"  {key:32} → {mark}  [{info['count']}]")
    print(f"\npropunere: {out} (revizuiește, apoi copiază `entry` în pachet)")


if __name__ == "__main__":
    asyncio.run(main())
