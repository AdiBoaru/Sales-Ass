"""Transportul asincron pe contractul `web-chat.v1` — poarta de paritate cu calea sincronă.

Invarianta care contează, și motivul pentru care testul ăsta există: mutarea pe accept 202 +
SSE schimbă CÂND ajunge răspunsul, nu CE conține. Dacă vreodată diferă, widgetul care randează
`/web/chat` azi ar randa altceva mâine, fără ca nimeni să fi cerut asta — exact clasa de regresie
tăcută pe care proiecția v2 o producea (chips, `offer` și comparația își pierdeau destinația).

Testul o măsoară, nu o presupune: ia forme REALE de `Reply`, le trece prin `render_web` (ce
persistă executorul în tranzacția terminală) și compară câmpurile de conținut ale proiecției
asincrone cu payload-ul persistat, cheie cu cheie.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from src.channels.web.render import render_web
from src.db.queries.web_turns import WebTurnRow
from src.models import (
    Chip,
    Comparison,
    ComparisonColumn,
    ComparisonRow,
    Offer,
    Reply,
    RichItem,
    RichReply,
)
from src.web import app as wa
from src.web.turn_service import error_view
from src.web.turn_view_v1 import v1_terminal_view, wire_payload

# Cheile de PLIC — ce adaugă transportul asincron peste payload-ul de conținut. Lista e explicită
# ca diferența „plic vs conținut" să fie verificabilă, nu comentată.
ENVELOPE_KEYS = frozenset({"schema_version", "conversation", "turn", "error"})

NOW = datetime(2026, 9, 17, 10, 0, tzinfo=UTC)


def make_row(payload: dict | None, *, status: str = "completed", code: str | None = None):
    return WebTurnRow(
        id="11111111-1111-1111-1111-111111111111",
        business_id="99fe1292-f9ed-469e-8183-f994ea5b59c0",
        conversation_id="22222222-2222-2222-2222-222222222222",
        contact_id="33333333-3333-3333-3333-333333333333",
        session_ref_hash="sess",
        client_turn_id="44444444-4444-4444-4444-444444444444",
        request_fingerprint="fp",
        schema_version="1",
        status=status,
        attempt=1,
        lease_owner=None,
        lease_epoch=1,
        lease_expires_at=None,
        deadline_at=None,
        conversation_revision_at_accept=3,
        pipeline_version="web-chat.v1",
        response_json=payload,
        safe_error_code=code,
        accepted_at=NOW,
        updated_at=NOW,
        completed_at=NOW,
    )


def rich_item(i: int) -> RichItem:
    return RichItem(
        product_id=f"prod-{i}",
        name=f"Cremă hidratantă {i}",
        price=89.0 + i,
        reason="Textură ușoară, bună pentru ten uscat.",
        url=f"https://sole.ro/p/{i}",
        image=f"https://cdn.sole.ro/{i}.jpg",
        rating=4.7,
        review_count=120,
        badge="Reducere",
        list_price=119.0 + i,
        badge_tone="danger",
        currency="lei",
        details="Descriere extinsă din catalog.",
        variants=[],
    )


def chip(label: str) -> Chip:
    return Chip(label=label, payload=label)


#: Formele pe care pipeline-ul chiar le produce. Fiecare a fost, la un moment dat, un câmp pe care
#: proiecția v2 îl arunca: chips (recomandare), `offer` (CTA de checkout, NX-137), `comparison`.
CASES: dict[str, Reply] = {
    "recomandare_cu_chips": Reply(
        text="floor",
        rich=RichReply(
            intro="Pentru ten uscat merg astea trei.",
            items=[rich_item(1), rich_item(2), rich_item(3)],
            pick=None,
            education="Cum alegi: caută ceramide și acid hialuronic.",
            chips=[chip("Arată-mi variante sub 100 lei"), chip("Compară primele două")],
            disclaimer="",
        ),
    ),
    "clarificare": Reply(
        text="Ca să te ajut, ce tip de ten ai?",
        suggestions=["Am tenul uscat", "Am tenul gras"],
        pending_question={"field": "skin_type", "attempts": 1},
    ),
    "comparatie": Reply(
        text="floor",
        comparison=Comparison(
            columns=[
                ComparisonColumn(
                    product_id="prod-1",
                    name="Cremă A",
                    price=89.0,
                    list_price=119.0,
                    image="https://cdn.sole.ro/1.jpg",
                    url="https://sole.ro/p/1",
                    rating=4.7,
                ),
                ComparisonColumn(
                    product_id="prod-2",
                    name="Cremă B",
                    price=129.0,
                    list_price=None,
                    image="https://cdn.sole.ro/2.jpg",
                    url="https://sole.ro/p/2",
                    rating=4.5,
                ),
            ],
            rows=[ComparisonRow(label="Textură", values=["Ușoară", "Bogată"])],
            intro="Diferența principală e textura.",
            subtitle="Două creme pentru ten uscat",
            closing=["Dacă vrei ceva rapid dimineața, ia-o pe prima."],
        ),
    ),
    "oferta_checkout": Reply(
        text="Ți-am pregătit coșul.",
        offer=Offer(kind="checkout", label="Finalizează comanda", url="https://sole.ro/cart?ref=x"),
    ),
    "text_simplu": Reply(text="Livrarea durează 1-2 zile lucrătoare."),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_async_v1_body_equals_persisted_payload(name: str) -> None:
    """Conținutul servit asincron e IDENTIC cu payload-ul persistat de calea sincronă.

    Comparăm cheie cu cheie, nu doar „conține": un câmp în plus inventat de proiecție ar fi la
    fel de grav ca unul pierdut — ar însemna că vederea a devenit un al doilea writer.
    """
    persisted = render_web(CASES[name], "ro")
    view = v1_terminal_view(make_row(persisted), "ro")

    content = {k: v for k, v in view.items() if k not in ENVELOPE_KEYS}
    assert content == wire_payload(persisted)
    # Și, mai tare: fiecare cheie nevidă a payload-ului sincron chiar ajunge pe sârmă.
    for key, value in persisted.items():
        if value:
            assert view[key] == value, f"{key} s-a pierdut între sincron și asincron"


def test_chips_and_offer_survive() -> None:
    """Regresia pe care proiecția v2 o producea, pinuită explicit: chips-urile de recomandare și
    CTA-ul de checkout sunt câmpuri NATIVE ale contractului v1, deci nu au nevoie de un kind de
    acțiune ca să existe."""
    recommendation = v1_terminal_view(
        make_row(render_web(CASES["recomandare_cu_chips"], "ro")), "ro"
    )
    assert recommendation["suggestions"] == [
        "Arată-mi variante sub 100 lei",
        "Compară primele două",
    ]
    offer = v1_terminal_view(make_row(render_web(CASES["oferta_checkout"], "ro")), "ro")
    assert offer["offer"]["kind"] == "checkout"
    assert offer["offer"]["url"] == "https://sole.ro/cart?ref=x"


def test_internal_keys_never_reach_the_wire() -> None:
    """Planul de acțiuni (NX-236) și verdictul de grounding (NX-240) stau în ACELAȘI payload
    persistat, dar sunt dovezi server-side. Allowlist, deci o cheie internă nouă nu se scurge
    pentru că a uitat cineva să o treacă pe o blocklist."""
    payload = render_web(CASES["text_simplu"], "ro")
    payload["actions"] = [{"kind": "show_more", "args": {"session_ref": "s"}}]
    payload["grounded_v2"] = {"claims": ["secret"]}
    payload["cine_stie_ce_adaugam_maine"] = {"x": 1}
    view = v1_terminal_view(make_row(payload), "ro")
    for leaked in ("actions", "grounded_v2", "cine_stie_ce_adaugam_maine"):
        assert leaked not in view


def test_envelope_carries_turn_identity() -> None:
    view = v1_terminal_view(make_row(render_web(CASES["text_simplu"], "ro")), "ro")
    assert view["schema_version"] == "web-chat.v1"
    assert view["turn"] == {
        "id": "11111111-1111-1111-1111-111111111111",
        "client_turn_id": "44444444-4444-4444-4444-444444444444",
        "status": "completed",
    }
    assert view["conversation"] == {"id": "22222222-2222-2222-2222-222222222222", "revision": 3}
    assert "error" not in view


@pytest.mark.parametrize("status,code", [("failed", "deadline_exceeded"), ("cancelled", None)])
def test_negative_terminals_are_renderable(status: str, code: str | None) -> None:
    """P6 la nivel de proiecție: niciun terminal mut. Textul e cel persistat de `error_view`
    (localizat pe limba turului), iar `retryable` spune clientului dacă are rost să reîncerce."""
    persisted = error_view(code or "cancelled", "ro")
    view = v1_terminal_view(make_row(persisted, status=status, code=code), "ro")
    assert view["turn"]["status"] == status
    assert view["content"]
    assert view["error"]["code"] == (code or "cancelled")
    assert view["error"]["retryable"] is (code == "deadline_exceeded")


def test_completed_without_anything_renderable_still_answers() -> None:
    """`renderable()` e poarta de la commit, deci cazul nu ar trebui să existe. Dacă apare,
    servim un terminal onest — nu un blank pe care widgetul l-ar afișa ca bulă goală."""
    view = v1_terminal_view(make_row({"content": "", "products": [], "suggestions": []}), "ro")
    assert view["content"]
    assert view["error"]["code"] == "processing_error"


def test_projection_is_pure_and_deterministic() -> None:
    """Două citiri ale aceluiași rând dau aceiași bytes — condiția ca replay-ul (refresh, alt tab,
    reconectare SSE) să nu poată livra alt răspuns decât cel deja dat."""
    row = make_row(render_web(CASES["recomandare_cu_chips"], "ro"))
    assert v1_terminal_view(row, "ro") == v1_terminal_view(row, "ro")


def test_non_terminal_is_refused() -> None:
    with pytest.raises(ValueError):
        v1_terminal_view(make_row(None, status="running"), "ro")


# ── Bootstrap: serverul ANUNȚĂ transportul, clientul nu-l ghicește ──────────────────────────


class _Req:
    def __init__(self) -> None:
        self.client = SimpleNamespace(host="1.2.3.4")
        self.headers: dict[str, str] = {}


class _FakeRedis:
    async def incr(self, key):  # rate limit de bootstrap
        return 1

    async def expire(self, *a):
        return True


async def _bootstrap(monkeypatch: pytest.MonkeyPatch, **overrides):
    async def fake_resolve(token):
        return {"business_id": "b", "session_secret": "sek", "default_locale": "ro"}

    async def fake_redis():
        return _FakeRedis()

    monkeypatch.setattr(wa, "_resolve_token", fake_resolve)
    monkeypatch.setattr(wa, "get_redis", fake_redis)
    for key, value in overrides.items():
        monkeypatch.setattr(wa.get_settings(), key, value)
    return await wa.web_bootstrap("tok", _Req())


@pytest.mark.asyncio
async def test_bootstrap_advertises_async_turns_with_the_view_contract(monkeypatch) -> None:
    """Comutarea sincron↔asincron are UN owner: serverul. Clientul află din bootstrap că poate
    accepta asincron ȘI în ce contract primește rezultatul — nu deduce din forma răspunsului."""
    body = await _bootstrap(monkeypatch, web_turn_v2_enabled=True, web_turn_sse_enabled=True)
    assert body["async_turns"] == {
        "view_contract": "web-chat.v1",
        "sse": True,
        "poll_after_ms": wa.get_settings().web_turn_poll_after_ms,
        # Copy SERVER-OWNED, cheiat pe statusurile de sârmă: clientul face lookup, nu traducere.
        "progress": {
            "accepted": "Am primit mesajul",
            "working": "Pregătesc răspunsul",
            "validating": "Verific răspunsul",
        },
    }


@pytest.mark.asyncio
async def test_bootstrap_drops_the_key_when_async_is_off(monkeypatch) -> None:
    """Cheia DISPARE, nu devine `null` — aceeași regulă ca `sse_url` (NX-290): un client care
    verifică prezența câmpului nu trebuie să creadă că transportul există și e indisponibil.
    Asta e și rollbackul: stingi flagul, clientul cade pe `/web/chat`, zero rebuild de frontend."""
    body = await _bootstrap(monkeypatch, web_turn_v2_enabled=False)
    assert "async_turns" not in body
