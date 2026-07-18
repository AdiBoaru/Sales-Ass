"""NX-180 — gate-uri DETERMINISTE ale evaluatorului conversațional (funcții PURE, testabile).

Separat de `eval_run.py` (care conduce calea `/web/chat` reală) ca logica de verificare să fie
unit-testabilă FĂRĂ apeluri live: primește dict-uri `{content, products, suggestions, offer}` (exact
contractul widgetului, ca `Turn`) și întoarce lista de eșecuri. Judge-ul LLM (subiectiv) trăiește în
`eval_judge.py`; AICI stă doar ce se poate verifica determinist din contract.

Regula de aur (P2, review Codex runda 1): judge-ul NU poate anula un eșec determinist — de aceea
gate-urile sunt cod pur, nu prompt.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any


def norm(s: str) -> str:
    """lower + fără diacritice (NFKD) — potrivire robustă pe text RO scris fără diacritice."""
    d = unicodedata.normalize("NFKD", (s or "").lower())
    return "".join(c for c in d if not unicodedata.combining(c))


# Preț de PRODUS: doar tokeni cu 2 zecimale (89.99 / 89,99) — bugetul CLIENTULUI e rotund („sub 80")
# și NU se prinde aici (fără fals-pozitiv). Prinde prețurile inventate care imită formatul.
_PRICE_RE = re.compile(r"(?<!\d)(\d{1,4})[.,](\d{2})(?!\d)")
_URL_RE = re.compile(r"https?://\S+")


def price_tokens(content: str) -> set[float]:
    """Sumele cu 2 zecimale din text (candidate de preț de produs), ca float rotunjit la 2dp."""
    out: set[float] = set()
    for whole, frac in _PRICE_RE.findall(content or ""):
        out.add(round(float(f"{whole}.{frac}"), 2))
    return out


def _product_prices(products: list[dict[str, Any]]) -> set[float]:
    prices: set[float] = set()
    for p in products:
        for key in ("price", "list_price", "sale_price"):
            v = p.get(key)
            if isinstance(v, (int, float)):
                prices.add(round(float(v), 2))
    return prices


def opening(content: str) -> str:
    """Prima propoziție normalizată (pt gate-ul cross-tur anti-repetiție a deschiderii)."""
    first = re.split(r"[.!?\n]", (content or "").strip(), maxsplit=1)[0]
    return norm(first).strip()


def _names(products: list[dict[str, Any]]) -> list[str]:
    return [str(p.get("name") or p.get("title") or "") for p in products]


def _ids(products: list[dict[str, Any]]) -> set[str]:
    return {
        str(p.get("product_id") or p.get("id") or "")
        for p in products
        if p.get("product_id") or p.get("id")
    }


def check_turn(cur: dict[str, Any], prev: dict[str, Any] | None, spec: dict[str, Any]) -> list[str]:
    """Întoarce lista de coduri de eșec determinist pentru un tur, față de `spec` (din fixture).

    `cur`/`prev` = dict-uri de contract widget: {content, products, suggestions, offer}.
    Spec-ul acceptă (toate opționale):
      not_empty · min_cards · max_cards · name_forbidden_substr · content_forbidden_substr ·
      content_required_substr · no_new_cards · grounded · max_chip_len
    """
    fails: list[str] = []
    content = str(cur.get("content") or "")
    products = list(cur.get("products") or [])
    suggestions = list(cur.get("suggestions") or [])

    # P6: niciodată tăcere — content SAU carduri.
    if spec.get("not_empty", True) and not content.strip() and not products:
        fails.append("empty_reply")

    if "min_cards" in spec and len(products) < int(spec["min_cards"]):
        fails.append(f"too_few_cards:{len(products)}<{spec['min_cards']}")
    if "max_cards" in spec and len(products) > int(spec["max_cards"]):
        fails.append(f"too_many_cards:{len(products)}>{spec['max_cards']}")

    nc = norm(content)
    for bad in spec.get("content_forbidden_substr", []):
        if norm(bad) in nc:
            fails.append(f"content_forbidden:{bad}")
    for need in spec.get("content_required_substr", []):
        if norm(need) not in nc:
            fails.append(f"content_missing:{need}")

    for bad in spec.get("name_forbidden_substr", []):
        if any(norm(bad) in norm(n) for n in _names(products)):
            fails.append(f"name_forbidden:{bad}")

    # Grounding (#234): preț inventat (2 zecimale) ȘI link inventat în text.
    if spec.get("grounded", True):
        allowed = _product_prices(products)
        for tok in price_tokens(content):
            if tok not in allowed:
                fails.append(f"ungrounded_price:{tok}")
        # orice URL din PROZĂ trebuie să fie al unui produs afișat SAU al offer-ului (checkout).
        # Un link inventat / către alt produs = eșec. (Offer-ul de checkout are URL legit propriu.)
        offer = cur.get("offer") if isinstance(cur.get("offer"), dict) else {}
        offer_url = str(offer.get("url")) if offer and offer.get("url") else None
        allowed_urls = {str(p.get("url")) for p in products if p.get("url")}
        if offer_url:
            allowed_urls.add(offer_url)
        for raw in _URL_RE.findall(content):
            if raw.rstrip(").,;!?") not in allowed_urls:
                fails.append("ungrounded_link")

    # Follow-up direct: fără carduri NOI — nu re-lista la „care e mai lejeră?".
    if spec.get("no_new_cards") and prev is not None:
        new_ids = _ids(products) - _ids(list(prev.get("products") or []))
        if new_ids:
            fails.append(f"new_cards_on_followup:{len(new_ids)}")

    # Chip = etichetă tappabilă, nu propoziție.
    max_chip = int(spec.get("max_chip_len", 40))
    for s in suggestions:
        if len(str(s)) > max_chip:
            fails.append(f"chip_too_long:{len(str(s))}")

    return fails


def opening_repeated(cur: dict[str, Any], prev: dict[str, Any] | None) -> bool:
    """True dacă deschiderea (prima propoziție) e IDENTICĂ cu a turului anterior — gate cross-tur.
    Ignoră deschideri goale/foarte scurte (< 8 caractere normalizate: „da", „sigur")."""
    if prev is None:
        return False
    a, b = opening(str(cur.get("content") or "")), opening(str(prev.get("content") or ""))
    return bool(a) and len(a) >= 8 and a == b
