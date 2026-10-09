"""NX-393 / NX-395 — agentul unic (varianta A): modelul înțelege, caută, verifică și răspunde.

    PYTHONPATH=. python scripts/sim/agent_a_prototype.py --set tests/golden/prod_sets/<set>.json
    PYTHONPATH=. python scripts/sim/agent_a_prototype.py --set … --kernel-report <raport> --yes

Fără `--yes` e dry-run: încarcă meniurile din catalog (rafturi, tipuri, mărci, nevoi, familiile de
rutină), construiește uneltele și spune câte conversații și ture ar rula, fără niciun apel de model.
Cu `--yes`, fiecare conversație din set rulează prin agent, tur cu tur, pe catalogul REAL, doar cu
citiri. Coșul e simulat: nu se scrie nimic în baza de date.

Ce face agentul (un singur model, `gpt-6-luna`, cu raționament pe `/v1/responses`):
- înțelege mesajul, oricum ar fi scris, și decide singur ce face;
- caută cu `search_catalog` (text + filtre din MENIURI ÎNCHISE generate din catalog), citește
  rezultatele, iar dacă nu se potrivesc reformulează sau schimbă filtrele;
- citește fișa întreagă cu `product_details`, regulile magazinului cu `store_rules`, pașii unei
  rutine cu `routine_plan` (pașii sunt ai magazinului, alegerea și textul ale agentului);
- încheie mereu cu `answer`: textul, cardurile cu motivul lor, sugestiile, comparația opțională
  (tabelul îl face codul din fapte) și notele despre ce a spus clientul.

Codul face doar: execuția uneltelor (SQL tenant-scoped, read-only), meniurile închise, handle-urile
(`P1`…, singura formă prin care modelul numește un produs), regula coșului (doar produse arătate) și
porțile de ADEVĂR pe tot ce scrie agentul (prețuri, linkuri, afirmații medicale). Un răspuns respins
primește motivul și O reîncercare. Nicio frază fixă, nicio decizie peste model.

Ieșirea: `reports/nx395/agent-<set>-<stamp>.json` și `.md`, cu răspunsul kernelului (din raportul
`prod_set_run.py` dat la `--kernel-report`) lângă cel al agentului, tur cu tur, plus
`grading-<set>-<stamp>.md` cu DOAR agentul (notarea se face înaintea citirii kernelului). Rularea
o pornește Adi (credite). Conversațiile rulează pe rând, ca citirile să nu țină sesiuni ale
poolerului."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "reports" / "nx395"
#: Câte runde de model are un tur (căutări, reformulări, fișe, răspuns). O reîncercare după o
#: poartă de adevăr picată primește o rundă în plus, în afara plafonului.
MAX_ROUNDS = 6
GATE_RETRIES = 1
#: Câte produse vede agentul dintr-o căutare și câte poate arăta clientului.
SEARCH_ROWS = 8
MAX_SHOWN = 6
#: Câte valori dintr-o fațetă intră în meniul de nevoi (cele mai frecvente).
FACET_VALUES = 25
#: Câte produse cunoscute intră în vedere (cele mai recente).
KNOWN_IN_VIEW = 40
NOTES_MAX = 500
SUGGESTION_MAX = 60
SHEET_MAX = 3000
#: Forma unui handle. Un pattern, nu un enum: enumul s-ar schimba la fiecare produs nou, iar
#: schemele uneltelor stau în prefixul cache-uit al cererii (NX-395, R1).
HANDLE_PATTERN = r"^P[1-9][0-9]*$"
_HANDLE_RE = re.compile(HANDLE_PATTERN)
#: Fațetele care nu sunt nevoi ale clientului (raftul și tipul au meniul lor).
_NOT_NEEDS = frozenset({"category", "product_type", "compliance"})
_ROLES = {"inbound": "client", "outbound": "asistent"}

INSTRUCTIONS = """\
You are the shopping assistant of the online store "{store}". You talk with one customer in a chat.
Write to the customer in locale "{locale}", in the customer's language, short and natural.

How you work:
- Understand what the customer wants in this message, using the conversation so far and your
  NOTES. They may write without diacritics, with typos, briefly, or ask several things at once:
  answer all of them.
- When the customer asks for products or alternatives, search with search_catalog and show what you
  found. Do not ask whether you should search: search. Filters come only from the closed menus in
  the tool schema. Read what comes back: the type of each product, what it is for, price, stock.
- If the results do not fit the request (wrong kind of product, wrong purpose, over budget, not in
  stock when that matters), search again: other words, another filter, or drop a filter that is too
  narrow. If nothing fits, say so honestly and offer the closest option. Search first, then read
  sheets only for the products you are going to show or discuss.
