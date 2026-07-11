"""Replici deterministe + utilitare pure ale stagiului agent (NX-143, faza „fallbacks").

Zero LLM, zero `TurnContext`/`deps`/DB — doar `str`/`dict` in, `str`/`list`/`dict` out. Aici
trăiesc mesajele per-locale (niciodată tăcere, P6) + micile transformări de produse (dedup, câmpuri
de card, brief pt prompt). Consumate de `deterministic.py` (intenții pre-loop), `planner.py`
(shaping) și `finalize.py` (render). Un singur loc pentru textele deterministe (traducere: NX-156).
"""

from __future__ import annotations

from typing import Any

# Mesaj determinist când NU există nimic mai ieftin (niciodată tăcere/padding, P6). Per-locale.
_CHEAPEST_ALREADY: dict[str, str] = {
    "ro": "Momentan asta e cea mai ieftină opțiune pe care o am pentru tine. "
    "Vrei să-ți arăt altceva sau o altă categorie?",
    "en": "This is the cheapest option I have right now. "
    "Want me to show you something else or another category?",
    "hu": "Jelenleg ez a legolcsóbb lehetőség, amim van. Mutassak mást vagy egy másik kategóriát?",
}


def _cheapest_already_msg(language: str | None) -> str:
    return _CHEAPEST_ALREADY.get(language or "ro") or _CHEAPEST_ALREADY["ro"]


# NX-159 felia 2: chips deterministe de CONTINUARE pentru căile subțiri (no-result / cheapest-
# already). Voce de client (reintră ca tur nou: „Schimbă bugetul" → cheaper/refine). Căi CONCRETE,
# nu fundătură generică. Per-locale. GENERIC pe vertical (formularea nu e specifică beauty).
_THIN_PATH_CHIPS: dict[str, list[str]] = {
    "ro": ["Arată-mi ce e popular", "Schimbă bugetul", "Caut altă categorie"],
    "en": ["Show me what's popular", "Change the budget", "Look in another category"],
    "hu": ["Mutasd a népszerűeket", "Módosítsd a keretet", "Másik kategória"],
}


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
    "en": "Done — I added {name} to your cart 🛒 Here's what pairs well with it:",
    "hu": "Kész, betettem a kosaradba: {name} 🛒 Íme, ami jól illik hozzá:",
}
_CROSS_SELL_QUERY: dict[str, str] = {
    "ro": "Clientul tocmai a adăugat în coș «{name}». Recomandă produsele de mai jos ca fiind "
    "COMPLEMENTARE (merg bine împreună / completează rutina sau alegerea), NU ca alternative. "
    "Pentru fiecare, spune SCURT de ce se potrivește cu «{name}».",
    "en": "The customer just added «{name}» to the cart. Recommend the products below as "
    "COMPLEMENTARY (they pair well / complete the routine or choice), NOT as alternatives. For "
    "each, briefly say why it fits with «{name}».",
    "hu": "Az ügyfél most tette a kosárba: «{name}». Ajánld az alábbi termékeket KIEGÉSZÍTŐKÉNT "
    "(jól illenek együtt / kiegészítik a választást), NEM alternatívaként. Mindegyiknél mondd el "
    "röviden, miért illik «{name}»-hez.",
}


def _cart_confirm_msg(added: dict[str, Any], language: str | None) -> str:
    tmpl = _CART_CONFIRM.get(language or "ro") or _CART_CONFIRM["ro"]
    return tmpl.format(name=added.get("name") or "produsul")


def _cross_sell_query(added: dict[str, Any], language: str | None) -> str:
    tmpl = _CROSS_SELL_QUERY.get(language or "ro") or _CROSS_SELL_QUERY["ro"]
    return tmpl.format(name=added.get("name") or "produsul")


# IZI-compare: chips deterministe pe un tabel comparativ (voce de client → reintră ca tur nou:
# „Adaugă X" → cart_add; „Ceva mai ieftin" → cheaper). Etichete per-locale (text UI, nu rutare).
_ADD_LABEL: dict[str, str] = {"ro": "Adaugă", "en": "Add", "hu": "Hozzáad"}
_CHEAPER_CHIP: dict[str, str] = {
    "ro": "Ceva mai ieftin",
    "en": "Something cheaper",
    "hu": "Valami olcsóbb",
}


