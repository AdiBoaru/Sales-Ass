"""Replici deterministe + utilitare pure ale stagiului agent (NX-143, faza „fallbacks").

Zero LLM, zero `TurnContext`/`deps`/DB — doar `str`/`dict` in, `str`/`list`/`dict` out. Aici
trăiesc mesajele per-locale (niciodată tăcere, P6) + micile transformări de produse (dedup, câmpuri
de card, brief pt prompt). Consumate de `deterministic.py` (intenții pre-loop), `planner.py`
(shaping) și `finalize.py` (render). Un singur loc pentru textele deterministe (traducere: NX-156).
"""

from __future__ import annotations

from typing import Any

from src.catalog.render_text import display_name, size_label
from src.config import card_slots
from src.models import MAX_CHIP_LEN
from src.web.localization import amount_text

# Mesaj determinist când NU există nimic mai ieftin (niciodată tăcere/padding, P6). Per-locale.
_CHEAPEST_ALREADY: dict[str, str] = {
    "ro": "Momentan asta e cea mai ieftină opțiune pe care o am pentru tine. "
    "Vrei să-ți arăt altceva sau o altă categorie?",
    "en": "This is the cheapest option I have right now. "
    "Want me to show you something else or another category?",
}


def _cheapest_already_msg(language: str | None) -> str:
    return _CHEAPEST_ALREADY.get(language or "ro") or _CHEAPEST_ALREADY["ro"]


# NX-159 felia 2: chips deterministe de CONTINUARE pentru căile subțiri (no-result / cheapest-
# already). Voce de client (reintră ca tur nou: „Hai să schimbăm bugetul" → cheaper/refine). Căi
# CONCRETE, nu fundătură generică. Per-locale. GENERIC pe vertical (formularea nu e specifică
# beauty). Scrise ca MESAJE, nu ca etichete: pe o cale subțire clientul n-are context afișat de
# care să se agațe, deci un „Schimbă bugetul" retrimis singur nu spune nimănui ce schimbă.
_THIN_PATH_CHIPS: dict[str, list[str]] = {
    "ro": [
        "Arată-mi ce se caută cel mai des la voi",
        "Hai să schimbăm bugetul",
        "Prefer să caut în altă categorie",
    ],
    "en": [
        "Show me what people buy most from you",
        "Let's change the budget",
        "I'd rather look in another category",
    ],
}


# NX-241 — ce spunem când bugetul de timp al turului s-a terminat înainte de răspuns. Două
# variante, fiindcă „nu am apucat" e altceva decât „nu am găsit": dacă avem deja produse validate
# (retrievate, deci fapte reale), le ARĂTĂM — e mai onest și mai util decât un mesaj de eroare.
# Textele nu conțin cifre/nume → nu pot pica validatorul și nu pot inventa nimic.
_DEADLINE_EMPTY: dict[str, str] = {
    "ro": "Nu am apucat să verific tot la timp. Vrei să încerc din nou?",
    "en": "I didn't manage to check everything in time. Want me to try again?",
}
_DEADLINE_PARTIAL: dict[str, str] = {
    "ro": "Uite ce am găsit până acum. Spune-mi dacă vrei să caut mai departe.",
    "en": "Here's what I found so far. Tell me if you want me to keep looking.",
}


def _deadline_msg(language: str | None, *, partial: bool) -> str:
    """Mesajul determinist de epuizare a bugetului de timp (P6: niciodată tăcere, niciodată draft
    nevalidat). `partial=True` când avem deja produse validate de arătat."""
    table = _DEADLINE_PARTIAL if partial else _DEADLINE_EMPTY
    return table.get(language or "ro") or table["ro"]


def _thin_path_chips(language: str | None) -> list[str]:
    """Chips deterministe de continuare (căi concrete) pentru un răspuns de cale subțire."""
    return list(_THIN_PATH_CHIPS.get(language or "ro") or _THIN_PATH_CHIPS["ro"])


def _is_short_ack(text: str | None) -> bool:
    """Răspuns `simple`/nano „subțire": scurt (sub prag) ȘI fără întrebare — semnalul clasic „Da." /
    „Ok." / „Cu plăcere." care închide conversația sec. Un răspuns scurt DAR cu „?" (nano a întrebat
    ceva) NU e fundătură → nu-l atingem. Pragul e aliniat cu `SHORT_REPLY_CHARS` din telemetrie."""
    t = (text or "").strip()
    return 0 < len(t) < 20 and "?" not in t


