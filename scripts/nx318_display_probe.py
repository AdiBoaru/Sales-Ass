"""NX-318 — afișarea pe trafic real: ordinea cardurilor, badge-ul comun, ancora ambiguă. Read-only.

Citește `conversation_traces` din ultimele N zile (răspunsul SERVIT, `reply.rich`) și numără:

1. **ordinea**: ture în care textul numește un produs aflat pe o poziție de card MAI MARE decât
   un produs numit după el. Separat pe `intro` (ce citește `_order_by_first_mention`) și pe
   `intro + education` (tot textul pe care îl vede clientul), cu regula veche (≥2 cuvinte din
   nume) și cu prefixul unic din set;
2. **badge-ul**: ture cu ≥3 carduri în care același badge e pe mai mult de jumătate din ele, plus
   cât din catalog califică „Top Favorit” pe ratingul brut și pe cel shrunk;
3. **chips**: ture cu cel puțin un chip a cărui ancoră e prefixul numelui a DOUĂ carduri.

Nu apelează niciun model și nu scrie nimic.

    PYTHONPATH=. python scripts/nx318_display_probe.py [--business sole-ro] [--days 30]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from dotenv import load_dotenv

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

from src.catalog.render_text import name_keys, unique_prefixes  # noqa: E402
from src.db.connection import admin_conn, close_pool, get_pool  # noqa: E402
from src.db.queries.fusion import _shrunk_rating  # noqa: E402
from src.worker.badges import _DEFAULT_RULES  # noqa: E402
from src.worker.compose import _mention  # noqa: E402


def _json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _misordered(text: str, items: list[dict], *, unique: bool, locale: str) -> bool:
    """Există un produs numit mai devreme în text care stă pe un card mai jos decât unul numit
    după el? Aceeași regulă de mențiune ca `compose._order_by_first_mention`."""
    names = {str(i): str(it.get("name") or "") for i, it in enumerate(items)}
    prefixes = unique_prefixes(names, locale=locale) if unique else {}
    hits: list[tuple[int, int]] = []
    for i, it in enumerate(items):
        hit = _mention(text, str(it.get("name") or ""), prefixes.get(str(i)))
        if hit is not None:
            hits.append((hit[0], i))
    cards = [i for _, i in sorted(hits)]
    return cards != sorted(cards)


def _common_badge(items: list[dict]) -> str | None:
    if len(items) < 3:
        return None
    counts: dict[str, int] = {}
    for it in items:
        if it.get("badge"):
            counts[it["badge"]] = counts.get(it["badge"], 0) + 1
    common = [b for b, n in counts.items() if n * 2 > len(items)]
    return common[0] if common else None


def _ambiguous_chip(label: str, items: list[dict]) -> bool:
    """Ancora chip-ului = cel mai lung prefix de nume de card (≥2 cuvinte) care apare în text.
    Ambiguu = acel prefix maxim e atins de cel puțin DOUĂ carduri."""
    words = tuple(k for k in name_keys(label) if k)
    best: dict[int, int] = {}
    for it in items:
        keys = tuple(k for k in name_keys(str(it.get("name") or "")) if k)
        longest = 0
        for cut in range(2, len(keys) + 1):
            prefix = keys[:cut]
            if any(words[i : i + cut] == prefix for i in range(len(words) - cut + 1)):
                longest = cut
        if longest:
            best[longest] = best.get(longest, 0) + 1
    return bool(best) and best[max(best)] >= 2


async def main(business_slug: str, days: int) -> None:
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        biz = await conn.fetchrow("select id from businesses where slug = $1", business_slug)
        if biz is None:
            raise SystemExit(f"tenant necunoscut: {business_slug}")
        rows = await conn.fetch(
            """
            select turn_id, language, reply
              from conversation_traces
             where business_id = $1
               and created_at > now() - ($2::int || ' days')::interval
             order by created_at
            """,
            biz["id"],
            days,
        )
        catalog = await conn.fetch(
            """
            select rating, review_count
              from products
             where business_id = $1 and status = 'active'
            """,
            biz["id"],
        )
    await close_pool()

    counters = {
        "turns_with_cards": 0,
        "intro_misordered_old": 0,
        "intro_misordered_unique": 0,
        "full_text_misordered_old": 0,
        "full_text_misordered_unique": 0,
        "common_badge": 0,
        "common_badge_top": 0,
        "turns_with_chips": 0,
        "ambiguous_chip": 0,
    }
    examples: dict[str, list[str]] = {"misordered": [], "common_badge": [], "ambiguous_chip": []}
    for row in rows:
        reply = _json(row["reply"]) or {}
        rich = reply.get("rich") or {}
        items = [it for it in rich.get("items") or [] if it.get("name")]
        if not items:
            continue
        counters["turns_with_cards"] += 1
        locale = row["language"] or "ro"
        intro = rich.get("intro") or ""
        full = " ".join(t for t in (intro, rich.get("education") or "") if t)
        tid = str(row["turn_id"])[:8]
        if intro:
            counters["intro_misordered_old"] += _misordered(
                intro, items, unique=False, locale=locale
            )
            counters["intro_misordered_unique"] += _misordered(
                intro, items, unique=True, locale=locale
            )
        if full:
            old = _misordered(full, items, unique=False, locale=locale)
            new = _misordered(full, items, unique=True, locale=locale)
            counters["full_text_misordered_old"] += old
            counters["full_text_misordered_unique"] += new
            if new and len(examples["misordered"]) < 5:
                examples["misordered"].append(tid)
        badge = _common_badge(items)
        if badge:
            counters["common_badge"] += 1
            counters["common_badge_top"] += badge.startswith("Top")
            if len(examples["common_badge"]) < 5:
                examples["common_badge"].append(f"{tid} {badge}")
        chips = rich.get("chips") or reply.get("suggestions") or []
        labels = [c.get("label") if isinstance(c, dict) else str(c) for c in chips]
        labels = [label for label in labels if label]
        if labels:
            counters["turns_with_chips"] += 1
            if any(_ambiguous_chip(label, items) for label in labels):
                counters["ambiguous_chip"] += 1
                if len(examples["ambiguous_chip"]) < 5:
                    examples["ambiguous_chip"].append(tid)

    top_rating, top_reviews = _DEFAULT_RULES["top_rating"], _DEFAULT_RULES["top_reviews"]
    rated = [dict(r) for r in catalog]
    raw_top = sum(
        1
        for p in rated
        if p["rating"] is not None
        and float(p["rating"]) >= top_rating
        and (p["review_count"] or 0) >= top_reviews
    )
    shrunk_top = sum(
        1
        for p in rated
        if p["rating"] is not None
        and _shrunk_rating(p) >= top_rating
        and (p["review_count"] or 0) >= top_reviews
    )

    print(f"# NX-318 — {business_slug}, ultimele {days} zile, {len(rows)} ture\n")
    for key, value in counters.items():
        print(f"{key:32} {value}")
    total = len(rated) or 1
    print(f"\ncatalog activ                    {len(rated)}")
    print(f"top pe rating brut               {raw_top} ({raw_top / total:.1%})")
    print(f"top pe rating shrunk             {shrunk_top} ({shrunk_top / total:.1%})")
    for key, values in examples.items():
        if values:
            print(f"\nexemple {key}: {', '.join(values)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--business", default="sole-ro")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    asyncio.run(main(args.business, args.days))
