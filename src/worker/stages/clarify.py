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
from src.worker.state_writes import apply_v1_view

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
    # NX-396: într-o conversație a agentului unic, stratul ăsta nu răspunde: agentul e calea
    # principală și scrie tot textul. Import leneș: flag stins = zero import.
    if get_settings().assistant_agent_enabled:
        from src.assistant.mode import owns_turn  # noqa: PLC0415

        if owns_turn(ctx, deps, get_settings()):
            return
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
    # NX-235: umplerea e exprimată ca PROPUNERI typed. Reducerul normalizează răspunsul (sau îl
    # marchează `unknown`) și închide întrebarea pe `question_id` — memoria nu primește utterance
    # brut, iar întrebarea închisă nu mai poate fi „redeschisă" de un slot omonim.
    # NX-331: propunerile sunt SURSA și pentru forma v1: slotul (`constraints`) și semnalul
    # anti-buclă (`asked_intents`, NX-112, dedup + cap 8) se derivă din ele. Întrebarea în
    # așteptare rămâne până la scriere: `set_clarify` îi citește `attempts` în același tur.
    field = canonicalize_clarify_field(pq.get("field"), ctx.business.domain_pack)
    resolved, filled = _resume_proposals(ctx, field, answer, pq)
    apply_v1_view(ctx, filled)
    apply_v1_view(ctx, resolved)
    if isinstance(ctx.state_v2, ConversationStateV2):
        ctx.state_proposals += [resolved, filled]

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


def _resume_proposals(
    ctx: TurnContext, field: str, answer: str, pq: dict
) -> tuple[StateUpdateProposal, StateUpdateProposal]:
    """Propunerile turului pentru un slot umplut: închide întrebarea, apoi setează nevoia.

    Ordinea contează — `resolve_question` scrie `asked_questions` (semnalul anti-buclă) chiar dacă
    răspunsul nu se normalizează în nimic canonic. Un „nu știu" tot închide întrebarea; altfel am
    re-întreba exact clientul care ne-a spus deja că nu are un răspuns."""
    state_v2 = ctx.state_v2 if isinstance(ctx.state_v2, ConversationStateV2) else None
    pending = state_v2.pending_clarification if state_v2 is not None else None
    question_id = pending.question_id if pending else pq.get("question_id")
    return (
        StateUpdateProposal(
            "resolve_question",
            key=field,
            question_id=question_id,
            source="user_explicit",
            turn_id=ctx.turn_id,
        ),
        StateUpdateProposal(
            "set_need", key=field, value=answer, source="user_explicit", turn_id=ctx.turn_id
        ),
    )