# #7b — cross-sell la add-to-cart (model iZi). Confirmarea coșului e DETERMINISTĂ (per-locale, NU
# scrubuită → robustă la nume de produs cu cifre, ex. „30 ml"); produsele complementare + fit-ul
# lor vin din calea rich. `_CROSS_SELL_QUERY` = instrucțiunea către modelul rich (complement, nu
# alternativă). Generic pe vertical (formularea nu e specifică beauty).
_CART_CONFIRM: dict[str, str] = {
    "ro": "Gata, am adăugat {name} în coș 🛒 Iată ce merge bine cu el:",
    "en": "Done, I added {name} to your cart 🛒 Here's what pairs well with it:",
}
_CROSS_SELL_QUERY: dict[str, str] = {
    "ro": "Clientul tocmai a adăugat în coș «{name}». Recomandă produsele de mai jos ca fiind "
    "COMPLEMENTARE (merg bine împreună / completează rutina sau alegerea), NU ca alternative. "
    "Pentru fiecare, spune SCURT de ce se potrivește cu «{name}».",
    "en": "The customer just added «{name}» to the cart. Recommend the products below as "
    "COMPLEMENTARY (they pair well / complete the routine or choice), NOT as alternatives. For "
    "each, briefly say why it fits with «{name}».",
}


# NX-263: aceeași suprafață, dar produsele sunt PAȘII unei secvențe, nu un set de complemente.
# Instrucțiunea trebuie să fie alta, altfel ordonarea nu înseamnă nimic: modelul ar descrie o
# rutină ca pe o listă de sugestii, iar clientul n-ar afla că ordinea contează. Titlul secvenței
# (`{sequence}`) vine din `RelationKindSpec.label(locale)`, deci din config-ul tenantului: la beauty
# „Pași recomandați", la electrocasnice „Necesare la instalare". Nimic specific unui vertical aici.
_RELATION_CHAIN_QUERY: dict[str, str] = {
    "ro": "Clientul tocmai a adăugat în coș «{name}». Produsele de mai jos sunt PAȘII următori, "
    "ÎN ORDINE ({sequence}), nu alternative și nu o listă de sugestii. Spune scurt la ce e "
    "fiecare pas și păstrează ordinea în care ți-au fost date.",
    "en": "The customer just added «{name}» to the cart. The products below are the next STEPS, "
    "IN ORDER ({sequence}), not alternatives and not a list of suggestions. Briefly say what each "
    "step is for, and keep the order you were given.",
}


def _relation_chain_query(
    added: dict[str, Any], language: str | None, sequence_label: str | None
) -> str:
    """Instrucțiunea către modelul rich pentru o SECVENȚĂ. `sequence_label` lipsă → cade pe
    formularea de complement: mai bine un text corect fără titlu decât un titlu inventat (P11)."""
    tmpl = _RELATION_CHAIN_QUERY.get(language or "ro") or _RELATION_CHAIN_QUERY["ro"]
    if not sequence_label:
        return _cross_sell_query(added, language)
    return tmpl.format(name=added.get("name") or "produsul", sequence=sequence_label)


def _cart_confirm_msg(added: dict[str, Any], language: str | None) -> str:
    tmpl = _CART_CONFIRM.get(language or "ro") or _CART_CONFIRM["ro"]
    return tmpl.format(name=added.get("name") or "produsul")


def _cross_sell_query(added: dict[str, Any], language: str | None) -> str:
    tmpl = _CROSS_SELL_QUERY.get(language or "ro") or _CROSS_SELL_QUERY["ro"]
    return tmpl.format(name=added.get("name") or "produsul")


# IZI-compare: chips deterministe pe un tabel comparativ (voce de client → reintră ca tur nou:
# „Adaugă X în coș" → cart_add; „mai ieftin" → cheaper; „spune-mi mai multe" → detail_intent).
# Formulările PĂSTREAZĂ cuvintele pe care le prind regexurile din `deterministic` (`_CHEAPER_RE`,
# `_DETAIL_RE`): un chip mai natural care nu se mai rutează ar fi o regresie, nu o îmbunătățire.
_COMPARE_FOLLOWUPS: dict[str, dict[str, str]] = {
    "ro": {
        "add": "Adaugă {name} în coș",
        "detail": "Spune-mi mai multe despre {name}",
        "cheaper": "Vreau ceva mai ieftin decât astea",
    },
    "en": {
        "add": "Add {name} to my cart",
        "detail": "Tell me more about {name}",
        "cheaper": "I want something cheaper than these",
    },
}


