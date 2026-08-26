"""Proza și axele din jurul tabelului de comparație, compuse de agent peste fapte înghețate.

**Ce s-a schimbat față de tabelul determinist.** Vechiul tabel proiecta coloane de catalog:
„Finisaj: mat", „Disponibilitate: În stoc", „Brand: Velora". Fiecare celulă era adevărată și
niciuna nu ajuta la alegere: un client nu întreabă „ce valoare are câmpul finish", ci „care mi
se potrivește mie". Un vânzător bun răspunde pe axe pe care le alege pentru perechea din față —
textură, rezistență, confort, pentru ce ocazie — și încheie spunând ce ar lua el. Axele alea nu
există ca rânduri în DB; sunt o citire a datelor, deci le scrie modelul.

**Ce ține locul garanției pierdute.** Determinismul dădea „zero halucinație prin construcție". Aici
garanția e alta, în trei straturi, și e mecanică, nu bazată pe încrederea în prompt:

  1. **Fiecare celulă își numește SURSA** (`source`), dintr-un vocabular ÎNCHIS derivat din fișa de
     fapte a produsului ăluia (`compose.product_fact_sheet`). O sursă care nu există pentru acel
     produs face celula să pice. Un model care vrea să inventeze o axă „Rezistență" pentru un produs
     fără date despre rezistență trebuie mai întâi să inventeze o sursă, iar sursele se verifică.
     E tiparul `pro_index` de la calea bogată, extins la o matrice.
  2. **Cifrele rămân ale codului.** Rândurile de preț și de rating le scrie `compose`, cu valoarea
     exactă. Modelul are voie doar cifrele care apar deja în fișele de fapte („4 g") — niciodată un
     preț, un rating sau un procent. Exemplul concurent scria „Foarte ieftin (sub 10 lei)"; noi avem
     cifra reală, iar o bandă calitativă ar fi un regres deghizat în stil.
  3. **Sancțiunea e LOCALĂ.** O celulă care pică dispare (randată „—"); o axă rămasă fără nicio
     celulă dispare; abia dacă nu mai rămâne nimic cade tot pe tabelul determinist. Un adjectiv
     nefericit pe a treia axă n-are voie să arunce un răspuns altfel corect.

Proza din jur (lead, subtitlu, cele două paragrafe de îndrumare) trece prin aceeași poartă: fără
cifre negroundate, fără superlative (verdictul cantitativ e al codului și are prag de
materialitate), fără livrare/promoție/garanție fără sursă, fără claim medical, fără stoc nesusținut.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from src.agent import prompt_builder
from src.agent.grounding_guard import unsourced_claims
from src.agent.prompt_builder import PromptInputs
from src.agent.voice import naturalize
from src.config import get_settings
from src.models import Comparison, ComparisonRow
from src.worker import compose
from src.worker.context import conversation_transcript
from src.worker.text_scrub import has_medical_claim, has_stock_claim, has_superlative_claim

if TYPE_CHECKING:
    from collections.abc import Sequence

    from src.domain.pack import FacetSpec
    from src.models import TurnContext

log = logging.getLogger(__name__)

#: Leadul e promis ca 2-3 fraze. Peste plafon nu trunchiem (o frază tăiată la jumătate e mai rea
#: decât una lipsă) — respingem și cade pe determinist, care e scurt prin construcție.
_MAX_LEAD_CHARS = 600
#: Un paragraf de îndrumare. Două paragrafe lungi sub tabel nu se mai citesc.
_MAX_CLOSING_CHARS = 480
#: Câte axe SEMANTICE compune modelul. Peste 6, tabelul redevine o fișă tehnică, adică fix problema
#: de la care am plecat. Prețul și ratingul se adaugă în plus, de cod.
_MAX_AXES = 6
#: Câte paragrafe de îndrumare intră sub tabel.
_MAX_CLOSING = 2
#: Eticheta unei axe e un titlu, nu o propoziție.
_MAX_AXIS_LABEL_CHARS = 40

_PERCENT = re.compile(r"%|\bla sut[ăa]\b", re.IGNORECASE)
_DIGITS = re.compile(r"\d+")

_NARRATIVE_SCHEMA: dict[str, Any] = {
    "name": "comparison_narrative",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["lead", "subtitle", "axes", "closing"],
        "properties": {
            "lead": {"type": "string"},
            "subtitle": {"type": ["string", "null"]},
            "axes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["label", "cells"],
                    "properties": {
                        "label": {"type": "string"},
                        "cells": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["product_id", "source", "text"],
                                "properties": {
                                    "product_id": {"type": "string"},
                                    # Din ce fapt al produsului e scrisă celula. Verificat contra
                                    # fișei lui — vezi stratul 1 din docstringul modulului.
                                    "source": {"type": "string"},
                                    "text": {"type": "string"},
                                },
                            },
                        },
                    },
                },
            },
            "closing": {"type": "array", "items": {"type": "string"}},
        },
    },
}


def _allowed_numbers(
    ctx: TurnContext,
    comparison: Comparison,
    products: list[dict[str, Any]],
    facets: Sequence[FacetSpec],
) -> set[str]:
    """Cifrele pe care proza și celulele au voie să le conțină.

    Cifrele CLIENTULUI (bugetul lui nu e halucinație, R4), cifrele din fișele de fapte ale
    produselor comparate („4 g", „SPF 30", „3 variante") și cifrele din observațiile DERIVATE de cod
    („diferența de preț e mică, 2,00 lei").

    Prețurile și ratingurile NU intră, deliberat: rândurile lor le scrie codul, sub text. Dacă
    le-am permite, „44,99 lei" ar deschide și „44", iar un preț pe jumătate citat e o cifră greșită
    cu aparență de fapt."""
    allowed = compose._allowed_client_numbers(ctx)
    allowed |= compose.spec_numbers(products, facets, ctx.language)
    for product in products:
        for value in compose.product_fact_sheet(product, facets, ctx.language).values():
            allowed |= set(_DIGITS.findall(value))
    for note in comparison.notes:
        allowed |= set(_DIGITS.findall(note))
    return allowed


def _stock_supported(products: list[dict[str, Any]]) -> bool:
    """Vreun produs comparat e efectiv vandabil? Faptul, nu impresia (aceeași regulă ca NX-118)."""
    return any((p.get("availability") or "") in ("in_stock", "low_stock") for p in products)


def prose_failures(text: str, allowed: set[str], products: list[dict[str, Any]]) -> tuple[str, ...]:
    """Verdictul pe o bucată de proză (lead, subtitlu, paragraf de îndrumare, celulă). PUR.

    Gol = se livrează. Apelantul decide cât de larg e efectul: pentru o celulă înseamnă „—", pentru
    lead înseamnă căderea pe tabelul determinist."""
    stripped = text.strip()
    if not stripped:
        return ("empty",)
    failures: list[str] = []
    if any(n not in allowed for n in _DIGITS.findall(stripped)):
        failures.append("ungrounded_number")
    if _PERCENT.search(stripped):
        # Nu există motor de promoții canonic, iar reducerea afișabilă o calculează codul pe card.
        failures.append("ungrounded_percentage")
    if has_superlative_claim(stripped):
        # Verdictul cantitativ e al codului ȘI e gated pe materialitate. Un superlativ scris de
        # model ar ocoli exact pragul din cauza căruia „cea mai accesibilă" nu se mai declară
        # pentru 2 lei, adică ar reintroduce fix defectul pe care îl reparăm.
        failures.append("unverifiable_superlative")
    if get_settings().safety_medical_guardrail_enabled and has_medical_claim(stripped):
        failures.append("medical_claim")
    if has_stock_claim(stripped) and not _stock_supported(products):
        failures.append("unsupported_stock_claim")
    failures.extend(unsourced_claims(stripped))
    return tuple(dict.fromkeys(failures))


def lead_failures(
    text: str,
    ctx: TurnContext,
    comparison: Comparison,
    products: list[dict[str, Any]],
    facets: Sequence[FacetSpec] = (),
) -> tuple[str, ...]:
    """Poarta pentru textul de NIVEL DE RĂSPUNS (lead / subtitlu / îndrumare). Respinge ÎNTREG: un
    lead e trei fraze legate între ele, iar a păstra două dintr-un raționament care s-a dovedit
    greșit într-a treia n-ar fi degradare, ar fi o afirmație pe jumătate verificată."""
    stripped = text.strip()
    if not stripped:
        return ("empty_lead",)
    if len(stripped) > _MAX_LEAD_CHARS:
        return ("lead_too_long",)
    failures = prose_failures(
        stripped, _allowed_numbers(ctx, comparison, products, facets), products
    )
    return tuple("empty_lead" if f == "empty" else f for f in failures)


def _clean(text: Any, cap: int) -> str | None:
    """Normalizează spațiile și taie ce e vizibil prea lung. `None` la gol."""
    out = " ".join(str(text or "").split())
    if not out or len(out) > cap:
        return None
    return out


def assemble_axes(
    payload: dict[str, Any],
    comparison: Comparison,
    products: list[dict[str, Any]],
    allowed: set[str],
    facets: Sequence[FacetSpec] = (),
    language: str | None = None,
) -> tuple[list[ComparisonRow], dict[str, int]]:
    """Axele modelului → rânduri de tabel, cu verificarea sursei pe fiecare celulă. PUR.

    Întoarce (rânduri, contoare de respingere). Ordinea axelor e a MODELULUI: el a văzut perechea
    și a decis ce contează întâi, iar asta e chiar munca pe care i-am dat-o. Ordinea coloanelor
    rămâne a codului (cea cerută de client)."""
    order = [c.product_id for c in comparison.columns]
    sheets = {
        str(p.get("id")): compose.product_fact_sheet(p, facets, language)
        for p in products
        if p.get("id")
    }
    rows: list[ComparisonRow] = []
    rejected: dict[str, int] = {}

    def _reject(code: str) -> None:
        rejected[code] = rejected.get(code, 0) + 1

    for axis in payload.get("axes") or []:
        if len(rows) >= _MAX_AXES:
            break
        if not isinstance(axis, dict):
            continue
        label = _clean(axis.get("label"), _MAX_AXIS_LABEL_CHARS)
        if label is None or prose_failures(label, allowed, products):
            _reject("axis_label")
            continue
        values: list[str | None] = [None] * len(order)
        for cell in axis.get("cells") or []:
            if not isinstance(cell, dict):
                continue
            pid = str(cell.get("product_id") or "")
            if pid not in order:
                _reject("cell_foreign_product")  # membership, ca la calea bogată
                continue
            sheet = sheets.get(pid) or {}
            source = str(cell.get("source") or "")
            if source not in sheet:
                # Sursa e jumătatea verificabilă a celulei. Fără ea, „Rezistență: se menține bine"
                # ar fi o propoziție plauzibilă despre un produs pentru care nu știm nimic.
                _reject("cell_unknown_source")
                continue
            text = _clean(cell.get("text"), 160)
            if text is None or prose_failures(text, allowed, products):
                _reject("cell_ungrounded")
                continue
            values[order.index(pid)] = text
        if not any(values):
            _reject("axis_empty")
        elif not compose.discriminates(values):
            # Aceeași regulă ca la tabelul determinist, aplicată pe ce a compus modelul: o axă pe
            # care ambele produse spun același lucru („4 g, 3 variante" pe amândouă) ocupă exact
            # locul unde clientul caută diferența. Promptul o interzice, dar un prompt nu e o
            # garanție — prinsă de `scripts/compare_drive.py` la prima rulare.
            _reject("axis_not_discriminating")
        else:
            rows.append(ComparisonRow(label=label, values=values))
    return rows, rejected


def _prompt_inputs(ctx: TurnContext) -> PromptInputs:
    """`PromptInputs` construit din `ctx`, FĂRĂ DB. Categoriile și aliasele lipsesc intenționat:
    sunt indicii de RUTARE, iar aici nu se rutează nimic — produsele sunt deja alese. Fără ele,
    calea deterministă (`serve_comparison`, care azi nu atinge modelul deloc) nu plătește un
    checkout de conexiune în plus, iar prefixul rămâne identic pe ambele căi, deci același cache."""
    pack = getattr(ctx.business, "domain_pack", None)
    style = pack.response_style if (pack and get_settings().response_style_enabled) else None
    return PromptInputs.build(
        ctx.business.name,
        ctx.business.vertical,
        ctx.language,
        [],
        [],
        currency=getattr(pack, "currency", None) or "RON",
        response_style=style,
    )


def _user_block(
    ctx: TurnContext,
    comparison: Comparison,
    products: list[dict[str, Any]],
    facets: Sequence[FacetSpec],
    query: str,
) -> str:
    history = conversation_transcript(ctx.history)
    sources = sorted(
        {source for p in products for source in compose.product_fact_sheet(p, facets, ctx.language)}
    )
    return (
        f"Limba clientului: {ctx.language}\n"
        + (f"Conversație până acum:\n{history}\n\n" if history else "")
        + (f"Ce a întrebat clientul acum: {query}\n\n" if query.strip() else "")
        + "Fapte citabile, per produs (`source: valoare`). Fiecare celulă pe care o scrii trebuie "
        + "să numească una dintre sursele produsului ei:\n"
        + compose.comparison_fact_sheets(comparison, products, facets, ctx.language)
        + "\n\nSurse disponibile în acest tur: "
        + (", ".join(sources) or "(niciuna)")
        + "\n\n"
        + compose.comparison_facts_block(comparison, ctx.language)
    )


async def compose_comparison(
    llm,
    ctx: TurnContext,
    comparison: Comparison,
    products: list[dict[str, Any]],
    *,
    facets: Sequence[FacetSpec] = (),
    query: str = "",
) -> Comparison:
    """Comparația NARATIVĂ: axe semantice + proză de încadrare + îndrumare finală.

    Nu ridică niciodată și nu întoarce niciodată mai puțin decât intrarea: un apel eșuat, un JSON
    stricat sau o poartă care respinge leadul duc la `comparison` neatinsă, adică la tabelul
    determinist pe care `build_comparison` l-a produs deja. Un tur de comparație are un tabel corect
    indiferent de ce face modelul (P6)."""
    settings = get_settings()
    if not settings.comparison_narrative_enabled:
        return comparison

    system = prompt_builder.build_compare_system(_prompt_inputs(ctx))
    user = _user_block(ctx, comparison, products, facets, query)
    try:
        payload = await llm.complete_schema(system, user, _NARRATIVE_SCHEMA)
    except Exception as e:  # noqa: BLE001 — apel structurat eșuat → tabelul determinist (P6)
        log.warning("compare_narrative: apel structurat eșuat (%s)", type(e).__name__)
        ctx.emit("comparison_narrative", source="deterministic", reason=type(e).__name__)
        return comparison
    if not isinstance(payload, dict):
        ctx.emit("comparison_narrative", source="deterministic", reason="bad_payload")
        return comparison

    allowed = _allowed_numbers(ctx, comparison, products, facets)

    lead = _clean(payload.get("lead"), _MAX_LEAD_CHARS)
    failures = lead_failures(lead or "", ctx, comparison, products, facets)
    if failures:
        # Leadul e contractul cu clientul („uite ce le desparte"). Dacă el nu ține, tabelul de sub
        # el n-are cine să-l încadreze, deci nu promovăm nici axele.
        ctx.emit("comparison_narrative", source="deterministic", reasons=list(failures))
        return comparison

    rows, rejected = assemble_axes(payload, comparison, products, allowed, facets, ctx.language)
    if not rows:
        ctx.emit("comparison_narrative", source="deterministic", reasons=["no_axis_survived"])
        return comparison

    # Cifrele rămân ale codului: prețul mereu, ratingul doar când diferența e materială. Se pun DUPĂ
    # axele semantice, ca în modelul pe care îl urmărim — clientul citește întâi ce sunt produsele
    # și abia apoi cât costă.
    rows.extend(compose.price_and_rating_rows(products, ctx.language))

    subtitle = _clean(payload.get("subtitle"), _MAX_LEAD_CHARS)
    if subtitle and prose_failures(subtitle, allowed, products):
        subtitle = None  # sancțiune LOCALĂ: cade fraza, nu răspunsul
    closing: list[str] = []
    for paragraph in payload.get("closing") or []:
        if len(closing) >= _MAX_CLOSING:
            break
        text = _clean(paragraph, _MAX_CLOSING_CHARS)
        if text and not prose_failures(text, allowed, products):
            closing.append(naturalize(text) or "")
        else:
            rejected["closing"] = rejected.get("closing", 0) + 1
    closing = [c for c in closing if c]

    ctx.emit(
        "comparison_narrative",
        source="model",
        n_axes=len(rows),
        n_closing=len(closing),
        # Contoare, nu texte: ce a fost respins e diagnoză, iar valorile ar fi conținut de client.
        rejected=sorted(f"{k}:{v}" for k, v in rejected.items()),
    )
    return Comparison(
        columns=comparison.columns,
        rows=rows,
        # VOCE (principiul 13): `set_comparison_reply` naturalizează floor-ul aplatizat, dar
        # `intro`/`subtitle`/`closing` pleacă spre frontend pe câmpurile lor.
        intro=naturalize(lead or "") or comparison.intro,
        subtitle=naturalize(subtitle) if subtitle else None,
        closing=closing,
        common=comparison.common,
        notes=comparison.notes,
    )


__all__ = ["assemble_axes", "compose_comparison", "lead_failures", "prose_failures"]
