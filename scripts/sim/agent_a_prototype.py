"""NX-393 — prototipul agentului unic (varianta A): modelul înțelege, caută, verifică și răspunde.

    PYTHONPATH=. python scripts/sim/agent_a_prototype.py --set tests/golden/prod_sets/<set>.json
    PYTHONPATH=. python scripts/sim/agent_a_prototype.py --set … --kernel-report <raport> --yes

Fără `--yes` e dry-run: încarcă meniurile din catalog (rafturi, tipuri, mărci, nevoi), construiește
uneltele și spune câte conversații și ture ar rula, fără niciun apel de model. Cu `--yes`, fiecare
conversație din set rulează prin agent, tur cu tur, pe catalogul REAL, doar cu citiri. Coșul e
simulat: nu se scrie nimic în baza de date.

Ce face agentul (un singur model, `gpt-6-luna`, cu raționament pe `/v1/responses`):
- înțelege mesajul, oricum ar fi scris, și decide singur ce face;
- caută cu `search_catalog`: text + filtre din MENIURI ÎNCHISE generate din catalog, citește
  rezultatele (tipul, nevoile, prețul, stocul), iar dacă nu se potrivesc reformulează sau schimbă
  filtrele (cel mult `MAX_ROUNDS` runde pe tur);
- citește fișa întreagă cu `product_details`, regulile magazinului cu `store_rules`;
- încheie mereu cu `answer`: textul pentru client + produsele arătate ca carduri.

Codul face doar: execuția uneltelor (SQL tenant-scoped, read-only), meniurile închise, regula
coșului (doar produse arătate) și porțile de ADEVĂR pe răspuns (prețuri, linkuri, afirmații
medicale), care se raportează lângă text. Nicio frază fixă, nicio decizie peste model.

Ieșirea: `reports/nx393/agent-<set>-<stamp>.json` și `.md`, cu răspunsul kernelului (din raportul
`prod_set_run.py` dat la `--kernel-report`) lângă cel al agentului, tur cu tur. Rularea o pornește
Adi (credite). Conversațiile rulează pe rând, ca citirile să nu țină sesiuni ale poolerului."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "reports" / "nx393"
#: Câte runde de model are un tur (căutări, reformulări, fișe, răspuns).
MAX_ROUNDS = 6
#: Câte produse vede agentul dintr-o căutare și câte poate arăta clientului.
SEARCH_ROWS = 8
MAX_SHOWN = 6
#: Câte valori dintr-o fațetă intră în meniul de nevoi (cele mai frecvente).
FACET_VALUES = 25
#: Fațetele care nu sunt nevoi ale clientului (raftul și tipul au meniul lor).
_NOT_NEEDS = frozenset({"category", "product_type", "compliance"})
_ROLES = {"inbound": "client", "outbound": "asistent"}

INSTRUCTIONS = """\
You are the shopping assistant of the online store "{store}". You talk with one customer in a chat.
Write to the customer in locale "{locale}", in the customer's language, short and natural.

How you work:
- Understand what the customer wants in this message, using the conversation so far. They may write
  without diacritics, with typos, briefly, or ask several things at once: answer all of them.
- To recommend or look for products, use search_catalog. Filters come only from the closed menus in
  the tool schema. Read what comes back: the type of each product, what it is for, price, stock.
- If the results do not fit the request (wrong kind of product, wrong purpose, over budget, not in
  stock when that matters), search again: other words, another filter, or drop a filter that is too
  narrow. A few searches are fine. If nothing fits, say so honestly and offer the closest option.
- For a question about a product (how to use it, ingredients, whether it suits something), read its
  sheet with product_details and answer from it. If the sheet does not say, say you cannot confirm.
- For a question about the store itself (delivery, returns, payment, vouchers), use store_rules and
  answer from those rules only.
- For a general question (whether a step is needed, the difference between two kinds of products),
  answer it directly with general know-how; you may show products when that helps.
- Ask one short question only when you truly cannot go on without it.
- add_to_cart only when the customer asks to buy or add an item, and only an item you showed.
- Every price, ingredient, size or other fact you write comes from tool results. Never invent one.
  No medical claims (treats, cures, safe in pregnancy). Never promise anything the store does not
  state in its rules.
- Finish EVERY turn with exactly one call to answer: the text for the customer and the products to
  show as cards (only products that fit what the customer asked, at most {max_shown}).

