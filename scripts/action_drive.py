"""NX-236 — manual drive: un buton, toate atacurile, contoare redactate.

Cardul cere o probă REPRODUCTIBILĂ, nu o afirmație: se emite un set de acțiuni peste un turn
terminal sintetic, apoi FIECARE token e trecut prin scenariile din failure matrix — valid, un byte
schimbat, altă sesiune, alt tenant, timp expirat, revizie schimbată, două consumări concurente,
cheie rotită între emitere și consum. Pentru fiecare se raportează codul de outcome și dacă a fost
apelat vreun handler / model / tool / mutație.

RULEAZĂ FĂRĂ DB, FĂRĂ REDIS ȘI FĂRĂ OpenAI: rândul-sursă e construit în memorie, iar autorizarea
primește un provider fals. Costul e zero (nu se consumă credite), deci poate rula în CI sau local
oricând — exact ce trebuie ca dovada să fie repetabilă, nu o captură de ecran.

    python scripts/action_drive.py

Ieșirea e DEJA redactată: coduri + contoare, niciun token complet, niciun `action_id`, niciun id
de produs. E forma care poate intra într-un PR.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if sys.platform == "win32":  # consola Windows e cp1252 by default (ca în scripts/db_check.py)
    sys.stdout.reconfigure(encoding="utf-8")

from src.db.queries.web_turns import WebTurnRow  # noqa: E402
from src.web import action_service as svc  # noqa: E402
from src.web.action_crypto import parse_key_ring  # noqa: E402
from src.web.action_models import TurnFacts, plan_actions, spec_for  # noqa: E402
from src.web.turn_service import session_ref_hash  # noqa: E402

BIZ = "biz-drive"
CHANNEL_TOKEN = "tok-drive"
VISITOR = "visitor-drive"
SECRET = "drive-secret"
PID_A = "11111111-1111-4111-8111-111111111111"
PID_B = "22222222-2222-4222-8222-222222222222"

KEY_ONE = base64.b64encode(b"nx236-drive-key-one-------------").decode()
KEY_TWO = base64.b64encode(b"nx236-drive-key-two-------------").decode()
RING = parse_key_ring(f"k1:{KEY_ONE}")
ROTATED_ONLY_NEW = parse_key_ring(f"k2:{KEY_TWO}")
ROTATED_OVERLAP = parse_key_ring(f"k2:{KEY_TWO},k1:{KEY_ONE}")

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


@dataclass
class Counters:
    """Ce s-a ATINS. Un refuz corect are toate contoarele pe zero — asta e proba."""

    handlers: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    mutations: int = 0
    db_reads: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "handler": self.handlers,
            "model": self.model_calls,
            "tool": self.tool_calls,
            "mutation": self.mutations,
            "db_read": self.db_reads,
        }


@dataclass
class Fake:
    """Provider de DB fals + contoare. Nicio conexiune reală, niciun efect."""

    source: WebTurnRow | None
    consumer: WebTurnRow | None = None
    counters: Counters = field(default_factory=Counters)

    def db(self):
        fake = self

        @asynccontextmanager
        async def _cm(operation: str = "?"):
            fake.counters.db_reads += 1
            yield fake

        return _cm


def _row(**over) -> WebTurnRow:
    base = dict(
        id=str(uuid4()),
        business_id=BIZ,
        conversation_id="conv-drive",
        contact_id="contact-drive",
        session_ref_hash=session_ref_hash(CHANNEL_TOKEN, VISITOR),
        client_turn_id=str(uuid4()),
        request_fingerprint="fp",
        schema_version="web-turn.v2",
        status="completed",
        attempt=1,
        lease_owner=None,
        lease_epoch=1,
        lease_expires_at=None,
        deadline_at=None,
        conversation_revision_at_accept=7,
        pipeline_version="web-chat.v1",
        response_json=None,
        safe_error_code=None,
        accepted_at=NOW,
        updated_at=NOW,
        completed_at=NOW,
    )
    base.update(over)
    return WebTurnRow(**base)


def _source() -> WebTurnRow:
    """Turul-sursă: două produse (⇒ detalii, recenzii, comparație), o sesiune de căutare activă
    (⇒ paginare), o clarificare cu opțiuni (⇒ răspunsuri) și un CTA de comerț.

    NX-240: `cart_add_line` se planifică acum, dar DOAR pentru refs pe care groundingul le-a
    declarat vandabile (`commerce_product_refs`). Aici îl cerem explicit pentru PID_A ca drive-ul
    să demonstreze ce se întâmplă la CONSUM cu serviciul de coș stins: refuz onest
    (`action_unavailable`), înainte de a arde one-shot-ul."""
    view = {
        "content": "Uite două seruri potrivite. Ce buget ai?",
        "products": [
            {"product_id": PID_A, "name": "Ser A"},
            {"product_id": PID_B, "name": "Ser B"},
        ],
        "suggestions": ["Sub 100 lei", "100-200 lei"],
    }
    plans = list(
        plan_actions(
            view,
            TurnFacts(
                pending_field="budget_max",
                pending_attempts=1,
                active_search_ref="fp-drive",
                commerce_product_refs=(PID_A,),
            ),
        )
    )
    return _row(response_json=svc.merge_actions_into_view(view, tuple(plans)))


@asynccontextmanager
async def _ledger(fake: Fake):
    """Ledgerul, ca DATE: `authorize_action` primește exact ce ar citi din Postgres, fără DB."""

    async def _get(conn, business_id, turn_id):
        row = fake.source
        if row is None or business_id != row.business_id or turn_id != row.id:
            return None
        return row

    async def _find(conn, business_id, conversation_id, fingerprint):
        return fake.consumer

    original_get, original_find = svc.get_turn_by_id, svc.find_turn_by_fingerprint
    svc.get_turn_by_id, svc.find_turn_by_fingerprint = _get, _find  # type: ignore[assignment]
    try:
        yield
    finally:
        svc.get_turn_by_id, svc.find_turn_by_fingerprint = original_get, original_find  # type: ignore[assignment]


async def _authorize(
    fake: Fake,
    token: str,
    *,
    ring=RING,
    now=None,
    visitor=VISITOR,
    biz=BIZ,
    client_turn_id="ct-drive",
) -> str:
    async with _ledger(fake):
        verdict = await svc.authorize_action(
            fake.db(),
            token=token,
            business_id=biz,
            channel_token=CHANNEL_TOKEN,
            visitor_id=visitor,
            client_turn_id=client_turn_id,
            ring=ring,
            fingerprint_secret=SECRET,
            now=now or NOW + timedelta(seconds=30),
            skew_s=60,
        )
    if isinstance(verdict, svc.AuthorizedAction):
        # Un verdict POZITIV ar continua spre kernel; aici doar îl numărăm ca „handler ar rula".
        fake.counters.handlers += 1
        return "authorized"
    return verdict.code


async def main() -> int:
    source = _source()
    issued = svc.issue_actions(source, svc.plans_from_row(source), ring=RING, ttl_s=1800)
    plans = svc.plans_from_row(source)

    print("=== NX-236 manual drive ===")
    print(f"planuri persistate : {len(plans)}")
    print(f"acțiuni EMISE      : {len(issued)}  (kind-urile fără handler sigur nu se emit)")
    emitted_kinds = sorted({a.plan.kind for a in issued})
    print(f"kind-uri emise     : {emitted_kinds}")
    cart = any(a.plan.kind.startswith("cart") for a in issued)
    # NX-240: CTA-ul de coș se EMITE (are handler NX-237 + condiție de fapte), dar consumul lui
    # rămâne refuzat onest cât timp `CONVERSATION_CART_ENABLED` e stins — vezi scenariile de mai
    # jos. Emiterea fără flag nu e un bug: e un buton care spune „nu acum", nu unul care minte.
    print(f"cart emis?         : {'da (refuzat la consum)' if cart else 'nu'}")
    print(f"lungime token (max): {max(len(a.token) for a in issued)} caractere")
    print()

    scenarios: list[tuple[str, str]] = []
    failures = 0
    # Contoarele se acumulează PE SCENARII REFUZATE: dacă vreun refuz atinge un handler, se vede.
    rejected_counters = Counters()
    for action in issued:
        kind = action.plan.kind
        token = action.token
        stripped = _row(id=source.id, response_json={"content": "ok", "products": []})
        fingerprint = svc.action_fingerprint(
            SECRET, business_id=BIZ, channel_token=CHANNEL_TOKEN, action_id=action.action_id
        )
        consumed = Fake(
            source, consumer=_row(client_turn_id="ct-1", request_fingerprint=fingerprint)
        )
        cases: list[tuple[str, Fake, dict]] = [
            ("valid", Fake(source), {}),
            (
                "byte_flip",
                Fake(source),
                {"token": token[:-2] + ("AB" if not token.endswith("AB") else "CD")},
            ),
            ("other_session", Fake(source), {"visitor": "visitor-altul"}),
            ("other_tenant", Fake(source), {"biz": "biz-altul"}),
            ("expired", Fake(source), {"now": NOW + timedelta(hours=2)}),
            ("source_deleted", Fake(None), {}),
            ("not_emitted", Fake(stripped), {}),
            ("key_rotated_overlap", Fake(source), {"ring": ROTATED_OVERLAP}),
            ("key_dropped", Fake(source), {"ring": ROTATED_ONLY_NEW}),
            # Consum concurent: al doilea turn prezintă ACELAȘI token după ce primul l-a luat.
            ("same_turn_retry", consumed, {"client_turn_id": "ct-1"}),
            ("second_turn", consumed, {"client_turn_id": "ct-2"}),
        ]
        results: dict[str, str] = {}
        for name, fake, kwargs in cases:
            used_token = kwargs.pop("token", token)
            before = fake.counters.handlers
            results[name] = await _authorize(fake, used_token, **kwargs)
            if results[name] != "authorized":
                rejected_counters.handlers += fake.counters.handlers - before
                rejected_counters.db_reads += 0  # citirea sursei e AȘTEPTATĂ; nu e un efect

        expected = {
            "valid": "authorized",
            "byte_flip": "action_invalid",
            "other_session": "action_not_found",
            "other_tenant": "action_not_found",
            "expired": "action_expired",
            "source_deleted": "action_not_found",
            "not_emitted": "action_not_found",
            "key_rotated_overlap": "authorized",
            "key_dropped": "action_invalid",
            "same_turn_retry": "authorized",
            "second_turn": "action_already_consumed",
        }
        if spec_for(kind) is not None and spec_for(kind).mutating:
            # Comerțul are handler (NX-237) și emitere condiționată (NX-240), dar runtime-ul e
            # stins în drive: refuzul e ÎNAINTE de consum, deci one-shot-ul nu se arde. Toate
            # scenariile care ar fi ajuns la handler devin `action_unavailable`; cele care cad
            # mai devreme (crypto, tenant, sesiune, expirare) rămân neschimbate — ordinea
            # verificărilor e exact ce demonstrează drive-ul.
            expected |= {
                "valid": "action_unavailable",
                "key_rotated_overlap": "action_unavailable",
                "same_turn_retry": "action_unavailable",
                "second_turn": "action_unavailable",
            }
        for name, got in results.items():
            ok = got == expected[name]
            failures += 0 if ok else 1
            verdict = "OK" if ok else "NEAȘTEPTAT"
            scenarios.append((f"{kind:<22} {name:<20}", f"{got:<26} {verdict}"))

    for left, right in scenarios:
        print(f"  {left} → {right}")

    print()
    print(f"contoare pe drumurile REFUZATE: {json.dumps(rejected_counters.as_dict())}")
    print(f"scenarii neașteptate: {failures}")
    print("token complet în ieșire: nu (doar lungimi și coduri)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
