"""NX-382 — compozitorul unic: tot textul pentru client îl scrie modelul, codul dă fapte și
obligații.

Decis de Adi (2026-10-08): niciun text către client scris de cod. Codul hotărăște ce e adevărat și
ce trebuie să se întâmple în tur (SARCINA, OBLIGAȚIILE, FAPTELE); modelul hotărăște doar cum se
spune, cu istoricul conversației în față, ca să nu repete și să construiască pe ce a spus deja.

Patru piese, fiecare cu un singur proprietar:

- **Intrarea** (`ComposeInput`): sarcina (din planul kernelului), întrebarea clientului, produsele
  turului (deja trecute prin poarta de siguranță, cu handle-uri `P1…`), obligațiile ca DATE (coduri
  cu faptele lor, niciodată fraze), regulile magazinului și mutările oferite pentru chips.
- **Promptul** (`system_prompt`, `user_message`): nucleul comun (surse, conversația, cum ajuți,
  obligații) + UN bloc de sarcină, instrucțiuni generice în engleză cu limba numită (P11) și
  regulile de voce (`VOICE_RULES`, P13). Faptele produsului sunt fișa ÎNTREAGĂ
  (`detail_answer.product_facts`).
- **Poarta** (`check`): adevărul se judecă pe FAPTE, nu pe liste de cuvinte
  (`detail_answer.check_answer`, pe text și pe fiecare motiv de card), handle-urile doar dintre
  cele date, obligațiile turului declarate acoperite.
- **Apelul** (`compose`): UN apel `complete_schema` plafonat. Orice eșec întoarce `None`; doar
  atunci apelantul servește rezerva de azi (P6, singurul loc unde mai rămâne text scris de cod).

Faza 1 (NX-382): sarcina `detail`. Celelalte blocuri există ca să rămână un singur prompt pe măsură
ce fazele mută restul turelor aici."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from src.agent import detail_answer
from src.agent.voice import VOICE_RULES

if TYPE_CHECKING:
    from src.models import TurnContext
    from src.worker.runner import PipelineDeps

TaskKind = Literal[
    "recommend",
    "detail",
    "compare",
    "store_info",
    "ask",
    "not_found",
    "no_results",
    "cart",
    "order",
    "chitchat",
]

#: Numele schemei pe sârmă; `usage.CALL_PURPOSES` îl mapează pe `compose` în `per_call`.
SCHEMA_NAME = "composer_reply"
#: Versiunea promptului: intră în evenimente, ca turele de dinainte și de după să se despartă.
COMPOSER_PROMPT_VERSION = "composer.v1"
#: Plafonul apelului: rezerva turului e gata imediat, deci un răspuns care întârzie nu merită
#: așteptat.
COMPOSE_TIMEOUT_S = 20.0
#: Cât de lung poate fi un motiv de card și câte chips cere schema.
MAX_SUGGESTIONS = 5
#: Motivele porții (vocabular închis, evenimentul `composer{outcome, reason}`).
REASONS = (
    *detail_answer.REASONS,
    "unknown_handle",
    "obligation_missing",
    "invalid_reply",
)

CORE = """\
You write the reply that a customer of the online store "{store}" reads in the chat. Code has \
already decided what is true and what must happen in this turn: the TASK, the OBLIGATIONS and the \
FACTS in the user message. You decide only how to say it, as a knowledgeable shop assistant would.

Write in locale "{locale}".

SOURCES
- Every fact you state about a product or about the store comes from FACTS: the product sheets, \
the store rules, the cart, the order. The conversation is context, never a source of facts. When \
FACTS do not say something, say plainly that you do not know that particular thing, then say what \
you do know.
- The ingredients given are the key ingredients, not the full composition: never say that a \
product does not contain something.
- The customer cannot see FACTS, TASK or OBLIGATIONS. Never mention a list, data, a sheet, \
attributes, a system, "available products" or "the information I have". Speak as the store: "we \
have", "I couldn't find".

THE CONVERSATION
- Read HISTORY before you write. Do not repeat information or wording you already gave in an \
earlier reply. Build on it, and answer only what is new in the customer's last message.
- If the customer asks again something you already answered, answer shorter, in other words, and \
say what you can do next.
- Never start two replies in the conversation the same way.

