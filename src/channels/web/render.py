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

from src.models import (
    Chip,
    Comparison,
    ComparisonColumn,
    ComparisonRow,
    Offer,
    Reply,
    RichItem,
    RichReply,
)
from src.worker.compose import ensure_disclaimer, flatten_framing

if TYPE_CHECKING:
    pass


# Web = UI premium: max 4 chips ca butoane (widget-ul nu trebuie să pară încărcat). Chat-urile
# (WhatsApp/Telegram) rămân pe _MAX_CHIPS din compose — capul ăsta e DOAR pe render-ul web.
_MAX_WEB_CHIPS = 4
# Un chip e o ETICHETĂ tappabilă, nu o propoziție: pe calea clarify, nano poate genera „chips" care
# sunt de fapt întrebări lungi cu paranteze (ex. „Imi poti spune ce tip de produs cauti? (ex: …)")
# — rup UI-ul widgetului. Un chip mai lung decât atât nu e chip: îl DROPĂM (mai bine 0 chips decât
# unul malformat). Pragul lasă loc de „Adaugă {nume produs}" (~35), dar taie întrebările (60+).
_MAX_WEB_CHIP_LEN = 40


def _web_chips(labels: list[str]) -> list[str]:
    """Sanitizează chips-urile pentru contractul widgetului: strip, drop goale + prea lungi (nu-s
    chip-uri), cap la _MAX_WEB_CHIPS. Structural, nu prin disciplina promptului (P4)."""
    out: list[str] = []
    for raw in labels:
        s = (raw or "").strip()
        if s and len(s) <= _MAX_WEB_CHIP_LEN:
            out.append(s)
        if len(out) >= _MAX_WEB_CHIPS:
            break
    return out


def _card(
    name: Any,
    price: Any,
    image: Any = None,
    url: Any = None,
    *,
    product_id: Any = None,
    rating: Any = None,
    reason: Any = None,
    review_count: Any = None,
    badge: Any = None,
    list_price: Any = None,
    badge_tone: Any = None,
    currency: Any = None,
    details: Any = None,
    variants: Any = None,
) -> dict[str, Any]:
    """Un card de produs pt widget. Câmpuri compacte (P8); cheile lipsesc dacă datele nu există
    (NU inventăm `null`-uri). Frontendul randează ce primește. `price` = prețul CURENT; `list_price`
    (preț original tăiat) se emite DOAR când e strict peste `price` (reducere reală); `review_count`
    doar > 0; `badge` doar curat (data-gated în compose).

    Full-eMAG (contract FE extins, aditiv): `badges:[{label,tone}]` (păstrăm și `badge` string pt
    FE-ul de bază); `currency`; `details` (descriere extinsă „Spune-mi mai multe"). Absent → cheia
    lipsește (degradare grațioasă). Vezi docs/FRONTEND-CONTRACT-IZI.md + fixturile FE."""
    card: dict[str, Any] = {"product_id": product_id, "name": name, "price": price}
    if image:
        card["image_url"] = image
    if url:
        card["url"] = url
    if rating:
        card["rating"] = rating
    if reason:
        card["reason"] = reason
    if review_count and review_count > 0:
        card["review_count"] = review_count
    if badge:
        card["badge"] = badge  # legacy (FE de bază citește string-ul)
        card["badges"] = [{"label": badge, "tone": badge_tone or "info"}]  # Full-eMAG (cu ton)
    if list_price and price and list_price > price:
        card["list_price"] = list_price
    if currency:
        card["currency"] = currency
    if details:
        card["details"] = details
    if variants:
        card["variants"] = variants
    return card


