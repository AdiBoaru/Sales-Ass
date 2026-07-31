"""NX-207 — artefacte deterministe pentru search, generate în SHADOW.

Acest modul nu citește/scrie DB și nu înlocuiește `ai_summary`. Construiește, din facts validate,
documentul pozitiv, secțiunile FTS ponderabile, evidence și blurb-ul de card. Persistența și
embeddingurile versionate se leagă ulterior peste aceste ieșiri pure.

⚠ **RO-ONLY.** Funcția primește `locale` și îl pune pe artefacte, dar frazele pe care le COMPUNE
sunt în română, hardcodat („fără X adăugat", „Nu este recomandat pentru …"). Cu `locale='en'` ar
produce, tăcut, documente și evidence în română, etichetate ca engleză — un artefact greșit care
arată corect. Pilotul e RO-only prin decizie explicită, deci e acceptabil AZI; înainte de al doilea
locale, frazele astea trebuie să iasă din cod (per-locale, ca `_LABELS` din `worker/badges.py`).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from src.domain.contracts import CONTRACT_SCHEMA_VERSION, EvidenceChunk, ProductFacts

DOCUMENT_VERSION = 1


@dataclass(frozen=True)
class FtsDocument:
    """Componentele pentru `setweight`: A identifică, B diferențiază, C explică."""

    a: tuple[str, ...]
    b: tuple[str, ...]
    c: tuple[str, ...]

    def text(self) -> str:
        return " ".join((*self.a, *self.b, *self.c))


@dataclass(frozen=True)
class SearchArtifacts:
    business_id: str
    product_id: str
    locale: str
    document_version: int
    schema_version: int
    positive_search_document: str
    fts_document: FtsDocument
    evidence_chunks: tuple[EvidenceChunk, ...]
    # `None` = produsul n-are din ce compune un blurb util. Distinct de „blurb gol": vezi
    # `build_search_artifacts`.
    card_blurb: str | None
    content_hash: str


def _words(values: tuple[str, ...] | None) -> tuple[str, ...]:
    return values or ()


def _free_of_phrase(value: str) -> str:
    """O absență CONFIRMATĂ este criteriu pozitiv de selecție, nu o contraindicație."""
    clean = value.strip()
    return f"fără {clean} adăugat"


def _clean_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _hash_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_search_artifacts(
    product: dict[str, Any], *, business_id: str, locale: str, product_id: str | None = None
) -> SearchArtifacts:
    """Generează reproducibil artefactele NX-207 dintr-un produs care trece contractul NX-205.

    Documentul pozitiv nu include contraindicații, warning-uri sau `not_recommended_for`; acestea
    devin exclusiv evidence cu rol `warning`. `free_of` rămâne în document, fiind o proprietate
    pozitivă confirmată (de exemplu, căutarea „fără parfum”).
    """
    facts = ProductFacts.from_product(
        product, business_id=business_id, locale=locale, product_id=product_id
    )
    name = _clean_text(product.get("name")) or facts.product_id
    brand = _clean_text(product.get("brandSlug"))
    category = facts.category_slug
    description = _clean_text(product.get("description"))
    short_description = _clean_text(product.get("shortDescription"))

    b_terms = [
        *(_words(facts.key_ingredients)),
        *(_words(facts.concerns)),
        *(_words(facts.suitable_for)),
        *(_words(facts.differentiators)),
        *(_free_of_phrase(value) for value in _words(facts.free_of)),
    ]
    for value in (facts.finish, facts.coverage, facts.texture, facts.routine_step, facts.best_for):
        if value:
            b_terms.append(value)
    a_terms = [name, *(term for term in (brand, product.get("sku")) if _clean_text(term))]
    if category:
        b_terms.append(category)
    c_terms = [term for term in (short_description, description) if term]
    fts = FtsDocument(a=tuple(a_terms), b=tuple(b_terms), c=tuple(c_terms))

    # În vector intră numai semnale diferențiatoare și descrierea catalogului; nu marketing generic
    # sau negative care ar face un produs „NU pentru ten uscat” să potrivească query-ul „ten uscat”.
    positive_parts = [
        name,
        *(term for term in b_terms if term),
        *(term for term in c_terms if term),
    ]
    positive = ". ".join(dict.fromkeys(positive_parts))

    evidence: list[EvidenceChunk] = []
    for text, source in (
        (short_description, "catalog.shortDescription"),
        (description, "catalog.description"),
    ):
        if text:
            evidence.append(
                EvidenceChunk(
                    business_id=business_id,
                    product_id=facts.product_id,
                    locale=locale,
                    role="benefit",
                    text=text,
                    source=source,
                )
            )
    for warning in facts.not_recommended_for or ():
        warning_reason = warning.reason or "contraindicație"
        warning_text = f"Nu este recomandat pentru {warning.value}: {warning_reason}"
        evidence.append(
            EvidenceChunk(
                business_id=business_id,
                product_id=facts.product_id,
                locale=locale,
                role="warning",
                text=warning_text,
                source=warning.source or "catalog.not_recommended_for",
            )
        )

    # Fără cădere pe `name`: un blurb care e chiar numele produsului trece de `check length > 0` din
    # 036 și arată, pentru orice consumator, exact ca un blurb bun — dar nu spune nimic în plus față
    # de titlul de lângă el. `None` face golul VIZIBIL (poarta îl poate număra); un rând inutil nu.
    card_blurb = short_description or facts.key_benefit or facts.best_for or None
    payload = {
        "document_version": DOCUMENT_VERSION,
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "positive": positive,
        "fts": {"a": fts.a, "b": fts.b, "c": fts.c},
        "evidence": [chunk.model_dump() for chunk in evidence],
        "card_blurb": card_blurb,
    }
    return SearchArtifacts(
        business_id=business_id,
        product_id=facts.product_id,
        locale=locale,
        document_version=DOCUMENT_VERSION,
        schema_version=CONTRACT_SCHEMA_VERSION,
        positive_search_document=positive,
        fts_document=fts,
        evidence_chunks=tuple(evidence),
        card_blurb=card_blurb,
        content_hash=_hash_payload(payload),
    )
