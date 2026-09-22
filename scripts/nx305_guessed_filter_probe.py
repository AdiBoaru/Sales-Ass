"""NX-305 — cât costă un filtru de subiect pe care nimeni nu l-a rostit. Read-only.

Rulează `search_products` pe traseul REAL, cu argumentele turului `78b347fa` (2026-09-21 08:35,
«cat cost una?» despre mănușa de aplicare autobronzant), cu kill-switch-ul pornit și stins, și
tipărește ce s-ar fi servit: treapta lexicală, produsele și prețurile.

Al doilea bloc arată DE CE reparația nu e „raftul conversației câștigă": cu raftul corect impus,
interogarea cade pe `filters_only` și servește geluri, adică tot nu mănușa. Filtrele ghicite
trebuie SCOASE, nu înlocuite.

Nu apelează niciun model și nu scrie nimic.

    python -m scripts.nx305_guessed_filter_probe [--business-id <uuid>]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

from dotenv import load_dotenv

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

from src.catalog.render_text import display_name  # noqa: E402
from src.config import get_settings  # noqa: E402
from src.db.connection import close_pool, tenant_conn  # noqa: E402
from src.db.queries.businesses import load_business  # noqa: E402
from src.models import Contact, InboundMessage, TurnContext  # noqa: E402
from src.worker.runner import PipelineDeps  # noqa: E402

SOLE_BIZ = "99fe1292-f9ed-469e-8183-f994ea5b59c0"

#: Mesajul clientului. Subiectul („mănușă") vine din REPLICA BOTULUI de dinainte, nu de la client:
#: de aceea niciun filtru al turului nu se coroborează, adică exact poarta lui NX-305.
MESSAGE = "cat cost una?"

#: Argumentele EXACTE ale turului real. `query` nu e în whitelist-ul de analytics (P12), deci textul
#: e reconstruit; `lexical_pool=9` + `lexical_step=relaxed_any` din `product_search` confirmă
#: reconstrucția, fiindcă ies identice.
REAL_ARGS: dict[str, Any] = {
    "query": "manusa pentru aplicarea autobronzantului",
    "category": "accesorii",
    "concerns": ["autobronzant", "aplicare"],
    "sort_mode": "relevance",
    "in_stock_only": False,
    "limit": 6,
}

#: Variantele respinse pe MĂSURĂTOARE, păstrate ca să nu fie reîncercate.
ALTERNATIVES: list[tuple[str, dict[str, Any]]] = [
    ("raftul conversației, impus", {**REAL_ARGS, "category": "creme-autobronzante-si-bronzere"}),
    ("doar fără categorie", {k: v for k, v in REAL_ARGS.items() if k != "category"}),
]


class _Provider:
    def __init__(self, business_id: str) -> None:
        self.business_id = business_id

    def __call__(self, operation: str = "unlabeled"):  # noqa: ARG002
        return tenant_conn(self.business_id)


async def _run(deps: PipelineDeps, biz: Any, args: dict[str, Any]) -> dict[str, Any]:
    from src.tools.base import TOOL_REGISTRY

    ctx = TurnContext(
        turn_id=f"nx305-{abs(hash(str(args))) % 10**8}",
        business=biz,
        contact=Contact(id="probe-contact", business_id=biz.id),
        message=InboundMessage(provider_msg_id="nx305-probe", body=MESSAGE, channel_kind="webchat"),
        conversation_id="probe-conv",
        language=biz.default_locale or "ro",
    )
    result = await TOOL_REGISTRY["search_products"](ctx, deps, dict(args))
    by_type = {e.type: e.properties for e in ctx.events}
    search = by_type.get("product_search", {})
    products = list(result.products or [])
    return {
        "step": search.get("lexical_step") or "strict",
        "pool": search.get("lexical_pool"),
        "rescued": by_type.get("guessed_filter_rescued"),
        "rows": [
            f"{float(p.get('price') or 0):>8.2f} lei  {display_name(str(p.get('name')))[:52]}"
            for p in products
        ],
    }


def _show(label: str, r: dict[str, Any]) -> None:
    print(f"\n--- {label}")
    print(f"    treapta={r['step']}  pool={r['pool']}  servite={len(r['rows'])}")
    if r["rescued"]:
        ev = r["rescued"]
        print(f"    SALVAT: {ev.get('from_step')} -> {ev.get('to_step')}")
    for row in r["rows"]:
        print(f"      {row}")


async def run(business_id: str) -> int:
    import src.tools.catalog_tools  # noqa: F401 — înregistrează tool-urile

    deps = PipelineDeps(llm=None, db=_Provider(business_id))
    async with tenant_conn(business_id) as conn:
        biz = await load_business(conn, business_id)
    if biz is None:
        print(f"EROARE: business {business_id} inexistent", file=sys.stderr)
        return 2

    settings = get_settings()
    print("=" * 78)
    print(f"1. TURUL REAL «{MESSAGE}» — cu kill-switch-ul stins și pornit")
    print("=" * 78)
    for on in (False, True):
        settings.search_guessed_filter_rescue_enabled = on
        _show(
            f"SEARCH_GUESSED_FILTER_RESCUE_ENABLED={str(on).lower()}",
            await _run(deps, biz, REAL_ARGS),
        )

    settings.search_guessed_filter_rescue_enabled = False
    print("\n" + "=" * 78)
    print("2. DE CE nu «raftul conversatiei castiga» (variante respinse pe masuratoare)")
    print("=" * 78)
    for label, args in ALTERNATIVES:
        _show(label, await _run(deps, biz, args))

    settings.search_guessed_filter_rescue_enabled = True
    await close_pool()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--business-id", default=os.getenv("PROBE_BUSINESS_ID", SOLE_BIZ))
    return asyncio.run(run(ap.parse_args().business_id))


if __name__ == "__main__":
    raise SystemExit(main())
