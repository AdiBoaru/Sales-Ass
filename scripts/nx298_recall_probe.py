"""NX-298 — proba de recall: calea REALĂ a uneltei, cu argumentele turului REAL. Read-only.

Rulează `search_products` exact cum a rulat pe producție („vreau ceva sa scap de cosuri",
`concerns=[coșuri]`), cu cele două kill-switch-uri pornite și stinse, și tipărește ce s-ar fi
servit: câte produse, pe ce treaptă de text, din câte tipuri de produs distincte.

Nu apelează niciun model și nu scrie nimic: doar Postgres, pe rolul de runtime.

    python scripts/nx298_recall_probe.py [--business-id <uuid>]
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

from src.db.connection import tenant_conn  # noqa: E402
from src.db.queries.businesses import load_business  # noqa: E402
from src.models import Contact, InboundMessage, TurnContext  # noqa: E402
from src.worker.runner import PipelineDeps  # noqa: E402

SOLE_BIZ = "99fe1292-f9ed-469e-8183-f994ea5b59c0"

# Turul real din `conversation_traces` (2026-09-17 19:19, turn aa2dc1d9): un card servit, pool 5.
CASES: list[dict[str, Any]] = [
    {
        "label": "turul real: «vreau ceva sa scap de cosuri»",
        "body": "vreau ceva sa scap de cosuri",
        "args": {"query": "scap de cosuri", "concerns": ["coșuri"], "limit": 6},
    },
    {
        "label": "cerere de raft: «ce produse de barbati ai» (NX-293)",
        "body": "ce produse de barbati ai",
        "args": {"query": "produse barbati", "category": "barbati", "limit": 6},
    },
    {
        "label": "text care aduce informație peste filtru: «crema pentru cosuri»",
        "body": "crema pentru cosuri",
        "args": {"query": "crema pentru cosuri", "concerns": ["coșuri"], "limit": 6},
    },
    {
        "label": "pool mare, unde se vede diversitatea pe TIP: «ceva pentru acnee»",
        "body": "ceva pentru acnee",
        "args": {"query": "acnee", "concerns": ["acnee"], "limit": 6},
    },
]


class _Provider:
    """`deps.db` minimal: un checkout tenant-scoped per operație (ca în db_query_probe)."""

    def __init__(self, business_id: str) -> None:
        self.business_id = business_id

    def __call__(self, operation: str = "unlabeled"):  # noqa: ARG002 — eticheta nu ne interesează
        return tenant_conn(self.business_id)


async def _run_case(deps: PipelineDeps, biz: Any, case: dict[str, Any]) -> dict[str, Any]:
    from src.tools.base import TOOL_REGISTRY

    ctx = TurnContext(
        turn_id=f"nx298-{abs(hash(case['label'])) % 10**8}",
        business=biz,
        contact=Contact(id="probe-contact", business_id=biz.id),
        message=InboundMessage(
            provider_msg_id="nx298-probe", body=case["body"], channel_kind="webchat"
        ),
        conversation_id="probe-conv",
        language=biz.default_locale or "ro",
    )
    result = await TOOL_REGISTRY["search_products"](ctx, deps, case["args"])
    products = list(result.products or [])
    search = next((e for e in ctx.events if e.type == "product_search"), None)
    props = search.properties if search else {}
    types = sorted({str((p.get("attributes") or {}).get("product_type") or "?") for p in products})
    return {
        "n": len(products),
        "pool": props.get("lexical_pool"),
        "step": props.get("lexical_step"),
        "filled": props.get("filled_from_filter"),
        "types": types,
        "names": [str(p.get("name"))[:58] for p in products],
    }


async def diversity_demo(business_id: str) -> None:
    """Efectul cotei pe TIP, pe un pool REAL: aceleași 50 de produse, două ordini."""
    from src.db.queries.catalog import search_products_lexical
    from src.tools.catalog_tools import _product_type, diversify_pool

    async with tenant_conn(business_id) as conn:
        pool = await search_products_lexical(
            conn, business_id, "acnee", concerns=["acne"], locale="ro", pool=50
        )
    print("=" * 100)
    print(f"cota pe tip, pe un pool real de {len(pool)} produse cu `concerns=acne`")
    for label, kw in (("fără cotă de tip", {"max_per_type": None}), ("cu cotă de tip", {})):
        front = diversify_pool(pool, 6, **kw)[:6]
        types = [_product_type(p) or "?" for p in front]
        print(f"  {label:<18} tipuri distincte={len(set(types))}  {types}")


async def run(business_id: str) -> int:
    import src.tools.catalog_tools  # noqa: F401 — înregistrează tool-urile
    from src.config import get_settings

    deps = PipelineDeps(llm=None, db=_Provider(business_id))
    async with tenant_conn(business_id) as conn:
        biz = await load_business(conn, business_id)
    if biz is None:
        print(f"EROARE: business {business_id} inexistent", file=sys.stderr)
        return 2

    settings = get_settings()
    for case in CASES:
        print("=" * 100)
        print(case["label"])
        for on in (False, True):
            settings.search_fill_from_subject_filter_enabled = on
            r = await _run_case(deps, biz, case)
            tag = "NX-298 ON " if on else "înainte   "
            print(
                f"  {tag} servite={r['n']}  pool={r['pool']}  treapta={r['step']}"
                f"  completate={r['filled']}  tipuri={len(r['types'])}"
            )
            print(f"       tipuri: {', '.join(r['types'])}")
            for name in r["names"]:
                print(f"       - {name}")
    await diversity_demo(business_id)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="NX-298: proba de recall pe calea reală a uneltei")
    ap.add_argument("--business-id", default=os.environ.get("PROBE_BUSINESS_ID", SOLE_BIZ))
    args = ap.parse_args()
    return asyncio.run(run(args.business_id))


if __name__ == "__main__":
    raise SystemExit(main())
