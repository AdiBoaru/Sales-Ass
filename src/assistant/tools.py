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
import re
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
#: NX-403: atributele care nu spun nimic clientului (coduri de catalog, sursa prețului pe unitate)
#: nu ajung la model; ingredientele cheie vin separat, tăiate la `KEY_INGREDIENTS`.
NOISE_ATTRIBUTES = frozenset({"compliance", "key_ingredients", "mpn", "sku", "gtin", "ean"})
NOISE_SUFFIX = "_source"
KEY_INGREDIENTS = 5
#: Ce laudă recenziile (`product_review_summaries.top_pros`, NX-279), câte puncte pe produs.
REVIEW_POINTS = 3
#: NX-407: recenziile REALE. Temele din `top_pros` sunt 20 pe tot catalogul SOLE («textură
#: ușoară și plăcută» pe 1.138 de produse), deci motivele cardurilor sunau la fel; o recenzie
#: spune ce observă clientul pe ACEST produs. Un fragment pe rândul de căutare, două pe fișă.
REVIEW_EXCERPTS_ROW = 1
REVIEW_EXCERPTS_SHEET = 2
#: Câte recenzii se citesc pe produs: o recenzie care nu se poate reda (`relayable`) se sare.
REVIEW_CANDIDATES = 3
_HANDLE = re.compile(r"\bP[1-9][0-9]{0,3}\b")
#: Câte variante ale unei familii primesc handle și apar pe rândul ei.
MAX_VERSIONS = 6


