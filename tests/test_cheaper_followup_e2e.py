"""E2E reproducere (NX — bug live „mai ieftin"): DOUĂ tururi prin pipeline-ul REAL via
`processor.handle_turn`, observând round-trip-ul de state (displayed_products) și dacă calea
DETERMINISTĂ `cheaper_intent` (agent.py:691) chiar se aprinde cap-coadă.

NU atinge Supabase/OpenAI reale: query-urile de DB sunt monkeypatch-uite pe un MAGAZIN în memorie
(`_Store`) — dar `conversations.state` se persistă/re-hidratează prin codul REAL
(`patch_conversation_state` fake stochează jsonb-ul → `from_jsonb` îl reîncarcă la turul 2). LLM-ul
e scriptat (`ScriptedLLM`). Pipeline-ul rulat = `DEFAULT_STAGES` (gates→language→clarify→greeting→
alias→cache→faq→triage→agent→fallback), exact ca în producție. Calea SYNC web (deliver=False, ca
`src/web/app.py` POST /web/chat).

Turul 1: search → 3 produse (88.99 / 58.99 / 97.99) → reply rich → displayed_products = cele 3.
Turul 2 „ceva mai ieftin": state re-hidratat → `search_cheaper_than` (stub) → DOAR produsul 49.99,
  zero padding cu 88.99/97.99 (cheaper_intent s-a aprins).
Turul 2-empty: `search_cheaper_than` → [] → mesajul „cea mai ieftină", fără carduri.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import src.worker.processor as proc
import src.worker.stages.alias as alias_mod
import src.worker.stages.cache as cache_mod
import src.worker.stages.faq as faq_mod
import src.worker.stages.triage as triage_mod
from src.agent import planner as planner_mod
from src.models import BusinessConfig, Contact
from src.tools import catalog_tools as ct
from src.worker.runner import DEFAULT_STAGES
from src.worker.stages import agent as agent_mod

DEMO_BIZ = "6098812a-50fc-44bd-a1ba-bc77e6399158"

# Cele 3 produse afișate la turul 1 — cel mai ieftin AFIȘAT = 58.99 (ca în bug-ul live).
SHOWN = [
    {
        "id": "p-hi",
        "name": "Ardent Lab Calm 347",
        "brand": "Ardent",
        "price": 88.99,
        "url": "https://shop/p-hi",
        "ai_summary": "hidratare bogată",
        "availability": "in_stock",
        "rating": 4.4,
        "top_pros": ["textură plăcută"],
    },
    {
        "id": "p-cheapest-shown",
        "name": "Pure Arc Daily 284",
        "brand": "Pure Arc",
        "price": 58.99,
        "url": "https://shop/p-mid",
        "ai_summary": "hidratare zilnică",
        "availability": "in_stock",
        "rating": 4.6,
        "top_pros": ["pentru zi"],
    },
    {
        "id": "p-top",
        "name": "Pure Arc Calm 283",
        "brand": "Pure Arc",
        "price": 97.99,
        "url": "https://shop/p-top",
        "ai_summary": "calmare intensă",
        "availability": "in_stock",
        "rating": 4.7,
        "top_pros": ["calmează"],
    },
]
# Produsul real STRICT mai ieftin (49.99 < 58.99) pe care botul îl rata în bug.
CHEAPER = {
    "id": "p-cheap",
    "name": "Rhea Organics Soft 466",
    "brand": "Rhea",
    "price": 49.99,
    "url": "https://shop/cheap",
    "ai_summary": "crema lejeră accesibilă",
    "availability": "in_stock",
    "rating": 4.1,
    "top_pros": ["accesibil"],
}


# --------------------------------------------------------------------------- #
# Magazin în memorie + fake conn (doar `transaction()` ca async CM)
# --------------------------------------------------------------------------- #


class _Tx:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *a):
        return False


class _FakeConn:
    """Stand-in pentru asyncpg.Connection: handle_turn + get_or_create_* deschid o tranzacție
    pe ea; nimic altceva nu o atinge direct (query-urile sunt monkeypatch-uite)."""

    def transaction(self):
        return _Tx()


class _Store:
    """O singură conversație persistentă peste tururi. `state` e jsonb (dict) — exact ca în DB:
    serializat la scriere, re-hidratat de ConversationState.from_jsonb la turul următor."""

    def __init__(self) -> None:
        self.state: dict[str, Any] = {}
        self.state_version: int = 0
        self.messages: list[dict[str, Any]] = []
        self.outbox: list[dict[str, Any]] = []
        self.llm_holder: dict[str, Any] = {"llm": None}

    def conv(self) -> dict[str, Any]:
        # Round-trip onest jsonb: dump→load, ca un câmp json real (fără referințe partajate).
        return {
            "id": "conv-1",
            "status": "open",
            "bot_active": True,
            "handoff_until": None,
            "last_inbound_at": None,
            "last_outbound_at": None,
            "last_message_at": None,
            "locale": "ro",
            "state": json.loads(json.dumps(self.state)),
            "state_version": self.state_version,
            "risk_flags": [],
            "shadow_mode": False,
        }


class ScriptedLLM:
    """Triaj (classify_json) + bucla agentului (run_tool_loop) + recomandarea rich
    (complete_schema), scriptate per-tur. Embed → vector neutru (cache/faq stub-uite = miss)."""

    model_triage = "nano"
    model_agent = "mini"

    def __init__(self, *, mode: str, tool_products: list[dict[str, Any]]):
        self.mode = mode
        self._tool_products = tool_products

    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]

    async def moderate(self, text, *, model=None):  # gates: fail-open (non-flagged)
        from src.agent.llm import ModerationResult

        return ModerationResult(flagged=False, categories=[])

    async def classify_json(self, system, user, *, model=None):
        # Ambele tururi = SALES, confidence HIGH (follow-up-ul „mai ieftin" continuă sales — exact
        # ce cere promptul de triaj la linia 125-127). NU clarify, NU low-confidence.
        return {
            "route": "sales",
            "category_key": None,
            "missing_field": None,
            "reply": None,
            "confidence": "high",
            "slots": {},
        }

    async def run_tool_loop(self, system, user, tools, execute, *, max_steps=3, model=None):
        # Turul 1: modelul caută → execute populează `retrieved` cu cele 3 produse. Turul 2: modelul
        # tot cheamă search (set vechi); codul determinist `cheaper_intent` îl ÎNLOCUIEȘTE.
        await execute("search_products", {"query": "crema hidratanta", "limit": 6})
        return ""  # fără proză → calea rich (complete_schema) compune

    async def complete_schema(self, system, user, schema, *, model=None):
        # Recomandare rich validă peste produsele din ACEST tur (membership-grounded de compose).
        ids = _ids_from_bundle(user, self._tool_products)
        items = [{"product_id": pid, "pro_index": 0, "fit_clause": "se potrivește"} for pid in ids]
        return {
            "intro": "Uite ce am pentru tine:",
            "items": items,
            "pick": None,
            "education": None,
            "suggestions": [],
        }

    async def complete(self, system, user, *, model=None):
        return "Uite o variantă pentru tine."


def _ids_from_bundle(user: str, products: list[dict[str, Any]]) -> list[str]:
    """Care produse au ajuns în bundle-ul rich (din `retrieved` al turului) — le luăm pe cele
    al căror id apare în promptul user trimis la complete_schema."""
    return [p["id"] for p in products if str(p["id"]) in user]


# --------------------------------------------------------------------------- #
# Fixture: monkeypatch toate query-urile DB pe magazinul în memorie
# --------------------------------------------------------------------------- #


@pytest.fixture
def store(monkeypatch):
    st = _Store()

    # --- processor: identitate, conversație, mesaje, outbox, dedupe, analytics --------------
    async def _claim_inbound(conn, business_id, pid):
        return True  # nu e duplicat

    async def _mark_completed(conn, business_id, pid):
        return None

    async def _get_contact(conn, business_id, channel_kind, external_id, **k):
        return Contact(id="c-1", business_id=business_id, display_name="Ana")

    async def _get_conv(conn, business_id, contact_id, channel_id, **k):
        return st.conv()

    async def _touch_inbound(conn, business_id, conv_id):
        return None

    async def _insert_message(conn, business_id, conv_id, contact_id, direction, author, **k):
        st.messages.append({"direction": str(direction), "body": k.get("body")})
        return f"m-{len(st.messages)}"

    async def _get_recent(conn, business_id, conv_id, **k):
        return []  # istoric gol — round-trip-ul de state e singura punte între tururi

    async def _get_summary(conn, business_id, conv_id, **k):
        return None

    async def _enqueue_outbox(conn, business_id, conv_id, idem, payload, **k):
        st.outbox.append({"idem": idem, "payload": payload})
        return f"ob-{len(st.outbox)}"

    async def _patch_state(conn, business_id, conv_id, new_state, expected_version, **k):
        # Persistă jsonb-ul (dump→load) → turul următor îl re-hidratează prin from_jsonb. ASTA e
        # round-trip-ul pe care îl testăm: ce scrie Sender-ul la T1 trebuie să producă
        # displayed_products valide la T2.
        st.state = json.loads(json.dumps(new_state))
        st.state_version = expected_version + 1
        return st.state_version

    async def _insert_events(conn, business_id, events, *, conversation_id=None, contact_id=None):
        return None

    # POST-tur (cache write-back / summarizer / profil) → no-op (nu afectează reply-ul turului).
    async def _noop_writeback(*a, **k):
        return None

    async def _noop_summarize(*a, **k):
        return None

    async def _noop_profile(*a, **k):
        return None

    monkeypatch.setattr(proc, "claim_inbound", _claim_inbound)
    monkeypatch.setattr(proc, "mark_inbound_completed", _mark_completed)
    monkeypatch.setattr(proc, "get_or_create_contact", _get_contact)
    monkeypatch.setattr(proc, "get_or_create_conversation", _get_conv)
    monkeypatch.setattr(proc, "touch_last_inbound", _touch_inbound)
    monkeypatch.setattr(proc, "insert_message", _insert_message)
    monkeypatch.setattr(proc, "get_recent_messages", _get_recent)
    monkeypatch.setattr(proc, "get_summary_for_context", _get_summary)
    monkeypatch.setattr(proc, "enqueue_outbox", _enqueue_outbox)
    monkeypatch.setattr(proc, "patch_conversation_state", _patch_state)
    monkeypatch.setattr(proc, "_persist_events", _noop_writeback)
    monkeypatch.setattr(proc, "run_aftercare", _noop_writeback)

    # --- straturi gratuite (alias/cache/faq) → MISS, ca să ajungem la triaj+agent -----------
    async def _no_alias(conn, business_id, phrase_norm):
        return None

    async def _no_faq_answer(conn, business_id, target_id, language):
        return None

    async def _cache_exact_miss(conn, business_id, language, h, **k):
        return None

    async def _cache_semantic_miss(conn, business_id, language, emb, **k):
        return None

    async def _faq_semantic_miss(conn, business_id, language, emb, **k):
        return None

    # Stagiile importă funcțiile în PROPRIUL namespace (`from ... import x`) → patch pe modulul
    # STAGIULUI, nu pe modulul de query (altfel nu prinde — vezi run-ul anterior cu AttributeError).
    monkeypatch.setattr(alias_mod, "lookup_alias", _no_alias)
    monkeypatch.setattr(alias_mod, "get_faq_answer", _no_faq_answer)
    monkeypatch.setattr(cache_mod, "exact_lookup", _cache_exact_miss)
    monkeypatch.setattr(cache_mod, "semantic_lookup", _cache_semantic_miss)
    monkeypatch.setattr(faq_mod, "semantic_lookup", _faq_semantic_miss)

    # --- triaj + agent: categorii/aliase pentru promptul generat din DB ---------------------
    async def _cat_slugs(conn, business_id):
        return ["creme-hidratante"]

    async def _cat_names(conn, business_id):
        return ["Creme hidratante"]

    async def _routing_aliases(conn, business_id, **k):
        return []

    # list_category_slugs e importat în triage.py; list_category_names/list_routing_aliases în agent
    monkeypatch.setattr(triage_mod, "list_category_slugs", _cat_slugs)
    monkeypatch.setattr(agent_mod, "list_category_names", _cat_names)
    monkeypatch.setattr(agent_mod, "list_routing_aliases", _routing_aliases)

    # handle_turn își ia LLM-ul prin proc.get_llm() (NU prin parametru) → injectăm ScriptedLLM
    # printr-un holder mutabil (schimbat per-tur de `_turn`). redis=None → cost guard dă llm-ul.
    holder: dict[str, Any] = {"llm": None}
    monkeypatch.setattr(proc, "get_llm", lambda: holder["llm"])
    st.llm_holder = holder

    return st


def _biz() -> BusinessConfig:
    # Mono-lingv (un singur locale) → language_stage e no-op, fără DB de limbă (zero lookup).
    return BusinessConfig(
        id=DEMO_BIZ, slug="nativex-demo", name="Sole Demo", supported_locales=["ro"]
    )


def _event(body: str) -> dict[str, Any]:
    return {
        "channel_kind": "web",
        "channel_account_id": "web-demo",
        "sender_external_id": "web-user-1",
        "provider_msg_id": None,  # web sync: fără dedupe id (ca POST /web/chat)
        "content_type": "text",
        "body": body,
        "media_id": None,
        "sender_name": "Ana",
    }


def _patch_catalog_search(monkeypatch, products):
    """Turul 1: search_products (semantic+lexical) → cele 3 produse afișate."""

    async def fake_semantic(conn, business_id, vec, **k):
        return products

    async def fake_lexical(conn, business_id, **k):
        return []

    async def has_emb(conn, business_id):
        return True

    monkeypatch.setattr(ct, "has_embeddings", has_emb)
    monkeypatch.setattr(ct, "search_products_semantic", fake_semantic)
    monkeypatch.setattr(ct, "search_products_lexical", fake_lexical)


# --------------------------------------------------------------------------- #
# Tururi
# --------------------------------------------------------------------------- #


async def _turn(conn, biz, store, body, llm):
    store.llm_holder["llm"] = llm  # proc.get_llm() (patched) îl întoarce pe ăsta acestui tur
    return await proc.handle_turn(
        conn, biz, "ch-1", _event(body), redis=None, stages=DEFAULT_STAGES, deliver=False
    )


async def test_turn1_persists_displayed_then_turn2_cheaper_only(monkeypatch, store):
    conn = _FakeConn()
    biz = _biz()

    # --- TURUL 1: caut o cremă → 3 produse rich, state.displayed_products persistat ----------
    _patch_catalog_search(monkeypatch, SHOWN)
    llm1 = ScriptedLLM(mode="search", tool_products=SHOWN)
    r1 = await _turn(conn, biz, store, "caut o cremă hidratantă", llm1)

    assert r1.reply is not None
    shown_ids = [p["product_id"] for p in (r1.reply.products or [])]
    assert set(shown_ids) == {"p-hi", "p-cheapest-shown", "p-top"}, shown_ids
    # ROUND-TRIP: state-ul persistat (din _patch_state) are cele 3, cu price (cerut de from_jsonb).
    persisted = store.state.get("displayed_products") or []
    assert {p["product_id"] for p in persisted} == {"p-hi", "p-cheapest-shown", "p-top"}
    assert all("price" in p and "name" in p for p in persisted)
    prices_shown = sorted(float(p["price"]) for p in persisted)
    assert prices_shown == [58.99, 88.99, 97.99]

    # --- TURUL 2: „ceva mai ieftin" → DOAR produsul de 49.99 (cheaper_intent aprins) ---------
    captured: dict[str, Any] = {}

    async def fake_cheaper(conn, business_id, ref_ids, max_excl, *, limit=6):
        captured["baseline"] = max_excl
        captured["ref_ids"] = list(ref_ids)
        return [dict(CHEAPER)]  # UN singur produs strict mai ieftin

    monkeypatch.setattr(planner_mod, "search_cheaper_than", fake_cheaper)
    llm2 = ScriptedLLM(mode="search", tool_products=SHOWN)  # modelul ar refolosi setul vechi…
    r2 = await _turn(conn, biz, store, "ceva mai ieftin", llm2)

    # cheaper_intent a chemat search_cheaper_than cu baseline = cel mai ieftin AFIȘAT (58.99)
    assert captured.get("baseline") == 58.99, captured
    assert set(captured.get("ref_ids", [])) == {"p-hi", "p-cheapest-shown", "p-top"}
    # reply-ul turului 2 arată DOAR produsul mai ieftin — zero padding cu 88.99/97.99
    assert r2.reply is not None
    t2_ids = [p["product_id"] for p in (r2.reply.products or [])]
    assert t2_ids == ["p-cheap"], t2_ids
    t2_prices = [float(p["price"]) for p in (r2.reply.products or [])]
    assert t2_prices == [49.99]
    assert all(pr < 58.99 for pr in t2_prices)  # nimic ≥ cel mai ieftin afișat


async def test_turn2_empty_cheaper_returns_graceful_msg(monkeypatch, store):
    conn = _FakeConn()
    biz = _biz()

    # TURUL 1 (identic) — populează state.displayed_products.
    _patch_catalog_search(monkeypatch, SHOWN)
    llm1 = ScriptedLLM(mode="search", tool_products=SHOWN)
    await _turn(conn, biz, store, "caut o cremă hidratantă", llm1)
    assert store.state.get("displayed_products")

    # TURUL 2: nimic strict mai ieftin → mesaj determinist, FĂRĂ carduri (niciodată padding, P6).
    async def empty_cheaper(conn, business_id, ref_ids, max_excl, *, limit=6):
        return []

    monkeypatch.setattr(planner_mod, "search_cheaper_than", empty_cheaper)
    llm2 = ScriptedLLM(mode="search", tool_products=SHOWN)
    r2 = await _turn(conn, biz, store, "ceva mai ieftin", llm2)

    assert r2.reply is not None
    assert "cea mai ieftină" in r2.reply.text.lower()
    assert not r2.reply.products  # zero carduri (NU re-afișează setul vechi)
