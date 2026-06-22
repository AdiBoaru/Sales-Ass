"""Randor web UNIC (NX-127) — o singură sursă de adevăr pentru `{content, products,
suggestions, offer?}`, partajată de calea SINCRONĂ (`/web/chat` → `_build_chat_response`) și
de cea ASYNC (outbox → dispatcher → `WebSender.send_rich/send_products` → SSE).

Înainte de NX-127 randarea bogată exista DOAR pe ruta sincronă; pe SSE cardurile/chips-urile
cădeau tăcut la text. Acum ambele rute apelează `render_web(reply, language)` → paritate de UX
între rute pe ACELAȘI canal. Pur: fără I/O, fără DB. Cuplajul de canal stă la margine (P: canal
doar la margini); pipeline-ul rămâne agnostic.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from src.models import Chip, Offer, Reply, RichItem, RichReply
from src.worker.compose import ensure_disclaimer, flatten_framing

if TYPE_CHECKING:
    pass


def _card(
    name: Any,
    price: Any,
    image: Any = None,
    url: Any = None,
    *,
    product_id: Any = None,
    rating: Any = None,
    reason: Any = None,
) -> dict[str, Any]:
    """Un card de produs pt widget. Câmpuri compacte (P8); `image_url`/`url`/`rating`/`reason`
    lipsesc dacă datele nu există (NU inventăm `null`-uri). Frontendul randează ce primește."""
    card: dict[str, Any] = {"product_id": product_id, "name": name, "price": price}
    if image:
        card["image_url"] = image
    if url:
        card["url"] = url
    if rating:
        card["rating"] = rating
    if reason:
        card["reason"] = reason
    return card


def render_web(reply: Reply | None, language: str) -> dict[str, Any]:
    """`Reply` semantic → contractul widget-ului `{content, products, suggestions, offer?}`.

    RICH (recomandare cu carduri): `content` = DOAR framing-ul conversațional (`flatten_framing`:
    intro + pick + educație + disclaimer), NU enumerarea — o fac cardurile (`products`) și butoanele
    (`suggestions`). Reply simplu (text/produse fără rich): `content` = `reply.text`. Disclaimer-ul
    AI re-aplicat idempotent pe `language`. `Reply.offer` (NX-114) → câmp `offer` opțional (buton).
    Reply gol (handoff tăcut / degradare) → content gol, fără carduri (frontendul dă fallback)."""
    if reply is None:
        return {"content": "", "products": [], "suggestions": []}
    lang = language or "ro"
    products: list[dict[str, Any]] = []
    suggestions: list[str] = []
    if reply.rich is not None:
        products = [
            _card(
                it.name,
                it.price,
                it.image,
                it.url,
                product_id=it.product_id,
                rating=it.rating,
                reason=it.reason,
            )
            for it in reply.rich.items
        ]
        suggestions = [c.label for c in reply.rich.chips]
        content = ensure_disclaimer(flatten_framing(reply.rich), lang)
    elif reply.products:
        products = [
            _card(
                p.get("name"),
                p.get("price"),
                p.get("image"),
                p.get("url"),
                product_id=p.get("product_id"),
            )
            for p in reply.products
        ]
        content = ensure_disclaimer(reply.text, lang)
    else:
        content = ensure_disclaimer(reply.text, lang)
    out: dict[str, Any] = {"content": content, "products": products, "suggestions": suggestions}
    offer = getattr(reply, "offer", None)
    if offer is not None:
        # Web = buton tappabil; pe WhatsApp/Telegram același offer e CTA/text (floor aplatizat).
        out["offer"] = {"kind": offer.kind, "label": offer.label}
        if offer.url:
            out["offer"]["url"] = offer.url
        if offer.payload:
            out["offer"]["payload"] = offer.payload
    return out


def reply_from_outbox(payload: dict[str, Any]) -> Reply:
    """Reconstruiește `Reply` din payload-ul de outbox (calea async) ca să-l treacă prin ACELAȘI
    `render_web`. `payload["rich"]` = `asdict(RichReply)`; `payload["products"]` / `payload["text"]`
    pe ruta carduri/text. Simetric cu `asdict` (chei ⇔ câmpuri); `pick` revine tuple din listă."""
    rich: RichReply | None = None
    rd = payload.get("rich")
    if isinstance(rd, dict):
        pick = rd.get("pick")
        rich = RichReply(
            intro=rd.get("intro"),
            items=[RichItem(**it) for it in (rd.get("items") or []) if isinstance(it, dict)],
            pick=tuple(pick) if pick else None,
            education=rd.get("education"),
            chips=[Chip(**c) for c in (rd.get("chips") or []) if isinstance(c, dict)],
            disclaimer=rd.get("disclaimer") or "",
        )
    offer: Offer | None = None
    od = payload.get("offer")
    if isinstance(od, dict) and od.get("kind") and od.get("label"):
        offer = Offer(
            kind=od["kind"], label=od["label"], url=od.get("url"), payload=od.get("payload")
        )
    return Reply(
        text=payload.get("text") or "",
        rich=rich,
        products=payload.get("products"),
        offer=offer,
    )
