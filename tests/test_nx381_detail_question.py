"""NX-381 — întrebarea clientului despre un produs primește un RĂSPUNS din fișă, nu fișa standard.

Lanțul: `Act.question` (modelul de interpretare, kernel.v6.3) → `TurnPlan.question` (plannerul, pe
`detail` / `compare`) → executorul de `detail` → `detail_answer` (un apel, poarta de adevăr pe fișă)
→ fișa de azi pe orice eșec."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from src.agent import detail_answer as da
from src.agent import kernel_executors as kx
from src.config import get_settings
from src.conversation.interpretation import TurnPlan
from src.conversation.kernel_trace import KernelTrace, redact_trace
from tests.kernel import stage_harness as sh
from tests.test_interpreted_turn_c import _ctx, _outcome, _planned
from tests.test_kernel_planner import _ids, _interp, _only, _plan, _ref

PRODUCT = {
    "id": "p1",
    "name": "COSRX The Retinol 0.1 Cream - crema de noapte cu retinol",
    "price": 142.0,
    "availability": "in_stock",
    "rating": 4.89,
    "review_count": 19,
    "url": "https://example.test/p1",
    "attributes": {},
    "sections": [
        {"kind": "summary", "body": "Crema de noapte cu 0,1% retinol pur si pantenol."},
        {"kind": "usage", "body": "Se aplica seara, de 3 ori pe saptamana la inceput."},
    ],
    "top_pros": ["se absoarbe repede"],
    "ingredients_db": ["retinol", "pantenol"],
    "faqs": [{"question": "Merge cu vitamina C?", "answer": "Da, dar dimineata vitamina C."}],
}


@pytest.fixture
def electronics(monkeypatch):
    cat = sh.catalog("electronics")
    sh.install(monkeypatch, cat, executors=True)
    return cat


class AnswerLLM:
    """`complete` scriptat: întoarce `answer` (sau aruncă) și ține minte cererea."""

    def __init__(self, answer: str | Exception) -> None:
        self.answer = answer
        self.calls: list[tuple[str, str]] = []

    async def complete(self, system: str, user: str, **kw) -> str:
        self.calls.append((system, user))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


# --- plannerul ----------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["detail", "compare"])
def test_the_planner_carries_the_question_on_detail_and_compare(kind):
    a, b = _ids("electronics")[:2]
    interp = _interp(
        acts=[{"kind": kind, "targets": ["r1"], "question": "e rezistent la apa?"}],
        references=[{"id": "r1", "text": "primul", "kind": "ordinal", "ordinal": 1}],
    )
    plan = _only(_plan("electronics", interp, resolved=(_ref("r1", "exact", [a, b][:1]),)))
    assert plan.question == "e rezistent la apa?"


def test_the_planner_leaves_other_plans_without_a_question():
    a = _ids("electronics")[0]
    interp = _interp(
        acts=[{"kind": "link", "targets": ["r1"], "question": "linkul?"}],
        references=[{"id": "r1", "text": "primul", "kind": "ordinal", "ordinal": 1}],
    )
    assert (
        _only(_plan("electronics", interp, resolved=(_ref("r1", "exact", [a]),))).question is None
    )
    plain = _interp(
        acts=[{"kind": "detail", "targets": ["r1"]}],
        references=[{"id": "r1", "text": "primul", "kind": "ordinal", "ordinal": 1}],
    )
    assert _only(_plan("electronics", plain, resolved=(_ref("r1", "exact", [a]),))).question is None


# --- poarta de adevăr ---------------------------------------------------------------------------

FACTS = da.product_facts(PRODUCT, None, "ro")
UNITS = frozenset({"%", "spf", "ml", "g"})


def test_the_facts_carry_the_sheet_the_key_ingredients_and_the_product_faq():
    """`pack=None`: felurile de secțiuni prezente pe produs (pe producție, `detail_sections`)."""
    assert "0,1% retinol" in FACTS and "de 3 ori" in FACTS and "142" in FACTS
    assert "4.9/5 from 19 reviews" in FACTS and "se absoarbe repede" in FACTS
    assert "key ingredients: retinol, pantenol" in FACTS
    assert "faq: Merge cu vitamina C? -> Da, dar dimineata vitamina C." in FACTS


def test_the_facts_follow_the_pack_sections_in_production():
    import json
    import pathlib

    from src.domain.loader import load_domain_pack
    from src.models import BusinessConfig

    root = pathlib.Path(__file__).resolve().parents[1]
    seed = json.loads((root / "db/seed/domain_pack_sole_ro.json").read_text(encoding="utf-8"))
    biz = BusinessConfig(
        id="b", slug="s", name="n", vertical="ecommerce", settings={"domain_pack": seed}
    )
    pack = load_domain_pack(biz)
    facts = da.product_facts(PRODUCT, pack, "ro")
    assert "summary: Crema de noapte" in facts and "usage: Se aplica seara" in facts
    assert {"%", "spf", "ml"} <= da.unit_words(pack)


@pytest.mark.parametrize(
    "answer",
    [
        "Da, are 0,1% retinol pur.",
        "Fișa nu spune dacă merge cu vitamina C. Spune doar că se aplică seara.",
        "Costă 142 lei.",
        "Recenziile spun că se absoarbe repede, cu 4.9 din 19 recenzii.",
    ],
)
def test_a_grounded_answer_passes(answer):
    assert da.check_answer(answer, PRODUCT, FACTS, UNITS).ok


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        ("Are 2% retinol.", "ungrounded_number"),
        # recenzia: 5 e nota (4.9/5), 19 e numărul de recenzii, nu procente
        ("Conține 5% niacinamidă.", "ungrounded_number"),
        ("Are 19% acizi.", "ungrounded_number"),
        # recenzia: cifra întrebării nu mai e scutită («are SPF 50?» → «Da, are SPF 50»)
        ("Da, are SPF 50.", "ungrounded_number"),
        ("Costă 99 lei.", "ungrounded_price"),
        ("Vezi aici: https://evil.test/x", "invented_link"),
        ("", "empty"),
    ],
)
def test_an_ungrounded_answer_is_rejected(answer, reason):
    assert da.check_answer(answer, PRODUCT, FACTS, UNITS).reason == reason


def test_a_unit_must_sit_next_to_the_same_number_in_the_facts():
    """«50 ml» în fișă nu întemeiază «SPF 50»; «SPF 50» în fișă îl întemeiază."""
    with_size = FACTS + "\nsize: 50 ml"
    assert da.check_answer("Are SPF 50.", PRODUCT, with_size, UNITS).reason == "ungrounded_number"
    assert da.check_answer("Are 50 ml.", PRODUCT, with_size, UNITS).ok
    with_spf = FACTS + "\nattributes: SPF 50"
    assert da.check_answer("Are SPF 50.", PRODUCT, with_spf, UNITS).ok


# --- executorul ---------------------------------------------------------------------------------


def _detail(question: str | None) -> TurnPlan:
    return TurnPlan(
        executor="detail", product_ids=["p1"], search_args=None, depends_on=None, question=question
    )


def _deps(llm):
    return NS(db=sh.RecordingDb(), llm=llm)


@pytest.fixture
def catalog(monkeypatch):
    async def by_ids(conn, business_id, ids, limit=6):
        return [dict(PRODUCT)] if "p1" in ids else []

    monkeypatch.setattr(kx, "get_products_by_ids", by_ids)
    sheet: list[str] = []

    async def details(ctx, deps, pid, *, lead=None, gated=None):
        sheet.append(pid)
        assert gated is not None, "fișa de rezervă refolosește produsul deja citit"
        first = lead(gated[0]) if lead and gated else None
        text = f"{first}\n\nFISA STANDARD" if first else "FISA STANDARD"
        ctx.set_reply(text, cacheable=False)

    monkeypatch.setattr(kx.det, "serve_details", details)
    return sheet


async def test_a_question_is_answered_from_the_sheet_with_the_card(electronics, catalog):
    llm = AnswerLLM("Da, are 0,1% retinol pur.")
    ctx = _ctx(electronics, "ce concentratie are?")
    served = await kx.execute_read_plans(
        ctx, _deps(llm), _planned(_detail("ce concentratie are?")), _outcome()
    )
    assert served is True and catalog == []
    assert ctx.reply.text == "Da, are 0,1% retinol pur."
    assert [p["product_id"] for p in ctx.reply.products] == ["p1"]
    system, user = llm.calls[0]
    assert "CUSTOMER QUESTION\nce concentratie are?" in user and "0,1% retinol" in user
    assert [e.properties["outcome"] for e in ctx.events if e.type == "detail_question"] == [
        "answered"
    ]


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [("Are 2% retinol.", "rejected"), (RuntimeError("boom"), "call_failed")],
)
async def test_a_failed_answer_serves_todays_sheet(electronics, catalog, answer, outcome):
    ctx = _ctx(electronics, "ce concentratie are?")
    served = await kx.execute_read_plans(
        ctx, _deps(AnswerLLM(answer)), _planned(_detail("ce concentratie are?")), _outcome()
    )
    assert served is True and catalog == ["p1"] and ctx.reply.text == "FISA STANDARD"
    assert [e.properties["outcome"] for e in ctx.events if e.type == "detail_question"] == [outcome]


async def test_without_a_question_or_with_the_flag_off_nothing_changes(electronics, monkeypatch):
    """Calea de azi: `serve_details` fără argumente noi (I16, byte-identic)."""
    seen = []

    async def details(ctx, deps, pid, **kw):
        seen.append(kw)
        ctx.set_reply("FISA STANDARD", cacheable=False)

    monkeypatch.setattr(kx.det, "serve_details", details)
    llm = AnswerLLM("Da.")
    plain = _planned(_detail(None))
    await kx.execute_read_plans(_ctx(electronics, "x"), _deps(llm), plain, _outcome())
    monkeypatch.setattr(get_settings(), "detail_question_answer_enabled", False)
    asked = _planned(_detail("are retinol?"))
    await kx.execute_read_plans(_ctx(electronics, "x"), _deps(llm), asked, _outcome())
    assert llm.calls == [] and seen == [{}, {}]


async def test_a_product_not_found_serves_the_sheet_without_a_call(
    electronics, catalog, monkeypatch
):
    async def none(conn, business_id, ids, limit=6):
        return []

    monkeypatch.setattr(kx, "get_products_by_ids", none)
    llm = AnswerLLM("Da.")
    ctx = _ctx(electronics, "are retinol?")
    await kx.execute_read_plans(ctx, _deps(llm), _planned(_detail("are retinol?")), _outcome())
    assert llm.calls == [] and catalog == ["p1"]


async def test_a_product_blocked_by_safety_is_gated_once(electronics, catalog, monkeypatch):
    """Recenzia: fișa de rezervă nu re-citește și nu re-judecă produsul (un singur eveniment)."""
    calls = []

    class Policy:
        def gate(self, ctx, products, purpose):
            calls.append(purpose)
            return [], None

    monkeypatch.setattr(kx.SafetyPolicy, "for_turn", classmethod(lambda cls, ctx: Policy()))
    llm = AnswerLLM("Da.")
    ctx = _ctx(electronics, "are retinol?")
    await kx.execute_read_plans(ctx, _deps(llm), _planned(_detail("are retinol?")), _outcome())
    assert calls == ["detail_intent"] and llm.calls == [] and catalog == ["p1"]


async def test_a_medical_answer_serves_the_referral_before_the_sheet(electronics, catalog):
    """Recenzia: «are alergeni?» e formulat negat de model («fără alergeni») și respins de poarta
    medicală; clientul primește trimiterea la medic sau farmacist, apoi fișa."""
    from src.safety.messages import refer_sentence

    llm = AnswerLLM("Fișa nu spune dacă e fără alergeni.")
    ctx = _ctx(electronics, "are alergeni?")
    plan = _planned(_detail("are alergeni?"))
    await kx.execute_read_plans(ctx, _deps(llm), plan, _outcome())
    assert ctx.reply.text.startswith(refer_sentence("ro")) and "FISA STANDARD" in ctx.reply.text
    reasons = [e.properties.get("reason") for e in ctx.events if e.type == "detail_question"]
    assert reasons == ["medical_claim"]


def test_the_plan_question_is_redacted_in_the_trace():
    from tests.test_kernel_models import _trace

    phone = "0722 123 456"
    plan = _detail(f"ajunge la {phone}?")
    trace = _trace(plan=plan).model_copy(update={"plans": [plan]})
    stored = redact_trace(
        KernelTrace.model_validate(trace.model_dump()), lambda t: t.replace(phone, "[telefon]")
    )
    dumped = stored.model_dump_json()
    assert phone not in dumped and "[telefon]" in dumped