{voice}
"""


def load_set(path: Path) -> dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    for conv in doc["conversations"]:
        if not conv.get("id") or not conv.get("turns"):
            raise SystemExit(f"conversație fără id sau fără ture în {path}")
    return doc


# --- meniurile închise ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Menus:
    categories: tuple[str, ...]
    product_types: tuple[str, ...]
    brands: tuple[str, ...]
    needs: tuple[str, ...]  # „fațetă:valoare”

    def summary(self) -> str:
        return (
            f"{len(self.categories)} rafturi, {len(self.product_types)} tipuri, "
            f"{len(self.brands)} mărci, {len(self.needs)} nevoi"
        )


async def load_menus(deps: Any, business_id: str) -> Menus:
    from src.catalog.vocabulary_cache import get_vocabulary  # noqa: PLC0415

    vocab = await get_vocabulary(deps, business_id)
    categories = tuple(e.key for e in vocab.categories if e.count > 0)
    types = tuple(e.key for e in vocab.entries("product_type") if e.count > 0)
    needs: list[str] = []
    for dim in vocab.facet_names:
        if dim in _NOT_NEEDS:
            continue
        top = sorted(vocab.entries(dim), key=lambda e: (-e.count, e.key))[:FACET_VALUES]
        needs += [f"{dim}:{e.key}" for e in top if e.count > 0]
    async with deps.db("agent_brands") as conn:
        rows = await conn.fetch(
            """
            select b.name, count(*) as n from products p
              join brands b on b.id = p.brand_id and b.business_id = p.business_id
             where p.business_id = $1 and p.status = 'active'
             group by b.name order by b.name
            """,
            business_id,
        )
    return Menus(categories, types, tuple(r["name"] for r in rows), tuple(needs))


def tool_schemas(menus: Menus) -> list[dict[str, Any]]:
    def nullable(schema: dict[str, Any]) -> dict[str, Any]:
        return {"anyOf": [schema, {"type": "null"}]}

    search = {
        "name": "search_catalog",
        "description": (
            "Search the store's catalog. Returns up to 8 products with the facts that decide "
            "whether they fit (type, needs, price, stock). `match` says how the text matched: "
            "strict, relaxed, typo or filters_only (no text match, only the filters)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "words to search for"},
                "category": nullable({"type": "string", "enum": list(menus.categories)}),
                "product_types": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(menus.product_types)},
                },
                "brand": nullable({"type": "string", "enum": list(menus.brands)}),
                "needs": {
                    "type": "array",
                    "description": "hard filters, facet:value",
                    "items": {"type": "string", "enum": list(menus.needs)},
                },
                "price_max": nullable({"type": "number"}),
                "price_min": nullable({"type": "number"}),
                "in_stock_only": {"type": "boolean"},
                "sort": {
                    "type": "string",
                    "enum": ["relevance", "price_asc", "price_desc", "rating_desc"],
                },
                "exclude": {
                    "type": "array",
                    "description": "handles of products not to return (already shown)",
                    "items": {"type": "string"},
                },
            },
            "required": ["query"],
        },
    }
    details = {
        "name": "product_details",
        "description": "The full sheet of up to 3 products (usage, ingredients, attributes).",
        "parameters": {
            "type": "object",
            "properties": {"handles": {"type": "array", "items": {"type": "string"}}},
            "required": ["handles"],
        },
    }
    rules = {
        "name": "store_rules",
        "description": "All active store rules (delivery, returns, payment, vouchers).",
        "parameters": {"type": "object", "properties": {}},
    }
    cart = {
        "name": "add_to_cart",
        "description": "Add a product you showed to the customer's cart.",
        "parameters": {
            "type": "object",
            "properties": {
                "handle": {"type": "string"},
                "quantity": {"type": "integer", "minimum": 1},
            },
            "required": ["handle"],
        },
    }
    answer = {
        "name": "answer",
        "description": "Finish the turn: the text the customer reads and the products shown.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "show": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["text", "show"],
        },
    }
    return [
        {"type": "function", **t, "strict": False} for t in (search, details, rules, cart, answer)
    ]


# --- starea conversației (a scriptului, nu a modelului) ------------------------------------------


@dataclass
class Conversation:
    """Ce știe codul despre conversație: handle-urile produselor văzute de agent (`P1`…), ce a
    arătat clientului, coșul simulat și istoricul. Agentul primește handle-uri, nu UUID-uri
    (NX-324: id-urile copiate greșit scoteau carduri)."""

    handles: dict[str, str] = field(default_factory=dict)  # handle -> product_id
    facts: dict[str, dict[str, Any]] = field(default_factory=dict)  # product_id -> rând
    shown: list[str] = field(default_factory=list)  # handle-uri, în ordinea arătării
    shown_sets: list[list[str]] = field(default_factory=list)  # cardurile fiecărui tur
    cart: dict[str, int] = field(default_factory=dict)  # handle -> cantitate
    history: list[tuple[str, str]] = field(default_factory=list)  # (rol, text)

    def handle_of(self, product_id: str) -> str:
        for h, pid in self.handles.items():
            if pid == product_id:
                return h
        h = f"P{len(self.handles) + 1}"
        self.handles[h] = product_id
        return h

    def view(self) -> str:
        lines = []
        if self.shown:
            lines.append("PRODUCTS SHOWN TO THE CUSTOMER (latest last)")
            for h in self.shown[-12:]:
                p = self.facts.get(self.handles[h], {})
                lines.append(f"- {h}: {_name(p)}, {p.get('price')} lei, {p.get('availability')}")
        if self.cart:
            lines.append("CART: " + ", ".join(f"{h} x{q}" for h, q in self.cart.items()))
        if self.history:
            lines.append("HISTORY")
            lines += [f"{role}: {text}" for role, text in self.history[-20:]]
        return "\n".join(lines)


def _name(product: dict[str, Any]) -> str:
    from src.catalog.render_text import display_name  # noqa: PLC0415

    return display_name(str(product.get("name") or ""))


def _row_view(h: str, p: dict[str, Any]) -> dict[str, Any]:
    attrs = p.get("attributes") or {}
    keep = {
        k: v
        for k, v in attrs.items()
        if k not in ("compliance", "key_ingredients") and v not in (None, "", [], {})
    }
    out = {
        "handle": h,
        "name": _name(p),
        "brand": p.get("brand"),
        "price": p.get("price"),
        "availability": p.get("availability"),
        "rating": p.get("rating"),
        "attributes": keep,
    }
    if p.get("lexical_step"):
        out["match"] = p["lexical_step"]
    return out


# --- uneltele ------------------------------------------------------------------------------------


class Tools:
    def __init__(self, deps: Any, business: Any, menus: Menus, conv: Conversation) -> None:
        self.deps, self.business, self.menus, self.conv = deps, business, menus, conv
        self.calls: list[dict[str, Any]] = []

    async def run(self, name: str, args: dict[str, Any]) -> str:
        started = time.monotonic()
        try:
            out = await getattr(self, f"_{name}")(args)
        except _Refused as e:
            out = {"ok": False, "error": str(e)}
        self.calls.append(
            {"tool": name, "args": args, "ms": round((time.monotonic() - started) * 1000)}
        )
        return json.dumps(out, ensure_ascii=False, default=str)

    async def _search_catalog(self, a: dict[str, Any]) -> dict[str, Any]:
        from src.db.queries.catalog import search_products_lexical  # noqa: PLC0415

        m = self.menus
        _check(a.get("category"), m.categories, "category")
        _check(a.get("brand"), m.brands, "brand")
        types = list(a.get("product_types") or [])
        for t in types:
            _check(t, m.product_types, "product_types")
        facets: dict[str, list[str]] = {}
        for need in a.get("needs") or []:
            _check(need, m.needs, "needs")
            dim, _sep, value = need.partition(":")
            facets.setdefault(dim, []).append(value)
        if types:
            facets["product_type"] = types
        async with self.deps.db("agent_search") as conn:
            rows = await search_products_lexical(
                conn,
                self.business.id,
                str(a.get("query") or ""),
                category=a.get("category"),
                brand=a.get("brand"),
                facet_filters=facets or None,
                price_max=a.get("price_max"),
                sort_mode=a.get("sort") or "relevance",
                in_stock_only=bool(a.get("in_stock_only")),
                locale=self.business.default_locale or "ro",
                allow_filters_only=True,
                pool=40,
            )
        excluded = {self.conv.handles.get(h) for h in a.get("exclude") or []}
        price_min = a.get("price_min")
        rows = [
            r
            for r in rows
            if r.get("id") not in excluded
            and (price_min is None or (r.get("price") or 0) >= float(price_min))
        ]
        out = []
        for r in rows[:SEARCH_ROWS]:
            self.conv.facts[str(r["id"])] = r
            out.append(_row_view(self.conv.handle_of(str(r["id"])), r))
        return {"found": len(rows), "products": out}

    async def _product_details(self, a: dict[str, Any]) -> dict[str, Any]:
        from src.agent.detail_answer import product_facts  # noqa: PLC0415
        from src.db.queries.catalog import get_products_by_ids  # noqa: PLC0415

        handles = [h for h in (a.get("handles") or [])[:3] if h in self.conv.handles]
        if not handles:
            raise _Refused("unknown handles: use handles from search results")
        ids = [self.conv.handles[h] for h in handles]
        async with self.deps.db("agent_details") as conn:
            rows = await get_products_by_ids(conn, self.business.id, ids, limit=len(ids))
        by_id = {str(r["id"]): r for r in rows}
        sheets = {}
        for h, pid in zip(handles, ids, strict=True):
            row = by_id.get(pid)
            if row is None:
                continue
            self.conv.facts[pid] = {**self.conv.facts.get(pid, {}), **row}
            sheets[h] = product_facts(row, self.business.domain_pack, "ro")[:3000]
        return {"sheets": sheets}

    async def _store_rules(self, a: dict[str, Any]) -> dict[str, Any]:
        from src.db.queries.faqs import list_active  # noqa: PLC0415

        locale = self.business.default_locale or "ro"
        async with self.deps.db("agent_rules") as conn:
            rows = await list_active(conn, self.business.id, locale, limit=60)
        return {"rules": [f"{r['question']} -> {r['answer']}" for r in rows]}

    async def _add_to_cart(self, a: dict[str, Any]) -> dict[str, Any]:
        h = str(a.get("handle") or "")
        if h not in self.conv.shown:
            raise _Refused("only a product you showed to the customer can go to the cart")
        self.conv.cart[h] = self.conv.cart.get(h, 0) + int(a.get("quantity") or 1)
        return {"ok": True, "cart": dict(self.conv.cart)}

    async def _answer(self, a: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}


class _Refused(Exception):
    pass


def _cart_total(conv: Conversation) -> float | None:
    total = 0.0
    for h, qty in conv.cart.items():
        price = conv.facts.get(conv.handles.get(h, ""), {}).get("price")
        if price is None:
            return None
        total += float(price) * qty
    return round(total, 2) if conv.cart else None


def _check(value: Any, menu: tuple[str, ...], name: str) -> None:
    if value is not None and value not in menu:
        raise _Refused(f"{name}={value!r} is not in the menu; use a listed value or leave it out")


# --- porțile de adevăr pe răspuns ----------------------------------------------------------------


def truth_gate(
    text: str,
    products: list[dict[str, Any]],
    shown_sets: list[list[dict[str, Any]]],
    cart_total: float | None,
    client_text: str = "",
) -> list[str]:
    """Aceleași porți ca producția, pe toate produsele citite de agent în conversație. Sumele
    permise în plus: totalul fiecărui set arătat (NX-391: pe producție, ecranul și seturile de mai
    devreme), totalul coșului simulat și sumele scrise de CLIENT (bugetul lui, citat înapoi).
    Prima rulare (2026-10-09) a semnalat fals totalul corect al rutinei de la turul anterior
    (400 lei) și bugetul «sub 100 lei»."""
    from src.agent.validator import (  # noqa: PLC0415
        _PRICE_RE,
        _links_ok,
        _prices_ok,
        parse_amount,
        set_total,
    )
    from src.worker.text_scrub import has_medical_claim  # noqa: PLC0415

    reasons = []
    rows = [{"id": p.get("id"), "price": p.get("price"), "url": p.get("url")} for p in products]
    sums = {s for s in (*(set_total(x) for x in shown_sets), cart_total) if s is not None}
    for m in _PRICE_RE.finditer(client_text):
        sums.add(parse_amount(m.group(1) or m.group(2)))
    if not _prices_ok(text, rows, sums):
        reasons.append("ungrounded_price")
    if not _links_ok(text, rows):
        reasons.append("unknown_link")
    if has_medical_claim(text):
        reasons.append("medical_claim")
    return reasons


# --- un tur --------------------------------------------------------------------------------------


async def run_turn(
    llm: Any, tools: Tools, conv: Conversation, message: str, *, store: str, effort: str
) -> dict[str, Any]:
    from src.agent.llm import responses_tools  # noqa: PLC0415
    from src.agent.voice import VOICE_RULES  # noqa: PLC0415

    instructions = INSTRUCTIONS.format(
        store=store, locale="ro", max_shown=MAX_SHOWN, voice=VOICE_RULES.strip()
    )
    context = conv.view()
    user = (
        f"{context}\n\nCUSTOMER MESSAGE\n{message}" if context else f"CUSTOMER MESSAGE\n{message}"
    )
    items: list[Any] = [{"role": "user", "content": user}]
    schemas = responses_tools(tool_schemas(tools.menus))
    started = time.monotonic()
    tools.calls = []
    final: dict[str, Any] | None = None
    rounds = 0
    tokens = {"input": 0, "output": 0, "reasoning": 0}
    while rounds < MAX_ROUNDS and final is None:
        rounds += 1
        resp = await llm._respond(  # noqa: SLF001 — prototip: bucla de producție e felia 2
            effort=effort,
            model=llm.model_agent,
            instructions=instructions,
            input=items,
            tools=schemas,
            tool_choice="required",
        )
        u = getattr(resp, "usage", None)
        if u is not None:
            tokens["input"] += getattr(u, "input_tokens", 0) or 0
            tokens["output"] += getattr(u, "output_tokens", 0) or 0
            details = getattr(u, "output_tokens_details", None)
            tokens["reasoning"] += getattr(details, "reasoning_tokens", 0) or 0
        calls = []
        for item in getattr(resp, "output", None) or []:
            items.append(item.model_dump(exclude_none=True))
            if getattr(item, "type", None) == "function_call":
                calls.append(item)
        for call in calls:
            try:
                args = json.loads(call.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if call.name == "answer":
                final = args
            output = await tools.run(call.name, args)
            items.append(
                {"type": "function_call_output", "call_id": call.call_id, "output": output}
            )
    seconds = round(time.monotonic() - started, 1)
    if final is None:
        return {"ok": False, "seconds": seconds, "rounds": rounds, "tools": tools.calls}
    text = str(final.get("text") or "").strip()
    show = [h for h in (final.get("show") or []) if h in conv.handles][:MAX_SHOWN]
    for h in show:
        if h not in conv.shown:
            conv.shown.append(h)
    if show:
        conv.shown_sets.append(list(show))
    client_said = " ".join(t for role, t in conv.history if role == "client") + " " + message
    conv.history += [("client", message), ("asistent", text)]
    products = [conv.facts.get(conv.handles[h], {}) for h in show]
    return {
        "ok": True,
        "seconds": seconds,
        "rounds": rounds,
        "tokens": tokens,
        "tools": tools.calls,
        "text": text,
        "products": [
            {"handle": h, "name": _name(p), "price": p.get("price")}
            for h, p in zip(show, products, strict=True)
        ],
        "gate": truth_gate(
            text,
            list(conv.facts.values()),
            [
                [
                    {"id": conv.handles[h], "price": conv.facts[conv.handles[h]].get("price")}
                    for h in x
                ]
                for x in conv.shown_sets
            ],
            _cart_total(conv),
            client_said,
        ),
        "cart": dict(conv.cart),
    }


# --- raportul ------------------------------------------------------------------------------------


def kernel_turns(path: Path | None) -> dict[str, list[dict[str, Any]]]:
    if path is None:
        return {}
    doc = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for cid, conv in (doc.get("conversations") or {}).items():
        out[cid] = [
            {
                "text": (t.get("response") or {}).get("content"),
                "products": [
                    p.get("name") for p in (t.get("response") or {}).get("products") or []
                ],
                "seconds": t.get("seconds"),
            }
            for t in conv.get("turns") or []
        ]
    return out


def markdown(report: dict[str, Any]) -> str:
    lines = [f"# Agentul A vs kernel — {report['set']} ({report['stamp']})", ""]
    for cid, conv in report["conversations"].items():
        lines += [f"## {cid}", f"_{conv['covers']}_", ""]
        for i, t in enumerate(conv["turns"], 1):
            a = t["agent"]
            lines.append(f"**T{i} client:** {t['message']}  ")
            lines.append(f"**așteptat:** {t['expect']}  ")
            k = t.get("kernel")
            if k:
                lines.append(f"**kernel** ({k.get('seconds')} s): {k.get('text')}  ")
                if k.get("products"):
                    lines.append(f"carduri: {', '.join(map(str, k['products']))}  ")
            if a.get("ok"):
                tools = ", ".join(c["tool"] for c in a["tools"])
                lines.append(
                    f"**agent** ({a['seconds']} s, {a['rounds']} runde: {tools}): {a['text']}  "
                )
                if a["products"]:
                    names = ", ".join(f"{p['name']} ({p['price']} lei)" for p in a["products"])
                    lines.append(f"carduri: {names}  ")
                if a["gate"]:
                    lines.append(f"⚠ poarta de adevăr: {', '.join(a['gate'])}  ")
            else:
                lines.append(f"**agent**: fără răspuns după {a.get('rounds')} runde  ")
            lines.append("")
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> int:
    # Ordinea producției: pachetul conversației înaintea catalogului. Importat primul,
    # `src.db.queries.catalog` intră într-un import circular (rularea din 2026-10-09: primul tur a
    # picat cu ImportError, restul au mers).
    import src.conversation  # noqa: F401, PLC0415
    from src.agent.llm import get_llm  # noqa: PLC0415
    from src.db.connection import admin_conn, close_pool, get_pool, tenant_conn  # noqa: PLC0415
    from src.db.queries.businesses import load_business  # noqa: PLC0415

    doc = load_set(args.set)
    convs = doc["conversations"]
    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        convs = [c for c in convs if c["id"] in wanted]
    n_turns = sum(len(c["turns"]) for c in convs)
    pool = await get_pool()

    class Deps:
        """Câte un checkout scurt pe operație, tenant-scoped (NX-231), ca la producție."""

        def __init__(self, business_id: str) -> None:
            self.business_id = business_id

        def db(self, _op: str) -> Any:
            return tenant_conn(self.business_id)

    try:
        async with admin_conn(pool) as admin:
            bid = await admin.fetchval(
                "select id::text from businesses where slug = $1 or id::text = $1", args.business
            )
        if bid is None:
            raise SystemExit(f"tenant necunoscut: {args.business}")
        deps = Deps(bid)
        async with tenant_conn(bid) as conn:
            business = await load_business(conn, bid)
        menus = await load_menus(deps, bid)
        print(f"meniuri: {menus.summary()}")
        print(f"{doc.get('version')}: {len(convs)} conversații · {n_turns} ture · x{args.repeat}")
        if not args.yes:
            print("dry-run: niciun apel de model. Rularea reală: adaugă --yes (o pornește Adi).")
            return 0
        llm = get_llm()
        if llm is None:
            raise SystemExit("clientul de model nu e configurat (OPENAI_API_KEY)")
        kernel = kernel_turns(args.kernel_report)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        base = OUT_DIR / f"agent-{doc.get('version')}-{stamp}"
        report: dict[str, Any] = {
            "set": doc.get("version"),
            "stamp": stamp,
            "effort": args.effort,
            "model": llm.model_agent,
            "conversations": {},
        }
        for rep in range(args.repeat):
            for conv_def in convs:
                key = conv_def["id"] if args.repeat == 1 else f"{conv_def['id']}#{rep + 1}"
                conv = Conversation()
                tools = Tools(deps, business, menus, conv)
                turns = []
                for i, t in enumerate(conv_def["turns"]):
                    try:
                        agent = await run_turn(
                            llm,
                            tools,
                            conv,
                            t["message"],
                            store=str(business.name or ""),
                            effort=args.effort,
                        )
                    except Exception as e:  # noqa: BLE001 — un tur picat se raportează
                        agent = {"ok": False, "error": type(e).__name__, "detail": str(e)[:300]}
                    ker = kernel.get(conv_def["id"]) or [None] * (i + 1)
                    turns.append(
                        {
                            "message": t["message"],
                            "expect": t.get("expect"),
                            "kernel": ker[i] if i < len(ker) else None,
                            "agent": agent,
                        }
                    )
                    status = "ok" if agent.get("ok") else agent.get("error", "fără răspuns")
                    print(f"{key} T{i + 1} [{agent.get('seconds')} s] {status}", flush=True)
                report["conversations"][key] = {"covers": conv_def.get("covers"), "turns": turns}
                base.with_suffix(".json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
                )
                base.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
        print(f"raport: {base.with_suffix('.md').relative_to(ROOT)}")
        return 0
    finally:
        await close_pool()


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--set", required=True, type=Path)
    p.add_argument("--business", default="sole-ro")
    p.add_argument("--kernel-report", type=Path, default=None)
    p.add_argument("--only", default="")
    p.add_argument("--effort", default="low", choices=["none", "low", "medium"])
    p.add_argument("--repeat", type=int, default=1)
    p.add_argument("--yes", action="store_true", help="rulează modelul (consumă credite)")
    args = p.parse_args(argv)
    if args.set.name.startswith("heldout-"):
        p.error("setul nevăzut se rulează doar la verdictul final")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