def _compare_chips(columns: list[Any], language: str | None) -> list[str]:
    """Follow-up-uri deterministe după o comparație: „Adaugă <produs>" pentru primele 2 + „mai
    ieftin". Numele lungi se scurtează (butonul are limită). Voce de client (fără scrub)."""
    lang = language or "ro"
    add = _ADD_LABEL.get(lang) or _ADD_LABEL["ro"]
    chips: list[str] = []
    for c in columns[:2]:
        name = c.name if len(c.name) <= 28 else c.name[:27].rstrip() + "…"
        chips.append(f"{add} {name}")
    chips.append(_CHEAPER_CHIP.get(lang) or _CHEAPER_CHIP["ro"])
    return chips


# Pool epuizat pe „mai arată-mi" → mesaj determinist per-locale (P6, fără tăcere; cacheable=False
# fiindcă e relativ la sesiunea ACESTUI contact — un cache hit l-ar servi altui context).
_NO_MORE_RESULTS: dict[str, str] = {
    "ro": "Astea sunt toate opțiunile pe care le am pe criteriile astea. "
    "Vrei să căutăm altceva sau să schimbăm filtrele?",
    "en": "That's everything I have for these criteria. "
    "Want to search for something else or adjust the filters?",
    "hu": "Ez minden, amim ezekre a feltételekre van. Keressünk mást vagy módosítsuk a szűrőket?",
}


def _no_more_msg(language: str | None) -> str:
    return _NO_MORE_RESULTS.get(language or "ro") or _NO_MORE_RESULTS["ro"]


# Lead-uri SCURTE per-locale pt răspunsul de link (linkul REAL vine ca Offer(open_url)/card, NU în
# proză — validatorul ar respinge un url inventat oricum). Unul vs mai multe produse țintă.
_LINK_LEAD_ONE: dict[str, str] = {
    "ro": "Sigur! 🙂 Uite linkul direct 👇",
    "en": "Sure! 🙂 Here's the direct link 👇",
    "hu": "Persze! 🙂 Itt a közvetlen link 👇",
}
_LINK_LEAD_MANY: dict[str, str] = {
    "ro": "Sigur! Uite linkurile direct la produsele de mai sus 👇",
    "en": "Sure! Here are the direct links to the products above 👇",
    "hu": "Persze! Itt a fenti termékek közvetlen linkjei 👇",
}
# product_url absent (gaură de date pe demo) → ONEST, fără link inventat (PP-F4). Channel-neutru
# (pipeline-ul nu știe de „butonul Adaugă" al web-ului); oferim pasul care EXISTĂ. cacheable=False.
_NO_LINK: dict[str, str] = {
    "ro": "Momentan nu am o pagină de produs pe care să ți-o deschid direct, dar te pot ajuta "
    "să-l comanzi pas cu pas. Vrei?",
    "en": "I don't have a product page I can open directly right now, but I can help you order "
    "it step by step. Want me to?",
    "hu": "Most nincs külön termékoldalam, amit közvetlenül megnyithatnék, de segíthetek "
    "lépésről lépésre megrendelni. Szeretnéd?",
}
_VIEW_LABEL: dict[str, str] = {
    "ro": "Vezi produsul",
    "en": "View product",
    "hu": "Termék megtekintése",
}
# NX-137: eticheta CTA-ului de plată (Offer pe linkul de checkout creat în acest tur).
_CHECKOUT_LABEL: dict[str, str] = {
    "ro": "Finalizează comanda",
    "en": "Complete your order",
    "hu": "Rendelés befejezése",
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


def _products_brief(products: list[dict[str, Any]]) -> str:
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
            f"preț: {float(p['price']):.2f} lei{extra} | url: {p.get('url') or '-'} | {summary}"
        )
    return "\n".join(lines)


def _deterministic_reply(products: list[dict[str, Any]]) -> str:
    lines = ["Îți recomand:"]
    for p in products[:3]:
        lines.append(f"• {p['name']} — {float(p['price']):.2f} lei")
    lines.append("Vrei detalii sau linkul la vreunul?")
    return "\n".join(lines)


def _card_variants(product: dict[str, Any], n: int = 16) -> list[dict[str, Any]]:
    """Compact variant payload for product cards (shade/size stock, color and attributes)."""
    out: list[dict[str, Any]] = []
    for raw in (product.get("variants") or [])[:n]:
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


def _card_products(products: list[dict[str, Any]], n: int = 4) -> list[dict[str, Any]]:
    """Câmpuri compacte pentru cardurile de produs (W1 + carusel R2)."""
    cards: list[dict[str, Any]] = []
    for p in products[:n]:
        card = {
            "product_id": p["id"],
            "name": p["name"],
            "price": float(p["price"]),
            "url": p.get("url"),
            "image": p.get("image"),
        }
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