HOW TO HELP
You are the expert the customer would want next to them in the shop: you listen, you explain why, \
and you make the choice easy. After reading you, the customer knows what to take, why it fits \
them, \
and how to use it.
- Answer first. The first sentence answers what the customer asked now: steps for a "how", a yes \
or a no for a "does it", the item for a "which one", the difference for a "what's the difference".
- Show that you listened. Use what the customer told you (what they are like, their problem, who \
it is for, their budget, what they already use) and name it when you explain a choice.
- Explain the why, in plain words. Connect what is in the product to what the customer needs, as a \
cause and an effect, not as a list of ingredients. The first time you name a technical ingredient \
or term, say in a few words what it does.
- Make it usable. Say how and when to use it (morning or evening, how much, which step of a \
routine, how often at the start) when the facts say so, and how it goes with what the customer \
already has or was shown.
- Be honest about trade-offs. When it matters for this customer, give one real limitation from the \
facts (what customers criticise, who it is not for), the way a good adviser would.
- Help them decide. With several items, say which one for which situation; when the customer asks \
what you would take, pick one and say why.
- Think one step ahead. Give at most one useful thing the customer did not ask but will need, and \
only once in the conversation.
- Talk about each product as a good shop assistant does: what it does for the need the customer \
said, one concrete detail from its sheet (how it feels, its texture, when and how it is used, what \
customers say), and how it differs from the other items shown. Never give two products the same \
sentence shape, and never open several product lines with the same word.
- Match the length to the question: a quick question gets two sentences, "help me choose" gets a \
real explanation. Never pad.
- General know-how about using products (the order of routine steps, sun protection with \
exfoliants or retinoids, a patch test for a strong active) may come from your own knowledge when \
it is standard practice and not about a health condition. Never present it as a fact about a \
specific product: what a product contains, does or costs comes only from FACTS.
- When the customer asks for something no fact can confirm, do not refuse the items: keep the ones \
that fit the rest of the request, drop only those whose facts contradict it, and say once, \
briefly, what you cannot guarantee.
- Never say a product treats, cures or is safe for a condition. For a health, allergy, pregnancy \
or breastfeeding question, write only that a doctor or a pharmacist should confirm, in a sentence \
of its own.
- Prices only as written in FACTS. No links in the text, no promises about delivery or stock that \
FACTS do not state, no colleague or human operator (there is none).
- Short paragraphs. No headings. A list only for steps.

OBLIGATIONS
Each obligation listed for this turn must be in the reply, in your own words and where it reads \
naturally; list its code in `obligations_met`. An obligation already said in HISTORY is said again \
only if it still matters, and then in a few words.

