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
from src.config import card_slots


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
    assert "search_products" in s and "Reguli:" in s  # blocul de tool-uri + reguli


def test_tools_block_nu_promite_un_plafon_pe_care_codul_nu_il_impune():
    """Plafonul real e pe RUNDE (`run_tool_loop(max_steps=3)`), nu pe apeluri de unelte: o rundă
    poate cere oricâte. O cifră de apeluri în prompt ar fi o regulă pe care nimic n-o verifică."""
    s = build_agent_system(_inp())
    assert "Maxim 3 apeluri" not in s
    assert "apeluri de unelte" not in s


def test_intrebarea_finala_e_conditionata_de_tipul_turului():
    """Pe un fapt punctual sau după o acțiune reușită răspunsul se oprește, ca în profilul
    `exact`. Necondiționată, regula contrazicea profilul în ziua în care se aprinde."""
    s = build_agent_system(_inp())
    assert "Termină cu o întrebare scurtă" not in s
    assert "oprește-te după răspuns" in s


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


def test_rich_suggestions_are_client_messages_not_labels():
    """Chips-urile sunt MESAJELE pe care le-ar scrie clientul (3-5, ancorate în turul curent), nu
    etichete de două cuvinte: la tap textul se întoarce în pipeline ca mesaj nou, deci „Mai
    accesibil" pierde subiectul pe drum. Promptul trebuie să ceară explicit ancorarea și
    self-containment-ul, altfel modelul revine la eticheta generică."""
    r = build_rich_system(_inp())
    assert "3-5 MESAJE de follow-up" in r
    assert "ANCOREAZĂ fiecare sugestie" in r
    assert "ROLURI DIFERITE" in r
    assert "TAPPABILE" not in r  # vechea formulare, cea care producea „Culoare intensă"
    for role in ("rafinare pe ATRIBUT", "rafinare pe BUGET", "COMPARAȚIE"):
        assert role in r
    assert "Compară primele două" in r  # acum e exemplu BUN (scurt/tappabil), nu de evitat


def test_rich_segmentation_and_constraint_echo():
    r = build_rich_system(_inp())
    assert "SEGMENTARE" in r and "AXĂ DIFERITĂ" in r  # motivele = arbore de decizie (P1)
    assert "rămâne în bugetul tău" in r  # ecoul constrângerii în pick (P4)
    # NX-298: capul e 6 (decizia Adi, 2026-09-17), dar testul nu pinuiește cifra, ci PROPRIETARUL
    # ei. Un literal aici ar fi a cincea copie a aceluiași număr, adică exact defectul reparat:
    # cine schimbă `CARD_SLOTS` ar vedea testul roșu și ar fi împins să scrie cifra a doua oară.
    assert f"PÂNĂ LA {card_slots()} produse" in r
    assert "{CARD_SLOTS}" not in r  # marcatorul s-a substituit, nu a plecat literal spre model


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


# --- NX-299: mărimea raftului în antet ---------------------------------------


def test_shelf_sizes_reach_the_prompt():
    """Turul `42744330`: modelul a cerut «Dermato cosmetice» (6 produse) pentru o cerere de acnee,
    în timp ce «Ten» avea 1.461. Lista de nume nu-i spunea nimic despre mărime, iar filtrul dur a
    transformat alegerea într-un răspuns de două carduri dintr-un catalog cu 518 candidați."""
    s = build_agent_system(_inp(categories=[("Ten", 1461), ("Dermato cosmetice", 6)]))
    assert "Ten (1500)" in s
    assert "Dermato cosmetice (6)" in s
    assert "nu alege un raft mic" in s  # regula, nu doar datele


def test_shelf_size_rounding_survives_catalog_drift():
    """Prefixul static e cache-uit la furnizor, deci cifra trebuie să reziste la un produs
    intrat sau ieșit. Exact ar sparge cache-ul la fiecare sincronizare."""
    a = build_agent_system(_inp(categories=[("Ten", 1461)]))
    b = build_agent_system(_inp(categories=[("Ten", 1478)]))
    assert a == b  # byte-identic, deci cache-hit
    # …dar diferența care contează rămâne vizibilă
    c = build_agent_system(_inp(categories=[("Ten", 6)]))
    assert c != a


def test_small_shelves_keep_their_exact_size():
    """Sub 100, cifra E semnalul: «6» și «10» cer decizii diferite, iar rotunjirea
    le-ar confunda."""
    s = build_agent_system(_inp(categories=[("Barbati", 3), ("Copii", 5), ("Electrica", 47)]))
    assert "Barbati (3)" in s and "Copii (5)" in s and "Electrica (47)" in s


def test_plain_name_list_stays_byte_identical():
    """Apelanții vechi (și testele de mai sus) pasează `list[str]`. Fără toleranța asta, felia ar
    fi schimbat tăcut promptul oricărei căi care n-a fost actualizată."""
    s = build_agent_system(_inp(categories=["Creme", "Parfumuri"]))
    assert "Creme, Parfumuri" in s
    assert "(0)" not in s  # mărimea necunoscută se OMITE, nu se raportează ca zero
