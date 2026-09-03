"""NX-239 — contractele MainBrain: obligațiile turului (extractor determinist) + `BrainInput`.

Obligațiile NU sunt o clasificare LLM: sunt SEMNALE extrase din cod (semne de întrebare, clauze
mixte, acțiune opacă, clarificare în așteptare, salut, context de siguranță). Modelul poate
propune obligations în plan, dar validatorul (`validate_answer_plan_v2`) le confruntă cu ce a
extras codul — deci un „primul reply câștigă" pe un mesaj mixt devine demonstrabil incomplet.

`BrainInput` e TOT ce vede MainBrain: construit numai din snapshot/state/istoric bugetat — fără
conexiune DB, fără istoric nelimitat, fără fapte venite din frontend (NX-234: contextul de pagină
e deja rehidratat canonic în snapshot). Owner al construcției: `worker.context.build_brain_input`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal

ObligationSource = Literal[
    "question",
    "mixed_clause",
    "action",
    "pending_action",
    "greeting",
    "safety_context",
    "route",
]

#: Fraze: păstrăm delimitatorul ca să știm dacă fraza a fost o ÎNTREBARE (splitul l-ar înghiți).
_SENT_SPLIT_RE = re.compile(r"([.!?;,]+)")
#: Conjuncții care despart clauze cu intenții potențial diferite (RO/EN/HU, fără diacritice).
_CONJ_SPLIT_RE = re.compile(r"\bsi\b|\bdar\b|\biar\b|\bapoi\b|\band\b|\bbut\b|\balso\b")

#: Verbe/forme imperative de ACȚIUNE comercială (adaugă/comandă/cumpără) — semnal `action` în text.
_ACTION_RE = re.compile(
    r"\badaug\w*\b|\bpune\w*\s+in\s+cos\b|\bcomand\w*\b|\bcumpar\w*\b|\bcheckout\b|\bo\s+iau\b"
    r"|\badd\s+to\s+cart\b|\bbuy\b|\border\s+it\b",
    re.IGNORECASE,
)

#: Cerere de comparație — obligație `compare` (distinctă de o recomandare simplă).
_COMPARE_RE = re.compile(
    r"\bcompar\w*\b|\bdiferent\w*\b|\bversus\b|\bvs\.?\b|\bcare\s+e\s+mai\s+bun\w*\b"
    r"|\bcompare\b|\bdifference\b",
    re.IGNORECASE,
)

#: Cerere imperativă de INFORMAȚIE („zi-mi/spune-mi când ajunge") — obligație `answer` chiar fără
#: semnul de întrebare (imperativul îl înlocuiește).
_TELL_ME_RE = re.compile(r"\bzi-?mi\b|\bspune-?mi\b|\bexplica-?mi\b|\btell\s+me\b", re.IGNORECASE)

#: Cerere de recomandare/căutare de produs (RO/EN, fără diacritice) — obligație `recommend`.
_RECOMMEND_RE = re.compile(
    r"\brecomand\w*\b|\bcaut\b|\bvreau\b|\bimi\s+trebuie\b|\bam\s+nevoie\b"
    # NX-273: „ce <substantiv> ai / aveți / recomanzi" — declanșatorul e VERBUL, nu substantivul.
    # Aici era o listă de produse de cosmetice („crema|ser|sampon|parfum"), adică exact scurgerea
    # pe care o descrie NX-264: pe un magazin de electrocasnice regexul ar fi fost mort, fără ca
    # nimic să pice. Cu verbul ca ancoră, „ce frigider aveți" se prinde la fel de bine.
    r"|\bce\s+\w+\s+(?:imi\s+|mi\s+)?(?:recomanzi|recomandati|aveti|ai|as\s+lua)\b"
    r"|\bsugest\w*\b|\brecommend\w*\b|\blooking\s+for\b|\bi\s+need\b",
    re.IGNORECASE,
)


def _norm(text: str) -> str:
    """Lowercase + fără diacritice (NFKD) — aceleași reguli ca stagiile deterministe."""
    decomposed = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


@dataclass(frozen=True, slots=True)
class DetectedObligation:
    """O obligație extrasă determinist. `key` = slug scurt stabil (intră în telemetrie + în
    `required_obligations` la validare), niciodată text liber al clientului."""

    kind: str  # ObligationKind (answer/recommend/compare/explain/action/clarify/safety)
    key: str
    source: ObligationSource


def _is_greeting_lead(first_clause: str) -> bool:
    """Prima clauză e un salut? Refolosește setul EXACT al stagiului greeting (lazy import ca să
    nu legăm modelele de worker la import)."""
    from src.worker.stages.greeting import is_greeting  # noqa: PLC0415 — evită cuplaj la import

    return is_greeting(first_clause)


def clause_spans(text: str) -> list[tuple[str, bool]]:
    """Clauzele ne-goale ale mesajului + dacă fiecare a fost o ÎNTREBARE. Determinist.

    Frazele se despart pe punctuație (delimitatorul păstrat spune dacă fraza s-a încheiat cu
    `?`); fiecare frază se mai desparte pe conjuncții (și/dar/iar/and/but) — marcajul de
    întrebare aparține ULTIMEI sub-clauze (acolo se termina fraza)."""
    parts = _SENT_SPLIT_RE.split(_norm(text))
    out: list[tuple[str, bool]] = []
    for i in range(0, len(parts), 2):
        segment = parts[i].strip()
        delim = parts[i + 1] if i + 1 < len(parts) else ""
        if not segment:
            continue
        subs = [s.strip() for s in _CONJ_SPLIT_RE.split(segment) if s.strip()]
        for j, sub in enumerate(subs):
            out.append((sub, "?" in delim and j == len(subs) - 1))
    return out


def split_clauses(text: str) -> list[str]:
    """Doar textele clauzelor (compat pt teste/consumatori care nu au nevoie de marcaj)."""
    return [clause for clause, _ in clause_spans(text)]


def extract_obligations(
    text: str,
    *,
    has_action: bool = False,
    pending_clarification: bool = False,
    safety_active: bool = False,
) -> tuple[DetectedObligation, ...]:
    """Semnalele deterministe ale turului → obligații. PURĂ (fără ctx/DB/LLM).

    Reguli:
    - o acțiune opacă (NX-236) e o obligație `action` prin construcție (mesajul e gol);
    - salutul e obligație DOAR dacă e urmat de altceva (un pur salut = o singură obligație);
    - fiecare clauză cu semnal propriu (întrebare/recomandare/comparație/acțiune) adaugă o
      obligație; clauzele fără semnal nu inventează nimic;
    - clarificarea în așteptare (slot pending) = obligația de a CONSUMA răspunsul;
    - contextul de siguranță activ = obligație `safety` (fraza de siguranță + filtrarea).
    """
    obligations: list[DetectedObligation] = []
    if has_action:
        obligations.append(DetectedObligation("action", "opaque_action", "action"))
    clauses = clause_spans(text)
    if clauses and _is_greeting_lead(clauses[0][0]):
        obligations.append(DetectedObligation("answer", "greeting", "greeting"))
        clauses = clauses[1:]
        if not clauses and not has_action and not pending_clarification:
            # Pur salut: o singură obligație (plus safety, dacă e activ) — fast path exact.
            if safety_active:
                obligations.append(DetectedObligation("safety", "safety_context", "safety_context"))
            return tuple(obligations)
    if pending_clarification:
        obligations.append(DetectedObligation("answer", "pending_clarification", "pending_action"))

    seen: set[tuple[str, str]] = set()
    for idx, (clause, is_question) in enumerate(clauses):
        source: ObligationSource = "mixed_clause" if len(clauses) > 1 else "question"
        if _COMPARE_RE.search(clause):
            kind, key = "compare", "compare"
        elif _ACTION_RE.search(clause):
            kind, key = "action", "commerce_action"
        elif _RECOMMEND_RE.search(clause):
            kind, key = "recommend", f"recommend_{min(idx, 3)}"
        elif (
            is_question
            or _TELL_ME_RE.search(clause)
            or clause.startswith(
                ("ce ", "cat ", "cum ", "unde ", "cand ", "care ", "de ce ", "aveti ", "ai ")
            )
        ):
            kind, key = "answer", f"question_{min(idx, 3)}"
        else:
            continue
        if (kind, key) not in seen:
            seen.add((kind, key))
            obligations.append(DetectedObligation(kind, key, source))

    # Mesaj non-gol în care NICIO clauză n-a produs nimic (o afirmație, o rafinare) = tot ceva de
    # răspuns. Dacă o clauză a produs deja o obligație (ex. `action`), nu inventăm încă una.
    clause_hits = any(o.source in ("question", "mixed_clause") for o in obligations)
    if not clause_hits and _norm(text).strip():
        obligations.append(DetectedObligation("answer", "question_0", "question"))
    if safety_active:
        obligations.append(DetectedObligation("safety", "safety_context", "safety_context"))
    return tuple(obligations)


def obligations_from_ctx(ctx: Any) -> tuple[DetectedObligation, ...]:
    """Extractorul, legat la semnalele REALE ale turului. Best-effort pe safety (fail-safe)."""
    try:
        from src.safety.policy import SafetyPolicy  # noqa: PLC0415 — evită ciclu la import

        safety_active = bool(SafetyPolicy.for_turn(ctx).contexts)
    except Exception:  # noqa: BLE001 — polița stricată → tratăm ca sensibil (fail-safe)
        safety_active = True
    pending = getattr(ctx.state, "pending_question", None)
    return extract_obligations(
        ctx.message.body or "",
        has_action=getattr(ctx, "action", None) is not None,
        pending_clarification=isinstance(pending, dict) and bool(pending),
        safety_active=safety_active,
    )


@dataclass(frozen=True, slots=True)
class BrainSignal:
    """Un reply de stagiu DEMOTE-uit de control plane: nu mai iese la client, devine context
    pentru MainBrain (ex. răspunsul FAQ pe un mesaj mixt). Fără PII nou — textul e cel pe care
    stagiul l-ar fi trimis oricum clientului."""

    stage: str
    kind: str  # "faq_answer" | "cache_answer" | "triage_reply" | "stage_reply"
    text: str
    suggestions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BrainInput:
    """Intrarea COMPLETĂ a MainBrain — numai din snapshot/state/istoric bugetat (NX-234/235).

    Fără conexiune DB, fără istoric nelimitat (transcriptul e deja bugetat de
    `conversation_transcript`), fără fapte de frontend. `tool_schema_version` + versiunea de
    prompt intră în evenimente (trace attrs), ca răspunsul să fie reproductibil."""

    business_id: str
    locale: str
    message: str
    obligations: tuple[DetectedObligation, ...]
    history: str  # transcript bugetat (poate fi gol)
    context: str  # blocurile de context (rezumat/profil/facts/state) — deja PII-redactate
    signals: tuple[BrainSignal, ...] = ()
    category_hint: str | None = None
    filters_hint: str = ""
    active_need_keys: tuple[str, ...] = ()
    hard_need_keys: tuple[str, ...] = ()
    revoked_need_keys: tuple[str, ...] = ()
    evidence_budget: int = 24


__all__ = [
    "BrainInput",
    "BrainSignal",
    "DetectedObligation",
    "extract_obligations",
    "obligations_from_ctx",
    "split_clauses",
]
