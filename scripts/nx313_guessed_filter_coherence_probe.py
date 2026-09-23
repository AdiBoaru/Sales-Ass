"""NX-313 — cât din cerere cade în filtrul GHICIT, pe traficul real. Read-only.

Reia fiecare `search_products` real din ultimele N zile (argumentele din evenimentul `tool_call`,
textul din `conversation_traces.client_text`, fiindcă `query` nu e în whitelist-ul de analytics,
P12) și tipărește, pentru turele cu un filtru ghicit, proporția pe care o judecă
`guessed_filter_verdict`, verdictul și primele produse ÎNAINTE și DUPĂ. Pragul se citește din
distribuție, nu dintr-un caz.

Rulează tool-ul real de două ori: cu `SEARCH_GUESSED_FILTER_COHERENCE_ENABLED` stins (setul de
azi) și aprins (setul nou). Nu apelează niciun model (brațul vector e stins, decizia 2026-09-08)
și nu scrie nimic.

    PYTHONPATH=. python scripts/nx313_guessed_filter_coherence_probe.py [--business sole-ro]
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

from src.catalog.render_text import display_name  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.db.connection import admin_conn, close_pool, get_pool, tenant_conn  # noqa: E402
from src.db.queries.businesses import load_business  # noqa: E402
from src.models import Contact, InboundMessage, TurnContext  # noqa: E402
from src.worker.runner import PipelineDeps  # noqa: E402

#: Argumentele pe care evenimentul `tool_call` le poartă și tool-ul le acceptă.
_ARG_KEYS = ("category", "concerns", "brand", "price_max", "sort_mode", "in_stock_only", "limit")


class _Provider:
    def __init__(self, business_id: str) -> None:
        self.business_id = business_id

    def __call__(self, operation: str = "unlabeled"):  # noqa: ARG002
        return tenant_conn(self.business_id)


async def _cases(business_slug: str, days: int) -> tuple[str, list[tuple[str, dict[str, Any]]]]:
    pool = await get_pool()
    async with admin_conn(pool) as conn:
        biz = await conn.fetchrow("select id from businesses where slug = $1", business_slug)
        if biz is None:
            raise SystemExit(f"tenant necunoscut: {business_slug}")
        rows = await conn.fetch(
            """
            select e.properties, t.client_text
              from analytics_events e
              join conversation_traces t
                on t.turn_id = e.turn_id and t.business_id = e.business_id
             where e.business_id = $1
               and e.event_type = 'tool_call'
               and e.properties->>'name' = 'search_products'
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
        if not text or not (args.get("category") or args.get("concerns")):
            continue  # fără filtru de subiect nu are ce ghici
        key = json.dumps([text, args], sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        out.append((text, {**args, "query": text}))
    return str(biz["id"]), out


async def _run(deps: PipelineDeps, biz: Any, text: str, args: dict[str, Any]) -> dict[str, Any]:
    from src.tools.base import TOOL_REGISTRY

    ctx = TurnContext(
        turn_id="nx313-probe",
        business=biz,
        contact=Contact(id="probe-contact", business_id=biz.id),
        message=InboundMessage(provider_msg_id="nx313", body=text, channel_kind="webchat"),
        conversation_id="probe-conv",
        language=biz.default_locale or "ro",
    )
    result = await TOOL_REGISTRY["search_products"](ctx, deps, dict(args))
    ev: dict[str, Any] = {}
    for e in ctx.events:
        ev.setdefault(e.type, e.properties)
    return {
        "probe": ev.get("guessed_filter_probe"),
        "step": (ev.get("product_search") or {}).get("lexical_step"),
        "names": [display_name(str(p.get("name")))[:44] for p in (result.products or [])][:4],
    }


async def run(business_slug: str, days: int) -> int:
    import src.tools.catalog_tools  # noqa: F401 — înregistrează tool-urile

    business_id, cases = await _cases(business_slug, days)
    async with tenant_conn(business_id) as conn:
        biz = await load_business(conn, business_id)
    deps = PipelineDeps(llm=None, db=_Provider(business_id))
    settings = get_settings()
    probed: list[tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]] = []
    for text, args in cases:
        settings.search_guessed_filter_coherence_enabled = True
        after = await _run(deps, biz, text, args)
        if after["probe"] is None:
            continue
        settings.search_guessed_filter_coherence_enabled = False
        try:
            before = await _run(deps, biz, text, args)
        except Exception as exc:  # noqa: BLE001
            print("CRASH (flag OFF)", type(exc).__name__, text, args)
            continue
        probed.append((text, args, before, after))
    settings.search_guessed_filter_coherence_enabled = True

    print(
        f"{len(cases)} căutări reale distincte cu filtru de subiect, {len(probed)} cu ghicitură\n"
    )
    for text, args, before, after in sorted(
        probed, key=lambda x: (x[3]["probe"].get("share") is None, x[3]["probe"].get("share") or 0)
    ):
        p = after["probe"]
        share = p.get("share")
        guessed = [k for k in ("category", "facets") if p.get(f"{k}_guessed")]
        print(
            f"share={'  -  ' if share is None else f'{share:.2f}'}  {p['verdict']:<12} "
            f"[{before['step']}] «{text[:60]}»  "
            f"cat={args.get('category')} concerns={args.get('concerns')} "
            f"ghicit={guessed}"
        )
        if p["verdict"] != "kept":
            print(f"      ÎNAINTE: {before['names']}")
            print(f"      DUPĂ:    {after['names']}")
    await close_pool()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--business", default="sole-ro")
    ap.add_argument("--days", type=int, default=30)
    a = ap.parse_args()
    return asyncio.run(run(a.business, a.days))


if __name__ == "__main__":
    raise SystemExit(main())
