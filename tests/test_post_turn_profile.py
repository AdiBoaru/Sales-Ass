"""NX-88 — extractor profil (nano) + lead_score determinist, post-tur.

Trei straturi, ZERO OpenAI/DB real:
  • logică pură (`profile.py`): whitelist, formula de scor, parse-ul nano (ScriptedLLM), redactare;
  • query-ul de scriere (`update_contact_profile_and_score`) cu un conn care CAPTEAZĂ SQL-ul →
    verifică MERGE (`||`), nu overwrite, fără DB;
  • orchestrarea hook-ului (`processor._extract_profile_and_score`) cu query-uri monkeypatch-uite.
"""

from decimal import Decimal
from types import SimpleNamespace

from src.models import Author, Direction, Message
from src.worker import processor as proc
from src.worker import profile
from src.worker.profile import LeadSignals, ProfileDelta


def _msg(direction: Direction, body: str) -> Message:
    author = Author.CONTACT if direction == Direction.INBOUND else Author.BOT
    return Message(direction=direction, author=author, body=body)


def _bare_ctx() -> SimpleNamespace:
    """ctx minim pentru compute_lead_score (fără produse afișate → fără bonus de checkout)."""
    return SimpleNamespace(reply=None, state=None)


# --- redactare PII + prompt ---------------------------------------------------


def test_redact_pii_masks_phone_not_price():
    assert profile._redact_pii("sună la 0712 345 678") == "sună la ***"
    assert profile._redact_pii("nr +40712345678 acum") == "nr *** acum"
    assert profile._redact_pii("preț 82.99 lei") == "preț 82.99 lei"  # prețul NU e telefon


def test_build_prompt_has_antipii_and_latest_message():
    system, user = profile.build_profile_prompt(
        [_msg(Direction.INBOUND, "caut cremă")], SimpleNamespace(body="sub 80 lei"), "ro"
    )
    assert "telefon" in system.lower()  # instrucțiunea anti-PII
    assert "Client: caut cremă" in user
    assert "sub 80 lei" in user  # ultimul mesaj e subliniat


# --- filter_profile_patch (whitelist) ----------------------------------------


def test_filter_keeps_whitelisted_drops_unknown():
    kept, dropped = profile.filter_profile_patch(
        {"skin_type": "uscat", "fav_color": "roz"}, "beauty"
    )
    assert kept == {"skin_type": "uscat"}
    assert dropped == ["fav_color"]  # cheie în afara whitelist-ului → aruncată


def test_filter_normalizes_key_and_trims_value():
    kept, dropped = profile.filter_profile_patch({"Skin_Type": "  Uscat  "}, "beauty")
    assert kept == {"skin_type": "Uscat"}  # cheie lower, valoare trim-uită
    assert dropped == []


def test_filter_drops_non_scalar_values():
    kept, dropped = profile.filter_profile_patch(
        {"concerns": ["acnee", "pete"], "skin_type": "gras"}, "beauty"
    )
    assert kept == {"skin_type": "gras"}
    assert dropped == ["concerns"]  # listă → non-scalar, aruncată


def test_filter_drops_empty_and_overlong_strings():
    kept, dropped = profile.filter_profile_patch(
        {"skin_type": "   ", "fav_brands": "x" * 200}, "beauty"
    )
    assert kept == {}
    assert set(dropped) == {"skin_type", "fav_brands"}


def test_filter_default_whitelist_for_unknown_vertical():
    kept, dropped = profile.filter_profile_patch(
        {"budget_band": "mediu", "skin_type": "uscat"}, "ecommerce-generic"
    )
    assert kept == {"budget_band": "mediu"}  # whitelist default
    assert dropped == ["skin_type"]


# --- compute_lead_score (formulă deterministă) -------------------------------


def test_score_browsing_is_low_but_nonzero():
    assert profile.compute_lead_score(LeadSignals(buying_stage="browsing"), _bare_ctx()) == 10.0


def test_score_midstage_is_sum_of_weights():
    s = LeadSignals(buying_stage="narrowing", asked_price=True)
    assert profile.compute_lead_score(s, _bare_ctx()) == 43.0  # 35 + 8


def test_score_has_budget_weight_isolated():
    # NEsaturat: prinde un regres pe ponderea has_budget (clamp-ul la 100 l-ar masca)
    s = LeadSignals(buying_stage="browsing", has_budget=True)
    assert profile.compute_lead_score(s, _bare_ctx()) == 22.0  # 10 + 12


def test_score_ready_to_buy_flag_weight_isolated():
    # NEsaturat: izolează ponderea flag-ului ready_to_buy (separat de etapa ready_to_buy)
    s = LeadSignals(buying_stage="narrowing", ready_to_buy=True)
    assert profile.compute_lead_score(s, _bare_ctx()) == 55.0  # 35 + 20