- For a question about a product (how to use it, ingredients, whether it suits something), read its
  sheet with product_details and answer from it. If the sheet does not say, say you cannot confirm.
- For a question about the store itself (delivery, returns, payment, vouchers), use store_rules and
  answer from those rules only.
- For a general question (whether a step is needed, the difference between two kinds of products),
  answer it directly with general know-how; you may show products when that helps.
- For a routine, call routine_plan with the area the customer means. Its steps belong to the store;
  you choose the products and write the routine. Areas with routines: {families}.
- When a request, a routine included, can be about more than one area of the store (for example
  face, body, hair or make-up) and neither the message nor the conversation says which, ask ONE
  short question that names the options, and do not show products in that turn. Once the customer
  answered, do not ask it again. "Both" or "all" as an answer means all the options of YOUR
  question.
- Name products ONLY by their handle (P1, P2, ...), in tool calls and in answer. Never write a
  product's name or a slug where a handle is expected. Handles are listed in PRODUCTS YOU KNOW and
  in tool results.
- Several products with the same name are different shades, sizes or versions: show one card for
  the one that fits, or name each by what tells them apart.
- add_to_cart only when the customer asks to buy or add an item, and only an item you showed.
- Every price, ingredient, size or other fact you write comes from tool results. Never invent one.
  No medical claims (treats, cures, safe in pregnancy). Never promise anything the store does not
  state in its rules.
- Finish EVERY turn with exactly one call to answer:
  - text: what the customer reads;
  - cards: the products to show (only ones that fit, at most {max_shown}), each with a short reason
    in the customer's terms;
  - suggestions: up to {chips} short next messages the customer could send now, written as the
    customer would say them, following from this conversation (no prices);
  - comparison: only when you compare 2-4 products; the table is built from the product facts, you
    write the intro above it, an optional subtitle and the closing advice below it;
  - notes: what the CUSTOMER said they want or avoid (budget, skin or hair type, for whom, things
    to avoid), kept from earlier notes and updated; never your own recommendations; at most
    {notes_max} characters.
- If answer comes back rejected, fix exactly what it says and call answer again.

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
    #: Familiile de rutină ale pachetului, momentele (`time_markers`) și pașii (toate familiile).
    families: tuple[str, ...] = ()
    moments: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()

    def summary(self) -> str:
        return (
            f"{len(self.categories)} rafturi, {len(self.product_types)} tipuri, "
            f"{len(self.brands)} mărci, {len(self.needs)} nevoi, "
            f"{len(self.families)} familii de rutină"
        )


