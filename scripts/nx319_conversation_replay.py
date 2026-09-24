"""NX-319 — replay al conversației reale `1518d1d9` pe catalogul viu, fără niciun model.

Pentru turele de căutare (0, 4, 5) rulează `search_products` cu ARGUMENTELE REALE ale modelului
(din `analytics_events.tool_call`), cu istoricul clientului de la momentul turului, de două ori:
cu flagurile NX-319 stinse (comportamentul de pe prod) și aprinse. Textul de căutare e mesajul
clientului, ca în sonda NX-313 (evenimentul nu poartă `query`). Pentru turul 8 alege partenerul
lui «Compară-l cu un produs similar». Read-only, zero credite.

    PYTHONPATH=. python scripts/nx319_conversation_replay.py
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from dotenv import load_dotenv

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

from src.agent.deterministic import pick_similar_partner  # noqa: E402
from src.catalog.render_text import display_name  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.db.connection import admin_conn, close_pool, get_pool, tenant_conn  # noqa: E402
from src.db.queries.businesses import load_business  # noqa: E402
from src.db.queries.catalog import similar_candidates  # noqa: E402
from src.models import (  # noqa: E402
    Author,
    Contact,
    ConversationState,
    Direction,
    InboundMessage,
    Message,
    TurnContext,
)
from src.worker.runner import PipelineDeps  # noqa: E402

_FLAGS = (
    "search_price_bound_provenance_enabled",
    "search_subshelf_homograph_guard_enabled",
)

#: (eticheta, mesajul clientului, mesajele lui anterioare, argumentele modelului, sesiunea activă)
_TURNS: tuple[tuple[str, str, tuple[str, ...], dict[str, Any], dict[str, Any] | None], ...] = (
    (
        "T0 «vreau o crema de fata»",
        "vreau o crema de fata",
        (),
        {"category": "fata", "sort_mode": "relevance", "limit": 6},
        None,
    ),
    (
        "T4 «ai ceva anti aging?»",
        "ai ceva anti aging?",
        (
            "si ceva mai ieftin ?",
            "poti sa mi faci si o rutina ?",
            "pai mi se usuca peilea dupa ce fac dus",
            "vreau o crema de fata",
        ),
        {
            "category": "fata",
            "concerns": ["uscăciune după duș", "anti aging"],
            "features": ["anti_aging"],
            "price_max": 29.99,
            "sort_mode": "relevance",
            "limit": 6,
        },
        {"filters": {"category": "fata", "price_max": 34.99, "sort_mode": "price_asc"}},
    ),
    (
        "T5 «da ceva aparat as vrea»",
        "da ceva aparat as vrea",
        ("ai ceva anti aging?", "si ceva mai ieftin ?", "vreau o crema de fata"),
        {
            "category": "electrica",
            "concerns": ["anti aging", "uscăciune după duș"],
            "sort_mode": "price_asc",
            "limit": 6,
        },
        None,
    ),
)

_BRUSH = "fae2ab92-701f-457b-8326-13fcfed7e606"  # GESKE Sonic Facial Brush 5 in 1 (turul 8)


class _Provider:
    def __init__(self, business_id: str) -> None:
        self.business_id = business_id

    def __call__(self, operation: str = "unlabeled"):  # noqa: ARG002
        return tenant_conn(self.business_id)


async def _search(deps, biz, text, history, args, session) -> tuple[list[str], dict[str, Any]]:
    from src.tools.base import TOOL_REGISTRY

    ctx = TurnContext(
        turn_id="nx319-replay",
        business=biz,
        contact=Contact(id="replay", business_id=biz.id),
        message=InboundMessage(provider_msg_id="nx319", body=text, channel_kind="webchat"),
        conversation_id="replay-conv",
        language=biz.default_locale or "ro",
        history=[
            Message(direction=Direction.INBOUND, author=Author.CONTACT, body=h) for h in history
        ],
        state=ConversationState(active_search=session),
    )
    result = await TOOL_REGISTRY["search_products"](ctx, deps, {**args, "query": text})
    events: dict[str, Any] = {}
    for e in ctx.events:
        events.setdefault(e.type, e.properties)
    names = [f"{p.get('price')} {display_name(str(p.get('name')))[:46]}" for p in result.products]
    return names, events


async def main() -> None:
    import src.tools.catalog_tools  # noqa: F401 — înregistrează tool-urile

    pool = await get_pool()
    async with admin_conn(pool) as conn:
        business_id = str(await conn.fetchval("select id from businesses where slug = 'sole-ro'"))
    async with tenant_conn(business_id) as conn:
        biz = await load_business(conn, business_id)
    deps = PipelineDeps(llm=None, db=_Provider(business_id))
    settings = get_settings()

    for label, text, history, args, session in _TURNS:
        print(f"\n## {label}")
        for on in (False, True):
            for flag in _FLAGS:
                setattr(settings, flag, on)
            names, ev = await _search(deps, biz, text, history, args, session)
            tags = [
                k
                for k in (
                    "price_bound_provenance",
                    "category_subshelf_homograph",
                    "guessed_filter_rescued",
                )
                if k in ev
            ]
            print(f"  NX-319 {'ON ' if on else 'OFF'} {tags}")
            for n in names:
                print(f"      {n}")

    print("\n## T8 «Compară-l cu un produs similar» (ancora: GESKE Sonic Facial Brush 5 in 1)")
    async with tenant_conn(business_id) as conn:
        cands = await similar_candidates(conn, business_id, _BRUSH)
    for c in cands[:6]:
        print(f"      {c['price']} {display_name(str(c['name']))[:60]}")
    partner = pick_similar_partner(cands)
    name = next((c["name"] for c in cands if c["id"] == partner), None)
    print(f"  partener ales: {display_name(str(name)) if name else None}")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
