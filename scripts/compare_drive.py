"""Probă reproductibilă a comparației NARATIVE: ce primește frontendul, fără DB și fără OpenAI.

Modelul e SCRIPTAT (zero credite): scopul nu e să vedem cât de bine scrie, ci ce trece de porți și
ce formă exactă are payload-ul. Rulează trei scenarii, în ordinea în care contează:

  1. narativ acceptat        — axele modelului + cifrele codului + îndrumarea de sub tabel
  2. narativ parțial respins — o celulă fără sursă și un paragraf cu cifră inventată cad SINGURE
  3. narativ respins         — leadul pică poarta ⇒ tabelul determinist, întreg

    python scripts/compare_drive.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.agent.compare_narrative import compose_comparison  # noqa: E402
from src.channels.web.render import render_web  # noqa: E402
from src.domain.pack import FacetSpec  # noqa: E402
from src.models import (  # noqa: E402
    BusinessConfig,
    Contact,
    ConversationState,
    InboundMessage,
    Reply,
    TurnContext,
)
from src.worker.compose import build_comparison, comparison_cards, flatten_comparison  # noqa: E402

# Fațetele REALE ale pachetului `beauty_salon` (DomainPack.comparison_facets) — ele decid ce surse
# poate cita modelul, deci o probă cu alt set ar măsura alt sistem.
FACETS = (
    FacetSpec(key="key_benefit", labels={"ro": "Beneficiu principal"}),
    FacetSpec(key="key_ingredients", labels={"ro": "Ingrediente cheie"}),
    FacetSpec(key="concerns", labels={"ro": "Potrivit pentru"}),
    FacetSpec(key="suitable_for", labels={"ro": "Potrivit pentru"}),
    FacetSpec(key="finish", labels={"ro": "Finisaj"}),
    FacetSpec(key="coverage", labels={"ro": "Acoperire"}),
    FacetSpec(key="texture", labels={"ro": "Textură"}),
)

# Datele REALE ale celor două rujuri din conversația raportată (audit 2026-08-26 pe catalogul demo).
PRODUCTS = [
    {
        "id": "p1",
        "name": "Velora Soft Matte Ruj",
        "brand": "Velora",
        "price": 42.99,
        "rating": 4.5,
        "availability": "in_stock",
        "url": "https://shop/p1",
        "image": "https://cdn/p1.jpg",
        "ai_summary": "Recomandat pentru cine vrea un finish mat.",
        "attributes": {
            "finish": "mat",
            "key_benefit": "Culoare intensă cu finish mat confortabil.",
            "specs": {"Volum": "4 g", "Variante disponibile": "3"},
        },
        "top_pros": ["nu usucă buzele", "pigmentare bună"],
        "top_cons": ["se transferă la mese lungi"],
    },
    {
        "id": "p2",
        "name": "NudeLab Velvet Ruj mat",
        "brand": "NudeLab",
        "price": 44.99,
        "rating": 4.6,
        "availability": "in_stock",
        "url": "https://shop/p2",
        "image": "https://cdn/p2.jpg",
        "ai_summary": "Recomandat pentru cine vrea un finish mat. Ingrediente-cheie: unt de shea.",
        "attributes": {
            "finish": "mat",
            "key_benefit": "Mat catifelat care rămâne confortabil ore întregi.",
            "key_ingredients": ["unt de shea"],
            "specs": {"Volum": "4 g", "Variante disponibile": "3"},
        },
        "top_pros": ["confortabil pentru un mat", "pigment intens"],
        "top_cons": ["transferă ușor la mese"],
    },
]


def _axis(label, *cells):
    return {
        "label": label,
        "cells": [{"product_id": p, "source": s, "text": t} for p, s, t in cells],
    }


GOOD = {
    "lead": "Le-am pus față în față. Sunt amândouă mate, deci alegerea se joacă la senzația pe "
    "buze și la cât de intens vrei pigmentul.",
    "subtitle": "Unul mizează pe culoare intensă, celălalt pe un mat catifelat, mai blând.",
    "axes": [
        _axis(
            "Senzația pe buze",
            ("p1", "avantaje", "Nu usucă buzele, se poartă ușor."),
            ("p2", "de_luat_in_calcul", "Catifelat, rămâne comod ore întregi."),
        ),
        _axis(
            "Intensitatea culorii",
            ("p1", "key_benefit", "Culoare intensă, acoperire bună."),
            ("p2", "avantaje", "Pigment intens, dar cu aspect mai moale."),
        ),
        _axis(
            "Ce se ia în calcul",
            ("p1", "de_luat_in_calcul", "Se transferă la mesele lungi."),
            ("p2", "de_luat_in_calcul", "Transferă ușor la masă."),
        ),
        _axis(
            "Formatul",
            ("p1", "specificatii", "4 g, 3 variante de nuanță."),
            ("p2", "specificatii", "4 g, 3 variante de nuanță."),
        ),
    ],
    "closing": [
        "Când alegi, gândește-te întâi dacă vrea culoare cât mai vizibilă sau o senzație cât mai "
        "comodă pe buze. Amândouă se retușează după masă.",
        "Pentru un cadou aș lua varianta catifelată: e mai iertătoare pe buze uscate, deci merge "
        "și dacă nu-i știi preferințele exacte.",
    ],
}

PARTIAL = {
    **GOOD,
    "axes": [
        GOOD["axes"][0],
        _axis(
            "Cât rezistă",
            ("p1", "avantaje", "Ține bine peste zi."),
            ("p2", "rezistenta", "Rezistă 12 ore."),  # sursă inexistentă ȘI cifră inventată
        ),
    ],
    "closing": [
        "Alege după senzația pe buze.",
        "Ambele rezistă 8 ore fără retuș.",  # cifră fără sursă → cade singur
    ],
}

BAD = {**GOOD, "lead": "Velora e cea mai bună și costă 42 de lei."}


class ScriptedLLM:
    def __init__(self, payload):
        self.payload = payload

    async def complete_schema(self, system, user, schema, *, model=None):
        return self.payload


def _ctx() -> TurnContext:
    return TurnContext(
        turn_id="t",
        business=BusinessConfig(id="b", slug="nativex-demo", name="Sole Demo", vertical="beauty"),
        contact=Contact(id="c", business_id="b"),
        message=InboundMessage(provider_msg_id="m", body="care e diferenta dintre ultimele 2"),
        conversation_id="conv",
        state=ConversationState(),
    )


async def run(title: str, payload) -> None:
    ctx = _ctx()
    base = build_comparison(PRODUCTS, "ro", FACETS)
    cmp = await compose_comparison(
        ScriptedLLM(payload), ctx, base, PRODUCTS, facets=FACETS, query=ctx.message.body
    )
    reply = Reply(
        text=flatten_comparison(cmp, "ro"),
        products=comparison_cards(cmp),
        comparison=cmp,
        cacheable=False,
    )
    out = render_web(reply, "ro")
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")
    evt = next((e for e in ctx.events if e.type == "comparison_narrative"), None)
    print(f"verdict: {evt.properties if evt else '(niciun event)'}\n")
    print(json.dumps(out.get("comparison", {}), ensure_ascii=False, indent=2))
    print(f"\ncontent: {out['content']}")


async def main() -> None:
    await run("1. NARATIV ACCEPTAT — axele modelului + cifrele codului", GOOD)
    await run("2. PARȚIAL — celula fără sursă și paragraful cu cifră cad SINGURE", PARTIAL)
    await run("3. RESPINS — leadul pică poarta ⇒ tabelul determinist, întreg", BAD)


asyncio.run(main())
