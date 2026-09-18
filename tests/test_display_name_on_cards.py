"""NX-301 — spre client pleacă numele SCURT al produsului, nu fraza de reclamă.

Măsurat pe primul catalog real (`sole-ro`, 2.758 produse active): `products.name` are mediana
**195** de caractere și maximul 383, fiindcă e nume + frază de marketing + gramaj. `display_name`
(capul dinaintea primului ` - `) are mediana **38**.

Scurtarea exista din septembrie și era aplicată peste tot unde produsul e numit pentru MODEL (axe
de comparație, `llm_view`, ancora chips-urilor, `state_block`). Singurul consumator rămas pe numele
întreg era CLIENTUL — am scurtat unde plăteam tokeni și am lăsat lung unde nu plătim nimic.
"""

from __future__ import annotations

import ast
from pathlib import Path

from src.catalog.render_text import display_name, size_label

# Numele REAL al primului card din turul `3e582c6d` (2026-09-18 08:30, «vreau ceva sa scap de
# cosuri») — 293 de caractere, din care produsul e primele 47.
REAL = (
    "SKIN1004 Madagascar Centella Tea-Trica Bha Foam - spuma de curatare formulata cu BHA si "
    "ulei din frunze de arbore de ceai, care contribuie la ingrijirea pielii cu tendinta "
    "acneica, a acneei, punctelor negre, cosurilor, inflamatiilor si tenului gras si la "
    "metinerea echilibrului pielii - 125 ml"
)


def test_the_real_card_title_becomes_a_product_name() -> None:
    assert len(REAL) == 293
    assert display_name(REAL) == "SKIN1004 Madagascar Centella Tea-Trica Bha Foam"
    assert size_label(REAL) == "125 ml"  # ce s-ar fi pierdut prin scurtare


def test_size_is_absent_not_guessed() -> None:
    """Gramajul e recuperabil determinist pe 47,5% din catalog. Pe restul răspunsul e `None`, nu o
    ghicire — cheia lipsește din card, ca `variants` sau `badge` (nu inventăm `null`-uri)."""
    assert size_label("PURITO Wonder Releaf - plasturi cu centella") is None
    assert size_label("Fără liniuță deloc") is None
    assert size_label(None) is None
    # coada trebuie să ÎNCEAPĂ cu o cifră: un text oarecare după ` - ` nu e un gramaj
    assert size_label("Ceva - pentru ten uscat") is None
    # formele compuse reale din catalog trec
    assert size_label("SKIN1004 Mask - 22 ml x 5 buc") == "22 ml x 5 buc"
    assert size_label("TIRTIR Mask - 30 buc (310 gr)") == "30 buc (310 gr)"


def test_a_head_too_short_to_identify_keeps_the_whole_name() -> None:
    """Capcana lui `display_name`: sub 8 caractere capul nu mai identifică nimic („Ser", „Cremă"),
    deci întoarce numele ÎNTREG. Pe catalogul SOLE maximul rămâne 115, deci regula nu degenerează —
    dar testul o acoperă, nu o presupune."""
    assert display_name("Ser - ser cu vitamina C pentru ten gras") == (
        "Ser - ser cu vitamina C pentru ten gras"
    )


# ── Clasa, declarată și verificată mecanic ────────────────────────────────────────────────────

#: NX-301 — funcțiile care construiesc text sau carduri DESTINATE CLIENTULUI.
#:
#: Granularitatea e pe FUNCȚIE, nu pe fișier, și asta e o limită declarată, nu o scăpare: aceleași
#: fișiere construiesc și `llm_view`-uri (text pentru MODEL), iar acelea sunt altă clasă, cu altă
#: economie. A le scurta pe amândouă cu același gest ar schimba ce vede modelul — o schimbare care
#: cere măsurătoare proprie (D15), nu un efect colateral al unui fix de afișare.
#:
#: În afara clasei, tot deliberat: ref-urile de STARE (`models.ProductRef`, `processor._refs`,
#: `aftercare._recommended_refs`) — memorie internă, nu text pe ecran, iar promptul le scurtează
#: deja la consum (`worker/context.py`).
CLIENT_FACING: dict[str, tuple[str, ...]] = {
    "src/worker/compose.py": ("_build", "build_comparison", "_derived_notes", "_legacy_notes"),
    "src/agent/brain_rich.py": ("card_refs",),
    "src/agent/fallbacks.py": ("_card_products", "_deterministic_reply"),
}

#: NEACOPERIT, declarat: coșul. `commerce_tools` construiește linii `{product_id, name, price}`
#: care arată a card, dar nu sunt: pleacă în `state_patch={"cart": ...}` și în `checkout_links.cart`
#: (refs de stare) și în `llm_view` (text pentru model). Coșul pe care îl VEDE clientul e
#: `CartSnapshot` (NX-237), cu contractul lui de display și flagul stins — se repară acolo, nu aici.
NOT_COVERED = ("src/tools/commerce_tools.py", "src/commerce/cart_service.py")


def test_no_client_facing_surface_carries_the_raw_name() -> None:
    """Poarta pe FORMĂ: în funcțiile care compun text/carduri pentru client, `x["name"]` are voie
    să apară doar învelit în `display_name(...)` sau `size_label(...)`.

    Motivul pentru care e o poartă și nu patru teste de comportament: clasa a fost găsită prin
    derivare mecanică, nu prin căutare — iar poarta a găsit imediat un membru pe care căutarea îl
    ratase, `_deterministic_reply` (textul de fallback pe care îl citește clientul când validatorul
    respinge răspunsul modelului). Un al cincilea loc adăugat mâine ar arăta exact ca primele patru.
    """
    offenders: list[str] = []
    for path, functions in CLIENT_FACING.items():
        module = ast.parse(Path(path).read_text(encoding="utf-8"))
        wanted = [
            node
            for node in ast.walk(module)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in functions
        ]
        assert len(wanted) >= len(functions), (
            f"{path}: funcție declarată care nu mai există — poarta măsoară altceva decât crede "
            f"(găsite: {sorted(n.name for n in wanted)}, declarate: {sorted(functions)})"
        )
        tree = ast.Module(body=wanted, type_ignores=[])
        wrapped: set[int] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"display_name", "size_label"}
            ):
                for inner in ast.walk(node):
                    wrapped.add(id(inner))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.slice, ast.Constant)
                and node.slice.value == "name"
                and id(node) not in wrapped
            ):
                offenders.append(f"{path}:{node.lineno}")
    assert not offenders, (
        "nume BRUT de produs pe o suprafață destinată clientului (mediana 195 de caractere pe "
        "catalogul real): " + ", ".join(offenders)
    )


def test_the_card_on_the_wire_carries_the_short_name_and_the_size() -> None:
    """Capătul: ce ajunge efectiv în JSON-ul widgetului."""
    from src.channels.web.render import _card

    card = _card(display_name(REAL), 80.0, size=size_label(REAL), product_id="p1")
    assert card["name"] == "SKIN1004 Madagascar Centella Tea-Trica Bha Foam"
    assert card["size"] == "125 ml"
    assert "size" not in _card("Nume fără gramaj", 10.0, size=None, product_id="p2")
