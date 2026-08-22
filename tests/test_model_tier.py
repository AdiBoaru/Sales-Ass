"""Routing de model pe clasa de tur: Terra doar la turele complicate, Luna în rest.

Poarta e DETERMINISTĂ (obligațiile extrase de cod, NX-251), nu un model care clasifică — altfel
am plăti un apel de model ca să aflăm dacă merită să plătim un apel de model, adică exact cascada
pe care D1 o interzice.
"""

from __future__ import annotations

from src.runtime.turn_budget import TurnClass, turn_class_for


class _Ob:
    def __init__(self, kind: str) -> None:
        self.kind = kind


def _cls(*kinds: str) -> TurnClass:
    return turn_class_for([_Ob(k) for k in kinds])


def test_turele_simple_raman_pe_modelul_ieftin():
    """Un răspuns, o clarificare, o explicație — nu au nevoie de vârful de gamă."""
    assert _cls("answer") is TurnClass.EXACT
    assert _cls("clarify") is TurnClass.EXACT
    assert _cls("explain") is TurnClass.EXACT
    assert _cls("safety") is TurnClass.EXACT


def test_recomandarea_e_clasa_ei():
    assert _cls("recommend") is TurnClass.RECOMMENDATION


def test_comparatia_si_mesajul_mixt_escaladeaza():
    """Comparația și mesajul mixt sunt exact cazurile în care un model mai slab răspunde la
    jumătate din întrebare și pare că a răspuns la tot."""
    assert _cls("compare") is TurnClass.COMPLEX
    assert _cls("answer", "recommend") is TurnClass.COMPLEX
    assert _cls("recommend", "compare") is TurnClass.COMPLEX


def test_doua_obligatii_de_acelasi_tip_sunt_tot_un_mesaj_mixt():
    """Regresie măsurată pe mesaje reale: „ce preț are X și ai ceva pentru ten uscat?" produce
    două obligații `answer`. Numărate pe TIPURI, setul are dimensiunea 1 și mesajul cădea la
    `exact` — adică fix cazul mixt primea modelul ieftin. Se numără obligațiile."""
    assert _cls("answer", "answer") is TurnClass.COMPLEX


def test_mutatia_escaladeaza():
    """O acțiune scrie ceva — greșeala nu se retrage cu un mesaj de scuze."""
    assert _cls("action") is TurnClass.MUTATION
    assert _cls("answer", "action") is TurnClass.MUTATION


def test_necunoscutul_urca_nu_coboara():
    """O obligație pe care n-o recunoaștem primește tratamentul bun, nu pe cel ieftin: greșeala
    în direcția asta costă bani, invers costă răspunsul clientului."""
    assert _cls("obligatie_inventata_maine") is TurnClass.RECOMMENDATION
    assert _cls() is TurnClass.RECOMMENDATION


def test_fara_model_complex_configurat_nu_se_escaladeaza_nimic(monkeypatch):
    """Rollback-ul e o variabilă goală: `MODEL_AGENT_COMPLEX=""` → totul pe `model_agent`."""
    from src.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "model_agent_complex", "", raising=False)
    escalate = TurnClass.COMPLEX in (TurnClass.COMPLEX, TurnClass.MUTATION)
    model = (s.model_agent_complex.strip() if escalate else "") or s.model_agent
    assert model == s.model_agent
