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
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal

from src.agent import detail_answer
from src.agent.voice import VOICE_RULES

if TYPE_CHECKING:
    from src.models import RichReply, TurnContext
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
#: Sarcinile pe care sfatul general de folosire are sens (despre produse).
_ADVICE_TASKS = frozenset({"detail", "recommend", "compare", "not_found"})
#: Motivele porții (vocabular închis, evenimentul `composer{outcome, reason}`).
REASONS = (
    *detail_answer.REASONS,
    "unknown_handle",
    "obligation_missing",
    "invalid_reply",
    "ungrounded_rule",
    "not_a_question",
    "safety_referral_missing",
)
#: NX-382 faza 2c: codul obligației de siguranță (contextul declarat de client, NX-173).
SAFETY_REFERRAL = "safety_referral"

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
it is standard practice and not about a health condition. Put it in `general_advice`, never in \
`text` or in a reason: `text` and reasons hold only what FACTS say about the products, and \
`general_advice` never names a product, a price or what a product contains.
- When the customer asks for something no fact can confirm, do not refuse the items: keep the ones \
that fit the rest of the request, drop only those whose facts contradict it, and say once, \
briefly, what you cannot guarantee.
- Never say a product treats, cures or is safe for a condition. For a health, allergy, pregnancy \
or breastfeeding question, write only that a doctor or a pharmacist should confirm, in a sentence \
of its own; with the obligation `safety_referral` that sentence is the referral (one, not two), \
and with a SAFETY NOTE in FACTS you write none.
- Prices only as written in FACTS. No links in the text, no promises about delivery or stock that \
FACTS do not state, no colleague or human operator (there is none).
- Short paragraphs. No headings. A list only for steps.

OBLIGATIONS
Each obligation listed for this turn must be in the reply, in your own words and where it reads \
naturally; list its code in `obligations_met`. An obligation already said in HISTORY is said again \
only if it still matters, and then in a few words.
- `safety_referral`: the customer told you about a situation (its facts name it). Say it in every \
reply, in one sentence of its own near the start of `text`: name the situation, say that you \
cannot confirm what suits it, and that they should check their choice with their doctor or \
pharmacist (name both). Never say that a product is safe or suitable for it. When `already_told` \
is true, a short reminder that still names the situation, the doctor and the pharmacist is \
enough.

