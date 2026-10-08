"""NX-381 — întrebarea clientului despre un produs primește un RĂSPUNS, nu fișa standard.

Setul `wide-2026-10-07`: 72 de ture `detail`, 24% corecte. «Are SPF?», «e rezistent la apă?», «îl
pot folosi cu vitamina C?» primeau aceeași fișă fixă (`deterministic.serve_details`), fiindcă
întrebarea n-avea unde să ajungă. De la `kernel.v6.3` (NX-380) modelul de interpretare o scrie în
`Act.question`, iar plannerul o duce în `TurnPlan.question`. Aici o primește compunerea.

Trei piese, fiecare cu un singur proprietar:

- **Faptele** (`product_facts`, PUR): exact ce știe catalogul despre produs, din ACEEAȘI citire pe
  care o servește fișa (`get_products_by_ids`): secțiunile din `detail_sections`, cu plafonul și
  poarta medicală a lui `_section_text`; fațetele întregi; ingredientele cheie; întrebările
  frecvente ale produsului (`product_faqs`, sursa care răspunde des la «merge cu X?»); preț, preț de
  listă, voucher, disponibilitate, rating, avantaje și minusuri din recenzii. Nimic din afara ei.
- **Răspunsul**: UN apel de model (`llm.complete`, plafonat), instrucțiuni generice (P11: limba e
  numită, nu presupusă), 1-3 propoziții, întâi răspunsul. Ce fișa nu spune se spune ca atare.
- **Poarta** (`check_answer`, PURĂ): o poartă de ADEVĂR legată de fișă, nu lista de cuvinte a
  validatorului de proză (`_CLAIMY` respinge orice text cu „recenzii", iar `_PCT` orice procent,
  deci un răspuns corect din fișă, «are 0,1% retinol», ar fi pierit). Respinge: claim medical
  (P0), un link, un preț care nu e al produsului, o cifră absentă din fapte, o cifră pusă lângă o
  UNITATE (procent sau o unitate a pachetului) cu care faptele n-o poartă («SPF 50» când fișa are
  doar „50 ml", «5%» când 5 e nota din recenzii), și o afirmație de stoc pe un produs epuizat.

Orice eșec întoarce un `DetailAnswer` fără text, iar executorul servește fișa de azi (P6). Pe un
claim medical, fișa vine cu fraza de trimitere la medic sau farmacist înainte (`refer_sentence`):
întrebările despre sarcină sau alergii sunt exact cele pe care modelul le formulează negat, iar
poarta medicală le respinge și negate."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.agent.voice import VOICE_RULES
from src.catalog.folding import fold_text
from src.catalog.render_text import cut_at_sentence, display_name
from src.web.localization import amount_text
from src.worker import compose

if TYPE_CHECKING:
    from src.models import TurnContext
    from src.worker.runner import PipelineDeps

#: Câte avantaje / dezavantaje din recenzii intră în fapte.
_REVIEW_POINTS = 3
#: Câte întrebări frecvente ale produsului și cât din fiecare răspuns.
_FAQS = 6
_FAQ_ANSWER_CHARS = 300
#: Plafonul apelului: fișa de azi e gata imediat, deci un răspuns care întârzie nu merită așteptat.
ANSWER_TIMEOUT_S = 15.0
#: Motivele porții și ale ieșirii, vocabular ÎNCHIS (evenimentul `detail_question{outcome}`).
REASONS = (
    "medical_claim",
    "invented_link",
    "ungrounded_price",
    "ungrounded_number",
    "stock_claim",
    "empty",
)

_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_WORD_BEFORE = re.compile(r"([^\W\d_]+)[^\w%]*$")
_WORD_AFTER = re.compile(r"^\s*(%|[^\W\d_]+)")

SYSTEM = (
    "You answer ONE question a customer asked about ONE product of an online store. You use ONLY "
    "the product facts given in the user message: the catalog is the only source of truth.\n"
    'Write in locale "{locale}", addressing the customer directly, in one to three short '
    "sentences. Answer the question in the first sentence. When the facts do not answer it, say "
    "plainly that the product page does not say, then give the closest fact it does state.\n"
    "Never add a fact, a number, a price, a link, a stock or delivery promise that is not in the "
    "facts. The ingredients given are the KEY ingredients, not the full composition: never say "
    "that the product does not contain something.\n"
    "Do not answer a health, allergy or pregnancy question and never say that the product treats, "
    "cures or is safe for a condition: write only that a doctor or a pharmacist should confirm.\n"
    "Do not describe the product again: the customer already sees its card. No lists, no headings."
)


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str | None = None


@dataclass(frozen=True)
class DetailAnswer:
    """Ieșirea: `answer` validat, sau `None` cu motivul (`reason` din `REASONS`, `call_failed`)."""

    answer: str | None
    reason: str | None = None


def _sections(product: dict[str, Any], pack: Any) -> list[tuple[str, str]]:
    """Secțiunile fișei, în ordinea pachetului (`detail_sections`), cu plafonul și poarta medicală
    ale vederii de detaliu (`deterministic._section_text`, un singur proprietar). Fără declarație
    în pachet, felurile prezente pe produs, sortate, cu plafonul implicit."""
    from src.agent.deterministic import _section_text  # noqa: PLC0415 — ciclul deterministic

    specs = tuple(getattr(pack, "detail_sections", ()) or ())
    kinds = [s.kind for s in specs] or sorted(
        {str(s.get("kind")) for s in product.get("sections") or [] if isinstance(s, dict)}
    )
    out: list[tuple[str, str]] = []
    for kind in kinds:
        text = _section_text(product, pack, (kind,))
        if text:
            out.append((kind, text))
    return out


def _money(value: Any, language: str | None, currency: str) -> str:
    return f"{amount_text(value, language)} {currency}".rstrip()


def product_facts(product: dict[str, Any], pack: Any, language: str | None) -> str:
    """Faptele produsului ca text, ca rânduri `cheie: valoare`. PUR."""
    currency = str(getattr(pack, "currency", None) or product.get("currency") or "")
    name = str(product.get("name") or "")
    lines = [f"name: {display_name(name)}"]
    if name and name != display_name(name):
        lines.append(f"full name: {name}")
    if product.get("price") is not None:
        lines.append(f"price: {_money(product['price'], language, currency)}")
    if product.get("list_price") is not None:
        before = _money(product["list_price"], language, currency)
        lines.append(f"list price before discount: {before}")
    if product.get("coupon_price") is not None and product.get("coupon_code"):
        lines.append(
            f"price with voucher {product['coupon_code']}: "
            f"{_money(product['coupon_price'], language, currency)}"
        )
    if product.get("availability"):
        lines.append(f"availability: {product['availability']}")
    facets = tuple(getattr(pack, "comparison_facets", ()) or ()) if pack is not None else ()
    summary = compose.facet_summary(product, facets, language, whole=True) if facets else ""
    if summary:
        lines.append(f"attributes: {summary}")
    key = [str(i).strip() for i in product.get("ingredients_db") or [] if str(i).strip()]
    if key:
        lines.append("key ingredients: " + ", ".join(key))
    for kind, text in _sections(product, pack):
        lines.append(f"{kind}: {text}")
    for faq in (product.get("faqs") or [])[:_FAQS]:
        if isinstance(faq, dict) and faq.get("question") and faq.get("answer"):
            answer = cut_at_sentence(" ".join(str(faq["answer"]).split()), _FAQ_ANSWER_CHARS)
            lines.append(f"faq: {' '.join(str(faq['question']).split())} -> {answer}")
    if product.get("rating"):
        count = product.get("review_count")
        reviews = f" from {count} reviews" if count else ""
        lines.append(f"rating: {float(product['rating']):.1f}/5{reviews}")
    for field, label in (("top_pros", "reviews praise"), ("top_cons", "reviews criticise")):
        points = [str(p).strip() for p in product.get(field) or [] if str(p).strip()]
        if points:
            lines.append(f"{label}: " + "; ".join(points[:_REVIEW_POINTS]))
    return "\n".join(lines)


def user_message(question: str, facts: str, history: str) -> str:
    """Mesajul `user`: istoricul (pentru context, nu pentru fapte), faptele, apoi întrebarea."""
    parts = []
    if history:
        parts.append(f"CONVERSATION SO FAR (context only, not a source of facts)\n{history}")
    parts.append(f"PRODUCT FACTS\n{facts}")
    parts.append(f"CUSTOMER QUESTION\n{question}")
    return "\n\n".join(parts)


def unit_words(pack: Any) -> frozenset[str]:
    """Cuvintele de UNITATE ale pachetului (aliasurile din `units`, pliate) plus procentul. Sunt
    date de pachet, nu o listă din cod (P11)."""
    specs = getattr(getattr(pack, "units", None), "specs", None) or {}
    words = {fold_text(str(alias)) for spec in specs.values() for alias in spec.factors}
    return frozenset({"%", *(w for w in words if w)})


def _pairs(text: str, units: frozenset[str]) -> list[tuple[float, frozenset[str]]]:
    """Fiecare cifră din text, cu unitățile lipite de ea (cuvântul dinainte sau de după, când e o
    unitate a pachetului, sau `%`)."""
    out: list[tuple[float, frozenset[str]]] = []
    for m in _NUMBER.finditer(text or ""):
        try:
            value = round(float(m.group().replace(",", ".")), 4)
        except ValueError:
            continue
        near: set[str] = set()
        before = _WORD_BEFORE.search(text[: m.start()])
        after = _WORD_AFTER.search(text[m.end() :])
        for hit in (before, after):
            word = fold_text(hit.group(1)) if hit else ""
            if word in units:
                near.add(word)
        out.append((value, frozenset(near)))
    return out


def _numbers_grounded(answer: str, facts: str, units: frozenset[str]) -> bool:
    """O cifră a răspunsului trebuie să existe în fapte, iar o unitate pusă lângă ea în răspuns
    trebuie să stea lângă ACEEAȘI cifră și în fapte. Sumele de bani le judecă `_prices_ok`."""
    from src.agent.validator import _PRICE_RE  # noqa: PLC0415 — ciclul validator ↔ agent

    told = _pairs(_PRICE_RE.sub(" ", answer), units)
    known: dict[float, set[str]] = {}
    for value, near in _pairs(facts, units):
        known.setdefault(value, set()).update(near)
    return all(value in known and near <= known[value] for value, near in told)


def check_answer(
    answer: str,
    product: dict[str, Any] | list[dict[str, Any]],
    facts: str,
    units: frozenset[str] = frozenset({"%"}),
) -> Verdict:
    """Poarta de adevăr a răspunsului, legată de fișă. PURĂ (în afara flagurilor citite de
    porțile refolosite din `validator`). `product` = produsul răspunsului sau produsele unui text
    care le numește pe mai multe (NX-382): prețul și stocul se judecă pe oricare dintre ele."""
    products = list(product) if isinstance(product, list) else [product]
    from src.agent.validator import (  # noqa: PLC0415 — ciclul validator ↔ agent
        _links_ok,
        _prices_ok,
        _safety_ok,
        _stock_claim_ok,
    )

    text = (answer or "").strip()
    if not text:
        return Verdict(False, "empty")
    if not _safety_ok(text):
        return Verdict(False, "medical_claim")
    if not _links_ok(text, [], None):
        return Verdict(False, "invented_link")
    # NX-382 (recenzia): prețul de listă și prețul cu voucher sunt în fapte, deci și în răspuns
    extra = {
        float(p[k])
        for p in products
        for k in ("list_price", "coupon_price")
        if isinstance(p.get(k), int | float) and not isinstance(p.get(k), bool)
    }
    if not _prices_ok(text, products, extra):
        return Verdict(False, "ungrounded_price")
    if not _numbers_grounded(text, facts, units):
        return Verdict(False, "ungrounded_number")
    if not _stock_claim_ok(text, products):
        return Verdict(False, "stock_claim")
    return Verdict(True)


async def answer_question(
    ctx: TurnContext,
    deps: PipelineDeps,
    product: dict[str, Any],
    question: str,
    history: str,
) -> DetailAnswer:
    """Răspunsul la întrebare, validat pe fișă. Emite `detail_question{outcome}` (vocabular
    închis, zero text de client)."""
    pack = getattr(ctx.business, "domain_pack", None)
    facts = product_facts(product, pack, ctx.language)
    system = SYSTEM.format(locale=ctx.language or "") + "\n" + VOICE_RULES
    try:
        answer = await asyncio.wait_for(
            deps.llm.complete(system, user_message(question, facts, history)),
            timeout=ANSWER_TIMEOUT_S,
        )
    except Exception as e:  # noqa: BLE001 — P6: fișa de azi rămâne răspunsul
        ctx.emit("detail_question", outcome="call_failed", error=type(e).__name__)
        return DetailAnswer(None, "call_failed")
    verdict = check_answer(answer, product, facts, unit_words(pack))
    if not verdict.ok:
        ctx.emit("detail_question", outcome="rejected", reason=verdict.reason)
        return DetailAnswer(None, verdict.reason)
    ctx.emit("detail_question", outcome="answered")
    return DetailAnswer(answer.strip())


__all__ = [
    "ANSWER_TIMEOUT_S",
    "REASONS",
    "SYSTEM",
    "DetailAnswer",
    "Verdict",
    "answer_question",
    "check_answer",
    "product_facts",
    "unit_words",
    "user_message",
]
