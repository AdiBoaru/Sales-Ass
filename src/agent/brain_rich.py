"""Răspuns BOGAT derivat din fapte, fără al doilea apel de model. Doi apelanți, un singur cod.

Modulul s-a născut pentru creierul unic (`rich_from_plan`), dar regula pe care o implementează nu e
a lui: **un card bogat nu are nevoie de un model ca să existe**. Motivul, ratingul, badge-ul,
variantele, gramajul și încadrarea vin din catalog, nu din proză. De-aia stă aici și
`rich_from_facts`, folosit de calea v1 când apelul rich cade (NX-302) — dacă ar fi fost scris
separat, ar fi divergit din prima săptămână de la pragurile de badge, monedă și `details`.

**Defectul care a cerut `rich_from_plan`.** `finalize.py` (v1) chema `set_rich_reply`; `brain.py`
(NX-239) cheamă doar `set_reply(text, products=...)`. Diferența nu e de conținut, e de CONTRACT:
`channels/web/render.py` are trei ramuri, iar ramura săracă (`reply.products`) nu poartă `reason`,
`rating`, `review_count`, `badge`/`badge_tone`, `list_price`, `currency`, `details` și nici chips.
Aprinderea creierului unic a mutat fiecare recomandare pe ramura săracă, fără ca vreun test să pice:
cardurile erau REALE, doar goale. Nimic din aval nu putea prinde asta, din același motiv ca la
NX-293 și la garda off-category — validatorul (stagiul 8) și `grounding_guard` sunt porți de
ADEVĂR, nu de FORMĂ. Un card corect și sărac trece prin ele exact ca unul corect și bogat.

Al doilea defect, tăcut și mai grav: `render._card` citește `product_id`, iar rândurile de
retrieval poartă cheia `id`. Pe ramura bogată nu se vedea (RichItem are `product_id` explicit), pe
cea săracă rândurile plecau brute — deci cardurile ajungeau la widget cu identitate `null`. Butonul
de coș, acțiunile NX-236 și „spune-mi mai multe" n-aveau pe ce se lega. Starea conversației a
scăpat din întâmplare: `processor._displayed_product_refs` are fallback pe `id`, randorul nu.

**Reparația nu e un al doilea apel de model.** Asta ar reintroduce exact relay-ul pe care D1 îl
interzice (brain = UN SINGUR writer semantic), și ar plăti încă o rundă pe drumul sincron. Planul
are deja tot ce-i trebuia modelului rich: `recommendations[].reason` E motivul per produs, iar el
nu e text liber — trece prin `to_v1()` ca orice claim de tip `recommendation`, deci prin validatorul
de evidence, prin hard constraints și prin `grounding_guard`. Un motiv nefondat nu ajunge aici.

Deci cardurile se DERIVĂ, iar hidratarea faptelor rămâne `compose.assemble` — ACELAȘI cod ca pe v1,
nu o a doua implementare întreținută în paralel. Paritatea e prin construcție, tiparul lui NX-238
(`CurrentLiveRetrievalAdapter` APELEAZĂ `search_products_tool`, nu-l rescrie): dacă mâine se schimbă
pragurile de badge, moneda, `details` sau selectorul de variante, ambele căi se schimbă odată,
fiindcă e un singur loc. Un port scris de mână ar fi divergent din prima săptămână.

**Ce NU derivăm, declarat.** `education` (paragraful „cum alegi") n-are corespondent în plan și
rămâne gol: a-l inventa aici ar însemna un al doilea writer semantic, exact ce am evitat mai sus.
`pick` rămâne stins prin decizie de produs, nu prin lipsă de date (vezi `rich_from_plan`).
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from src.agent.fallbacks import _card_variants
from src.catalog.render_text import display_name, size_label
from src.worker import compose

if TYPE_CHECKING:
    from collections.abc import Sequence

    from src.agent.answer_plan import AnswerPlanV2
    from src.models import RichReply, TurnContext


def _data_reason(product: dict[str, Any] | None) -> str | None:
    """Motivul pe care îl poartă DATELE: `best_for` (NX-169), de pe rând sau din `attributes`.

    Există fiindcă un plan poate SELECTA un produs fără să-l recomande — pe un tur de întrebare
    („aveți fondul în nuanța ivory?") obligația e `answer`, nu `recommend`, deci
    `recommendations` e gol pe bună dreptate. Pe v1 problema nu apărea: modelul rich scria oricum
    o clauză per card, deci invariantul „card bogat ⇒ card cu motiv" ținea. Derivat din plan, n-ar
    mai ține, iar un card bogat cu secțiunea de motiv goală e mai rău decât unul simplu: arată ca
    o recomandare căreia i-a căzut argumentul.

    Nu e un al doilea writer semantic: `best_for` e un fapt de catalog, deja servit modelului în
    vederile de produs („bun pt …"), și e exact echivalentul pe care harnessul golden îl acceptă
    ca motiv (`_product_has_reason`). Se ia ca ATARE, fără frază în jur: o formulare ar însemna
    cuvinte românești în cod, iar P11 spune că limba e configurație, nu constantă.
    """
    if not product:
        return None
    value = product.get("best_for")
    if not value:
        attrs = product.get("attributes")
        value = attrs.get("best_for") if isinstance(attrs, dict) else None
    text = " ".join(str(value).split()) if value else ""
    return text or None


def plan_item_refs(
    plan: AnswerPlanV2, retrieved: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """`j["items"]` pentru `compose.assemble`, derivate din plan.

    Recomandările întâi, fiindcă ele poartă motivul (`fit_clause` = `reason`); apoi produsele
    SELECTATE pe care planul nu le-a motivat. A doua jumătate nu e un detaliu de completitudine: un
    tur poate selecta produse fără să emită `recommendations` (răspunsul direct la „ce ai pe raftul
    X" e o listă, nu o pledoarie), iar `_plan_products` — codul pe care îl înlocuim — citea EXACT
    `selected_products`. Fără ea, turul acela ar rămâne fără carduri, adică aceeași regresie mutată
    cu un pas mai încolo. Motivul lor vine din date (`_data_reason`), nu din nimic.

    Dedupe pe prima apariție: un produs poate fi și selectat, și recomandat, iar `assemble` ar
    ignora oricum al doilea — dar atunci capul de 6 s-ar consuma pe un duplicat.
    """
    by_id = {str(p.get("id") or p.get("product_id") or ""): p for p in (retrieved or [])}
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rec in plan.recommendations:
        if rec.product_id in seen:
            continue
        seen.add(rec.product_id)
        items.append({"product_id": rec.product_id, "fit_clause": rec.reason})
    for sel in plan.selected_products:
        if sel.product_id in seen:
            continue
        seen.add(sel.product_id)
        item: dict[str, Any] = {"product_id": sel.product_id}
        fallback = _data_reason(by_id.get(sel.product_id))
        if fallback:
            item["fit_clause"] = fallback
        items.append(item)
    return items


def card_refs(products: list[dict[str, Any]], n: int = 6) -> list[dict[str, Any]]:
    """Rânduri de retrieval → carduri cu IDENTITATE, pentru căile care nu trec prin `RichReply`.

    Există din cauza unei asimetrii de chei care a costat deja un defect tăcut: retrievalul scrie
    `id`, iar `channels/web/render._card` citește `product_id`. `processor._displayed_product_refs`
    are fallback pe `id` și de-aia starea conversației a rămas corectă; randorul nu are, deci
    cardurile plecau la widget cu `product_id: null` — și un card fără identitate nu e un card cu
    un câmp lipsă, e un card pe care nimic nu se poate lega (coș, acțiuni NX-236, „spune-mi mai
    multe"). Un rând fără id sau fără preț se ARUNCĂ: pe sârmă, un card mut e mai rău decât unul
    absent, fiindcă arată apăsabil.
    """
    cards: list[dict[str, Any]] = []
    for p in products[:n]:
        pid = p.get("product_id") or p.get("id")
        if not pid or p.get("name") is None or p.get("price") is None:
            continue
        card: dict[str, Any] = {
            "product_id": str(pid),
            "name": display_name(p["name"]),
            "price": float(p["price"]),
            "url": p.get("url"),
            "image": p.get("image"),
        }
        # NX-301: cheia lipsește când numele nu poartă gramajul (52,5% din catalogul real) —
        # aceeași regulă ca `variants`/`badge`: nu inventăm `null`-uri pe sârmă.
        size = size_label(p["name"])
        if size:
            card["size"] = size
        variants = _card_variants(p)
        if variants:
            card["variants"] = variants
        cards.append(card)
    return cards


def rich_from_plan(
    ctx: TurnContext,
    plan: AnswerPlanV2,
    retrieved: list[dict[str, Any]],
    *,
    text: str,
    suggestions: Sequence[str] = (),
) -> RichReply | None:
    """`RichReply` derivat din plan, sau `None` dacă turul n-are carduri (→ calea de azi).

    `None` nu e un eșec: un tur pur de clarificare sau un refuz onest NU au produse, iar niște
    carduri sub ele ar fi un răspuns care se contrazice singur. Apelantul cade pe `set_reply`.

    `intro` = proza creierului, nu proza recompusă de `compose`. Motivul e că textul ăsta a trecut
    deja `validate_revised_draft`, criticul semantic și `grounding_guard` — porți STRICT mai tari
    decât `scrub_intro`, care cunoaște doar cifrele clientului și pe cele de specificație. Trecut
    din nou prin `scrub_intro`, un răspuns corect care numește un preț ar fi aruncat ÎNTREG, iar
    clientul ar primi șase carduri și zero text. Îl punem, deci, direct — o singură poartă, cea
    tare, nu două care se contrazic.

    `pick` se stinge explicit. Nu fiindcă lipsesc datele, ci fiindcă justificarea lui vine din
    `_recommendation_anchor` (un pro din recenzii), adică o A DOUA voce lângă cea a creierului —
    exact relay-ul pe care D1 îl interzice. Regula de produs „fără «Recomandarea mea»" spune
    oricum același lucru, iar `rich_pick_web_enabled` e OFF; punând `None` aici, comportamentul nu
    mai depinde de care dintre cele două flag-uri e citit primul.
    """
    items = plan_item_refs(plan, retrieved)
    if not items:
        return None
    # `intro` = textul creierului, deși îl vom ÎNLOCUI oricum mai jos. Nu e redundant: `assemble`
    # îl folosește și ca ORDINE (`_order_by_first_mention`), iar fără el cardurile s-ar aranja
    # strict după ranking în timp ce proza numește produsele în ordinea ei. E defectul măsurat pe
    # trafic real în 2026-08-26 („textul spunea Nourish… Repair… Pure, cardurile veneau Nourish,
    # Pure, Repair"): un răspuns care se contrazice pe sine e mai rău decât unul cu position bias.
    # Valoarea SCRUBUITĂ pe care o calculează `assemble` se aruncă — vezi `replace` de mai jos.
    j: dict[str, Any] = {
        "items": items,
        "intro": text,
        "education": None,
        "pick": None,
        "suggestions": list(suggestions),
    }
    rich = compose.assemble(ctx, j, retrieved)
    # Poarta de apartenență a lui `assemble` poate goli setul (id-uri care nu-s în retrieval). Un
    # `RichReply` fără items ar randa un contract bogat fără nimic bogat în el.
    if not rich.items:
        return None
    return replace(rich, intro=text, pick=None)


def facts_item_refs(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`j["items"]` construite DOAR din rândurile de retrieval, fără niciun plan și niciun model.

    Un rând fără id, nume sau preț se ARUNCĂ, din același motiv ca în `card_refs`: pe sârmă, un card
    mut arată apăsabil. Dedupe pe prima apariție, ca `assemble` să nu consume capul pe un duplicat
    (retrievalul poate întoarce aceeași familie de nuanțe de două ori).
    """
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for p in products:
        raw = p.get("id") or p.get("product_id")
        if not raw or p.get("name") is None or p.get("price") is None:
            continue
        pid = str(raw)
        if pid in seen:
            continue
        seen.add(pid)
        item: dict[str, Any] = {"product_id": pid}
        reason = _data_reason(p)
        if reason:
            item["fit_clause"] = reason
        items.append(item)
    return items


def rich_from_facts(
    ctx: TurnContext,
    products: list[dict[str, Any]],
    *,
    intro: str | None = None,
) -> RichReply | None:
    """`RichReply` construit din faptele de catalog, pentru turele în care modelul rich a căzut.

    **De ce există.** Pe v1, TOATĂ bogăția răspunsului atârna de un singur apel structurat
    (`finalize._rich`). Când el eșua — timeout, zero items după poarta de apartenență, refuz —
    clientul nu primea un răspuns puțin mai sărac, ci `_deterministic_reply`: trei nume cu prețuri
    și o întrebare. Măsurat pe traficul real al lui `sole-ro` (56 de ture cu `conversation_traces`):
    **14 ture cu produse au ajuns la client cu `rich = null`**, adică un sfert. Pe turul
    `57fa9fbe` («vreau o rutina de cosuri») cauza a fost `APITimeoutError` pe un apel pe care
    NX-300 îl măsurase la 86,7 s dintr-un tur de 95,2 s, contra unui plafon de 30 s pe încercare:
    nu ghinion, ci o loterie pe care o pierdem previzibil.

    **Ce se pierde onest și ce nu.** Fără model nu există `intro` scris, `education`, și nici
    `fit_clause` narat. Restul nu depindea NICIODATĂ de el: prețul, ratingul, numărul de recenzii,
    badge-ul derivat, prețul de listă, `details`, variantele, gramajul și ordinea de ranking sunt
    fapte de catalog pe care serverul le are deja în mână. A le arunca odată cu proza e o degradare
    pe care n-o cere nimic — exact argumentul lui `grounded_fallback_reply`, dus până la capăt: nu
    doar textul prezintă faptele, ci contractul întreg.

    Motivul de sub card vine din `best_for` (`_data_reason`), ca pe calea creierului unic pe turele
    de întrebare. Nu e un al doilea writer semantic: e un fapt de catalog luat ca atare.

    `intro` = proza buclei de vânzare, și apelantul are voie s-o dea DOAR dacă a trecut `_valid`.
    Pe ea se ocolește `scrub_intro`, din același motiv ca în `rich_from_plan`: poarta prin care a
    trecut deja e strict mai tare. `validate_prose` cere ca fiecare preț să existe în retrieval,
    fiecare link să vină din catalog, zero claim medical, zero cifră bare negroundată și niciun
    claim de stoc nefondat; `scrub_intro` știe doar cifrele clientului și pe cele de specificație,
    deci ar arunca ÎNTREG un text corect fiindcă numește un preț REAL. Măsurat pe suită: fără
    ocolire, «Îți recomand Crema Hidratantă la 82,99 lei» dispărea, iar clientul rămânea cu lista de
    carduri și fără nicio frază.

    `intro=None` ⇒ `assemble` umple slotul de încadrare cu rezerva serverului (NX-299), deci tot o
    frază iese, nu un ecran de carduri mute.
    """
    items = facts_item_refs(products)
    if not items:
        return None
    j: dict[str, Any] = {
        "items": items,
        "intro": intro,
        "education": None,
        "pick": None,
        "suggestions": [],
    }
    rich = compose.assemble(ctx, j, products)
    if not rich.items:
        return None
    # `pick` stins din aceleași motive ca în `rich_from_plan`: e o A DOUA voce, iar regula de produs
    # „fără «Recomandarea mea»" o interzice oricum. `intro` se repune doar când apelantul a dat o
    # proză validată; altfel rămâne ce a decis `assemble` (rezerva de încadrare, sau nimic).
    if intro:
        return replace(rich, intro=intro, pick=None)
    return replace(rich, pick=None)


__all__ = [
    "card_refs",
    "facts_item_refs",
    "plan_item_refs",
    "rich_from_facts",
    "rich_from_plan",
]
