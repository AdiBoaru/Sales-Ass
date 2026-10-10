"""Poarta de adevăr pe tot ce scrie agentul unic (NX-396, pe fapte din NX-403).

Nimic nu re-verifică `ctx.reply` după `agent_stage`, deci agentul se judecă aici, înainte de
răspuns: textul, sfatul de sub carduri, motivele cardurilor și textele comparației, judecate
împreună (`judge`). Poarta e pe FAPTE, ca a compozitorului (NX-382): fiecare preț e al unui produs
citit sau o sumă-fapt; fiecare altă cifră există în ce au întors uneltele, cu aceeași unitate
lângă ea; linkurile sunt ale produselor; stocul afirmat are un produs disponibil; nicio afirmație
medicală; livrarea, garanția sau o promoție doar când o sursă a turului le poartă. Până la NX-403
poarta era `validate_prose`, cu liste de cuvinte («recenzii», «zile», «cea mai») și cifre permise
doar dacă erau preț, stoc sau rating: pe 9-10 oct a respins toate cele 7 răspunsuri căzute ale
agentului și 9 din cele 12 reîncercări, toate corecte («SPF 30», «din 23 de recenzii», «cea mai
ieftină», «30 de zile» din regula de retur). Rămâne o singură listă, îngustă: popularitatea
(«best seller», «cel mai vândut»), pentru care catalogul n-are nicio dată.

Sursele sumelor: produsele citite în tur (gated), regulile magazinului citite și sumele care sunt
fapte: totalul fiecărui set arătat (NX-391), al coșului, sumele unei comenzi găsite. Sumele scrise
de CLIENT nu sunt fapte: un preț „văzut pe alt site” ar deveni preț permis, adică exact poarta
anti-injecție (NX-121) deschisă; de aceea faptele nu conțin niciodată mesajele clientului.

Handle-urile (`P3`) sunt eticheta NOASTRĂ: scăpate în text devin numele produsului (NX-369), iar
unul necunoscut respinge răspunsul. O greșeală respinge răspunsul și agentul primește motivul; o
sugestie greșită doar iese. Funcție PURĂ pe faptele date.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: Lungimea maximă a unei sugestii (randorul web taie peste 56, `render._web_chips`).
SUGGESTION_MAX = 56
_HANDLE_IN_TEXT = re.compile(r"\bP[1-9][0-9]{0,3}\b")
#: Numerotarea pașilor («1. Curățare») e forma textului, nu o cifră despre produs.
_ENUMERATOR = re.compile(r"(?m)^\s*\d{1,2}[.)](?=\s)")

HINTS = {
    "ungrounded_price": (
        "a price or sum is not in the tool results; use the exact prices, and do not repeat "
        "the customer's own figures as prices"
    ),
    "invented_link": "a link is not a product link from the tool results; remove it",
    "ungrounded_number": (
        "a number is not in what the tools returned, or not with that unit; write it exactly as "
        "the facts give it (with its unit) or remove it"
    ),
    "card_number": (
        "a number in a card reason is not in that product's own facts; use that product's "
        "figure, with its unit, or remove it"
    ),
    "popularity_claim": (
        "remove the sales or popularity claim (best seller, most sold, number one): the store "
        "has no data for it; say what the reviews or the facts say instead"
    ),
    "unsourced_claim": (
        "delivery, a warranty or a promotion that no store rule or sheet you read states; remove "
        "it, or read store_rules and say it as the rule does"
    ),
    "stock_claim": (
        "a stock statement contradicts the product's availability; fix it, and if you did not "
        "mean stock, say it without the words for available or in stock"
    ),
    "medical_claim": "remove the medical claim (treats, cures, safe in pregnancy and the like)",
    "unknown_card": "a card handle is not one you know; use handles from PRODUCTS YOU KNOW",
    "unavailable_card": "a card product cannot be shown now; choose another one",
    "comparison_handles": "a comparison needs 2-4 different products you can show",
    "handle_in_text": "write product names in the text, not handles; handles go only in fields",
    "empty": "the answer has no text; write what the customer reads",
}


@dataclass
class Checked:
    """Un `answer` judecat: ce s-ar servi și motivele de respingere (gol = acceptat)."""

    text: str
    advice: str  # NX-403: sfatul de sub carduri (cum alegi + ce ai lua tu)
    cards: list[dict[str, str]]  # {handle, reason}
    kept_suggestions: list[str]
    dropped_suggestions: int
    comparison: dict[str, Any] | None
    notes: str
    rejected: list[str]


def name_handles(text: str, names: dict[str, str]) -> str:
    """Handle-urile cunoscute din text devin numele produsului. PUR."""
    return _HANDLE_IN_TEXT.sub(lambda m: names.get(m.group(0), m.group(0)), text or "")


def has_handle(text: str) -> bool:
    return bool(_HANDLE_IN_TEXT.search(text or ""))


def unit_aliases(pack: Any) -> dict[str, str]:
    """Aliasul pliat al fiecărei unități a pachetului → unitatea canonică («gr» → «g», «ip» →
    «spf»), plus procentul. Date de pachet, nu o listă din cod (P11). PUR."""
    from src.catalog.folding import fold_text  # noqa: PLC0415

    specs = getattr(getattr(pack, "units", None), "specs", None) or {}
    out = {"%": "%"}
    for spec in specs.values():
        canonical = fold_text(str(getattr(spec, "canonical", "") or ""))
        for alias in getattr(spec, "factors", None) or {}:
            word = fold_text(str(alias))
            if word:
                out[word] = canonical or word
    return out


def _unit_pairs(text: str, aliases: Mapping[str, str]) -> list[tuple[float, frozenset[str]]]:
    """Ca `detail_answer._pairs`, cu o excepție: înaintea cifrei, o unitate are cel puțin două
    litere («SPF 50», «IP 30»). Altfel «lasă-l 1–2 minute» se citea „1 litru”. PUR."""
    from src.agent.detail_answer import _NUMBER, _WORD_AFTER, _WORD_BEFORE  # noqa: PLC0415
    from src.catalog.folding import fold_text  # noqa: PLC0415

    out: list[tuple[float, frozenset[str]]] = []
    for m in _NUMBER.finditer(text or ""):
        try:
            value = round(float(m.group().replace(",", ".")), 4)
        except ValueError:
            continue
        near: set[str] = set()
        before = _WORD_BEFORE.search(text[: m.start()])
        after = _WORD_AFTER.search(text[m.end() :])
        word = fold_text(before.group(1)) if before else ""
        if word in aliases and len(word) > 1:
            near.add(word)
        word = fold_text(after.group(1)) if after else ""
        if word in aliases:
            near.add(word)
        out.append((value, frozenset(near)))
    return out


def numbers_grounded(
    text: str, facts: str, aliases: Mapping[str, str], *, with_unit_only: bool = False
) -> bool:
    """Fiecare cifră a textului există în fapte, iar unitatea lipită de ea în text stă lângă
    ACEEAȘI cifră și în fapte, comparată canonic («200 g» = «200 gr»). Sumele de bani le judecă
    `_prices_ok`. O cifră de o singură unitate fără unitate («1–2 minute», «de 2 ori pe zi») nu se
    judecă, ca la poarta de dinainte (`_BARE_NUM_RE` prindea doar cifrele „grele”).
    `with_unit_only` = se judecă doar cifrele cu unitate (motivul unui card, pe faptele lui).
    PUR."""
    from src.agent.validator import _PRICE_RE  # noqa: PLC0415

    def canon(near: frozenset[str]) -> set[str]:
        return {aliases.get(w, w) for w in near}

    known: dict[float, set[str]] = {}
    for value, near in _unit_pairs(facts, aliases):
        known.setdefault(value, set()).update(canon(near))
    for value, near in _unit_pairs(_PRICE_RE.sub(" ", text or ""), aliases):
        if not near and (with_unit_only or (value < 10 and float(value).is_integer())):
            continue
        if value not in known or not canon(near) <= known[value]:
            return False
    return True


def judge(
    body: str,
    *,
    products: list[dict[str, Any]],
    facts: str,
    units: Mapping[str, str],
    sums: set[float],
    sources: list[str],
    order_found: bool,
) -> list[str]:
    """Poarta pe fapte a textului agentului (NX-403). PURĂ (în afara flagurilor citite de porțile
    refolosite). `facts` = `Tools.facts_text()`; `units` = aliasurile unităților pachetului
    (`unit_aliases`); `sums` = sumele-fapt ale turului. Întoarce motivele de respingere, în ordinea
    porților."""
    from src.agent.composer import rule_prices, unsourced  # noqa: PLC0415
    from src.agent.composer import sentences as composer_sentences  # noqa: PLC0415
    from src.agent.detail_answer import _unfounded_stock_claim  # noqa: PLC0415
    from src.agent.validator import (  # noqa: PLC0415
        _links_ok,
        _prices_ok,
        _safety_ok,
        strip_quoted,
    )
    from src.worker.text_scrub import has_popularity_claim  # noqa: PLC0415

    reasons: list[str] = []
    # P0 pe PROPOZIȚIE, ca la compozitor: detectorul cere un verb terapeutic și o afecțiune, iar pe
    # tot textul le lega peste propoziții («3. Tratament: …» lângă numele „ABIB Acne Foam”).
    if any(
        not _safety_ok(sentence)
        for line in body.splitlines()
        for sentence in composer_sentences(line)
    ):
        reasons.append("medical_claim")
    if not _links_ok(body, products):
        reasons.append("invented_link")
    extra = {
        float(p[k])
        for p in products
        for k in ("list_price", "coupon_price")
        if isinstance(p.get(k), int | float) and not isinstance(p.get(k), bool)
    }
    if not _prices_ok(body, products, extra | sums | set(rule_prices(sources))):
        reasons.append("ungrounded_price")
    # O comandă găsită aduce cifrele ei (dată, AWB, cantitate) prin unealtă; o regulă citată
    # literal (NX-346) e a magazinului, cu cifrele ei. Restul cifrelor se caută în fapte.
    if not order_found:
        loose = _ENUMERATOR.sub(" ", strip_quoted(body, sources))
        if not numbers_grounded(loose, facts, units):
            reasons.append("ungrounded_number")
    # Fără niciun produs citit, „disponibil” nu are la ce se referi («din informațiile
    # disponibile», după o trimitere la medic). Poarta de dinainte nu judeca deloc stocul (flagul
    # `_stock_claim_ok` e stins).
    if products and _unfounded_stock_claim(body, products):
        reasons.append("stock_claim")
    if unsourced(body, facts):
        reasons.append("unsourced_claim")
    if has_popularity_claim(body):
        reasons.append("popularity_claim")
    return reasons


def _suggestion_ok(s: str, products: list[dict[str, Any]], sums: set[float]) -> bool:
    """O sugestie e mesajul pe care l-ar trimite clientul: scurtă, fără handle, fără link, fără
    un preț care nu e fapt, fără afirmație medicală sau de popularitate."""
    from src.agent.validator import _links_ok, _prices_ok, _safety_ok  # noqa: PLC0415
    from src.worker.text_scrub import has_popularity_claim  # noqa: PLC0415

    return (
        len(s) <= SUGGESTION_MAX
        and not has_handle(s)
        and _safety_ok(s)
        and _links_ok(s, products)
        and _prices_ok(s, products, sums)
        and not has_popularity_claim(s)
    )


def check_answer(
    a: dict[str, Any],
    *,
    handles: dict[str, str],
    names: dict[str, str],
    short_names: dict[str, str],
    rows: dict[str, dict[str, Any]],
    shown_sets: list[list[str]],
    grounded: set[float],
    sources: list[str],
    order_found: bool,
    max_cards: int,
    max_suggestions: int,
    notes_max: int,
    facts: str = "",
    units: Mapping[str, str] | None = None,
    product_facts: dict[str, str] | None = None,
) -> Checked:
    """`handles` = handle → id; `names` = handle → numele distinct (textul); `short_names` =
    handle → numele scurt (sugestiile: randorul web aruncă parantezele); `rows` = id → rândul gated
    al turului; `shown_sets` = seturile de id-uri arătate mai devreme; `grounded` = sumele-fapt;
    `facts` = faptele turului ca text (`Tools.facts_text`); `units` = unitățile pachetului;
    `product_facts` = id → faptele UNUI produs (`Tools.product_text`), sursa cifrelor din motivul
    cardului lui."""
    from src.agent.validator import set_total  # noqa: PLC0415

    units = units if units is not None else {"%": "%"}
    rejected: list[str] = []
    cards: list[dict[str, str]] = []
    for c in a.get("cards") or []:
        h = str((c or {}).get("handle") or "")
        if h not in handles:
            rejected.append("unknown_card")
            continue
        if handles[h] not in rows:
            rejected.append("unavailable_card")
            continue
        if h not in {x["handle"] for x in cards}:
            reason = name_handles(str((c or {}).get("reason") or "").strip(), names)
            cards.append({"handle": h, "reason": reason})
    cards = cards[:max_cards]

    comp = a.get("comparison")
    if comp:
        wanted = list(dict.fromkeys(str(h) for h in comp.get("handles") or []))
        if not 2 <= len(wanted) <= 4 or any(
            h not in handles or handles[h] not in rows for h in wanted
        ):
            rejected.append("comparison_handles")
            comp = None
        else:
            comp = {
                "handles": wanted,
                **{
                    k: name_handles(str(comp.get(k) or ""), names)
                    for k in ("intro", "subtitle", "closing")
                },
            }

    sets = [*shown_sets, [handles[c["handle"]] for c in cards]]
    if comp:
        sets.append([handles[h] for h in comp["handles"]])
    sums = set(grounded)
    for ids in sets:
        total = set_total([rows[i] for i in ids if i in rows])
        if total is not None:
            sums.add(total)

    products = list(rows.values())
    text = name_handles(str(a.get("text") or "").strip(), names)
    advice = name_handles(str(a.get("advice") or "").strip(), names)
    body = "\n".join(
        [
            text,
            advice,
            *(c["reason"] for c in cards if c["reason"]),
            *(str(comp.get(k) or "") for k in ("intro", "subtitle", "closing") if comp),
        ]
    ).strip()
    if not body:
        rejected.append("empty")
    else:
        if has_handle(body):
            rejected.append("handle_in_text")
        rejected += judge(
            body,
            products=products,
            facts=facts,
            units=units,
            sums=sums,
            sources=sources,
            order_found=order_found,
        )
        if product_facts is not None and not order_found:
            # Doar cifrele cu unitate (SPF, ml, %): clasa „faptul altui produs pe cardul ăsta”.
            # Prețul și o cifră fără unitate rămân ale porții pe tot textul: cardul își arată
            # oricum prețul real lângă motiv.
            for c in cards:
                own = product_facts.get(handles[c["handle"]], "")
                if c["reason"] and not numbers_grounded(
                    c["reason"], own, units, with_unit_only=True
                ):
                    rejected.append("card_number")
                    break

    kept: list[str] = []
    dropped = 0
    for raw in a.get("suggestions") or []:
        s = name_handles(str(raw or "").strip(), short_names)
        if not s or s in kept:
            continue
        if _suggestion_ok(s, products, sums):
            kept.append(s)
        else:
            dropped += 1

    return Checked(
        text=text,
        advice=advice,
        cards=cards,
        kept_suggestions=kept[:max_suggestions],
        dropped_suggestions=dropped,
        comparison=comp,
        notes=str(a.get("notes") or "").strip()[:notes_max],
        rejected=list(dict.fromkeys(rejected)),
    )
