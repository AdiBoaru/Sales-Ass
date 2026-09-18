"""NX-302 — ce primește clientul când apelul rich cade, înainte și după.

Probă reproductibilă pe turul REAL `57fa9fbe` (`conversation_traces`, 2026-09-18 09:36:59):
«vreau o rutina de cosuri» a ieșit din producție cu
`{"rich_error": "APITimeoutError", "rich_downgraded": "structured-call-failed"}`, deci cu
`reply.rich = null`. Clientul a văzut patru carduri mute și, sub ele, o listă de trei nume cu
prețuri.

Proba hidratează EXACT cele patru produse din acel tur prin `get_products_by_ids` (calea reală de
catalog, tenant-scoped) și compune ambele forme de răspuns:

  * `_deterministic_reply` + `_card_products` — ce servea ramura degradată;
  * `rich_from_facts` — ce servește de acum, prin ACELAȘI `compose.assemble` ca pe calea reușită.

Nu cheamă niciun model și nu scrie nimic: read-only, zero credite.

    python -m scripts.nx302_degraded_probe
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from src.agent.brain_rich import rich_from_facts  # noqa: E402
from src.agent.fallbacks import _card_products, _deterministic_reply  # noqa: E402
from src.db.connection import close_pool, tenant_conn  # noqa: E402
from src.db.queries.businesses import load_business  # noqa: E402
from src.db.queries.catalog import get_products_by_ids  # noqa: E402
from src.domain.loader import load_domain_pack  # noqa: E402
from src.models import (  # noqa: E402
    Contact,
    ConversationState,
    InboundMessage,
    TurnContext,
)
from src.worker import compose  # noqa: E402

SOLE = "99fe1292-f9ed-469e-8183-f994ea5b59c0"
TURN = "57fa9fbe-b588-4081-a07b-ae19a8e61a97"
MESSAGE = "vreau o rutina de cosuri"
# Cele patru produse pe care turul real le-a servit (din `reply.products` al rândului de trace).
PRODUCT_IDS = [
    "4b3333da-9f81-4203-9236-148d14725130",  # EVY TECHNOLOGY Renew Mousse Cleanser
    "f356eb92-2ff8-4cd5-9900-3a9378c72057",  # ALLIES OF SKIN Azelaic & Kojic
    "d1773281-0129-4170-8c6e-5946c90ec368",  # SKINTEGRA Superba C
    "c63dc2a8-96f2-426b-9eb4-5405d8b8939c",  # SOME BY MI Retinol Bakuchiol Bubble Toner
]


def _ctx(business) -> TurnContext:
    ctx = TurnContext(
        turn_id=TURN,
        business=business,
        contact=Contact(id="probe", business_id=SOLE),
        message=InboundMessage(provider_msg_id="probe", body=MESSAGE, channel_kind="webchat"),
        conversation_id="probe",
        state=ConversationState(),
    )
    ctx.language = "ro"
    return ctx


#: Câmpurile care fac diferența dintre un card bogat și unul mut, pe contractul pe care îl citește
#: randorul web (`channels/web/render._card`).
_RICH_FIELDS = ("reason", "rating", "review_count", "badge", "details", "list_price", "size")


def _report_simple(card: dict) -> dict:
    """Ramura SĂRACĂ: `reply.products`. Niciunul dintre câmpurile bogate nu există aici — de-aia
    randorul cădea pe carduri mute când `reply.rich` era `null`."""
    return {k: card.get(k) for k in _RICH_FIELDS}


def _report_rich(item) -> dict:
    """Ramura BOGATĂ: `reply.rich.items`. `details` se raportează ca prezență, nu ca text."""
    out = {k: getattr(item, k, None) for k in _RICH_FIELDS if k != "details"}
    out["details"] = bool(getattr(item, "details", None))
    return out


async def main() -> None:
    async with tenant_conn(SOLE) as conn:
        products = await get_products_by_ids(conn, SOLE, PRODUCT_IDS, limit=6)
        business = await load_business(conn, SOLE)
    if business is None:
        raise SystemExit("tenantul sole-ro nu a fost găsit")
    # Pachetul de domeniu trăiește în `businesses.settings`, nu în JSON-ul de default — badge-urile
    # și încadrarea depind de el, deci o probă care l-ar sări ar măsura alt sistem.
    business.domain_pack = load_domain_pack(business)

    print(f"tur real: {TURN}  «{MESSAGE}»")
    print(f"produse hidratate din catalog: {len(products)}\n")

    print("=" * 78)
    print("ÎNAINTE — ramura degradată (`_deterministic_reply` + carduri simple)")
    print("=" * 78)
    print(_deterministic_reply(products))
    before = _card_products(products, n=4)  # plafonul scris de mână, cel de dinainte de NX-302
    print(f"\ncarduri: {len(before)}  (textul numea 3)")
    for c in before:
        print(f"  - {c['name']}: {json.dumps(_report_simple(c), ensure_ascii=False)}")

    ctx = _ctx(business)
    rich = rich_from_facts(ctx, products, intro=None)
    print()
    print("=" * 78)
    print("DUPĂ — `rich_from_facts` (același `compose.assemble` ca pe calea reușită)")
    print("=" * 78)
    if rich is None:
        print("rich = None (niciun produs cu nume ȘI preț) → rămâne calea de azi")
        await close_pool()
        return
    print(compose.flatten(rich, "ro"))
    after = rich.items
    print(f"\ncarduri: {len(after)}")
    for it in after:
        print(f"  - {it.name}: {json.dumps(_report_rich(it), ensure_ascii=False)}")

    n = len(after)
    rated = sum(1 for it in after if it.rating is not None)
    badged = sum(1 for it in after if it.badge)
    detailed = sum(1 for it in after if it.details)
    sized = sum(1 for it in after if it.size)
    reasoned = sum(1 for it in after if it.reason)
    print()
    print(f"  intro          : {'da' if rich.intro else 'nu'}   (înainte: text determinist)")
    print(f"  rating pe card : {rated}/{n}   (înainte 0/{len(before)})")
    print(f"  badge pe card  : {badged}/{n}   (înainte 0/{len(before)})")
    print(f"  details        : {detailed}/{n}   (înainte 0/{len(before)})")
    print(f"  gramaj         : {sized}/{n}   (înainte 0/{len(before)})")
    print(f"  motiv pe card  : {reasoned}/{n}   (înainte 0/{len(before)})")
    if not reasoned:
        # Declarat, nu ascuns: `_data_reason` citește `best_for` (NX-169), iar pe catalogul SOLE
        # coloana e GOALĂ pe toate cele 2.758 de rânduri. Nu e un defect al recuperării — e aceeași
        # clasă cu `product_card_blurbs = 0`: mecanismul are producător în cod și n-are date. Se
        # repară printr-o derivare de catalog (ca NX-279 pentru rezumatele de recenzii), nu aici.
        print("    ^ gol fiindcă `best_for` e 0/2758 pe SOLE — gaură de DATE, nu de cod")
    await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
