"""NX-181 — Prompt vNext: relaxare `_RICH_RULES` + `response_shape`/anti-repetiție în USER.

Contractul de cod: OFF → byte-identic (kill-switch); ON → reguli relaxate (fără număr-țintă) +
header de mod. Judecata de naturalețe se măsoară cu evaluatorul (NX-180), nu aici.
"""

from src.agent import prompt_builder
from src.agent.finalize import _last_bot_opening
from src.agent.prompt_builder import PromptInputs


def _inp() -> PromptInputs:
    return PromptInputs.build("Demo", "beauty", "ro", ["creme"], [])


def test_rich_system_vnext_relaxes_quantity_and_adds_shape():
    off = prompt_builder.build_rich_system(_inp(), vnext=False)
    on = prompt_builder.build_rich_system(_inp(), vnext=True)
    assert off != on
    # OFF păstrează număr-țintă; ON îl scoate → 1-3 fără țintă
    assert "PÂNĂ LA 4 produse" in off
    assert "PÂNĂ LA 4 produse" not in on
    assert "1-3 produse" in on
    # ON adaugă header-ul de MOD DE RĂSPUNS (respectă response_shape din USER)
    assert "MOD DE RĂSPUNS" in on
    assert "MOD DE RĂSPUNS" not in off
    # grounding/safety păstrate în ambele (nu relaxăm siguranța)
    assert "NU inventa" in on or "id inventat" in on


def test_rich_system_off_byte_identic_backcompat():
    # apelul fără param (callerii vechi, ex. cross_sell) == vnext=False → OFF neschimbat
    assert prompt_builder.build_rich_system(_inp()) == prompt_builder.build_rich_system(
        _inp(), vnext=False
    )


def test_last_bot_opening_first_sentence_of_last_outbound():
    class _M:
        def __init__(self, direction, body):
            self.direction = direction
            self.body = body

    class _Ctx:
        history = [
            _M("inbound", "salut"),
            _M("outbound", "Pentru ten gras, uite variantele. Prima e X."),
            _M("inbound", "si mai ieftin?"),
        ]

    assert _last_bot_opening(_Ctx()) == "Pentru ten gras, uite variantele"

    class _Empty:
        history: list = []

    assert _last_bot_opening(_Empty()) == ""
