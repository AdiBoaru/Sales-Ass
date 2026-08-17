"""NX-247 — probe READ-ONLY, tenant-scoped, peste starea persistată a unui tur.

De ce probe și nu asserturi pe UI: „un singur POST" e o afirmație despre browser, iar „o singură
execuție" e o afirmație despre DB. Un test care verifică doar UI-ul trece și când serverul a rulat
turul de două ori și a suprascris rezultatul. Probele sunt jumătatea care contează.

Contract mecanic, verificat de `test_stage1_harness.py`:

  • fiecare statement e în `PROBE_SQL` — nu există SQL împrăștiat în funcții;
  • fiecare statement începe cu `select`; niciun verb de scriere nu apare nicăieri;
  • fiecare statement filtrează explicit `business_id = $1` (P7), inclusiv cele care ar putea
    trece pe un PK global — RLS e plasă, nu mecanism.

Zero PII: probele întorc NUMERE și statusuri închise. `visitor_id`, `safe_body`, tokenuri sau text
de client nu ies de aici, ca un artefact de CI să nu poată conține ce logurile n-au voie să vadă.
"""

from __future__ import annotations

import json
from typing import Any

#: Registrul de statements. Cheia e numele probei; testul de igienă iterează exact peste el.
PROBE_SQL: dict[str, str] = {
    "ledger_rows": (
        "select count(*)::int from web_turns where business_id = $1 and client_turn_id = $2"
    ),
    "ledger_rows_for_conversation": (
        "select count(*)::int from web_turns where business_id = $1 and conversation_id = $2"
    ),
    "turn_state": (
        "select status, attempt, lease_epoch, safe_error_code, "
        "(response_json is not null) as has_result "
        "from web_turns where business_id = $1 and id = $2"
    ),
    "active_turns": (
        "select count(*)::int from web_turns where business_id = $1 and conversation_id = $2 "
        "and status in ('accepted', 'running')"
    ),
    "assistant_messages": (
        "select count(*)::int from messages where business_id = $1 and conversation_id = $2 "
        "and direction = 'outbound'"
    ),
    "inbound_messages": (
        "select count(*)::int from messages where business_id = $1 and conversation_id = $2 "
        "and direction = 'inbound'"
    ),
    "receipts": (
        "select count(*)::int from commerce_action_receipts where business_id = $1 "
        "and conversation_id = $2"
    ),
    "succeeded_receipts": (
        "select count(*)::int from commerce_action_receipts where business_id = $1 "
        "and conversation_id = $2 and status = 'succeeded'"
    ),
    "cart_items": (
        "select coalesce(sum(i.quantity), 0)::int from conversation_cart_items i "
        "join conversation_carts c on c.id = i.cart_id and c.business_id = i.business_id "
        "where i.business_id = $1 and c.conversation_id = $2"
    ),
    "feedback_rows": (
        "select count(*)::int from web_feedback where business_id = $1 and turn_id = $2"
    ),
    "feedback_revision": (
        "select coalesce(max(revision), 0)::int from web_feedback where business_id = $1 "
        "and turn_id = $2"
    ),
    # `conversations` NU are coloană `revision`: revizia de pe sârmă e `state_version` la accept
    # (`web_turns.conversation_revision_at_accept`). Probele nu inventează un câmp ca să pară
    # simetrice cu contractul.
    "conversation_state": (
        "select state, state_version from conversations where business_id = $1 and id = $2"
    ),
    "conversations": "select count(*)::int from conversations where business_id = $1",
    "product_names": (
        "select name from products where business_id = $1 and status = 'active' order by name"
    ),
}

#: Verbele care nu au ce căuta într-o probă. Lista e mică și explicită, ca testul de igienă să fie
#: citibil: o probă care scrie nu e o probă, e o mutație deghizată în măsurătoare.
FORBIDDEN_SQL_VERBS: tuple[str, ...] = (
    "insert",
    "update",
    "delete",
    "truncate",
    "alter",
    "drop",
    "create",
    "grant",
    "revoke",
    "merge",
)


async def _scalar(conn, key: str, *params: Any) -> Any:
    return await conn.fetchval(PROBE_SQL[key], *params)


