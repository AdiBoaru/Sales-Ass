"""NX-240 — `GroundingGuard`: poarta dintre ce a PROPUS modelul și ce are voie să vadă clientul.

`AnswerPlanV2` a trecut deja de validatorul NX-211/239: tenant, evidence IDs, hard constraints,
obligații, nevoi revocate. Guardul de aici nu repetă acele verificări — le PRESUPUNE și pune
întrebarea următoare, singura care contează la randare:

    „Fiecare lucru pe care ViewModel-ul e pe cale să-l AFIRME are un fapt cu sursă în spate?"

Diferența nu e academică. Validatorul de plan verifică *referințe*: claimul citează un
`evidence_id` care există. Guardul verifică *valori*: cifra din proză e chiar valoarea faptului,
procentul de reducere se recalculează din două prețuri în aceeași monedă, iar o promisiune de
livrare pentru care nu există adaptor nu poate fi „aproape adevărată" — pur și simplu nu are cum
să fie adevărată.

**Două feluri de verdict, deliberat separate:**

  • **`failures`** — răspunsul NU se livrează așa. Proza afirmă un preț care nu există, un claim
    medical, o livrare fără sursă. Ieșirea e fallback determinist (P6), nu o „corectare": guardul
    nu rescrie niciodată un fapt, fiindcă nu are de unde să știe care era cel corect.
  • **`omissions`** — răspunsul se livrează, dar mai sărac. Un rating fără recenzii dispare, un
    preț stale dispare împreună cu CTA-ul care depindea de el, o celulă de comparație rămâne
    explicit necunoscută. Fiecare omisiune poartă un cod → `view_field_omitted{field,reason}`.

Un răspuns onest mai sărac bate un răspuns bogat inventat; un răspuns fals nu bate nimic.

**UNKNOWN ≠ MISMATCH (D7), aplicat la randare:** un produs contrazis de o constrângere HARD e
BLOCAT (nu apare nicăieri: nici card, nici comparație, nici acțiune); un produs cu fațete
necunoscute apare, dar nu declară match — necunoscutul devine disclosure, nu tăcere.

**Reuse, nu a treia politică:** proza trece prin `validator.validate_prose` (NX-91/117/118 +
P0-safety medical), exact cel de pe calea de proză, dar alimentat cu faptele BUNDLE-ului, nu cu
rândurile brute de retrieval. Un singur entrypoint (`prose_failures`), folosit ȘI pentru răspuns,
ȘI pentru fiecare motiv de recomandare — o singură definiție a lui „fondat".

Singura regulă de acolo pe care NU o moștenim e `check_claims`: NX-117 interzice orice pomenire de
„reducere"/„recenzii"/„livrare" fiindcă pe calea de proză nu are cum să le verifice. Aici avem cum,
iar înlocuitorul e mai STRICT decât interdicția — procentul se recalculează din două prețuri în
aceeași monedă, stocul se cere dintr-un fapt de disponibilitate, iar livrarea/promoția/garanția se
resping fiindcă nu au sursă deloc. Superlativul rămâne respins: nu există fapt „cel mai bun".
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Final, Literal

from src.agent.answer_plan import AnswerPlanV2, PlanClarification, PlanNoResults
from src.agent.evidence_bundle import EvidenceBundle, ProductEvidence
from src.agent.validator import validate_prose
from src.web.localization import format_discount, normalize_locale
from src.worker.text_scrub import has_stock_claim, has_superlative_claim

# ── Vocabular ÎNCHIS de coduri (labels de metrică — low cardinality) ────────────────────────
GroundingFailure = Literal[
    "tenant_mismatch",
    "locale_mismatch",
    "empty_answer",
    "ungrounded_prose",
    "ungrounded_percentage",
    "unsourced_delivery_claim",
    "unsourced_promo_claim",
    "unsourced_warranty_claim",
    "unsupported_stock_claim",
    "unverifiable_superlative",
    "no_renderable_product",
]

OmissionReason = Literal[
    "unknown",
    "stale",
    "no_source",
    "not_in_evidence",
    "blocked",
    "generic_reason",
    "ungrounded_reason",
    "invalid_url",
    "unverified",
    "commerce_disabled",
]

#: Procent în proză („-18%", „18 la sută"). Trebuie să corespundă unei reduceri REALE calculabile
#: din două prețuri ale bundle-ului — altfel e o cifră comercială fără sursă.
_PERCENT_RE: Final[re.Pattern[str]] = re.compile(r"(\d{1,3})\s*(?:%|la sut[ăa])", re.IGNORECASE)

#: Promisiuni de livrare. Fără adaptor de fulfillment (STRUCTURALLY_UNSOURCED), ORICE formulare
#: din familia asta e o promisiune pe care nu o putem susține — inclusiv „ajunge mâine".
_DELIVERY_RE: Final[re.Pattern[str]] = re.compile(
    r"\bliv(?:rare|rat|ram)\w*\s+(?:in|în|pana|până|maxim|gratuit|rapid)"
    # Pluralul e la fel de promisiune ca singularul, iar o comparație vorbește din start despre
    # DOUĂ produse („amândouă ajung mâine") — forma cea mai probabilă era exact cea neacoperită.
    r"|\bajung(?:e|i|em)?\s+(?:maine|mâine|azi|astazi|astăzi|in|în)\b"
    r"|\bcurier\w*\s+(?:in|în|azi|maine|mâine)\b"
    r"|\bdelivered?\s+(?:in|by|tomorrow|today)\b|\bdelivery\s+(?:in|within|by)\b",
    re.IGNORECASE,
)

#: Promoții/vouchere. Nu există motor de promoții canonic: un „cod de reducere" e invenție.
_PROMO_RE: Final[re.Pattern[str]] = re.compile(
    r"\bvoucher\w*\b|\bcupon\w*\b|\bcod\s+(?:de\s+)?(?:reducere|promo\w*|discount)\b"
    r"|\bpromo\s*cod\w*\b|\bcoupon\b|\bpromo\s*code\b",
    re.IGNORECASE,
)

#: Garanție/retur. Sunt fapte JURIDICE ale comerciantului, nu coloane de catalog: „garanție 24 de
#: luni" nu se poate deriva din nimic din bundle, deci nu se poate afirma.
_WARRANTY_RE: Final[re.Pattern[str]] = re.compile(
    r"\bgaran[țt]\w*\b|\bwarrant\w*\b|\bdrept\s+de\s+retur\b",
    re.IGNORECASE,
)


def unsourced_claims(text: str) -> tuple[str, ...]:
    """Codurile familiilor FĂRĂ SURSĂ pe care le atinge textul (livrare, promoție, garanție), în
    ordine fixă. Gol = niciuna.

    Public fiindcă întrebarea „are livrarea/promoția/garanția un fapt în spate?" nu e specifică
    planului v2: orice proză comercială scrisă de model o pune. Un al doilea set de regexuri pe
    calea de comparație ar diverge de ăsta exact în tăcere, adică fix cum arată o gaură de
    grounding. Ordinea e parte din contract: codurile ajung în metrici cu vocabular ÎNCHIS."""
    out: list[str] = []
    if _DELIVERY_RE.search(text):
        out.append("unsourced_delivery_claim")
    if _PROMO_RE.search(text):
        out.append("unsourced_promo_claim")
    if _WARRANTY_RE.search(text):
        out.append("unsourced_warranty_claim")
    return tuple(out)


# ── Rezultatul grounding-ului ───────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class FieldOmission:
    """Un câmp care NU intră în ViewModel + motivul. Perechea e telemetrie
    (`view_field_omitted{field,reason}`), nu text pentru client."""

    field: str
    reason: str
    product_id: str | None = None


@dataclass(frozen=True, slots=True)
class GroundedProduct:
    """Un produs care a supraviețuit: faptele lui + motivul (dacă a rezistat) + dreptul de a
    purta un CTA comercial. Projectorul nu mai ia nicio decizie de adevăr peste asta."""

    evidence: ProductEvidence
    reason: str | None = None
    commerce_allowed: bool = False
    commerce_blocked_reason: str | None = None


@dataclass(frozen=True, slots=True)
class GroundedComparison:
    """Comparație cu celule deja rezolvate: `None` = necunoscut ONEST (projectorul îl randează ca
    „—"), nu zero și nu celulă lipsă. Fără câmp `winner`: concluzia se citește din celule."""

    product_ids: tuple[str, ...]
    axes: tuple[str, ...]
    cells: dict[tuple[str, str], Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GroundedAnswer:
    """Ce are voie să ajungă în `web-view.v2`. Dacă `ok` e False, nimic din asta nu se randează:
    apelantul livrează fallback-ul determinist."""

    ok: bool
    locale: str
    business_id: str
    #: Momentul în care faptele au fost înghețate. E ceasul projectorului: două proiecții ale
    #: aceluiași turn folosesc ACEEAȘI valoare, deci nu pot produce texte de prospețime diferite.
    as_of: datetime | None = None
    direct_answer: str = ""
    disclosures: tuple[str, ...] = ()
    clarification: PlanClarification | None = None
    no_results: PlanNoResults | None = None
    products: tuple[GroundedProduct, ...] = ()
    comparison: GroundedComparison | None = None
    memory_criteria: tuple[str, ...] = ()
    cart: Any = None
    failures: tuple[str, ...] = ()
    omissions: tuple[FieldOmission, ...] = ()

    @property
    def has_content(self) -> bool:
        return bool(
            self.direct_answer.strip()
            or self.products
            or self.comparison
            or self.clarification
            or self.no_results
        )


# ── Proiecția bundle → forma pe care validatorul de proză o înțelege ────────────────────────
def bundle_products_for_validator(bundle: EvidenceBundle) -> list[dict[str, Any]]:
    """Faptele bundle-ului → `list[dict]`, forma pe care `validate_prose` o consumă.

    Deliberat construită din FAPTE, nu din rândurile de retrieval: proza se validează contra a
    ceea ce am înghețat și vom afișa, nu contra a ceea ce a văzut modelul. Diferența contează
    exact în cazul în care contează cel mai mult — un preț stale, exclus din afișare, nu are voie
    să legitimeze o cifră din text."""
    out: list[dict[str, Any]] = []
    for product in bundle.products:
        price = product.fact("price")
        stock = product.fact("stock")
        rating = product.fact("rating")
        url = product.fact("url")
        availability = product.fact("availability")
        row: dict[str, Any] = {}
        if price.usable:
            row["price"] = float(price.value) if isinstance(price.value, Decimal) else price.value
        if stock.usable:
            row["stock"] = stock.value
        if rating.usable:
            row["rating"] = (
                float(rating.value) if isinstance(rating.value, Decimal) else rating.value
            )
        if url.usable:
            row["url"] = url.value
        if availability.usable:
            row["availability"] = availability.value
        list_price = product.fact("list_price")
        if list_price.usable:
            # `variants` e canalul prin care validatorul acceptă un al doilea preț legitim pentru
            # ACELAȘI produs (prețul tăiat) — fără el, „era 120 lei, acum 89" ar pica.
            row["variants"] = [
                {
                    "price": float(list_price.value)
                    if isinstance(list_price.value, Decimal)
                    else list_price.value
                }
            ]
        out.append(row)
    return out


def _grounded_percentages(bundle: EvidenceBundle) -> set[int]:
    """Procentele pe care le putem SUSȚINE: reducerile reale, recalculate din perechile
    (list_price, price) ale bundle-ului. Nimic altceva nu justifică un „%" într-un răspuns
    comercial."""
    out: set[int] = set()
    for product in bundle.products:
        price = product.fact("price")
        list_price = product.fact("list_price")
        if not price.usable or not list_price.usable:
            continue
        text = format_discount(price.value, list_price.value)
        if text:
            try:
                out.add(int(text.lstrip("-").rstrip("%")))
            except ValueError:  # pragma: no cover — format_discount produce mereu „-N%"
                continue
    return out


def _prose_of(plan: AnswerPlanV2, *, include_clarification: bool) -> str:
    """Textul de NIVEL DE RĂSPUNS care ajunge la client, într-un singur șir.

    Motivele recomandărilor NU sunt aici, deliberat: se validează individual (`reason_ok`) și se
    OMIT când pică. Un motiv slab pe al treilea card n-are voie să arunce la fallback un răspuns
    altfel corect — sancțiunea trebuie să fie la fel de locală ca greșeala.

    Clarificarea intră doar dacă se va PUNE: poarta NX-235 o poate suprima, iar validarea unui text
    care nu se livrează ar respinge răspunsuri bune pentru o întrebare pe care n-o punem."""
    parts = [plan.direct_answer, *plan.disclosures, *(c.text for c in plan.claims)]
    if include_clarification and plan.clarification is not None:
        parts.append(plan.clarification.question)
    return "\n".join(p for p in parts if p and p.strip())


def _stock_supported(bundle: EvidenceBundle) -> bool:
    """Există măcar un produs despre care putem spune „e disponibil"? Faptul, nu impresia."""
    return any(
        p.fact("availability").usable and p.fact("availability").value in ("in_stock", "low_stock")
        for p in bundle.products
    )


def prose_failures(text: str, bundle: EvidenceBundle) -> tuple[str, ...]:
    """Verdictul pe un text, contra faptelor înghețate. Folosit ȘI pentru răspuns (fatal), ȘI
    pentru fiecare motiv de recomandare (omisiune) — o singură definiție a lui „fondat".

    `check_claims=False` NU e o relaxare: NX-117 respinge orice pomenire de „reducere"/„recenzii"
    /„livrare" fiindcă pe calea de proză nu are cum să le verifice. Aici AVEM cum, iar verificarea
    e mai strictă decât interdicția: procentul se recalculează din două prețuri, stocul se cere
    dintr-un fapt de disponibilitate, livrarea/promoția/garanția se resping fiindcă nu au sursă
    deloc. Superlativul rămâne respins — nu există fapt „cel mai bun"."""
    if not text.strip():
        return ()
    failures: list[str] = []
    validation = validate_prose(
        text,
        products=bundle_products_for_validator(bundle),
        generated_links={str(p.fact("url").value) for p in bundle.products if p.fact("url").usable},
        grounded_prices=set(),
        check_claims=False,
    )
    if not validation.ok:
        failures.append("ungrounded_prose")
    grounded_pct = _grounded_percentages(bundle)
    for match in _PERCENT_RE.finditer(text):
        value = int(match.group(1))
        if value == 100:  # „100% natural" e o formulare de compoziție, nu o reducere
            continue
        if value not in grounded_pct:
            failures.append("ungrounded_percentage")
            break
    if has_superlative_claim(text):
        failures.append("unverifiable_superlative")
    if has_stock_claim(text) and not _stock_supported(bundle):
        failures.append("unsupported_stock_claim")
    failures.extend(unsourced_claims(text))
    return tuple(dict.fromkeys(failures))


# ── Guard ───────────────────────────────────────────────────────────────────────────────────
def _ground_products(
    plan: AnswerPlanV2,
    bundle: EvidenceBundle,
    *,
    commerce_enabled: bool,
    omissions: list[FieldOmission],
) -> tuple[GroundedProduct, ...]:
    """Produsele selectate de plan → produse randabile. Ordinea planului se păstrează (e decizia
    de vânzare a brain-ului); filtrarea e strict pe adevăr, niciodată pe preferință."""
    reasons = {rec.product_id: rec.reason for rec in plan.recommendations}
    out: list[GroundedProduct] = []
    for selected in plan.selected_products:
        evidence = bundle.by_id(selected.product_id)
        if evidence is None:
            omissions.append(
                FieldOmission("product", "not_in_evidence", product_id=selected.product_id)
            )
            continue
        if evidence.blocked:
            # Hard MISMATCH: produsul nu apare NICĂIERI. Nu e o degradare de card, e o
            # contrazicere de fapt — a-l arăta „cu o notă" ar fi tot o recomandare.
            omissions.append(FieldOmission("product", "blocked", product_id=evidence.product_id))
            continue
        if not evidence.fact("title").usable:
            omissions.append(FieldOmission("product", "unknown", product_id=evidence.product_id))
            continue
        raw_reason = (reasons.get(selected.product_id) or "").strip()
        reason: str | None = raw_reason or None
        if reason:
            # Motivul se judecă SINGUR, cu aceleași reguli ca răspunsul, și cade singur: un
            # superlativ („cel mai bun") sau o cifră nefondată șterge MOTIVUL, nu cardul. Cardul
            # rămâne pentru că el nu afirmă nimic în plus față de faptele lui.
            reason_failures = prose_failures(reason, bundle)
            if reason_failures:
                omissions.append(
                    FieldOmission(
                        "reason",
                        "generic_reason"
                        if "unverifiable_superlative" in reason_failures
                        else "ungrounded_reason",
                        product_id=evidence.product_id,
                    )
                )
                reason = None
        blocked_reason = evidence.sellable
        allowed = commerce_enabled and blocked_reason is None
        if not allowed:
            omissions.append(
                FieldOmission(
                    "commerce_cta",
                    "commerce_disabled" if not commerce_enabled else (blocked_reason or "unknown"),
                    product_id=evidence.product_id,
                )
            )
        out.append(
            GroundedProduct(
                evidence=evidence,
                reason=reason,
                commerce_allowed=allowed,
                commerce_blocked_reason=None
                if allowed
                else (blocked_reason or "commerce_disabled"),
            )
        )
    return tuple(out)


def _ground_comparison(
    plan: AnswerPlanV2,
    bundle: EvidenceBundle,
    *,
    allowed_ids: frozenset[str],
    omissions: list[FieldOmission],
) -> GroundedComparison | None:
    """Celulele comparației, rezolvate. O celulă rămâne `None` (necunoscut onest) când produsul
    nu e în bundle sau când axa nu are fapt — niciodată „0" și niciodată o valoare împrumutată de
    la alt produs. Sub două coloane randabile comparația dispare: un tabel cu o singură coloană
    nu compară nimic."""
    comparison = plan.comparison
    if comparison is None:
        return None
    product_ids = tuple(pid for pid in comparison.product_ids if pid in allowed_ids)
    if len(product_ids) < 2:
        omissions.append(FieldOmission("comparison", "not_in_evidence"))
        return None
    cells: dict[tuple[str, str], Any] = {}
    for cell in comparison.cells:
        if cell.product_id not in product_ids:
            continue
        evidence = bundle.by_id(cell.product_id)
        if evidence is None:
            continue
        cells[(cell.product_id, cell.axis)] = cell.value
    axes = tuple(dict.fromkeys(comparison.axes))
    for axis in axes:
        for pid in product_ids:
            if (pid, axis) not in cells:
                omissions.append(FieldOmission("comparison_cell", "unknown", product_id=pid))
    if not axes:
        omissions.append(FieldOmission("comparison", "unknown"))
        return None
    return GroundedComparison(product_ids=product_ids, axes=axes, cells=cells)


def ground_answer(
    plan: AnswerPlanV2,
    bundle: EvidenceBundle,
    *,
    locale: str,
    ask_clarification: bool = True,
    memory_criteria: tuple[str, ...] = (),
    commerce_enabled: bool = False,
) -> GroundedAnswer:
    """Planul + faptele înghețate → ce are voie să fie randat. DETERMINIST, fără I/O.

    `ask_clarification` vine de la poarta NX-235 (information gain + anti-buclă): guardul nu
    decide DACĂ întrebăm, doar verifică ce se livrează dacă întrebăm. `commerce_enabled` e
    `conversation_cart_enabled` (NX-237) — fără serviciu de coș, niciun CTA mutant nu se emite,
    chiar dacă faptele ar permite."""
    loc = normalize_locale(locale)
    failures: list[str] = []
    omissions: list[FieldOmission] = []

    if plan.business_id != bundle.business_id:
        failures.append("tenant_mismatch")
    if normalize_locale(plan.locale) != loc:
        failures.append("locale_mismatch")
    if failures:
        # Tenant/locale greșit nu se repară prin omisiuni: tot ce urmează ar fi date ale altcuiva.
        return GroundedAnswer(
            ok=False,
            locale=loc,
            business_id=bundle.business_id,
            as_of=bundle.as_of,
            failures=tuple(failures),
        )

    # Proza de nivel de răspuns: preț/link/cifre bare/medical (validatorul EXISTENT) + procente
    # recalculate + stoc cerut din fapt + livrare/promo/garanție fără sursă + superlativ.
    failures.extend(
        prose_failures(_prose_of(plan, include_clarification=ask_clarification), bundle)
    )

    products = _ground_products(
        plan, bundle, commerce_enabled=commerce_enabled, omissions=omissions
    )
    allowed_ids = frozenset(p.evidence.product_id for p in products)
    comparison = _ground_comparison(plan, bundle, allowed_ids=allowed_ids, omissions=omissions)

    # 4. Un plan care SELECTA produse și n-a rămas cu niciunul nu e „un răspuns fără carduri": e
    #    un răspuns al cărui subiect a dispărut. Textul ar rămâne vorbind despre ce nu se arată.
    if plan.selected_products and not products:
        failures.append("no_renderable_product")

    clarification = plan.clarification if ask_clarification else None
    answer = GroundedAnswer(
        ok=not failures,
        locale=loc,
        business_id=bundle.business_id,
        as_of=bundle.as_of,
        direct_answer=plan.direct_answer.strip(),
        disclosures=tuple(d.strip() for d in plan.disclosures if d.strip()),
        clarification=clarification,
        no_results=plan.no_results,
        products=products,
        comparison=comparison,
        memory_criteria=memory_criteria,
        cart=bundle.cart,
        failures=tuple(dict.fromkeys(failures)),
        omissions=tuple(omissions),
    )
    if answer.ok and not answer.has_content:
        return GroundedAnswer(
            ok=False,
            locale=loc,
            business_id=bundle.business_id,
            as_of=bundle.as_of,
            failures=("empty_answer",),
            omissions=tuple(omissions),
        )
    return answer


# ── Serializare: ce se ÎNGHEAȚĂ în rândul terminal ──────────────────────────────────────────
# Se persistă REZULTATUL grounding-ului, nu intrarea lui. Diferența e replay-ul: dacă am persista
# planul + bundle-ul și am re-rula guardul la fiecare citire, un kill-switch rotit între timp
# (`validator_claims_enabled`, de pildă) ar putea transforma un răspuns deja livrat într-un eșec.
# Verdictul e o decizie luată o dată, la commit, în tranzacția terminală — după aceea, proiecția
# e pură pe date înghețate și nu mai are nimic de decis.
GROUNDED_PAYLOAD_KEY: Final[str] = "grounded_v2"
GROUNDED_SCHEMA_VERSION: Final[int] = 1


def answer_to_jsonb(answer: GroundedAnswer) -> dict[str, Any]:
    """`GroundedAnswer` → payload persistabil. Numai produsele SUPRAVIEȚUITOARE intră (bundle-ul
    complet a fost deja folosit pentru validare și telemetrie); ce n-a trecut poarta nu se
    persistă, ca să nu poată fi „recuperat" de o proiecție mai indulgentă."""
    payload: dict[str, Any] = {
        "schema_version": GROUNDED_SCHEMA_VERSION,
        "business_id": answer.business_id,
        "locale": answer.locale,
        "as_of": answer.as_of.isoformat() if answer.as_of is not None else "",
        "direct_answer": answer.direct_answer,
        "disclosures": list(answer.disclosures),
        "memory_criteria": list(answer.memory_criteria),
        "products": [
            {
                "e": p.evidence.to_jsonb(),
                "reason": p.reason,
                "commerce": p.commerce_allowed,
            }
            for p in answer.products
        ],
    }
    if answer.clarification is not None:
        payload["clarification"] = {
            "question": answer.clarification.question,
            "target_need": answer.clarification.target_need,
            "reason": answer.clarification.reason,
            "options": list(answer.clarification.options),
        }
    if answer.no_results is not None:
        payload["no_results"] = {
            "reason_class": answer.no_results.reason_class,
            "criteria": list(answer.no_results.criteria),
            "alternatives": list(answer.no_results.alternatives),
        }
    if answer.comparison is not None:
        payload["comparison"] = {
            "product_ids": list(answer.comparison.product_ids),
            "axes": list(answer.comparison.axes),
            "cells": [
                [product_id, axis, value]
                for (product_id, axis), value in answer.comparison.cells.items()
            ],
        }
    if answer.cart is not None:
        payload["cart"] = answer.cart.to_jsonb()
    return payload


def _parse_as_of(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def answer_from_jsonb(raw: Any) -> GroundedAnswer | None:
    """Inversul, DEFENSIV. `None` pe orice formă necunoscută → apelantul cade pe proiecția
    precedentă (NX-233), nu pe un 500."""
    from src.agent.evidence_bundle import CartEvidence, ProductEvidence  # noqa: PLC0415 — ciclu

    if not isinstance(raw, Mapping) or raw.get("schema_version") != GROUNDED_SCHEMA_VERSION:
        return None
    business_id = raw.get("business_id")
    if not isinstance(business_id, str) or not business_id:
        return None
    products: list[GroundedProduct] = []
    for item in raw.get("products") or []:
        if not isinstance(item, Mapping):
            continue
        evidence = ProductEvidence.from_jsonb(item.get("e"))
        if evidence is None:
            continue
        reason = item.get("reason")
        products.append(
            GroundedProduct(
                evidence=evidence,
                reason=str(reason) if isinstance(reason, str) and reason.strip() else None,
                commerce_allowed=bool(item.get("commerce")),
            )
        )
    clarification = None
    raw_clarification = raw.get("clarification")
    if isinstance(raw_clarification, Mapping):
        try:
            clarification = PlanClarification(
                question=str(raw_clarification.get("question") or ""),
                target_need=str(raw_clarification.get("target_need") or ""),
                reason=str(raw_clarification.get("reason") or ""),
                options=tuple(
                    str(o) for o in (raw_clarification.get("options") or []) if isinstance(o, str)
                ),
            )
        except (TypeError, ValueError):
            clarification = None
    no_results = None
    raw_no_results = raw.get("no_results")
    if isinstance(raw_no_results, Mapping):
        try:
            no_results = PlanNoResults(
                reason_class=raw_no_results.get("reason_class"),
                criteria=tuple(str(c) for c in (raw_no_results.get("criteria") or [])),
                alternatives=tuple(str(a) for a in (raw_no_results.get("alternatives") or [])),
            )
        except (TypeError, ValueError):
            no_results = None
    comparison = None
    raw_comparison = raw.get("comparison")
    if isinstance(raw_comparison, Mapping):
        cells: dict[tuple[str, str], Any] = {}
        for entry in raw_comparison.get("cells") or []:
            if isinstance(entry, (list, tuple)) and len(entry) == 3:
                cells[(str(entry[0]), str(entry[1]))] = entry[2]
        product_ids = tuple(
            str(p) for p in (raw_comparison.get("product_ids") or []) if isinstance(p, str)
        )
        axes = tuple(str(a) for a in (raw_comparison.get("axes") or []) if isinstance(a, str))
        if len(product_ids) >= 2 and axes:
            comparison = GroundedComparison(product_ids=product_ids, axes=axes, cells=cells)
    as_of = raw.get("as_of")
    return GroundedAnswer(
        ok=True,
        locale=normalize_locale(raw.get("locale")),
        business_id=business_id,
        as_of=_parse_as_of(as_of),
        direct_answer=str(raw.get("direct_answer") or ""),
        disclosures=tuple(str(d) for d in (raw.get("disclosures") or []) if isinstance(d, str)),
        clarification=clarification,
        no_results=no_results,
        products=tuple(products),
        comparison=comparison,
        memory_criteria=tuple(
            str(c) for c in (raw.get("memory_criteria") or []) if isinstance(c, str)
        ),
        cart=CartEvidence.from_jsonb(raw.get("cart")),
    )


def omission_counts(answer: GroundedAnswer) -> dict[str, int]:
    """Proiecție PII-safe pentru telemetrie: `field:reason → count`. Fără id-uri de produs —
    cardinalitatea unei metrici nu are voie să crească cu catalogul."""
    counts: dict[str, int] = {}
    for omission in answer.omissions:
        key = f"{omission.field}:{omission.reason}"
        counts[key] = counts.get(key, 0) + 1
    return counts


__all__ = [
    "GROUNDED_PAYLOAD_KEY",
    "GROUNDED_SCHEMA_VERSION",
    "FieldOmission",
    "GroundedAnswer",
    "GroundedComparison",
    "GroundedProduct",
    "answer_from_jsonb",
    "answer_to_jsonb",
    "bundle_products_for_validator",
    "ground_answer",
    "prose_failures",
    "omission_counts",
    "unsourced_claims",
]