def fit_template(template: str, **slots: str) -> str:
    """Umple un șablon de chip scurtând SLOTURILE, nu chip-ul.

    Tăierea la coadă („Adaugă Velora Soft…") ar rupe fix partea care rutează (în engleză verbul e
    la început: „Add to cart: …"), deci bugetul se calculează din ce rămâne după șablon.

    Cu mai multe sloturi (o comparație numește două produse) se scurtează mereu CEL MAI LUNG,
    până încape: două nume tăiate la jumătate identifică mai prost decât unul întreg și unul
    scurtat. Ordinea e deterministă (lungime, apoi nume de slot), deci același chip iese identic
    la fiecare rulare — un chip care se schimbă între ture ar rupe dedupe-ul de mai sus.
    """
    budget = MAX_CHIP_LEN - len(template.format(**dict.fromkeys(slots, "")))
    values = {k: " ".join(str(v).split()) for k, v in slots.items()}
    while values and sum(len(v) for v in values.values()) > budget:
        key = max(values, key=lambda k: (len(values[k]), k))
        allowed = budget - sum(len(v) for k, v in values.items() if k != key)
        if allowed <= 1:
            values[key] = values[key][:1]
            break
        values[key] = values[key][: allowed - 1].rstrip() + "…"
    return template.format(**values)


def fit_chip(template: str, name: str) -> str:
    """`fit_template` cu slotul clasic `{name}` — păstrat fiindcă îl cheamă patru producători."""
    return fit_template(template, name=name)


def _compare_chips(columns: list[Any], language: str | None) -> list[str]:
    """Follow-up-uri deterministe după o comparație, ca mesaje de client (fără scrub): „adaugă în
    coș" pentru primele două, detalii pe prima, plus calea „mai ieftin". Patru chips, fiindcă după
    un tabel clientul are deja produsele în față și pasul următor e o alegere, nu o rafinare."""
    lang = language or "ro"
    copy = _COMPARE_FOLLOWUPS.get(lang) or _COMPARE_FOLLOWUPS["ro"]
    from src.config import get_settings  # noqa: PLC0415 — evită ciclul la import

    if getattr(get_settings(), "comparison_axes_v2_enabled", False):
        return _compare_chips_whole_words(columns, copy, lang)
    chips = [fit_chip(copy["add"], c.name) for c in columns[:2]]
    if columns:
        chips.append(fit_chip(copy["detail"], columns[0].name))
    chips.append(copy["cheaper"])
    return chips


def _compare_chips_whole_words(
    columns: list[Any], copy: dict[str, str], language: str
) -> list[str]:
    """NX-317: aceleași patru chips, cu numele scurtat pe CUVINTE ÎNTREGI, nu pe caractere.

    Pe turul real ieșea „Adaugă Bakuchiol C… în coș": textul se retrimite ca mesaj, iar un nume
    tăiat în mijlocul cuvântului nu mai numește produsul. Numele se scurtează până la prefixul lui
    UNIC între coloane (NX-318, `unique_prefixes`), minimum două cuvinte, nu mai jos. Dacă nici așa
    nu încape, chip-ul nu se oferă: un chip ambiguu e mai rău decât unul lipsă (aceeași regulă ca
    `chip_moves._fit_anchor`). „Mai ieftin" nu numește nimic, deci rămâne mereu."""
    from src.catalog.render_text import unique_prefixes  # noqa: PLC0415

    names = {str(c.product_id): str(c.name) for c in columns if c.product_id and c.name}
    unique = unique_prefixes(names, locale=language)

    def _fit(template: str, pid: str) -> str | None:
        words = names.get(pid, "").split()
        floor = max(2, len(unique.get(pid, ())))
        for n in range(len(words), min(floor, len(words)) - 1, -1):
            text = template.format(name=" ".join(words[:n]))
            if len(text) <= MAX_CHIP_LEN:
                return text
        return None

    chips = [_fit(copy["add"], str(c.product_id)) for c in columns[:2]]
    if columns:
        chips.append(_fit(copy["detail"], str(columns[0].product_id)))
    chips.append(copy["cheaper"])
    return [c for c in chips if c]


