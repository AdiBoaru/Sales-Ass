"""NX-366 — turele greșite din setul `prod-2026-10-01`, rejucate determinist pe codul de acum.

Cazurile sunt rândurile `conversation_traces` ale setului NOSTRU (`tests/golden/prod_sets/
prod-2026-10-01.json`), înregistrate pe producție (release `6b383ad`, 2026-10-01 07:22 UTC) și
exportate cu `scripts/trace_replay.py --export --set-report`. Replay-ul joacă ieșirile înregistrate
ale modelului peste codul de acum: un test trece doar când MECANISMUL e reparat, nu când modelul a
nimerit altă formulare.

Fiecare defect e un invariant (o proprietate care trebuie să țină pe orice tur), nu o frază:
  • `content_never_empty` (P6) pe TOATE cele 36 de ture;
  • `moderation_does_not_silence_a_product_question` (c13, „mă mănâncă" citit ca violență);
  • `faq_turn_never_answers_with_product_no_result` (c14, regula de retur);
  • `safety_exclusion_is_disclosed` (c6, „nu am găsit retinol" după 13 retinoizi excluși);
  • `refinement_keeps_the_on_screen_match` (c8, „fără sulfați" pierde șamponul fără sulfați afișat);
  • `cheaper_than_the_named_step` (c9, tonerul „mai ieftin" servit ca benzi de nas de 3 lei).

`xfail(strict=True)` cu cardul care repară: când reparația intră, testul trece, iar `strict` cere
scoaterea marcajului în același PR. O reparație care schimbă CE se cere modelului (un apel în plus,
alt set de unelte) face replay-ul `diverged`: testul se SARE cu motivul ăsta, iar dovada cere o
înregistrare nouă (`scripts/sim/prod_set_run.py`), nu o presupunere.

Declarat, nereproduse la înregistrare (modelul a formulat altfel; cauza e știută din analiza din
2026-10-01, deci primesc teste unitare pe mecanism în cardurile lor): „nu am găsit produsul" după
ce l-a arătat (c10), serul anti-pete fără carduri și vergeturile (c6 T0/T2). Rutina pierdută la
buget (c9 T1) nu se poate fixa prin replay: reparația schimbă uneltele oferite modelului.

    NX_TESTS_READ_ENV_FILE=1 pytest tests/replay -m integration -q
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

CASES = Path(__file__).parent / "cases" / "prod-2026-10-01"
ALL = sorted(CASES.glob("*/*.json"))


@pytest.fixture(autouse=True)
async def _pool_per_test():
    """Poolul asyncpg e legat de bucla în care s-a creat, iar fiecare test are bucla lui: fără
    închidere, al doilea test găsește conexiuni ale unei bucle moarte și se blochează."""
    yield
    from src.db.connection import close_pool  # noqa: PLC0415

    await close_pool()


def _load(rel: str) -> dict[str, Any]:
    return json.loads((CASES / rel).read_text(encoding="utf-8"))


async def _replay(row: dict[str, Any]) -> Any:
    from src.evals import trace_replay  # noqa: PLC0415

    res = await trace_replay.replay_turn(row)
    if res.status in ("wrote", "db_aborted", "not_replayable"):
        pytest.fail(f"replay {res.status} ({res.reason}): instrumentul nu poate judeca turul")
    # `shortened`: codul s-a oprit mai devreme, pe un prefix al înregistrării (o reparație care
    # scoate un apel): rezultatul e al ieșirilor înregistrate, deci se judecă.
    if res.status == "diverged":
        pytest.skip(
            f"diverged la apelul {res.divergence and res.divergence['index']}: codul cere "
            "modelului altceva decât s-a înregistrat; dovada cere o înregistrare nouă"
        )
    return res


def shown_text(reply: dict[str, Any] | None) -> str:
    """`content`-ul widgetului (`channels/web/render.render_web`): pe un răspuns bogat, încadrarea
    (intro, educație, disclaimer), nu enumerarea cardurilor; altfel textul."""
    if not reply:
        return ""
    rich = reply.get("rich")
    if rich:
        return " ".join(
            str(rich.get(k) or "") for k in ("intro", "education", "disclaimer")
        ).strip()
    return (reply.get("text") or "").strip()


def _events(res: Any, kind: str) -> list[dict[str, Any]]:
    return [e for e in res.events if e.get("type") == kind]


# --- P6: niciun răspuns fără text --------------------------------------------------------------

#: Reparat de NX-369 (handle-urile `P1…` din proză devin numele de pe card, iar rezerva de
#: încadrare coboară la un tip când e singura frază). Lista rămâne pentru următoarele cazuri.
_EMPTY_TEXT: set[str] = set()


@pytest.mark.parametrize("path", ALL, ids=[f"{p.parent.name}/{p.stem}" for p in ALL])
async def test_content_never_empty(path, request):
    rel = f"{path.parent.name}/{path.name}"
    if rel in _EMPTY_TEXT:
        request.node.add_marker(
            pytest.mark.xfail(strict=True, reason="text gol: intro-ul scrubbuit fără rezervă")
        )
    res = await _replay(json.loads(path.read_text(encoding="utf-8")))
    assert shown_text(res.reply), "clientul a primit carduri fără niciun cuvânt (P6)"


# --- P0: moderarea --------------------------------------------------------------------------------


@pytest.mark.xfail(strict=True, reason="P0 moderare: un flag al clasificatorului reduce la tăcere")
async def test_moderation_does_not_silence_a_product_question():
    from src.worker.stages.gates import NEUTRAL_MSG  # noqa: PLC0415

    res = await _replay(_load("c13_vag/9a5ed692-2.json"))
    assert (res.reply or {}).get("text") != NEUTRAL_MSG


# --- FAQ ----------------------------------------------------------------------------------------


async def test_faq_turn_never_answers_with_product_no_result():
    """Reparat de NX-369 (era xfail): regula parafrazată se servește în cuvintele magazinului."""
    from src.agent.finalize import _no_result_msg  # noqa: PLC0415

    res = await _replay(_load("c14_retur/f27b0ee3-0.json"))
    assert shown_text(res.reply) != _no_result_msg(False)


# --- P0: siguranța -------------------------------------------------------------------------------


@pytest.mark.xfail(strict=True, reason="P0 siguranță: excluderea nu e spusă, iar modelul o neagă")
async def test_safety_exclusion_is_disclosed():
    from src.safety.messages import safety_sentence  # noqa: PLC0415

    res = await _replay(_load("c6_sarcina/f09fea77-1.json"))
    blocks = [e for e in _events(res, "safety_contraindication_block") if e.get("blocked")]
    assert blocks, "cazul cere o excludere de siguranță; fără ea, înregistrarea nu mai e relevantă"
    contexts = sorted({c for e in blocks for c in e.get("contexts") or []})
    rules = sorted({r for e in blocks for r in e.get("rules") or []})
    sentence = safety_sentence(contexts, rules, locale="ro", blocked=True)
    assert sentence and sentence in (res.reply or {}).get("text", "")


# --- rafinarea pierde produsul potrivit de pe ecran ----------------------------------------------

#: LEE STAFFORD Moisture Burst Hydrating Shampoo: pe ecranul turului 0, fără sulfați, pentru păr
#: uscat — exact ce cere turul 1. Eticheta cazului, nu o regulă de produs.
_MOISTURE_BURST = "bcdfd78b-d5f4-4eea-9b52-fb3632962a5a"


@pytest.mark.xfail(
    strict=True, reason="căutarea nouă exclude produsele afișate; features nevalidat"
)
async def test_refinement_keeps_the_on_screen_match():
    res = await _replay(_load("c8_par_cret/fea953f3-1.json"))
    ids = [str(p.get("product_id") or p.get("id")) for p in (res.reply or {}).get("products") or []]
    assert _MOISTURE_BURST in ids


# --- „mai ieftin" pe pasul numit ------------------------------------------------------------------

_TONER_PRICE = 110.0  # ANUA Heartleaf 77% Soothing, tonerul din rutina turului 0
_TONER_TYPE = "toner de fata"  # cheia `attributes.product_type` din catalogul SOLE


@pytest.mark.xfail(
    strict=True, reason="«mai ieftin» ancorat pe cel mai ieftin card, nu pe tonerul numit"
)
async def test_cheaper_than_the_named_step():
    from src.db.connection import tenant_conn  # noqa: PLC0415

    row = _load("c9_rutina/4ac20c2b-2.json")
    res = await _replay(row)
    cards = (res.reply or {}).get("products") or []
    ids = [str(p.get("product_id") or p.get("id")) for p in cards]
    assert ids, "niciun card"
    async with tenant_conn(row["business_id"]) as conn:
        rows = await conn.fetch(
            """select id::text, price, attributes ->> 'product_type' as product_type
                 from products where business_id = $1::uuid and id = any($2::uuid[])""",
            row["business_id"],
            ids,
        )
    toners = [
        r for r in rows if r["product_type"] == _TONER_TYPE and float(r["price"]) < _TONER_PRICE
    ]
    assert toners, "niciun toner mai ieftin decât tonerul numit"


async def test_store_info_turn_does_not_reload_old_cards():
    """NX-369, c7 T3 („cat costa livrarea?"): proza care parafraza regula de livrare pica, iar R3 o
    citea drept follow-up pe cardurile de dinainte: răspunsul plecau cu trei carduri vechi dedesubt
    (verificat pe înregistrare). Acum: regula de livrare, fără carduri."""
    from src.agent.finalize import _no_result_msg  # noqa: PLC0415

    row = _load("c7_cadou/1fd688a6-3.json")
    assert len(row["reply"].get("products") or []) == 3  # defectul, pe înregistrare
    res = await _replay(row)
    assert not (res.reply or {}).get("products")
    text = shown_text(res.reply)
    assert text and text != _no_result_msg(False)
