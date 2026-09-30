"""NX-360 — citirea paginii LIVE a unui produs SOLE: disponibilitate și preț. PUR: zero I/O.

De ce un al doilea cititor, lângă `sole_source` (care citește exportul scraperului): cele 391 de
produse `out_of_stock` din import aveau prețuri cu bani (135,92, 93,41), iar 3 din 7 verificate
erau de fapt în stoc. Cauza, măsurată pe pagini reale pe 2026-09-30:

  - prețul de LISTĂ e doar în rândul de preț al paginii (`.product-price-row__value`, „120 lei”);
  - `offers.price` din JSON-LD e prețul cu voucherul de bun venit (102,00058 = 120 × 0,85);
  - pe o pagină EPUIZATĂ rândul de preț lipsește, iar JSON-LD are `OutOfStock` + prețul cu voucher.

Scraperul original cădea pe `offers.price` când rândul lipsea, deci fiecare produs epuizat a intrat
în catalog cu prețul voucherului drept preț de listă. Regula de aici: prețul de listă vine DOAR din
rândul de preț; fără rând, prețul e NECUNOSCUT (`None`), niciodată dedus. Disponibilitatea vine din
JSON-LD (`schema.org/*`), cu vocabular închis; o valoare necunoscută e tot `None`, nu o ghicire.

Parserul folosește doar biblioteca standard: `bs4` nu e dependință a imaginii (hash-locked, NX-248).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from decimal import Decimal
from html.parser import HTMLParser

from src.catalog.sole_source import PriceFacts, _money, parse_price

#: `schema.org/ItemAvailability` → CHECK-ul nostru. Ce nu e aici rămâne necunoscut.
_SCHEMA_AVAILABILITY = {
    "instock": "in_stock",
    "limitedavailability": "in_stock",
    "onlineonly": "in_stock",
    "outofstock": "out_of_stock",
    "soldout": "out_of_stock",
    "discontinued": "out_of_stock",
}

#: Clasele din pagina SOLE care poartă prețurile afișate (aceleași ca în scraperul original).
PRICE_ROW_CLASS = "product-price-row__value"
PROMO_CLASS = "product-welcome-price__value"
PROMO_CODE_CLASS = "product-welcome-price__code"

#: Primul număr scris în pagină, cu separatorii lui («1900», «76.50», «1.299,90»). Formatul real
#: SOLE, verificat pe pagini (recenzia NX-360): prețul de listă FĂRĂ separator de mii («1900 lei»),
#: iar bănuții voucherului într-un tag separat (`76<small>.50</small>`, lipit fără spațiu).
_AMOUNT = re.compile(r"\d[\d.,]*")
#: Codul voucherului: un singur token alfanumeric (WELCOME15).
_CODE = re.compile(r"\b([A-Z][A-Z0-9]{3,})\b")


@dataclass(frozen=True, slots=True)
class LivePage:
    """Ce spune pagina acum. `None` = pagina nu spune (niciodată o valoare plauzibilă)."""

    availability: str | None
    price_regular: Decimal | None
    price_promo: Decimal | None
    promo_code: str | None
    #: `offers.price` din JSON-LD: doar pentru diagnoză (e prețul cu voucher), nu se scrie.
    ld_price: Decimal | None
    is_product: bool

    def price_facts(self) -> PriceFacts | None:
        """Prețul comercial, prin ACEEAȘI regulă ca importul (`parse_price`). Fără rândul de preț
        (pagina epuizată) ⇒ `None`: prețul de listă nu se poate afla, deci nu se scrie."""
        if self.price_regular is None:
            return None
        if self.price_promo is not None and self.promo_code is None:
            # Blocul „welcome price” e MEREU un voucher. Fără cod recunoscut, `parse_price` l-ar
            # scrie ca reducere pentru oricine (`sale_price`); îl lăsăm deoparte și îl semnalăm
            # (recenzia finală NX-360).
            facts = parse_price(self.price_regular, None, None)
            if facts is None:
                return None
            return replace(facts, anomalies=(*facts.anomalies, "voucher_without_code"))
        return parse_price(self.price_regular, self.price_promo, self.promo_code)


def parse_amount(text: str | None) -> Decimal | None:
    """«1900 lei» → 1900, «76.50 lei» → 76.50, «1.299,90 lei» → 1299.90. PUR; fără sumă ⇒ None.

    Separatorul zecimal e cel care are după el 1-2 cifre la final; celălalt (sau același, urmat de
    exact 3 cifre) e separator de mii. Recenzia NX-360: o primă variantă tăia «1900» la «190»."""
    if not text:
        return None
    # Orice spațiu, inclusiv cel neîntrerupt (`\xa0`, `\u202f`) ca separator de mii.
    m = _AMOUNT.search(re.sub(r"\s+", "", text))
    if not m:
        return None
    token = m.group(0).rstrip(".,")
    last = max(token.rfind("."), token.rfind(","))
    if last >= 0 and 1 <= len(token) - last - 1 <= 2:
        whole, cents = token[:last], token[last + 1 :]
    else:
        whole, cents = token, "0"
    whole = whole.replace(".", "").replace(",", "")
    if not whole.isdigit() or not cents.isdigit():
        return None
    return _money(Decimal(f"{whole}.{cents.ljust(2, '0')}"))


def availability_from_schema(raw: object) -> str | None:
    """`http://schema.org/InStock` → `in_stock`. Necunoscut ⇒ None."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    key = raw.strip().rstrip("/").rsplit("/", 1)[-1].lower()
    return _SCHEMA_AVAILABILITY.get(key)


