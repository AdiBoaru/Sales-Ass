"""NX-336 — catalog SINTETIC în forma pachetului SOLE, DOAR pentru harnessul de stagiu.

Pachetul REAL `sole-ro` (din `db/seed/`) n-are produse de fixture, deci journey-urile SOLE nu pot
trece prin `agent_stage` fără un catalog: contractul cere „× 5 packs" pe stagiul real. Produsele de
aici sunt INVENTATE (nume, prețuri, atribute), niciun rând nu vine din catalogul clientului, iar
cheile de atribut sunt fațetele pachetului SOLE (`product_type`, `skin_type`, `concerns`,
`routine_time`, `routine_step`, `finish`).

Catalogul trăiește DOAR aici. `fixture_catalog.vocabulary("sole-ro")`, folosit de replay, rămâne
GOL, fiindcă etichetele existente presupun vocabularul gol (n15 așteaptă `prefer: {skin_type:
[dry]}`; cu vocabularul de aici „ten uscat" ar fi `explicit`, deci filtru dur). Harnessul de stagiu
nu compară cu etichetele journey-ului, ci cu lanțul pur (`kernel_step`) pe ACEST catalog."""

from __future__ import annotations

from typing import Any

from src.catalog.vocabulary import CatalogVocabulary
from src.conversation.references import CatalogLookup, ReferenceFacts
from tests.kernel.fixture_catalog import facts_of, vocabulary_of

NAME = "sole-ro"

CATEGORIES: tuple[dict[str, str], ...] = (
    {"key": "ten-ingrijirea-tenului", "name": "Ingrijirea tenului"},
    {"key": "par-ingrijirea-parului", "name": "Ingrijirea parului"},
    {"key": "corp-ingrijirea-corpului", "name": "Ingrijirea corpului"},
    {"key": "machiaj-fata", "name": "Fata"},
)


def _p(pid: str, name: str, category: str, price: float, **attributes: str) -> dict[str, Any]:
    availability = attributes.pop("availability", "in_stock")
    return {
        "id": pid,
        "name": name,
        "category": category,
        "price": price,
        "availability": availability,
        "attributes": attributes,
    }


_TEN = "ten-ingrijirea-tenului"
PRODUCTS: tuple[dict[str, Any], ...] = (
    _p(
        "so-01",
        "Crema Aurelia Hidratare Intensa",
        _TEN,
        89,
        product_type="crema de fata",
        skin_type="dry",
        concerns="hydration",
        routine_time="am_pm",
        routine_step="fata:hidratare",
    ),
    _p(
        "so-02",
        "Crema Belvia Matifianta Zilnica",
        _TEN,
        75,
        product_type="crema de fata",
        skin_type="oily",
        concerns="pores",
        routine_time="am",
        routine_step="fata:hidratare",
    ),
    _p(
        "so-03",
        "Ser Corvina Luminozitate",
        _TEN,
        120,
        product_type="ser de fata",
        skin_type="normal",
        concerns="dullness",
        routine_time="pm",
        routine_step="fata:tratament",
    ),
    _p(
        "so-04",
        "Ser Delmira Calmant",
        _TEN,
        110,
        product_type="ser de fata",
        skin_type="sensitive",
        concerns="redness",
        routine_time="am_pm",
        routine_step="fata:tratament",
    ),
    _p(
        "so-05",
        "Toner Elvaro Echilibrant",
        _TEN,
        55,
        product_type="toner de fata",
        skin_type="combination",
        concerns="pores",
        routine_time="am_pm",
        routine_step="fata:tonifiere",
    ),
    _p(
        "so-06",
        "Masca Florenta Nutritiva",
        _TEN,
        35,
        product_type="masca de fata",
        skin_type="dry",
        concerns="hydration",
    ),
    _p("so-07", "Sampon Galvina Volum", "par-ingrijirea-parului", 45, product_type="sampon"),
    _p(
        "so-08",
        "Sampon Harvia Hidratant",
        "par-ingrijirea-parului",
        49,
        product_type="sampon",
        concerns="hydration",
    ),
    _p(
        "so-09",
        "Fond de ten Ilvena Satin",
        "machiaj-fata",
        99,
        product_type="fond de ten",
        finish="satin",
    ),
    _p(
        "so-10",
        "Crema Jorvala Reparatoare Noapte",
        _TEN,
        140,
        product_type="crema de fata",
        skin_type="normal",
        concerns="anti_aging",
        routine_time="pm",
        routine_step="fata:hidratare",
    ),
    _p(
        "so-11",
        "Crema Kelvira Bariera Protectoare",
        _TEN,
        95,
        product_type="crema de fata",
        skin_type="sensitive",
        concerns="barrier",
        availability="out_of_stock",
    ),
    _p("so-12", "Gel de corp Lumara Revigorant", "corp-ingrijirea-corpului", 39),
)


def products() -> dict[str, dict[str, Any]]:
    return {p["id"]: p for p in PRODUCTS}


def vocabulary() -> CatalogVocabulary:
    return vocabulary_of(f"b-{NAME}", PRODUCTS, CATEGORIES)


def facts(lookup: CatalogLookup | None = None) -> ReferenceFacts:
    return facts_of(products(), lookup, snapshot=f"synthetic:{NAME}")


def category_menu() -> tuple[tuple[str, int], ...]:
    return tuple(sorted((e.key, e.count) for e in vocabulary().categories))


__all__ = ["CATEGORIES", "NAME", "PRODUCTS", "category_menu", "facts", "products", "vocabulary"]