# Pool epuizat pe „mai arată-mi" → mesaj determinist per-locale (P6, fără tăcere; cacheable=False
# fiindcă e relativ la sesiunea ACESTUI contact — un cache hit l-ar servi altui context).
_NO_MORE_RESULTS: dict[str, str] = {
    "ro": "Astea sunt toate opțiunile pe care le am pe criteriile astea. "
    "Vrei să căutăm altceva sau să schimbăm filtrele?",
    "en": "That's everything I have for these criteria. "
    "Want to search for something else or adjust the filters?",
}


def _no_more_msg(language: str | None) -> str:
    return _NO_MORE_RESULTS.get(language or "ro") or _NO_MORE_RESULTS["ro"]


# Lead-uri SCURTE per-locale pt răspunsul de link (linkul REAL vine ca Offer(open_url)/card, NU în
# proză — validatorul ar respinge un url inventat oricum). Unul vs mai multe produse țintă.
_LINK_LEAD_ONE: dict[str, str] = {
    "ro": "Sigur! 🙂 Uite linkul direct 👇",
    "en": "Sure! 🙂 Here's the direct link 👇",
}
_LINK_LEAD_MANY: dict[str, str] = {
    "ro": "Sigur! Uite linkurile direct la produsele de mai sus 👇",
    "en": "Sure! Here are the direct links to the products above 👇",
}
# product_url absent (gaură de date pe demo) → ONEST, fără link inventat (PP-F4). Channel-neutru
# (pipeline-ul nu știe de „butonul Adaugă" al web-ului); oferim pasul care EXISTĂ. cacheable=False.
_NO_LINK: dict[str, str] = {
    "ro": "Momentan nu am o pagină de produs pe care să ți-o deschid direct, dar te pot ajuta "
    "să-l comanzi pas cu pas. Vrei?",
    "en": "I don't have a product page I can open directly right now, but I can help you order "
    "it step by step. Want me to?",
}
_VIEW_LABEL: dict[str, str] = {
    "ro": "Vezi produsul",
    "en": "View product",
}
# NX-137: eticheta CTA-ului de plată (Offer pe linkul de checkout creat în acest tur).
_CHECKOUT_LABEL: dict[str, str] = {
    "ro": "Finalizează comanda",
    "en": "Complete your order",
}


def _link_lead(language: str | None, *, many: bool) -> str:
    d = _LINK_LEAD_MANY if many else _LINK_LEAD_ONE
    return d.get(language or "ro") or d["ro"]


def _checkout_label(language: str | None) -> str:
    return _CHECKOUT_LABEL.get(language or "ro") or _CHECKOUT_LABEL["ro"]


def _no_link_msg(language: str | None) -> str:
    return _NO_LINK.get(language or "ro") or _NO_LINK["ro"]


def _view_label(language: str | None) -> str:
    return _VIEW_LABEL.get(language or "ro") or _VIEW_LABEL["ro"]


def _products_brief(products: list[dict[str, Any]], language: str | None = None) -> str:
    lines = []
    for p in products:
        summary = (p.get("ai_summary") or "")[:140]
        extra = ""
        if p.get("rating"):
            extra += f" | {float(p['rating']):.1f}★"
        if p.get("review_summary") or p.get("review_pro"):
            laud = p.get("review_pro") or (p.get("top_pros") or [""])[0]
            if laud:
                extra += f" | clienții laudă: {laud}"
        lines.append(
            f"- {p['name']} | brand: {p.get('brand') or '-'} | "
            f"preț: {amount_text(p['price'], language)} lei{extra} | "
            f"url: {p.get('url') or '-'} | {summary}"
        )
    return "\n".join(lines)


#: Câte produse NUMEȘTE în text fallback-ul determinist. Public fiindcă apelantul trebuie să
#: atașeze EXACT produsele numite: două plafoane independente au produs deja un răspuns în care
#: textul enumera 3 produse iar ecranul arăta 6 carduri, fără nicio legătură vizibilă între ele.
DETERMINISTIC_REPLY_MAX = 3


