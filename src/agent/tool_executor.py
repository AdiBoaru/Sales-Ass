"""Faza D — tool executor (NX-143). `ToolRun` rulează tool-urile deterministe pe care le cheamă
modelul în bucla de function-calling și acumulează rezultatele cu STARE EXPLICITĂ.

Înainte, closure-ul `execute` din `agent_stage` folosea ~10 acumulatori `nonlocal` (produse,
linkuri, sume grounded, set comparat, vederi de comandă, login-gate, coș, relevanță, eșecuri de
comerț, link de checkout) — greu de testat și de urmărit cine scrie ce (P3). Aici devin CÂMPURI ale
unui dataclass: `ToolRun(ctx, deps)`, pasezi `run.execute` la `run_tool_loop`, apoi citești
`run.retrieved`/`run.generated_links`/... după buclă.

INVARIANT DE SECURITATE (seam NX-150): `business_id` se ia din `ctx`, NICIODATĂ din `args` —
`run_tool(ctx, deps, ...)` primește tenantul din context, nu din ce cere modelul. `tool_call` se
emite din `execute` (cu `turn_id`, P10); args-urile sunt whitelisted (`_safe_tool_args`, fără PII).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter
from typing import TYPE_CHECKING, Any

from src.models import TurnContext
from src.tools.base import run_tool

if TYPE_CHECKING:
    from src.worker.runner import PipelineDeps

_TOOL_ARG_WHITELIST: dict[str, tuple[str, ...]] = {
    "search_products": (
        "category",
        "brand",
        "concerns",
        "features",
        "price_max",
        "sort_mode",
        "in_stock_only",
        "limit",
    ),
    "get_product_details": ("product_id",),
    "compare_products": ("product_ids",),
    "cart_add": ("product_id", "variant_id", "quantity"),
    "checkout_link": ("cart_items",),  # listă de {product_id, variant_id, quantity} — fără PII
    "subscribe_back_in_stock": ("product_id", "variant_id"),
}


def _trunc(v: Any) -> Any:
    """Trunchiere defensivă a unei valori de arg pentru tracing: scalari / liste scurte /
    dict mic (ex. `filters`). Stringuri tăiate la 64 de caractere, liste la 8 elemente,
    dict la 8 chei — nimic care să poarte text liber lung al userului în analytics."""
    if isinstance(v, str):
        return v[:64]
    if isinstance(v, list):
        # recursiv: elementele pot fi dict-uri (ex. cart_items) → bornăm și ele (string cap +
        # 8-key cap), nu doar string-urile top-level. Altfel un dict în listă scăpa neplafonat.
        return [_trunc(s) for s in v[:8]]
    if isinstance(v, dict):
        return {k: _trunc(val) for k, val in list(v.items())[:8]}
    return v  # int / float / bool / None — neschimbat


def _safe_tool_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """NX-122: args sanitizate pentru event-ul `tool_call` (whitelist per tool, fără PII — P12).
    `check_order` → DOAR `{has_arg}` (numărul/contactul nu ajung niciodată în analytics); tool
    necunoscut / fără chei whitelisted → `{}`."""
    if name == "check_order":
        return {"has_arg": bool(args)}
    allowed = _TOOL_ARG_WHITELIST.get(name)
    if not allowed:
        return {}
    out: dict[str, Any] = {}
    for k in allowed:
        val = args.get(k)
        if val is not None:
            out[k] = _trunc(val)
    return out


@dataclass
class ToolRun:
    """Starea acumulată a unei rulări de tool-uri într-un tur. `execute` e callback-ul buclei;
    câmpurile se citesc după buclă (faza E/planner). Un singur owner explicit per câmp (P3)."""

    ctx: TurnContext
    deps: PipelineDeps
    retrieved: list[dict[str, Any]] = field(default_factory=list)
    generated_links: set[str] = field(default_factory=set)  # linkuri bot (checkout) → validator
    grounded_prices: set[float] = field(default_factory=set)  # sume DB (total comandă) → validator
    order_views: list[str] = field(default_factory=list)  # vederi grounded de comandă (fallback)
    compared: list[dict[str, Any]] = field(default_factory=list)  # setul EXPLICIT comparat
    order_gated_login: bool = False  # web anonim a încercat lookup de comandă → login wall
    added_product: dict[str, Any] | None = None  # #7b: ultimul produs adăugat în coș (cart_add)
    search_relevance: Any = None  # izi-parity: relevanța ultimului search_products (off-category)
    failed_commerce: set[str] = field(default_factory=set)  # NX-137: cart/checkout eșuate
    checkout_url: str | None = None  # NX-137: linkul REAL de checkout creat în acest tur → CTA

    async def execute(self, name: str, args: dict[str, Any]) -> str:
        """Callback al buclei: rulează tool-ul, acumulează produse + linkuri + sume grounded,
        întoarce vederea compactă modelului. `business_id` se ia din `ctx` (nu din `args`)."""
        ctx, deps = self.ctx, self.deps
        started = perf_counter()
        result = await run_tool(ctx, deps, name, args)
        latency_ms = round((perf_counter() - started) * 1000, 1)
        self.retrieved.extend(result.products)
        # IZI-compare: dacă modelul a chemat compare_products (a înțeles „compară primele două"),
        # reține setul comparat ÎN ORDINEA cerută (get_products_by_ids o păstrează) → tabel.
        if name == "compare_products" and result.ok and result.products:
            self.compared = list(result.products)
        # izi-parity hardening: reține relevanța ULTIMULUI search_products (off-category signal) →
        # o punem pe ctx.retrieval mai jos, ca compose să suprime pick-ul pe categoria greșită.
        if name == "search_products" and result.relevance is not None:
            self.search_relevance = result.relevance
        self.generated_links.update(result.links)
        self.grounded_prices.update(result.prices)
        if result.state_patch:  # NX-79: cart_add → mutație de state (persistată de processor)
            ctx.state_patch.update(result.state_patch)
        if name == "cart_add" and result.ok and result.products:
            self.added_product = result.products[0]  # #7b: ancora pentru cross-sell
        # NX-137: un eșec de comerț în ACEST tur → compunerea nu are voie să sugereze chips-ul
        # exact refuzat în mesaj („Adaugă-l în coș" sub un „nu pot adăuga în coș" — runda 2, iZi).
        if name in ("cart_add", "checkout_link") and not result.ok:
            self.failed_commerce.add(name)
        if name == "checkout_link" and result.ok and result.links:
            self.checkout_url = result.links[0]  # NX-137: → Offer(open_url) pe reply
        if name == "check_order":
            if result.ok and result.llm_view:
                self.order_views.append(result.llm_view)
            elif result.error == "login_required":
                # Web anonim: lookup-ul de comandă a fost gated în tool → servim mesajul de login
                # determinist după buclă (nu lăsăm modelul să-l parafrazeze / să ceară nr comandă).
                self.order_gated_login = True
        # NX-122: args whitelisted + count + latență + clasă de eroare (NU corpul). Corelat
        # cu restul turului prin `turn_id` injectat automat în emit() → traiectorie rejucabilă.
        ctx.emit(
            "tool_call",
            name=name,
            ok=result.ok,
            args=_safe_tool_args(name, args),
            n_results=len(result.products),
            latency_ms=latency_ms,
            error=(result.error if not result.ok else None),
        )
        return result.llm_view or (result.error or "(fără rezultat)")