@dataclass
class Facts:
    """Ce a citit turul: sursa porții de adevăr și a răspunsului."""

    rows: dict[str, dict[str, Any]] = field(default_factory=dict)  # product_id → rând gated
    sources: list[str] = field(default_factory=list)  # răspunsurile regulilor citite (NX-346)
    #: NX-403: tot ce au întors uneltele în tur, așa cum l-a citit modelul. O cifră a răspunsului
    #: se judecă pe ce a văzut agentul (poarta pe fapte, `gate.judge`), nu pe o listă de cuvinte.
    seen: list[str] = field(default_factory=list)
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
    #: NX-404/406: locul fiecărui produs în ULTIMA listă clasată a turului în care a apărut
    #: (poziție de la 1, din câți candidați) și starea nevoilor cerute în acea listă. Fișa îl
    #: poartă, `assistant_turn.card_positions` îl măsoară. Doar produsele unei liste din ACEST tur.
    ranks: dict[str, dict[str, Any]] = field(default_factory=dict)
    needs: dict[str, list[str]] = field(default_factory=dict)
    #: NX-404: id-ul reprezentantului unei familii → handle-urile celorlalte variante (nuanțe,
    #: gramaje), ca agentul să le poată compara sau pune pe card fără câte un rând fiecare.
    versions: dict[str, list[str]] = field(default_factory=dict)
    #: Câți candidați au rămas în ultima listă, după nepotrivire și siguranță (`found` al căutării).
    listed: int = 0
    #: NX-409: în ultima listă, (familii care poartă nevoile cerute, câte dintre ele sunt arătate).
    need_matches: tuple[int, int] = (0, 0)


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
        text = json.dumps(out, ensure_ascii=False, default=str)
        if refused is None:
            # `found` e un contor al nostru, nu un fapt de catalog: lăsat în fapte, «ai 40 de zile
            # de retur» trecea pe „found: 40” (recenzia NX-403).
            # NX-409: și numărătoarea potrivirilor
            counters = ("found", "need_matches")
            seen = out
            if isinstance(out, dict):
                seen = {k: v for k, v in out.items() if k not in counters}
            # NX-407: fără textul recenziilor (o cifră scrisă de un client nu e un fapt)
            seen = _without_reviews(seen)
            self.facts.seen.append(json.dumps(seen, ensure_ascii=False, default=str))
        return text

    def gate(self, rows: list[dict[str, Any]], purpose: str) -> tuple[list[dict[str, Any]], str]:
        """NX-173: rândurile permise + o linie pentru model (ce s-a scos), fără text de client."""
        from src.safety.compose import model_hint  # noqa: PLC0415

        kept, decision = self.policy.gate(self.ctx, rows, purpose=purpose)
        return kept, model_hint(decision)

    def present(
        self,
        rows: list[dict[str, Any]],
        needs: dict[str, list[str]],
        *,
        purpose: str,
        limit: int,
        sort: str = "relevance",
        order: bool = True,
        gated: bool = False,
    ) -> tuple[list[str], str]:
        """NX-404: PÂLNIA prin care trece orice LISTĂ de produse pe care o primește agentul, din
        orice unealtă: rankingul (`search.rank_candidates`: nepotrivitul iese, potrivirea înaintea
        necunoscutului, rankingul producției, o familie pe rând; `order=False` păstrează ordinea
        uneltei, ca pașii unei rutine), siguranța (NX-173; `gated=True` pentru o listă pe care
        unealta de producție a trecut-o deja prin ea, ca decizia turului să nu numere de două ori),
        handle-urile, locul în listă. Întoarce handle-urile primelor `limit` și linia de siguranță
        pentru model."""
        from src.assistant.search import (  # noqa: PLC0415
            family_key,
            group_families,
            need_status,
            rank_candidates,
        )

        if order:
            ranked = rank_candidates(rows, needs, sort=sort, weights=self.rank_weights())
        else:
            ranked = [dict(r) for r in rows]
        # Siguranța pe TOȚI candidații, apoi familiile: tăiată la primele N rânduri, două-trei
        # familii cu multe nuanțe umpleau tăietura și agentul vedea 2-3 produse (recenzia NX-404).
        kept, hint = (ranked, "") if gated else self.gate(ranked, purpose)
        groups = group_families(kept, limit) if order else [(r, []) for r in kept[:limit]]
        # Fiecare produs își ține locul din ULTIMA listă în care a apărut, cu starea nevoii de
        # atunci în aceeași intrare, deci două căutări cu nevoi diferite nu se amestecă. NX-406:
        # golirea tabelului la fiecare listă lăsa fără loc cardurile alese dintr-o căutare
        # anterioară (pe smoke-ul de după deploy, 3 căutări ⇒ `card_positions` [None, None, None]).
        self.facts.needs = dict(needs)
        self.facts.versions = {}
        self.facts.listed = len(kept)
        # NX-409: câte familii POARTĂ nevoile în toată lista (după excludere și siguranță) și câte
        # dintre ele sunt pe rândurile arătate. Sens doar când căutarea a adus tot setul nevoilor.
        matching = {family_key(r) or str(r["id"]) for r in kept if need_status(r, needs) == "match"}
        self.facts.need_matches = (
            len(matching),
            sum(1 for rep, _versions in groups if need_status(rep, needs) == "match"),
        )
        handles = self.remember([rep for rep, _versions in groups])
        for i, (rep, versions) in enumerate(groups, 1):
            others = self.remember(versions[:MAX_VERSIONS])
            self.facts.versions[str(rep["id"])] = others
            for r in (rep, *versions[:MAX_VERSIONS]):
                self.facts.ranks[str(r["id"])] = {
                    "position": i,
                    "of": len(kept),
                    "need": need_status(r, needs),
                }
        # Memoria păstrează cele mai recente `MAX_HANDLES`: reprezentanții se ating la urmă, cel
        # mai bun ultimul, ca variantele să nu-i scoată (recenzia NX-404: familiile 1-4 dispăreau).
        for rep, _versions in reversed(groups):
            self.memory.handle_of(str(rep["id"]))
        return handles, hint

    def rank_weights(self) -> dict[str, float] | None:
        """Ponderile rankingului: ale pachetului (`DomainPack.rank_weights`), peste cele ale
        producției; `None` cu kill-switch-ul `SEARCH_BLENDED_RANK_ENABLED` stins. Același
        proprietar ca pe calea veche (`catalog_tools._rank_weights`)."""
        from src.tools.catalog_tools import _rank_weights  # noqa: PLC0415

        return _rank_weights(self.ctx)

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
                if k not in NOISE_ATTRIBUTES
                and not k.endswith(NOISE_SUFFIX)
                and v not in (None, "", [], {})
            }
            row = {
                "handle": h,
                "name": names.get(h),
                "brand": p.get("brand"),
                "price": p.get("price"),
                "availability": p.get("availability"),
                "rating": p.get("rating"),
                "reviews": p.get("review_count"),
                "attributes": keep,
            }
            # NX-403: ce spun clienții și ce conține produsul sunt motivele pe care le dă un
            # vânzător; fără ele, motivul cardului repeta filtrele căutării («pentru ten gras»).
            praise = [str(x).strip() for x in p.get("top_pros") or [] if str(x).strip()]
            if praise:
                row["reviews_praise"] = praise[:REVIEW_POINTS]
            key = [str(x).strip() for x in attrs.get("key_ingredients") or [] if str(x).strip()]
            if key:
                row["key_ingredients"] = key[:KEY_INGREDIENTS]
            said = _excerpts(p, REVIEW_EXCERPTS_ROW)
            if said:
                row["customers_say"] = said
            if p.get("lexical_step"):
                row["match"] = p["lexical_step"]
            # NX-404: starea nevoilor cerute în căutare (`unknown` = fișa nu spune) și câte variante
            # (nuanțe, gramaje) are familia pe care o reprezintă rândul.
            rank = self.facts.ranks.get(str(p.get("id")))
            if rank and rank["need"] != "n/a":
                row["need"] = rank["need"]
            others = self.facts.versions.get(str(p.get("id"))) or []
            if others:
                row["versions"] = [{"handle": o, "name": names.get(o)} for o in others]
            out.append(row)
        return out

    def facts_text(self) -> str:
        """NX-403: faptele turului ca text, sursa cifrelor din răspuns: fișa fiecărui produs
        cunoscut (nume, atribute, ingrediente, recenzii), tot ce au întors uneltele și regulile
        citite. Handle-urile (`P12`) se scot: sunt eticheta noastră, nu un număr din catalog."""
        parts = [self.product_text(pid) for pid in self.facts.rows]
        parts += self.facts.seen
        parts += self.facts.sources
        return _HANDLE.sub("P", "\n".join(parts))

    def product_text(self, pid: str) -> str:
        """Faptele UNUI produs ca text (fișa + atributele brute): sursa cifrelor din motivul
        cardului lui, ca un SPF sau un gramaj al altui produs să nu treacă pe cardul ăsta."""
        from src.agent.detail_answer import product_facts  # noqa: PLC0415

        row = self.facts.rows.get(pid)
        if row is None:
            return ""
        pack = getattr(self.ctx.business, "domain_pack", None)
        # NX-407 (recenzia): fără recenzii. O cifră scrisă de un client («după 14 zile», «SPF 50»
        # despre crema lui de dinainte) nu e un fapt al produsului, deci nu întemeiază nimic.
        text = product_facts(row, pack, self.ctx.language)
        attrs = row.get("attributes") or {}
        if attrs:
            text += "\n" + json.dumps(attrs, ensure_ascii=False, default=str)
        return text

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

    async def attach_reviews(self, ids: list[str]) -> None:
        """NX-407: recenziile reale ale produselor din listă, într-o singură citire, pe rândurile
        care nu le au deja (fișa le aduce singură). Citirea picată lasă rândurile fără fragment:
        agentul răspunde din celelalte fapte, iar evenimentul o face vizibilă."""
        from src.db.queries.catalog import review_excerpts  # noqa: PLC0415

        missing = [pid for pid in ids if "reviews_list" not in self.facts.rows.get(pid, {})]
        if not missing:
            return
        try:
            async with self.deps.db("assistant_reviews") as conn:
                found = await review_excerpts(
                    conn, self.ctx.business.id, missing, per_product=REVIEW_CANDIDATES
                )
        except Exception as e:  # noqa: BLE001 — fragmentele sunt un plus, nu o condiție a turului
            log.warning("assistant: recenziile n-au putut fi citite: %s", type(e).__name__)
            self.ctx.emit("assistant_reviews", outcome="error", error=type(e).__name__)
            return
        for pid in missing:
            self.facts.rows[pid]["reviews_list"] = found.get(pid, [])

    def _id(self, handle: Any) -> str:
        return self.memory.handles[self.memory.check(handle)]

    def _shown_id(self, handle: Any) -> str:
        pid = self._id(handle)
        if pid not in self.shown_ids:
            raise Refused("only a product you showed to the customer can be used here")
        return pid

    # --- uneltele --------------------------------------------------------------------------------

    async def _search_catalog(self, a: dict[str, Any]) -> dict[str, Any]:
        """NX-404: candidații (`search.fetch_candidates`: nevoile spuse nu mai scot produsele
        fără atribut, scara de text nu se oprește la un singur rezultat) trec prin pâlnie
        (`present`): rankingul producției, potrivirea înaintea necunoscutului, o familie pe rând."""
        from src.assistant.search import fetch_candidates, split_needs  # noqa: PLC0415
        from src.db.provider import is_shared_connection  # noqa: PLC0415

        m = self.menus
        check(a.get("category"), m.categories, "category")
        check(a.get("brand"), m.brands, "brand")
        for need in a.get("needs") or []:
            check(need, m.needs, "needs")
        for t in a.get("product_types") or []:
            check(t, m.product_types, "product_types")
        sort = a.get("sort") or "relevance"
        check(sort, ("relevance", "price_asc", "price_desc", "rating_desc"), "sort")
        excluded = {self._id(h) for h in a.get("exclude") or []}
        self.facts.catalog_read = True
        stats: dict[str, Any] = {}
        rows = await fetch_candidates(
            lambda: self.deps.db("assistant_search"),
            self.ctx.business.id,
            a,
            locale=self.ctx.business.default_locale or self.ctx.language,
            parallel=not is_shared_connection(getattr(self.deps, "db", None)),
            stats=stats,
        )
        price_min = a.get("price_min")
        rows = [
            r
            for r in rows
            if str(r.get("id")) not in excluded
            and (price_min is None or (r.get("price") or 0) >= float(price_min))
        ]
        handles, hint = self.present(
            rows, split_needs(a.get("needs") or []), purpose="search", limit=SEARCH_ROWS, sort=sort
        )
        await self.attach_reviews([self.memory.handles[h] for h in handles])
        out: dict[str, Any] = {"found": self.facts.listed, "products": self.view_rows(handles)}
        if stats.get("need_counted"):
            # NX-409: agentul află dacă mai există produse care spun nevoia, ca să nu caute din nou
            # ce nu e în magazin. Un contor al nostru, nu un fapt de catalog (ca `found`).
            total, here = self.facts.need_matches
            out["need_matches"] = {
                "in_store": f"{total}+" if stats.get("need_pool_full") else total,
                "in_this_list": here,
            }
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
            sheet = product_facts(row, pack, self.ctx.language, reviews=REVIEW_EXCERPTS_SHEET)
            sheet = sheet[:SHEET_MAX]
            sheets[h] = f"name on card: {names.get(h)}\n{self._rank_line(pid, row)}{sheet}"
        out: dict[str, Any] = {"sheets": sheets}
        if hint:
            out["safety"] = hint
        return out

    def _rank_line(self, pid: str, row: dict[str, Any]) -> str:
        """NX-404: ce a stabilit lista clasată a turului despre produs, pe fișa lui: starea
        nevoilor cerute (fișa citită singură ar lăsa „pentru ten gras” afirmat pe un produs care
        nu spune asta) și locul în listă. Starea vine din lista în care a apărut produsul (cu
        nevoile ei); un produs din afara listelor turului e judecat pe nevoile ultimei liste. Fără
        o listă în tur, nimic."""
        from src.assistant.search import need_status  # noqa: PLC0415

        lines = []
        rank = self.facts.ranks.get(pid)
        need = rank["need"] if rank else need_status(row, self.facts.needs)
        if need != "n/a":
            lines.append(f"need: {need}")
        if rank:
            lines.append(f"position in this turn's list: {rank['position']} of {rank['of']}")
        return "".join(f"{line}\n" for line in lines)

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
        from src.assistant.search import split_needs  # noqa: PLC0415
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
        # NX-404: prin pâlnie pentru starea nevoilor, fără reordonare (pașii sunt ai magazinului)
        # și fără a doua trecere prin siguranță (unealta rutinei o aplică deja).
        products = list(result.products or [])
        handles, _hint = self.present(
            products,
            split_needs(a.get("needs") or []),
            purpose="routine",
            limit=len(products),
            order=False,
            gated=True,
        )
        await self.attach_reviews([self.memory.handles[h] for h in handles])
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