def test_score_ready_to_buy_clamped_to_100():
    s = LeadSignals(
        buying_stage="ready_to_buy", has_budget=True, asked_price=True, ready_to_buy=True
    )
    assert profile.compute_lead_score(s, _bare_ctx()) == 100.0  # 80+12+8+20 = 120 → clamp


def test_score_unknown_stage_falls_back_to_base():
    s = LeadSignals(buying_stage="cine-stie", mentioned_product=True)
    assert profile.compute_lead_score(s, _bare_ctx()) == 15.0  # 10 (default) + 5


def test_score_engaged_products_adds_checkout_proximity():
    ctx = SimpleNamespace(
        reply=SimpleNamespace(products=[{"product_id": "p1"}]),
        state=SimpleNamespace(displayed_products=[]),
    )
    assert profile.compute_lead_score(LeadSignals(buying_stage="browsing"), ctx) == 15.0  # 10 + 5


def test_score_engaged_via_state_only():
    # ramura de fallback: reply fără produse, dar setul afișat persistă în state (follow-up tipic)
    ctx = SimpleNamespace(
        reply=SimpleNamespace(products=None),
        state=SimpleNamespace(displayed_products=[{"product_id": "p1"}]),
    )
    assert profile.compute_lead_score(LeadSignals(buying_stage="browsing"), ctx) == 15.0  # 10 + 5


# --- extract_profile (ScriptedLLM, zero OpenAI real) -------------------------


class _ScriptedLLM:
    model_triage = "nano"
    model_agent = "mini"

    def __init__(self, out=None, *, boom=False):
        self._out = out
        self.boom = boom
        self.calls: list[dict] = []

    async def classify_json(self, system, user, *, model=None):
        self.calls.append({"model": model, "user": user})
        if self.boom:
            raise ValueError("json invalid")
        return self._out


async def test_extract_profile_parses_and_forces_nano():
    out = {
        "profile_patch": {"skin_type": "uscat"},
        "lead_signals": {"buying_stage": "narrowing", "has_budget": True},
    }
    llm = _ScriptedLLM(out)
    delta = await profile.extract_profile(
        llm, [_msg(Direction.INBOUND, "caut cremă")], SimpleNamespace(body="sub 80"), "ro"
    )
    assert delta is not None
    assert delta.profile_patch == {"skin_type": "uscat"}
    assert delta.lead_signals.buying_stage == "narrowing" and delta.lead_signals.has_budget is True
    assert llm.calls[0]["model"] == "nano"  # FORȚEAZĂ model_triage (nano), nu mini


async def test_extract_profile_invalid_json_returns_none():
    llm = _ScriptedLLM(boom=True)
    out = await profile.extract_profile(llm, [], SimpleNamespace(body="x"), "ro")
    assert out is None  # fail-soft, nu propagă


async def test_extract_profile_no_content_skips_call():
    llm = _ScriptedLLM({"profile_patch": {}})
    out = await profile.extract_profile(llm, [], SimpleNamespace(body="   "), "ro")
    assert out is None
    assert llm.calls == []  # nici nu cheamă modelul (zero cost)


# --- update_contact_profile_and_score (MERGE, nu overwrite) — fără DB --------


class _CaptureConn:
    def __init__(self):
        self.sql = None
        self.args = None

    async def execute(self, sql, *args):
        self.sql = sql
        self.args = args


async def test_update_query_merges_profile_not_overwrites():
    from src.db.queries.contacts import update_contact_profile_and_score

    conn = _CaptureConn()
    await update_contact_profile_and_score(conn, "biz1", "c1", {"skin_type": "uscat"}, 80.0)
    # MERGE, nu overwrite: patch ($3) e operandul DREPT al `||` → right-operand-wins, deci cheile
    # vechi rămân. Verificăm ORDINEA (nu doar prezența lui `||`) ca un regres de operanzi să pice.
    assert "coalesce(profile" in conn.sql
    assert conn.sql.index("coalesce(profile") < conn.sql.index("|| $3")  # profil în STÂNGA
    assert "|| $3::jsonb" in conn.sql  # patch în DREAPTA
    assert conn.args[0] == "biz1" and conn.args[1] == "c1"  # business_id explicit (P7)
    assert conn.args[2] == '{"skin_type": "uscat"}'  # patch serializat ca jsonb param
    assert conn.args[3] == Decimal("80.00")  # numeric(5,2) → Decimal, nu float


class _MergeConn:
    """Fake conn care EMULează `profile = coalesce(profile,'{}') || $3` (right-operand-wins) ca să
    probeze MERGE-ul CUMULATIV peste tururi (card Happy Path #2) fără DB. Ordinea operanzilor o
    fixează testul de string de mai sus."""

    def __init__(self):
        self.profile: dict = {}
        self.lead_score = None

    async def execute(self, sql, *args):
        import json as _json

        patch = _json.loads(args[2])
        self.profile = {**self.profile, **patch}  # right operand (patch) câștigă conflictele
        self.lead_score = args[3]


