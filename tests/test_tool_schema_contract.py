"""Poartă: nicio schemă de unealtă nu pleacă la furnizor într-o formă care NU poate fi chemată.

## De ce exact asta

O unealtă cu `enum: []` nu e o unealtă slabă, e una imposibilă: nu există argument valid. Cu
`strict: true`, furnizorul refuză schema cu 400, iar un 4xx e TERMINAL în `_with_retry` și înghițit
de `agent_stage` — deci moare tot drumul de vânzare, și numai el. Triajul (alt model, altă cerere)
rămâne sănătos deasupra, deci sistemul pare viu. E aceeași formă de eșec ca incidentul din
2026-08-24, și singurul mod de a o vedea înainte de producție e s-o verifici pe SCHEMA construită,
nu pe lista de nume.

NX-297 a intrat exact pe acolo: regula „fără familii nu se oferă tool-ul deloc" era DECLARATĂ în
`tool_definitions`, dar impusă doar în `turn_profile.select`, adică într-un singur apelant. Când
`_SALES_TOOLS` a devenit al doilea, a moștenit doar numele, nu și regula.

## Ce verifică, și ce NU

Verifică FORMA a tot ce poate ieși, pe ambele profile de tenant (pachet gol / pachet complet), nu
un caz ales cu mâna. Nu verifică dacă valorile enumului sunt cele bune — alea sunt ale pachetului
și au testele lor (NX-292, NX-262).
"""

from __future__ import annotations

from typing import Any

from src.agent.tool_definitions import _SCHEMAS, tenant_enum_values, tool_schemas
from src.tools.base import _ORDER_TOOLS, _SALES_TOOLS

ALL_NAMES = sorted(set(_SCHEMAS) | set(_SALES_TOOLS) | set(_ORDER_TOOLS))


class _Steps:
    families = {"fata": (), "par": ()}
    time_markers = {"am": (), "pm": ()}


class _Registry:
    specs = {"complement": (), "step": ()}


class _FullPack:
    """Tenant care a declarat TOT ce pot cere enumurile."""

    relation_kinds = _Registry()
    routine_steps = _Steps()


def _bad_enums(schema: dict[str, Any]) -> list[str]:
    props = (schema.get("function") or {}).get("parameters", {}).get("properties", {})
    return [name for name, spec in props.items() if spec.get("enum") == []]


def test_nicio_schema_emisa_nu_are_enum_gol() -> None:
    """Invariantul, pe AMBELE profile de tenant. Cu pachet gol uneltele care depind de el trebuie
    să DISPARĂ din listă, nu să iasă cu enum vid."""
    for label, values in (("pachet gol", {}), ("pachet plin", tenant_enum_values(_FullPack()))):
        for schema in tool_schemas(list(ALL_NAMES), **values):
            bad = _bad_enums(schema)
            assert not bad, (
                f"{label}: `{schema['function']['name']}` iese cu enum GOL pe {bad}. "
                "Nu există argument valid ⇒ furnizorul refuză schema ⇒ 4xx terminal, înghițit."
            )


def test_unealta_fara_valori_de_tenant_nu_se_ofera_deloc() -> None:
    """Perechea obligatorie a testului de mai sus: dovada că dispariția e chiar mecanismul, nu un
    efect secundar al faptului că nimeni nu cere uneltele alea."""
    empty = {s["function"]["name"] for s in tool_schemas(list(ALL_NAMES))}
    full = {
        s["function"]["name"]
        for s in tool_schemas(list(ALL_NAMES), **tenant_enum_values(_FullPack()))
    }
    assert "routine_plan" not in empty and "related_products" not in empty
    assert {"routine_plan", "related_products"} <= full
    # Restul toolsetului nu depinde de pachet: o unealtă fără enum de tenant nu are voie să cadă
    # odată cu ele (altfel un pachet subțire ar lăsa turul fără `search_products`).
    assert full - empty == {"routine_plan", "related_products"}


def test_parametrul_de_rafinament_dispare_in_loc_sa_ramana_fara_valori() -> None:
    """`moment` e rafinament (o rutină e validă și fără el), deci DISPARE; `family` e indispensabil,
    deci ia unealta cu el. Cele două mecanisme nu se confundă."""

    class _NoMoments:
        routine_steps = type("S", (), {"families": {"fata": ()}, "time_markers": {}})()

    (routine,) = tool_schemas(["routine_plan"], **tenant_enum_values(_NoMoments()))
    props = routine["function"]["parameters"]["properties"]
    assert "moment" not in props
    assert props["family"]["enum"] == ["fata"]
    assert "moment" not in routine["function"]["parameters"]["required"]


def test_toate_uneltele_oferite_au_schema() -> None:
    """Un nume în toolset fără schemă e tăcut de două ori: `enabled_tools` îl filtrează pe
    `TOOL_REGISTRY`, iar `tool_schemas` pe `_SCHEMAS`, deci n-ar apărea nicăieri ca lipsă."""
    missing = sorted((set(_SALES_TOOLS) | set(_ORDER_TOOLS)) - set(_SCHEMAS))
    assert not missing, f"unelte oferite fără schemă OpenAI: {missing}"
