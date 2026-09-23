"""NX-312 felia 4 — apelul de compunere bogată nu mai cere ce aruncă.

Pe turul măsurat, `suggestions` (530 de tokeni de instrucțiuni + 58 de output) erau suprascrise de
mutările de chip, `pick` (47 + 68) nu se afișa pe niciun canal, iar lista de 45 de rafturi (~400)
stătea într-un prompt în care modelul alege dintr-o listă fixă de produse. Felia le scoate din
SCHEMĂ și din SYSTEM, cu aceeași mulțime de omisiuni, ca promptul să nu poată numi un câmp pe care
schema nu-l are (defectul de evidence din 2026-09-16).

Cititorii lui `pick`, derivați mecanic (`rg '\\.pick\\b|"pick"'` pe `src/`), fiecare decis:
- `compose._select_pick` citește `j["pick"]`: cu câmpul absent, ramura deterministă alege tot
  `items[0]` cu ancora din recenzii, iar cea veche întoarce `None`. Niciun `KeyError`.
- `compose.flatten` și `compose.flatten_framing` îl transformă în text DOAR sub
  `rich_pick_web_enabled`, adică exact condiția în care felia NU îl scoate.
- `render.py` (deserializare), `planner` (îl pune `None` la cross-sell), `brain_rich` (`None`):
  trec valoarea mai departe sau o sting, nu o cer de la model.
ZERO OpenAI, zero DB."""

from __future__ import annotations

import itertools
import re

import pytest

from src.agent import finalize as finalize_mod
from src.agent.finalize import _rich_schema, render, rich_omissions
from src.agent.planner import ResponsePlan
from src.agent.prompt_builder import RICH_OMITTABLE, PromptInputs, build_rich_system
from src.config import get_settings
from src.worker.runner import PipelineDeps
from tests.test_plan_guidance import INP, QUERY, VARIED, _ctx

CATS = PromptInputs.build("S", "ecommerce", "ro", [("Ten", 1461), ("Par", 400)], [])
ALL = frozenset(RICH_OMITTABLE)


@pytest.fixture
def slim_on(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "rich_schema_slim_enabled", True)
    monkeypatch.setattr(s, "chip_moves_v1_enabled", True)
    monkeypatch.setattr(s, "rich_pick_web_enabled", False)
    return s


@pytest.fixture
def slim_off(monkeypatch):
    monkeypatch.setattr(get_settings(), "rich_schema_slim_enabled", False)


# --- flagul stins: nimic nu se schimbă ---------------------------------------------------------


def test_flag_off_removes_nothing(slim_off):
    assert rich_omissions() == frozenset()


def test_flag_off_schema_is_the_module_constant_itself(slim_off):
    """Identitate, nu egalitate: turele de azi trimit EXACT obiectul de dinainte."""
    assert _rich_schema(rich_omissions()) is finalize_mod._RICH_SCHEMA
    assert _rich_schema(rich_omissions(), question=True) is finalize_mod._RICH_SCHEMA_WITH_QUESTION


def test_flag_off_system_keeps_the_shelf_list_and_both_rules(slim_off):
    system = build_rich_system(CATS, omit=rich_omissions())
    assert system == build_rich_system(CATS)
    assert "Vinzi din aceste categorii" in system
    assert "- `pick` = " in system
    assert "- `suggestions` = " in system


# --- ce iese, după configurație ----------------------------------------------------------------


def test_slim_removes_all_three_under_production_flags(slim_on):
    assert rich_omissions() == {"pick", "suggestions", "categories"}


def test_suggestions_stay_when_chip_moves_are_off(slim_on, monkeypatch):
    """Fără mutări de chip, sugestiile modelului SUNT chips-urile: nu se aruncă nimic."""
    monkeypatch.setattr(slim_on, "chip_moves_v1_enabled", False)
    assert "suggestions" not in rich_omissions()


def test_pick_stays_when_the_pick_line_is_shown(slim_on, monkeypatch):
    monkeypatch.setattr(slim_on, "rich_pick_web_enabled", True)
    assert "pick" not in rich_omissions()


def test_slim_schema_drops_the_fields_and_stays_strict(slim_on):
    schema = _rich_schema(rich_omissions())
    body = schema["schema"]
    assert schema["strict"] is True and body["additionalProperties"] is False
    assert body["required"] == ["intro", "items", "education"]
    assert set(body["properties"]) == {"intro", "items", "education"}
    # Constanta de modul NU a fost mutată pe loc (e folosită concurent de toate turele).
    assert "pick" in finalize_mod._RICH_SCHEMA["schema"]["properties"]


def test_slim_schema_is_cached_per_omission_set(slim_on):
    """Același set ⇒ același obiect: nimeni nu reconstruiește schema pe fiecare tur."""
    assert _rich_schema(rich_omissions()) is _rich_schema(rich_omissions())


