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


#: Afirmațiile despre MAGAZIN (livrare, retur, garanție, promoție): au sursă doar în regulile
#: citite în tur, nu într-o fișă de produs (un FAQ de produs despre livrare nu e regula
#: magazinului). Formele de verb care descriu produsul («oferă», «reduce», «nu pot garanta») nu
#: intră. Recenzia NX-403: tiparele compozitorului lăsau «livrarea e gratuită la orice comandă».
_STORE_FAMILIES = (
    re.compile(
        r"\b(livr\w*|curier\w*|expedi[ez]\w*|transportul\w*|transport\s+gratuit|shipping"
        r"|deliver\w*)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(retur\w*|returnez\w*|rambursa\w*|refund\w*)\b", re.IGNORECASE),
    re.compile(r"\b(garan[tț]i\w*|warranty)\b", re.IGNORECASE),
)
_PROMO = re.compile(
    r"\b(reducer\w*|ofert[aăe]\w*|promo[tț]i\w*|promo|voucher\w*|cupon\w*|discount\w*)\b",
    re.IGNORECASE,
)
#: Cât text se privește înaintea unei cifre ca să-i găsim unitatea (fără să recitim tot prefixul).
_LOOKBACK = 40


def unit_aliases(pack: Any) -> dict[str, tuple[str, float]]:
    """Aliasul pliat al fiecărei unități a pachetului → (unitatea canonică, factorul): «gr» →
    («g», 1), «l» → («ml», 1000), «ip» → («spf», 1); plus procentul. Date de pachet, nu o listă
    din cod (P11). PUR."""
    from src.catalog.folding import fold_text  # noqa: PLC0415

    specs = getattr(getattr(pack, "units", None), "specs", None) or {}
    out: dict[str, tuple[str, float]] = {"%": ("%", 1.0)}
    for spec in specs.values():
        canonical = fold_text(str(getattr(spec, "canonical", "") or ""))
        for alias, factor in (getattr(spec, "factors", None) or {}).items():
            word = fold_text(str(alias))
            if word:
                out[word] = (canonical or word, float(factor))
    return out


def _unit_pairs(
    text: str, aliases: Mapping[str, tuple[str, float]]
) -> list[tuple[float, str | None]]:
    """Fiecare cifră a textului ca (valoarea în unitatea canonică, unitatea canonică | None).
    Unitatea e cuvântul de după cifră, altfel cel dinainte, dar acela doar de cel puțin două
    litere («SPF 50», «IP 30»): altfel «lasă-l 1–2 minute» se citea „1 litru”. PUR."""
    from src.agent.detail_answer import _NUMBER, _WORD_AFTER, _WORD_BEFORE  # noqa: PLC0415
    from src.catalog.folding import fold_text  # noqa: PLC0415

    out: list[tuple[float, str | None]] = []
    for m in _NUMBER.finditer(text or ""):
        try:
            value = float(m.group().replace(",", "."))
        except ValueError:
            continue
        after = _WORD_AFTER.search(text[m.end() : m.end() + _LOOKBACK])
        before = _WORD_BEFORE.search(text[max(0, m.start() - _LOOKBACK) : m.start()])
        unit = None
        word = fold_text(after.group(1)) if after else ""
        if word in aliases:
            unit = aliases[word]
        else:
            word = fold_text(before.group(1)) if before else ""
            if word in aliases and len(word) > 1:
                unit = aliases[word]
        if unit is None:
            out.append((round(value, 4), None))
        else:
            out.append((round(value * unit[1], 4), unit[0]))
    return out


def numbers_grounded(
    text: str,
    facts: str,
    aliases: Mapping[str, tuple[str, float]],
    *,
    with_unit_only: bool = False,
) -> bool:
    """Fiecare cifră a textului există în fapte EXACT cum e scrisă: cu aceeași unitate, comparată
    canonic și la scară («200 g» = «200 gr», «0,5 l» = «500 ml», dar «50 l» ≠ «50 ml»), iar una
    fără unitate doar ca cifră fără unitate în fapte (recenzia NX-403: «30 de zile» trecea pe un
    „SPF 30”). Sumele de bani le judecă `_prices_ok`. O cifră de o singură unitate fără unitate
    («1–2 minute», «de 2 ori pe zi») nu se judecă, ca la poarta de dinainte (`_BARE_NUM_RE`
    prindea doar cifrele „grele”). `with_unit_only` = doar cifrele cu unitate (motivul unui card,
    pe faptele lui). PUR."""
    from src.agent.validator import _PRICE_RE  # noqa: PLC0415

    known = set(_unit_pairs(facts, aliases))
    for value, unit in _unit_pairs(_PRICE_RE.sub(" ", text or ""), aliases):
        if unit is None and (with_unit_only or (value < 10 and float(value).is_integer())):
            continue
        if (value, unit) not in known:
            return False
    return True


def _without_names(text: str, names: list[str]) -> str:
    """Textul fără numele produselor turului: un nume („ABIB Acne Foam”) nu e o afirmație."""
    out = text
    for name in sorted({n for n in names if n}, key=len, reverse=True):
        out = re.sub(re.escape(name), " ", out, flags=re.IGNORECASE)
    return out


def _names_a_product(sentence: str, names: list[str]) -> bool:
    """Propoziția numește un produs al turului: numele întreg, primele două cuvinte ale lui (cum
    îl scurtează agentul: «Crema Solara A» pentru „Crema Solara A SPF 50+”) sau marca. Greșeala
    merge spre partea sigură: o propoziție luată drept „despre produs” pierde doar dreptul de a
    folosi sumele regulilor."""
    from src.catalog.folding import fold_text  # noqa: PLC0415

    folded = f" {' '.join(fold_text(sentence).split())} "
    for name in names:
        words = fold_text(name).split()
        if not words or len(" ".join(words)) < 3:
            continue
        for probe in (words, words[:2]):
            if f" {' '.join(probe)} " in folded:
                return True
    return False