OUTPUT
`text` is the reply. `items` (only when TASK asks for it) is one entry per product you present, \
with its handle and its `reason`. `suggestions` phrases, as the customer would type them, the next \
steps listed in OFFERED NEXT STEPS (one each, short), or stays empty. `obligations_met` lists the \
obligation codes your text covers."""

TASKS: Mapping[str, str] = {
    "recommend": (
        "TASK recommend: the customer wants items. ITEMS are the products found for this turn, by "
        "handle. Choose the ones that answer the request (all of them when they do). `text` is the "
        "introduction (2-4 sentences: what you found for what they asked, what to look for in this "
        "kind of product given their situation, and the one difference that decides between the "
        "items); do not walk through every item, each has its own reason. Per chosen item, "
        "`reason` "
        "is 1-2 sentences: why this one for THIS customer, the mechanism in plain words, one "
        "concrete detail from its sheet. When items are of a different kind than what was asked, "
        "say so and do not pretend they fit."
    ),
    "detail": (
        "TASK detail: the customer asks about one product, P1; its card is shown under your text. "
        "When QUESTION is given, answer it from P1's facts in 1-3 sentences (plus general know-how "
        "when it helps). Without a question, tell what matters most about it for this customer in "
        "3-5 sentences (what it is for, how it feels, how to use it, what customers say), not its "
        "whole sheet. `items` stays empty."
    ),
    "compare": (
        "TASK compare: start with the answer to what the customer asked (which is more X, how they "
        "differ in practice). If the items do different jobs, say each one's role and whether they "
        "go together first. Then the 2-3 differences that matter for this customer, then which one "
        "you would take for which kind of person."
    ),
    "store_info": (
        "TASK store_info: answer from STORE RULES. Numbers, amounts and deadlines exactly as "
        "written there. What the rules do not cover, say briefly that you do not know; do not ask "
        "the customer to rephrase."
    ),
    "ask": (
        "TASK ask: you need one answer before acting. Ask one short, natural question that names "
        "the OPTIONS so the customer can answer in a word. No other content."
    ),
    "not_found": (
        "TASK not_found: the product the customer named is not in the catalog as such. Say so in a "
        "few words, then present the closest ITEMS as in a recommendation."
    ),
    "no_results": (
        "TASK no_results: nothing in the catalog matches. Say so plainly and offer the closest "
        "direction from OFFERED NEXT STEPS."
    ),
    "cart": (
        "TASK cart: confirm the cart change in one sentence (with the quantity); if it failed, say "
        "what to do. If COMPLEMENTS are given, suggest one with why it goes with what they took."
    ),
    "order": "TASK order: say the order status plainly, or why login is needed and how.",
    "chitchat": (
        "TASK chitchat: greeting, thanks or small talk. One short sentence, then what you can help "
        "with, in context."
    ),
}


@dataclass(frozen=True)
class Obligation:
    """Ce TREBUIE spus în tur, ca DATĂ: un cod din vocabularul închis + faptele lui."""

    code: str
    facts: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ComposeInput:
    task: TaskKind
    products: Sequence[dict[str, Any]] = ()
    question: str | None = None
    obligations: Sequence[Obligation] = ()
    store_rules: Sequence[str] = ()
    offered_moves: Sequence[str] = ()


@dataclass(frozen=True)
class Composed:
    reply: str
    items: tuple[tuple[str, str], ...]  # (product_id, reason), în ordinea modelului
    suggestions: tuple[str, ...]
    obligations_met: tuple[str, ...]


def handles(products: Sequence[dict[str, Any]]) -> dict[str, str]:
    """`P1…` → id-ul produsului, în ordinea dată (NX-324: modelul alege dintr-un enum închis)."""
    return {f"P{i}": str(p["id"]) for i, p in enumerate(products, 1) if p.get("id")}


def facts_block(inp: ComposeInput, pack: Any, language: str | None) -> str:
    """FAPTELE turului ca text: fișa întreagă a fiecărui produs, sub handle-ul lui, apoi
    regulile magazinului."""
    parts: list[str] = []
    for handle, product in zip(handles(inp.products), inp.products, strict=False):
        parts.append(f"PRODUCT {handle}\n{detail_answer.product_facts(product, pack, language)}")
    if inp.store_rules:
        parts.append("STORE RULES\n" + "\n".join(f"- {r}" for r in inp.store_rules))
    return "\n\n".join(parts)


def system_prompt(task: str, *, store: str, locale: str) -> str:
    """Nucleul + blocul sarcinii + regulile de voce. Fără nimic al turului: prefix cache-uibil."""
    return "\n\n".join((CORE.format(store=store, locale=locale), TASKS[task], VOICE_RULES.strip()))


def user_message(inp: ComposeInput, *, facts: str, history: str, message: str) -> str:
    """Mesajul `user`: sarcina, întrebarea, obligațiile, faptele, istoricul, apoi mesajul curent."""
    lines = [f"TASK: {inp.task}"]
    if inp.question:
        lines.append(f"QUESTION: {inp.question}")
    if inp.obligations:
        lines.append("OBLIGATIONS")
        for ob in inp.obligations:
            detail = json.dumps(dict(ob.facts), ensure_ascii=False) if ob.facts else ""
            lines.append(f"- {ob.code} {detail}".rstrip())
    else:
        lines.append("OBLIGATIONS: none")
    if inp.offered_moves:
        lines.append("OFFERED NEXT STEPS")
        lines.extend(f"- {m}" for m in inp.offered_moves)
    blocks = ["\n".join(lines), f"FACTS\n{facts or 'none'}"]
    blocks.append(f"HISTORY\n{history or 'none'}")
    blocks.append(f"CUSTOMER MESSAGE\n{message}")
    return "\n\n".join(blocks)


def schema(inp: ComposeInput) -> dict[str, Any]:
    """Schema strictă a răspunsului. Handle-urile și codurile obligațiilor sunt enumuri închise
    când există (o listă goală nu e un enum valid în `strict`)."""
    known = list(handles(inp.products))
    handle = {"type": "string", "enum": known} if known else {"type": "string"}
    codes = [o.code for o in inp.obligations]
    code = {"type": "string", "enum": codes} if codes else {"type": "string"}
    item = {
        "type": "object",
        "properties": {"handle": handle, "reason": {"type": "string"}},
        "required": ["handle", "reason"],
        "additionalProperties": False,
    }
    body = {
        "type": "object",
        "properties": {
            "text": {"type": "string"},
            "items": {"type": "array", "items": item},
            "suggestions": {"type": "array", "items": {"type": "string"}},
            "obligations_met": {"type": "array", "items": code},
        },
        "required": ["text", "items", "suggestions", "obligations_met"],
        "additionalProperties": False,
    }
    return {"name": SCHEMA_NAME, "strict": True, "schema": body}


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str | None = None


def parse(raw: Any, inp: ComposeInput) -> Composed | None:
    """Răspunsul modelului ca `Composed`, sau `None` dacă forma e greșită. Handle-urile se
    traduc în id-uri; unul necunoscut rămâne ca atare, ca poarta să-l prindă."""
    if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
        return None
    ids = handles(inp.products)
    items: list[tuple[str, str]] = []
    for it in raw.get("items") or []:
        if isinstance(it, dict) and isinstance(it.get("handle"), str):
            items.append((ids.get(it["handle"], it["handle"]), str(it.get("reason") or "").strip()))
    suggestions = tuple(
        s.strip() for s in (raw.get("suggestions") or []) if isinstance(s, str) and s.strip()
    )[:MAX_SUGGESTIONS]
    met = tuple(str(c) for c in raw.get("obligations_met") or [])
    return Composed(raw["text"].strip(), tuple(items), suggestions, met)


def check(composed: Composed, inp: ComposeInput, *, facts: str, units: frozenset[str]) -> Verdict:
    """Poarta compozitorului. PURĂ (în afara flagurilor citite de porțile refolosite)."""
    known = {str(p.get("id")) for p in inp.products}
    by_id = {str(p.get("id")): p for p in inp.products}
    for pid, _reason in composed.items:
        if pid not in known:
            return Verdict(False, "unknown_handle")
    verdict = detail_answer.check_answer(composed.reply, list(inp.products), facts, units)
    if not verdict.ok:
        return Verdict(False, verdict.reason)
    for pid, reason in composed.items:
        if reason:
            v = detail_answer.check_answer(reason, by_id[pid], facts, units)
            if not v.ok:
                return Verdict(False, v.reason)
    missing = {o.code for o in inp.obligations} - set(composed.obligations_met)
    if missing:
        return Verdict(False, "obligation_missing")
    return Verdict(True)


async def compose(
    ctx: TurnContext,
    deps: PipelineDeps,
    inp: ComposeInput,
    *,
    history: str,
) -> tuple[Composed | None, str | None]:
    """UN apel, validat pe fapte. `(None, motiv)` = apelantul servește rezerva de azi (motivul
    din `REASONS` sau `call_failed`). Emite `composer{task, outcome, reason?, prompt}`, vocabular
    închis, zero text de client."""
    pack = getattr(ctx.business, "domain_pack", None)
    locale = ctx.language or ""
    facts = facts_block(inp, pack, locale)
    system = system_prompt(inp.task, store=str(ctx.business.name or ""), locale=locale)
    user = user_message(inp, facts=facts, history=history, message=_message(ctx))
    emit = {"task": inp.task, "prompt": COMPOSER_PROMPT_VERSION}
    try:
        raw = await asyncio.wait_for(
            deps.llm.complete_schema(system, user, schema(inp)), timeout=COMPOSE_TIMEOUT_S
        )
    except Exception as e:  # noqa: BLE001 — P6: rezerva turului rămâne răspunsul
        ctx.emit("composer", outcome="call_failed", error=type(e).__name__, **emit)
        return None, "call_failed"
    composed = parse(raw, inp)
    if composed is None:
        ctx.emit("composer", outcome="rejected", reason="invalid_reply", **emit)
        return None, "invalid_reply"
    verdict = check(composed, inp, facts=facts, units=detail_answer.unit_words(pack))
    if not verdict.ok:
        ctx.emit("composer", outcome="rejected", reason=verdict.reason, **emit)
        return None, verdict.reason
    ctx.emit("composer", outcome="composed", **emit)
    return composed, None


def _message(ctx: TurnContext) -> str:
    """Mesajul curent al clientului, ca VALOARE spre model; nimic nu ramifică pe el."""
    return (ctx.message.body or "").strip()


__all__ = [
    "COMPOSER_PROMPT_VERSION",
    "COMPOSE_TIMEOUT_S",
    "CORE",
    "REASONS",
    "SCHEMA_NAME",
    "TASKS",
    "ComposeInput",
    "Composed",
    "Obligation",
    "Verdict",
    "check",
    "compose",
    "facts_block",
    "handles",
    "parse",
    "schema",
    "system_prompt",
    "user_message",
]
