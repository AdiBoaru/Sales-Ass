"""NX-275 felia 6 — retrieval speculativ: căutarea rulează înainte de primul apel de model.

Ce contează, în ordine: să NU speculăm când n-are sens (fiecare refuz are un motiv numit), să
speculăm prin ACEEAȘI cale ca orice tool (nu pe lângă porți), să fie determinist la reluare, și să
nu rupă nimic când eșuează (P6).

ZERO OpenAI, ZERO DB.
"""

from __future__ import annotations

import pytest

from src.agent import speculative_retrieval as spec


def _skip(**over):
    base = dict(
        speculative_profile=True,
        has_action=False,
        has_anchor=False,
        is_pagination=False,
        message="vreau un ser pentru ten uscat",
        locale="ro",
    )
    base.update(over)
    return spec.skip_reason(**base)


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({}, None),
        ({"speculative_profile": False}, "profile"),
        ({"has_action": True}, "action"),
        ({"has_anchor": True}, "anchor"),
        ({"is_pagination": True}, "pagination"),
        ({"message": ""}, "no_content_terms"),
    ],
)
def test_fiecare_refuz_are_un_motiv_numit(override, expected):
    """Un refuz fără motiv ar face imposibil de spus dacă felia e stinsă sau doar nu nimerește:
    o felie în care toate turele sar peste seed arată identic cu una care ratează mereu."""
    assert _skip(**override) == expected


def test_paginarea_nu_se_speculeaza_niciodata():
    """Regresia NX-251, ca test: pe `show_more` există deja un pool de sesiune, iar o căutare nouă
    l-ar înlocui tăcut. Clientul ar primi „pagina 2" dintr-un alt set de rezultate."""
    assert _skip(is_pagination=True) == "pagination"


def test_bugetul_se_filtreaza_doar_daca_a_fost_rostit():
    """D7: o constrângere pe care clientul n-a rostit-o nu are voie să restrângă candidații.

    Un buget DEDUS ar tăia produse pe baza unei presupuneri, iar rezultatul ar arăta ca o căutare
    onestă — nicio poartă din aval nu verifică de ce lipsește un produs."""
    assert spec._spoken_budget("vreau un ser sub 150 lei") == 150.0
    assert spec._spoken_budget("vreau un ser bun") is None


def test_id_ul_de_tool_call_e_determinist():
    """Nu `uuid4()`: un tur reluat (reclaim) trebuie să reconstruiască ACEIAȘI octeți, altfel
    prefixul conversației diferă între încercări și prompt cachingul nu se mai prinde pe reluări."""
    assert spec.seed_call_id("turn-1") == spec.seed_call_id("turn-1")
    assert spec.seed_call_id("turn-1") != spec.seed_call_id("turn-2")
    assert spec.seed_call_id("turn-1").startswith("spec_")


async def test_seedul_arata_exact_ca_un_tool_call_al_modelului():
    """Perechea trebuie să fie indistinctibilă de una emisă de model, altfel furnizorul respinge
    conversația (un `tool` fără `tool_calls` corespunzător e invalid)."""
    seen: dict = {}

    async def execute(name, args):
        seen["name"], seen["args"] = name, args
        return "1. [p1] Ser Hidratant | 99,00 lei"

    messages, outcome = await spec.seed_messages(
        turn_id="t1", message="vreau un ser", locale="ro", execute=execute
    )
    assert outcome == "seeded" and messages is not None
    assistant, tool = messages
    assert seen["name"] == "search_products"
    assert assistant["role"] == "assistant" and assistant["tool_calls"][0]["type"] == "function"
    assert tool["role"] == "tool"
    assert tool["tool_call_id"] == assistant["tool_calls"][0]["id"]


async def test_o_cautare_esuata_nu_rupe_turul():
    """P6: o optimizare nu are voie să transforme un tur care ar fi mers într-o eroare. Fără seed,
    bucla rulează exact ca azi."""

    async def boom(name, args):
        raise RuntimeError("DB jos")

    messages, outcome = await spec.seed_messages(
        turn_id="t1", message="vreau un ser", locale="ro", execute=boom
    )
    assert messages is None and outcome == "failed"


@pytest.mark.parametrize("view", ["", "dependency_unavailable", "safety_excluded: nu pot"])
async def test_o_vedere_inutilizabila_nu_se_seamana_in_conversatie(view):
    """Un refuz de safety sau o dependență căzută nu au ce căuta în conversație ca „am căutat":
    modelul le-ar citi drept „am căutat și n-am găsit", ceea ce e o minciună despre catalog."""

    async def execute(name, args):
        return view

    messages, outcome = await spec.seed_messages(
        turn_id="t1", message="vreau un ser", locale="ro", execute=execute
    )
    assert messages is None and outcome == "unusable"


async def test_argumentele_sunt_deterministe_si_serializate_stabil():
    """`sort_keys` nu e cosmetică: aceiași octeți la fiecare reluare înseamnă că prefixul rămâne
    cache-uibil (felia 3)."""

    async def execute(name, args):
        return "1. [p1] Ceva"

    a, _ = await spec.seed_messages(
        turn_id="t1", message="vreau un ser", locale="ro", execute=execute
    )
    b, _ = await spec.seed_messages(
        turn_id="t1", message="vreau un ser", locale="ro", execute=execute
    )
    assert (
        a[0]["tool_calls"][0]["function"]["arguments"]
        == (b[0]["tool_calls"][0]["function"]["arguments"])
    )


def test_seedul_nu_consuma_o_runda_de_model():
    """`rounds` numără apelurile de MODEL. Seed-ul n-a făcut niciunul, deci un tur care produce
    planul din prima trebuie să raporteze 0 runde — exact semnalul de `hit`."""
    import inspect

    from src.agent.llm import LLMClient

    src = inspect.getsource(LLMClient.run_tool_loop_structured)
    seed_pos = src.index("messages.extend(seed)")
    rounds_pos = src.index("rounds = 0")
    assert seed_pos < rounds_pos, "seed-ul trebuie inserat ÎNAINTE de bucla care numără rundele"