def test_question_variant_composes_with_slim(slim_on):
    """NX-315: întrebarea de îngustare se adaugă peste schema slim, nu peste cea completă."""
    body = _rich_schema(rich_omissions(), question=True)["schema"]
    assert body["required"] == ["intro", "items", "education", "question"]
    assert "pick" not in body["properties"] and "question" in body["properties"]


# --- system-ul: nicio regulă despre un câmp absent ---------------------------------------------


def test_slim_system_never_names_a_removed_field(slim_on):
    system = build_rich_system(CATS, omit=rich_omissions())
    assert not re.search(r"\bpick\b", system)
    assert "suggestions" not in system
    assert "Vinzi din aceste categorii" not in system
    # Regulile care numeau `pick` au fost rescrise, nu tăiate: constrângerea rămâne.
    assert "LEAG-O de recomandare" in system
    assert "· `items` = doar acel produs." in system


@pytest.mark.parametrize("routine", [False, True])
def test_every_schema_field_the_prompt_names_exists_in_the_schema(slim_on, routine):
    """Invarianta de care depinde felia: pentru orice câmp de output scos din schemă, promptul nu-l
    mai numește; pentru orice câmp rămas, promptul încă îl explică."""
    omit = rich_omissions()
    system = build_rich_system(CATS, routine=routine, omit=omit)
    kept = set(_rich_schema(omit)["schema"]["properties"])
    for field in finalize_mod._RICH_SCHEMA["schema"]["properties"]:
        named = f"`{field}`" in system
        assert named == (field in kept), field


@pytest.mark.parametrize(
    "omit",
    [frozenset(c) for n in range(len(ALL) + 1) for c in itertools.combinations(sorted(ALL), n)],
)
@pytest.mark.parametrize("routine", [False, True])
def test_no_marker_reaches_the_model_on_any_variant(omit, routine):
    """Garda pentru defectul care NU dă eroare: `{MAX_PER_TYPE}` pleca literal spre model pe
    FIECARE apel rich, fiindcă NX-303 îl substituise doar în system-ul buclei."""
    system = build_rich_system(CATS, routine=routine, omit=omit)
    assert re.findall(r"\{[A-Z_]+\}", system) == []


def test_unknown_omission_is_refused():
    with pytest.raises(ValueError, match="necunoscute"):
        build_rich_system(CATS, omit=frozenset({"items"}))


def test_the_slim_prompt_is_actually_smaller(slim_on):
    full = build_rich_system(CATS)
    slim = build_rich_system(CATS, omit=rich_omissions())
    assert len(slim) < len(full) - 1500  # regula de sugestii singură are ~1.600 de caractere


# --- cap-coadă: turul trimite schema slim și răspunsul iese la fel ---------------------------


class _SlimLLM:
    """Răspunde exact cum ar răspunde modelul pe schema slim: fără `pick`, fără `suggestions`."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []

    async def embed(self, texts, *, model=None):
        return [[0.0] * 8 for _ in texts]

    async def complete(self, system, user, *, model=None):
        raise AssertionError("pe calea bogată reușită nu se cere nicio recompunere")

    async def complete_schema(self, system, user, schema, *, model=None):
        self.calls.append((system, user, schema))
        return {
            "intro": "Am ales creme hidratante.",
            "items": [
                {"product_id": p["id"], "pro_index": 0, "fit_clause": "ușoară"} for p in VARIED
            ],
            "education": None,
        }


def _plan() -> ResponsePlan:
    return ResponsePlan(
        handled=False,
        products=[dict(p) for p in VARIED],
        final="",
        is_order=False,
        query=QUERY,
        history="",
        inp=INP,
        mode="rich",
    )


async def test_turn_sends_the_slim_schema_and_still_serves_rich_cards(slim_on):
    llm = _SlimLLM()
    ctx = _ctx()
    await render(ctx, PipelineDeps(conn=object(), redis=None, llm=llm), _plan())
    system, _user, schema = llm.calls[0]
    assert "pick" not in schema["schema"]["properties"]
    assert "suggestions" not in schema["schema"]["properties"]
    assert "`suggestions`" not in system
    rich = ctx.reply.rich
    assert rich is not None and [it.product_id for it in rich.items] == ["p1", "p2", "p3"]
    assert rich.intro == "Am ales creme hidratante."


async def test_turn_with_flag_off_sends_the_full_schema(slim_off):
    llm = _SlimLLM()
    await render(ctx := _ctx(), PipelineDeps(conn=object(), redis=None, llm=llm), _plan())
    assert llm.calls[0][2] is finalize_mod._RICH_SCHEMA
    assert ctx.reply.rich is not None