def _comparison_payload(cmp: Comparison) -> dict[str, Any]:
    """Forma de CONTRACT FRONTEND a tabelului: coloane (un produs/coloană) + rânduri (o
    dimensiune/rând, `values` aliniat 1:1 cu coloanele; `null` = celulă lipsă → „—"). Cheile
    opționale lipsesc dacă datele nu există. Vezi docs/FRONTEND-CONTRACT-IZI.md."""
    columns: list[dict[str, Any]] = []
    for c in cmp.columns:
        col: dict[str, Any] = {"product_id": c.product_id, "name": c.name, "price": c.price}
        if c.list_price is not None and c.price and c.list_price > c.price:
            col["list_price"] = c.list_price
        if c.image:
            col["image_url"] = c.image
        if c.url:
            col["url"] = c.url
        if c.rating is not None:
            col["rating"] = c.rating
        columns.append(col)
    rows = [{"label": r.label, "values": list(r.values)} for r in cmp.rows]
    return {"columns": columns, "rows": rows}


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
    suggestions: list[str] = _web_chips(
        reply.suggestions
    )  # non-rich (ex. clarify): chips de pe reply
    extra: dict[str, Any] = {}  # câmpuri suplimentare de contract (ex. `comparison`)
    if reply.comparison is not None:
        # IZI-compare: tabel structurat. `content` = DOAR lead-ul (tabelul îl randează frontendul
        # din `comparison`); cardurile = produsele comparate (header poză+preț). Vezi spec FE.
        cmp = reply.comparison
        products = [
            _card(
                c.name,
                c.price,
                c.image,
                c.url,
                product_id=c.product_id,
                rating=c.rating,
                list_price=c.list_price,
            )
            for c in cmp.columns
        ]
        extra["comparison"] = _comparison_payload(cmp)
        content = ensure_disclaimer(cmp.intro or "", lang)
    elif reply.rich is not None:
        products = [
            _card(
                it.name,
                it.price,
                it.image,
                it.url,
                product_id=it.product_id,
                rating=it.rating,
                reason=it.reason,
                review_count=it.review_count,
                badge=it.badge,
                list_price=getattr(it, "list_price", None),
                badge_tone=getattr(it, "badge_tone", None),
                currency=getattr(it, "currency", None),
                details=getattr(it, "details", None),
                variants=getattr(it, "variants", None),
            )
            for it in reply.rich.items
        ]
        suggestions = _web_chips([c.label for c in reply.rich.chips])
        content = ensure_disclaimer(flatten_framing(reply.rich, lang), lang)
    elif reply.products:
        products = [
            _card(
                p.get("name"),
                p.get("price"),
                p.get("image"),
                p.get("url"),
                product_id=p.get("product_id"),
                variants=p.get("variants"),
            )
            for p in reply.products
        ]
        content = ensure_disclaimer(reply.text, lang)
    else:
        content = ensure_disclaimer(reply.text, lang)
    out: dict[str, Any] = {
        "content": content,
        "products": products,
        "suggestions": suggestions,
        **extra,
    }
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
    comparison: Comparison | None = None
    cd = payload.get("comparison")
    if isinstance(cd, dict):
        cols = [
            ComparisonColumn(
                product_id=col.get("product_id"),
                name=col.get("name"),
                price=col.get("price"),
                list_price=col.get("list_price"),
                image=col.get("image"),
                url=col.get("url"),
                rating=col.get("rating"),
            )
            for col in (cd.get("columns") or [])
            if isinstance(col, dict)
        ]
        rows = [
            ComparisonRow(label=r.get("label") or "", values=list(r.get("values") or []))
            for r in (cd.get("rows") or [])
            if isinstance(r, dict)
        ]
        comparison = Comparison(columns=cols, rows=rows, intro=cd.get("intro"))
    offer: Offer | None = None
    od = payload.get("offer")
    if isinstance(od, dict) and od.get("kind") and od.get("label"):
        offer = Offer(
            kind=od["kind"], label=od["label"], url=od.get("url"), payload=od.get("payload")
        )
    return Reply(
        text=payload.get("text") or "",
        rich=rich,
        comparison=comparison,
        products=payload.get("products"),
        offer=offer,
    )