async def test_two_turns_merge_accumulates_keys_not_overwrites():
    from src.db.queries.contacts import update_contact_profile_and_score

    conn = _MergeConn()
    await update_contact_profile_and_score(conn, "b", "c", {"skin_type": "uscat"}, 35.0)
    await update_contact_profile_and_score(conn, "b", "c", {"fav_brands": "CeraVe"}, 80.0)
    # turul 2 NU pierde cheia turului 1 (merge, nu overwrite)
    assert conn.profile == {"skin_type": "uscat", "fav_brands": "CeraVe"}
    # turul 3 SUPRASCRIE o cheie existentă (patch = operand drept → câștigă)
    await update_contact_profile_and_score(conn, "b", "c", {"skin_type": "mixt"}, 80.0)
    assert conn.profile["skin_type"] == "mixt" and conn.profile["fav_brands"] == "CeraVe"


# --- orchestrarea hook-ului post-tur -----------------------------------------


class _Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeConn:
    def transaction(self):
        return _Tx()


def _ctx(*, vertical="beauty", lead_score=0.0, route="sales", products=None):
    return SimpleNamespace(
        turn_id="turn-1",  # NX-122: stampat pe event-urile post-tur (replay per-tur)
        route=route,
        language="ro",
        history=[_msg(Direction.INBOUND, "caut cremă pentru ten uscat, buget 80")],
        message=SimpleNamespace(body="buget 80"),
        conversation_id="conv1",
        business=SimpleNamespace(id="biz1", vertical=vertical),
        contact=SimpleNamespace(id="contact1", lead_score=lead_score),
        reply=SimpleNamespace(products=products),
        state=SimpleNamespace(displayed_products=[]),
    )


def _patch(monkeypatch, *, delta, boom_update=False):
    sink: dict = {}

    async def f_extract(llm, history, message, language):
        sink["extract_called"] = True
        sink["extract_history_len"] = len(history)
        return delta

    async def f_window(conn, business_id, conversation_id, limit=20):
        # NX-148: fereastra de extracție (20 mesaje) — stub, testele nu ating DB reală.
        return []

    async def f_update(conn, business_id, contact_id, patch, score):
        if boom_update:
            raise RuntimeError("db down")
        sink["update"] = {"patch": patch, "score": score, "contact_id": contact_id}

    async def f_cost(redis, business_id, amount):
        sink["cost"] = amount

    async def f_events(conn, business_id, events, *, conversation_id=None, contact_id=None):
        sink["events"] = [(e.type, e.properties) for e in events]
        sink["events_contact"] = contact_id

    monkeypatch.setattr(proc, "extract_profile", f_extract)
    monkeypatch.setattr(proc, "get_messages_for_extraction", f_window)
    monkeypatch.setattr(proc, "update_contact_profile_and_score", f_update)
    monkeypatch.setattr(proc, "cost_add", f_cost)
    monkeypatch.setattr(proc, "insert_events", f_events)
    return sink


async def test_happy_path_filters_patch_and_scores(monkeypatch):
    delta = ProfileDelta(
        profile_patch={"skin_type": "uscat", "fav_color": "roz"},  # fav_color în afara whitelist
        lead_signals=LeadSignals(buying_stage="ready_to_buy", has_budget=True, ready_to_buy=True),
    )
    sink = _patch(monkeypatch, delta=delta)
    await proc._extract_profile_and_score(
        _FakeConn(), object(), _ctx(), object(), shadow_mode=False
    )
    # patch FILTRAT pe whitelist (skin_type păstrat, fav_color aruncat)
    assert sink["update"]["patch"] == {"skin_type": "uscat"}
    assert sink["update"]["contact_id"] == "contact1"
    # scor DETERMINIST din cod (clamp 100), nu din numărul LLM-ului
    assert sink["update"]["score"] == 100.0
    # evenimente (P12 — chei + contoare, fără valori; NX-122 — + turn_id de corelare)
    assert ("profile_key_dropped", {"key": "fav_color", "turn_id": "turn-1"}) in sink["events"]
    assert (
        "profile_updated",
        {"keys_set": ["skin_type"], "dropped": 1, "turn_id": "turn-1"},
    ) in sink["events"]
    assert ("lead_score_updated", {"old": 0.0, "new": 100.0, "turn_id": "turn-1"}) in sink["events"]
    assert sink["events_contact"] == "contact1"
    assert sink["cost"] > 0  # apelul nano contabilizat (G2c)


