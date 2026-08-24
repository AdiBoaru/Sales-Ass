"""Tool-uri de comerț (F2 + NX-79/80) — WRITE tools ale agentului, cod determinist.

`checkout_link(cart_items)` construiește un link de cumpărare cu `?ref=<ref_code>` și scrie
un rând în `checkout_links` (ancora de atribuire). `cart_add` acumulează coșul în
`conversations.state` (ref-uri, pas intermediar înainte de checkout). `reorder` propune
re-comanda ultimei comenzi a contactului. `subscribe_back_in_stock` (NX-80) abonează la
notificare la restock (citit de proactiv, NX-70). Toate scoped pe `ctx.business.id` (modelul
NU primește business_id) și validate contra catalogului — nu acumulăm/linkuim produse
inexistente. Prețuri/linkuri grounded în `ToolResult` → validatorul (stagiul 8) le acceptă.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from src.analytics.demand import clean_ids
from src.commerce.cart_models import CART_MAX_LINE_QUANTITY, CartCommand, MutationOutcome
from src.commerce.cart_service import CartService, tool_idempotency_key
from src.config import get_settings
from src.db.provider import db_tx
from src.db.queries.catalog import get_products_by_ids
from src.db.queries.commerce import (
    create_checkout_link,
    get_orders_status,
    has_back_in_stock_sub,
    subscribe_back_in_stock,
)
from src.safety.policy import SafetyPolicy
from src.tools.base import ToolResult, register
from src.web.localization import amount_text
from src.worker.order_gate import login_required_for_ctx, web_unidentified

# NX-173 (P0): ce vede modelul când o MUTAȚIE e refuzată de policy. Refuzul se întâmplă ÎNAINTE de
# scriere — o filtrare de rezultat nu poate anula un rând deja scris în `checkout_links` /
# `back_in_stock_subscriptions` / `state.cart` (review Codex). Text scurt, comportamental; fraza
# spre client o garantează codul la compunere (src/safety/messages.py).
_SAFETY_REFUSED_VIEW = (
    "(nu pot adăuga/comanda acest produs în contextul declarat de client. Nu-l numi și nu-i da "
    "preț sau link. Oferă-te să cauți o alternativă.)"
)

if TYPE_CHECKING:
    from src.models import TurnContext
    from src.worker.runner import PipelineDeps


# NX-237: ce vede MODELUL pentru fiecare cod de refuz al CartService (vocabular închis).
# Instructiv, nu doar descriptiv (lecția NX-137): spunem ce funcționează în continuare.
_CART_CODE_VIEWS: dict[str, str] = {
    "product_not_found": "Produsul nu mai e în catalog.",
    "variant_not_found": "Varianta cerută nu există; spune-mi ce mărime/nuanță dorești.",
    "safety_excluded": _SAFETY_REFUSED_VIEW,
    "out_of_stock": (
        "Produsul nu e pe stoc acum — NU-l adăuga în coș; oferă-te să anunți clientul "
        "când revine (subscribe_back_in_stock)."
    ),
    "availability_unknown": (
        "Nu pot confirma stocul produsului acum, deci nu îl adaug în coș. NU promite că e "
        "disponibil."
    ),
    "insufficient_stock": "Stocul e mai mic decât cantitatea cerută; propune o cantitate mai mică.",
    "quantity_invalid": (
        f"Cantitatea maximă per produs în coș e {CART_MAX_LINE_QUANTITY}; propune o cantitate "
        "mai mică."
    ),
    "cart_full": "Coșul a atins numărul maxim de produse; finalizează sau scoate ceva întâi.",
    "price_unknown": (
        "Produsul nu are un preț confirmat, deci nu poate intra în coș. NU inventa un preț."
    ),
    "currency_mismatch": "Produsele au monede diferite; nu pot fi în același coș/checkout.",
    "line_not_found": "Produsul nu e în coșul curent.",
    "cart_empty": "Coșul e gol — adaugă întâi un produs cu cart_add.",
    "cart_not_active": "Coșul a fost deja finalizat; un nou add pornește un coș nou.",
    "receipt_pending": "Operațiunea anterioară încă se procesează; nu o repeta.",
    "internal_error": "Unealta a eșuat.",
}


def _cart_view(code: str | None) -> str:
    return _CART_CODE_VIEWS.get(code or "internal_error", "Unealta a eșuat.")


def _cart_fail_result(ctx: TurnContext, tool: str, out: MutationOutcome) -> ToolResult:
    """Refuzul serviciului → `ToolResult` de paritate cu calea legacy (aceleași event-uri)."""
    code = out.error or "internal_error"
    if code == "variant_not_found":
        ctx.emit("variant_rejected", tool=tool, product_id=None)
    return ToolResult(ok=False, error=code, llm_view=_cart_view(code))


# --- argumente (validare strictă a inputului de la model) --------------------


class CartItem(BaseModel):
    product_id: str = Field(min_length=1)
    variant_id: str | None = None
    quantity: int = Field(default=1, ge=1, le=99)


class CheckoutArgs(BaseModel):
    cart_items: list[CartItem] = Field(min_length=1, max_length=10)


def _checkout_base(ctx: TurnContext) -> str:
    """Base URL de checkout: settings-ul businessului are prioritate, apoi config global.
    Gol → checkout indisponibil (NU inventăm domeniu)."""
    per_business = (ctx.business.settings or {}).get("checkout_url")
    return (per_business or get_settings().checkout_base_url or "").strip()


def _variant_known(p: dict[str, Any], variant_id: str | None) -> bool:
    """NX-118: `variant_id` None → ok (linie fără variantă). Altfel trebuie să existe în variantele
    HIDRATATE ale produsului (`get_products_by_ids` întoarce `variants` cu `id`). Omoară un
    `variant_id` fabricat („nuanța 03") înainte să ajungă în coș/checkout."""
    if variant_id is None:
        return True
    return any(str(v.get("id")) == variant_id for v in (p.get("variants") or []))


@register("checkout_link")
async def checkout_link_tool(
    ctx: TurnContext, deps: PipelineDeps, args: dict[str, Any]
) -> ToolResult:
    """Creează un link de cumpărare atribuibil (`?ref=`) pentru coșul cerut.

    NX-237 (`CONVERSATION_CART_ENABLED`): validarea + scrierea trec prin `CartService`
    (receipt idempotent, fapte rehidratate); OFF (default) → calea legacy, byte-identic."""
    a = CheckoutArgs(**args)
    if get_settings().conversation_cart_enabled:
        return await _checkout_v2(ctx, deps, a)

    base = _checkout_base(ctx)
    if not base:
        # NX-137: llm_view instructiv — „Checkout indisponibil" gol făcea modelul să GENERALIZEZE
        # refuzul („nu pot nici să adaug în coș"), deși cart_add nu depinde de URL. Spunem explicit
        # ce funcționează, ca răspunsul către client să ofere pasul următor, nu un refuz total.
        return ToolResult(
            ok=False,
            error="no_checkout_url",
            llm_view=(
                "Linkul de plată nu e configurat pentru acest magazin — NU promite și NU inventa "
                "un link. Coșul FUNCȚIONEAZĂ separat: adaugă produsul cu cart_add și spune-i "
                "clientului că finalizarea se face pe site."
            ),
        )

    # Validăm produsele contra catalogului (scoped pe business) — nu linkuim ce nu există.
    ids = list(dict.fromkeys(it.product_id for it in a.cart_items))
    async with deps.db("checkout_link_products") as conn:
        products = await get_products_by_ids(conn, ctx.business.id, ids, limit=6)
    by_id = {p["id"]: p for p in products}

    cart: list[dict[str, Any]] = []
    total = 0.0
    for it in a.cart_items:
        p = by_id.get(it.product_id)
        if p is None or p.get("price") is None:
            continue
        # NX-118: nu linkuim un `variant_id` fabricat (orice variantă cerută trebuie să existe).
        if not _variant_known(p, it.variant_id):
            ctx.emit("variant_rejected", tool="checkout_link", product_id=it.product_id)
            return ToolResult(
                ok=False,
                error="variant_not_found",
                llm_view="Una dintre variantele cerute nu există; confirmă mărimea/nuanța.",
            )
        price = round(float(p["price"]), 2)
        cart.append(
            {
                "product_id": it.product_id,
                "variant_id": it.variant_id,
                "name": p["name"],
                "price": price,
                "quantity": it.quantity,
            }
        )
        total += price * it.quantity

    if not cart:
        return ToolResult(
            ok=False,
            error="no_valid_products",
            llm_view="Produsele cerute nu mai sunt în catalog.",
        )

    # NX-173 (P0): poarta de MUTAȚIE — ÎNAINTE de `create_checkout_link` (scrie în DB) și de
    # generarea url-ului. Refuzăm TOT checkout-ul dacă vreo linie e contraindicată: un checkout
    # „parțial", tăcut, ar schimba comanda clientului fără să-i spună (mai rău ca un refuz onest).
    policy = SafetyPolicy.for_turn(ctx)
    d = policy.evaluate([by_id[c["product_id"]] for c in cart], purpose="checkout")
    if d.blocked or d.unavailable:
        policy.emit(ctx, d, purpose="checkout")
        return ToolResult(ok=False, error="safety_excluded", llm_view=_SAFETY_REFUSED_VIEW)

    sep = "&" if "?" in base else "?"
    url = f"{base}{sep}ref={ctx.turn_id}"
    expires_at = datetime.now(UTC) + timedelta(days=get_settings().checkout_link_ttl_days)
    async with deps.db("create_checkout_link") as conn:
        await create_checkout_link(
            conn,
            ctx.business.id,
            ctx.conversation_id,
            ctx.contact.id,
            ctx.turn_id,  # ref_code = turn_id → idempotent per tur
            cart,
            url,
            expires_at,
        )
    total = round(total, 2)
    # NX-163: ce a ajuns în checkout, ca ref-uri (P8) — leagă recomandare→coș→comandă în raportul de
    # cerere (NX-164). Doar product_id-uri, fără PII.
    ctx.emit(
        "checkout_link_created",
        items=len(cart),
        value=total,
        product_ids=clean_ids(c["product_id"] for c in cart),
    )

    lines = ", ".join(
        f"{c['name']} ×{c['quantity']} ({amount_text(c['price'], ctx.language)} lei)" for c in cart
    )
    llm_view = (
        f"Link de checkout creat: {url}\n"
        f"Coș: {lines} | total {amount_text(total, ctx.language)} lei"
    )
    # `products` (cart) → prețurile produselor sunt grounded; `links` → linkul permis;
    # `prices=[total]` → TOTALUL coșului e grounded (altfel validatorul l-ar respinge).
    return ToolResult(
        ok=True,
        products=[by_id[c["product_id"]] for c in cart],
        links=[url],
        prices=[total],
        llm_view=llm_view,
    )


# --- cart_add (NX-79): acumulează coșul în state (ref-uri, P8) ----------------


class CartAddArgs(BaseModel):
    product_id: str = Field(min_length=1)
    variant_id: str | None = None
    quantity: int = Field(default=1, ge=1, le=99)


_CART_MAX_LINES = 10  # cap dur (aliniat cu CheckoutArgs.max_length)


@register("cart_add")
async def cart_add_tool(ctx: TurnContext, deps: PipelineDeps, args: dict[str, Any]) -> ToolResult:
    """Adaugă un produs în coșul conversației (persistat în `state.cart` prin `state_patch`).
    Validează produsul contra catalogului (scoped pe business); merge pe (product_id, variant_id)
    → re-apel crește cantitatea, nu duplică linia. Întoarce totalul grounded (validator, P8).

    NX-237 (`CONVERSATION_CART_ENABLED`): mutația trece prin `CartService` — coșul canonic din
    `conversation_carts`, cu receipt idempotent per (tur, op, args); starea primește DOAR
    `cart_ref`, nu linii cu preț copiat. OFF (default) → calea legacy de mai jos, byte-identic."""
    a = CartAddArgs(**args)
    if get_settings().conversation_cart_enabled:
        return await _cart_add_v2(ctx, deps, a)
    async with deps.db("cart_add_product") as conn:
        products = await get_products_by_ids(conn, ctx.business.id, [a.product_id], limit=1)
    p = products[0] if products else None
    if p is None or p.get("price") is None:
        return ToolResult(
            ok=False, error="product_not_found", llm_view="Produsul nu mai e în catalog."
        )
    # NX-118: variant-membership — un `variant_id` inexistent în catalog NU intră în coș.
    if not _variant_known(p, a.variant_id):
        ctx.emit("variant_rejected", tool="cart_add", product_id=a.product_id)
        return ToolResult(
            ok=False,
            error="variant_not_found",
            llm_view="Varianta cerută nu există; spune-mi ce mărime/nuanță dorești.",
        )
    # NX-173 (P0): poarta de MUTAȚIE — înainte de a scrie coșul. Backstop-ul din executor filtra
    # `result.products`, dar `state_patch["cart"]` plecase deja cu produsul contraindicat în el.
    policy = SafetyPolicy.for_turn(ctx)
    if not policy.allows(p, purpose="cart_add"):
        policy.emit(ctx, policy.evaluate([p], purpose="cart_add"), purpose="cart_add")
        return ToolResult(ok=False, error="safety_excluded", llm_view=_SAFETY_REFUSED_VIEW)

    # Coșul curent din state (ref-uri compacte, NU obiectul complet — P8). Copie → nu mutăm state.
    cart: list[dict[str, Any]] = [dict(line) for line in (ctx.state.cart or [])]
    key = (a.product_id, a.variant_id)
    for line in cart:
        if (line["product_id"], line.get("variant_id")) == key:
            line["quantity"] = min(line["quantity"] + a.quantity, 99)
            break
    else:
        cart.append(
            {
                "product_id": a.product_id,
                "variant_id": a.variant_id,
                "name": p["name"],
                "price": round(float(p["price"]), 2),
                "quantity": a.quantity,
            }
        )
    cart = cart[:_CART_MAX_LINES]
    total = round(sum(line["price"] * line["quantity"] for line in cart), 2)
    # NX-163: ce s-a adăugat în coș, ca ref-uri (P8) — semnal de add-to-cart per produs (NX-164).
    ctx.emit(
        "cart_updated",
        lines=len(cart),
        value=total,
        product_ids=clean_ids(line["product_id"] for line in cart),
    )

    summary = ", ".join(f"{line['name']} ×{line['quantity']}" for line in cart)
    return ToolResult(
        ok=True,
        products=[p],  # complet → ctx.retrieval + validator de preț
        prices=[total],  # totalul coșului grounded (P8) → validator
        state_patch={"cart": cart},  # ref-uri compacte → persistate de processor
        llm_view=(
            f"Coș actualizat ({len(cart)} produse): {summary} | "
            f"total {amount_text(total, ctx.language)} lei"
        ),
    )


async def _cart_add_v2(ctx: TurnContext, deps: PipelineDeps, a: CartAddArgs) -> ToolResult:
    """NX-237: `cart_add` prin serviciul canonic. Aceeași comandă typed ca un click de acțiune;
    prețul/numele/stocul se rehidratează în serviciu — modelul nu poate afirma un preț stale."""
    command = CartCommand.parse(
        "add",
        {"product_id": a.product_id, "variant_id": a.variant_id, "quantity": a.quantity},
    )
    if command is None:
        # Peste capul canonic (CartAddArgs acceptă ≤99 pt schema legacy) sau ref malformat.
        code = "quantity_invalid" if a.quantity > CART_MAX_LINE_QUANTITY else "product_not_found"
        return ToolResult(ok=False, error=code, llm_view=_cart_view(code))
    service = CartService.for_turn(ctx, deps)
    out = await service.mutate(
        ctx.conversation_id,
        command,
        idempotency_key=tool_idempotency_key(ctx.turn_id, command),
        turn_id=ctx.turn_id,
    )
    if not out.ok:
        return _cart_fail_result(ctx, "cart_add", out)
    snap = out.snapshot
    # NX-163: aceleași semnale ca pe calea legacy (raportul de cerere nu vede migrarea).
    ctx.emit(
        "cart_updated",
        lines=len(snap.lines),
        value=snap.totals.value,
        product_ids=clean_ids(ln.product_id for ln in snap.lines),
    )
    summary = ", ".join(f"{ln.name} ×{ln.quantity}" for ln in snap.lines)
    total_view = (
        f" | total {snap.totals.display}"
        if snap.totals.status == "known"
        else " | total de confirmat (un preț lipsește)"
    )
    return ToolResult(
        ok=True,
        products=[dict(p) for p in out.products],  # complet → ctx.retrieval + validator
        prices=([snap.totals.value] if snap.totals.value is not None else []),
        state_patch={"cart_ref": snap.to_state_ref()},  # ref, NU linii (P8)
        cart_snapshot=snap,
        llm_view=f"Coș actualizat ({len(snap.lines)} produse): {summary}{total_view}",
    )


async def _checkout_v2(ctx: TurnContext, deps: PipelineDeps, a: CheckoutArgs) -> ToolResult:
    """NX-237: checkout prin serviciul canonic — aceleași validări ca un click, receipt
    idempotent (`ref_code = turn_id`, ca înainte), coșul canonic închis DOAR dacă e acoperit."""
    base = _checkout_base(ctx)
    if not base:
        return ToolResult(
            ok=False,
            error="no_checkout_url",
            llm_view=(
                "Linkul de plată nu e configurat pentru acest magazin — NU promite și NU inventa "
                "un link. Coșul FUNCȚIONEAZĂ separat: adaugă produsul cu cart_add și spune-i "
                "clientului că finalizarea se face pe site."
            ),
        )
    lines: list[dict[str, Any]] = []
    for it in a.cart_items:
        if it.quantity > CART_MAX_LINE_QUANTITY:
            return ToolResult(
                ok=False, error="quantity_invalid", llm_view=_cart_view("quantity_invalid")
            )
        lines.append(
            {"product_id": it.product_id, "variant_id": it.variant_id, "quantity": it.quantity}
        )
    service = CartService.for_turn(ctx, deps)
    fingerprint_cmd = CartCommand.parse("checkout") or CartCommand(operation="checkout")
    out = await service.create_checkout(
        ctx.conversation_id,
        idempotency_key=tool_idempotency_key(ctx.turn_id, fingerprint_cmd),
        turn_id=ctx.turn_id,
        base_url=base,
        lines=lines,
    )
    if not out.ok:
        code = out.error or "internal_error"
        if code == "product_not_found":
            # Paritate legacy: „niciun produs valid" are propriul mesaj.
            return ToolResult(
                ok=False,
                error="no_valid_products",
                llm_view="Produsele cerute nu mai sunt în catalog.",
            )
        return _cart_fail_result(ctx, "checkout_link", out)
    url = out.receipt.url if out.receipt else None
    total = round(sum(float(ln["price"]) * int(ln["quantity"]) for ln in out.lines), 2)
    ctx.emit(
        "checkout_link_created",
        items=len(out.lines),
        value=total,
        product_ids=clean_ids(str(ln["product_id"]) for ln in out.lines),
    )
    lines_view = ", ".join(
        f"{ln['name']} ×{ln['quantity']} ({amount_text(ln['price'], ctx.language)} lei)"
        for ln in out.lines
    )
    return ToolResult(
        ok=True,
        products=[dict(p) for p in out.products],
        links=[url] if url else [],
        prices=[total],
        cart_snapshot=out.snapshot,
        llm_view=(
            f"Link de checkout creat: {url}\n"
            f"Coș: {lines_view} | total {amount_text(total, ctx.language)} lei"
        ),
    )


# --- reorder (NX-79): re-comandă din istoricul contactului -------------------


@register("reorder")
async def reorder_tool(ctx: TurnContext, deps: PipelineDeps, args: dict[str, Any]) -> ToolResult:
    """Propune re-comanda ultimei comenzi a contactului. `contact_id` din `ctx` (NU din args, P7).
    Citire pură (NU scrie coș/link). Numele + prețurile vin din `orders` (date reale ale
    tenantului, ca `check_order`) → `prices` grounded; produsele istorice pot fi inactive, deci
    NU le validăm contra catalogului."""
    # Re-comanda e legată de contul clientului (istoricul LUI). Pe web anonim contactul e throwaway
    # → niciun istoric; întoarcem mesajul de login în loc de „n-ai comenzi anterioare" (înșelător).
    if web_unidentified(ctx):
        ctx.emit(
            "order_lookup_gated",
            channel_kind=ctx.message.channel_kind,
            reason="web_unidentified_reorder",
        )
        return ToolResult(ok=False, error="login_required", llm_view=login_required_for_ctx(ctx))
    async with deps.db("reorder_history") as conn:
        orders = await get_orders_status(conn, ctx.business.id, contact_id=ctx.contact.id, limit=3)
    if not orders:
        return ToolResult(
            ok=False, error="no_orders", llm_view="Nu găsesc comenzi anterioare pe contul tău."
        )
    last = orders[0]  # cea mai recentă (order by placed_at desc)
    items = last.get("items") or []
    if not items:
        return ToolResult(ok=False, error="no_items", llm_view="Comanda anterioară n-are produse.")
    prices = [round(float(i["unit_price"]), 2) for i in items if i.get("unit_price") is not None]
    ctx.emit("reorder_suggested", order_id=last["id"], lines=len(items))
    summary = ", ".join(f"{i['name']} ×{i.get('quantity', 1)}" for i in items)
    return ToolResult(
        ok=True,
        prices=prices,
        llm_view=(f"Ultima comandă a clientului: {summary}. Sugerează re-comanda acestor produse."),
    )


# --- subscribe_back_in_stock (NX-80): notificare la restock (WRITE) ----------


class BackInStockArgs(BaseModel):
    product_id: str = Field(min_length=1)
    variant_id: str | None = None


@register("subscribe_back_in_stock")
async def subscribe_back_in_stock_tool(
    ctx: TurnContext, deps: PipelineDeps, args: dict[str, Any]
) -> ToolResult:
    """Abonează clientul la notificare când un produs fără stoc revine. `contact_id` din `ctx`
    (PII-ul nu trece prin model, P12). Idempotent: re-abonare = no-op (re-armează notificarea),
    cu guard pe `variant_id IS NULL` (NULL distinct în UNIQUE → ON CONFLICT nu prinde). NU
    trimite confirmarea de restock — aia e proactivul (NX-70), care citește rândul scris aici."""
    a = BackInStockArgs(**args)
    async with deps.db("back_in_stock_product") as conn:
        products = await get_products_by_ids(conn, ctx.business.id, [a.product_id], limit=1)
    if not products:
        return ToolResult(ok=False, error="not_found", llm_view="Produsul nu există în catalog.")
    p = products[0]
    # NX-173 (P0): poarta de MUTAȚIE — abonarea scrie un rând care declanșează un mesaj PROACTIV
    # („a revenit pe stoc") peste zile. Ar fi cea mai proastă formă a bug-ului: promovare activă a
    # unui produs contraindicat, în afara conversației.
    policy = SafetyPolicy.for_turn(ctx)
    if not policy.allows(p, purpose="back_in_stock"):
        policy.emit(ctx, policy.evaluate([p], purpose="back_in_stock"), purpose="back_in_stock")
        return ToolResult(ok=False, error="safety_excluded", llm_view=_SAFETY_REFUSED_VIEW)
    if p.get("availability") == "in_stock":
        return ToolResult(
            ok=True,
            products=[p],
            llm_view=f"{p['name']} este pe stoc acum, nu e nevoie de notificare.",
        )
    # NX-231: check-then-insert e o SINGURĂ operație atomică → un checkout scurt + o tranzacție
    # internă (`db_tx`), nu o conexiune ținută între apeluri de tool. Guard variant NULL: dacă
    # deja abonat, nu mai inserăm (în Postgres NULL e DISTINCT în UNIQUE → ON CONFLICT nu prinde);
    # fără tranzacție, două tururi concurente ale aceluiași client puteau strecura un duplicat
    # între verificare și insert.
    async with db_tx(deps.db, "back_in_stock_subscribe") as conn:
        already = a.variant_id is None and await has_back_in_stock_sub(
            conn, ctx.business.id, ctx.contact.id, a.product_id, None
        )
        res = (
            None
            if already
            else await subscribe_back_in_stock(
                conn, ctx.business.id, ctx.contact.id, a.product_id, a.variant_id
            )
        )
    if already:
        ctx.emit("back_in_stock_subscribed", product_id=a.product_id, created=False)
        return ToolResult(
            ok=True, products=[p], llm_view=f"Ești deja pe lista de notificare pentru {p['name']}."
        )
    ctx.emit("back_in_stock_subscribed", product_id=a.product_id, created=res["created"])
    return ToolResult(ok=True, products=[p], llm_view=f"Te anunț când {p['name']} revine pe stoc.")
