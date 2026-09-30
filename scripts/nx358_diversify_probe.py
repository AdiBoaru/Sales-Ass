"""NX-358 — sonda diversificării pe catalogul real: flag stins vs aprins. Read-only, zero model.

Rulează căutarea planificată (`run_planned_search`: fuziune, retrogradarea epuizatelor,
diversificare) pe două feluri de cereri:

- cu TIP cerut (`prefer.product_type`, cum îl pun plannerul și NX-355): pagina trebuie să fie din
  tipul cerut, fiindcă asta a cerut clientul (conversația `1848eeba`: trei măști la o cremă);
- DOAR cu nevoie (fără tip): paleta de tipuri (NX-298) trebuie să rămână.

    PYTHONPATH=. python scripts/nx358_diversify_probe.py [--business-id <uuid>]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import Counter
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
SHELF = "ten-ingrijirea-tenului"

#: (eticheta, argumentele căutării). Primul caz e turul real din `1848eeba`.
TYPED: list[tuple[str, dict[str, Any]]] = [
    (
        "1848eeba t3: cremă, ten uscat, sub 100",
        {
            "query": "cremă de față pentru hidratare, ten uscat",
            "category": SHELF,
            "concerns": ["hydration", "dry"],
            "price_max": 100,
            "prefer": {"product_type": ["crema de fata"]},
        },
    ),
    (
        "cremă de hidratare",
        {
            "query": "crema de fata hidratare",
            "category": SHELF,
            "concerns": ["hydration"],
            "prefer": {"product_type": ["crema de fata"]},
        },
    ),
    (
        "ser pentru acnee",
        {
            "query": "ser de fata acnee",
            "concerns": ["acne"],
            "prefer": {"product_type": ["ser de fata"]},
        },
    ),
    (
        "cremă pentru pete, sub 150",
        {
            "query": "crema de fata pete",
            "concerns": ["hyperpigmentation"],
            "price_max": 150,
            "prefer": {"product_type": ["crema de fata"]},
        },
    ),
    (
        "mască hidratantă",
        {
            "query": "masca de fata hidratare",
            "concerns": ["hydration"],
            "prefer": {"product_type": ["masca de fata"]},
        },
    ),
]

NEED_ONLY: list[tuple[str, dict[str, Any]]] = [
    ("scap de coșuri (NX-298)", {"query": "scap de cosuri", "concerns": ["acne"]}),
    ("hidratare", {"query": "hidratare", "concerns": ["hydration"]}),
    ("pete, sub 100", {"query": "pete", "concerns": ["hyperpigmentation"], "price_max": 100}),
]


class _Provider:
    def __init__(self, business_id: str) -> None:
        self.business_id = business_id

    def __call__(self, operation: str = "unlabeled"):  # noqa: ARG002
        return tenant_conn(self.business_id)


#: Clasa după NUME, pentru măsurătoare: un sfert din catalog n-are `product_type` derivat, deși
#: numele spune ce e („Crema pentru fata", „Masca de fata"). Doar pentru sondă, nu în producție.
_KINDS = (
    ("masc", "masca"),
    ("crem", "crema"),
    ("ser ", "ser"),
    ("serum", "ser"),
    ("gel", "gel"),
    ("plastur", "plasturi"),
    ("spum", "spuma"),
    ("lotiun", "lotiune"),
    ("toner", "toner"),
    ("esent", "esenta"),
    ("ampou", "ser"),
    ("ulei", "ulei"),
    ("balsam", "balsam"),
)


def _type(p: dict[str, Any]) -> str:
    derived = (p.get("attributes") or {}).get("product_type")
    if derived:
        return str(derived).split()[0]
    name = " " + str(p.get("name") or "").lower()
    for needle, kind in _KINDS:
        if needle in name:
            return kind
    return "-"


async def _page(deps: PipelineDeps, biz: Any, args: dict[str, Any]) -> list[dict[str, Any]]:
    from src.tools.catalog_tools import SearchArgs, run_planned_search

    ctx = TurnContext(
        turn_id="nx358-probe",
        business=biz,
        contact=Contact(id="probe-contact", business_id=biz.id),
        message=InboundMessage(provider_msg_id="nx358", body=args["query"], channel_kind="webchat"),
        conversation_id="probe-conv",
        language=biz.default_locale or "ro",
    )
    res = await run_planned_search(ctx, deps, SearchArgs(limit=6, **args))
    return list(res.products or [])


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

    print("=" * 100)
    print("CU TIP CERUT: câte carduri din 6 sunt din tipul cerut")
    for label, args in TYPED:
        wanted = args["prefer"]["product_type"][0].split()[0]
        row = []
        for on in (False, True):
            settings.search_diversify_subject_aware_enabled = on
            page = await _page(deps, biz, args)
            row.append((sum(_type(p) == wanted for p in page), len(page), page))
        (b, bn, _), (a, an, page) = row
        print(f"  {label:<40} înainte {b}/{bn}  după {a}/{an}")
        for p in page:
            print(f"      {str(p.get('name'))[:56]:<56} {p.get('price')!s:>7} {_type(p)}")

    print("=" * 100)
    print("DOAR NEVOIE: tipuri distincte pe pagină (paleta NX-298 trebuie să rămână)")
    for label, args in NEED_ONLY:
        row = []
        for on in (False, True):
            settings.search_diversify_subject_aware_enabled = on
            page = await _page(deps, biz, args)
            row.append(Counter(_type(p) for p in page if _type(p) != "-"))
        before, after = row
        print(f"  {label:<40} înainte {len(before)} {dict(before)}")
        print(f"  {'':<40} după    {len(after)} {dict(after)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="NX-358: sonda diversificării pe catalogul real")
    ap.add_argument("--business-id", default=os.environ.get("PROBE_BUSINESS_ID", SOLE_BIZ))
    return asyncio.run(run(ap.parse_args().business_id))


if __name__ == "__main__":
    raise SystemExit(main())
