"""NX-299 — proba de FORMĂ: calea reală a uneltei, cu argumentele turului real. Read-only.

Rulează `search_products` exact cum a rulat pe producție pe turul `42744330` (2026-09-17 20:39,
«vreau ceva sa scap de cosuri», `category="dermato-cosmetice"` + `concerns=["coșuri"]`), cu
kill-switch-ul de proveniență pornit și stins, și tipărește ce s-ar fi servit: câte produse, din
câte CLASE de produs, pe ce treaptă, plus sloturile de formă pe care setul rezultat le cere.

Nu apelează niciun model și nu scrie nimic: doar Postgres, pe rolul de runtime.

    python scripts/nx299_shape_probe.py [--business-id <uuid>]
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

from src.agent import answer_shape  # noqa: E402
from src.db.connection import tenant_conn  # noqa: E402
from src.db.queries.businesses import load_business  # noqa: E402
from src.models import Contact, InboundMessage, TurnContext  # noqa: E402
from src.worker.runner import PipelineDeps  # noqa: E402

SOLE_BIZ = "99fe1292-f9ed-469e-8183-f994ea5b59c0"

CASES: list[dict[str, Any]] = [
    {
        "label": "turul REAL 42744330: raft ghicit (6 produse) peste nevoie rostită (518)",
        "body": "vreau ceva sa scap de cosuri",
        "args": {
            "query": "scap de cosuri",
            "category": "dermato-cosmetice",
            "concerns": ["coșuri"],
            "limit": 6,
        },
    },
    {
        "label": "raft ROSTIT de client: nu se relaxează, oricât de mic ar fi",
        "body": "ce ai la dermato cosmetice",
        "args": {"query": "dermato cosmetice", "category": "dermato-cosmetice", "limit": 6},
    },
    {
        "label": "raft ghicit, dar SINGURUL subiect: rămâne dur (n-ar mai rămâne nimic cerut)",
        "body": "vreau makeup",
        "args": {"query": "makeup", "category": "machiaj", "limit": 6},
    },
    {
        "label": "fără raft, doar nevoia: neatins de felie (nimic de relaxat)",
        "body": "vreau ceva sa scap de cosuri",
        "args": {"query": "scap de cosuri", "concerns": ["coșuri"], "limit": 6},
    },
]


class _Provider:
    def __init__(self, business_id: str) -> None:
        self.business_id = business_id

    def __call__(self, operation: str = "unlabeled"):  # noqa: ARG002
        return tenant_conn(self.business_id)


async def _run_case(deps: PipelineDeps, biz: Any, case: dict[str, Any]) -> dict[str, Any]:
    from src.tools.base import TOOL_REGISTRY
    from src.worker import compose

    ctx = TurnContext(
        turn_id=f"nx299-{abs(hash(case['label'])) % 10**8}",
        business=biz,
        contact=Contact(id="probe-contact", business_id=biz.id),
        message=InboundMessage(
            provider_msg_id="nx299-probe", body=case["body"], channel_kind="webchat"
        ),
        conversation_id="probe-conv",
        language=biz.default_locale or "ro",
    )
    result = await TOOL_REGISTRY["search_products"](ctx, deps, case["args"])
    products = list(result.products or [])
    by_type = {e.type: e.properties for e in ctx.events}
    types = answer_shape.distinct_types(products)
    pack = getattr(biz, "domain_pack", None)
    facets = tuple(getattr(pack, "comparison_facets", ()) or ()) if pack else ()
    axes = compose.decision_axes(products, facets, ctx.language)
    shape = answer_shape.shape_for(n_items=len(products), product_types=types, n_axes=len(axes))
    return {
        "n": len(products),
        "search": by_type.get("product_search", {}),
        "inferred": "category_inferred" in by_type,
        "relaxed": "category_hypothesis_relaxed" in by_type,
        "types": types,
        "axes": axes,
        "required": shape.required,
        "framing": answer_shape.framing_text(pack, ctx.language, types),
        "names": [str(p.get("name"))[:64] for p in products],
    }


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
        print(f"  args: {case['args']}")
        for on in (False, True):
            settings.search_relax_by_provenance_enabled = on
            r = await _run_case(deps, biz, case)
            s = r["search"]
            tag = "NX-299 ON " if on else "înainte   "
            print(
                f"  {tag} servite={r['n']}  pool={s.get('lexical_pool')}"
                f"  treapta={s.get('lexical_step')}  relax={s.get('relax_depth')}"
                f"  raft_ghicit={r['inferred']}  raft_relaxat={r['relaxed']}"
            )
            print(f"       clase={len(r['types'])} {list(r['types'])}  axe={len(r['axes'])}")
            print(f"       sloturi cerute: {list(r['required'])}")
            if on and r["framing"]:
                print(f"       încadrare de rezervă: {r['framing']}")
            for name in r["names"]:
                print(f"       - {name}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="NX-299: proba de formă pe calea reală a uneltei")
    ap.add_argument("--business-id", default=os.environ.get("PROBE_BUSINESS_ID", SOLE_BIZ))
    args = ap.parse_args()
    return asyncio.run(run(args.business_id))


if __name__ == "__main__":
    raise SystemExit(main())
