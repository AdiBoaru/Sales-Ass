"""NX-162 (Funnel Truth) — redirect de atribuire click.

`GET /r/{business_id}/{ref_code}`:
  1. ștampilează `checkout_links.clicked_at` (idempotent — primul click câștigă),
  2. emite `checkout_link_clicked` în `analytics_events` (append-only, tenant-scoped,
     FĂRĂ PII — doar `ref_code` + `checkout_link_id`), un singur event pe link,
  3. `302` către `url`-ul real de checkout.
Link inexistent/expirat → fallback safe (`checkout_base_url`) sau `404`, fără a scurge existența
(`ref_code` = uuid, oricum neghicibil).

Spre deosebire de webhook-ul Meta (margine SUBȚIRE, fără DB), redirectul FACE DB sincron: are
nevoie de `url`-ul destinație înainte de `302`. Tenantul e EXPLICIT în path (P7) — fără scanare
cross-tenant, fără `admin_conn`. Analytics-ul e best-effort (nu blochează redirectul, ca P6/P10).

NOTĂ (NX-162b): commerce_tools încă dă clientului URL-ul magazinului direct; wiring-ul care face
linkul să treacă prin acest endpoint (ca să se măsoare click-ul în prod) e felia următoare.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse, RedirectResponse

from src.config import get_settings
from src.db.connection import tenant_conn
from src.db.queries.analytics import insert_events
from src.db.queries.commerce import get_checkout_redirect, stamp_checkout_clicked
from src.models import Event

log = logging.getLogger(__name__)

router = APIRouter(tags=["redirect"])


def _is_uuid(s: str) -> bool:
    try:
        UUID(s)
    except ValueError:
        return False
    return True


def _fallback() -> RedirectResponse | PlainTextResponse:
    """Link inexistent/expirat: 302 la store base dacă e configurat, altfel 404 neutru.
    Același răspuns ca pentru un link expirat → nu dezvăluie dacă un `ref_code` există."""
    base = (get_settings().checkout_base_url or "").strip()
    if base:
        return RedirectResponse(url=base, status_code=302)
    return PlainTextResponse("not found", status_code=404)


@router.get("/r/{business_id}/{ref_code}")
async def checkout_redirect(business_id: str, ref_code: str):
    # business_id ne-uuid → tratat ca not-found FĂRĂ a deschide tenant_conn (set_config ar primi
    # un string ne-uuid și query-urile ar crăpa la cast). Fail safe, fără leak.
    if not _is_uuid(business_id):
        return _fallback()

    async with tenant_conn(business_id) as conn:
        target = await get_checkout_redirect(conn, business_id, ref_code)
        if target is None:
            return _fallback()  # inexistent/expirat — fallback safe, fără leak de existență

        first_click_id = await stamp_checkout_clicked(conn, business_id, ref_code)
        if first_click_id is not None:
            # Primul click → un singur event (idempotent). Best-effort: un eșec de analytics NU
            # trebuie să pice redirectul clientului (P6). Fără PII (P12): doar ref_code + id.
            try:
                await insert_events(
                    conn,
                    business_id,
                    [
                        Event(
                            "checkout_link_clicked",
                            {"ref_code": ref_code, "checkout_link_id": first_click_id},
                        )
                    ],
                    conversation_id=target["conversation_id"],
                    contact_id=target["contact_id"],
                )
            except Exception:  # noqa: BLE001 — analytics best-effort; redirectul are prioritate
                log.warning("redirect: emitere checkout_link_clicked eșuată (redirect continuă)")

        return RedirectResponse(url=target["url"], status_code=302)