OUTPUT
`text` is the reply. `general_advice` is the general know-how that completes it (one or two \
sentences, shown right after `text`), or empty. `items` (only when TASK asks for it) is one entry \
per product you present, with its handle and its `reason`. `suggestions` phrases, as the customer \
would type them, the next steps listed in OFFERED NEXT STEPS (at most one each, short), or stays \
empty. `obligations_met` lists the obligation codes your text covers."""

TASKS: Mapping[str, str] = {
    "recommend": (
        "TASK recommend: the customer wants items. ITEMS are the products found for this turn, by "
        "handle. Choose the ones that answer the request (all of them when they do). `text` is the "
        "introduction (2-4 sentences: what you found for what they asked, what to look for in this "
        "kind of product given their situation, and the one difference that decides between the "
        "items); do not walk through every item, each has its own reason. Per chosen item, "
        "`reason` is 1-2 sentences: why this one for THIS customer, the mechanism in plain "
        "words, one concrete detail from its sheet. When items are of a different kind than what "
        "was asked, say so and do not pretend they fit."
    ),
    "detail": (
        "TASK detail: the customer asks about one product, P1; its card is shown under your text. "
        "When QUESTION is given, answer it from P1's facts in 1-3 sentences (general know-how that "
        "helps goes in `general_advice`). Without a question, tell what matters most about it "
        "for this customer in 3-5 sentences (what it is for, how it feels, how to use it, what "
        "customers say), not its whole sheet. `items` stays empty."
    ),
    "compare": (
        "TASK compare: start with the answer to what the customer asked (which is more X, how they "
        "differ in practice). If the items do different jobs, say each one's role and whether they "
        "go together first. Then the 2-3 differences that matter for this customer, then which one "
        "you would take for which kind of person."
    ),
    "store_info": (
        "TASK store_info: answer from STORE RULES. A sentence that gives a number from a rule "
        "(an amount, a threshold, a number of days or hours) is copied from that rule word for "
        "word, as one whole sentence; the rest you say in your own words. What the rules do not "
        "cover, say briefly that you do not know; do not ask the customer to rephrase."
    ),
    "ask": (
        "TASK ask: you need one answer before acting. The obligation `ask` carries the question "
        "code needs answered, with its options; the candidate products, if any, are shown as "
        "cards. Ask it in one short, natural sentence that names the options (short product names) "
        "so the customer can answer in a word, in the order they are given. No other content, "
        "apart from what another obligation requires."
    ),
    "not_found": (
        "TASK not_found: the product the customer named is not in the catalog as such. Say so in a "
        "few words, then present the closest ITEMS as in a recommendation."
    ),
    "no_results": (
        "TASK no_results: nothing in the catalog matches what was searched (obligation "
        "`nothing_found`, with what was searched for). Say so plainly and concretely, then offer "
        "one or two ways forward that the customer can take: drop or loosen one requirement, a "
        "related kind of product, or a looser budget (without naming a new amount). Never say "
        "what the store sells in general, and never claim that a product exists."
    ),
    "cart": (
        "TASK cart: the obligation `cart_change` says what was put in the cart (`added`, by short "
        "name) and how many additions failed (`failed`). Confirm it in one sentence; if something "
        "failed, say so plainly, without guessing why. ITEMS, when given, are products that "
        "usually go with what they took: present only those that really complete it (one or two, "
        "in `items`, each `reason` saying why it goes with what they took), or none; never present "
        "an item as part of the cart. No checkout link, no price for the cart."
    ),
    "order": "TASK order: say the order status plainly, or why login is needed and how.",
    "chitchat": (
        "TASK chitchat: a greeting, thanks, goodbye or small talk, with no request. Answer it in "
        "one or two short sentences that fit the conversation so far (a goodbye after a choice is "
        "a goodbye, not a new pitch). Offer help only when the conversation has not started yet. "
        "No product facts, no prices, no promises."
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
    #: Blocuri de fapte în plus, cu titlul lor (faza 2c: nota de siguranță; faza 3: sursele
    #: citabile ale comparației).
    extra_facts: Sequence[tuple[str, str]] = ()


@dataclass(frozen=True)
class Composed:
    reply: str
    advice: str  # sfatul general (NX-382, recenzia): permis, dar separat de faptele produsului
    items: tuple[tuple[str, str], ...]  # (product_id, reason), în ordinea modelului
    suggestions: tuple[str, ...]
    obligations_met: tuple[str, ...]

    @property
    def served(self) -> str:
        """Ce citește clientul: răspunsul, apoi sfatul general, ca ultim paragraf."""
        return f"{self.reply}\n\n{self.advice}" if self.advice else self.reply


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
    told = [ob for ob in inp.obligations if ob.facts]
    if told:
        # faptele obligațiilor (ce s-a căutat, întrebarea porții) sunt și ele fapte ale turului:
        # un răspuns care le redă e întemeiat
        lines = [f"- {ob.code}: {json.dumps(dict(ob.facts), ensure_ascii=False)}" for ob in told]
        parts.append("OBLIGATION FACTS\n" + "\n".join(lines))
    parts.extend(f"{title}\n{body}" for title, body in inp.extra_facts if body)
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
    blocks.append(f"HISTORY (context only, not a source of facts)\n{history or 'none'}")
    blocks.append(f"CUSTOMER MESSAGE\n{message}")
    return "\n\n".join(blocks)


def schema(inp: ComposeInput) -> dict[str, Any]:
    """Schema strictă a răspunsului. Handle-urile și codurile obligațiilor sunt enumuri închise
    când există (o listă goală nu e un enum valid în `strict`; codurile, fără dubluri)."""
    known = list(handles(inp.products))
    handle = {"type": "string", "enum": known} if known else {"type": "string"}
    codes = list(dict.fromkeys(o.code for o in inp.obligations))
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
            "general_advice": {"type": "string"},
            "items": {"type": "array", "items": item},
            "suggestions": {"type": "array", "items": {"type": "string"}},
            "obligations_met": {"type": "array", "items": code},
        },
        "required": ["text", "general_advice", "items", "suggestions", "obligations_met"],
        "additionalProperties": False,
    }
    return {"name": SCHEMA_NAME, "strict": True, "schema": body}


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str | None = None


def parse(raw: Any, inp: ComposeInput) -> Composed | None:
    """Răspunsul modelului ca `Composed`, sau `None` dacă forma e greșită. Handle-urile se
    traduc în id-uri (unul necunoscut rămâne ca atare, ca poarta să-l prindă). Chips-urile: cel
    mult câte un text pe pas OFERIT de cod (NX-296: un chip e o mutare oferită, nu o idee a
    modelului), trecute prin plasa de voce (P13)."""
    from src.agent.voice import naturalize  # noqa: PLC0415

    if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
        return None
    ids = handles(inp.products)
    items: list[tuple[str, str]] = []
    for it in raw.get("items") or []:
        if isinstance(it, dict) and isinstance(it.get("handle"), str):
            items.append((ids.get(it["handle"], it["handle"]), str(it.get("reason") or "").strip()))
    cap = min(len(inp.offered_moves), MAX_SUGGESTIONS)
    suggestions = tuple(
        naturalize(s.strip())
        for s in (raw.get("suggestions") or [])
        if isinstance(s, str) and s.strip()
    )[:cap]
    met = tuple(str(c) for c in raw.get("obligations_met") or [])
    advice = raw.get("general_advice")
    advice = advice.strip() if isinstance(advice, str) else ""
    return Composed(raw["text"].strip(), advice, tuple(items), suggestions, met)


_SPLIT = re.compile(r"(?<=[.!?])\s+")
#: Un fragment care se termină într-o abreviere scurtă cu majusculă („DR.", „Dr.") continuă
#: propoziția: numele de brand nu se taie (recenzia NX-382, clasa „DR. SKINTEGRA").
_ABBREVIATION = re.compile(r"\b[A-Z][A-Za-z]{0,3}\.$")


def sentences(text: str) -> list[str]:
    """Propozițiile unui text, fără să taie după o abreviere de nume. PUR."""
    out: list[str] = []
    for part in _SPLIT.split(text or ""):
        if out and _ABBREVIATION.search(out[-1]):
            out[-1] = f"{out[-1]} {part}"
        elif part.strip():
            out.append(part)
    return out


def _without_medical(text: str) -> tuple[str, int]:
    """Scoate propozițiile cu claim medical (P0, `has_medical_claim`), una câte una: o fișă de
    produs pentru acnee sau mătreață nu pierde tot răspunsul pentru o propoziție. Întoarce textul
    rămas și câte s-au scos."""
    from src.worker.text_scrub import has_medical_claim  # noqa: PLC0415

    kept = [s for s in sentences(text) if not has_medical_claim(s)]
    dropped = len(sentences(text)) - len(kept)
    return " ".join(kept).strip(), dropped


def _advice_ok(advice: str, products: Sequence[dict[str, Any]]) -> bool:
    """Sfatul general nu e o afirmație despre produs: fără preț, fără link, fără numele unui
    produs al turului (numele scurt, ca pe card). Cifrele unei rutine sunt permise."""
    from src.agent.validator import _PRICE_RE, _URL_RE  # noqa: PLC0415
    from src.catalog.folding import fold_text  # noqa: PLC0415
    from src.catalog.render_text import display_name  # noqa: PLC0415

    if _PRICE_RE.search(advice) or _URL_RE.search(advice):
        return False
    folded = fold_text(advice)
    for p in products:
        name = fold_text(display_name(str(p.get("name") or "")))
        if name and name in folded:
            return False
    return True


def rule_prices(rules: Sequence[str]) -> frozenset[float]:
    """Numerele scrise în regulile magazinului (pragul de livrare, costul returului, capetele unei
    plaje „între 19,9 și 24,90 lei"): o sumă care le redă e întemeiată ca sumă. Ce regulă o
    poartă și lângă ce cuvinte o judecă `rule_numbers_ok`."""
    return frozenset(v for rule in rules for v, _start, _end in _numbers_in(rule))


def _numbers_in(text: str) -> list[tuple[float, int, int]]:
    out: list[tuple[float, int, int]] = []
    for m in re.finditer(r"\d+(?:[.,]\d+)?", text or ""):
        try:
            out.append((round(float(m.group().replace(",", ".")), 4), m.start(), m.end()))
        except ValueError:
            continue
    return out


def rule_numbers_ok(reply: str, rules: Sequence[str]) -> bool:
    """Recenzia fazei 2: pe un răspuns despre magazin, o propoziție cu o cifră (sumă, prag, zile,
    ore) e acceptată doar CITATĂ LITERAL dintr-o regulă (`validator.strip_quoted`, NX-346).
    Regulile SOLE pun mai multe cifre în aceeași frază («peste 199 lei… pragul scade la 149»), deci
    o parafrază poate lega o cifră reală de alt sens («gratuită la prima comandă peste 149 lei»,
    «returul costă 24,90 lei», «14 zile pentru retur»), iar nicio potrivire pe cuvinte nu le
    deosebește. Propozițiile fără cifre rămân libere, în cuvintele modelului. PUR."""
    from src.agent.validator import strip_quoted  # noqa: PLC0415

    answers = [r.split(" -> ", 1)[-1] for r in rules]
    return not _numbers_in(strip_quoted(reply, answers))


def _obligation_amounts(obligations: Sequence[Obligation]) -> frozenset[float]:
    """Sumele numerice din faptele obligațiilor (bugetul căutat): un răspuns care le redă e
    întemeiat."""
    out: set[float] = set()
    for ob in obligations:
        if ob.code in _NO_AMOUNTS:
            # recenzia 2c: cifrele din numele adăugate în coș («Heartleaf 77») și numărul celor
            # picate nu sunt sume; altfel «costă 77 lei» sau «0 lei de plată» ar trece ca fapt
            continue
        for v in ob.facts.values():
            if isinstance(v, int | float) and not isinstance(v, bool):
                out.add(float(v))
            elif isinstance(v, str):
                out.update(n for n, _s, _e in _numbers_in(v))
    return frozenset(out)


#: Obligațiile ale căror fapte nu poartă sume (cifrele lor rămân permise ca text, prin FAPTE).
_NO_AMOUNTS = frozenset({"cart_change", "safety_referral"})


def check(
    composed: Composed,
    inp: ComposeInput,
    *,
    facts: str,
    units: frozenset[str],
    locale: str | None = None,
    safety_contexts: frozenset[str] = frozenset(),
) -> tuple[Verdict, Composed]:
    """Poarta compozitorului. PURĂ (în afara flagurilor citite de porțile refolosite). Întoarce
    verdictul și răspunsul CURĂȚAT: propozițiile cu claim medical scoase (P0), sfatul general
    scos dacă numește un produs, un preț sau un link. `safety_contexts` = contextele declarate ale
    turului (id-uri NX-173): răspunsul trebuie să le numească lângă trimiterea la medic."""
    known = {str(p.get("id")) for p in inp.products}
    by_id = {str(p.get("id")): p for p in inp.products}
    for pid, _reason in composed.items:
        if pid not in known:
            return Verdict(False, "unknown_handle"), composed
    reply, _dropped = _without_medical(composed.reply)
    if not reply:
        return Verdict(False, "medical_claim"), composed
    grounded = rule_prices(inp.store_rules) | _obligation_amounts(inp.obligations)
    verdict = detail_answer.check_answer(
        reply,
        list(inp.products),
        facts,
        units,
        grounded,
        # o regulă a magazinului nu e o afirmație de stoc despre un produs; în rest, fără produse,
        # orice „avem pe stoc" e nefondat (recenzia fazei 2)
        check_stock=inp.task != "store_info",
    )
    if verdict.ok and inp.task == "store_info" and not rule_numbers_ok(reply, inp.store_rules):
        verdict = Verdict(False, "ungrounded_rule")
    if verdict.ok and inp.task == "ask" and "?" not in reply:
        verdict = Verdict(False, "not_a_question")
    if not verdict.ok:
        return Verdict(False, verdict.reason), composed
    items: list[tuple[str, str]] = []
    for pid, reason in composed.items:
        reason, _ = _without_medical(reason)
        if reason:
            v = detail_answer.check_answer(reason, by_id[pid], facts, units)
            if not v.ok:
                return Verdict(False, v.reason), composed
        items.append((pid, reason))
    advice, _ = _without_medical(composed.advice)
    if advice and (inp.task not in _ADVICE_TASKS or not _advice_ok(advice, inp.products)):
        # sfatul general e despre FOLOSIREA produselor; pe magazin, întrebare sau „n-am găsit" ar fi
        # un canal pentru o regulă inventată (recenzia fazei 2)
        advice = ""
    missing = {o.code for o in inp.obligations} - set(composed.obligations_met)
    if missing:
        return Verdict(False, "obligation_missing"), composed
    if (
        any(o.code == SAFETY_REFERRAL for o in inp.obligations)
        and referral_sentence(reply, safety_contexts, locale) is None
    ):
        # P0: declarația nu ajunge; trimiterea la medic sau farmacist trebuie să fie în text, după
        # scoaterea propozițiilor medicale (o trimitere care afirmă că un produs e sigur a căzut)
        return Verdict(False, "safety_referral_missing"), composed
    cleaned = Composed(reply, advice, tuple(items), composed.suggestions, composed.obligations_met)
    return Verdict(True), cleaned


def referral_sentence(
    text: str, contexts: frozenset[str] = frozenset(), locale: str | None = None
) -> str | None:
    """Prima propoziție din `text` care TRIMITE la medic sau farmacist (`safety.compose.
    is_model_referral`: amprenta farmacistului, medicul, fără negație chiar înainte), sau `None`.
    Recenzia 2c: amprenta singură primea «Nu e nevoie să mergi la farmacist» și «Farmacistul tău o
    să fie mulțumit». Cu `contexts`, textul trebuie să și numească fiecare context declarat
    (aceleași tipare ale registrului NX-173 care îl detectează la client). PUR."""
    from src.safety.compose import is_model_referral  # noqa: PLC0415
    from src.safety.contraindications import detect_contexts  # noqa: PLC0415

    if contexts and not contexts <= detect_contexts(text):
        return None
    return next((s for s in sentences(text) if is_model_referral(s, locale)), None)


def safety_obligation(ctx: TurnContext) -> Obligation | None:
    """Obligația de siguranță a turului, din decizia ACUMULATĂ (`ctx.safety_decision`, NX-173): un
    context declarat ⇒ trimiterea la medic sau farmacist e a modelului, verificată de poartă. Pe
    registrul indisponibil (fail-closed) fraza rămâne a codului: nu există o decizie de povestit.
    Faptele sunt etichetele localizate ale contextului și ale celor lăsate deoparte, nu id-uri.

    `already_told` doar cu `SAFETY_REFERRAL_SHORT_AFTER_FIRST` (decizia lui Adi, implicit stins:
    fraza întreagă pe fiecare tur, ca azi) și doar când un răspuns anterior a trimis deja la
    medic.

    Recenzia 2c (P0): când decizia a EXCLUS produse, fraza rămâne a codului (recunoaștere + ce s-a
    lăsat deoparte + trimiterea): ce s-a lăsat deoparte și „nu spune că lipsește din magazin"
    (NX-367) nu se pot verifica pe textul modelului. Modelul primește doar nota `safety_note`, ca
    să nu scrie el trimiterea și să nu nege excluderea."""
    decision = _safety_decision(ctx)
    if decision is None or decision.blocked:
        return None
    from src.config import get_settings  # noqa: PLC0415
    from src.safety import messages  # noqa: PLC0415

    locale = ctx.language or ""
    facts: dict[str, Any] = {
        "situation": [messages.context_label(c, locale) for c in decision.contexts]
    }
    if get_settings().safety_referral_short_after_first and _referred_before(ctx):
        facts["already_told"] = True
    return Obligation(SAFETY_REFERRAL, facts)


def _safety_decision(ctx: TurnContext) -> Any:
    """Decizia de siguranță a turului, dacă e una pe care compozitorul o poate povesti: flagul
    aprins, context declarat, registrul valid (pe cel indisponibil, fail-closed, fraza e a
    codului)."""
    from src.config import get_settings  # noqa: PLC0415

    decision = getattr(ctx, "safety_decision", None)
    if not get_settings().composer_safety_enabled:
        return None
    if decision is None or not decision.must_refer or decision.unavailable:
        return None
    return decision


def safety_note(ctx: TurnContext) -> tuple[str, str] | None:
    """Recenzia 2c: pe o decizie care a EXCLUS produse, fraza de siguranță e a codului, pusă
    înaintea răspunsului. Modelul o află ca fapt, ca să nu scrie a doua trimitere și să nu spună
    că magazinul n-are ce s-a exclus (NX-367, c6)."""
    decision = _safety_decision(ctx)
    if decision is None or not decision.blocked:
        return None
    from src.safety import messages  # noqa: PLC0415

    situation = ", ".join(messages.context_label(c, ctx.language or "") for c in decision.contexts)
    return (
        "SAFETY NOTE",
        f"The customer told you: {situation}. {len(decision.blocked_ids)} products found for "
        "this turn were left out for that reason. A sentence about it, with the referral to a "
        "doctor or pharmacist, is added before your text by the store: do not write a referral "
        "yourself, and never say that the store does not have what was left out.",
    )


def _referred_before(ctx: TurnContext) -> bool:
    """Un răspuns ANTERIOR al botului a trimis deja la medic sau farmacist. Citește doar textul
    botului din istoric (amprenta NX-173), niciodată cuvintele clientului."""
    from src.models import Author  # noqa: PLC0415
    from src.safety.compose import already_has_sentence  # noqa: PLC0415

    return any(m.author == Author.BOT and already_has_sentence(m.body) for m in ctx.history or [])


async def compose(
    ctx: TurnContext,
    deps: PipelineDeps,
    inp: ComposeInput,
    *,
    history: str,
) -> tuple[Composed | None, str | None]:
    """UN apel, validat pe fapte. `(None, motiv)` = apelantul servește rezerva de azi (motivul
    din `REASONS` sau `call_failed`). Emite `composer{task, outcome, reason?, prompt}`, vocabular
    închis, zero text de client.

    Faza 2c: pe un context de siguranță declarat (NX-173), obligația `safety_referral` se adaugă
    aici, pe orice sarcină, iar propoziția verificată se ține minte pe tur
    (`ctx.safety_referral_composed`): `safety.compose.enforce` nu mai pune fraza codului peste
    ea."""
    pack = getattr(ctx.business, "domain_pack", None)
    locale = ctx.language or ""
    safety = safety_obligation(ctx)
    if safety is not None and all(o.code != SAFETY_REFERRAL for o in inp.obligations):
        inp = replace(inp, obligations=(*inp.obligations, safety))
    note = safety_note(ctx)
    if note is not None:
        inp = replace(inp, extra_facts=(*inp.extra_facts, note))
    contexts = frozenset(ctx.safety_decision.contexts) if safety is not None else frozenset()
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
    verdict, cleaned = check(
        composed,
        inp,
        facts=facts,
        units=detail_answer.unit_words(pack),
        locale=locale,
        safety_contexts=contexts,
    )
    if not verdict.ok:
        ctx.emit("composer", outcome="rejected", reason=verdict.reason, **emit)
        return None, verdict.reason
    if safety is not None:
        ctx.safety_referral_composed = referral_sentence(cleaned.reply, contexts, locale)
    ctx.emit(
        "composer",
        outcome="composed",
        trimmed=cleaned.reply != composed.reply or cleaned.advice != composed.advice,
        advice=bool(cleaned.advice),
        **({"safety": True} if safety is not None else {}),
        **emit,
    )
    return cleaned, None


def rich_reply(
    ctx: TurnContext, composed: Composed, products: Sequence[dict[str, Any]]
) -> RichReply | None:
    """Răspunsul cu carduri: cardurile ALESE de model (`items`, în ordinea lui, fiecare o dată),
    hidratate din catalog pe același drum ca recomandarea v1 (`compose.hydrated_item`: preț, preț de
    listă, rating, badge, link, variante), cu motivul lui, deja verificat pe fapte. Textul e
    `intro`, sfatul general `education`, chips-urile pașii oferiți și formulați de model. Fără
    nicio scurtare pe liste de cuvinte: poarta compozitorului a judecat deja. `None` = modelul n-a
    ales niciun produs."""
    from src.config import card_slots, get_settings  # noqa: PLC0415
    from src.models import RichReply  # noqa: PLC0415
    from src.worker import compose as wc  # noqa: PLC0415 — ciclul worker ↔ agent

    by_id = {str(p.get("id")): p for p in products if p.get("id")}
    items: list[Any] = []
    kinds: dict[str, str | None] = {}
    for pid, reason in composed.items:
        p = by_id.get(pid)
        if p is None or pid in kinds:
            continue
        item, kinds[pid] = wc.hydrated_item(ctx, p, reason=reason or None)
        items.append(item)
    if not items:
        return None
    items = wc.suppress_common_badges(ctx, items[: card_slots()], kinds)
    return RichReply(
        intro=composed.reply,
        items=items,
        pick=None,
        education=composed.advice or None,
        chips=wc._suggestion_chips(list(composed.suggestions)),
        disclaimer=(wc.disclaimer(ctx.language) if get_settings().ai_disclaimer_enabled else None),
    )


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
