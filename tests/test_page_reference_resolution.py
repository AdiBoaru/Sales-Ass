"""NX-234 — „acesta" pe o pagină de produs: ancora canonică, fără mesaj compus de frontend.

Cazul care justifică tot cardul: un client deschide un PDP și scrie „ce părere ai despre acesta?".
Fără ancoră, asistentul n-are ce rezolva (zero produse afișate) și fie ghicește, fie cere o
clarificare pe care clientul o consideră absurdă — se uită la produs. Singura „soluție" fără
backend ar fi ca frontendul să compună întrebarea, adică exact ce interzice boundary-ul.

Testele acoperă precedența (`named` > `ordinal` > `page` > `single`), non-regresia pe canalele
fără context și faptul că ancora circulă ca ID + nume REHIDRATATE, niciodată ca fapte de browser.
"""

from contextlib import asynccontextmanager

import pytest

from src.agent import deterministic as det
from src.agent import reference_resolver as rr
from src.catalog.context_resolver import ProductEvidence, SurfaceContext
from src.config import get_settings
from src.models import ConversationState, ProductRef, Route, RouteDecision, TurnContext
from src.worker.turn_snapshot import (
    ActorRef,
    ConversationRef,
    InputSnapshot,
    MemorySnapshot,
    TenantRef,
    TurnSnapshot,
)

PAGE_ID = "0f6c2c3a-4c1e-4b1e-9a0d-2b0d2f1e7a11"


def _snapshot(product: ProductEvidence | None) -> TurnSnapshot:
    return TurnSnapshot(
        schema_version="turn-snapshot.v1",
        turn_id="t1",
        tenant=TenantRef(business_id="b1"),
        actor=ActorRef(contact_id="ct1"),
        conversation=ConversationRef(conversation_id="c1", revision=1),
        input=InputSnapshot(raw=None),
        surface=SurfaceContext(
            surface="product", status="resolved" if product else "partial", product=product
        ),
        memory=MemorySnapshot(),
        locale="ro",
    )


def _page(name="Ser Niacinamidă", price=89.0) -> TurnSnapshot:
    return _snapshot(ProductEvidence(product_id=PAGE_ID, name=name, price=price))


def _refs(*pairs) -> list[ProductRef]:
    return [ProductRef(pid, name, 10.0) for pid, name in pairs]


# ── Precedența ───────────────────────────────────────────────────────────────


def test_deictic_on_a_pdp_anchors_the_page_product():
    r = rr.resolve_product_reference(
        "ce părere ai despre acesta?", [], page=rr.PageAnchor(PAGE_ID, "Ser Niacinamidă")
    )
    assert r.product_id == PAGE_ID
    assert r.source == "page" and r.outcome == "resolved"


def test_an_explicitly_named_product_beats_the_page_anchor():
    """Ancora spune unde SE AFLĂ clientul; numele spune ce VREA. A doua câștigă."""
    refs = _refs(("p1", "Cremă hidratantă Petală"), ("p2", "Ser cu vitamina C"))
    r = rr.resolve_product_reference(
        "de fapt ce zici de crema hidratantă?", refs, page=rr.PageAnchor(PAGE_ID, "Ser")
    )
    assert r.product_id == "p1" and r.source == "named"


def test_an_ordinal_beats_the_page_anchor_and_points_at_the_list():
    refs = _refs(("p1", "Primul produs"), ("p2", "Al doilea produs"))
    r = rr.resolve_product_reference("spune-mi de a doua", refs, page=rr.PageAnchor(PAGE_ID))
    assert r.product_id == "p2" and r.source == "ordinal" and r.index == 1


def test_the_page_beats_a_single_stale_displayed_product():
    """Setul afișat poate fi de acum trei tururi; pagina e ACUM."""
    r = rr.resolve_product_reference(
        "și asta cum e?", _refs(("p_old", "Produs vechi")), page=rr.PageAnchor(PAGE_ID)
    )
    assert r.product_id == PAGE_ID and r.source == "page"


def test_without_a_page_anchor_the_behaviour_is_exactly_the_old_one():
    refs = _refs(("p1", "Unicul produs"))
    assert rr.resolve_product_reference("și asta?", refs, page=None) == (
        rr.resolve_from_displayed("și asta?", refs)
    )
    assert rr.resolve_from_displayed("și asta?", refs).source == "single"


def test_no_anchor_at_all_is_ambiguous_not_a_guess():
    refs = _refs(("p1", "Cremă A"), ("p2", "Cremă B"))
    r = rr.resolve_from_displayed("și asta?", refs)
    assert r.product_id is None and r.outcome == "ambiguous"


def test_ordinal_out_of_range_does_not_select_anything():
    refs = _refs(("p1", "A"), ("p2", "B"))
    assert rr.match_ordinal("a treia", len(refs)) is None


def test_a_common_token_never_decides_between_two_products():
    refs = _refs(("p1", "Cremă de zi"), ("p2", "Cremă de noapte"))
    assert rr.match_name("vreau crema", [r.name for r in refs]) is None


def test_diacritics_do_not_change_the_match():
    refs = _refs(("p1", "Ser cu Niacinamidă"), ("p2", "Balsam"))
    assert rr.match_name("ce zici de serul cu niacinamida?", [r.name for r in refs]) == 0


# ── Ancora în pipeline-ul determinist ────────────────────────────────────────


