"""Stagiul 4 (felia clarificare) — reluare DETERMINISTĂ a unei întrebări în așteptare.

Slot-filling fără LLM (NX-130): dacă turul anterior a pus o întrebare de clarificare
(`conversations.state.pending_question`), mesajul scurt al clientului umple slotul cerut
și rutăm determinist pe intenția de reluat — FĂRĂ să mai chemăm triajul (nano). Răspunsul
la o întrebare pe care NOI am pus-o nu mai costă un apel LLM (P2) și nu mai e re-clasificat
izolat (ex. „200 lei" → nu „ambiguu", ci „bugetul pentru căutarea de produse").

Rulează ÎNTRE `language_stage` și `greeting_stage`: răspunsul scurt NU trebuie tratat ca
salut, ca query de cache, nici re-triat de la zero. No-op dacă nu există slot în așteptare
sau mesajul e gol (P6 — pipeline-ul continuă normal).

NX-235 — două schimbări, ambele în spatele flagului:
  • reluarea se leagă de `question_id` (întrebarea EXACTĂ pusă), nu doar de numele slotului:
    două întrebări diferite pe aceeași cheie nu se mai confundă între ele;
  • răspunsul nu mai devine valoare de stare BRUTĂ. Trece ca PROPUNERE prin reducer, care îl
    normalizează canonic sau îl marchează `unknown` — text liber nu intră în memorie (P12).
    `ctx.state.constraints` rămâne populat pentru cititorii v1, ca turul curent să vadă slotul.

Aici trăiește și POARTA de clarificare (`clarification_gate`), chemată de triaj înainte să
întrebe: o întrebare fără câștig informațional, sau deja pusă, nu se mai pune.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from src.config import get_settings
from src.conversation.clarification_policy import (
    ClarificationCandidate,
    ClarificationDecision,
    ClarificationPolicy,
    decide_clarification,
)
from src.conversation.needs import NeedVocabulary
from src.conversation.state_reducer import StateUpdateProposal
from src.conversation.state_v2 import ConversationStateV2
from src.models import Route, RouteDecision, TurnContext
from src.web.action_models import action_command
from src.worker.canonicalize import canonicalize_clarify_field

if TYPE_CHECKING:
    from src.worker.runner import PipelineDeps

log = logging.getLogger(__name__)


def clarification_gate(
    ctx: TurnContext,
    key: str,
    *,
    reason: str = "missing_required",
    partition: tuple[int, ...] = (),
    total_candidates: int | None = None,
    options_refs: tuple[str, ...] = (),
) -> ClarificationDecision:
    """Merită întrebat? Decizia e a politicii (`clarification_policy`), nu a apelantului.

    Cu flagul stins întoarce mereu `ask=True` — comportamentul de dinainte de card, bit cu bit.
    Cu el aprins, o întrebare deja pusă de destule ori, una la care știm deja răspunsul sau una
    care n-ar tăia nimic din candidați se transformă în „răspunde cu ce ai și spune ce nu știi"."""
    settings = get_settings()
    state_v2 = ctx.state_v2
    if not settings.clarification_policy_v2_enabled or not isinstance(
        state_v2, ConversationStateV2
    ):
        return ClarificationDecision(True, None, "policy_off")
    policy = ClarificationPolicy(
        vocabulary=NeedVocabulary.from_pack(getattr(ctx.business, "domain_pack", None)),
        min_information_gain=settings.clarification_min_information_gain,
        max_attempts_per_key=settings.clarify_max_attempts,
    )
    decision = decide_clarification(
        state_v2,
        [
            ClarificationCandidate(
                key=key,
                reason=reason,  # type: ignore[arg-type]
                options_refs=options_refs,
                partition=partition,
            )
        ],
        total_candidates=total_candidates,
        policy=policy,
    )
    ctx.emit(
        "clarification_decision",
        decision="ask" if decision.ask else "answer",
        reason=decision.reason,
        information_gain_bucket=decision.gain_bucket,
    )
    return decision