class _PageParser(HTMLParser):
    """Colectează blocurile JSON-LD și textul elementelor cu clasele de preț."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ld_blocks: list[str] = []
        self.texts: dict[str, list[str]] = {}
        self._in_ld = False
        self._ld_buf: list[str] = []
        #: Clasele de preț deschise, cu adâncimea la care s-au deschis.
        self._open: list[tuple[str, int]] = []
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "script":
            self._in_ld = (a.get("type") or "").lower() == "application/ld+json"
            self._ld_buf = []
            return
        self._depth += 1
        classes = set((a.get("class") or "").split())
        for cls in (PRICE_ROW_CLASS, PROMO_CLASS, PROMO_CODE_CLASS):
            if cls in classes and cls not in self.texts:
                self.texts[cls] = []
                self._open.append((cls, self._depth))

    def handle_endtag(self, tag):
        if tag == "script":
            if self._in_ld:
                self.ld_blocks.append("".join(self._ld_buf))
            self._in_ld = False
            return
        self._open = [(c, d) for c, d in self._open if d < self._depth]
        self._depth = max(0, self._depth - 1)

    def handle_data(self, data):
        if self._in_ld:
            self._ld_buf.append(data)
            return
        for cls, _ in self._open:
            self.texts[cls].append(data)


def _product_offer(blocks: list[str]) -> tuple[dict | None, bool]:
    for raw in blocks:
        try:
            doc = json.loads(raw)
        except ValueError:
            continue
        for block in doc if isinstance(doc, list) else [doc]:
            if isinstance(block, dict) and block.get("@type") == "Product":
                offers = block.get("offers")
                if isinstance(offers, list):
                    offers = offers[0] if offers else None
                return (offers if isinstance(offers, dict) else None), True
    return None, False


def parse_page(html: str) -> LivePage:
    """Pagina de produs SOLE → `LivePage`. PUR."""
    parser = _PageParser()
    parser.feed(html or "")
    offer, is_product = _product_offer(parser.ld_blocks)

    def text(cls: str, sep: str = "") -> str | None:
        parts = parser.texts.get(cls)
        # Prețurile se lipesc FĂRĂ spațiu, ca `get_text(strip=True)` din scraperul original:
        # `76<small>.50` trebuie să rămână «76.50», nu «76 .50» (recenzia NX-360). Textul codului
        # se lipește CU spațiu: «cu codul <b>WELCOME15</b>» lipit ar fi «cucodulWELCOME15», codul
        # n-ar mai fi găsit, iar voucherul s-ar scrie ca reducere pentru oricine (`sale_price`).
        joined = sep.join(p.strip() for p in parts) if parts else ""
        return joined.strip() or None

    code_text = text(PROMO_CODE_CLASS, sep=" ")
    code = _CODE.search(code_text) if code_text else None
    ld_price = None
    if offer is not None and offer.get("price") is not None:
        try:
            ld_price = _money(Decimal(str(offer.get("price"))))
        except ArithmeticError:
            ld_price = None
    return LivePage(
        availability=availability_from_schema(offer.get("availability") if offer else None),
        price_regular=parse_amount(text(PRICE_ROW_CLASS)),
        price_promo=parse_amount(text(PROMO_CLASS)),
        promo_code=code.group(1) if code else None,
        ld_price=ld_price,
        is_product=is_product,
    )


@dataclass(frozen=True, slots=True)
class Refresh:
    """Ce se schimbă pe un produs. `None` pe un câmp = nu se atinge."""

    product_id: str
    availability: str | None
    price: PriceFacts | None
    #: Vocabular închis: `ok` | `gone` (nu mai e pagină de produs) | `unknown_availability` |
    #: `in_stock_without_price` (pagina zice în stoc, dar nu arată prețul: nu se scrie nimic).
    status: str
    #: Produsul rămâne (sau devine) epuizat, iar prețul lui de listă nu se poate afla.
    price_unverified: bool


def same_product_url(requested: str, final: str) -> bool:
    """Redirecționarea a rămas pe ACELAȘI produs. sole.ro redirecționează `.../x.html` spre `.../x`
    (301), ceea ce e normal; un produs retras poate însă redirecționa spre alt produs, iar prețul
    acela nu e al nostru. Se compară ultimul segment al căii, fără `.html` și fără `/`. PUR."""

    def slug(url: str) -> str:
        path = url.split("?", 1)[0].split("#", 1)[0].rstrip("/")
        last = path.rsplit("/", 1)[-1]
        return last[:-5] if last.endswith(".html") else last

    return slug(requested) == slug(final)


def decide(product_id: str, page: LivePage) -> Refresh:
    """Pagina live → schimbarea de scris. PUR. Nimic nu se deduce: ce pagina nu spune, rămâne."""
    if not page.is_product:
        return Refresh(product_id, None, None, "gone", price_unverified=False)
    if page.availability is None:
        return Refresh(product_id, None, None, "unknown_availability", price_unverified=False)
    facts = page.price_facts()
    if page.availability == "in_stock" and facts is None:
        # Recenzia NX-360: trecut în stoc cu prețul vechi (al voucherului, pe cele 391 epuizate),
        # produsul ar deveni vandabil exact la prețul pe care cardul îl repară.
        return Refresh(product_id, None, None, "in_stock_without_price", price_unverified=True)
    return Refresh(
        product_id,
        page.availability,
        facts,
        "ok",
        price_unverified=facts is None,
    )