def _ctx(*, displayed=(), snapshot=None) -> TurnContext:
    from types import SimpleNamespace

    ctx = TurnContext(
        turn_id="t1",
        business=SimpleNamespace(id="b1", domain_pack=None),
        contact=SimpleNamespace(id="ct1", profile={}, lifecycle="new"),
        message=SimpleNamespace(body="ce părere ai despre acesta?", content_type="text"),
        conversation_id="c1",
        state=ConversationState(displayed_products=list(displayed)),
    )
    ctx.route = RouteDecision(route=Route.SALES)
    ctx.snapshot = snapshot
    return ctx


def test_anchor_refs_appends_the_page_product_at_the_end():
    """Ordinea contează: „a doua" se referă la lista AFIȘATĂ, deci pagina nu se bagă în față."""
    ctx = _ctx(displayed=_refs(("p1", "A"), ("p2", "B")), snapshot=_page())
    refs = det._anchor_refs(ctx)
    assert [r.product_id for r in refs] == ["p1", "p2", PAGE_ID]


def test_page_product_is_not_duplicated_when_already_displayed():
    ctx = _ctx(displayed=_refs((PAGE_ID, "Ser Niacinamidă")), snapshot=_page())
    assert [r.product_id for r in det._anchor_refs(ctx)] == [PAGE_ID]


def test_without_snapshot_the_anchor_set_is_just_the_displayed_products():
    ctx = _ctx(displayed=_refs(("p1", "A")))
    assert [r.product_id for r in det._anchor_refs(ctx)] == ["p1"]


def test_anchor_carries_the_rehydrated_name_not_a_browser_claim():
    ctx = _ctx(snapshot=_page(name="Ser Niacinamidă 10%", price=89.0))
    ref = det._page_anchor_ref(ctx)
    assert ref.product_id == PAGE_ID and ref.name == "Ser Niacinamidă 10%" and ref.price == 89.0


def test_shadow_snapshot_without_evidence_yields_no_anchor():
    """Cu prompt exposure stins, faptele nu există în obiect → nici ancora."""
    ctx = _ctx(snapshot=_snapshot(None))
    assert det._page_anchor_ref(ctx) is None
    assert rr.page_anchor_from_snapshot(ctx.snapshot) is None


def test_resolve_anchor_emits_provenance():
    ctx = _ctx(snapshot=_page())
    selected = det._resolve_anchor(ctx, "ce părere ai despre acesta?")
    assert selected.product_id == PAGE_ID
    emitted = [e for e in ctx.events if e.type == "web_reference_resolved"]
    assert emitted and emitted[0].properties["source"] == "page"


# ── Intențiile deterministe se pot ancora pe PDP fără nimic afișat ───────────


class _FakeDeps:
    def __init__(self, products):
        self.products = products

    def db(self, operation: str = "?"):
        @asynccontextmanager
        async def cm():
            yield None

        return cm()


@pytest.fixture
def _catalog(monkeypatch):
    """Catalogul răspunde cu produsul paginii — rehidratat, ca în runtime."""
    seen: list[list[str]] = []

    async def fake_get_products_by_ids(conn, business_id, ids, **kw):
        seen.append(list(ids))
        return [
            {
                "id": PAGE_ID,
                "name": "Ser Niacinamidă",
                "price": 89.0,
                "url": "https://shop.example.com/p/ser",
                "rating": 4.8,
                "review_count": 120,
                "review_summary": "Textură plăcută.",
                "top_pros": ["absorbție rapidă"],
                "top_cons": [],
                "availability": "in_stock",
                "ai_summary": "Ser cu niacinamidă pentru pori vizibili.",
                "attributes": {},
            }
        ]

    monkeypatch.setattr(det, "get_products_by_ids", fake_get_products_by_ids)
    return seen


async def test_detail_intent_on_a_pdp_with_nothing_displayed(_catalog, monkeypatch):
    """Cazul din card: zero produse afișate, dar clientul e pe pagina produsului."""
    monkeypatch.setattr(get_settings(), "detail_intent_enabled", True)
    ctx = _ctx(snapshot=_page())
    ctx.message.body = "spune-mi mai multe"
    handled = await det.try_pre_intents(ctx, _FakeDeps([]))
    assert handled is True
    assert _catalog[-1] == [PAGE_ID]  # ancora canonică, nu un id afirmat de browser
    assert ctx.reply is not None and ctx.retrieval.products[0]["id"] == PAGE_ID


async def test_without_the_page_anchor_the_same_turn_is_not_handled(_catalog, monkeypatch):
    """Non-regresie: pe orice canal fără context de pagină, comportamentul e cel de dinainte."""
    monkeypatch.setattr(get_settings(), "detail_intent_enabled", True)
    ctx = _ctx()
    ctx.message.body = "spune-mi mai multe"
    assert await det.try_pre_intents(ctx, _FakeDeps([])) is False
    assert ctx.reply is None


async def test_link_intent_uses_the_catalog_url_of_the_page_product(_catalog, monkeypatch):
    monkeypatch.setattr(get_settings(), "link_intent_enabled", True)
    ctx = _ctx(snapshot=_page())
    ctx.message.body = "unde îl cumpăr?"
    assert await det.try_pre_intents(ctx, _FakeDeps([])) is True
    assert _catalog[-1] == [PAGE_ID]
    assert ctx.reply.offer.url == "https://shop.example.com/p/ser"