async def clarify_resume_stage(ctx: TurnContext, deps: PipelineDeps) -> None:
    """Consumă `pending_question`: umple `constraints[field]` cu răspunsul brut și setează
    `ctx.route` pe `resume_route` (triajul devine no-op prin gardă pe `ctx.route`). NU setează
    reply — lasă agentul (sales) să răspundă cu slotul acum cunoscut. `pending_question` se
    curăță la writeback (reply non-clarify → slot None)."""
    if action_command(ctx) is not None:
        # NX-236: pe un turn de ACȚIUNE, reluarea aparține kernelului (rulează înaintea acestui
        # stagiu). El leagă răspunsul de `question_id` — întrebarea EXACTĂ peste care s-a emis
        # butonul — și închide clarificarea. Aici am avea doar textul opțiunii, fără dovada că e
        # răspunsul la întrebarea curentă: exact ambiguitatea pe care cardul o elimină.
        return
    pq = ctx.state.pending_question
    if not isinstance(pq, dict):
        return  # nimic în așteptare (sau state corupt) → mai departe în pipeline
    # O POZĂ (NX-76: gates îi injectează o descriere ca body) NU e răspuns la un slot de TEXT —
    # e o re-orientare către produs. Nu consuma slotul cu textul derivat din imagine; lasă
    # triajul/agentul să caute pe descriere, iar slotul rămâne în așteptare pentru un răspuns text.
    if ctx.message.content_type == "image":
        return
    answer = (ctx.message.body or "").strip()
    if not answer:
        return  # body gol (ex. media fără descriere) → nu consumăm slotul; rămâne pt data viitoare

    # 1. mesajul curent umple slotul cerut → memorie scurtă citită de context_blocks (state_block).
    field = canonicalize_clarify_field(pq.get("field"), ctx.business.domain_pack)
    ctx.state.constraints[field] = answer
    # NX-235: aceeași umplere, exprimată ca PROPUNERI typed. Reducerul normalizează răspunsul
    # (sau îl marchează `unknown`) și închide întrebarea pe `question_id` — memoria nu primește
    # utterance brut, iar întrebarea închisă nu mai poate fi „redeschisă" de un slot omonim.
    _propose_resume(ctx, field, answer, pq)
    # NX-112: marchează slotul ca „deja întrebat" (semnal anti-loop citit de context_blocks/NX-116).
    # Dedup + cap 8 (P4). Mutația pe ctx.state e persistată de processor (merge canonic, P3).
    if field not in ctx.state.asked_intents:
        ctx.state.asked_intents.append(field)
        ctx.state.asked_intents[:] = ctx.state.asked_intents[-8:]

    # NX-116: ANTI-BUCLĂ reală — `attempts` (scris de set_clarify la re-întrebarea ACELUIAȘI slot,
    # azi necitit) e consumat aici. Peste prag NU mai re-întrebăm la infinit (P6): mergem pe SALES
    # cu ce avem (agentul vede constraint-ul stocat). `pending_question` se curăță la writeback.
    #
    # Slotul „intent" (nu știm nici măcar ce vrea) trimitea înainte la HANDOFF. Fără operator,
    # asta ar fi însemnat o promisiune neonorată; cel mai bun răspuns pe care îl avem e tot un
    # răspuns, iar agentul poate întreba din nou în vocea lui dacă chiar nu are pe ce merge.
    attempts = int(pq.get("attempts") or 0)
    if attempts >= get_settings().clarify_max_attempts:
        ctx.route = RouteDecision(route=Route.SALES)
        # P12: fără `answer` în event (poate fi PII).
        ctx.emit("clarify_escalated", field=field, attempts=attempts, to="sales")
        return

    # 2. rutăm determinist pe intenția de reluat — fără triaj. `route` deja setat → triajul no-op.
    resume = pq.get("resume_route") or Route.SALES.value
    try:
        ctx.route = RouteDecision(route=Route(resume))
    except ValueError:
        ctx.route = RouteDecision(route=Route.SALES)  # rută veche/invalidă → default sales
    ctx.emit("clarify_resumed", field=field)  # FĂRĂ `answer` (P12 — poate fi PII)


def _propose_resume(ctx: TurnContext, field: str, answer: str, pq: dict) -> None:
    """Propunerile turului pentru un slot umplut: închide întrebarea, apoi setează nevoia.

    Ordinea contează — `resolve_question` scrie `asked_questions` (semnalul anti-buclă) chiar dacă
    răspunsul nu se normalizează în nimic canonic. Un „nu știu" tot închide întrebarea; altfel am
    re-întreba exact clientul care ne-a spus deja că nu are un răspuns."""
    if not isinstance(ctx.state_v2, ConversationStateV2):
        return
    pending = ctx.state_v2.pending_clarification
    question_id = pending.question_id if pending else pq.get("question_id")
    ctx.state_proposals.append(
        StateUpdateProposal(
            "resolve_question",
            key=field,
            question_id=question_id,
            source="user_explicit",
            turn_id=ctx.turn_id,
        )
    )
    ctx.state_proposals.append(
        StateUpdateProposal(
            "set_need", key=field, value=answer, source="user_explicit", turn_id=ctx.turn_id
        )
    )