def routine_menus(pack: Any) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Familiile, momentele și pașii din `domain_pack.routine_steps`. PUR."""
    spec = getattr(pack, "routine_steps", None)
    families = dict(getattr(spec, "families", {}) or {})
    moments = tuple(sorted(getattr(spec, "time_markers", {}) or {}))
    steps = tuple(dict.fromkeys(s for fam in families.values() for s in fam))
    return tuple(sorted(families)), moments, steps


async def load_menus(deps: Any, business_id: str, pack: Any = None) -> Menus:
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
    families, moments, steps = routine_menus(pack)
    return Menus(
        categories,
        types,
        tuple(r["name"] for r in rows),
        tuple(needs),
        families=families,
        moments=moments,
        steps=steps,
    )


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    """Un obiect STRICT: toate câmpurile cerute, nimic în plus (opționalele sunt `null`-abile)."""
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


HANDLE = {"type": "string", "pattern": HANDLE_PATTERN}


def tool_schemas(menus: Menus, *, chips: int = 5) -> list[dict[str, Any]]:
    """Schemele uneltelor, STRICTE (NX-395, R1). Depind doar de meniuri, deci sunt byte-identice
    pe toată conversația (prefixul cache-uit al cererii)."""
    search = {
        "name": "search_catalog",
        "description": (
            "Search the store's catalog. Returns up to 8 products with the facts that decide "
            "whether they fit (type, needs, price, stock). `match` says how the text matched: "
            "strict, relaxed, typo or filters_only (no text match, only the filters)."
        ),
        "parameters": _obj(
            {
                "query": {"type": "string", "description": "words to search for"},
                "category": _nullable({"type": "string", "enum": list(menus.categories)}),
                "product_types": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(menus.product_types)},
                },
                "brand": _nullable({"type": "string", "enum": list(menus.brands)}),
                "needs": {
                    "type": "array",
                    "description": "hard filters, facet:value",
                    "items": {"type": "string", "enum": list(menus.needs)},
                },
                "price_max": _nullable({"type": "number"}),
                "price_min": _nullable({"type": "number"}),
                "in_stock_only": {"type": "boolean"},
                "sort": {
                    "type": "string",
                    "enum": ["relevance", "price_asc", "price_desc", "rating_desc"],
                },
                "exclude": {
                    "type": "array",
                    "description": "handles of products not to return (already shown)",
                    "items": HANDLE,
                },
            }
        ),
    }
    details = {
        "name": "product_details",
        "description": "The full sheet of up to 3 products (usage, ingredients, attributes).",
        "parameters": _obj({"handles": {"type": "array", "items": HANDLE}}),
    }
    rules = {
        "name": "store_rules",
        "description": "All active store rules (delivery, returns, payment, vouchers).",
        "parameters": _obj({}),
    }
    cart = {
        "name": "add_to_cart",
        "description": "Add a product you showed to the customer's cart.",
        "parameters": _obj({"handle": HANDLE, "quantity": {"type": "integer"}}),
    }
    answer = {
        "name": "answer",
        "description": (
            "Finish the turn: the text the customer reads, the cards, the suggestions, an "
            "optional comparison and your notes about what the customer said."
        ),
        "parameters": _obj(
            {
                "text": {"type": "string"},
                "cards": {
                    "type": "array",
                    "items": _obj({"handle": HANDLE, "reason": {"type": "string"}}),
                },
                "suggestions": {"type": "array", "items": {"type": "string"}},
                "comparison": _nullable(
                    _obj(
                        {
                            "handles": {"type": "array", "items": HANDLE},
                            "intro": {"type": "string"},
                            "subtitle": _nullable({"type": "string"}),
                            "closing": {"type": "string"},
                        }
                    )
                ),
                "notes": {"type": "string"},
            }
        ),
    }
    tools = [search, details, rules, cart]
    if menus.families:
        tools.append(
            {
                "name": "routine_plan",
                "description": (
                    "The steps of a routine for one area, in the store's order, with the product "
                    "the store would put on each step and the steps it has no product for."
                ),
                "parameters": _obj(
                    {
                        "family": {"type": "string", "enum": list(menus.families)},
                        "moment": _nullable({"type": "string", "enum": list(menus.moments)})
                        if menus.moments
                        else {"type": "null"},
                        "needs": {
                            "type": "array",
                            "items": {"type": "string", "enum": list(menus.needs)},
                        },
                        "budget_max": _nullable({"type": "number"}),
                        "steps": {
                            "type": "array",
                            "description": "only the steps the customer named; empty = all",
                            "items": {"type": "string", "enum": list(menus.steps)},
                        },
                        "anchor": _nullable(HANDLE),
                    }
                ),
            }
        )
    tools.append(answer)
    return [{"type": "function", **t, "strict": True} for t in tools]


# --- numele produselor ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[\w'+.%-]+", re.UNICODE)


def distinct_names(full_names: dict[str, str]) -> dict[str, str]:
    """NX-395, R4: numele scurt al fiecărui produs, deosebit de al celorlalte. PUR.

    Nuanțele și gramajele sunt produse separate cu același `display_name`, deci agentul vedea trei
    carduri identice. Pe un grup cu același nume scurt se adaugă cuvintele care deosebesc numele
    ÎNTREG de restul grupului (întâi cele cu cifre: nuanța, gramajul), cel mult trei. Numele
    întregi identice primesc un număr de ordine: nimic din date nu le deosebește."""
    from src.catalog.render_text import display_name  # noqa: PLC0415

    short = {k: display_name(v) or v for k, v in full_names.items()}
    groups: dict[str, list[str]] = {}
    for k, s in short.items():
        groups.setdefault(s.casefold(), []).append(k)
    out = dict(short)
    for members in groups.values():
        if len(members) < 2:
            continue
        words = {k: _WORD_RE.findall(full_names[k] or "") for k in members}
        common = set.intersection(*({w.casefold() for w in ws} for ws in words.values()))
        for i, k in enumerate(members, 1):
            tag = _distinct_run(words[k], common)
            out[k] = f"{short[k]} ({tag or i})"
    return out


def _distinct_run(words: list[str], common: set[str]) -> str:
    """Cuvintele care deosebesc un nume de restul grupului, ca SECVENȚĂ din nume: pornește de la
    primul cuvânt propriu cu cifre (nuanța, gramajul), altfel de la primul cuvânt propriu, și ia
    cuvintele proprii imediat următoare, cel mult trei («21W Creamer», nu «21W aspectului»)."""
    own = [j for j, w in enumerate(words) if w.casefold() not in common]
    if not own:
        return ""
    start = next((j for j in own if any(c.isdigit() for c in words[j])), own[0])
    run = [words[start]]
    for j in range(start + 1, min(start + 3, len(words))):
        if words[j].casefold() in common:
            break
        run.append(words[j])
    return " ".join(run)


# --- starea conversației (a scriptului, nu a modelului) ------------------------------------------


@dataclass
class Conversation:
    """Ce știe codul despre conversație: handle-urile produselor văzute de agent (`P1`…), ce a
    arătat clientului, coșul simulat, notele agentului și istoricul. Agentul primește handle-uri, nu
    UUID-uri (NX-324: id-urile copiate greșit scoteau carduri). Scrisă doar de `run_turn`; uneltele
    adaugă doar fapte și handle-uri noi."""

    handles: dict[str, str] = field(default_factory=dict)  # handle -> product_id
    facts: dict[str, dict[str, Any]] = field(default_factory=dict)  # product_id -> rând
    recent: list[str] = field(default_factory=list)  # handle-uri, cel mai recent ultimul
    shown: list[str] = field(default_factory=list)  # handle-uri, în ordinea arătării
    shown_sets: list[list[str]] = field(default_factory=list)  # cardurile fiecărui tur
    cart: dict[str, int] = field(default_factory=dict)  # handle -> cantitate
    notes: str = ""
    #: `{"role", "text", "shown": [handle]}`
    history: list[dict[str, Any]] = field(default_factory=list)

    def handle_of(self, product_id: str) -> str:
        for h, pid in self.handles.items():
            if pid == product_id:
                break
        else:
            h = f"P{len(self.handles) + 1}"
            self.handles[h] = product_id
        if h in self.recent:
            self.recent.remove(h)
        self.recent.append(h)
        return h

    def names(self) -> dict[str, str]:
        """handle → numele distinct, pe TOATE produsele cunoscute."""
        full = {
            h: str(self.facts.get(pid, {}).get("name") or "") for h, pid in self.handles.items()
        }
        return distinct_names(full)

    def check(self, handle: Any) -> str:
        """Un handle valid și cunoscut, altfel refuz cu lista celor valide."""
        h = str(handle or "")
        if not _HANDLE_RE.match(h):
            raise _Refused(f"{h!r} is not a handle; use a handle like P1 from PRODUCTS YOU KNOW")
        if h not in self.handles:
            valid = ", ".join(self.recent[-20:]) or "none yet: search first"
            raise _Refused(f"unknown handle {h}; valid handles: {valid}")
        return h

    def view(self) -> str:
        names = self.names()
        lines = []
        if self.notes:
            lines += ["NOTES (what the customer said, written by you earlier)", self.notes, ""]
        if self.recent:
            lines.append("PRODUCTS YOU KNOW (latest last)")
            for h in self.recent[-KNOWN_IN_VIEW:]:
                p = self.facts.get(self.handles[h], {})
                tags = [
                    t for t, on in (("shown", h in self.shown), ("in cart", h in self.cart)) if on
                ]
                tail = f" [{', '.join(tags)}]" if tags else ""
                lines.append(
                    f"- {h}: {names.get(h)}, {p.get('price')} lei, {p.get('availability')}{tail}"
                )
            lines.append("")
        if self.cart:
            total = cart_total(self)
            lines.append(
                "CART: "
                + ", ".join(f"{h} x{q}" for h, q in self.cart.items())
                + (f" (total {total} lei)" if total is not None else "")
            )
            lines.append("")
        if self.history:
            lines.append("HISTORY")
            for m in self.history[-20:]:
                line = f"{m['role']}: {m['text']}"
                if m.get("shown"):
                    line += (
                        " [showed: " + "; ".join(f"{h} {names.get(h)}" for h in m["shown"]) + "]"
                    )
                lines.append(line)
        return "\n".join(lines).strip()


def _row_view(h: str, p: dict[str, Any], name: str) -> dict[str, Any]:
    attrs = p.get("attributes") or {}
    keep = {
        k: v
        for k, v in attrs.items()
        if k not in ("compliance", "key_ingredients") and v not in (None, "", [], {})
    }
    out = {
        "handle": h,
        "name": name,
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
        refused = None
        try:
            out = await getattr(self, f"_{name}")(args)
        except _Refused as e:
            refused = str(e)
            out = {"ok": False, "error": refused}
        self.calls.append(
            {
                "tool": name,
                "args": args,
                "ms": round((time.monotonic() - started) * 1000),
                **({"refused": refused} if refused else {}),
            }
        )
        return json.dumps(out, ensure_ascii=False, default=str)

    def _remember(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Faptele și handle-urile întâi, numele după: un nume e distinct față de TOT ce știm."""
        handles = []
        for r in rows:
            self.conv.facts[str(r["id"])] = {**self.conv.facts.get(str(r["id"]), {}), **r}
            handles.append(self.conv.handle_of(str(r["id"])))
        names = self.conv.names()
        return [_row_view(h, r, names[h]) for h, r in zip(handles, rows, strict=True)]

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
        excluded = {self.conv.handles[self.conv.check(h)] for h in a.get("exclude") or []}
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
        price_min = a.get("price_min")
        rows = [
            r
            for r in rows
            if r.get("id") not in excluded
            and (price_min is None or (r.get("price") or 0) >= float(price_min))
        ]
        return {"found": len(rows), "products": self._remember(rows[:SEARCH_ROWS])}

    async def _product_details(self, a: dict[str, Any]) -> dict[str, Any]:
        from src.agent.detail_answer import product_facts  # noqa: PLC0415
        from src.db.queries.catalog import get_products_by_ids  # noqa: PLC0415

        handles = [self.conv.check(h) for h in (a.get("handles") or [])[:3]]
        if not handles:
            raise _Refused("no handles: give 1-3 handles from PRODUCTS YOU KNOW")
        ids = [self.conv.handles[h] for h in handles]
        async with self.deps.db("agent_details") as conn:
            rows = await get_products_by_ids(conn, self.business.id, ids, limit=len(ids))
        by_id = {str(r["id"]): r for r in rows}
        self._remember([by_id[i] for i in ids if i in by_id])
        names = self.conv.names()
        sheets = {}
        for h, pid in zip(handles, ids, strict=True):
            row = by_id.get(pid)
            if row is None:
                continue
            sheet = product_facts(row, self.business.domain_pack, "ro")[:SHEET_MAX]
            sheets[h] = f"name on card: {names[h]}\n{sheet}"
        return {"sheets": sheets}

    async def _store_rules(self, a: dict[str, Any]) -> dict[str, Any]:
        from src.db.queries.faqs import list_active  # noqa: PLC0415

        locale = self.business.default_locale or "ro"
        async with self.deps.db("agent_rules") as conn:
            rows = await list_active(conn, self.business.id, locale, limit=60)
        return {"rules": [f"{r['question']} -> {r['answer']}" for r in rows]}

    async def _add_to_cart(self, a: dict[str, Any]) -> dict[str, Any]:
        h = self.conv.check(a.get("handle"))
        if h not in self.conv.shown:
            raise _Refused("only a product you showed to the customer can go to the cart")
        self.conv.cart[h] = self.conv.cart.get(h, 0) + max(1, int(a.get("quantity") or 1))
        return {"ok": True, "cart": dict(self.conv.cart), "total": cart_total(self.conv)}

    async def _routine_plan(self, a: dict[str, Any]) -> dict[str, Any]:
        """`routine_tools._routine` cu `planned=True`: argumentele sunt ale agentului, deci nu se
        re-judecă pe textul recent (NX-333: un buget spus acum zece mesaje ar fi aruncat). Pașii și
        produsul pus pe fiecare sunt ale magazinului; id-urile din vedere devin handle-uri."""
        from src.tools.routine_tools import _routine  # noqa: PLC0415

        m = self.menus
        _check(a.get("family"), m.families, "family")
        if a.get("moment") is not None:
            _check(a.get("moment"), m.moments, "moment")
        values = []
        for need in a.get("needs") or []:
            _check(need, m.needs, "needs")
            values.append(need.partition(":")[2])
        anchor = self.conv.check(a["anchor"]) if a.get("anchor") else None
        args = {
            "family": a["family"],
            "concerns": values,
            "budget_max": a.get("budget_max"),
            "anchor_id": self.conv.handles[anchor] if anchor else None,
            "moment": a.get("moment"),
            "steps": [s for s in a.get("steps") or [] if s in m.steps] or None,
        }
        ctx = routine_context(self.business)
        result = await _routine(ctx, self.deps, args, planned=True)
        products = self._remember(list(result.products or []))
        view = result.llm_view or ""
        for p in products:
            view = view.replace(f"[{self.conv.handles[p['handle']]}]", f"[{p['handle']}]")
        out: dict[str, Any] = {"ok": result.ok, "routine": view, "products": products}
        if result.error:
            out["error"] = result.error
        return out

    async def _answer(self, a: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    async def comparison(self, handles: list[str], texts: dict[str, Any]) -> Any:
        """Tabelul comparației din FAPTE (`compose.build_comparison`, determinist, ca azi);
        textele din jurul lui sunt ale agentului, deja trecute prin poarta de adevăr."""
        from src.db.queries.catalog import get_products_by_ids  # noqa: PLC0415
        from src.worker.compose import build_comparison  # noqa: PLC0415

        ids = [self.conv.handles[h] for h in handles]
        async with self.deps.db("agent_compare") as conn:
            rows = await get_products_by_ids(conn, self.business.id, ids, limit=len(ids))
        pack = self.business.domain_pack
        table = build_comparison(rows, "ro", pack.comparison_facets if pack else ())
        if table is None:
            return None
        # Antetul tabelului poartă numele distinct (R4): două nuanțe nu se compară sub același nume.
        names = self.conv.names()
        by_id = {pid: h for h, pid in self.conv.handles.items()}
        for col in table.columns:
            col.name = names.get(by_id.get(str(col.product_id), ""), col.name)
        table.intro = texts.get("intro") or None
        table.subtitle = texts.get("subtitle") or None
        table.closing = [texts["closing"]] if texts.get("closing") else []
        return table


class _Refused(Exception):
    pass


def routine_context(business: Any) -> Any:
    """Un `TurnContext` minim pentru `routine_plan`: tenantul, limba, fără context de siguranță
    (siguranța e a Fazei 2, declarat în NX-395). Citit de unealtă, aruncat după apel."""
    from src.models import Contact, InboundMessage, TurnContext  # noqa: PLC0415

    return TurnContext(
        turn_id="agent-a",
        business=business,
        contact=Contact(id="agent-a", business_id=business.id),
        message=InboundMessage(provider_msg_id="agent-a", body=""),
        conversation_id="agent-a",
        language=business.default_locale or "ro",
    )


def cart_total(conv: Conversation) -> float | None:
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


_HINTS = {
    "ungrounded_price": "a price or sum is not in the tool results; use the exact prices",
    "unknown_link": "a link is not a product link from the tool results; remove it",
    "medical_claim": "remove the medical claim (treats, cures, safe in pregnancy and the like)",
    "unknown_card": "a card handle is not one you know; use handles from PRODUCTS YOU KNOW",
    "comparison_handles": "a comparison needs 2-4 known handles",
}


@dataclass
class Checked:
    """Un `answer` judecat: ce s-ar servi și motivele de respingere (gol = acceptat)."""

    text: str
    cards: list[dict[str, str]]
    suggestions: list[str]
    dropped_suggestions: list[str]
    comparison: dict[str, Any] | None
    notes: str
    rejected: list[str]


def check_answer(conv: Conversation, a: dict[str, Any], client_text: str) -> Checked:
    """Porțile de adevăr pe TOT ce scrie agentul în `answer` (NX-395, R5). PUR pe starea dată.

    Textul, motivele cardurilor și textele comparației se judecă împreună: o greșeală acolo
    respinge răspunsul și agentul primește motivul. O sugestie greșită doar iese (nu merită o
    rundă). Totalul setului propus se adaugă la sumele permise, ca la producție (NX-391)."""
    text = str(a.get("text") or "").strip()
    rejected: list[str] = []
    cards: list[dict[str, str]] = []
    for c in a.get("cards") or []:
        h = str((c or {}).get("handle") or "")
        if h not in conv.handles:
            rejected.append("unknown_card")
            continue
        if h not in {x["handle"] for x in cards}:
            cards.append({"handle": h, "reason": str((c or {}).get("reason") or "").strip()})
    cards = cards[:MAX_SHOWN]
    comp = a.get("comparison")
    if comp:
        handles = [h for h in comp.get("handles") or [] if h in conv.handles]
        if not 2 <= len(dict.fromkeys(handles)) <= 4:
            rejected.append("comparison_handles")
            comp = None
        else:
            comp = {**comp, "handles": list(dict.fromkeys(handles))}
    facts = list(conv.facts.values())
    proposed = [c["handle"] for c in cards]
    compared = comp["handles"] if comp else []
    sets = [
        [{"id": conv.handles[h], "price": conv.facts[conv.handles[h]].get("price")} for h in x]
        for x in (*conv.shown_sets, proposed, compared)
        if x
    ]
    body = "\n".join(
        [
            text,
            *(c["reason"] for c in cards if c["reason"]),
            *(
                str(comp.get(k) or "")
                for k in ("intro", "subtitle", "closing")
                if comp and comp.get(k)
            ),
        ]
    )
    rejected += truth_gate(body, facts, sets, cart_total(conv), client_text)
    kept, dropped = [], []
    for s in a.get("suggestions") or []:
        s = str(s or "").strip()
        if not s:
            continue
        if len(s) > SUGGESTION_MAX or truth_gate(s, facts, sets, cart_total(conv), client_text):
            dropped.append(s)
        elif s not in kept:
            kept.append(s)
    from src.config import chip_slots  # noqa: PLC0415

    return Checked(
        text=text,
        cards=cards,
        suggestions=kept[: chip_slots()],
        dropped_suggestions=dropped,
        comparison=comp,
        notes=str(a.get("notes") or "").strip()[:NOTES_MAX],
        rejected=list(dict.fromkeys(rejected)),
    )


# --- un tur --------------------------------------------------------------------------------------


def instructions(store: str, menus: Menus) -> str:
    from src.agent.voice import VOICE_RULES  # noqa: PLC0415
    from src.config import chip_slots  # noqa: PLC0415

    return INSTRUCTIONS.format(
        store=store,
        locale="ro",
        max_shown=MAX_SHOWN,
        chips=chip_slots(),
        notes_max=NOTES_MAX,
        families=", ".join(menus.families) or "none",
        voice=VOICE_RULES.strip(),
    )


async def run_turn(
    llm: Any, tools: Tools, conv: Conversation, message: str, *, store: str, effort: str
) -> dict[str, Any]:
    from src.agent.llm import responses_tools  # noqa: PLC0415
    from src.agent.voice import naturalize  # noqa: PLC0415

    context = conv.view()
    user = (
        f"{context}\n\nCUSTOMER MESSAGE\n{message}" if context else f"CUSTOMER MESSAGE\n{message}"
    )
    items: list[Any] = [{"role": "user", "content": user}]
    schemas = responses_tools(tool_schemas(tools.menus))
    started = time.monotonic()
    tools.calls = []
    client_said = " ".join(m["text"] for m in conv.history if m["role"] == "client")
    client_said += " " + message
    final: Checked | None = None
    last: Checked | None = None
    first_rejected: list[str] = []
    retries = 0
    rounds = 0
    budget = MAX_ROUNDS
    tokens = {"input": 0, "cached": 0, "output": 0, "reasoning": 0}
    while rounds < budget and final is None:
        rounds += 1
        resp = await llm._respond(  # noqa: SLF001 — prototip: bucla de producție e Faza 2
            effort=effort,
            model=llm.model_agent,
            instructions=instructions(store, tools.menus),
            input=items,
            tools=schemas,
            tool_choice="required",
        )
        u = getattr(resp, "usage", None)
        if u is not None:
            tokens["input"] += getattr(u, "input_tokens", 0) or 0
            tokens["output"] += getattr(u, "output_tokens", 0) or 0
            in_details = getattr(u, "input_tokens_details", None)
            tokens["cached"] += getattr(in_details, "cached_tokens", 0) or 0
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
            if call.name == "answer" and final is None:
                last = check_answer(conv, args, client_said)
                tools.calls.append({"tool": "answer", "args": {}, "ms": 0})
                if not last.rejected:
                    final = last
                    output = json.dumps({"ok": True})
                else:
                    first_rejected = first_rejected or last.rejected
                    output = json.dumps(
                        {
                            "ok": False,
                            "rejected": last.rejected,
                            "fix": [_HINTS.get(r, r) for r in last.rejected],
                        },
                        ensure_ascii=False,
                    )
                    if retries < GATE_RETRIES:
                        retries += 1
                        budget += 1
                    else:
                        budget = rounds  # respins și după reîncercare: turul se încheie aici
            else:
                output = await tools.run(call.name, args)
            items.append(
                {"type": "function_call_output", "call_id": call.call_id, "output": output}
            )
    seconds = round(time.monotonic() - started, 1)
    refusals = [c for c in tools.calls if c.get("refused")]
    base = {
        "seconds": seconds,
        "rounds": rounds,
        "tokens": tokens,
        "tools": tools.calls,
        "retries": retries,
        "first_rejected": first_rejected,
        "unknown_handle": sum("handle" in (c.get("refused") or "") for c in refusals),
        "routine": any(c["tool"] == "routine_plan" for c in tools.calls),
    }
    conv.history.append({"role": "client", "text": message})
    if final is None:
        # Fără `answer`, sau respins și după reîncercare: în producție ar urma rezerva P6. Aici
        # nu se servește nimic, iar textul respins rămâne doar în raport.
        return {
            "ok": False,
            "served": False,
            "error": "gate_failed" if last is not None else "no_answer",
            "rejected_text": last.text if last else None,
            "gate": last.rejected if last else [],
            **base,
        }
    text = naturalize(final.text)
    shown = [c["handle"] for c in final.cards]
    for h in shown:
        if h not in conv.shown:
            conv.shown.append(h)
    if shown:
        conv.shown_sets.append(list(shown))
    if final.notes:
        conv.notes = final.notes
    conv.history.append({"role": "asistent", "text": text, "shown": shown})
    comparison = None
    if final.comparison:
        table = await tools.comparison(final.comparison["handles"], final.comparison)
        comparison = _comparison_view(table, conv) if table is not None else {"dropped": True}
    names = conv.names()
    return {
        "ok": True,
        "served": True,
        **base,
        "text": text,
        "products": [
            {
                "handle": c["handle"],
                "name": names.get(c["handle"]),
                "price": conv.facts[conv.handles[c["handle"]]].get("price"),
                "reason": naturalize(c["reason"]),
            }
            for c in final.cards
        ],
        "suggestions": [naturalize(s) for s in final.suggestions],
        "dropped_suggestions": final.dropped_suggestions,
        "comparison": comparison,
        "notes": conv.notes,
        "gate": [],
        "cart": dict(conv.cart),
    }


def _comparison_view(table: Any, conv: Conversation) -> dict[str, Any]:
    return {
        "columns": [c.name for c in table.columns],
        "rows": [
            {"label": getattr(r, "label", None), "values": list(getattr(r, "values", []) or [])}
            for r in table.rows
        ],
        "intro": table.intro,
        "subtitle": table.subtitle,
        "closing": table.closing,
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


def _agent_lines(a: dict[str, Any]) -> list[str]:
    tools = ", ".join(c["tool"] + ("✗" if c.get("refused") else "") for c in a.get("tools") or [])
    head = f"**agent** ({a.get('seconds')} s, {a.get('rounds')} runde: {tools})"
    if not a.get("served"):
        lines = [f"{head}: NESERVIT ({a.get('error')}, {', '.join(a.get('gate') or [])})  "]
        if a.get("rejected_text"):
            lines.append(f"text respins: {a['rejected_text']}  ")
        return lines
    lines = [f"{head}: {a['text']}  "]
    for p in a.get("products") or []:
        lines.append(f"- card {p['handle']} {p['name']} ({p['price']} lei): {p['reason']}  ")
    comp = a.get("comparison")
    if comp and not comp.get("dropped"):
        lines.append(f"comparație: {' | '.join(comp['columns'])}  ")
        for r in comp["rows"]:
            lines.append(f"  · {r['label']}: {' | '.join(map(str, r['values']))}  ")
        if comp.get("closing"):
            lines.append(f"  închidere: {' '.join(comp['closing'])}  ")
    if a.get("suggestions"):
        lines.append(f"sugestii: {' · '.join(a['suggestions'])}  ")
    if a.get("notes"):
        lines.append(f"note: {a['notes']}  ")
    if a.get("retries"):
        lines.append(f"↻ reîncercare după: {', '.join(a.get('first_rejected') or [])}  ")
    if a.get("unknown_handle"):
        lines.append(f"⚠ handle-uri refuzate: {a['unknown_handle']}  ")
    return lines


def markdown(report: dict[str, Any], *, with_kernel: bool = True) -> str:
    title = "Agentul A vs kernel" if with_kernel else "Agentul A — notare"
    lines = [f"# {title} — {report['set']} ({report['stamp']})", ""]
    for cid, conv in report["conversations"].items():
        lines += [f"## {cid}", f"_{conv['covers']}_", ""]
        for i, t in enumerate(conv["turns"], 1):
            lines.append(f"**T{i} client:** {t['message']}  ")
            lines.append(f"**așteptat:** {t['expect']}  ")
            k = t.get("kernel")
            if with_kernel and k:
                lines.append(f"**kernel** ({k.get('seconds')} s): {k.get('text')}  ")
                if k.get("products"):
                    lines.append(f"carduri: {', '.join(map(str, k['products']))}  ")
            lines += _agent_lines(t["agent"])
            if not with_kernel:
                lines.append("**notă:** corect / parțial / greșit — …  ")
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
        menus = await load_menus(deps, bid, business.domain_pack)
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
        grading = OUT_DIR / f"grading-{doc.get('version')}-{stamp}.md"
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
                    status = "ok" if agent.get("served") else agent.get("error", "fără răspuns")
                    print(f"{key} T{i + 1} [{agent.get('seconds')} s] {status}", flush=True)
                report["conversations"][key] = {"covers": conv_def.get("covers"), "turns": turns}
                base.with_suffix(".json").write_text(
                    json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
                )
                base.with_suffix(".md").write_text(markdown(report), encoding="utf-8")
                grading.write_text(markdown(report, with_kernel=False), encoding="utf-8")
        print(f"raport: {base.with_suffix('.md').relative_to(ROOT)}")
        print(f"notare (fără kernel): {grading.relative_to(ROOT)}")
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
