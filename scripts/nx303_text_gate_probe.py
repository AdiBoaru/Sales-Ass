"""NX-303 — cât taie din recall cuvântul clientului, când filtrul îl poartă deja. Read-only.

Rulează `search_products` pe traseul REAL, cu argumentele turului `c820226a` (2026-09-18 09:34,
«vreau ceva sa scap de cosuri», `category="ten"` + `concerns=["coșuri","imperfecțiuni","acnee"]`),
cu kill-switch-ul pornit și stins, și tipărește ce s-ar fi servit: mărimea pool-ului, treapta
lexicală, produsele și clasele lor.

Al doilea bloc arată DE CE: aceleași filtre, alt text — sinonimul magazinului dă un pool de 50,
cuvântul clientului dă 6. Nu apelează niciun model și nu scrie nimic.

    python -m scripts.nx303_text_gate_probe [--business-id <uuid>]
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

#: Argumentele EXACTE ale turului real (din `analytics_events.tool_call`).
REAL_ARGS: dict[str, Any] = {
    "query": "vreau ceva sa scap de cosuri",
    "category": "ten",
    "concerns": ["coșuri", "imperfecțiuni", "acnee"],
    "limit": 6,
}

#: Aceleași filtre, alt text — cât valorează cuvântul, singur.
TEXTS = [
    ("cuvintele clientului (ce trimite azi)", "vreau ceva sa scap de cosuri"),
    ("sinonimul magazinului", "acnee"),
    ("celălalt sinonim", "imperfectiuni"),
]


class _Provider:
    def __init__(self, business_id: str) -> None:
        self.business_id = business_id

    def __call__(self, operation: str = "unlabeled"):  # noqa: ARG002
        return tenant_conn(self.business_id)


async def _run(deps: PipelineDeps, biz: Any, args: dict[str, Any]) -> dict[str, Any]:
    from src.tools.base import TOOL_REGISTRY

    ctx = TurnContext(
        turn_id=f"nx303-{abs(hash(str(args))) % 10**8}",
        business=biz,
        contact=Contact(id="probe-contact", business_id=biz.id),
        message=InboundMessage(
            provider_msg_id="nx303-probe", body=str(args["query"]), channel_kind="webchat"
        ),
        conversation_id="probe-conv",
        language=biz.default_locale or "ro",
    )
    result = await TOOL_REGISTRY["search_products"](ctx, deps, dict(args))
    by_type = {e.type: e.properties for e in ctx.events}
    search = by_type.get("product_search", {})
    session = by_type.get("search_session", {})
    ext = by_type.get("pool_extended_from_filter", {})
    products = list(result.products or [])
    return {
        "pool": search.get("lexical_pool"),
        "session_pool": session.get("pool_size"),
        "added": ext.get("added"),
        "step": search.get("lexical_step") or "strict",
        "skipped": "text_gate_skipped" in by_type,
        "n": len(products),
        "types": sorted(
            {(p.get("attributes") or {}).get("product_type") or "(fara tip)" for p in products}
        ),
        "names": [display_name(str(p.get("name"))) for p in products],
    }


def _show(label: str, r: dict[str, Any]) -> None:
    print(f"\n--- {label}")
    print(
        f"    pool_text={r['pool']}  POOL_SESIUNE={r['session_pool']}  "
        f"adaugate={r['added']}\n"
        f"    treapta={r['step']}  servite={r['n']}  clase={len(r['types'])}  "
        f"text_redundant={r['skipped']}"
    )
    for name in r["names"]:
        print(f"      {name[:66]}")


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
    print("1. TURUL REAL, cu kill-switch-ul stins și pornit")
    print("=" * 78)
    for on in (False, True):
        settings.search_text_gate_when_redundant_enabled = on
        r = await _run(deps, biz, REAL_ARGS)
        _show(f"SEARCH_TEXT_GATE_WHEN_REDUNDANT_ENABLED={str(on).lower()}", r)

    settings.search_text_gate_when_redundant_enabled = False
    print("\n" + "=" * 78)
    print("2. DE CE: aceleași filtre, alt text (poarta veche)")
    print("=" * 78)
    for label, text in TEXTS:
        r = await _run(deps, biz, {**REAL_ARGS, "query": text})
        print(f"  pool={str(r['pool']):>4}  clase={len(r['types'])}  «{text}»  ← {label}")

    await close_pool()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--business-id", default=os.getenv("PROBE_BUSINESS_ID", SOLE_BIZ))
    return asyncio.run(run(ap.parse_args().business_id))


if __name__ == "__main__":
    raise SystemExit(main())