async def test_score_only_update_when_patch_empty(monkeypatch):
    delta = ProfileDelta(
        profile_patch={}, lead_signals=LeadSignals(buying_stage="comparing", asked_price=True)
    )
    sink = _patch(monkeypatch, delta=delta)
    await proc._extract_profile_and_score(
        _FakeConn(), object(), _ctx(), object(), shadow_mode=False
    )
    assert sink["update"]["patch"] == {}
    assert sink["update"]["score"] == 63.0  # comparing 55 + asked_price 8
    types = [t for t, _ in sink["events"]]
    assert "lead_score_updated" in types and "profile_updated" not in types


async def test_no_write_when_empty_patch_and_score_unchanged(monkeypatch):
    # browsing → 10; contactul e deja la 10 → nicio scriere, niciun event de update
    delta = ProfileDelta(profile_patch={}, lead_signals=LeadSignals(buying_stage="browsing"))
    sink = _patch(monkeypatch, delta=delta)
    await proc._extract_profile_and_score(
        _FakeConn(), object(), _ctx(lead_score=10.0), object(), shadow_mode=False
    )
    assert "update" not in sink
    assert "events" not in sink
    assert sink["cost"] > 0  # apelul nano a avut loc oricum → contat


async def test_llm_none_skips_everything(monkeypatch):
    sink = _patch(monkeypatch, delta=ProfileDelta(profile_patch={"skin_type": "uscat"}))
    await proc._extract_profile_and_score(_FakeConn(), object(), _ctx(), None, shadow_mode=False)
    assert "extract_called" not in sink and "update" not in sink and "cost" not in sink


async def test_shadow_mode_skips(monkeypatch):
    sink = _patch(monkeypatch, delta=ProfileDelta(profile_patch={"skin_type": "uscat"}))
    await proc._extract_profile_and_score(_FakeConn(), object(), _ctx(), object(), shadow_mode=True)
    assert "extract_called" not in sink


async def test_free_layer_no_route_skips(monkeypatch):
    # ctx.route None ⟺ tur deflectat de free-layer/cache/gates → niciun apel nano
    sink = _patch(monkeypatch, delta=ProfileDelta(profile_patch={"skin_type": "uscat"}))
    await proc._extract_profile_and_score(
        _FakeConn(), object(), _ctx(route=None), object(), shadow_mode=False
    )
    assert "extract_called" not in sink


async def test_extract_returns_none_no_update(monkeypatch):
    sink = _patch(monkeypatch, delta=None)  # parse/API fail → None
    await proc._extract_profile_and_score(
        _FakeConn(), object(), _ctx(), object(), shadow_mode=False
    )
    assert sink["extract_called"] and "update" not in sink and "cost" not in sink


async def test_db_failure_is_best_effort(monkeypatch):
    delta = ProfileDelta(
        profile_patch={"skin_type": "uscat"}, lead_signals=LeadSignals(buying_stage="narrowing")
    )
    sink = _patch(monkeypatch, delta=delta, boom_update=True)
    # NU trebuie să propage (turul a răspuns deja, reply-ul e în outbox)
    await proc._extract_profile_and_score(
        _FakeConn(), object(), _ctx(), object(), shadow_mode=False
    )
    assert "update" not in sink  # a aruncat → nimic capturat
    assert "events" not in sink  # am sărit la except înainte de insert
    assert sink["cost"] > 0  # apelul nano a avut loc înainte de eșecul DB


async def test_dropped_keys_emitted_even_without_db_write(monkeypatch):
    # doar o cheie non-whitelist + scor neschimbat → niciun UPDATE, dar semnalul NX-43 se emite
    delta = ProfileDelta(
        profile_patch={"fav_color": "roz"}, lead_signals=LeadSignals(buying_stage="browsing")
    )
    sink = _patch(monkeypatch, delta=delta)
    await proc._extract_profile_and_score(
        _FakeConn(), object(), _ctx(lead_score=10.0), object(), shadow_mode=False
    )
    assert "update" not in sink  # patch gol post-whitelist + scor 10==10 → nicio scriere
    # semnalul NX-43 rămâne (+ turn_id NX-122)
    assert ("profile_key_dropped", {"key": "fav_color", "turn_id": "turn-1"}) in sink["events"]
    assert sink["events_contact"] == "contact1"


async def test_kill_switch_disables_hook(monkeypatch):
    # profile_extraction_enabled=False → hook complet sărit (convenția test_faq.py)
    from src.config import get_settings

    monkeypatch.setattr(get_settings(), "profile_extraction_enabled", False)
    sink = _patch(monkeypatch, delta=ProfileDelta(profile_patch={"skin_type": "uscat"}))
    await proc._extract_profile_and_score(
        _FakeConn(), object(), _ctx(), object(), shadow_mode=False
    )
    assert "extract_called" not in sink
