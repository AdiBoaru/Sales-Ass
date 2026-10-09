"""Uneltele agentului unic (NX-396): fapte și acțiuni, peste serviciile de producție.

Agentul cere, codul execută. Fiecare unealtă:
- ia `business_id` din `ctx` (P7), niciodată din argumente;
- primește produsele DOAR prin handle-uri (`memory.check`), iar un handle necunoscut e refuzat cu
  lista celor valide;
- trece ORICE set de produse prin `SafetyPolicy.gate` (NX-173) înainte ca agentul să-l vadă, deci
  un produs contraindicat nu ajunge nici la model, nici pe card;
- refolosește unealta de producție unde există (coș, comandă, abonare, rutină), cu regulile ei.

Ce citesc uneltele se adună în `facts` (rândurile de produs, regulile, sumele comenzii), pe care
poarta de adevăr le folosește ca sursă. Numele uneltelor agentului sunt ale lui, separate de
registrul `TOOL_NAMES`.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from src.assistant.memory import Memory, Refused, distinct_names
from src.assistant.menus import Menus, check

log = logging.getLogger(__name__)

SEARCH_ROWS = 8
SHEET_MAX = 3000
#: Câte rânduri cere căutarea (scara lexicală, NX-293), din care agentul vede `SEARCH_ROWS`.
SEARCH_POOL = 40


@dataclass
class Facts:
    """Ce a citit turul: sursa porții de adevăr și a răspunsului."""

    rows: dict[str, dict[str, Any]] = field(default_factory=dict)  # product_id → rând gated
    sources: list[str] = field(default_factory=list)  # răspunsurile regulilor citite (NX-346)
    order_prices: list[float] = field(default_factory=list)
    #: O comandă GĂSITĂ (nu doar cerută): atunci cifrele ei (dată, AWB, cantitate) sunt fapte.
    order_found: bool = False
    catalog_read: bool = False
    read_beyond_catalog: bool = False
    mutated: bool = False
    #: Mutațiile reușite, ca un tur căzut după ele să nu le piardă: cheile de stare scrise, cu
    #: valoarea lor, evenimentele uneltei și (unealtă, id-ul produsului).
    mutation_patch: dict[str, Any] = field(default_factory=dict)
    mutation_events: list[Any] = field(default_factory=list)
    mutations: list[tuple[str, str]] = field(default_factory=list)


class Tools:
    def __init__(
        self,
        ctx: Any,
        deps: Any,
        menus: Menus,
        memory: Memory,
        *,
        shown_ids: set[str],
        facts: Facts | None = None,
    ) -> None:
        from src.safety.policy import SafetyPolicy  # noqa: PLC0415

        self.ctx, self.deps, self.menus, self.memory = ctx, deps, menus, memory
        self.shown_ids = shown_ids
        self.facts = facts or Facts()
        self.policy = SafetyPolicy.for_turn(ctx)
        self.calls: list[dict[str, Any]] = []

    # --- infrastructura --------------------------------------------------------------------------

    async def run(self, name: str, args: dict[str, Any]) -> str:
        started = time.monotonic()
        refused = None
        handler = getattr(self, f"_{name}", None) if name != "answer" else None
        from src.observability import turn_latency  # noqa: PLC0415

        try:
            if handler is None:
                raise Refused(f"unknown tool {name!r}")
            with turn_latency.span("tools"):
                out = await handler(args)
        except Refused as e:
            refused = str(e)
            out = {"ok": False, "error": refused}
        except Exception as e:  # noqa: BLE001 — o unealtă picată e un rezultat, nu sfârșitul turului
            log.warning("assistant: unealta %s a picat: %s", name, type(e).__name__)
            refused = "tool_failed"
            out = {"ok": False, "error": "the tool failed; try again differently or answer without"}
        self.calls.append(
            {
                "tool": name,
                "ms": round((time.monotonic() - started) * 1000),
                **({"refused": refused} if refused else {}),
            }
        )
        return json.dumps(out, ensure_ascii=False, default=str)

    def gate(self, rows: list[dict[str, Any]], purpose: str) -> tuple[list[dict[str, Any]], str]:
        """NX-173: rândurile permise + o linie pentru model (ce s-a scos), fără text de client."""
        from src.safety.compose import model_hint  # noqa: PLC0415

        kept, decision = self.policy.gate(self.ctx, rows, purpose=purpose)
        return kept, model_hint(decision)

    def remember(self, rows: list[dict[str, Any]]) -> list[str]:
        """Faptele și handle-urile rândurilor (deja gated), în ordine."""
        handles = []
        for r in rows:
            pid = str(r["id"])
            self.facts.rows[pid] = {**self.facts.rows.get(pid, {}), **r}
            handles.append(self.memory.handle_of(pid))
        return handles

    def names(self) -> dict[str, str]:
        """handle → numele distinct, pe produsele ale căror fapte le are turul."""
        full = {
            h: str(self.facts.rows[pid].get("name") or "")
            for h, pid in self.memory.handles.items()
            if pid in self.facts.rows
        }
        return distinct_names(full)

    def view_rows(self, handles: list[str]) -> list[dict[str, Any]]:
        names = self.names()
        out = []
        for h in handles:
            p = self.facts.rows[self.memory.handles[h]]
            attrs = p.get("attributes") or {}
            keep = {
                k: v
                for k, v in attrs.items()
                if k not in ("compliance", "key_ingredients") and v not in (None, "", [], {})
            }
            row = {
                "handle": h,
                "name": names.get(h),
                "brand": p.get("brand"),
                "price": p.get("price"),
                "availability": p.get("availability"),
                "rating": p.get("rating"),
                "attributes": keep,
            }
            if p.get("lexical_step"):
                row["match"] = p["lexical_step"]
            out.append(row)
        return out

    async def detail_rows(self, ids: list[str]) -> list[dict[str, Any]]:
        """Fișele complete ale 2-4 produse (comparația), gated. Rândurile din căutare n-au toate
        faptele fișei, iar tabelul se construiește din ele."""
        from src.db.queries.catalog import get_products_by_ids  # noqa: PLC0415

        async with self.deps.db("assistant_compare") as conn:
            rows = await get_products_by_ids(conn, self.ctx.business.id, ids, limit=len(ids))
        kept, _hint = self.gate(rows, "retrieval_final")
        for r in kept:
            pid = str(r["id"])
            self.facts.rows[pid] = {**self.facts.rows.get(pid, {}), **r}
        return kept

    def _id(self, handle: Any) -> str:
        return self.memory.handles[self.memory.check(handle)]

    def _shown_id(self, handle: Any) -> str:
        pid = self._id(handle)
        if pid not in self.shown_ids:
            raise Refused("only a product you showed to the customer can be used here")
        return pid

    # --- uneltele --------------------------------------------------------------------------------

    async def _search_catalog(self, a: dict[str, Any]) -> dict[str, Any]:
        from src.db.queries.catalog import search_products_lexical  # noqa: PLC0415

        m = self.menus
        check(a.get("category"), m.categories, "category")
        check(a.get("brand"), m.brands, "brand")
        facets: dict[str, list[str]] = {}
        for need in a.get("needs") or []:
            check(need, m.needs, "needs")
            dim, _sep, value = need.partition(":")
            facets.setdefault(dim, []).append(value)
        types = list(a.get("product_types") or [])
        for t in types:
            check(t, m.product_types, "product_types")
        if types:
            facets["product_type"] = types
        sort = a.get("sort") or "relevance"
        check(sort, ("relevance", "price_asc", "price_desc", "rating_desc"), "sort")
        excluded = {self._id(h) for h in a.get("exclude") or []}
        self.facts.catalog_read = True
        async with self.deps.db("assistant_search") as conn:
            rows = await search_products_lexical(
                conn,
                self.ctx.business.id,
                str(a.get("query") or ""),
                category=a.get("category"),
                brand=a.get("brand"),
                facet_filters=facets or None,
                price_max=a.get("price_max"),
                sort_mode=sort,
                in_stock_only=bool(a.get("in_stock_only")),
                locale=self.ctx.business.default_locale or self.ctx.language,
                allow_filters_only=True,
                pool=SEARCH_POOL,
            )
        price_min = a.get("price_min")
        rows = [
            r
            for r in rows
            if str(r.get("id")) not in excluded
            and (price_min is None or (r.get("price") or 0) >= float(price_min))
        ]
        kept, hint = self.gate(rows[: SEARCH_ROWS * 2], "search")
        handles = self.remember(kept[:SEARCH_ROWS])
        out: dict[str, Any] = {"found": len(rows), "products": self.view_rows(handles)}
        if hint:
            out["safety"] = hint
        return out

    async def _product_details(self, a: dict[str, Any]) -> dict[str, Any]:
        from src.agent.detail_answer import product_facts  # noqa: PLC0415
        from src.db.queries.catalog import get_products_by_ids  # noqa: PLC0415

        handles = [self.memory.check(h) for h in (a.get("handles") or [])[:3]]
        if not handles:
            raise Refused("no handles: give 1-3 handles from PRODUCTS YOU KNOW")
        ids = [self.memory.handles[h] for h in handles]
        self.facts.catalog_read = True
        async with self.deps.db("assistant_details") as conn:
            rows = await get_products_by_ids(conn, self.ctx.business.id, ids, limit=len(ids))
        kept, hint = self.gate(rows, "details")
        self.remember(kept)
        by_id = {str(r["id"]): r for r in kept}
        names = self.names()
        pack = getattr(self.ctx.business, "domain_pack", None)
        sheets = {}
        for h, pid in zip(handles, ids, strict=True):
            row = by_id.get(pid)
            if row is None:
                sheets[h] = "not available"
                continue
            sheet = product_facts(row, pack, self.ctx.language)[:SHEET_MAX]
            sheets[h] = f"name on card: {names.get(h)}\n{sheet}"
        out: dict[str, Any] = {"sheets": sheets}
        if hint:
            out["safety"] = hint
        return out

    async def _store_rules(self, a: dict[str, Any]) -> dict[str, Any]:
        from src.tools.faq_tools import load_rules  # noqa: PLC0415

        self.facts.read_beyond_catalog = True
        rows = await load_rules(self.ctx, self.deps)
        rules = []
        for r in rows:
            answer = str(r.get("answer") or "").strip()
            if answer:
                self.facts.sources.append(answer)
                rules.append(f"{str(r.get('question') or '').strip()} -> {answer}")
        return {"rules": rules}

    async def _add_to_cart(self, a: dict[str, Any]) -> dict[str, Any]:
        pid = self._shown_id(a.get("handle"))
        qty = max(1, min(99, int(a.get("quantity") or 1)))
        return await self._mutation("cart_add", {"product_id": pid, "quantity": qty})

    async def _back_in_stock(self, a: dict[str, Any]) -> dict[str, Any]:
        pid = self._shown_id(a.get("handle"))
        return await self._mutation("subscribe_back_in_stock", {"product_id": pid})

    async def _mutation(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """Unealta de producție (coș, abonare), cu regulile ei: siguranța înaintea scrierii,
        revalidarea prețului și a stocului, plafoanele. Mutația din stare trece prin
        `ctx.state_patch`, ca la `ToolRun`."""
        from src.tools.base import run_tool  # noqa: PLC0415

        events_before = len(self.ctx.events)
        result = await run_tool(self.ctx, self.deps, tool, args)
        if result.state_patch:
            self.ctx.state_patch.update(result.state_patch)
        if result.ok:
            self.facts.mutated = True
            self.facts.mutation_patch.update(result.state_patch or {})
            self.facts.mutation_events += self.ctx.events[events_before:]
            self.facts.mutations.append((tool, str(args.get("product_id") or "")))
        self.facts.order_prices += [float(p) for p in result.prices or []]
        return {"ok": result.ok, "result": result.llm_view, "error": result.error}

    async def _check_order(self, a: dict[str, Any]) -> dict[str, Any]:
        from src.tools.base import run_tool  # noqa: PLC0415

        self.facts.read_beyond_catalog = True
        ref = a.get("order_ref")
        result = await run_tool(
            self.ctx, self.deps, "check_order", {"order_ref": str(ref)[:64] if ref else None}
        )
        self.facts.order_prices += [float(p) for p in result.prices or []]
        self.facts.order_found = self.facts.order_found or bool(result.ok and result.prices)
        return {"ok": result.ok, "result": result.llm_view, "error": result.error}

    async def _routine_plan(self, a: dict[str, Any]) -> dict[str, Any]:
        """`run_planned_routine`: argumentele agentului nu se re-judecă pe text (NX-333), pașii și
        produsul de pe fiecare pas sunt ai magazinului, siguranța e în unealtă. Id-urile din vedere
        devin handle-uri."""
        from src.tools.routine_tools import RoutineArgs, run_planned_routine  # noqa: PLC0415

        m = self.menus
        check(a.get("family"), m.families, "family")
        if a.get("moment") is not None:
            check(a.get("moment"), m.moments, "moment")
        values = []
        for need in a.get("needs") or []:
            check(need, m.needs, "needs")
            values.append(need.partition(":")[2])
        anchor = self._id(a["anchor"]) if a.get("anchor") else None
        args = RoutineArgs(
            family=a["family"],
            concerns=values,
            budget_max=a.get("budget_max"),
            anchor_id=anchor,
            moment=a.get("moment"),
            steps=[s for s in a.get("steps") or [] if s in m.steps] or None,
        )
        self.facts.catalog_read = True
        result = await run_planned_routine(self.ctx, self.deps, args)
        handles = self.remember(list(result.products or []))
        view = result.llm_view or ""
        for h in handles:
            view = view.replace(f"[{self.memory.handles[h]}]", f"[{h}]")
        out: dict[str, Any] = {
            "ok": result.ok,
            "routine": view,
            "products": self.view_rows(handles),
        }
        if result.error:
            out["error"] = result.error
        return out
