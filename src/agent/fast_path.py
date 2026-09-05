"""NX-275 felia 7 (D2) — FAST PATH EXACT: fapte pe un produs identificat, fără niciun apel de model.

E singura felie din card care poate răspunde **greșit** fără ca vreo poartă din aval s-o prindă.
Validatorul (stagiul 8) și `grounding_guard` (NX-240) sunt porți de ADEVĂR: verifică dacă prețul
rostit există în evidence. Aici prețul VA exista în evidence, fiindcă vine direct din catalog. Ce
nu verifică nimeni e dacă e prețul produsului DESPRE CARE a întrebat clientul. O ancorare greșită
produce un răspuns corect despre alt produs, iar asta trece de tot ce am construit.

De aceea regula nu e „răspunde repede când poți", ci **„răspunde doar când nu există dubiu"**:

  • clasa turului e `EXACT` ȘI există exact O obligație, de tip `answer`. Un mesaj mixt merge la
    creier, oricât de simplă ar părea prima jumătate;
  • referința e rezolvată de `reference_resolver` cu o sursă ANCORATĂ (pagina pe care e clientul,
    un produs afișat, un ordinal). Nu căutăm în catalog după nume: o potrivire aproximativă e
    exact modul în care fast path-ul ar răspunde despre alt produs;
  • faptul cerut e detectat DETERMINIST și e unul singur (preț, stoc sau link);
  • faptul e CUNOSCUT și PROASPĂT. `unknown` sau `stale` (NX-240) ⇒ creier, fiindcă „nu știu" și
    „știam acum trei zile" sunt răspunsuri care cer nuanță, nu un șablon.

Textul se compune din `src/web/localization` — niciun număr formatat de mână, nicio monedă
concatenată în cod (P11). Textul iese prin `ctx.set_reply`, deci trece prin `naturalize` (P13) ca
orice alt răspuns.

Câștigul: pe clasa lui, turul costă **zero** apeluri de model, față de 1-2 azi. Restul feliilor
NX-275 taie 30-50% din cost; asta taie 100% — dar numai unde nu poate greși.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from src.agent.reference_resolver import PageAnchor, resolve_product_reference
from src.catalog.freshness import facts_sla_s
from src.commerce.facts_provider import load_facts
from src.config import get_settings
from src.web.localization import copy_for, format_availability, format_money

if TYPE_CHECKING:
    from src.models import TurnContext
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)

FactKind = Literal["price", "stock", "link"]

#: Ce fapt cere mesajul. Tipare de FORMĂ (cât costă / e pe stoc / unde îl găsesc), nu vocabular de
#: produs: aceleași întrebări se pun despre un frigider și despre o cremă. Fără diacritice, ca tot
#: lanțul determinist.
_FACT_PATTERNS: tuple[tuple[FactKind, re.Pattern[str]], ...] = (
    (
        "price",
        re.compile(
            r"\bcat\s+cost\w*\b|\bce\s+pret\b|\bcare\s+e\s+pretul\b|\bpretul\b"
            r"|\bhow\s+much\b|\bwhat\s+price\b|\bprice\b",
            re.IGNORECASE,
        ),
    ),
    (
        "stock",
        re.compile(
            r"\b(?:mai\s+)?(?:aveti|ai)\s+(?:pe\s+)?stoc\b|\be\s+(?:pe\s+)?stoc\b"
            r"|\bin\s+stoc\b|\bdisponibil\w*\b|\bin\s+stock\b|\bavailable\b",
            re.IGNORECASE,
        ),
    ),
    (
        "link",
        re.compile(
            r"\blink\w*\b|\bunde\s+(?:il|o|le)\s+gasesc\b|\bda-?mi\s+adresa\b|\burl\b",
            re.IGNORECASE,
        ),
    ),
)

#: Sursele de referință pe care le acceptăm. `page` = clientul E pe pagina produsului; `ordinal` și
#: `named` = a arătat spre ceva din lista afișată în turul anterior. TOATE sunt ancorate în ceva ce
#: serverul a arătat sau știe. Lipsesc deliberat `single` (un singur produs afișat NU înseamnă că
#: despre el întreabă) și orice sursă derivată dintr-o căutare făcută acum.
_TRUSTED_SOURCES: frozenset[str] = frozenset({"page", "ordinal", "named", "action"})

#: Câți termeni de conținut trebuie să aibă mesajul ca să merite o căutare de nume în catalog.
#:
#: Nu e o euristică de calitate, e una de COST. Interogarea de ancorare e o scanare (indexurile
#: sunt inerte pe conexiunea de runtime sub RLS) și costă 130-235ms măsurat pe catalogul SOLE.
#: Pe un „cât costă?" fără nume am plăti-o degeaba, apoi tot am merge la creier. Numele distinctiv
#: are în medie 39 de caractere, deci un mesaj care chiar conține unul are cel puțin 4 termeni de
#: conținut („cât costă RIEMANN P20 Original SPF" are 7); unul care nu are, are 1-3.
_MIN_TERMS_FOR_NAME_LOOKUP = 4


@dataclass(frozen=True, slots=True)
class FastPathOutcome:
    """De ce s-a răspuns sau nu. `reason` e vocabular ÎNCHIS (etichetă de telemetrie)."""

    served: bool
    reason: str
    fact: FactKind | None = None


def wanted_fact(query: str) -> FactKind | None:
    """Faptul cerut, sau None dacă mesajul cere altceva (sau mai multe lucruri).

    Ambiguitatea NU se rezolvă prin prioritate: dacă mesajul potrivește două tipare („cât costă și
    aveți pe stoc?"), întoarcem None și lăsăm creierul să acopere ambele. Un fast path care alege
    unul din două ar răspunde pe jumătate, iar jumătatea lipsă n-ar apărea nicăieri."""
    hits = [kind for kind, pattern in _FACT_PATTERNS if pattern.search(query or "")]
    return hits[0] if len(hits) == 1 else None


def _single_answer_obligation(obligations: Any) -> bool:
    items = list(obligations or ())
    return len(items) == 1 and str(getattr(items[0], "kind", "")) == "answer"


def eligible(ctx: TurnContext, query: str) -> tuple[str | None, FactKind | None, str | None]:
    """`(motiv_de_refuz, faptul_cerut, product_id)`. PURĂ: zero I/O.

    Ordinea verificărilor e de la cea mai ieftină la cea mai scumpă, dar asta e un detaliu; ce
    contează e că fiecare refuz are un NUME. Un fast path care tace fără motiv face imposibil de
    spus dacă e stins, nefolosit sau stricat."""
    from src.agent.brain_models import obligations_from_ctx  # noqa: PLC0415 — evită ciclu
    from src.runtime.turn_budget import TurnClass, turn_class_for  # noqa: PLC0415

    if getattr(ctx, "action", None) is not None:
        return "action", None, None
    obligations = obligations_from_ctx(ctx)
    if turn_class_for(obligations) is not TurnClass.EXACT:
        return "not_exact", None, None
    if not _single_answer_obligation(obligations):
        return "mixed", None, None
    fact = wanted_fact(query)
    if fact is None:
        return "no_single_fact", None, None

    page = getattr(ctx, "page_context", None)
    anchor = PageAnchor(product_id=page.product_id) if getattr(page, "product_id", None) else None
    displayed = list(getattr(ctx.state, "displayed_products", None) or [])
    resolution = resolve_product_reference(query, displayed, page=anchor)
    if not resolution.resolved or resolution.stale:
        return "no_anchor", fact, None
    if resolution.source not in _TRUSTED_SOURCES:
        # `single` și celelalte surse slabe: produsul e plauzibil, nu sigur. Plauzibil nu ajunge
        # pentru o cale fără poartă în aval.
        return "weak_anchor", fact, None
    return None, fact, resolution.product_id


def name_lookup_worth_it(query: str, locale: str) -> bool:
    """Merită să plătim scanarea de nume? Decizie de COST, luată fără să atingem DB-ul."""
    from src.catalog.query_terms import content_terms  # noqa: PLC0415 — cale rece

    return len(content_terms(query or "", locale)) >= _MIN_TERMS_FOR_NAME_LOOKUP


def _compose(fact: FactKind, facts: Any, locale: str) -> str | None:
    """Textul, din tabelul de copy. None = faptul nu se poate rosti (necunoscut/stale) ⇒ creier."""
    template = copy_for(locale)["fast_path"][fact]
    name = (facts.name or "").strip()
    if not name:
        return None
    if fact == "price":
        if not facts.price_known or "price" in facts.unknown:
            return None
        # `format_money`, nu `amount_text`: moneda vine din FAPTELE tenantului, nu se presupune.
        # Un „lei" concatenat in cod ar fi corect pe pilot si gresit pe primul client din alta tara,
        # fara ca nimic sa semnaleze (P11).
        amount = format_money(facts.price, facts.currency, locale)
        return template.format(name=name, amount=amount) if amount else None
    if fact == "stock":
        text = format_availability(facts.availability, facts.stock, locale)
        return template.format(name=name, availability=text) if text else None
    url = (facts.raw or {}).get("product_url")
    return template.format(name=name, url=url) if url else None


async def _anchor_by_name(
    ctx: TurnContext, deps: PipelineDeps, query: str
) -> tuple[str | None, str]:
    """Ancorare prin numele DISTINCTIV al produsului, rostit de client. `(product_id, motiv)`.

    E singura ancoră care nu vine din ceva ce serverul a arătat, deci are cele mai strânse condiții:
    conținere EXACTĂ a numelui în mesaj (nu similaritate) plus unicitatea potrivirii maximale. Pe
    catalogul SOLE asta refuză familiile de nuanțe (35 de variante `TIRTIR Mask Fit Red Cushion`) și
    variantele de culoare — exact cazurile în care un răspuns ar fi o ghicire cu preț real.
    """
    from src.db.queries.catalog import find_product_named_in_query  # noqa: PLC0415 — cale rece

    try:
        async with deps.db("fast_path_anchor") as conn:
            return await find_product_named_in_query(conn, ctx.business.id, query)
    except Exception as e:  # noqa: BLE001 — ancorarea eșuată = turul merge la creier (P6)
        log.warning("fast_path: ancorarea prin nume a eșuat (%s)", type(e).__name__)
        return None, "anchor_unavailable"


async def try_fast_path(ctx: TurnContext, deps: PipelineDeps) -> FastPathOutcome:
    """Răspunde la o întrebare de fapt fără niciun apel de model, sau lasă turul creierului.

    Întoarce ÎNTOTDEAUNA un `FastPathOutcome` cu motiv, și emite `fast_path{outcome,reason}`:
    câștigul feliei se măsoară în ture servite, iar refuzurile spun de ce nu.
    """
    settings = get_settings()
    if not getattr(settings, "fast_path_exact_enabled", False):
        return FastPathOutcome(False, "flag_off")

    query = (ctx.message.body or "").strip()
    reason, fact, product_id = eligible(ctx, query)
    if fact is not None and product_id is None and reason in ("no_anchor", "weak_anchor"):
        # Ancorele ARĂTATE de server (pagină, ordinal, nume din lista afișată) au întâietate. Abia
        # când niciuna nu se aplică încercăm numele din catalog — și doar dacă mesajul e destul de
        # lung ca să conțină unul, fiindcă interogarea e o scanare, nu un lookup.
        if name_lookup_worth_it(query, ctx.language):
            product_id, name_reason = await _anchor_by_name(ctx, deps, query)
            reason = None if product_id else name_reason
        else:
            reason = "name_lookup_skipped"
    if reason is not None or fact is None or product_id is None:
        ctx.emit("fast_path", outcome="skipped", reason=reason or "no_anchor")
        return FastPathOutcome(False, reason or "no_anchor", fact)

    sla = facts_sla_s(
        getattr(ctx.business, "settings", None), default=settings.commerce_facts_sla_s
    )
    try:
        async with deps.db("fast_path_facts") as conn:
            batch = await load_facts(conn, ctx.business.id, [(product_id, None)], sla_s=sla)
    except Exception as e:  # noqa: BLE001 — o scurtătură nu are voie să rupă turul (P6)
        log.warning("fast_path: hidratarea a eșuat (%s) — turul merge la creier", type(e).__name__)
        ctx.emit("fast_path", outcome="skipped", reason="facts_unavailable")
        return FastPathOutcome(False, "facts_unavailable", fact)

    facts = batch.get(product_id, None)
    if facts is None:
        ctx.emit("fast_path", outcome="skipped", reason="facts_missing")
        return FastPathOutcome(False, "facts_missing", fact)
    if getattr(facts.freshness, "stale", True):
        # „Știam acum trei zile" e un răspuns care cere nuanță (NX-240), nu un șablon.
        ctx.emit("fast_path", outcome="skipped", reason="stale")
        return FastPathOutcome(False, "stale", fact)

    # NX-173 (P0): e o cale care SERVEȘTE un produs, deci trece prin aceeași poartă ca oricare alta.
    from src.safety.policy import SafetyPolicy  # noqa: PLC0415 — evită ciclu la import

    row = batch.product(product_id)
    kept, _ = SafetyPolicy.for_turn(ctx).gate(ctx, [dict(row)] if row else [], purpose="fast_path")
    if not kept:
        ctx.emit("fast_path", outcome="skipped", reason="safety")
        return FastPathOutcome(False, "safety", fact)

    text = _compose(fact, facts, ctx.language)
    if not text:
        ctx.emit("fast_path", outcome="skipped", reason="fact_unknown")
        return FastPathOutcome(False, "fact_unknown", fact)

    # `cacheable=False`: răspunsul e legat de o ancoră a ACESTEI conversații (produsul de pe pagină
    # sau din lista afișată). Cache-uit, ar fi servit altui client care a pus aceeași întrebare
    # despre alt produs — exact clasa de bug din [[cache-poisoning-no-result]].
    ctx.set_reply(text, cacheable=False)
    ctx.emit("fast_path", outcome="served", reason=fact)
    return FastPathOutcome(True, "served", fact)
