"""NX-78 — prompt_builder: prompt agent GENERAT din DB + determinism (prompt caching).

Modul pur (zero LLM/DB) → assert-uri pe string direct. Acoperă: vertical injectat (≠ beauty),
categorii goale (fallback fără vertical inventat), determinism byte-identic (ordine amestecată →
același string = invariantul de cache), aliase doar când există.
"""

from src.agent.prompt_builder import (
    ORDER_RECO_SYSTEM,
    PromptInputs,
    build_agent_system,
    build_reco_system,
    build_rich_system,
)


def _inp(**kw):
    base = {
        "business_name": "Sole Demo",
        "vertical": "beauty",
        "locale": "ro",
        "categories": ["Creme", "Parfumuri", "Rujuri"],
        "aliases": [],
    }
    base.update(kw)
    return PromptInputs.build(**base)


# --- vertical din DB (P9) ----------------------------------------------------


def test_vertical_injected_beauty():
    s = build_agent_system(_inp())
    assert "beauty" in s
    assert "Creme" in s and "Parfumuri" in s and "Rujuri" in s
    assert "search_products" in s and "Maxim 3 apeluri" in s  # blocul de tool-uri + reguli


def test_anti_invented_need_in_prose_and_rich():
    """Val1: regula anti-nevoie-inventată + hedge e în prompturile de proză ȘI bogat."""
    prose = build_agent_system(_inp())
    rich = build_rich_system(_inp())
    for s in (prose, rich):
        low = s.lower()
        assert "atribute despre client" in low  # nu inventa atribute nespuse
    assert "ipotez" in prose.lower()  # proza cere formulare ca ipoteză (hedge)
    assert "formulă blândă" in rich or "formula blanda" in rich.lower()  # leagă de produs


# --- NX-114: moneda din DomainPack în prompt ---------------------------------


def test_currency_default_ron_shows_lei():
    s = build_agent_system(_inp())  # currency default RON
    assert "prețul EXACT (lei)" in s  # byte-identic cu comportamentul de dinainte de NX-114


def test_currency_override_shows_label():
    s = build_agent_system(_inp(currency="EUR"))
    assert "prețul EXACT (euro)" in s
    assert "prețul EXACT (lei)" not in s


def test_currency_in_reco_system():
    assert "prețul EXACT (euro)" in build_reco_system(_inp(currency="EUR"))


def test_vertical_injected_hvac_not_beauty():
    s = build_agent_system(
        _inp(vertical="hvac", categories=["Aer condiționat", "Centrale termice"])
    )
    assert "hvac" in s and "beauty" not in s
    assert "Aer condiționat" in s and "Centrale termice" in s


def test_empty_categories_no_invented_vertical():
    s = build_agent_system(_inp(vertical="auto", categories=[]))
    assert "beauty" not in s
    assert "Vinzi din aceste categorii" not in s  # linia lipsește când n-avem categorii
    assert "search_products" in s  # promptul rămâne valid pt bucla de tool-calling


# --- determinism = prompt caching --------------------------------------------


def test_byte_identical_same_input():
    assert build_agent_system(_inp()) == build_agent_system(_inp())


def test_category_order_does_not_matter():
    a = build_agent_system(_inp(categories=["Rujuri", "Creme", "Parfumuri"]))
    b = build_agent_system(_inp(categories=["Creme", "Parfumuri", "Rujuri"]))
    assert a == b  # sortare internă → prefix byte-identic indiferent de ordinea din DB


def test_no_dynamic_content_in_system():
    # promptul NU conține mesajul/produsele clientului (alea stau în USER) → stabil per tenant
    s = build_agent_system(_inp())
    assert "Mesaj client" not in s and "Nevoia clientului" not in s


# --- aliase de rutare --------------------------------------------------------


def test_aliases_line_present_only_when_nonempty():
    without = build_agent_system(_inp(aliases=[]))
    assert "Indicii de rutare" not in without
    with_alias = build_agent_system(_inp(aliases=[("crema fata", "creme")]))
    assert "Indicii de rutare" in with_alias and "crema fata" in with_alias


# --- celelalte builder-e -----------------------------------------------------


def test_reco_and_rich_carry_vertical():
    assert "hvac" in build_reco_system(_inp(vertical="hvac"))
    assert "hvac" in build_rich_system(_inp(vertical="hvac"))
    # REGULI DURE din calea rich rămân (anti-halucinație)
    assert "REGULI DURE" in build_rich_system(_inp())


def test_order_reco_is_vertical_neutral():
    # status comandă = suport, neutru pe vertical → constantă, fără „beauty"/categorie
    assert "beauty" not in ORDER_RECO_SYSTEM and "comenzii" in ORDER_RECO_SYSTEM


# --- NX-132: gramatica iZi în prompturi --------------------------------------


def test_rich_suggestions_five_anchored_roles():
    # chips-urile cer 5 roluri DISTINCTE ancorate pe nume; genericele sunt marcate ca DE EVITAT.
    r = build_rich_system(_inp())
    assert "ROL DIFERIT" in r
    for role in ("rafinare pe ATRIBUT", "COMPARAȚIE cu NUMELE", "pas de COMERȚ cu NUME"):
        assert role in r
    # „Compară primele două" apare DOAR ca exemplu de evitat (nu ca șablon de urmat)
    assert "evită generice" in r


def test_rich_segmentation_and_constraint_echo():
    r = build_rich_system(_inp())
    assert "SEGMENTARE" in r and "AXĂ DIFERITĂ" in r  # motivele = arbore de decizie (P1)
    assert "rămâne în bugetul tău" in r  # ecoul constrângerii în pick (P4)
    assert "PÂNĂ LA 4 produse" in r  # decizia Adi: capul rămâne 4 (aliniat cu _MAX_RICH_ITEMS)


def test_rich_detail_mode_forbids_list_skeleton():
    r = build_rich_system(_inp())
    assert "NU refolosi scheletul de LISTĂ" in r  # MOD DETALIU aduce fapte noi, nu coaching repetat


def test_rich_prompt_forbids_repetitive_ai_phrasing():
    r = build_rich_system(_inp())
    assert "ANTI-REPETIȚIE" in r
    for forbidden in (
        "Analizez catalogul",
        "compar opțiunile",
        "îți explic exact de ce",
        "nu doar ce",
    ):
        assert forbidden in r
    assert "Stil de răspuns" not in r  # apare doar când DomainPack trimite response_style


def test_rich_prompt_carries_style_when_present():
    styled = _inp(response_style={"ton": "natural, fara fraze-stampila"})
    r = build_rich_system(styled)
    assert "Stil de răspuns" in r
    assert "fraze-stampila" in r


def test_agent_prose_forbids_repetitive_ai_phrasing():
    # Garanția anti-template există și pe calea PROZĂ (tool-calling), necondiționat de
    # response_style (care e gated pe flag/pack) — nu doar în calea rich.
    s = build_agent_system(_inp())
    for forbidden in ("Analizez catalogul", "compar opțiunile", "îți explic exact de ce"):
        assert forbidden in s
    assert "ca un om din magazin" in s


def test_tools_block_multi_intent_and_concept_compare():
    s = build_agent_system(_inp())
    assert "MAI MULTE intenții" in s  # onorează toate intențiile unui mesaj (P8)
    assert "TIPURI/CONCEPTE" in s  # comparație de concepte, nu căutare de product_name inexistent


def test_build_defaults_tolerant():
    inp = PromptInputs.build("", "", "", [], [])
    assert inp.vertical == "ecommerce" and inp.business_name and inp.locale == "ro"
    # nu crapă, produce un prompt valid generic
    assert "search_products" in build_agent_system(inp)