def _excerpts(row: dict[str, Any], n: int) -> list[str]:
    """NX-407: primele `n` recenzii redabile ale rândului ca fragmente (fără autor, fără notă).
    Aceeași regulă ca pe fișă (`detail_answer.relayable`). PUR."""
    from src.agent.detail_answer import relayable, review_excerpt  # noqa: PLC0415

    out = []
    for item in row.get("reviews_list") or []:
        text = review_excerpt(item.get("body")) if isinstance(item, dict) else ""
        if text and relayable(text):
            out.append(text)
        if len(out) >= n:
            break
    return out


def _without_reviews(out: Any) -> Any:
    """NX-407 (recenzia): ieșirea unei unelte fără textul recenziilor, pentru faptele porții.
    Agentul le citește, dar o cifră dintr-o recenzie (o durată, gramajul altei creme) nu e un fapt
    al produsului: rămâne neîntemeiată, ca înainte. PUR."""
    from src.agent.detail_answer import REVIEW_LINE  # noqa: PLC0415

    if not isinstance(out, dict):
        return out
    clean = dict(out)
    if isinstance(clean.get("products"), list):
        clean["products"] = [
            {k: v for k, v in r.items() if k != "customers_say"} if isinstance(r, dict) else r
            for r in clean["products"]
        ]
    if isinstance(clean.get("sheets"), dict):
        clean["sheets"] = {
            h: "\n".join(x for x in str(v).splitlines() if not x.startswith(REVIEW_LINE))
            for h, v in clean["sheets"].items()
        }
    return clean
