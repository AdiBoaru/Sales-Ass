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

import asyncio
from dataclasses import dataclass, field
from time import perf_counter
from typing import TYPE_CHECKING, Any

from src.agent import tool_budget
from src.config import get_settings
from src.db.provider import is_shared_connection
from src.models import TurnContext
from src.observability import turn_latency
from src.runtime import deadline, turn_budget
from src.safety.policy import SafetyPolicy
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
    # NX-237: ultimul snapshot al coșului CANONIC (CartService, sub flag). Plannerul citește de
    # aici (checkout fallback / cross-sell exclude), nu din `state.cart` — o singură autoritate.
    cart_snapshot: Any = None
    # NX-211: server-owned IDs for mutations that returned ok=True.
    successful_action_ids: set[str] = field(default_factory=set)
    # Shared asyncpg connections only support one active operation at a time. The LLM adapter may
    # dispatch several calls together, so serialize this run's DB access and state mutations.
    _execution_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    # NX-241: poarta read/mutation (reader-writer). Construită LENEȘ, la primul tool: plafonul de
    # citiri paralele vine din bugetul clasei, iar pe un provider cu conexiune partajată rămâne 1.
    _gate: tool_budget.ToolGate | None = field(default=None, init=False, repr=False)
    # NX-241: ORDINEA de acumulare, când citirile chiar rulează în paralel. `retrieved` nu e o
    # mulțime, e o LISTĂ ordonată după relevanță — dacă două citiri concurente ar scrie în ordinea
    # în care s-a întâmplat să răspundă providerul, aceleași două tool-uri ar produce carduri în
    # ordini diferite de la o rulare la alta. Bilețelele restabilesc ordinea APELURILOR: munca
    # externă rămâne concurentă, acumularea se aplică strict în ordinea cerută de model.
    _order: asyncio.Condition = field(default_factory=asyncio.Condition, init=False, repr=False)
    _next_ticket: int = field(default=0, init=False, repr=False)
    _serving: int = field(default=0, init=False, repr=False)
    _done_tickets: set[int] = field(default_factory=set, init=False, repr=False)

    def _tool_gate(self) -> tool_budget.ToolGate:
        if self._gate is None:
            self._gate = tool_budget.ToolGate(self._max_parallel_reads())
        return self._gate

    def _max_parallel_reads(self) -> int:
        """Câte citiri independente au voie simultan. 1 = serializarea de azi.

        Trei condiții, toate necesare: flagul, un buget activ (care dă plafonul clasei) și un
        provider DB care chiar dă conexiuni separate (`conn-per-op`, NX-231). Pe `static_db` un
        `gather` peste două query-uri ar rupe conexiunea partajată — deci acolo rămâne 1, oricât
        ar spune configul. Plafonul e al CODULUI, nu al modelului (P7)."""
        ledger = turn_budget.current()
        if ledger is None or not getattr(get_settings(), "turn_parallel_reads_enabled", False):
            return 1
        if is_shared_connection(self.deps.db):
            return 1
        return max(1, ledger.budget.max_parallel_reads)

    def _take_ticket(self) -> int | None:
        """Un bilețel DOAR când paralelismul e real (>1). Cu plafonul 1, poarta servește deja în
        ordinea apelurilor (semafor FIFO), deci nu adăugăm nimic pe calea implicită."""
        if self._max_parallel_reads() <= 1:
            return None
        seq = self._next_ticket
        self._next_ticket += 1
        return seq

    async def _await_ticket(self, seq: int) -> None:
        async with self._order:
            await self._order.wait_for(lambda: self._serving == seq)

    async def _finish_ticket(self, seq: int) -> None:
        """Se cheamă din `finally` MEREU (inclusiv la refuz sau excepție) — un bilețel neeliberat
        ar bloca definitiv toate apelurile de după el."""
        async with self._order:
            self._done_tickets.add(seq)
            while self._serving in self._done_tickets:
                self._done_tickets.discard(self._serving)
                self._serving += 1
            self._order.notify_all()

    def _safe_products(self, products: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """NX-173: plasa de siguranță peste rezultatul ORICĂRUI tool.

        NU e protecția principală — tool-urile gate-uiesc la sursă (catalog) și refuză mutațiile
        ÎNAINTE de scriere (comerț). Aici prindem doar o cale nouă care ar uita gate-ul; e ultimul
        loc dinaintea lui `retrieved` → `ctx.retrieval` → validator → carduri → displayed_products.

        Limita ei (de-asta nu e suficientă singură, review Codex): filtrează `products`, dar
        `llm_view`/`links`/`state_patch` ale tool-ului au plecat deja. Un blocaj AICI = bug la
        sursă, nu o salvare — de-aia se emite cu `purpose="unfiltered_path"`."""
        if not products:
            return products
        return SafetyPolicy.for_turn(self.ctx).gate(self.ctx, products, purpose="unfiltered_path")[
            0
        ]

    async def execute(self, name: str, args: dict[str, Any]) -> str:
        """Callback-ul buclei LLM. `name` vine EXCLUSIV de la model, dintre schemele pe care i le-am
        dat (`tool_schemas`).

        NX-236: acțiunile opace ale clientului NU intră niciodată pe aici. Ele au registrul lor
        (`src/web/action_models.KIND_REGISTRY`, disjunct de `TOOL_NAMES` — verificat la import) și
        dispatch-ul lor typed (`src/agent/action_kernel.dispatch`), care primește un `ActionSpec`
        deja rezolvat, nu un nume. Un token nu poate deci numi un tool, oricât ar fi de valid.

        NX-241: fără buget și fără deadline activ (default) rămâne exact lock-ul de azi. Cu ele
        active, apelul trece întâi prin ADMISSION (plafon de tool calls / mutații / timp rămas) și
        apoi prin poarta read-mutation. Un refuz e TYPED și ajunge la model ca text scurt și onest,
        nu ca timeout: modelul trebuie să încheie cu ce are, nu să reîncerce."""
        ledger = turn_budget.current()
        d = deadline.current()
        if ledger is None and d is None:
            async with self._execution_lock:
                return await self._execute_serialized(name, args)

        # Bilețelul se ia ÎNAINTE de orice `await` → în ordinea în care modelul a cerut tool-urile
        # (`gather` pornește corutinele în ordinea argumentelor). Se eliberează MEREU, în `finally`.
        seq = self._take_ticket()
        try:
            admission = tool_budget.admit(name, ledger=ledger, deadline=d)
            if not admission:
                self.ctx.emit(
                    "tool_budget",
                    name=name,
                    outcome="rejected",
                    reason=admission.reason or "unknown",
                )
                return admission.refusal or tool_budget.REFUSAL_BUDGET
            async with self._tool_gate().hold(name) as spec:
                if spec.is_mutation and d is not None and d.has_room_for("tools").exhausted:
                    # O mutație pornită după deadline scrie ceva despre care clientul nu mai află.
                    # Verificăm ULTIMA dată aici: între admission și rândul nostru a trecut timp.
                    self.ctx.emit(
                        "tool_budget", name=name, outcome="rejected", reason="deadline_at_gate"
                    )
                    return tool_budget.REFUSAL_DEADLINE
                with turn_latency.span("tools"):
                    return await self._execute_serialized(name, args, seq=seq)
        finally:
            if seq is not None:
                await self._finish_ticket(seq)

    async def _execute_serialized(
        self, name: str, args: dict[str, Any], *, seq: int | None = None
    ) -> str:
        """Callback al buclei: rulează tool-ul, acumulează produse + linkuri + sume grounded,
        întoarce vederea compactă modelului. `business_id` se ia din `ctx` (nu din `args`).

        `seq` (NX-241, doar sub paralelism real): apelul EXTERN e concurent, dar acumularea de mai
        jos se aplică strict în ordinea apelurilor — `retrieved` e o listă ordonată după relevanță,
        nu o mulțime."""
        ctx, deps = self.ctx, self.deps
        started = perf_counter()
        result = await run_tool(ctx, deps, name, args)
        if seq is not None:
            await self._await_ticket(seq)
        latency_ms = round((perf_counter() - started) * 1000, 1)
        # NX-173 (P0) BACKSTOP: tool-urile de catalog filtrează deja contraindicațiile la sursă (cu
        # `llm_view` construit din setul curat). Asta e plasa de siguranță pentru orice cale care
        # UITĂ filtrul — un tool nou, o cale de comerț care întoarce produse. Aici trec TOATE
        # rezultatele de tool, iar `retrieved` alimentează `ctx.retrieval` → validator → carduri →
        # `displayed_products`: ce cade aici nu mai ajunge nicăieri.
        products = self._safe_products(result.products)
        self.retrieved.extend(products)
        # IZI-compare: dacă modelul a chemat compare_products (a înțeles „compară primele două"),
        # reține setul comparat ÎN ORDINEA cerută (get_products_by_ids o păstrează) → tabel.
        if name == "compare_products" and result.ok and products:
            self.compared = list(products)
        # izi-parity hardening: reține relevanța ULTIMULUI search_products (off-category signal) →
        # o punem pe ctx.retrieval mai jos, ca compose să suprime pick-ul pe categoria greșită.
        if name == "search_products" and result.relevance is not None:
            self.search_relevance = result.relevance
        self.generated_links.update(result.links)
        self.grounded_prices.update(result.prices)
        if result.state_patch:  # NX-79: cart_add → mutație de state (persistată de processor)
            ctx.state_patch.update(result.state_patch)
        # NX-237: coșul canonic al turului (sub flag). `getattr` — testele duck-type-uiesc
        # ToolResult cu SimpleNamespace, iar câmpul e nou.
        if getattr(result, "cart_snapshot", None) is not None:
            self.cart_snapshot = result.cart_snapshot
        if name == "cart_add" and result.ok and products:
            self.added_product = products[0]  # #7b: ancora pentru cross-sell
        # NX-137: un eșec de comerț în ACEST tur → compunerea nu are voie să sugereze chips-ul
        # exact refuzat în mesaj („Adaugă-l în coș" sub un „nu pot adăuga în coș" — runda 2, iZi).
        if name in ("cart_add", "checkout_link") and not result.ok:
            self.failed_commerce.add(name)
        if name == "checkout_link" and result.ok and result.links:
            self.checkout_url = result.links[0]  # NX-137: → Offer(open_url) pe reply
        if name in ("cart_add", "checkout_link", "subscribe_back_in_stock") and result.ok:
            action_id = f"{name}:{len(self.successful_action_ids) + 1}"
            self.successful_action_ids.add(action_id)
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
            n_results=len(products),
            latency_ms=latency_ms,
            error=(result.error if not result.ok else None),
        )
        view = result.llm_view or (result.error or "(fără rezultat)")
        # NX-241: plafon de VOLUM înainte ca rezultatul să intre în conversație. Un `llm_view`
        # patologic (tool nou, catalog cu descrieri uriașe) nu e un rezultat bogat, e un prompt
        # otrăvit: cost, latență și un model care se pierde. Trunchierea e bounded și DECLARATĂ.
        ledger = turn_budget.current()
        if ledger is not None:
            view, dropped = tool_budget.cap_result(view, ledger.budget.max_result_bytes)
            if dropped:
                ledger.consume("result_bytes", dropped)
                turn_latency.degrade("tool_result_truncated")
                ctx.emit("tool_budget", name=name, outcome="truncated", reason="result_bytes")
        return view