def _store_claim_unsourced(body: str, sources: list[str], products: list[dict[str, Any]]) -> bool:
    """O afirmație despre livrare, retur, garanție sau promoție fără o regulă a magazinului citită
    în tur care să poarte aceeași familie. O promoție are sursă și când textul numește codul de
    voucher de pe fișa unui produs citit."""
    rules = "\n".join(sources)
    for family in _STORE_FAMILIES:
        if family.search(body) and not family.search(rules):
            return True
    if _PROMO.search(body) and not _PROMO.search(rules):
        codes = {str(p.get("coupon_code") or "").strip() for p in products}
        if not any(c and c.casefold() in body.casefold() for c in codes):
            return True
    return False


def judge(
    body: str,
    *,
    products: list[dict[str, Any]],
    facts: str,
    units: Mapping[str, tuple[str, float]],
    sums: set[float],
    sources: list[str],
    order_found: bool,
    names: list[str] | None = None,
) -> list[str]:
    """Poarta pe fapte a textului agentului (NX-403). PURĂ (în afara flagurilor citite de porțile
    refolosite). `facts` = `Tools.facts_text()`; `units` = aliasurile unităților pachetului
    (`unit_aliases`); `sums` = sumele-fapt ale turului; `names` = numele produselor turului (cum
    le scrie agentul și cum sunt în catalog). Întoarce motivele de respingere, în ordinea
    porților."""
    from src.agent.composer import rule_prices  # noqa: PLC0415
    from src.agent.composer import sentences as composer_sentences  # noqa: PLC0415
    from src.agent.detail_answer import _unfounded_stock_claim  # noqa: PLC0415
    from src.agent.validator import (  # noqa: PLC0415
        _links_ok,
        _prices_ok,
        _safety_ok,
        strip_quoted,
    )
    from src.worker.text_scrub import has_popularity_claim  # noqa: PLC0415

    names = list(names or [])
    reasons: list[str] = []
    # P0 pe TOT textul (o afecțiune și verbul pot sta în propoziții diferite: «Ai acnee? Serul ăsta
    # o tratează»), dar fără numele produselor: «3. Tratament: …» lângă numele „ABIB Acne Foam”
    # nu e o afirmație medicală.
    if not _safety_ok(_without_names(body, names)):
        reasons.append("medical_claim")
    if not _links_ok(body, products):
        reasons.append("invented_link")
    extra = {
        float(p[k])
        for p in products
        for k in ("list_price", "coupon_price")
        if isinstance(p.get(k), int | float) and not isinstance(p.get(k), bool)
    }
    allowed = extra | sums
    if not _prices_ok(body, products, allowed):
        # O sumă dintr-o regulă citită (pragul de livrare, costul returului) e întemeiată doar
        # într-o propoziție care nu numește un produs: altfel «Crema A costă 199 lei» ar trece
        # pe pragul de livrare (recenzia NX-403, clasa NX-121).
        rule = set(rule_prices(sources))
        if not rule or not all(
            _prices_ok(s, products, allowed | (set() if _names_a_product(s, names) else rule))
            for line in body.splitlines()
            for s in composer_sentences(line)
        ):
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
    if _store_claim_unsourced(body, sources, products):
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


def _product_names(
    names: dict[str, str], short_names: dict[str, str], rows: dict[str, dict[str, Any]]
) -> list[str]:
    """Numele produselor turului, în toate formele în care le poate scrie agentul: distinct,
    scurt, numele scurt din catalog, numele întreg, marca."""
    from src.catalog.render_text import display_name  # noqa: PLC0415

    out = [*names.values(), *short_names.values()]
    for row in rows.values():
        name = str(row.get("name") or "")
        out += [name, display_name(name), str(row.get("brand") or "")]
    return [n.strip() for n in out if n and n.strip()]


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
    units: Mapping[str, tuple[str, float]] | None = None,
    product_facts: dict[str, str] | None = None,
) -> Checked:
    """`handles` = handle → id; `names` = handle → numele distinct (textul); `short_names` =
    handle → numele scurt (sugestiile: randorul web aruncă parantezele); `rows` = id → rândul gated
    al turului; `shown_sets` = seturile de id-uri arătate mai devreme; `grounded` = sumele-fapt;
    `facts` = faptele turului ca text (`Tools.facts_text`); `units` = unitățile pachetului;
    `product_facts` = id → faptele UNUI produs (`Tools.product_text`), sursa cifrelor din motivul
    cardului lui."""
    from src.agent.validator import set_total  # noqa: PLC0415

    units = units if units is not None else {"%": ("%", 1.0)}
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
            names=_product_names(names, short_names, rows),
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
        # Motivul unui card vorbește mereu despre produsul lui: o sumă dintr-o regulă a magazinului
        # (pragul de livrare) nu poate sta acolo ca preț.
        from src.agent.validator import _prices_ok  # noqa: PLC0415

        own_sums = sums | {
            float(p[k])
            for p in products
            for k in ("list_price", "coupon_price")
            if isinstance(p.get(k), int | float) and not isinstance(p.get(k), bool)
        }
        if any(c["reason"] and not _prices_ok(c["reason"], products, own_sums) for c in cards):
            rejected.append("ungrounded_price")

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
