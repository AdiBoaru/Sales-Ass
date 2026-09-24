"""Meniul ÎNCHIS de rafturi — antetul pe care îl vede modelul și cât l-a adoptat. Read-only.

1. Tipărește antetul de rafturi exact cum pleacă spre model (`list_category_menu`, `_store_header`).
2. Pentru fiecare `search_products` real din ultimele N zile care a trimis o `category`, spune dacă
   valoarea era o CHEIE din meniu și, dacă nu, ce raft alegea rezolvarea liberă. Rulată ÎNAINTE de
   deploy, dă baza (22 din 72 chei pe 30 de zile); rulată DUPĂ, spune dacă modelul a adoptat
   cheile (aceeași cifră o poartă evenimentul `category_off_menu`).
3. `--replay`: reia căutarea cu valoarea BLOCATĂ (fără raft) lângă rezolvarea liberă. Asta a fost
   măsurătoarea care a decis ca meniul să NU blocheze: fără raft, «si ceva de volum ?» (păr) aducea
   plumpere de buze, iar numele unice («buze», «styling») erau corecte.

Nu apelează niciun model și nu scrie nimic.

    PYTHONPATH=. python scripts/shelf_menu_probe.py [--business sole-ro] [--days 30] [--replay]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from typing import Any

from dotenv import load_dotenv

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

from src.agent.prompt_builder import PromptInputs, _store_header  # noqa: E402
from src.catalog.render_text import display_name  # noqa: E402
from src.catalog.vocabulary import CATEGORY_DIMENSION, category_on_menu, resolve  # noqa: E402
from src.catalog.vocabulary_cache import get_vocabulary  # noqa: E402
from src.db.connection import admin_conn, close_pool, get_pool, tenant_conn  # noqa: E402
from src.db.queries.businesses import load_business  # noqa: E402
from src.db.queries.catalog import list_category_menu  # noqa: E402
from src.models import Contact, InboundMessage, TurnContext  # noqa: E402
from src.worker.runner import PipelineDeps  # noqa: E402

_ARG_KEYS = ("category", "concerns", "brand", "price_max", "sort_mode", "in_stock_only", "limit")


class _Provider:
    def __init__(self, business_id: str) -> None:
        self.business_id = business_id

    def __call__(self, operation: str = "unlabeled"):  # noqa: ARG002
        return tenant_conn(self.business_id)


async def _cases(slug: str, days: int) -> tuple[str, list[tuple[str, dict[str, Any]]]]:
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        biz = await conn.fetchrow("select id from businesses where slug = $1", slug)
        if biz is None:
            raise SystemExit(f"tenant necunoscut: {slug}")
        rows = await conn.fetch(
            """
            select e.properties, t.client_text
              from analytics_events e
              join conversation_traces t
                on t.turn_id = e.turn_id and t.business_id = e.business_id
             where e.business_id = $1
               and e.event_type = 'tool_call'
               and e.properties->>'name' = 'search_products'
               and e.properties->'args'->>'category' is not null
               and e.created_at > now() - ($2::int || ' days')::interval
             order by e.created_at
            """,
            biz["id"],
            days,
        )
    out: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    for r in rows:
        props = r["properties"]
        props = json.loads(props) if isinstance(props, str) else props
        args = {k: v for k, v in (props.get("args") or {}).items() if k in _ARG_KEYS}
        text = (r["client_text"] or "").strip()
        key = json.dumps([text, args], sort_keys=True, ensure_ascii=False)
        if not text or key in seen:
            continue
        seen.add(key)
        out.append((text, {**args, "query": text}))
    return str(biz["id"]), out


async def _run(deps: PipelineDeps, biz: Any, text: str, args: dict[str, Any]) -> dict[str, Any]:
    from src.tools.base import TOOL_REGISTRY

    ctx = TurnContext(
        turn_id="shelf-menu-probe",
        business=biz,
        contact=Contact(id="probe-contact", business_id=biz.id),
        message=InboundMessage(provider_msg_id="probe", body=text, channel_kind="webchat"),
        conversation_id="probe-conv",
        language=biz.default_locale or "ro",
    )
    result = await TOOL_REGISTRY["search_products"](ctx, deps, dict(args))
    return {
        "names": [display_name(str(p.get("name")))[:40] for p in (result.products or [])][:4],
    }


async def run(slug: str, days: int, replay: bool) -> int:
    import src.tools.catalog_tools  # noqa: F401 — înregistrează tool-urile

    business_id, cases = await _cases(slug, days)
    async with tenant_conn(business_id) as conn:
        biz = await load_business(conn, business_id)
        menu = await list_category_menu(conn, business_id)
    inputs = PromptInputs.build(biz.name, biz.vertical, "ro", menu, [], shelf_menu=True)
    header = _store_header(inputs)
    print(f"# Meniul de rafturi ({len(menu)} chei, {len(header)} caractere în antet)\n")
    print(header, "\n")

    deps = PipelineDeps(llm=None, db=_Provider(business_id))
    vocab = await get_vocabulary(deps, business_id)
    stats: Counter[str] = Counter()
    off_values: Counter[str] = Counter()
    for text, args in cases:
        value = str(args.get("category"))
        on_menu = category_on_menu(vocab, value).reason != "off_menu"
        stats["calls"] += 1
        stats["on_menu" if on_menu else "off_menu"] += 1
        if on_menu:
            continue
        free = resolve(vocab, value, CATEGORY_DIMENSION)
        off_values[f"{value} → {free.status.value}:{','.join(free.constraint_keys)[:70]}"] += 1
        if replay:
            liber = await _run(deps, biz, text, args)
            blocat = await _run(deps, biz, text, {**args, "category": None})
            print(f"«{text}»  category={value!r}")
            print(f"   liber : {liber['names']}")
            print(f"   blocat: {blocat['names']}")
    print("## Valori din afara meniului (valoare → rezolvarea liberă)\n")
    for k, n in off_values.most_common():
        print(f"  {n:>3}  {k}")
    print("\n", dict(stats))
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--business", default="sole-ro")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--replay", action="store_true")
    args = ap.parse_args()

    async def _go() -> int:
        try:
            return await run(args.business, args.days, args.replay)
        finally:
            await close_pool()

    raise SystemExit(asyncio.run(_go()))


if __name__ == "__main__":
    main()