async def ledger_rows(conn, business_id: str, client_turn_id: str) -> int:
    return await _scalar(conn, "ledger_rows", business_id, client_turn_id)


async def ledger_rows_for_conversation(conn, business_id: str, conversation_id: str) -> int:
    return await _scalar(conn, "ledger_rows_for_conversation", business_id, conversation_id)


async def turn_state(conn, business_id: str, turn_id: str) -> dict[str, Any] | None:
    row = await conn.fetchrow(PROBE_SQL["turn_state"], business_id, turn_id)
    return dict(row) if row else None


async def active_turns(conn, business_id: str, conversation_id: str) -> int:
    return await _scalar(conn, "active_turns", business_id, conversation_id)


async def assistant_messages(conn, business_id: str, conversation_id: str) -> int:
    return await _scalar(conn, "assistant_messages", business_id, conversation_id)


async def inbound_messages(conn, business_id: str, conversation_id: str) -> int:
    return await _scalar(conn, "inbound_messages", business_id, conversation_id)


async def receipts(conn, business_id: str, conversation_id: str) -> int:
    return await _scalar(conn, "receipts", business_id, conversation_id)


async def succeeded_receipts(conn, business_id: str, conversation_id: str) -> int:
    return await _scalar(conn, "succeeded_receipts", business_id, conversation_id)


async def cart_items(conn, business_id: str, conversation_id: str) -> int:
    return await _scalar(conn, "cart_items", business_id, conversation_id)


async def feedback_rows(conn, business_id: str, turn_id: str) -> int:
    return await _scalar(conn, "feedback_rows", business_id, turn_id)


async def feedback_revision(conn, business_id: str, turn_id: str) -> int:
    return await _scalar(conn, "feedback_revision", business_id, turn_id)


async def conversations(conn, business_id: str) -> int:
    return await _scalar(conn, "conversations", business_id)


async def product_names(conn, business_id: str) -> list[str]:
    return [r["name"] for r in await conn.fetch(PROBE_SQL["product_names"], business_id)]


async def conversation_state(conn, business_id: str, conversation_id: str) -> dict[str, Any]:
    """Starea conversației, REDUSĂ la ce au nevoie invarianții: nevoile active, revocările și
    dacă referința de context a fost rezolvată server-side. Textul liber nu iese."""
    row = await conn.fetchrow(PROBE_SQL["conversation_state"], business_id, conversation_id)
    if row is None:
        return {}
    raw = row["state"]
    state = json.loads(raw) if isinstance(raw, str) else (raw or {})
    needs = state.get("needs") or []
    return {
        "schema_version": state.get("schema_version"),
        "state_version": row["state_version"],
        "active_needs": sorted(
            {
                f"{n.get('kind')}:{n.get('value')}"
                for n in needs
                if isinstance(n, dict) and n.get("status") == "active"
            }
        ),
        "revoked": sorted(
            {
                f"{r.get('kind')}:{r.get('value')}"
                for r in (state.get("revocations") or [])
                if isinstance(r, dict)
            }
        ),
        "context_resolved": bool((state.get("references") or {}).get("resolved")),
    }


async def turn_bundle(
    conn, *, business_id: str, conversation_id: str, turn_id: str, client_turn_id: str
) -> dict[str, Any]:
    """Toate probele de care are nevoie un scenariu, într-un singur pachet. Un checkout scurt,
    etichetat de apelant — probele nu deschid conexiuni ele însele (NX-231)."""
    return {
        "ledger_rows": await ledger_rows(conn, business_id, client_turn_id),
        "ledger_rows_for_conversation": await ledger_rows_for_conversation(
            conn, business_id, conversation_id
        ),
        "turn": await turn_state(conn, business_id, turn_id),
        "active_turns": await active_turns(conn, business_id, conversation_id),
        "assistant_messages": await assistant_messages(conn, business_id, conversation_id),
        "inbound_messages": await inbound_messages(conn, business_id, conversation_id),
        "receipts": await receipts(conn, business_id, conversation_id),
        "succeeded_receipts": await succeeded_receipts(conn, business_id, conversation_id),
        "cart_items": await cart_items(conn, business_id, conversation_id),
        "feedback_rows": await feedback_rows(conn, business_id, turn_id),
        "feedback_revision": await feedback_revision(conn, business_id, turn_id),
    }
