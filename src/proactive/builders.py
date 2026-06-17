"""Construirea textului mesajului proactiv per `kind` (NX-70) — cod pur determinist.

Textele vin din payload-ul jobului / catalog / shipments, NU sunt generate de LLM (P2).
Fiecare builder întoarce un `MessageSpec`:
  • `free_text`  — mesajul liber (folosit dacă poarta NX-71 returnează `mode='free'`),
  • `template_name` + `variables` — pentru ramura de template a porții (P11),
  • `cancel=True` — jobul nu mai are sens (ex. coș deja convertit/expirat) → `cancelled`.

`BuildError` = jobul nu poate fi construit (date lipsă / kind neacceptat în v1) → motorul
marchează jobul `failed` (P6: vizibil, nu pierdut tăcut). `kind='custom'` nu e tratat în v1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.db.queries.proactive import (
    get_latest_checkout,
    get_product_for_notice,
    get_shipment_for_order,
)


class BuildError(RuntimeError):
    """Jobul nu poate fi construit (date insuficiente sau kind necunoscut)."""


@dataclass
class MessageSpec:
    free_text: str = ""
    template_name: str = ""
    variables: dict[str, str] = field(default_factory=dict)
    cancel: bool = False  # job fără sens (coș convertit/expirat) → mark 'cancelled'


async def build_message_spec(
    conn, business_id: str, job: dict[str, Any], route: dict[str, Any]
) -> MessageSpec:
    """Dispecer pe `kind`. `route` = ieșirea lui `get_proactive_route` (id, locale, ...)."""
    kind = job["kind"]
    payload = job.get("payload") or {}
    if kind == "awb_update":
        return await _build_awb(conn, business_id, payload)
    if kind == "back_in_stock":
        return await _build_back_in_stock(conn, business_id, payload)
    if kind == "abandoned_cart":
        return await _build_abandoned_cart(conn, business_id, route)
    if kind == "follow_up":
        return _build_follow_up(payload)
    raise BuildError(f"kind neacceptat în v1: {kind}")


async def _build_awb(conn, business_id: str, payload: dict[str, Any]) -> MessageSpec:
    awb = payload.get("awb")
    carrier = payload.get("carrier") or ""
    order_id = payload.get("order_id")
    if not awb and order_id:  # confirmare din shipments dacă payload n-are AWB-ul
        ship = await get_shipment_for_order(conn, business_id, order_id)
        if ship:
            awb = ship.get("awb")
            carrier = carrier or (ship.get("carrier") or "")
    if not awb:
        raise BuildError("awb_update fără AWB")
    suffix = f" ({carrier})." if carrier else "."
    return MessageSpec(
        free_text=f"Comanda ta a fost expediată! AWB {awb}{suffix}",
        template_name="awb_update",
        variables={"awb": str(awb), "courier": str(carrier)},
    )


async def _build_back_in_stock(conn, business_id: str, payload: dict[str, Any]) -> MessageSpec:
    product_id = payload.get("product_id")
    if not product_id:
        raise BuildError("back_in_stock fără product_id")
    prod = await get_product_for_notice(conn, business_id, product_id)
    if prod is None:
        raise BuildError("back_in_stock: produs inexistent")
    name = prod.get("name") or "Produsul"
    url = prod.get("product_url") or ""
    text = f"{name} e din nou pe stoc!" + (f" {url}" if url else "")
    return MessageSpec(
        free_text=text, template_name="back_in_stock", variables={"product": name, "url": url}
    )


async def _build_abandoned_cart(conn, business_id: str, route: dict[str, Any]) -> MessageSpec:
    co = await get_latest_checkout(conn, business_id, route["id"])
    if co is None:
        raise BuildError("abandoned_cart fără checkout link")
    if co.get("converted_order_id") or co.get("expired"):
        return MessageSpec(cancel=True)  # deja cumpărat / expirat → nu mai reamintim
    url = co["url"]
    return MessageSpec(
        free_text=f"Ți-am păstrat coșul. Finalizează comanda aici: {url}",
        template_name="abandoned_cart",
        variables={"url": url},
    )


def _build_follow_up(payload: dict[str, Any]) -> MessageSpec:
    body = (payload.get("body") or "").strip()
    if not body:
        raise BuildError("follow_up fără body")
    extra = payload.get("variables") or {}
    return MessageSpec(
        free_text=body,
        template_name="follow_up",
        variables={k: str(v) for k, v in extra.items()},
    )