def grounded_fallback_reply(
    products: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]] | None:
    """Fallback-ul care PREZINTĂ faptele, nu doar refuză — `None` dacă n-avem niciun fapt.

    Calea v1 face asta de mult (`_finalize` → `_deterministic_reply`): când proza modelului pică
    validatorul, clientul primește totuși produsele REALE cu prețurile REALE, fiindcă serverul le
    are deja în mână. A răspunde „nu pot confirma" cu catalogul pe masă e o degradare pe care n-o
    cere nimic — și e cu atât mai proastă cu cât apare exact pe inputurile adversariale, adică
    acolo unde clientul vede diferența dintre un asistent și un zid.

    `None` (fără produse cu nume ȘI preț) rămâne cazul lui `safe_fallback`: fără fapte, forma de
    produs ar fi o promisiune goală.

    Întoarce ȘI produsele pe care textul chiar le numește, ca apelantul să nu fie nevoit să
    reconstruiască felia după aceeași regulă — vezi `DETERMINISTIC_REPLY_MAX`."""
    usable = [p for p in products if p.get("name") and p.get("price") is not None]
    if not usable:
        return None
    named = usable[:DETERMINISTIC_REPLY_MAX]
    return _deterministic_reply(usable), named


def _deterministic_reply(products: list[dict[str, Any]]) -> str:
    lines = ["Îți recomand:"]
    for p in products[:DETERMINISTIC_REPLY_MAX]:
        lines.append(f"• {display_name(p['name'])}, {amount_text(p['price'], 'ro')} lei")
    lines.append("Vrei detalii sau linkul la vreunul?")
    return "\n".join(lines)


def _card_variants(product: dict[str, Any], n: int = 16) -> list[dict[str, Any]]:
    """Compact variant payload for product cards (shade/size stock, color and attributes).

    NX-197: un selector cu O SINGURĂ opțiune nu e o alegere, e zgomot pe card. Modelul de date
    rămâne uniform („tot ce se vinde e o variantă", ca să existe SKU/gramaj/preț-per-unitate pe
    toate produsele), dar UI-ul primește selectorul doar când chiar are ce alege."""
    raws = product.get("variants") or []
    if len(raws) < 2:
        return []
    out: list[dict[str, Any]] = []
    for raw in raws[:n]:
        if not isinstance(raw, dict):
            continue
        variant_id = raw.get("variant_id") or raw.get("id")
        label = raw.get("label")
        if not variant_id or not label:
            continue
        item: dict[str, Any] = {"variant_id": str(variant_id), "label": label}
        if raw.get("price") is not None:
            item["price"] = float(raw["price"])
        if raw.get("list_price") is not None:
            item["list_price"] = float(raw["list_price"])
        if raw.get("stock") is not None:
            item["stock"] = int(raw["stock"])
        if raw.get("color_hex"):
            item["color_hex"] = raw["color_hex"]
        attrs = raw.get("attributes") if isinstance(raw.get("attributes"), dict) else {}
        compact_attrs = {
            k: attrs.get(k) for k in ("shade", "undertone", "depth") if attrs.get(k) is not None
        }
        for key in ("shade", "undertone", "depth"):
            if raw.get(key) is not None and key not in compact_attrs:
                compact_attrs[key] = raw[key]
        if compact_attrs:
            item["attributes"] = compact_attrs
        out.append(item)
    return out


def _card_products(products: list[dict[str, Any]], n: int | None = None) -> list[dict[str, Any]]:
    """Câmpuri compacte pentru cardurile de produs (W1 + carusel R2).

    `n=None` ⇒ `settings.card_slots`. Era `4` scris aici, iar apelantul de pe calea degradată
    (`finalize.render`) îl lăsa implicit: clientul vedea 4 carduri sub un text care numea 3
    (`DETERMINISTIC_REPLY_MAX`), pe un tenant configurat cu 6. NX-298 a declarat `card_slots`
    proprietar UNIC al cifrei „câte produse"; plafonul ăsta îi scăpase, fiindcă trăia pe ramura pe
    care nimeni n-o citește până nu cade ceva. Apelanții care cer explicit `n` (detaliu = 1,
    thin path = 6) numesc un plafon de CONTRACT, nu unul de produs, și rămân neatinși."""
    if n is None:
        n = card_slots()
    cards: list[dict[str, Any]] = []
    for p in products[:n]:
        card = {
            "product_id": p["id"],
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


def _dedupe(products: list[dict[str, Any]], cap: int = 6) -> list[dict[str, Any]]:
    """Produse unice (după id), ordine păstrată, max `cap` (principiul: ≤6 produse)."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for p in products:
        pid = p.get("id")
        if pid in seen:
            continue
        seen.add(pid)
        out.append(p)
        if len(out) >= cap:
            break
    return out
